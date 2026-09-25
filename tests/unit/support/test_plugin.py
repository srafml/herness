"""Tests for tests.support.plugin: marker enforcement and test IDs (U11-30, U11-31)."""

import json
from pathlib import Path

import pytest
from tests.support.plugin import TEST_ID_PATTERN, extract_test_ids

pytestmark = pytest.mark.unit

PLUGIN = ("-p", "tests.support.plugin", "-p", "no:asyncio")

TAGGED = '''
import pytest

pytestmark = pytest.mark.unit


def test_ut99_01_first() -> None:
    """UT99-01 first."""


def test_second() -> None:
    """UT99-02 second, tagged in the docstring."""


def test_third() -> None:
    """Untagged."""
'''


def _run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest(*PLUGIN, *args)


def test_ut11_31_missing_category_marker(pytester: pytest.Pytester) -> None:
    """UT11-31 a file without a category marker fails collection naming the file.

    A `slow` marker alone is not a category; the tagged file next to it is not listed.
    """
    pytester.makepyfile(
        test_nomark="import pytest\n\npytestmark = pytest.mark.slow\n\n"
        "def test_ut99_01_x() -> None:\n    pass\n",
        test_ok="import pytest\n\npytestmark = pytest.mark.unit\n\n"
        "def test_ut99_02_y() -> None:\n    pass\n",
    )
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    err = result.stderr.str()
    assert "test_nomark.py" in err
    assert "test_ok.py" not in err


def test_ut11_32_two_category_markers(pytester: pytest.Pytester) -> None:
    """UT11-32 `unit` plus `integration` on a file, or a second category on a test, fail."""
    pytester.makepyfile(
        test_both="import pytest\n\npytestmark = [pytest.mark.unit, pytest.mark.integration]\n\n"
        "def test_ut99_01_x() -> None:\n    pass\n",
    )
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "test_both.py" in result.stderr.str()
    (pytester.path / "test_both.py").unlink()
    pytester.makepyfile(
        test_mixed="import pytest\n\npytestmark = pytest.mark.unit\n\n"
        "@pytest.mark.fault\ndef test_ut99_02_y() -> None:\n    pass\n\n"
        "@pytest.mark.slow\ndef test_ut99_03_z() -> None:\n    pass\n",
    )
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    err = result.stderr.str()
    assert "test_mixed.py::test_ut99_02_y" in err
    assert "test_ut99_03_z" not in err


def test_ut11_33_extract_test_ids() -> None:
    """UT11-33 IDs from name and docstring are normalized, sorted and unique."""
    assert extract_test_ids("test_ut11_01_x", "Covers ST11-05.") == ("ST11-05", "UT11-01")
    assert extract_test_ids("test_it02_115_a", "it02-115 again; BT11_07.") == (
        "BT11-07",
        "IT02-115",
    )
    assert extract_test_ids("test_plain", None) == ()
    assert extract_test_ids("test_xut11_01", "UT11-0123 and UT1-01") == ()
    assert TEST_ID_PATTERN.fullmatch("et11-02") is not None


def test_ut11_34_duplicate_and_parametrized_ids(pytester: pytest.Pytester) -> None:
    """UT11-34 one ID on two functions is a usage error; parametrized items share one ID."""
    pytester.makepyfile(
        test_dup="import pytest\n\npytestmark = pytest.mark.unit\n\n"
        "def test_ut99_01_a() -> None:\n    pass\n\n"
        'def test_other() -> None:\n    """UT99-01 again."""\n',
    )
    result = _run(pytester, "--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    err = result.stderr.str()
    assert "UT99-01" in err
    assert "test_dup.py::test_other" in err
    (pytester.path / "test_dup.py").unlink()
    pytester.makepyfile(
        test_param="import pytest\n\npytestmark = pytest.mark.unit\n\n"
        "@pytest.mark.parametrize('n', [1, 2, 3])\n"
        "def test_ut99_02_p(n: int) -> None:\n    assert n\n",
    )
    out = pytester.path / "ids.json"
    result = _run(pytester, "--collect-only", f"--collect-test-ids={out}")
    assert result.ret == pytest.ExitCode.OK
    index = json.loads(out.read_text(encoding="utf-8"))
    assert list(index["ids"]) == ["UT99-02"]
    assert len(index["ids"]["UT99-02"]) == 3


def test_ut11_35_select_test_ids(pytester: pytest.Pytester) -> None:
    """UT11-35 `--select-test-ids` runs only the matching test; a bad ID is a usage error."""
    pytester.makepyfile(test_three=TAGGED)
    result = _run(pytester, "--select-test-ids=ut99_01")
    result.assert_outcomes(passed=1, deselected=2)
    result = _run(pytester, "--select-test-ids=UT99-01,UT99-02")
    result.assert_outcomes(passed=2, deselected=1)
    result = _run(pytester, "--select-test-ids=UT99-01,bogus")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "bogus" in result.stderr.str()
    result = _run(pytester, "--select-test-ids=,")
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_ut11_36_require_and_collect_test_ids(pytester: pytest.Pytester) -> None:
    """UT11-36 `--require-test-ids` lists untagged nodes; otherwise JSON lists them."""
    pytester.makepyfile(test_three=TAGGED)
    out = pytester.path / "build" / "out.json"
    result = _run(pytester, "--collect-only", f"--collect-test-ids={out}", "--require-test-ids")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "test_three.py::test_third" in result.stderr.str()
    assert not out.exists()
    result = _run(pytester, "--collect-only", f"--collect-test-ids={out}")
    assert result.ret == pytest.ExitCode.OK
    index = json.loads(out.read_text(encoding="utf-8"))
    assert index == {
        "schema": 1,
        "ids": {
            "UT99-01": ["test_three.py::test_ut99_01_first"],
            "UT99-02": ["test_three.py::test_second"],
        },
        "untagged": ["test_three.py::test_third"],
    }
    assert [path.name for path in Path(out.parent).iterdir()] == ["out.json"]
