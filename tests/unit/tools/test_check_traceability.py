"""Tests for tools.check_traceability (U00-56).

Every ID a test needs in its tmp docs is built at run time with ``_id`` so this file's own
function names and docstrings carry exactly one ID each.
"""

from pathlib import Path

import pytest

from tools.check_traceability import main

pytestmark = pytest.mark.unit


def _id(kind: str, spec: str, number: str) -> str:
    return f"{kind}{spec}-{number}"


def _doc(root: Path, name: str, text: str) -> None:
    doc_dir = root / "docs" / "impl"
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / name).write_text(text, encoding="utf-8")


def _code(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _card(card: str, tests: str) -> str:
    return f"#### {card} Card\n\n| Field | Content |\n|-------|---------|\n| Tests | {tests} |\n\n"


def _test_rows(*ids: str) -> str:
    rows = "".join(f"| {i} | under test |\n" for i in ids)
    return f"## 11. Tests\n\n| ID | Under test |\n|----|-----------|\n{rows}"


def _run(root: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str]:
    code = main(["--root", str(root), *extra])
    return code, capsys.readouterr().out


def test_ut00_61_undefined_reference(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-61 a reference to an undefined unit ID gives TR001; fenced lines are skipped."""
    undefined = _id("U", "00", "99")
    fenced = _id("U", "00", "98")
    _doc(tmp_path, "00-a.impl.md", f"# Spec\n\nSee {undefined}.\n\n```\n{fenced}\n```\n")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert f"docs/impl/00-a.impl.md:3: TR001 undefined {undefined}" in out
    assert fenced not in out


def test_ut00_62_duplicate_definition(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-62 a task ID defined twice gives TR002."""
    task = _id("T", "00", "01")
    _doc(tmp_path, "00-a.impl.md", f"#### {task} First\n\n#### {task} Second\n")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert f"docs/impl/00-a.impl.md:3: TR002 duplicate {task}" in out


def test_ut00_63_definition_in_wrong_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT00-63 a spec-05 unit defined in a spec-00 doc gives TR003."""
    unit = _id("U", "05", "01")
    _doc(tmp_path, "00-x.impl.md", f"#### {unit} thing\n")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert f"docs/impl/00-x.impl.md:1: TR003 wrong spec {unit}" in out


def test_ut00_64_threat_without_security_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT00-64 a threat row with no ST ID gives TR004; a row with one does not."""
    bare, covered = _id("TH", "00", "01"), _id("TH", "00", "02")
    security = _id("ST", "00", "01")
    text = (
        "| ID | Threat | Tests |\n|----|--------|-------|\n"
        f"| {bare} | spoofing | none |\n| `{covered}` | tampering | {security} |\n\n"
        + _card(_id("T", "00", "01"), security)
        + _test_rows(security)
    )
    _doc(tmp_path, "00-a.impl.md", text)

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert f"docs/impl/00-a.impl.md:3: TR004 threat without security test {bare}" in out
    assert f"TR004 threat without security test {covered}" not in out


def test_ut00_65_test_not_on_a_card(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-65 a test row on no card's Tests row gives TR005; card ranges count as listed."""
    listed, in_range, orphan = _id("UT", "00", "01"), _id("UT", "00", "04"), _id("UT", "00", "07")
    unit_only = _id("UT", "00", "08")
    card_tests = f"{listed}, {_id('UT', '00', '03')}\N{HORIZONTAL ELLIPSIS}{_id('UT', '00', '05')}"
    text = (
        _card(_id("T", "00", "01"), card_tests)
        + f"#### {_id('U', '00', '01')} unit\n\n| Field | Content |\n|---|---|\n"
        + f"| Tests | {unit_only} |\n\n"
        + _test_rows(listed, in_range, orphan, unit_only)
    )
    _doc(tmp_path, "00-a.impl.md", text)

    code, out = _run(tmp_path, capsys)

    assert code == 1
    tr005 = [line for line in out.splitlines() if " TR005 " in line]
    assert sorted(line.rsplit(" ", 1)[1] for line in tr005) == [orphan, unit_only]


def test_ut00_66_code_ids(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-66 unknown, reused and multiple IDs on test functions give TR006, TR007, TR010."""
    one, two = _id("UT", "00", "01"), _id("UT", "00", "02")
    _doc(
        tmp_path,
        "00-a.impl.md",
        _card(_id("T", "00", "01"), f"{one}, {two}") + _test_rows(one, two),
    )
    _code(
        tmp_path,
        "tests/unit/test_x.py",
        "def test_ut00_99_x():\n    pass\n\n\n"
        f'def test_a():\n    """{one} first"""\n\n\n'
        f'async def test_b():\n    """{one} second"""\n\n\n'
        f'def test_ut00_02_c():\n    """{one} and more"""\n',
    )
    _code(tmp_path, "tests/unit/test_broken.py", "def test_(:\n")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert "tests/unit/test_x.py:1: TR006 unknown test id UT00-99 on test_ut00_99_x" in out
    assert f"tests/unit/test_x.py:9: TR007 test id used twice {one}" in out
    assert f"tests/unit/test_x.py:13: TR010 several ids on one test {one}, {two}" in out
    assert "tests/unit/test_broken.py:1: TR090 syntax error" in out


def test_ut00_67_cross_spec_placeholder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-67 an unresolved cross-spec placeholder gives TR008."""
    placeholder = "X:" + "05" + "/" + "herness.harness.loop.run"
    _doc(tmp_path, "00-a.impl.md", f"# Spec\n\nCalls {placeholder} here.\n")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert f"docs/impl/00-a.impl.md:3: TR008 unresolved cross-spec reference {placeholder}" in out


def test_ut00_68_require_implemented(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-68 with --require-implemented only the uncarried test ID gives TR009."""
    one, two = _id("UT", "00", "01"), _id("UT", "00", "02")
    _doc(
        tmp_path,
        "00-a.impl.md",
        _card(_id("T", "00", "01"), f"{one}, {two}") + _test_rows(one, two),
    )
    _code(tmp_path, "tests/unit/test_x.py", "def test_ut00_01_ok():\n    pass\n")

    assert _run(tmp_path, capsys) == (0, "defined=3 referenced=2 implemented=1\n")
    code, out = _run(tmp_path, capsys, "--require-implemented", "00")

    assert code == 1
    assert [line.split(": ", 1)[1] for line in out.splitlines()[:-1]] == [
        f"TR009 not implemented {two}"
    ]
    assert _run(tmp_path, capsys, "--require-implemented", "0")[0] == 2
    assert main(["--root", str(tmp_path / "missing")]) == 2
