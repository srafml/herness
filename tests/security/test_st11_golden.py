"""Security tests for the golden suite (ST11-06 TH11-09, ST11-12 suite part TH11-08)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from tests.unit.eval._golden_fixtures import BUILD_ID, make_con, question, truth, write_suite

from herness.core.errors import ConfigError
from herness.eval import golden as g

pytestmark = pytest.mark.unit

_COUNTED = ("core.incident", "core.team", "core.service", "metrics.team_mttr")


def _counts(con: object) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in _COUNTED:
        row = con.execute(f"SELECT count(*) FROM {table}").fetchone()  # type: ignore[attr-defined]  # noqa: S608 - fixed table names
        out[table] = int(row[0])
    return out


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM core.incident",
        "COPY core.team TO 'x.csv'",
        "SELECT * FROM read_csv('x')",
    ],
)
def test_st11_06_hostile_reference_sql(
    sql: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST11-06 write/file-reading reference SQL is a suite error; nothing changes on disk."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x").write_text("a\n1\n", encoding="utf-8")
    con = make_con()
    before = _counts(con)
    for block in (
        {"numeric": {"reference_sql": sql, "unit": "count", "tolerance": {"abs": 0}}},
        {"entities": {"reference_sql": sql, "check": "rank1"}},
        {"placeholders_sql": sql, "rubric": {"criteria": ["clear"], "min_score": 3}},
    ):
        q = g.EvalQuestion.model_validate(question(expected=block))
        with pytest.raises(g.SuiteError, match="rejected") as info:
            g.resolve(q, truth(), con, build_id=BUILD_ID, dataset_kind="synthetic", cache={})
        assert info.value.context["question_id"] == "G03"
    assert _counts(con) == before
    assert not (tmp_path / "x.csv").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x"]


def _assert_fast_config_error(path: Path) -> None:
    start = time.perf_counter()
    with pytest.raises(ConfigError):
        g.load_suite(path)
    assert time.perf_counter() - start < 2.0


def test_st11_12_two_megabyte_suite_rejected(tmp_path: Path) -> None:
    """ST11-12 a 2 MB suite raises ConfigError within 2 s (cap 1 MB)."""
    path = write_suite(tmp_path / "golden.yaml", [question()])
    path.write_text(path.read_text("utf-8") + "# " + "x" * 2_000_000 + "\n", "utf-8")
    _assert_fast_config_error(path)


def test_st11_12_million_aliases_rejected(tmp_path: Path) -> None:
    """ST11-12 a suite YAML with 10^6 aliases raises ConfigError within 2 s."""
    path = tmp_path / "aliases.yaml"
    path.write_text("a: &a x\nquestions: [" + ",".join(["*a"] * 1_000_000) + "]\n", "utf-8")
    _assert_fast_config_error(path)


def test_st11_12_alias_bomb_under_size_cap_rejected(tmp_path: Path) -> None:
    """ST11-12 a sub-1 KB billion-laughs suite (under the size cap) raises ConfigError."""
    lines = ["l0: &l0 [lol, lol, lol, lol, lol, lol, lol, lol, lol, lol]"]
    lines += [f"l{i}: &l{i} [{', '.join([f'*l{i - 1}'] * 10)}]" for i in range(1, 10)]
    lines.append("questions: *l9")
    path = tmp_path / "bomb.yaml"
    path.write_text("\n".join(lines) + "\n", "utf-8")
    assert path.stat().st_size < 1024
    _assert_fast_config_error(path)


def test_st11_12_safe_load_rejects_python_tags(tmp_path: Path) -> None:
    """ST11-12 python object tags are refused by the safe loader."""
    path = tmp_path / "tag.yaml"
    path.write_text("version: !!python/object/apply:os.system ['echo hi']\n", "utf-8")
    _assert_fast_config_error(path)
