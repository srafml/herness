"""Hybrid set-up of the impl 07 security row ST07-14 (T07-24; TH07-14).

`hybrid_profile_config` loads profile `hybrid` on a full test tree whose `herness.yaml` also
records the data-policy chat approval (`security.data_policy.chat_approved`), the D5 substitute
of the w49 ruling (the T06-26 precedent), with `paths.data` on the test's ops store; it becomes
the process config. `RecordingGuard` is the real spec 10 `EgressGuard` that also keeps every
decision (URL, body bytes, purpose, refusal reason). `guard_redactor` is the guard's re-scan
redactor: the test key and a name directory. `LazyWriterRouter` is the spec 11 `ScriptRouter`
(both chat wires) whose Writer script is built at the first request, when the run's verified
findings exist. No side effects at import; the names differ from T06-26's `_f06_hybrid`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx2
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

__all__ = [
    "Decision", "LazyWriterRouter", "RecordingGuard", "guard_redactor", "hybrid_profile_config",
    "writer_script",
]  # fmt: skip

_APPROVED: Final = "hybrid_approved: true,"


def hybrid_profile_config(root: Path, data_root: Path) -> c.HernessConfig:
    """Profile `hybrid` with hybrid and chat approval recorded (D5 substitute)."""
    c.reset_config()
    cfg_dir = write_full_config(root)
    path = cfg_dir / "herness.yaml"
    text = path.read_text(encoding="utf-8")
    assert _APPROVED in text
    path.write_text(text.replace(_APPROVED, f"{_APPROVED} chat_approved: true,"), "utf-8")
    overrides = (f"paths.data={data_root.as_posix()}",)
    return c.init_config("hybrid", overrides, config_dir=cfg_dir, env={})


def guard_redactor(*names: str) -> Redactor:
    """The guard's re-scan redactor: the test key and a directory of `names`."""
    directory = NameDirectory.from_files(None, names, None)
    return Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)


@dataclass(frozen=True)
class Decision:
    """One request the guard decided on."""

    url: str
    body: bytes
    purpose: str
    blocked: str | None

    def text(self) -> str:
        return self.body.decode("utf-8")


class RecordingGuard(EgressGuard):
    """The real guard; every decision is also kept in `decisions` (allowed and blocked)."""

    def __init__(self, cfg: c.HernessConfig, redactor: Redactor) -> None:
        super().__init__(cfg, lambda: redactor, EgressLog(cfg.paths.logs, "cfg_st07_14",
                                                          cfg.profile))  # fmt: skip
        self.decisions: list[Decision] = []

    def _check_and_log(  # noqa: PLR0913 - the recorded U10-51 signature
        self, url: str, body: bytes | None, purpose: Any, payload_class: Any,
        token_estimate: int | None, *, method: str, run_id: str | None, task_id: str | None,
    ) -> EgressTicket:  # fmt: skip
        kw: dict[str, Any] = {"method": method, "run_id": run_id, "task_id": task_id}
        try:
            ticket = super()._check_and_log(url, body, purpose, payload_class, token_estimate, **kw)
        except EgressBlocked as exc:
            self.decisions.append(Decision(url, body or b"", str(purpose), str(exc.reason)))
            raise
        self.decisions.append(Decision(url, body or b"", str(purpose), None))
        return ticket


class LazyWriterRouter(ScriptRouter):
    """`respx_router` whose book is `make_book()` built at the first request."""

    def __init__(self, make_book: Callable[[], ScriptBook]) -> None:
        base = respx_router(ScriptBook([]))
        super().__init__(base.book, *(_url(base, wire) for wire in ("openai", "anthropic")))
        self._make_book: Callable[[], ScriptBook] | None = make_book

    def _reply(self, request: httpx2.Request, wire: Any) -> httpx2.Response:
        if self._make_book is not None:
            self.book, self._make_book = self._make_book(), None
        return super()._reply(request, wire)


def _url(router: ScriptRouter, wire: str) -> str:
    [(scheme, host, port, path)] = [k for k, v in router.endpoints.items() if v == wire]
    return f"{scheme}://{host}:{port}{path}"


def writer_script(turns: list[Mapping[str, Any]]) -> LLMScript:
    """One script answering every request on the wire with `turns` in order (the SDK requests
    carry no role headers, so it matches `*`; only the Writer calls a model in ST07-14)."""
    return LLMScript.model_validate({"turns": list(turns), "source": "st07_14#writer"})
