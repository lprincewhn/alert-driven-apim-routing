"""Offline artifacts contain no notification or callback credentials."""

import json
from pathlib import Path
from xml.sax.saxutils import escape

from .config import action_group_id, alert_id, digest, named_id, routes, workflow_id
from .workflow import definition

METRIC = "AzureOpenAITTLTInMS"
NAMESPACE = "Microsoft.CognitiveServices/accounts"


def policy(config):
    text = Path(__file__).with_name("policy.xml.template").read_text(encoding="utf-8")
    replacements = {"__UAMI__": config["uami_client_id"]}
    for group, spec in config["groups"].items():
        prefix = "__" + group.upper()
        replacements[prefix + "_BACKENDS__"] = ",".join(r["backend_id"] for r in spec["routes"])
        replacements[prefix + "_TOKENS__"] = ",".join(r["token"] for r in spec["routes"])
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


def manifest(config):
    return {
        "config_sha256": digest(config),
        "workflow": workflow_id(config), "action_group": action_group_id(config),
        "named_values": {group: named_id(config, group) for group in config["groups"]},
        "alerts": [alert_id(config, route) for _, route in routes(config)],
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
                 "alerts.json": {alert_id(config, r): alert(config, r) for _, r in routes(config)}}
    for filename, value in artifacts.items():
        (directory / filename).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return list(artifacts) + ["policy.xml"]
