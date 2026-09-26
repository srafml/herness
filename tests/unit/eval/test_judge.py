"""Tests for herness.eval.judge.RubricJudge: cache, request, repair and trace (U11-61)."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest
from tests.unit.eval._judge_fixtures import (
    CRITERIA,
    MODEL,
    FakeJudgeClient,
    make_judge,
    prompt_of,
    scores,
)

from herness.core.errors import ModelUnavailable, OutputValidationError
from herness.eval import judge as j
from herness.harness.llm.settings import TraceSettings
from herness.harness.tracing import Tracer

pytestmark = pytest.mark.unit

_RUN = "run_" + "1" * 26


def _key(question_id: str, text: str, min_score: float = 3.5, model: str = MODEL) -> str:
    body = {
        "model": model,
        "question_id": question_id,
        "rubric": {"criteria": list(CRITERIA), "min_score": min_score},
        "text": text,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_ut11_53_second_score_is_cached(tmp_path: Path) -> None:
    """UT11-53 scoring twice makes one client call; the second result has cached = true."""
    client = FakeJudgeClient(scores(4))
    judge = make_judge(client, tmp_path)
    first = judge.score("G07", CRITERIA, 3.5, "final answer")
    second = judge.score("G07", CRITERIA, 3.5, "final answer")
    assert client.calls == 1
    assert (first.cached, second.cached) == (False, True)
    assert first.scores == second.scores == {"clarity": 4, "actionability": 4}
    assert second.model == MODEL
    assert second.rationale == "clear and actionable"


def test_ut11_53_cache_file_and_key(tmp_path: Path) -> None:
    """UT11-53 the cache file is <key[:2]>/<key>.json with the spec key; it survives instances."""
    make_judge(FakeJudgeClient(scores(3)), tmp_path).score("G07", CRITERIA, 3.5, "text")
    key = _key("G07", "text")
    path = tmp_path / key[:2] / f"{key}.json"
    assert path.is_file()
    assert j.cache_key(MODEL, "G07", CRITERIA, 3.5, "text") == key
    other = FakeJudgeClient(scores(5))
    again = make_judge(other, tmp_path).score("G07", CRITERIA, 3.5, "text")
    assert other.calls == 0
    assert again.cached
    assert again.scores == {"clarity": 3, "actionability": 3}
    assert list(tmp_path.rglob("*.tmp")) == []


def test_ut11_53_key_inputs_miss_cache(tmp_path: Path) -> None:
    """UT11-53 other text, question, min_score or model each miss the cache."""
    client = FakeJudgeClient(scores(4))
    make_judge(client, tmp_path).score("G07", CRITERIA, 3.5, "a")
    make_judge(client, tmp_path).score("G07", CRITERIA, 3.5, "b")
    make_judge(client, tmp_path).score("G08", CRITERIA, 3.5, "a")
    make_judge(client, tmp_path).score("G07", CRITERIA, 4.0, "a")
    make_judge(client, tmp_path, model="judge-model-b").score("G07", CRITERIA, 3.5, "a")
    assert client.calls == 5


def test_ut11_53_corrupt_cache_is_a_miss(tmp_path: Path) -> None:
    """UT11-53 an unreadable or invalid cache file is ignored and rewritten."""
    key = _key("G07", "text")
    path = tmp_path / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True)
    for bad in ("{not json", json.dumps({"scores": {"clarity": 9}, "rationale": "", "model": "m"})):
        path.write_text(bad, encoding="utf-8")
        client = FakeJudgeClient(scores(2))
        result = make_judge(client, tmp_path).score("G07", CRITERIA, 3.5, "text")
        assert (client.calls, result.cached) == (1, False)
    assert json.loads(path.read_text(encoding="utf-8"))["scores"]["clarity"] == 2


def test_ut11_53_request_shape(tmp_path: Path) -> None:
    """UT11-53 the request carries the spec client, schema, metadata and temperature."""
    client = FakeJudgeClient(scores(4))
    make_judge(client, tmp_path).score("G07", CRITERIA, 3.5, "final answer")
    req = client.requests[0]
    key = _key("G07", "final answer")
    assert req.client == "local-judge"
    assert req.temperature == 0.0
    assert req.response_schema_name == "judge_scores"
    assert [m.role for m in req.messages] == ["user"]
    assert req.messages[0].parts[0].text == "Score the answer."  # type: ignore[union-attr]
    meta = req.metadata
    assert (meta.task_id, meta.role, meta.model_role, meta.step) == (
        None,
        "judge_eval",
        "eval_judge",
        0,
    )
    assert meta.request_key == f"judge:G07:{key[:8]}"
    schema = req.response_schema
    assert schema is not None
    props = schema["properties"]
    assert isinstance(props, dict)
    score_schema = props["scores"]
    assert isinstance(score_schema, dict)
    assert score_schema["required"] == list(CRITERIA)
    assert score_schema["properties"] == {
        c: {"type": "integer", "minimum": 1, "maximum": 5} for c in CRITERIA
    }
    assert props["rationale"] == {"type": "string", "maxLength": 500}
    prompt = prompt_of(req)
    assert "- clarity\n- actionability" in prompt
    assert "{{" not in prompt


def test_ut11_53_text_reply_without_parsed(tmp_path: Path) -> None:
    """UT11-53 a reply with no parsed output is parsed from its JSON text."""
    client = FakeJudgeClient(json.dumps(scores(5)))
    result = make_judge(client, tmp_path).score("G07", CRITERIA, 3.5, "x")
    assert result.scores == {"clarity": 5, "actionability": 5}


def test_ut11_53_concurrent_scores(tmp_path: Path) -> None:
    """UT11-53 threads scoring the same and different answers leave valid cache files."""
    client = FakeJudgeClient(scores(4))
    judge = make_judge(client, tmp_path)
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            judge.score("G07", CRITERIA, 3.5, f"text {i % 3}")
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    files = sorted(tmp_path.rglob("*.json"))
    assert len(files) == 3
    for file in files:
        assert json.loads(file.read_text(encoding="utf-8"))["model"] == MODEL
    assert list(tmp_path.rglob("*.tmp")) == []


def test_ut11_53_trace_event(tmp_path: Path) -> None:
    """UT11-53 a live call emits one llm_call event; a cache hit emits none."""
    tracer = Tracer(
        _RUN, build_id=None, run_kind="eval", traces_dir=tmp_path / "t", settings=TraceSettings()
    )
    client = FakeJudgeClient(scores(4))
    judge = make_judge(client, tmp_path / "c", tracer=tracer)
    judge.score("G07", CRITERIA, 3.5, "answer")
    judge.score("G07", CRITERIA, 3.5, "answer")
    tracer.close()
    lines = (tmp_path / "t" / f"{_RUN}.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    llm = [e for e in events if e["type"] == "llm_call"]
    assert len(llm) == 1
    assert llm[0]["role"] == "judge_eval"
    assert llm[0]["prompt_hash"] == j.judge_prompt_hash()
    assert llm[0]["response_schema_name"] == "judge_scores"
    assert client.requests[0].metadata.run_id == _RUN


def test_ut11_53_prompt_hash() -> None:
    """UT11-53 the prompt hash is the 16-hex SHA-256 prefix of the shipped prompt file."""
    raw = (Path(j.__file__).parent / "prompts" / "judge.md").read_bytes()
    assert j.judge_prompt_hash() == hashlib.sha256(raw).hexdigest()[:16]


def test_ut11_53_replace_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT11-53 a failed replace is ignored when another writer made the file, else raised."""
    judge = make_judge(FakeJudgeClient(scores(4)), tmp_path)
    key = _key("G07", "race")
    target = tmp_path / key[:2] / f"{key}.json"

    def busy(_src: str, _dst: str) -> None:
        if not target.exists():  # the concurrent writer lands its file first
            target.write_text(json.dumps({"scores": {}, "rationale": "", "model": MODEL}), "utf-8")
        msg = "target in use"
        raise PermissionError(msg)

    monkeypatch.setattr(j.os, "replace", busy)
    assert judge.score("G07", CRITERIA, 3.5, "race").cached is False
    target.unlink()
    monkeypatch.setattr(j.os, "replace", lambda _s, _d: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        judge.score("G07", CRITERIA, 3.5, "race")
    assert list(tmp_path.rglob("*.tmp")) == []


def test_ut11_54_repair_then_unavailable(tmp_path: Path) -> None:
    """UT11-54 a score of 7 twice makes one repair call, then ModelUnavailable."""
    client = FakeJudgeClient(scores(7), scores(7))
    judge = make_judge(client, tmp_path)
    with pytest.raises(ModelUnavailable):
        judge.score("G07", CRITERIA, 3.5, "answer")
    assert client.calls == 2
    first, repair = client.requests
    assert repair.metadata.request_key == first.metadata.request_key
    assert (first.metadata.step, repair.metadata.step) == (0, 1)
    assert len(repair.messages) == 2
    assert list(tmp_path.rglob("*.json")) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"scores": {"clarity": 4}, "rationale": "r"},
        {"scores": {"clarity": 4, "actionability": 0}, "rationale": "r"},
        {"scores": {"clarity": 4, "actionability": True}, "rationale": "r"},
        {"scores": {"clarity": 4, "actionability": "4"}, "rationale": "r"},
        {"scores": [4, 4], "rationale": "r"},
        {"scores": {"clarity": 4, "actionability": 4}, "rationale": "r" * 501},
        {"scores": {"clarity": 4, "actionability": 4}},
        "not json",
        "[1, 2]",
    ],
)
def test_ut11_54_invalid_then_repaired(bad: dict[str, object] | str, tmp_path: Path) -> None:
    """UT11-54 an invalid first reply is repaired once and the valid reply is cached."""
    client = FakeJudgeClient(bad, scores(3))
    result = make_judge(client, tmp_path).score("G07", CRITERIA, 3.5, "answer")
    assert client.calls == 2
    assert result.scores == {"clarity": 3, "actionability": 3}
    assert not result.cached


def test_ut11_54_client_validation_error_counts(tmp_path: Path) -> None:
    """UT11-54 an OutputValidationError from the client uses the one repair attempt."""
    msg = "schema"
    client = FakeJudgeClient(OutputValidationError(msg), OutputValidationError(msg))
    with pytest.raises(ModelUnavailable):
        make_judge(client, tmp_path).score("G07", CRITERIA, 3.5, "answer")
    assert client.calls == 2


def test_ut11_54_extra_criteria_dropped(tmp_path: Path) -> None:
    """UT11-54 scores for criteria not in the rubric are dropped, not graded."""
    reply = {"scores": {"clarity": 4, "actionability": 5, "numbers": 1}, "rationale": "r"}
    result = make_judge(FakeJudgeClient(reply), tmp_path).score("G07", CRITERIA, 3.5, "a")
    assert result.scores == {"clarity": 4, "actionability": 5}


def test_ut11_54_client_unavailable_propagates(tmp_path: Path) -> None:
    """UT11-54 ModelUnavailable from the client propagates without a repair call."""
    msg = "down"
    client = FakeJudgeClient(ModelUnavailable(msg))
    with pytest.raises(ModelUnavailable):
        make_judge(client, tmp_path).score("G07", CRITERIA, 3.5, "answer")
    assert client.calls == 1
