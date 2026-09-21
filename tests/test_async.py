from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from apim_routing import azure
from apim_routing.config import load, named_id, parse_routes, routes
from apim_routing.deploy import smoke
from apim_routing.workflow import definition

ROOT = Path(__file__).resolve().parents[1]


class ArmAsyncTests(unittest.TestCase):
    def client(self, timeout=600):
        client = azure.Azure("00000000-0000-0000-0000-000000000000", lro_timeout=timeout)
        client._token = Mock(return_value="PRIVATE")
        return client

    def test_async_operation_waits_then_reads_final_resource_and_etag(self):
        responses = [
            ({}, {"Azure-AsyncOperation": azure.ARM + "/operations/example?secret=PRIVATE", "Retry-After": "3"}, 202),
            ({"status": "InProgress"}, {"Retry-After": "7"}, 200),
            ({"status": "Succeeded"}, {}, 200),
            ({"properties": {"value": "none"}}, {"ETag": '"completed"'}, 200),
        ]
        with patch("apim_routing.azure.request", side_effect=responses) as request, \
                patch("apim_routing.azure.time.monotonic", return_value=0), \
                patch("apim_routing.azure.time.sleep") as sleep:
            value, headers = self.client().call("PATCH", "/subscriptions/example/namedValues/routes", {})
        self.assertEqual(value["properties"]["value"], "none")
        self.assertEqual(headers["ETag"], '"completed"')
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 7])
        self.assertEqual([call.args[1] for call in request.call_args_list], ["PATCH", "GET", "GET", "GET"])
        self.assertEqual(request.call_args_list[0].args[0], request.call_args_list[-1].args[0])

    def test_location_polling_and_operation_location_are_supported(self):
        for polling_header in ("Location", "Operation-Location"):
            final = {} if polling_header == "Location" else {"status": "Succeeded"}
            responses = [
                ({}, {polling_header: "/operations/example", "Retry-After": "0"}, 202),
                ({}, {"Retry-After": "0"}, 202),
                (final, {}, 200),
            ]
            with self.subTest(header=polling_header), \
                    patch("apim_routing.azure.request", side_effect=responses), \
                    patch("apim_routing.azure.time.sleep"):
                self.client().call("POST", "/subscriptions/example/workflow/disable", {})

    def test_throttled_poll_honors_retry_after_without_reissuing_write(self):
        responses = [
            ({}, {"Location": "/operations/example", "Retry-After": "0"}, 202),
            ({"error": {"message": "PRIVATE"}}, {"Retry-After": "9"}, 429),
            ({}, {}, 204),
        ]
        with patch("apim_routing.azure.request", side_effect=responses) as request, \
                patch("apim_routing.azure.time.monotonic", return_value=0), \
                patch("apim_routing.azure.time.sleep") as sleep:
            self.client().call("POST", "/subscriptions/example/workflow/disable", {})
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0, 9])
        self.assertEqual([call.args[1] for call in request.call_args_list], ["POST", "GET", "GET"])

    def test_terminal_failure_and_cancellation_do_not_leak_details(self):
        for status in ("Failed", "Canceled", "Cancelled"):
            responses = [
                ({}, {"Azure-AsyncOperation": azure.ARM + "/operations/example", "Retry-After": "0"}, 202),
                ({"status": status, "error": {"message": "PRIVATE"}}, {}, 200),
            ]
            with self.subTest(status=status), \
                    patch("apim_routing.azure.request", side_effect=responses), \
                    patch("apim_routing.azure.time.sleep"):
                with self.assertRaises(azure.AzureError) as error:
                    self.client().call("PATCH", "/subscriptions/example/namedValues/routes", {})
            self.assertNotIn("PRIVATE", str(error.exception))
            self.assertFalse(error.exception.uncertain)

    def test_deadline_prevents_premature_retry_or_unbounded_wait(self):
        with patch("apim_routing.azure.request", return_value=(
                {}, {"Location": azure.ARM + "/operations/example", "Retry-After": "10"}, 202)) as request, \
                patch("apim_routing.azure.time.monotonic", return_value=0), \
                patch("apim_routing.azure.time.sleep") as sleep:
            with self.assertRaisesRegex(azure.AzureError, "timed out") as error:
                self.client(timeout=5).call("PATCH", "/subscriptions/example/namedValues/routes", {})
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_terminal_never_arrives_and_deadline_expires(self):
        responses = [
            ({}, {"Location": azure.ARM + "/operations/example", "Retry-After": "0"}, 202),
            ({}, {"Retry-After": "0"}, 202),
        ]
        with patch("apim_routing.azure.request", side_effect=responses), \
                patch("apim_routing.azure.time.monotonic", side_effect=[0, 1, 1, 6]), \
                patch("apim_routing.azure.time.sleep"):
            with self.assertRaisesRegex(azure.AzureError, "timed out"):
                self.client(timeout=5).call("PATCH", "/subscriptions/example/namedValues/routes", {})

    def test_missing_or_external_polling_url_fails_closed(self):
        for headers in ({}, {"Location": "https://evil.invalid/?PRIVATE"}):
            with patch("apim_routing.azure.request", return_value=({}, headers, 202)) as request:
                with self.assertRaises(azure.AzureError) as error:
                    self.client().call("PATCH", "/subscriptions/example/namedValues/routes", {})
            self.assertTrue(error.exception.uncertain)
            self.assertNotIn("PRIVATE", str(error.exception))
            self.assertEqual(request.call_count, 1)

    def test_polling_transport_error_marks_operation_unsettled(self):
        responses = [
            ({}, {"Location": azure.ARM + "/operations/example", "Retry-After": "0"}, 202),
            azure.AzureError("HTTP polling failed", 503),
        ]
        with patch("apim_routing.azure.request", side_effect=responses), \
                patch("apim_routing.azure.time.sleep"):
            with self.assertRaises(azure.AzureError) as error:
                self.client().call("PATCH", "/subscriptions/example/namedValues/routes", {})
        self.assertTrue(error.exception.uncertain)

    def test_retry_after_date_and_invalid_numeric(self):
        self.assertEqual(azure.retry_delay({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}), 0)
        for value in ("nan", "inf", "PRIVATE"):
            with self.assertRaises(azure.AzureError) as error:
                azure.retry_delay({"Retry-After": value})
            self.assertNotIn("PRIVATE", str(error.exception))

    def test_callback_resume_only_polls_existing_operation(self):
        url = "https://example.logic.azure.com/operations/example?sig=PRIVATE"
        with self.assertRaises(azure.CallbackPending) as pending:
            azure.finish_callback({}, {"Location": url}, 202, "example.logic.azure.com", timeout=0)
        with patch("apim_routing.azure.request", return_value=(
                {"outcome": "ControllerFailed", "writeStatus": "Skipped"}, {}, 500)) as request, \
                patch("apim_routing.azure.time.sleep"):
            status, result = azure.resume_callback(pending.exception)
        self.assertEqual(status, 500)
        self.assertEqual(result["writeStatus"], "Skipped")
        self.assertEqual([call.args[1] for call in request.call_args_list], ["GET"])
        self.assertNotIn("PRIVATE", str(pending.exception))

    def test_wdl_retains_default_http_async_pattern_and_exposes_state_versions(self):
        workflow = definition(load(ROOT / "examples/config.example.json"))

        def walk(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from walk(child)
            elif isinstance(value, list):
                for child in value:
                    yield from walk(child)

        writes = [value for value in walk(workflow)
                  if value.get("type") == "Http" and value["inputs"]["method"] == "PATCH"]
        self.assertEqual(len(writes), 1)
        self.assertNotIn("operationOptions", writes[0])
        response = workflow["actions"]["Respond"]["inputs"]["body"]
        self.assertIn("afterETag", response)
        self.assertIn("writeStatus", response)


class SmokeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = load(ROOT / "examples/config.example.json")
        self.receipt = ROOT / "build" / "unit-recovery-receipt.json"
        self.state = {"chat": "region-a,region-b", "embedding": "region-c,region-d"}
        self.original = dict(self.state)
        self.revision = 1
        self.client = Mock()
        self.client.call.side_effect = self.arm
        self.patches = []

    def tearDown(self):
        self.receipt.unlink(missing_ok=True)

    def arm(self, method, resource, body=None, **kwargs):
        group = next(group for group in self.state if resource == named_id(self.config, group))
        if method == "PATCH":
            self.assertEqual(kwargs["headers"]["If-Match"], str(self.revision))
            self.patches.append((group, body["properties"]["value"]))
            self.state[group] = body["properties"]["value"]
            self.revision += 1
        return {"id": resource, "properties": {
            "value": self.state[group], "secret": False,
            "displayName": self.config["groups"][group]["named_value_name"],
        }}, {"ETag": str(self.revision)}

    def event(self, callback, event, failure=None):
        essentials = event["data"]["essentials"]
        group, route = next((g, r) for g, r in routes(self.config)
                            if r["alert_name"] == essentials["alertRule"])
        before_etag = str(self.revision)
        if essentials["monitorCondition"] == "Resolved":
            return 200, {"outcome": "ResolvedIgnored", "result": "ResolvedIgnored", "writeStatus": "Skipped"}
        if failure == "before":
            return 500, {"outcome": "ControllerFailed", "result": "ControllerFailed",
                         "writeStatus": "Skipped", "beforeETag": before_etag, "afterETag": before_etag}
        if failure == "exception":
            raise azure.AzureError("Callback failed before writing", 500)
        if failure == "pending":
            raise azure.CallbackPending("https://example.logic.azure.com/operations/example")
        tokens = parse_routes(self.state[group], [r["token"] for r in self.config["groups"][group]["routes"]])
        outcome = "AlreadyDegraded" if route["token"] in tokens else "Updated"
        if outcome == "Updated":
            tokens.append(route["token"])
            self.state[group] = ",".join(tokens)
            self.revision += 1
        after_etag = str(self.revision)
        if failure == "concurrent":
            # Another operator writes even the same value: its new ETag must
            # prevent cleanup from erasing that operator's decision.
            self.revision += 1
        return (500 if failure else 200), {
            "outcome": outcome, "result": "NotificationFailed" if failure else outcome,
            "notificationFailed": bool(failure), "controllerFailed": False,
            "beforeETag": before_etag, "afterETag": after_etag, "writeStatus": "Succeeded",
        }

    def run_smoke(self, failure):
        with patch("apim_routing.deploy.preflight"), \
                patch("apim_routing.deploy.check_controller", return_value=(
                    "principal", {"properties": {"changedTime": "stable"}}, "hash", "callback")), \
                patch("apim_routing.deploy.check_alerts"), \
                patch("apim_routing.deploy.rejection_checks", return_value=3), \
                patch("apim_routing.deploy.invoke_callback",
                      side_effect=lambda callback, event: self.event(callback, event, failure)):
            return smoke(self.config, self.client, self.receipt)

    def test_failure_before_write_restores_original_degraded_flags(self):
        for failure in ("before", "exception"):
            with self.subTest(failure=failure):
                self.state = dict(self.original)
                with self.assertRaises(azure.AzureError):
                    self.run_smoke(failure)
                self.assertEqual(self.state, self.original)
                self.assertFalse(self.receipt.exists())
                self.assertEqual(self.patches[-1], ("chat", "region-a,region-b"))

    def test_notification_failure_after_write_uses_returned_write_etag(self):
        with self.assertRaises(azure.AzureError):
            self.run_smoke("after")
        self.assertEqual(self.state, self.original)
        self.assertFalse(self.receipt.exists())

    def test_concurrent_operator_etag_is_never_overwritten(self):
        with self.assertRaisesRegex(azure.AzureError, "concurrent or unconfirmed"):
            self.run_smoke("concurrent")
        self.assertEqual(self.state["chat"], "region-a")
        self.assertEqual(self.patches, [("chat", "none")])
        self.assertFalse(self.receipt.exists())

    def test_unresolved_callback_prevents_cleanup_write(self):
        with patch("apim_routing.deploy.resume_callback", side_effect=azure.CallbackPending()), \
                self.assertRaisesRegex(azure.AzureError, "unresolved operation"):
            self.run_smoke("pending")
        self.assertEqual(self.patches, [("chat", "none")])
        self.assertFalse(self.receipt.exists())

    def test_pending_callback_is_resolved_before_restoring_routes(self):
        with patch("apim_routing.deploy.resume_callback", return_value=(
                500, {"outcome": "ControllerFailed", "writeStatus": "Skipped"})) as resume:
            with self.assertRaises(azure.AzureError):
                self.run_smoke("pending")
        resume.assert_called_once()
        self.assertEqual(self.state, self.original)
        self.assertEqual(self.patches[-1], ("chat", "region-a,region-b"))


if __name__ == "__main__":
    unittest.main()
