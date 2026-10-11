"""Queue and memory commands: ``jobs``, ``review-queue``, ``memory`` (impl 09 U09-97).

Every write goes through ``herness.reports.actions`` (``job_control``, ``decide_review``,
``decide_memory``) or impl 07's ``MemoryStore`` with the CLI actor; reads use impl 08's
``list_jobs`` and impl 02 / impl 09 ops reads. Review payloads are summarised to their key
fields (never text) and memory content is cut and passed through ``safe_terminal_text``
(TH09-15). L2-L4 modules are imported inside the command bodies (BT09-07).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, cast

import typer

from herness._cli._job_cmds import current_opts
from herness._cli.identity import guarded
from herness._cli.output import CliResult, emit, markup, table_printer
from herness._cli.term import safe_terminal_text
from herness.reports.rules import Actor, UserInputError  # Actor: Typer reads the hints

if TYPE_CHECKING:
    from rich.console import Console

    from herness.harness.memory import MemoryStore
    from herness.store.ops import ReviewItem

__all__ = ["register"]

_Opt = typer.Option
_Arg = typer.Argument
_LABEL_CHECK: Final = "Approve label checks in the dashboard Review Queue."
_AUTHOR_REF: Final = re.compile(r"^[0-9a-f]{32}$")
_CONTENT_CHARS: Final = 120
_JOB_FIELDS: Final = ("job_id", "kind", "gpu_class", "status", "priority", "attempts",
                      "max_attempts", "scheduled_for", "created_at", "started_at",
                      "finished_at")  # fmt: skip
_MEMORY_FIELDS: Final = ("memory_id", "layer", "kind", "status", "confidence", "created_at",
                         "expires_at", "last_used_at", "use_count")  # fmt: skip
# Kind-specific key fields of a review payload (no text: U09-97); unknown kinds show none.
_REVIEW_KEYS: Final[Mapping[str, tuple[str, ...]]] = {
    "label_check": ("record_id", "question", "answer", "probability", "decider"),
    "mapping_suggestion": ("subject_type", "jira_project", "jira_component", "team_id",
                           "service_id", "score"),
    "memory_write": ("memory_id", "layer", "kind", "flags"),
    "weight_change": ("blocks", "origin", "memory_id"),
}  # fmt: skip
Note = Annotated[str | None, _Opt("--note", help="Note (required to reject).")]
RejectNote = Annotated[str, _Opt("--note", help="Why (required).")]


def _lines(rows: Iterable[Mapping[str, object]]) -> Callable[[Console], None]:
    """One ``key=value`` line per row, every value terminal-safe."""
    shown = list(rows)

    def show(console: Console) -> None:
        for row in shown:
            console.print(" ".join(f"{k}={markup(str(v))}" for k, v in row.items()))

    return show


def _result(key: str, rows: list[dict[str, object]]) -> int:
    opts = current_opts()
    data: dict[str, object] = {key: rows}
    return emit(opts, CliResult(opts.command, data, [], _lines(rows)))


def _done(data: dict[str, object]) -> int:
    opts = current_opts()
    return emit(opts, CliResult(opts.command, data, [], table_printer(data)))


# --- jobs ----------------------------------------------------------------------------------------


@guarded("jobs list")
def jobs_list(
    *, status: Annotated[str | None, _Opt("--status", help="Job status.")] = None,
    kind: Annotated[str | None, _Opt("--kind", help="Job kind.")] = None,
    limit: Annotated[int, _Opt("--limit", min=1, max=500, help="At most N jobs.")] = 50,
    actor: Actor,
) -> int:  # fmt: skip
    """List jobs, newest first (spec 08)."""
    from herness.core import jobs  # noqa: PLC0415 - lazy (BT09-07)

    del actor
    current_opts().config()
    rows = jobs.list_jobs(status=status, kind=kind, limit=limit)
    dumped = [cast("dict[str, object]", row.model_dump(mode="json")) for row in rows]
    return _result("jobs", [{k: row[k] for k in _JOB_FIELDS} for row in dumped])


def _job_control(actor: Actor, job_id: str, op: Literal["cancel", "retry"]) -> int:
    from herness.reports.actions import job_control  # noqa: PLC0415 - lazy (BT09-07)

    current_opts().config()
    return _done({"job_id": job_id, "status": job_control(actor, job_id=job_id, op=op)})


@guarded("jobs cancel")
def jobs_cancel(job_id: Annotated[str, _Arg(help="Job id.")], *, actor: Actor) -> int:
    """Cancel a job (spec 08)."""
    return _job_control(actor, job_id, "cancel")


@guarded("jobs retry")
def jobs_retry(job_id: Annotated[str, _Arg(help="Job id.")], *, actor: Actor) -> int:
    """Retry a failed or dead job (spec 08)."""
    return _job_control(actor, job_id, "retry")


# --- review-queue --------------------------------------------------------------------------------


def _summary(item: ReviewItem) -> dict[str, object]:
    keys = _REVIEW_KEYS.get(item.kind, ())
    summary = {k: item.payload[k] for k in keys if k in item.payload}
    return {"item_id": item.item_id, "kind": item.kind, "status": item.status,
            "created_at": item.created_at, "summary": summary}  # fmt: skip


@guarded("review-queue list")
def review_list(
    *, kind: Annotated[str | None, _Opt("--kind", help="Review item kind.")] = None,
    status: Annotated[str, _Opt("--status", help="pending, approved or rejected.")] = "pending",
    actor: Actor,
) -> int:  # fmt: skip
    """List review items, one summary line each (spec 02)."""
    from herness.store.ops import list_review_items  # noqa: PLC0415 - lazy (BT09-07)

    del actor
    current_opts().config()
    items = list_review_items(kind=kind, status=status)  # type: ignore[arg-type]
    return _result("items", [_summary(item) for item in items])


def _decide_review(actor: Actor, item_id: str, *, approve: bool, note: str | None) -> int:
    """U09-38 through ``decide_review``; a label check has no CLI answer option (DD-11)."""
    from herness.core import time as clock  # noqa: PLC0415 - lazy (BT09-07)
    from herness.harness import memory  # noqa: PLC0415
    from herness.reports.actions import decide_review  # noqa: PLC0415
    from herness.reports.rules import ITEM_ID_RE, check_id  # noqa: PLC0415
    from herness.store.ops import get_review_item  # noqa: PLC0415

    current_opts().config()
    check_id(ITEM_ID_RE, item_id, "review item")
    kind = get_review_item(item_id).kind
    if kind == "label_check" and approve:
        raise UserInputError(_LABEL_CHECK)
    # Only a memory_write item reaches the store; other kinds never build it (pilot: the local
    # profile's MemoryStore needs the off-network fallback client, T09-27 note 6).
    store = memory.get_memory_store() if kind == "memory_write" else cast("MemoryStore", None)
    status = decide_review(actor, item_id=item_id, approve=approve, note=note, answer=None,
                           memory=store, now=clock.now())  # fmt: skip
    return _done({"item_id": item_id, "kind": kind, "status": status})


@guarded("review-queue approve")
def review_approve(
    item_id: Annotated[str, _Arg(help="Review item id (rev_...).")], *, note: Note = None,
    actor: Actor,
) -> int:  # fmt: skip
    """Approve a review item."""
    return _decide_review(actor, item_id, approve=True, note=note)


@guarded("review-queue reject")
def review_reject(
    item_id: Annotated[str, _Arg(help="Review item id (rev_...).")], *, note: RejectNote,
    actor: Actor,
) -> int:  # fmt: skip
    """Reject a review item."""
    return _decide_review(actor, item_id, approve=False, note=note)


# --- memory --------------------------------------------------------------------------------------


@guarded("memory list")
def memory_list(
    *, layer: Annotated[str | None, _Opt("--layer", help="Memory layer.")] = None,
    status: Annotated[str | None, _Opt("--status", help="Memory status.")] = None,
    limit: Annotated[int, _Opt("--limit", min=1, max=500, help="At most N items.")] = 50,
    actor: Actor,
) -> int:  # fmt: skip
    """List memory items, newest first (spec 07)."""
    from herness.store.ops import ui_list_memory_items  # noqa: PLC0415 - lazy (BT09-07)

    del actor
    current_opts().config()
    rows = ui_list_memory_items(layer=layer, status=status, limit=limit)
    items: list[dict[str, object]] = []
    for row in rows:
        raw: dict[str, object] = dict(row)
        item = {k: raw[k] for k in _MEMORY_FIELDS}
        item["content"] = safe_terminal_text(row["content"], max_chars=_CONTENT_CHARS)
        items.append(item)
    return _result("memory_items", items)


def _decide_memory(actor: Actor, memory_id: str, *, approve: bool, note: str | None) -> int:
    from herness.harness import memory  # noqa: PLC0415 - lazy (BT09-07)
    from herness.reports.actions import decide_memory  # noqa: PLC0415

    current_opts().config()
    status = decide_memory(actor, memory_id=memory_id, approve=approve, note=note,
                           memory=memory.get_memory_store())  # fmt: skip
    return _done({"memory_id": memory_id, "status": status})


@guarded("memory approve")
def memory_approve(
    memory_id: Annotated[str, _Arg(help="Memory id (mem_...).")], *, note: Note = None,
    actor: Actor,
) -> int:  # fmt: skip
    """Approve a pending memory item (R-33)."""
    return _decide_memory(actor, memory_id, approve=True, note=note)


@guarded("memory reject")
def memory_reject(
    memory_id: Annotated[str, _Arg(help="Memory id (mem_...).")], *, note: RejectNote,
    actor: Actor,
) -> int:  # fmt: skip
    """Reject a pending memory item (R-33)."""
    return _decide_memory(actor, memory_id, approve=False, note=note)


def _golden_questions(config_dir: Path) -> list[str]:
    """The golden suite's question texts, excluded from the export (DD29); relative suite
    paths are read from the checkout root (the parent of the config directory)."""
    from herness.core.errors import ConfigError  # noqa: PLC0415 - lazy (BT09-07)
    from herness.eval.golden import load_suite  # noqa: PLC0415

    settings = current_opts().config().eval
    if settings is None:
        msg = "eval settings are not configured"
        raise ConfigError(msg)
    path = Path(settings.suite)
    path = path if path.is_absolute() else config_dir.resolve().parent / path
    return [question.question for question in load_suite(path).questions]


@guarded("memory export-lora")
def memory_export_lora(
    *, out: Annotated[Path, _Opt("--out", help="Directory below data/models/lora_data.")],
    actor: Actor,
) -> int:  # fmt: skip
    """Export active QA pairs as LoRA training data (spec 07)."""
    from herness.harness import memory  # noqa: PLC0415 - lazy (BT09-07)

    del actor
    golden = _golden_questions(current_opts().config_dir)
    report = memory.get_memory_store().export_lora(out, golden_questions=golden)
    return _done(cast("dict[str, object]", report.model_dump(mode="json")))


@guarded("memory purge")
def memory_purge(
    *, author_ref: Annotated[str, _Opt("--author-ref", help="32 hex author reference.")],
    actor: Actor,
) -> int:  # fmt: skip
    """Erase every memory item by one author (privacy, R-54)."""
    from herness.core.audit import audit  # noqa: PLC0415 - lazy (BT09-07)
    from herness.harness import memory  # noqa: PLC0415

    if _AUTHOR_REF.fullmatch(author_ref) is None:
        msg = "--author-ref must be 32 lowercase hex characters"
        raise UserInputError(msg, hint="Pass the author_ref shown by the privacy request.")
    current_opts().config()
    audit("admin_action", actor.user_ref, action="purge", target=f"author_ref:{author_ref[:8]}")
    removed = memory.get_memory_store().purge(author_ref=author_ref)
    return _done({"removed": removed})


def register(root: typer.Typer) -> None:
    """Register the ``jobs``, ``review-queue`` and ``memory`` groups (U09-84 step 4)."""
    groups: tuple[tuple[str, str, tuple[tuple[str, Callable[..., int]], ...]], ...] = (
        ("jobs", "Job queue (spec 08).",
         (("list", jobs_list), ("cancel", jobs_cancel), ("retry", jobs_retry))),
        ("review-queue", "Review items (spec 02).",
         (("list", review_list), ("approve", review_approve), ("reject", review_reject))),
        ("memory", "Memory items (spec 07).",
         (("list", memory_list), ("approve", memory_approve), ("reject", memory_reject),
          ("export-lora", memory_export_lora), ("purge", memory_purge))),
    )  # fmt: skip
    for name, help_text, commands in groups:
        group = typer.Typer(name=name, no_args_is_help=True, help=help_text)
        for command, handler in commands:
            group.command(command)(handler)
        root.add_typer(group, name=name)
