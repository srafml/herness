"""UT06-82 and the Writer-step helpers (impl 06 T06-19): draft path, atomic write, pass-file
resume, the off-network task input with checkpointed pseudonyms, and the fix-pass input."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support.report_drafts import RUN_ID, make_draft
from tests.unit.harness import _blackboard_env as env_mod
from tests.unit.harness.swarm import _swarm_drafts as d
from tests.unit.harness.swarm._swarm_findings import finding

from herness.core.errors import ConfigError, FatalError
from herness.core.types import EntityScope, TaskBudget, TaskSpec
from herness.harness.llm.settings import ClientConfig, PricePerMTok
from herness.harness.pipelines.settings import PipelinesConfig
from herness.harness.swarm import _writer_input as wi_mod
from herness.harness.swarm import hybrid, writer
from herness.harness.swarm.draft_rules import DraftExtras

pytestmark = pytest.mark.unit

test_redactor = env_mod.test_redactor  # fixture
_ZERO = Decimal(0)
CFG = ClientConfig.model_validate(
    {
        "name": "claude-writer", "kind": "openai_compat", "base_url": "http://127.0.0.1:8000/v1",
        "model": "stand-in", "context_window": 200_000, "max_output_tokens": 4096,
        "tokenizer": "estimate", "max_concurrency": 1,
        "price_per_mtok": PricePerMTok(input=_ZERO, output=_ZERO, cache_read=_ZERO,
                                       cache_write=_ZERO),
    }
)  # fmt: skip


# --- UT06-82


def test_ut06_82_draft_path_valid_and_invalid_ids(tmp_path: Path) -> None:
    """UT06-82 a valid run id gives `<root>/<run_id>/draft.json`; `run_../x` → ConfigError."""
    assert writer.draft_path(RUN_ID, tmp_path) == tmp_path / RUN_ID / "draft.json"
    for bad in ("run_../x", "run_x", "", f"{RUN_ID}/..", "../" + RUN_ID):
        with pytest.raises(ConfigError, match="invalid run_id"):
            writer.draft_path(bad, tmp_path)


def test_ut06_82_containment_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT06-82 a run directory resolving outside the root (a link) → ConfigError."""
    outside = tmp_path / "outside"
    root = tmp_path / "root"
    root.mkdir()
    real_resolve = Path.resolve

    def resolve(self: Path, strict: bool = False) -> Path:
        return outside if self == root / RUN_ID else real_resolve(self, strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ConfigError, match="escapes"):
        writer.draft_path(RUN_ID, root)


def test_ut06_82_atomic_replace_leaves_no_temp_file(tmp_path: Path) -> None:
    """UT06-82 the write creates the directory, replaces an existing file atomically and
    leaves no temp file; the content is the draft's JSON."""
    path = writer.draft_path(RUN_ID, tmp_path)
    first, second = make_draft(), make_draft(title="Second title")
    writer.write_draft_atomic(path, first)
    writer.write_draft_atomic(path, second)
    assert os.listdir(path.parent) == ["draft.json"]
    assert path.read_text(encoding="utf-8") == second.model_dump_json(indent=2)


def test_ut06_82_failed_write_removes_temp_and_keeps_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT06-82 a failing replace removes the temp file and keeps the previous draft."""
    path = writer.draft_path(RUN_ID, tmp_path)
    writer.write_draft_atomic(path, make_draft())
    before = path.read_bytes()

    def boom(self: Path, target: Path) -> Path:
        msg = "disk gone"
        raise OSError(msg)

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="disk gone"):
        writer.write_draft_atomic(path, make_draft(title="Other"))
    assert os.listdir(path.parent) == ["draft.json"]
    assert path.read_bytes() == before


def test_ut06_82_last_pass_highest_k(tmp_path: Path) -> None:
    """UT06-82 resume loads the pass file with the highest `k`; none without a directory."""
    path = writer.draft_path(RUN_ID, tmp_path)
    assert writer._last_pass(path) is None
    path.parent.mkdir()
    (path.parent / "draft.passX.json").write_text("{}", encoding="utf-8")
    assert writer._last_pass(path) is None
    for k in (0, 2, 10):
        writer.write_draft_atomic(path.with_name(f"draft.pass{k}.json"), make_draft(title=f"P{k}"))
    found = writer._last_pass(path)
    assert found is not None
    assert (found[0], found[1].title) == (10, "P10")


# --- fix-pass and task input


def test_ut06_82_writer_output_drops_rank_and_rec_id() -> None:
    """The fix pass `previous` holds only Writer fields, recommendations without rank/rec_id."""
    out = wi_mod.writer_output(make_draft())
    assert set(out) == {"title", "sections", "recommendations", "caveats",
                        "prior_outcomes_commentary"}  # fmt: skip
    rec = out["recommendations"][0]  # type: ignore[index]
    assert "rank" not in rec
    assert "rec_id" not in rec


def test_ut06_82_fix_input_local_and_off_network(
    test_redactor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locally the fix payload is wrapped (U06-141); off-network every string is pseudonymized
    and redacted; a string the redactor refuses fails the task closed."""
    payload: dict[str, object] = {"previous": {"text": "Team team-1 is slow"},
                                  "rule_violations": [{"where": "title"}]}  # fmt: skip
    assert wi_mod.fix_input(None, "task_x", payload) == payload
    pseudo = hybrid.Pseudonymizer([("team", "team-1", "Payments")])
    sent = wi_mod.fix_input(pseudo, "task_x", payload)
    assert sent["previous"] == {"text": "Team team_001 is slow"}
    monkeypatch.setattr(hybrid, "redact_text", lambda text: None)
    with pytest.raises(FatalError, match="redaction failed"):
        wi_mod.fix_input(pseudo, "task_x", payload)


class _Bb:
    async def run_on_writer(self, fn: Callable[[], Any]) -> Any:
        return fn()


def _spec() -> TaskSpec:
    scope = EntityScope(entity_type="run", entity_ids=[d.RUN_ID])
    return TaskSpec(
        task_id="task_" + "1" * 26, run_id=d.RUN_ID, role="writer",
        objective="Write the funding_review report.", scope=scope, tools=[],
        budget=TaskBudget(max_steps=5, max_tokens=5_000, wall_clock_s=60), model_role="writer",
        dedup_key="0123456789abcdef",
    )  # fmt: skip


def _wi(selected: list[Any]) -> wi_mod.WriterInput:
    return wi_mod.WriterInput(
        d.plan_ctx(), [], selected, selected, [], {"findings": [], "levers": []}, DraftExtras()
    )


def test_ut06_82_off_network_pack_and_checkpointed_pseudonyms(
    test_redactor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off-network: the evidence pack with entity names pseudonymized; the mapping is saved as
    `SwarmTaskState(phase="writing")` before the first call; local: the wrapped input."""
    saved: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(wi_mod, "save_checkpoint", lambda *a: saved.append(a))
    names = {"c1": "Cache Rewrite"}
    catalog = SimpleNamespace(names=lambda kind, ids: {i: names[i] for i in ids if i in names})
    env = SimpleNamespace(catalog=catalog, cfg=PipelinesConfig(), bb=_Bb())
    f = finding(1, entity=("candidate", "c1"), claim="Cache Rewrite had [[n1]] incidents")
    route = SimpleNamespace(off_network=True, config=CFG)
    pack, pseudo = asyncio.run(wi_mod.task_input_for(env, _spec(), route, _wi([f])))  # type: ignore[arg-type]
    assert pseudo is not None
    assert pack["findings"][0]["claim"] == "candidate_001 had [[n1]] incidents"  # type: ignore[index]
    [(task_id, key, state)] = saved
    assert (task_id, key, state["phase"]) == (_spec().task_id, "state", "writing")
    assert state["pseudonyms"]["candidate_001"]["entity_id"] == "c1"
    local, none = asyncio.run(
        wi_mod.task_input_for(env, _spec(), SimpleNamespace(off_network=False), _wi([f]))  # type: ignore[arg-type]
    )
    assert none is None
    assert local == {"findings": [], "levers": []}


def test_ut06_82_off_network_pack_failure_is_fatal(
    test_redactor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pack that cannot be built fails the Writer task closed (`FatalError`), nothing saved."""
    saved: list[Any] = []
    monkeypatch.setattr(wi_mod, "save_checkpoint", lambda *a: saved.append(a))

    def broken(*args: Any, **kw: Any) -> dict[str, object]:
        msg = "too big"
        raise ValueError(msg)

    monkeypatch.setattr(hybrid, "build_evidence_pack", broken)
    env = SimpleNamespace(catalog=SimpleNamespace(names=lambda k, i: {}), cfg=PipelinesConfig(),
                          bb=_Bb())  # fmt: skip
    route = SimpleNamespace(off_network=True, config=CFG)
    with pytest.raises(FatalError, match="evidence pack unavailable"):
        asyncio.run(wi_mod.task_input_for(env, _spec(), route, _wi([finding(1)])))  # type: ignore[arg-type]
    assert saved == []
