"""Security tests for the cross-checks (impl 10 U10-20 C05 and C16; ST10-04, ST10-27)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.support.config_tree import register_checked_names, write_checked_config

from herness.core import config as c
from herness.core import config_validate as cv
from herness.core import registry

pytestmark = pytest.mark.unit

# Built at run time so detect-secrets sees no literal credential in this file.
PLAIN_PW = "hunter2" * 2
PLAIN_KEY = "sk-" + "live" + "-" + "4f9a" * 6


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Iterator[Path]:
    register_checked_names()
    yield write_checked_config(tmp_path)
    c.reset_config()
    registry.reset_registry()
    cv.reset_owner_validators()


def _shape(issues: list[c.ConfigIssue]) -> list[tuple[str, str, str]]:
    return [(i.message.split(" ")[0], i.severity, i.path) for i in issues]


def test_st10_04_plain_text_credentials_in_sources(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-04 sources.yaml with password and api_key_secret in plain text: C16 errors.

    ``SourcesConfig`` forbids unknown keys, so the plain values are placed in the merged raw
    dict that C16 sweeps (defense in depth behind ``SecretRefStr``); no value is echoed.
    """
    real = cv._tree

    def tree(cfg: c.HernessConfig) -> dict[str, Any]:
        data = real(cfg)
        data["sources"]["sources"]["servicenow"] = {
            "enabled": False,
            "password": PLAIN_PW,
            "api_key_secret": PLAIN_KEY,
        }
        return data

    monkeypatch.setattr(cv, "_tree", tree)
    issues = c.validate(cfg_dir, "local", offline=True)
    assert _shape(issues) == [
        ("C16", "error", "sources.sources.servicenow.api_key_secret"),
        ("C16", "error", "sources.sources.servicenow.password"),
    ]
    assert all(issue.file == "sources.yaml" for issue in issues)
    text = " ".join(str(issue) for issue in issues)
    assert PLAIN_PW not in text
    assert PLAIN_KEY not in text
    assert "write secret:<name>" in issues[1].message  # the bare-name hint (R-72)


def test_st10_04_rejected_at_load_too(cfg_dir: Path) -> None:
    """ST10-04 the same keys in the real sources.yaml never load, and nothing is echoed."""
    (cfg_dir / "sources.yaml").write_text(
        f"version: 1\nsources:\n  servicenow:\n    password: {PLAIN_PW}\n", encoding="utf-8"
    )
    issues = c.validate(cfg_dir, "local", offline=True)
    assert issues
    assert all(issue.severity == "error" for issue in issues)
    assert PLAIN_PW not in " ".join(str(issue) for issue in issues)


def _ui(cfg_dir: Path, ui: str) -> list[tuple[str, str, str]]:
    herness = cfg_dir / "herness.yaml"
    text = herness.read_text(encoding="utf-8")
    old = "  redaction: {directory_file: null}\n"
    herness.write_text(text.replace(old, old + f"  ui: {ui}\n"), encoding="utf-8")
    return _shape(c.validate(cfg_dir, "local", offline=True))


BIND = "security.ui.bind"
PROXY = "security.ui.expose.trusted_proxy"


@pytest.mark.parametrize(
    ("ui", "expected"),
    [
        ("{bind: 0.0.0.0}", [("C05", "error", BIND)]),
        (
            "{bind: 0.0.0.0, expose: {enabled: true, trusted_proxy: 127.0.0.1}}",
            [("C05", "error", BIND), ("C21", "warn", "security.ui.roles.default_role")],
        ),
        ("{expose: {enabled: true}, roles: {default_role: denied}}", [("C05", "error", PROXY)]),
        (
            "{expose: {enabled: true, trusted_proxy: 10.0.0.9}}",
            [("C05", "error", PROXY), ("C21", "warn", "security.ui.roles.default_role")],
        ),
        ("{expose: {enabled: false, trusted_proxy: 10.0.0.9}}", [("C05", "error", PROXY)]),
        (
            "{bind: '::1', expose: {enabled: true, trusted_proxy: '::1'},"
            " roles: {default_role: denied}}",
            [],
        ),
    ],
    ids=["bind", "bind-exposed", "no-proxy", "lan-proxy", "lan-proxy-unexposed", "loopback-ok"],
)
def test_st10_27_dashboard_exposure(cfg_dir: Path, ui: str, expected: list[Any]) -> None:
    """ST10-27 non-loopback bind, expose without trusted_proxy, LAN proxy: C05 errors (R-50)."""
    assert _ui(cfg_dir, ui) == expected
