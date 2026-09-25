"""IT00-02: the three CI check scripts pass on the repository as is."""

from pathlib import Path

import pytest

from tools import check_module_size, check_traceability, check_type_ownership

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "check_traceability still reports doc defects in specs 01-11 (TR001, TR002, TR004, "
        "TR005) and reused test IDs in tests/unit/tools/test_check_module_size.py (TR007); "
        "remove this marker once the consistency pass has cleaned them"
    ),
)
def test_it00_02_check_scripts_pass_on_repo(capsys: pytest.CaptureFixture[str]) -> None:
    """IT00-02 check_type_ownership, check_module_size and check_traceability each return 0."""
    codes = {
        module.__name__: module.main(["--root", str(ROOT)])
        for module in (check_type_ownership, check_module_size, check_traceability)
    }
    out = capsys.readouterr().out

    assert codes == dict.fromkeys(codes, 0), out
