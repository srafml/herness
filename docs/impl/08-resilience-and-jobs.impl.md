# 08 — Resilience and Jobs: Implementation Specification

Status: Draft v2 (consistency pass: rulings of [`DECISIONS.md`](DECISIONS.md) applied) · 2026-09-24 · Design spec: [`docs/specs/08-resilience-and-jobs.md`](../specs/08-resilience-and-jobs.md) (Draft v2) · Phase: 3 · Standards: [`ENG-STANDARDS.md`](ENG-STANDARDS.md)
Depends on implementation specs: 02 (ops store `connection()`, `run_write()`, migrations 001–006 including `metric_sample`), 05 (`LLMRequest`, `LLMResponse`, `LoopState`, `LoopSignal`, `LLMRegistry`; `Tracer` used structurally), 10 (config loader, owner validator hook `register_owner_validator` (R-71), `cloud_chat_allowed` (R-38), secrets, redaction, `herness.core.egress.loopback_http_client`, registry, `herness.admin` retention), 00 artifacts (`herness.core.errors`, `herness.core.ids` including `canonical_json`, `herness.core.time`, `herness.core.logging`, the `herness.core.types` package skeleton). Consumers: 01, 02, 03, 04, 05, 06, 07, 09, 10, 11.

Conventions used below: `cfg` is `herness.core.config.get_config()`. `R` is `cfg.resilience.resilience` (the `resilience` key of `config/resilience.yaml`) and `S` is `cfg.resilience.schedule`. "ts" means the spec 00 §8 fixed-width UTC text `YYYY-MM-DDTHH:MM:SS.ffffffZ`. "Canonical JSON" means the output of `herness.core.ids.canonical_json` (R-14) encoded as UTF-8. A cross-spec dependency is written `T<NN>-<nn> (<qualified name>)`: the task card of spec NN whose Units list contains the defining unit (DECISIONS §8). A ruling of `DECISIONS.md` is cited as `R-nn`.

## 1. Scope and traceability

This spec turns design 08 into build instructions for `herness/core/resilience/` (retry policies, timeouts, error classification, circuit breakers, probes, model and decider fallback chains, structured-output repair, loop-signal policy, fault injection, `resilience_event` and component metric recording) and `herness/core/jobs/` (job queue, leases, handlers, outcomes, supervisor and child processes, GPU arbitration and service control, schedule windows, cron scheduler and chains, planned rekey, chat-hours policy, task lease and checkpoint helpers, inline runs, status snapshot and health). It also specifies the 08-owned ops store areas `herness/store/ops/{jobs,tasks,worker,resilience,metrics}.py` (R-08), which realise the 08-owned ops tables (`job`, `worker`, `source_health`, `resilience_event`, task mechanics), and the single `metric_sample` writer `herness.store.ops.metrics.record_metric_samples` (ENG §4, delta E5, R-12; the table DDL is impl 02 migration 006). Under R-09 this spec specifies every function of those five areas that any other implementation spec references. It owns the `herness.core.types.jobs` submodule (R-01) and the behavioral classes `ModelChain`, `loop_signal_policy` (in `herness.core.resilience`) and `JobContext` (in `herness.core.jobs`) (R-02). What each job does (handlers), loop-signal detection, budgets, trace file format and CLI rendering are out of scope (design 08 §1).

### 1.1 Traceability matrix

| Design § | Requirement (short) | Impl § | Units | Tasks | Tests |
|----------|---------------------|--------|-------|-------|-------|
| 1 | Two modules, scope and exclusions | 1, 2, 13 (D08-01, D08-02) | all | T08-01, T08-03, T08-26 | UT08-103 |
| 2.1 | Classify by taxonomy, one policy per call type, no message matching | 3.4, 6 | U08-11–U08-17 | T08-04 | UT08-06, UT08-07, UT08-10, UT08-11 |
| 2.2 | Breakers in `source_health` | 3.6, 4.1.3 | U08-23–U08-27, U08-94 | T08-05, T08-06 | UT08-12–UT08-19, IT08-01, UT08-104 |
| 2.3 | Execute per-role fallback chains with repair | 3.9 | U08-35–U08-38 | T08-09 | UT08-33–UT08-42 |
| 2.4 | Leased jobs survive kills, reboots, swaps | 3.10, 3.16, 5 (F08-04, F08-05, F08-10) | U08-46–U08-50, U08-87, U08-88 | T08-12, T08-15, T08-21 | IT08-04–IT08-09, FT08-07 |
| 2.5 | One GPU class, windows, in-job switch API | 3.12, 3.15, 3.16 | U08-43, U08-66–U08-70, U08-78–U08-86, U08-102 | T08-12, T08-13, T08-17, T08-18, T08-20 | UT08-75–UT08-78, UT08-87–UT08-99, UT08-107, UT08-108, FT08-12 |
| 2.6 | Chat mode decision | 3.14 | U08-75–U08-77 | T08-19 | UT08-84–UT08-86 |
| 2.7 | Visibility in logs, traces, `resilience_event`, status | 3.5, 8 | U08-18–U08-22, U08-91 | T08-05, T08-22 | UT08-25, UT08-29–UT08-32, UT08-100 |
| 2.8 | Fault hooks for spec 11 (JSON plans, `HERNESS_ENV=test` only, R-40) | 3.8 | U08-33, U08-34 | T08-08 | UT08-46–UT08-50, UT08-105, FT08-01–FT08-12 |
| 3.1 | `policy`, `retrying`, `retry_call`, `aretry_call`, `retry_page`, `guard`, `call_with_timeout`, `classify`, `CircuitBreaker`, `breaker` | 3.4, 3.6, 3.7 | U08-11–U08-32 | T08-04–T08-07 | UT08-06–UT08-28 |
| 3.2 | `ModelChain`, `complete_validated`, `DeciderChain`, `loop_signal_policy` | 3.9 | U08-35–U08-40 | T08-09, T08-10 | UT08-33–UT08-45 |
| 3.3 | `fault_point` | 3.8 | U08-34 | T08-08 | UT08-46, UT08-49, UT08-105 |
| 3.4 | Jobs API, `JobSpec`, `JobOutcome`, `ServiceControl`, `JobContext` (R-01, R-02, R-41, R-42) | 3.1, 3.10, 3.16 | U08-01–U08-04, U08-41–U08-56, U08-84–U08-86 | T08-01, T08-03, T08-12, T08-20 | UT08-01, UT08-02, UT08-51–UT08-65, UT08-97–UT08-99, UT08-106 |
| 3.5 | `GpuStateReader`, `gpu_state`, supervisor performs swaps, requested class | 3.15 | U08-08, U08-81, U08-83, U08-102 | T08-18 | UT08-91–UT08-94, UT08-108 |
| 3.6 | `chat_policy`, `chat_model_profile`, `chat_next_live_at` | 3.14 | U08-75–U08-77 | T08-19 | UT08-84–UT08-86 |
| 3.7 | Task lease/resume helpers; only 08 writes these columns | 3.11, 4.1.5 | U08-57–U08-63, U08-97 | T08-16 | UT08-66–UT08-71, UT08-109 |
| 3.8 | CLI behavior (`worker`, `resume`, `jobs`, `status`) | 3.16, 3.17 | U08-89–U08-93 | T08-21, T08-22 | IT08-11, IT08-12, UT08-100, UT08-102 |
| 4.1 | `job` columns, idem keys, result, attempts, last_error, lease owner | 4.1.1 | U08-42–U08-50, U08-95 | T08-11, T08-12, T08-15 | UT08-51–UT08-62 |
| 4.2 | `worker`, `source_health`, `resilience_event` | 4.1.2–4.1.4 | U08-18, U08-94, U08-96 | T08-05, T08-11 | UT08-29, UT08-65 |
| 4.3 | Task checkpoint envelope `{schema_version, loop, state, scratchpad}`, key-scoped save, 4 MiB cap (R-21) | 4.2 | U08-59, U08-60 | T08-16 | UT08-68, UT08-109 |
| 4.4 | Trace events `retry`, `fallback`, `repair`, `guard_stop` | 3.5, 8.3 | U08-18, U08-28, U08-35, U08-38, U08-40 | T08-05, T08-07, T08-09, T08-10 | UT08-25, UT08-33, UT08-42, UT08-45 |
| 5.1 | Job kinds, handler ownership, start classes (GPU-slot kinds, R-43), `exclusive_kinds`, error wrapping | 3.10 | U08-43, U08-48, U08-55, U08-56 | T08-12 | UT08-57, UT08-64, UT08-107 |
| 5.2 | Retry policies, full jitter, Retry-After, error class rules, classify mapping, timeouts, tenacity factory | 3.4, 3.7, 3.10, 6 | U08-11–U08-17, U08-28–U08-32, U08-49 | T08-04, T08-07, T08-15 | UT08-06–UT08-11, UT08-20–UT08-28, UT08-58, UT08-112, PT08-01–PT08-03 |
| 5.3 | Breaker state machine, cooldown, probe claim, probes, service restart, sync effect | 3.6, 3.15 | U08-23–U08-27, U08-81 | T08-06, T08-18 | UT08-12–UT08-19, UT08-96, UT08-104, IT08-01, FT08-05 |
| 5.4 | Candidates filter, per-candidate retry/repair/fallback, repair protocol | 3.9 | U08-35–U08-38 | T08-09 | UT08-33–UT08-42, FT08-02 |
| 5.5 | Decider fallback order, skipped entries deferred | 3.9 | U08-39 | T08-10 | UT08-43, UT08-44 |
| 5.6 | Loop guards division of labor, checkpoint interval | 3.9, 3.11 | U08-40, U08-60 | T08-10, T08-16 | UT08-45, UT08-68 |
| 5.7 | Enqueue (per-kind default priority, R-41), sched no-refire, claim SQL, heartbeat, reaper, completion guards, cancel, `run_inline` (R-45) | 3.10, 3.16, 4.1.1 | U08-46–U08-53, U08-90, U08-95 | T08-11, T08-12, T08-15, T08-22 | UT08-52–UT08-62, UT08-106, IT08-02, IT08-12 |
| 5.8 | GPU classes, warm-up, compose commands, swap algorithm, service control, detection, deploy up/down and `gpu load/unload` requests | 3.15 | U08-78–U08-83, U08-102 | T08-17, T08-18 | UT08-87–UT08-96, UT08-108, FT08-09, FT08-12 |
| 5.9 | Worker process model, pipe, tick, stall, shutdown, crash recovery, GPU lock | 3.16 | U08-82, U08-84, U08-87–U08-89 | T08-17, T08-20, T08-21 | UT08-95, UT08-97, IT08-04–IT08-11 |
| 5.10 | Windows, arbiter, preemption, chat-window priority, `chat_policy` (R-35, R-38) | 3.12, 3.14 | U08-66–U08-70, U08-75–U08-77 | T08-13, T08-19, T08-21 | UT08-75–UT08-78, UT08-84–UT08-86, IT08-13, FT08-10 |
| 5.11 | Cron, catch-up, chains, defaults, planned rekey | 3.12, 3.13 | U08-64, U08-65, U08-71–U08-74 | T08-02, T08-14 | UT08-72–UT08-74, UT08-79–UT08-83, IT08-03, PT08-04 |
| 5.12 | Task resume, idempotency rule, `herness resume` | 3.11, 3.17 | U08-57–U08-63, U08-93 | T08-16, T08-22 | UT08-66–UT08-71, UT08-102, FT08-06 |
| 5.13 | Fault plan (JSON only, test environment only, R-40), actions, selectors, named points | 3.8 | U08-33, U08-34 | T08-08 | UT08-46–UT08-50, UT08-105 |
| 5.14 | Log events, derived metrics, `status_snapshot` | 3.5, 3.17, 8 | U08-18–U08-22, U08-91 | T08-05, T08-22 | UT08-100 |
| 6 | Supervisor store outage, clock jumps, WSL down, child crash, broken schedule | 5, 6 | U08-72, U08-81, U08-87 | T08-14, T08-18, T08-21 | UT08-83, UT08-92, IT08-07, IT08-10 |
| 7 | `config/resilience.yaml` keys and validation (settings imports per R-03) | 9 | U08-06, U08-07, U08-67, U08-99 | T08-26, T08-02 | UT08-03–UT08-05 |
| 8 | Performance targets | 10 | U08-28, U08-46, U08-48, U08-75, U08-87 | T08-25 | BT08-01–BT08-11 |
| 9 | Security (argument lists, redaction, payload refs, egress, Retry-After cap, inert faults, ACLs) | 7 | U08-16, U08-18, U08-34, U08-37, U08-45, U08-78 | T08-04, T08-05, T08-08, T08-09, T08-12, T08-17 | ST08-01–ST08-15 |
| 10 | Unit tests and fault suite F1–F12, Phase 3 acceptance | 11 | all | T08-24 | FT08-01–FT08-12 |
| 11 | Open questions (Q1–Q6, Q8 resolved; Q7 open) | 13.2 | U08-79 | T08-17 | UT08-89 |
| 12 | Dependencies | 14 | — | — | — |
| 13 | Resolved contract changes | 4, 13.1 | U08-01–U08-05 | T08-01, T08-03 | UT08-01, UT08-02 |
| ENG §4 / E5 | Component metrics recording into `metric_sample`; single writer `record_metric_samples` (R-12) | 3.5, 3.18, 8.2 | U08-19–U08-22, U08-100, U08-101, U08-103 | T08-01, T08-05 | UT08-31, UT08-32, UT08-110, UT08-111 |

## 2. Module map

Design 08 names two files, `herness/core/resilience.py` and `herness/core/jobs.py`. Their content exceeds the 400-line module limit (ENG §2.4), so each becomes a package with the same import path (delta D08-01). Each package `__init__.py` re-exports the public API through a PEP 562 module `__getattr__` that imports the owning submodule on first access, so importing `herness.core.resilience.settings` from the config loader does not import the supervisor. The only settings module of this spec is `herness/core/resilience/settings.py`; it imports only the standard library, pydantic, `herness.core.types` and `herness.core.errors` (R-03). The shared data types are in the `herness.core.types.jobs` submodule (R-01); `JobContext` and `ServiceControl` are protocols in `herness.core.jobs.ports` and `ModelChain` and `loop_signal_policy` are in `herness.core.resilience` (R-02). The SQL lives in the L1 ops areas `herness/store/ops/{resilience,metrics,jobs,worker,tasks}.py` (R-08) because L0 may not import L1 (ENG §2.1, R-04); core code reaches it through the backend ports of U08-08 and U08-41, bound at the composition root (U08-10, U08-98). Every store write goes through impl 02 `run_write()` (R-10), which already applies the `sqlite_write` retry policy (U08-28) and the `sqlite.write` fault point.

| Path | Purpose | Public symbols | Layer | Extra imports | Line budget |
|------|---------|----------------|-------|---------------|-------------|
| `herness/core/types/jobs.py` | 08-owned shared data types (R-01) | `GpuClass`, `JobKind`, `ServiceName`, `ChatMode`, `BreakerState`, `PolicyName`, `JobSpec`, `JobOutcome`, `MetricSample` | L0 | `herness.core.types.harness` only (impl 00 §3.5) | 150 |
| `herness/core/types/__init__.py` (08 import line) | Re-export of the 08 names (impl 00 U00-44) | the names above | L0 | none | +2 |
| `herness/core/types/_ownership.py` (08 entries) | `TYPE_OWNERS` entries for the 08 helper types (impl 00 U00-45) | none new | L0 | none | +6 |
| `herness/core/errors.py` (08 section) | 08 taxonomy subclass (R-19) | `JobStateError` | L0 | none | +12 |
| `herness/core/resilience/__init__.py` | Lazy re-export of the resilience API | all public names of §3.3–§3.8 | L0 | none | 70 |
| `herness/core/resilience/settings.py` | Every model of `config/resilience.yaml` (R-03) | `ResilienceSection` and sub-models, `ScheduleSection`, `WindowSpec`, `ScheduledJob`, `ChainStep`, `ResilienceConfig` | L0 | `pydantic` | 390 |
| `herness/core/resilience/ports.py` | Protocols the resilience code depends on | `ResilienceBackend`, `HealthRow`, `EventRow`, `ChainRegistry`, `ClientInfo`, `GpuStateReader`, `AsyncCompleter`, `DeciderLike`, `TracerLike` | L0 | none | 190 |
| `herness/core/resilience/_state.py` | The single process-state holder | `ProcessState`, `process_state`, `reset_process_state`, `bind_ops_backend`, `bind_chain_registry` | L0 | none | 140 |
| `herness/core/resilience/policies.py` | Policies and backoff math | `RetryPolicy`, `POLICY_FAMILY`, `GPU_HEALTH_POLICY`, `policy`, `policy_for_client`, `full_jitter_delay`, `FullJitterRetryAfter`, `job_backoff_delay` | L0 | `tenacity` | 220 |
| `herness/core/resilience/classify.py` | Foreign exception → taxonomy | `classify`, `parse_retry_after` | L0 | `httpx` | 230 |
| `herness/core/resilience/events.py` | `resilience_event` rows, logs, trace fan-out | `record_event`, `EVENT_KINDS` | L0 | `structlog` | 200 |
| `herness/core/resilience/metrics.py` | Component metric recording | `record_counter`, `record_histogram`, `record_gauge`, `timed`, `flush_metrics` | L0 | none | 260 |
| `herness/core/resilience/breaker.py` | Breaker state machine, registry, probes | `breaker_transition`, `probe_due`, `CircuitBreaker`, `breaker`, `guard`, `register_probe`, `run_due_probes` | L0 | none | 380 |
| `herness/core/resilience/retry.py` | Retry execution | `retry_call`, `aretry_call`, `retrying`, `retry_page`, `call_with_timeout` | L0 | `tenacity` | 340 |
| `herness/core/resilience/faults.py` | Fault plans and `fault_point` | `FaultRule`, `load_fault_plan`, `fault_point`, `NAMED_POINTS` | L0 | none | 340 |
| `herness/core/resilience/chain.py` | Repair and model fallback | `complete_validated`, `build_repair_request`, `ModelChain` | L0 | `jsonschema`, `pydantic` | 390 |
| `herness/core/resilience/deciders.py` | Decider fallback | `DeciderChain` | L0 | none | 170 |
| `herness/core/resilience/loop_policy.py` | Loop-signal policy | `loop_signal_policy` | L0 | none | 90 |
| `herness/core/jobs/__init__.py` | Lazy re-export of the jobs API | all public names of §3.9–§3.15 | L0 | none | 90 |
| `herness/core/jobs/validate.py` | Window coverage and cron checks; the `resilience` owner validator registered with impl 10's start-up validation hook (R-03, R-71) | `validate_windows`, `validate_resilience_config` | L0 | none | 180 |
| `herness/core/jobs/cron.py` | Cron parser and DST resolution | `CronExpr`, `resolve_local` | L0 | none | 290 |
| `herness/core/jobs/ports.py` | Jobs backend protocol, row models and the handler-facing protocols (R-02) | `JobsBackend`, `JobRow`, `WorkerRow`, `NewJob`, `bind_jobs_backend`, `JobContext`, `ServiceControl` | L0 | `pydantic` | 300 |
| `herness/core/jobs/queue.py` | Enqueue, claim, cancel, retry, reads | `DEFAULT_PRIORITY`, `MANUAL_PRIORITY`, `GPU_SLOT_KINDS`, `default_idem_key`, `validate_payload`, `submit`, `enqueue`, `claim`, `cancel`, `retry`, `get`, `list_jobs`, `worker_alive` | L0 | none | 390 |
| `herness/core/jobs/handlers.py` | Handler registry and wrapper | `register_handler`, `resolve_handler`, `run_handler` | L0 | none | 120 |
| `herness/core/jobs/outcomes.py` | Failure policy and completion | `decide_failure`, `FailureAction`, `finish_job` | L0 | none | 260 |
| `herness/core/jobs/windows.py` | Window evaluation and preemption math | `ActiveWindow`, `window_at`, `next_window_allowing`, `preempt_deadline` | L0 | none | 290 |
| `herness/core/jobs/arbiter.py` | GPU arbiter decision | `ArbiterDecision`, `arbiter_decide` | L0 | none | 150 |
| `herness/core/jobs/scheduler.py` | Scheduler tick, chains, rekey | `ScheduleEntry`, `collect_schedules`, `run_scheduler`, `advance_chain`, `schedule_rekey` | L0 | none | 390 |
| `herness/core/jobs/chat_policy.py` | Chat mode | `chat_policy`, `chat_model_profile`, `chat_next_live_at` | L0 | none | 170 |
| `herness/core/jobs/tasks.py` | Task lease/resume helpers | `RecoverySummary`, `recover_run_tasks`, `claim_task`, `build_checkpoint_envelope`, `save_checkpoint`, `complete_task`, `fail_task`, `release_task` | L0 | none | 260 |
| `herness/core/jobs/gpu_services.py` | Compose commands, loopback health and warm-up, VRAM | `ComposeRunner`, `LoopbackHttp`, `vram_used_mb`, `wait_vram_free` | L0 | none (loopback clients come from `herness.core.egress.loopback_http_client`, R-06) | 360 |
| `herness/core/jobs/gpu_lock.py` | Host GPU owner lock | `GpuLock` | L0 | none (`msvcrt`/`fcntl` stdlib) | 90 |
| `herness/core/jobs/gpu.py` | Swap and service control, state reader, external class requests | `GpuController`, `WorkerGpuState`, `gpu_state`, `request_gpu_class` | L0 | none | 400 |
| `herness/core/jobs/pipe.py` | Supervisor↔child JSON messages | `PipeMessage`, `encode_message`, `decode_message`, `MAX_PIPE_MSG_BYTES` | L0 | `pydantic` | 150 |
| `herness/core/jobs/context.py` | `JobContext` implementations | `ChildJobContext`, `ChildServiceControl`, `InlineJobContext`, `InlineServiceControl` | L0 | none | 360 |
| `herness/core/jobs/supervisor.py` | Worker supervisor | `WorkerOptions`, `Supervisor`, `run_worker` | L0 | `psutil` | 400 |
| `herness/core/jobs/child.py` | Child process entry | `child_main` | L0 | none | 150 |
| `herness/core/jobs/inline.py` | CLI inline run | `run_inline` | L0 | none | 200 |
| `herness/core/jobs/status.py` | Status, health, resume enqueue | `StatusSnapshot`, `status_snapshot`, `ComponentHealth`, `health`, `ResumeResult`, `enqueue_resume` | L0 | none | 330 |
| `herness/store/ops/resilience.py` | SQL for `source_health`, `resilience_event`; backend binding (R-08) | `SqliteResilienceBackend`, `purge_events`, `bind_core_backends` | L1 | none | 320 |
| `herness/store/ops/metrics.py` | The single `metric_sample` writer and its purge (R-12) | `record_metric_samples`, `purge_metric_samples` | L1 | none | 120 |
| `herness/store/ops/jobs.py` | SQL for `job` (R-08) | `SqliteJobsBackend` (job methods) | L1 | none | 400 |
| `herness/store/ops/worker.py` | SQL for `worker`, `run` reads (R-08) | `WorkerSqlMixin` | L1 | none | 200 |
| `herness/store/ops/tasks.py` | SQL for task mechanics (R-08) | `TaskSqlMixin` | L1 | none | 300 |
| `herness/store/ops/__init__.py` (08 block) | Re-export of the 08 module-level functions (ENG §2.1) | `record_metric_samples`, `purge_metric_samples`, `purge_events`, `bind_core_backends` | L1 | none | +8 |

Approved suppressions (ENG §2.4, §3.4). Each carries a `# noqa` with the reason text given here.

| Location | Rule | Reason |
|----------|------|--------|
| `resilience/retry.py` `_invoke` | `BLE001` | Classification boundary: every foreign exception is converted by `classify()` (design 08 §5.2 "Wrapped functions convert foreign exceptions") |
| `resilience/deciders.py` `_call_entry` | `BLE001` | In-process decider crash moves the batch down the chain (design 08 §5.5) |
| `resilience/breaker.py` `run_due_probes` | `BLE001` | A probe function crash is recorded as a probe failure |
| `jobs/handlers.py` `run_handler` | `BLE001` | Job worker top level (ENG §3.4 allowed place) |
| `jobs/supervisor.py` `Supervisor._tick_guarded` | `BLE001` | Job worker top level (ENG §3.4 allowed place) |
| `resilience/_state.py` `ProcessState` | ENG §2.3 | Design-mandated per-process state: breaker cache (5 s), fault plan (loaded once), metric buffer, handler and probe registries, bound backends. Reset by the `reset_process_state` fixture (delta D08-19) |

## 3. Unit specs

Each block follows ENG §12.3. The parameter table and return type come first, then the field table, then "Algorithm" and "Errors". Every ops write of this spec runs inside impl 02 `run_write(fn, op=...)` (R-10), which applies the `sqlite_write` policy (U08-28) and the `sqlite.write` fault point; core units therefore never wrap a backend call in another `retry_call("sqlite_write", ...)`. That rule is not repeated in each block.

### 3.1 Shared types and errors

#### U08-01 `herness.core.types.jobs` — 08 literal aliases

| Name | Definition |
|------|------------|
| `GpuClass` | `Literal["none", "reasoning", "decider", "large"]` |
| `JobKind` | `Literal["sync", "reconcile", "build_pipeline", "distill", "review", "chat", "outcome_measure", "memory_maintenance", "maintenance", "eval"]` |
| `ServiceName` | `Literal["vllm-reasoning", "openjev", "llamacpp-large"]` |
| `ChatMode` | `Literal["live", "small_model", "defer", "cloud"]` |
| `BreakerState` | `Literal["closed", "open", "half_open"]` |
| `PolicyName` | `Literal["source_http_page", "llm_local", "llm_large", "llm_cloud", "decider_local", "decider_cloud", "embed_batch", "tool_store", "warehouse_read", "sqlite_write", "gpu_health"]` |

| Field | Content |
|-------|---------|
| Kind | constant (type aliases) |
| Purpose | Closed sets shared by 08 and its consumers (design 08 §3.1, §3.4, §3.6). Defined in `herness/core/types/jobs.py` and re-exported from `herness.core.types` (R-01); `ChatMode` is listed in `TYPE_OWNERS` by impl 00 and the other five aliases are appended to `TYPE_OWNERS` under owner `08` by T08-01. |
| Postconditions | Values match design 08 exactly; `typing.get_args` of each alias is used for runtime membership checks. |
| Concurrency | Immutable. |
| Security notes | Closed sets are the allowlists for config, pipe messages and compose arguments (TH08-01). |
| Tests | UT08-01 |

#### U08-02 `herness.core.types.jobs.JobSpec`

| Field name | Type | Default | Constraints |
|------------|------|---------|-------------|
| `kind` | `JobKind` | required | member of `JobKind` |
| `payload` | `dict[str, JsonValue]` | required | validated by U08-45 at submit |
| `gpu_class` | `GpuClass` | required | |
| `priority` | `int \| None` | `None` | `None` or 0 ≤ value ≤ 100; `None` = `DEFAULT_PRIORITY[kind]` (U08-43, R-41) |
| `max_attempts` | `int \| None` | `None` | `None` or 1 ≤ value ≤ 20; `None` = `R.jobs.max_attempts[kind]` |
| `scheduled_for` | `datetime \| None` | `None` | timezone-aware; `None` = now at submit |
| `idem_key` | `str \| None` | `None` | ≤ 200 chars, regex `^[A-Za-z0-9_:.\-/]+$`; `None` = U08-44 |

| Field | Content |
|-------|---------|
| Kind | class (pydantic v2 model, `extra="forbid"`, `strict=True`, `frozen=True`) |
| Purpose | Request to create one job (design 08 §3.4). |
| Preconditions | Construction validates the constraints; violations raise pydantic `ValidationError`, which `submit` converts to `SchemaViolation`. |
| Postconditions | Instance is immutable. |
| Concurrency | Immutable. |
| Security notes | Naive `scheduled_for` rejected (ENG §3.2). |
| Tests | UT08-01 |

#### U08-03 `herness.core.types.jobs.JobOutcome`

| Field name | Type | Default | Constraints |
|------------|------|---------|-------------|
| `status` | `Literal["done", "yield"]` | required | |
| `result` | `dict[str, JsonValue]` | `{}` | canonical JSON ≤ 1 MiB (1 048 576 bytes); key `state` is dropped on `done` |

| Field | Content |
|-------|---------|
| Kind | class (pydantic v2, `extra="forbid"`, `frozen=True`) |
| Purpose | Handler return value (design 08 §3.4). `yield` means stopped at a safe point, requeue without attempt charge. |
| Postconditions | Oversized `result` raises `ValidationError`; `run_handler` (U08-56) converts it to `SchemaViolation`. |
| Concurrency | Immutable. |
| Security notes | TH08-10 (size cap). |
| Tests | UT08-02 |

#### U08-04 `herness.core.jobs.JobContext` and `herness.core.jobs.ServiceControl`

| Member | Signature | Meaning |
|--------|-----------|---------|
| `JobContext.job` | `JobRow` (U08-42) | the claimed row; handlers read their payload from `ctx.job.payload` (R-42); the row is frozen and the payload is a read-only mapping |
| `JobContext.job_id` | `str` | shortcut for `job.job_id` |
| `JobContext.kind` | `JobKind` | shortcut for `job.kind` |
| `JobContext.attempt` | `int` | shortcut for `job.attempts` after this claim |
| `JobContext.services` | `ServiceControl` | |
| `JobContext.should_yield()` | `-> bool` | true once a `stop` was received |
| `JobContext.stop_reason` | property `-> Literal["cancel", "preempt", "shutdown"] \| None` | |
| `JobContext.heartbeat(note=None)` | `(note: str \| None) -> None` | progress signal |
| `JobContext.require_gpu_class(cls, *, timeout_s=None)` | `(GpuClass, float \| None) -> None` | blocking swap |
| `JobContext.gpu_scope(cls)` | context manager `-> Iterator[None]` | switch, then restore |
| `JobContext.save_state(state)` / `load_state()` | `(dict) -> None` / `-> dict` | job checkpoint |
| `ServiceControl.start(name, *, timeout_s=None)` / `stop(name)` / `healthy(name)` | `ServiceName` | in-class service control |

| Field | Content |
|-------|---------|
| Kind | protocol (two `typing.Protocol` classes) |
| Purpose | The handler-facing contract owned by 08 (spec 00 §6). It is behavioral, so it lives in `herness/core/jobs/ports.py` and is re-exported from `herness.core.jobs`, not from `herness.core.types` (R-02; impl 00 `DECLARED_ELSEWHERE`). Concrete classes are U08-85 and U08-86. |
| Postconditions | Every handler has the signature `(ctx: JobContext) -> JobOutcome` and takes no other argument (R-42); both concrete classes satisfy the protocol structurally (checked by mypy). |
| Concurrency | Implementations are used from the handler thread only; `should_yield` is also safe from other threads of the handler. |
| Tests | UT08-98, UT08-99 |

#### U08-05 `herness.core.errors.JobStateError`

| Field | Content |
|-------|---------|
| Kind | class (`FatalError` subclass; spec-local taxonomy subclass declared by its owner, R-19) |
| Purpose | A job or task is not in the state the operation needs: unknown `job_id`, `retry` of a non-failed job, checkpoint or completion of a task that is not `running`, inline run of a job that cannot be claimed. |
| Signature | `JobStateError(message: str, *, job_id: str \| None = None, task_id: str \| None = None, run_id: str \| None = None)`; attributes of the same names. |
| Postconditions | Message names the operation and the identifiers; never payload or text. |
| Tests | UT08-61, UT08-69 |

#### U08-101 `herness.core.types.jobs.MetricSample`

| Field name | Type | Default | Constraints |
|------------|------|---------|-------------|
| `ts` | `datetime` | required | timezone-aware |
| `name` | `str` | required | regex `^herness_[a-z][a-z0-9]*(_[a-z0-9]+)+$` (the table's `GLOB 'herness_*'` check, narrowed) |
| `kind` | `Literal["counter", "gauge", "histogram"]` | required | the `metric_sample.kind` CHECK set of impl 02 migration 006 |
| `value` | `float` | required | finite; ≥ 0 for `counter` and `histogram` |
| `labels` | `dict[str, str]` | `{}` | ≤ 6 keys; key regex `^[a-z][a-z0-9_]{0,31}$`; value regex `^[A-Za-z0-9_.:\-]{1,64}$`; canonical JSON ≤ 1 024 bytes |
| `component` | `str` | required | regex `^[a-z][a-z0-9_]{0,31}$` |

| Field | Content |
|-------|---------|
| Kind | class (pydantic v2 model, `extra="forbid"`, `strict=True`, `frozen=True`) |
| Purpose | One `metric_sample` row as every spec hands it to the single writer `herness.store.ops.metrics.record_metric_samples` (U08-100, R-12); the L0 recorder (U08-22) builds the same type. |
| Preconditions | Construction validates the constraints; violations raise pydantic `ValidationError`. |
| Postconditions | Instance is immutable; its fields map one to one onto the impl 02 §4.3.6 columns. |
| Concurrency | Immutable. |
| Security notes | TH08-02: the label regex admits identifiers only, never free text. |
| Tests | UT08-31, UT08-110 |

### 3.2 Configuration models

#### U08-06 `herness.core.resilience.settings.ResilienceSection`

Pydantic v2 models, all `extra="forbid"`, `strict=True`, `frozen=True`. Field defaults equal design 08 §7 exactly.

| Model | Fields (type, default, validation) |
|-------|------------------------------------|
| `PolicySettings` | `attempts: int` (≥ 1, ≤ 100); `base_s: float` (> 0); `cap_s: float` (≥ `base_s`); `max_elapsed_s: float` (> 0); `timeout_s: float \| None = None` (> 0); `connect_timeout_s: float \| None = None` (> 0); `retry_after_cap_s: float = 0` (≥ 0) |
| `RetrySettings` | `policies: dict[str, PolicySettings]` (keys must be exactly the `PolicyName` members minus `gpu_health`; missing or extra key → error); `job_backoff: JobBackoffSettings` (`base_s: float = 60`, `cap_s: float = 3600`); `retry_after_max_s: float = 86400` (> 0; the clamp of U08-17, additive key, delta D08-27) |
| `BreakerSettings` | `failure_threshold: int` (≥ 1); `cooldown_s: float` (> 0); `cooldown_max_s: float` (≥ `cooldown_s`) |
| `BreakersSection` | `source`, `model`, `decider`: `BreakerSettings`; `restart_max_per_hour: int = 1` (≥ 0) |
| `FallbackSettings` | `max_repairs: int = 2` (0–5); `decider_chain: dict[str, list[str]]` (keys are profile names; values non-empty, unique names matching `^[a-z][a-z0-9_]{0,31}$`) |
| `LoopSettings` | `checkpoint_min_interval_s: float = 5` (≥ 0); `stop_on_signal_no: int = 2` (≥ 1) |
| `TaskSettings` | `max_task_attempts: int = 3` (≥ 1) |
| `JobsSettings` | `lease_s: int = 300`; `heartbeat_s: int = 30`; `reaper_interval_s: int = 30`; `tick_s: float = 2`; `cancel_grace_s: int = 60`; `shutdown_grace_s: int = 120`; `stall_timeout_s: int = 1800`; `stall_timeout_large_s: int = 7200`; `cpu_slots: int = 2` (0–16); `exclusive_kinds: list[JobKind]`; `max_attempts: dict[JobKind, int]` (every `JobKind` present, each 1–20). Cross-check: `heartbeat_s * 3 < lease_s` |
| `HealthCheck` | `path: str` (starts with `/`); `bearer_secret: str \| None = None` (secret reference, regex `^secret:[A-Z][A-Z0-9_]{0,63}$`, checked in this module because a settings module may not import `herness.core.secrets`, R-03; the OpenJev default is `secret:OPENJEV_API_KEY`, R-53) |
| `ServiceSettings` | `url: str` (scheme `http`, host `127.0.0.1`, `localhost` or `::1`, explicit port; defaults `http://127.0.0.1:8000` for `vllm-reasoning`, `http://127.0.0.1:8100` for `openjev`, `http://127.0.0.1:8200` for `llamacpp-large`, R-51); `health: HealthCheck`; `start_timeout_s: int` (> 0); `start_on_entry: bool = True` |
| `GpuClassSettings` | `services: dict[ServiceName, ServiceSettings]` |
| `GpuSettings` | `compose_cmd: list[str]` (non-empty, each item without NUL); `compose_file: str`; `vram_check_cmd: list[str]`; `vram_free_threshold_mb: int = 2000`; `stop_timeout_s: int = 120`; `warmup_timeout_s: int = 120`; `classes: dict[Literal["reasoning","decider","large"], GpuClassSettings]` (all three present; a service name appears in exactly one class) |
| `ResilienceSection` | `retry`, `breakers`, `fallback`, `loop`, `tasks`, `jobs`, `gpu` |

| Field | Content |
|-------|---------|
| Kind | class (pydantic section models) |
| Purpose | Typed `resilience` key of `config/resilience.yaml` (design 08 §7). Loaded by spec 10 as part of `ResilienceConfig` (U08-07). |
| Preconditions | The module imports only the standard library, pydantic, `herness.core.types` and `herness.core.errors` (R-03). |
| Postconditions | A valid instance satisfies every constraint above; defaults reproduce design 08 §7. |
| Errors | Validation failure → pydantic `ValidationError`; spec 10 loader reports it as `ConfigError` with the key path. |
| Concurrency | Immutable. |
| Security notes | URL loopback check and service-name allowlist feed TH08-01 and TH08-12. |
| Tests | UT08-03 |

#### U08-07 `herness.core.resilience.settings.ScheduleSection` and `ResilienceConfig`

These models live in `herness/core/resilience/settings.py` next to U08-06 (moved from the former `herness/core/jobs/settings.py`), so that no settings module imports another herness module outside `herness.core.types` and `herness.core.errors` (R-03). Every cron field below is checked here only for shape: five whitespace-separated fields, each matching `^[0-9A-Za-z*/,\-]+$`. The full parse, the week coverage of windows and the source schedules are checked by U08-99.

| Model | Fields (type, default, validation) |
|-------|------------------------------------|
| `WindowSpec` | `name: str` (`^[a-z][a-z0-9_]{0,31}$`); `start: str`, `end: str` (`^([01]\d\|2[0-3]):[0-5]\d$`, `start != end`); `days: list[Literal["MON","TUE","WED","THU","FRI","SAT","SUN"]] \| None = None` (None = every day; the day is the day the window starts); `classes: list[Literal["reasoning","decider","large"]]` (non-empty, unique, preference order); `preload: Literal["reasoning","decider","large"] \| None = None` (must be in `classes`); `hard_start: bool = False`; `overrun_max_min: int = 0` (0–240) |
| `ChatSchedule` | `off_hours: Literal["small_model","defer","cloud"] = "small_model"`; `in_hours_unavailable`: same type, default `"small_model"` |
| `RekeySchedule` | `cron: str = "0 19 * * SAT"` (cron shape; full parse by U08-99); `min_notice_h: int = 12` (0–168) |
| `ChainStep` | `kind: JobKind`; `gpu_class: GpuClass`; `payload: dict[str, JsonValue] = {}`; `skip_on: list[weekday] = []`; `enabled: bool = True`; `priority: int \| None = None` (0–100; None = `DEFAULT_PRIORITY[kind]`) |
| `ScheduledJob` | `name` (`^[a-z][a-z0-9_]{0,31}$`, unique, not starting with `sync.` or `reconcile.` and not `maintenance`); `cron: str` (cron shape; full parse by U08-99); `catch_up_max: str` (`^\d+(m\|h\|d)$`, parsed to `timedelta` ≤ 7 d); `job: ChainStep`; `then: list[ChainStep] = []` (≤ 10) |
| `MaintenanceSchedule` | `catch_up_max: str = "12h"` |
| `ScheduleSection` | `windows: list[WindowSpec]`; `preempt_grace_min: int = 15`; `batch_in_chat_min_priority: int = 70`; `chat: ChatSchedule`; `rekey: RekeySchedule`; `jobs: list[ScheduledJob]`; `maintenance: MaintenanceSchedule` |
| `ResilienceConfig` | `resilience: ResilienceSection`; `schedule: ScheduleSection`. Model validator: every class and every `preload` in every window is a key of `resilience.gpu.classes`; every `ChainStep.gpu_class` other than `none` is such a key. Week coverage is not checked here (U08-67 through U08-99) |

| Field | Content |
|-------|---------|
| Kind | class (pydantic section models) |
| Purpose | Typed `schedule` key and the root model spec 10 mounts at `cfg.resilience` (design 08 §7, spec 10 §3.1). |
| Preconditions | The module imports only the standard library, pydantic, `herness.core.types` and `herness.core.errors` (R-03). |
| Postconditions | Defaults reproduce design 08 §7 `schedule`, except that the `nightly` entry's `job.gpu_class` is `none`: the build pipeline starts without a GPU class and its enrichment stages take `decider` through `ctx.gpu_scope("decider")` (R-43). A window set that fails U08-67 is reported by U08-99, not by this model. |
| Errors | `ValidationError` naming the path, for example `schedule.jobs[0].cron`. |
| Concurrency | Immutable. |
| Tests | UT08-05 |

#### U08-67 `herness.core.jobs.validate.validate_windows`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `windows` | `Sequence[WindowSpec]` | — | positional | |

Returns `list[str]` (issue messages; empty = valid).

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Check that windows cover each minute of the week exactly once, names are unique and exactly one window is named `chat` (design 08 §7 validation). |
| Postconditions | Returns every issue found, in minute order, at most 20 messages. |
| Complexity and limits | 10 080 minutes × window count; < 50 ms for 10 windows. |
| Tests | UT08-04, PT08-05 |

Algorithm:
1. Build an array `cover[10080]` of empty lists (minute of week, Monday 00:00 = 0) in naive local time. DST is ignored here because windows are wall-clock rules.
2. For each window and each day `d` in `days` (or all seven): start minute `s = d*1440 + start`, end `e = d*1440 + end`, adding 1440 when `end <= start`. For each minute `m` in `[s, e)`, append the window name to `cover[m % 10080]`.
3. Each minute with zero entries → issue `"minute <DAY HH:MM> not covered"`; with more than one → `"minute <DAY HH:MM> covered by <a>, <b>"`. Consecutive minutes with the same issue are merged into one message `"<DAY HH:MM>–<DAY HH:MM> ..."`.
4. Duplicate names → issue; count of windows named `chat` ≠ 1 → issue.

#### U08-99 `herness.core.jobs.validate.validate_resilience_config`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `cfg` | `HernessConfig` (T10-03 (herness.core.config.HernessConfig)) | — | positional | fully loaded |
| `offline` | `bool` | `True` | keyword-only | accepted for the R-71 `OwnerValidator` protocol; no check uses the network |

Returns `list[ConfigIssue]` (T10-03 (herness.core.config.ConfigIssue)); empty = valid.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | The checks that a settings module may not run because they need `herness.core.jobs.cron` (R-03): week coverage of the windows and a full parse of every cron that 08 schedules. Satisfies impl 10's `OwnerValidator` protocol. Every composition root (T09-20 (herness.cli.run_startup_validation), T09-13 (app.common.bootstrap.get_services), T09-27 (herness.cli.worker_bootstrap)) registers it as `register_owner_validator("resilience", validate_resilience_config)` (T10-12 (herness.core.config_validate.register_owner_validator), R-71) before `init_config`; `config validate` and `doctor` run it through `run_owner_validators`. The worker also calls it at start (U08-87 step 1). `offline` changes nothing: every check is local. |
| Preconditions | None beyond a loaded config. |
| Postconditions | Returns one `error` issue per problem, each with the key path and a message; never raises for a config problem. |
| Side effects | None. |
| Concurrency | Pure given `cfg`. |
| Complexity and limits | U08-67 bound plus one `CronExpr.parse` and one `next_after` per cron; < 100 ms for 50 schedules. |
| Tests | UT08-04, UT08-05 |

Algorithm:
1. For each message of `validate_windows(S.windows)` (U08-67) add an issue with path `schedule.windows`.
2. For each cron text with its path — `schedule.jobs[i].cron`, `schedule.rekey.cron`, and for every enabled source `sources.<name>.schedule` and `sources.<name>.reconcile.schedule` when set — call `CronExpr.parse` (U08-64) and then `next_after(now_utc(), tz)`; a `ConfigError` from either becomes an issue with that path and the error message.
3. `cfg.backup.nightly_at` must match `^([01]\d|2[0-3]):[0-5]\d$` (it feeds the `maintenance` schedule, U08-71); otherwise an issue with path `backup.nightly_at`.
4. Return the issues in the order found.

### 3.3 Ports, process state and binding

#### U08-08 `herness.core.resilience.ports`

| Name | Kind | Members |
|------|------|---------|
| `HealthRow` | frozen dataclass | `source: str`, `state: BreakerState`, `failures: int`, `trips: int`, `opened_at: datetime \| None`, `last_error: str \| None`, `updated_at: datetime` |
| `EventRow` | frozen dataclass | `event_id`, `ts`, `kind`, `component`, `target: str \| None`, `run_id`, `job_id`, `task_id: str \| None`, `detail: dict[str, JsonScalar \| list[JsonScalar]]` |
| `TracerLike` | protocol | read-only `run_id: str \| None`, `task_id: str \| None` (R-66); `emit(type: str, **fields: object) -> None`. Impl 05 `herness.harness.tracing.Tracer` satisfies it structurally; L0 code never imports it (ENG §2.1) |
| `ResilienceBackend` | protocol | `health_get(key) -> HealthRow \| None`; `health_list(states: Sequence[BreakerState]) -> list[HealthRow]`; `health_apply(key, fn: Callable[[HealthRow \| None], HealthRow \| None], now) -> tuple[HealthRow \| None, HealthRow \| None]` (read-modify-write in one `BEGIN IMMEDIATE`; returns (before, after); `fn` returning `None` means no write); `health_claim_probe(key, now, probe_due, stale_before) -> bool`; `health_reset(keys: Sequence[str], now) -> list[str]`; `insert_event(row: EventRow) -> None`; `count_events(kind, *, target=None, since) -> int`; `event_counts(since, kinds) -> dict[str, int]`; `latest_event(kind) -> EventRow \| None`; `insert_metric_samples(rows: Sequence[MetricSample]) -> int` (implemented by U08-100); `purge_events(before) -> int`; `purge_metric_samples(before) -> int` |
| `ClientInfo` | protocol | attributes `off_network: bool`, `gpu_class: str \| None`, `timeout_s: float`, `model: str`, `base_url: str \| None`, `api_key: str \| None` |
| `ChainRegistry` | protocol | `chain_for(model_role: str, depth: str) -> list[str]`; `config(name: str) -> ClientInfo` (spec 05 `LLMRegistry` satisfies it structurally) |
| `GpuStateReader` | protocol | `loaded_class() -> GpuClass \| Literal["swapping"]`; `service_healthy(name: ServiceName) -> bool` (design 08 §3.5) |
| `AsyncCompleter` | protocol | `name: str`; `async acomplete(req: LLMRequest) -> LLMResponse` (structural subset of spec 05 `LLMClient`) |
| `DeciderLike` | protocol | `name: str`; `decide(items, questions) -> list[DecisionOutput]`; `health() -> None` |

| Field | Content |
|-------|---------|
| Kind | protocol (module of protocols and row types) |
| Purpose | Let L0 code depend on L1 SQL and L4 registries without importing them (ENG §2.1, §2.2). |
| Preconditions | Imports only `herness.core.types`, `herness.core.errors`, stdlib. |
| Concurrency | Backend implementations use one SQLite connection per thread (ENG §2.5). |
| Tests | covered by the tests of their users |

#### U08-09 `herness.core.resilience._state.ProcessState`, `process_state`, `reset_process_state`

| Attribute | Type | Meaning |
|-----------|------|---------|
| `ops` | `ResilienceBackend \| None` | bound backend |
| `jobs` | `JobsBackend \| None` | bound jobs backend (set by U08-41) |
| `chains` | `ChainRegistry \| None` | bound model registry |
| `breakers` | `dict[str, CircuitBreaker]` | registry of U08-25 |
| `probes` | `dict[str, Callable[[str], None]]` | U08-27, keyed by `source`, `model`, `decider` |
| `handlers` | `dict[JobKind, Callable[[JobContext], JobOutcome]]` | U08-55 |
| `fault_plan` | `FaultPlan \| None \| _NotLoaded` | U08-33 |
| `faults_enabled` | `bool` | |
| `metric_buffer` | `MetricBuffer` | U08-19 |
| `auth_dropped` | `set[tuple[str, str]]` | `(run_id, client_key)` dropped after `AuthError` (U08-38) |
| `rng` | `random.Random` | `secrets.SystemRandom()` in production; tests replace it with a seeded `random.Random` |
| `sleep`, `asleep` | callables | `time.sleep`, `asyncio.sleep`; tests replace them |
| `kill_service_hook` | `Callable[[str], None] \| None` | set by U08-81 for the `kill_service:` fault action |
| `policies_cache` | `dict[str, RetryPolicy]` plus the `config_hash` it was built for | U08-12 |
| `lock` | `threading.Lock` | guards every mutable attribute above |

| Field | Content |
|-------|---------|
| Kind | class plus two functions |
| Purpose | The only module-level mutable state of 08 (ENG §2.3 exception D08-19). `process_state()` returns the singleton; `reset_process_state()` replaces it with a fresh instance (test fixture). |
| Invariants | Exactly one instance per process; child processes build their own (spawn). |
| Concurrency | Lock-protected (`ProcessState.lock`) for writes; reads of immutable snapshots need no lock. |
| Tests | fixture used by every unit test |

#### U08-10 `herness.core.resilience.bind_ops_backend`, `bind_chain_registry`

| Param | Type | Kind | Constraints |
|-------|------|------|-------------|
| `backend` | `ResilienceBackend` | positional | not `None` |
| `registry` | `ChainRegistry` | positional | not `None` |

Returns `None`.

| Field | Content |
|-------|---------|
| Kind | function (two) |
| Purpose | Composition-root wiring of the ports (ENG §2.2). Called by `herness.store.ops.resilience.bind_core_backends()` (U08-98) and by the CLI and app entry points for the chain registry (T09-20 (herness.cli.main), T09-13 (app.common.bootstrap.get_services)). |
| Postconditions | `process_state().ops` / `.chains` set; rebinding replaces the previous value and logs `resilience.backend.bound` at DEBUG. `bind_ops_backend` also registers `atexit` flushing of metrics (U08-22) once. |
| Errors | Any 08 function that needs an unbound port raises `ConfigError("resilience backend not bound; call herness.store.ops.resilience.bind_core_backends()")`. |
| Tests | UT08-29 (unbound case) |

#### U08-41 `herness.core.jobs.ports.JobsBackend`, `NewJob`, `WorkerRow`, `bind_jobs_backend`

| Method | Signature | Meaning (SQL in U08-95–U08-97) |
|--------|-----------|--------------------------------|
| `insert_job` | `(job: NewJob, *, sched_check: SchedCheck \| None) -> tuple[str, bool]` | enqueue transaction of design 08 §5.7 |
| `claim_job` | `(*, owner, now, lease_until, allowed_classes, exclusive_kinds, job_id, min_priority, priority_exempt_kinds) -> JobRow \| None` | atomic claim |
| `claimable_counts` | `(*, now, classes, exclusive_kinds, min_priority, priority_exempt_kinds) -> dict[GpuClass, int]` | for the arbiter |
| `heartbeat_job` | `(job_id, owner, lease_until) -> bool` | lease extension |
| `finish_done` / `finish_yield` / `finish_requeue` / `finish_failed` / `finalize_canceled` | owner-guarded completion statements | §4.1.1 |
| `save_job_state` / `load_job_state` | `(job_id, owner, state_json) -> bool` / `(job_id) -> dict` | `job.result = {"state": ...}` |
| `cancel_job` | `(job_id, now) -> Literal["canceled","cancel_requested","not_active"]` | |
| `retry_job` | `(job_id, now) -> Literal["queued","not_failed","conflict","missing"]` | |
| `get_job` / `list_jobs` | reads | |
| `reap_expired` | `(now) -> list[tuple[str, JobKind, Literal["queued","failed"]]]` | reaper statements |
| `requeue_owned` | `(owners: Sequence[str], now) -> list[tuple[str, JobKind, Literal["queued","failed"]]]` | reaper statements restricted to these lease owners |
| `running_on_host` | `(host) -> list[JobRow]` | crash recovery |
| `postpone_class` | `(gpu_class, now, until) -> int` | swap failure: `scheduled_for = until` for queued jobs of that class due now |
| `sched_fired` | `(schedule, fire_at_ts) -> bool` | any job, any status, with `payload.schedule`/`payload.fire_at` |
| `recent_scheduled` | `(since) -> list[JobRow]` | done or failed jobs with `payload.schedule`, finished since |
| `rekey_on` | `(start, end) -> JobRow \| None` | `maintenance` job with `payload.action = 'rekey'`, status queued, running or done, `scheduled_for` in `[start, end)` |
| `queue_stats` | `(now, since) -> QueueStats` | counts for status |
| `upsert_worker` / `update_worker` / `list_workers` | worker row writes and reads | U08-96 |
| `run_row` | `(run_id) -> tuple[str, str] \| None` | `(kind, status)` of `run` |
| task methods | see U08-97 | |

| Field | Content |
|-------|---------|
| Kind | protocol plus row models plus a binding function |
| Purpose | The SQL port for `herness.core.jobs`. `NewJob` = all insert columns of `job`; `WorkerRow` = all `worker` columns with JSON parsed; `SchedCheck` = `(schedule: str, fire_at: str)`. `bind_jobs_backend(backend)` sets `process_state().jobs`. |
| Errors | Unbound use → `ConfigError` (same message pattern as U08-10). |
| Tests | covered by U08-95–U08-97 tests |

#### U08-42 `herness.core.jobs.JobRow`

| Field | Type |
|-------|------|
| `job_id`, `kind`, `gpu_class`, `status` | `str` (literal types) |
| `priority`, `attempts`, `max_attempts` | `int` |
| `payload` | `dict` |
| `idem_key` | `str \| None` |
| `result`, `last_error` | `dict \| None` |
| `lease_owner` | `str \| None` |
| `lease_expires_at`, `scheduled_for`, `created_at`, `started_at`, `finished_at` | `datetime \| None` (aware UTC) |

| Field | Content |
|-------|---------|
| Kind | class (pydantic v2, `frozen=True`, `extra="forbid"`) |
| Purpose | Parsed `job` row returned by queue functions and read by spec 09. |
| Tests | UT08-63 |

### 3.4 Policies and classification

#### U08-11 `herness.core.resilience.policies.RetryPolicy`, `POLICY_FAMILY`, `GPU_HEALTH_POLICY`

| Field name | Type | Meaning |
|------------|------|---------|
| `name` | `str` | policy name |
| `attempts` | `int` | total tries including the first |
| `base_s`, `cap_s` | `float` | full-jitter parameters |
| `max_elapsed_s` | `float` | stop after this many seconds since the first attempt |
| `timeout_s` | `float \| None` | per attempt; `None` = caller supplies |
| `connect_timeout_s` | `float \| None` | |
| `retry_after_cap_s` | `float` | 0 = any positive `retry_after` re-raises |

| Constant | Value |
|----------|-------|
| `POLICY_FAMILY` | `source_http_page` → `source`; `llm_local`, `llm_large`, `llm_cloud`, `embed_batch`, `gpu_health` → `model`; `decider_local`, `decider_cloud` → `decider`; `tool_store`, `warehouse_read`, `sqlite_write` → `store` |
| `GPU_HEALTH_POLICY` | `RetryPolicy("gpu_health", attempts=10000, base_s=2, cap_s=10, max_elapsed_s=900, timeout_s=5, connect_timeout_s=5, retry_after_cap_s=0)`; callers replace `max_elapsed_s` with the class `start_timeout_s` via `dataclasses.replace` |

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass) plus constants |
| Purpose | Design 08 §3.1, §5.2 table. |
| Tests | UT08-06 |

#### U08-12 `herness.core.resilience.policy`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `name` | `PolicyName` | — | positional | member of `PolicyName` |

Returns `RetryPolicy`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Look up a policy (design 08 §3.1). |
| Postconditions | For `gpu_health` returns `GPU_HEALTH_POLICY`; otherwise builds from `R.retry.policies[name]`. |
| Complexity and limits | O(1) after first call per `config_hash`. |
| Tests | UT08-06 |

Algorithm:
1. If `name == "gpu_health"` return `GPU_HEALTH_POLICY`.
2. Read `h = config_hash(cfg)` (T10-03 (herness.core.config.config_hash)). If `process_state().policies_cache` was built for `h` and holds `name`, return it.
3. Otherwise rebuild the whole cache from `R.retry.policies` (one `RetryPolicy` per key) under `ProcessState.lock`, record `h`, and return the entry.

Errors:

| Condition | Error class | Message identifiers |
|-----------|-------------|---------------------|
| `name` not a `PolicyName` | `ConfigError` | `policy`, the name |

#### U08-13 `herness.core.resilience.policies.policy_for_client`

| Param | Type | Kind |
|-------|------|------|
| `info` | `ClientInfo` | positional |

Returns `RetryPolicy`.

| Field | Content |
|-------|---------|
| Kind | function (pure except the `policy` lookup) |
| Purpose | Pick the LLM policy per client (design 08 §5.2 table). |
| Algorithm | 1. `info.off_network` → `policy("llm_cloud")`. 2. Else `info.gpu_class == "large"` → `policy("llm_large")`. 3. Else `policy("llm_local")`. |
| Tests | UT08-07 |

#### U08-14 `herness.core.resilience.policies.full_jitter_delay`, `FullJitterRetryAfter`

| Param | Type | Kind | Constraints |
|-------|------|------|-------------|
| `attempt` | `int` | positional | ≥ 1, the attempt that just failed |
| `p` | `RetryPolicy` | positional | |
| `retry_after` | `float \| None` | keyword-only | ≥ 0 |
| `rng` | `random.Random` | keyword-only | |

Returns `float` seconds.

| Field | Content |
|-------|---------|
| Kind | function (pure) and class (`tenacity.wait.wait_base` subclass) |
| Purpose | Full jitter with Retry-After (design 08 §5.2). |
| Algorithm | 1. `ceiling = min(p.cap_s, p.base_s * 2 ** (attempt - 1))`; `jitter = rng.uniform(0, ceiling)`. 2. If `retry_after is None` return `jitter`. 3. Return `min(max(retry_after, jitter), p.retry_after_cap_s)`. `FullJitterRetryAfter(p, rng).__call__(retry_state)` takes `attempt = retry_state.attempt_number` and `retry_after` from the last exception when it is a `RateLimited`, else `None`. |
| Postconditions | Without `retry_after`: `0 ≤ result ≤ ceiling`. With `retry_after ≤ retry_after_cap_s` (guaranteed by U08-28 step 3): `retry_after ≤ result ≤ retry_after_cap_s`. |
| Tests | PT08-01, UT08-08 |

#### U08-15 `herness.core.resilience.policies.job_backoff_delay`

| Param | Type | Kind | Constraints |
|-------|------|------|-------------|
| `attempts` | `int` | positional | ≥ 1 |
| `rng` | `random.Random` | keyword-only | |

Returns `float` seconds: `rng.uniform(0, min(R.retry.job_backoff.cap_s, R.retry.job_backoff.base_s * 2 ** (attempts - 1)))` (design 08 §5.2 "Job" column, defaults 60 s and 3600 s). Kind: function (pure given config). Tests: PT08-02.

#### U08-16 `herness.core.resilience.classify`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `exc` | `BaseException` | — | positional | |
| `family` | `Literal["source","model","decider","store"]` | — | keyword-only | |

Returns `HernessError` (not raised; callers use `raise classify(e, family=...) from e`).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Map a foreign exception to the spec 00 §7 taxonomy without string matching on messages, except the SQLite and DuckDB lock texts the design names (design 08 §5.2 "classify() mapping"). |
| Preconditions | None. |
| Postconditions | The returned error carries a message `"<family> call failed: <ExceptionType>[ HTTP <status>][: <detail>]"` where `<detail>` is the response body cut to 500 chars and then passed through `redact_text` (T10-10 (herness.core.redact.redact_text)); total message ≤ 2 KB. |
| Side effects | None. |
| Concurrency | Thread-safe, async-safe. |
| Complexity and limits | O(1); body cut before redaction bounds redaction cost. |
| Security notes | TH08-02 (no secrets or text in messages), TH08-04 (Retry-After parsed here and capped later). |
| Tests | UT08-10, UT08-11 |

Algorithm (first matching rule wins; "unavailable" means `SourceUnavailable` for family `source`, `ModelUnavailable` for `model` and `decider`, `StoreBusy` for `store`):
1. `exc` is a `HernessError` → return it unchanged.
2. `httpx.HTTPStatusError` (status from `exc.response.status_code`): 429 → `RateLimited(retry_after=parse_retry_after(exc.response.headers, now))`; 401 or 403 → `AuthError`; 500, 502, 503, 504, 529 → unavailable; any other status → `ConfigError("unexpected HTTP <status>")`.
3. `httpx.ConnectError`, `httpx.ConnectTimeout`, `httpx.ReadTimeout`, `httpx.WriteTimeout`, `httpx.PoolTimeout`, `httpx.RemoteProtocolError` → unavailable.
4. If module `openai` is in `sys.modules`: `openai.APIConnectionError` or `openai.APITimeoutError` → `ModelUnavailable`; `openai.APIStatusError` → apply rule 2 to `exc.status_code` and `exc.response.headers`. Same for `anthropic` with `anthropic.APIConnectionError`, `anthropic.APITimeoutError`, `anthropic.APIStatusError`; an `anthropic` exception class named `OverloadedError` (looked up with `getattr`, absent in older versions) → `ModelUnavailable`. The `sys.modules` check means core never imports these SDKs.
5. `sqlite3.OperationalError` whose lower-cased text contains `"locked"` or `"busy"` → `StoreBusy`; other `sqlite3.OperationalError` → `FatalError("sqlite error")`.
6. If `duckdb` is in `sys.modules`: `duckdb.InterruptException` → `QueryError("query interrupted after timeout", timeout=True)`; `duckdb.IOException` whose lower-cased text contains `"lock"` → `StoreBusy`.
7. `TimeoutError` or `concurrent.futures.TimeoutError` → unavailable.
8. Anything else → `FatalError("unclassified <ExceptionType>")`.

#### U08-17 `herness.core.resilience.classify.parse_retry_after`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `headers` | `Mapping[str, str]` | — | positional | case-insensitive lookup |
| `now` | `datetime` | — | positional or keyword | aware; injected, never read from the clock here |
| `max_s` | `float \| None` | `None` | keyword-only | > 0; `None` = `R.retry.retry_after_max_s` (default 86 400 s) |

Returns `float | None` seconds, in `[0, max_s]`.

| Field | Content |
|-------|---------|
| Kind | function (pure when `max_s` is given; otherwise reads one config value) |
| Purpose | The single Retry-After parser of the codebase (owner 08). Impl 01 replaces its own `herness.connectors.http.parse_retry_after` (U01-61) with this function; every HTTP adapter uses it. |
| Algorithm | 1. `Retry-After` present (value stripped): RFC 9110 delay-seconds form, regex `^\d{1,10}$` → `d = float(value)`. Otherwise the RFC 9110 HTTP-date form: `email.utils.parsedate_to_datetime(value)`; a result without a timezone is invalid; a valid date gives `d = (date − now).total_seconds()`. Any other text, or a parse error, makes the header invalid and the function goes to step 2. 2. `Retry-After` absent or invalid, and `X-RateLimit-Reset` present with a value matching `^\d{1,16}(\.\d+)?$`: `v ≥ 10^12` → epoch milliseconds, `d = v / 1000 − now.timestamp()`; `v ≥ 10^9` → epoch seconds, `d = v − now.timestamp()`; else `d = v` seconds. 3. No valid header → return `None`. 4. Return `min(max(d, 0.0), max_s)` (a date in the past gives 0). |
| Postconditions | Result is `None` or within `[0, max_s]`; never raises. The per-policy `retry_after_cap_s` rule of U08-28 applies afterwards: a value above the policy cap re-raises so the job layer reschedules at `now + retry_after`, which is therefore never more than `max_s` away. |
| Tests | PT08-03, UT08-112 |

### 3.5 Events and metrics

#### U08-18 `herness.core.resilience.record_event`, `EVENT_KINDS`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `kind` | `str` | — | positional | member of `EVENT_KINDS` |
| `component` | `Literal["resilience","jobs"]` | — | keyword-only | |
| `target` | `str \| None` | `None` | keyword-only | ≤ 200 chars: breaker key, policy name, job kind, service or schedule name |
| `run_id`, `job_id`, `task_id` | `str \| None` | `None` | keyword-only | ID formats of spec 00 §5 |
| `detail` | `Mapping[str, object]` | `{}` | keyword-only | keys filtered by `DETAIL_FIELDS[kind]` |
| `tracer` | `TracerLike \| None` | `None` | keyword-only | port of U08-08; impl 05 `Tracer` (T05-11 (herness.harness.tracing.Tracer)) satisfies it structurally |

Returns `None`.

`EVENT_KINDS` = the 21 kinds of design 08 §4.2. `DETAIL_FIELDS`:

| Kind | Allowed detail keys | Log level | Trace type |
|------|---------------------|-----------|------------|
| `retry` | `target`, `attempt`, `error_type`, `wait_s`, `policy`, `breaker_key`, `retry_after_s` | WARNING | `retry` |
| `fallback` | `from_profile`, `to_profile`, `reason` | WARNING | `fallback` |
| `repair` | `model_profile`, `repair_no`, `error_paths` | WARNING | `repair` |
| `guard_stop` | `cause`, `step` | WARNING | `guard_stop` |
| `breaker_open`, `breaker_half_open`, `breaker_close` | `key`, `failures`, `trips`, `reason`, `probe_due` | WARNING, INFO, INFO | — |
| `job_done`, `job_yield` | `kind`, `attempt`, `duration_s`, `stop_reason`, `partial` | INFO | — |
| `job_failed` | `kind`, `attempt`, `error_type` | ERROR | — |
| `lease_expired` | `kind`, `attempt`, `outcome` | WARNING | — |
| `gpu_swap` | `from`, `to`, `duration_s`, `reason` | INFO | — |
| `gpu_swap_failed` | `from`, `to`, `step`, `error_type` | ERROR | — |
| `service_restart`, `service_start`, `service_stop` | `service`, `duration_s`, `reason` | WARNING, INFO, INFO | — |
| `schedule_fired`, `schedule_missed` | `schedule`, `fire_at`, `step` | INFO, WARNING | — |
| `chain_broken`, `chain_skipped` | `schedule`, `fire_at`, `step`, `reason` | WARNING, INFO | — |
| `rekey_planned` | `fire_at`, `key_id` | INFO | — |

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Single writer of `resilience_event` rows, the matching log line and (when a tracer is given) the trace event (design 08 §2.7, §4.2, §4.4). |
| Preconditions | Ops backend bound (U08-10). |
| Postconditions | One row inserted with `event_id = "evt_" + new_ulid()` (T00-05 (herness.core.ids.new_ulid), D08-14) and `ts` = now; one log line with event name from §8.1 and field `kind`; trace event emitted only for the four trace kinds when `tracer` is not `None`. |
| Side effects | `resilience_event` insert; structlog line; `tracer.emit(type, **fields)`. |
| Concurrency | Thread-safe; the insert uses the calling thread's connection. |
| Complexity and limits | detail ≤ 20 keys; list values ≤ 20 items; string values ≤ 200 chars after redaction. |
| Security notes | TH08-02: detail values must be scalars or lists of scalars; each string passes `redact_text` and is cut to 200 chars; keys not in the allowlist are dropped and logged at DEBUG. |
| Tests | UT08-25, UT08-29, UT08-30, ST08-02 |

Algorithm:
1. Unknown `kind` → raise `ConfigError`.
2. Filter `detail` to `DETAIL_FIELDS[kind]`; convert `datetime` values to ts text; reject nested dicts (dropped with DEBUG log); redact and cut strings.
3. Insert the row through `process_state().ops.insert_event` inside a `try`. The backend insert runs in `run_write(op="resilience_event")`, whose `sqlite_write` policy is the only retry. On `HernessError` (including `StoreBusy` after that policy) log WARNING `resilience.event.dropped` with `kind` and `error_type` and continue; `record_event` never raises for a store failure, so an event failure cannot fail the operation being recorded.
4. Log the line at the table's level with fields `kind`, `target`, IDs and the filtered detail.
5. If `tracer` is given and the kind has a trace type, call `tracer.emit(trace_type, **filtered_detail)`.

#### U08-19 `herness.core.resilience.metrics.record_counter`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `name` | `str` | — | positional | regex `^herness_[a-z][a-z0-9]*(_[a-z0-9]+)+$`; last segment in `{total, seconds, bytes, ratio, count, mb}` |
| `value` | `float` | `1.0` | positional | finite, ≥ 0 |
| `component` | `str` | — | keyword-only | equals the second segment of `name` |
| `labels` | `Mapping[str, str] \| None` | `None` | keyword-only | ≤ 6 keys, key regex `^[a-z][a-z0-9_]{0,31}$`, value regex `^[A-Za-z0-9_.:\-]{1,64}$` |

Returns `None`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Record a counter increment for the ops `metric_sample` table (ENG §4, delta E5). L0 callers use this buffered API; callers at L1 and above may also call U08-100 directly with `MetricSample` values (R-12). |
| Postconditions | The value is added to the in-memory aggregate for `(name, canonical labels, component)`. |
| Side effects | May trigger `flush_metrics()` (U08-22) when the last flush is ≥ 10 s old (`METRIC_FLUSH_INTERVAL_S = 10`). |
| Concurrency | Buffer guarded by `ProcessState.lock`. |
| Complexity and limits | O(labels); validated names cached in a set so the check runs once per name. |
| Security notes | Labels are low-cardinality identifiers only; the value regex blocks free text (TH08-02). |
| Errors | Invalid name, labels or value → `ConfigError` naming the metric. |
| Tests | UT08-31, UT08-32 |

#### U08-20 `herness.core.resilience.metrics.record_histogram`

Same parameters as U08-19 except `value` is required (finite, ≥ 0). Each call appends one observation to the histogram list of the buffer. The buffer holds at most `METRIC_BUFFER_MAX = 10000` observations; beyond that the observation is dropped and `dropped` is incremented, and the next flush logs WARNING `resilience.metrics.dropped` with the count. When the list reaches 1 000 observations the call triggers a flush. Kind: function. Tests: UT08-31, UT08-32.

#### U08-21 `herness.core.resilience.metrics.timed`

| Param | Type | Kind |
|-------|------|------|
| `name` | `str` (unit `seconds`) | positional |
| `component` | `str` | keyword-only |
| `labels` | `Mapping[str, str] \| None` | keyword-only |

Returns a context manager. On exit (normal or exception) it records `time.perf_counter()` elapsed seconds with U08-20. Kind: function. Tests: UT08-32.

#### U08-103 `herness.core.resilience.metrics.record_gauge`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `name` | `str` | — | positional | regex of U08-19; last segment in `{bytes, ratio, count, mb, seconds}` (a point-in-time value, never `total`) |
| `value` | `float` | — | positional | finite (may be negative) |
| `component` | `str` | — | keyword-only | equals the second segment of `name` |
| `labels` | `Mapping[str, str] \| None` | `None` | keyword-only | rules of U08-19 |

Returns `None`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Record a gauge (a point-in-time value such as queue depth or GPU memory in use) for `metric_sample` with `kind = "gauge"` (answer to impl 03 request RQ-04). Callers at L1 and above that already hold values may instead pass `MetricSample(kind="gauge", ...)` directly to U08-100. |
| Postconditions | The buffer holds the latest value per `(name, canonical labels, component)`; a later call for the same key replaces the earlier value (last write wins). |
| Side effects | May trigger `flush_metrics()` (U08-22) on the same 10 s rule as U08-19. At flush, one `gauge` row per key with the latest value and the time of that last call as `ts`. |
| Concurrency | Buffer guarded by `ProcessState.lock`. |
| Complexity and limits | O(labels); at most 1 000 gauge keys per buffer (`METRIC_GAUGE_KEYS_MAX = 1000`); a new key beyond that is dropped and counted in `dropped` like U08-20. |
| Security notes | TH08-02 (label regex). |
| Errors | Invalid name, labels or a non-finite value → `ConfigError` naming the metric. |
| Tests | UT08-111 |

The job layer records two gauges of its own with this function: `herness_jobs_queue_depth_count{gpu_class}` (queued, due jobs per class, U08-87 step 7a) and `herness_jobs_gpu_vram_used_mb` (U08-80 reading after each swap, U08-81 step 7).

#### U08-22 `herness.core.resilience.metrics.flush_metrics`

Returns `int` (rows written).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Move the buffer into `metric_sample` rows. |
| Algorithm | 1. Under the lock, swap the buffer for an empty one. 2. Build `MetricSample` values (U08-101): one `counter` sample per aggregate key with the summed value, one `gauge` sample per gauge key with its latest value and that value's time (U08-103), one `histogram` sample per observation; `ts` = now except for gauges. 3. Call `ops.insert_metric_samples(samples)`, which calls `herness.store.ops.metrics.record_metric_samples` (U08-100, 500 rows per transaction). 4. On `HernessError` log WARNING `resilience.metrics.flush_failed` with the row count; the rows are discarded (metrics are best effort). 5. Return the number written. |
| Side effects | `metric_sample` inserts through the single writer U08-100 (R-12; table from T02-06 (herness/store/migrations/006_metric_sample.sql), columns in §4.1.6). |
| Concurrency | Safe from any thread; the supervisor calls it every reaper interval; `atexit` calls it once. |
| Tests | UT08-32 |

### 3.6 Circuit breakers and probes

#### U08-23 `herness.core.resilience.breaker.breaker_transition`, `probe_due`

| Param | Type | Kind | Constraints |
|-------|------|------|-------------|
| `row` | `HealthRow \| None` | positional | `None` = no row yet (treated as closed, 0, 0) |
| `event` | `Literal["failure","success","force_open","probe_claimed"]` | positional | |
| `now` | `datetime` | positional | aware UTC |
| `settings` | `BreakerSettings` | positional | family settings |
| `key` | `str` | keyword-only | |
| `error` | `str \| None` | keyword-only | redacted `last_error` text, ≤ 500 chars |

Returns `tuple[HealthRow, list[Literal["breaker_open","breaker_half_open","breaker_close"]]]`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | The state table of design 08 §5.3. |
| Algorithm | Let `s, f, t` = state, failures, trips. 1. `closed` + `failure`: if `f + 1 < threshold` → `f += 1`; else → `open`, `f += 1`, `opened_at = now`, `t += 1`, emit `breaker_open`. 2. `closed` or `half_open` + `force_open` → `open`, `opened_at = now`, `t += 1`, emit `breaker_open`. 3. `closed` + `success` → `f = 0`. 4. `open` + `probe_claimed` → `half_open`, emit `breaker_half_open`. 5. `half_open` + `success` → `closed`, `f = 0`, `t = 0`, emit `breaker_close`. 6. `half_open` + `failure` → `open`, `opened_at = now`, `t += 1`, emit `breaker_open`. 7. `open` + `failure`, `open` + `success`, `open` + `force_open` → unchanged except `last_error` (failure) — an in-flight call finishing while open does not change the state. Every result sets `updated_at = now`; `failure` and `force_open` set `last_error = error`. `probe_due(row, settings) = row.opened_at + cooldown`, `cooldown = min(settings.cooldown_s * 2 ** (row.trips - 1), settings.cooldown_max_s)` seconds. |
| Postconditions | Output satisfies `trips ≥ 0`, `failures ≥ 0`; `opened_at` set whenever state is `open`. |
| Tests | UT08-12, UT08-13, UT08-14 |

#### U08-24 `herness.core.resilience.CircuitBreaker`

| Member | Signature |
|--------|-----------|
| constructor | `CircuitBreaker(key: str)` (use `breaker(key)`, not the constructor) |
| `key` | `str` |
| `allow()` | `-> bool` |
| `record_success()` | `-> None` |
| `record_failure(err)` | `(err: HernessError) -> None` |
| `force_open(err)` | `(err: HernessError) -> None` |
| `state()` | `-> BreakerState` |
| `retry_at()` | `-> datetime \| None` (probe due time while open) |

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | Per-key breaker persisted in `source_health`, cached per process for 5 s (`BREAKER_CACHE_S = 5`), transitions written through (design 08 §3.1, §5.3). |
| Invariants | `_cached` is the last row read or written plus its read time; family settings come from the key prefix: `model:` → `R.breakers.model`, `decider:` → `R.breakers.decider`, anything else (connector names, `monitoring:<tool>`) → `R.breakers.source`. |
| Concurrency | One instance per key per process; methods take a per-instance `threading.Lock` around cache access; cross-process consistency comes from `BEGIN IMMEDIATE` in `health_apply` and the probe-claim `UPDATE`. |
| Complexity and limits | Hot path (`allow` with fresh closed cache, `record_success` with cached `closed` and `failures == 0`) does no I/O; that keeps retry overhead < 50 µs (BT08-01). |
| Security notes | TH08-05 (stops hammering down endpoints). |
| Tests | UT08-12–UT08-19, IT08-01, BT08-05 |

Algorithm:
1. `_row()`: if the cache is younger than 5 s return it; else `ops.health_get(key)` (a missing row reads as closed, 0, 0) and cache it.
2. `state()` returns `_row().state`.
3. `allow()`: `closed` → `True`. `half_open` → `False` unless `updated_at < now − HALF_OPEN_STALE_S` (600 s, a probe caller that died), in which case go to step 4 with the stale rule. `open`: if `now < probe_due(row)` → `False`; else step 4.
4. Probe claim: `ops.health_claim_probe(key, now, probe_due, stale_before=now − 600 s)` runs the design 08 §5.3 `UPDATE ... RETURNING` extended with `OR (state = 'half_open' AND updated_at < :stale_before)`. `True` → cache the half-open row, `record_event("breaker_half_open", ...)`, return `True`. `False` → re-read the row into the cache, return `False`.
5. `record_success()`: if the cached row is `closed` with `failures == 0` return without I/O. Else `ops.health_apply(key, fn)` where `fn` applies `breaker_transition(..., "success", ...)`; cache the result; emit the returned event kinds with `record_event`; increment `herness_resilience_breaker_transitions_total{key, to_state}` for each transition.
6. `record_failure(err)`: if `err` is not a `SourceUnavailable` or `ModelUnavailable`, return (not counted). Else `health_apply` with `"failure"` and `error = redact_text(str(err))[:500]`; cache; emit events and metrics as in step 5.
7. `force_open(err)`: `health_apply` with `"force_open"`, reason `auth` in the event detail when `err` is an `AuthError`.

Errors:

| Condition | Error class | Message identifiers |
|-----------|-------------|---------------------|
| `StoreBusy` from the backend after `sqlite_write` retries | `StoreBusy` (propagates) | `key` |

#### U08-25 `herness.core.resilience.breaker`

| Param | Type | Kind | Constraints |
|-------|------|------|-------------|
| `key` | `str` | positional | regex `^(model\|decider\|monitoring):[A-Za-z0-9_.\-]{1,64}$` or `^[a-z][a-z0-9_]{0,63}$` |

Returns `CircuitBreaker` (the process-wide instance for `key`, created on first use under `ProcessState.lock`). Invalid key → `ConfigError`. Kind: function. Tests: UT08-18.

#### U08-26 `herness.core.resilience.guard`

| Param | Type | Kind |
|-------|------|------|
| `key` | `str` | positional |

Returns `None`. If `breaker(key).allow()` is `False`, raises `CircuitOpen("circuit open: <key>", key=key, retry_at=b.retry_at() or now + cooldown_s)`. Used by spec 01 before each entity and by U08-28 before each attempt. Kind: function. Tests: UT08-17.

#### U08-27 `herness.core.resilience.register_probe`, `run_due_probes`

| Function | Parameters | Returns |
|----------|------------|---------|
| `register_probe` | `prefix: Literal["source","model","decider"]`, `fn: Callable[[str], None]` (receives the key without prefix; raises a taxonomy error on failure) | `None` |
| `run_due_probes` | `now: datetime` | `int` probes run |

| Field | Content |
|-------|---------|
| Kind | function (two) |
| Purpose | Active probes of design 08 §5.3 ("sources call `Connector.check()`; `model:*` calls `GET /health`; `decider:*` calls `Decider.health()`"). Probe functions are supplied by the composition root from T01-03 (herness.connectors.base.Connector.check), T05-10 (herness.harness.llm.registry.LLMRegistry.health) and T03-11 (herness.enrich.decide.Decider.health). Without a registered function for a prefix, breakers of that prefix recover only through the caller-probe path of U08-24 step 4. |
| Algorithm | `run_due_probes`: 1. `rows = ops.health_list(["open"])`. 2. For each row whose `probe_due ≤ now`: map the key to a prefix (`model:` → `model`, `decider:` → `decider`, else `source`); skip if no probe function; for a `model:` key whose client is off-network (`chains.config(name).off_network`) skip unless `cfg.security.egress.enabled` (design 08 §5.3). 3. Claim the probe (U08-24 step 4); if not won, skip. 4. Run `call_with_timeout(lambda: fn(name), 30)`; success → `record_success()`; a `HernessError` → `record_failure(err)` (a non-counting class re-opens with a `ModelUnavailable("probe failed: <type>")` substitute so the probe outcome is always recorded); any other exception → same substitute. 5. Return the count. |
| Concurrency | Called by the supervisor thread only. |
| Tests | UT08-104 |

### 3.7 Retry execution

#### U08-28 `herness.core.resilience.retry_call`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `name` | `PolicyName` | — | positional | |
| `fn` | `Callable[..., T]` | — | positional | synchronous |
| `*a` | `Any` | — | var-positional | passed to `fn` |
| `breaker_key` | `str \| None` | `None` | keyword-only | valid breaker key |
| `**kw` | `Any` | — | var-keyword | passed to `fn` |

Returns `T`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Run `fn` under a named policy, breaker and classification (design 08 §3.1, §5.2 tenacity wiring). |
| Preconditions | Config loaded; ops backend bound when `breaker_key` is set or a retry happens. |
| Postconditions | Returns `fn`'s value, or raises the last `HernessError` after the stop condition; foreign exceptions never escape (they are classified). `BaseException` subclasses that are not `Exception` (`KeyboardInterrupt`, `SystemExit`, `asyncio.CancelledError`) pass through unchanged. |
| Side effects | Breaker writes (U08-24), `retry` events (U08-18) and `herness_resilience_retries_total{policy, error_class}` per scheduled retry. |
| Concurrency | Thread-safe. Sleeps through `process_state().sleep`. |
| Complexity and limits | At most `attempts` calls and `max_elapsed_s` wall time plus one attempt duration. |
| Security notes | TH08-04 (Retry-After cap), TH08-05 (bounded retries). |
| Tests | UT08-20–UT08-23, UT08-25, UT08-26, BT08-01 |

Algorithm (private helpers `_run(p, fn, key, family, tracer, target)` and `_invoke`; `retry_call` calls `_run(policy(name), ..., tracer=None, target=None)`):
1. `p` = policy; `family = POLICY_FAMILY[p.name]`.
2. Build `tenacity.Retrying(retry=retry_if_exception(_should_retry), stop=stop_after_attempt(p.attempts) | stop_after_delay(p.max_elapsed_s), wait=FullJitterRetryAfter(p, rng), before_sleep=_emit_retry, sleep=process_state().sleep, reraise=True)`.
3. `_should_retry(e)`: `True` only when `e` is a `RetryableError`, not a `CircuitOpen`, and not a `RateLimited` whose `retry_after` is not `None` and exceeds `p.retry_after_cap_s` (that one re-raises at once so the job layer reschedules at `now + retry_after`).
4. Each attempt runs `_invoke`: (a) if `key` is set, `guard(key)` (raises `CircuitOpen`); (b) call `fn`; (c) on success, `breaker(key).record_success()` if `key`; return; (d) on `HernessError e`: `breaker(key).record_failure(e)` if `key`, re-raise; (e) on any other `Exception e`: `err = classify(e, family=family)`, record failure as in (d), `raise err from e`.
5. `_emit_retry(retry_state)`: `record_event("retry", component="resilience", target=key or p.name, detail={target: target or family-derived value ("llm" for model policies, "tool" for `tool_store`, the policy name otherwise), attempt, error_type, wait_s, policy, breaker_key, retry_after_s}, tracer=tracer)` and the metric.

#### U08-29 `herness.core.resilience.aretry_call`

Same parameters as U08-28 with `fn: Callable[..., Awaitable[T]]`; returns `T`; `async def`. Uses `tenacity.AsyncRetrying` and `process_state().asleep`. Identical rules otherwise. Breaker and event writes are synchronous SQLite calls; they run through `asyncio.to_thread` only when the breaker cache is stale or a transition must be written (ENG §2.5); the cached hot path stays on the loop. Kind: async function. Tests: UT08-24.

#### U08-30 `herness.core.resilience.retrying`

| Param | Type | Kind |
|-------|------|------|
| `name` | `PolicyName` | positional |
| `breaker_key` | `str \| None` | keyword-only, default `None` |

Returns `Callable[[F], F]`. The decorator returns a `functools.wraps` wrapper: `inspect.iscoroutinefunction(f)` → async wrapper calling `aretry_call(name, f, *a, breaker_key=breaker_key, **kw)`; else sync wrapper calling `retry_call`. Kind: function. Tests: UT08-24.

#### U08-31 `herness.core.resilience.retry_page`

| Param | Type | Kind |
|-------|------|------|
| `fn` | `Callable[[], T]` | positional |
| `source` | `str` | keyword-only (connector name) |

Returns `T` = `retry_call("source_http_page", fn, breaker_key=source)` (design 08 §3.1, spec 01). Kind: function. Tests: UT08-27, FT08-04.

#### U08-32 `herness.core.resilience.call_with_timeout`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `fn` | `Callable[[], T]` | — | positional | |
| `timeout_s` | `float` | — | positional | > 0 |
| `on_timeout` | `Callable[[], None] \| None` | `None` | keyword-only | |

Returns `T`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Timeout for in-process models (Laya, embeddings) (design 08 §5.2 "Timeouts"). |
| Algorithm | 1. Start `threading.Thread(target=runner, name="herness-timeout", daemon=True)`; `runner` stores the value or the exception in a holder. 2. `join(timeout_s)`. 3. Still alive → call `on_timeout()` if given (its exceptions are logged at WARNING `resilience.timeout.hook_failed`, not raised) and raise `ModelUnavailable("call timed out after <timeout_s>s")`. The stuck thread is left to die with the job's child process (design 08 §5.2). 4. Holder has an exception → re-raise it unchanged. 5. Return the value. |
| Errors | `timeout_s ≤ 0` → `ConfigError`. |
| Concurrency | Creates one daemon thread per call. |
| Tests | UT08-28 |

### 3.8 Fault injection

#### U08-33 `herness.core.resilience.faults.FaultRule`, `load_fault_plan`, `NAMED_POINTS`

This unit is the only registry of fault point names (R-40). Impl 11 uses `job.before_complete` for its X1 case and `connector.before_watermark` for X5; any further point another spec needs is added here first. `NAMED_POINTS` = the 18 points of design 08 §5.13: `http.page`, `connector.before_watermark`, `llm.call`, `llm.output`, `sql.query`, `decider.batch`, `embed.batch`, `enrich.after_batch_write`, `build.mid_sql`, `pipeline.before_promote`, `swarm.after_task_claim`, `swarm.after_finding_write`, `verifier.mid_batch`, `sqlite.write`, `job.after_claim`, `job.before_complete`, `gpu.after_stop`, `gpu.after_start`.

| `FaultRule` field | Type | Validation |
|-------------------|------|------------|
| `point` | `str` | in `NAMED_POINTS` |
| `action` | `str` | one of `timeout`, `http_429`, `http_503`, `malformed_json`, `kill`, or `error:<Class>` (Class in the spec 00 §7 leaf classes), `delay:<s>` (0 < s ≤ 600), `kill_service:<ServiceName>` |
| `nth`, `count` | `int \| None` | ≥ 1; at most one of them |
| `p` | `float \| None` | 0 < p ≤ 1; requires `seed` |
| `seed` | `int \| None` | |
| `retry_after` | `float \| None` | ≥ 0; only with `http_429` |
| `model`, `source`, `role`, `kind` | `str \| None` | label filters |

| Field | Content |
|-------|---------|
| Kind | class (pydantic, `extra="forbid"`, `strict=True`) plus function |
| Purpose | Validated fault plan (design 08 §5.13). |
| Algorithm | `load_fault_plan(path: Path) -> FaultPlan`: 1. Resolve the path; it must be a regular file (not a symlink) with suffix `.json` and size ≤ 65 536 bytes; fault plans are JSON only (R-40). 2. Parse with `json.loads`. 3. The document must be a list of ≤ 100 mappings; validate each as `FaultRule`. 4. Return `FaultPlan(rules, counters=[0]*n, rngs=[random.Random(seed) or None])`. |
| Errors | Any violation → `ConfigError("invalid fault plan: <path name>: <reason>")`. |
| Security notes | TH08-06: bounded file, JSON parser only (no object construction), closed action set. |
| Tests | UT08-47 |

#### U08-34 `herness.core.resilience.fault_point`

| Param | Type | Kind | Constraints |
|-------|------|------|-------------|
| `name` | `str` | positional | in `NAMED_POINTS` (checked only when a plan is loaded) |
| `**labels` | `str` | var-keyword | keys among `model`, `source`, `role`, `kind` |

Returns `None` (or raises the injected error).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | In-process fault hook (design 08 §3.3, §5.13). |
| Preconditions | None. |
| Postconditions | Without `HERNESS_FAULTS`, or with `HERNESS_FAULTS` set while `HERNESS_ENV` is not `test`, the call reads one attribute and returns; no file access (R-40). |
| Side effects | First call with `HERNESS_FAULTS` set and `HERNESS_ENV=test` loads the plan (U08-33), sets `process_state().faults_enabled = True` and logs WARNING `resilience.faults.enabled` (fields `plan` = file name only, `rules` = count). First call with `HERNESS_FAULTS` set in any other environment ignores the plan file and logs WARNING `resilience.faults.ignored` once per process (field `env` = the `HERNESS_ENV` value or `unset`). |
| Concurrency | Rule counters under `ProcessState.lock`. |
| Complexity and limits | O(rules) per call when enabled. |
| Security notes | TH08-06. |
| Tests | UT08-46, UT08-48, UT08-49, UT08-50, UT08-105, ST08-06 |

Algorithm:
1. If `process_state().fault_plan` is not loaded: read `os.environ.get("HERNESS_FAULTS")`; empty → store `None`. Set but `os.environ.get("HERNESS_ENV") != "test"` → store `None` and log `resilience.faults.ignored` (R-40). Otherwise load it (side effects above).
2. Plan `None` → return.
3. For each rule in order with `rule.point == name` and every non-`None` label filter equal to `labels.get(filter)`: increment its counter `c`; fire when (`nth` set and `c == nth`) or (`count` set and `c ≤ count`) or (`p` set and `rng.random() < p`) or (none of the three set). The first rule that fires wins; others are not evaluated.
4. Action of the firing rule (the "family" of a point is its prefix: `http.` → `SourceUnavailable`; `sqlite.` → `StoreBusy`; everything else → `ModelUnavailable`):

| Action | Effect |
|--------|--------|
| `timeout` | `sql.*` → raise `QueryError("fault: timeout", timeout=True)`; other points → raise the family error `"fault: timeout"` |
| `error:<Class>` | raise `<Class>("fault: injected")` |
| `http_429` | raise `RateLimited("fault: http_429", retry_after=rule.retry_after)` |
| `http_503` | raise the family error `"fault: http_503"` |
| `malformed_json` | raise `OutputValidationError("fault: malformed_json")` (U08-35 treats it as an invalid reply) |
| `delay:<s>` | `process_state().sleep(s)` then return |
| `kill` | log CRITICAL `resilience.faults.kill`, then `os.kill(os.getpid(), signal.SIGKILL)` where available, else `os.kill(os.getpid(), signal.SIGTERM)` (Windows `TerminateProcess`) |
| `kill_service:<svc>` | if `HERNESS_STUB_SERVICES` is set, it must be a JSON object mapping service names to loopback base URLs; when it names `svc`, `POST <url>/__control/kill` through `herness.core.egress.loopback_http_client(url, timeout_s=5)` (R-06; impl 11 DD11-04; a non-loopback URL → `ConfigError`); else call `process_state().kill_service_hook(svc)` (compose `kill`, U08-81); neither available → log WARNING `resilience.faults.no_service_hook` and return. Faults run only under `HERNESS_ENV=test`, so this lookup never runs in production |

### 3.9 Repair, model chains, decider chains, loop policy

#### U08-35 `herness.core.resilience.complete_validated`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `client` | `AsyncCompleter` | — | positional | |
| `req` | `LLMRequest` | — | positional | `req.response_schema` not `None` |
| `max_repairs` | `int` | `2` | keyword-only | 0–5 |
| `tracer` | `TracerLike \| None` | `None` | keyword-only | |
| `model` | `type[BaseModel] \| None` | `None` | keyword-only | additive (D08-12): when set, validation uses this pydantic model instead of `jsonschema` |

Returns `LLMResponse` (the first valid response).

| Field | Content |
|-------|---------|
| Kind | async function |
| Purpose | Structured-output validation and the repair protocol (design 08 §3.2, §5.4 steps 1–4). |
| Preconditions | `response_schema` set, else `ConfigError`. |
| Postconditions | The returned response's JSON validates; at most `max_repairs` extra calls were made, all at temperature 0 on the same client. |
| Side effects | One `repair` event (U08-18) and `herness_resilience_repairs_total{client}` per repair. |
| Concurrency | Async-safe; no shared state. |
| Complexity and limits | ≤ `1 + max_repairs` calls; assistant echo ≤ 4 000 chars; error list ≤ 20 lines. |
| Security notes | TH08-15: error lines carry JSON pointer paths and validator messages only, never input values; LLM05 control. |
| Tests | UT08-33–UT08-36, FT08-02 |

Algorithm:
1. `current = req`; `repairs = 0`.
2. `resp = await client.acomplete(current)`.
3. Call `fault_point("llm.output", model=client.name, role=req.metadata.role)`; an `OutputValidationError` from it counts as "invalid" with error list `[("/", "fault: malformed_json")]`.
4. Obtain JSON: `resp.parsed` if not `None`, else `json.loads(resp.text)`; a decode error gives `[("/", "invalid JSON: <msg without the document>")]`.
5. Validate: with `model`, `model.model_validate(obj)`; each pydantic error becomes `(pointer(loc), err["msg"])` (the `input` field is never used). Without `model`, `jsonschema.Draft202012Validator(req.response_schema).iter_errors(obj)`; each error becomes `(pointer(error.absolute_path), "failed '<error.validator>' constraint")`. `pointer` joins path items with `/` and escapes `~` and `/` per RFC 6901; the root is `/`.
6. No errors → return `resp`.
7. `repairs == max_repairs` → raise `OutputValidationError("output invalid after <n> repairs", client=client.name)`.
8. `repairs += 1`; `current = build_repair_request(req_original_with_history=current, resp, errors)` (U08-36); `record_event("repair", component="resilience", target=client.name, run_id, task_id from req.metadata, detail={model_profile: client.name, repair_no: repairs, error_paths: [p for p, _ in errors][:20]}, tracer=tracer)`; go to step 2.

#### U08-36 `herness.core.resilience.chain.build_repair_request`

| Param | Type | Kind |
|-------|------|------|
| `req` | `LLMRequest` | positional (the request that produced `resp`) |
| `resp` | `LLMResponse` | positional |
| `errors` | `Sequence[tuple[str, str]]` | positional |

Returns `LLMRequest`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Algorithm | 1. `text = "Your previous reply was not valid. Errors:\n" + "\n".join(f"{path}: {msg}" for the first 20 errors) + "\nReply with only a JSON object that matches this JSON Schema:\n" + canonical JSON of req.response_schema`. 2. Return `req.model_copy(update={"messages": req.messages + [Message(role="assistant", parts=[TextPart(text=resp.text[:4000])]), Message(role="user", parts=[TextPart(text=text)])], "temperature": 0.0})`. Each error message is cut to 200 chars. |
| Tests | UT08-35 |

#### U08-37 `herness.core.resilience.ModelChain` (constructor and `candidates`)

| Param | Type | Kind | Constraints |
|-------|------|------|-------------|
| `model_role` | `str` | positional | non-empty |
| `registry` | `ChainRegistry` | keyword-only | spec 05 `LLMRegistry` |
| `depth` | `str` | keyword-only | `fast`, `standard` or `deep` |
| `gpu` | `GpuStateReader` | keyword-only | usually `gpu_state()` |

`candidates()` returns `list[str]` (client keys).

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | Execute `chain_for(model_role, depth)` (design 08 §3.2, §5.4). Constructor does no I/O. Lives in `herness.core.resilience` (R-02); it refers only to types in `herness.core.types` and to the ports of U08-08, and it receives model clients only through the `client_for` callable of U08-38, so it never imports `herness.harness`. |
| Invariants | `model_role`, `registry`, `depth`, `gpu` never change after construction. |
| Algorithm (`candidates`) | 1. `chain = registry.chain_for(model_role, depth)`; `egress = cfg.security.egress.enabled`; `loaded = gpu.loaded_class()`. 2. Keep each key in order unless: (a) `registry.config(key).off_network` and not `egress`; (b) `b = breaker("model:" + key)` has state `open` and `now < b.retry_at()` (an open breaker whose probe is due stays in the list so the retry guard can claim the probe); (c) `(run_id, key)` in `process_state().auth_dropped` for the current `run_id` (set by `acomplete`; `candidates()` called directly uses no run filter); (d) `config(key).gpu_class` is not `None` and differs from `loaded` (so `"swapping"` removes every GPU client). 3. Return the kept keys. |
| Security notes | TH08-07: rule (a) keeps fallback inside the network boundary when the profile forbids egress (D5); LLM02. |
| Tests | UT08-37 |

#### U08-38 `herness.core.resilience.ModelChain.acomplete`

| Param | Type | Default | Kind |
|-------|------|---------|------|
| `req` | `LLMRequest` | — | positional |
| `schema` | `type[BaseModel] \| None` | `None` | keyword-only |
| `client_for` | `Callable[[str], AsyncCompleter]` | — | keyword-only |
| `tracer` | `TracerLike \| None` | `None` | keyword-only |

Returns `tuple[LLMResponse, BaseModel | None]`.

| Field | Content |
|-------|---------|
| Kind | async method |
| Purpose | Retry, repair and fallback over the candidates (design 08 §5.4). |
| Postconditions | On return the response came from one candidate; with `schema` the second element is the validated model instance. |
| Side effects | `retry`, `repair`, `fallback` events; `herness_resilience_fallbacks_total{from_profile, to_profile, reason}`; `process_state().auth_dropped` additions. |
| Concurrency | Async-safe; many tasks may share one instance. |
| Security notes | TH08-07, TH08-14 (bounded work per call). |
| Tests | UT08-38–UT08-42, FT08-01, FT08-02 |

Algorithm:
1. `run_id = req.metadata.run_id`; `cands = [k for k in self.candidates() if (run_id, k) not in auth_dropped]`. Empty → raise `ModelUnavailable("no available model for role <model_role>")`.
2. For index `i`, key `k` in `cands`:
   a. `info = registry.config(k)`; `req_k = req.model_copy(update={"client": k, "timeout_s": info.timeout_s})` (the first candidate keeps `req.timeout_s` when it equals the caller's client).
   b. `p = policy_for_client(info)`; wrap `inner = client_for(k)` in a private `_RetryingCompleter(inner, p, "model:" + k, tracer)` whose `acomplete` runs `_run` (U08-28, async form) around `fault_point("llm.call", model=k, role=req.metadata.role)` followed by `inner.acomplete(r)`, with trace target `llm`.
   c. If `schema is None and req.response_schema is None`: `resp = await wrapped.acomplete(req_k)`; return `(resp, None)`.
   d. Else, when `req_k.response_schema` is `None`, set it to `schema.model_json_schema()` and `response_schema_name` to `schema.__name__`; `resp = await complete_validated(wrapped, req_k, max_repairs=R.fallback.max_repairs, tracer=tracer, model=schema)`; `obj = schema.model_validate(json of resp) if schema else None`; return `(resp, obj)`.
   e. On a fallback trigger, with reason: `ModelUnavailable` → `unavailable`; `CircuitOpen` → `circuit_open`; `OutputValidationError` → `validation`; `ModelRefused` → `refusal`; `EgressBlocked` → `egress_blocked`; `AuthError` → `auth` (also add `(run_id, k)` to `auth_dropped`). Remember the error. If `i + 1 < len(cands)`: `record_event("fallback", detail={from_profile: k, to_profile: cands[i+1], reason}, tracer=tracer)`, increment the metric, continue with the next key and the original `req` (not the repaired history).
   f. Every other exception (`BudgetExceeded`, `QueryError`, `ToolInputError`, `RateLimited`, `StoreBusy`, other `FatalError`) propagates at once.
3. Chain exhausted → log ERROR `resilience.chain.exhausted` (`model_role`, `tried` keys) and raise the remembered last error.

#### U08-39 `herness.core.resilience.DeciderChain`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `order` | `Sequence[str]` | — | positional | non-empty; from `R.fallback.decider_chain[<active profile>]` |
| `gpu` | `GpuStateReader` | — | keyword-only | |
| `resolve` | `Callable[[str], DeciderLike] \| None` | `None` | keyword-only | additive (D08-12); default `lambda n: herness.core.registry.get("decider", n)()` |

`decide(items: Sequence[DecisionInput], questions: QuestionSet) -> tuple[list[DecisionOutput], list[DecisionInput]]` returns `(decided, deferred)`.

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | Move a batch down the decider order on failure; defer items no available entry decided (design 08 §5.5). |
| Algorithm | 1. `remaining = list(items)`. 2. For each `name` in `order`: availability — `laya`: always; `openjev`: `gpu.loaded_class() == "decider"` and `gpu.service_healthy("openjev")`; `jev`: `cfg.security.egress.enabled`; `llm`: `gpu.loaded_class() == "reasoning"` and `gpu.service_healthy("vllm-reasoning")`; any other name: always. Unavailable → log DEBUG `resilience.decider.skipped` and continue. 3. Available: `p = "decider_cloud" if name == "jev" else "decider_local"`; `out = retry_call(p, _call_entry, name, remaining, questions, breaker_key="decider:" + name)` where `_call_entry` calls `fault_point("decider.batch", model=name)` then `resolve(name).decide(remaining, questions)`, converting any non-`HernessError` exception into `ModelUnavailable("decider crash: <Type>")`. 4. Success → return `(out, [])`. 5. `ModelUnavailable` or `CircuitOpen` → log WARNING `resilience.decider.fallback` (`from`, `reason`) and continue with the next name. Any other error propagates. 6. Order exhausted → return `([], remaining)`. |
| Concurrency | Synchronous; one batch at a time per instance. |
| Tests | UT08-43, UT08-44 |

#### U08-40 `herness.core.resilience.loop_signal_policy`

| Param | Type | Default | Kind |
|-------|------|---------|------|
| `state` | `LoopState` | — | positional |
| `signal` | `LoopSignal` | — | positional |
| `tracer` | `TracerLike \| None` | `None` | keyword-only |

Returns `Literal["nudge", "stop"]`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | First loop signal of a task → nudge; second → stop with `guard_stop` (design 08 §3.2, §5.6). |
| Algorithm | 1. `ordinal = state.loop_signals`: the loop-signal counter of `LoopState`, separate from `nudges`, which spec 05 increments before it calls the policy (R-66, D08-04 accepted), so a `max_tokens` continuation nudge never counts as a loop signal. 2. `ordinal < R.loop.stop_on_signal_no` → return `"nudge"`. 3. Else `record_event("guard_stop", component="resilience", run_id=tracer.run_id if tracer else None, task_id=tracer.task_id if tracer else None, detail={cause: signal.cause, step: state.step}, tracer=tracer)` (the tracer exposes `run_id` and `task_id`, R-66, D08-05 accepted) and increment `herness_resilience_guard_stops_total{cause}`; return `"stop"`. |
| Side effects | One `resilience_event` row on stop. |
| Security notes | TH08-14 (LLM10: runaway loops end on the second signal). |
| Tests | UT08-45 |

### 3.10 Job queue, handlers and outcomes

#### U08-43 `herness.core.jobs.DEFAULT_PRIORITY`, `MANUAL_PRIORITY`

`DEFAULT_PRIORITY` = `{"chat": 75, "sync": 60, "build_pipeline": 60, "review": 40, "reconcile": 40, "outcome_measure": 30, "memory_maintenance": 30, "maintenance": 30, "eval": 20, "distill": 40}`; `MANUAL_PRIORITY = 80` (design 08 §4.1). `distill` is not listed in design 08 §4.1; it uses 40 (open item O08-06). `DEFAULT_PRIORITY[kind]` is the value of every `priority=None` (`JobSpec`, `enqueue`, R-41); it is used by the scheduler, and `schedule_rekey` (fixed 70), `enqueue_resume` (80) and CLI callers (80) pass explicit values.

`GPU_SLOT_KINDS = frozenset({"build_pipeline"})` (R-43, delta D08-20): kinds whose jobs start with `gpu_class = "none"` but switch classes in-job through `ctx.gpu_scope` or `ctx.require_gpu_class`. A `none`-class job of such a kind is claimable only by the GPU slot (and by inline runs); every other `none`-class job only by CPU slots (U08-48). Kind: constant. Tests: UT08-79, UT08-106, UT08-107.

#### U08-44 `herness.core.jobs.default_idem_key`

| Param | Type | Kind |
|-------|------|------|
| `kind` | `JobKind` | positional |
| `payload` | `Mapping[str, JsonValue]` | positional |

Returns `str` = `f"{kind}:" + sha256(canonical_json(payload)).hexdigest()[:16]` (design 08 §4.1). Kind: function (pure). Tests: UT08-51, PT08-06.

#### U08-45 `herness.core.jobs.validate_payload`

| Param | Type | Kind |
|-------|------|------|
| `payload` | `Mapping[str, object]` | positional |

Returns `bytes` (canonical JSON, reused by the caller).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Enforce "JSON, ≤ 64 KB, no secrets or record text" (design 08 §4.1, §9). |
| Algorithm | 1. Serialize with canonical JSON; non-JSON values (`datetime`, `Decimal`, sets, bytes) → error. 2. Size > 65 536 bytes → error. 3. Walk the value (depth ≤ 8, error beyond): every key matches `^[A-Za-z_][A-Za-z0-9_]{0,63}$`. 4. For every string value: if any value of `herness.core.secrets.known_values()` (T10-07 (herness.core.secrets.known_values)) of length ≥ 8 occurs in it → error; run `get_redactor().scan(value)` (T10-10 (herness.core.redact.get_redactor)) and error on any `CREDENTIAL` or `URL_TOKEN` span. 5. Return the bytes. |
| Errors | Each failure → `SchemaViolation("job payload rejected: <reason>")`; the reason never includes the value. |
| Security notes | TH08-03, TH08-10. |
| Tests | UT08-55, ST08-03 |

#### U08-46 `herness.core.jobs.submit`

| Param | Type | Kind |
|-------|------|------|
| `spec` | `JobSpec` | positional |
| `sched_check` | `SchedCheck \| None` | keyword-only, default `None` (used by the scheduler) |

Returns `tuple[str, bool]` = `(job_id, created)`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Idempotent enqueue in one transaction (design 08 §5.7 "Enqueue"). |
| Postconditions | Exactly one `queued` or `running` job holds the idem key; a finished job with a `sched:` key or with the same `payload.schedule`/`payload.fire_at` blocks re-creation when `sched_check` is given. |
| Side effects | `job` insert; log `jobs.job.enqueued` (INFO when created, DEBUG when deduped); `herness_jobs_enqueued_total{kind}` when created. |
| Concurrency | Safe across processes via `BEGIN IMMEDIATE` and the partial unique index. |
| Complexity and limits | p95 < 10 ms (BT08-02). |
| Tests | UT08-52–UT08-54, BT08-02 |

Algorithm:
1. `payload_bytes = validate_payload(spec.payload)`.
2. `now = now_utc()`; `idem = spec.idem_key or default_idem_key(spec.kind, spec.payload)`; `priority = DEFAULT_PRIORITY[spec.kind] if spec.priority is None else spec.priority` (R-41); `max_attempts = spec.max_attempts or R.jobs.max_attempts[spec.kind]`; `scheduled_for = spec.scheduled_for or now`.
3. `NewJob(job_id="job_" + new_ulid(), kind, gpu_class, status="queued", priority, payload=payload_bytes, idem_key=idem, attempts=0, max_attempts, scheduled_for, created_at=now)`.
4. `jobs_backend.insert_job(new_job, sched_check=sched_check)` (U08-95; its `run_write` applies the `sqlite_write` policy).
5. Log and meter; return.

#### U08-47 `herness.core.jobs.enqueue`

Parameters as design 08 §3.4 with the R-41 default: `kind: JobKind`, `payload: dict`, `gpu_class: GpuClass`, `priority: int | None = None` (`None` = `DEFAULT_PRIORITY[kind]`, U08-43), `scheduled_for: datetime | None = None`, keyword-only `max_attempts: int | None = None`, `idem_key: str | None = None`. Returns `str` (job_id, the existing one when deduped). Algorithm: build `JobSpec` (pydantic `ValidationError` → `SchemaViolation` naming the field) and return `submit(spec)[0]`. CLI commands that start work call this by default and warn when `worker_alive()` is false (R-45, impl 09). Kind: function. Tests: UT08-52, UT08-106.

#### U08-48 `herness.core.jobs.claim`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `owner` | `str` | — | keyword-only | regex `^[^:\s]{1,64}:\d{1,10}:(gpu\|cpu\d{1,2}\|cli)$` |
| `allowed_classes` | `Sequence[GpuClass]` | — | keyword-only | non-empty subset of `GpuClass` |
| `job_id` | `str \| None` | `None` | keyword-only | `job_<ulid>` |

Returns `JobRow | None`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Atomic claim (design 08 §5.7 "Atomic claim"). The supervisor uses the private `_claim(..., min_priority, priority_exempt_kinds)` form for the chat-window rule; the public form passes `None` and `()`. The slot is read from the owner suffix and restricts `none`-class jobs (R-43): suffix `gpu` → a `none`-class job is claimable only when its kind is in `GPU_SLOT_KINDS`; suffix `cpuN` → only when its kind is not in `GPU_SLOT_KINDS`; suffix `cli` → no kind restriction. Jobs of any other class are claimable when their class is in `allowed_classes`. |
| Postconditions | The returned row has `status = 'running'`, `lease_owner = owner`, `lease_expires_at = now + R.jobs.lease_s`, `attempts` incremented, `started_at` kept from the first start. |
| Side effects | `fault_point("job.after_claim", kind=row.kind)` after a successful claim; log `jobs.job.claimed`; histogram `herness_jobs_queue_wait_seconds{kind}` = `now − scheduled_for`. |
| Concurrency | `BEGIN IMMEDIATE`; no double claim across threads and processes (IT08-02). |
| Complexity and limits | p95 < 20 ms with 10 000 queued jobs using index `job(status, gpu_class, scheduled_for, priority)` (BT08-03). |
| Errors | Invalid owner or classes → `ConfigError`. |
| Tests | UT08-56, UT08-57, UT08-107, IT08-02, IT08-13, BT08-03 |

#### U08-49 `herness.core.jobs.outcomes.decide_failure`

| Param | Type | Kind |
|-------|------|------|
| `err` | `HernessError` | positional |
| `row` | `JobRow` | positional (after the claim; `attempts` already incremented) |
| `now` | `datetime` | positional |
| `rng` | `random.Random` | keyword-only |

Returns `FailureAction` = frozen dataclass `(action: Literal["requeue","failed","done"], scheduled_for: datetime | None, result: dict | None)`.

| Field | Content |
|-------|---------|
| Kind | function (pure given config) |
| Purpose | The "Job" column of the design 08 §5.2 error table. |
| Algorithm | 1. `CircuitOpen`: kind `sync` or `reconcile` → `done` with `result = {"partial": True, "outcome": "skipped_open_circuit", "skipped_open_circuit": [err.key]}`; the next scheduled run retries (R-39); other kinds → if `row.attempts < row.max_attempts` `requeue` at `err.retry_at` (at least `now`), else `failed`. 2. `RateLimited` with `retry_after` not `None`: `attempts < max_attempts` → `requeue` at `now + retry_after`; else `failed`. 3. Other `RetryableError` (`SourceUnavailable`, `ModelUnavailable`, `StoreBusy`, `RateLimited` without `retry_after`): `attempts < max_attempts` → `requeue` at `now + job_backoff_delay(row.attempts, rng=rng)`; else `failed`. 4. Everything else (all `RecoverableError`, all `FatalError` including `BudgetExceeded` that escaped its handler) → `failed`. |
| Tests | UT08-58 |

#### U08-50 `herness.core.jobs.outcomes.finish_job`

| Param | Type | Kind |
|-------|------|------|
| `row` | `JobRow` | positional |
| `owner` | `str` | positional |
| `outcome` | `JobOutcome \| HernessError` | positional |
| `attempt_started_at` | `datetime` | keyword-only |
| `stop_reason` | `Literal["cancel","preempt","shutdown"] \| None` | keyword-only |

Returns `Literal["done","queued","failed","canceled","lease_lost"]`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Apply a job's outcome with the owner guard (design 08 §5.7 "Completion statements"). |
| Side effects | `job` update; events `job_done`, `job_yield`, `job_failed`, `retry` (target `job`); metrics `herness_jobs_finished_total{kind, status}`, `herness_jobs_run_seconds{kind}`; chain advance or `chain_broken` (U08-73). |
| Concurrency | Supervisor thread or inline CLI thread. |
| Tests | UT08-59, UT08-62, IT08-04 |

Algorithm:
1. `fault_point("job.before_complete", kind=row.kind)`.
2. Re-read the row; `status == 'canceled'` → `finalize_canceled(job_id, owner, now)` (`finished_at = now`, `lease_owner = NULL`, status stays `canceled`); log `jobs.job.canceled`; return `canceled`.
3. `JobOutcome` `done` → `finish_done(job_id, owner, result without key "state", now)`; `record_event("job_done", detail={kind, attempt, duration_s = now − attempt_started_at, partial = result.get("partial", False)})`; then `advance_chain(row_after)` when `payload` has `schedule`.
4. `JobOutcome` `yield` → `resume_at = next_window_allowing(row.gpu_class, now)` when `stop_reason == "preempt"`, else `now`; `finish_yield(job_id, owner, resume_at, now)` (attempts − 1, `scheduled_for = resume_at`); `record_event("job_yield", detail={kind, attempt, stop_reason})`.
5. `HernessError` → `a = decide_failure(err, row, now, rng=process_state().rng)`; `last_error = {"class": type(err).__name__, "message": redact_text(str(err))[:2048], "at": ts(now), "attempt": row.attempts}`. `requeue` → `finish_requeue(job_id, owner, a.scheduled_for, last_error)` plus `record_event("retry", detail={target: "job", policy: "job_backoff", attempt, error_type, wait_s})` and WARNING log `jobs.job.rescheduled`. `done` → as step 3 with `a.result`. `failed` → `finish_failed(job_id, owner, last_error, now)`, `record_event("job_failed", ...)`, and `advance_chain` in its failure mode (emits `chain_broken` when the job was part of a schedule).
6. Any completion statement that updates 0 rows → log WARNING `jobs.job.lease_lost` (`job_id`, `owner`) and return `lease_lost`; nothing else happens.

#### U08-51 `herness.core.jobs.cancel`

`cancel(job_id: str) -> Literal["canceled", "cancel_requested", "not_active"]`. One `BEGIN IMMEDIATE`: `queued` → `status = 'canceled'`, `finished_at = now` → `canceled`; `running` → `status = 'canceled'` (lease kept; the supervisor's next heartbeat gets 0 rows and stops the child, design 08 §5.7) → `cancel_requested`; any other status or unknown id → `not_active`. Logs INFO `jobs.job.cancel_requested` with `job_id` and the result (TH08-11). Kind: function. Tests: UT08-60, ST08-11.

#### U08-52 `herness.core.jobs.retry`

`retry(job_id: str) -> None`. `failed` → `queued`, `attempts = 0`, `scheduled_for = now`, `lease_owner = NULL`, `lease_expires_at = NULL`, `finished_at = NULL`; `last_error` kept. Backend result `not_failed` → `JobStateError("job is not failed", job_id)`; `conflict` (another active job holds the idem key) → `JobStateError("an active job with the same idem_key exists", job_id)`; `missing` → `JobStateError("unknown job", job_id)`. Logs INFO `jobs.job.retry_requested`. Kind: function. Tests: UT08-61.

#### U08-53 `herness.core.jobs.get`, `list_jobs`

| Function | Parameters | Returns | Errors |
|----------|------------|---------|--------|
| `get` | `job_id: str` | `JobRow` | unknown → `JobStateError` |
| `list_jobs` | keyword-only `status: str \| None = None` (one of the five job statuses), `kind: str \| None = None` (a `JobKind`), `limit: int = 50` (1–1000) | `list[JobRow]` ordered by `created_at` descending | invalid filter → `ConfigError` |

Kind: function. Tests: UT08-63.

#### U08-54 `herness.core.jobs.worker_alive`

`worker_alive() -> bool`: `True` when a `worker` row has `status` in (`starting`, `running`, `draining`) and `heartbeat_at > now − 3 × R.jobs.heartbeat_s` (design 08 §3.4). This is the only worker-liveness test; every spec uses it and the 60 s check of design 09 is dropped (R-44). Kind: function. Tests: UT08-65.

#### U08-55 `herness.core.jobs.register_handler`, `resolve_handler`

| Function | Parameters | Returns |
|----------|------------|---------|
| `register_handler` | `kind: JobKind`, `handler: Callable[[JobContext], JobOutcome]` | `None` |
| `resolve_handler` | `kind: JobKind` | the handler |

Every handler takes the single argument `ctx: JobContext` and reads its payload from `ctx.job.payload` (R-42). Registration stores into `process_state().handlers`. Registering a different callable for a kind that already has one → `ConfigError("handler already registered for <kind>")`; the same object again is a no-op. `resolve_handler` of an unregistered kind → `ConfigError("no handler for <kind>")`. Handlers are registered by the worker bootstrap in every process that runs jobs (T09-27 (herness.cli.worker_bootstrap), which imports the handler registrations of T01-11 (herness.connectors.jobs.register_job_handlers), T02-19 (herness.model.build.make_build_pipeline_handler), T06-22 (herness.harness.swarm.handler.review_job_handler), T07-18 (herness.harness.memory.outcome.outcome_measure_handler), T07-22 (herness.harness.memory.maintenance.memory_maintenance_handler), T10-19 (herness.admin.register_handlers) and T11-30 (herness.eval.runner.handle_eval)). Kind: function. Tests: UT08-64.

#### U08-56 `herness.core.jobs.run_handler`

| Param | Type | Kind |
|-------|------|------|
| `ctx` | `JobContext` | positional |
| `handler` | `Callable[[JobContext], JobOutcome]` | positional |

Returns `JobOutcome | HernessError` (errors are returned, not raised, so the caller applies U08-50).

| Field | Content |
|-------|---------|
| Kind | function |
| Algorithm | 1. Call `handler(ctx)`. 2. A result that is not a `JobOutcome` → return `FatalError("handler returned <type>")`. 3. `HernessError` raised → return it. 4. `pydantic.ValidationError` raised → return `SchemaViolation("handler output invalid")`. 5. Any other `Exception` → log ERROR `jobs.handler.crashed` (`job_id`, `kind`, `error_type`) and return `FatalError("handler raised <Type>")` (worker top level, ENG §3.4). `BaseException` that is not `Exception` propagates. |
| Tests | UT08-64 |

### 3.11 Task lease and resume helpers

#### U08-57 `herness.core.jobs.recover_run_tasks`, `RecoverySummary`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `run_id` | `str` | — | positional | regex `^run_[0-9A-HJKMNP-TV-Z]{26}$` |
| `max_task_attempts` | `int` | — | keyword-only | ≥ 1 |
| `retry_dead` | `bool` | `False` | keyword-only | |

Returns `RecoverySummary` (frozen dataclass: `running_reset`, `failed_reset`, `dead_reset`: `int`).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Reset a run's interrupted and retryable tasks before spec 06 drives it (design 08 §5.12). |
| Algorithm | One `run_write(op="task_recover")` running the three design 08 §5.12 statements (the third only with `retry_dead`, setting `attempts = 0`), each setting `updated_at = now`; the row counts fill the summary. `done` tasks are never touched; `attempts` is not changed except by `retry_dead`. Log INFO `jobs.tasks.recovered` with the counts. |
| Errors | Invalid arguments → `ConfigError`. |
| Tests | UT08-66 |

#### U08-58 `herness.core.jobs.claim_task`

`claim_task(task_id: str) -> bool`: `UPDATE task SET status = 'running', attempts = attempts + 1, updated_at = :now WHERE task_id = :id AND status = 'pending'`; returns `rowcount == 1` (design 08 §3.7; the only place `attempts` grows, §11 Q1). Kind: function. Tests: UT08-67.

#### U08-59 `herness.core.jobs.build_checkpoint_envelope`

The checkpoint envelope is owned by 08 (R-21): `task.checkpoint` is a JSON object `{schema_version, loop, state, scratchpad}` with `schema_version = 1` (`CHECKPOINT_SCHEMA_VERSION`). `loop` holds the `LoopCheckpoint` fields (owner impl 05), `state` holds the swarm phase and pending findings (owner impl 06) and `scratchpad` holds working memory (owner impl 07). Each of `loop`, `state` and `scratchpad` is absent until its owner first saves it. 08 never interprets the content of the three keys.

| Param | Type | Kind |
|-------|------|------|
| `key` | `Literal["loop", "state", "scratchpad"]` | positional |
| `value` | `Mapping[str, object]` | positional (a JSON object) |
| `existing` | `Mapping[str, object] \| None` | positional (current `task.checkpoint`; `None` when the column is NULL) |

Returns `tuple[bytes, bool]` = (canonical JSON, `loop_dropped`).

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Algorithm | 1. `env = dict(existing)` when `existing` is not `None`, else `{"schema_version": 1}`. 2. `env.get("schema_version") != 1`, or a top-level key outside `schema_version`, `loop`, `state`, `scratchpad` → `SchemaViolation("checkpoint envelope invalid")`. 3. `env[key] = dict(value)`; the other two keys keep their stored values unchanged (R-21). 4. Serialize. If > 4 194 304 bytes (`CHECKPOINT_MAX_BYTES`) and `"loop"` is in `env`, remove `"loop"` and serialize again with `loop_dropped = True` (the loop then resumes from its task spec). 5. Still > cap → `SchemaViolation("checkpoint exceeds 4 MiB without loop")`. |
| Tests | UT08-68, UT08-109 |

#### U08-60 `herness.core.jobs.save_checkpoint`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `task_id` | `str` | — | positional | ID of a `running` task (spec 00 §5 format) |
| `key` | `Literal["loop", "state", "scratchpad"]` | — | positional | the caller's own key: impl 05 writes `loop`, impl 06 `state`, impl 07 `scratchpad` (R-21) |
| `value` | `Mapping[str, object]` | — | positional | JSON object |
| `writes` | `Callable[[sqlite3.Connection], None] \| None` | `None` | keyword-only | additive (D08-12): durable writes that commit with the checkpoint |

Returns `None`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Replace one key of the task checkpoint envelope and run the caller's durable writes in the same transaction, never erasing the other keys (R-21; design 08 §3.7, §4.3, §5.12 idempotency rule). |
| Preconditions | `writes` performs only SQL on the given connection, never calls `run_write` itself, and is safe to re-run after a rollback (it runs again when `run_write` retries the transaction). |
| Postconditions | On return, `task.checkpoint[key] == value` (unless `loop` was dropped for size) and every other envelope key is unchanged. |
| Algorithm | `backend.save_checkpoint(task_id, key, value, writes)` (U08-97), which inside one `run_write(op="task_checkpoint")` reads the current checkpoint, calls `build_checkpoint_envelope(key, value, existing)`, runs `writes(conn)` if given, then `UPDATE task SET checkpoint = :env, updated_at = :now WHERE task_id = :id AND status = 'running'`; 0 rows → rollback and `JobStateError("task not running", task_id)`; any exception → rollback and re-raise. `loop_dropped` → log WARNING `jobs.checkpoint.loop_dropped`. The impl 05 caller enforces `checkpoint_min_interval_s` for the `loop` key. |
| Concurrency | The read and the update run in one `BEGIN IMMEDIATE` transaction, so two owners saving different keys of the same task at the same time both keep their key. Spec 06 serialises its own writes on one thread per swarm process. |
| Tests | UT08-68, UT08-69, UT08-109, FT08-06 |

#### U08-61 `herness.core.jobs.complete_task`

`complete_task(task_id: str, result: dict, *, writes: Callable[[sqlite3.Connection], None] | None = None) -> None`: same transaction pattern as U08-60 with `UPDATE task SET status = 'done', result = :result, updated_at = :now WHERE task_id = :id AND status = 'running'`; `result` canonical JSON ≤ 4 MiB else `SchemaViolation`; 0 rows → `JobStateError`. Kind: function. Tests: UT08-69.

#### U08-62 `herness.core.jobs.fail_task`

| Param | Type | Kind |
|-------|------|------|
| `task_id` | `str` | positional |
| `err` | `HernessError` | positional |
| `max_task_attempts` | `int` | keyword-only, ≥ 1 |

Returns `Literal["pending", "dead"]`.

| Field | Content |
|-------|---------|
| Kind | function |
| Algorithm | In one `BEGIN IMMEDIATE`: read `attempts`; `status = "pending"` when `err` is a `RetryableError` (including `RateLimited` and `CircuitOpen`) and `attempts < max_task_attempts`, else `"dead"` (all `RecoverableError` and `FatalError`, design 08 §5.2 "Task" column); `UPDATE task SET status = :s, last_error = :le, updated_at = :now WHERE task_id = :id AND status = 'running'` with `last_error` shaped as in U08-50; 0 rows → `JobStateError`. The design's `failed` task status is never written by 08 (O08-07). |
| Tests | UT08-70 |

#### U08-63 `herness.core.jobs.release_task`

`release_task(task_id: str) -> None`: `UPDATE task SET status = 'pending', attempts = MAX(attempts - 1, 0), updated_at = :now WHERE task_id = :id AND status = 'running'`. 0 rows → log DEBUG `jobs.task.release_skipped` and return (releasing a task that already finished is harmless). A cancelled or preempted running task is released with this function, never left `running` (R-36). Kind: function. Tests: UT08-71.

### 3.12 Cron, windows and arbitration

#### U08-64 `herness.core.jobs.cron.CronExpr`

| Member | Signature |
|--------|-----------|
| `parse` | classmethod `(text: str) -> CronExpr` |
| `next_after` | `(t: datetime, tz: ZoneInfo) -> datetime` (first fire strictly after `t`, aware UTC) |
| `latest_at_or_before` | `(t: datetime, tz: ZoneInfo) -> datetime \| None` (latest fire ≤ `t` within 366 days) |
| `text` | `str` (original) |

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass of five `frozenset[int]` plus two flags) |
| Purpose | 5-field cron in the business timezone, no extra dependency (design 08 §5.11). |
| Invariants | Minutes ⊆ 0–59, hours ⊆ 0–23, day-of-month ⊆ 1–31, months ⊆ 1–12, day-of-week ⊆ 0–6 (0 = Sunday; `7` is read as 0). `dom_star`, `dow_star` record whether the field was `*`. |
| Algorithm (`parse`) | 1. Split on whitespace; exactly 5 fields. 2. Each field is a comma list of items; an item is `*`, `*/n` (n ≥ 1), `a`, `a-b` (a ≤ b) or `a-b/n`. Day-of-week items also accept `MON`–`SUN` (case-insensitive) in place of numbers. 3. Every value within its range. |
| Algorithm (matching) | A local date matches when its month is in `months` and: both `dom_star` and `dow_star` → true; only one restricted → that one must match; both restricted → either matches (Vixie cron rule). |
| Algorithm (`next_after`) | 1. `local = t.astimezone(tz)` truncated to the minute, plus one minute. 2. Walk dates from `local.date()` for at most 1 830 days: for matching dates, for each hour in `hours` ascending and minute in `minutes` ascending that is ≥ the start on the first date, build the naive time and resolve it with `resolve_local` (U08-65); the first resolved instant > `t` is the result. 3. No match → `ConfigError("cron never fires: <text>")`. |
| Algorithm (`latest_at_or_before`) | Mirror of `next_after` walking backwards from `t` truncated to the minute, at most 366 days; returns the first resolved instant ≤ `t`, else `None`. |
| Errors | Parse failure → `ConfigError("invalid cron '<text>': <field> <reason>")`. |
| Complexity and limits | ≤ 1 830 days × matched hours × matched minutes resolutions; < 1 ms for the defaults. |
| Tests | UT08-72, UT08-73, UT08-74, PT08-04 |

#### U08-65 `herness.core.jobs.cron.resolve_local`

| Param | Type | Kind |
|-------|------|------|
| `naive` | `datetime` (no tzinfo) | positional |
| `tz` | `ZoneInfo` | positional |

Returns `datetime` (aware UTC).

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | DST rules of design 08 §5.11: a time in a DST gap resolves to the next valid minute; an ambiguous time uses `fold=0` only. |
| Algorithm | 1. `aware = naive.replace(tzinfo=tz, fold=0)`. 2. Round-trip check: if `aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == naive`, return `aware.astimezone(UTC)`. 3. Otherwise the time is in a gap: step `naive` forward one minute at a time (at most 180 steps) until step 2 succeeds and return that instant. Two cron minutes inside one gap therefore resolve to the same instant; callers de-duplicate. |
| Tests | UT08-74 |

#### U08-66 `herness.core.jobs.window_at`, `ActiveWindow`

| Param | Type | Kind |
|-------|------|------|
| `now` | `datetime` | positional, aware |

Returns `ActiveWindow` (frozen dataclass: `spec: WindowSpec`, `start_at: datetime`, `end_at: datetime`, both aware UTC).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | The window active at `now` in `cfg.weights.business_timezone` (design 08 §5.10). Additive public helper (D08-12) used by status, chat policy and spec 06 (chat reserved slots): `window_at(now).spec.name == "chat"` is the test for an open chat window that impl 06 D06-09 asks for. |
| Algorithm | 1. `local = now.astimezone(tz)`. 2. For each window and each candidate start date `d` in (`local.date()`, `local.date() − 1 day`): skip when `days` is set and `d`'s weekday is not in it; `start_at = resolve_local(d + start)`, `end_at = resolve_local(d' + end)` where `d' = d + 1 day` if `end ≤ start` else `d`; match when `start_at ≤ now < end_at`. 3. Validation (U08-67) guarantees exactly one match outside DST transitions; if none matches (a DST-shortened minute), use the window matched at `now + 1 hour`; if several match, take the one with the latest `start_at`. |
| Tests | UT08-75, PT08-05 |

#### U08-68 `herness.core.jobs.next_window_allowing`

`next_window_allowing(cls: GpuClass, now: datetime) -> datetime`: `cls == "none"` → `now`; if `window_at(now)` allows `cls` → `now`; else walk successive windows (`w = window_at(w.end_at)`) for at most 8 days and return the first `start_at` whose `classes` contain `cls`; none found → `now + 1 day` and a WARNING `jobs.window.class_never_allowed`. Kind: function. Tests: UT08-77.

#### U08-69 `herness.core.jobs.preempt_deadline`

| Param | Type | Kind |
|-------|------|------|
| `cls` | `GpuClass` | positional (currently loaded class of the running GPU job) |
| `class_since` | `datetime` | positional (when that class was loaded) |
| `now` | `datetime` | positional |

Returns `datetime | None` (instant at which `stop("preempt")` is due; `None` = no preemption).

| Field | Content |
|-------|---------|
| Kind | function (pure given config) |
| Purpose | Design 08 §5.10 "Preemption applies only when the next window does not allow the running job's current class". |
| Algorithm | 1. `cls == "none"` → `None`. 2. `w = window_at(now)`. If `cls in w.spec.classes` → `None`. 3. If `class_since ≥ w.start_at` → `None` (the class was loaded by an in-job switch inside this window; allowed until the next boundary). 4. `prev = window_at(w.start_at − 1 minute)`. 5. Return `w.start_at` if `w.spec.hard_start` else `w.start_at + prev.spec.overrun_max_min minutes`. |
| Tests | UT08-76, FT08-10 |

#### U08-70 `herness.core.jobs.arbiter.arbiter_decide`, `ArbiterDecision`

| Param | Type | Kind |
|-------|------|------|
| `loaded` | `GpuClass` | positional |
| `window` | `ActiveWindow` | positional |
| `claimable` | `Mapping[GpuClass, int]` | positional (queued, due jobs claimable by the GPU slot per class under the chat-window priority rule; key `none` counts only `GPU_SLOT_KINDS` jobs, R-43) |
| `requested` | `GpuClass \| None` | keyword-only (external `worker.requested_class`, spec 10 deploy up/down) |

Returns `ArbiterDecision` (frozen dataclass: `action: Literal["keep","swap","idle"]`, `target: GpuClass`, `reason: str`).

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Design 08 §5.10 arbiter, run only while the GPU slot is free. |
| Algorithm | 1. `requested` set and ≠ `loaded`: if `requested == "none"` or `requested in window.spec.classes` → `swap` to it (reason `requested`); else → `idle` with reason `request_not_allowed` (the supervisor logs WARNING `jobs.gpu.request_rejected` and clears the request). 2. `loaded in classes` and `claimable[loaded] > 0` → `keep`. 2a. `claimable.get("none", 0) > 0` → `keep` (a `GPU_SLOT_KINDS` job starts on the loaded class without a swap and switches in-job, R-43). 3. First `c` in `classes` (preference order) with `claimable[c] > 0` → `keep` if `c == loaded` else `swap` to `c`. 4. `window.spec.preload` set and ≠ `loaded` → `swap` to preload (reason `preload`). 5. Else `idle` with `target = loaded`. |
| Tests | UT08-78 |

### 3.13 Scheduler

#### U08-71 `herness.core.jobs.scheduler.collect_schedules`, `ScheduleEntry`

Returns `list[ScheduleEntry]` (frozen dataclass: `name`, `cron: CronExpr`, `catch_up_max: timedelta`, `job: ChainStep`, `then: tuple[ChainStep, ...]`, `idem_mode: Literal["sched","sync"]`).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Gather every schedule source (design 08 §5.11 "Sources of schedules"). |
| Algorithm | 1. For each `(name, src)` in `cfg.sources.sources` with `src.enabled` and `src.schedule`: entry `sync.<name>`, cron `src.schedule`, `catch_up_max = SOURCE_SYNC_CATCH_UP` (1 h, O08-03), job `ChainStep(kind="sync", gpu_class="none", payload={"source": name})`, `idem_mode = "sync"`. 2. Same sources with `src.reconcile.schedule`: entry `reconcile.<name>`, `catch_up_max = SOURCE_RECONCILE_CATCH_UP` (12 h), job `reconcile` class `none` payload `{"source": name}`, `idem_mode = "sched"`. 3. `maintenance`: cron `"<MM> <HH> * * *"` from `cfg.backup.nightly_at` (`HH:MM`), `catch_up_max = S.maintenance.catch_up_max`, job `maintenance` class `none` payload `{"action": "backup"}`, `then = (ChainStep(kind="maintenance", gpu_class="none", payload={"action": "purge"}),)`. 4. Every `S.jobs` entry as configured. Entries whose cron fails to parse are skipped with ERROR log `jobs.schedule.error` (config validation normally prevents this). |
| Tests | UT08-82 |

#### U08-72 `herness.core.jobs.scheduler.run_scheduler`

| Param | Type | Kind |
|-------|------|------|
| `now` | `datetime` | positional |

Returns `SchedulerReport` (frozen dataclass: `fired: int`, `missed: int`, `errors: int`, `chains_advanced: int`).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Fire due schedules with catch-up, and repair chains (design 08 §5.11). Called every `R.jobs.reaper_interval_s` by the supervisor. |
| Side effects | Job inserts; `schedule_fired`, `schedule_missed` events; `herness_jobs_schedule_fired_total{schedule}`, `herness_jobs_schedule_missed_total{schedule}`. |
| Concurrency | Several workers may run it at once; `BEGIN IMMEDIATE` plus the sched check in `insert_job` make firing exactly-once. |
| Tests | UT08-79, UT08-83 |

Algorithm:
1. For each entry of `collect_schedules()`, inside its own error boundary (a `HernessError` → log ERROR `jobs.schedule.error` with `schedule` and `error_type`, count, continue):
   a. `F = entry.cron.latest_at_or_before(now, tz)`; `None` → next entry.
   b. `now − F > catch_up_max`: if `(name, F)` is not in the process-local set `_missed_reported` (bounded to 1 000 entries, oldest evicted) and `backend.sched_fired(name, ts(F))` is false → `record_event("schedule_missed", detail={schedule, fire_at})`, add to the set. Next entry.
   c. `payload = entry.job.payload | {"schedule": name, "fire_at": ts(F)}`; for `nightly` add `"rekey_night": True` when `backend.rekey_on(local day of F)` returns a job.
   d. `idem = f"sync:{source}"` for `idem_mode == "sync"`, else `f"sched:{name}:{ts(F)}"`.
   e. `(job_id, created) = submit(JobSpec(kind, payload, gpu_class, priority = step.priority or DEFAULT_PRIORITY[kind], idem_key=idem), sched_check=(name, ts(F)))`. `created` → `record_event("schedule_fired", target=name, job_id=job_id, detail={schedule, fire_at})`.
2. Chain repair: for each row of `backend.recent_scheduled(since=now − 24 h)` call `advance_chain(row)`; idempotent through idem keys and the sched check.

#### U08-73 `herness.core.jobs.scheduler.advance_chain`

| Param | Type | Kind |
|-------|------|------|
| `row` | `JobRow` | positional (a finished job whose payload has `schedule`) |

Returns `str | None` (job_id of the enqueued next step).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Sequential chain steps (design 08 §5.11 "Chains"): step k+1 is enqueued when step k ends `done`; a `failed` step stops the chain. |
| Algorithm | 1. Find the entry named `row.payload["schedule"]`; missing or no `then` → `None`. `F = row.payload["fire_at"]`; `step = row.payload.get("step", 0)`. 2. `row.status == "failed"` → `record_event("chain_broken", target=f"{name}:{F}", detail={schedule, fire_at, step, reason: "failed"})` unless `count_events("chain_broken", target=f"{name}:{F}", since=F) > 0`, and return `None`. 3. `row.status != "done"` → `None`. 4. For `k` from `step + 1` to `len(then)`: `s = then[k-1]`; skip with `record_event("chain_skipped", target=f"{name}:{F}:{k}", detail={schedule, fire_at, step: k, reason})` (written only when `count_events("chain_skipped", target=f"{name}:{F}:{k}", since=F) == 0`, so chain repair does not duplicate it) when `not s.enabled` (reason `disabled`), when the business-timezone weekday of `F` is in `s.skip_on` (reason `skip_on`), or when `F`'s local day has a rekey job (`backend.rekey_on`) and `s.kind == "review"` and `s.payload.get("depth") == "standard"` (reason `rekey`). Otherwise `submit(JobSpec(s.kind, s.payload | {"schedule": name, "fire_at": F, "step": k}, s.gpu_class, priority = s.priority or DEFAULT_PRIORITY[s.kind], idem_key=f"sched:{name}:{F}:{k}"), sched_check=None)` and return the job_id. 5. All remaining steps skipped → `None`. |
| Tests | UT08-80, IT08-03 |

#### U08-74 `herness.core.jobs.schedule_rekey`

| Param | Type | Default | Kind |
|-------|------|---------|------|
| `now` | `datetime \| None` | `None` | keyword-only (additive, tests) |

Returns `str` (job_id).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Plan the redaction-key rotation night (design 08 §5.11 "Rekey", spec 10 §5.3). |
| Algorithm | 1. `value = herness.core.secrets.resolve("redact.hmac_key.next")` (T10-06 (herness.core.secrets.resolve); missing → `ConfigError`). 2. `key_id` = first 8 hex chars of SHA-256 over `bytes.fromhex(value.get_secret_value())` (spec 10 `Redactor.key_id` rule); the secret value is never logged. 3. `earliest = now + S.rekey.min_notice_h hours`; `F = CronExpr.parse(S.rekey.cron).next_after(earliest − 1 minute, tz)` (first fire ≥ `earliest`). 4. `job_id = enqueue("maintenance", {"action": "rekey", "key_id": key_id}, "decider", 70, F, idem_key=f"rekey:{key_id}")`. 5. `record_event("rekey_planned", job_id=job_id, detail={fire_at: ts(F), key_id})`. |
| Security notes | Only the 8-hex key identifier leaves the function (TH08-02). |
| Tests | UT08-81, IT08-03 |

### 3.14 Chat policy

#### U08-75 `herness.core.jobs.chat_policy`

| Param | Type | Kind |
|-------|------|------|
| `now` | `datetime` | positional |

Returns `ChatMode`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Design 08 §5.10 `chat_policy(now)` pseudocode. |
| Algorithm | 1. `chat_key` = first key of `chains.chain_for("chat", "fast")` whose `config(key).off_network` is false; none → `ConfigError("chat chain has no local client")`. 2. `g = gpu_state()`. If `g.loaded_class() == "reasoning"` and `g.service_healthy("vllm-reasoning")` and `breaker("model:" + chat_key).state() != "open"` → `"live"`. 3. `mode = S.chat.in_hours_unavailable if window_at(now).spec.name == "chat" else S.chat.off_hours`. 4. `mode == "cloud"` and `herness.core.egress.cloud_chat_allowed(cfg)` is false → `"small_model"` (T10-16 (herness.core.egress.cloud_chat_allowed), R-38). That function is false without egress, without purpose `reasoning` in `security.egress.purposes`, in the `hybrid` profile unless `security.data_policy.chat_approved` is true, and in every profile other than `hybrid` and `premium` (O08-13 resolved). 5. `mode == "small_model"` and `breaker("model:" + first key of chain_for("chat_off_hours", "fast")).state() == "open"` → `"defer"`. 6. Return `mode`. |
| Complexity and limits | < 5 ms p95 on warm caches: worker row cached 5 s, service health cached 5 s, breaker cached 5 s (BT08-09, O08-05). |
| Security notes | TH08-07 (`cloud` never returned without egress, nor in `hybrid` without the `chat` approval, R-38). |
| Tests | UT08-84, BT08-09 |

#### U08-76 `herness.core.jobs.chat_model_profile`

`chat_model_profile(mode: ChatMode, depth: str = "fast") -> str | None`: `live` → first key of `chain_for("chat", depth)` with `off_network` false; `small_model` → first key of `chain_for("chat_off_hours", depth)`; `cloud` → first key of `chain_for("chat", depth)` with `off_network` true, or `None` with WARNING `jobs.chat.no_cloud_client` when there is none (the impl 05 adapter sends that call with egress purpose `reasoning` and `payload_class = "aggregated_evidence"`, R-38); `defer` → `None` (design 08 §3.6). Kind: function. Tests: UT08-85.

#### U08-77 `herness.core.jobs.chat_next_live_at`

| Param | Type | Kind |
|-------|------|------|
| `now` | `datetime` | positional |

Returns `datetime | None` (UTC).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | ETA of the next `live` chat window. Impl 06 uses it as `scheduled_for` for the `chat` job under `defer` and for escalation runs enqueued under `small_model` by T06-24 (herness.harness.swarm.escalation.escalate_to_review), which wait for the next live window instead of being refused (R-35). |
| Algorithm | 1. If some alive worker has `gpu_class_loaded == "swapping"` and `requested_class == "reasoning"` → `now + SWAP_ETA_S` (360 s, the design 08 §8 decider → reasoning target; O08-04). 2. `w = window_at(now)`; walk the following windows (`w = window_at(w.end_at)`) for at most 8 days; return the `start_at` of the first whose `preload == "reasoning"` or whose `classes` contain `reasoning`. 3. None → `None`. |
| Tests | UT08-86 |

### 3.15 GPU services and arbitration

#### U08-78 `herness.core.jobs.gpu_services.ComposeRunner`

| Method | Arguments appended after `R.gpu.compose_cmd + ["-f", R.gpu.compose_file]` | Timeout |
|--------|------------------------------------------------------------------------------|---------|
| `up(cls, service)` | `["--profile", cls, "up", "-d", service]` | 120 s (`COMPOSE_UP_TIMEOUT_S`) |
| `stop(service)` | `["stop", "-t", "60", service]` | `R.gpu.stop_timeout_s` |
| `kill(service)` | `["kill", service]` | 60 s |
| `ps()` | `["ps", "--format", "json"]` → `dict[str, str]` service → state | 30 s |

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | The only place that runs compose (design 08 §5.8 "Commands"). |
| Preconditions | `service` is a key under `R.gpu.classes[*].services` and matches `^[a-z][a-z0-9-]{0,63}$`; `cls` is a key of `R.gpu.classes`. |
| Algorithm | 1. Validate arguments (else `ConfigError`). 2. `subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=<t>, check=False)`. 3. `FileNotFoundError` → `ModelUnavailable("compose unavailable")`; `TimeoutExpired` → `ModelUnavailable("compose <verb> <service> timed out")`; return code ≠ 0 → `ModelUnavailable("compose <verb> <service> failed rc=<n>")`, with stderr cut to 500 chars, redacted and logged at WARNING `jobs.gpu.compose_failed`. 4. `ps` parses stdout: text starting with `[` is one JSON array; otherwise one JSON object per non-empty line; each object's `Service` and `State` fields are read; parse failure → `ModelUnavailable("compose ps unreadable")`. |
| Security notes | TH08-01: argument lists only, allowlisted names, no user input. |
| Tests | UT08-87, UT08-88, ST08-01 |

#### U08-79 `herness.core.jobs.gpu_services.LoopbackHttp`

| Method | Signature | Behavior |
|--------|-----------|----------|
| `healthy` | `(svc: ServiceSettings, *, timeout_s: float = 5) -> bool` | `GET svc.url + svc.health.path`; header `Authorization: Bearer <secret>` when `bearer_secret` is set (resolved with T10-06 (herness.core.secrets.resolve)); 2xx → `True`; any other status or exception → `False` |
| `warm_up` | `(name: ServiceName, svc: ServiceSettings, *, timeout_s: float) -> None` | per-service request below; non-2xx or exception → `ModelUnavailable("warmup failed <name>")` |

Warm-up requests (design 08 §5.8 "Warm-up"):

| Service | Request |
|---------|---------|
| `vllm-reasoning` | `POST /v1/chat/completions` with `{"model": M, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8}`; `M` = `model` of the first `models.yaml` client with `gpu_class == "reasoning"`; bearer from that client's `api_key` secret reference when set |
| `llamacpp-large` | `POST /completion` with `{"prompt": "ping", "n_predict": 8}` |
| `openjev` | `POST /v1/systemone` with `{"model": "openjev-latest", "state": "warm-up", "questions": {"warmup": {"type": "noul", "instructions": "Is this text a warm-up request?"}}, "samples": 1, "steps": 1, "think": 0}` and the bearer header (verification item (b)15, O08-01) |

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | Loopback-only HTTP for health and warm-up. |
| Algorithm | Every request first checks that the URL scheme is `http` and the host is `127.0.0.1`, `localhost` or `::1` (else `ConfigError`); the client comes from `herness.core.egress.loopback_http_client(svc.url, timeout_s=timeout_s)` (T10-17 (herness.core.egress.loopback_http_client), R-06), the only factory for loopback model-server clients; this module constructs no `httpx` client (D08-06 resolved). Redirects are not followed; a 3xx answer counts as unhealthy. Response bodies are read up to 64 KB and discarded. The bearer value comes from `herness.core.secrets.resolve` of the `bearer_secret` reference (`secret:OPENJEV_API_KEY` for OpenJev, R-53). |
| Security notes | TH08-12, TH08-02 (bearer never logged). |
| Tests | UT08-89, ST08-12 |

#### U08-80 `herness.core.jobs.gpu_services.vram_used_mb`, `wait_vram_free`

| Function | Parameters | Returns | Behavior |
|----------|------------|---------|----------|
| `vram_used_mb` | none | `int \| None` | runs `R.gpu.vram_check_cmd` (argument list, 10 s timeout); first non-empty stdout line parsed as int; failure → `None` |
| `wait_vram_free` | keyword-only `threshold_mb: int`, `poll_s: float = 2`, `timeout_s: float = 60` | `None` | polls until `vram_used_mb() < threshold_mb`; a `None` reading counts as not free; timeout → `ModelUnavailable("vram_not_freed")` |

Kind: function. Tests: UT08-90.

#### U08-81 `herness.core.jobs.GpuController`

| Member | Signature |
|--------|-----------|
| constructor | `GpuController(*, worker_id: str \| None, runner: ComposeRunner, http: LoopbackHttp)` |
| `loaded` | property `-> GpuClass` |
| `class_since` | property `-> datetime` |
| `swap(target, *, reason)` | `(GpuClass, str) -> float` duration seconds |
| `service_start(name, *, timeout_s=None)` | `-> None` |
| `service_stop(name)` | `-> None` |
| `service_healthy(name)` | `-> bool` |
| `detect_loaded_class()` | `-> GpuClass` |
| `restart_service(name)` | `-> bool` (false when rate-limited) |

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | Perform every swap and service action (design 08 §3.5, §5.8). Only the supervisor (or `run_inline` holding the GPU lock) constructs it. |
| Invariants | `loaded` equals `worker.gpu_class_loaded` after every method returns; while a method runs, the worker row shows `swapping` for swaps. At most one method runs at a time (internal lock). |
| Side effects | Compose commands; `worker` updates; events `gpu_swap`, `gpu_swap_failed`, `service_start`, `service_stop`, `service_restart`; breaker resets and failures; histogram `herness_jobs_gpu_swap_seconds{from, to}`; counter `herness_jobs_gpu_swap_failed_total{to}`. On construction it sets `process_state().kill_service_hook = runner.kill`. |
| Concurrency | Lock-protected; called from the supervisor's single `herness-gpu` executor thread or the inline CLI thread. |
| Complexity and limits | Swap time bounded by `stop_timeout_s` + 60 s VRAM + `start_timeout_s` + `warmup_timeout_s`. |
| Security notes | TH08-01, TH08-09. |
| Tests | UT08-91–UT08-94, UT08-96, FT08-09, FT08-12, BT08-06–BT08-08 |

Algorithm `swap(target)` (design 08 §5.8 steps 1–6); `svc_of(c)` = services of class `c`; `endpoints(c)` = breaker keys `model:<k>` for every `models.yaml` client with `gpu_class == c`, plus `decider:openjev` for `decider`:
1. `target == loaded` and every `start_on_entry` service of `target` is healthy → return 0.0.
2. Update worker: `gpu_class_loaded = 'swapping'`, `requested_class = target`. `t0 = monotonic()`.
3. Stop: `running = runner.ps()`; for every configured service not in `svc_of(target)` whose state is `running`: `runner.stop(s)`; if `runner.ps()` still shows it running, `runner.kill(s)`. Then `fault_point("gpu.after_stop")`.
4. VRAM: `wait_vram_free(threshold_mb=R.gpu.vram_free_threshold_mb)`. Failure → set `loaded = none`, worker `gpu_class_loaded = 'none'`, `record_event("gpu_swap_failed", detail={from, to, step: "vram", error_type})`, raise `ModelUnavailable("vram_not_freed")`.
5. Start: if `target == "none"` skip to step 7. For each service of `target` with `start_on_entry`: `runner.up(target, s)`; poll `http.healthy` under `dataclasses.replace(GPU_HEALTH_POLICY, max_elapsed_s=svc.start_timeout_s)` with a function that raises `ModelUnavailable("unhealthy")` when unhealthy; `fault_point("gpu.after_start")`.
6. Warm-up each started service with `timeout_s = R.gpu.warmup_timeout_s`.
7. `loaded = target`; `class_since = now`; worker `gpu_class_loaded = target`, `requested_class = NULL`; `ops.health_reset(endpoints(target))`; `record_event("gpu_swap", detail={from, to, duration_s, reason})`; histogram; return the duration.
8. Failure in steps 5–6: `runner.stop` each service of `target` (errors logged, not raised); `loaded = none`; worker `gpu_class_loaded = 'none'`; `breaker(k).record_failure(ModelUnavailable(...))` for each key of `endpoints(target)`; `record_event("gpu_swap_failed", detail={from, to, step: "start" | "warmup", error_type})`; raise the `ModelUnavailable`.

`service_start(name)`: `name` must be in `svc_of(loaded)` else `ConfigError("service <name> belongs to another class; use require_gpu_class")`. Runs steps 4–6 for that service only (design: "runs steps 3–5 for that service only"; the handler must have released in-process GPU models), resets `decider:openjev` when `name == "openjev"`, emits `service_start`. Failure → stop that service, record the breaker failure, raise `ModelUnavailable`.
`service_stop(name)`: same class check; `runner.stop` (kill fallback), `wait_vram_free`, emit `service_stop`.
`service_healthy(name)`: `http.healthy(svc, timeout_s=5)`.
`detect_loaded_class()` (worker start, design 08 §5.8): `running = runner.ps()`; map each running service to its class. More than one class → stop all running services, return `none`. One class `c`: every running service must be healthy, else stop them and return `none`; `c == "decider"` with only `openjev` running, or any class whose `start_on_entry` services are all running and healthy → `c`. Nothing running → `none`. Sets `loaded`, `class_since = now` and the worker row.
`restart_service(name)` (open `model:` breaker of a loaded service, design 08 §5.3): if `ops.count_events("service_restart", target=name, since=now − 3600 s) ≥ R.breakers.restart_max_per_hour` → return `False`; else stop, up, health poll, warm-up, `record_event("service_restart", target=name, ...)`, return `True`.

#### U08-82 `herness.core.jobs.gpu_lock.GpuLock`

| Param | Type | Kind |
|-------|------|------|
| `path` | `Path` | positional; default callers pass `cfg.paths.data / "locks" / "gpu.lock"` |

Context manager. `__enter__` creates the parent directory, opens the file in `a+b`, and takes a non-blocking exclusive lock on byte 0 (`msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` on Windows, `fcntl.flock(fd, LOCK_EX | LOCK_NB)` elsewhere). Failure → close the file and raise `ConfigError("another GPU owner holds data/locks/gpu.lock")`. `__exit__` unlocks and closes. The OS releases the lock if the process dies. Kind: class. Security: TH08-09. Tests: UT08-95, ST08-09.

#### U08-83 `herness.core.jobs.gpu_state`, `WorkerGpuState`

`gpu_state() -> GpuStateReader` returns a process-wide `WorkerGpuState`. `loaded_class()` reads `list_workers()` at most every 5 s and returns `gpu_class_loaded` of the alive worker with `gpu_slot = 1` (alive per U08-54), or `"none"` when there is none. `service_healthy(name)` calls `LoopbackHttp.healthy(svc, timeout_s=2)` at most every 5 s per service and caches the result. Kind: function plus class. Concurrency: cache under a lock. Tests: UT08-84, BT08-09.

#### U08-102 `herness.core.jobs.request_gpu_class`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `cls` | `GpuClass` | — | positional | any member, including `large` (R-47) and `none` (unload) |

Returns `Literal["requested", "no_worker"]`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | The manual GPU class switch behind `herness gpu load CLASS` / `gpu unload` (impl 09, R-47) and `herness deploy up|down` with a live worker (impl 10): it asks the worker's arbiter for a class and never starts or stops containers itself (design 08 §5.8, design 09 CLI table). |
| Preconditions | The caller has checked the admin role and written the audit line (impl 09, impl 10; TH08-11). |
| Postconditions | `requested`: the alive GPU worker's `requested_class = cls`. `no_worker`: nothing changed; impl 09 prints the "no worker" warning with the fix (R-45) and impl 10 takes its GPU-lock path. |
| Algorithm | 1. `workers = backend.list_workers()`; pick the worker that is alive per U08-54 and has `gpu_slot = 1`; none → return `"no_worker"`. 2. `backend.set_requested_class(worker_id, cls)` (U08-96). 3. Log INFO `jobs.gpu.class_requested` (`worker_id`, `class`); return `"requested"`. The arbiter (U08-70 step 1) swaps when the active window allows `cls` or `cls == "none"`, else rejects and clears the request with WARNING `jobs.gpu.request_rejected`. Callers poll `gpu_state().loaded_class()` for the result. |
| Side effects | One `worker` update. |
| Concurrency | Safe from any process; the supervisor reads the request on its next free GPU tick. |
| Security notes | TH08-09: the request goes through the arbiter, so a manual load never makes two classes resident. |
| Tests | UT08-108 |

### 3.16 Pipe, job contexts, worker and inline runs

#### U08-84 `herness.core.jobs.pipe.PipeMessage`, `encode_message`, `decode_message`

| Message `type` | Direction | Fields |
|----------------|-----------|--------|
| `stop` | parent → child | `reason: Literal["cancel","preempt","shutdown"]` |
| `gpu_reply` | parent → child | `request_id: str`, `ok: bool`, `error_class: Literal["ModelUnavailable","ConfigError"] \| None`, `message: str \| None` (≤ 500), `previous_class: GpuClass \| None`, `healthy: bool \| None` |
| `heartbeat` | child → parent | `note: str \| None` (≤ 200, redacted by the child) |
| `save_state` | child → parent | `state: dict` |
| `gpu_request` | child → parent | `request_id: str` (ULID), `op: Literal["require_class","service_start","service_stop","service_healthy"]`, `cls: GpuClass \| None`, `service: ServiceName \| None`, `timeout_s: float \| None` |
| `outcome` | child → parent | `status: Literal["done","yield","error"]`, `result: dict`, `error_class: str \| None`, `error_message: str \| None` (≤ 2 KB, redacted) |

| Field | Content |
|-------|---------|
| Kind | class (pydantic discriminated union on `type`, `extra="forbid"`, `strict=True`) plus two functions |
| Purpose | Design 08 §5.9 pipe messages, sent as JSON bytes (`Connection.send_bytes` / `recv_bytes`), never pickled objects (ENG §3.5). |
| Algorithm | `encode_message(m) -> bytes`: canonical JSON of `m.model_dump()`; > `MAX_PIPE_MSG_BYTES` (8 388 608) → `SchemaViolation`. `decode_message(b) -> PipeMessage`: size check, `json.loads`, `TypeAdapter(PipeMessage).validate_python`; failure → `SchemaViolation("bad pipe message")`. |
| Security notes | TH08-13. |
| Tests | UT08-97, ST08-13 |

#### U08-85 `herness.core.jobs.ChildJobContext`, `ChildServiceControl`

| Param | Type | Kind |
|-------|------|------|
| `conn` | `multiprocessing.connection.Connection` | positional |
| `row` | `JobRow` | positional |
| `slot` | `Literal["gpu","cpu"]` | keyword-only |

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | `JobContext` inside a child process; all effects go through the pipe to the supervisor (design 08 §5.8 last paragraph, §5.9). |
| Invariants | A daemon reader thread (`herness-pipe-reader`) is the only reader of `conn`: `stop` sets `_stop_reason` (first one wins) and a `threading.Event`; `gpu_reply` goes into a `queue.Queue` keyed by `request_id`. Writes to `conn` are serialised by a lock. |
| Algorithm | `job` returns `row` (R-42). `should_yield()` → `_stop_reason is not None`. `heartbeat(note)`: send at most one message per second (later calls within the second overwrite a pending note that the next allowed send carries); `note` is cut to 200 chars and passed through `redact_text`. `require_gpu_class(cls, timeout_s)`: `slot == "cpu"` → `ConfigError("GPU control requires the GPU slot")`; send `gpu_request(op="require_class", cls)`; wait for the reply up to `timeout_s` or the default (`R.gpu.stop_timeout_s + 60 + max start_timeout_s of cls + R.gpu.warmup_timeout_s + 60`); no reply → `ModelUnavailable("gpu request timed out")`; `ok == False` → raise the named class with the message. `gpu_scope(cls)`: call `require_gpu_class(cls)` and remember the reply's `previous_class`; on exit (normal or exception) call `require_gpu_class(previous_class)` unless `previous_class` equals `cls`; a restore failure during exception exit is logged at ERROR `jobs.gpu.restore_failed` and the original exception propagates. `save_state(state)`: canonical JSON ≤ 4 MiB else `SchemaViolation`; send `save_state`. `load_state()`: `row.result.get("state", {})` copied (empty on first attempt). `services.start/stop/healthy` send the corresponding `gpu_request` op (same slot check and reply handling; `healthy` returns the reply's `healthy`). |
| Concurrency | Handler thread plus the reader thread. |
| Tests | UT08-98, UT08-99 |

#### U08-86 `herness.core.jobs.InlineJobContext`, `InlineServiceControl`

Same behavior as U08-85 with direct calls: `require_gpu_class` → `controller.swap(cls, reason="in_job")` (the controller's own lock serialises it); `services.*` → `controller.service_*`; `save_state` → `backend.save_job_state(job_id, owner, bytes)` (0 rows → `JobStateError`); `heartbeat` stores the note in memory (no worker row exists for inline runs); `should_yield` reads a `threading.Event` set by the inline SIGINT handler or the heartbeat thread. `controller is None` (a `none`-class job whose kind is not in `GPU_SLOT_KINDS`) → GPU calls raise `ConfigError`. Kind: class. Tests: IT08-12.

#### U08-87 `herness.core.jobs.Supervisor`

| Param | Type | Kind |
|-------|------|------|
| `options` | `WorkerOptions` (frozen dataclass: `gpu_classes: tuple[GpuClass, ...]`, `concurrency: int` (0–16), `once: bool`, `bootstrap: str` as `"module:function"`) | positional |

Method `run() -> int` returns the process exit code under the R-46 scheme: 0 normal end; 1 operation failed (a `ConfigError` at start, the GPU lock held by another owner, or the store-unavailable shutdown). The worker never returns 2, 3 or 4 (delta D08-23).

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | The `herness worker` process (design 08 §5.9, §5.10). |
| Invariants | At most one child per slot; one GPU slot iff `gpu_classes` contains a class other than `none`; `worker_id = f"{host}:{pid}"` with `host = socket.gethostname().lower()`; lease owners `f"{worker_id}:gpu"` and `f"{worker_id}:cpu{i}"`. |
| Side effects | Everything in F08-04; `worker` row upserts every `heartbeat_s`. |
| Concurrency | Main thread runs the tick loop; one `ThreadPoolExecutor(max_workers=1, thread_name_prefix="herness-gpu")` runs `GpuController` work; children are `multiprocessing.get_context("spawn").Process`. |
| Complexity and limits | Idle tick does one worker-row read and at most one claim query per free slot; idle CPU < 1 % of one core (BT08-11). |
| Security notes | TH08-08 (owner guard), TH08-09 (GPU lock), TH08-13 (JSON pipe). |
| Tests | IT08-04–IT08-11, IT08-13, FT08-07, FT08-09, FT08-10, BT08-11 |

Algorithm `run()`:
1. Import and call `options.bootstrap` (loads config, binds backends, registers handlers and probes). Then `issues = validate_resilience_config(cfg)` (U08-99); any issue → log ERROR `jobs.worker.config_invalid` with the paths and return 1.
2. `upsert_worker(status="starting", gpu_slot, cpu_slots=concurrency, version=herness.__version__, faults_enabled)` where `faults_enabled` is true when a fault plan was loaded, which happens only with `HERNESS_FAULTS` set and `HERNESS_ENV=test` (R-40; the plan is loaded eagerly here so a bad plan fails at start with exit 1; outside the test environment the plan is ignored with WARNING `resilience.faults.ignored`).
3. GPU slot: enter `GpuLock` (failure → log ERROR `jobs.worker.gpu_lock_held`, return 1); `controller = GpuController(...)`; `controller.detect_loaded_class()`.
4. Crash recovery: for `row in running_on_host(host)`, parse the pid from `lease_owner`; if the pid ≠ own pid and `psutil.pid_exists(pid)` is false, collect the owner; `requeue_owned(owners, now)`; one `lease_expired` event per job (detail `outcome`).
5. Install signal handlers: `SIGINT`; `SIGBREAK` on Windows; `SIGTERM` on other systems. First signal → `draining = True`; second → `terminate_now = True`.
6. `update_worker(status="running")`; loop `tick(now)` then sleep `R.jobs.tick_s` until the loop ends (step 8).
7. `tick(now)` runs `_tick_guarded` (top-level `except Exception`): a `StoreBusy` or other failure logs ERROR `jobs.supervisor.tick_failed` and increments `failed_ticks`; a clean tick resets it; `failed_ticks ≥ 10` → log CRITICAL `jobs.supervisor.store_unavailable`, `draining = True`, exit code 1 at the end (R-46). Inside a tick, in order:
   a. Every `reaper_interval_s`: `reap_expired(now)` (events `lease_expired`, counter `herness_jobs_lease_expired_total{kind}`); `run_scheduler(now)`; `run_due_probes(now)`; restart check — for each open breaker `model:<k>` whose client's `gpu_class` equals `controller.loaded` and whose class has exactly one service, submit `controller.restart_service(service)` to the GPU executor when it is idle; `flush_metrics()`.
   b. Drain child messages with `conn.poll(0)`: `heartbeat` → `last_hb = now`, `note`; `save_state` → `save_job_state(job_id, owner, bytes)`; `gpu_request` → submit to the GPU executor (`require_class` → `controller.swap(cls, reason="in_job")` and reply with `previous_class`; `service_*` → the controller method); completed futures → send `gpu_reply`; `outcome` → keep for step c.
   c. Reap exited children: with an outcome → `finish_job(row, owner, outcome_or_error, attempt_started_at, stop_reason)`; without one → `finish_job(..., ModelUnavailable("child_crash"), ...)`. `join()` the process.
   d. Every `heartbeat_s` per running child: `heartbeat_job(job_id, owner, now + lease_s)`; `False` → send `stop("cancel")` once and remember the time; a child still alive `cancel_grace_s` later → `terminate()`, then `finish_job` (which finalises a canceled job or logs lease lost).
   e. Stall: `now − last_hb > stall_timeout_large_s` when `controller.loaded == "large"`, else `stall_timeout_s` → `terminate()`, `finish_job(..., ModelUnavailable("stalled"))`.
   f. Preemption for the GPU child: `d = preempt_deadline(controller.loaded, controller.class_since, now)`; `d` and `now ≥ d` and not yet sent → send `stop("preempt")`, record `preempt_sent_at`; `now ≥ preempt_sent_at + S.preempt_grace_min` → `terminate()` and `finish_yield(job_id, owner, next_window_allowing(row.gpu_class, now), now)` (no attempt charge), event `job_yield` with `stop_reason: "preempt"`.
   g. Not draining: CPU slots free → `_claim(owner=cpu owner, allowed=["none"])` (`GPU_SLOT_KINDS` jobs excluded by the owner suffix, U08-48). GPU slot free and GPU executor idle → `requested = worker.requested_class` if it was set by someone other than this supervisor (spec 10 deploy up/down); `claimable = claimable_counts(...)` with the chat-window rule (`min_priority = S.batch_in_chat_min_priority`, exempt kinds `["chat"]` when `window_at(now).spec.name == "chat"`); `d = arbiter_decide(controller.loaded, window, claimable, requested=requested)`; `swap` → submit `controller.swap(d.target, reason=d.reason)` (on `ModelUnavailable`: `postpone_class(d.target, now, now + 10 min)`, design 08 §5.8 "B jobs rescheduled +10 min without attempt charge"); `keep` → `_claim(owner=gpu owner, allowed=[controller.loaded, "none"], min_priority as above)` (class `none` admits only `GPU_SLOT_KINDS` jobs for the GPU owner, R-43). Each claimed row starts a child: `Pipe(duplex=True)`, `Process(target=child_main, args=(job_id, owner, slot, options.bootstrap, child_conn))`, `start()`.
   h. Every `heartbeat_s`: `update_worker(heartbeat_at=now, gpu_class_loaded, current_jobs=[{"job_id", "slot", "kind", "started_at", "note"}], status)`.
   i. `once`: after the first claimed job is finished, set `draining`; if nothing was claimable on the first tick, end the loop with exit code 0 and log INFO `jobs.worker.nothing_to_do`.
8. Draining: send `stop("shutdown")` to every child once; keep ticking steps b–c (no claims) until no child is alive or `shutdown_grace_s` passed; then `terminate()` the rest and call `finish_yield(job_id, owner, now, now)` for each (requeued at once, no charge). `terminate_now` skips the grace wait; those jobs stay `running` and are requeued by crash recovery at the next start (design 08 §5.9). Then `update_worker(status="stopped")`, `flush_metrics()`, release `GpuLock`. GPU services are left as they are.

#### U08-88 `herness.core.jobs.child.child_main`

| Param | Type | Kind |
|-------|------|------|
| `job_id` | `str` | positional |
| `owner` | `str` | positional |
| `slot` | `Literal["gpu","cpu"]` | positional |
| `bootstrap` | `str` | positional (`"module:function"`) |
| `conn` | `Connection` | positional |

Returns `None` (process exit code 0; 1 when the lease is not ours, R-46). The child's exit code is internal: the supervisor treats any exit without an `outcome` message as `child_crash`.

| Field | Content |
|-------|---------|
| Kind | function (child process entry; arguments are plain strings so the spawn payload carries no objects, TH08-13) |
| Algorithm | 1. Ignore `SIGINT` and, on Windows, `SIGBREAK` (the supervisor coordinates stops through the pipe). 2. Import and call `bootstrap`. 3. `row = get(job_id)`; if `row.status != "running"` or `row.lease_owner != owner` → exit 1. 4. `ctx = ChildJobContext(conn, row, slot=slot)`. 5. `handler = resolve_handler(row.kind)` (a `ConfigError` becomes the outcome). 6. `res = run_handler(ctx, handler)`. 7. Send `outcome` (`done`/`yield` with `result`, or `error` with class name and redacted message cut to 2 KB). 8. `flush_metrics()`; close `conn`; return. |
| Tests | IT08-04, IT08-07 |

#### U08-89 `herness.core.jobs.run_worker`

`run_worker(*, gpu_classes: Sequence[GpuClass], concurrency: int | None = None, once: bool = False, bootstrap: str) -> int`: builds `WorkerOptions` (`concurrency` default `R.jobs.cpu_slots`; `gpu_classes` default in the CLI is all four), returns `Supervisor(options).run()`. The CLI `worker` command (T09-22 (herness._cli.cmd_system)) passes `bootstrap="herness.cli:worker_bootstrap"` and exits with the returned code. Kind: function. Tests: IT08-11.

#### U08-90 `herness.core.jobs.run_inline`

| Param | Type | Kind |
|-------|------|------|
| `job_id` | `str` | positional |

Returns `JobOutcome`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | In-process run of one job, kept by R-45. CLI commands enqueue by default; the admin-only `--inline` flag of impl 09 calls this function (design 08 §5.7 last paragraph, spec 09 §5.6). |
| Algorithm | 1. `owner = f"{host}:{pid}:cli"`; `row = claim(owner=owner, allowed_classes=["none","reasoning","decider","large"], job_id=job_id)`; `None` → `JobStateError("job not claimable", job_id)`. 2. If `row.gpu_class != "none"` or `row.kind in GPU_SLOT_KINDS` (R-43): enter `GpuLock` (a `ConfigError` → `finish_yield(job_id, owner, now, now)` then re-raise); `controller = GpuController(worker_id=None, ...)`; `controller.detect_loaded_class()`; `controller.swap(row.gpu_class, reason="inline")` when the class is not `none` and differs from the loaded class (a `ModelUnavailable` → `finish_job` with that error, then re-raise). 3. Start a daemon heartbeat thread: every `heartbeat_s`, `heartbeat_job(job_id, owner, now + lease_s)`; `False` → set the stop event with reason `cancel`. 4. Install a SIGINT handler that sets the stop event with reason `shutdown` (Ctrl+C → yield). 5. `res = run_handler(InlineJobContext(...), resolve_handler(row.kind))`. 6. `finish_job(row, owner, res, attempt_started_at, stop_reason)`. 7. `finally`: stop the heartbeat thread, restore the SIGINT handler, release the lock. 8. `res` is a `JobOutcome` → return it; a `HernessError` → raise it. |
| Tests | IT08-12 |

### 3.17 Status, health and resume

#### U08-91 `herness.core.jobs.status_snapshot`, `StatusSnapshot`

`status_snapshot(now: datetime | None = None) -> StatusSnapshot` (a `TypedDict`; the design's `dict`).

| Key | Content (design 08 §5.14) |
|-----|---------------------------|
| `workers` | list of `{worker_id, host, pid, status, heartbeat_age_s, gpu_slot, cpu_slots, gpu_class_loaded, requested_class, services: {name: bool}, faults_enabled}`; service health from `LoopbackHttp.healthy` (2 s timeout, 5 s cache) only for the local host's GPU worker |
| `gpu` | `"no_worker"` when no alive GPU worker; `"unavailable"` when `gpu_class_loaded == "none"` and the latest `gpu_swap_failed` event is newer than the latest `gpu_swap`; else `"available"` |
| `window`, `next_window` | `{name, start_at, end_at, classes, preload}` from U08-66 |
| `chat_mode`, `chat_next_live_at` | `chat_policy(now)`, `chat_next_live_at(now)` |
| `running` | per running job `{job_id, kind, attempt, max_attempts, lease_left_s, slot, heartbeat_note}` |
| `planned_rekey` | `{job_id, scheduled_for}` of a queued `maintenance` job with `payload.action == "rekey"`, or `None` |
| `queue` | `{queued, next_job: {job_id, kind, priority, scheduled_for} \| None}` |
| `failed_24h`, `dead_letters` | failed jobs finished in the last 24 h; all jobs with `status = 'failed'` |
| `breakers` | not-closed rows `{key, state, since: opened_at, trips, probe_due}` |
| `counters_24h` | `event_counts(now − 24 h, [retry, fallback, repair, guard_stop, breaker_open, lease_expired, gpu_swap, gpu_swap_failed, job_done, job_failed, job_yield])` |
| `schedules` | `{name, next_fire_at}` for every `collect_schedules()` entry |
| `faults_enabled` | true when any alive worker has `faults_enabled = 1` or this process has faults enabled |

Kind: function. Read-only. Tests: UT08-100.

#### U08-92 `herness.core.jobs.health`, `ComponentHealth`

`health(now: datetime | None = None) -> ComponentHealth` (frozen dataclass `status: Literal["ok","degraded","down"]`, `reason: str`). `down` when `worker_alive()` is false (reason `no worker heartbeat`). `degraded` when any breaker is open, `dead_letters > 0` in the last 24 h, faults are enabled, or `gpu == "unavailable"` (reason lists the conditions). Else `ok`. Called by `herness doctor` (ENG §4; T09-22 (herness._cli.doctor.run_doctor)). Kind: function. Tests: UT08-101.

#### U08-93 `herness.core.jobs.enqueue_resume`, `ResumeResult`

| Param | Type | Default | Kind |
|-------|------|---------|------|
| `run_id` | `str` | — | positional |
| `force` | `bool` | `False` | keyword-only |
| `retry_dead` | `bool` | `False` | keyword-only |

Returns `ResumeResult` (frozen dataclass: `job_id: str | None`, `created: bool`, `run_status: str`).

| Field | Content |
|-------|---------|
| Kind | function (additive, D08-12) |
| Purpose | Logic behind `herness resume RUN_ID` (design 08 §5.12). Printing and `--wait` belong to spec 09. |
| Algorithm | 1. `(kind, status) = backend.run_row(run_id)`; `None` → `JobStateError("unknown run", run_id)`. 2. `kind == "chat"` → `JobStateError("chat runs resume through their chat job", run_id)`. 3. `status` in (`done`, `partial`, `failed`, `canceled`) and not `force` → return `ResumeResult(None, False, status)`. 4. `payload = {"run_id": run_id, "resume": True}` plus `"force": True` and `"retry_dead": True` when set; `(job_id, created) = submit(JobSpec("review", payload, "reasoning", priority=MANUAL_PRIORITY, idem_key=f"resume:{run_id}"))`; return. |
| Tests | UT08-102 |

### 3.18 Store adapters (L1)

#### U08-94 `herness.store.ops.resilience.SqliteResilienceBackend`

| Field | Content |
|-------|---------|
| Kind | class (implements `ResilienceBackend`) |
| Purpose | SQL for `source_health` and `resilience_event` (area `herness/store/ops/resilience.py`, R-08); metric rows are delegated to U08-100. |
| Preconditions | Reads use `read_one()` / `read_all()` and every write uses `run_write(fn, op=...)` of impl 02 (T02-04 (herness.store.ops.core.connection), T02-04 (herness.store.ops.core.run_write); R-10). |
| Algorithm | `health_get`: `SELECT * FROM source_health WHERE source = ?`. `health_apply(key, fn, now)`: `run_write`: select the row, `after = fn(before)`; if `after` is not `None`, `INSERT ... ON CONFLICT(source) DO UPDATE SET state, failures, trips, opened_at, last_error, updated_at`; return `(before, after)`. `health_claim_probe`: the design 08 §5.3 statement with the stale half-open extension `UPDATE source_health SET state = 'half_open', updated_at = :now WHERE source = :key AND ((state = 'open' AND :now >= :probe_due) OR (state = 'half_open' AND updated_at < :stale_before)) RETURNING source`. `health_reset`: `UPDATE ... SET state = 'closed', failures = 0, trips = 0, opened_at = NULL, updated_at = :now WHERE source IN (...) AND (state != 'closed' OR failures != 0)` returning the keys. `insert_event`: plain insert. `count_events`, `event_counts`, `latest_event`: indexed by `resilience_event(kind, ts)`. `insert_metric_samples(samples)`: returns `herness.store.ops.metrics.record_metric_samples(samples)` (U08-100). Module function `purge_events(before: datetime) -> int`: `DELETE FROM resilience_event WHERE ts < ?` in `run_write(op="resilience_event_purge")`, returning the count; called with `now − 90 days` by the `herness.admin` retention purge (R-07; T10-20 (herness.admin.maintenance.run_purge)) and re-exported as `herness.store.ops.purge_events`. All SQL is parameterised; the `IN (...)` placeholder list is generated from the list length only. |
| Concurrency | Per-thread connections; writes in `run_write` (`BEGIN IMMEDIATE`). |
| Tests | UT08-12–UT08-19, UT08-29, UT08-32, IT08-01 |

#### U08-95 `herness.store.ops.jobs.SqliteJobsBackend` (job methods)

| Method | SQL (all timestamps as ts text; JSON as TEXT) |
|--------|---------------------------------------------|
| `insert_job` | `run_write`: if `sched_check`: `SELECT 1 FROM job WHERE (idem_key = :sched_key) OR (json_extract(payload, '$.schedule') = :schedule AND json_extract(payload, '$.fire_at') = :fire_at) LIMIT 1` → found: return `(existing job_id, False)`. Then the design 08 §5.7 `INSERT ... ON CONFLICT (idem_key) WHERE status IN ('queued','running') DO NOTHING RETURNING job_id`; no row → `SELECT job_id FROM job WHERE idem_key = :idem_key AND status IN ('queued','running')` → `(that id, False)`. Else `(new id, True)` |
| `claim_job` | the design 08 §5.7 claim statement, with `:allowed_classes` and `:exclusive_kinds` as JSON arrays, extended inside the subquery by `AND (:min_priority IS NULL OR j.priority >= :min_priority OR j.kind IN (SELECT value FROM json_each(:exempt_kinds)))` and by the slot rule of U08-48: `AND (j.gpu_class != 'none' OR :slot = 'cli' OR ((j.kind IN (SELECT value FROM json_each(:gpu_slot_kinds))) = (:slot = 'gpu')))`, with `:slot` in `gpu`, `cpu`, `cli` and `:gpu_slot_kinds` = `GPU_SLOT_KINDS` as a JSON array (R-43); returns the updated row |
| `claimable_counts` | `SELECT gpu_class, COUNT(*)` with the same `WHERE` as the claim subquery for slot `gpu`, grouped by class |
| `heartbeat_job` | design 08 §5.7 heartbeat statement; returns `rowcount == 1` |
| `finish_done` | `UPDATE job SET status='done', result=:result, finished_at=:now, lease_owner=NULL, lease_expires_at=NULL WHERE job_id=:id AND lease_owner=:owner AND status='running'` |
| `finish_yield` | `SET status='queued', attempts=attempts-1, scheduled_for=:resume_at, lease_owner=NULL, lease_expires_at=NULL` with the same guard |
| `finish_requeue` | `SET status='queued', scheduled_for=:at, last_error=:le, lease_owner=NULL, lease_expires_at=NULL` with the same guard |
| `finish_failed` | `SET status='failed', finished_at=:now, last_error=:le, lease_owner=NULL, lease_expires_at=NULL` with the same guard |
| `finalize_canceled` | `SET finished_at=:now, lease_owner=NULL, lease_expires_at=NULL WHERE job_id=:id AND lease_owner=:owner AND status='canceled'` |
| `save_job_state` | `SET result = json_object('state', json(:state)) WHERE job_id=:id AND lease_owner=:owner AND status='running'` |
| `cancel_job`, `retry_job` | `run_write` read-then-update per U08-51, U08-52; `retry_job` catches the unique-index `IntegrityError` → `conflict` |
| `reap_expired` | the two design 08 §5.7 reaper statements in one `run_write`, preceded by a `SELECT job_id, kind, attempts, max_attempts` of the affected rows to report them |
| `requeue_owned` | the same two statements with `lease_owner IN (...)` in place of the expiry test |
| `running_on_host` | `SELECT * FROM job WHERE status='running' AND lease_owner LIKE :host || ':%'` (`:host` escaped for `LIKE`) |
| `postpone_class`, `sched_fired`, `recent_scheduled`, `rekey_on`, `queue_stats`, `get_job`, `list_jobs` | straightforward parameterised selects and one update per U08-41 |

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | Every write to `job` (design 08 §3.7 last paragraph); area `herness/store/ops/jobs.py` (R-08). |
| Concurrency | Every write in `run_write` (`BEGIN IMMEDIATE`, `sqlite_write` policy); the connection settings, including `busy_timeout`, are impl 02's. |
| Security notes | TH08-08 (owner guard on every completion). |
| Tests | UT08-52–UT08-62, UT08-107, IT08-02, BT08-02, BT08-03 |

#### U08-96 `herness.store.ops.worker.WorkerSqlMixin`

`upsert_worker(row: WorkerRow)` (`INSERT ... ON CONFLICT(worker_id) DO UPDATE`), `update_worker(worker_id, **fields)` (column names checked against a fixed allowlist `status, gpu_class_loaded, requested_class, current_jobs, heartbeat_at, faults_enabled`; values bound as parameters), `list_workers()`, `set_requested_class(worker_id, cls)` (`UPDATE worker SET requested_class = ? WHERE worker_id = ?`; called only by `request_gpu_class`, U08-102, which serves impl 09 `gpu load/unload` and impl 10 `deploy up/down`), `run_row(run_id)` (`SELECT kind, status FROM run WHERE run_id = ?`). Area `herness/store/ops/worker.py` (R-08). Kind: class (mixin of `SqliteJobsBackend`). Tests: UT08-65, UT08-108.

#### U08-97 `herness.store.ops.tasks.TaskSqlMixin`

`recover_tasks`, `claim_task`, `save_checkpoint(task_id, key, value, writes)`, `complete_task`, `fail_task`, `release_task`, each implementing the SQL named in U08-57–U08-63 in one `run_write` (area `herness/store/ops/tasks.py`, R-08). `save_checkpoint` reads `task.checkpoint`, applies `build_checkpoint_envelope(key, value, existing)` (U08-59) and writes the result in the same transaction (R-21). The `writes` callback receives the same connection inside the transaction; any exception rolls back. Only this mixin writes `task.status`, `task.attempts` and `task.checkpoint` (design 08 §3.7). Kind: class (mixin). Tests: UT08-66–UT08-71, UT08-109.

#### U08-98 `herness.store.ops.resilience.bind_core_backends`

`bind_core_backends() -> None`: constructs `SqliteResilienceBackend()` and `SqliteJobsBackend()` and calls `herness.core.resilience.bind_ops_backend` and `herness.core.jobs.bind_jobs_backend` (L1 → L0 imports are allowed). Called by every composition root (T09-20 (herness.cli.main), T09-27 (herness.cli.worker_bootstrap), T09-13 (app.common.bootstrap.get_services)) after `load_config` (R-04). Re-exported as `herness.store.ops.bind_core_backends`. Kind: function. Tests: IT08-04.

#### U08-100 `herness.store.ops.metrics.record_metric_samples`, `purge_metric_samples`

| Param | Type | Default | Kind | Constraints |
|-------|------|---------|------|-------------|
| `samples` | `Sequence[MetricSample]` | — | positional | U08-101 values; ≤ 100 000 per call |
| `conn` | `sqlite3.Connection \| None` | `None` | keyword-only | when given, the caller is inside its own `run_write` callback and the rows join that transaction |

Returns `int` (rows written).

| Field | Content |
|-------|---------|
| Kind | function (two) |
| Purpose | The single writer of `metric_sample` (R-12). Every spec records component metrics through this name (it replaces `write_metric_samples` and `record_metric_sample`) or through the buffered L0 API of U08-19–U08-22 and U08-103, which calls it. Counters, gauges and histograms are all accepted; the kind travels in `MetricSample.kind`. |
| Preconditions | Every sample passed pydantic validation (U08-101); more than 100 000 samples → `ConfigError("too many metric samples")`. |
| Postconditions | One `metric_sample` row per sample, columns exactly as impl 02 §4.3.6: `ts` (ts text), `name`, `kind`, `value`, `labels` (canonical JSON), `component`. |
| Algorithm | 1. Empty input → return 0. 2. Build parameter tuples; `labels` = canonical JSON of the label map; a labels text longer than 1 024 bytes → `ConfigError` naming the metric (the table's length check). 3. With `conn`: one `executemany("INSERT INTO metric_sample (ts, name, kind, value, labels, component) VALUES (?, ?, ?, ?, ?, ?)")` on it. Without `conn`: the same statement in chunks of 500 rows, each chunk in its own `run_write(op="metric_samples")`. 4. Return the row count. `purge_metric_samples(before: datetime) -> int`: `DELETE FROM metric_sample WHERE ts < ?` in `run_write(op="metric_purge")`, returning the count; called with `now − 90 days` by the `herness.admin` retention purge (R-07, O08-09). |
| Side effects | `metric_sample` inserts or deletes. |
| Errors | `StoreBusy` after the `run_write` policy; `SchemaViolation` for a table constraint failure (mapped by `run_write`); `ConfigError` as above. Callers treat metrics as best effort: they log and continue. |
| Concurrency | Per-thread connection; each chunk is one short writer transaction. |
| Complexity and limits | 500 rows per transaction; O(n). |
| Security notes | TH08-02 (labels are identifiers only), TH08-10 (row cap per call). |
| Tests | UT08-32, UT08-110 |

## 4. State and data

### 4.1 Ops tables

The migration files that create these tables are impl 02 migrations 001–006 (T02-05 (herness/store/migrations/001_ingestion_health.sql, 002_jobs.sql) and T02-06 (herness/store/migrations/003_runs_evidence.sql to 006_metric_sample.sql), applied by T02-05 (herness.store.ops.migrate.migrate); R-11). The columns below restate impl 02 §4.3 with the constraints 08 relies on. Every column and index 08 reads or writes exists in impl 02 migrations 001–006, so this spec adds no migration in its range 050–059 (R-11); a future 08-only column gets a migration numbered 050 or above and a unit here. All timestamps are ts text (fixed width, UTC); JSON is TEXT.

#### 4.1.1 `job` (owner 08)

| Column | Type | Null | Constraint | Meaning |
|--------|------|------|------------|---------|
| `job_id` | TEXT | no | PK, `job_<ulid>` | |
| `kind` | TEXT | no | CHECK in `JobKind` | |
| `gpu_class` | TEXT | no | CHECK in `GpuClass` | class loaded before the handler starts |
| `status` | TEXT | no | CHECK in `queued`, `running`, `done`, `failed`, `canceled` | `failed` = dead letter |
| `priority` | INTEGER | no | CHECK 0–100 | higher first |
| `payload` | TEXT | no | JSON object ≤ 64 KB | references only |
| `idem_key` | TEXT | yes | partial unique index | §4.1 of design |
| `result` | TEXT | yes | JSON | `{"state": …}` while running; `JobOutcome.result` when done |
| `attempts` | INTEGER | no | ≥ 0 | +1 at claim, −1 on yield |
| `max_attempts` | INTEGER | no | 1–20 | |
| `last_error` | TEXT | yes | JSON `{class, message, at, attempt}` | message redacted, ≤ 2 KB |
| `lease_owner` | TEXT | yes | `<host>:<pid>:<slot>` | |
| `lease_expires_at`, `scheduled_for`, `created_at`, `started_at`, `finished_at` | TEXT | `scheduled_for`, `created_at` no; others yes | ts | |

Indexes: `UNIQUE job(idem_key) WHERE status IN ('queued','running')`; `job(status, gpu_class, scheduled_for, priority)`. Retention: not purged by 08 (O08-08).

Writes and idempotency keys:

| Write | Unit | Idempotency | Transaction |
|-------|------|-------------|-------------|
| insert | U08-46 | `idem_key` (default `kind:`+16 hex; `sched:<name>:<fire_at>[:<step>]`; `sync:<source>`; `resume:<run_id>`; `rekey:<key_id>`; `chat:<session_id>:<message_id>` from spec 06) plus the sched check | one `BEGIN IMMEDIATE` |
| claim | U08-48 | `WHERE status = 'queued'` on the chosen row | one `BEGIN IMMEDIATE` |
| heartbeat, completions, state save | U08-50, U08-87 | `WHERE job_id AND lease_owner AND status` guard: a stale owner changes nothing | single statement |
| cancel, retry | U08-51, U08-52 | state checks | one `BEGIN IMMEDIATE` |
| reaper, owner requeue, postpone | U08-87 | `WHERE status = 'running' AND lease_expires_at < now` / owner list / class and due time | one `BEGIN IMMEDIATE` |

#### 4.1.2 `worker` (owner 08)

| Column | Type | Null | Constraint |
|--------|------|------|------------|
| `worker_id` | TEXT | no | PK `<host>:<pid>` |
| `host` | TEXT | no | |
| `pid` | INTEGER | no | |
| `gpu_slot` | INTEGER | no | 0 or 1 |
| `cpu_slots` | INTEGER | no | 0–16 |
| `gpu_class_loaded` | TEXT | no | `none`, `reasoning`, `decider`, `large`, `swapping` |
| `requested_class` | TEXT | yes | `GpuClass` |
| `status` | TEXT | no | `starting`, `running`, `draining`, `stopped` |
| `current_jobs` | TEXT | no | JSON list of `{job_id, slot, kind, started_at, note}` (`note` additive, D08-11) |
| `started_at`, `heartbeat_at` | TEXT | no | ts |
| `version` | TEXT | no | package version |
| `faults_enabled` | INTEGER | no | 0 or 1 |

Idempotency: upsert on `worker_id`. Rows of stopped workers are kept (status `stopped`) and overwritten when the same host and pid reappear.

#### 4.1.3 `source_health` (owner 08)

| Column | Type | Null | Constraint |
|--------|------|------|------------|
| `source` | TEXT | no | PK; `<connector>`, `monitoring:<tool>`, `model:<client key>`, `decider:<name>` |
| `state` | TEXT | no | `closed`, `open`, `half_open` |
| `failures` | INTEGER | no | consecutive failed attempts |
| `trips` | INTEGER | no | consecutive opens without a good probe |
| `opened_at` | TEXT | yes | ts |
| `last_error` | TEXT | yes | redacted, ≤ 500 chars |
| `updated_at` | TEXT | no | ts |

Idempotency: upsert on `source`; transitions are read-modify-write in one `BEGIN IMMEDIATE`; the probe claim is a guarded `UPDATE ... RETURNING`.

#### 4.1.4 `resilience_event` (owner 08)

| Column | Type | Null | Constraint |
|--------|------|------|------------|
| `event_id` | TEXT | no | PK `evt_<ulid>` (D08-14) |
| `ts` | TEXT | no | ts |
| `kind` | TEXT | no | `EVENT_KINDS` |
| `component` | TEXT | no | `resilience` or `jobs` |
| `target` | TEXT | yes | ≤ 200 chars |
| `run_id`, `job_id`, `task_id` | TEXT | yes | |
| `detail` | TEXT | no | JSON object of allowlisted scalar fields |

Index `resilience_event(kind, ts)`. Retention 90 days, purged through `purge_events` (U08-94) by the `herness.admin` retention purge (R-07; T10-20 (herness.admin.maintenance.run_purge)). Inserts are append-only; no idempotency key (an event written twice after a crash is acceptable history).

#### 4.1.5 `task` mechanics (table owner 06, mechanics 08)

08 writes only `status`, `attempts`, `checkpoint`, `result`, `last_error`, `updated_at`, through U08-57–U08-63. The `checkpoint` column holds the R-21 envelope; each key is replaced only by its owner's `save_checkpoint` call. Idempotency: every update is guarded by the expected current status; durable side effects run inside `save_checkpoint(..., writes=)` or `complete_task(..., writes=)` in the same transaction (design 08 §5.12). Index `task(run_id, status)` (spec 02 §5.3).

#### 4.1.6 `metric_sample` (table owner impl 02; recording API 08)

Table from impl 02 migration 006 (R-12): rowid primary key; `ts` TEXT not null (ts check); `name` TEXT not null (`GLOB 'herness_*'`); `kind` TEXT not null (`counter`, `gauge`, `histogram`); `value` REAL not null; `labels` TEXT not null (JSON object, ≤ 1 024 bytes); `component` TEXT not null. Indexes `metric_sample_name_ts` on (`name`, `ts`) and `metric_sample_ts` on (`ts`). The earlier 08 draft columns `sample_id`, `host` and `pid` are dropped (D08-13 resolved by R-12). Only U08-100 writes it. Retention 90 days (`purge_metric_samples`, called by the `herness.admin` retention purge, R-07; O08-09). Append-only; no idempotency key (a sample written twice after a crash is acceptable).

### 4.2 Files

| File | Written by | Content | Rules |
|------|-----------|---------|-------|
| `data/locks/gpu.lock` | U08-82 | empty; OS byte lock | created on demand; ACL per spec 10 (service account only) |
| Fault plan (path in `HERNESS_FAULTS`) | spec 11 tests | JSON list of `FaultRule` (R-40) | honoured only with `HERNESS_ENV=test`, else ignored with a WARNING; read once per process; `.json` only; ≤ 64 KB; never written by Herness |
| `task.checkpoint` envelope | U08-59/U08-60 | `{schema_version, loop, state, scratchpad}` (R-21) | ≤ 4 MiB; key-scoped replace; `loop` dropped first |

### 4.3 In-memory state

| State | Holder | Lifetime | Concurrency |
|-------|--------|----------|-------------|
| Breaker cache (row + read time, 5 s) | `CircuitBreaker` in `ProcessState.breakers` | process | per-breaker lock |
| Fault plan and rule counters | `ProcessState` | process | `ProcessState.lock` |
| Metric buffer (≤ 10 000 histogram observations, counter aggregates, ≤ 1 000 gauge keys with their latest value) | `ProcessState` | until flush | `ProcessState.lock` |
| Handler and probe registries | `ProcessState` | process | written at bootstrap only |
| `auth_dropped` | `ProcessState` | process (entries are per `run_id`) | `ProcessState.lock` |
| Policy cache keyed by `config_hash` | `ProcessState` | process | `ProcessState.lock` |
| Worker-row and service-health caches (5 s) | `WorkerGpuState` | process | instance lock |
| Children, last heartbeat, preempt timers, GPU executor futures | `Supervisor` | worker process | main thread only |
| `_missed_reported` set (≤ 1 000) | `scheduler` module via `ProcessState` | process | `ProcessState.lock` |

## 5. Control flows

**F08-01 Retried call with breaker** (`retry_call`, design 08 §5.2, §5.3)
1. U08-12 resolves the policy. Failure: unknown name → `ConfigError` to caller.
2. U08-28 `_invoke` calls U08-26 `guard(key)`. Open and not due → `CircuitOpen` to caller, no retry (sync: spec 01 or the job layer marks the source skipped).
3. `fn` runs. Success → U08-24 `record_success` (no I/O on the closed hot path) → return.
4. Failure: foreign exception → U08-16 `classify`. Counting classes → U08-24 `record_failure` (may open the breaker, `breaker_open` event).
5. `_should_retry` false (non-retryable, `CircuitOpen`, Retry-After over cap) → raise.
6. Retry allowed: U08-14 wait; `retry` event and metric (U08-18); sleep; go to step 2.
7. Stop condition (attempts or `max_elapsed_s`) → raise the last error.

**F08-02 Model chain call** (`ModelChain.acomplete`, design 08 §5.4)
1. U08-37 `candidates()` filters the chain (egress, open breakers, auth-dropped, GPU class). Empty → `ModelUnavailable`.
2. Per candidate: F08-01 around `fault_point("llm.call")` + `client.acomplete` with breaker `model:<key>`.
3. Schema set → U08-35 validates, repairs ≤ 2 times (`repair` events); still invalid → `OutputValidationError`.
4. Trigger error → `fallback` event → next candidate with the original request. Non-trigger error → propagate.
5. Exhausted → ERROR log, raise last error; spec 06 `fail_task` decides the task state.

**F08-03 Enqueue** (U08-46)
1. U08-45 validates the payload. Failure → `SchemaViolation`, nothing written.
2. `priority=None` becomes `DEFAULT_PRIORITY[kind]` (R-41).
3. `insert_job` in one `run_write` (`BEGIN IMMEDIATE`): sched check → existing id; insert or dedupe → id. `StoreBusy` → retried by the `run_write` `sqlite_write` policy; exhausted → `StoreBusy` to caller.
4. Log and metric.

**F08-04 Supervisor tick** (U08-87 step 7) — steps a–i as listed in U08-87. Failure at any step: `StoreBusy` after retries or any exception → the tick stops at that step, children keep running, `failed_ticks += 1`; 10 in a row → graceful shutdown (F08-09) with exit code 1 (design 08 §6, R-46).

**F08-05 Job run in a child and completion**
1. U08-48 claims (lease, attempts + 1, `job.after_claim` fault point).
2. Supervisor spawns U08-88 with plain-string arguments. Child bootstrap failure → child exits without outcome → step 5 treats it as `child_crash`.
3. Child verifies lease ownership (exit 1 if not ours), resolves the handler, runs U08-56 with `ctx` as the only argument (R-42).
4. Child sends `outcome`; heartbeats and `save_state` flow meanwhile (state written with the owner guard).
5. Supervisor reaps the child → U08-50 applies the outcome: `done` (+ chain advance), `yield` (−1 attempt), requeue with backoff, or `failed` (+ `chain_broken`). Lease lost → WARNING only.

**F08-06 GPU swap** (U08-81 `swap`, design 08 §5.8)
1. Worker row → `swapping`. 2. Stop services not in the target (kill fallback); `gpu.after_stop` fault point. 3. VRAM check; failure → class `none`, `gpu_swap_failed`, `ModelUnavailable`. 4. Start target services; health poll under `gpu_health`; `gpu.after_start` fault point. 5. Warm-up. 6. Class set, breakers reset, `gpu_swap` event. Failure in 4–5 → stop target services, class `none`, breaker failures, `gpu_swap_failed`; arbiter swaps postpone that class's due jobs by 10 min; in-job requests raise `ModelUnavailable` to the handler.

**F08-07 In-job GPU request**
1. Handler calls `ctx.require_gpu_class`, `ctx.gpu_scope` or `ctx.services.*` (after releasing in-process GPU models). CPU slot → `ConfigError`.
2. Child sends `gpu_request`; supervisor submits it to the GPU executor; ticks continue (lease heartbeats go on).
3. Executor runs F08-06 or the service method; reply `gpu_reply` with `previous_class`.
4. Child raises the named error on failure; `gpu_scope` restores the previous class on exit.

**F08-08 Preemption** (design 08 §5.10)
1. Each tick U08-69 computes the deadline for the GPU child's current class.
2. Past the deadline → `stop("preempt")`; handler sees `should_yield()`, checkpoints and returns `yield` → F08-05 step 5 requeues at `next_window_allowing` with no attempt charge.
3. No outcome within `preempt_grace_min` → terminate; `finish_yield` at the next allowed window, no charge.

**F08-09 Shutdown** — first signal: stop claiming, `stop("shutdown")` to children, wait `shutdown_grace_s` while processing outcomes; terminate the rest and requeue them at once without charge. Second signal: terminate immediately; crash recovery requeues at next start. GPU services untouched; `GpuLock` released; worker row `stopped`.

**F08-10 Lease expiry and crash recovery**
1. Every `reaper_interval_s` each worker runs the reaper statements: expired and below `max_attempts` → `queued` with `last_error.class = LeaseExpired`; at the cap → `failed`. `lease_expired` events.
2. At worker start: running jobs owned by this host with a dead pid are requeued with the same statements (same-host restart ≤ 10 s, design 08 §8).
3. A stale owner that comes back finds 0 rows on heartbeat → `stop("cancel")`; its completion updates 0 rows (TH08-08).

**F08-11 Scheduler fire, catch-up and chain** — U08-72 per entry: latest fire `F`; too old → `schedule_missed` once; else submit with sched check (`schedule_fired` when created). On `done`, U08-73 enqueues the next enabled, non-skipped step; on `failed`, `chain_broken`. Chain repair re-runs U08-73 over the last 24 h of finished scheduled jobs. A broken entry logs `jobs.schedule.error` and never blocks others.

**F08-12 Rekey planning and night** — spec 10 calls U08-74 → `maintenance {"action":"rekey"}` at the next `schedule.rekey.cron` fire ≥ 12 h ahead, priority 70, class `decider`, `rekey_planned` event. That night the `nightly` build (class `none`, a `GPU_SLOT_KINDS` job, R-43) fires with `rekey_night: true`; both jobs are claimed by the GPU slot, and priority 70 > 60 and `exclusive_kinds` make the rekey job run first; the build's enrichment stages enter `ctx.gpu_scope("decider")`, and decider is allowed in the Saturday `reviews` window so the build is not preempted at 21:00; U08-73 skips the standard review steps with `chain_skipped` reason `rekey`. Canceling the rekey job (U08-51) leaves the old key active (spec 10).

**F08-13 Task lifecycle and resume** — spec 06 `resume()` → U08-57 resets `running` and eligible `failed` tasks (and `dead` with `retry_dead`); U08-58 claims each pending task (+1 attempt); spec 05 `after_step` → U08-60 checkpoints; findings commit through `writes`; U08-61 completes; errors → U08-62 (`pending` or `dead`); cancel or preempt → spec 06 calls U08-63 `release_task` (R-36). Spec 05 saves key `loop`, spec 06 key `state` and spec 07 key `scratchpad` of the same envelope, and no save erases another owner's key (R-21). `herness resume` → U08-93 enqueues `resume:<run_id>` at priority 80.

**F08-14 Inline run** — U08-90 (admin-only `--inline`, R-45): claim the named job as `<host>:<pid>:cli`; take `GpuLock` when the class is not `none` or the kind is in `GPU_SLOT_KINDS`, and swap when the class is not `none`; heartbeat thread; handler in-process; Ctrl+C → `shutdown` → `yield`; U08-50 applies the outcome; lock released in `finally`.

**F08-15 Chat mode decision** — U08-75: live when reasoning is loaded, `vllm-reasoning` healthy and the chat model breaker not open; else the configured in-hours or off-hours mode; `cloud` without egress, or in the `hybrid` profile without the `chat` data-policy approval, becomes `small_model` (R-38); `small_model` with its breaker open becomes `defer`. Spec 06 enqueues the `chat` job for `defer` and enqueues escalation runs under `small_model` for the next live window (R-35); U08-77 gives the ETA used as `scheduled_for`.

**F08-16 Fault injection** — U08-34: without `HERNESS_FAULTS`, return; with it but `HERNESS_ENV` not `test`, ignore the plan and log `resilience.faults.ignored` once (R-40); under `test`, load and validate the JSON plan once (bad plan → `ConfigError` at first use or at worker start), log WARNING, set the worker flag, and apply the first matching rule's action.

## 6. Error handling

| Failure condition | Class raised | Caught where | Retry or fallback | User-visible effect | Log event |
|-------------------|--------------|--------------|-------------------|---------------------|-----------|
| HTTP 5xx/529, connect or read timeout (source) | `SourceUnavailable` | U08-28 | policy retries; breaker counts | sync slower or skipped | `resilience.call.retry_scheduled` |
| Same for model or decider | `ModelUnavailable` | U08-28, U08-38, U08-39 | retries, then next chain entry | answer from a fallback model; trace `fallback` | `resilience.chain.fallback_used` |
| HTTP 429 | `RateLimited` | U08-28 | honor `retry_after` ≤ cap; over cap re-raise; job reschedules at `now + retry_after` | delay | `resilience.call.retry_scheduled` |
| Breaker open | `CircuitOpen` | U08-26 callers, U08-38, U08-49 | no retry; next chain entry; sync/reconcile `done` partial; other jobs at `retry_at` | `herness status` lists the breaker | `resilience.breaker.opened` |
| HTTP 401/403 | `AuthError` | U08-28 (not retried), U08-38 | source breaker `force_open` (spec 01 calls `force_open`); model entry dropped for the run | job `failed`, task `dead` | `resilience.breaker.opened` |
| Output fails schema | `OutputValidationError` | U08-35, U08-38 | 2 repairs, then next candidate | none unless the chain is exhausted (task `dead`) | `resilience.output.repaired` |
| Refusal | `ModelRefused` | U08-38 | next candidate, no retry | as above | `resilience.chain.fallback_used` |
| Egress refused | `EgressBlocked` | U08-38 | next local candidate | as above | `resilience.chain.fallback_used` |
| SQLite locked or busy | `StoreBusy` | impl 02 `run_write` with policy `sqlite_write` (U08-28, R-10) | 6 attempts within 30 s | none; beyond → caller error; supervisor skips the tick | `resilience.call.retry_scheduled` |
| Supervisor tick fails 10 times in a row | — | U08-87 | graceful shutdown, exit 1 (R-46) | worker stops; status shows no worker | `jobs.supervisor.store_unavailable` |
| Handler raises a foreign exception | `FatalError` | U08-56 | none | job `failed` with `last_error` | `jobs.handler.crashed` |
| Child exits without outcome | `ModelUnavailable("child_crash")` | U08-87 | job backoff until `max_attempts` | job retried | `jobs.job.rescheduled` |
| Child silent past stall timeout | `ModelUnavailable("stalled")` | U08-87 | job backoff | job retried | `jobs.job.stalled` |
| Lease lost (clock jump, reaper, cancel) | — (0 rows) | U08-50, U08-87 | child stopped at next heartbeat | none | `jobs.job.lease_lost` |
| Compose missing, WSL or Docker down | `ModelUnavailable("compose unavailable")` | U08-81 | GPU jobs postponed 10 min; CPU slots continue | status `gpu: unavailable`; chat uses `in_hours_unavailable` | `jobs.gpu.compose_failed` |
| VRAM not freed | `ModelUnavailable("vram_not_freed")` | U08-81 | class `none`; postpone | as above | `jobs.gpu.swap_failed` |
| Service start or warm-up fails | `ModelUnavailable` | U08-81 | stop services, class `none`, breaker failure | as above | `jobs.gpu.swap_failed` |
| Start of a service from another class | `ConfigError` | U08-81 → handler | none | job `failed` | `jobs.job.failed` |
| GPU call from a CPU slot | `ConfigError` | U08-85 | none | job `failed` | `jobs.job.failed` |
| Second GPU worker on the host | `ConfigError` | U08-82 | none | `herness worker` exits 1 (R-46) | `jobs.worker.gpu_lock_held` |
| Invalid schedule or window set at worker start | `ConfigError` issues from U08-99 | U08-87 | none | `herness worker` exits 1; `herness config validate` lists the same issues | `jobs.worker.config_invalid` |
| `HERNESS_FAULTS` set outside `HERNESS_ENV=test` | — | U08-34 | plan ignored (R-40) | none; status shows no faults | `resilience.faults.ignored` |
| Invalid fault plan | `ConfigError` | U08-33 | none | process fails at first fault point or worker start | `resilience.faults.invalid` |
| Payload too large or holds secrets | `SchemaViolation` | U08-45 | none | enqueue refused | `jobs.job.rejected` |
| Checkpoint > 4 MiB | warning, then `SchemaViolation` | U08-59 | `loop` dropped first | resume restarts that task from its spec | `jobs.checkpoint.loop_dropped` |
| Task not running on checkpoint or completion | `JobStateError` | U08-60–U08-62 | none | spec 06 stops the task | `jobs.task.state_conflict` |
| Broken schedule entry | `HernessError` | U08-72 | entry skipped this tick | other schedules fire | `jobs.schedule.error` |
| Event insert fails | — | U08-18 | dropped | none | `resilience.event.dropped` |
| Metric flush fails | — | U08-22 | rows discarded | gaps in metrics | `resilience.metrics.flush_failed` |

Retries happen only in the resilience layer (ENG §3.4). Error messages carry `job_id`, `task_id`, `run_id`, keys and class names only (TH08-02).

## 7. Security

### 7.1 Trust boundaries touched

| Boundary | How 08 touches it |
|----------|-------------------|
| TB4 Model output → tools and stores | `complete_validated` parses and validates model JSON before anything uses it; loop policy bounds runaway loops |
| TB6 Host → off-network endpoints | `ModelChain.candidates` and `chat_policy` never select an off-network client unless the profile enables egress; every such call still passes the spec 10 guard in the adapter |
| TB8 Host ↔ WSL2 containers | compose commands, loopback health and warm-up requests, VRAM checks, service restarts |
| TB10 Operator → CLI, config, env | `config/resilience.yaml` (commands, URLs, windows), `HERNESS_FAULTS`, CLI job control |

### 7.2 STRIDE threats

| ID | Boundary | STRIDE | Threat | L | I | Control | Reference | Test |
|----|----------|--------|--------|---|---|---------|-----------|------|
| TH08-01 | TB8, TB10 | T, E | Shell or argument injection through compose, `wsl.exe` or `nvidia-smi` invocations | L | H | Argument lists with `shell=False`; service names must be configured keys matching `^[a-z][a-z0-9-]{0,63}$`; class names from `GpuClass`; no user input reaches argv (U08-78) | ASVS v5.0.0-V1.2, ASVS v5.0.0-V15.3 | ST08-01 |
| TH08-02 | all | I | Secrets, ticket text or prompts leak into `last_error`, `resilience_event.detail`, logs, traces or metric labels | M | H | Bodies cut to 500 chars then `redact_text`; messages ≤ 2 KB; detail allowlist of scalar fields; metric label regex; bearer and key values never logged; rekey logs only `key_id` (U08-16, U08-18, U08-19, U08-74) | ASVS v5.0.0-V16.2, ASVS v5.0.0-V14.2, LLM02 | ST08-02 |
| TH08-03 | TB10 | I | Job payload carries a secret or record text into the ops store | M | M | `validate_payload`: ≤ 64 KB, key regex, known secret values, credential and URL-token scan (U08-45) | ASVS v5.0.0-V2.2, ASVS v5.0.0-V13.3 | ST08-03 |
| TH08-04 | TB1, TB6 | D | A hostile or broken server sends a huge `Retry-After` and parks work | M | M | `retry_after_cap_s`; above the cap the call re-raises and the job reschedules at a known time (U08-28) | ASVS v5.0.0-V2.4 | ST08-04 |
| TH08-05 | TB1, TB6 | D | Retry storms hammer a down source or model | M | M | Bounded attempts and `max_elapsed_s`, full jitter, breakers with exponential cooldown, single probe claim across processes (U08-24, U08-28) | ASVS v5.0.0-V15.4, LLM10 | ST08-05 |
| TH08-06 | TB10 | T, D | `HERNESS_FAULTS` set in production kills processes or corrupts behavior unnoticed | L | H | Hook inert without the variable and outside `HERNESS_ENV=test` (a plan is then ignored with a WARNING, R-40); JSON-only plan file ≤ 64 KB, closed action and point sets; WARNING log, `worker.faults_enabled`, `FAULTS ENABLED` in status and a degraded `health()` (U08-33, U08-34, U08-92) | ASVS v5.0.0-V13.1, ASVS v5.0.0-V16.3 | ST08-06 |
| TH08-07 | TB6 | I | Fallback or chat routing sends data off-network when the profile forbids egress | L | H | Off-network candidates removed unless `security.egress.enabled`; `cloud` mode degraded to `small_model`; spec 10 egress guard still checks each call; `EgressBlocked` falls back to local (U08-37, U08-75) | ASVS v5.0.0-V14.2, LLM02 | ST08-07 |
| TH08-08 | TB10 | T | A stale lease owner (clock jump, reaped job) completes a job or saves state, duplicating side effects | M | M | Every completion and state write guarded by `lease_owner` and `status`; heartbeat 0 rows stops the child (U08-50, U08-95) | ASVS v5.0.0-V15.4 | ST08-08 |
| TH08-09 | TB8 | S, T | A second GPU-capable worker or a manual `deploy up` loads a second class and corrupts VRAM state | L | M | OS lock on `data/locks/gpu.lock`; deploy up/down goes through `worker.requested_class` while a worker lives (U08-82, U08-70) | ASVS v5.0.0-V15.4 | ST08-09 |
| TH08-10 | TB10, TB4 | D | Unbounded payloads, results, checkpoints or pipe messages exhaust the ops store or memory | L | M | Caps: payload 64 KB, result 1 MiB, checkpoint 4 MiB, pipe message 8 MiB, metric buffer 10 000 (U08-03, U08-45, U08-59, U08-84, U08-20) | ASVS v5.0.0-V2.3 | ST08-10 |
| TH08-11 | TB10 | R | Job cancel or retry actions cannot be attributed | M | L | 08 logs every cancel and retry with `job_id`; spec 09 checks the admin role and writes the `audit` line with the actor (T09-12 (herness.reports.actions.job_control)) | ASVS v5.0.0-V16.3 | ST08-11 |
| TH08-12 | TB8 | S | Another local process binds a model port and answers health or warm-up checks | L | M | Loopback-only URLs enforced per request, with clients only from `herness.core.egress.loopback_http_client` (R-06); bearer key on OpenJev (`secret:OPENJEV_API_KEY`, R-53) and vLLM; fixed ports 8000, 8100, 8200 (R-51); ports bound by spec 10 firewall rules; host compromise is accepted residual R2 | ASVS v5.0.0-V12.1 | ST08-12 |
| TH08-13 | TB8 | T, E | Unsafe deserialisation of supervisor↔child messages | L | H | JSON bytes only (`send_bytes`), pydantic strict validation, 8 MiB cap; spawn arguments are plain strings (U08-84, U08-88) | ASVS v5.0.0-V1.5 | ST08-13 |
| TH08-14 | TB4 | D | Prompt-injected text drives an agent into endless tool loops or repair cycles | M | M | Second loop signal stops the task (`guard_stop`); repairs capped at 2; chain length bounded; spec 06 budgets (U08-35, U08-40) | LLM01, LLM10 | ST08-14 |
| TH08-15 | TB4 | I | Repair prompt echoes attacker-controlled values into logs or traces | L | L | Repair error lines hold JSON pointer paths and validator names only; traces carry paths only; the echoed assistant text goes only back to the same model (U08-35, U08-36) | LLM05, ASVS v5.0.0-V16.2 | ST08-15 |

### 7.3 ASVS 5.0 mapping

| ASVS section | Requirement area | Where |
|--------------|------------------|-------|
| ASVS v5.0.0-V1.2 | Injection prevention (OS command, SQL) | U08-78 argv; parameterised SQL in U08-94–U08-97; allowlisted column names in U08-96 |
| ASVS v5.0.0-V1.5 | Safe deserialization | U08-33 JSON-only fault plans (R-40); U08-84 JSON pipe |
| ASVS v5.0.0-V2.2, V2.3, V2.4 | Input validation, business limits, anti-automation | U08-02, U08-45, U08-59; attempt, elapsed and Retry-After caps |
| ASVS v5.0.0-V12.1 | Communication security configuration | Loopback-only local HTTP; off-network only via spec 10 |
| ASVS v5.0.0-V13.1, V13.3 | Configuration, secret management | Section models with `extra="forbid"`; secrets only by reference (U08-06, U08-79, U08-74) |
| ASVS v5.0.0-V14.2 | Data protection | Redaction of stored errors; egress gating of fallbacks |
| ASVS v5.0.0-V15.3, V15.4 | Defensive coding, safe concurrency | Owner-guarded updates, `BEGIN IMMEDIATE`, single GPU owner, taxonomy-only errors |
| ASVS v5.0.0-V16.2, V16.3, V16.5 | General logging, security events, error handling | §8 event list; faults and job control logged; no sensitive data in errors |

### 7.4 LLM Top 10 (2025) and NIST AI RMF

| Item | Control in 08 | Tests |
|------|---------------|-------|
| LLM01 Prompt injection | Loop-signal stop bounds the impact of injected instructions | ST08-14 |
| LLM02 Sensitive information disclosure | Egress-gated candidates and chat modes; redacted errors and events | ST08-02, ST08-07 |
| LLM05 Improper output handling | `complete_validated` schema validation and repair before use | UT08-33, ST08-15 |
| LLM10 Unbounded consumption | Retry and repair caps, breakers, loop policy | ST08-05, ST08-14 |

AI RMF functions served: **Manage** (fallback chains, breakers, preemption, dead-letter review through `jobs.retry`), **Measure** (retry, fallback, repair, guard-stop counters per run for spec 11 `retries_per_run` and `fallbacks_per_run`).

### 7.5 Secrets used

| Secret | Used by | Resolution | Exposure |
|--------|---------|------------|----------|
| `R.gpu.classes.decider.services.openjev.health.bearer_secret` (default `secret:OPENJEV_API_KEY`, R-53; D08-08 resolved) | U08-79 health and warm-up | `herness.core.secrets.resolve` per request (T10-06 (herness.core.secrets.resolve)) | Authorization header only; never logged |
| vLLM key from the reasoning client's `api_key` reference in `models.yaml` | U08-79 warm-up | same | same |
| `redact.hmac_key.next` | U08-74 | same | only the 8-hex `key_id` leaves the function |

### 7.6 Data classification

| Field | Class |
|-------|-------|
| `job.payload` (references), `job.idem_key`, `job.kind`, `worker.*`, `source_health.source/state/failures/trips` | internal |
| `job.last_error`, `task.last_error`, `source_health.last_error` (redacted) | internal |
| `job.result`, `task.checkpoint` (redacted messages, tool results, pseudonym maps per spec 06/10 rules) | confidential |
| `resilience_event.*` | internal |
| `metric_sample.*` | internal |
| Log lines and trace fields written by 08 | internal |
| Fault plan | internal |

### 7.7 Accepted residual risks

| ID | Risk | Reason | Owner |
|----|------|--------|-------|
| R1 | `multiprocessing` spawn pickles its own bootstrap data for the child | Arguments are plain strings; the pipe between parent and child of the same service account is private | 08 |
| R2 | A process running as `svc-herness` can bind model ports or edit the ops store | Host compromise is outside the threat model; ACLs per spec 10 | 10 |
| R3 | A thread stuck in `call_with_timeout` keeps running until the child process exits | Design 08 §5.2 accepts it; jobs run in child processes | 08 |
| R4 | Duplicate `resilience_event` rows after a crash between action and event write | Events are history, not state | 08 |

## 8. Observability

### 8.1 Log events

All events carry `ts`, `level`, `event`, `component` (`resilience` or `jobs`), the IDs in scope and, for `resilience_event` kinds, `kind`.

| Event | Level | Fields | When |
|-------|-------|--------|------|
| `resilience.call.retry_scheduled` | WARNING | `policy`, `breaker_key`, `attempt`, `error_type`, `wait_s`, `retry_after_s` | U08-28 before a sleep (`retry`) |
| `resilience.chain.fallback_used` | WARNING | `from_profile`, `to_profile`, `reason` | U08-38 (`fallback`) |
| `resilience.chain.exhausted` | ERROR | `model_role`, `tried` | U08-38 |
| `resilience.output.repaired` | WARNING | `model_profile`, `repair_no`, `n_errors` | U08-35 (`repair`) |
| `resilience.loop.guard_stopped` | WARNING | `cause`, `step` | U08-40 (`guard_stop`) |
| `resilience.breaker.opened` / `.half_opened` / `.closed` | WARNING / INFO / INFO | `key`, `failures`, `trips`, `probe_due`, `reason` | U08-24 |
| `resilience.decider.skipped` / `.fallback` | DEBUG / WARNING | `decider`, `reason` | U08-39 |
| `resilience.event.dropped` | WARNING | `kind`, `error_type` | U08-18 |
| `resilience.metrics.flush_failed` / `.dropped` | WARNING | `rows` / `count` | U08-22, U08-20 |
| `resilience.faults.enabled` / `.invalid` / `.kill` / `.no_service_hook` / `.ignored` | WARNING / ERROR / CRITICAL / WARNING / WARNING | `plan`, `rules`, `point`, `env` | U08-33, U08-34 |
| `resilience.timeout.hook_failed` | WARNING | `error_type` | U08-32 |
| `resilience.backend.bound` | DEBUG | `port` | U08-10 |
| `jobs.job.enqueued` | INFO (DEBUG when deduped) | `job_id`, `kind`, `priority`, `created`, `scheduled_for` | U08-46 |
| `jobs.job.rejected` | WARNING | `kind`, `reason` | U08-45 |
| `jobs.job.claimed` | INFO | `job_id`, `kind`, `attempt`, `owner`, `wait_s` | U08-48 |
| `jobs.job.done` / `.yielded` / `.failed` | INFO / INFO / ERROR | `job_id`, `kind`, `attempt`, `duration_s`, `stop_reason`, `error_type` | U08-50 |
| `jobs.job.rescheduled` | WARNING | `job_id`, `error_type`, `scheduled_for` | U08-50 |
| `jobs.job.lease_lost` / `.stalled` / `.canceled` | WARNING / WARNING / INFO | `job_id`, `owner` | U08-50, U08-87 |
| `jobs.job.cancel_requested` / `.retry_requested` | INFO | `job_id`, `result` | U08-51, U08-52 |
| `jobs.lease.expired` | WARNING | `job_id`, `kind`, `outcome` | U08-87 |
| `jobs.handler.crashed` | ERROR | `job_id`, `kind`, `error_type` | U08-56 |
| `jobs.gpu.swapped` / `.swap_failed` | INFO / ERROR | `from`, `to`, `duration_s`, `step`, `error_type` | U08-81 |
| `jobs.gpu.compose_failed` | WARNING | `verb`, `service`, `rc` | U08-78 |
| `jobs.gpu.request_rejected` / `.restore_failed` | WARNING / ERROR | `requested`, `window` / `class` | U08-87, U08-85 |
| `jobs.service.started` / `.stopped` / `.restarted` | INFO / INFO / WARNING | `service`, `duration_s` | U08-81 |
| `jobs.schedule.fired` / `.missed` / `.error` | INFO / WARNING / ERROR | `schedule`, `fire_at`, `job_id`, `error_type` | U08-72 |
| `jobs.chain.broken` / `.skipped` | WARNING / INFO | `schedule`, `fire_at`, `step`, `reason` | U08-73 |
| `jobs.rekey.planned` | INFO | `job_id`, `fire_at`, `key_id` | U08-74 |
| `jobs.tasks.recovered` | INFO | `run_id`, counts | U08-57 |
| `jobs.task.release_skipped` / `.state_conflict` | DEBUG / WARNING | `task_id` | U08-63, U08-60 |
| `jobs.checkpoint.loop_dropped` | WARNING | `task_id`, `bytes` | U08-60 |
| `jobs.window.class_never_allowed` | WARNING | `class` | U08-68 |
| `jobs.chat.no_cloud_client` | WARNING | `depth` | U08-76 |
| `jobs.worker.started` / `.stopped` / `.gpu_lock_held` / `.nothing_to_do` / `.config_invalid` | INFO / INFO / ERROR / INFO / ERROR | `worker_id`, `slots`, `exit_code`, `paths` | U08-87 |
| `jobs.gpu.class_requested` | INFO | `worker_id`, `class` | U08-102 |
| `jobs.supervisor.tick_failed` / `.store_unavailable` | ERROR / CRITICAL | `failed_ticks`, `error_type` | U08-87 |

### 8.2 Metrics (`metric_sample`)

| Name | Kind | Labels | Emitted by |
|------|------|--------|------------|
| `herness_resilience_retries_total` | counter | `policy`, `error_class` | U08-28 |
| `herness_resilience_fallbacks_total` | counter | `from_profile`, `to_profile`, `reason` | U08-38 |
| `herness_resilience_repairs_total` | counter | `client` | U08-35 |
| `herness_resilience_guard_stops_total` | counter | `cause` | U08-40 |
| `herness_resilience_breaker_transitions_total` | counter | `key`, `to_state` | U08-24 |
| `herness_jobs_enqueued_total` | counter | `kind` | U08-46 |
| `herness_jobs_finished_total` | counter | `kind`, `status` (`done`, `yield`, `failed`, `requeued`) | U08-50 |
| `herness_jobs_queue_wait_seconds` | histogram | `kind` | U08-48 |
| `herness_jobs_run_seconds` | histogram | `kind` | U08-50 |
| `herness_jobs_lease_expired_total` | counter | `kind` | U08-87 |
| `herness_jobs_gpu_swap_seconds` | histogram | `from`, `to` | U08-81 |
| `herness_jobs_gpu_swap_failed_total` | counter | `to` | U08-81 |
| `herness_jobs_schedule_fired_total` / `_missed_total` | counter | `schedule` | U08-72 |
| `herness_jobs_supervisor_tick_seconds` | histogram | none | U08-87 |
| `herness_jobs_queue_depth_count` | gauge | `gpu_class` | U08-87 step 7a through U08-103 |
| `herness_jobs_gpu_vram_used_mb` | gauge | none | U08-81 step 7 through U08-103 |

Every row reaches `metric_sample` through the single writer `herness.store.ops.metrics.record_metric_samples` (U08-100, R-12).

Derived views for the dashboard and spec 11 (design 08 §5.14) come from `resilience_event` and these samples: retries and fallbacks per policy or profile per day, breaker opens per key, queue wait and run time p50/p95 per kind, swap duration, dead-letter count.

### 8.3 Trace events

Emitted through spec 05's `Tracer` only when a tracer is passed (design 08 §4.4): `retry` (`target`, `attempt`, `error_type`, `wait_s`, `policy`, `breaker_key`, `retry_after_s`) from U08-28 via `ModelChain`; `repair` (`model_profile`, `repair_no`, `error_paths`) from U08-35; `fallback` (`from_profile`, `to_profile`, `reason`) from U08-38; `guard_stop` (`cause`, `step`) from U08-40. Common fields (`run_id`, `task_id`, `span_id`) are filled by the `Tracer`.

### 8.4 Health

`herness.core.jobs.health()` (U08-92): `down` without a live worker; `degraded` with an open breaker, dead letters in 24 h, faults enabled or GPU unavailable; `ok` otherwise. `status_snapshot()` (U08-91) is the detailed view for `herness status` and the dashboard.

## 9. Configuration

All keys come from `config/resilience.yaml` unless stated. Every key is read through `get_config()`, which is loaded once per process, so a change takes effect after a restart of the worker, CLI or app process. None of these keys is secret; `bearer_secret` holds a secret name only.

| Key path | Type | Default | Validation | Restart | Sensitivity |
|----------|------|---------|------------|---------|-------------|
| `resilience.retry.policies.<name>.*` | `PolicySettings` | design 08 §7 table | U08-06 | yes | internal |
| `resilience.retry.job_backoff.base_s` / `.cap_s` | float | 60 / 3600 | > 0, cap ≥ base | yes | internal |
| `resilience.retry.retry_after_max_s` | float | 86400 | > 0 (U08-17 clamp; D08-27) | yes | internal |
| `resilience.breakers.{source,model,decider}.failure_threshold` | int | 8 / 5 / 5 | ≥ 1 | yes | internal |
| `resilience.breakers.{source,model,decider}.cooldown_s` / `.cooldown_max_s` | float | 300/3600, 60/900, 60/900 | max ≥ base | yes | internal |
| `resilience.breakers.restart_max_per_hour` | int | 1 | ≥ 0 | yes | internal |
| `resilience.fallback.max_repairs` | int | 2 | 0–5 | yes | internal |
| `resilience.fallback.decider_chain.<profile>` | list[str] | local `[laya, openjev, llm]`, hybrid `[laya, openjev, jev, llm]`, premium `[jev, llm]` | non-empty, unique | yes | internal |
| `resilience.loop.checkpoint_min_interval_s` | float | 5 | ≥ 0 | yes | internal |
| `resilience.loop.stop_on_signal_no` | int | 2 | ≥ 1 | yes | internal |
| `resilience.tasks.max_task_attempts` | int | 3 | ≥ 1 | yes | internal |
| `resilience.jobs.lease_s`, `heartbeat_s`, `reaper_interval_s`, `tick_s`, `cancel_grace_s`, `shutdown_grace_s`, `stall_timeout_s`, `stall_timeout_large_s`, `cpu_slots` | numbers | 300, 30, 30, 2, 60, 120, 1800, 7200, 2 | positive; `3 × heartbeat_s < lease_s` | yes | internal |
| `resilience.jobs.exclusive_kinds` | list[JobKind] | `[build_pipeline, distill, maintenance]` | members of `JobKind` | yes | internal |
| `resilience.jobs.max_attempts.<kind>` | int | design 08 §7 | every kind, 1–20 | yes | internal |
| `resilience.gpu.compose_cmd`, `compose_file`, `vram_check_cmd` | list[str] / str | design 08 §7 | non-empty; no NUL | yes | internal |
| `resilience.gpu.vram_free_threshold_mb`, `stop_timeout_s`, `warmup_timeout_s` | int | 2000, 120, 120 | > 0 | yes | internal |
| `resilience.gpu.classes.<class>.services.<svc>.{url, health.path, health.bearer_secret, start_timeout_s, start_on_entry}` | see U08-06 | design 08 §7; URLs `http://127.0.0.1:8000` (vLLM), `:8100` (OpenJev), `:8200` (llama.cpp) per R-51; OpenJev `bearer_secret` `secret:OPENJEV_API_KEY` (R-53) | loopback URL; service in one class; `bearer_secret` matches `^secret:[A-Z][A-Z0-9_]{0,63}$` | yes | internal (secret reference) |
| `schedule.windows[]` | `WindowSpec` | design 08 §7 | U08-67 full-week coverage | yes | internal |
| `schedule.preempt_grace_min` | int | 15 | ≥ 0 | yes | internal |
| `schedule.batch_in_chat_min_priority` | int | 70 | 0–100 | yes | internal |
| `schedule.chat.off_hours` / `.in_hours_unavailable` | enum | `small_model` / `small_model` | `small_model`, `defer`, `cloud` | yes | internal |
| `schedule.rekey.cron` / `.min_notice_h` | str / int | `0 19 * * SAT` / 12 | cron parses; 0–168 | yes | internal |
| `schedule.jobs[]` | `ScheduledJob` | design 08 §7, with `nightly.job.gpu_class = none` (R-43) | U08-07 shape; U08-99 cron parse | yes | internal |
| `schedule.maintenance.catch_up_max` | str | `12h` | `^\d+(m\|h\|d)$` | yes | internal |
| `weights.yaml: business_timezone` (`cfg.weights.business_timezone`) | IANA name | spec 04 | `zoneinfo` loads it | yes | internal |
| `herness.yaml: security.egress.enabled` | bool | profile | spec 10 (file-only) | yes | internal |
| `herness.yaml: paths.data` | path | `data` | spec 10 | yes | internal |
| `herness.yaml: backup.nightly_at` | `HH:MM` | `01:30` | spec 10 | yes | internal |
| `sources.yaml: sources.<s>.{enabled, schedule, reconcile.schedule}` | bool / cron | spec 01 | cron shape in spec 01 settings; full parse by U08-99 through impl 10's owner validator hook (R-03, R-71) | yes | internal |
| `herness.yaml: profile` and `security.data_policy.chat_approved` | str / bool | `local` / `false` | spec 10 (T10-01 (herness.core.settings.SecurityConfig)); read through T10-16 (herness.core.egress.cloud_chat_allowed) (R-38, O08-13) | yes | internal |
| `models.yaml: models.*` (through `ChainRegistry`) | spec 05 | spec 05 | spec 05 | yes | internal |
| env `HERNESS_FAULTS` | path to a `.json` plan | unset | U08-33; honoured only with `HERNESS_ENV=test` (R-40) | yes (read once per process) | internal |
| env `HERNESS_ENV` | str | unset | `test` enables fault plans (R-40); any other value or unset ignores them | yes (read once per process) | internal |
| env `HERNESS_STUB_SERVICES` | JSON object, service name → loopback base URL | unset | read only by the `kill_service:` fault action under `HERNESS_ENV=test` (impl 11 DD11-04) | yes | internal |

Constants (not configurable): `BREAKER_CACHE_S = 5`, `HALF_OPEN_STALE_S = 600`, `METRIC_FLUSH_INTERVAL_S = 10`, `METRIC_BUFFER_MAX = 10000`, `METRIC_GAUGE_KEYS_MAX = 1000`, `CHECKPOINT_MAX_BYTES = 4194304`, `CHECKPOINT_SCHEMA_VERSION = 1`, `GPU_SLOT_KINDS = {build_pipeline}`, `MAX_PIPE_MSG_BYTES = 8388608`, `PAYLOAD_MAX_BYTES = 65536`, `RESULT_MAX_BYTES = 1048576`, `COMPOSE_UP_TIMEOUT_S = 120`, `SWAP_ETA_S = 360`, `SOURCE_SYNC_CATCH_UP = 1 h`, `SOURCE_RECONCILE_CATCH_UP = 12 h`, `SWAP_FAILURE_POSTPONE = 10 min`, supervisor store-failure limit 10 ticks.

## 10. Performance and capacity

| ID | Target (design 08 §8) | Dataset and setup | Hardware | Pass threshold | Marker |
|----|-----------------------|-------------------|----------|----------------|--------|
| BT08-01 | Retry wrapper overhead per call, no retry, breaker cached | `retry_call("tool_store", noop, breaker_key="tool")`, 100 000 calls after warm-up | dev box CPU | mean < 50 µs over `noop` baseline | `unit` + `benchmark` |
| BT08-02 | Enqueue | temp ops store with 10 000 existing jobs; 2 000 `enqueue` calls | dev box SSD | p95 < 10 ms | `integration` + `benchmark` |
| BT08-03 | Claim with 10 000 queued jobs | 10 000 queued over 4 classes and 10 kinds; 1 000 claims | dev box SSD | p95 < 20 ms | `integration` + `benchmark` |
| BT08-04 | Requeue after owner death | FT08-07 timings | dev box | same host ≤ 10 s; other owner ≤ `lease_s` + 30 s | `fault` |
| BT08-05 | Breaker open seen by all processes | two processes; open in A; poll `state()` in B | dev box | ≤ 5 s | `integration` |
| BT08-06 | Swap reasoning → decider (stop, VRAM check) | real containers | target PC | < 2 min | `gpu` |
| BT08-07 | `services.start("openjev")` incl. warm-up | real containers | target PC | < 5 min | `gpu` |
| BT08-08 | Swap decider → reasoning | real containers, 30B model | target PC | < 6 min | `gpu` |
| BT08-09 | `chat_policy(now)` | warm caches, stub health server | dev box | p95 < 5 ms | `integration` + `benchmark` |
| BT08-10 | `herness resume` → first task running | idle worker, class loaded, fake LLM | dev box | < 30 s | `integration` + `slow` |
| BT08-11 | Supervisor idle CPU | worker with no jobs for 10 min | dev box | < 1 % of one core (`psutil.Process.cpu_percent`) | `slow` |

Resource limits the code enforces: payload 64 KB; job result 1 MiB; checkpoint 4 MiB; pipe message 8 MiB; metric buffer 10 000 observations and 1 000 gauge keys; 100 000 samples per `record_metric_samples` call; Retry-After clamp `retry_after_max_s` (86 400 s); event detail 20 keys; repair echo 4 000 chars; error message 2 KB; cron search 1 830 days; one GPU slot and `R.jobs.cpu_slots` CPU slots per worker; one GPU executor thread.

## 11. Test specification

Layout: unit tests in `tests/unit/core/resilience/` and `tests/unit/core/jobs/`, store adapter tests in `tests/unit/store/`, integration in `tests/integration/jobs/`, fault in `tests/fault/`, benchmarks in `tests/bench/`. Common fixtures: `process_state_reset` (calls `reset_process_state`, seeds `rng` with `random.Random(0)`, replaces `sleep`/`asleep` with a fake clock that advances `freezegun` time), `ops_db` (temp SQLite migrated by T02-05 (herness.store.ops.migrate.migrate) and bound with U08-98), `cfg_default` (config from design 08 §7 defaults, business timezone `Europe/London` for DST cases), `fake_chain_registry` (in-memory `ChainRegistry` with the spec 05 default clients), `fake_gpu` (`tests/support/fake_gpu.py`: a fake `ComposeRunner` recording argv and service states, a stub loopback health/warm-up server on ephemeral ports, a fake VRAM reader), `fake_llm` (impl 11 `tests/support/fake_llm.py`: T11-23 (tests.support.fake_llm.FakeLLMServer) over HTTP or T11-23 (tests.support.fake_llm.FakeLLMClient) in process, R-65), `fault_env` (sets `HERNESS_ENV=test` and writes a JSON plan for `HERNESS_FAULTS`; every FT08 test uses it, R-40), `recording_tracer` (collects `emit` calls). Every test names its ID in the docstring.

### 11.1 Unit tests

| ID | Unit / flow | Setup | Action | Expected | Marker |
|----|-------------|-------|--------|----------|--------|
| UT08-01 | U08-01, U08-02, U08-04 | none | build `JobSpec` with priority −1, 101, `None`, extra field, naive `scheduled_for` | −1, 101, extra field and naive time raise `ValidationError`; `None` accepted (R-41); valid spec is frozen; literal sets equal design 08; `JobContext` is importable from `herness.core.jobs` and not from `herness.core.types` (R-02) | unit |
| UT08-02 | U08-03 | none | `JobOutcome(status="done")`; oversize result | `result == {}`; oversize raises | unit |
| UT08-03 | U08-06, U08-07 | `cfg_default` | load | every field equals design 08 §7 with the R-43, R-51 and R-53 defaults (`nightly` class `none`; ports 8000/8100/8200; OpenJev `secret:OPENJEV_API_KEY`); `retry_after_max_s == 86400`; missing policy key rejected; non-loopback service URL rejected; `bearer_secret` `openjev.api_key` rejected by the reference regex | unit |
| UT08-04 | U08-67, U08-99 | windows with a 1-minute gap, an overlap, two `chat` windows | `validate_resilience_config` | issues with path `schedule.windows` name the minute ranges; default windows give no issue | unit |
| UT08-05 | U08-07, U08-99 | cron with 4 fields, cron `60 * * * *`, source schedule `*/0 * * * *`, `then` kind `foo`, preload not in classes, window class not in `gpu.classes` | load, then `validate_resilience_config` | 4-field cron, kind, preload and class rejected by the model with their paths; the two unparsable crons reported by U08-99 with paths `schedule.jobs[0].cron` and `sources.<name>.schedule` | unit |
| UT08-06 | U08-11, U08-12 | `cfg_default` | `policy(n)` for every name; `policy("bogus")` | values equal the §5.2 table; `gpu_health` constant; bogus → `ConfigError` | unit |
| UT08-07 | U08-13 | client infos (off-network; `large`; `reasoning`; `null`) | select | `llm_cloud`, `llm_large`, `llm_local`, `llm_local` | unit |
| UT08-08 | U08-14 | policy `llm_cloud`, fake clock | `RateLimited(retry_after=7)` then success | slept ≥ 7 s and ≤ 120 s | unit |
| UT08-09 | U08-28 | policy `source_http_page` (cap 300) | fn raises `RateLimited(retry_after=301)` | raised after one call, no sleep | unit |
| UT08-10 | U08-16 | parametrised foreign exceptions of design 08 §5.2 (httpx errors, statuses 429/401/403/500/502/503/504/529/418, fake `openai`/`anthropic`/`duckdb` modules in `sys.modules`, sqlite locked/busy/other, `TimeoutError`, `ValueError`) × families | classify | class per U08-16 rules; `HernessError` returned unchanged | unit |
| UT08-11 | U08-16 | 503 response with a 5 000-char body containing an e-mail and `api_key=synthetic_key_abc` (R-67) | classify | message ≤ 2 KB, body part ≤ 500 chars before redaction, no e-mail or key text | unit |
| UT08-12 | U08-23 | every row of design 08 §5.3 plus the no-op rows | transition | state, counters, `opened_at`, emitted kinds as specified | unit |
| UT08-13 | U08-23 | trips 1..8, cooldown 60, max 900 | `probe_due` | 60, 120, 240, 480, 900, 900… s after `opened_at` | unit |
| UT08-14 | U08-24 | closed breaker | `force_open(AuthError())` | `open`, `trips == 1`, event detail `reason = auth` | unit |
| UT08-15 | U08-24 | closed breaker | `record_failure` with `QueryError`, `RateLimited`, `ModelUnavailable` | only `ModelUnavailable` increments failures | unit |
| UT08-16 | U08-24 | `ops_db`, spy backend | 100 `allow()` within 5 s; one transition | one `health_get` read; transition written at once | unit |
| UT08-17 | U08-26 | open breaker, probe not due | `guard(key)` | `CircuitOpen` with `.key` and `.retry_at == probe_due` | unit |
| UT08-18 | U08-25 | keys `jira`, `model:local-30b`, `monitoring:datadog`, `bad key`, `Model:x` | `breaker(k)` | first three accepted (same instance on repeat); others `ConfigError` | unit |
| UT08-19 | U08-24, U08-94 | row `half_open` updated 601 s ago | `allow()` | `True` (stale probe re-claimed); at 599 s `False` | unit |
| UT08-20 | U08-28 | fn fails 3× `ModelUnavailable` then succeeds; policy `llm_local` | `retry_call` | 4 calls, value returned; with 4 failures the 4th error is raised | unit |
| UT08-21 | U08-28 | fake clock, each attempt advances 400 s, `llm_local` (900 s) | call | stops after the attempt that crosses 900 s | unit |
| UT08-22 | U08-28 | fn raises `CircuitOpen` | call | one call, raised | unit |
| UT08-23 | U08-28 | fn raises `httpx.ConnectError` with policy `source_http_page` | call | retried as `SourceUnavailable`; final error has `__cause__` | unit |
| UT08-24 | U08-29, U08-30 | decorated sync and async functions | call both | both retried; `functools.wraps` preserved; async uses `asleep` | unit |
| UT08-25 | U08-18, U08-28 | `ops_db`, `recording_tracer` via `ModelChain` path; plain `retry_call` path | one retry each | one `retry` row each with §4.4 fields; metric counted; trace only on the tracer path | unit |
| UT08-26 | U08-28 | breaker key, spy breaker | 2 failures + success | `guard` called 3×, `record_failure` 2×, `record_success` 1× | unit |
| UT08-27 | U08-31 | spy on `retry_call` | `retry_page(fn, source="jira")` | policy `source_http_page`, breaker `jira` | unit |
| UT08-28 | U08-32 | fn returns; fn sleeps 2 s with timeout 0.1 s; fn raises `KeyError` | call | value; `ModelUnavailable` + `on_timeout` called once; `KeyError` re-raised | unit |
| UT08-29 | U08-18, U08-10 | `ops_db`; unbound state | record `retry` with extra key, nested dict, secret-looking string; unknown kind; call while unbound | extra and nested dropped, string redacted and ≤ 200; unknown → `ConfigError`; unbound → `ConfigError` | unit |
| UT08-30 | U08-18 | backend whose `insert_event` raises `StoreBusy` | record | no exception; `resilience.event.dropped` logged | unit |
| UT08-31 | U08-19, U08-20 | none | bad names (no `herness_`, wrong unit, component mismatch), 7 labels, label value with a space | each `ConfigError` | unit |
| UT08-32 | U08-19–U08-22 | `ops_db`, fake clock | 1 000 counter increments on 2 label sets, 5 histogram values, `timed` block, 10 001 histogram values, flush | 2 counter rows with summed values; histogram rows raw; overflow dropped and logged; `timed` records elapsed | unit |
| UT08-33 | U08-35 | scripted client: 2 malformed replies then valid | `complete_validated` | valid response; 3 calls; 2 `repair` events with `repair_no` 1, 2 | unit |
| UT08-34 | U08-35 | 3 malformed replies | call | `OutputValidationError` after 3 calls | unit |
| UT08-35 | U08-36 | reply text 5 000 chars, 25 errors | build | assistant echo 4 000 chars; 20 error lines; schema canonical JSON; `temperature == 0.0` | unit |
| UT08-36 | U08-35 | pydantic model; reply with field value `"synthetic-secret-123"` (R-67) of wrong type | validate | error lines and `error_paths` contain paths and messages, not the value | unit |
| UT08-37 | U08-37 | chain `[claude-opus, local-30b, local-large-offload, local-small-cpu]`; egress off; loaded `reasoning`; `model:local-small-cpu` open, probe not due | `candidates()` | `[local-30b]`; with `large` loaded `[local-large-offload]`; with `swapping` `[]` then only CPU clients when the breaker closes | unit |
| UT08-38 | U08-38 | candidate 1 raises `ModelRefused`, then `EgressBlocked` in another case | `acomplete` | one call on candidate 1 (no retry), answer from candidate 2, `fallback` reason `refusal` / `egress_blocked` | unit |
| UT08-39 | U08-38 | candidate 1 always malformed; candidate 2 valid | `acomplete(schema=M)` | 3 calls on 1, then candidate 2 receives the original messages (no repair history) | unit |
| UT08-40 | U08-38 | candidate 1 raises `BudgetExceeded` / `QueryError` / `ToolInputError` | call | raised at once; candidate 2 never called | unit |
| UT08-41 | U08-38 | all candidates `ModelUnavailable`; empty chain | call | last error raised and `resilience.chain.exhausted`; empty → `ModelUnavailable` | unit |
| UT08-42 | U08-38 | candidate 1 raises `AuthError` in run `run_A` | two calls in `run_A`, one in `run_B` | `fallback` reason `auth`; second `run_A` call skips candidate 1; `run_B` still tries it | unit |
| UT08-43 | U08-39 | deciders: `laya` raises `RuntimeError`, `openjev` breaker open, `llm` returns outputs; gpu reasoning loaded | decide | `laya` classified `ModelUnavailable` and retried 3×, `openjev` skipped (unavailable in reasoning), `llm` decides; `([outputs], [])` | unit |
| UT08-44 | U08-39 | loaded `decider`, `openjev` unhealthy, order `[openjev, llm]` | decide | `([], items)` | unit |
| UT08-45 | U08-40 | `LoopState(loop_signals=1, nudges=3)` and `(loop_signals=2, nudges=0)`; tracer | policy | first: `nudge` with no event (nudges ignored, R-66); second: `stop` with one `guard_stop` row carrying the tracer's `run_id`/`task_id` and a trace event (`cause`, `step`) | unit |
| UT08-46 | U08-34 | env unset; spy on `open` | 1 000 calls | no file access, no effect | unit |
| UT08-47 | U08-33 | plans: unknown point, unknown action, `nth`+`count`, `p` without seed, 70 KB file, symlink, a valid plan saved with suffix `.yaml` (R-40) | load | each `ConfigError` | unit |
| UT08-48 | U08-34 | rules `nth: 3`; `count: 2`; `p: 0.2, seed: 7`; label filter `model: local-30b` | 10 calls each | fires on call 3 only; calls 1–2; the same call indices on two runs; only matching labels fire | unit |
| UT08-49 | U08-34 | one rule per action | call | error classes per U08-34 table; `delay` sleeps on the fake clock; `kill_service` calls the hook | unit |
| UT08-50 | U08-34 | valid plan | first call | WARNING `resilience.faults.enabled`; `faults_enabled` true | unit |
| UT08-51 | U08-44 | payloads with reordered keys | hash | equal keys; prefix `kind:`; 16 hex | unit |
| UT08-52 | U08-46, U08-47 | `ops_db` | enqueue twice with the same idem key; finish the first; enqueue again | same id, `created` false; after finish a new id | unit |
| UT08-53 | U08-46, U08-95 | `ops_db`; a `done` job with `sched:nightly:<F>` | submit with `sched_check` | existing id, no new row | unit |
| UT08-54 | U08-72 | running `sync` for `jira` | scheduler fires `sync.jira` | no second job; `sched_fired` records the fire through payload | unit |
| UT08-55 | U08-45 | payload 70 KB; payload containing a known secret value; `Authorization: Bearer synthetic_token_x` (R-67); depth 9; key `a b` | validate | each `SchemaViolation`, reason without the value | unit |
| UT08-56 | U08-48 | jobs with priorities 10/90/50, one future `scheduled_for`, classes mixed | claim `allowed=["none"]` repeatedly | order 90, 50, 10 among due `none` jobs; future job untouched | unit |
| UT08-57 | U08-48 | running `build_pipeline`; queued `distill` and `sync` | claim all classes | `sync` claimed; `distill` not while the build runs | unit |
| UT08-58 | U08-49 | table: each error class × attempts below and at max × kinds `sync` and `review` | decide | actions and times per U08-49 | unit |
| UT08-59 | U08-50 | running job; outcomes `done` with `state` key, `yield`, error; wrong owner | finish | `state` dropped; attempts −1 on yield; wrong owner → `lease_lost`, row unchanged | unit |
| UT08-60 | U08-51 | queued, running, done jobs, unknown id | cancel | `canceled`, `cancel_requested`, `not_active`, `not_active` | unit |
| UT08-61 | U08-52 | failed job; done job; failed job whose idem key is active elsewhere | retry | queued with attempts 0; `JobStateError` twice | unit |
| UT08-62 | U08-95, U08-87 | expired running jobs with attempts 1/3 and 3/3 | reap | first `queued` with `LeaseExpired`; second `failed`; two `lease_expired` events | unit |
| UT08-63 | U08-53, U08-42 | `ops_db` | `get("job_x")`, `list_jobs(limit=0)`, `list_jobs(status="bogus")` | `JobStateError`, `ConfigError`, `ConfigError`; valid rows parse to `JobRow` | unit |
| UT08-64 | U08-55, U08-56 | handlers raising `ValueError`, returning `None`, raising `SourceUnavailable` | register twice; run | duplicate → `ConfigError`; `FatalError` naming `ValueError`; `FatalError` for `None`; `SourceUnavailable` returned | unit |
| UT08-65 | U08-54, U08-96 | worker rows with heartbeat 89 s and 91 s old (`heartbeat_s` 30) | `worker_alive()` | true, false | unit |
| UT08-66 | U08-57 | tasks running(2), done(1), failed(1, below cap), failed(3, at cap), dead(1) | recover without and with `retry_dead` | running → pending, attempts unchanged; done untouched; one failed reset; dead reset only with `retry_dead` (attempts 0) | unit |
| UT08-67 | U08-58 | pending and running tasks | `claim_task` | true and attempts +1; false for running | unit |
| UT08-68 | U08-59, U08-60 | envelope with `scratchpad`; save key `loop` 5 MB; save key `state` 5 MB; envelope with `schema_version` 2; envelope with an extra top-level key | save | first: `scratchpad` kept and `loop` dropped with warning; 5 MB state → `SchemaViolation`; version 2 and extra key → `SchemaViolation` | unit |
| UT08-69 | U08-60, U08-61 | `writes` inserting a finding then raising; task not running | save / complete | finding row absent after rollback; not running → `JobStateError` | unit |
| UT08-70 | U08-62 | attempts 1 and 3 with max 3; errors `ModelUnavailable`, `OutputValidationError`, `BudgetExceeded`, `CircuitOpen` | fail | pending / dead per U08-62 | unit |
| UT08-71 | U08-63 | running task attempts 2; done task | release | pending with attempts 1; done unchanged, DEBUG log | unit |
| UT08-72 | U08-64 | table of valid (`*/30 * * * *`, `0 3 * * SUN`, `0 19 * * 1-5`, `0,30 8-18 * * MON,WED`) and invalid (`60 * * * *`, `* * *`, `*/0 * * * *`, `5-1 * * * *`) | parse | parsed sets / `ConfigError` naming the field | unit |
| UT08-73 | U08-64 | `0 0 13 * FRI` (OR rule); fixed instants | next and latest | Vixie semantics; `next_after(t) > t`; `latest ≤ t` | unit |
| UT08-74 | U08-64, U08-65 | `Europe/London` 2026-03-29 (gap 01:00–02:00) and 2026-10-25 (fold); cron `30 1 * * *` | next | gap day fires 02:00 local; fold day fires once, first occurrence | unit |
| UT08-75 | U08-66 | default windows; instants each hour across one week and at 23:59 Sunday | `window_at` | window names per design 08 §5.10 table | unit |
| UT08-76 | U08-69 | decider job loaded 05:00 Tue at 07:01; large loaded by in-job switch at 22:00 Sun then 06:30 Mon; reasoning at 08:00 | deadline | 07:00 (06:00 + 60 overrun); 07:00 Mon; `None` | unit |
| UT08-77 | U08-68 | Tuesday 10:00, class `decider` | next allowed | 19:00 same day | unit |
| UT08-78 | U08-70 | table of loaded × window × claimable (including key `none` for `GPU_SLOT_KINDS` jobs) × requested (including `large` and `none`) | decide | per U08-70 steps; `claimable["none"] > 0` gives `keep` without a swap | unit |
| UT08-79 | U08-72, U08-43 | `nightly` with `catch_up_max` 6h; worker down 18:00–20:00 and 18:00–02:00 | run scheduler | enqueued once with `fire_at` 19:00 and priority 60; second case `schedule_missed` once | unit |
| UT08-80 | U08-73 | nightly on Sunday and Tuesday; step 1 `done`, step 2 `failed` | advance | Sunday: both review steps `chain_skipped` (`skip_on`), eval `disabled`; Tuesday: step 2 enqueued after step 1; failure → one `chain_broken` | unit |
| UT08-81 | U08-74 | fake secret; now Friday 10:00 and Saturday 10:00 | `schedule_rekey` | Saturday 19:00 same week; next Saturday 19:00 (< 12 h notice); idem `rekey:<8 hex>`; `rekey_planned` | unit |
| UT08-82 | U08-71 | sources `jira` (schedule, reconcile), `snow` disabled; `backup.nightly_at` 01:30 | collect | entries `sync.jira`, `reconcile.jira`, `maintenance` (`30 1 * * *`, then purge), config jobs | unit |
| UT08-83 | U08-72 | one entry whose submit raises `SchemaViolation` | run | `jobs.schedule.error` logged; other entries fire | unit |
| UT08-84 | U08-75 | table: windows {chat, reviews} × loaded {reasoning, decider, swapping} × vLLM healthy {t, f} × chat breaker {closed, open} × off-hours breaker {closed, open} × egress {off, on} × profile {local, hybrid} × `chat` approval {absent, present} × config modes {small_model, defer, cloud} | policy | expected `ChatMode` per the design pseudocode; `cloud` never with egress off, and never in `hybrid` without the approval (R-38) | unit |
| UT08-85 | U08-76 | fake chains | each mode × depth | first local / chat_off_hours / first off-network / `None` | unit |
| UT08-86 | U08-77 | Tue 19:30; Tue 03:00; swap to reasoning in progress | ETA | 21:00 Tue (reviews allows reasoning); 06:00; now + 360 s | unit |
| UT08-87 | U08-78 | `fake_gpu` capturing argv | up/stop/kill/ps; service `"x; rm -rf"` | exact argv lists, `shell=False`; bad name → `ConfigError` | unit |
| UT08-88 | U08-78 | ps output as array and as JSON lines; garbage | parse | service states; garbage → `ModelUnavailable` | unit |
| UT08-89 | U08-79 | stub server; spy on `herness.core.egress.loopback_http_client`; URL `http://10.0.0.1:8000`; stub answering 302 | healthy / warm-up | every client comes from the spy (R-06); bearer header sent, not logged; non-loopback → `ConfigError`; 302 → unhealthy; warm-up bodies match the table | unit |
| UT08-90 | U08-80 | fake VRAM readings 20 000, 1 500; never below | wait | returns on second poll; timeout → `ModelUnavailable("vram_not_freed")` | unit |
| UT08-91 | U08-81 | `fake_gpu`, loaded decider | swap to reasoning | stop openjev, VRAM, up vllm, health, warm-up; worker `swapping` then `reasoning`; `gpu_swap`; `model:local-30b` reset | unit |
| UT08-92 | U08-81 | warm-up fails | swap | vllm stopped; class `none`; breaker failure; `gpu_swap_failed`; `ModelUnavailable` | unit |
| UT08-93 | U08-81 | loaded decider | `service_start("vllm-reasoning")`; `service_start("openjev")` | `ConfigError`; openjev started with VRAM, health, warm-up, `service_start` | unit |
| UT08-94 | U08-81 | fake compose with vllm and openjev running | detect | both stopped; class `none` | unit |
| UT08-95 | U08-82 | two `GpuLock` in two processes | enter | second `ConfigError`; after first exits, second succeeds | unit |
| UT08-96 | U08-81 | one `service_restart` event 30 min ago | `restart_service` | `False`; 61 min ago → `True` | unit |
| UT08-97 | U08-84 | valid messages, 9 MB message, unknown type, extra field | encode/decode | round trip; others `SchemaViolation` | unit |
| UT08-98 | U08-85 | pipe pair, fake parent | stop, heartbeats × 5 in 1 s, 5 MB state, GPU call on CPU slot | `should_yield` true with reason; one heartbeat sent; `SchemaViolation`; `ConfigError` | unit |
| UT08-99 | U08-85 | fake parent replying `previous_class = decider` | `with gpu_scope("large")` normal and raising | restore request for `decider` in both cases; original exception propagates | unit |
| UT08-100 | U08-91 | `ops_db` seeded with workers, jobs, events, breakers | snapshot | every key present; counts equal the seeded rows | unit |
| UT08-101 | U08-92 | no worker; worker + open breaker; clean | health | `down`, `degraded`, `ok` | unit |
| UT08-102 | U08-93 | runs `done`, `running`, `chat` | resume | `job_id None`; review job with `resume:<run_id>`, priority 80; `JobStateError` | unit |
| UT08-103 | §2 layering | repository | run `lint-imports` | contracts pass: `herness.core` imports no `herness.store`/`harness`; `herness.core.types.jobs` imports only pydantic, stdlib, `herness.core.errors`, `herness.core.ids` and `herness.core.types.harness`; `herness.core.resilience.settings` imports only stdlib, pydantic, `herness.core.types` and `herness.core.errors` (R-03); no module under `herness/core/jobs/` constructs an `httpx` client (R-06) | unit |
| UT08-104 | U08-27 | registered probe fns (succeeding, failing), open breakers due and not due | `run_due_probes` | due+success → closed; due+failure → open with trips+1; not due untouched; no fn → skipped | unit |
| UT08-105 | U08-34 | valid JSON plan in `HERNESS_FAULTS`; `HERNESS_ENV` unset, then `prod`, then `test`; spy on `open` | 10 `fault_point` calls per case | unset and `prod`: no file access, no fault, one `resilience.faults.ignored` WARNING per process; `test`: plan loaded and applied (R-40) | unit |
| UT08-106 | U08-43, U08-46, U08-47 | `ops_db` | `enqueue` of each `JobKind` with `priority=None`, and one with `priority=10` | stored priority equals `DEFAULT_PRIORITY[kind]`; explicit 10 kept (R-41) | unit |
| UT08-107 | U08-48, U08-95 | `ops_db`; queued `build_pipeline` class `none`, `sync` class `none`, `review` class `reasoning` | claim with owners `h:1:cpu0`, `h:1:gpu` (allowed `[reasoning, none]`), `h:1:cli` | CPU owner gets only `sync`; GPU owner gets `build_pipeline` or `review` by priority, never `sync`; CLI owner can claim any of them by `job_id` (R-43) | unit |
| UT08-108 | U08-102, U08-96 | `ops_db`; no worker; then an alive GPU worker; then a stale one | `request_gpu_class("large")`, `request_gpu_class("none")` | `no_worker` and no write; `requested` with `requested_class` set and INFO log; stale worker → `no_worker` | unit |
| UT08-109 | U08-59, U08-60, U08-97 | `ops_db`; running task | save `state` {a}, then `loop` {b}, then `scratchpad` {c}, then `state` {d}; two threads saving `loop` and `scratchpad` at once | envelope `{schema_version: 1, loop: b, state: d, scratchpad: c}`; no save erased another key; concurrent saves keep both keys (R-21) | unit |
| UT08-110 | U08-100, U08-101 | `ops_db` | `record_metric_samples` with 1 201 samples (counter, gauge, histogram); again inside a caller's `run_write` with `conn`; a sample with 1 100-byte labels; `MetricSample` with a free-text label value; `purge_metric_samples(now − 90 d)` | 1 201 rows in 3 transactions; `conn` rows commit with the caller; labels → `ConfigError`; free text → `ValidationError`; purge removes only older rows | unit |
| UT08-111 | U08-103, U08-22 | `ops_db`, fake clock | `record_gauge("herness_jobs_queue_depth_count", 5, component="jobs", labels={"gpu_class": "none"})`, then 3 for the same key, then 1 001 distinct keys; `flush_metrics()` | one `gauge` row with value 3 for the first key; negative value accepted; `_total` name and NaN → `ConfigError`; key 1 001 dropped and counted | unit |
| UT08-112 | U08-17 | fixed `now`; headers `Retry-After: 120`, `Retry-After: 1.5`, `Retry-After: <HTTP date now + 60 s>`, `<date now − 60 s>`, `<date with no zone>`, `Retry-After: soon`, `Retry-After: 999999`, `X-RateLimit-Reset` in epoch seconds and in epoch milliseconds; `max_s = 3600` | parse | 120; `None` (not RFC 9110 delay-seconds); 60; 0; `None`; `None`; 3600 (clamped); both epoch forms give the delay to `now` | unit |

### 11.2 Property tests (hypothesis)

| ID | Unit | Property | Marker |
|----|------|----------|--------|
| PT08-01 | U08-14 | For random attempt 1–20 and policy values, 10 000 samples lie in `[0, min(cap, base·2^(a−1))]`; with `retry_after ≤ cap` result in `[retry_after, cap]` | unit |
| PT08-02 | U08-15 | Job backoff in `[0, min(3600, 60·2^(a−1))]` | unit |
| PT08-03 | U08-17 | Any header strings and any `max_s` > 0: never raises; result `None` or within `[0, max_s]`; integer delay-seconds below `max_s` parsed exactly | unit |
| PT08-04 | U08-64 | Random valid crons and instants: `next_after(t) > t`, `latest_at_or_before(next_after(t)) == next_after(t)`, no fire between `t` and `next_after(t)` at minute resolution | unit |
| PT08-05 | U08-66, U08-67 | For configs passing validation and random instants, exactly one window contains the instant (outside DST transition hours) | unit |
| PT08-06 | U08-44 | Key order permutations of a payload give one idem key; distinct payloads give distinct keys in samples | unit |

### 11.3 Integration tests

| ID | Flow | Setup | Action | Expected | Marker |
|----|------|-------|--------|----------|--------|
| IT08-01 | F08-01 | `ops_db`; two processes with open breaker due | both call `allow()` 100× concurrently | exactly one `True` | integration |
| IT08-02 | F08-03, U08-48 | 5 000 jobs across classes and kinds incl. exclusive kinds | 8 threads × 1 000 claims, completing each | no job claimed twice; per-thread claims in non-increasing priority among due jobs; never two exclusive kinds running | integration |
| IT08-03 | F08-12 | rekey planned for Saturday; fake clock Saturday 18:59–23:00; worker with fake handlers | run | rekey job claimed before `build_pipeline` (both by the GPU slot; the build has class `none`, R-43); build payload `rekey_night`; the build's `gpu_scope("decider")` swaps to decider; standard reviews `chain_skipped` reason `rekey`; no preemption at 21:00 | integration |
| IT08-04 | F08-04, F08-05 | worker `--concurrency 1`, fake handlers returning `done` | enqueue 3 `none` jobs | all `done` in child processes; events and metrics written | integration |
| IT08-05 | F08-04 d | handler that loops until `should_yield`; cancel while running; handler ignoring stop | cancel | first yields → `canceled` finalised; second terminated after `cancel_grace_s` (set 2 s) | integration |
| IT08-06 | F08-04 e | handler sleeping without heartbeat; `stall_timeout_s` 3 s | run | child killed; job requeued with `ModelUnavailable("stalled")` | integration |
| IT08-07 | F08-05 | handler calling `os._exit(9)` | run | `child_crash`; requeued; attempt 2 succeeds | integration |
| IT08-08 | F08-09 | long handler checkpointing via `save_state` | send SIGINT (Windows `CTRL_BREAK_EVENT`) once; separately twice | first: job `yield`, `queued`, attempts unchanged, state kept; second: terminated, requeued at next start | integration |
| IT08-09 | F08-10 | `running` rows owned by `<host>:<dead pid>:cpu0` | start worker | requeued within 10 s; attempts unchanged by recovery | integration |
| IT08-10 | F08-04 i | backend raising `StoreBusy` for every tick | run 12 ticks | children untouched for 9; after 10 graceful shutdown, exit 1 (R-46) | integration |
| IT08-11 | U08-89 | `--once` with 2 queued jobs; with none | run | one job done, exit 0; none → `nothing_to_do`, exit 0 | integration |
| IT08-12 | F08-14 | no worker; queued `reasoning` job; `fake_gpu` | `run_inline`; second case SIGINT mid-handler | `done` with swap performed; SIGINT → `yield`, lock released | integration |
| IT08-13 | U08-87 g | chat window; queued review priority 40 and 75, chat priority 75 | tick | chat and priority-75 review claimable; priority-40 review stays queued | integration |

### 11.4 Fault tests (design 08 §10 F1–F12)

| ID | Design | Injection | Acceptance | Marker |
|----|--------|-----------|------------|--------|
| FT08-01 | F1 | Stub LLM `die` mid-review | retries, `model:*` breaker opens, fallback or tasks pend; run `done`; no `done` task has `attempts > 1` | fault |
| FT08-02 | F2 | `llm.output malformed_json count=2` / `count=3` | 2 repairs and success / 1 fallback | fault |
| FT08-03 | F3 | `sql.query timeout p=0.2` | `QueryError` results with hints; run completes | fault |
| FT08-04 | F4 | `http.page http_429 retry_after=7 count=3` | 3 `retry` events with `retry_after_s=7`; watermark advanced once | fault |
| FT08-05 | F5 | 10 consecutive 503s on one source | breaker `open`; sync `done` with `skipped_open_circuit`; `half_open` after cooldown; `closed` after a good probe | fault |
| FT08-06 | F6 | `swarm.after_finding_write kill`, then `herness resume` | run finishes; finding count equals a clean run; no duplicate findings or evidence | fault |
| FT08-07 | F7 | `job.after_claim kill`, worker restart | requeued ≤ 10 s and completed; without restart, requeued ≤ `lease_s` + 30 s | fault |
| FT08-08 | F8 | `pipeline.before_promote kill` | `CURRENT` unchanged; rerun promotes | fault, gpu (target PC) |
| FT08-09 | F9 | `gpu.after_stop kill`, worker restart | detection leaves exactly one class or none; never two classes running | fault, gpu (target PC) |
| FT08-10 | F10 | 08:00 boundary during a decider job | yields within `preempt_grace_min`, attempts unchanged, finished batches not redone at 19:00 | fault, gpu (target PC) |
| FT08-11 | F11 | `sqlite.write error:StoreBusy count=4` | writes succeed on retry; no lost job state | fault |
| FT08-12 | F12 | `build_pipeline` (class `none`, R-43) entering `ctx.gpu_scope("decider")`, calling `ctx.services.start("openjev")`, then `ctx.require_gpu_class("reasoning")` on `fake_gpu` | job claimed by the GPU slot; `gpu_class_loaded` none → decider → reasoning; never two services up; `gpu_swap` events | fault |

Phase 3 acceptance: FT08-01–FT08-07, FT08-11, FT08-12 in CI (CPU, stubs, `HERNESS_ENV=test`); FT08-08–FT08-10 on the target PC; `status_snapshot()` counters equal the suite's `resilience_event` rows.

### 11.5 Security tests

| ID | Threat | Attack | Expected | Marker |
|----|--------|--------|----------|--------|
| ST08-01 | TH08-01 | Service names `openjev;calc`, `$(id)`, `../x` in config and in `gpu_request` | config rejects; request `ConfigError`; no subprocess started | unit |
| ST08-02 | TH08-02 | Errors whose text holds a known secret, an e-mail, a bearer header, 10 KB of ticket text flow through `classify`, `finish_job`, `record_event`, breaker `last_error`, logs | none of the planted values in `job.last_error`, `resilience_event.detail`, `source_health.last_error`, captured logs | unit |
| ST08-03 | TH08-03 | Enqueue payloads with a secret value, `password=synthetic_pw_123` (R-67), 65 537 bytes | `SchemaViolation`; no row | unit |
| ST08-04 | TH08-04 | Stub returns `Retry-After: 86400` and an HTTP date a year ahead | call re-raises at once; job requeued at `now + 86400 s` without sleeping in both cases (the year-ahead date is clamped by `retry_after_max_s`) | unit |
| ST08-05 | TH08-05 | Endpoint always 503; 20 concurrent callers for 10 min (fake clock) | calls stop after the threshold; while open no calls; one probe per cooldown | integration |
| ST08-06 | TH08-06 | `HERNESS_ENV=test` with `HERNESS_FAULTS` pointing to a symlink, a 1 MB file, a `.yaml` file with `!!python/object/apply`; and a valid plan with `HERNESS_ENV` unset | `ConfigError` for the first three; nothing executed; the unset-env case ignores the plan with `resilience.faults.ignored`; a valid plan under `test` shows `faults_enabled` in status and `degraded` health | unit |
| ST08-07 | TH08-07 | Profile `local`; chain starts with `claude-opus`; chat mode config `cloud` | `claude-opus` never called; `chat_policy` returns `small_model` | unit |
| ST08-08 | TH08-08 | Owner A's lease reaped and reclaimed by B; A completes and saves state | A's writes change 0 rows; B's result kept | unit |
| ST08-09 | TH08-09 | Start a second GPU worker; `request_gpu_class("large")` (the `deploy up large` path) while a worker runs in the chat window | second exits 1 (R-46); request rejected and logged | integration |
| ST08-10 | TH08-10 | Oversized result, checkpoint, pipe message, 20 000 metric observations | each rejected or capped as specified; memory bounded | unit |
| ST08-11 | TH08-11 | Cancel and retry jobs | `jobs.job.cancel_requested` / `retry_requested` logged with `job_id` | unit |
| ST08-12 | TH08-12 | Service URL `http://attacker.example:8000` in config; redirect from loopback to external host | config rejected; redirect not followed (unhealthy) | unit |
| ST08-13 | TH08-13 | Child sends pickled bytes and a JSON message with extra fields | `SchemaViolation`; supervisor treats the child as crashed; nothing unpickled | unit |
| ST08-14 | TH08-14 | Fake LLM repeats the same tool call forever (spec 05 loop with `HarnessHooks` stub) | task stops on the second signal with `guard_stop`; ≤ 3 model calls per structured output | integration |
| ST08-15 | TH08-15 | Model output value `"<script>ignore previous</script>"` failing a type check | repair lines and trace `error_paths` do not contain it | unit |

## 12. Task cards

All cards are Phase 3. "Acceptance" always also includes: `ruff check` and `ruff format --check` clean, `mypy --strict herness/` 0 errors, `lint-imports` passes, the card's tests pass with their IDs, and no new suppression other than those in §2.

### T08-01 Shared types and error class

| Field | Content |
|-------|---------|
| Goal | The `herness.core.types.jobs` submodule (08 aliases, `JobSpec`, `JobOutcome`, `MetricSample`) is re-exported from `herness.core.types` and registered in `TYPE_OWNERS`, and `JobStateError` exists (R-01, R-19). |
| Depends on | T00-08 (herness.core.types) (package skeleton, `__init__` and `_ownership`), T00-03 (herness.core.errors) (taxonomy) |
| Units | U08-01, U08-02, U08-03, U08-05, U08-101 |
| Files | `herness/core/types/jobs.py`, `herness/core/types/__init__.py` (08 import line), `herness/core/types/_ownership.py` (08 `TYPE_OWNERS` entries), `herness/core/errors.py` (08 section) |
| Tests | UT08-01, UT08-02 |
| Threats | TH08-10 (result cap) |
| Acceptance checks | `pytest -k "UT08-01 or UT08-02"` passes; the impl 00 ownership check passes with the 08 names; `from herness.core.types import JobSpec, MetricSample` works and `herness.core.types` exports no `JobContext` |
| Blocked by | none |
| Size | S |

### T08-26 Configuration section models

| Field | Content |
|-------|---------|
| Goal | `ResilienceSection`, `ScheduleSection` and the root `ResilienceConfig` exist in the single settings module `herness.core.resilience.settings` with the R-43, R-51 and R-53 defaults (R-03). |
| Depends on | T08-01, T10-03 (herness.core.config) (settings convention and the `HernessConfig.resilience` field) |
| Units | U08-06, U08-07 |
| Files | `herness/core/resilience/settings.py`, `herness/core/resilience/__init__.py` (lazy map skeleton) |
| Tests | UT08-03 |
| Threats | TH08-01, TH08-12 (allowlisted service names, loopback URLs) |
| Acceptance checks | `pytest -k "UT08-03"` passes; loading the design 08 §7 YAML into `ResilienceConfig` succeeds; importing `herness.core.resilience.settings` imports no herness module other than `herness.core.types` and `herness.core.errors` (R-03; UT08-103 once it exists) |
| Blocked by | none |
| Size | M |

### T08-02 Cron parser and resilience config validator

| Field | Content |
|-------|---------|
| Goal | `CronExpr`, `resolve_local`, `validate_windows` and `validate_resilience_config` exist, and the composition roots can register the validator with `register_owner_validator` (R-71). |
| Depends on | T08-26, T10-03 (herness.core.config.ConfigIssue), T10-12 (herness.core.config_validate.register_owner_validator) (R-71) |
| Units | U08-64, U08-65, U08-67, U08-99 |
| Files | `herness/core/jobs/cron.py`, `herness/core/jobs/validate.py`, `herness/core/jobs/__init__.py` (lazy map skeleton) |
| Tests | UT08-04, UT08-05, UT08-72, UT08-73, UT08-74, PT08-04, PT08-05 |
| Threats | none |
| Acceptance checks | `validate_resilience_config` returns no issue for the design 08 §7 defaults; `herness config validate` lists U08-99 issues through the `resilience` owner validator (R-71) |
| Blocked by | none |
| Size | M |

### T08-03 Ports, process state and binding

| Field | Content |
|-------|---------|
| Goal | Backend, registry and tracer protocols, the `JobContext`/`ServiceControl` protocols (R-02, R-42), `ProcessState`, bind functions, `JobRow`/`WorkerRow`/`NewJob` and the import-linter contracts exist. |
| Depends on | T08-01 |
| Units | U08-04, U08-08, U08-09, U08-10, U08-41, U08-42 |
| Files | `herness/core/resilience/ports.py`, `herness/core/resilience/_state.py`, `herness/core/jobs/ports.py`, `pyproject.toml` (import-linter contracts) |
| Tests | UT08-63 (row parsing part), UT08-103 |
| Threats | none |
| Acceptance checks | `reset_process_state` fixture registered in `tests/conftest.py` (T11-01 (tests/conftest.py) hook); `lint-imports` contracts "herness.core must not import herness.store or herness.harness" and the R-03 settings contract for `herness.core.resilience.settings` pass |
| Blocked by | none |
| Size | M |

### T08-04 Policies and classification

| Field | Content |
|-------|---------|
| Goal | `RetryPolicy`, `policy`, `policy_for_client`, jitter, job backoff, `classify` and the single Retry-After parser `parse_retry_after` (clamped to `retry_after_max_s`; impl 01 switches to it). |
| Depends on | T08-03, T10-10 (herness.core.redact.redact_text), T10-03 (herness.core.config.config_hash) |
| Units | U08-11–U08-17 |
| Files | `herness/core/resilience/policies.py`, `herness/core/resilience/classify.py` |
| Tests | UT08-06, UT08-07, UT08-10, UT08-11, UT08-112, PT08-01, PT08-02, PT08-03 |
| Threats | TH08-02, TH08-04 |
| Acceptance checks | `pytest -m unit tests/unit/core/resilience -k "UT08-06 or UT08-07 or UT08-10 or UT08-11 or UT08-112 or PT08-01 or PT08-02 or PT08-03"` passes; `classify` imports neither `openai`, `anthropic` nor `duckdb` (asserted by checking `sys.modules` after import in a subprocess) |
| Blocked by | none |
| Size | M |

### T08-05 Resilience store backend, events and metrics

| Field | Content |
|-------|---------|
| Goal | `SqliteResilienceBackend`, `record_event`, metric recording and flushing work against a migrated ops store. |
| Depends on | T08-04, T02-04 (herness.store.ops.core.connection), T02-04 (herness.store.ops.core.run_write), T02-05 (herness/store/migrations/001_ingestion_health.sql, 002_jobs.sql) (tables `source_health`, `resilience_event`), T02-06 (herness/store/migrations/006_metric_sample.sql) (table `metric_sample`), T00-05 (herness.core.ids.new_ulid), T00-07 (herness.core.logging) |
| Units | U08-18–U08-22, U08-94, U08-100, U08-103 |
| Files | `herness/store/ops/resilience.py`, `herness/store/ops/metrics.py`, `herness/core/resilience/events.py`, `herness/core/resilience/metrics.py` |
| Tests | UT08-29, UT08-30, UT08-31, UT08-32, UT08-110, UT08-111, ST08-02 (event and log part), ST08-10 (metric part) |
| Threats | TH08-02, TH08-10 |
| Acceptance checks | Rows written match §4.1.3, §4.1.4, §4.1.6; `resilience.event.dropped` asserted; `herness.store.ops.metrics.record_metric_samples` accepts counter, gauge and histogram samples from any caller (R-12) |
| Blocked by | none (O08-02 resolved by R-12) |
| Size | M |

### T08-06 Circuit breakers and probes

| Field | Content |
|-------|---------|
| Goal | Breaker state machine, per-process cache, cross-process probe claim, `guard`, probe registry. |
| Depends on | T08-05 |
| Units | U08-23–U08-27 |
| Files | `herness/core/resilience/breaker.py` |
| Tests | UT08-12–UT08-19, UT08-104, IT08-01, BT08-05 |
| Threats | TH08-05 |
| Acceptance checks | IT08-01 shows exactly one probe winner over 100 races; BT08-05 ≤ 5 s |
| Blocked by | none |
| Size | M |

### T08-07 Retry execution

| Field | Content |
|-------|---------|
| Goal | `retry_call`, `aretry_call`, `retrying`, `retry_page`, `call_with_timeout`. |
| Depends on | T08-06 |
| Units | U08-28–U08-32 |
| Files | `herness/core/resilience/retry.py` |
| Tests | UT08-08, UT08-09, UT08-20–UT08-28, ST08-04, ST08-05, BT08-01 |
| Threats | TH08-04, TH08-05 |
| Acceptance checks | BT08-01 mean overhead < 50 µs; `retry` events carry the §4.4 fields |
| Blocked by | none |
| Size | M |

### T08-08 Fault hook

| Field | Content |
|-------|---------|
| Goal | Fault plans load safely and `fault_point` applies every action and selector. |
| Depends on | T08-04 |
| Units | U08-33, U08-34 |
| Files | `herness/core/resilience/faults.py` |
| Tests | UT08-46–UT08-50, UT08-105, ST08-06 |
| Threats | TH08-06 |
| Acceptance checks | UT08-46 proves zero file access without the variable; UT08-105 proves a plan is ignored outside `HERNESS_ENV=test` (R-40); no `pyyaml` import in `herness/core/resilience/faults.py` |
| Blocked by | none |
| Size | M |

### T08-09 Repair and model chain

| Field | Content |
|-------|---------|
| Goal | `complete_validated`, `build_repair_request`, `ModelChain` with candidates, retry, repair and fallback. |
| Depends on | T08-07, T08-08, T05-01 (herness.core.types.LLMRequest), T05-01 (herness.core.types.LLMResponse), T05-01 (herness.core.types.Message), T05-11 (herness.harness.tracing.Tracer) |
| Units | U08-35–U08-38 |
| Files | `herness/core/resilience/chain.py` |
| Tests | UT08-33–UT08-42, ST08-07, ST08-15 |
| Threats | TH08-07, TH08-14, TH08-15 |
| Acceptance checks | `fallback`, `repair`, `retry` trace events captured by `recording_tracer` with exact field sets |
| Blocked by | none |
| Size | M |

### T08-10 Decider chain and loop-signal policy

| Field | Content |
|-------|---------|
| Goal | `DeciderChain` and `loop_signal_policy`. |
| Depends on | T08-07, T03-01 (herness.core.types.DecisionInput), T03-01 (herness.core.types.QuestionSet), T05-03 (herness.core.types.LoopState) (with the R-66 `loop_signals` counter), T05-03 (herness.core.types.LoopSignal), T05-11 (herness.harness.tracing.Tracer) (`run_id`, `task_id`, R-66) |
| Units | U08-39, U08-40 |
| Files | `herness/core/resilience/deciders.py`, `herness/core/resilience/loop_policy.py` |
| Tests | UT08-43, UT08-44, UT08-45, ST08-14 (unit part with a stub loop) |
| Threats | TH08-14 |
| Acceptance checks | `guard_stop` row and trace event asserted |
| Blocked by | none (D08-04 and D08-05 accepted by R-66) |
| Size | S |

### T08-11 Jobs store backend

| Field | Content |
|-------|---------|
| Goal | All `job` and `worker` SQL of U08-95 and U08-96. |
| Depends on | T08-03, T08-07, T02-05 (herness/store/migrations/002_jobs.sql) (tables `job`, `worker`), T02-06 (herness/store/migrations/003_runs_evidence.sql) (table `run`) |
| Units | U08-95, U08-96 |
| Files | `herness/store/ops/jobs.py`, `herness/store/ops/worker.py` |
| Tests | UT08-53, UT08-62, UT08-65, IT08-02, ST08-08 |
| Threats | TH08-08 |
| Acceptance checks | IT08-02 (8 × 1 000 claims) has no double claim; `EXPLAIN QUERY PLAN` of the claim uses the `job(status, gpu_class, scheduled_for, priority)` index |
| Blocked by | none |
| Size | M |

### T08-12 Queue API and handlers

| Field | Content |
|-------|---------|
| Goal | `submit`, `enqueue`, `claim`, `cancel`, `retry`, `get`, `list_jobs`, `worker_alive`, handler registry and wrapper. |
| Depends on | T08-11, T10-07 (herness.core.secrets.known_values), T10-10 (herness.core.redact.get_redactor) |
| Units | U08-43–U08-48, U08-51–U08-56 |
| Files | `herness/core/jobs/queue.py`, `herness/core/jobs/handlers.py` |
| Tests | UT08-51, UT08-52, UT08-55–UT08-57, UT08-60, UT08-61, UT08-63, UT08-64, UT08-106, UT08-107, PT08-06, ST08-03, ST08-11, BT08-02, BT08-03 |
| Threats | TH08-03, TH08-10, TH08-11 |
| Acceptance checks | BT08-02 p95 < 10 ms; BT08-03 p95 < 20 ms |
| Blocked by | none |
| Size | M |

### T08-13 Windows and arbiter

| Field | Content |
|-------|---------|
| Goal | `window_at`, `next_window_allowing`, `preempt_deadline`, `arbiter_decide`. |
| Depends on | T08-02 |
| Units | U08-66, U08-68, U08-69, U08-70 |
| Files | `herness/core/jobs/windows.py`, `herness/core/jobs/arbiter.py` |
| Tests | UT08-75–UT08-78 |
| Threats | TH08-09 (request rejection) |
| Acceptance checks | UT08-75 covers all 168 hours of the default week |
| Blocked by | none |
| Size | M |

### T08-14 Scheduler and planned rekey

| Field | Content |
|-------|---------|
| Goal | Schedule collection, catch-up firing, sequential chains with skips, chain repair, `schedule_rekey`. |
| Depends on | T08-12, T08-13, T10-06 (herness.core.secrets.resolve), T01-01 (herness.connectors.settings_base.SourceSettings) (`schedule`, and `reconcile.schedule` of `ReconcileSettings`) |
| Units | U08-71–U08-74 |
| Files | `herness/core/jobs/scheduler.py` |
| Tests | UT08-54, UT08-79–UT08-83, IT08-03 |
| Threats | TH08-02 (key id only) |
| Acceptance checks | UT08-79 enqueues exactly one job per missed fire |
| Blocked by | none |
| Size | M |

### T08-15 Outcome application

| Field | Content |
|-------|---------|
| Goal | `decide_failure` and `finish_job` with owner guard, events, metrics and chain advance. |
| Depends on | T08-12, T08-14 |
| Units | U08-49, U08-50 |
| Files | `herness/core/jobs/outcomes.py` |
| Tests | UT08-58, UT08-59 |
| Threats | TH08-08 |
| Acceptance checks | UT08-58 table covers every taxonomy leaf class |
| Blocked by | none |
| Size | M |

### T08-16 Task helpers

| Field | Content |
|-------|---------|
| Goal | `recover_run_tasks`, `claim_task`, checkpoint envelope, `save_checkpoint`, `complete_task`, `fail_task`, `release_task` with their SQL. |
| Depends on | T08-07, T08-11, T02-06 (herness/store/migrations/003_runs_evidence.sql) (table `task`, index `task(run_id, status)`) |
| Units | U08-57–U08-63, U08-97 |
| Files | `herness/core/jobs/tasks.py`, `herness/store/ops/tasks.py` |
| Tests | UT08-66–UT08-71, UT08-109 |
| Threats | TH08-10 |
| Acceptance checks | UT08-69 proves the `writes` rollback; UT08-109 proves key-scoped checkpoint saves never erase another owner's key (R-21) |
| Blocked by | none |
| Size | M |

### T08-17 GPU services and lock

| Field | Content |
|-------|---------|
| Goal | `ComposeRunner`, `LoopbackHttp`, VRAM helpers, `GpuLock`, and the `tests/support/fake_gpu.py` fixture. |
| Depends on | T08-07, T10-17 (herness.core.egress.loopback_http_client) (R-06) |
| Units | U08-78, U08-79, U08-80, U08-82 |
| Files | `herness/core/jobs/gpu_services.py`, `herness/core/jobs/gpu_lock.py` |
| Tests | UT08-87–UT08-90, UT08-95, ST08-01, ST08-12 |
| Threats | TH08-01, TH08-09, TH08-12 |
| Acceptance checks | argv lists asserted exactly; UT08-103 finds no `httpx` client construction under `herness/core/jobs/` |
| Blocked by | open-questions (b)15 for the real OpenJev warm-up body (fake server used until Phase 4) |
| Size | M |

### T08-18 GPU controller and state reader

| Field | Content |
|-------|---------|
| Goal | `GpuController` swap, service control, detection, restart; `gpu_state()`; `request_gpu_class` for `gpu load/unload` and `deploy up/down` (R-47). |
| Depends on | T08-06, T08-17, T08-11 |
| Units | U08-81, U08-83, U08-102 |
| Files | `herness/core/jobs/gpu.py` |
| Tests | UT08-91–UT08-94, UT08-96, UT08-108 |
| Threats | TH08-01, TH08-09 |
| Acceptance checks | `fake_gpu` never shows services of two classes running during UT08-91–UT08-94 |
| Blocked by | none |
| Size | M |

### T08-19 Chat policy

| Field | Content |
|-------|---------|
| Goal | `chat_policy`, `chat_model_profile`, `chat_next_live_at`. |
| Depends on | T08-13, T08-18, T10-16 (herness.core.egress.cloud_chat_allowed) (R-38) |
| Units | U08-75, U08-76, U08-77 |
| Files | `herness/core/jobs/chat_policy.py` |
| Tests | UT08-84, UT08-85, UT08-86, ST08-07 (chat part), BT08-09 |
| Threats | TH08-07 |
| Acceptance checks | BT08-09 p95 < 5 ms on warm caches |
| Blocked by | none |
| Size | S |

### T08-20 Pipe and job contexts

| Field | Content |
|-------|---------|
| Goal | JSON pipe messages, `ChildJobContext`, `InlineJobContext` and service controls. |
| Depends on | T08-12, T08-18 |
| Units | U08-84, U08-85, U08-86 |
| Files | `herness/core/jobs/pipe.py`, `herness/core/jobs/context.py` |
| Tests | UT08-97, UT08-98, UT08-99, ST08-13 |
| Threats | TH08-10, TH08-13 |
| Acceptance checks | a grep test asserts no `Connection.send(`/`recv(` (pickle) calls in `herness/core/jobs/` |
| Blocked by | none |
| Size | M |

### T08-21 Supervisor, child entry and `run_worker`

| Field | Content |
|-------|---------|
| Goal | The worker process of design 08 §5.9–§5.10. |
| Depends on | T08-05, T08-06, T08-14, T08-15, T08-19, T08-20, T09-27 (herness.cli.worker_bootstrap) (a test bootstrap is used until it exists) |
| Units | U08-87, U08-88, U08-89 |
| Files | `herness/core/jobs/supervisor.py`, `herness/core/jobs/child.py` |
| Tests | IT08-04–IT08-11, IT08-13, ST08-09, BT08-11 |
| Threats | TH08-08, TH08-09, TH08-13 |
| Acceptance checks | IT08-08 passes on Windows (`CTRL_BREAK_EVENT`) and Linux (SIGTERM) runners; the worker exits only with 0 or 1 (R-46) |
| Blocked by | open-questions (b)19 (`wsl.exe` from a service session) for service-mode deployment only |
| Size | M |

### T08-22 Inline runs, status, health and resume

| Field | Content |
|-------|---------|
| Goal | `run_inline`, `status_snapshot`, `health`, `enqueue_resume`. |
| Depends on | T08-21 |
| Units | U08-90, U08-91, U08-92, U08-93 |
| Files | `herness/core/jobs/inline.py`, `herness/core/jobs/status.py` |
| Tests | IT08-12, UT08-100, UT08-101, UT08-102, BT08-10 |
| Threats | TH08-06 (status and health flags) |
| Acceptance checks | `status_snapshot` keys equal §3.17 table |
| Blocked by | none |
| Size | M |

### T08-23 Backend binding and package export

| Field | Content |
|-------|---------|
| Goal | `bind_core_backends`, complete lazy export maps of both packages, import-linter contracts. |
| Depends on | T08-16, T08-22, T02-04 (herness.store.ops) (composition entry calls) |
| Units | U08-98 (and the export maps of U08-10) |
| Files | `herness/store/ops/resilience.py` (add function), `herness/store/ops/__init__.py` (08 re-export block), `herness/core/resilience/__init__.py`, `herness/core/jobs/__init__.py` |
| Tests | UT08-103, IT08-04 (bound through `bind_core_backends`) |
| Threats | none |
| Acceptance checks | every public name of design 08 §3 importable from `herness.core.resilience` / `herness.core.jobs`; `python -c "import herness.core.resilience.settings"` does not import `herness.core.jobs.supervisor`; `herness.store.ops.record_metric_samples`, `purge_metric_samples`, `purge_events` and `bind_core_backends` are importable from `herness.store.ops` |
| Blocked by | none |
| Size | S |

### T08-24 Fault suite F1–F12

| Field | Content |
|-------|---------|
| Goal | FT08-01–FT08-12 and the integration-level security tests exist. |
| Depends on | T08-23, T11-23 (tests.support.fake_llm.FakeLLMServer), T11-24 (tests.support.stub_decider.StubDeciderServer), T06-22 (herness.harness.swarm.handler.review_job_handler) (review runs), T01-06 (herness.connectors.runner), T02-18 (herness.model.build.run_build_pipeline) (F8) |
| Units | none (tests only) |
| Files | `tests/fault/test_resilience_f01_f12.py`, `tests/support/fake_gpu.py` (extended) — test files only |
| Tests | FT08-01–FT08-12, ST08-05, ST08-14 |
| Threats | TH08-05, TH08-14 |
| Acceptance checks | CPU subset (FT08-01–07, 11, 12) passes in CI with `HERNESS_ENV=test` and JSON plans (R-40); FT08-08–10 pass on the target PC |
| Blocked by | open-questions (b)9 and (b)15 for FT08-08–FT08-10 on real containers |
| Size | M |

### T08-25 Benchmarks

| Field | Content |
|-------|---------|
| Goal | BT08-01–BT08-11 exist with thresholds enforced. |
| Depends on | T08-23 |
| Units | none |
| Files | `tests/bench/test_resilience_bench.py`, `tests/bench/test_jobs_bench.py` — test files only |
| Tests | BT08-01–BT08-11 |
| Threats | none |
| Acceptance checks | CPU benchmarks pass on the dev box; GPU ones on the target PC |
| Blocked by | open-questions (b)9 (D7) for BT08-07 |
| Size | S |

## 13. Design deltas and open items

Cross-spec rulings are recorded in [`DECISIONS.md`](DECISIONS.md) (R-01 to R-76). This spec applies R-01, R-02, R-03, R-04, R-06, R-07, R-08, R-09, R-10, R-11, R-12, R-14, R-19, R-21, R-35, R-36, R-38, R-39, R-40, R-41, R-42, R-43, R-44, R-45, R-46, R-47, R-51, R-53, R-65, R-66, R-67, R-68, R-70, R-71 and R-72; the other rulings do not concern this spec. The Status column below marks each delta "Resolved by R-nn" (the ruling settled it, and this spec follows the ruling), "Accepted (R-nn)" (the ruling adopted this spec's proposal) or "Still open" (no ruling; the current default applies until the design specs are edited, DECISIONS §9).

### 13.1 Design deltas

| # | Design spec | Change needed | Reason | Current default | Status |
|---|-------------|---------------|--------|-----------------|--------|
| D08-01 | 08 §1, 00 §3 | `herness/core/resilience.py` and `jobs.py` become packages with the same import paths | ENG §2.4 400-line limit | packages as in §2 | Still open |
| D08-02 | 08 §3.7, 00 §4 | "helpers write through `herness.store.ops`" is realised by the L1 areas `herness/store/ops/{resilience,metrics,jobs,worker,tasks}.py` behind L0 ports bound at the composition root | ENG §2.1: L0 must not import L1 | ports (U08-08, U08-41) | Resolved by R-04, R-08 |
| D08-03 | 00 §6 | `ModelChain` and `loop_signal_policy` are behavior in `herness.core.resilience`; `JobContext` and `ServiceControl` are protocols in `herness.core.jobs` (no longer in `core.types`); the 08 data types are in `herness.core.types.jobs` | `core.types` holds data types only | as stated (U08-01–U08-04) | Accepted (R-01, R-02) |
| D08-04 | 05 §4.8 | `LoopState` gains a loop-signal counter `loop_signals`, separate from `nudges`, incremented by the loop before the policy call | `nudges` also counts `max_tokens` continuation nudges | U08-40 reads `state.loop_signals` | Accepted (R-66) |
| D08-05 | 05 §5.7 | `Tracer` exposes read-only `run_id` and `task_id` | `guard_stop` rows need the IDs | `TracerLike` port (U08-08) | Accepted (R-66) |
| D08-06 | 10 §3.5 | Loopback health and warm-up clients | health and warm-up need a local client | clients only from `herness.core.egress.loopback_http_client` (U08-79); no allowlist entry | Resolved by R-06 |
| D08-07 | 03 §3.3, 05 §7 | Port disagreement for OpenJev and llama.cpp | contradiction | vLLM 8000, OpenJev 8100, llama.cpp 8200 (U08-06, §9) | Resolved by R-51 |
| D08-08 | 08 §7 vs 03, 10 §3.3 | OpenJev bearer secret name | contradiction | `secret:OPENJEV_API_KEY` | Resolved by R-53 |
| D08-09 | 01 §5, §6 | Open circuit during a sync | contradiction | sync ends `done` with outcome `skipped_open_circuit`; the next scheduled run retries (U08-49) | Resolved by R-39 |
| D08-10 | 06 §6.4 vs 08 §3.7 | How a cancelled running task is handled | two options | `release_task` (U08-63) | Resolved by R-36 |
| D08-11 | 02 §5.2 | `worker.current_jobs` entries gain `note` (last heartbeat note) | status shows the note (08 §5.14) | additive JSON field; no migration needed | Still open |
| D08-12 | 08 §3 | Additive public API: `window_at`, `enqueue_resume`, `health`, `ComponentHealth`, `register_probe`, `run_due_probes`, `bind_ops_backend`, `bind_chain_registry`, `bind_jobs_backend`, metrics API including `record_gauge`, `complete_validated(model=)`, `DeciderChain(resolve=)`, `schedule_rekey(now=)`, `save_checkpoint(writes=)`, `request_gpu_class`, `validate_resilience_config`, `parse_retry_after(max_s=)` | needed to wire and test the design | as specified | Still open |
| D08-13 | 02 §5, ENG E5 | `metric_sample` columns | the earlier 08 draft added `sample_id`, `host`, `pid` | impl 02 migration 006 columns only (§4.1.6); writer U08-100 | Resolved by R-12 |
| D08-14 | 00 §5 | `resilience_event.event_id` format `evt_<ulid>` | ID not listed | `evt_<ulid>` | Still open |
| D08-15 | 08 §3.4 vs §4.1 | `enqueue(priority=50)` default vs per-kind defaults | internal inconsistency | `priority: int \| None = None`, `None` = `DEFAULT_PRIORITY[kind]` (U08-02, U08-47) | Resolved by R-41 |
| D08-16 | 00 §7 | New `JobStateError(FatalError)` | state conflicts need a taxonomy class | as U08-05 | Accepted (R-19) |
| D08-17 | 11 §5.5 vs 08 §5.13 | Fault plan format and gate | contradiction | JSON only; honoured only with `HERNESS_ENV=test`, otherwise ignored with a WARNING (U08-33, U08-34) | Resolved by R-40 |
| D08-18 | 02 §5.2 | Optional `worker.services JSON` column so `chat_policy` needs no HTTP health call | chat_policy < 5 ms target | 5 s per-process health cache (O08-05); a column would need migration 050 (R-11) | Still open |
| D08-19 | ENG §2.3, §3.4 | Exceptions: module state holder `ProcessState`; broad `except` at the classification boundary, decider crash wrapper and probe runner | design-mandated caches and conversions | listed in §2 | Still open |
| D08-20 | 08 §5.1, §5.9, §7 | `build_pipeline` starts with GPU class `none` (default `nightly` step) and takes `decider` through `ctx.gpu_scope`; `GPU_SLOT_KINDS` jobs of class `none` run in the GPU slot | in-job switches need the GPU slot | U08-43, U08-48, U08-70, U08-95 | Accepted (R-43) |
| D08-21 | 08 §7, 10 §3.1 | All `config/resilience.yaml` models in `herness.core.resilience.settings`; week coverage and cron parsing move to `validate_resilience_config`, run through impl 10's owner validator hook (R-71) and at worker start | a settings module may not import `herness.core.jobs.cron` | U08-06, U08-07, U08-99 | Accepted (R-03, R-71) |
| D08-22 | 08 §3.7, §4.3 | Checkpoint envelope `{schema_version, loop, state, scratchpad}` and key-scoped `save_checkpoint(task_id, key, value)` | a whole-object replace erased other owners' keys (impl 05 D05-04) | U08-59, U08-60 | Accepted (R-21) |
| D08-23 | 08 §3.8 | `herness worker` exit codes 0 and 1 only (was 2 for a start `ConfigError` and 3 for the store-unavailable shutdown) | one exit-code scheme | U08-87 | Resolved by R-46 |
| D08-24 | 08 §5.13 | `kill_service:<name>` first looks up `HERNESS_STUB_SERVICES` under `HERNESS_ENV=test` and posts `/__control/kill` to the stub | impl 11 DD11-04 needs it for FT11-03 | U08-34 | Still open |
| D08-25 | 08 §5.10 | Chat `cloud` mode in the `hybrid` profile only with the `chat` data-policy approval | D5 data policy | U08-75 | Accepted (R-38) |
| D08-26 | 08 §5.10 | Escalation under `small_model` is enqueued for the next live window, not refused | impl 06 D06-17 | U08-77 purpose, F08-15 | Resolved by R-35 |
| D08-27 | 08 §5.2, §7 | `parse_retry_after` is the single Retry-After parser (impl 01 U01-61 switches to it); new key `resilience.retry.retry_after_max_s` (default 86 400 s) clamps its result | one parser; bounded reschedule times | U08-17, U08-06 | Accepted (R-70) |
| D08-28 | 08 §5.14, ENG §4 | Gauge recording: `record_gauge` and `kind = "gauge"` samples through `record_metric_samples` | impl 03 request RQ-04 | U08-100, U08-101, U08-103 | Still open |

### 13.2 Open items (with current defaults)

| # | Item | Default | Blocks | Status |
|---|------|---------|--------|--------|
| O08-01 | Design 08 Q7 / open-questions (b)15: OpenJev health `GET /v1/models` and warm-up `POST /v1/systemone` body against the pinned image | body in U08-79 | T08-17 real-container check, FT08-12 on hardware | Still open |
| O08-02 | `metric_sample` DDL agreed with impl 02 | impl 02 §4.3.6 columns | none | Resolved by R-12 |
| O08-03 | Catch-up windows for per-source schedules (not in design) | sync 1 h, reconcile 12 h | none | Still open |
| O08-04 | ETA while a swap to reasoning is running | now + 360 s | none | Still open |
| O08-05 | Service health for `chat_policy` without an HTTP call (D08-18) | 5 s cache, 2 s timeout | none | Still open |
| O08-06 | Default priority of `distill` (absent from design 08 §4.1) | 40 | none | Still open |
| O08-07 | When the task status `failed` is written (never by 08) | 08 writes only `pending`/`dead` | none | Still open |
| O08-08 | Retention of finished `job` rows | not purged by 08 | none | Still open |
| O08-09 | Retention of `metric_sample` | 90 days via the `herness.admin` retention purge (R-07) | none | Still open |
| O08-10 | open-questions (b)19: `wsl.exe` from a non-interactive service session | fallback per spec 10 (auto-logon) | T08-21 service deployment | Still open |
| O08-11 | open-questions (b)9 / D7: OpenJev weights on the target GPU | tests use fakes | T08-24 F8–F10, T08-25 BT08-07 | Still open |
| O08-12 | Open decisions D3 (cadence), D4 (chat hours), D5 (egress) | design defaults in §9 | none | Still open |
| O08-13 | Name of the `chat` approval flag in `security.data_policy` (R-38; impl 10 adds it) | `security.data_policy.chat_approved` (default `false`), read through `cloud_chat_allowed` (U08-75 step 4) | none | Resolved by R-38 |

### 13.3 Contradictions found in this pass

| # | Between | Contradiction | This spec's position | Status |
|---|---------|---------------|----------------------|--------|
| C08-01 | R-03 vs impl 10 §2 (settings import rule) | Impl 10 lets a `settings.py` module import `herness.core.secrets` and other `settings.py` modules; R-03 allows only the standard library, pydantic, `herness.core.types` and `herness.core.errors` | follows R-03: one settings module, secret references checked by a local regex (U08-06, D08-21) | Still open |
| C08-02 | impl 06 D06-09 vs U08-66 | Impl 06 states that no 08 function reports whether the chat window is open | `window_at(now).spec.name == "chat"` is that test (U08-66) | Still open |
| C08-03 | impl 02 U02-38 vs the earlier 08 draft | `run_write` already applies `retry_call("sqlite_write")`; the earlier 08 draft wrapped backend calls in a second `retry_call` | 08 never adds a second `sqlite_write` retry (§3 intro) | Resolved by R-10 |
| C08-04 | R-71 vs the earlier 08 draft | Impl 10 removed `SECTION_VALIDATORS` (U10-22) in favour of the start-up hook `register_owner_validator` (U10-109, signature `(cfg, *, offline)`) | U08-99 takes `offline` and is registered by the composition roots (T09-20, T09-13, T09-27) | Resolved by R-71 |
| C08-05 | R-70 vs impl 01 U01-61 | Impl 01 still defines its own `herness.connectors.http.parse_retry_after` (U01-61, T01-14) | impl 01 replaces U01-61 with a reference to T08-04 (herness.core.resilience.classify.parse_retry_after), U08-17 | Resolved (impl 01 U01-61 removed, R-70) |
| C08-06 | impl 10 U10-107 vs U08-75 | U10-107 says impl 09 does the chat mode selection; the selection is `chat_policy` (U08-75, T08-19), which calls `cloud_chat_allowed` | impl 10 changes that reference to T08-19 (herness.core.jobs.chat_policy) | Resolved (impl 10 U10-107 now cites T08-19) |

## 14. Dependencies

### 14.1 Third-party packages

| Package | Minimum version | Licence | Use |
|---------|-----------------|---------|-----|
| `tenacity` | 9 | Apache-2.0 | retry engine (U08-28, U08-29) |
| `httpx` | 0.27 | BSD-3-Clause | exception classes in `classify` (U08-16); the loopback client objects themselves come from `herness.core.egress.loopback_http_client` (U08-79, R-06) |
| `pydantic` | 2.9 | MIT | config, types, pipe messages |
| `structlog` | 24 | MIT or Apache-2.0 | logging |
| `psutil` | 5.9 | BSD-3-Clause | pid checks, CPU benchmark |
| `tzdata` | 2024.1 | Apache-2.0 | `zoneinfo` data on Windows |
| `jsonschema` | 4.21 | MIT | schema validation in repair without a model |
| Dev: `pytest`, `hypothesis`, `freezegun`, `respx`, `pytest-benchmark`, `pytest-timeout` | per spec 00 §9 | MIT/MPL-2.0/Apache-2.0 | tests |

No new dependency beyond spec 00 §9. `pyyaml` is no longer used by this spec: fault plans are JSON only (R-40).

### 14.2 Internal dependencies

| Spec | Units used |
|------|------------|
| 00 artifacts | `herness.core.errors` taxonomy (R-19); `herness.core.ids.new_ulid` and `canonical_json` (R-14); `herness.core.time` (now and ts formatting); `herness.core.logging`; the `herness.core.types` package skeleton, `__init__` re-export and `TYPE_OWNERS` (U00-44, U00-45, R-01) |
| 02 | `herness.store.ops.connection`, `run_write`, `read_one`, `read_all`, `migrate` (R-10); migrations 001–006 for `job`, `worker`, `source_health`, `resilience_event`, `task`, `run`, `metric_sample` (R-11, R-12); the `herness.store.ops` package `__init__` that re-exports the 08 block |
| 03 | `DecisionInput`, `DecisionOutput`, `QuestionSet`; decider registry entries; `Decider.health` as probe |
| 05 | `LLMRequest`, `LLMResponse`, `Message`, `TextPart`, `LoopState` (with `loop_signals`, R-66), `LoopSignal`; `Tracer` structurally through `TracerLike` (R-66); `LLMRegistry` (as `ChainRegistry`); consumers `HarnessHooks`, `GatedClient`; caller of `save_checkpoint` for key `loop` (R-21) |
| 06 | consumer of task helpers (key `state` of the checkpoint envelope, `release_task` for cancelled tasks, R-21, R-36), `ModelChain`, `JobContext`, chat policy and `chat_next_live_at` (R-35); `review` and `chat` handlers |
| 01, 03, 04, 07, 10, 11 | handlers and consumers; 01 switches to `parse_retry_after` (D08-27); 02, 03, 04, 05, 09, 11 write metrics through `record_metric_samples` (R-12); 07: key `scratchpad` of the checkpoint envelope (R-21); 10: `get_config`, `config_hash`, `ConfigIssue`, `register_owner_validator` (R-71), `secrets.resolve`, `egress.cloud_chat_allowed` (R-38), `secrets.known_values`, `redact_text`, `get_redactor`, registry, `egress.loopback_http_client` (R-06), `security.data_policy.chat_approved` through `cloud_chat_allowed` (R-38), `herness.admin` retention purge (R-07), `deploy up/down` through `request_gpu_class`; 11: `tests/support/fake_llm.py` (`FakeLLMClient`, `FakeLLMServer`, R-65) and `StubDeciderServer` |
| 09 | composition roots (`herness.cli` entry, `worker_bootstrap`, `app/common`), CLI and dashboard rendering of §3.17; `gpu load/unload` through `request_gpu_class` (R-47); admin-only `--inline` through `run_inline` (R-45) |
