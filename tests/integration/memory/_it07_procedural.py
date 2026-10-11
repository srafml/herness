"""Procedural-memory runs for IT07-06 and IT07-09 (T07-24; not a test module).

Review runs go through the T06-22 driver (`drv`) pinned to the tiny build, with the real spec
07 `MemoryStore`, whose record step calls the real `promote_procedural` (U07-90) against the
`CURRENT` build guard. The stub pipeline gives three analyst tasks per team; each scripted
analyst cites its own incident count (`cite_literal_sql`): a different literal priority per
task, so the run's three findings per team are distinct (no shared number, no dedup merge) and
all verified, and all three parameterise to one template per team. Team `t1` cites
`SQL_SHAPES["t1"]` and team `t2` `SQL_SHAPES["t2"]`, two templates whose fingerprints fall into
the LoRA `train` and `val` splits respectively (the U07-92 bucket of the fingerprint; the
aliases were picked for that). With the shipped `procedural.promote` gate (3 passes, 2 runs,
Wilson lower bound 0.7) three runs of three passes promote a template (9 passes: 0.701), two
runs do not (6 passes: 0.61). No side effects at import.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from tests.integration.harness import _swarm_run_env as e

from herness.core.ids import query_id as compute_query_id
from herness.store.ops import core, record_evidence

if TYPE_CHECKING:
    import pytest
    from tests.integration.harness._swarm_run_env import Drv

__all__ = ["PER_TEAM", "SQL_SHAPES", "cite_literal_sql", "sql_for", "template_rows"]

SQL_SHAPES: Final = {
    "t1": "SELECT count(*) AS incidents_0 FROM core.incident WHERE team_id = 't1'"
    " AND priority <= {k}",
    "t2": "SELECT count(*) AS incidents_3 FROM core.incident WHERE team_id = 't2'"
    " AND priority <= {k}",
}
PER_TEAM: Final = 3


def sql_for(team: str, k: int) -> str:
    """The `k`-th (1-based) literal query of `team`."""
    return SQL_SHAPES[team].format(k=k)


def cite_literal_sql(drv: Drv, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], str]:
    """Analysts cite literal queries (no `$` parameter, so promotion can parameterise them),
    the k-th analyst call of a team its k-th query; the evidence rows exist for the pinned
    build. Returns the replacement of `incident_qid`."""
    calls = dict.fromkeys(SQL_SHAPES, 0)
    for team in SQL_SHAPES:
        for k in range(1, PER_TEAM + 1):
            assert record_evidence(e._evidence(sql_for(team, k), {}, {"incidents": 7}))

    def qid(team: str) -> str:
        calls[team] += 1
        k = (calls[team] - 1) % PER_TEAM + 1
        return compute_query_id(sql_for(team, k), {}, vars(e)["BUILD_ID"])

    monkeypatch.setattr(e, "incident_qid", qid)
    drv.set_pipeline(t1=PER_TEAM, t2=PER_TEAM, t2_must=True)
    return qid


def template_rows() -> list[dict[str, Any]]:
    """Every `sql_template` item: id, status, content and parsed data, by fingerprint."""
    rows = core.read_all(
        "SELECT memory_id, status, content, data FROM memory_item WHERE kind = 'sql_template'"
        " ORDER BY json_extract(data, '$.fingerprint')", (),
    )  # fmt: skip
    return [dict(r) | {"data": core.load_json(r["data"], field="data")} for r in rows]
