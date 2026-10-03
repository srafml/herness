"""Claude prompt cache eval ET05-02 (flow F05-09; T05-28).

Spec (impl 05 §10.1): "ET05-02 | Claude prompt cache (reviews, steps ≥ 2) |
`HERNESS_ANTHROPIC_IT=1`, one scripted hybrid writer task | ≥ 80 % of input tokens read from
cache". Product decision D5 (impl 05 §13.2): `local` only until hybrid is approved; ET05-02 is
skipped, not blocked, until then. The approval is the recorded
`security.data_policy.hybrid_approved` of `config/herness.yaml` (R-38, read only here).

When enabled it runs one scripted writer task of `STEPS` steps against the real Messages API
through the hybrid profile's writer client (`claude-opus`): the same writer system prompt and a
fixed synthetic findings brief, one user turn more per step. Over steps ≥ 2 the share of input
tokens read from the cache (`cache_read / (input + cache_read + cache_write)`) must be ≥ 80 %.
Default runs make no network call.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from herness.core import config as c
from herness.core.types import LLMRequest, Message, RequestMeta, SystemBlock, TextPart, Usage
from herness.harness.llm.anthropic_client import AnthropicClient

ROOT = Path(__file__).resolve().parents[3]
PROMPTS = ROOT / "herness" / "harness" / "roles" / "prompts"
STEPS = 3
MIN_CACHE_SHARE = 0.80


def _hybrid_approved() -> bool:
    """D5 as recorded in `config/herness.yaml` (`security.data_policy.hybrid_approved`)."""
    raw = yaml.safe_load((ROOT / "config" / "herness.yaml").read_text(encoding="utf-8"))
    policy = (raw.get("security") or {}).get("data_policy") or {}
    return policy.get("hybrid_approved") is True


pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not _hybrid_approved(), reason="D5 (hybrid) not approved: impl 05 §13.2, R-38"
    ),
    pytest.mark.skipif(
        os.environ.get("HERNESS_ANTHROPIC_IT") != "1", reason="set HERNESS_ANTHROPIC_IT=1"
    ),
]


@pytest.fixture
def hybrid_config() -> Iterator[c.HernessConfig]:
    c.reset_config()
    yield c.init_config("hybrid", config_dir=ROOT / "config")
    c.reset_config()


def _system() -> list[SystemBlock]:
    """The writer's prompt files and a fixed synthetic findings brief; the last block is the
    cache breakpoint."""
    texts = [(PROMPTS / name).read_text(encoding="utf-8") for name in ("_common.md", "writer.md")]
    findings = "\n".join(
        f"- Finding F-{i:03d}: team T{i % 9} incident load changed; evidence query q_{i:016x}."
        for i in range(120)
    )
    return [
        SystemBlock(text=texts[0]),
        SystemBlock(text=texts[1]),
        SystemBlock(text=f"Verified findings (synthetic):\n{findings}", cache=True),
    ]


def _request(client: str, messages: list[Message], step: int) -> LLMRequest:
    return LLMRequest(
        client=client,
        system=_system(),
        messages=messages,
        max_output_tokens=512,
        thinking="off",
        timeout_s=120.0,
        metadata=RequestMeta(
            run_id="run_et0502",
            task_id="task_writer",
            role="writer",
            model_role="writer",
            step=step,
            request_key=f"run_et0502:{step}:step",
        ),
    )


def test_et05_02_writer_task_reads_80pct_of_input_from_cache(
    hybrid_config: c.HernessConfig,
) -> None:
    """ET05-02 one scripted hybrid writer task: >= 80 % of input tokens read from cache over
    steps >= 2."""
    models = hybrid_config.models.models
    cfg = models.clients[models.roles["writer"]]
    assert cfg.kind == "anthropic", "the hybrid profile routes the writer to Claude"
    client = AnthropicClient(cfg)
    asks = (
        "Draft the executive summary section from the findings.",
        "Now draft the incident load section, citing the findings by id.",
        "Now draft the recommendations section.",
    )
    messages: list[Message] = []
    later = Usage()
    for step, ask in enumerate(asks[:STEPS]):
        messages.append(Message(role="user", parts=[TextPart(text=ask)]))
        resp = asyncio.run(client.acomplete(_request(cfg.name, messages, step)))
        messages.append(Message(role="assistant", parts=[TextPart(text=resp.text or "ok")]))
        if step >= 1:
            later = later.plus(resp.usage)
    total = later.input_tokens + later.cache_read_tokens + later.cache_write_tokens
    share = later.cache_read_tokens / total if total else 0.0
    sys.stderr.write(f"ET05-02 cache_read_share {share:.4f} (target >= {MIN_CACHE_SHARE})\n")
    assert share >= MIN_CACHE_SHARE, f"cache read share {share:.3f}"
