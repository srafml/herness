"""Nightly enrichment benchmarks BT03-06 and BT03-10 (impl 03 §10, T03-36).

Run (idle host only): `HERNESS_BENCH_FULL=1 HERNESS_SYNTH_FULL_ROOT=<seed 42 full root>
pytest -m "integration and slow" tests/bench/enrich/test_enrich_nightly_bench.py`. Needs about
80 GB of free disk next to the lake (two build copies, the vector store, the decision cache).

Status at full scale on the w50 idle host: both rows are strict expected failures (xfail,
raises ArrowInvalid): the night-1 priming crashed after 4:29:46 in `cache_maint.compact`
(owner T03-39), so night 2 was never measured. Small scale runs and asserts normally.

One module fixture (`_nightly_env.run_two_nights`) primes night 1 (the whole build minus
5,000 records, enriched from nothing, which at the spec scale takes hours: text, 5.5M fake
embeddings, cached decisions, a full recluster) and then runs night 2, the measured one, on the
full build with 5,000 new and 5,000 changed records and `prev_warehouse` = night 1. See the
module docstring of `_nightly_env` for what is real and what is faked.

* BT03-10 is the wall time of the night 2 `run_enrichment` call (all stages) with the model
  stages on fakes and stubs; the real-model figure is owed on the target PC (GPU, bge-m3,
  Laya, OpenJev). It also checks the preconditions of the row: the cluster stage ran
  incrementally (no recluster) and nothing failed.
* BT03-06 measures the escalation share after acceptance: the share of the night's decided
  (record, question) pairs, for the 10,000 new and changed records, that fell below the
  student's gate and were answered by a teacher (`enrich.decision.escalated`). With an
  accepted fake student that is unsure on a fixed 2 % of the texts, it checks the pipeline's
  accounting and gating of escalation (the stage counts, the `escalated` flag, the stub's call
  count); it does NOT measure a real student's confidence, which is calibration and ET03-01's
  job. The fake student's `calibration.json` has an empty `questions` map, which bypasses the
  real rule "an uncalibrated question cannot be accepted" (TH03-17); the manifest accepts
  every question directly. The `EnrichReport.escalation_share` of the whole build (checked
  against the same SQL share) and the record-level share of the stage reports are recorded
  next to it.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator

import pyarrow as pa
import pytest
import yaml
from tests.bench.enrich._common import (
    BT06_SHARE_MAX,
    BT10_LIMIT_S,
    NIGHTLY_RECORDS,
    TIMEOUT_S,
    plant_full_scale_xfail,
    record,
    record_info,
)
from tests.bench.enrich._lake import LakeBuild
from tests.bench.enrich._nightly_env import LOW_RATE, Night, run_two_nights
from tests.support.bench import BenchRecorder

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_PRIMING_CRASH = (
    "night-1 priming at full scale (5.7M texts) crashes in herness/enrich/cache_maint.compact: "
    "pyarrow ArrowInvalid offset overflow while concatenating arrays (after 4:29:46 on the w50 "
    "idle host); owner T03-39 (new card: cache compaction offset overflow at 5M-record priming)"
)


def _escalation_cap(lake: LakeBuild) -> int:
    """`escalation.max_rows_per_night` of the config tree the rows run with."""
    doc = yaml.safe_load((lake.env.cfg_dir / "decisions.yaml").read_text(encoding="utf-8"))
    return int(doc["escalation"]["max_rows_per_night"])


@pytest.fixture(scope="module")
def night(
    lake: LakeBuild, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Night | pa.lib.ArrowInvalid]:
    """Night 1 (priming) and night 2 (measured) on copies of the lake build. At full scale
    only, the known priming crash (`ArrowInvalid`, T03-39) is returned instead of raised, so
    the rows can mark themselves as strict expected failures; anything else propagates."""
    work = tmp_path_factory.mktemp("bt03-night")
    gen = run_two_nights(lake, work)
    try:
        try:
            first: Night | pa.lib.ArrowInvalid = next(gen)
        except pa.lib.ArrowInvalid as exc:
            if lake.scale != "full":
                raise
            first = exc
        yield first
        next(gen, None)  # resume the scenario to its cleanup
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _ready(night: Night | pa.lib.ArrowInvalid, scale: str, request: pytest.FixtureRequest) -> Night:
    """The nights, or (full scale, priming crashed) plant the strict xfail and re-raise."""
    if isinstance(night, pa.lib.ArrowInvalid):
        plant_full_scale_xfail(request, scale, _PRIMING_CRASH, raises=pa.lib.ArrowInvalid)
        raise night
    return night


def _failed(night: Night) -> list[str]:
    return [n for n, s in night.report2.stages.items() if s.status == "failed"]


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_10_nightly_enrichment_total_10k_no_recluster(
    lake: LakeBuild,
    night: Night | pa.lib.ArrowInvalid,
    bench_recorder: BenchRecorder,
    request: pytest.FixtureRequest,
) -> None:
    """BT03-10 nightly enrichment of 10k new or changed records (no recluster, no backlog)
    takes under 20 min; model stages on fakes, the real-model figure is owed on the target PC."""
    night = _ready(night, lake.scale, request)
    report, report1 = night.report2, night.report1
    assert not _failed(night)
    assert report.cluster_run == "incremental", "the night reclustered"
    assert report.stages["text"].rows >= night.incidents  # every record has its text row
    redacted = report.stages["text"].rows - report.stages["text"].cache_hits
    floor = int(0.95 * NIGHTLY_RECORDS)
    assert floor <= redacted <= NIGHTLY_RECORDS  # exactly the new and changed records
    embed = report.stages["embed"]
    assert embed.embedded + embed.cache_hits >= floor  # texts embedded or reused by hash
    primary = report.stages["decide-primary"].decided
    assert night.night_new_incident_hashes > 0
    # every new distinct incident text is asked the Laya-primary questions
    assert primary >= int(0.95 * night.night_new_incident_hashes)
    record_info(bench_recorder, "BT03-10", "new_incident_hashes", night.night_new_incident_hashes,
                "rows", lake.scale)  # fmt: skip
    # no backlog: night 1 stayed under the nightly escalation cap and left nothing queued
    esc1, esc2 = report1.stages["decide-escalate"], report.stages["decide-escalate"]
    assert esc1.status == "done"
    assert esc2.status == "done"
    assert esc1.escalated < _escalation_cap(lake)
    assert report.stages["reasoning"].decided == 0  # no deferred item reached the LLM
    record_info(bench_recorder, "BT03-10", "texts_redacted", redacted, "rows", lake.scale)
    record_info(bench_recorder, "BT03-10", "embedded", report.stages["embed"].embedded, "rows",
                lake.scale)  # fmt: skip
    record_info(bench_recorder, "BT03-10", "night1_priming_s", night.night1_s, "s", lake.scale)
    for name, stage in report.stages.items():
        record_info(bench_recorder, "BT03-10", f"stage_{name}_s", stage.duration_s, "s",
                    lake.scale)  # fmt: skip
    assert record(bench_recorder, "BT03-10", "nightly_total_s", night.night2_s, "s",
                  BT10_LIMIT_S, lake.scale).passed  # fmt: skip


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_06_escalation_share_after_acceptance(
    lake: LakeBuild,
    night: Night | pa.lib.ArrowInvalid,
    bench_recorder: BenchRecorder,
    request: pytest.FixtureRequest,
) -> None:
    """BT03-06 with an accepted (fake) student the share of decided pairs escalated to the
    teacher in a nightly run is at most 10 %."""
    night = _ready(night, lake.scale, request)
    stages = night.report2.stages
    laya_records = stages["decide-primary"].decided
    escalated_records = stages["decide-escalate"].escalated
    assert night.night_pairs > 0, "the night decided no pair"
    assert not _failed(night)
    share = night.night_escalated_pairs / night.night_pairs
    assert night.openjev_calls > 0  # escalated pairs really reached the (stub) teacher
    assert night.night_escalated_pairs > 0  # the 2 % unsure texts really escalated
    assert night.escalated_decisions <= night.decisions
    whole = night.report2.escalation_share
    assert whole is not None
    # agreement: the report's build-wide share equals the same SQL share over all decisions
    assert whole == pytest.approx(night.escalated_decisions / night.decisions)
    scale = lake.scale
    record_info(bench_recorder, "BT03-06", "student_unsure_rate", LOW_RATE, "ratio", scale)
    record_info(bench_recorder, "BT03-06", "night_pairs", night.night_pairs, "pairs", scale)
    by_record = escalated_records / max(laya_records + escalated_records, 1)
    record_info(
        bench_recorder, "BT03-06", "night_record_escalation_share", by_record, "ratio", scale
    )
    record_info(bench_recorder, "BT03-06", "build_escalation_share", whole, "ratio", scale)
    assert record(bench_recorder, "BT03-06", "night_escalation_share", share, "ratio",
                  BT06_SHARE_MAX, scale, inclusive=True).passed  # fmt: skip
