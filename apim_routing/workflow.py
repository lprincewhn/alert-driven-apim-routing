"""Secret-free WDL generator; guarded ETag union, no automatic recovery."""

from .config import rule_map

FINISHED = ["Succeeded", "Failed", "TimedOut", "Skipped"]


def sequence(**actions):
    previous = None
    for name, action in actions.items():
        if previous:
            action["runAfter"] = {previous: ["Succeeded"]}
        previous = name
    return actions


def set_value(name, value):
    return {"type": "SetVariable", "inputs": {"name": name, "value": value}}


def condition(expression, actions, otherwise=None):
    return {
        "type": "If", "expression": expression, "actions": actions,
        "else": {"actions": otherwise or {}},
    }


def controller_error(code):
    return sequence(
        Error_outcome=set_value("Outcome", "ControllerFailed"),
        Error_code=set_value("ErrorCode", code),
        Error_status=set_value("StatusCode", 409),
        Error_flag=set_value("ControllerFailed", True),
    )


def http_action(method, body=None, headers=None):
    inputs = {
        "method": method,
        "uri": "@concat('https://management.azure.com', "
               "outputs('Match_rule')?['namedValue'], '?api-version=2024-05-01')",
        "authentication": {
            "type": "ManagedServiceIdentity",
            "audience": "https://management.azure.com/",
        },
        "retryPolicy": {"type": "none"},
    }
    if body is not None:
        inputs["body"] = body
    if headers:
        inputs["headers"] = headers
    return {"type": "Http", "inputs": inputs}


def alert_schema(include_logs=False):
    string = {"type": "string", "minLength": 1}
    numeric = {"type": ["number", "string"]}
    essentials = {
        "alertRule": string, "signalType": string, "monitoringService": string,
        "monitorCondition": {"type": "string", "enum": ["Fired", "Resolved"]},
        "firedDateTime": string,
        "resolvedDateTime": {"type": ["string", "null"]},
        "alertTargetIDs": {
            "type": "array", "minItems": 1, "maxItems": 1, "items": string,
        },
    }
    criterion = {
        "metricName": string, "metricNamespace": string, "operator": string,
        "timeAggregation": string, "threshold": numeric, "metricValue": numeric,
        "dimensions": {
            "type": "array", "minItems": 1, "maxItems": 1,
            "items": {
                "type": "object", "required": ["name", "value"],
                "properties": {"name": string, "value": string},
            },
        },
    }
    criterion_schema = {"type": "object", "required": list(criterion), "properties": criterion}
    if include_logs:
        log_criterion = {
            key: value for key, value in criterion.items()
            if key not in ("metricName", "metricNamespace")
        }
        log_criterion.update({
            "searchQuery": string, "metricMeasureColumn": string,
            "metricValue": {"type": ["number", "string", "null"]},
        })
        criterion_schema = {"anyOf": [
            criterion_schema,
            {"type": "object", "required": [key for key in log_criterion if key != "metricValue"],
             "properties": log_criterion},
        ]}
    return {
        "type": "object", "required": ["schemaId", "data"],
        "properties": {
            "schemaId": {"type": "string", "enum": ["azureMonitorCommonAlertSchema"]},
            "data": {
                "type": "object", "required": ["essentials", "alertContext"],
                "properties": {
                    "essentials": {
                        "type": "object",
                        "required": [key for key in essentials if key != "resolvedDateTime"],
                        "properties": essentials,
                    },
                    "alertContext": {
                        "type": "object", "required": ["condition"],
                        "properties": {
                            "condition": {
                                "type": "object", "required": ["allOf"],
                                "properties": {
                                    "allOf": {
                                        "type": "array", "minItems": 1, "maxItems": 1,
                                        "items": criterion_schema,
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def definition(config):
    """Return a secret-free WDL definition with immutable trusted mappings.

    Fired alerts require the configured latency threshold and a fired time within -30/+2 minutes.
    Resolved alerts validate the same signal identity, route and threshold,
    but allow recovered latency and use resolvedDateTime for freshness; their
    firedDateTime must not follow resolvedDateTime. Neither event accepts extra
    criteria, targets or dimensions.
    """
    essentials = "body('Parse_alert')?['data']?['essentials']"
    criterion = "first(body('Parse_alert')?['data']?['alertContext']?['condition']?['allOf'])"
    fired = f"equals({essentials}?['monitorCondition'], 'Fired')"
    event_time = (
        f"if({fired}, {essentials}?['firedDateTime'], "
        f"{essentials}?['resolvedDateTime'])"
    )
    valid = (
        f"@and(equals({essentials}?['signalType'], 'Metric'), "
        f"equals({essentials}?['monitoringService'], 'Platform'), "
        f"equals(toLower(first({essentials}?['alertTargetIDs'])), outputs('Match_rule')?['account']), "
        f"equals({criterion}?['metricName'], 'AzureOpenAITTLTInMS'), "
        f"equals(toLower({criterion}?['metricNamespace']), 'microsoft.cognitiveservices/accounts'), "
        f"equals({criterion}?['operator'], 'GreaterThan'), "
        f"equals({criterion}?['timeAggregation'], 'Average'), "
        f"equals(float({criterion}?['threshold']), {config['threshold_ms']}), "
        f"greaterOrEquals(float({criterion}?['metricValue']), 0), "
        f"or(not({fired}), greater(float({criterion}?['metricValue']), {config['threshold_ms']})), "
        f"equals(toLower(first({criterion}?['dimensions'])?['name']), 'modeldeploymentname'), "
        f"equals(first({criterion}?['dimensions'])?['value'], outputs('Match_rule')?['deployment']), "
        f"greaterOrEquals(ticks({event_time}), ticks(addMinutes(utcNow(), -30))), "
        f"lessOrEquals(ticks({event_time}), ticks(addMinutes(utcNow(), 2))), "
        f"lessOrEquals(ticks({essentials}?['firedDateTime']), ticks({event_time})))"
    )
    metric_value = f"float({criterion}?['metricValue'])"
    if "apim_log_alerts" in config:
        context = "body('Parse_alert')?['data']?['alertContext']"
        threshold = config["apim_log_alerts"]["threshold_ms"]
        # No-data recovery can omit metricValue/resolvedDateTime. Use the
        # evaluation end time only for log recovery; never for Fired freshness.
        log_time = (
            f"if({fired}, {essentials}?['firedDateTime'], "
            f"coalesce({essentials}?['resolvedDateTime'], {context}?['condition']?['windowEndTime']))"
        )
        log_valid = (
            f"and(equals({essentials}?['signalType'], 'Log'), "
            f"equals({essentials}?['monitoringService'], 'Log Alerts V2'), "
            f"equals({context}?['conditionType'], 'LogQueryCriteria'), "
            f"equals(toLower(first({essentials}?['alertTargetIDs'])), outputs('Match_rule')?['account']), "
            f"equals({criterion}?['searchQuery'], outputs('Match_rule')?['query']), "
            f"equals({criterion}?['metricMeasureColumn'], 'BackendLatencyP95Ms'), "
            f"equals({criterion}?['operator'], 'GreaterThan'), "
            f"equals({criterion}?['timeAggregation'], 'Maximum'), "
            f"equals(float({criterion}?['threshold']), {threshold}), "
            f"greaterOrEquals(float(coalesce({criterion}?['metricValue'], 0)), 0), "
            f"or(not({fired}), greater(float(coalesce({criterion}?['metricValue'], 0)), {threshold})), "
            f"equals(first({criterion}?['dimensions'])?['name'], 'BackendId'), "
            f"equals(first({criterion}?['dimensions'])?['value'], outputs('Match_rule')?['backend_id']), "
            f"greaterOrEquals(ticks({log_time}), ticks(addMinutes(utcNow(), -30))), "
            f"lessOrEquals(ticks({log_time}), ticks(addMinutes(utcNow(), 2))), "
            f"lessOrEquals(ticks({essentials}?['firedDateTime']), ticks({log_time})))"
        )
        valid = f"@if(equals(outputs('Match_rule')?['source'], 'apim_logs'), {log_valid}, {valid[1:]})"
        metric_value = f"coalesce({criterion}?['metricValue'], 'no data')"
    config_valid = (
        "@and(equals(body('Read_routes')?['properties']?['secret'], false), "
        "equals(toLower(body('Read_routes')?['id']), toLower(outputs('Match_rule')?['namedValue'])), "
        "equals(length(outputs('degraded_backend_names')), length(union(outputs('degraded_backend_names'), outputs('degraded_backend_names')))), "
        "equals(length(outputs('degraded_backend_names')), length(intersection(outputs('degraded_backend_names'), "
        "outputs('Match_rule')?['backend_names']))))"
    )
    # Do not use '*' or retry a stale ETag: conflicts and authorization errors
    # must remain visible and must not overwrite a concurrent operator change.
    write = http_action(
        "PATCH", {"properties": {"value": "@outputs('Union_csv')"}},
        {"If-Match": "@outputs('Read_etag')"},
    )
    update = sequence(
        Union_csv={
            "type": "Compose",
            "inputs": "@join(union(outputs('degraded_backend_names'), createArray(variables('backend_name'))), ',')",
        },
        Write_routes=write,
        Save_after_etag=set_value(
            "AfterETag", "@coalesce(outputs('Write_routes')?['headers']?['ETag'], "
            "outputs('Write_routes')?['headers']?['etag'], body('Write_routes')?['etag'], '')"),
        Save_after=set_value("After", "@outputs('Union_csv')"),
        Updated=set_value("Outcome", "Updated"),
    )
    etag_guard = condition(
        "@and(not(empty(outputs('Read_etag'))), not(equals(outputs('Read_etag'), '*')))",
        update,
        sequence(
            Missing_etag_outcome=set_value("Outcome", "ControllerFailed"),
            Missing_etag_code=set_value("ErrorCode", "MissingOrUnsafeETag"),
            Missing_etag_status=set_value("StatusCode", 409),
            Missing_etag_flag=set_value("ControllerFailed", True),
        ),
    )
    change = condition(
        "@contains(outputs('degraded_backend_names'), variables('backend_name'))",
        sequence(Already_degraded=set_value("Outcome", "AlreadyDegraded")),
        sequence(
            Read_etag={
                "type": "Compose",
                "inputs": "@coalesce(outputs('Read_routes')?['headers']?['ETag'], "
                          "outputs('Read_routes')?['headers']?['etag'], '')",
            },
            Require_etag=etag_guard,
        ),
    )
    read_and_update = sequence(
        Read_routes=http_action("GET"),
        Parse_config={
            "type": "ParseJson",
            "inputs": {
                "content": "@body('Read_routes')",
                "schema": {
                    "type": "object", "required": ["id", "properties"],
                    "properties": {
                        "id": {"type": "string"},
                        "properties": {
                            "type": "object", "required": ["value", "secret"],
                            "properties": {
                                "value": {"type": "string"},
                                "secret": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
        },
        degraded_backend_names={
            "type": "Compose",
            "inputs": "@if(equals(body('Read_routes')?['properties']?['value'], 'none'), "
                      "json('[]'), split(body('Read_routes')?['properties']?['value'], ','))",
        },
        Validate_config=condition(
            config_valid,
            sequence(
                Save_before=set_value("Before", "@body('Read_routes')?['properties']?['value']"),
                Save_before_etag=set_value(
                    "BeforeETag", "@coalesce(outputs('Read_routes')?['headers']?['ETag'], "
                    "outputs('Read_routes')?['headers']?['etag'], '')"),
                Save_unchanged_etag=set_value("AfterETag", "@variables('BeforeETag')"),
                Save_unchanged=set_value("After", "@variables('Before')"),
                Add_if_absent=change,
            ),
            controller_error("InvalidRouteConfiguration"),
        ),
    )
    accepted = sequence(
        Save_route=set_value("Route", "@outputs('Match_rule')?['route']"),
        Save_group=set_value("Group", "@outputs('Match_rule')?['group']"),
        Save_backend_name=set_value("backend_name", "@outputs('Match_rule')?['backend_name']"),
        Save_metric=set_value("Metric", f"@string({metric_value})"),
        Accept_event=set_value("Accepted", True),
        Accept_status=set_value("StatusCode", 200),
        Fired_only=condition(
            "@" + fired, read_and_update,
            sequence(Resolved_ignored=set_value("Outcome", "ResolvedIgnored")),
        ),
    )
    process = {
        "type": "Scope",
        "runAfter": {"Initialize": ["Succeeded"]},
        "actions": sequence(
            Parse_alert={
                "type": "ParseJson",
                "inputs": {"content": "@triggerBody()", "schema": alert_schema("apim_log_alerts" in config)},
            },
            Rule_map={"type": "Compose", "inputs": rule_map(config)},
            Trusted_rule=condition(
                f"@contains(outputs('Rule_map'), {essentials}?['alertRule'])",
                sequence(
                    Match_rule={
                        "type": "Compose",
                        "inputs": f"@outputs('Rule_map')[{essentials}?['alertRule']]",
                    },
                    Validate_alert=condition(valid, accepted),
                ),
            ),
        ),
    }
    failure = condition(
        "@not(equals(actions('Process')?['status'], 'Succeeded'))",
        sequence(
            Process_failed=set_value("ControllerFailed", True),
            Failure_outcome=set_value(
                "Outcome", "@if(variables('Accepted'), 'ControllerFailed', 'Rejected')",
            ),
            Failure_code=set_value(
                "ErrorCode", "@if(variables('Accepted'), 'ControllerActionFailed', 'AlertValidationFailed')",
            ),
            Failure_status=set_value("StatusCode", "@if(variables('Accepted'), 500, 400)"),
            Unknown_after=set_value("After", ""),
        ),
    )
    failure["runAfter"] = {"Process": FINISHED}
    notification = {
        "type": "Http",
        "operationOptions": "DisableAsyncPattern",
        "runtimeConfiguration": {"secureData": {"properties": ["inputs", "outputs"]}},
        "inputs": {
            "method": "POST",
            "uri": "@parameters('dingtalkWebhook')",
            "headers": {"Content-Type": "application/json"},
            "retryPolicy": {"type": "none"},
            "body": {
                "msgtype": "text",
                "text": {
                    "content": "@concat('Azure APIM 路由告警', decodeUriComponent('%0A'), "
                               "'业务类型：', if(equals(variables('Group'), 'chat'), '对话', '向量嵌入'), "
                               "'；后端名称：', variables('backend_name'), decodeUriComponent('%0A'), "
                               "'处理结果：', "
                               "if(equals(variables('Outcome'), 'Updated'), '已加入降级名单', "
                               "if(equals(variables('Outcome'), 'AlreadyDegraded'), '已在降级名单中，无需重复更新', "
                               "if(equals(variables('Outcome'), 'ResolvedIgnored'), '告警已恢复，保留降级标记，需人工恢复路由', "
                               "if(equals(variables('Outcome'), 'ControllerFailed'), '路由控制器处理失败，请检查权限、ETag 和运行记录', "
                               "variables('Outcome'))))), decodeUriComponent('%0A'), "
                               "if(equals(outputs('Match_rule')?['source'], 'apim_logs'), "
                               "'APIM 后端时延 p95：', '平均总响应时延（TTLT）：'), variables('Metric'), ' 毫秒', "
                               "if(empty(variables('Before')), '', concat(decodeUriComponent('%0A'), "
                               "'变更前降级名单：', if(equals(variables('Before'), 'none'), '无', variables('Before')))), "
                               "if(empty(variables('After')), '', concat(decodeUriComponent('%0A'), "
                               "'变更后降级名单：', if(equals(variables('After'), 'none'), '无', variables('After')))), "
                               "if(variables('ControllerFailed'), concat(decodeUriComponent('%0A'), "
                               "'错误代码：', variables('ErrorCode'), '；名单最终状态请以 APIM 为准，不会自动回滚。'), ''))",
                },
            },
        },
    }
    notify = condition(
        "@variables('Accepted')",
        sequence(
            Assume_notification_failed=set_value("NotificationFailed", True),
            DingTalk=notification,
            Check_DingTalk=condition(
                "@and(equals(outputs('DingTalk')?['statusCode'], 200), "
                "equals(body('DingTalk')?['errcode'], 0))",
                sequence(Notification_delivered=set_value("NotificationFailed", False)),
            ),
        ),
    )
    notify["runAfter"] = {"Capture_failure": FINISHED}
    finish = condition(
        "@or(variables('NotificationFailed'), not(equals(actions('Notify')?['status'], 'Succeeded')))",
        sequence(
            Notification_error=set_value("NotificationFailed", True),
            Notification_status=set_value("StatusCode", 500),
        ),
    )
    finish["runAfter"] = {"Notify": FINISHED}
    variables = {
        "Outcome": ("string", "Rejected"), "Route": ("string", ""),
        "Group": ("string", ""), "backend_name": ("string", ""), "Metric": ("string", ""),
        "Before": ("string", ""), "After": ("string", ""),
        "BeforeETag": ("string", ""), "AfterETag": ("string", ""),
        "Accepted": ("boolean", False), "ControllerFailed": ("boolean", False),
        "NotificationFailed": ("boolean", False), "StatusCode": ("integer", 400),
        "ErrorCode": ("string", "AlertValidationFailed"),
    }
    response = {
        "type": "Response", "kind": "Http", "operationOptions": "Asynchronous",
        "runAfter": {"Finalize_notification": FINISHED},
        "inputs": {
            "statusCode": "@variables('StatusCode')",
            "body": {
                "result": "@if(variables('NotificationFailed'), 'NotificationFailed', variables('Outcome'))",
                "outcome": "@variables('Outcome')",
                "route": "@variables('Route')",
                "latencyMs": "@variables('Metric')",
                "before": "@variables('Before')", "after": "@variables('After')",
                "beforeETag": "@variables('BeforeETag')", "afterETag": "@variables('AfterETag')",
                "writeStatus": "@coalesce(actions('Write_routes')?['status'], 'Skipped')",
                "writeHttpStatus": "@coalesce(outputs('Write_routes')?['statusCode'], 0)",
                "controllerFailed": "@variables('ControllerFailed')",
                "notificationFailed": "@variables('NotificationFailed')",
                "error": "@if(or(variables('ControllerFailed'), not(variables('Accepted'))), variables('ErrorCode'), '')",
            },
        },
    }
    terminate = condition(
        "@or(variables('ControllerFailed'), variables('NotificationFailed'), "
        "not(variables('Accepted')), not(equals(actions('Respond')?['status'], 'Succeeded')))",
        {
            "Mark_failed": {
                "type": "Terminate",
                "inputs": {
                    "runStatus": "Failed",
                    "runError": {
                        "code": "DegradedControllerFailed",
                        "message": "Alert rejected, controller action failed, or notification failed. "
                                   "Inspect action status for authorization/ETag errors; no bypass or automatic retry.",
                    },
                },
            },
        },
    )
    terminate["runAfter"] = {"Respond": FINISHED}
    return {
        "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {"dingtalkWebhook": {"defaultValue": "none", "type": "SecureString"}},
        "triggers": {
            "receive": {
                "type": "Request", "kind": "Http",
                # Validation is inside Process so malformed JSON objects get
                # the same response path and cannot skip failure handling.
                "inputs": {"method": "POST", "schema": {}},
                "runtimeConfiguration": {"concurrency": {"runs": 1, "maximumWaitingRuns": 100}},
            },
        },
        "actions": {
            "Initialize": {
                "type": "InitializeVariable",
                "inputs": {"variables": [
                    {"name": name, "type": kind, "value": value}
                    for name, (kind, value) in variables.items()
                ]},
            },
            "Process": process,
            "Capture_failure": failure,
            "Notify": notify,
            "Finalize_notification": finish,
            "Respond": response,
            "Finish": terminate,
        },
        "outputs": {},
    }
