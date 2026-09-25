"""Tests for tools.check_audit (U00-57)."""

import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from herness.core import time as clock
from tools.check_audit import load_ignore, main

pytestmark = pytest.mark.unit

TODAY = "2026-09-25"


def _pip_audit(
    path: Path, vuln_id: str, aliases: list[str], fix_versions: list[str], package: str = "demo"
) -> None:
    dependency = {
        "name": package,
        "version": "1.0.0",
        "vulns": [{"id": vuln_id, "fix_versions": fix_versions, "aliases": aliases}],
    }
    skipped = {"name": "herness", "skip_reason": "not on PyPI"}
    path.write_text(json.dumps({"dependencies": [dependency, skipped], "fixes": []}), "utf-8")


def _osv(path: Path, vuln_id: str, severity: str | None, package: str = "Demo") -> None:
    group: dict[str, Any] = {"ids": [vuln_id], "aliases": [vuln_id]}
    if severity is not None:
        group["max_severity"] = severity
    entry = {
        "package": {"name": package, "version": "1.0.0", "ecosystem": "PyPI"},
        "vulnerabilities": [
            {
                "id": vuln_id,
                "aliases": [],
                "affected": [{"ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}]}],
            }
        ],
        "groups": [group],
    }
    doc = {"results": [{"source": {"path": "req.txt"}, "packages": [entry]}]}
    path.write_text(json.dumps(doc), "utf-8")


def _ignore(path: Path, entries: list[dict[str, str]]) -> None:
    blocks = [
        "[[ignore]]\n" + "".join(f'{key} = "{value}"\n' for key, value in entry.items())
        for entry in entries
    ]
    path.write_text("ignore = []\n" if not blocks else "\n".join(blocks), "utf-8")


def _run(tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str, str]:
    code = main(
        [
            "--pip-audit",
            str(tmp_path / "pip-audit.json"),
            "--osv",
            str(tmp_path / "osv.json"),
            "--ignore",
            str(tmp_path / "ignore.toml"),
            "--today",
            TODAY,
            *extra,
        ]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_ut00_69_merges_pip_audit_and_osv_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT00-69 a pip-audit ID and its osv alias merge into one finding with severity and fix."""
    _pip_audit(tmp_path / "pip-audit.json", "PYSEC-1", ["GHSA-a"], ["1.0.1"])
    _osv(tmp_path / "osv.json", "GHSA-a", "8.1")
    _ignore(tmp_path / "ignore.toml", [])
    summary = tmp_path / "summary.md"

    code, out, _ = _run(tmp_path, capsys, "--summary", str(summary))

    assert code == 1
    findings = [line for line in out.splitlines() if line.startswith("AU00")]
    assert findings == ["AU001 blocking demo 1.0.0 GHSA-a,PYSEC-1 severity=8.1 fix=yes"]
    assert "AU010 skipped herness" in out
    table = summary.read_text(encoding="utf-8").splitlines()
    assert table[0] == "| package | version | ids | severity | fix | decision |"
    assert table[2] == "| demo | 1.0.0 | GHSA-a, PYSEC-1 | 8.1 | yes | AU001 blocking |"
    assert len(table) == 3


_EXPIRED = {
    "id": "PYSEC-1",
    "package": "Demo",
    "reason": "no exploit path",
    "expires": "2026-09-24",
}
_CURRENT = {**_EXPIRED, "expires": TODAY}


@pytest.mark.parametrize(
    ("severity", "fix_versions", "entries", "expected"),
    [
        ("7.5", ["1.0.1"], [], (1, "AU001 blocking demo")),
        ("7.5", [], [], (0, "AU002 warning demo")),
        (None, ["1.0.1"], [], (1, "AU001 blocking demo")),
        ("9.8", ["1.0.1"], [_CURRENT], (0, "ignored demo")),
        ("9.8", ["1.0.1"], [_EXPIRED], (1, "AU003 expired ignore demo")),
    ],
    ids=["high-fix", "high-no-fix", "unknown-fix", "ignored", "expired"],
)
def test_st00_07_audit_gate_decisions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    severity: str | None,
    fix_versions: list[str],
    entries: list[dict[str, str]],
    expected: tuple[int, str],
) -> None:
    """ST00-07 blocks High with a fix, unknown severity with a fix and expired ignores."""
    _pip_audit(tmp_path / "pip-audit.json", "PYSEC-1", ["GHSA-a"], fix_versions)
    _osv(tmp_path / "osv.json", "GHSA-a", severity)
    _ignore(tmp_path / "ignore.toml", entries)

    code, out, _ = _run(tmp_path, capsys)

    expected_code, expected_line = expected
    assert code == expected_code
    assert any(line.startswith(expected_line) for line in out.splitlines()), out
    assert "AU011" not in out


def test_ut00_69_check_audit_unused_ignore_and_osv_only_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An osv-only finding with a fixed event counts as fixable; an unmatched entry warns."""
    (tmp_path / "pip-audit.json").write_text('{"dependencies": []}', "utf-8")
    _osv(tmp_path / "osv.json", "GHSA-b", "5.0", package="Other_Pkg")
    osv = json.loads((tmp_path / "osv.json").read_text("utf-8"))
    events = osv["results"][0]["packages"][0]["vulnerabilities"][0]["affected"][0]["ranges"][0]
    events["events"].append({"fixed": "2.0"})
    (tmp_path / "osv.json").write_text(json.dumps(osv), "utf-8")
    _ignore(tmp_path / "ignore.toml", [_CURRENT])

    code, out, _ = _run(tmp_path, capsys)

    assert code == 0
    assert "AU002 warning other-pkg 1.0.0 GHSA-b severity=5.0 fix=yes" in out
    assert "AU011 unused ignore PYSEC-1 demo" in out


@pytest.mark.parametrize(
    "ignore_text",
    [
        '[[ignore]]\nid = "X"\npackage = "p"\nreason = "r"\n',
        '[[ignore]]\nid = "X"\npackage = "p"\nreason = "r"\nexpires = "soon"\n',
        "ignore = 3\n",
        "not toml = = \n",
    ],
    ids=["missing-key", "bad-date", "not-a-list", "bad-toml"],
)
def test_ut00_69_check_audit_rejects_malformed_ignore_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], ignore_text: str
) -> None:
    """A malformed ignore file is an input error (exit 2)."""
    _pip_audit(tmp_path / "pip-audit.json", "PYSEC-1", [], [])
    _osv(tmp_path / "osv.json", "GHSA-a", "1.0")
    (tmp_path / "ignore.toml").write_text(ignore_text, "utf-8")

    code, _, err = _run(tmp_path, capsys)

    assert code == 2
    assert "input error" in err


def test_ut00_69_check_audit_rejects_malformed_json_and_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Malformed JSON, a missing file, a bad --today and a missing option all exit 2."""
    (tmp_path / "pip-audit.json").write_text("{not json", "utf-8")
    _osv(tmp_path / "osv.json", "GHSA-a", "1.0")
    _ignore(tmp_path / "ignore.toml", [])
    assert _run(tmp_path, capsys)[0] == 2

    (tmp_path / "pip-audit.json").write_text('{"dependencies": {}}', "utf-8")
    assert _run(tmp_path, capsys)[0] == 2

    (tmp_path / "pip-audit.json").unlink()
    assert _run(tmp_path, capsys)[0] == 2

    _pip_audit(tmp_path / "pip-audit.json", "PYSEC-1", [], [])
    assert _run(tmp_path, capsys, "--today", "25-09-2026")[0] == 2
    assert main(["--osv", str(tmp_path / "osv.json")]) == 2
    capsys.readouterr()


def test_ut00_69_check_audit_defaults_to_ignore_file_and_clock_today(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --ignore and --today it reads tools/audit_ignore.toml and the UTC day."""
    repo_ignore = Path(__file__).resolve().parents[3] / "tools" / "audit_ignore.toml"
    assert load_ignore(repo_ignore) == []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        clock, "now", lambda: datetime.datetime(2026, 9, 25, 23, 30, tzinfo=datetime.UTC)
    )
    (tmp_path / "tools").mkdir()
    _ignore(tmp_path / "tools" / "audit_ignore.toml", [{**_EXPIRED, "expires": "2026-09-25"}])
    _pip_audit(tmp_path / "pip-audit.json", "PYSEC-1", [], ["1.0.1"])
    _osv(tmp_path / "osv.json", "GHSA-z", "2.0")

    code = main(["--pip-audit", "pip-audit.json", "--osv", "osv.json"])
    out = capsys.readouterr().out

    assert code == 0
    assert "ignored demo 1.0.0 PYSEC-1 severity=unknown fix=yes" in out
    assert "AU002 warning demo 1.0.0 GHSA-z severity=2.0 fix=no" in out
