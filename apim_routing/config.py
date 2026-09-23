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
TOKEN = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
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
         ("threshold_ms", "window_size", "evaluation_frequency"))
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
    keys(data["groups"], ("chat", "embedding"))
    alerts, named = [], []
    for group, spec in data["groups"].items():
        keys(spec, ("named_value_name", "routes"))
        named.append(name(spec["named_value_name"], "named_value_name"))
        require(isinstance(spec["routes"], list) and len(spec["routes"]) == 2,
                "Each group requires exactly two routes")
        tokens, backends, deployments = [], [], []
        for route in spec["routes"]:
            keys(route, ("token", "backend_id", "foundry_resource_id", "deployment_name", "alert_name"))
            token = route["token"]
            require(isinstance(token, str) and TOKEN.fullmatch(token) and token != "none",
                    "Route token must be lowercase alphanumeric/hyphen and not none")
            tokens.append(token)
            backends.append(name(route["backend_id"], "backend_id"))
            alerts.append(name(route["alert_name"], "alert_name"))
            deployments.append(name(route["deployment_name"], "deployment_name"))
            resource(route["foundry_resource_id"], "Microsoft.CognitiveServices", "accounts", "Foundry")
            require(("embedding" in route["deployment_name"]) == (group == "embedding"),
                    "Deployment names must match case-sensitive Contains('embedding') classification")
        require(len(set(tokens)) == 2, "Route tokens must be unique within each group")
        require(len(set(backends)) == 2, "Backend IDs must be distinct within each group")
        require(len(set(deployments)) == 1,
                "Both routes in a group must use the same deployment name (request path is unchanged)")
    require(len(set(named)) == 2, "Named value names must be distinct")
    require(len(set(alerts)) == 4, "Alert names must be globally distinct")
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


def routes(config):
    for group in ("chat", "embedding"):
        for route in config["groups"][group]["routes"]:
            yield group, route


def rule_map(config):
    return {
        route["alert_name"]: {
            "route": group + "-" + route["token"], "group": group,
            "region": route["token"], "account": route["foundry_resource_id"].lower(),
            "deployment": route["deployment_name"], "namedValue": named_id(config, group),
            "allowed": [item["token"] for item in config["groups"][group]["routes"]],
        } for group, route in routes(config)
    }


def digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_routes(value, allowed):
    if value == "none":
        return []
    tokens = value.split(",") if isinstance(value, str) else []
    require(bool(tokens) and len(set(tokens)) == len(tokens) and set(tokens) <= set(allowed),
            "Invalid existing degraded route list; refusing to overwrite")
    return tokens
