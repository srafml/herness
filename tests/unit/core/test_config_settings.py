"""Tests for herness.core.settings (impl 10 U10-02 … U10-07; UT10-19 model cases, UT10-61)."""

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from herness.core import settings as st

pytestmark = pytest.mark.unit

PLACEHOLDER_DEPLOY: dict[str, Any] = {
    "reasoning": {
        "image": "<reasoning-image>",
        "model": "<reasoning-model>",
        "revision": "<revision>",
        "tool_call_parser": "<parser>",
    },
    "openjev": {"image": "<openjev-image>", "revision": "<revision>"},
    "large": {"image": "<large-image>", "gguf": "<gguf>", "sha256": "<sha256>"},
}


def _errors(model: type[BaseModel], data: dict[str, Any]) -> list[tuple[tuple[Any, ...], str]]:
    with pytest.raises(ValidationError) as info:
        model.model_validate(data)
    return [(tuple(e["loc"]), e["type"]) for e in info.value.errors()]


def test_ut10_19_defaults() -> None:
    """UT10-19 every section builds from defaults with the spec values."""
    paths = st.PathsConfig()
    assert paths.backup_target == Path("E:/herness-backup")
    sec = st.SecurityConfig()
    assert sec.data_policy.chat_approved is False
    assert sec.secrets.backend == "keyring"
    assert sec.egress.enabled is False
    assert sec.egress.max_tokens_per_day == 3_000_000
    assert sec.redaction.id_patterns == {"EMPLOYEE_ID": (r"\bE\d{6}\b",), "USER_ID": ()}
    assert sec.redaction.national_id_patterns == (r"\b\d{3}-\d{2}-\d{4}\b",)
    assert sec.redaction.key == "secret:redact.hmac_key"
    assert sec.ui.bind == "127.0.0.1"
    assert sec.ui.port == 8501
    assert sec.ui.expose.identity_header == "X-Forwarded-User"
    assert sec.ui.roles.default_role == "viewer"
    assert st.LoggingConfig().level == "INFO"
    assert st.RetentionConfig().chat_days == 180
    assert st.BackupConfig().nightly_at == "01:30"
    deploy = st.DeployConfig.model_validate(PLACEHOLDER_DEPLOY)
    assert deploy.wsl_distro == "herness"
    assert deploy.reasoning.port == 8000
    assert deploy.openjev.port == 8100
    assert deploy.large.port == 8200
    assert deploy.openjev.model == "nvidia/diffusiongemma-26B-A4B-it-NVFP4"
    assert deploy.service.manager == "nssm"
    assert deploy.release.duckdb_extensions == {}


def test_ut10_19_models_are_frozen_and_forbid_extra() -> None:
    """UT10-19 unknown keys are rejected and instances are immutable."""
    assert _errors(st.PathsConfig, {"cache": "x"}) == [(("cache",), "extra_forbidden")]
    paths = st.PathsConfig()
    with pytest.raises(ValidationError):
        paths.data = Path("other")  # type: ignore[misc]


def test_ut10_19_paths_accept_strings() -> None:
    """UT10-19 path fields accept YAML strings (lax Path) and reject empty or NUL values."""
    paths = st.PathsConfig.model_validate({"data": "D:/data", "logs": "logs"})
    assert paths.data == Path("D:/data")
    assert paths.logs == Path("logs")
    assert _errors(st.PathsConfig, {"data": ""}) == [(("data",), "value_error")]
    assert _errors(st.PathsConfig, {"logs": "a\x00b"}) == [(("logs",), "value_error")]
    assert _errors(st.PathsConfig, {"backup_target": Path("a\x00b")}) == [
        (("backup_target",), "value_error")
    ]
    assert _errors(st.PathsConfig, {"data": 5})[0][0] == ("data",)


@pytest.mark.parametrize(
    ("model", "data", "loc", "kind"),
    [
        (st.DataPolicyConfig, {"approved_on": "24/09/2026"}, ("approved_on",), "date_type"),
        (st.DataPolicyConfig, {"approved_on": "2026-02-30"}, ("approved_on",), "value_error"),
        (
            st.DataPolicyConfig,
            {"approved_on": datetime(2026, 9, 24)},  # noqa: DTZ001 - value under test
            ("approved_on",),
            "date_type",
        ),
        (st.DataPolicyConfig, {"approved_by": ""}, ("approved_by",), "string_too_short"),
        (st.DataPolicyConfig, {"approved_by": "x" * 129}, ("approved_by",), "string_too_long"),
        (st.DataPolicyConfig, {"hybrid_approved": "yes"}, ("hybrid_approved",), "bool_type"),
        (st.SecretsConfig, {"backend": "vault"}, ("backend",), "literal_error"),
        (st.EgressConfig, {"purposes": ["model_download"]}, ("purposes", 0), "literal_error"),
        (st.EgressConfig, {"destinations": ["a.com"] * 33}, ("destinations",), "too_long"),
        (st.EgressConfig, {"max_request_bytes": 0}, ("max_request_bytes",), "greater_than_equal"),
        (
            st.EgressConfig,
            {"max_request_bytes": 50_000_001},
            ("max_request_bytes",),
            "less_than_equal",
        ),
        (
            st.EgressConfig,
            {"max_tokens_per_request": 2_000_001},
            ("max_tokens_per_request",),
            "less_than_equal",
        ),
        (
            st.EgressConfig,
            {"max_tokens_per_day": 100_000_001},
            ("max_tokens_per_day",),
            "less_than_equal",
        ),
        (st.EgressConfig, {"max_tokens_per_day": "5"}, ("max_tokens_per_day",), "int_type"),
        (
            st.NetworkConfig,
            {"extra_allowed_hosts": ["h.example.com"] * 33},
            ("extra_allowed_hosts",),
            "too_long",
        ),
        (
            st.NetworkConfig,
            {"http_proxy": "socks5://p:1"},
            ("http_proxy",),
            "string_pattern_mismatch",
        ),
        (
            st.NetworkConfig,
            {"http_proxy": "http://user@p:8080"},
            ("http_proxy",),
            "string_pattern_mismatch",
        ),
        (
            st.NetworkConfig,
            {"http_proxy": "http://p:\u0668\u0660"},
            ("http_proxy",),
            "string_pattern_mismatch",
        ),
        (st.SecurityConfig, {"audit": {}}, ("audit",), "extra_forbidden"),
    ],
)
def test_ut10_19_security_validators(
    model: type[BaseModel], data: dict[str, Any], loc: tuple[Any, ...], kind: str
) -> None:
    """UT10-19 data policy, secrets, egress and network field rules."""
    assert _errors(model, data) == [(loc, kind)]


def test_ut10_19_security_normalisation() -> None:
    """UT10-19 ISO dates parse; host lists are lower-cased and accept YAML lists."""
    policy = st.DataPolicyConfig.model_validate(
        {"approved_on": "2026-09-24", "approved_by": "Ops Lead"}
    )
    assert policy.approved_on == date(2026, 9, 24)
    assert st.DataPolicyConfig(approved_on=date(2026, 9, 24)).approved_on == date(2026, 9, 24)
    egress = st.EgressConfig.model_validate(
        {"destinations": ["API.Anthropic.com"], "purposes": ["reasoning_final"]}
    )
    assert egress.destinations == ("api.anthropic.com",)
    assert egress.purposes == ("reasoning_final",)
    net = st.NetworkConfig.model_validate(
        {"extra_allowed_hosts": ["Proxy.Corp.Example"], "http_proxy": "http://proxy.corp:8080"}
    )
    assert net.extra_allowed_hosts == ("proxy.corp.example",)
    assert net.http_proxy == "http://proxy.corp:8080"
    sec = st.SecurityConfig.model_validate({"egress": {"enabled": True}})
    assert sec.egress.enabled is True


@pytest.mark.parametrize(
    ("data", "loc", "kind"),
    [
        ({"national_id_patterns": ["("]}, ("national_id_patterns", 0), "value_error"),
        (
            {"id_patterns": {"EMPLOYEE_ID": ["x" * 501]}},
            ("id_patterns", "EMPLOYEE_ID", 0),
            "value_error",
        ),
        ({"id_patterns": {"OTHER": []}}, ("id_patterns", "OTHER", "[key]"), "literal_error"),
        ({"national_id_patterns": ["a"] * 65}, ("national_id_patterns",), "too_long"),
        ({"custom_patterns": {f"p{i}": "a" for i in range(65)}}, ("custom_patterns",), "too_long"),
        (
            {"custom_patterns": {"Bad": "a"}},
            ("custom_patterns", "Bad", "[key]"),
            "string_pattern_mismatch",
        ),
        ({"extra_names": ["A"]}, ("extra_names", 0), "string_too_short"),
        ({"extra_names": ["A" * 129]}, ("extra_names", 0), "string_too_long"),
        ({"ner": "spacy"}, ("ner",), "literal_error"),
        ({"key": "plain-text-key"}, ("key",), "string_pattern_mismatch"),
        ({"key": "secret:x"}, ("key",), "string_pattern_mismatch"),
        ({"denylist_domains": ["Example.com"]}, ("denylist_domains", 0), "value_error"),
        ({"denylist_domains": ["10.0.0.1"]}, ("denylist_domains", 0), "value_error"),
        ({"denylist_domains": ["localhost"]}, ("denylist_domains", 0), "value_error"),
        ({"directory_file": ""}, ("directory_file",), "value_error"),
        ({"mask_ip": 1}, ("mask_ip",), "bool_type"),
    ],
)
def test_ut10_19_redaction_validators(
    data: dict[str, Any], loc: tuple[Any, ...], kind: str
) -> None:
    """UT10-19 redaction patterns, names, key reference and domains."""
    assert _errors(st.RedactionConfig, data) == [(loc, kind)]


def test_ut10_19_redaction_accepts_valid_values() -> None:
    """UT10-19 valid patterns, domains and a null directory file are accepted."""
    cfg = st.RedactionConfig.model_validate(
        {
            "directory_file": None,
            "extra_names": ["Jo"],
            "id_patterns": {"USER_ID": [r"\bU\d{5}\b", r"(ab)+c", r"(a{2})*"]},
            "custom_patterns": {"ticket_ref": r"\bT-\d{4}\b"},
            "denylist_domains": ["mail.example.com"],
            "key": "secret:redact.hmac_key2",
            "ner": "presidio",
        }
    )
    assert cfg.directory_file is None
    assert cfg.id_patterns == {"USER_ID": (r"\bU\d{5}\b", r"(ab)+c", r"(a{2})*")}
    assert st.RedactionConfig(directory_file=Path("x.csv")).directory_file == Path("x.csv")


@pytest.mark.parametrize(
    ("model", "data", "loc", "kind"),
    [
        (st.UiConfig, {"bind": "localhost"}, ("bind",), "value_error"),
        (st.UiConfig, {"port": 80}, ("port",), "greater_than_equal"),
        (st.UiConfig, {"port": 65536}, ("port",), "less_than_equal"),
        (st.ExposeConfig, {"trusted_proxy": "proxy.local"}, ("trusted_proxy",), "value_error"),
        (
            st.ExposeConfig,
            {"identity_header": "X_User"},
            ("identity_header",),
            "string_pattern_mismatch",
        ),
        (
            st.ExposeConfig,
            {"identity_header": "X-User\r\nInjected"},
            ("identity_header",),
            "string_pattern_mismatch",
        ),
        (st.RolesConfig, {"admins": [""]}, ("admins", 0), "string_too_short"),
        (st.RolesConfig, {"reviewers": ["x" * 129]}, ("reviewers", 0), "string_too_long"),
        (st.RolesConfig, {"admins": ["u"] * 501}, ("admins",), "too_long"),
        (st.RolesConfig, {"default_role": "admin"}, ("default_role",), "literal_error"),
    ],
)
def test_ut10_19_ui_validators(
    model: type[BaseModel], data: dict[str, Any], loc: tuple[Any, ...], kind: str
) -> None:
    """UT10-19 UI bind, port, proxy, header and role rules."""
    assert _errors(model, data) == [(loc, kind)]


def test_ut10_19_ui_normalisation() -> None:
    """UT10-19 IP literals are accepted; usernames are lower-cased and de-duplicated."""
    ui = st.UiConfig.model_validate(
        {
            "bind": "::1",
            "expose": {"enabled": True, "trusted_proxy": "127.0.0.1"},
            "roles": {"admins": ["Alice", "bob", "ALICE"], "default_role": "denied"},
        }
    )
    assert ui.bind == "::1"
    assert ui.expose.trusted_proxy == "127.0.0.1"
    assert ui.roles.admins == ("alice", "bob")
    assert ui.roles.default_role == "denied"


@pytest.mark.parametrize(
    ("model", "data", "loc", "kind"),
    [
        (st.LoggingConfig, {"level": "TRACE"}, ("level",), "literal_error"),
        (st.RetentionConfig, {"raw_lake_months": 0}, ("raw_lake_months",), "greater_than_equal"),
        (st.RetentionConfig, {"raw_lake_months": 241}, ("raw_lake_months",), "less_than_equal"),
        (st.RetentionConfig, {"traces_days": 7301}, ("traces_days",), "less_than_equal"),
        (st.RetentionConfig, {"chat_days": 0}, ("chat_days",), "greater_than_equal"),
        (st.BackupConfig, {"nightly_at": "24:00"}, ("nightly_at",), "string_pattern_mismatch"),
        (st.BackupConfig, {"nightly_at": "1:30"}, ("nightly_at",), "string_pattern_mismatch"),
        (st.BackupConfig, {"nightly_at": "0\u0661:30"}, ("nightly_at",), "string_pattern_mismatch"),
        (st.BackupConfig, {"keep_daily": 0}, ("keep_daily",), "greater_than_equal"),
        (st.BackupConfig, {"keep_daily": 366}, ("keep_daily",), "less_than_equal"),
        (st.BackupConfig, {"keep_weekly": -1}, ("keep_weekly",), "greater_than_equal"),
        (st.BackupConfig, {"keep_weekly": 521}, ("keep_weekly",), "less_than_equal"),
        (st.BackupConfig, {"include_lake": "no"}, ("include_lake",), "bool_type"),
    ],
)
def test_ut10_19_logging_retention_backup(
    model: type[BaseModel], data: dict[str, Any], loc: tuple[Any, ...], kind: str
) -> None:
    """UT10-19 logging level, retention ranges and backup schedule rules."""
    assert _errors(model, data) == [(loc, kind)]
