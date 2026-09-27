"""UT06-83, UT06-84, PT06-06: hybrid evidence pack and run-local pseudonyms (U06-123, U06-124).

The shared redactor is a real ``Redactor`` (directory name ``Jane Doe``) installed as the
process-wide instance, so ``redact_text`` runs its real detectors. Secret-looking fixtures are
built at runtime so the detect-secrets baseline does not change.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core import redact as r
from herness.core.errors import BudgetExceeded, NotFound
from herness.core.ids import canonical_json
from herness.core.redact import RedactionFailed
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.core.types import (
    SKEPTIC_CHECKS,
    Challenge,
    CheckResult,
    EntityScope,
    Finding,
    Message,
    NumberRef,
    SwarmTaskState,
    TaskBudget,
    TaskInputs,
    TaskSpec,
    TextPart,
)
from herness.harness.llm import tokens
from herness.harness.llm.settings import ClientConfig, PricePerMTok
from herness.harness.swarm import hybrid
from herness.harness.swarm.hybrid import PACK_HEADROOM_TOKENS, Pseudonymizer, build_evidence_pack

pytestmark = pytest.mark.unit

RUN = "run_" + "0" * 26
TASK = "task_" + "1" * 26
Q1 = "q_" + "a" * 16
Q2 = "q_" + "b" * 16
SECRET = "api" + "_key=" + "Zq9xT4" + "mW2pLk" + "Hh7Rr3Vv"  # pragma: allowlist secret
PERSON = "Jane Doe"
_ZERO = Decimal(0)
CFG = ClientConfig.model_validate(
    {
        "name": "claude-writer",
        "kind": "openai_compat",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "stand-in",
        "context_window": 200_000,
        "max_output_tokens": 4096,
        "tokenizer": "estimate",
        "max_concurrency": 1,
        "price_per_mtok": PricePerMTok(
            input=_ZERO, output=_ZERO, cache_read=_ZERO, cache_write=_ZERO
        ),
    }
)
ENTITIES = [
    ("team", "team-payments", "Payments Platform"),
    ("team", "team-core", "Core"),
    ("team", "team-edge", None),
    ("service", "svc-ledger", "Ledger"),
]
BIG = 10**9  # a token cap nothing reaches


@pytest.fixture(autouse=True)
def _real_redactor(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A real Redactor as the process-wide instance; dropped after the test."""
    directory = NameDirectory.from_files(None, (PERSON,), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    yield
    r.reset_redactor()


def _fid(n: int) -> str:
    return f"fnd_{n:026d}"


def _challenge(fid: str, note: str) -> Challenge:
    checks = [
        CheckResult(check=c, result="concern", note=note, query_ids=[Q1])
        if c == "confounding"
        else CheckResult(check=c, result="pass", note="")
        for c in SKEPTIC_CHECKS
    ]
    return Challenge(finding_id=fid, round=1, checks=checks, verdict="uphold")


def _finding(n: int, *, entity: tuple[str, str] = ("team", "team-payments"), **kw: Any) -> Finding:
    fid = _fid(n)
    data: dict[str, Any] = {
        "finding_id": fid,
        "run_id": RUN,
        "task_id": TASK,
        "author_role": "analyst",
        "claim": f"Payments Platform lead time doubled after the Ledger change ({n}).",
        "entity_type": entity[0],
        "entity_id": entity[1],
        "numbers": [
            NumberRef(
                id="n1",
                value=41.5,
                unit="hours",
                query_id=Q1,
                column="lead_time_p50",
                row_key={"team_id": entity[1], "metric": "lead_time", "week": 3},
            )
        ],
        "query_ids": [Q1],
        "confidence": 0.8,
        "status": "verified",
        "challenge": [_challenge(fid, "Core shipped the same week as svc-ledger.")],
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
    }
    data.update(kw)
    return Finding.model_validate(data)


def _spec(objective: str = "Write the org_review report.", notes: str | None = None) -> TaskSpec:
    return TaskSpec(
        task_id=TASK,
        run_id=RUN,
        role="writer",
        objective=objective,
        scope=EntityScope(entity_type="team", entity_ids=["team-payments"]),
        inputs=TaskInputs(notes=notes),
        tools=["list_findings"],
        budget=TaskBudget(max_steps=5, max_tokens=10_000, wall_clock_s=60),
        model_role="writer",
        dedup_key="0" * 16,
    )


PLANTED = {
    "sample": "SAMPLE-ROW-Zx81",
    "ticket": "TICKET-TEXT-Qm42",
    "enrich": "ENRICH-TEXT-Wk97",
    "prior": "PRIOR-CONTEXT-Jd55",
    "notes": "INPUT-NOTES-Pv63",
}


def _inp(**extra: object) -> dict[str, object]:
    inp: dict[str, object] = {
        "kind": "org_review",
        "findings": [{"claim": "raw Payments Platform claim", "entity_id": "team-payments"}],
        "portfolio": {
            "scenario": "baseline",
            "rows": [{"candidate_id": "team-core", "score": 0.7, "query_ids": [Q2]}],
            "selected": ["team-core"],
            "query_ids": [Q2],
            "custom": [{"note": "custom Core scenario"}],
        },
        "levers": [
            {
                "entity_id": "team-edge",
                "metric": "lead_time",
                "delta_usd": "1200.00",
                "query_id": Q2,
            }
        ],
        "dq_warnings": [
            {"check_name": "freshness_jira", "severity": "warn", "details": {"x": 1}},
            {"check_name": "row_count_pagerduty", "severity": "warn", "details": None},
        ],
        "unconfirmed_weights": ["delivery.lead_time"],
        "evidence": [{"query_id": Q1, "result_sample": [{"summary": PLANTED["sample"]}]}],
        "ticket_text": PLANTED["ticket"],
        "enrich": {"text_redacted": PLANTED["enrich"]},
        "prior_context": PLANTED["prior"],
        "inputs": {"notes": PLANTED["notes"]},
    }
    inp.update(extra)
    return inp


def _pack(
    findings: Sequence[Finding],
    *,
    max_tokens: int = BIG,
    inp: dict[str, object] | None = None,
    spec: TaskSpec | None = None,
) -> dict[str, object]:
    return build_evidence_pack(
        spec or _spec(notes=PLANTED["notes"]),
        findings=findings,
        inp=_inp() if inp is None else inp,
        pseudo=Pseudonymizer(ENTITIES),
        max_tokens=max_tokens,
        cfg=CFG,
    )


def _tokens(pack: dict[str, object]) -> int:
    msg = Message(role="user", parts=[TextPart(text=canonical_json(pack))])
    return tokens.count_tokens(CFG, [msg], [], [])[0]


# --- UT06-83 build_evidence_pack ---------------------------------------------------------------


def test_ut06_83_pack_items_pseudonymized_and_redacted() -> None:
    """UT06-83 items per U06-123 step 1: ids and names pseudonymized, prose redacted."""
    spec = _spec(objective=f"Write the report for Payments Platform; ask {PERSON}. {SECRET}")
    pack = _pack([_finding(1)], spec=spec)
    assert set(pack) == {
        "objective", "findings", "portfolio", "levers", "dq_checks", "unconfirmed_weights"
    }  # fmt: skip
    objective = str(pack["objective"])
    assert objective.startswith("Write the report for team_001; ask [PERSON_")
    assert "[SECRET]" in objective
    item = pack["findings"][0]  # type: ignore[index]
    assert item["finding_id"] == _fid(1)
    assert item["entity_type"] == "team"
    assert item["entity_id"] == "team_001"
    assert item["claim"] == "team_001 lead time doubled after the service_001 change (1)."
    assert item["confidence"] == 0.8
    row_key = item["numbers"][0]["row_key"]
    assert row_key == {"team_id": "team_001", "metric": "lead_time", "week": 3}
    assert item["numbers"][0]["query_id"] == Q1
    assert item["challenges"] == [
        {
            "round": 1,
            "verdict": "uphold",
            "checks": [
                {
                    "check": "confounding",
                    "result": "concern",
                    "note": "team_002 shipped the same week as service_001.",
                }
            ],
        }
    ]
    assert pack["portfolio"] == {
        "scenario": "baseline",
        "rows": [{"candidate_id": "team_002", "score": 0.7, "query_ids": [Q2]}],
        "selected": ["team_002"],
        "query_ids": [Q2],
    }
    assert pack["levers"] == [
        {"entity_id": "team_003", "metric": "lead_time", "delta_usd": "1200.00", "query_id": Q2}
    ]
    assert pack["dq_checks"] == ["freshness_jira", "row_count_pagerduty"]
    assert pack["unconfirmed_weights"] == ["delivery.lead_time"]


def test_ut06_83_pack_never_holds_samples_notes_ticket_text_or_raw_entities() -> None:
    """UT06-83 no result_sample, ticket text, enrich text, prior context or notes; no raw ids."""
    text = canonical_json(_pack([_finding(1), _finding(2, entity=("service", "svc-ledger"))]))
    for planted in PLANTED.values():
        assert planted not in text
    for forbidden in ("result_sample", "text_redacted", "prior_context", "notes", "custom"):
        assert forbidden not in text
    for _etype, eid, name in ENTITIES:
        assert eid not in text
        assert name is None or name not in text
    assert PERSON not in text
    assert "Zq9xT4" not in text


def test_ut06_83_pack_holds_no_pseudonym_mapping() -> None:
    """UT06-83 the reverse map stays in process: no token-to-id pair reaches the pack."""
    pseudo = Pseudonymizer(ENTITIES)
    pack = build_evidence_pack(
        _spec(), findings=[_finding(1)], inp=_inp(), pseudo=pseudo, max_tokens=BIG, cfg=CFG
    )
    mapping = pseudo.mapping()
    assert mapping["team_001"] == {
        "entity_type": "team", "entity_id": "team-payments", "name": "Payments Platform"
    }  # fmt: skip
    assert 'entity_id":"team-payments' not in canonical_json(pack)
    assert SwarmTaskState(phase="writing", pseudonyms=mapping).pseudonyms == mapping


def test_ut06_83_size_trimmed_by_dropping_last_findings() -> None:
    """UT06-83 over max_tokens - PACK_HEADROOM_TOKENS: the last findings are dropped in order."""
    findings = [_finding(n) for n in range(1, 6)]
    two = _pack(findings[:2])
    limit = _tokens(two) + PACK_HEADROOM_TOKENS
    assert _tokens(_pack(findings)) > limit - PACK_HEADROOM_TOKENS
    trimmed = _pack(findings, max_tokens=limit)
    assert trimmed == two
    assert [f["finding_id"] for f in trimmed["findings"]] == [_fid(1), _fid(2)]  # type: ignore[attr-defined]
    assert _tokens(trimmed) <= limit - PACK_HEADROOM_TOKENS


def test_ut06_83_count_tokens_gets_cfg_and_final_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT06-83 count_tokens (R-17) sees cfg and one user TextPart of the final redacted pack."""
    seen: list[tuple[Any, ...]] = []

    def spy(*args: Any) -> tuple[int, bool]:
        seen.append(args)
        return tokens.count_tokens(*args)

    monkeypatch.setattr(hybrid, "count_tokens", spy)
    spec = _spec(objective=f"Report on {PERSON}")
    pack = _pack([_finding(1)], spec=spec)
    cfg, messages, tls, sys = seen[-1]
    assert cfg is CFG
    assert tls == []
    assert sys == []
    (message,) = messages
    assert message.role == "user"
    (part,) = message.parts
    assert isinstance(part, TextPart)
    assert part.text == canonical_json(pack)
    assert PERSON not in part.text


def test_ut06_83_over_cap_without_findings_raises() -> None:
    """UT06-83 a pack still over the cap with every finding dropped is never returned."""
    with pytest.raises(BudgetExceeded) as info:
        _pack([_finding(1)], max_tokens=PACK_HEADROOM_TOKENS + 1)
    assert "Payments" not in str(info.value)


def test_ut06_83_text_too_long_for_one_part_counts_as_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT06-83 a pack JSON longer than one TextPart allows is over the cap without a count."""
    calls: list[int] = []

    def counting(*_args: object) -> tuple[int, bool]:
        calls.append(1)
        return 0, True

    monkeypatch.setattr(hybrid, "count_tokens", counting)
    rows = [{"candidate_id": "team-core", "blob": "x" * 1000} for _ in range(250)]
    inp = _inp(levers=rows)
    with pytest.raises(BudgetExceeded):
        _pack([_finding(1)], inp=inp)
    assert calls == []


def test_ut06_83_redaction_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT06-83 claim, note or row text that cannot be redacted is dropped, never sent raw."""
    real = r.redact_text

    def failing(text: str | None) -> str | None:
        return None if text is not None and "BLOCK" in text else real(text)

    monkeypatch.setattr(hybrid, "redact_text", failing)
    bad_claim = _finding(2, claim="BLOCK claim about Core")
    bad_note = _finding(3, challenge=[_challenge(_fid(3), "BLOCK note")])
    bad_key = _finding(
        4,
        numbers=[
            NumberRef(
                id="n1",
                value=1,
                unit="count",
                query_id=Q1,
                column="c",
                row_key={"k": "BLOCK"},
            )
        ],
    )
    inp = _inp(
        levers=[{"entity_id": "team-edge", "why": "BLOCK"}, {"entity_id": "team-core"}],
        unconfirmed_weights=["ok.key", "BLOCK.key"],
    )
    pack = _pack([_finding(1), bad_claim, bad_note, bad_key], inp=inp)
    assert [f["finding_id"] for f in pack["findings"]] == [_fid(1)]  # type: ignore[attr-defined]
    assert pack["levers"] == [{"entity_id": "team_002"}]
    assert pack["unconfirmed_weights"] == ["ok.key"]
    assert "BLOCK" not in canonical_json(pack)


def test_ut06_83_objective_redaction_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT06-83 an objective that cannot be redacted raises; the message holds no text."""
    monkeypatch.setattr(hybrid, "redact_text", lambda _text: None)
    with pytest.raises(RedactionFailed) as info:
        _pack([_finding(1)], spec=_spec(objective="Report on Payments Platform"))
    assert "Payments" not in str(info.value)
    assert "team_001" not in str(info.value)


def test_ut06_83_redactor_sees_pseudonymized_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT06-83 names and ids are pseudonymized before the shared redactor sees the text."""
    seen: list[str] = []
    real = r.redact_text

    def spy(text: str | None) -> str | None:
        seen.append(str(text))
        return real(text)

    monkeypatch.setattr(hybrid, "redact_text", spy)
    _pack([_finding(1)])
    assert seen
    assert all("Payments Platform" not in t and "team-payments" not in t for t in seen)


def test_ut06_83_unknown_entity_finding_dropped() -> None:
    """UT06-83 a finding about an entity without a pseudonym is dropped, never sent raw."""
    pack = _pack([_finding(1), _finding(2, entity=("team", "team-unknown"))])
    assert [f["finding_id"] for f in pack["findings"]] == [_fid(1)]  # type: ignore[attr-defined]
    assert "team-unknown" not in canonical_json(pack)


def test_ut06_83_row_keys_int_ids_and_never_keys() -> None:
    """UT06-83 dict keys and int id-field values are pseudonymized; never-packed keys dropped."""
    pseudo = Pseudonymizer([*ENTITIES, ("work_item", "10042", None)])
    number = NumberRef(
        id="n1", value=1, unit="count", query_id=Q1, column="c",
        row_key={"team-core": "x", "wid": 10042, "week": 3},
    )  # fmt: skip
    inp = _inp(
        levers=[
            {
                "by_team": {"team-core": 3, "Payments Platform": 4},
                "entity_id": 10042,
                "score": 10042,
                "ok": True,
                "result_sample": [{"s": PLANTED["sample"]}],
                "nested": [{"text_redacted": PLANTED["enrich"], "notes": PLANTED["notes"]}],
            }
        ],
        portfolio={
            "rows": [{"candidate_id": "team-core", "prior_context": PLANTED["prior"]}],
            "selected": [{"ticket_text": PLANTED["ticket"]}],
        },
    )
    pack = build_evidence_pack(
        _spec(), findings=[_finding(1, numbers=[number])], inp=inp, pseudo=pseudo,
        max_tokens=BIG, cfg=CFG,
    )  # fmt: skip
    row_key = pack["findings"][0]["numbers"][0]["row_key"]  # type: ignore[index]
    assert row_key == {"team_002": "x", "wid": "work_item_001", "week": 3}
    assert pack["levers"] == [
        {
            "by_team": {"team_002": 3, "team_001": 4},
            "entity_id": "work_item_001",
            "score": 10042,
            "ok": True,
            "nested": [{}],
        }
    ]
    assert pack["portfolio"]["rows"] == [{"candidate_id": "team_002"}]  # type: ignore[index]
    assert pack["portfolio"]["selected"] == [{}]  # type: ignore[index]
    text = canonical_json(pack)
    for planted in PLANTED.values():
        assert planted not in text
    for key in ("result_sample", "text_redacted", "notes", "prior_context", "ticket_text"):
        assert key not in text


def test_ut06_83_missing_or_malformed_inputs_give_empty_sections() -> None:
    """UT06-83 inp without portfolio, levers or flags yields empty, well-formed sections."""
    pack = _pack([], inp={"portfolio": "bad", "levers": None, "dq_warnings": [1, {"x": 2}]})
    assert pack["findings"] == []
    assert pack["portfolio"] == {"scenario": "", "rows": [], "selected": [], "query_ids": []}
    assert pack["levers"] == []
    assert pack["dq_checks"] == []
    assert pack["unconfirmed_weights"] == []


# --- UT06-84 Pseudonymizer ---------------------------------------------------------------------

OVERLAP = [
    ("team", "t-1", "Core"),
    ("team", "t-2", "Core Platform"),
    ("service", "s-1", "Platform"),
    ("team", "t-3", None),
]


def test_ut06_84_tokens_stable_by_input_order() -> None:
    """UT06-84 tokens count from 1 per type in input order; equal input, equal tokens."""
    a, b = Pseudonymizer(OVERLAP), Pseudonymizer(list(OVERLAP))
    assert [a.token(t, i) for t, i, _ in OVERLAP] == ["team_001", "team_002", "service_001",
                                                      "team_003"]  # fmt: skip
    assert a.mapping() == b.mapping()
    dup = Pseudonymizer([*OVERLAP, ("team", "t-1", "Other")])
    assert dup.mapping() == a.mapping()


def test_ut06_84_case_variants_pseudonymized_restore_canonical() -> None:
    """UT06-84 names and ids match in any case on the way out; restore gives the canonical form."""
    p = Pseudonymizer(OVERLAP)
    out = p.pseudonymize("core platform CORE PLATFORM core Core T-3 cOrE")
    assert out == "team_002 team_002 team_001 team_001 team_003 team_001"
    assert p.restore(out) == "Core Platform Core Platform Core Core t-3 Core"
    assert p.restore("TEAM_001") == "TEAM_001"  # restore stays case-sensitive
    first = Pseudonymizer([("team", "a-1", "Core"), ("team", "a-2", "CORE")])
    assert first.pseudonymize("CORE core") == "team_001 team_001"  # first entity wins


def test_ut06_84_longest_first_whole_token_replacement() -> None:
    """UT06-84 overlapping names: the longest wins; partial-word matches are left alone."""
    p = Pseudonymizer(OVERLAP)
    text = "Core Platform owns Platform; Core and t-3 too. Coregate t-1x Platforms"
    out = p.pseudonymize(text)
    assert out == "team_002 owns service_001; team_001 and team_003 too. Coregate t-1x Platforms"
    assert p.pseudonymize("t-1 and t-2") == "team_001 and team_002"


def test_ut06_84_round_trip_and_restore_rules() -> None:
    """UT06-84 prose restores to names (id when unnamed); id fields and row_key restore to ids."""
    p = Pseudonymizer(OVERLAP)
    text = "Core Platform owns Platform; Core and t-3 too."
    assert p.restore(p.pseudonymize(text)) == text
    assert p.restore("team_001 and team_009") == "Core and team_009"
    obj = {
        "entity_id": "team_002",
        "target_id": "service_001",
        "row_key": {"team_id": "team_001", "n": 3},
        "text": "team_002 beat team_003",
        "items": [{"entity_id": "team_003", "claim": "team_001 grew"}, 4, None],
        "pair": ("team_001", "x"),
    }
    assert p.restore_obj(obj) == {
        "entity_id": "t-2",
        "target_id": "s-1",
        "row_key": {"team_id": "t-1", "n": 3},
        "text": "Core Platform beat t-3",
        "items": [{"entity_id": "t-3", "claim": "Core grew"}, 4, None],
        "pair": ["Core", "x"],
    }


def test_ut06_84_unknown_entity_and_empty_map() -> None:
    """UT06-84 token of an unknown entity raises NotFound; an empty map changes nothing."""
    p = Pseudonymizer([])
    with pytest.raises(NotFound):
        p.token("team", "t-1")
    assert p.pseudonymize("Core t-1") == "Core t-1"
    assert p.restore("team_001") == "team_001"
    assert p.restore_obj({"entity_id": "team_001"}) == {"entity_id": "team_001"}
    assert p.mapping() == {}


# --- PT06-06 round trip -------------------------------------------------------------------------

_NAME_WORD = st.text(alphabet="abcdefghijABCDEFGHIJ", min_size=1, max_size=6)
_FILLER = st.text(alphabet="klmnopqrstuvwxyzKLMNOPQRSTUVWXYZ", min_size=1, max_size=6)
_NAME = st.lists(_NAME_WORD, min_size=1, max_size=3).map(" ".join)
_ID = st.from_regex(r"[a-z]{1,4}-[0-9]{1,3}", fullmatch=True)


@st.composite
def _world(draw: st.DrawFn) -> tuple[list[tuple[str, str, str | None]], str]:
    ids = draw(st.lists(_ID, min_size=1, max_size=6, unique=True))
    names = draw(st.lists(_NAME, min_size=len(ids), max_size=len(ids), unique_by=str.lower))
    named = draw(st.lists(st.booleans(), min_size=len(ids), max_size=len(ids)))
    entities: list[tuple[str, str, str | None]] = [
        (draw(st.sampled_from(["team", "service"])), eid, name if keep else None)
        for eid, name, keep in zip(ids, names, named, strict=True)
    ]
    # Prose restores a token to the name, so a named entity appears by name and an unnamed one
    # by id (U06-124 step 3). Matching ignores case, so names are unique ignoring case and filler
    # letters are disjoint from name letters; ids hold a digit, so filler never forms an id.
    pieces = [name if name is not None else eid for _t, eid, name in entities]
    text = " ".join(draw(st.lists(st.sampled_from(pieces) | _FILLER, max_size=12)))
    return entities, text


def _whole(raw: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(raw)}(?!\w)", re.IGNORECASE)


@given(_world())
def test_pt06_06_restore_inverts_pseudonymize_and_hides_raw(
    world: tuple[list[tuple[str, str, str | None]], str],
) -> None:
    """PT06-06 restore(pseudonymize(x)) == x; no raw id or name, in any case, survives."""
    entities, text = world
    p = Pseudonymizer(entities)
    out = p.pseudonymize(text)
    assert p.restore(out) == text
    assert p.pseudonymize(text.upper()).lower() == p.pseudonymize(text.lower())
    for _t, eid, name in entities:
        assert _whole(eid).search(out) is None
        assert name is None or _whole(name).search(out) is None


@given(_world())
def test_pt06_06_id_fields_round_trip(world: tuple[list[tuple[str, str, str | None]], str]) -> None:
    """PT06-06 id-valued fields restore to the entity id, named or not."""
    entities, _text = world
    p = Pseudonymizer(entities)
    for _t, eid, _name in entities:
        sent = {"entity_id": p.pseudonymize(eid), "row_key": {"k": p.pseudonymize(eid)}}
        assert eid not in str(sent)
        assert p.restore_obj(sent) == {"entity_id": eid, "row_key": {"k": eid}}


def test_ut06_83_module_builds_no_client() -> None:
    """UT06-83 hybrid.py imports no HTTP or LLM SDK and builds no client (count_tokens only)."""
    tree = ast.parse(Path(hybrid.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    sdk = {"httpx", "httpx2", "anthropic", "openai"}
    assert not {m for m in imported if m.split(".")[0] in sdk or m.endswith("client")}
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not {c for c in calls if c.endswith("Client")}
