"""Model-service benchmarks BT03-02 ... BT03-05 (impl 03 §10, T03-36).

These rows need a REAL local model or GPU service and are blocked on this host (no GPU, no
bge-m3, no Laya/OpenJev service, no local LLM: V-09 / V-11 / V-12 / D17). They are implemented
to run against the real services when `HERNESS_BENCH_FULL=1` and the service variable of the
row are set (names in `_services.py` and below) and skip, naming the blocker and the variables
needed, otherwise. With the variables set a row really executes: a missing CUDA device, an
unreadable model directory or an unreachable service fails the row, it never passes silently.

* BT03-02 `HERNESS_BENCH_EMBED_MODEL_DIR` (bge-m3 directory): `embed.embed_texts` on CUDA,
  100,000 texts (rate >= 700 texts/s, extrapolated to 5.5M texts) and 10,000 texts (< 30 s).
* BT03-03 `HERNESS_BENCH_LAYA_DATA` (data root with `models/laya/CURRENT`): `LayaDecider.decide`
  with the five incident questions of the shipped config, 100,000 records (>= 400 records/s)
  and 10,000 (< 1 min); the load is excluded (warm model).
* BT03-04 `HERNESS_BENCH_OPENJEV_URL` (loopback): `OpenJevDecider.decide` over 10,000 records,
  all five questions, in the pipeline's 2,000-item chunks (>= 25 records/s).
* BT03-05 `HERNESS_BENCH_LLM_PROFILE`: `LlmDecider` with 3 votes (D7) over 2,000 records;
  the rate is recorded, not judged ("about 5 records/s, target to confirm", V-12).

The corpus is `_texts.ticket_texts` (deterministic, no personal data).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

import pytest
from tests.bench.enrich._common import (
    BT02_10K_LIMIT_S,
    BT02_RATE_MIN,
    BT03_10K_LIMIT_S,
    BT03_RATE_MIN,
    BT04_RATE_MIN,
    EMBED_MODEL_ENV,
    LAYA_DATA_ENV,
    LLM_PROFILE_ENV,
    NIGHTLY_RECORDS,
    OPENJEV_URL_ENV,
    TIMEOUT_S,
    record,
    record_info,
    require_service,
)
from tests.bench.enrich._services import llm_client, load_shipped_config, shipped_questions
from tests.bench.enrich._texts import decision_items, ticket_texts
from tests.support.bench import BenchRecorder

from herness.enrich.deciders.laya import LayaDecider
from herness.enrich.deciders.llm import LlmDecider
from herness.enrich.deciders.openjev import OpenJevDecider
from herness.enrich.embed import Encoder, embed_texts
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import LayaSettings, OpenJevSettings

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.gpu]

_N_BULK: Final = 100_000
_FULL_TEXTS: Final = 5_500_000  # the production corpus the bge-m3 rate is extrapolated to
_N_OPENJEV: Final = 10_000
_N_LLM: Final = 2_000
_CHUNK: Final = 2_000  # the pipeline's `decide` chunk (impl 03 §10 limits)
_BLOCKER_GPU: Final = "V-09 (no GPU / bge-m3 weights on this host)"
_BLOCKER_LAYA: Final = "V-11 / D17 (no Laya package, GPU or accepted model on this host)"
_BLOCKER_OPENJEV: Final = "V-09 (no GPU OpenJev service on this host)"
_BLOCKER_LLM: Final = "V-12 (no local LLM service on this host)"


def _cuda_encoder(model_dir: str, config: pytest.Config) -> tuple[Encoder, int]:
    """A loaded bge-m3 encoder on CUDA and the configured batch size; the row fails when
    there is no CUDA device or the directory does not load (never a silent CPU run)."""
    import torch  # noqa: PLC0415 - only when a model row runs

    if not torch.cuda.is_available():
        pytest.fail(f"{EMBED_MODEL_ENV} is set but this host has no CUDA device")
    cfg = load_shipped_config(config)
    encoder = Encoder(Path(model_dir), model_name=cfg.decisions.embedding.model)
    encoder.load("cuda")
    return encoder, cfg.decisions.embedding.batch_size


def _rate_row(
    recorder: BenchRecorder, bench_id: str, count: int, elapsed: float, unit: str, floor: float
) -> float:  # fmt: skip
    rate = count / elapsed
    assert record(recorder, bench_id, f"{unit}_per_s", rate, f"{unit}/s", floor, f"n{count}",
                  better="higher").passed  # fmt: skip
    return rate


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_02_bge_m3_embedding_rate_100k(
    request: pytest.FixtureRequest, bench_recorder: BenchRecorder
) -> None:
    """BT03-02 bge-m3 embeds 100k texts at 700 texts/s or more (extrapolated to 5.5M)."""
    env = require_service(request.config, "bge-m3 embedding", _BLOCKER_GPU, [EMBED_MODEL_ENV])
    encoder, batch = _cuda_encoder(env[EMBED_MODEL_ENV], request.config)
    texts = ticket_texts(_N_BULK)
    embed_texts(encoder, texts[:512], batch_size=batch)  # warm-up (kernels, allocator)
    start = time.perf_counter()
    rows = embed_texts(encoder, texts, batch_size=batch)
    elapsed = time.perf_counter() - start
    assert rows.shape[0] == _N_BULK
    rate = _rate_row(bench_recorder, "BT03-02", _N_BULK, elapsed, "texts", BT02_RATE_MIN)
    record_info(bench_recorder, "BT03-02", "extrapolated_5_5m_h", _FULL_TEXTS / rate / 3600,
                "h", f"n{_N_BULK}")  # fmt: skip


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_02_bge_m3_nightly_10k(
    request: pytest.FixtureRequest, bench_recorder: BenchRecorder
) -> None:
    """BT03-02 bge-m3 embeds a 10k nightly increment in under 30 s."""
    env = require_service(request.config, "bge-m3 embedding", _BLOCKER_GPU, [EMBED_MODEL_ENV])
    encoder, batch = _cuda_encoder(env[EMBED_MODEL_ENV], request.config)
    texts = ticket_texts(NIGHTLY_RECORDS, seed=7)
    embed_texts(encoder, texts[:512], batch_size=batch)
    start = time.perf_counter()
    embed_texts(encoder, texts, batch_size=batch)
    elapsed = time.perf_counter() - start
    assert record(bench_recorder, "BT03-02", "embed_10k_s", elapsed, "s", BT02_10K_LIMIT_S,
                  f"n{NIGHTLY_RECORDS}").passed  # fmt: skip


def _laya(data_root: str) -> LayaDecider:
    """A loaded `LayaDecider` on CUDA from the data root's CURRENT version (fails loudly)."""
    import torch  # noqa: PLC0415 - only when a model row runs

    if not torch.cuda.is_available():
        pytest.fail(f"{LAYA_DATA_ENV} is set but this host has no CUDA device")
    paths = EnrichPaths(data_root=Path(data_root).resolve(),
                        embedding_path="data/models/embedding",
                        laya_current_file="data/models/laya/CURRENT")  # fmt: skip
    decider = LayaDecider(LayaSettings(device="cuda"), paths=paths)
    decider.health()  # CURRENT names an accepted, untampered version
    decider.load()
    return decider


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_03_laya_bulk_inference_100k(
    request: pytest.FixtureRequest, bench_recorder: BenchRecorder
) -> None:
    """BT03-03 Laya answers 5 questions for 100k records at 400 records/s or more."""
    env = require_service(request.config, "Laya bulk inference", _BLOCKER_LAYA, [LAYA_DATA_ENV])
    qs = shipped_questions(load_shipped_config(request.config))
    decider = _laya(env[LAYA_DATA_ENV])
    items = decision_items(ticket_texts(_N_BULK))
    try:
        decider.decide(items[:512], qs)  # warm-up
        start = time.perf_counter()
        out = decider.decide(items, qs)
        elapsed = time.perf_counter() - start
    finally:
        decider.unload()
    assert len(out) == _N_BULK
    assert all(len(o.answers) == 5 for o in out[:100])  # the five incident questions
    _rate_row(bench_recorder, "BT03-03", _N_BULK, elapsed, "records", BT03_RATE_MIN)


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_03_laya_nightly_10k(
    request: pytest.FixtureRequest, bench_recorder: BenchRecorder
) -> None:
    """BT03-03 Laya answers a 10k nightly increment in under 1 min."""
    env = require_service(request.config, "Laya bulk inference", _BLOCKER_LAYA, [LAYA_DATA_ENV])
    qs = shipped_questions(load_shipped_config(request.config))
    decider = _laya(env[LAYA_DATA_ENV])
    items = decision_items(ticket_texts(NIGHTLY_RECORDS, seed=7))
    try:
        decider.decide(items[:512], qs)
        start = time.perf_counter()
        out = decider.decide(items, qs)
        elapsed = time.perf_counter() - start
    finally:
        decider.unload()
    assert len(out) == NIGHTLY_RECORDS
    assert record(bench_recorder, "BT03-03", "laya_10k_s", elapsed, "s", BT03_10K_LIMIT_S,
                  f"n{NIGHTLY_RECORDS}").passed  # fmt: skip


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_04_openjev_escalation_10k_all_questions(
    request: pytest.FixtureRequest, bench_recorder: BenchRecorder
) -> None:
    """BT03-04 OpenJev escalation of 10k records, all questions, at 25 records/s or more."""
    env = require_service(request.config, "OpenJev escalation", _BLOCKER_OPENJEV,
                          [OPENJEV_URL_ENV])  # fmt: skip
    cfg = load_shipped_config(request.config)
    qs = shipped_questions(cfg)
    decider = OpenJevDecider(OpenJevSettings(base_url=env[OPENJEV_URL_ENV]), api_key=None,
                             image_tag="bench", samples=None)  # fmt: skip
    decider.health()  # fails loudly when the service is not up
    items = decision_items(ticket_texts(_N_OPENJEV))
    start = time.perf_counter()
    answered = 0
    for i in range(0, len(items), _CHUNK):
        answered += len(decider.decide(items[i : i + _CHUNK], qs))
    elapsed = time.perf_counter() - start
    assert answered == _N_OPENJEV
    _rate_row(bench_recorder, "BT03-04", _N_OPENJEV, elapsed, "records", BT04_RATE_MIN)


@pytest.mark.timeout(TIMEOUT_S)
def test_bt03_05_llm_decider_three_votes_2k(
    request: pytest.FixtureRequest, bench_recorder: BenchRecorder
) -> None:
    """BT03-05 the LLM decider with 3 votes (D7) answers 2k records; the rate (about 5
    records/s, target to confirm under V-12) is recorded."""
    env = require_service(request.config, "LLM decider (3 votes)", _BLOCKER_LLM,
                          [LLM_PROFILE_ENV])  # fmt: skip
    client, version, concurrency = llm_client(request.config, env[LLM_PROFILE_ENV],
                                              role="enrich_decider")  # fmt: skip
    cfg = load_shipped_config(request.config, env[LLM_PROFILE_ENV])
    qs = shipped_questions(cfg)
    decider = LlmDecider(client, version=version, votes=3,
                         temperature=cfg.models.deciders.llm.temperature,
                         max_concurrency=concurrency)  # fmt: skip
    items = decision_items(ticket_texts(_N_LLM))
    start = time.perf_counter()
    out = decider.decide(items, qs)
    elapsed = time.perf_counter() - start
    assert len(out) == _N_LLM
    rate = _N_LLM / elapsed
    assert rate > 0
    record_info(bench_recorder, "BT03-05", "records_per_s", rate, "records/s", f"n{_N_LLM}")
