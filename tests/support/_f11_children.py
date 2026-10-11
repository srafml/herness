"""Worker-subprocess side of the spec 11 fault cases X1 and X6 (impl 11 FT11-01, FT11-06;
F11-11 step 3; T11-33).

Run as ``python -m tests.support._f11_children <command> <cfg_root> <data_root> <build_id>
<warehouse> <job_id> <call_log>`` from the repository root. The child rebuilds the T06-22
run-driver set-up (`_f08_swarm.build_drv`: scripted agents, stub pipeline, fake Verifier and
memory) on the parent's config tree and migrated ops store, pinned to the parent's
`tiny_build` copy as `tests.integration.eval._eval_it_env` does, then claims and runs the
parent's job with `run_inline` (the `--inline` path, R-45):

* ``review``: the spec 08 `review` job (U06-85 `review_job_handler`) with its composition root
  bound to the driver's swarm (FT11-01, ``job.before_complete kind=review``).
* ``eval``: the `eval` job (U11-68 `handle_eval`) with deps over the driver's swarm and a
  scripted chat service (FT11-06, ``job.before_complete kind=eval``).

Every scripted agent call (its role) and chat answer (``chat``) is appended to ``call_log``
before it runs, so the parent counts the calls of a process the kill ended. The fault plan
reaches the child only through its environment (`tests.support.faults.fault_env`, R-40); the
result line ``F11_RESULT <json>`` is printed only when no kill fired.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    import pytest
    from tests.integration.harness._swarm_run_env import Drv

    from herness.core.types import ChatEvent, ChatMode

RESULT_PREFIX: Final = "F11_RESULT "
_USAGE: Final = (
    "usage: python -m tests.support._f11_children review|eval <cfg_root> <data_root>"
    " <build_id> <warehouse> <job_id> <call_log>\n"
)


def log_call(call_log: Path, name: str) -> None:
    """Append one call line (`name`) to `call_log`."""
    with call_log.open("a", encoding="utf-8") as handle:
        handle.write(name + "\n")


def logged_agents(mp: pytest.MonkeyPatch, call_log: Path) -> None:
    """Make every scripted `run_agent` of the driver log its role before it runs."""
    from herness.harness.swarm import challenge, planner, scheduler, writer  # noqa: PLC0415

    for module in (planner, scheduler, challenge, writer):
        agent = module.run_agent  # the driver's scripted Agents

        async def logged(*args: Any, _agent: Any = agent, **kwargs: Any) -> Any:
            log_call(call_log, str(args[2].role))
            return await _agent(*args, **kwargs)

        mp.setattr(module, "run_agent", logged)


def pinned_drv(mp: pytest.MonkeyPatch, cfg_root: Path, data_root: Path, build_id: str) -> Drv:
    """`build_drv` pinned to the tiny build as `_eval_it_env.eval_env` pins the parent's."""
    from tests.integration.eval import _eval_it_env as it  # noqa: PLC0415 - child only
    from tests.integration.harness import _swarm_run_env as e  # noqa: PLC0415 - child only
    from tests.support._f08_swarm import build_drv  # noqa: PLC0415 - child only
    from tests.unit.harness._blackboard_env import FakeWarehouse  # noqa: PLC0415 - child only

    mp.setattr(e, "BUILD_ID", build_id)
    mp.setattr(e, "INCIDENT_SQL", it.INCIDENT_SQL)
    mp.setattr(e, "FUNDING_SQL", it.FUNDING_SQL)
    drv = build_drv(mp, cfg_root, data_root)
    drv.pool.warehouse = FakeWarehouse(build_id)
    return drv


def _review(drv: Drv, build_id: str, job_id: str) -> dict[str, object]:
    from herness.core.jobs import run_inline  # noqa: PLC0415 - child only
    from herness.harness.swarm import handler  # noqa: PLC0415 - child only

    def swarm(cfg: object) -> object:
        del cfg
        return drv.swarm(current_build=lambda: build_id, build_exists=lambda b: b == build_id)

    setattr(handler, "build_swarm_from_config", swarm)  # noqa: B010 - the composition root
    handler.register_handlers()
    return dict(run_inline(job_id).result)


class LoggedChat:
    """The parent's scripted chat (`AnswerChat` with the IT values) logging each answer."""

    def __init__(self, call_log: Path) -> None:
        from tests.integration.eval import _eval_it_env as it  # noqa: PLC0415 - child only
        from tests.integration.harness import _swarm_run_env as e  # noqa: PLC0415
        from tests.unit.eval._runner_env import AnswerChat  # noqa: PLC0415 - child only

        self._log = call_log
        self._chat = AnswerChat(value=7, query_id=e.incident_qid("t1"), column="incidents",
                                text=it.CHAT_TEXT)  # fmt: skip

    def answer(self, session_id: str, text: str, user_ref: str, mode: ChatMode
               ) -> Iterator[ChatEvent]:  # fmt: skip
        log_call(self._log, "chat")
        return self._chat.answer(session_id, text, user_ref, mode)


def _eval(drv: Drv, build_id: str, warehouse: Path, job_id: str, call_log: Path
          ) -> dict[str, object]:  # fmt: skip
    import duckdb  # noqa: PLC0415 - child only
    from tests.unit.eval._runner_env import settings  # noqa: PLC0415 - child only

    from herness.core import time as clock  # noqa: PLC0415 - child only
    from herness.core.jobs import run_inline  # noqa: PLC0415 - child only
    from herness.eval import runner  # noqa: PLC0415 - child only

    con = duckdb.connect(str(warehouse), read_only=True)
    chat = LoggedChat(call_log)
    swarm = drv.swarm(current_build=lambda: build_id, build_exists=lambda b: b == build_id)

    def deps(ctx: Any) -> runner.EvalDeps:
        return runner.EvalDeps(
            warehouse_for=lambda b: con, swarm_for=lambda profile: swarm,
            chat_for=lambda profile: chat,  # type: ignore[arg-type,return-value]
            judge_for=lambda: None, memory=None, job=ctx, clock=clock.now,
            data_root=Path(drv.hcfg.paths.data), settings=settings(),
        )  # fmt: skip

    runner.set_deps_factory(deps)
    runner.register_handlers()
    return dict(run_inline(job_id).result)


def _run(command: str, args: Sequence[str]) -> dict[str, object]:
    import pytest  # noqa: PLC0415 - child only

    cfg_root, data_root, build_id, warehouse, job_id, call_log = args
    log = Path(call_log)
    with pytest.MonkeyPatch.context() as mp:
        drv = pinned_drv(mp, Path(cfg_root), Path(data_root), build_id)
        logged_agents(mp, log)
        commands: dict[str, Callable[[], dict[str, object]]] = {
            "review": lambda: _review(drv, build_id, job_id),
            "eval": lambda: _eval(drv, build_id, Path(warehouse), job_id, log),
        }
        try:
            return commands[command]()
        finally:
            for s in drv.swarms:
                s.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exit 0 after printing the result line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 7 or args[0] not in ("review", "eval"):
        sys.stderr.write(_USAGE)
        return 2
    out = _run(args[0], args[1:])
    sys.stdout.write(RESULT_PREFIX + json.dumps(out, sort_keys=True, default=str) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry
    raise SystemExit(main())
