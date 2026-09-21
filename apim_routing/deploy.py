"""Explicit, narrow-scope Azure operations; APIM and Foundry remain external."""

import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, uuid5
import xml.etree.ElementTree as ET

from .azure import AzureError, CallbackPending, etag, invoke_callback, resume_callback
from .config import (
    action_group_id, alert_id, digest, named_id, parse_routes, root, routes, rule_map, workflow_id,
)
from .render import NAMESPACE, METRIC, alert, policy
from .workflow import definition

LOGIC_VERSION = "2019-05-01"
ALERT_VERSION = "2018-03-01"
GROUP_VERSION = "2023-01-01"
ROLE_VERSION = "2022-04-01"
FOUNDRY_VERSION = "2024-10-01"
CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"


def ensure(condition, message):
    if not condition:
        raise AzureError(message)


def webhook_valid(webhook):
    try:
        parsed = urlsplit(webhook)
        query = parse_qs(parsed.query)
        valid = (parsed.scheme == "https" and parsed.netloc == "oapi.dingtalk.com"
                 and parsed.path == "/robot/send" and not parsed.fragment
                 and len(query.get("access_token", [])) == 1
                 and not ({"timestamp", "sign"} & set(query)))
    except (TypeError, ValueError):
        valid = False
    ensure(valid, "Expected a long-lived HTTPS DingTalk robot webhook (not a timestamp-signed URL)")


def api_id(config):
    return config["apim_resource_id"] + "/apis/" + config["api_id"]


def policy_id(config):
    return api_id(config) + "/policies/policy"


def allowed(config, group):
    return [r["token"] for r in config["groups"][group]["routes"]]


def check_named(config, group, value):
    properties = value.get("properties", {})
    ensure(properties.get("secret") is False and not properties.get("keyVault"),
           "Named values must be nonsecret and not Key Vault references")
    parse_routes(properties.get("value"), allowed(config, group))
    ensure(properties.get("displayName") == config["groups"][group]["named_value_name"],
           "Named value displayName must equal configured named_value_name for policy expansion")


def preflight(config, azure):
    """Read-only validation. No model inference or authorization changes."""
    azure.call("GET", root(config), version="2021-04-01")
    apim, _ = azure.call("GET", config["apim_resource_id"])
    ensure(apim.get("properties", {}).get("provisioningState") == "Succeeded",
           "Existing APIM must be fully provisioned")
    identities = apim.get("identity", {}).get("userAssignedIdentities", {})
    attached = False
    for identity_id, identity in identities.items():
        client_id = (identity or {}).get("clientId")
        if not client_id:
            identity_resource, _ = azure.call("GET", identity_id, version="2023-01-31")
            client_id = identity_resource.get("properties", {}).get("clientId")
        attached |= (client_id or "").lower() == config["uami_client_id"].lower()
    ensure(attached, "Configured UAMI is not attached to the existing APIM")
    azure.call("GET", api_id(config))
    operations, _ = azure.call("GET", api_id(config) + "/operations", query={"$top": "1000"})
    ensure(not operations.get("nextLink"), "API has over 1000 operations; unsupported preflight scope")
    relevant = [op for op in operations.get("value", [])
                if "{deployment-id}" in op.get("properties", {}).get("urlTemplate", "")]
    ensure(relevant, "Existing API needs an operation with a {deployment-id} path parameter")
    for _, route in routes(config):
        account, _ = azure.call("GET", route["foundry_resource_id"], version=FOUNDRY_VERSION)
        deployment, _ = azure.call("GET", route["foundry_resource_id"] + "/deployments/" + route["deployment_name"],
                                   version=FOUNDRY_VERSION)
        ensure(deployment.get("properties", {}).get("provisioningState") == "Succeeded",
               "External model deployment must be fully provisioned")
        backend, _ = azure.call("GET", config["apim_resource_id"] + "/backends/" + route["backend_id"])
        properties = backend.get("properties", {})
        ensure(properties.get("protocol") == "http", "Existing backend must use the HTTP protocol")
        credentials = properties.get("credentials") or {}
        credential_headers = {key.lower() for key in credentials.get("header", {})}
        ensure(not credentials.get("authorization") and not ({"authorization", "api-key"} & credential_headers)
               and "api-key" not in {key.lower() for key in credentials.get("query", {})},
               "Existing backend credentials conflict with UAMI authentication")
        endpoint = urlsplit(properties.get("url", ""))
        account_properties = account.get("properties", {})
        known_endpoints = [account_properties.get("endpoint", "")]
        known_endpoints.extend((account_properties.get("endpoints") or {}).values())
        hosts = {urlsplit(v).hostname for v in known_endpoints if isinstance(v, str) and v}
        ensure(endpoint.scheme == "https" and endpoint.hostname in hosts and endpoint.hostname
               and endpoint.path in ("", "/") and not endpoint.query and not endpoint.fragment
               and not endpoint.username and not endpoint.password and endpoint.port in (None, 443),
               "Backend URL must be an HTTPS root endpoint of the configured Foundry account; no rewrites performed")
        backend_resource = properties.get("resourceId")
        if backend_resource:
            ensure(backend_resource.lower().removeprefix("https://management.azure.com")
                   == route["foundry_resource_id"].lower(),
                   "Backend resourceId does not match configured Foundry account")
    for group in config["groups"]:
        value, _ = azure.optional(named_id(config, group))
        if value:
            check_named(config, group, value)


def identity_payload(existing):
    identity = (existing or {}).get("identity", {})
    identity_type = identity.get("type", "")
    if existing:
        ensure("SystemAssigned" in identity_type and identity.get("principalId"),
               "Existing workflow must already have a system-assigned identity; refusing identity replacement")
    result = {"type": identity_type or "SystemAssigned"}
    if identity.get("userAssignedIdentities"):
        result["userAssignedIdentities"] = {key: {} for key in identity["userAssignedIdentities"]}
    return result


def workflow_payload(config, webhook, existing=None):
    identity = identity_payload(existing)
    current = (existing or {}).get("properties", {})
    properties = {key: copy.deepcopy(current[key]) for key in (
        "state", "accessControl", "integrationAccount", "integrationServiceEnvironment",
        "runtimeConfiguration",
    ) if key in current}
    properties.setdefault("state", "Enabled")
    properties.update({"definition": definition(config),
                       "parameters": {"dingtalkWebhook": {"value": webhook}}})
    return {
        "location": (existing or {}).get("location", config["location"]),
        "tags": (existing or {}).get("tags", {"managed-by": "alert-driven-apim-routing"}),
        "identity": identity, "properties": properties,
    }


def check_existing_mapping(config, existing):
    if existing is None:
        return
    try:
        current = existing["properties"]["definition"]["actions"]["Process"]["actions"]["Rule_map"]["inputs"]
    except (KeyError, TypeError):
        raise AzureError("Existing workflow is not a recognized routing controller; choose a new workflow name") from None
    expected = rule_map(config)
    ensure(isinstance(current, dict) and set(current) == set(expected),
           "Existing alert names differ; use an explicit migration to avoid orphaning active rules")
    for name, rule in expected.items():
        old = current[name]
        ensure(isinstance(old, dict) and all(old.get(key) == rule[key]
               for key in ("group", "region", "namedValue", "allowed")),
               "Existing named-value/token mapping differs; migrate routing state explicitly before deployment")


def quiesce_workflow(config, azure, existing):
    if existing is None:
        return None, {}
    resource = workflow_id(config)
    if existing.get("properties", {}).get("state") != "Disabled":
        azure.call("POST", resource + "/disable", {}, version=LOGIC_VERSION)
    current, headers = azure.call("GET", resource, version=LOGIC_VERSION)
    ensure(current.get("properties", {}).get("state") == "Disabled",
           "Workflow has not become Disabled; retry deployment after verifying its state")
    active_filter = " or ".join("Status eq '" + status + "'"
                               for status in ("Running", "Waiting", "Paused", "Suspended"))
    runs, _ = azure.call("GET", resource + "/runs", version=LOGIC_VERSION,
                         query={"$filter": active_filter, "$top": "1"})
    ensure(not runs.get("value") and not runs.get("nextLink"),
           "Workflow is disabled but has active runs; let them finish or explicitly cancel them, then retry")
    return current, headers


def deploy(config, azure, webhook):
    webhook_valid(webhook)
    preflight(config, azure)
    existing, headers = azure.optional(workflow_id(config), LOGIC_VERSION)
    payload = workflow_payload(config, webhook, existing)
    check_existing_mapping(config, existing)
    # Disable old rules before changing their controller. This intentionally
    # leaves any partially failed deployment inert instead of auto-enabling it.
    disable_alerts(config, azure, missing_ok=True)
    quiesced, headers = quiesce_workflow(config, azure, existing)
    if existing:
        ensure(quiesced.get("identity", {}).get("principalId") == existing["identity"]["principalId"],
               "Workflow principal changed during quiescence; deployment stopped")
        check_existing_mapping(config, quiesced)
        payload = workflow_payload(config, webhook, quiesced)
    for group in config["groups"]:
        value, _ = azure.optional(named_id(config, group))
        if value is None:
            azure.call("PUT", named_id(config, group), {
                "properties": {"displayName": config["groups"][group]["named_value_name"],
                               "secret": False, "value": "none"}},
                headers={"If-None-Match": "*"})
        else:
            check_named(config, group, value)
    # Logic workflow GET does not consistently return an ETag. Preserve its
    # identity explicitly; strict compare-and-swap is mandatory for route state.
    workflow_etag = next((v for k, v in headers.items() if k.lower() == "etag"), None)
    write_headers = {"If-Match": workflow_etag} if workflow_etag else {}
    updated, _ = azure.call("PUT", workflow_id(config), payload, version=LOGIC_VERSION,
                            headers=write_headers)
    principal = updated.get("identity", {}).get("principalId")
    if not principal:
        updated, _ = azure.call("GET", workflow_id(config), version=LOGIC_VERSION)
        principal = updated.get("identity", {}).get("principalId")
    ensure(principal, "Workflow provisioning incomplete; re-run deploy once provisioning finishes")
    if existing:
        ensure(principal == existing["identity"]["principalId"],
               "Workflow principal changed unexpectedly; do not enable alerts")
    callback, _ = azure.call("POST", workflow_id(config) + "/triggers/receive/listCallbackUrl",
                            {}, version=LOGIC_VERSION)
    from .azure import callback_host
    callback_host(callback.get("value", ""))
    group_existing, _ = azure.optional(action_group_id(config), GROUP_VERSION)
    azure.call("PUT", action_group_id(config), {
        "location": "global",
        "tags": (group_existing or {}).get("tags", {"managed-by": "alert-driven-apim-routing"}),
        "properties": {
            "groupShortName": config["action_group_short_name"], "enabled": True,
            "logicAppReceivers": [{
                "name": "degraded-routing-controller", "resourceId": workflow_id(config),
                "callbackUrl": callback["value"], "useCommonAlertSchema": True,
            }],
        },
    }, version=GROUP_VERSION)
    for _, route in routes(config):
        azure.call("PUT", alert_id(config, route), alert(config, route), version=ALERT_VERSION)
    if existing and existing.get("properties", {}).get("state") == "Enabled":
        azure.call("POST", workflow_id(config) + "/enable", {}, version=LOGIC_VERSION)
        resumed, _ = azure.call("GET", workflow_id(config), version=LOGIC_VERSION)
        ensure(resumed.get("properties", {}).get("state") == "Enabled",
               "Resources deployed with alerts disabled; verify workflow enable completion before smoke")
    return {"workflow": workflow_id(config), "principal_id": principal, "alerts_enabled": False}


def principal(config, azure):
    workflow, _ = azure.call("GET", workflow_id(config), version=LOGIC_VERSION)
    identity_payload(workflow)
    return workflow["identity"]["principalId"], workflow


def role_spec(config, group, principal_id):
    scope = named_id(config, group)
    subscription = config["apim_resource_id"].split("/")[2]
    role = f"/subscriptions/{subscription}/providers/Microsoft.Authorization/roleDefinitions/{CONTRIBUTOR}"
    assignment = str(uuid5(NAMESPACE_URL, (scope + principal_id + role).lower()))
    return scope + "/providers/Microsoft.Authorization/roleAssignments/" + assignment, {
        "properties": {"principalId": principal_id, "principalType": "ServicePrincipal",
                       "roleDefinitionId": role}
    }


def grant_roles(config, azure):
    principal_id, _ = principal(config, azure)
    for group in config["groups"]:
        value, _ = azure.call("GET", named_id(config, group))
        check_named(config, group, value)
        resource, body = role_spec(config, group, principal_id)
        azure.call("PUT", resource, body, version=ROLE_VERSION)
    return {"granted": "Contributor at the two exact named value scopes", "principal_id": principal_id}


def check_roles(config, azure, principal_id):
    for group in config["groups"]:
        resource, expected = role_spec(config, group, principal_id)
        actual, _ = azure.call("GET", resource, version=ROLE_VERSION)
        props = actual.get("properties", {})
        ensure(props.get("principalId", "").lower() == principal_id.lower()
               and props.get("roleDefinitionId", "").lower()
               == expected["properties"]["roleDefinitionId"].lower()
               and props.get("scope", "").lower() == named_id(config, group).lower(),
               "Missing expected exact-scope Contributor assignment; run grant-controller-roles")


def normalized_policy(text):
    try:
        # Azure may change XML whitespace and quote style during serialization.
        return ET.canonicalize(text, strip_text=True)
    except ET.ParseError:
        raise AzureError("APIM policy is not valid XML") from None


def check_policy(config, azure):
    actual, _ = azure.call("GET", policy_id(config), query={"format": "rawxml"})
    expected = normalized_policy(policy(config))
    ensure(normalized_policy(actual.get("properties", {}).get("value", "")) == expected,
           "Installed API policy does not match generated configuration; run install-policy")
    return hashlib.sha256(expected.encode()).hexdigest()


def install_policy(config, azure, backup):
    preflight(config, azure)
    ensure(not Path(backup).exists(), "Policy backup already exists; choose a new path")
    existing, headers = azure.optional(policy_id(config))
    if existing:
        # Request raw policy explicitly; the default can contain XML-escaped text.
        existing, headers = azure.call("GET", policy_id(config), query={"format": "rawxml"})
    Path(backup).parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
        json.dump(existing, stream, indent=2)
        stream.write("\n")
    azure.call("PUT", policy_id(config), {"properties": {"format": "rawxml", "value": policy(config)}},
               headers={"If-Match": etag(headers, existing)} if existing else {"If-None-Match": "*"})
    return {"policy_installed": True, "policy_sha256": check_policy(config, azure)}


def disable_alerts(config, azure, missing_ok=False):
    disabled = 0
    failures = []
    for _, route in routes(config):
        try:
            existing, _ = azure.optional(alert_id(config, route), ALERT_VERSION)
            if existing is None:
                ensure(missing_ok, "Expected metric alert is missing; deploy before changing state")
                continue
            azure.call("PATCH", alert_id(config, route), {"properties": {"enabled": False}}, version=ALERT_VERSION)
            disabled += 1
        except AzureError as error:
            failures.append(error.status)
    ensure(not failures, "Could not disable every alert; inspect Azure permissions and all four rules")
    return {"disabled_alerts": disabled}


def check_controller(config, azure):
    principal_id, workflow = principal(config, azure)
    ensure(workflow.get("properties", {}).get("state") == "Enabled", "Workflow must be Enabled")
    ensure(workflow.get("properties", {}).get("definition") == definition(config),
           "Deployed workflow definition differs from this configuration; deploy again")
    ensure(workflow.get("properties", {}).get("changedTime"), "Workflow changedTime is missing")
    check_roles(config, azure, principal_id)
    policy_hash = check_policy(config, azure)
    action_group, _ = azure.call("GET", action_group_id(config), version=GROUP_VERSION)
    props = action_group.get("properties", {})
    receivers = props.get("logicAppReceivers", [])
    ensure(props.get("enabled") is True and len(receivers) == 1
           and receivers[0].get("resourceId", "").lower() == workflow_id(config).lower()
           and receivers[0].get("useCommonAlertSchema") is True,
           "Action Group is not wired to the expected workflow using Common Alert Schema")
    callback, _ = azure.call("POST", workflow_id(config) + "/triggers/receive/listCallbackUrl",
                            {}, version=LOGIC_VERSION)
    ensure(receivers[0].get("callbackUrl") == callback.get("value"),
           "Action Group callback is stale; run deploy")
    return principal_id, workflow, policy_hash, callback["value"]


def check_alerts(config, azure, disabled=False):
    for _, route in routes(config):
        current, _ = azure.call("GET", alert_id(config, route), version=ALERT_VERSION)
        properties = current.get("properties", {})
        expected = alert(config, route)["properties"]
        for key in ("scopes", "evaluationFrequency", "windowSize", "autoMitigate", "criteria", "actions"):
            ensure(properties.get(key) == expected[key], "Metric alert configuration drift; deploy again")
        if disabled:
            ensure(properties.get("enabled") is False, "All alerts must be disabled before synthetic smoke")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def rejection_checks(config, callback):
    _, route = next(routes(config))
    unknown = synthetic_event(config, route)
    unknown["data"]["essentials"]["alertRule"] = "__untrusted_synthetic_rule__"
    stale = synthetic_event(config, route)
    stale["data"]["essentials"]["firedDateTime"] = "2000-01-01T00:00:00Z"
    wrong_deployment = synthetic_event(config, route)
    wrong_deployment["data"]["alertContext"]["condition"]["allOf"][0]["dimensions"][0]["value"] = "__untrusted_deployment__"
    for event in (unknown, stale, wrong_deployment):
        try:
            status, _ = invoke_callback(callback, event)
        except AzureError as error:
            ensure(error.status == 400, "Synthetic rejection check returned an unexpected error")
        else:
            ensure(status == 400, "Controller accepted an untrusted synthetic event")
    return 3


def synthetic_event(config, route, condition="Fired"):
    now = stamp()
    return {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {
            "essentials": {
                "alertRule": route["alert_name"], "signalType": "Metric",
                "monitoringService": "Platform", "monitorCondition": condition,
                "firedDateTime": now, "resolvedDateTime": now if condition == "Resolved" else None,
                "alertTargetIDs": [route["foundry_resource_id"]],
            },
            "alertContext": {"condition": {"allOf": [{
                "metricName": METRIC, "metricNamespace": NAMESPACE,
                "operator": "GreaterThan", "timeAggregation": "Average",
                "threshold": config["threshold_ms"],
                "metricValue": config["threshold_ms"] + 1 if condition == "Fired" else 0,
                "dimensions": [{"name": "ModelDeploymentName", "value": route["deployment_name"]}],
            }]}},
        },
    }


def smoke(config, azure, receipt):
    """Exercise the deployed controller without calling any model endpoint.

    Requires an operator change window: named values are temporarily modified.
    Every cleanup is conditional on a fresh ETag and the exact expected value.
    """
    Path(receipt).unlink(missing_ok=True)
    preflight(config, azure)
    principal_id, workflow, policy_hash, callback = check_controller(config, azure)
    check_alerts(config, azure, disabled=True)
    rejected = rejection_checks(config, callback)
    completed = []
    for group, spec in config["groups"].items():
        original, original_headers = azure.call("GET", named_id(config, group))
        check_named(config, group, original)
        original_value = original["properties"]["value"]
        confirmed = (original_value, etag(original_headers, original))
        possible = None
        unsettled = False
        try:
            # Establish an observable write path even when every route started
            # degraded. The explicit smoke confirmation authorizes this reset.
            if original_value != "none":
                reset, reset_headers = azure.call(
                    "PATCH", named_id(config, group), {"properties": {"value": "none"}},
                    headers={"If-Match": confirmed[1]})
                possible = ("none", etag(reset_headers, reset))
                current, headers = azure.call("GET", named_id(config, group))
                ensure((current["properties"]["value"], etag(headers, current)) == possible,
                       "Concurrent change after smoke reset; refusing to adopt another writer's state")
                confirmed, possible = possible, None
            for route in spec["routes"]:
                for condition in ("Resolved", "Fired", "Fired"):
                    before = confirmed[0]
                    candidate = before
                    tokens = parse_routes(before, allowed(config, group))
                    outcome = "ResolvedIgnored"
                    if condition == "Fired":
                        outcome = "AlreadyDegraded" if route["token"] in tokens else "Updated"
                        if route["token"] not in tokens:
                            tokens.append(route["token"])
                        candidate = ",".join(tokens)
                    try:
                        status, result = invoke_callback(callback, synthetic_event(config, route, condition))
                    except CallbackPending as pending:
                        # Only resume the existing response poll; never resend
                        # a Fired event or restore while its outcome is unknown.
                        status, result = resume_callback(pending)
                    if result.get("beforeETag"):
                        ensure(result["beforeETag"] == confirmed[1],
                               "Concurrent named-value modification detected before controller write")
                    response_etag = result.get("afterETag")
                    if response_etag and response_etag != "*" and response_etag != confirmed[1]:
                        possible = (candidate, response_etag)
                    write_status = result.get("writeStatus")
                    if result.get("writeHttpStatus") == 202 or write_status in ("Running", "Waiting", "TimedOut") or (
                            write_status == "Failed" and result.get("writeHttpStatus") not in
                            (400, 401, 403, 404, 409, 412, 422)):
                        raise AzureError("Controller write completion is uncertain; inspect its ARM operation before recovery",
                                         uncertain=True)
                    ensure(status == 200 and result.get("outcome") == outcome
                           and result.get("result") == outcome and not result.get("controllerFailed")
                           and not result.get("notificationFailed"),
                           "Synthetic event failed; inspect secure workflow run history")
                    current, headers = azure.call("GET", named_id(config, group))
                    observed = (current["properties"]["value"], etag(headers, current))
                    ensure(observed[0] == candidate,
                           "Named value changed unexpectedly during smoke; stop concurrent writers")
                    if candidate == before:
                        ensure(observed == confirmed, "Concurrent ETag change during no-op synthetic event")
                    elif possible:
                        ensure(observed == possible, "Concurrent ETag change after controller write")
                    confirmed, possible = observed, None
                    completed.append({"group": group, "route": route["token"],
                                      "condition": condition, "outcome": outcome})
        except AzureError as error:
            unsettled = error.uncertain
            raise
        except KeyboardInterrupt:
            unsettled = True
            raise
        finally:
            ensure(not unsettled, "Smoke cleanup blocked by an unresolved operation; wait for completion and reconcile routes manually")
            current, headers = azure.call("GET", named_id(config, group))
            if current["properties"]["value"] != original_value:
                observed = (current["properties"]["value"], etag(headers, current))
                ensure(observed == confirmed or observed == possible,
                       "Smoke cleanup refused: concurrent or unconfirmed modification; manually reconcile routes")
                azure.call("PATCH", named_id(config, group),
                           {"properties": {"value": original_value}},
                           headers={"If-Match": observed[1]})
            restored, _ = azure.call("GET", named_id(config, group))
            ensure(restored["properties"]["value"] == original_value,
                   "Smoke restoration failed; manually reconcile routes")
    result = {
        "config_sha256": digest(config), "completed_at": stamp(), "principal_id": principal_id,
        "workflow_changed_time": workflow.get("properties", {}).get("changedTime"),
        "policy_sha256": policy_hash, "restored": True, "checks": completed, "rejection_checks": rejected,
    }
    Path(receipt).parent.mkdir(parents=True, exist_ok=True)
    Path(receipt).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return {"smoke_passed": True, "checks": len(completed), "rejection_checks": rejected,
            "named_values_restored": True}


def enable_alerts(config, azure, receipt):
    preflight(config, azure)
    principal_id, workflow, policy_hash, _ = check_controller(config, azure)
    check_alerts(config, azure)
    try:
        evidence = json.loads(Path(receipt).read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(evidence["completed_at"])).total_seconds()
    except (OSError, ValueError, KeyError, TypeError):
        raise AzureError("Missing or invalid smoke receipt; run smoke first") from None
    ensure(0 <= age <= 86400 and evidence.get("restored") is True
           and evidence.get("config_sha256") == digest(config)
           and evidence.get("principal_id") == principal_id
           and evidence.get("workflow_changed_time") == workflow.get("properties", {}).get("changedTime")
           and evidence.get("policy_sha256") == policy_hash
           and evidence.get("rejection_checks") == 3
           and len(evidence.get("checks", [])) == 12,
           "Smoke receipt is stale or mismatched; run smoke again with alerts disabled")
    enabled = []
    try:
        for _, route in routes(config):
            azure.call("PATCH", alert_id(config, route), {"properties": {"enabled": True}}, version=ALERT_VERSION)
            enabled.append(route["alert_name"])
    except AzureError:
        # Best-effort fail closed; failures here remain explicit to the operator.
        disable_alerts(config, azure, missing_ok=True)
        raise
    return {"enabled_alerts": enabled}
