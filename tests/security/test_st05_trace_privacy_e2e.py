"""Security test ST05-14 (TH05-14), end-to-end half: a scripted run with personal data and a
secret in ticket text and memory leaves none of them in the trace file or the logs (T05-27).

The tracer half is tests/security/test_st05_tracing.py (T05-21). Here the real loop runs the
real `get_record` tool on the stand-in build of `tests.support.warehouse_read_build`, whose
redacted ticket text was planted with a sentinel email, name and secret (a redaction miss
upstream), and a spec 07 stand-in `recall_memory` returning a memory with the same sentinels.
The scripted model (`ListClient`) answers with reasoning carrying an `opaque` block and echoes
the sentinels in its text. The real `Tracer` writes the run's file at payload rate 1.0 (eval)
with the real `Redactor` (sentinel name in its directory); logs at INFO go through the secret
scrubber. The secret is built at runtime so the detect-secrets baseline does not change.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from pydantic import JsonValue
from tests.support import loop_standin as ls
from tests.support import warehouse_read_build as rb
from tests.support.config_tree import write_full_config
from tests.support.dispatch_standin import SyncTool, call, strict_schema
from tests.support.fake_keyring import MemoryKeyring
from tests.support.harness_fakes import FakeOps

from herness.core import config as c
from herness.core import redact as r
from herness.core import secrets
from herness.core.logging import configure_logging, get_logger, reset_logging
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.core.types import LLMResponse, ReasoningPart, ToolContext, ToolResult, Usage
from herness.harness import hooks as h
from herness.harness import loop
from herness.harness import warehouse_tools as wt
from herness.harness.llm.settings import SqlSettings
from herness.harness.tools import tool_registry
from herness.harness.tracing import Tracer
from herness.harness.warehouse import open_warehouse

pytestmark = pytest.mark.unit

RUN_ID = "run_01J8ST05140000000000000001"
EMAIL = "sentinel.person@example.org"
NAME = "Jane Doe"
PLANTED = "Zq" + "9xT4" + "mW2pLk" + "Sentinel8"  # built at runtime (detect-secrets)
OPAQUE = "opaque-block-" + "sentinel-e2e"  # built at runtime (detect-secrets)
SENTINELS = (EMAIL, NAME, PLANTED, OPAQUE)
TICKET = f"Printer broken, contact {EMAIL} ({NAME}); admin password={PLANTED}"
MEMORY = f"Last quarter {NAME} <{EMAIL}> shared token {PLANTED} in a ticket."


@pytest.fixture
def run_env(
    tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Config, the secret known to the scrubber, logging at INFO with the scrubber, the real
    Redactor with the sentinel name, prompts, `get_record` on the planted build and a
    `recall_memory` stand-in returning the planted memory. Yields the warehouse directory."""
    fake_keyring.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()
    fake_keyring.store[("herness", "vllm.api_key")] = PLANTED
    c.reset_config()
    c.init_config("local", config_dir=write_full_config(tmp_path), env={})
    secrets.resolve("secret:vllm.api_key")  # the secret joins known_values
    directory = NameDirectory.from_files(None, (NAME,), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    ls.write_prompts(tmp_path / "prompts", monkeypatch)
    wt.register_warehouse_tools()

    def recall(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        return ToolResult(ok=True, content=MEMORY)

    memory = SyncTool("recall_memory", recall, schema=strict_schema({"query": {"type": "string"}}))
    tool_registry().register(memory, owner="07")
    path = rb.make_build(tmp_path / "wh")
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "UPDATE enrich.text_redacted SET text = $t WHERE record_id = $r",
            {"t": TICKET, "r": rb.WORK_ITEM_ID},
        )
    finally:
        con.close()
    yield tmp_path / "wh"
    c.reset_config()
    reset_logging()


def _replies() -> list[LLMResponse]:
    reasoning = [ReasoningPart(text="plan", provider="anthropic", opaque={"sig": OPAQUE})]
    first = ls.resp(
        calls=[
            call("get_record", "c1", record_id=rb.WORK_ITEM_ID),
            call("recall_memory", "c2", query="printer"),
        ]
    ).model_copy(update={"reasoning": reasoning})
    answer = f"The ticket asks to contact {EMAIL} ({NAME}); a password {PLANTED} was pasted."
    last = ls.resp(answer).model_copy(
        update={"reasoning": reasoning, "usage": Usage(input_tokens=5)}
    )
    return [first, last]


def test_st05_14_scripted_run_sentinels_absent_from_trace_and_logs(
    run_env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST05-14 end-to-end scripted run with sentinel email, name and secret in ticket text and
    memory, payload rate 1.0: the model saw them, yet they are absent from the trace file and
    from the logs above DEBUG, and the trace has no `opaque` key. Logging is configured here,
    after `capsys` started, so its handler writes into the captured stderr."""
    configure_logging("INFO", scrubber=secrets.scrub_secrets)
    tracer = Tracer.for_run(RUN_ID, build_id=rb.BUILD_ID, run_kind="eval")
    view = tracer.bind(task_id="task_1", role="analyst_general")
    assert view.is_sampled("task_1")  # eval rate 1.0: every payload is written
    handle = open_warehouse(rb.BUILD_ID, warehouse_dir=run_env, sql=SqlSettings())
    client = ls.ListClient(_replies())
    try:
        ctx = ls.loop_ctx(
            tool_names=("get_record", "recall_memory"), warehouse=handle, ops=FakeOps(),
            tracer=view,
        )  # fmt: skip
        hooks = h.HarnessHooks(
            registry=None, gates={}, chain=None, compactor=None, task_id=None, phase=None,  # type: ignore[arg-type]
            stop=None, on_text_delta=None, tracer=view,
        )  # fmt: skip
        role = ls.demo_role(tools=("get_record", "recall_memory"), output_model=None)
        result = asyncio.run(
            loop.run_agent(role, {"ticket": TICKET}, ctx, client, ls.profile(), hooks)
        )
    finally:
        tracer.close()
        handle.close()
    assert result.status == "completed"
    assert result.cost_usd == Decimal(0)
    seen = repr(client.requests[1].messages)  # the run really carried every sentinel
    for sentinel in (EMAIL, NAME, PLANTED):
        assert sentinel in seen
    text = (tmp_path / "data" / "traces" / f"{RUN_ID}.jsonl").read_text(encoding="utf-8")
    kinds = [json.loads(line)["type"] for line in text.splitlines()]
    assert (kinds.count("llm_call"), kinds.count("tool_call")) == (2, 2)
    assert all("payload" in json.loads(line) for line in text.splitlines())
    for sentinel in SENTINELS:
        assert sentinel not in text
    assert '"opaque"' not in text
    get_logger("st05_14").info("st05_14.control", note="positive control")
    logs = "".join(capsys.readouterr())
    assert '"event":"core.logging.configured"' in logs  # the capture sees the pipeline
    assert '"event":"st05_14.control"' in logs
    for sentinel in SENTINELS:
        assert sentinel not in logs
