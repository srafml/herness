"""Security tests for herness.harness.tracing (ST05-14 tracer part; TH05-14).

The end-to-end scripted run of ST05-14 needs `run_agent` (T05-22) and the impl 11 scripted-run
fixtures; this module covers the tracer part: every sentinel reaches the Tracer through the
`llm_call_fields` / `tool_call_fields` payloads and plain fields, and none reaches the file.
The secret is built at runtime so the detect-secrets baseline does not change.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import redact as r
from herness.core import secrets
from herness.core.logging import configure_logging, reset_logging
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.core.types import (
    LLMRequest,
    LLMResponse,
    Message,
    ReasoningPart,
    RequestMeta,
    SystemBlock,
    TextPart,
    ToolCall,
    ToolResult,
    Usage,
)
from herness.harness.tracing import Tracer, llm_call_fields, tool_call_fields

pytestmark = pytest.mark.unit

RUN_ID = "run_01J8ST05140000000000000000"
EMAIL = "sentinel.person@example.org"
NAME = "Jane Doe"
PLANTED = "Zq" + "9xT4" + "mW2pLk" + "Sentinel7"  # built at runtime (detect-secrets)
OPAQUE = "opaque-block-" + "sentinel"  # built at runtime (detect-secrets)


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    c.reset_config()
    yield
    c.reset_config()
    reset_logging()


def _request(ticket: str) -> LLMRequest:
    memory = f"memory: {NAME} asked; token {PLANTED}"
    return LLMRequest(
        client="local-30b",
        system=[SystemBlock(text=f"context {memory}")],
        messages=[
            Message(role="user", parts=[TextPart(text=ticket)]),
            Message(
                role="assistant",
                parts=[
                    ReasoningPart(text="plan", provider="anthropic", opaque={"sig": OPAQUE}),
                    TextPart(text=memory),
                ],
            ),
        ],
        max_output_tokens=100,
        timeout_s=10.0,
        metadata=RequestMeta(
            run_id=RUN_ID,
            task_id="task_1",
            role="analyst",
            model_role="main",
            step=1,
            request_key="task_1:1:main",
        ),
    )


def _response(ticket: str) -> LLMResponse:
    return LLMResponse(
        text=f"summary of: {ticket}",
        tool_calls=[ToolCall(id="c1", name="search_tickets", arguments={"q": ticket})],
        parsed=None,
        reasoning=[ReasoningPart(text="r", provider="anthropic", opaque={"sig": OPAQUE})],
        stop_reason="tool_use",
        raw_stop_reason="tool_use",
        refusal_category=None,
        usage=Usage(input_tokens=1),
        cost_usd=Decimal(0),
        client="local-30b",
        model="m",
        provider="anthropic",
        latency_ms=1,
        request_id="req-1",
    )


def test_st05_14_sentinels_and_opaque_absent_from_trace_and_logs(
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ST05-14 sentinel email, name and secret in ticket text and memory, payload rate 1.0:
    absent from the trace file and from logs above DEBUG; no `opaque` key in the trace."""
    fake_keyring.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()
    fake_keyring.store[("herness", "vllm.api_key")] = PLANTED
    c.init_config("local", config_dir=write_full_config(tmp_path), env={})
    configure_logging("INFO", scrubber=secrets.scrub_secrets)
    secrets.resolve("secret:vllm.api_key")  # the secret joins known_values
    assert PLANTED in secrets.known_values()
    directory = NameDirectory.from_files(None, (NAME,), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)

    tracer = Tracer.for_run(RUN_ID, build_id=None, run_kind="eval")
    view = tracer.bind(task_id="task_1", role="analyst")
    assert view.is_sampled("task_1")  # eval rate 1.0
    ticket = f"Printer broken, contact {EMAIL} ({NAME}); password={PLANTED}"
    fields, payload = llm_call_fields(
        _request(ticket), _response(ticket), prompt_hash="ph", gate_wait_ms=0
    )
    view.emit("llm_call", payload=payload, **fields)
    call = ToolCall(id="c1", name="search_tickets", arguments={"q": ticket, "n": {"s": PLANTED}})
    tfields, tpayload = tool_call_fields(call, ToolResult(ok=True, content="ok"))
    view.emit("tool_call", payload=tpayload, **tfields)
    view.emit("budget", kind="warn", used={}, limit={}, message=f"note {PLANTED}")
    tracer.close()

    path = tmp_path / "data" / "traces" / f"{RUN_ID}.jsonl"
    text = path.read_text(encoding="utf-8")
    assert len(text.splitlines()) == 3
    assert '"payload"' in text  # payloads were sampled and written
    for sentinel in (EMAIL, NAME, PLANTED, OPAQUE):
        assert sentinel not in text
    assert '"opaque"' not in text
    logs = "".join(capsys.readouterr())
    for sentinel in (EMAIL, NAME, PLANTED, OPAQUE):
        assert sentinel not in logs
