import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from apim_routing.azure import AzureError
from apim_routing.config import (
    ConfigError, load, log_alert_id, log_query, named_id, routes, rule_map, validate, workflow_id,
)
from apim_routing.deploy import (
    check_alerts, check_existing_mapping, deploy, disable_alerts, enable_alerts,
    preflight, smoke, synthetic_event,
)
from apim_routing.render import alert, alert_resources, log_alert, manifest, policy, render
from apim_routing.workflow import alert_schema, definition

ROOT = Path(__file__).resolve().parents[1]


def config():
    data = load(ROOT / "examples/config.example.json")
    data["apim_log_alerts"] = {
        "workspace_resource_id": (
            "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/"
            "replace-logs-rg/providers/Microsoft.OperationalInsights/workspaces/replace-workspace"
        ),
        "location": "replacewithworkspacelocation",
    }
    for _, route in routes(data):
        route["log_alert_name"] = route["alert_name"] + "-p95"
    return validate(data)


def evaluate_guard(expression, event, mapping, now):
    """Evaluate the WDL guard subset, including lazy if, against payload fixtures."""
    tokens = re.findall(r"'(?:[^']|'')*'|-?\d+(?:\.\d+)?|[A-Za-z_]\w*|[(),?\[\]]", expression)
    position = 0

    def take(expected=None):
        nonlocal position
        token = tokens[position]
        position += 1
        if expected is not None:
            assert token == expected, (token, expected)
        return token

    def parse():
        token = take()
        if token.startswith("'"):
            node = ("value", token[1:-1].replace("''", "'"))
        elif re.fullmatch(r"-?\d+(?:\.\d+)?", token):
            node = ("value", float(token))
        else:
            take("(")
            arguments = []
            if tokens[position] != ")":
                arguments.append(parse())
                while tokens[position] == ",":
                    take(",")
                    arguments.append(parse())
            take(")")
            node = ("call", token, arguments)
        while position < len(tokens) and tokens[position] in ("?", "["):
            if tokens[position] == "?":
                take("?")
            take("[")
            key = parse()
            take("]")
            node = ("get", node, key)
        return node

    def evaluate(node):
        if node[0] == "value":
            return node[1]
        if node[0] == "get":
            value = evaluate(node[1])
            return value.get(evaluate(node[2])) if value is not None else None
        _, function, arguments = node
        if function == "if":
            return evaluate(arguments[1] if evaluate(arguments[0]) else arguments[2])
        values = [evaluate(argument) for argument in arguments]
        operations = {
            "body": lambda _: event,
            "outputs": lambda _: mapping,
            "equals": lambda a, b: a == b,
            "and": lambda *args: all(args),
            "or": lambda *args: any(args),
            "not": lambda value: not value,
            "first": lambda value: value[0],
            "toLower": lambda value: value.lower(),
            "float": float,
            "greater": lambda a, b: a > b,
            "greaterOrEquals": lambda a, b: a >= b,
            "lessOrEquals": lambda a, b: a <= b,
            "utcNow": lambda: now,
            "addMinutes": lambda value, minutes: value + timedelta(minutes=minutes),
            "ticks": lambda value: (
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                if isinstance(value, str) else value
            ).timestamp(),
            "coalesce": lambda *args: next((v for v in args if v is not None), None),
        }
        return operations[function](*values)

    tree = parse()
    assert position == len(tokens)
    return evaluate(tree)


class LogConfigurationTests(unittest.TestCase):
    def test_opt_in_defaults_and_legacy_behavior(self):
        data = config()
        legacy = load(ROOT / "examples/config.example.json")
        self.assertEqual(len(list(alert_resources(legacy))), 4)
        self.assertEqual(len(list(alert_resources(data))), 8)
        self.assertEqual(policy(data), policy(legacy))
        for _, route in routes(data):
            self.assertEqual(alert(data, route), alert(legacy, route))
        self.assertEqual(data["apim_log_alerts"]["min_samples"], 20)
        self.assertEqual(data["apim_log_alerts"]["threshold_ms"], 2000)
        self.assertEqual(data["apim_log_alerts"]["window_size"], "PT5M")
        self.assertEqual(data["apim_log_alerts"]["evaluation_frequency"], "PT5M")
        self.assertEqual(validate(copy.deepcopy(data)), data)

    def test_invalid_log_configuration_rejected(self):
        cases = {
            "workspace_resource_id": ("not-a-resource", config()["apim_resource_id"]),
            "location": ("", None, "West Europe"),
            "threshold_ms": (True, 0, -1, float("nan"), float("inf"), "2000", 86400001),
            "min_samples": (True, 0, -1, 1.5, "20", 1000001),
            "window_size": ("PT1M", "PT2M", None, []),
            "evaluation_frequency": ("PT1M", "PT6H", True, {}),
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    data = config()
                    data["apim_log_alerts"][key] = value
                    with self.assertRaises(ConfigError):
                        validate(data)
        data = config()
        data["apim_log_alerts"]["evaluation_frequency"] = "PT15M"
        with self.assertRaisesRegex(ConfigError, "must not exceed"):
            validate(data)
        data = config()
        data["apim_log_alerts"]["query"] = "arbitrary query"
        with self.assertRaises(ConfigError):
            validate(data)

    def test_rule_names_required_unique_and_safe(self):
        for value in (None, "unsafe'query", config()["groups"]["chat"]["routes"][1]["alert_name"].upper()):
            data = config()
            data["groups"]["chat"]["routes"][0]["log_alert_name"] = value
            with self.assertRaises(ConfigError):
                validate(data)
        for remove in ("apim_log_alerts", "log_alert_name"):
            data = config()
            if remove == "apim_log_alerts":
                del data[remove]
            else:
                del data["groups"]["chat"]["routes"][0][remove]
            with self.assertRaisesRegex(ConfigError, "every route"):
                validate(data)


class LogGeneratorTests(unittest.TestCase):
    def test_exact_query_and_rule_contract_for_all_routes(self):
        data = config()
        data["apim_log_alerts"].update(threshold_ms=3456.5, min_samples=37, window_size="PT15M")
        for group, route in routes(data):
            query = log_query(data, route)
            for expected in (
                "ApiManagementGatewayLogs", f"_ResourceId =~ '{data['apim_resource_id']}'",
                f"ApiId == '{data['api_id']}'", f"BackendId == '{route['backend_id']}'",
                f"/openai/deployments/{route['deployment_name']}/", "BackendMethod == 'POST'",
                "isnotnull(BackendTime) and BackendTime > 0 and BackendResponseCode > 0",
                "SampleCount = count(), BackendLatencyP95Ms = percentile(BackendTime, 95) by BackendId",
                "SampleCount >= 37", "project BackendId, BackendLatencyP95Ms",
            ):
                self.assertIn(expected, query)
            self.assertNotIn("bin(", query)
            self.assertNotIn("avg(", query)
            body = log_alert(data, route)
            props = body["properties"]
            self.assertFalse(props["enabled"])
            self.assertFalse(props["skipQueryValidation"])
            self.assertEqual(body["kind"], "LogAlert")
            self.assertEqual(body["location"], data["apim_log_alerts"]["location"])
            self.assertEqual(props["scopes"], [data["apim_log_alerts"]["workspace_resource_id"]])
            self.assertEqual(props["windowSize"], "PT15M")
            self.assertEqual(props["evaluationFrequency"], "PT5M")
            criterion = props["criteria"]["allOf"][0]
            self.assertEqual(criterion["query"], query)
            self.assertEqual(criterion["threshold"], 3456.5)
            self.assertEqual(criterion["metricMeasureColumn"], "BackendLatencyP95Ms")
            self.assertEqual(criterion["timeAggregation"], "Maximum")
            self.assertEqual(criterion["operator"], "GreaterThan")
            self.assertEqual(criterion["dimensions"], [
                {"name": "BackendId", "operator": "Include", "values": [route["backend_id"]]},
            ])
            mapping = rule_map(data)[route["log_alert_name"]]
            self.assertEqual(mapping["group"], group)
            self.assertEqual(mapping["namedValue"], named_id(data, group))
            self.assertEqual(mapping["backend_name"], route["backend_name"])
            self.assertEqual(mapping["query"], query)

    def test_render_manifest_contains_both_api_versions_and_eight_disabled_rules(self):
        data = config()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            render(data, directory)
            artifacts = json.loads((Path(directory) / "alerts.json").read_text())
            self.assertEqual(len(artifacts), 8)
            self.assertTrue(all(not body["properties"]["enabled"] for body in artifacts.values()))
            versions = manifest(data)["alert_api_versions"]
            self.assertEqual(set(versions), set(artifacts))
            for resource, version in versions.items():
                self.assertEqual(version, "2023-12-01" if "/scheduledQueryRules/" in resource else "2018-03-01")

    def test_schema_preserves_single_target_criterion_dimension_and_metric_requirements(self):
        schema = alert_schema(True)["properties"]["data"]["properties"]
        self.assertEqual(schema["essentials"]["properties"]["alertTargetIDs"]["maxItems"], 1)
        criteria = schema["alertContext"]["properties"]["condition"]["properties"]["allOf"]
        self.assertEqual(criteria["maxItems"], 1)
        metric, logs = criteria["items"]["anyOf"]
        self.assertIn("metricNamespace", metric["required"])
        self.assertIn("searchQuery", logs["required"])
        self.assertIn("metricMeasureColumn", logs["required"])
        for variant in (metric, logs):
            self.assertEqual(variant["properties"]["dimensions"]["minItems"], 1)
            self.assertEqual(variant["properties"]["dimensions"]["maxItems"], 1)


class LogWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.data = config()
        self.data["apim_log_alerts"]["threshold_ms"] = 3456.5
        self.workflow = definition(self.data)
        self.expression = self.workflow["actions"]["Process"]["actions"]["Trusted_rule"]["actions"]["Validate_alert"]["expression"]

    def accepted(self, event):
        mapping = rule_map(self.data)[event["data"]["essentials"]["alertRule"]]
        return evaluate_guard(self.expression, event, mapping, datetime.now(timezone.utc))

    def test_metric_and_log_fired_resolved_threshold_and_null_boundaries(self):
        for _, route in routes(self.data):
            for source in ("metric", "apim_logs"):
                with self.subTest(route=route["backend_name"], source=source):
                    event = synthetic_event(self.data, route, source=source)
                    self.assertTrue(self.accepted(event))
                    criterion = event["data"]["alertContext"]["condition"]["allOf"][0]
                    for value in (criterion["threshold"], 0, -1):
                        criterion["metricValue"] = value
                        self.assertFalse(self.accepted(event))
                    resolved = synthetic_event(self.data, route, "Resolved", source)
                    resolved["data"]["essentials"]["firedDateTime"] = "2000-01-01T00:00:00Z"
                    self.assertTrue(self.accepted(resolved))
            event = synthetic_event(self.data, route, source="apim_logs")
            event["data"]["alertContext"]["condition"]["allOf"][0]["metricValue"] = None
            self.assertFalse(self.accepted(event))
            event["data"]["essentials"]["monitorCondition"] = "Resolved"
            event["data"]["essentials"]["resolvedDateTime"] = None
            self.assertTrue(self.accepted(event))
            del event["data"]["alertContext"]["condition"]["allOf"][0]["metricValue"]
            self.assertTrue(self.accepted(event))
            event["data"]["essentials"]["monitorCondition"] = "Fired"
            self.assertFalse(self.accepted(event))

    def test_mismatched_log_payloads_rejected(self):
        _, route = next(routes(self.data))
        original = synthetic_event(self.data, route, source="apim_logs")
        mutations = [
            ("essentials", "signalType", "Metric"),
            ("essentials", "monitoringService", "Platform"),
            ("essentials", "alertTargetIDs", [self.data["apim_resource_id"]]),
            ("essentials", "firedDateTime", "2000-01-01T00:00:00Z"),
            ("essentials", "firedDateTime", "2100-01-01T00:00:00Z"),
            ("criterion", "searchQuery", "ApiManagementGatewayLogs"),
            ("criterion", "metricMeasureColumn", "TotalTime"),
            ("criterion", "timeAggregation", "Average"),
            ("criterion", "threshold", 2000),
            ("criterion", "operator", "GreaterThanOrEqual"),
            ("criterion", "dimensions", [{"name": "BackendId", "value": "other-backend"}]),
            ("criterion", "dimensions", [{"name": "ModelDeploymentName", "value": route["backend_id"]}]),
        ]
        for section, key, value in mutations:
            with self.subTest(section=section, key=key, value=value):
                event = copy.deepcopy(original)
                target = (event["data"]["essentials"] if section == "essentials"
                          else event["data"]["alertContext"]["condition"]["allOf"][0])
                target[key] = value
                self.assertFalse(self.accepted(event))
        original["data"]["essentials"]["alertRule"] = route["alert_name"]
        # Missing metric fields fail the expression; Process failure takes the
        # existing 400/Rejected path, never the accepted write branch.
        with self.assertRaises(AttributeError):
            self.accepted(original)
        self.assertIn("Failed", self.workflow["actions"]["Capture_failure"]["runAfter"]["Process"])
        self.assertIn("'Rejected'", json.dumps(self.workflow["actions"]["Capture_failure"]))

    def test_notifications_label_both_sources_and_share_one_etag_write(self):
        serialized = json.dumps(self.workflow, ensure_ascii=False)
        self.assertIn("APIM 后端时延 p95", serialized)
        self.assertIn("平均总响应时延（TTLT）", serialized)
        self.assertEqual(serialized.count('"method": "PATCH"'), 1)
        self.assertIn('"If-Match": "@outputs(\'Read_etag\')"', serialized)
        self.assertIn("ResolvedIgnored", serialized)


class LogDeploymentTests(unittest.TestCase):
    def test_additive_mapping_allowed_but_removal_and_state_changes_rejected(self):
        data = config()
        legacy = load(ROOT / "examples/config.example.json")
        check_existing_mapping(data, {"properties": {"definition": definition(legacy)}})
        with self.assertRaisesRegex(AzureError, "alert names differ"):
            check_existing_mapping(legacy, {"properties": {"definition": definition(data)}})
        modified = copy.deepcopy(data)
        modified["groups"]["chat"]["named_value_name"] = "different-state"
        with self.assertRaisesRegex(AzureError, "mapping differs"):
            check_existing_mapping(modified, {"properties": {"definition": definition(data)}})
        modified = copy.deepcopy(data)
        route = modified["groups"]["chat"]["routes"][0]
        route["alert_name"], route["log_alert_name"] = route["log_alert_name"], route["alert_name"]
        with self.assertRaisesRegex(AzureError, "alert source differs"):
            check_existing_mapping(modified, {"properties": {"definition": definition(data)}})

    def test_check_disable_and_deploy_use_correct_resource_versions(self):
        data = config()
        resources = {resource: (body, version) for resource, body, version in alert_resources(data)}
        client = Mock()

        def call(method, resource, body=None, **kwargs):
            if resource in resources:
                expected, version = resources[resource]
                self.assertEqual(kwargs["version"], version)
                return copy.deepcopy(expected), {}
            if resource == workflow_id(data):
                return {"identity": {"principalId": "principal"}}, {}
            if resource.endswith("listCallbackUrl"):
                return {"value": "https://example.logic.azure.com/workflows/example?sig=PRIVATE"}, {}
            return {}, {}

        client.call.side_effect = call
        client.optional.return_value = ({}, {})
        check_alerts(data, client, disabled=True)
        self.assertEqual(disable_alerts(data, client)["disabled_alerts"], 8)
        client.optional.return_value = (None, {})
        with patch("apim_routing.deploy.preflight"):
            deploy(data, client, "https://oapi.dingtalk.com/robot/send?access_token=PRIVATE")
        puts = [c for c in client.call.call_args_list if c.args[0] == "PUT" and c.args[1] in resources]
        self.assertEqual(len(puts), 8)
        self.assertTrue(all(c.args[2]["properties"]["enabled"] is False for c in puts))
        _, route = next(routes(data))
        resources[log_alert_id(data, route)][0]["properties"]["criteria"]["allOf"][0]["query"] = "wrong"
        with self.assertRaisesRegex(AzureError, "drift"):
            check_alerts(data, client)

    def test_preflight_rejects_workspace_region_mismatch_before_writes(self):
        data = config()
        client = Mock()
        client.call.side_effect = [({}, {}), ({"location": "other"}, {})]
        with self.assertRaisesRegex(AzureError, "workspace"):
            preflight(data, client)
        self.assertTrue(all(c.args[0] == "GET" for c in client.call.call_args_list))

    def test_smoke_restores_state_for_each_source_and_requires_extended_receipt(self):
        data = config()
        state = {"chat": "region-a", "embedding": "region-c,region-d"}
        original = dict(state)
        revision = 1
        client = Mock()

        def call(method, resource, body=None, **kwargs):
            nonlocal revision
            for group in state:
                if resource == named_id(data, group):
                    if method == "PATCH":
                        self.assertEqual(kwargs["headers"]["If-Match"], str(revision))
                        state[group] = body["properties"]["value"]
                        revision += 1
                    return {"id": resource, "properties": {
                        "value": state[group], "secret": False,
                        "displayName": data["groups"][group]["named_value_name"],
                    }}, {"ETag": str(revision)}
            return {}, {}

        def invoke(callback, event):
            nonlocal revision
            essentials = event["data"]["essentials"]
            rule = rule_map(data)[essentials["alertRule"]]
            group, backend = rule["group"], rule["backend_name"]
            names = [] if state[group] == "none" else state[group].split(",")
            outcome = "ResolvedIgnored"
            if essentials["monitorCondition"] == "Fired":
                outcome = "AlreadyDegraded" if backend in names else "Updated"
                if backend not in names:
                    state[group] = ",".join(names + [backend])
                    revision += 1
            return 200, {"outcome": outcome, "result": outcome,
                         "controllerFailed": False, "notificationFailed": False}

        client.call.side_effect = call
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, \
                patch("apim_routing.deploy.preflight"), \
                patch("apim_routing.deploy.check_controller", return_value=(
                    "principal", {"properties": {"changedTime": "stable"}}, "hash", "callback")), \
                patch("apim_routing.deploy.check_alerts"), \
                patch("apim_routing.deploy.rejection_checks", return_value=3), \
                patch("apim_routing.deploy.invoke_callback", side_effect=invoke):
            receipt = Path(directory) / "receipt.json"
            result = smoke(data, client, receipt)
            self.assertEqual(result["checks"], 24)
            self.assertEqual(result["rejection_checks"], 6)
            self.assertEqual(state, original)
            evidence = json.loads(receipt.read_text())
            self.assertEqual(sum(c["outcome"] == "Updated" for c in evidence["checks"]), 8)
            self.assertEqual({c["source"] for c in evidence["checks"]}, {"metric", "apim_logs"})
            self.assertEqual(len(enable_alerts(data, client, receipt)["enabled_alerts"]), 8)
            evidence["checks"] = evidence["checks"][:12]
            receipt.write_text(json.dumps(evidence))
            with self.assertRaisesRegex(AzureError, "stale or mismatched"):
                enable_alerts(data, client, receipt)


if __name__ == "__main__":
    unittest.main()
