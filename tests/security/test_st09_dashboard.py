"""ST09-02 (sanitiser part), ST09-04 - ST09-06, ST09-21 (dashboard path), ST09-28."""

from __future__ import annotations

import ast
import getpass
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest
from tests.support.app_env import (
    USER_REF_KEY,
    actor_script,
    fake_services,
    preserved_main,
    ui_config,
)

from app.common import auth, bootstrap
from app.common.auth import resolve_identity
from herness.reports import _os_user
from herness.reports.rules import role_for

pytestmark = pytest.mark.unit

APP = Path(__file__).resolve().parents[2] / "app"
FORGED = {"X-Forwarded-User": "root-admin"}


@pytest.fixture(autouse=True)
def _keep_main() -> Iterator[None]:
    with preserved_main():
        yield


def test_st09_04_header_from_untrusted_peer_denied() -> None:
    """ST09-04 exposed mode, header from peer 10.0.0.9: identity not taken; denied."""
    ui = ui_config(exposed=True, admins=["root-admin"])
    identity = resolve_identity(FORGED, "10.0.0.9", ui, "svc")
    assert (identity.username, identity.reason) == (None, "untrusted_peer")
    assert role_for(identity.username, ui.roles) == "denied"


def test_st09_05_expose_disabled_forged_header_ignored() -> None:
    """ST09-05 expose disabled, forged header: OS user used, header ignored."""
    ui = ui_config(admins=["root-admin"], default_role="denied")
    identity = resolve_identity(FORGED, "127.0.0.1", ui, "svc")
    assert (identity.username, identity.source) == ("svc", "os")
    assert identity.reason == "header_ignored_expose_disabled"
    assert role_for(identity.username, ui.roles) == "denied"


@pytest.mark.parametrize("peer", ["127.0.0.1", "10.0.0.9", None])
def test_st09_06_exposed_non_loopback_bind_denies_everyone(peer: str | None) -> None:
    """ST09-06 exposed with non-loopback bind: all users denied (the U09-54 half).

    The `herness ui` refusal half belongs to U09-93 (T09-22), not yet in the tree.
    """
    ui = ui_config(exposed=True, bind="0.0.0.0", admins=["root-admin"])  # noqa: S104
    identity = resolve_identity(FORGED, peer, ui, "root-admin")
    assert (identity.username, identity.reason) == (None, "bind_not_loopback")
    assert role_for(identity.username, ui.roles) == "denied"


_SPOOF_VARS = ("USERNAME", "LOGNAME", "USER", "LNAME")


@pytest.mark.parametrize("variable", _SPOOF_VARS)
def test_st09_21_dashboard_env_spoof_gains_no_role(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """ST09-21 dashboard path (TH09-27): a denied user exporting an admin's name stays denied."""
    calls: list[dict[str, Any]] = []
    services = fake_services(ui_config(admins=["root-admin"], default_role="denied"))
    monkeypatch.setattr(bootstrap, "get_services", lambda: services)
    monkeypatch.setattr(auth, "load_user_ref_key", lambda: USER_REF_KEY)
    monkeypatch.setattr(auth, "audit", lambda _e, _a, **fields: calls.append(fields))
    monkeypatch.setattr(_os_user, "windows_user", lambda: "real-user")
    monkeypatch.setattr(_os_user, "posix_user", lambda: "real-user")
    for name in _SPOOF_VARS:  # only the variable under test is set: getpass would return it
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "root-admin")
    assert getpass.getuser() == "root-admin"
    at = AppTest.from_function(actor_script)
    at.run()
    assert not at.exception
    role, _ref, display = at.text[0].value.split("|")
    assert (role, display) == ("denied", f"user:{_ref[:8]}")
    assert [c["role"] for c in calls] == ["denied"]


@pytest.mark.skipif(os.name != "nt", reason="Windows token lookup")
def test_st09_21_dashboard_os_user_is_the_token_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """ST09-21 dashboard path, real Windows token: spoofed variables do not change the OS user."""
    real = _os_user.windows_user()
    for variable in _SPOOF_VARS:
        monkeypatch.setenv(variable, "root-admin")
    assert getpass.getuser() == "root-admin"  # the spoof works on the env-reading API
    assert auth._process_user() == real


HOSTILE_ANSWER = (
    "Total ![](http://x/?d=secret) [click](javascript:x) $\\frac{a}{b}$ "
    "<img src=x onerror=a> see https://evil.example/p and [ok](#ev-q_0123456789abcdef)"
)
HOSTILE_MEMORY = "Remember ![logo](https://e.example/l.png) and :red[alert] $$x$$"


def test_st09_02_markdown_elements_have_no_image_link_or_latex() -> None:
    """ST09-02 (sanitiser part) chat answer and memory content: no image, external link, LaTeX."""
    script = (
        "import streamlit as st\n"
        "from app.common.sanitize import safe_markdown\n"
        f"st.markdown(safe_markdown({HOSTILE_ANSWER!r}, "
        "allowed_anchor_ids=['q_0123456789abcdef']))\n"
        f"st.markdown(safe_markdown({HOSTILE_MEMORY!r}))\n"
    )
    at = AppTest.from_string(script)
    at.run()
    assert not at.exception
    bodies = [element.value for element in at.markdown]
    assert len(bodies) == 2
    for body in bodies:
        assert "![" not in body
        assert "](http" not in body
        assert "](javascript" not in body
        assert "<" not in body
        assert "$" not in body.replace("\\$", "")
        assert ":red[" not in body
    assert "[ok](#ev-q_0123456789abcdef)" in bodies[0]
    assert "`https://evil.example/p`" in bodies[0]


def _not_false(value: ast.expr) -> bool:
    return not (isinstance(value, ast.Constant) and value.value is False)


def _unsafe_kw(kw: ast.keyword) -> bool:
    """`unsafe_allow_html=<not False>`, also inside a `**{...}` dict literal."""
    if kw.arg == "unsafe_allow_html":
        return _not_false(kw.value)
    if kw.arg is None and isinstance(kw.value, ast.Dict):
        return any(
            isinstance(key, ast.Constant) and key.value == "unsafe_allow_html" and _not_false(val)
            for key, val in zip(kw.value.keys, kw.value.values, strict=True)
        )
    return False


def _streamlit_names(tree: ast.AST) -> set[str]:
    """Names bound to `streamlit` or one of its submodules (`import streamlit as x`, ...)."""
    names = {"st", "streamlit"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {
                a.asname or a.name.split(".")[0]
                for a in node.names
                if a.name == "streamlit" or a.name.startswith("streamlit.")
            }
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("streamlit"):
            names |= {a.asname or a.name for a in node.names if a.name != "html"}
    return names


def _root(func: ast.expr) -> str | None:
    while isinstance(func, ast.Attribute):
        func = func.value
    return func.id if isinstance(func, ast.Name) else None


def _violations(path: Path) -> list[tuple[int, str]]:
    """Forbidden HTML paths in one file, as `(line, kind)` in line order."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    names = _streamlit_names(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            found += [(node.lineno, "unsafe_allow_html") for kw in node.keywords if _unsafe_kw(kw)]
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "html" and _root(func) in names:
                found.append((node.lineno, "html call"))
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("streamlit")
            and any(alias.name == "html" for alias in node.names)
        ):
            found.append((node.lineno, "html import"))
    return sorted(found)


def test_st09_28_no_unsafe_html_in_app() -> None:
    """ST09-28 AST scan of `app/`: no `unsafe_allow_html=True` and no `st.html` call."""
    files = sorted(APP.rglob("*.py"))
    assert files
    assert [(path.name, v) for path in files for v in _violations(path)] == []


def test_st09_28_scanner_detects_violations(tmp_path: Path) -> None:
    """ST09-28 the scanner itself flags each forbidden form, including aliases and `**{}`."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import streamlit as st\n"  # 1
        "from streamlit import html\n"  # 2
        "st.markdown('x', unsafe_allow_html=True)\n"  # 3
        "st.html('<b>')\n"  # 4
        "st.write('x', unsafe_allow_html=flag)\n"  # 5
        "st.markdown('ok', unsafe_allow_html=False)\n"  # 6
        "import streamlit as x\n"  # 7
        "x.html('<b>')\n"  # 8
        "import streamlit.components.v1 as components\n"  # 9
        "components.html('<b>')\n"  # 10
        "streamlit.components.v1.html('<b>')\n"  # 11
        "from streamlit.components.v1 import html as h2\n"  # 12
        "st.markdown('x', **{'unsafe_allow_html': True})\n"  # 13
        "st.markdown('x', **{'unsafe_allow_html': False})\n"  # 14
        "other.html('ok')\n",  # 15 - not streamlit
        encoding="utf-8",
    )
    assert _violations(bad) == [
        (2, "html import"),
        (3, "unsafe_allow_html"),
        (4, "html call"),
        (5, "unsafe_allow_html"),
        (8, "html call"),
        (10, "html call"),
        (11, "html call"),
        (12, "html import"),
        (13, "unsafe_allow_html"),
    ]
