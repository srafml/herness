"""UT03-139 (stage bodies): F03-01 step functions of `run_enrichment` with fake collaborators.

The heavy stage units (embed, decide, cluster, resolve ...) have their own tests; here they
are replaced by recorders in the `_pipeline_stages` / `pipeline` namespaces so the
composition rules are checked: config errors before work, Laya degraded mode, the OpenJev
service lifecycle, deferred work, the too-few-vectors policy, deep-mode members and the
table rebuilds a rerun needs. Card T03-28.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import duckdb
import pyarrow as pa
import pytest
from tests.support.build_harness import FakeJobContext

from herness.core.config import HernessConfig
from herness.core.errors import (
    AuthError,
    CircuitOpen,
    ConfigError,
    ModelUnavailable,
    SchemaViolation,
    StoreBusy,
)
from herness.core.types import Answer, DecisionInput, DecisionOutput, QuestionSet
from herness.enrich import _pipeline_stages as steps
from herness.enrich import cache_maint, pipeline
from herness.enrich.cache import CACHE_SCHEMA, DecisionCache, write_part
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.cluster_describe import NameResult, NamingCandidate
from herness.enrich.cluster_stage import ClusterStageResult
from herness.enrich.deciders.laya import LayaDecider
from herness.enrich.deciders.llm import LlmDecider
from herness.enrich.gpu import YieldRequested
from herness.enrich.labels import LabelStore
from herness.enrich.layout import EnrichPaths
from herness.enrich.pipeline import Run, StageReport
from herness.enrich.questions import load_question_set, question_fingerprint
from herness.enrich.resolve import QueueItem
from herness.enrich.settings import DecidersSettings, DecisionsConfig

pytestmark = pytest.mark.unit

SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
BUILD = "20261002-010000-01ABCD"
QSV = "qs-2026-10-01.1"
NOW = datetime(2026, 10, 2, 1, tzinfo=UTC)
_Q = {"id": "q_bool", "type": "bool", "applies_to": ["incident"], "threshold": 0.8,
      "instructions": "The ticket describes an outage of a business service."}  # fmt: skip


def _decisions(**kw: Any) -> DecisionsConfig:
    base = {"question_set_version": QSV, "questions": [_Q], "change_link": {"use_decider": False}}
    return DecisionsConfig.model_validate(base | kw)


def _cfg(tmp_path: Path, *, decisions: DecisionsConfig | None = None, **deciders: Any) -> Any:
    settings = DecidersSettings.model_validate(
        {"openjev": {"enabled": False}} | {k: dict(v) for k, v in deciders.items()}
    )
    return SimpleNamespace(
        decisions=decisions or _decisions(),
        models=SimpleNamespace(deciders=settings),
        paths=SimpleNamespace(data=tmp_path),
    )


def _warehouse() -> duckdb.DuckDBPyConnection:
    wh = duckdb.connect()
    wh.execute(SETTINGS_SQL.read_text("utf-8"))
    wh.execute("CREATE TABLE core.incident (record_id VARCHAR, opened_at TIMESTAMPTZ)")
    return wh


def _run(tmp_path: Path, *, depth: str = "standard", cfg: Any = None, **kw: Any) -> Run:
    ctx = kw.pop("ctx", None) or FakeJobContext()
    run = Run(_warehouse(), BUILD, depth, ctx, kw.pop("prev", None), kw.pop("llm_factory", None),  # type: ignore[arg-type]
              False, NOW)  # fmt: skip
    run.cfg = cast("HernessConfig", cfg or _cfg(tmp_path))
    run.paths = EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="c")
    run.qs = load_question_set(run.cfg.decisions)
    run.cache, run.labels = DecisionCache(run.paths, QSV), LabelStore(run.paths, QSV)
    run.calibration = CalibrationStore(run.paths)
    return run


def _item(n: int, qids: tuple[str, ...] = ("q_bool",)) -> QueueItem:
    return QueueItem(f"servicenow:incident:{n}", "incident", f"{n:032x}", f"text {n}", qids)


class Decider:
    """A decider recording its calls; answers `true` with p 0.9, or raises `fail`."""

    def __init__(self, name: str, version: str = "v1", fail: Exception | None = None) -> None:
        self.name, self.version, self.fail = name, version, fail
        self.calls: list[list[str]] = []

    def decide(self, items: Sequence[DecisionInput], qs: QuestionSet) -> list[DecisionOutput]:
        self.calls.append([i.record_id for i in items])
        if self.fail is not None:
            raise self.fail
        answer = Answer.model_validate({"answer": "true", "probability": 0.9,
                                        "distribution": {"true": 0.9, "false": 0.1}})  # fmt: skip
        return [DecisionOutput.model_validate({"record_id": i.record_id, "content_hash":
                 i.content_hash, "decider": self.name, "decider_version": self.version,
                 "answers": dict.fromkeys(i.question_ids or (), answer)})
                for i in items]  # fmt: skip

    def health(self) -> None:
        return None


def _calls(monkeypatch: pytest.MonkeyPatch, *names: str) -> dict[str, list[dict[str, Any]]]:
    """Replace `_pipeline_stages.<name>` by recorders of their keyword arguments."""
    seen: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
    for name in names:

        def record(*args: Any, _name: str = name, **kw: Any) -> Any:
            seen[_name].append({"args": args, **kw})
            empty = {"run_decide_escalate", "ensemble_band", "pair_inputs", "escalation_queue"}
            return [] if _name in empty else None

        monkeypatch.setattr(steps, name, record)
    return seen


# --- prepare (steps 1-2) ----------------------------------------------------------------


def test_ut03_139_prepare_refuses_a_disabled_decider_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 1: an `error` issue of `check_decider_refs` is a ConfigError, first."""
    cfg = _cfg(tmp_path, decisions=_decisions(escalation_chain=["jev", "llm"]))
    monkeypatch.setattr(steps, "get_config", lambda: cfg)
    run = Run(_warehouse(), BUILD, "standard", FakeJobContext(), None, None, False, NOW)
    with pytest.raises(ConfigError, match="disabled decider"):
        steps.prepare(run)


def test_ut03_139_prepare_laya_degraded_teacher_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 2 / FT03-04: no Laya CURRENT -> `laya_degraded`, teacher-primary."""
    cfg = _cfg(tmp_path, openjev={"enabled": False})
    monkeypatch.setattr(steps, "get_config", lambda: cfg)
    run = Run(_warehouse(), BUILD, "standard", FakeJobContext(), None, None, False, NOW)
    steps.prepare(run)
    assert run.warnings == ["laya_degraded"]
    assert (run.laya, run.primaries, run.versions) == (None, {"q_bool": "llm"}, {})
    assert run.qs.version == QSV
    assert (tmp_path / "cache" / "decisions" / QSV / "questions.json").is_file()


def test_ut03_139_prepare_builds_laya_teachers_and_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 2: an accepted Laya, OpenJev and the LLM from `llm_factory`: versions."""
    cfg = _cfg(tmp_path, openjev={"enabled": True}, jev={"enabled": True})
    monkeypatch.setattr(steps, "get_config", lambda: cfg)
    built: list[str] = []

    def build(name: str, **kw: Any) -> Any:
        built.append(name)
        if name == "jev":
            msg = "decider jev has no api key"
            raise AuthError(msg)
        if name == "llm":
            assert kw["llm"] == ("client", "llm-v1", 4)
            return SimpleNamespace(name="llm", version="llm-v1")
        if name == "laya":
            assert kw["embed_fn"] is None  # no wide choice question
            return LayaDecider.__new__(LayaDecider)
        return SimpleNamespace(name=name, version=f"{name}-v1")

    def laya_version(self: Any) -> str:
        return "laya-20260901-1"

    monkeypatch.setattr(LayaDecider, "version", property(laya_version), raising=False)
    monkeypatch.setattr(steps, "build_decider", build)
    manifest = SimpleNamespace(accepted_questions=["q_bool"])
    monkeypatch.setattr(steps, "verify_model_dir", lambda *a, **k: manifest)
    factory_calls: list[str] = []

    def factory(role: str) -> Any:
        factory_calls.append(role)
        return ("client", "llm-v1", 4)

    run = Run(_warehouse(), BUILD, "standard", FakeJobContext(), None, factory, False, NOW)
    steps.prepare(run)
    assert built == ["laya", "openjev", "jev", "llm"]
    assert run.primaries == {"q_bool": "laya"}
    assert run.versions == {"laya": "laya-20260901-1", "openjev": "openjev-v1", "llm": "llm-v1"}
    assert run.warnings == ["jev_unavailable"]
    assert factory_calls == ["enrich_decider"]


def test_ut03_139_prepare_llm_factory_unavailable_leaves_llm_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 2: `llm_factory` raising ModelUnavailable/CircuitOpen: no `llm` decider."""
    monkeypatch.setattr(steps, "get_config", lambda: _cfg(tmp_path))
    for exc in (ModelUnavailable("llm down"), CircuitOpen("open", key="model:x", retry_at=NOW)):

        def factory(role: str, _exc: Exception = exc) -> Any:
            raise _exc

        run = Run(_warehouse(), BUILD, "fast", FakeJobContext(), None, factory, False, NOW)
        steps.prepare(run)
        assert "llm" not in run.deciders
        assert "llm" not in run.versions


def test_ut03_139_prepare_wide_choice_gets_cpu_embed_fn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 OI-06 fallback: a >20-option question gives Laya a CPU `embed_fn`."""
    options = {f"opt_{i:02d}": f"option {i}" for i in range(21)}
    wide = {"id": "q_wide", "type": "choice", "applies_to": ["incident"], "threshold": 0.6,
            "instructions": "Which option fits this ticket best?", "options": options}  # fmt: skip
    run = _run(tmp_path, cfg=_cfg(tmp_path, decisions=_decisions(questions=[_Q, wide])))
    fn = steps._embed_fn(run)
    assert fn is not None
    loaded: list[str] = []
    encoder = SimpleNamespace(load=loaded.append)
    monkeypatch.setattr(steps, "get_encoder", lambda: encoder)
    monkeypatch.setattr(steps, "embed_texts", lambda enc, texts, batch_size: (enc, texts))
    assert fn(["a"]) == (encoder, ["a"])
    assert loaded == ["cpu"]


def _prev_build(path: Path, qsv: str | None) -> Path:
    con = duckdb.connect(str(path))
    con.execute(SETTINGS_SQL.read_text("utf-8"))
    if qsv is not None:
        con.execute("INSERT INTO enrich.decision (record_id, question, question_set_version) "
                    "VALUES ('r', 'q_bool', ?)", [qsv])  # fmt: skip
    con.close()
    return path


def test_ut03_139_migrate_on_question_set_change_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 F03-16: an older version in the previous build migrates cache and labels."""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(cache_maint, "migrate", lambda p, old, qs: seen.append(("c", old)))
    monkeypatch.setattr(LabelStore, "migrate_from", lambda s, old, qs: seen.append(("l", old)))
    for name, qsv in (("old", "qs-2026-09-01.1"), ("same", QSV), ("none", None)):
        run = _run(tmp_path, prev=_prev_build(tmp_path / f"{name}.duckdb", qsv))
        steps._migrate(run)
    assert seen == [("c", "qs-2026-09-01.1"), ("l", "qs-2026-09-01.1")]
    broken = tmp_path / "broken.duckdb"
    broken.write_bytes(b"not a duckdb file")
    steps._migrate(_run(tmp_path, prev=broken))  # unreadable previous build: no migration
    steps._migrate(_run(tmp_path, prev=tmp_path / "missing.duckdb"))
    run = _run(tmp_path, prev=_prev_build(tmp_path / "old2.duckdb", "qs-2026-09-01.1"))
    run.wh.execute("DROP SCHEMA enrich CASCADE")
    steps._migrate(run)  # the attach alias is free again afterwards
    assert len(seen) == 4
    assert run.wh.execute("SELECT count(*) FROM duckdb_databases() WHERE database_name = "
                          "'enrich_prev_qsv'").fetchone() == (0,)  # fmt: skip


# --- steps 3-8 ------------------------------------------------------------------------------


def test_ut03_139_text_rebuilds_the_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-139 a rerun of `text` first empties `enrich.text_redacted` (§4.1 rebuild)."""
    run = _run(tmp_path)
    run.wh.execute("INSERT INTO enrich.text_redacted VALUES ('r', 'incident', 't', 'h')")
    seen = _calls(monkeypatch, "build_text_redacted")
    steps.text(run, StageReport())
    assert run.wh.execute("SELECT count(*) FROM enrich.text_redacted").fetchone() == (0,)
    assert seen["build_text_redacted"][0]["prev_warehouse"] is None
    run.wh.execute("DROP TABLE enrich.text_redacted")
    with pytest.raises(SchemaViolation, match="enrichment pipeline: CatalogException"):
        steps.text(run, StageReport())


def test_ut03_139_embed_unloads_the_encoder_on_every_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 4: encoder loaded, stage, mapping vectors, unloaded even on a yield."""
    events: list[str] = []
    encoder = SimpleNamespace(load=lambda d: events.append(f"load:{d}"),
                              unload=lambda: events.append("unload"))  # fmt: skip
    monkeypatch.setattr(steps, "get_encoder", lambda: encoder)
    monkeypatch.setattr(steps, "device", lambda: "cpu")
    monkeypatch.setattr(steps, "run_embed_stage", lambda *a, **k: events.append("embed"))
    monkeypatch.setattr(steps, "prepare_mapping_vectors", lambda *a, **k: "vectors")
    run = _run(tmp_path)
    steps.embed(run, StageReport())
    assert events == ["load:cpu", "embed", "unload"]
    assert run.vectors == cast("Any", "vectors")

    def yields(*a: Any, **k: Any) -> None:
        stage = "embed"
        raise YieldRequested(stage)

    monkeypatch.setattr(steps, "run_embed_stage", yields)
    with pytest.raises(YieldRequested):
        steps.embed(run, StageReport())
    assert events[-1] == "unload"


def test_ut03_139_device_follows_cuda_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-139 the GPU stages run on CUDA when torch sees it, else on the CPU."""
    import torch  # noqa: PLC0415 - the test patches it

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert steps.device() == "cpu"
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    assert steps.device() == "cuda"


def test_ut03_139_decide_primary_passes_laya_and_primaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 5: `run_decide_primary` gets the run's Laya, primaries and cache."""
    seen = _calls(monkeypatch, "run_decide_primary")
    run = _run(tmp_path)
    run.primaries = {"q_bool": "laya"}
    steps.decide_primary(run, StageReport())
    call = seen["run_decide_primary"][0]
    assert (call["laya"], call["primaries"], call["cache"]) == (None, run.primaries, run.cache)


def _openjev_cfg(tmp_path: Path) -> Any:
    return _cfg(tmp_path, openjev={"enabled": True})


def test_ut03_139_escalate_starts_and_stops_openjev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 7: guard, release_cuda, start openjev, U03-86, stop (also on errors)."""
    seen = _calls(monkeypatch, "link_candidates", "pair_inputs", "run_decide_escalate",
                  "release_cuda", "guard", "frame", "escalation_queue")  # fmt: skip
    run = _run(tmp_path, cfg=_openjev_cfg(tmp_path))
    teacher = Decider("openjev")
    run.deciders["openjev"] = cast("Any", teacher)
    report = StageReport()
    steps.decide_escalate(run, report)
    ctx = cast("FakeJobContext", run.ctx)
    assert ctx.services.calls == [("start", "openjev"), ("stop", "openjev")]
    assert seen["guard"][0]["args"] == ("decider:openjev",)
    assert seen["release_cuda"]
    assert run.linked
    assert run.teacher_up
    assert seen["run_decide_escalate"][0]["teacher"] is teacher
    assert (report.status, run.band) == ("done", None)

    def boom(*a: Any, **k: Any) -> Any:
        msg = "escalation_queue: CatalogException"
        raise SchemaViolation(msg)

    monkeypatch.setattr(steps, "run_decide_escalate", boom)
    with pytest.raises(SchemaViolation):
        steps.decide_escalate(run, StageReport())
    assert ctx.services.calls[-1] == ("stop", "openjev")
    assert len(seen["link_candidates"]) == 1  # `link_cand` is built once per connection


def test_ut03_139_escalate_openjev_unavailable_defers_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 7: start `ModelUnavailable` or an open breaker -> degraded, no teacher."""
    seen = _calls(monkeypatch, "link_candidates", "pair_inputs", "run_decide_escalate",
                  "release_cuda")  # fmt: skip
    for exc in (ModelUnavailable("openjev start"), CircuitOpen("o", key="decider:openjev",
                                                                 retry_at=NOW)):  # fmt: skip
        run = _run(tmp_path, cfg=_openjev_cfg(tmp_path))
        run.deciders["openjev"] = cast("Any", Decider("openjev"))

        def guard(key: str, _exc: Exception = exc) -> None:
            if isinstance(_exc, CircuitOpen):
                raise _exc

        def start(name: str, *, timeout_s: float | None = None, _exc: Exception = exc) -> None:
            raise _exc

        monkeypatch.setattr(steps, "guard", guard)
        monkeypatch.setattr(cast("FakeJobContext", run.ctx).services, "start", start)
        report = StageReport()
        steps.decide_escalate(run, report)
        assert (report.status, report.note) == ("degraded", "openjev_unavailable")
        assert seen["run_decide_escalate"][-1]["teacher"] is None
        assert cast("FakeJobContext", run.ctx).services.calls == []  # never started: no stop
        assert not run.teacher_up


def test_ut03_139_escalate_llm_only_chain_has_no_teacher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 OpenJev disabled: no service call; U03-86 runs with no teacher (all deferred)."""
    seen = _calls(monkeypatch, "link_candidates", "pair_inputs", "run_decide_escalate")
    run = _run(tmp_path)
    report = StageReport()
    steps.decide_escalate(run, report)
    assert seen["run_decide_escalate"][0]["teacher"] is None
    assert cast("FakeJobContext", run.ctx).services.calls == []
    assert report.status == "done"  # U03-86 itself marks `teacher_unavailable` (stubbed here)


def test_ut03_139_escalate_deep_runs_openjev_band_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 deep: band after U03-86; OpenJev answers the band items it has no row for."""
    _calls(monkeypatch, "link_candidates", "pair_inputs", "run_decide_escalate", "release_cuda",
           "guard", "frame", "escalation_queue")  # fmt: skip
    band = [_item(1), _item(2)]
    monkeypatch.setattr(steps, "ensemble_band", lambda wh, cfg, qs: band)
    run = _run(tmp_path, depth="deep", cfg=_openjev_cfg(tmp_path))
    teacher = Decider("openjev")
    run.deciders["openjev"] = cast("Any", teacher)
    steps.decide_escalate(run, StageReport())
    assert run.band == band
    assert teacher.calls == [[band[0].record_id, band[1].record_id]]
    assert run.teacher_up
    assert run.cache.existing_keys("openjev", "v1", run.qs) == {
        (band[0].content_hash, "q_bool"), (band[1].content_hash, "q_bool"),
    }  # fmt: skip


def test_ut03_139_escalate_requeues_below_gate_answers_for_tonight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 after the teacher answered: re-frame; deferred = still-queued records, then
    the deferred pair items (T03-21 note: below-gate teacher answers go to the LLM phase)."""
    _calls(monkeypatch, "link_candidates", "pair_inputs", "release_cuda", "guard")
    pair = QueueItem("servicenow:incident:9|servicenow:change:1", "incident", "9" * 32, "p",
                     ("change_caused_pair",))  # fmt: skip
    monkeypatch.setattr(steps, "run_decide_escalate", lambda wh, **kw: [_item(1), pair])
    framed: list[Run] = []
    monkeypatch.setattr(steps, "frame", framed.append)
    caps: list[int] = []

    def queue(wh: Any, max_records: int) -> list[QueueItem]:
        caps.append(max_records)
        return [_item(2)]

    monkeypatch.setattr(steps, "escalation_queue", queue)
    run = _run(tmp_path, cfg=_openjev_cfg(tmp_path))
    run.deciders["openjev"] = cast("Any", Decider("openjev"))
    steps.decide_escalate(run, StageReport())
    assert (framed, caps) == ([run], [150_000])
    assert run.deferred == [_item(2), pair]


def test_ut03_139_members_skip_cached_rows_and_stop_when_unavailable(tmp_path: Path) -> None:
    """UT03-139 deep members: cached pairs are not asked again; unavailability returns False."""
    run = _run(tmp_path)
    member = Decider("openjev")
    assert steps.decide_members(run, cast("Any", member), [_item(1)], "decide-escalate")
    assert steps.decide_members(run, cast("Any", member), [_item(1), _item(2)], "decide-escalate")
    assert member.calls == [[_item(1).record_id], [_item(2).record_id]]
    down = Decider("openjev", "v2", fail=ModelUnavailable("openjev timeout"))
    assert not steps.decide_members(run, cast("Any", down), [_item(3)], "decide-escalate")


def test_ut03_139_members_flush_then_yield(tmp_path: Path) -> None:
    """UT03-139 deep members: a yield request flushes the cache writer, then yields."""
    ctx = FakeJobContext()
    ctx.request_yield()
    run = _run(tmp_path, ctx=ctx)
    member = Decider("openjev")
    with pytest.raises(YieldRequested, match="decide-escalate"):
        steps.decide_members(run, cast("Any", member), [_item(1)], "decide-escalate")
    assert run.cache.existing_keys("openjev", "v1", run.qs) == {(_item(1).content_hash, "q_bool")}


def test_ut03_139_members_record_llm_votes(tmp_path: Path) -> None:
    """UT03-139 an LLM member's rows carry its vote count as `samples`."""
    run = _run(tmp_path)
    llm = LlmDecider.__new__(LlmDecider)
    fake = Decider("llm")
    object.__setattr__(llm, "decide", fake.decide)
    llm.version = "v1"
    llm._votes = 3
    steps.decide_members(run, llm, [_item(5)], "reasoning")
    table = run.cache.dataset().to_table()  # type: ignore[union-attr]
    assert table.column("samples").to_pylist() == [3]


def _incidents(run: Run, n: int) -> None:
    for i in range(n):
        run.wh.execute(
            "INSERT INTO core.incident VALUES (?, ?)", [f"r{i}", NOW - timedelta(days=1)]
        )
        run.wh.execute("INSERT INTO enrich.text_redacted VALUES (?, 'incident', 't', ?)",
                       [f"r{i}", f"{i:032x}"])  # fmt: skip


def test_ut03_139_cluster_too_few_vectors_is_degraded_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 8: ConfigError with fewer in-window incidents than PCA needs -> degraded."""

    def few(*a: Any, **k: Any) -> Any:
        msg = "cluster stage: 3 incident vectors in the window, too few"
        raise ConfigError(msg)

    monkeypatch.setattr(steps, "run_cluster_stage", few)
    monkeypatch.setattr(steps, "device", lambda: "cpu")
    run = _run(tmp_path)
    _incidents(run, 3)
    report = StageReport()
    steps.cluster(run, report)
    assert (report.status, report.note, run.cluster) == ("degraded", "too_few_vectors", None)
    _incidents(run, 70)  # 73 >= pca_dims 64: the same error is a real config fault
    with pytest.raises(ConfigError, match="too few"):
        steps.cluster(run, StageReport())


def test_ut03_139_cluster_result_kept_for_naming_and_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 8: the stage result is kept on the run; `force_full` passed through."""
    result = ClusterStageResult(cast("Any", None), [], "incremental")
    seen: list[dict[str, Any]] = []

    def stage(wh: Any, **kw: Any) -> ClusterStageResult:
        seen.append(kw)
        return result

    monkeypatch.setattr(steps, "run_cluster_stage", stage)
    monkeypatch.setattr(steps, "device", lambda: "cpu")
    run = _run(tmp_path)
    run.force_full = True
    steps.cluster(run, StageReport())
    assert run.cluster is result
    assert (seen[0]["force_full"], seen[0]["build_id"], seen[0]["device"]) == (True, BUILD, "cpu")


# --- steps 11-13 ----------------------------------------------------------------------------


def test_ut03_139_link_rebuilds_and_uses_the_teacher_pair_decider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 11: `link_cand` built if missing; table emptied; teacher's pair rows."""
    seen = _calls(monkeypatch, "link_candidates", "run_link_stage")
    run = _run(tmp_path, cfg=_openjev_cfg(tmp_path))
    run.wh.execute("INSERT INTO enrich.incident_change_link VALUES ('i', 'c', 'rule', 1.0)")
    run.versions["openjev"] = "openjev-0.4.0/openjev-latest"
    steps.link(run, StageReport())
    assert len(seen["link_candidates"]) == 1
    assert seen["run_link_stage"][0]["pair_decider"] == ("openjev", "openjev-0.4.0/openjev-latest")
    assert run.wh.execute("SELECT count(*) FROM enrich.incident_change_link").fetchone() == (0,)
    run2 = _run(tmp_path)
    steps.link(run2, StageReport())
    assert seen["run_link_stage"][1]["pair_decider"] is None


def test_ut03_139_suggest_ops_busy_is_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 12: StoreBusy after impl 02's retries -> degraded `ops_busy`."""

    def busy(*a: Any, **k: Any) -> None:
        msg = "ops store busy"
        raise StoreBusy(msg)

    monkeypatch.setattr(steps, "run_suggest_stage", busy)
    report = StageReport()
    steps.suggest(_run(tmp_path), report)
    assert (report.status, report.note) == ("degraded", "ops_busy")


def test_ut03_139_resolve_rebuilds_decisions_then_finalizes_and_compacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 13: `enrich.decision` emptied first (rerun-safe), coverage and share
    from `enrich_resolved`, `finalize_clusters` with the names, cache compaction."""
    run = _run(tmp_path)
    run.wh.execute("INSERT INTO enrich.decision (record_id, question) VALUES ('r', 'q')")

    def resolve(wh: duckdb.DuckDBPyConnection, **kw: Any) -> None:
        assert wh.execute("SELECT count(*) FROM enrich.decision").fetchone() == (0,)
        assert kw["run_started_at"] == NOW
        wh.execute("CREATE OR REPLACE TEMP TABLE enrich_resolved AS SELECT * FROM (VALUES "
                   "('incident', 'final'), ('incident', 'queue'), ('change', 'out_of_scope'))"
                   " AS t(entity, status)")  # fmt: skip
        kw["report"].decided, kw["report"].escalated = 4, 1

    monkeypatch.setattr(steps, "run_resolve", resolve)
    seen = _calls(monkeypatch, "finalize_clusters")
    compacted: list[str] = []
    monkeypatch.setattr(cache_maint, "compact", lambda paths, qsv: compacted.append(qsv))
    steps.resolve(run, StageReport())
    assert (run.coverage, run.escalation_share) == ({"incident": 0.5}, 0.25)
    assert (seen["finalize_clusters"], compacted) == ([], [QSV])
    run.cluster = ClusterStageResult(cast("Any", None), [], "full")
    run.names = {"cl_1": NameResult("Disk full", None, "llm")}
    steps.resolve(run, StageReport())
    assert seen["finalize_clusters"][0]["names"] == run.names


# --- pipeline steps 9-10 --------------------------------------------------------------------


def _candidate() -> NamingCandidate:
    return NamingCandidate("cl_1", 40, ["disk", "full"], ["Storage"], ["example"])


def test_ut03_139_reasoning_names_and_escalates_with_the_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 9: namer client from `llm_factory("cluster_namer")`, LLM escalation."""
    named: list[dict[str, Any]] = []
    escalated: list[dict[str, Any]] = []

    def name(candidates: Any, **kw: Any) -> dict[str, NameResult]:
        named.append(kw)
        return {"cl_1": NameResult("x", None, "llm")}

    monkeypatch.setattr(pipeline, "name_clusters", name)
    monkeypatch.setattr(pipeline, "run_llm_escalation", lambda d, **kw: escalated.append(kw))
    roles: list[str] = []

    def factory(role: str) -> Any:
        roles.append(role)
        return ("namer-client", "v", 2)

    run = _run(tmp_path, llm_factory=factory)
    llm = LlmDecider.__new__(LlmDecider)
    run.deciders["llm"] = llm
    run.cluster = ClusterStageResult(cast("Any", None), [_candidate()], "full")
    run.deferred = [_item(1)]
    report = StageReport()
    pipeline._reasoning(run, report, available=True)
    assert roles == ["cluster_namer"]
    assert (named[0]["client"], named[0]["root_cause_labels"]) == ("namer-client", None)
    assert escalated[0]["llm"] is llm
    assert escalated[0]["cap"] == 20_000
    assert report.status == "done"
    assert set(run.names) == {"cl_1"}


def test_ut03_139_reasoning_unavailable_gives_auto_labels_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 9: no LLM, or the namer factory failing -> `reasoning_unavailable`."""
    named: list[Any] = []

    def name(candidates: Any, **kw: Any) -> dict[str, NameResult]:
        named.append(kw["client"])
        return {}

    monkeypatch.setattr(pipeline, "name_clusters", name)
    escalated: list[Any] = []
    monkeypatch.setattr(pipeline, "run_llm_escalation", lambda d, **kw: escalated.append(kw["llm"]))
    run = _run(tmp_path)
    run.cluster = ClusterStageResult(cast("Any", None), [_candidate()], "full")
    report = StageReport()
    pipeline._reasoning(run, report, available=False)
    assert (report.status, report.note, named, escalated) == (
        "degraded", "reasoning_unavailable", [None], [None],
    )  # fmt: skip

    def factory(role: str) -> Any:
        msg = "namer down"
        raise ModelUnavailable(msg)

    run = _run(tmp_path, llm_factory=factory)
    run.deciders["llm"] = LlmDecider.__new__(LlmDecider)
    run.cluster = ClusterStageResult(cast("Any", None), [_candidate()], "full")
    report = StageReport()
    pipeline._reasoning(run, report, available=True)
    assert (report.note, named[-1]) == ("reasoning_unavailable", None)


def _cache_rows(run: Run, decider: str, version: str, answers: dict[str, str]) -> None:
    fp = question_fingerprint(run.qs.questions[0])
    rows = [{"content_hash": h, "question": "q_bool", "question_fingerprint": fp, "answer": a,
             "probability": 0.9, "distribution": [(a, 0.9)], "backend_confidence": None,
             "samples": None, "decided_at": NOW} for h, a in answers.items()]  # fmt: skip
    write_part(run.paths.cache_partition(QSV, decider, version),
               pa.Table.from_pylist(rows, schema=CACHE_SCHEMA))  # fmt: skip


def test_ut03_139_deep_llm_band_is_where_laya_and_openjev_differ(tmp_path: Path) -> None:
    """UT03-139 F03-08 step 3: with OpenJev the LLM gets the disagreements; else the band."""
    run = _run(tmp_path, depth="deep")
    run.band = [_item(1), _item(2), _item(3)]
    assert pipeline._llm_band(run) == run.band  # OpenJev not up: the whole band
    run.teacher_up = True
    run.versions = {"laya": "l1", "openjev": "o1"}
    h1, h2, h3 = (i.content_hash for i in run.band)
    _cache_rows(run, "laya", "l1", {h1: "true", h2: "true", h3: "true"})
    _cache_rows(run, "openjev", "o1", {h1: "true", h2: "false"})
    _cache_rows(run, "openjev", "old", {h3: "true"})  # another version does not count
    assert pipeline._llm_band(run) == [_item(2), _item(3)]


def test_ut03_139_deep_reasoning_runs_llm_band_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 deep step 9: the LLM member answers the band rows within `llm_max_rows`."""
    monkeypatch.setattr(pipeline, "run_llm_escalation", lambda d, **kw: 0)
    members: list[tuple[Any, list[QueueItem], str]] = []
    monkeypatch.setattr(steps, "decide_members", lambda r, d, i, s: members.append((d, i, s)))
    cfg = _cfg(tmp_path, decisions=_decisions(ensemble={"llm_max_rows": 1}))
    run = _run(tmp_path, depth="deep", cfg=cfg)
    llm = LlmDecider.__new__(LlmDecider)
    run.deciders["llm"] = llm
    run.band = [_item(1), _item(2)]
    pipeline._reasoning(run, StageReport(), available=True)
    assert members == [(llm, [_item(1)], "reasoning")]


def test_ut03_139_ensemble_only_at_deep_and_pools_present_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-139 step 10: skipped below `deep`; deep pools members with gold accuracy weights."""
    report = StageReport()
    pipeline._ensemble(_run(tmp_path), report)
    assert (report.status, report.note) == ("skipped", "not_deep")
    framed: list[Run] = []
    monkeypatch.setattr(steps, "frame", framed.append)
    monkeypatch.setattr(pipeline, "ensemble_band", lambda wh, cfg, qs: [_item(1)])
    pooled: list[dict[str, Any]] = []

    def pool(wh: Any, **kw: Any) -> str:
        pooled.append(kw)
        return "ens-v1"

    monkeypatch.setattr(pipeline, "run_ensemble_pool", pool)
    run = _run(tmp_path, depth="deep")
    run.versions = {"laya": "l1", "llm": "m1"}
    cal = SimpleNamespace(accuracy=0.8)
    monkeypatch.setattr(CalibrationStore, "load",
                        lambda self, d, v, qsv: {"q_bool": cal} if d == "laya" else {})  # fmt: skip
    pipeline._ensemble(run, StageReport())
    assert framed == [run]
    assert pooled[0]["members"] == [("laya", "l1"), ("llm", "m1")]
    assert pooled[0]["gold_accuracy"] == {("laya", "q_bool"): 0.8}
    assert run.versions["ensemble"] == "ens-v1"
