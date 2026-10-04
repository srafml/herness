"""ST03-11 (TH03-09) unbounded consumption: oversize text, option explosion, huge queues.

Real functions only (no network, no real model): a 10M-character field is cut to 4,000
characters by `normalize_text` and the text stage, and a `DecisionInput` refuses more than
12,000; a 1,000-team dynamic question resolves (no upper bound for dynamic sources), is
refused on the wire (> 255) until `shortlist_options` cuts it to 64, and a static question
is refused above 255; a queue of 1M records is capped at the nightly 150,000 by
`escalation_queue` (DuckDB `range`, lazy) and at the nightly 20,000 by
`run_llm_escalation` (a lazy 1M-item sequence, a counting stub LLM). The nightly caps are the
spec's 150k / 20k / 300k / 30k / 500.
"""

from __future__ import annotations

import dataclasses
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, cast, overload

import duckdb
import numpy as np
import pytest
from pydantic import ValidationError
from tests.support.fake_keyring import MemoryKeyring
from tests.support.text_warehouse import Rec, create_warehouse
from tests.unit.enrich._distill_support import FakeCtx, decisions
from tests.unit.enrich._fake_llm import FakeLLMClient
from tests.unit.enrich._openjev_support import jev_env
from tests.unit.enrich._sampling_support import SETTINGS_SQL

from herness.core.config import get_config
from herness.core.errors import ConfigError
from herness.core.resilience import ProcessState
from herness.core.types import (
    Answer,
    DecisionInput,
    DecisionOutput,
    LLMRequest,
    Question,
    QuestionSet,
)
from herness.enrich.cache import DecisionCache
from herness.enrich.decide_stage import run_llm_escalation
from herness.enrich.deciders.jev_wire import to_wire_questions
from herness.enrich.deciders.llm import LlmDecider
from herness.enrich.layout import EnrichPaths
from herness.enrich.pipeline import StageReport
from herness.enrich.questions import resolve_dynamic_options, shortlist_options
from herness.enrich.resolve import QueueItem, escalation_queue
from herness.enrich.text import MAX_FIELD_CHARS, build_text_redacted, compose_text, normalize_text

pytestmark = pytest.mark.unit

__all__ = ["jev_env"]  # the fixture is used by name

HUGE: Final = 10_000_000
QUEUE: Final = 1_000_000
QSV: Final = "qs-2026-10-04"
_TEAM: Final = Question(
    id="owning_team", type="choice", options_source="core.team", applies_to=("incident",),
    instructions="Which support team should own this issue?", threshold=0.6, scoring_use=False,
)  # fmt: skip


# --- 10M-character text ---------------------------------------------------------------------


def test_st03_11_ten_million_char_field_truncated_to_4000() -> None:
    """ST03-11 a 10M-character field (words and whitespace runs) is cut to 4,000 characters;
    a composed text holds at most two cut fields; a DecisionInput refuses > 12,000."""
    field = ("disk  full\t" * (HUGE // 11 + 1))[:HUGE]
    assert len(field) == HUGE
    text = normalize_text(field)
    assert MAX_FIELD_CHARS == 4_000
    assert 0 < len(text) <= 4_000
    assert normalize_text(text) == text
    composed = compose_text(field, field)
    assert len(composed) <= 2 * 4_000 + 2
    DecisionInput(record_id="INC1", entity="incident", content_hash="0" * 32, text=composed)
    with pytest.raises(ValidationError):
        DecisionInput(record_id="INC1", entity="incident", content_hash="0" * 32, text=field)


def test_st03_11_text_stage_stores_truncated_text(
    jev_env: ProcessState, fake_keyring: MemoryKeyring, tmp_path: Path
) -> None:
    """ST03-11 the text stage over a record with two 10M-character fields stores one text of
    at most 8,002 characters (two 4,000-character fields and the blank line)."""
    del jev_env
    fake_keyring.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()
    huge = "printer jam " * (HUGE // 12)
    wh = create_warehouse(tmp_path / "wh.duckdb", [Rec("incident", "INC-1", huge, huge)])
    try:
        report = StageReport()
        build_text_redacted(wh, prev_warehouse=None, report=report)
        rows = wh.execute("SELECT text FROM enrich.text_redacted").fetchall()
    finally:
        wh.close()
    assert report.rows == 1
    [(text,)] = rows
    assert 4_000 < len(text) <= 2 * 4_000 + 2


# --- 1,000-option dynamic question ------------------------------------------------------------


def _teams(n: int) -> duckdb.DuckDBPyConnection:
    wh = duckdb.connect()
    wh.execute("CREATE SCHEMA core")
    wh.execute("CREATE TABLE core.team (team_id VARCHAR, name VARCHAR, active BOOLEAN)")
    wh.execute("CREATE TABLE core.service (service_id VARCHAR, name VARCHAR)")
    wh.execute(
        "INSERT INTO core.team SELECT 'team_' || lpad(i::VARCHAR, 4, '0'), "
        "'Team ' || i::VARCHAR, true FROM range(?) AS t(i)",
        [n],
    )
    return wh


def _unit(v: np.ndarray) -> np.ndarray:
    out: np.ndarray = (v / np.linalg.norm(v)).astype(np.float32)
    return out


def _thousand_option_question() -> Question:
    qs = QuestionSet(version=QSV, questions=(_TEAM,))
    resolved = resolve_dynamic_options(qs, wh=_teams(1_000), redact=lambda s: s)
    question = resolved.get("owning_team")
    assert question.options is not None
    assert len(question.options) == 1_000  # dynamic sources have no upper bound
    return question


def test_st03_11_thousand_option_dynamic_question_shortlisted_to_64() -> None:
    """ST03-11 1,000 active teams: the dynamic question holds 1,000 options, the wire refuses
    it (> 255), `shortlist_options` keeps the 64 most similar and the wire accepts those."""
    question = _thousand_option_question()
    with pytest.raises(ConfigError, match="more than 255 options"):
        to_wire_questions([question])
    rng = np.random.default_rng(11)
    vecs = {label: _unit(rng.standard_normal(1024)) for label in question.options or {}}
    text_vec = _unit(rng.standard_normal(1024))
    short = shortlist_options(question, text_vec, vecs)
    assert short.options is not None
    assert len(short.options) == 64
    assert short.fingerprint == question.fingerprint
    sims = {label: float(vecs[label] @ text_vec) for label in vecs}
    assert set(short.options) == set(sorted(sims, key=lambda k: (-sims[k], k))[:64])
    wire = to_wire_questions([short])
    assert len(cast("dict[str, str]", wire["owning_team"]["criteria"])) == 64


def test_st03_11_static_question_above_255_options_refused() -> None:
    """ST03-11 a static choice question with 256 options is refused (2-255); 255 is kept and
    goes on the wire."""
    options = {f"opt_{i:03d}": f"Option {i}" for i in range(256)}
    with pytest.raises(ValidationError, match="2-255"):
        Question(
            id="cause",
            type="choice",
            options=options,
            instructions="Which option applies?",
            threshold=0.8,
        )
    del options["opt_255"]
    q = Question(
        id="cause",
        type="choice",
        options=options,
        instructions="Which option applies?",
        threshold=0.8,
    )
    assert len(cast("dict[str, str]", to_wire_questions([q])["cause"]["criteria"])) == 255


def _first_label(req: LLMRequest) -> dict[str, object]:
    schema = cast("dict[str, Any]", req.response_schema or {})
    props: dict[str, Any] = schema.get("properties", {})
    return {qid: {"answer": spec["properties"]["answer"]["enum"][0]} for qid, spec in props.items()}


def _fake_embed(texts: Sequence[str]) -> np.ndarray:
    """Deterministic unit vectors (seeded by the text hash): the OI-06 `embed_fn` stand-in."""
    rows = [np.random.default_rng(zlib.crc32(t.encode())).standard_normal(1024) for t in texts]
    return np.stack([_unit(r) for r in rows])


def test_st03_11_llm_decider_asks_at_most_64_of_1000_dynamic_options(
    jev_env: ProcessState,
) -> None:
    """ST03-11 the LLM decider asked the 1,000-option dynamic question lists at most 64
    options per record (design 03 §5.5 per-record shortlist, TH03-09) and answers it."""
    del jev_env
    question = _thousand_option_question()
    asked: list[int] = []

    def reply(req: LLMRequest) -> dict[str, object]:
        schema = cast("dict[str, Any]", req.response_schema or {})
        asked.append(len(schema["properties"]["owning_team"]["properties"]["answer"]["enum"]))
        return _first_label(req)

    client = FakeLLMClient(reply)
    decider = LlmDecider(client, version="local/qwen-test", votes=1, temperature=0.0,
                         max_concurrency=1, embed_fn=_fake_embed)  # fmt: skip
    item = DecisionInput(record_id="INC1", entity="incident", content_hash="1" * 32,
                         text="Team 7 cannot log in", question_ids=("owning_team",))  # fmt: skip
    [out] = decider.decide([item], QuestionSet(version=QSV, questions=(question,)))
    assert asked
    assert max(asked) <= 64, asked
    assert out.error is None
    assert out.answers["owning_team"].answer in (question.options or {})


# --- queue of 1M ------------------------------------------------------------------------------


def _million_queue() -> duckdb.DuckDBPyConnection:
    """`enrich_resolved` with 1M queued records (half scoring) and their redacted texts."""
    wh = duckdb.connect()
    wh.execute(SETTINGS_SQL.read_text("utf-8"))
    wh.execute(
        "CREATE TEMP TABLE enrich_resolved AS SELECT 'INC' || lpad(i::VARCHAR, 7, '0') "
        "AS record_id, 'incident' AS entity, lpad(i::VARCHAR, 32, '0') AS content_hash, "
        "'root_cause' AS question, 'queue' AS status, i % 2 = 0 AS scoring_use, "
        "TIMESTAMPTZ '2026-01-01 00:00:00+00' + to_seconds(i) AS opened_at "
        "FROM range(?) AS t(i)",
        [QUEUE],
    )
    wh.execute(
        "INSERT INTO enrich.text_redacted SELECT record_id, entity, 'ticket ' || record_id, "
        "content_hash FROM enrich_resolved"
    )
    return wh


def test_st03_11_million_record_queue_capped_at_150000() -> None:
    """ST03-11 1M queued records: `escalation_queue` at the nightly cap returns exactly
    150,000 items, scoring records first, newest first."""
    cap = decisions().escalation.max_rows_per_night
    assert cap == 150_000
    wh = _million_queue()
    try:
        assert wh.execute("SELECT count(*) FROM enrich_resolved").fetchone() == (QUEUE,)
        items = escalation_queue(wh, max_records=cap)
    finally:
        wh.close()
    assert len(items) == cap
    assert items[0].record_id == "INC0999998"  # newest scoring record (even index)
    assert len({item.record_id for item in items}) == cap


class _LazyQueue(Sequence[QueueItem]):
    """A 1M-item deferred list built on access (no 1M objects in memory)."""

    def __len__(self) -> int:
        return QUEUE

    def _item(self, i: int) -> QueueItem:
        return QueueItem(f"INC{i:07d}", "incident", f"{i:032x}", f"ticket {i}", ("root_cause",))

    @overload
    def __getitem__(self, index: int) -> QueueItem: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[QueueItem]: ...
    def __getitem__(self, index: int | slice) -> QueueItem | Sequence[QueueItem]:
        if isinstance(index, slice):
            return [self._item(i) for i in range(*index.indices(QUEUE))]
        return self._item(index)


@dataclasses.dataclass
class _CountingLlm:
    """LlmDecider stand-in: answers every item, counts them, fails fast past `limit`."""

    limit: int
    name: str = "llm"
    version: str = "local/qwen-test"
    samples: int = 1
    seen: int = 0
    chunks: list[int] = dataclasses.field(default_factory=list)

    def decide(self, items: Sequence[DecisionInput], qs: QuestionSet) -> list[DecisionOutput]:
        self.seen += len(items)
        self.chunks.append(len(items))
        assert self.seen <= self.limit, "LLM asked beyond the nightly cap"
        answer = Answer(answer="a", probability=0.9, distribution={"a": 0.9, "b": 0.1})
        return [DecisionOutput(record_id=i.record_id, content_hash=i.content_hash, decider="llm",
                               decider_version=self.version, answers={"root_cause": answer})
                for i in items]  # fmt: skip


def test_st03_11_million_deferred_items_capped_at_20000_llm_records(
    jev_env: ProcessState,
) -> None:
    """ST03-11 1M deferred items: the LLM escalation answers exactly the nightly 20,000
    records, in chunks of at most 500."""
    del jev_env
    cap = decisions().escalation.llm_max_rows_per_night
    assert cap == 20_000
    root = Question(id="root_cause", type="choice", options={"a": "A.", "b": "B."},
                    instructions="Which option applies?", threshold=0.8)  # fmt: skip
    qs = QuestionSet(version=QSV, questions=(root,))
    cache = DecisionCache(EnrichPaths.from_config(get_config()), QSV)
    llm = _CountingLlm(limit=cap)
    report = StageReport()
    answered = run_llm_escalation(_LazyQueue(), llm=cast("LlmDecider", llm), qs=qs, cache=cache,
                                  cap=cap, ctx=FakeCtx().as_ctx(), report=report)  # fmt: skip
    assert (answered, llm.seen) == (cap, cap)
    assert max(llm.chunks) <= 500  # U03-87: chunks of 500 (the literal, not LLM_CHUNK)
    assert len(cache.existing_keys("llm", llm.version, qs)) == cap


def test_st03_11_nightly_caps_match_the_spec() -> None:
    """ST03-11 the nightly caps default to the spec's 150k escalation, 20k LLM, 300k ensemble
    band, 30k pairs and 500 naming calls / disagreement reviews."""
    cfg = decisions()
    assert cfg.escalation.max_rows_per_night == 150_000
    assert cfg.escalation.llm_max_rows_per_night == 20_000
    assert cfg.ensemble.max_rows == 300_000
    assert cfg.change_link.decider_max_pairs == 30_000
    assert cfg.clustering.naming.max_llm_calls == 500
    assert cfg.ensemble.disagreement_review_cap == 500
