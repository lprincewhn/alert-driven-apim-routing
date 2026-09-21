"""Minimal Azure Public Cloud ARM client; never surface response bodies or tokens."""

import json
import math
import subprocess
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ARM = "https://management.azure.com"


class AzureError(RuntimeError):
    def __init__(self, message, status=None, uncertain=False):
        self.status = status
        self.uncertain = uncertain
        super().__init__(message + (f" (HTTP {status})" if status else ""))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, method, body=None, headers=None, timeout=90, error_response=False):
    data = None if body is None else json.dumps(body).encode()
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    try:
        with build_opener(NoRedirect).open(
                Request(url, data=data, method=method, headers=request_headers), timeout=timeout) as response:
            raw = response.read()
            return (json.loads(raw) if raw else {}), dict(response.headers), response.status
    except HTTPError as error:
        if error_response:
            try:
                value = json.loads(error.read())
            except (ValueError, OSError):
                value = {}
            return value, dict(error.headers), error.code
        raise AzureError("HTTP operation failed; response details suppressed", error.code,
                         uncertain=method != "GET" and error.code >= 500) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise AzureError("HTTP operation failed; details suppressed", uncertain=method != "GET") from None


def header(headers, name):
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def retry_delay(headers):
    raw = header(headers, "Retry-After")
    if raw is None:
        return 2.0
    try:
        delay = float(raw)
        if not math.isfinite(delay):
            raise AzureError("Invalid Retry-After response header; details suppressed", uncertain=True)
        return max(0.0, delay)
    except (ValueError, TypeError):
        try:
            return max(0.0, (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            raise AzureError("Invalid Retry-After response header; details suppressed", uncertain=True) from None


def polling_url(headers):
    for name in ("Azure-AsyncOperation", "Operation-Location", "Location"):
        value = header(headers, name)
        if value:
            try:
                url = urljoin(ARM + "/", value)
                parsed = urlsplit(url)
                valid = (parsed.scheme == "https" and parsed.hostname == "management.azure.com"
                         and not parsed.username and not parsed.password and not parsed.fragment
                         and parsed.port in (None, 443))
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise AzureError("Unsafe ARM polling URL; refusing to send credentials", uncertain=True)
            return url, name.lower() != "location"
    return None, False


def operation_state(value):
    if not isinstance(value, dict):
        return ""
    properties = value.get("properties")
    provisioning = properties.get("provisioningState") if isinstance(properties, dict) else None
    return str(value.get("status") or provisioning or "").lower()


def wait_interval(headers, deadline, description):
    remaining = deadline - time.monotonic()
    delay = retry_delay(headers)
    if remaining <= 0 or delay >= remaining:
        raise AzureError(description + " timed out; operation may still be running", uncertain=True)
    time.sleep(delay)


class Azure:
    def __init__(self, subscription, lro_timeout=600):
        self.subscription = subscription
        self.token = None
        self.token_at = 0
        self.lro_timeout = lro_timeout

    def _token(self):
        if self.token is None or time.monotonic() - self.token_at > 240:
            try:
                result = subprocess.run(
                    ["az", "account", "get-access-token", "--subscription", self.subscription,
                     "--resource", ARM + "/", "--output", "json", "--only-show-errors"],
                    capture_output=True, check=True, text=True, timeout=60)
                self.token = json.loads(result.stdout)["accessToken"]
                self.token_at = time.monotonic()
            except (OSError, subprocess.SubprocessError, ValueError, KeyError):
                raise AzureError("Azure CLI authentication failed; run az login for the intended subscription") from None
        return self.token

    def call(self, method, resource, body=None, version="2024-05-01", headers=None, query=None):
        if not resource.startswith("/subscriptions/") or "?" in resource or "#" in resource:
            raise AzureError("Invalid ARM resource path")
        params = {"api-version": version, **(query or {})}
        resource_url = ARM + resource + "?" + urlencode(params)
        token = self._token()
        deadline = time.monotonic() + self.lro_timeout
        value, response_headers, status = request(
            resource_url, method, body,
            {"Authorization": "Bearer " + token, **(headers or {})}, timeout=min(90, self.lro_timeout))
        poll, status_endpoint = polling_url(response_headers)
        state = operation_state(value)
        if state in ("failed", "canceled", "cancelled"):
            raise AzureError("ARM operation failed; response details suppressed")
        pending = status == 202 or state in ("accepted", "running", "inprogress", "updating", "creating", "deleting")
        if not pending and not (poll and status == 201):
            return value, response_headers
        if not poll:
            raise AzureError("ARM accepted an operation without a polling URL; completion is unknown", uncertain=True)
        while True:
            wait_interval(response_headers, deadline, "ARM long-running operation")
            try:
                value, response_headers, status = request(
                    poll, "GET", headers={"Authorization": "Bearer " + self._token()},
                    timeout=min(90, max(0.1, deadline - time.monotonic())), error_response=True)
            except AzureError as error:
                raise AzureError("ARM polling failed; completion is unknown", error.status, uncertain=True) from None
            if status in (429, 503):
                continue
            if status >= 400:
                raise AzureError("ARM polling failed; completion is unknown", status, uncertain=True)
            state = operation_state(value)
            if state in ("failed", "canceled", "cancelled"):
                raise AzureError("ARM long-running operation failed; response details suppressed")
            next_poll, next_status_endpoint = polling_url(response_headers)
            if next_poll:
                poll, status_endpoint = next_poll, next_status_endpoint
            if state == "succeeded" or (not status_endpoint and status != 202 and not state):
                break
            if not state and status != 202:
                raise AzureError("ARM status response is missing its terminal state", uncertain=True)
        if method in ("PUT", "PATCH"):
            while True:
                if time.monotonic() >= deadline:
                    raise AzureError("ARM final resource read timed out; verify resource state", uncertain=True)
                value, response_headers, status = request(
                    resource_url, "GET", headers={"Authorization": "Bearer " + self._token()},
                    timeout=min(90, max(0.1, deadline - time.monotonic())), error_response=True)
                state = operation_state(value)
                if status in (202, 429, 503) or state in ("accepted", "running", "inprogress", "updating", "creating"):
                    wait_interval(response_headers, deadline, "ARM final resource read")
                    continue
                if status >= 400 or state in ("failed", "canceled", "cancelled"):
                    raise AzureError("ARM final resource read failed; details suppressed", status)
                break
        return value, response_headers

    def optional(self, resource, version="2024-05-01"):
        try:
            return self.call("GET", resource, version=version)
        except AzureError as error:
            if error.status == 404:
                return None, {}
            raise


def etag(headers, value=None):
    result = next((v for k, v in headers.items() if k.lower() == "etag"), None)
    result = result or (value or {}).get("etag")
    if not result or result == "*":
        raise AzureError("Missing or unsafe ETag; refusing a blind overwrite")
    return result


def callback_host(url):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname
                 and parsed.hostname.endswith(".logic.azure.com")
                 and not parsed.username and not parsed.password and parsed.port in (None, 443))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise AzureError("Unsupported Logic App callback host; expected Azure Public Cloud Consumption")
    return parsed.hostname


class CallbackPending(AzureError):
    def __init__(self, poll_url=None):
        super().__init__("Synthetic callback completion is unknown; do not restore routes until resolved",
                         uncertain=True)
        self.poll_url = poll_url


def finish_callback(value, headers, status, host, poll=None, timeout=240):
    deadline = time.monotonic() + timeout
    try:
        while status == 202:
            location = header(headers, "Location") or poll
            if not location or callback_host(location) != host:
                raise CallbackPending()
            poll = location
            wait_interval(headers, deadline, "Synthetic controller request")
            value, headers, status = request(
                poll, "GET", error_response=True,
                timeout=min(90, max(0.1, deadline - time.monotonic())))
        if status >= 500 and (not isinstance(value, dict) or "outcome" not in value):
            raise CallbackPending(poll)
        return status, value
    except AzureError as error:
        if isinstance(error, CallbackPending):
            raise
        raise CallbackPending(poll) from None


def invoke_callback(url, event):
    host = callback_host(url)
    try:
        value, headers, status = request(url, "POST", event, error_response=True)
    except AzureError:
        raise CallbackPending() from None
    return finish_callback(value, headers, status, host)


def resume_callback(pending):
    if not pending.poll_url:
        raise pending
    host = callback_host(pending.poll_url)
    return finish_callback({}, {}, 202, host, poll=pending.poll_url)
