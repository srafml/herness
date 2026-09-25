"""Tests for tools.check_module_size (U00-55)."""

from pathlib import Path

import pytest

from tools.check_module_size import main

pytestmark = pytest.mark.unit


def _pyproject(root: Path, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(body, encoding="utf-8")


def _doc(root: Path, name: str, rows: list[tuple[str, int]]) -> None:
    doc_dir = root / "docs" / "impl"
    doc_dir.mkdir(parents=True, exist_ok=True)
    lines = ["| Path | Purpose | Line budget |", "|------|---------|-------------|"]
    lines += [f"| `{path}` | purpose | {budget} |" for path, budget in rows]
    (doc_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _module(root: Path, rel: str, n_lines: int) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(f"x_{i} = {i}" for i in range(n_lines)) + "\n"
    path.write_text(text, encoding="utf-8")


def _run(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = main(["--root", str(root)])
    return code, capsys.readouterr().out


def test_ut00_59_default_override_and_doc_budgets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT00-59 an override, a doc budget and a passing default-budget file."""
    over = tmp_path / "over"
    _pyproject(
        over,
        "[tool.herness.module_budgets]\n"
        "default = 400\n"
        'overrides = { "herness/a.py" = { limit = 220, reason = "override for a" } }\n',
    )
    _doc(over, "00-foundation.impl.md", [("herness/b.py", 150)])
    _module(over, "herness/a.py", 221)
    _module(over, "herness/b.py", 151)
    _module(over, "herness/c.py", 400)

    code, out = _run(over, capsys)

    assert code == 1
    assert "herness/a.py:0: MS001 221 lines > budget 220" in out
    assert "herness/b.py:0: MS001 151 lines > budget 150" in out
    assert "herness/c.py" not in out

    # A repository within all budgets exits 0 with no output.
    clean = tmp_path / "clean"
    _pyproject(clean, "[tool.herness.module_budgets]\ndefault = 400\n")
    _module(clean, "herness/ok.py", 10)
    _module(clean, "tools/ok.py", 10)

    assert _run(clean, capsys) == (0, "")


def test_ut00_60_empty_reason_and_conflicting_doc_budgets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT00-60 an empty-reason override gives MS003; conflicting docs give MS002."""
    conflict = tmp_path / "conflict"
    _pyproject(
        conflict,
        "[tool.herness.module_budgets]\n"
        "default = 400\n"
        'overrides = { "herness/x.py" = { limit = 500, reason = "" } }\n',
    )
    _doc(conflict, "00-foundation.impl.md", [("herness/c.py", 100)])
    _doc(conflict, "01-store.impl.md", [("herness/c.py", 120)])

    code, out = _run(conflict, capsys)

    assert code == 1
    assert "MS003" in out
    assert "MS002" in out

    # A missing default in the config table is a usage error (exit 2).
    no_default = tmp_path / "no_default"
    _pyproject(no_default, "[tool.herness.module_budgets]\n")

    assert main(["--root", str(no_default)]) == 2

    # A file that cannot decode as UTF-8 gives MS004.
    bad = tmp_path / "bad"
    _pyproject(bad, "[tool.herness.module_budgets]\ndefault = 400\n")
    path = bad / "herness" / "bad.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00bad")

    code, out = _run(bad, capsys)

    assert code == 1
    assert "MS004" in out
