"""Export map of `herness.core.jobs` (private; T08-23, impl 08 §2 module-map note).

`EXPORTS` maps every public name of the §2 rows of the API submodules (§3.2 `validate`, the
§3.3 jobs ports, §3.10 to §3.17) to its owning submodule; the package `__getattr__` imports that
submodule on first access (PEP 562). Kept apart from `__init__.py` for its line budget. This
module imports nothing at run time; the `TYPE_CHECKING` block gives type checkers the
re-exported names (the package star-imports it under `TYPE_CHECKING` only). `JobContext` and
`ServiceControl` are exported here, not from `herness.core.types` (R-02).
"""

# ruff: noqa: PLC0414 - explicit `X as X` re-exports are this module's purpose (PEP 484)

from __future__ import annotations

from typing import TYPE_CHECKING, Final

EXPORTS: Final[dict[str, str]] = {
    "ActiveWindow": "windows",
    "ArbiterDecision": "arbiter",
    "ChildJobContext": "context",
    "ChildServiceControl": "context",
    "ComponentHealth": "status",
    "ComposeRunner": "gpu_services",
    "CronExpr": "cron",
    "DEFAULT_PRIORITY": "queue",
    "FailureAction": "outcomes",
    "GPU_SLOT_KINDS": "queue",
    "GpuController": "gpu",
    "GpuLock": "gpu_lock",
    "InlineJobContext": "context",
    "InlineServiceControl": "context",
    "JobContext": "ports",
    "JobRow": "ports",
    "JobsBackend": "ports",
    "LoopbackHttp": "gpu_services",
    "MANUAL_PRIORITY": "queue",
    "MAX_PIPE_MSG_BYTES": "pipe",
    "NewJob": "ports",
    "PipeMessage": "pipe",
    "RecoverySummary": "tasks",
    "ResumeResult": "status",
    "SchedCheck": "ports",
    "ScheduleEntry": "scheduler",
    "SchedulerReport": "scheduler",
    "ServiceControl": "ports",
    "StatusSnapshot": "status",
    "Supervisor": "supervisor",
    "WorkerGpuState": "gpu",
    "WorkerOptions": "supervisor",
    "WorkerRow": "ports",
    "advance_chain": "scheduler",
    "arbiter_decide": "arbiter",
    "bind_jobs_backend": "ports",
    "build_checkpoint_envelope": "tasks",
    "cancel": "queue",
    "chat_model_profile": "chat_policy",
    "chat_next_live_at": "chat_policy",
    "chat_policy": "chat_policy",
    "child_main": "child",
    "claim": "queue",
    "claim_task": "tasks",
    "collect_schedules": "scheduler",
    "complete_task": "tasks",
    "decide_failure": "outcomes",
    "decode_message": "pipe",
    "default_idem_key": "queue",
    "encode_message": "pipe",
    "enqueue": "queue",
    "enqueue_resume": "status",
    "fail_task": "tasks",
    "finish_job": "outcomes",
    "get": "queue",
    "gpu_state": "gpu",
    "health": "status",
    "list_jobs": "queue",
    "next_window_allowing": "windows",
    "preempt_deadline": "windows",
    "recover_run_tasks": "tasks",
    "register_handler": "handlers",
    "release_task": "tasks",
    "request_gpu_class": "gpu",
    "resolve_handler": "handlers",
    "resolve_local": "cron",
    "retry": "queue",
    "run_handler": "handlers",
    "run_inline": "inline",
    "run_scheduler": "scheduler",
    "run_worker": "supervisor",
    "save_checkpoint": "tasks",
    "schedule_rekey": "scheduler",
    "status_snapshot": "status",
    "submit": "queue",
    "validate_payload": "queue",
    "validate_resilience_config": "validate",
    "validate_windows": "validate",
    "vram_used_mb": "gpu_services",
    "wait_vram_free": "gpu_services",
    "window_at": "windows",
    "worker_alive": "queue",
}

# Explicit `X as X` re-exports, one statement per submodule.
# isort: off
if TYPE_CHECKING:
    from herness.core.jobs.arbiter import (
        ArbiterDecision as ArbiterDecision,
        arbiter_decide as arbiter_decide,
    )
    from herness.core.jobs.chat_policy import (
        chat_model_profile as chat_model_profile,
        chat_next_live_at as chat_next_live_at,
        chat_policy as chat_policy,
    )
    from herness.core.jobs.child import child_main as child_main
    from herness.core.jobs.context import (
        ChildJobContext as ChildJobContext,
        ChildServiceControl as ChildServiceControl,
        InlineJobContext as InlineJobContext,
        InlineServiceControl as InlineServiceControl,
    )
    from herness.core.jobs.cron import CronExpr as CronExpr, resolve_local as resolve_local
    from herness.core.jobs.gpu import (
        GpuController as GpuController,
        WorkerGpuState as WorkerGpuState,
        gpu_state as gpu_state,
        request_gpu_class as request_gpu_class,
    )
    from herness.core.jobs.gpu_lock import GpuLock as GpuLock
    from herness.core.jobs.gpu_services import (
        ComposeRunner as ComposeRunner,
        LoopbackHttp as LoopbackHttp,
        vram_used_mb as vram_used_mb,
        wait_vram_free as wait_vram_free,
    )
    from herness.core.jobs.handlers import (
        register_handler as register_handler,
        resolve_handler as resolve_handler,
        run_handler as run_handler,
    )
    from herness.core.jobs.inline import run_inline as run_inline
    from herness.core.jobs.outcomes import (
        FailureAction as FailureAction,
        decide_failure as decide_failure,
        finish_job as finish_job,
    )
    from herness.core.jobs.pipe import (
        MAX_PIPE_MSG_BYTES as MAX_PIPE_MSG_BYTES,
        PipeMessage as PipeMessage,
        decode_message as decode_message,
        encode_message as encode_message,
    )
    from herness.core.jobs.ports import (
        JobContext as JobContext,
        JobRow as JobRow,
        JobsBackend as JobsBackend,
        NewJob as NewJob,
        SchedCheck as SchedCheck,
        ServiceControl as ServiceControl,
        WorkerRow as WorkerRow,
        bind_jobs_backend as bind_jobs_backend,
    )
    from herness.core.jobs.queue import (
        DEFAULT_PRIORITY as DEFAULT_PRIORITY,
        GPU_SLOT_KINDS as GPU_SLOT_KINDS,
        MANUAL_PRIORITY as MANUAL_PRIORITY,
        cancel as cancel,
        claim as claim,
        default_idem_key as default_idem_key,
        enqueue as enqueue,
        get as get,
        list_jobs as list_jobs,
        retry as retry,
        submit as submit,
        validate_payload as validate_payload,
        worker_alive as worker_alive,
    )
    from herness.core.jobs.scheduler import (
        ScheduleEntry as ScheduleEntry,
        SchedulerReport as SchedulerReport,
        advance_chain as advance_chain,
        collect_schedules as collect_schedules,
        run_scheduler as run_scheduler,
        schedule_rekey as schedule_rekey,
    )
    from herness.core.jobs.status import (
        ComponentHealth as ComponentHealth,
        ResumeResult as ResumeResult,
        StatusSnapshot as StatusSnapshot,
        enqueue_resume as enqueue_resume,
        health as health,
        status_snapshot as status_snapshot,
    )
    from herness.core.jobs.supervisor import (
        Supervisor as Supervisor,
        WorkerOptions as WorkerOptions,
        run_worker as run_worker,
    )
    from herness.core.jobs.tasks import (
        RecoverySummary as RecoverySummary,
        build_checkpoint_envelope as build_checkpoint_envelope,
        claim_task as claim_task,
        complete_task as complete_task,
        fail_task as fail_task,
        recover_run_tasks as recover_run_tasks,
        release_task as release_task,
        save_checkpoint as save_checkpoint,
    )
    from herness.core.jobs.validate import (
        validate_resilience_config as validate_resilience_config,
        validate_windows as validate_windows,
    )
    from herness.core.jobs.windows import (
        ActiveWindow as ActiveWindow,
        next_window_allowing as next_window_allowing,
        preempt_deadline as preempt_deadline,
        window_at as window_at,
    )
# isort: on
