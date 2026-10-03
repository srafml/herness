"""Child process of the loop checkpoint/resume crash test (impl 05 IT05-12, flow F05-06; T05-27).

Run as ``python -m tests.support.loop_kill <config_dir> <ops_db> <warehouse_dir> <prompts_dir>
<run_id> <task_id> <script>`` from the repository root. The child loads the config tree written
by the parent (checkpoint interval 0, so every step saves), binds the parent's migrated ops store
as the jobs and resilience backend, opens the stand-in build and runs ONE analyst task through
the real loop: `HarnessHooks` with a real spec 08 `ModelChain` over `FakeLLMClient`, the real
warehouse tools recording into the real `evidence` area, and checkpoints through
`herness.core.jobs.save_checkpoint`. When the task row already holds a `loop` checkpoint the run
resumes from it (`LoopCheckpoint.from_envelope`, R-29). The fault plan comes only from the
environment (``HERNESS_ENV=test`` and ``HERNESS_FAULTS``): a `kill` rule on `llm.call` ends the
process inside the chain. Otherwise it prints one line ``LOOP_RESULT {...}`` and exits 0.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

RESULT_PREFIX: Final = "LOOP_RESULT "
CHAIN_KEY: Final = "fake"
CHECKPOINT_EVERY_STEP: Final = "resilience.resilience.loop.checkpoint_min_interval_s=0"
_USAGE: Final = (
    "usage: python -m tests.support.loop_kill <config_dir> <ops_db> <warehouse_dir>"
    " <prompts_dir> <run_id> <task_id> <script>\n"
)


class _Info:
    off_network = False
    gpu_class = None
    timeout_s = 60.0
    model = "small-cpu-model"
    base_url = None
    api_key = None


class _Registry:
    def __init__(self, client: object) -> None:
        self._client = client

    def chain_for(self, model_role: str, depth: str) -> list[str]:
        del model_role, depth
        return [CHAIN_KEY]

    def config(self, name: str) -> _Info:
        del name
        return _Info()

    def client(self, name: str) -> object:
        del name
        return self._client


class _Gpu:
    def loaded_class(self) -> str:
        return "reasoning"

    def service_healthy(self, name: str) -> bool:
        del name
        return True


def _bootstrap(config_dir: Path, db_path: Path, prompts_dir: Path) -> None:
    """Config, keyring, ops store, backends, redaction stubs, prompts, warehouse tools."""
    import keyring  # noqa: PLC0415 - child process only
    from tests.support import warehouse_tools_build as wb  # noqa: PLC0415 - child only
    from tests.support.fake_keyring import MemoryKeyring  # noqa: PLC0415 - child only

    from herness.core import redact as r  # noqa: PLC0415 - child process only
    from herness.core.config import init_config  # noqa: PLC0415 - child process only
    from herness.core.jobs.ports import bind_jobs_backend  # noqa: PLC0415 - child only
    from herness.core.redact_directory import NameDirectory  # noqa: PLC0415 - child only
    from herness.core.resilience import bind_ops_backend  # noqa: PLC0415 - child only
    from herness.core.settings import RedactionConfig  # noqa: PLC0415 - child only
    from herness.harness import _tools_record as rec  # noqa: PLC0415 - child only
    from herness.harness import _warehouse_tools_sql as ws  # noqa: PLC0415 - child only
    from herness.harness import warehouse_tools as wt  # noqa: PLC0415 - child only
    from herness.harness.roles import base  # noqa: PLC0415 - child only
    from herness.store.ops import reset_connections  # noqa: PLC0415 - child only
    from herness.store.ops.jobs import SqliteJobsBackend  # noqa: PLC0415 - child only
    from herness.store.ops.resilience import SqliteResilienceBackend  # noqa: PLC0415

    keyring.set_keyring(MemoryKeyring())
    init_config("local", (CHECKPOINT_EVERY_STEP,), config_dir=config_dir, env={})
    reset_connections(path=db_path)
    bind_jobs_backend(SqliteJobsBackend())
    bind_ops_backend(SqliteResilienceBackend())
    directory = NameDirectory.from_files(None, (), None)
    r._State.redactor = r.Redactor(
        RedactionConfig(directory_file=None), bytes(range(32)), directory
    )
    setattr(ws, "redact_text", wb.fake_redact)  # noqa: B010 - the stand-in stub redactor
    setattr(rec, "redact_text", wb.fake_redact)  # noqa: B010 - as in wb.patch_redaction
    base._prompts_root = lambda: prompts_dir
    wt.register_warehouse_tools()


def run_task(argv: Sequence[str]) -> dict[str, object]:
    """Run (or resume) the task once; the outcome as a JSON-able dict."""
    config_dir, db_path, wh_dir, prompts_dir = (Path(a) for a in argv[:4])
    run_id, task_id, script = argv[4], argv[5], Path(argv[6])
    _bootstrap(config_dir, db_path, prompts_dir)
    from tests.support import loop_standin as ls  # noqa: PLC0415 - child only
    from tests.support import warehouse_tools_build as wb  # noqa: PLC0415 - child only
    from tests.support.fake_llm import FakeLLMClient  # noqa: PLC0415 - child only
    from tests.support.harness_fakes import RecordingTracer  # noqa: PLC0415 - child only
    from tests.support.tools_standin import StoreOps  # noqa: PLC0415 - child only

    from herness.core.resilience import ModelChain  # noqa: PLC0415 - child only
    from herness.core.types import LoopCheckpoint  # noqa: PLC0415 - child only
    from herness.harness import hooks as h  # noqa: PLC0415 - child only
    from herness.harness import loop  # noqa: PLC0415 - child only
    from herness.harness.llm.settings import SqlSettings  # noqa: PLC0415 - child only
    from herness.harness.warehouse import open_warehouse  # noqa: PLC0415 - child only
    from herness.store.ops import read_one  # noqa: PLC0415 - child only

    row = read_one("SELECT checkpoint FROM task WHERE task_id = ?", (task_id,))
    envelope = json.loads(row["checkpoint"]) if row and row["checkpoint"] else {}
    resume = LoopCheckpoint.from_envelope(envelope)
    client = FakeLLMClient(script)
    registry = _Registry(client)
    chain = ModelChain("analyst", registry=registry, depth="standard", gpu=_Gpu())  # type: ignore[arg-type]
    tracer = RecordingTracer(run_id, task_id)
    hooks = h.HarnessHooks(
        registry=registry, gates={}, chain=chain, compactor=None, task_id=task_id,  # type: ignore[arg-type]
        phase="analysis", stop=None, on_text_delta=None, tracer=tracer,  # type: ignore[arg-type]
    )  # fmt: skip
    handle = open_warehouse(wb.BUILD_ID, warehouse_dir=wh_dir, sql=SqlSettings())
    try:
        ctx = ls.loop_ctx(
            tool_names=("run_sql",), warehouse=handle, ops=StoreOps(), tracer=tracer,
            task_id=task_id,
        ).model_copy(update={"run_id": run_id})  # fmt: skip
        role = ls.demo_role(tools=("run_sql",))
        result = asyncio.run(
            loop.run_agent(
                role, {"q": "incidents"}, ctx, client, ls.profile(CHAIN_KEY), hooks, resume
            )
        )
    finally:
        handle.close()
    return {
        "resumed_at": None if resume is None else resume.state.get("step"),
        "status": result.status,
        "steps": result.steps,
        "query_ids": list(result.query_ids),
        "output": result.output,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exit 0 after printing the result line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 7:
        sys.stderr.write(_USAGE)
        return 2
    outcome = run_task(args)
    sys.stdout.write(RESULT_PREFIX + json.dumps(outcome, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry
    raise SystemExit(main())
