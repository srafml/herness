"""Tests for herness.reports.rules: roles, user_ref, require_role, validators, messages (T09-02)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import errors as e
from herness.core.settings import RolesConfig
from herness.harness.memory.types import MemoryNotFound
from herness.reports import rules as r

pytestmark = pytest.mark.unit

KEY = b"unit-test-key-not-secret"  # pragma: allowlist secret
ALICE_REF = "5c737e395de21e30772d7d9d0dbc5a16"  # pragma: allowlist secret
REF = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
ULID = "01HZX3K5V7Q8R9S0T1V2W3X4Y5"
KEY_NAME = "ui_user_ref_key"
NON_HERNESS = "See the log `data/logs/herness-<date>.jsonl`."
DOCTOR = "See `herness doctor` and the log `data/logs/herness-<date>.jsonl`."
LIST_FIX = (
    "Check the id with the matching list command "
    "(`herness jobs list`, `herness review-queue list`, `herness memory list`)."
)


@pytest.fixture(autouse=True)
def _reset(fake_keyring: MemoryKeyring) -> Iterator[None]:
    """In-memory keyring; config and the user_ref key cache reset around every test."""
    c.reset_config()
    yield
    c.reset_config()


def _audit_lines(cfg: c.HernessConfig) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for p in sorted(Path(cfg.paths.logs).glob("audit-*.jsonl"))
        for line in p.read_text("utf-8").splitlines()
    ]


# --- UT09-50 / PT09-06 user_ref ------------------------------------------------------------------


def test_ut09_50_user_ref_vector_and_case() -> None:
    """UT09-50 known key/name vector; `Alice` and `alice ` give the same 32-hex reference."""
    assert r.user_ref_for("alice", KEY) == ALICE_REF
    assert r.user_ref_for("Alice", KEY) == ALICE_REF
    assert r.user_ref_for("alice ", KEY) == ALICE_REF
    assert r.user_ref_for("  ALICE\t", KEY) == ALICE_REF
    assert r.user_ref_for("bob", KEY) != ALICE_REF
    assert r.user_ref_for("alice", b"another-key") != ALICE_REF
    with pytest.raises(r.UserInputError):
        r.user_ref_for("   ", KEY)
    with pytest.raises(e.ConfigError):
        r.user_ref_for("alice", b"")


def test_ut09_50_load_key_cached_and_reset(fake_keyring: MemoryKeyring) -> None:
    """UT09-50 key read once per process from the secret backend; reset_config clears the cache."""
    fake_keyring.store[("herness", "ui_user_ref_key")] = KEY.decode()
    assert r.load_user_ref_key() == KEY
    fake_keyring.store[("herness", "ui_user_ref_key")] = "changed-value-not-secret"
    assert r.load_user_ref_key() == KEY  # cached
    c.reset_config()
    assert r.load_user_ref_key() == b"changed-value-not-secret"


def test_ut09_50_load_key_missing() -> None:
    """UT09-50 missing key -> ConfigError code secret_missing with the `secrets init` fix."""
    with pytest.raises(e.ConfigError, match=r"^secret not found: ui_user_ref_key$") as exc:
        r.load_user_ref_key()
    assert exc.value.details["code"] == "secret_missing"
    assert exc.value.details["secret"] == KEY_NAME
    assert r.user_message(exc.value) == (
        "The user reference key is missing.",
        "Run `herness secrets init`.",
    )


_names = st.text(alphabet=st.characters(codec="utf-8"), min_size=1, max_size=40).filter(
    lambda s: s.strip() != ""
)
_ws = st.sampled_from(["", " ", "\t", "  ", "\n"])


@given(name=_names, left=_ws, right=_ws, key=st.binary(min_size=1, max_size=64))
def test_pt09_06_user_ref_properties(name: str, left: str, right: str, key: bytes) -> None:
    """PT09-06 output is 32 lowercase hex; case and surrounding whitespace invariant."""
    ref = r.user_ref_for(name, key)
    assert re.fullmatch(r"[0-9a-f]{32}", ref)
    assert r.user_ref_for(left + name + right, key) == ref
    assert r.user_ref_for(name.lower(), key) == r.user_ref_for(name.strip().lower(), key)
    assert r.user_ref_for(name.upper().lower(), key) == r.user_ref_for(name.upper(), key)


# --- UT09-51 role_for ----------------------------------------------------------------------------


def test_ut09_51_role_for() -> None:
    """UT09-51 admin wins over reviewer; default applied; None or empty -> denied."""
    roles = RolesConfig(admins=("Ann", "both"), reviewers=("rita", "Both"))
    assert r.role_for("ann", roles) == "admin"
    assert r.role_for("  ANN ", roles) == "admin"
    assert r.role_for("both", roles) == "admin"
    assert r.role_for("Rita", roles) == "reviewer"
    assert r.role_for("zed", roles) == "viewer"
    assert r.role_for(None, roles) == "denied"
    assert r.role_for("  ", roles) == "denied"
    assert r.role_for("", roles) == "denied"
    closed = RolesConfig(reviewers=("rita",), default_role="denied")
    assert r.role_for("zed", closed) == "denied"
    assert r.role_for("rita", closed) == "reviewer"


# --- UT09-52 require_role and tables -------------------------------------------------------------


def test_ut09_52_tables() -> None:
    """UT09-52 ACTION_ROLES and ACTION_TEXT keys are equal; ranks as specified; mismatch raises."""
    assert set(r.ACTION_ROLES) == set(r.ACTION_TEXT)
    assert len(r.ACTION_ROLES) == 15
    assert dict(r.ROLE_RANK) == {"denied": 0, "viewer": 1, "reviewer": 2, "admin": 3}
    assert get_args(r.UiRole.__value__) == ("denied", "viewer", "reviewer", "admin")
    assert r.ACTION_ROLES["job_inline"] == "admin"
    assert r.ACTION_ROLES["report_render"] == "viewer"
    assert r.ACTION_ROLES["report_rerender"] == "admin"
    with pytest.raises(TypeError):
        r.ACTION_ROLES["view"] = "admin"  # type: ignore[index]
    with pytest.raises(e.ConfigError):
        r._check_tables({"a": "viewer"}, {"b": "x"})
    r._check_tables({"a": "viewer"}, {"a": "x"})


def test_ut09_52_actor_display_not_in_repr() -> None:
    """UT09-52 Actor is frozen and its display name stays out of repr."""
    actor = r.Actor(REF, "viewer", "cli", display="Alice Example")
    assert "Alice" not in repr(actor)
    with pytest.raises(AttributeError):
        actor.role = "admin"  # type: ignore[misc]


_HIGHER = [a for a, need in r.ACTION_ROLES.items() if need in {"reviewer", "admin"}]


@pytest.mark.parametrize("action", _HIGHER)
def test_ut09_52_viewer_refused(tmp_path: Path, action: str) -> None:
    """UT09-52 viewer on each reviewer/admin action: PermissionDenied with phrase, one auth line."""
    cfg = c.init_config(config_dir=write_full_config(tmp_path), env={})
    actor = r.Actor(REF, "viewer", "dashboard", display="Alice")
    needed = r.ACTION_ROLES[action]
    with capture_logs() as logs, pytest.raises(e.PermissionDenied) as exc:
        r.require_role(actor, action)
    assert exc.value.message == f"You need the {needed} role to {r.ACTION_TEXT[action]}."
    group = "admins" if needed == "admin" else "reviewers"
    assert exc.value.hint == f"Ask an admin to add you to security.ui.roles.{group}."
    (line,) = _audit_lines(cfg)
    assert line["event"] == "auth"
    assert line["actor"] == REF
    assert line["fields"] == {"user_ref": REF, "role": "viewer", "result": "denied"}
    denied = [x for x in logs if x["event"] == "app.auth.denied"]
    assert denied == [
        {
            "component": "app.auth",
            "event": "app.auth.denied",
            "log_level": "warning",
            "user_ref": REF,
            "role": "viewer",
            "action": action,
            "channel": "dashboard",
        }
    ]
    assert "Alice" not in json.dumps(logs) + json.dumps(line)


def test_ut09_52_job_inline_phrase(tmp_path: Path) -> None:
    """UT09-52 job_inline is admin-only; the reviewer is refused with the R-45 phrase."""
    c.init_config(config_dir=write_full_config(tmp_path), env={})
    with pytest.raises(e.PermissionDenied) as exc:
        r.require_role(r.Actor(REF, "reviewer", "cli", display="x"), "job_inline")
    assert exc.value.message == (
        "You need the admin role to run jobs in this process with `--inline` (R-45)."
    )


def test_ut09_52_allowed_and_unknown(tmp_path: Path) -> None:
    """UT09-52 allowed calls return without audit; unknown action -> ConfigError."""
    cfg = c.init_config(config_dir=write_full_config(tmp_path), env={})
    admin = r.Actor(REF, "admin", "cli", display="x")
    for action in r.ACTION_ROLES:
        r.require_role(admin, action)
    r.require_role(r.Actor(REF, "viewer", "cli", display="x"), "report_render")
    r.require_role(r.Actor(REF, "reviewer", "cli", display="x"), "review_decide")
    assert _audit_lines(cfg) == []
    with pytest.raises(e.ConfigError, match=r"^unknown action nope$"):
        r.require_role(admin, "nope")


def test_ut09_52_audit_failure_still_refuses(tmp_path: Path) -> None:
    """UT09-52 anonymous actor fails the audit actor check: audit_failed logged, still refused."""
    cfg = c.init_config(config_dir=write_full_config(tmp_path), env={})
    actor = r.Actor("anonymous", "denied", "dashboard", display="x")
    with capture_logs() as logs, pytest.raises(e.PermissionDenied) as exc:
        r.require_role(actor, "view")
    assert exc.value.hint == "Ask an admin to add you to security.ui.roles.reviewers."
    assert _audit_lines(cfg) == []
    failed = [x for x in logs if x["event"] == "app.auth.audit_failed"]
    assert failed == [
        {
            "component": "app.auth",
            "event": "app.auth.audit_failed",
            "log_level": "error",
            "action": "view",
            "error_type": "SchemaViolation",
        }
    ]
    assert any(x["event"] == "app.auth.denied" for x in logs)


# --- UT09-65 user_message ------------------------------------------------------------------------


class NoCurrentBuild(e.NotFound):
    """Stand-in for app/common/wh.py NoCurrentBuild (matched by class name)."""


class _SubNoCurrent(NoCurrentBuild):
    pass


def _d(**kw: str) -> dict[str, str]:
    return kw


_ROWS: list[tuple[BaseException, tuple[str, str]]] = [
    (NoCurrentBuild("no current build"), ("No promoted warehouse yet.", "Run `herness pipeline`.")),
    (_SubNoCurrent("x"), ("No promoted warehouse yet.", "Run `herness pipeline`.")),
    (
        e.NotFound("x", details=_d(code="no_current")),
        ("No promoted warehouse yet.", "Run `herness pipeline`."),
    ),
    (e.NotFound("job job_1 not found", details=_d(kind="job")), ("job job_1 not found", LIST_FIX)),
    (MemoryNotFound("memory", "mem_1"), ("memory not found: mem_1", LIST_FIX)),
    (
        e.ReportContractError("x", details=_d(code="build_retired", build_id="b1")),
        (
            "Build b1 used by this run was deleted by retention.",
            "Re-run the review: `herness report funding`.",
        ),
    ),
    (
        e.ReportContractError("x", details=_d(code="draft_missing", run_id="run_1")),
        (
            "Run run_1 has no valid report draft.",
            "Check `herness status`; resume with `herness resume run_1`.",
        ),
    ),
    (
        e.ReportContractError("x", details=_d(code="draft_invalid", run_id="run_2")),
        (
            "Run run_2 has no valid report draft.",
            "Check `herness status`; resume with `herness resume run_2`.",
        ),
    ),
    (
        e.ReportContractError("x", details=_d(code="uncited", count="3")),
        (
            "3 numbers in the draft have no evidence.",
            "Re-run the review, or `--no-strict` to inspect.",
        ),
    ),
    (
        e.ReportContractError(
            "x", details=_d(code="run_not_finished", run_id="r", status="running")
        ),
        (
            "Run r is not finished (status running).",
            "Wait for the run, or `herness resume r`.",
        ),
    ),
    (
        e.ReportContractError("bad contract"),
        ("bad contract", "Re-run the review, or render with `--no-strict` to inspect."),
    ),
    (
        e.ConfigError("x", details=_d(code="pdf_missing")),
        (
            "PDF engine not installed.",
            "`uv sync --extra pdf` (GTK/Pango on Windows, spec 10).",
        ),
    ),
    (
        e.StoreBusy("x", details=_d(job_id="job_9")),
        ("Another pipeline is running (job job_9).", "Wait, or `herness jobs list`."),
    ),
    (
        e.StoreBusy("x"),
        (
            "Ops store is locked.",
            "Retry; check for a stuck process in `herness status`.",
        ),
    ),
    (
        e.ModelUnavailable("x", details=_d(url="http://gpu:8000")),
        (
            "Reasoning model not reachable at http://gpu:8000.",
            "`herness deploy up reasoning`; `herness doctor`.",
        ),
    ),
    (
        e.ModelUnavailable("x"),
        (
            "Reasoning model not reachable at the configured endpoint.",
            "`herness deploy up reasoning`; `herness doctor`.",
        ),
    ),
    (
        e.AuthError(
            "x",
            details={"source": "jira", "secret": "jira.token"},  # pragma: allowlist secret
        ),
        (
            "jira rejected the credentials.",
            "`herness secrets set jira.token`; `herness doctor --sources`.",
        ),
    ),
    (
        e.EgressBlocked("x"),
        (
            "An off-network call was refused by the data policy.",
            "Use profile `local`, or record the approval in `herness.yaml` (spec 10).",
        ),
    ),
    (
        e.PermissionDenied(
            "You need the reviewer role to approve items.",
            hint="Ask an admin to add you to security.ui.roles.reviewers.",
        ),
        (
            "You need the reviewer role to approve items.",
            "Ask an admin to add you to security.ui.roles.reviewers.",
        ),
    ),
    (
        e.ConfigError("x", details={"code": "secret_missing", "secret": KEY_NAME}),
        ("The user reference key is missing.", "Run `herness secrets init`."),
    ),
    (e.ConfigError("secret not found: other"), ("secret not found: other", DOCTOR)),
    (
        e.ConfigError(
            "x",
            details={"code": "secret_missing", "secret": "other"},  # pragma: allowlist secret
        ),
        ("x", DOCTOR),
    ),
    (e.QueryError("bad sql", hint="Fix the query."), ("bad sql", "Fix the query.")),
    (r.UserInputError("invalid job id"), ("invalid job id", DOCTOR)),
    (ValueError("secret-ish detail"), ("Unexpected error.", NON_HERNESS)),
]


@pytest.mark.parametrize(("exc", "expected"), _ROWS)
def test_ut09_65_user_message_rows(exc: BaseException, expected: tuple[str, str]) -> None:
    """UT09-65 each table row gives its exact what/fix strings."""
    assert r.user_message(exc) == expected


def test_ut09_65_user_message_fallbacks() -> None:
    """UT09-65 missing details still give non-empty text; no hint on PermissionDenied -> default."""
    cases: list[BaseException] = [
        e.ReportContractError("x", details=_d(code="build_retired")),
        e.ReportContractError("x", details=_d(code="uncited")),
        e.ReportContractError("x", details=_d(code="run_not_finished")),
        e.AuthError("x"),
        e.PermissionDenied("no"),
        e.SchemaViolation(""),
        KeyboardInterrupt(),
    ]
    for exc in cases:
        what, fix = r.user_message(exc)
        assert what
        assert fix
        assert "Traceback" not in what + fix
    assert r.user_message(e.AuthError("x"))[0] == "The source rejected the credentials."
    assert r.user_message(e.PermissionDenied("no"))[1] == DOCTOR


# --- UT09-72 validators and ids ------------------------------------------------------------------


def test_ut09_72_reason() -> None:
    """UT09-72 reasons of 9, 10 and 1001 chars: error, ok, error; control chars removed."""
    with pytest.raises(r.UserInputError, match=r"^Reason must be 10 to 1000 characters\.$"):
        r.validate_reason("a" * 9)
    assert r.validate_reason("a" * 10) == "a" * 10
    assert r.validate_reason("a" * 1000) == "a" * 1000
    with pytest.raises(r.UserInputError):
        r.validate_reason("a" * 1001)
    assert r.validate_reason("  \x1b[31mbad\x07 reason\x9b ok\n\tend\x00 ") == (
        "[31mbad reason ok\n\tend"
    )
    with pytest.raises(r.UserInputError):
        r.validate_reason("\x00" * 20 + "short")


def test_ut09_72_note() -> None:
    """UT09-72 note of 501 chars with max_chars=500 -> error; required and empty -> error."""
    with pytest.raises(r.UserInputError):
        r.validate_note("n" * 501, required=False, max_chars=500)
    assert r.validate_note("n" * 500, required=False, max_chars=500) == "n" * 500
    assert r.validate_note("n" * 1000, required=True) == "n" * 1000
    with pytest.raises(r.UserInputError):
        r.validate_note("n" * 1001, required=True)
    for empty in (None, "", "  \x01 "):
        with pytest.raises(r.UserInputError, match=r"^A note is required to reject\.$"):
            r.validate_note(empty, required=True)
        assert r.validate_note(empty, required=False) is None
    assert r.validate_note(" ok\x7f ", required=False) == "ok"


def test_ut09_72_question_correction_answer() -> None:
    """UT09-72 question, correction and answer bounds after cleaning."""
    assert r.validate_question(" why? ", max_chars=5) == "why?"
    for bad in ("", " \x02 ", "123456"):
        with pytest.raises(r.UserInputError):
            r.validate_question(bad, max_chars=5)
    assert r.validate_correction("c" * 1000) == "c" * 1000
    for bad in ("", "c" * 1001):
        with pytest.raises(r.UserInputError):
            r.validate_correction(bad)
    assert r.validate_answer(" yes ") == "yes"
    for bad in ("\t", "y" * 201):
        with pytest.raises(r.UserInputError):
            r.validate_answer(bad)


@pytest.mark.parametrize(
    ("pattern", "prefix"),
    [
        (r.SESSION_ID_RE, "ses_"),
        (r.MESSAGE_ID_RE, "msg_"),
        (r.ITEM_ID_RE, "rev_"),
        (r.REC_ID_RE, "rec_"),
        (r.JOB_ID_RE, "job_"),
        (r.MEMORY_ID_RE, "mem_"),
    ],
)
def test_ut09_72_id_patterns(pattern: re.Pattern[str], prefix: str) -> None:
    """UT09-72 id patterns: prefix + 26 Crockford chars; anything else -> invalid <kind> id."""
    good = prefix + ULID
    assert r.check_id(pattern, good, "x") == good
    bad_values = [
        ULID,
        prefix + ULID[:-1],
        prefix + ULID + "A",
        prefix + ULID.lower(),
        prefix + ULID[:-1] + "I",
        prefix + ULID + "\n",
        "../" + good,
        "zzz_" + ULID,
    ]
    for bad in bad_values:
        with pytest.raises(r.UserInputError, match=r"^invalid job id$") as exc:
            r.check_id(pattern, bad, "job")
        assert bad not in str(exc.value)
    assert isinstance(r.UserInputError("x"), e.RecoverableError)
