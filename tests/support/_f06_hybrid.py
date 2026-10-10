"""Hybrid review set-up for ST06-06 (impl 06 T06-26): profile, spied guard, Anthropic wire.

`hybrid_config` loads profile `hybrid` on a full test tree whose `herness.yaml` also sets the
data-policy chat approval flag (`security.data_policy.chat_approved`, impl 10 `init_config`
on an edited tree): the D5 substitute for "synth profile approval fixture for hybrid"
(controller ruling w49). `SpyGuard` is the real spec 10 `EgressGuard` that also keeps what it
decided on: the exact URL and body bytes of every off-network request and its refusal reason.
`WriterRouter` is the spec 11 `ScriptRouter` (the Anthropic wire under the guarded transport)
whose script is the Writer output of the run, built at the first call, when the run's
verified findings exist. `plant_sample_text` puts ticket-like text into the result samples of
the run's evidence, so a leak of samples or ticket text into the pack is visible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx2
from tests.integration.harness import _swarm_run_env as e
from tests.support.config_tree import write_full_config
from tests.support.fake_llm import ScriptRouter, respx_router

from herness.core import config as c
from herness.core.egress import EgressGuard, EgressTicket
from herness.core.egress_log import EgressLog
from herness.core.errors import EgressBlocked
from herness.core.redact import Redactor
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.eval.scripted import LLMScript, ScriptBook
from herness.store.ops import core

__all__ = [
    "GUARD_ONLY_NAME",
    "TICKET_TEXT",
    "Seen",
    "SpyGuard",
    "WriterRouter",
    "guard_redactor",
    "hybrid_config",
    "plant_sample_text",
    "writer_router",
]

GUARD_ONLY_NAME: Final = "José García"  # in the guard's re-scan directory only
TICKET_TEXT: Final = "Ticket INC-4711: printer on floor 3 jams when Priya badges in"
_APPROVED: Final = "hybrid_approved: true,"


def hybrid_config(root: Path, data_root: Path) -> c.HernessConfig:
    """Profile `hybrid` with hybrid and chat approval recorded (D5 substitute), `paths.data`
    on the test's ops store; it becomes the process config."""
    c.reset_config()
    cfg_dir = write_full_config(root)
    path = cfg_dir / "herness.yaml"
    text = path.read_text(encoding="utf-8")
    assert _APPROVED in text
    path.write_text(text.replace(_APPROVED, f"{_APPROVED} chat_approved: true,"), "utf-8")
    overrides = (f"paths.data={data_root.as_posix()}",)
    return c.init_config("hybrid", overrides, config_dir=cfg_dir, env={})


def guard_redactor(*names: str) -> Redactor:
    """The guard's redactor: the test key, a directory of `names` plus `GUARD_ONLY_NAME`."""
    directory = NameDirectory.from_files(None, (*names, GUARD_ONLY_NAME), None)
    return Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)


@dataclass(frozen=True)
class Seen:
    """One request the guard decided on: URL, body bytes, purpose and refusal reason."""

    url: str
    body: bytes
    purpose: str
    blocked: str | None

    def text(self) -> str:
        return self.body.decode("utf-8")


class SpyGuard(EgressGuard):
    """The real guard; every decision is also kept in `seen` (allowed and blocked)."""

    def __init__(self, cfg: c.HernessConfig, redactor: Redactor) -> None:
        log = EgressLog(cfg.paths.logs, "cfg_st06_06", cfg.profile)
        super().__init__(cfg, lambda: redactor, log)
        self.seen: list[Seen] = []

    def _check_and_log(  # noqa: PLR0913 - the spied U10-51 signature
        self,
        url: str,
        body: bytes | None,
        purpose: Any,
        payload_class: Any,
        token_estimate: int | None,
        *,
        method: str,
        run_id: str | None,
        task_id: str | None,
    ) -> EgressTicket:
        kw: dict[str, Any] = {"method": method, "run_id": run_id, "task_id": task_id}
        try:
            ticket = super()._check_and_log(url, body, purpose, payload_class, token_estimate, **kw)
        except EgressBlocked as exc:
            self.seen.append(Seen(url, body or b"", str(purpose), str(exc.reason)))
            raise
        self.seen.append(Seen(url, body or b"", str(purpose), None))
        return ticket


@dataclass
class WriterRouter:
    """Holds the router and the run whose Writer output its script answers with."""

    router: ScriptRouter
    run_ids: list[str] = field(default_factory=list)


def writer_router() -> WriterRouter:
    """A `ScriptRouter` for the Anthropic wire; at its first call it scripts the Writer output
    of the only run in the store (`[text, output] x 3`, enough for the fix passes)."""
    holder = WriterRouter(respx_router(ScriptBook([])))
    router = holder.router
    reply = router._reply

    def scripted(request: httpx2.Request, wire: Any) -> httpx2.Response:
        if not holder.run_ids:
            (row,) = core.read_all("SELECT run_id FROM run", ())
            holder.run_ids.append(str(row["run_id"]))
            out = e.writer_output(holder.run_ids[0])
            turns = [{"final": {"text": "draft"}}, {"final": {"output": out}}] * 3
            script = LLMScript.model_validate({"turns": turns, "source": "st06_06#writer"})
            router.book = ScriptBook([script])
        return reply(request, wire)

    router._reply = scripted  # type: ignore[method-assign]
    return holder


def plant_sample_text() -> None:
    """Add `TICKET_TEXT` to the result sample of every evidence row of the build."""
    rows = core.read_all("SELECT query_id, result_sample FROM evidence", ())
    for row in rows:
        sample = json.loads(str(row["result_sample"]))
        planted = [dict(item, description=TICKET_TEXT) for item in sample]

        def write(conn: Any, qid: str = str(row["query_id"]), value: object = planted) -> None:
            conn.execute(
                "UPDATE evidence SET result_sample = ? WHERE query_id = ?",
                (json.dumps(value), qid),
            )

        core.run_write(write, op="test_plant_sample")
