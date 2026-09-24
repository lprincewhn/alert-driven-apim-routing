"""Strict, secret-free configuration and deterministic resource identifiers."""

import hashlib
import json
import math
import re
from pathlib import Path
from uuid import UUID


class ConfigError(ValueError):
    pass


NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
BACKEND_NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
RESOURCE = re.compile(
    r"^/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/([^/]+)/providers/"
    r"([^/]+)/([^/]+)/([^/]+)$", re.I
)
WINDOW_MINUTES = {
    "PT1M": 1, "PT5M": 5, "PT15M": 15, "PT30M": 30,
    "PT1H": 60, "PT6H": 360, "PT12H": 720, "P1D": 1440,
}
EVALUATION_MINUTES = {
    "PT1M": 1, "PT5M": 5, "PT10M": 10, "PT15M": 15, "PT30M": 30, "PT1H": 60,
}


def require(condition, message):
    if not condition:
        raise ConfigError(message)


def keys(value, required, optional=()):
    require(isinstance(value, dict), "Configuration sections must be objects")
    require(set(required) <= set(value), "Missing required configuration keys: " +
            ", ".join(sorted(set(required) - set(value))))
    require(set(value) <= set(required) | set(optional), "Unknown configuration keys")


def name(value, label):
    require(isinstance(value, str) and NAME.fullmatch(value), "Invalid " + label)
    return value


def guid(value, label):
    try:
        require(isinstance(value, str) and str(UUID(value)) == value.lower(),
                "Invalid " + label)
    except (ValueError, TypeError, AttributeError):
        raise ConfigError("Invalid " + label) from None
    return value


def resource(value, provider, kind, label):
    match = RESOURCE.fullmatch(value) if isinstance(value, str) else None
    require(match is not None, "Expected full resource ID for " + label)
    guid(match[1], label + " subscription")
    name(match[2], label + " resource group")
    name(match[5], label + " name")
    require(match[3].lower() == provider.lower() and match[4].lower() == kind.lower(),
            "Wrong resource type for " + label)
    return value


def validate(data):
    keys(data, ("subscription_id", "controller_resource_group", "location",
                "workflow_name", "action_group_name", "action_group_short_name",
                "apim_resource_id", "api_id", "uami_client_id", "groups"),
         ("threshold_ms", "window_size", "evaluation_frequency", "apim_log_alerts"))
    guid(data["subscription_id"], "subscription_id")
    guid(data["uami_client_id"], "uami_client_id")
    for key in ("controller_resource_group", "workflow_name", "action_group_name", "api_id"):
        name(data[key], key)
    require(isinstance(data["location"], str) and re.fullmatch(r"[a-z0-9]+", data["location"]),
            "location must be an Azure location code")
    name(data["action_group_short_name"], "action_group_short_name")
    require(len(data["action_group_short_name"]) <= 12, "Action Group short name exceeds 12 characters")
    resource(data["apim_resource_id"], "Microsoft.ApiManagement", "service", "APIM")
    threshold = data.setdefault("threshold_ms", 2000)
    require(type(threshold) in (int, float) and math.isfinite(threshold) and 0 < threshold <= 86400000,
            "threshold_ms must be a finite positive number no greater than one day")
    for key, choices in (("window_size", WINDOW_MINUTES),
                         ("evaluation_frequency", EVALUATION_MINUTES)):
        value = data.setdefault(key, "PT1M")
        require(isinstance(value, str) and value in choices,
                key + " must be one of: " + ", ".join(choices))
    require(EVALUATION_MINUTES[data["evaluation_frequency"]] <= WINDOW_MINUTES[data["window_size"]],
            "evaluation_frequency must not exceed window_size")
    if "apim_log_alerts" in data:
        logs = data["apim_log_alerts"]
        keys(logs, ("workspace_resource_id", "location"),
             ("threshold_ms", "window_size", "evaluation_frequency", "min_samples"))
        resource(logs["workspace_resource_id"], "Microsoft.OperationalInsights", "workspaces", "log workspace")
        require(isinstance(logs["location"], str) and re.fullmatch(r"[a-z0-9]+", logs["location"]),
                "apim_log_alerts.location must be the workspace Azure location code")
        threshold = logs.setdefault("threshold_ms", 2000)
        require(type(threshold) in (int, float) and math.isfinite(threshold) and 0 < threshold <= 86400000,
                "apim_log_alerts.threshold_ms must be a finite positive number no greater than one day")
        samples = logs.setdefault("min_samples", 20)
        require(type(samples) is int and 1 <= samples <= 1000000,
                "apim_log_alerts.min_samples must be an integer between 1 and 1000000")
        for key, choices in (("window_size", WINDOW_MINUTES), ("evaluation_frequency", EVALUATION_MINUTES)):
            value = logs.setdefault(key, "PT5M")
            require(isinstance(value, str) and value in choices and choices[value] >= 5,
                    "apim_log_alerts." + key + " must be a supported duration of at least five minutes")
        require(EVALUATION_MINUTES[logs["evaluation_frequency"]] <= WINDOW_MINUTES[logs["window_size"]],
                "apim_log_alerts.evaluation_frequency must not exceed window_size")
    keys(data["groups"], ("chat", "embedding"))
    alerts, named = [], []
    for group, spec in data["groups"].items():
        keys(spec, ("named_value_name", "routes"))
        named.append(name(spec["named_value_name"], "named_value_name"))
        require(isinstance(spec["routes"], list) and len(spec["routes"]) == 2,
                "Each group requires exactly two routes")
        backend_names, backends, deployments = [], [], []
        for route in spec["routes"]:
            keys(route, ("backend_id", "foundry_resource_id", "deployment_name", "alert_name"),
                 ("backend_name", "token", "log_alert_name"))
            require(("log_alert_name" in route) == ("apim_log_alerts" in data),
                    "log_alert_name on every route requires apim_log_alerts and vice versa")
            if "log_alert_name" in route:
                alerts.append(name(route["log_alert_name"], "log_alert_name"))
            require(("backend_name" in route) != ("token" in route),
                    "Specify exactly one of backend_name or legacy token")
            # Normalize the legacy input only; all generated artifacts use backend_name.
            if "token" in route:
                route["backend_name"] = route.pop("token")
            backend_name = route["backend_name"]
            require(isinstance(backend_name, str) and BACKEND_NAME.fullmatch(backend_name)
                    and backend_name != "none",
                    "Backend name must be lowercase alphanumeric/hyphen and not none")
            backend_names.append(backend_name)
            backends.append(name(route["backend_id"], "backend_id"))
            alerts.append(name(route["alert_name"], "alert_name"))
            deployments.append(name(route["deployment_name"], "deployment_name"))
            resource(route["foundry_resource_id"], "Microsoft.CognitiveServices", "accounts", "Foundry")
            require(("embedding" in route["deployment_name"]) == (group == "embedding"),
                    "Deployment names must match case-sensitive Contains('embedding') classification")
        require(len(set(backend_names)) == 2, "Backend names must be unique within each group")
        require(len(set(backends)) == 2, "Backend IDs must be distinct within each group")
        require(len(set(deployments)) == 1,
                "Both routes in a group must use the same deployment name (request path is unchanged)")
    require(len(set(named)) == 2, "Named value names must be distinct")
    require(len({value.lower() for value in alerts}) == len(alerts),
            "Alert names must be globally distinct")
    return data


def load(path):
    try:
        return validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        raise ConfigError("Cannot read configuration JSON") from None


def root(config):
    return "/subscriptions/{subscription_id}/resourceGroups/{controller_resource_group}".format(**config)


def workflow_id(config):
    return root(config) + "/providers/Microsoft.Logic/workflows/" + config["workflow_name"]


def action_group_id(config):
    return root(config) + "/providers/Microsoft.Insights/actionGroups/" + config["action_group_name"]


def named_id(config, group):
    return config["apim_resource_id"] + "/namedValues/" + config["groups"][group]["named_value_name"]


def alert_id(config, route):
    return root(config) + "/providers/Microsoft.Insights/metricAlerts/" + route["alert_name"]


def log_alert_id(config, route):
    return root(config) + "/providers/Microsoft.Insights/scheduledQueryRules/" + route["log_alert_name"]


def log_query(config, route):
    # All interpolated identifiers are restricted by validate; no arbitrary KQL input.
    return "\n".join((
        "ApiManagementGatewayLogs",
        f"| where _ResourceId =~ '{config['apim_resource_id']}'",
        f"| where ApiId == '{config['api_id']}'",
        f"| where BackendId == '{route['backend_id']}'",
        "| where BackendMethod == 'POST'",
        "| extend BackendPath = tostring(parse_url(BackendUrl).Path)",
        f"| where BackendPath in ('/openai/deployments/{route['deployment_name']}/chat/completions', "
        f"'/openai/deployments/{route['deployment_name']}/embeddings')",
        "| where isnotnull(BackendTime) and BackendTime > 0 and BackendResponseCode > 0",
        "| summarize SampleCount = count(), BackendLatencyP95Ms = percentile(BackendTime, 95) by BackendId",
        f"| where SampleCount >= {config['apim_log_alerts']['min_samples']}",
        "| project BackendId, BackendLatencyP95Ms",
    ))


def routes(config):
    for group in ("chat", "embedding"):
        for route in config["groups"][group]["routes"]:
            yield group, route


def rule_map(config):
    mapping = {
        route["alert_name"]: {
            "route": group + "-" + route["backend_name"], "group": group,
            "backend_name": route["backend_name"], "account": route["foundry_resource_id"].lower(),
            "deployment": route["deployment_name"], "namedValue": named_id(config, group),
            "backend_names": [item["backend_name"] for item in config["groups"][group]["routes"]],
        } for group, route in routes(config)
    }
    if "apim_log_alerts" in config:
        for _, route in routes(config):
            mapping[route["log_alert_name"]] = {
                **mapping[route["alert_name"]], "source": "apim_logs",
                "account": config["apim_log_alerts"]["workspace_resource_id"].lower(),
                "backend_id": route["backend_id"], "query": log_query(config, route),
            }
    return mapping


def digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_routes(value, backend_names):
    if value == "none":
        return []
    degraded_backend_names = value.split(",") if isinstance(value, str) else []
    require(bool(degraded_backend_names)
            and len(set(degraded_backend_names)) == len(degraded_backend_names)
            and set(degraded_backend_names) <= set(backend_names),
            "Invalid existing degraded route list; refusing to overwrite")
    return degraded_backend_names
