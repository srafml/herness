"""Tests for the cross-checks and full validation (impl 10 U10-13, U10-20; UT10-19, UT10-75).

Most C-rows guard values the owner models already reject at load (defense in depth), so the
parametrised cases edit the effective dict that ``run_cross_checks`` reads (``_tree``) and then
call ``validate`` as the spec's action; the model-validator and owner rows use real files.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.support.config_tree import register_checked_names, write_checked_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import config_validate as cv
from herness.core import registry
from herness.core.config_view import ConfigIssue

pytestmark = pytest.mark.unit

Tree = dict[str, Any]
CLIENTS = "models.models.clients"
ALL_IFACES = "0.0.0.0"  # noqa: S104 - the misconfiguration under test
GPU = "resilience.resilience.gpu.classes"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    register_checked_names()
    yield
    c.reset_config()
    registry.reset_registry()
    cv.reset_owner_validators()


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    return write_checked_config(tmp_path)


def _set(tree: Tree, path: str, value: object) -> None:
    *parents, leaf = path.split(".")
    node = tree
    for part in parents:
        node = node[part]
    node[leaf] = value


_REAL_TREE = cv._tree


def _present(name: str) -> str:
    """A stored value; redact.hmac_key must be 64 hex once herness.core.redact is imported."""
    return "ab" * 32 if name == "redact.hmac_key" else "value-present"


def _edit(monkeypatch: pytest.MonkeyPatch, *edits: tuple[str, object]) -> None:
    real = _REAL_TREE

    def patched(cfg: c.HernessConfig) -> Tree:
        tree = real(cfg)
        for path, value in edits:
            _set(tree, path, value)
        return tree

    monkeypatch.setattr(cv, "_tree", patched)


def _shape(issues: list[ConfigIssue]) -> list[tuple[str, str, str]]:
    return [(i.message.split(" ")[0], i.severity, i.path) for i in issues]


def test_ut10_19_checked_tree_has_no_issues(cfg_dir: Path) -> None:
    """UT10-19 the pinned test tree passes every offline row, with registry, in each profile."""
    for profile in ("local", "hybrid", "premium", "synth"):
        assert c.validate(cfg_dir, profile, offline=True) == []  # type: ignore[arg-type]


def _snowflake(**extra: object) -> dict[str, object]:
    return {"enabled": True, "account": "Acme", **extra}


def _msal(hosts: list[str]) -> dict[str, object]:
    auth = {"method": "msal_client_credentials"}
    return {
        "enabled": True,
        "base_url": "https://org.crm.dynamics.com",
        "auth": auth,
        "hosts": hosts,
    }


def _expose(proxy: str | None, role: str) -> tuple[tuple[str, object], ...]:
    return (
        ("security.ui.expose", {"enabled": True, "trusted_proxy": proxy}),
        ("security.ui.roles.default_role", role),
    )


def _profile(name: str, purposes: list[str], **policy: bool) -> tuple[tuple[str, object], ...]:
    egress = {"enabled": True, "destinations": ["api.anthropic.com"], "purposes": purposes}
    return (
        ("profile", name),
        ("security.egress", egress),
        *((f"security.data_policy.{k}", v) for k, v in policy.items()),
    )


OFFLINE_CASES: list[tuple[str, tuple[tuple[str, object], ...], list[tuple[str, str, str]]]] = [
    (
        "C01",
        (("models.models.roles.writer", "nope"),),
        [("C01", "error", "models.models.roles.writer")],
    ),
    (
        "C01",
        (("models.models.fallback.writer", ["local-30b", "nope"]),),
        [("C01", "error", "models.models.fallback.writer[1]")],
    ),
    (
        "C02",
        (("models.models.roles.writer", "claude-opus"),),
        [("C02", "error", "models.models.roles.writer")],
    ),
    (
        "C04",
        (
            ("security.egress.destinations", ["10.0.0.1"]),
            ("security.network.extra_allowed_hosts", ["Bad_Host"]),
        ),
        [
            ("C04", "error", "security.egress.destinations[0]"),
            ("C04", "error", "security.network.extra_allowed_hosts[0]"),
        ],
    ),
    (
        "C04",
        (
            (
                "sources.sources.mongodb",
                {"enabled": False, "hosts": ["db.example.com", "127.0.0.1"]},
            ),
        ),
        [("C04", "error", "sources.sources.mongodb.hosts[1]")],
    ),
    ("C05", (("security.ui.bind", ALL_IFACES),), [("C05", "error", "security.ui.bind")]),
    ("C05", _expose(None, "denied"), [("C05", "error", "security.ui.expose.trusted_proxy")]),
    ("C05", _expose("10.0.0.9", "denied"), [("C05", "error", "security.ui.expose.trusted_proxy")]),
    (
        "C07",
        (("deploy.reasoning.served_name", "other"),),
        [("C07", "error", "deploy.reasoning.served_name")],
    ),
    (
        "C08a",
        ((f"{GPU}.decider.services.openjev.url", "http://127.0.0.1:8101"),),
        [("C08a", "error", f"{GPU}.decider.services.openjev.url")],
    ),
    (
        "C09",
        (("models.models.clients.local-lora-14b.base_url", "http://127.0.0.1:8001/v1"),),
        [("C09", "error", f"{CLIENTS}.local-lora-14b.base_url")],
    ),
    (
        "C10",
        ((f"{CLIENTS}.local-30b.context_window", 65536),),
        [("C10", "error", f"{CLIENTS}.local-30b.context_window")],
    ),
    (
        "C11",
        _profile("hybrid", ["reasoning_final", "reasoning"], chat_approved=False),
        [("C11", "error", "security.egress.purposes[1]")],
    ),
    (
        "C11",
        _profile("premium", ["reasoning", "model_download"]),
        [("C11", "error", "security.egress.purposes[1]")],
    ),
    (
        "C12",
        (("security.secrets.backend", "dotenv"),),
        [("C12", "error", "security.secrets.backend")],
    ),
    ("C13", (("deploy.large.sha256", "<sha256>"),), [("C13", "warn", "deploy.large.sha256")]),
    (
        "C14",
        (("security.redaction.custom_patterns", {"abc": "x", "ABC": "y"}),),
        [("C14", "error", "security.redaction.custom_patterns.ABC")],
    ),
    (
        "C16",
        (("sources.sources.files", {"enabled": False, "token": "vllm.api_key"}),),
        [("C16", "error", "sources.sources.files.token")],
    ),
    (
        "C16",
        (("sources.sources.files", {"enabled": False, "note": "use token=" + "q1w2e3r4" * 2}),),
        [("C16", "error", "sources.sources.files.note")],
    ),
    (
        "C17",
        (("sources.sources.servicenow", {"enabled": True, "base_url": "http://sn.example.com"}),),
        [("C17", "error", "sources.sources.servicenow.base_url")],
    ),
    (
        "C17",
        (
            (
                "sources.sources.servicenow",
                {
                    "enabled": True,
                    "base_url": "https://sn.example.com",
                    "mirrors": [
                        {"base_url": "https://a.example.com"},
                        {"base_url": "http://b.example.com"},
                    ],
                },
            ),
        ),
        [("C17", "error", "sources.sources.servicenow.mirrors[1].base_url")],
    ),
    (
        "C20",
        (("sources.sources.snowflake", _snowflake(hosts=[])),),
        [("C20", "error", "sources.sources.snowflake.hosts")],
    ),
    (
        "C20",
        (("sources.sources.snowflake", _snowflake(hosts=["other.example.com"])),),
        [("C20", "error", "sources.sources.snowflake.hosts")],
    ),
    (
        "C20",
        (("sources.sources.dataverse", _msal(["org.crm.dynamics.com"])),),
        [("C20", "error", "sources.sources.dataverse.hosts")],
    ),
    ("C21", _expose("127.0.0.1", "viewer"), [("C21", "warn", "security.ui.roles.default_role")]),
    (
        "C24",
        (
            ("profile", "premium"),
            ("security.egress", {"enabled": True, "destinations": [], "purposes": []}),
        ),
        [
            ("C24", "error", "security.egress.destinations"),
            ("C24", "error", "security.egress.purposes"),
        ],
    ),
    (
        "C25",
        (
            ("security.data_policy.chat_approved", True),
            ("security.data_policy.hybrid_approved", False),
            ("security.data_policy.premium_approved", False),
        ),
        [("C25", "error", "security.data_policy.chat_approved")],
    ),
]


@pytest.mark.parametrize(
    ("row", "edits", "expected"),
    OFFLINE_CASES,
    ids=[f"{row}-{i}" for i, (row, _, _) in enumerate(OFFLINE_CASES)],
)
def test_ut10_19_offline_row(
    cfg_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    row: str,
    edits: tuple[tuple[str, object], ...],
    expected: list[tuple[str, str, str]],
) -> None:
    """UT10-19 one case per offline C-row: validate returns exactly the expected issue(s)."""
    _edit(monkeypatch, *edits)
    issues = c.validate(cfg_dir, "local", offline=True)
    assert _shape(issues) == expected
    assert {i.message.split(" ")[0] for i in issues} == {row}


def test_ut10_19_file_follows_section(cfg_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-19 issues name the YAML file of their section; the message leads with the row ID."""
    _edit(
        monkeypatch,
        ("security.ui.bind", ALL_IFACES),
        (f"{CLIENTS}.local-30b.context_window", 99999),
    )
    issues = c.validate(cfg_dir, "local", offline=True)
    assert [(i.path, i.file) for i in issues] == [
        (f"{CLIENTS}.local-30b.context_window", "models.yaml"),
        ("security.ui.bind", "herness.yaml"),
    ]


def test_ut10_19_c11_chat_approved_allows_reasoning(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-19 C11: hybrid may use purpose reasoning once chat_approved is recorded (R-38)."""
    _edit(monkeypatch, *_profile("hybrid", ["reasoning_final", "reasoning"], chat_approved=True))
    assert c.validate(cfg_dir, "local", offline=True) == []


def test_ut10_19_c12_dotenv_allowed_in_dev_and_synth(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-19 C12: dotenv passes with HERNESS_ENV=dev, and in profile synth."""
    _edit(monkeypatch, ("security.secrets.backend", "dotenv"))
    assert c.validate(cfg_dir, "synth", offline=True) == []
    monkeypatch.setenv("HERNESS_ENV", "dev")
    assert c.validate(cfg_dir, "local", offline=True) == []


def test_ut10_19_c03_registry(cfg_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-19 C03: an enabled name with no implementation is an error (validate only)."""
    _edit(
        monkeypatch,
        ("models.deciders.jev.enabled", True),
        (
            "sources.sources.monitoring",
            {"enabled": True, "adapters": {"splunk": {"enabled": True}}},
        ),
    )
    issues = c.validate(cfg_dir, "local", offline=True)
    assert _shape(issues) == [
        ("C03", "error", "models.deciders.jev"),
        ("C03", "error", "sources.sources.monitoring"),
        ("C03", "error", "sources.sources.monitoring.adapters.splunk"),
    ]
    assert c.load_config("local", config_dir=cfg_dir).profile == "local"  # C03 not at load


# --- online rows (C06, C08b, C23) ----------------------------------------------------------


def _online(cfg_dir: Path, row: str) -> list[tuple[str, str, str]]:
    return [s for s in _shape(c.validate(cfg_dir, "local")) if s[0] == row]


def test_ut10_19_c06_missing_secret(
    cfg_dir: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-19 C06 (online): each referenced secret missing from the store is an error."""
    monkeypatch.setattr(cv, "_wsl_available", lambda: False)
    monkeypatch.setattr(c, "_KEY_ID_PROVIDER", None)  # redact loaded: no key load
    cfg = c.load_config("local", config_dir=cfg_dir)
    names = cv.secrets.referenced_secret_names(cfg)
    for name in names:
        fake_keyring.store[("herness", name)] = _present(name)
    assert _online(cfg_dir, "C06") == []
    del fake_keyring.store[("herness", "openjev_api_key")]
    assert _online(cfg_dir, "C06") == [("C06", "error", "secret:openjev_api_key")]
    # With herness.core.redact imported, config_hash would resolve the key id first and a
    # dead store is then one load error (U10-11 falls back only for a missing secret).
    monkeypatch.setattr(c, "_KEY_ID_PROVIDER", None)
    fake_keyring.error = RuntimeError("store down")
    assert len(_online(cfg_dir, "C06")) == len(names)
    assert c.validate(cfg_dir, "local", offline=True) == []  # offline skips C06


def test_ut10_19_c08b_compose_config(
    cfg_dir: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-19 C08b (online): warn off Windows/WSL; argv and exit code decide on Windows."""
    monkeypatch.setattr(cv, "_wsl_available", lambda: False)
    warn = ("C08b", "warn", "resilience.resilience.gpu.compose_cmd")
    assert _online(cfg_dir, "C08b") == [warn]
    calls: list[tuple[list[str], float]] = []
    result = {"code": 0}

    def fake_run(argv: list[str], **kw: Any) -> cv.subprocess.CompletedProcess[bytes]:
        calls.append((argv, kw["timeout"]))
        if result["code"] < 0:
            raise cv.subprocess.TimeoutExpired(argv, kw["timeout"])
        return cv.subprocess.CompletedProcess(argv, result["code"])

    monkeypatch.setattr(cv, "_wsl_available", lambda: True)
    monkeypatch.setattr(cv.subprocess, "run", fake_run)
    assert _online(cfg_dir, "C08b") == []
    argv, timeout = calls[0]
    assert argv[:2] == ["wsl.exe", "-d"]
    assert argv[-4:] == ["-f", "/mnt/d/herness/docker/compose.yaml", "config", "--quiet"]
    assert timeout == 20
    for code in (1, -1):
        result["code"] = code
        assert _online(cfg_dir, "C08b") == [(warn[0], "error", warn[2])]


def test_ut10_19_c23_directory_file(
    cfg_dir: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UT10-19 C23 (online): directory_file unset or missing is a warn; a readable file passes."""
    monkeypatch.setattr(cv, "_wsl_available", lambda: False)
    path = "security.redaction.directory_file"
    assert _online(cfg_dir, "C23") == [("C23", "warn", path)]
    _edit(monkeypatch, (path, (tmp_path / "missing.csv").as_posix()))
    assert _online(cfg_dir, "C23") == [("C23", "warn", path)]
    (tmp_path / "dir.csv").write_text("name\n", encoding="utf-8")
    _edit(monkeypatch, (path, (tmp_path / "dir.csv").as_posix()))
    assert _online(cfg_dir, "C23") == []


# --- model validators, owner rows, table shape, U10-09 step 8 ---------------------------------


def _replace(cfg_dir: Path, old: str, new: str) -> None:
    herness = cfg_dir / "herness.yaml"
    text = herness.read_text(encoding="utf-8")
    assert old in text
    herness.write_text(text.replace(old, new), encoding="utf-8")


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            "redaction: {directory_file: null}",
            'redaction: {directory_file: null, custom_patterns: {bad: "(a+)+$"}}',
            [("error", "security.redaction.custom_patterns.bad", "herness.yaml")],
        ),
        (
            "tool_call_parser: hermes}",
            "tool_call_parser: hermes, port: 8200}",
            [("error", "deploy", "herness.yaml")],
        ),
        (
            "tool_call_parser: hermes}",
            'tool_call_parser: "<parser>"}',
            [
                ("warn", "deploy.reasoning.tool_call_parser", "herness.yaml"),
            ],
        ),
    ],
    ids=["patterns", "ports", "pins"],
)
def test_ut10_19_model_validators(
    cfg_dir: Path, old: str, new: str, expected: list[tuple[str, str, str]]
) -> None:
    """UT10-19 model validator rows (patterns, ports, pins) come back as issues, not raises."""
    _replace(cfg_dir, old, new)
    issues = c.validate(cfg_dir, "local", offline=True)
    assert [(i.severity, i.path, i.file) for i in issues] == expected


def test_ut10_19_owner_rows_and_sorting(cfg_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-19 fake owner validators registered through U10-109 join validate's sorted list."""
    seen: list[bool] = []

    def decisions(cfg: c.HernessConfig, *, offline: bool) -> list[dict[str, str]]:
        seen.append(offline)
        return [{"severity": "warn", "path": "decisions.primary_decider", "message": "m"}]

    cv.register_owner_validator("enrich.deciders", decisions)
    _edit(monkeypatch, ("security.ui.bind", ALL_IFACES))
    assert _shape(c.validate(cfg_dir, "local", offline=True)) == [
        ("C05", "error", "security.ui.bind"),  # load failed: owners and registry do not run
    ]
    _edit(monkeypatch, ("deploy.large.sha256", "<sha256>"))
    issues = c.validate(cfg_dir, "local", offline=True)
    assert [(i.severity, i.path) for i in issues] == [
        ("warn", "decisions.primary_decider"),
        ("warn", "deploy.large.sha256"),
    ]
    assert seen == [True]


def test_ut10_19_table_rows() -> None:
    """UT10-19 CROSS_CHECKS holds C01-C25 minus the structural IDs, in order, with modes."""
    rows = {row.id: (row.severity, row.mode) for row in cv.CROSS_CHECKS}
    assert list(rows) == [
        *(f"C{n:02d}" for n in range(1, 8)),
        "C08a", "C08b", "C09", "C10", "C11", "C12", "C13", "C14",
        "C16", "C17", "C20", "C21", "C23", "C24", "C25",
    ]  # fmt: skip
    assert {k for k, v in rows.items() if v[0] == "warn"} == {"C13", "C21", "C23"}
    assert {k for k, v in rows.items() if v[1] == "online"} == {"C06", "C08b", "C23"}
    assert {k for k, v in rows.items() if v[1] == "registry"} == {"C03"}


def test_ut10_19_load_config_step8(cfg_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-19 U10-09 step 8: an error row fails load with issues; a warn row is logged."""
    _edit(monkeypatch, ("security.ui.bind", ALL_IFACES), ("deploy.large.sha256", "<sha256>"))
    with pytest.raises(c.ConfigError, match=r"^invalid config \(2 issues") as info:
        c.load_config("local", config_dir=cfg_dir)
    assert _shape(list(info.value.issues)) == [  # type: ignore[arg-type]
        ("C05", "error", "security.ui.bind"),
        ("C13", "warn", "deploy.large.sha256"),
    ]
    _edit(monkeypatch, ("deploy.large.sha256", "<sha256>"))
    with structlog.testing.capture_logs() as logs:
        assert c.load_config("local", config_dir=cfg_dir).profile == "local"
    issues = [e for e in logs if e["event"] == "config.validate.issue"]
    assert [(e["log_level"], e["path"], e["severity"]) for e in issues] == [
        ("warning", "deploy.large.sha256", "warn")
    ]


def test_ut10_19_validate_reports_load_errors(tmp_path: Path) -> None:
    """UT10-19 a ConfigError without structured issues still becomes one error issue."""
    issues = c.validate(tmp_path / "absent", "local")
    assert [(i.severity, i.path) for i in issues] == [("error", "config")]
    assert issues[0].message.startswith("config dir not found")


def test_ut10_19_offline_validate_under_one_second(cfg_dir: Path) -> None:
    """UT10-19 BT10-01 acceptance: offline validate of the template tree is well under 1 s."""
    c.validate(cfg_dir, "local", offline=True)  # warm imports
    started = time.perf_counter()
    assert c.validate(cfg_dir, "local", offline=True) == []
    assert time.perf_counter() - started < 3.0  # generous margin for CI; measured ~0.1 s


# --- UT10-75 compose file ---------------------------------------------------------------------


def _checks(cfg_dir: Path, compose: Path | None = None) -> list[tuple[str, str, str]]:
    cfg = c.load_config("local", config_dir=cfg_dir)
    compose = compose or cfg_dir.parent / "docker" / "compose.yaml"
    return _shape(
        cv.run_cross_checks(cfg, offline=True, include_registry=False, compose_path=compose)
    )


def _compose_edit(cfg_dir: Path, old: str, new: str) -> None:
    compose = cfg_dir.parent / "docker" / "compose.yaml"
    text = compose.read_text(encoding="utf-8")
    assert old in text
    compose.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_ut10_75_compose_matches_resilience(cfg_dir: Path) -> None:
    """UT10-75 the U10-80 compose copy and the impl 08 default resilience.yaml: no issues."""
    assert _checks(cfg_dir) == []


def test_ut10_75_altered_port_is_c08a(cfg_dir: Path) -> None:
    """UT10-75 altering the resilience URL port gives exactly one C08a error."""
    resilience = cfg_dir / "resilience.yaml"
    text = resilience.read_text(encoding="utf-8")
    resilience.write_text(text.replace("127.0.0.1:8200", "127.0.0.1:8201"), encoding="utf-8")
    with pytest.raises(c.ConfigError) as info:
        c.load_config("local", config_dir=cfg_dir)
    assert _shape(list(info.value.issues)) == [  # type: ignore[arg-type]
        ("C08a", "error", f"{GPU}.large.services.llamacpp-large.url"),
    ]


@pytest.mark.parametrize(
    ("old", "new", "path"),
    [
        ('profiles: ["decider"]', 'profiles: ["reasoning"]', f"{GPU}.decider.services.openjev"),
        ("  openjev:\n", "  openjev-renamed:\n", f"{GPU}.decider.services.openjev"),
        ("services:\n", "services: 3\nx-unused:\n", GPU),
        ("  openjev:\n", "  openjev: 3\n  openjev-old:\n", f"{GPU}.decider.services.openjev"),
    ],
    ids=["profile", "missing", "services-not-a-mapping", "service-not-a-mapping"],
)
def test_ut10_75_compose_service_rules(cfg_dir: Path, old: str, new: str, path: str) -> None:
    """UT10-75 C08a: a service missing from compose or with the wrong profile is an error."""
    cfg = c.load_config("local", config_dir=cfg_dir)
    _compose_edit(cfg_dir, old, new)
    compose = cfg_dir.parent / "docker" / "compose.yaml"
    issues = cv.run_cross_checks(cfg, offline=True, include_registry=False, compose_path=compose)
    assert (("C08a", "error", path)) in _shape(issues)
    assert {s[0] for s in _shape(issues)} == {"C08a"}


def test_ut10_75_compose_missing_or_unreadable(cfg_dir: Path) -> None:
    """UT10-75 no compose file is one C08a warn (T10-23); an unparsable file is an error."""
    missing = cfg_dir.parent / "nowhere.yaml"
    assert _checks(cfg_dir, missing) == [("C08a", "warn", "resilience.resilience.gpu.classes")]
    _compose_edit(cfg_dir, "name: herness", "name: [unclosed")
    with pytest.raises(c.ConfigError) as info:
        c.load_config("local", config_dir=cfg_dir)
    assert _shape(list(info.value.issues)) == [  # type: ignore[arg-type]
        ("C08a", "error", "resilience.resilience.gpu.classes")
    ]


def test_ut10_75_list_root_is_an_issue(cfg_dir: Path) -> None:
    """UT10-75 a compose file whose root is a list is one C08a error, never a raise."""
    (cfg_dir.parent / "docker" / "compose.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
    cfg = c.load_config("local", config_dir=write_checked_config(cfg_dir.parent / "other"))
    compose = cfg_dir.parent / "docker" / "compose.yaml"
    issues = cv.run_cross_checks(cfg, offline=True, include_registry=False, compose_path=compose)
    assert _shape(issues) == [("C08a", "error", GPU)]


def test_ut10_75_missing_compose_still_checks_ports(cfg_dir: Path) -> None:
    """UT10-75 without a compose file the resilience URL port vs deploy port half still runs."""
    resilience = cfg_dir / "resilience.yaml"
    text = resilience.read_text(encoding="utf-8")
    resilience.write_text(text.replace("127.0.0.1:8100", "127.0.0.1:8101"), encoding="utf-8")
    (cfg_dir.parent / "docker" / "compose.yaml").unlink()
    with pytest.raises(c.ConfigError) as info:
        c.load_config("local", config_dir=cfg_dir)
    assert _shape(list(info.value.issues)) == [  # type: ignore[arg-type]
        ("C08a", "error", f"{GPU}.decider.services.openjev.url"),
        ("C08a", "warn", GPU),
    ]


def test_ut10_19_c03_import_failure_is_an_issue(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-19 C03: any exception from registry.get is one error naming only its class."""

    def boom(kind: str, name: str) -> object:
        msg = "secret-ish import text"
        raise RuntimeError(msg)

    monkeypatch.setattr(cv.registry, "get", boom)
    issues = c.validate(cfg_dir, "local", offline=True)
    assert {s[0] for s in _shape(issues)} == {"C03"}
    assert all(i.message.endswith(": RuntimeError") for i in issues)
    assert "secret-ish" not in " ".join(str(i) for i in issues)


def test_ut10_19_c06_uses_backend_of_validated_config(
    cfg_dir: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-19 C06: with backend dotenv the validated config's .env is read, not the keyring."""
    monkeypatch.setattr(cv, "_wsl_available", lambda: False)
    monkeypatch.setattr(c, "_KEY_ID_PROVIDER", None)  # redact loaded: no key load
    monkeypatch.setenv("HERNESS_ENV", "dev")
    _replace(cfg_dir, "security:\n", "security:\n  secrets: {backend: dotenv}\n")
    names = cv.secrets.referenced_secret_names(c.load_config("local", config_dir=cfg_dir))
    for name in names:
        fake_keyring.store[("herness", name)] = _present(name)  # must not be consulted
    keys = [n.replace(".", "_").replace("-", "_").upper() for n in names if n != "vllm.api_key"]
    lines = "".join(f"HERNESS_SECRET__{k}=value-present\n" for k in keys)
    (cfg_dir.parent / ".env").write_text(lines, encoding="utf-8")
    assert _online(cfg_dir, "C06") == [("C06", "error", "secret:vllm.api_key")]
    _replace(cfg_dir, "secrets: {backend: dotenv}", "secrets: {backend: keyring}")
    assert _online(cfg_dir, "C06") == []
