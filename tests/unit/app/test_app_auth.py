"""UT09-45 - UT09-49, UT09-53, UT09-83: dashboard identity, roster and session actor."""

from __future__ import annotations

import getpass
from collections.abc import Iterator
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest
from tests.support.app_env import (
    PROXY,
    USER_REF_KEY,
    actor_script,
    fake_services,
    preserved_main,
    ui_config,
)

from app.common import auth, bootstrap
from app.common.auth import Identity, build_roster, display_name, resolve_identity
from herness.core.errors import FatalError
from herness.reports import _os_user
from herness.reports.rules import role_for, user_ref_for

pytestmark = pytest.mark.unit

HEADER = {"X-Forwarded-User": "alice@corp"}


@pytest.fixture(autouse=True)
def _keep_main() -> Iterator[None]:
    with preserved_main():
        yield


def test_ut09_45_expose_disabled_uses_os_user() -> None:
    """UT09-45 expose disabled with header: OS user, reason `header_ignored_expose_disabled`."""
    ui = ui_config()
    assert resolve_identity(HEADER, "127.0.0.1", ui, "svc") == Identity(
        "svc", "os", "header_ignored_expose_disabled"
    )
    assert resolve_identity({}, None, ui, "svc") == Identity("svc", "os", None)


@pytest.mark.parametrize("bind", ["0.0.0.0", "10.0.0.5"])  # noqa: S104 - cases under test
@pytest.mark.parametrize("headers", [{}, HEADER])
def test_ut09_45_expose_disabled_non_loopback_bind_denied(
    bind: str, headers: dict[str, str]
) -> None:
    """UT09-45 expose off + non-loopback bind: no identity, `bind_not_loopback` (header or not)."""
    ui = ui_config(bind=bind, admins=["svc"])
    identity = resolve_identity(headers, "127.0.0.1", ui, "svc")
    assert identity == Identity(None, "none", "bind_not_loopback")
    assert role_for(identity.username, ui.roles) == "denied"


@pytest.mark.parametrize("bind", ["127.0.0.1", "::1"])
def test_ut09_45_expose_disabled_loopback_bind_os_identity(bind: str) -> None:
    """UT09-45 expose off + loopback bind: the OS identity."""
    assert resolve_identity({}, None, ui_config(bind=bind), "svc") == Identity("svc", "os", None)


def test_ut09_83_non_loopback_bind_session_denied_and_logged(
    session: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-83 expose off + bind 0.0.0.0: session actor denied; reason logged (no header value)."""
    services = fake_services(ui_config(bind="0.0.0.0", reviewers=["alice"]))  # noqa: S104
    monkeypatch.setattr(bootstrap, "get_services", lambda: services)
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(auth._log, "warning", lambda event, **kw: seen.append((event, kw)))
    at = AppTest.from_function(actor_script)
    at.run()
    assert not at.exception
    assert at.text[0].value == "denied|anonymous|anonymous"
    assert seen == [("app.identity.header_ignored", {"reason": "bind_not_loopback"})]
    assert session == [
        {"event": "auth", "user_ref": "anonymous", "role": "denied", "result": "denied"}
    ]


def test_ut09_46_trusted_proxy_header_identity() -> None:
    """UT09-46 exposed, loopback bind, proxy 127.0.0.1, peer 127.0.0.1: header identity."""
    ui = ui_config(exposed=True)
    assert resolve_identity(HEADER, "127.0.0.1", ui, "svc") == Identity(
        "alice@corp", "header", None
    )
    lower = {"x-forwarded-user": "  bob.smith  "}  # case-insensitive name, value stripped
    assert resolve_identity(lower, "127.0.0.1", ui, "svc").username == "bob.smith"
    ipv6 = ui_config(exposed=True, bind="::1", proxy="::1")
    assert resolve_identity(HEADER, "0:0:0:0:0:0:0:1", ipv6, "svc").source == "header"


def test_ut09_47_untrusted_or_unknown_peer() -> None:
    """UT09-47 peer 10.0.0.5 -> `untrusted_peer`; peer None -> `remote_ip_unknown`."""
    ui = ui_config(exposed=True)
    assert resolve_identity(HEADER, "10.0.0.5", ui, "svc") == Identity(
        None, "none", "untrusted_peer"
    )
    assert resolve_identity(HEADER, None, ui, "svc") == Identity(None, "none", "remote_ip_unknown")
    assert resolve_identity(HEADER, "not-an-ip", ui, "svc").reason == "untrusted_peer"
    no_proxy = ui_config(exposed=True, proxy=None)
    assert resolve_identity(HEADER, "127.0.0.1", no_proxy, "svc").reason == "no_trusted_proxy"


def test_ut09_48_exposed_non_loopback_bind() -> None:
    """UT09-48 exposed with bind 0.0.0.0: `bind_not_loopback`, username None."""
    ui = ui_config(exposed=True, bind="0.0.0.0")  # noqa: S104 - the case under test
    assert resolve_identity(HEADER, "127.0.0.1", ui, "svc") == Identity(
        None, "none", "bind_not_loopback"
    )


@pytest.mark.parametrize(
    ("value", "reason"),
    [("   ", "header_missing"), ("a<b>", "header_invalid"), ("a" * 300, "header_invalid")],
)
def test_ut09_49_blank_or_invalid_header(value: str, reason: str) -> None:
    """UT09-49 header blank, `a<b>`, 300 chars: `header_missing`, `header_invalid` x2."""
    ui = ui_config(exposed=True)
    assert resolve_identity({"X-Forwarded-User": value}, PROXY, ui, "svc") == Identity(
        None, "none", reason
    )
    assert resolve_identity({}, PROXY, ui, "svc").reason == "header_missing"


def test_ut09_53_display_names() -> None:
    """UT09-53 roster: known name; `user:<8>` for others; `anonymous` stays `anonymous`."""
    roster = build_roster(ui_config(admins=["ops-lead"], reviewers=["rev"]).roles, USER_REF_KEY)
    admin_ref = user_ref_for("ops-lead", USER_REF_KEY)
    assert set(roster.values()) == {"ops-lead", "rev"}
    assert display_name(admin_ref, roster) == "ops-lead"
    other = user_ref_for("stranger", USER_REF_KEY)
    assert display_name(other, roster) == f"user:{other[:8]}"
    assert display_name("anonymous", roster) == "anonymous"
    with pytest.raises(TypeError):
        roster["x"] = "y"  # type: ignore[index]  # immutable


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Fake services (alice is a reviewer), a fixed OS user and an `audit` spy."""
    calls: list[dict[str, Any]] = []
    services = fake_services(ui_config(reviewers=["alice"]))
    monkeypatch.setattr(bootstrap, "get_services", lambda: services)
    monkeypatch.setattr(auth, "_process_user", lambda **_: "alice")
    monkeypatch.setattr(auth, "load_user_ref_key", lambda: USER_REF_KEY)
    monkeypatch.setattr(
        auth, "audit", lambda event, actor, **fields: calls.append({"event": event, **fields})
    )
    return calls


def test_ut09_83_one_auth_audit_line_per_session(session: list[dict[str, Any]]) -> None:
    """UT09-83 AppTest with two reruns: one `auth` audit line for the session."""
    at = AppTest.from_function(actor_script)
    at.run()
    at.run()
    assert not at.exception
    ref = user_ref_for("alice", USER_REF_KEY)
    assert at.text[0].value == f"reviewer|{ref}|alice"
    assert session == [{"event": "auth", "user_ref": ref, "role": "reviewer", "result": "allowed"}]
    second = AppTest.from_function(actor_script)  # a new browser session audits again
    second.run()
    assert len(session) == 2


def test_ut09_83_audit_failure_refuses_session(
    session: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-83 an unauditable session is refused: the error reaches the page; nothing cached."""

    def broken(*_: object, **__: object) -> None:
        msg = "audit log not writable"
        raise FatalError(msg)

    monkeypatch.setattr(auth, "audit", broken)
    at = AppTest.from_function(actor_script)
    at.run()
    assert at.exception
    assert not at.text
    assert "herness_actor" not in at.session_state
    assert session == []


def test_ut09_83_header_reason_logged_without_value(
    session: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-83 an identity reason is logged as `app.identity.header_ignored`, never the value."""
    seen: list[tuple[str, dict[str, Any]]] = []
    ignored = Identity("alice", "os", "header_ignored_expose_disabled")
    monkeypatch.setattr(auth, "resolve_identity", lambda *_: ignored)
    monkeypatch.setattr(auth._log, "warning", lambda event, **kw: seen.append((event, kw)))
    at = AppTest.from_function(actor_script)
    at.run()
    assert not at.exception
    assert seen == [("app.identity.header_ignored", {"reason": "header_ignored_expose_disabled"})]
    assert len(session) == 1


def test_ut09_83_credential_failure_falls_back_with_one_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT09-83 failed credential lookup -> `getpass` fallback, warned once without the name."""

    def broken() -> str:
        msg = "no token"
        raise OSError(msg)

    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(_os_user, "windows_user", broken)
    monkeypatch.setattr(_os_user, "posix_user", broken)
    monkeypatch.setattr(getpass, "getuser", lambda: "fallback-user")
    monkeypatch.setattr(auth, "_fallback_logged", [])
    monkeypatch.setattr(auth._log, "warning", lambda event, **kw: seen.append((event, kw)))
    assert auth._process_user(on_fallback=auth._warn_fallback) == "fallback-user"
    assert auth._process_user(on_fallback=auth._warn_fallback) == "fallback-user"
    assert seen == [("app.identity.fallback", {"source": "getpass"})]
