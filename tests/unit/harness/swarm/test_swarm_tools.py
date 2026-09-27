"""UT06-43 … UT06-45, ST06-17: swarm-provided tools and the per-task tool map (U06-60 … U06-64).

`post_finding` and review-mode `list_findings` run against a real `Blackboard` over a migrated
ops store (`bb_env`); the spawn broker and the escalate callable are fakes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from tests.support.dispatch_standin import call, make_state, use_test_config
from tests.unit.harness import _blackboard_env as env_mod
from tests.unit.harness._blackboard_env import BUILD_ID, T0, BbEnv, add_running_task, task_spec
from tests.unit.harness._blackboard_env import args as bb_args

from herness.core import config as c
from herness.core import time as clock
from herness.core.errors import ConfigError, ToolInputError
from herness.core.ids import IdKind, new_id
from herness.core.resilience import ProcessState
from herness.core.types import Finding, TaskSpec, ToolContext
from herness.harness._tools_schema import check_tool_schema, schema_hint
from herness.harness.blackboard import Blackboard
from herness.harness.findings import EntityCatalog
from herness.harness.roles.analyst import analyst_role
from herness.harness.swarm import tools as st
from herness.harness.swarm.spawn import SpawnBroker, SpawnDecision
from herness.harness.tools import ToolRegistry, dispatch
from herness.store.ops import run_write

pytestmark = pytest.mark.unit

bb_env = env_mod.bb_env  # fixtures
test_redactor = env_mod.test_redactor
QID = "q_" + "a" * 16
_NULL_FILTER: dict[str, Any] = dict.fromkeys(st.LIST_FINDINGS_SCHEMA["properties"])  # type: ignore[arg-type]


def args(qid: str, **overrides: object) -> dict[str, object]:
    """Valid strict `post_finding` arguments: every NumberRef field present (`format` null)."""
    values = bb_args(qid, **overrides)
    values["numbers"] = [{**n, "format": None} for n in values["numbers"]]  # type: ignore[attr-defined]
    return values


class FakeBroker:
    """Records requests; answers with a fixed decision."""

    def __init__(self, decision: SpawnDecision) -> None:
        self.decision = decision
        self.calls: list[tuple[TaskSpec, dict[str, object]]] = []

    async def request(self, parent: TaskSpec, a: Mapping[str, object]) -> SpawnDecision:
        self.calls.append((parent, dict(a)))
        return self.decision


def _broker(decision: SpawnDecision | None = None) -> SpawnBroker:
    return cast("SpawnBroker", FakeBroker(decision or SpawnDecision(True, None, "task_x")))


def _tools(env: BbEnv) -> list[Any]:
    async def escalate(question: str, reason: str) -> tuple[str, str]:
        return "run_1", "job_1"

    return [
        st.PostFindingTool(env.bb),
        st.ListFindingsTool(bb=env.bb, run_id=env.run_id, past_reader=None),
        st.RequestSubtaskTool(_broker(), task_spec(env.run_id)),
        st.EscalateTool(escalate),
    ]


def _walk(schema: object) -> Iterator[dict[str, Any]]:
    if isinstance(schema, dict):
        yield schema
        for value in schema.values():
            yield from _walk(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from _walk(item)


def _finding(n: int, claim: str, run_id: str = "run_" + "0" * 26) -> Finding:
    return Finding.model_validate(
        {
            "finding_id": new_id(IdKind.FINDING),
            "run_id": run_id,
            "task_id": new_id(IdKind.TASK),
            "author_role": "analyst",
            "claim": claim,
            "entity_type": "team",
            "entity_id": f"t{n}",
            "numbers": [
                {
                    "id": "n1",
                    "value": n,
                    "unit": "count",
                    "query_id": QID,
                    "column": "c",
                    "row_key": None,
                },
            ],
            "query_ids": [QID],
            "confidence": 0.456,
            "created_at": datetime(2026, 9, 26, tzinfo=UTC),
        }
    )


# --- UT06-43 strict schemas ------------------------------------------------------------------


def test_ut06_43_schemas_are_strict(bb_env: BbEnv) -> None:
    """UT06-43 every object level: additionalProperties false, all properties required."""
    for tool in _tools(bb_env):
        check_tool_schema(tool)  # the real registry rule (R-26)
        objects = [s for s in _walk(tool.input_schema) if s.get("type") == "object"]
        assert objects
        for obj in objects:
            assert obj["additionalProperties"] is False
            assert set(obj["required"]) == set(obj["properties"])
        assert not {"run_id", "session_id", "task_id", "message_id"} & set(
            tool.input_schema["properties"]
        )


def test_ut06_43_list_findings_filters_nullable_and_no_run_id() -> None:
    """UT06-43 list_findings: every FindingFilter field but run_id, each nullable."""
    props = st.LIST_FINDINGS_SCHEMA["properties"]
    assert isinstance(props, dict)
    assert set(props) == {
        "status", "entity_type", "entity_ids", "task_ids", "author_roles", "min_confidence",
        "include_superseded", "limit",
    }  # fmt: skip
    for prop in props.values():
        assert isinstance(prop, dict)
        assert "null" in cast("list[str]", prop["type"])
    assert schema_hint(st.LIST_FINDINGS_SCHEMA, _NULL_FILTER) is None
    assert schema_hint(st.LIST_FINDINGS_SCHEMA, {**_NULL_FILTER, "limit": 501}) is not None


def test_ut06_43_row_key_pairs_valid_and_open_object_rejected() -> None:
    """UT06-43 row_key is a nullable list of {column, value} pairs (strict fallback)."""
    good = args(QID)
    assert schema_hint(st.POST_FINDING_SCHEMA, good) is None
    pairs = [{"column": "team_id", "value": "t1"}]
    good["numbers"] = [{**good["numbers"][0], "row_key": pairs}]  # type: ignore[index]
    assert schema_hint(st.POST_FINDING_SCHEMA, good) is None
    good["numbers"] = [{**good["numbers"][0], "row_key": {"team_id": "t1"}}]  # type: ignore[index]
    assert schema_hint(st.POST_FINDING_SCHEMA, good) is not None


def test_ut06_43_resolve_through_real_registry(bb_env: BbEnv) -> None:
    """UT06-43 spec 05 resolve accepts the bound task tools for the analyst role."""
    spec = task_spec(bb_env.run_id).model_copy(
        update={"tools": ["list_findings", "post_finding", "request_subtask"]}
    )
    task_tools = st.build_task_tools(spec, bb=bb_env.bb, broker=_broker())
    resolved = ToolRegistry().resolve(analyst_role("general"), spec.tools, task_tools)
    assert [t.name for t in resolved] == ["list_findings", "post_finding", "request_subtask"]


# --- UT06-44 post_finding and list_findings ----------------------------------------------


def test_ut06_44_post_finding_result_and_row_key_pairs(bb_env: BbEnv) -> None:
    """UT06-44 post: content, data, ids; row_key pairs become the stored object."""
    a = args(bb_env.ops_qid)
    a["numbers"] = [{**a["numbers"][0], "row_key": [{"column": "team", "value": "t1"}]}]  # type: ignore[index]
    result = st.PostFindingTool(bb_env.bb)(bb_env.ctx(), **a)  # type: ignore[arg-type]
    fid = result.finding_ids[0]
    assert result.ok
    assert result.content == f"finding_id={fid} status=proposed"
    assert result.data == {"finding_id": fid}
    assert result.query_ids == [bb_env.ops_qid]
    found = asyncio.run(
        st.ListFindingsTool(bb=bb_env.bb, run_id=bb_env.run_id, past_reader=None)(
            bb_env.ctx(), **_NULL_FILTER
        )
    )
    assert found.finding_ids == [fid]
    assert found.content == (
        f"{fid} | proposed | team:t1 | 0.80 | Team t1 had [[n1]] incidents\n"
        f"numbers: n1=7 count ({bb_env.ops_qid}, incidents)"
    )
    assert found.query_ids == [bb_env.ops_qid]
    assert not found.truncated


def test_ut06_44_post_finding_rejections_raise(bb_env: BbEnv) -> None:
    """UT06-44 a rejected post and a duplicate row_key column raise ToolInputError."""
    tool = st.PostFindingTool(bb_env.bb)
    with pytest.raises(ToolInputError):
        tool(bb_env.ctx(), **args(bb_env.ops_qid, claim="Team t1 had 7 incidents"))  # type: ignore[arg-type]
    a = args(bb_env.ops_qid)
    dup = [{"column": "k", "value": 1}, {"column": "k", "value": 2}]
    a["numbers"] = [{**a["numbers"][0], "row_key": dup}]  # type: ignore[index]
    with pytest.raises(ToolInputError, match="duplicate row_key column"):
        tool(bb_env.ctx(), **a)  # type: ignore[arg-type]


def test_ut06_44_row_key_conversion_leaves_other_shapes() -> None:
    """UT06-44 only a list of {column, value} pairs is converted."""
    assert st._row_key(None) is None
    assert st._row_key([1]) == [1]
    assert st._numbers("x") == "x"
    assert st._numbers([{"id": "n1"}, 3]) == [{"id": "n1"}, 3]


def test_ut06_44_list_findings_filters_and_empty(bb_env: BbEnv) -> None:
    """UT06-44 filters reach the Blackboard; nothing found -> 'no findings'."""
    tool = st.ListFindingsTool(bb=bb_env.bb, run_id=bb_env.run_id, past_reader=None)
    st.PostFindingTool(bb_env.bb)(bb_env.ctx(), **args(bb_env.ops_qid))  # type: ignore[arg-type]
    none = asyncio.run(tool(bb_env.ctx(), **{**_NULL_FILTER, "status": ["verified"]}))
    assert none.content == "no findings"
    assert none.finding_ids == []
    some = asyncio.run(tool(bb_env.ctx(), **{**_NULL_FILTER, "entity_ids": ["t1"], "limit": 5}))
    assert len(some.finding_ids) == 1
    with pytest.raises(ToolInputError, match="invalid list_findings arguments"):
        asyncio.run(tool(bb_env.ctx(), **{**_NULL_FILTER, "limit": 0}))


def test_ut06_44_truncation_before_12000_chars() -> None:
    """UT06-44 findings stop before 12,000 chars; truncated set; ids list what was shown."""
    findings = [_finding(i, f"claim {i} [[n1]] " + "w" * 1_400) for i in range(12)]
    result = st.render_findings(findings)
    assert result.truncated
    assert len(result.content) <= 12_000
    shown = len(result.finding_ids)
    assert 0 < shown < 12
    assert result.finding_ids == [f.finding_id for f in findings[:shown]]
    assert result.content.endswith(f"truncated: {shown} of 12 findings shown")
    assert result.content.count("\nnumbers: ") == shown
    first = result.content.splitlines()[0]
    assert first.startswith(f"{findings[0].finding_id} | proposed | team:t0 | 0.46 | claim 0")


def test_ut06_44_claim_rendered_on_one_line() -> None:
    """UT06-44 one line per finding even when the claim holds line breaks."""
    result = st.render_findings([_finding(1, "a\r\nb\nc\rd [[n1]]")])
    assert result.content.splitlines()[0].endswith("| a b c d [[n1]]")


def test_ut06_44_chat_mode_reads_past_findings() -> None:
    """UT06-44 chat mode: past_reader(entity_type, entity_ids, limit), same format."""
    seen: list[dict[str, object]] = []

    def reader(**kw: object) -> list[Finding]:
        seen.append(kw)
        return [_finding(1, "past [[n1]]")]

    tool = st.ListFindingsTool(bb=None, run_id=None, past_reader=reader)
    ctx = cast("ToolContext", object())
    out = asyncio.run(tool(ctx, **{**_NULL_FILTER, "entity_type": "team", "entity_ids": ["t1"]}))
    assert seen == [{"entity_type": "team", "entity_ids": ["t1"], "limit": 500}]
    assert out.query_ids == [QID]
    assert len(out.finding_ids) == 1
    asyncio.run(tool(ctx, **{**_NULL_FILTER, "limit": 3}))
    assert seen[-1] == {"entity_type": None, "entity_ids": None, "limit": 3}
    with pytest.raises(ConfigError, match="no backend"):
        st.ListFindingsTool(bb=None, run_id=None, past_reader=None)


# --- UT06-45 request_subtask, escalate, build_task_tools ----------------------------------


def test_ut06_45_request_subtask_json_results() -> None:
    """UT06-45 approved and denied decisions are ok results with JSON content."""
    parent = task_spec("run_" + "1" * 26)
    fake = FakeBroker(SpawnDecision(True, None, "task_abc"))
    tool = st.RequestSubtaskTool(cast("SpawnBroker", fake), parent)
    req = {"objective": "o", "specialty": "ops", "entity_type": "team", "entity_ids": ["t1"],
           "reason": "r"}  # fmt: skip
    ctx = cast("ToolContext", object())
    out = asyncio.run(tool(ctx, **req))  # type: ignore[arg-type]
    assert out.ok
    assert json.loads(out.content) == {"approved": True, "task_id": "task_abc"}
    assert fake.calls == [(parent, req)]
    fake.decision = SpawnDecision(False, "max_depth", None)
    out = asyncio.run(tool(ctx, **req))  # type: ignore[arg-type]
    assert out.ok
    assert json.loads(out.content) == {"approved": False, "reason": "max_depth"}
    assert out.data == {"approved": False, "reason": "max_depth"}


def test_ut06_45_escalate_json_and_argument_check() -> None:
    """UT06-45 escalate returns {run_id, job_id}; other arguments raise ToolInputError."""
    calls: list[tuple[str, str]] = []

    async def escalate(question: str, reason: str) -> tuple[str, str]:
        calls.append((question, reason))
        return "run_9", "job_9"

    tool = st.EscalateTool(escalate)
    ctx = cast("ToolContext", object())
    out = asyncio.run(tool(ctx, question="which team?", reason="ranking"))
    assert out.ok
    assert json.loads(out.content) == {"run_id": "run_9", "job_id": "job_9"}
    assert calls == [("which team?", "ranking")]
    with pytest.raises(ToolInputError):
        asyncio.run(tool(ctx, question="q", reason="r", session_id="s_other"))
    with pytest.raises(ToolInputError):
        asyncio.run(tool(ctx, question=1, reason="r"))


def test_ut06_45_build_task_tools(bb_env: BbEnv) -> None:
    """UT06-45 only the swarm tools of spec.tools are built; chat gets the chat-mode reader."""
    spec = task_spec(bb_env.run_id).model_copy(
        update={"tools": ["run_sql", "post_finding", "list_findings", "request_subtask"]}
    )
    built = st.build_task_tools(spec, bb=bb_env.bb, broker=_broker())
    assert sorted(built) == ["list_findings", "post_finding", "request_subtask"]
    assert isinstance(built["request_subtask"], st.RequestSubtaskTool)
    chat = spec.model_copy(update={"role": "chat", "tools": ["list_findings", "escalate"]})

    async def escalate(question: str, reason: str) -> tuple[str, str]:
        return "r", "j"

    built = st.build_task_tools(chat, bb=None, broker=None, escalate=escalate, past_reader=list)
    reader = built["list_findings"]
    assert isinstance(reader, st.ListFindingsTool)
    assert reader._bb is None
    assert isinstance(built["escalate"], st.EscalateTool)


@pytest.mark.parametrize(("tools", "role", "name"), [
    (["post_finding"], "analyst", "post_finding"),
    (["list_findings"], "analyst", "list_findings"),
    (["list_findings"], "chat", "list_findings"),
    (["request_subtask"], "analyst", "request_subtask"),
    (["escalate"], "chat", "escalate"),
])  # fmt: skip
def test_ut06_45_build_without_backend_is_config_error(
    tools: list[str], role: str, name: str
) -> None:
    """UT06-45 a tool without its backend -> ConfigError('tool <name> has no backend')."""
    spec = task_spec("run_" + "2" * 26).model_copy(update={"tools": tools, "role": role})
    with pytest.raises(ConfigError, match=f"tool {name} has no backend"):
        st.build_task_tools(spec, bb=None, broker=None)


# --- ST06-17 ids come from the binding -----------------------------------------------------


@pytest.mark.parametrize("field", ["run_id", "session_id"])
def test_st06_17_schema_rejects_id_arguments(bb_env: BbEnv, field: str) -> None:
    """ST06-17 a model-supplied run_id/session_id fails every swarm tool schema."""
    valid: dict[str, dict[str, object]] = {
        "post_finding": args(bb_env.ops_qid),
        "list_findings": dict(_NULL_FILTER),
        "request_subtask": {
            "objective": "o",
            "specialty": "ops",
            "entity_type": "team",
            "entity_ids": ["t1"],
            "reason": "r",
        },
        "escalate": {"question": "q", "reason": "r"},
    }
    for tool in _tools(bb_env):
        good = valid[tool.name]
        assert schema_hint(tool.input_schema, good) is None
        hint = schema_hint(tool.input_schema, {**good, field: "run_" + "9" * 26})
        assert hint is not None
        assert "additionalProperties" in hint


@pytest.fixture
def cfg(tmp_path: Path, reset_process_state: ProcessState) -> Iterator[None]:
    del reset_process_state
    use_test_config(tmp_path)
    yield
    c.reset_config()


def _post_in_second_run(env: BbEnv) -> tuple[str, str]:
    """A second running review run with one posted finding: (run_id, finding_id)."""
    run_id = new_id(IdKind.RUN)
    sql = (
        "INSERT INTO run (run_id, kind, depth, profile, config_hash, build_id, status,"
        " started_at) VALUES (?, 'org_review', 'standard', 'default', 'h', ?, 'running', ?)"
    )
    run_write(
        lambda conn: conn.execute(sql, (run_id, BUILD_ID, clock.format_utc(T0))), op="test_run"
    )
    task_id = add_running_task(run_id, n=9)
    bb = Blackboard(
        run_id, build_id=BUILD_ID, catalog=EntityCatalog(env.warehouse), allowed_numerals=()
    )
    env.extra.append(bb)
    ctx = env.ctx(task_id=task_id).model_copy(update={"run_id": run_id})
    return run_id, st.PostFindingTool(bb)(ctx, **args(env.ops_qid)).finding_ids[0]  # type: ignore[arg-type]


def test_st06_17_dispatch_rejects_and_binding_is_used(cfg: None, bb_env: BbEnv) -> None:
    """ST06-17 dispatch refuses a run_id argument; the tool reads its bound run only."""
    del cfg
    fid = st.PostFindingTool(bb_env.bb)(bb_env.ctx(), **args(bb_env.ops_qid)).finding_ids[0]  # type: ignore[arg-type]
    other_run, other_fid = _post_in_second_run(bb_env)
    assert other_fid != fid
    tool = st.ListFindingsTool(bb=bb_env.bb, run_id=bb_env.run_id, past_reader=None)
    ctx = bb_env.ctx()
    other = other_run
    bad = call("list_findings", **{**_NULL_FILTER, "run_id": other})
    [refused] = asyncio.run(dispatch(ctx, {"list_findings": tool}, [bad], None, make_state()))
    assert not refused.ok
    assert refused.error is not None
    assert refused.error.type == "ToolInputError"
    good = call("list_findings", "c2", **_NULL_FILTER)
    [listed] = asyncio.run(dispatch(ctx, {"list_findings": tool}, [good], None, make_state()))
    assert listed.ok
    assert listed.finding_ids == [fid]  # the second run's finding is not listed
    assert other_fid not in listed.content
    ctx_other = ctx.model_copy(update={"run_id": other_run})  # even from the other run's context
    assert asyncio.run(tool(ctx_other, **_NULL_FILTER)).finding_ids == [fid]
    with pytest.raises(ToolInputError, match="unexpected list_findings arguments"):
        asyncio.run(tool(ctx, **{**_NULL_FILTER, "run_id": other}))
