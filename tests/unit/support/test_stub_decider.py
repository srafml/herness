"""Tests for tests.support.stub_decider.StubDeciderServer (U11-46; UT11-75, UT11-76, T11-24).

The spec 03 §3.3 fixture shapes are what ``jev_wire.parse_wire_answers`` (U03-50) accepts
(recorded fixtures under ``tests/fixtures/openjev`` do not exist: group ruling), so every
response is validated through it, and one test drives the registered ``OpenJevDecider``
end to end over real loopback HTTP.
"""

from __future__ import annotations

import hashlib
import json
import math
import socket
from pathlib import Path
from typing import Any

import httpx2
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.support.stub_decider import (
    MODEL_ID,
    StubDeciderServer,
    StubFault,
    content_hash_of,
)
from tests.unit.enrich._openjev_support import jev_env

from herness.core import egress, registry
from herness.core.errors import OutputValidationError
from herness.core.resilience import ProcessState
from herness.core.types import DecisionInput, Question, QuestionSet
from herness.enrich.deciders import register_deciders
from herness.enrich.deciders.jev_wire import (
    MAX_BODY_BYTES,
    load_wire_body,
    parse_wire_answers,
    to_wire_questions,
)
from herness.enrich.settings import OpenJevSettings

pytestmark = pytest.mark.unit

__all__ = ["jev_env"]  # the fixture is used by name

_INSTR = "Classify the ticket text."
BOOL = Question(id="change_caused", type="bool", instructions=_INSTR, threshold=0.8)
CHOICE = Question(
    id="root_cause",
    type="choice",
    instructions=_INSTR,
    options={"network": "Network", "database": "Database", "config": "Configuration"},
    threshold=0.8,
)
SCORE = Question(
    id="business_impact",
    type="score",
    instructions=_INSTR,
    levels=("none", "minor", "major", "critical"),
    threshold=0.8,
)
QUESTIONS = (BOOL, CHOICE, SCORE)
STATE = "Checkout fails with a timeout after the [REDACTED:NAME] database failover."
TRUTH = {"change_caused": "false", "root_cause": "database", "business_impact": "2"}


def _write_labels(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    """A ``truth_labels.parquet`` with the U11-20 columns (content_hash, question, answer)."""
    table = pa.table(
        {
            "record_id": [f"servicenow:incident:{n:032x}" for n in range(len(rows))],
            "content_hash": [row[0] for row in rows],
            "question": [row[1] for row in rows],
            "answer": [row[2] for row in rows],
            "pii_spans": ["[]"] * len(rows),
        }
    )
    pq.write_table(table, path)
    return path


@pytest.fixture
def labels(tmp_path: Path) -> Path:
    chash = content_hash_of(STATE)
    rows = [(chash, qid, answer) for qid, answer in TRUTH.items()]
    return _write_labels(tmp_path / "truth_labels.parquet", rows)


def _request(state: str = STATE, questions: tuple[Question, ...] = QUESTIONS) -> dict[str, Any]:
    """The request body ``OpenJevDecider`` sends (U03-53), questions via ``to_wire_questions``."""
    return {
        "model": MODEL_ID,
        "state": state,
        "questions": to_wire_questions(questions),
        "steps": 1,
        "think": 0,
    }


def _client(server: StubDeciderServer) -> httpx2.Client:
    return egress.loopback_http_client(server.base_url, timeout_s=10)


def _post(server: StubDeciderServer, body: dict[str, Any] | None = None) -> httpx2.Response:
    with _client(server) as client:
        return client.post("/v1/systemone", json=_request() if body is None else body)


def _parsed(resp: httpx2.Response, questions: tuple[Question, ...] = QUESTIONS) -> Any:
    assert resp.status_code == 200
    data = load_wire_body(resp.content)
    return parse_wire_answers(data["answers"], questions)  # type: ignore[arg-type]


def _refused(server: StubDeciderServer) -> bool:
    try:
        with socket.create_connection(server.server_address, timeout=10):
            return False
    except ConnectionRefusedError:
        return True


# --- UT11-75: oracle mode --------------------------------------------------------------------


@pytest.mark.parametrize("noise", [0.0, 0.1, 0.3])
def test_ut11_75_oracle_answer_equals_truth_with_probability_1_minus_noise(
    labels: Path, noise: float
) -> None:
    """UT11-75 oracle + labels file: the answer equals truth with probability 1 - noise."""
    with StubDeciderServer("oracle", truth_labels=labels, noise=noise) as server:
        resp = _post(server)
    answers = _parsed(resp, QUESTIONS)
    for qid, truth in TRUTH.items():
        assert answers[qid].answer == truth
        assert answers[qid].distribution[truth] == pytest.approx(1 - noise)
    data = resp.json()
    assert data["model"] == MODEL_ID
    assert data["usage"] == {"input_tokens": len(STATE) // 4, "output_tokens": 0}


def test_ut11_75_oracle_wire_fields_and_noise_spread(labels: Path) -> None:
    """UT11-75 noul is P(true); choice/score spread noise/(K-1); confidence 1 - H(p)/ln K."""
    noise = 0.3
    with StubDeciderServer("oracle", truth_labels=labels, noise=noise) as server:
        raw = _post(server).json()["answers"]
    assert raw["change_caused"] == {"noul": pytest.approx(noise)}
    choice = raw["root_cause"]
    assert choice["choice"] == "database"
    assert choice["probabilities"] == pytest.approx(
        {"network": 0.15, "database": 0.7, "config": 0.15}
    )
    entropy = -sum(p * math.log(p) for p in choice["probabilities"].values())
    assert choice["confidence"] == pytest.approx(1 - entropy / math.log(3))
    score = raw["business_impact"]
    assert score["probabilities"] == pytest.approx({"0": 0.1, "1": 0.1, "2": 0.7, "3": 0.1})
    assert score["score"] == pytest.approx(0 * 0.1 + 1 * 0.1 + 2 * 0.7 + 3 * 0.1)
    assert score["legend"] == ["none", "minor", "major", "critical"]
    assert set(score) == {"score", "legend", "probabilities", "confidence"}


def test_ut11_75_oracle_without_label_falls_back_to_hash(labels: Path) -> None:
    """UT11-75 oracle: a (content_hash, question) with no truth label answers like hash mode."""
    body = _request(state="an unlabelled ticket")
    with StubDeciderServer("oracle", truth_labels=labels) as oracle:
        from_oracle = _post(oracle, body).content
    with StubDeciderServer("hash") as hashed:
        from_hash = _post(hashed, body).content
    assert from_oracle == from_hash


def test_ut11_75_openjev_decider_end_to_end(jev_env: ProcessState, labels: Path) -> None:
    """UT11-75 the registered OpenJevDecider (T03-11 Protocol) gets the truth from the stub."""
    del jev_env
    register_deciders()
    cls = registry.get("decider", "openjev")
    item = DecisionInput(
        record_id="servicenow:incident:1",
        entity="incident",
        content_hash=content_hash_of(STATE),
        text=STATE,
        question_ids=tuple(TRUTH),
    )
    qs = QuestionSet(version="qs-2026-10-01.1", questions=QUESTIONS)
    with StubDeciderServer("oracle", truth_labels=labels, noise=0.1) as server:
        settings = OpenJevSettings(base_url=server.base_url, concurrency=2, timeout_s=10)
        decider = cls(settings, api_key=None, image_tag="0.4.0", samples=None)
        decider.health()  # GET /v1/models lists openjev-latest
        (out,) = decider.decide([item], qs)
    assert out.error is None
    assert {qid: a.answer for qid, a in out.answers.items()} == TRUTH
    assert all(a.probability == pytest.approx(0.9) for a in out.answers.values())


def test_ut11_75_models_listing(labels: Path) -> None:
    """UT11-75 GET /v1/models lists openjev-latest; unknown routes answer 404."""
    with StubDeciderServer("oracle", truth_labels=labels) as server, _client(server) as client:
        assert client.get("/v1/models").json() == {
            "object": "list",
            "data": [{"id": "openjev-latest"}],
        }
        assert client.get("/v1/other").status_code == 404
        assert client.get("/v1/systemone").status_code == 404
        assert server.calls == 0  # only POST /v1/systemone counts toward fault indexes


def test_ut11_75_body_over_1_mb_is_rejected() -> None:
    """UT11-75 a request body over 1 MB answers 413; one of exactly 1 MB is served."""
    with StubDeciderServer("hash") as server, _client(server) as client:
        big = client.post("/v1/systemone", content=b" " * (MAX_BODY_BYTES + 1))
        assert big.status_code == 413
        body = json.dumps(_request()).encode()
        padded = body + b" " * (MAX_BODY_BYTES - len(body))
        assert client.post("/v1/systemone", content=padded).status_code == 200


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"[]",
        json.dumps({"model": MODEL_ID, "questions": {}}).encode(),
        json.dumps({"state": "s", "questions": []}).encode(),
        json.dumps({"state": "s", "questions": {"q": {"type": "text"}}}).encode(),
        json.dumps({"state": "s", "questions": {"q": {"type": "choice", "criteria": {}}}}).encode(),
        json.dumps({"state": "s", "questions": {"q": "noul"}}).encode(),
    ],
)
def test_ut11_75_malformed_requests_answer_400(content: bytes) -> None:
    """UT11-75 a body that is not a Jev-shape request answers 400."""
    with StubDeciderServer("hash") as server, _client(server) as client:
        assert client.post("/v1/systemone", content=content).status_code == 400


def test_ut11_75_preconditions_raise_before_binding(tmp_path: Path) -> None:
    """UT11-75 oracle needs truth_labels; noise must be in [0, 1); unknown modes are refused."""
    with pytest.raises(ValueError, match="truth_labels"):
        StubDeciderServer("oracle")
    with pytest.raises(ValueError, match="truth_labels"):
        StubDeciderServer()
    for noise in (-0.01, 1.0, 1.5, math.nan):
        with pytest.raises(ValueError, match="noise"):
            StubDeciderServer("hash", noise=noise)
    with pytest.raises(ValueError, match="mode"):
        StubDeciderServer("random")  # type: ignore[arg-type]
    with pytest.raises(FileNotFoundError):
        StubDeciderServer("oracle", truth_labels=tmp_path / "missing.parquet")


def test_ut11_75_binds_127_0_0_1_only() -> None:
    """UT11-75 TH11-10: the stub listens on 127.0.0.1; another bind host is refused."""

    class _Wide(StubDeciderServer):
        bind_host = "0.0.0.0"  # noqa: S104 - the refused case under test

    with StubDeciderServer("hash") as server:
        assert server.server_address[0] == "127.0.0.1"
        assert server.base_url.startswith("http://127.0.0.1:")
    with pytest.raises(PermissionError, match=r"127\.0\.0\.1"):
        _Wide("hash")


def test_ut11_75_registers_the_openjev_service(stub_services: Path) -> None:
    """UT11-75 service_name defaults to openjev (FT11-03 kill_service:openjev)."""
    with StubDeciderServer("hash") as server:
        assert json.loads(stub_services.read_text("utf-8")) == {"openjev": server.base_url}


# --- UT11-76: hash mode and faults ------------------------------------------------------------


def test_ut11_76_faults_429_and_529_then_identical_answers() -> None:
    """UT11-76 hash, faults 429 at 0 and 529 at 1: 4 calls; 2 and 3 identical for one state."""
    faults = (StubFault(0, "http_429"), StubFault(1, "http_529"))
    with StubDeciderServer("hash", faults=faults) as server, _client(server) as client:
        resps = [client.post("/v1/systemone", json=_request()) for _ in range(4)]
    assert [r.status_code for r in resps] == [429, 529, 200, 200]
    assert resps[0].headers["retry-after"] == "1"
    assert resps[2].content == resps[3].content
    _parsed(resps[2])
    assert server.calls == 4


def test_ut11_76_hash_answer_follows_the_formula() -> None:
    """UT11-76 index int(h[0:8]) mod K; top 0.5 + 0.5 * int(h[8:16]) / 0xFFFFFFFF; rest even."""
    chash = hashlib.sha256(STATE.encode()).hexdigest()[:32]
    assert content_hash_of(STATE) == chash
    with StubDeciderServer("hash") as server:
        answers = _parsed(_post(server))
    options = {
        "change_caused": ("true", "false"),
        "root_cause": ("network", "database", "config"),
        "business_impact": ("0", "1", "2", "3"),
    }
    for qid, labels in options.items():
        h = hashlib.sha256(f"{chash}:{qid}".encode()).hexdigest()
        index = int(h[0:8], 16) % len(labels)
        top = 0.5 + 0.5 * int(h[8:16], 16) / 0xFFFFFFFF
        rest = (1 - top) / (len(labels) - 1)
        expected = {label: top if n == index else rest for n, label in enumerate(labels)}
        assert answers[qid].distribution == pytest.approx(expected)
        assert answers[qid].answer == labels[index]


def test_ut11_76_same_state_same_bytes_across_instances(labels: Path) -> None:
    """UT11-76 determinism: identical responses from separate server instances."""
    for mode in ("hash", "oracle"):
        bodies = []
        for _ in range(2):
            with StubDeciderServer(mode, truth_labels=labels, noise=0.2) as server:
                bodies.append(_post(server).content)
        assert bodies[0] == bodies[1]


def test_ut11_76_malformed_json_fault() -> None:
    """UT11-76 malformed_json: 200 with body '{"answers": ', which the parser refuses."""
    faults = [StubFault(0, "malformed_json")]
    with StubDeciderServer("hash", faults=faults) as server, _client(server) as client:
        bad = client.post("/v1/systemone", json=_request())
        good = client.post("/v1/systemone", json=_request())
    assert (bad.status_code, bad.content) == (200, b'{"answers": ')
    with pytest.raises(OutputValidationError):
        load_wire_body(bad.content)
    _parsed(good)


def test_ut11_76_fault_count_covers_consecutive_calls() -> None:
    """UT11-76 StubFault(count=2) serves the fault for two consecutive request indexes."""
    faults = [StubFault(1, "http_529", count=2)]
    with StubDeciderServer("hash", faults=faults) as server, _client(server) as client:
        codes = [client.post("/v1/systemone", json=_request()).status_code for _ in range(4)]
    assert codes == [200, 529, 529, 200]


def test_ut11_76_kill_fault_drops_the_call_and_refuses_connects() -> None:
    """UT11-76 kill at 1: call 0 served, call 1 gets no response, later connects refused."""
    server = StubDeciderServer("hash", faults=[StubFault(1, "kill")])
    server.start()
    try:
        with _client(server) as client:
            assert client.post("/v1/systemone", json=_request()).status_code == 200
            with pytest.raises(httpx2.TransportError):
                client.post("/v1/systemone", json=_request())
        assert _refused(server)
    finally:
        server.stop()
