"""Export maps of herness.core.resilience and herness.core.jobs (impl 08 §2, U08-10; T08-23).

Each package re-exports every public name of the impl 08 §2 module-map rows of its non-private
submodules (resilience without `settings`, R-03) lazily (PEP 562): importing a package, or the
resilience settings module, loads no API submodule.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import types

import pytest

from herness.core import jobs, resilience

pytestmark = pytest.mark.unit

RESILIENCE: dict[str, tuple[str, ...]] = {
    "ports": (
        "ResilienceBackend", "HealthRow", "EventRow", "ChainRegistry", "ClientInfo",
        "GpuStateReader", "AsyncCompleter", "DeciderLike", "TracerLike",
    ),
    "_state": (
        "ProcessState", "process_state", "reset_process_state", "bind_ops_backend",
        "bind_chain_registry",
    ),
    "policies": (
        "RetryPolicy", "POLICY_FAMILY", "GPU_HEALTH_POLICY", "policy", "policy_for_client",
        "full_jitter_delay", "FullJitterRetryAfter", "job_backoff_delay",
    ),
    "classify": ("classify", "parse_retry_after"),
    "events": ("record_event", "EVENT_KINDS"),
    "metrics": ("record_counter", "record_histogram", "record_gauge", "timed", "flush_metrics"),
    "breaker": (
        "breaker_transition", "probe_due", "CircuitBreaker", "breaker", "guard",
        "register_probe", "run_due_probes",
    ),
    "retry": ("retry_call", "aretry_call", "retrying", "retry_page", "call_with_timeout"),
    "faults": ("FaultRule", "load_fault_plan", "fault_point", "NAMED_POINTS"),
    "chain": ("complete_validated", "build_repair_request", "ModelChain"),
    "deciders": ("DeciderChain",),
    "loop_policy": ("loop_signal_policy",),
}  # fmt: skip
JOBS: dict[str, tuple[str, ...]] = {
    "validate": ("validate_windows", "validate_resilience_config"),
    "cron": ("CronExpr", "resolve_local"),
    "ports": (
        "JobsBackend", "JobRow", "WorkerRow", "NewJob", "SchedCheck", "bind_jobs_backend",
        "JobContext", "ServiceControl",
    ),
    "queue": (
        "DEFAULT_PRIORITY", "MANUAL_PRIORITY", "GPU_SLOT_KINDS", "default_idem_key",
        "validate_payload", "submit", "enqueue", "claim", "cancel", "retry", "get", "list_jobs",
        "worker_alive",
    ),
    "handlers": ("register_handler", "resolve_handler", "run_handler"),
    "outcomes": ("decide_failure", "FailureAction", "finish_job"),
    "windows": ("ActiveWindow", "window_at", "next_window_allowing", "preempt_deadline"),
    "arbiter": ("ArbiterDecision", "arbiter_decide"),
    "scheduler": (
        "ScheduleEntry", "SchedulerReport", "collect_schedules", "run_scheduler",
        "advance_chain", "schedule_rekey",
    ),
    "chat_policy": ("chat_policy", "chat_model_profile", "chat_next_live_at"),
    "tasks": (
        "RecoverySummary", "recover_run_tasks", "claim_task", "build_checkpoint_envelope",
        "save_checkpoint", "complete_task", "fail_task", "release_task",
    ),
    "gpu_services": ("ComposeRunner", "LoopbackHttp", "vram_used_mb", "wait_vram_free"),
    "gpu_lock": ("GpuLock",),
    "gpu": ("GpuController", "WorkerGpuState", "gpu_state", "request_gpu_class"),
    "pipe": ("PipeMessage", "encode_message", "decode_message", "MAX_PIPE_MSG_BYTES"),
    "context": (
        "ChildJobContext", "ChildServiceControl", "InlineJobContext", "InlineServiceControl",
    ),
    "supervisor": ("WorkerOptions", "Supervisor", "run_worker"),
    "child": ("child_main",),
    "inline": ("run_inline",),
    "status": (
        "StatusSnapshot", "status_snapshot", "ComponentHealth", "health", "ResumeResult",
        "enqueue_resume",
    ),
}  # fmt: skip
CASES = [(resilience, RESILIENCE), (jobs, JOBS)]
# A public name equal to its submodule's name resolves to that (callable) module.
SAME_NAME = {"classify", "breaker", "chat_policy"}


def _loaded_after(code: str, prefixes: tuple[str, ...]) -> set[str]:
    """The modules under `prefixes` that `code` leaves loaded in a fresh interpreter."""
    report = (
        "\nimport json, sys\n"
        f"print(json.dumps([m for m in sys.modules if m.startswith({prefixes!r})]))"
    )
    result = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and test-owned code
        [sys.executable, "-c", code + report],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return set(json.loads(result.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize(("package", "expected"), CASES, ids=["resilience", "jobs"])
def test_cv_t08_23_export_map_is_the_spec_list(
    package: types.ModuleType, expected: dict[str, tuple[str, ...]]
) -> None:
    """UT08-63 (cv) the export map is exactly the §2 public names; `__all__` is its sorted keys."""
    wanted = {name: submodule for submodule, names in expected.items() for name in names}
    assert wanted == package._EXPORTS
    assert package.__all__ == tuple(sorted(wanted))


@pytest.mark.parametrize(("package", "expected"), CASES, ids=["resilience", "jobs"])
def test_cv_t08_23_every_name_resolves_to_its_owner(
    package: types.ModuleType, expected: dict[str, tuple[str, ...]]
) -> None:
    """UT08-63 (cv) every exported name is the owning submodule's object."""
    for submodule, names in expected.items():
        owner = importlib.import_module(f"{package.__name__}.{submodule}")
        for name in names:
            value = getattr(package, name)
            assert value is (owner if name in SAME_NAME else getattr(owner, name)), name


def test_cv_t08_23_same_name_exports_are_callable_modules() -> None:
    """UT08-63 (cv) `chat_policy`, `classify`, `breaker` call through to their functions."""
    for package, name in ((jobs, "chat_policy"), (resilience, "classify"), (resilience, "breaker")):
        value = getattr(package, name)
        assert isinstance(value, types.ModuleType)
        assert callable(value)


def test_cv_t08_23_package_import_loads_no_submodule() -> None:
    """UT08-63 (cv) importing either package loads at most its private export map module."""
    loaded = _loaded_after(
        "import herness.core.jobs, herness.core.resilience",
        ("herness.core.jobs.", "herness.core.resilience."),
    )
    assert loaded <= {"herness.core.jobs._exports", "herness.core.resilience._exports"}


def test_cv_t08_23_settings_import_stays_light() -> None:
    """UT08-63 (cv) importing herness.core.resilience.settings never imports the supervisor."""
    loaded = _loaded_after(
        "import herness.core.resilience.settings",
        ("herness.core.jobs", "herness.core.resilience."),
    )
    assert "herness.core.jobs.supervisor" not in loaded
    assert loaded <= {"herness.core.resilience.settings", "herness.core.resilience._exports"}
