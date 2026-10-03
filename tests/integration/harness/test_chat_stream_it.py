"""Integration test IT05-11 (flow F05-12, chat streaming turn) (T05-27).

A chat-style run of the real loop: `HarnessHooks` with `on_text_delta`, a real spec 08
`ModelChain` (retry under the `llm_local` policy, no-op sleeps of `reset_process_state`), the
real `OpenAICompatClient` streaming from a loopback `FakeLLMServer`
(`tests.support.impostor_llm.TamperingLLMServer`) and the real `run_sql` tool on a tmp
stand-in warehouse. The script `chat_stream_500.yaml` injects a 500 after the first tokens of
the answer: the UI gets the reset (`on_text_delta(None)`) before the retried stream, and the
final text is the whole answer exactly once.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from tests.support import loop_standin as ls
from tests.support import warehouse_tools_build as wb
from tests.support.dispatch_standin import use_test_config
from tests.support.harness_fakes import FakeOps, RecordingTracer
from tests.support.impostor_llm import TamperingLLMServer
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core import redact as r
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ModelChain, ProcessState, bind_ops_backend
from herness.core.settings import RedactionConfig
from herness.core.types import AgentResult
from herness.eval.scripted import load_scripts
from herness.harness import hooks as h
from herness.harness import loop
from herness.harness import warehouse_tools as wt
from herness.harness.llm.openai_compat import OpenAICompatClient
from herness.harness.llm.settings import ClientConfig
from herness.harness.warehouse import DuckWarehouse
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tests" / "fixtures" / "llm_scripts" / "chat_stream_500.yaml"
ANSWER = "There are 40 incidents in the build, spread over four services."
PARTIAL = ANSWER[:32]  # two 16-char deltas reach the UI before the 500
CLIENT = "local-30b"
_ZERO = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}


@dataclass
class _Info:
    off_network: bool = False
    gpu_class: str | None = None
    timeout_s: float = 30.0
    model: str = "qwen3-30b"
    base_url: str | None = None
    api_key: str | None = None


class _Registry:
    """The spec 05 registry as the chain sees it: one on-network client."""

    def __init__(self, client: OpenAICompatClient) -> None:
        self._client = client

    def chain_for(self, model_role: str, depth: str) -> list[str]:
        del model_role, depth
        return [CLIENT]

    def config(self, name: str) -> _Info:
        del name
        return _Info()

    def client(self, name: str) -> OpenAICompatClient:
        assert name == CLIENT
        return self._client


class _Gpu:
    def loaded_class(self) -> str:
        return "reasoning"

    def service_healthy(self, name: str) -> bool:
        del name
        return True


@pytest.fixture
def wh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
) -> Iterator[DuckWarehouse]:
    """Migrated ops store as resilience backend, test config, prompts, a test redactor, the
    warehouse tools on the opened tmp stand-in build."""
    del ops_store, reset_process_state
    use_test_config(tmp_path / "cfg")
    bind_ops_backend(SqliteResilienceBackend())
    ls.write_prompts(tmp_path / "prompts", monkeypatch)
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    wb.patch_redaction(monkeypatch)
    wt.register_warehouse_tools()
    with wb.opened(tmp_path / "wh") as handle:
        yield handle
    c.reset_config()


def _cfg(server: TamperingLLMServer) -> ClientConfig:
    return ClientConfig.model_validate(
        {
            "name": CLIENT, "kind": "openai_compat", "base_url": f"{server.base_url}/v1",
            "model": "qwen3-30b", "context_window": 32768, "max_output_tokens": 4096,
            "tokenizer": "estimate", "max_concurrency": 4, "price_per_mtok": _ZERO,
            "server": "vllm",
        }
    )  # fmt: skip


def _chat_run(
    wh: DuckWarehouse, server: TamperingLLMServer
) -> tuple[AgentResult, list[str | None], RecordingTracer]:
    deltas: list[str | None] = []

    async def on_text_delta(text: str | None) -> None:
        deltas.append(text)

    cfg = _cfg(server)
    client = OpenAICompatClient(cfg)
    registry: Any = _Registry(client)
    chain = ModelChain("analyst", registry=registry, depth="standard", gpu=_Gpu())
    tracer = RecordingTracer("run_1", "task_1")
    hooks = h.HarnessHooks(
        registry=registry, gates={}, chain=chain, compactor=None, task_id=None, phase=None,
        stop=None, on_text_delta=on_text_delta, tracer=tracer,  # type: ignore[arg-type]
    )  # fmt: skip
    ctx = ls.loop_ctx(tool_names=("run_sql",), warehouse=wh, ops=FakeOps(), tracer=tracer)
    role = ls.demo_role(tools=("run_sql",), output_model=None)  # a chat answer is text
    result = asyncio.run(loop.run_agent(role, {"q": "how many?"}, ctx, client, cfg, hooks))
    return result, deltas, tracer


def test_it05_11_stream_500_after_first_tokens_resets_then_final_text(
    wh: DuckWarehouse,
) -> None:
    """IT05-11 `FakeLLMServer` streaming with an injected 500 after the first tokens, a
    chat-style run: the first tokens reach the UI, then the reset (`None`) before the retried
    stream, which delivers the whole answer; the final text is the answer exactly once."""
    assert load_scripts(SCRIPT).scripts[0].faults[0].kind == "http_500"
    with TamperingLLMServer(SCRIPT, partial_text=PARTIAL) as server:
        result, deltas, tracer = _chat_run(wh, server)
        served = server.completed
    assert result.status == "completed"
    assert result.output == {"text": ANSWER}
    assert server.cut_streams == 1
    assert served == 3
    assert deltas.count(None) == 1
    reset = deltas.index(None)
    before, after = deltas[:reset], deltas[reset + 1 :]
    assert "".join(t for t in before if t is not None) == PARTIAL
    assert "".join(t for t in after if t is not None) == ANSWER
    assert len(result.query_ids) == 1  # the run_sql step ran on the tmp warehouse
    retries = [e for e in tracer.events if e[0] == "retry"]
    assert len(retries) == 1
    llm_calls = [fields for kind, _span, fields in tracer.events if kind == "llm_call"]
    assert [call["stop_reason"] for call in llm_calls] == ["tool_use", "end_turn"]


def test_it05_11_without_fault_no_reset(wh: DuckWarehouse) -> None:
    """IT05-11 control: the same chat run without the injected 500 streams the answer once and
    never sends the reset."""
    (script,) = load_scripts(SCRIPT).scripts
    book_path = wh.path.parent / "no_fault.yaml"
    plain = [script.model_dump(exclude={"source", "faults"}, mode="json")]
    book_path.write_text(json.dumps(plain), encoding="utf-8")
    with TamperingLLMServer(book_path) as server:
        result, deltas, _tracer = _chat_run(wh, server)
    assert result.output == {"text": ANSWER}
    assert None not in deltas
    assert "".join(t for t in deltas if t is not None) == ANSWER
    assert server.cut_streams == 0
