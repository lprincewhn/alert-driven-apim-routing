import copy
import io
import json
import re
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

from apim_routing import azure
from apim_routing.__main__ import main, parser
from apim_routing.config import (
    ConfigError, alert_id, digest, load, named_id, parse_routes, routes, validate, workflow_id,
)
from apim_routing.deploy import (
    check_existing_mapping, check_named, deploy, enable_alerts, grant_roles, identity_payload, install_policy,
    normalized_policy, preflight, role_spec, smoke, synthetic_event, webhook_valid, workflow_payload,
    quiesce_workflow,
)
from apim_routing.render import alert, policy, render
from apim_routing.workflow import definition

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "config.example.json"


def config():
    return load(EXAMPLE)


def named(c, group, value="none"):
    return {"id": named_id(c, group), "properties": {
        "value": value, "secret": False, "displayName": c["groups"][group]["named_value_name"],
    }}


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


class ConfigurationTests(unittest.TestCase):
    def test_legacy_token_normalizes_to_backend_name(self):
        canonical = config()
        legacy = copy.deepcopy(canonical)
        for _, route in routes(legacy):
            route["token"] = route.pop("backend_name")
        normalized = validate(legacy)
        self.assertEqual(normalized, canonical)
        self.assertEqual(digest(normalized), digest(canonical))
        self.assertEqual(policy(normalized), policy(canonical))
        self.assertEqual(definition(normalized), definition(canonical))
        self.assertEqual(validate(normalized), canonical)

    def test_missing_or_ambiguous_backend_name_rejected(self):
        for legacy in ("region-a", "different"):
            data = config()
            data["groups"]["chat"]["routes"][0]["token"] = legacy
            with self.assertRaisesRegex(ConfigError, "exactly one"):
                validate(data)
        data = config()
        del data["groups"]["chat"]["routes"][0]["backend_name"]
        with self.assertRaisesRegex(ConfigError, "exactly one"):
            validate(data)
        data["groups"]["chat"]["routes"][0]["token"] = "none"
        with self.assertRaisesRegex(ConfigError, "Backend name"):
            validate(data)

    def test_example_and_defaults(self):
        data = config()
        for key in ("threshold_ms", "window_size", "evaluation_frequency"):
            del data[key]
        validated = validate(data)
        self.assertEqual(validated["threshold_ms"], 2000)
        self.assertEqual(validated["window_size"], "PT1M")
        self.assertEqual(validated["evaluation_frequency"], "PT1M")

    def test_unknown_keys_and_secrets_rejected(self):
        for key in ("webhook", "token", "password", "typo"):
            with self.subTest(key=key):
                data = config()
                data[key] = "credential"
                with self.assertRaises(ConfigError):
                    validate(data)

    def test_thresholds(self):
        for value in (0, -1, True, "2000", float("inf"), float("nan"), 86400001):
            with self.subTest(value=value):
                data = config()
                data["threshold_ms"] = value
                with self.assertRaises(ConfigError):
                    validate(data)

    def test_alert_timing_combinations(self):
        windows = {"PT1M": 1, "PT5M": 5, "PT15M": 15, "PT30M": 30,
                   "PT1H": 60, "PT6H": 360, "PT12H": 720, "P1D": 1440}
        frequencies = {"PT1M": 1, "PT5M": 5, "PT10M": 10,
                       "PT15M": 15, "PT30M": 30, "PT1H": 60}
        for window, window_minutes in windows.items():
            for frequency, frequency_minutes in frequencies.items():
                with self.subTest(window=window, frequency=frequency):
                    data = config()
                    data.update(window_size=window, evaluation_frequency=frequency)
                    if frequency_minutes > window_minutes:
                        with self.assertRaisesRegex(ConfigError, "must not exceed"):
                            validate(data)
                    else:
                        self.assertEqual(validate(data)["window_size"], window)
                        for _, route in routes(data):
                            properties = alert(data, route)["properties"]
                            self.assertEqual(properties["windowSize"], window)
                            self.assertEqual(properties["evaluationFrequency"], frequency)

    def test_invalid_alert_timing(self):
        for key, unsupported in (("window_size", "PT10M"),
                                 ("evaluation_frequency", "PT6H")):
            for value in (unsupported, "", "PT0M", "PT2M", "pt5m", "PT60S",
                          None, True, 5, [], {}):
                with self.subTest(key=key, value=value):
                    data = config()
                    data[key] = value
                    with self.assertRaisesRegex(ConfigError, key + " must be one of"):
                        validate(data)

    def test_fixed_groups(self):
        data = config()
        data["groups"]["chat"]["routes"].pop()
        with self.assertRaises(ConfigError):
            validate(data)

    def test_duplicate_backend_names_alerts_and_backends(self):
        for key in ("backend_name", "backend_id", "alert_name"):
            data = config()
            data["groups"]["chat"]["routes"][1][key] = data["groups"]["chat"]["routes"][0][key]
            with self.assertRaises(ConfigError):
                validate(data)

    def test_bad_route_values(self):
        for backend_name in ("none", "A", "x,y", "x'quote", "{{injection}}", ""):
            data = config()
            data["groups"]["chat"]["routes"][0]["backend_name"] = backend_name
            with self.assertRaises(ConfigError):
                validate(data)

    def test_deployment_routing_invariant(self):
        for group, name in (("chat", "contains-embedding"), ("embedding", "Embedding")):
            data = config()
            for route in data["groups"][group]["routes"]:
                route["deployment_name"] = name
            with self.assertRaises(ConfigError):
                validate(data)
        data = config()
        data["groups"]["chat"]["routes"][0]["deployment_name"] = "other-model"
        with self.assertRaises(ConfigError):
            validate(data)

    def test_resource_ids_and_client_id(self):
        for key in ("apim_resource_id", "uami_client_id", "subscription_id"):
            data = config()
            data[key] = "not-an-id"
            with self.assertRaises(ConfigError):
                validate(data)

    def test_config_digest_stable(self):
        self.assertEqual(digest(config()), digest(config()))
        modified = config()
        modified["threshold_ms"] = 3000
        self.assertNotEqual(digest(config()), digest(modified))

    def test_strict_backend_name_state_parser(self):
        self.assertEqual(parse_routes("none", ["a", "b"]), [])
        self.assertEqual(parse_routes("a,b", ["a", "b"]), ["a", "b"])
        for raw in ("", " a", "a,", "a,a", "none,a", "c", None):
            with self.assertRaises(ConfigError):
                parse_routes(raw, ["a", "b"])


class GeneratorTests(unittest.TestCase):
    def test_backend_names_consistent_across_generated_artifacts(self):
        data = config()
        workflow = definition(data)
        mapping = workflow["actions"]["Process"]["actions"]["Rule_map"]["inputs"]
        for group, route in routes(data):
            rule = mapping[route["alert_name"]]
            self.assertEqual(rule["backend_name"], route["backend_name"])
            self.assertEqual(rule["backend_names"],
                             [r["backend_name"] for r in data["groups"][group]["routes"]])
            self.assertEqual(rule["route"], group + "-" + route["backend_name"])
            self.assertNotIn("region", rule)
            self.assertNotIn("allowed", rule)
        dumped = json.dumps(workflow)
        for obsolete in ("outputs('Tokens')", "variables('Region')", "['region']", "['allowed']"):
            self.assertNotIn(obsolete, dumped)
        actions = set()
        def collect_actions(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "actions" and isinstance(child, dict):
                        actions.update(child)
                    collect_actions(child)
            elif isinstance(value, list):
                for child in value:
                    collect_actions(child)
        collect_actions(workflow)
        for reference in re.findall(r"(?:outputs|body|actions)\('([^']+)'\)", dumped):
            self.assertIn(reference, actions)
        for node in walk(workflow):
            for predecessor in node.get("runAfter", {}):
                self.assertIn(predecessor, actions)
        variables = {v["name"] for v in workflow["actions"]["Initialize"]["inputs"]["variables"]}
        for reference in re.findall(r"variables\('([^']+)'\)", dumped):
            self.assertIn(reference, variables)
        for node in walk(workflow):
            if node.get("type") == "SetVariable":
                self.assertIn(node["inputs"]["name"], variables)
        root = ET.fromstring(policy(data))
        names = {v.attrib["name"] for v in root.iter("set-variable")}
        self.assertTrue({"backend_name", "backend_names"} <= names)
        self.assertFalse({"backend-name", "backend-names"} & names)
        self.assertIn("backend-id", root.find("./backend/retry/set-backend-service").attrib)
        self.assertEqual(root.find("./outbound/set-header").attrib["name"], "X-Backend-Region")

    def test_policy_preserves_runtime_semantics(self):
        root = ET.fromstring(policy(config()))
        retry = root.find("./backend/retry")
        self.assertEqual(retry.attrib["count"], "1")
        self.assertIn("StatusCode == 429", retry.attrib["condition"])
        self.assertIn("StatusCode >= 500", retry.attrib["condition"])
        forward = retry.find("forward-request")
        self.assertEqual(forward.attrib, {"buffer-request-body": "true"})
        identity = root.find("./inbound/authentication-managed-identity")
        self.assertEqual(identity.attrib["client-id"], config()["uami_client_id"])
        self.assertEqual(identity.attrib["resource"], "https://cognitiveservices.azure.com")
        self.assertEqual(root.find("./outbound/set-header").attrib["name"], "X-Backend-Region")
        content = policy(config())
        for expected in ('Contains("embedding")', "preferred.Length == 0", "GetHashCode",
                         'value="{{chat-degraded-routes}}"', "OriginalUrl.Path"):
            self.assertIn(expected, content)
        self.assertNotIn("__CHAT_", content)
        self.assertEqual(normalized_policy(content), normalized_policy(content.replace("\n    <", "\n  <")))

    def test_workflow_guard_structure(self):
        data = config()
        data["threshold_ms"] = 3456
        workflow = definition(data)
        dumped = json.dumps(workflow)
        for expected in ("3456", "ResolvedIgnored", "AlreadyDegraded", "If-Match",
                         "MissingOrUnsafeETag", "ModelDeployment", "NotificationFailed"):
            self.assertIn(expected.lower(), dumped.lower())
        self.assertNotIn("2000", dumped)
        for expected in ("-30", "utcNow(), 2", "GreaterThan", "Average", "AzureOpenAITTLTInMS"):
            self.assertIn(expected, dumped)
        self.assertEqual(workflow["triggers"]["receive"]["runtimeConfiguration"]["concurrency"]["runs"], 1)
        self.assertEqual(workflow["parameters"]["dingtalkWebhook"],
                         {"defaultValue": "none", "type": "SecureString"})
        actions = list(walk(workflow))
        dingtalk = [a for a in actions if a.get("type") == "Http"
                    and a["inputs"].get("uri") == "@parameters('dingtalkWebhook')"][0]
        self.assertEqual(dingtalk["runtimeConfiguration"]["secureData"]["properties"], ["inputs", "outputs"])
        writes = [a for a in actions if a.get("type") == "Http" and a["inputs"]["method"] == "PATCH"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["inputs"]["retryPolicy"], {"type": "none"})
        self.assertEqual(writes[0]["inputs"]["headers"], {"If-Match": "@outputs('Read_etag')"})

    def test_dingtalk_notification_is_chinese_without_changing_response_codes(self):
        workflow = definition(config())
        notify = workflow["actions"]["Notify"]
        message = notify["actions"]["DingTalk"]["inputs"]["body"]["text"]["content"]
        for expected in (
            "Azure APIM 路由告警", "业务类型：", "对话", "向量嵌入", "后端名称：",
            "已加入降级名单", "已在降级名单中，无需重复更新",
            "告警已恢复，保留降级标记，需人工恢复路由",
            "路由控制器处理失败", "平均总响应时延（TTLT）：", "毫秒",
            "变更前降级名单：", "变更后降级名单：", "错误代码：",
            "名单最终状态请以 APIM 为准", "decodeUriComponent('%0A')",
        ):
            self.assertIn(expected, message)
        for outcome in ("Updated", "AlreadyDegraded", "ResolvedIgnored", "ControllerFailed"):
            self.assertIn(f"equals(variables('Outcome'), '{outcome}')", message)
        self.assertNotIn(" outcome=", message)
        self.assertEqual(notify["expression"], "@variables('Accepted')")
        response = workflow["actions"]["Respond"]["inputs"]["body"]
        self.assertEqual(response["outcome"], "@variables('Outcome')")
        self.assertEqual(response["result"],
                         "@if(variables('NotificationFailed'), 'NotificationFailed', variables('Outcome'))")

    def test_rules_all_resources_parameterized(self):
        data = config()
        dumped = json.dumps(definition(data))
        for group, route in routes(data):
            self.assertIn(route["alert_name"], dumped)
            self.assertIn(route["foundry_resource_id"].lower(), dumped)
            self.assertIn(named_id(data, group), dumped)

    def test_alerts_disabled_and_metric_contract(self):
        data = config()
        for _, route in routes(data):
            props = alert(data, route)["properties"]
            self.assertFalse(props["enabled"])
            self.assertEqual(props["scopes"], [route["foundry_resource_id"]])
            self.assertEqual(props["evaluationFrequency"], "PT1M")
            self.assertEqual(props["windowSize"], "PT5M")
            metric = props["criteria"]["allOf"][0]
            self.assertEqual(metric["threshold"], 2000)
            self.assertEqual(metric["dimensions"][0]["values"], [route["deployment_name"]])

    def test_offline_render_never_uses_secret_env_or_azure(self):
        destination = ROOT / "build" / "unit-render"
        try:
            with patch.dict("os.environ", {"DINGTALK_WEBHOOK": "SENSITIVE-SENTINEL"}), \
                    patch("subprocess.run", side_effect=AssertionError("No external commands")):
                data = config()
                data.update(window_size="PT1H", evaluation_frequency="PT10M")
                files = render(validate(data), destination)
            self.assertEqual(len(files), 4)
            manifest = json.loads((destination / "manifest.json").read_text())
            self.assertEqual(manifest["window_size"], "PT1H")
            self.assertEqual(manifest["evaluation_frequency"], "PT10M")
            alerts = json.loads((destination / "alerts.json").read_text())
            self.assertEqual(len(alerts), 4)
            for item in alerts.values():
                self.assertEqual(item["properties"]["windowSize"], "PT1H")
                self.assertEqual(item["properties"]["evaluationFrequency"], "PT10M")
                self.assertFalse(item["properties"]["enabled"])
            workflow_text = (destination / "workflow.json").read_text(encoding="utf-8")
            self.assertEqual(json.loads(workflow_text), definition(config()))
            self.assertIn("Azure APIM 路由告警", workflow_text)
            self.assertNotIn("\\u8def", workflow_text)
            self.assertEqual(json.loads(workflow_text)["parameters"]["dingtalkWebhook"],
                             {"defaultValue": "none", "type": "SecureString"})
            for file in destination.iterdir():
                self.assertNotIn("SENSITIVE-SENTINEL", file.read_text())
        finally:
            shutil.rmtree(destination, ignore_errors=True)

    def test_synthetic_event_contract(self):
        data = config()
        _, route = next(routes(data))
        event = synthetic_event(data, route, "Resolved")
        self.assertEqual(event["data"]["essentials"]["monitorCondition"], "Resolved")
        self.assertEqual(event["data"]["alertContext"]["condition"]["allOf"][0]["metricValue"], 0)


class DeploymentTests(unittest.TestCase):
    def test_legacy_workflow_mapping_migrates_without_reinterpreting_state(self):
        data = config()
        existing = {"properties": {"definition": definition(data)}}
        mapping = existing["properties"]["definition"]["actions"]["Process"]["actions"]["Rule_map"]["inputs"]
        for rule in mapping.values():
            rule["region"] = rule.pop("backend_name")
            rule["allowed"] = rule.pop("backend_names")
        original = copy.deepcopy(existing)
        check_existing_mapping(data, existing)
        self.assertEqual(existing, original)
        rule = next(iter(mapping.values()))
        for field, value in (("region", "different"), ("allowed", ["different"]),
                             ("namedValue", "/different"), ("group", "different")):
            modified = copy.deepcopy(existing)
            first = next(iter(modified["properties"]["definition"]["actions"]["Process"]["actions"]["Rule_map"]["inputs"].values()))
            first[field] = value
            with self.assertRaisesRegex(azure.AzureError, "mapping differs"):
                check_existing_mapping(data, modified)
        for legacy, canonical in (("region", "backend_name"), ("allowed", "backend_names")):
            rule[canonical] = rule[legacy]
            with self.assertRaisesRegex(azure.AzureError, "Ambiguous"):
                check_existing_mapping(data, existing)
            del rule[canonical]

    def test_partial_existing_deployment_stays_disabled_and_preserves_identity(self):
        data = config()
        existing = {
            "location": data["location"],
            "identity": {"type": "SystemAssigned", "principalId": "keep-principal"},
            "properties": {"state": "Enabled", "definition": definition(data)},
        }
        current = copy.deepcopy(existing)
        client = Mock()

        def optional(resource, *args):
            if resource == workflow_id(data):
                return copy.deepcopy(existing), {"ETag": "before-disable"}
            for group in data["groups"]:
                if resource == named_id(data, group):
                    return named(data, group), {}
            return None, {}

        def call(method, resource, body=None, **kwargs):
            if resource.endswith("/disable"):
                current["properties"]["state"] = "Disabled"
                return {}, {}
            if resource.endswith("/runs"):
                return {"value": []}, {}
            if resource == workflow_id(data):
                if method == "PUT":
                    self.assertEqual(body["properties"]["state"], "Disabled")
                    self.assertEqual(kwargs["headers"], {"If-Match": "after-disable"})
                    current.update(copy.deepcopy(body))
                    current["identity"]["principalId"] = "keep-principal"
                return copy.deepcopy(current), {"ETag": "after-disable"}
            if resource.endswith("listCallbackUrl"):
                return {"value": "https://example.logic.azure.com/workflows/example?sig=PRIVATE"}, {}
            if "/actionGroups/" in resource and method == "PUT":
                raise azure.AzureError("Group update failed; details suppressed", 403)
            self.fail("Unexpected operation")

        client.optional.side_effect = optional
        client.call.side_effect = call
        with patch("apim_routing.deploy.preflight"):
            with self.assertRaises(azure.AzureError):
                deploy(data, client, "https://oapi.dingtalk.com/robot/send?access_token=PRIVATE")
        self.assertEqual(current["properties"]["state"], "Disabled")
        self.assertEqual(current["identity"]["principalId"], "keep-principal")
        self.assertFalse(any(call.args[1].endswith("/enable") for call in client.call.call_args_list))

    def test_existing_mapping_rejects_orphan_alerts_or_state_reinterpretation(self):
        data = config()
        existing = {"properties": {"definition": definition(data)}}
        check_existing_mapping(data, existing)
        changed = copy.deepcopy(data)
        changed["groups"]["chat"]["routes"][0]["alert_name"] = "renamed-alert"
        with self.assertRaisesRegex(azure.AzureError, "alert names differ"):
            check_existing_mapping(changed, existing)
        changed = copy.deepcopy(data)
        changed["groups"]["chat"]["routes"][0]["backend_name"] = "different-backend"
        with self.assertRaisesRegex(azure.AzureError, "mapping differs"):
            check_existing_mapping(changed, existing)
        with self.assertRaisesRegex(azure.AzureError, "not a recognized"):
            check_existing_mapping(data, {"properties": {}})

    def test_existing_workflow_is_disabled_and_drained_before_update(self):
        data = config()
        client = Mock()
        client.call.side_effect = [
            ({}, {}), ({"properties": {"state": "Disabled"}}, {}), ({"value": []}, {}),
        ]
        quiesce_workflow(data, client, {"properties": {"state": "Enabled"}})
        calls = client.call.call_args_list
        self.assertEqual(calls[0].args[:2], ("POST", workflow_id(data) + "/disable"))
        self.assertEqual(calls[2].args[:2], ("GET", workflow_id(data) + "/runs"))
        self.assertIn("Running", calls[2].kwargs["query"]["$filter"])
        client.call.reset_mock()
        client.call.side_effect = [
            ({"properties": {"state": "Disabled"}}, {}), ({"value": [{"name": "active-run"}]}, {}),
        ]
        with self.assertRaisesRegex(azure.AzureError, "active runs"):
            quiesce_workflow(data, client, {"properties": {"state": "Disabled"}})
        self.assertTrue(all(call.args[0] == "GET" for call in client.call.call_args_list))

    def test_failed_smoke_invalidates_previous_receipt(self):
        receipt = ROOT / "build" / "unit-failed-receipt.json"
        receipt.parent.mkdir(exist_ok=True)
        receipt.write_text('{"old": "success"}')
        with patch("apim_routing.deploy.preflight", side_effect=azure.AzureError("Denied", 403)):
            with self.assertRaises(azure.AzureError):
                smoke(config(), Mock(), receipt)
        self.assertFalse(receipt.exists())

    def test_preflight_reads_only_and_matches_foundry_endpoints(self):
        data = config()
        client = Mock()

        def respond(method, resource, *args, **kwargs):
            self.assertEqual(method, "GET")
            if resource == data["apim_resource_id"]:
                return {"properties": {"provisioningState": "Succeeded"}, "identity": {
                    "userAssignedIdentities": {"/generic/identity": {"clientId": data["uami_client_id"]}}}}, {}
            if resource.endswith("/operations"):
                return {"value": [{"properties": {"urlTemplate": "/deployments/{deployment-id}/chat/completions"}}]}, {}
            if "/backends/" in resource:
                return {"properties": {"protocol": "http", "url": "https://example.invalid"}}, {}
            return {"properties": {"provisioningState": "Succeeded", "endpoint": "https://example.invalid/"}}, {}

        client.call.side_effect = respond
        client.optional.return_value = (None, {})
        preflight(data, client)
        original = respond

        def mismatch(method, resource, *args, **kwargs):
            value, headers = original(method, resource, *args, **kwargs)
            if "/backends/" in resource:
                value["properties"]["url"] = "https://different.invalid"
            return value, headers

        client.call.side_effect = mismatch
        with self.assertRaisesRegex(azure.AzureError, "Backend URL"):
            preflight(data, client)

    def test_smoke_exercises_and_restores_existing_state_then_enables(self):
        data = config()
        receipt = ROOT / "build" / "unit-smoke-receipt.json"
        state = {"chat": "region-a,region-b", "embedding": "region-c"}
        original = dict(state)
        client = Mock()
        revision = [1]

        def call(method, resource, body=None, **kwargs):
            group = next((g for g in state if resource == named_id(data, g)), None)
            if group:
                if method == "PATCH":
                    self.assertEqual(kwargs["headers"]["If-Match"], str(revision[0]))
                    state[group] = body["properties"]["value"]
                    revision[0] += 1
                return named(data, group, state[group]), {"ETag": str(revision[0])}
            return {}, {}

        def invoke(callback, event):
            essentials = event["data"]["essentials"]
            group, route = next((g, r) for g, r in routes(data) if r["alert_name"] == essentials["alertRule"])
            backend_names = parse_routes(state[group], [r["backend_name"] for r in data["groups"][group]["routes"]])
            outcome = "ResolvedIgnored"
            if essentials["monitorCondition"] == "Fired":
                outcome = "AlreadyDegraded" if route["backend_name"] in backend_names else "Updated"
                if route["backend_name"] not in backend_names:
                    backend_names.append(route["backend_name"])
                    state[group] = ",".join(backend_names)
                    revision[0] += 1
            return 200, {"outcome": outcome, "result": outcome,
                         "controllerFailed": False, "notificationFailed": False}

        client.call.side_effect = call
        try:
            with patch("apim_routing.deploy.preflight"), \
                    patch("apim_routing.deploy.check_controller", return_value=(
                        "principal", {"properties": {"changedTime": "stable"}}, "hash", "callback")), \
                    patch("apim_routing.deploy.check_alerts"), \
                    patch("apim_routing.deploy.rejection_checks", return_value=3), \
                    patch("apim_routing.deploy.invoke_callback", side_effect=invoke):
                result = smoke(data, client, receipt)
                self.assertEqual(result["checks"], 12)
                self.assertEqual(state, original)
                self.assertTrue(receipt.exists())
                evidence = json.loads(receipt.read_text())
                self.assertEqual(sum(c["outcome"] == "Updated" for c in evidence["checks"]), 4)
                enabled = enable_alerts(data, client, receipt)
                self.assertEqual(len(enabled["enabled_alerts"]), 4)
        finally:
            receipt.unlink(missing_ok=True)

    def test_enable_partial_failure_disables_every_rule(self):
        data = config()
        receipt = ROOT / "build" / "unit-enable-receipt.json"
        receipt.parent.mkdir(exist_ok=True)
        from apim_routing.deploy import stamp
        receipt.write_text(json.dumps({
            "completed_at": stamp(), "restored": True, "config_sha256": digest(data),
            "principal_id": "principal", "workflow_changed_time": "stable", "policy_sha256": "hash",
            "checks": [{}] * 12, "rejection_checks": 3,
        }))
        client = Mock()
        client.call.side_effect = [({}, {}), azure.AzureError("Denied", 403)]
        try:
            with patch("apim_routing.deploy.preflight"), \
                    patch("apim_routing.deploy.check_controller", return_value=(
                        "principal", {"properties": {"changedTime": "stable"}}, "hash", "callback")), \
                    patch("apim_routing.deploy.check_alerts"), \
                    patch("apim_routing.deploy.disable_alerts") as disable:
                with self.assertRaises(azure.AzureError):
                    enable_alerts(data, client, receipt)
                disable.assert_called_once_with(data, client, missing_ok=True)
        finally:
            receipt.unlink(missing_ok=True)

    def test_workflow_preserves_identity_state_access_control_and_tags(self):
        data = config()
        existing = {
            "location": "existinglocation", "tags": {"keep": "me"},
            "identity": {"type": "SystemAssigned, UserAssigned", "principalId": "existing-principal",
                         "userAssignedIdentities": {"/existing/identity": {"clientId": "existing-client"}}},
            "properties": {"state": "Disabled", "accessControl": {"triggers": {"allowedCallerIpAddresses": []}}},
        }
        value = workflow_payload(data, "SENSITIVE-SENTINEL", existing)
        self.assertEqual(value["identity"]["type"], existing["identity"]["type"])
        self.assertEqual(value["identity"]["userAssignedIdentities"], {"/existing/identity": {}})
        self.assertEqual(value["properties"]["state"], "Disabled")
        self.assertEqual(value["properties"]["accessControl"], existing["properties"]["accessControl"])
        self.assertEqual(value["location"], existing["location"])
        self.assertEqual(value["tags"], existing["tags"])
        self.assertNotIn("SENSITIVE-SENTINEL", json.dumps(value["properties"]["definition"]))

    def test_no_silent_identity_replacement(self):
        with self.assertRaises(azure.AzureError):
            identity_payload({"identity": {"type": "UserAssigned"}})

    def test_named_value_validation(self):
        data = config()
        check_named(data, "chat", named(data, "chat", "region-a"))
        for key, value in (("secret", True), ("value", "unknown"), ("displayName", "other")):
            item = named(data, "chat")
            item["properties"][key] = value
            with self.assertRaises((azure.AzureError, ConfigError)):
                check_named(data, "chat", item)

    def test_deploy_preserves_existing_nv_and_never_installs_policy_or_roles(self):
        data = config()
        client = Mock()
        client.optional.side_effect = lambda resource, *a: (
            (named(data, "chat", "region-a"), {}) if resource == named_id(data, "chat") else
            (named(data, "embedding"), {}) if resource == named_id(data, "embedding") else (None, {}))

        def call(method, resource, body=None, **kwargs):
            if resource == workflow_id(data):
                return {"identity": {"principalId": "new-principal"}}, {}
            if resource.endswith("listCallbackUrl"):
                return {"value": "https://example.logic.azure.com/workflows/example?sig=PRIVATE"}, {}
            return {}, {}

        client.call.side_effect = call
        with patch("apim_routing.deploy.preflight"):
            result = deploy(data, client, "https://oapi.dingtalk.com/robot/send?access_token=PRIVATE")
        self.assertFalse(result["alerts_enabled"])
        writes = [call.args for call in client.call.call_args_list if call.args[0] in ("PUT", "PATCH")]
        self.assertEqual(len(writes), 6)
        for call in writes:
            self.assertNotIn("/namedValues/", call[1])
            self.assertNotIn("/backends/", call[1])
            self.assertNotIn("/policies/", call[1])
            self.assertNotIn("/roleAssignments/", call[1])

    def test_role_scope_and_denial_is_fatal(self):
        data = config()
        resource, body = role_spec(data, "chat", "controller-principal")
        self.assertTrue(resource.startswith(named_id(data, "chat") + "/providers/Microsoft.Authorization/roleAssignments/"))
        self.assertTrue(body["properties"]["roleDefinitionId"].endswith("b24988ac-6180-42a0-ab88-20f7382dd24c"))
        client = Mock()
        client.call.side_effect = [(named(data, "chat"), {}), azure.AzureError("Denied", 403)]
        with patch("apim_routing.deploy.principal", return_value=("controller-principal", {})):
            with self.assertRaises(azure.AzureError) as error:
                grant_roles(data, client)
        self.assertEqual(error.exception.status, 403)

    def test_enable_requires_valid_smoke(self):
        client = Mock()
        with patch("apim_routing.deploy.preflight"), \
                patch("apim_routing.deploy.check_controller", return_value=("principal", {}, "hash", "callback")), \
                patch("apim_routing.deploy.check_alerts"):
            with self.assertRaises(azure.AzureError):
                enable_alerts(config(), client, ROOT / "does-not-exist.json")
        client.call.assert_not_called()

    def test_etag_required(self):
        self.assertEqual(azure.etag({"ETag": '"abc"'}), '"abc"')
        self.assertEqual(azure.etag({"etag": '"def"'}), '"def"')
        for headers in ({}, {"ETag": "*"}):
            with self.assertRaises(azure.AzureError):
                azure.etag(headers)

    def test_webhook_and_callback_validation(self):
        webhook_valid("https://oapi.dingtalk.com/robot/send?access_token=PLACEHOLDER")
        for url in ("http://oapi.dingtalk.com/robot/send?access_token=x",
                    "https://evil.invalid/?access_token=x",
                    "https://oapi.dingtalk.com/robot/send?access_token=x&timestamp=1&sign=x"):
            with self.assertRaises(azure.AzureError):
                webhook_valid(url)
        for url in ("https://evil.invalid/?sig=x", "http://a.logic.azure.com/?sig=x"):
            with self.assertRaises(azure.AzureError):
                azure.callback_host(url)

    def test_http_errors_never_echo_secrets(self):
        secret = "https://example.logic.azure.com/?sig=PRIVATE"
        client = Mock()
        client.open.side_effect = HTTPError(secret, 403, "PRIVATE", {}, io.BytesIO(b"PRIVATE"))
        with patch("apim_routing.azure.build_opener", return_value=client):
            with self.assertRaises(azure.AzureError) as error:
                azure.request(secret, "POST", {"secret": "PRIVATE"})
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertEqual(error.exception.status, 403)

    def test_cli_does_not_authenticate_offline(self):
        with patch("apim_routing.__main__.Azure", side_effect=AssertionError("Must be offline")), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["--config", str(EXAMPLE), "validate"]), 0)

    def test_cli_requires_confirmation(self):
        for args in (["install-policy", "--backup", "backup.json"],
                     ["smoke"], ["enable-alerts", "--smoke-receipt", "receipt.json"]):
            with patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit):
                    parser().parse_args(["--config", str(EXAMPLE)] + args)


if __name__ == "__main__":
    unittest.main()
