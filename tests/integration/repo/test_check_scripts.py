"""IT00-02: the three CI check scripts pass on the repository as is."""

from pathlib import Path

import pytest

from tools import check_module_size, check_traceability, check_type_ownership

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]


class TraceabilityDebtError(AssertionError):
    """check_traceability found violations in the repository's docs or tests."""


@pytest.mark.xfail(
    strict=True,
    raises=TraceabilityDebtError,
    reason=(
        "check_traceability still reports doc defects in specs 01-11 (TR001, TR002, TR004, "
        "TR005); remove this marker once the consistency pass has cleaned them"
    ),
)
def test_it00_02_check_scripts_pass_on_repo(capsys: pytest.CaptureFixture[str]) -> None:
    """IT00-02 check_type_ownership, check_module_size and check_traceability each return 0."""
    root = ["--root", str(ROOT)]
    assert check_type_ownership.main(root) == 0, capsys.readouterr().out
    assert check_module_size.main(root) == 0, capsys.readouterr().out
    capsys.readouterr()

    code = check_traceability.main(root)

    if code != 0:
        raise TraceabilityDebtError(capsys.readouterr().out)
