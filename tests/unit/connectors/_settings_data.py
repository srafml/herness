"""Valid minimal source sections shared by the settings tests (T01-02)."""

from typing import Any

GUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def servicenow(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "enabled": True,
        "base_url": "https://acme.service-now.com",
        "auth": {"method": "oauth_client_credentials", "credentials": "secret:sn_oauth"},
        "entities": {"incident": {"fields": ["number", "opened_at"]}},
    }
    return data | extra


def jira(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "enabled": True,
        "flavor": "cloud",
        "base_url": "https://acme.atlassian.net",
        "auth": {"method": "api_token", "credentials": "secret:jira_token"},
    }
    return data | extra


def adapter(tool: str, **extra: Any) -> dict[str, Any]:
    methods = {
        "prometheus": "bearer",
        "datadog": "api_and_app_key",
        "splunk": "bearer",
        "dynatrace": "api_token",
    }
    data: dict[str, Any] = {
        "enabled": True,
        "base_url": f"https://{tool}.example.com",
        "auth": {"method": methods[tool], "credentials": f"secret:{tool}_key"},
    }
    return data | extra


def monitoring(**adapters: dict[str, Any]) -> dict[str, Any]:
    return {"enabled": True, "adapters": adapters or {"prometheus": adapter("prometheus")}}


def mongodb(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "enabled": True,
        "hosts": ["db0.example.com"],
        "auth": {"method": "connection_string", "credentials": "secret:mongo_uri"},
        "database": "ops",
        "entities": {"orders": {"collection": "orders", "updated_field": "ts", "fields": ["a"]}},
    }
    return data | extra


def mongo_entity(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"collection": "orders", "updated_field": "ts", "fields": ["a", "b"]}
    return data | extra


def snowflake(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "enabled": True,
        "account": "Acme-XY12345",
        "hosts": ["acme-xy12345.snowflakecomputing.com"],
        "auth": {"method": "key_pair", "credentials": "secret:snowflake_svc"},
        "warehouse": "HERNESS_XS",
        "role": "HERNESS_READER",
        "entities": {"cost_center": snowflake_entity()},
    }
    return data | extra


def snowflake_entity(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "table": "FINANCE.PUBLIC.COST_CENTER",
        "key_field": "CC_ID",
        "updated_field": "UPDATED_AT",
        "columns": ["CC_ID", "NAME", "UPDATED_AT"],
    }
    return data | extra


def dataverse(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "enabled": True,
        "base_url": "https://acme.crm.dynamics.com",
        "hosts": ["login.microsoftonline.com"],
        "auth": {
            "method": "msal_client_credentials",
            "tenant_id": GUID,
            "credentials": "secret:dataverse_app",
        },
        "entities": {
            "msdyn_project": {
                "entityset": "msdyn_projects",
                "key_field": "msdyn_projectid",
                "select": ["msdyn_subject", "modifiedon"],
            }
        },
    }
    return data | extra


def files(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "enabled": True,
        "entities": {"roster": {"pattern": "*.csv", "key_field": ["team_code"]}},
    }
    return data | extra
