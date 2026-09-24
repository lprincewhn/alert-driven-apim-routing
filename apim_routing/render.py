"""Offline artifacts contain no notification or callback credentials."""

import json
from pathlib import Path
from xml.sax.saxutils import escape

from .config import action_group_id, alert_id, digest, log_alert_id, log_query, named_id, routes, workflow_id
from .workflow import definition

METRIC = "AzureOpenAITTLTInMS"
NAMESPACE = "Microsoft.CognitiveServices/accounts"
ALERT_VERSION = "2018-03-01"
LOG_ALERT_VERSION = "2023-12-01"


def policy(config):
    text = Path(__file__).with_name("policy.xml.template").read_text(encoding="utf-8")
    replacements = {"__UAMI__": config["uami_client_id"]}
    for group, spec in config["groups"].items():
        prefix = "__" + group.upper()
        replacements[prefix + "_BACKENDS__"] = ",".join(r["backend_id"] for r in spec["routes"])
        replacements[prefix + "_BACKEND_NAMES__"] = ",".join(r["backend_name"] for r in spec["routes"])
        replacements[prefix + "_NAMED__"] = "{{" + spec["named_value_name"] + "}}"
    for key, value in replacements.items():
        text = text.replace(key, escape(value, {'"': "&quot;"}))
    return text


def alert(config, route, enabled=False):
    return {
        "location": "global",
        "tags": {"managed-by": "alert-driven-apim-routing"},
        "properties": {
            "description": "Degrade route on service-side TTLT. Manual recovery only.",
            "severity": 2, "enabled": enabled,
            "scopes": [route["foundry_resource_id"]],
            "evaluationFrequency": config["evaluation_frequency"],
            "windowSize": config["window_size"], "autoMitigate": True,
            "criteria": {
                "odata.type": "Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria",
                "allOf": [{
                    "name": "TotalResponseLatency", "criterionType": "StaticThresholdCriterion",
                    "metricNamespace": NAMESPACE, "metricName": METRIC,
                    "dimensions": [{"name": "ModelDeploymentName", "operator": "Include",
                                    "values": [route["deployment_name"]]}],
                    "operator": "GreaterThan", "threshold": config["threshold_ms"],
                    "timeAggregation": "Average",
                }],
            },
            "actions": [{"actionGroupId": action_group_id(config)}],
        },
    }


def log_alert(config, route, enabled=False):
    logs = config["apim_log_alerts"]
    return {
        "location": logs["location"], "kind": "LogAlert",
        "tags": {"managed-by": "alert-driven-apim-routing"},
        "properties": {
            "description": "Degrade route on APIM diagnostic BackendTime p95. Manual recovery only.",
            "severity": 2, "enabled": enabled,
            "scopes": [logs["workspace_resource_id"]],
            "evaluationFrequency": logs["evaluation_frequency"],
            "windowSize": logs["window_size"], "autoMitigate": True,
            "skipQueryValidation": False,
            "criteria": {"allOf": [{
                "query": log_query(config, route), "metricMeasureColumn": "BackendLatencyP95Ms",
                "timeAggregation": "Maximum", "operator": "GreaterThan",
                "threshold": logs["threshold_ms"],
                "dimensions": [{"name": "BackendId", "operator": "Include", "values": [route["backend_id"]]}],
                "failingPeriods": {"numberOfEvaluationPeriods": 1, "minFailingPeriodsToAlert": 1},
            }]},
            "actions": {"actionGroups": [action_group_id(config)]},
        },
    }


def alert_resources(config):
    for _, route in routes(config):
        yield alert_id(config, route), alert(config, route), ALERT_VERSION
    if "apim_log_alerts" in config:
        for _, route in routes(config):
            yield log_alert_id(config, route), log_alert(config, route), LOG_ALERT_VERSION


def manifest(config):
    return {
        "config_sha256": digest(config),
        "workflow": workflow_id(config), "action_group": action_group_id(config),
        "named_values": {group: named_id(config, group) for group in config["groups"]},
        "alerts": [resource for resource, _, _ in alert_resources(config)],
        "alert_api_versions": {resource: version for resource, _, version in alert_resources(config)},
        "apim_log_alerts": config.get("apim_log_alerts"),
        "external_apim": config["apim_resource_id"], "external_api": config["api_id"],
        "metric": METRIC, "threshold_ms": config["threshold_ms"],
        "window_size": config["window_size"], "evaluation_frequency": config["evaluation_frequency"],
        "alerts_enabled": False, "automatic_recovery": False,
        "policy_installation": "explicit install-policy command only",
    }


def render(config, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "policy.xml").write_text(policy(config), encoding="utf-8")
    artifacts = {"workflow.json": definition(config), "manifest.json": manifest(config),
                 "alerts.json": {resource: body for resource, body, _ in alert_resources(config)}}
    for filename, value in artifacts.items():
        (directory / filename).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return list(artifacts) + ["policy.xml"]
