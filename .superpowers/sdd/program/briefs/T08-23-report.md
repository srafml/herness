# T08-23 report: Backend binding and package export

Worktree: D:\herness\.claude\worktrees\agent-af6ca4b3d433f45d9 (base 5f2b7c0). Commits: 3270d1f `wip(T08-23): ...` holds all the content. The final `feat(jobs): ...` commit is an empty marker that carries the card subject (see Concerns).

## What changed (lines vs budget)
| File | Change | Lines / budget |
|---|---|---|
| herness/store/ops/resilience.py | `bind_core_backends()` (U08-98): `bind_ops_backend(SqliteResilienceBackend())`, then `bind_jobs_backend(SqliteJobsBackend())`. It imports from the packages `herness.core.resilience` and `herness.core.jobs`, as U08-98 names them | 248 / 320 |
| herness/store/ops/__init__.py | 08 resilience block: `from .resilience import bind_core_backends, purge_events`; `__all__` gets "purge_events", "bind_core_backends". `record_metric_samples`, `purge_metric_samples` and `purge_events` were already re-exported; block order is unchanged | 316 / 400 (+0 lines) |
| herness/core/resilience/__init__.py | Now holds only the PEP 562 `__getattr__`; the map comes from `_exports` | 30 / 120 (the spec row says 120; the brief's 70 is out of date) |
| herness/core/resilience/_exports.py (new, private) | `EXPORTS` map plus `TYPE_CHECKING` `X as X` imports | 146 / 160 (new §2 row) |
| herness/core/jobs/__init__.py | Same pattern | 32 / 90 |
| herness/core/jobs/_exports.py (new, private) | `EXPORTS` map plus `TYPE_CHECKING` imports | 218 / 230 (new §2 row) |
| herness/core/jobs/chat_policy.py | Added `_CallableModule`, the same trick the resilience `classify` and `breaker` modules use. Python sets the package attribute `chat_policy` to the submodule on import, so the module is made callable and `from herness.core.jobs import chat_policy; chat_policy(now)` works | 164 / 170 |
| pyproject.toml | Contracts (see below) | — |
| docs/impl/08-resilience-and-jobs.impl.md | §2: two new rows for the private `_exports.py` siblings (module-map note, T08-23). The `store/ops/resilience.py` row now notes the jobs-area import ignore | — |
| .secrets.baseline | The hook regenerated the line numbers of the two audited 08 doc entries; LF endings kept, no entry dropped | — |

Budgets: no ruling was needed. Both package `__init__` files stay within budget because the map moved into a private sibling `_exports.py`, recorded with a §2 module-map row (the controller notes allow a private sibling with a spec note).

Typing: `__init__` does `if TYPE_CHECKING: from ._exports import *  # noqa: F403`. A mypy probe confirmed that `from herness.core.jobs import submit / chat_policy / GpuLock` and `from herness.core.resilience import breaker / record_event` resolve to the real signatures. `_exports.py` carries `# ruff: noqa: PLC0414` because the `X as X` re-export form is its purpose. At run time `_exports` imports only `typing`.

## Export lists
These are the §2 public-symbol columns of each package's non-private submodules; where the carry-over lists and the spec differ, the spec wins.

Section numbers: the §2 rows say "§3.3–§3.8" (resilience) and "§3.9–§3.15" (jobs). In the impl spec, though, §3.9 is ModelChain and DeciderChain (resilience), and §3.16–§3.17 are supervisor, inline and status (jobs). I therefore split by owning package: each package exports every §2 public symbol of its own submodules.

**herness.core.resilience (52 names, 17 new):**
- ports: ResilienceBackend, HealthRow, EventRow, ChainRegistry, ClientInfo, GpuStateReader, AsyncCompleter, DeciderLike, TracerLike
- _state: ProcessState, process_state, reset_process_state, bind_ops_backend, bind_chain_registry
- policies: RetryPolicy, POLICY_FAMILY, GPU_HEALTH_POLICY, policy, policy_for_client, full_jitter_delay, FullJitterRetryAfter, job_backoff_delay
- classify: classify, parse_retry_after
- events: record_event, EVENT_KINDS
- metrics: record_counter, record_histogram, record_gauge, timed, flush_metrics
- breaker: breaker_transition, probe_due, CircuitBreaker, breaker, guard, register_probe, run_due_probes
- retry: retry_call, aretry_call, retrying, retry_page, call_with_timeout
- faults: FaultRule, load_fault_plan, fault_point, NAMED_POINTS
- chain: complete_validated, build_repair_request, ModelChain
- deciders: DeciderChain
- loop_policy: loop_signal_policy

Not exported: the settings models (§3.2; settings must stay a light import under R-03) and private helpers.

**herness.core.jobs (82 names, 64 new):**
- validate: validate_windows, validate_resilience_config (already exported; kept)
- cron: CronExpr, resolve_local
- ports: JobsBackend, JobRow, WorkerRow, NewJob, SchedCheck (U08-41), bind_jobs_backend, JobContext, ServiceControl
- queue: DEFAULT_PRIORITY, MANUAL_PRIORITY, GPU_SLOT_KINDS, default_idem_key, validate_payload, submit, enqueue, claim, cancel, retry, get, list_jobs, worker_alive
- handlers: register_handler, resolve_handler, run_handler
- outcomes: decide_failure, FailureAction, finish_job
- windows: ActiveWindow, window_at, next_window_allowing, preempt_deadline
- arbiter: ArbiterDecision, arbiter_decide
- scheduler: ScheduleEntry, SchedulerReport (U08-72 return type), collect_schedules, run_scheduler, advance_chain, schedule_rekey
- chat_policy: chat_policy, chat_model_profile, chat_next_live_at
- tasks: RecoverySummary, recover_run_tasks, claim_task, build_checkpoint_envelope, save_checkpoint, complete_task, fail_task, release_task
- gpu_services: ComposeRunner, LoopbackHttp, vram_used_mb, wait_vram_free
- gpu_lock: GpuLock
- gpu: GpuController, WorkerGpuState, gpu_state, request_gpu_class
- pipe: PipeMessage, encode_message, decode_message, MAX_PIPE_MSG_BYTES
- context: ChildJobContext, ChildServiceControl, InlineJobContext, InlineServiceControl
- supervisor: WorkerOptions, Supervisor, run_worker
- child: child_main
- inline: run_inline
- status: StatusSnapshot, status_snapshot, ComponentHealth, health, ResumeResult, enqueue_resume

Every carry-over name exists in the tree, and every name the spec lists as public exists too; nothing had to be reported as missing.

## Contracts (pyproject; 14 kept, 0 broken)
- **Unchanged:** "herness.core must not import herness.store or herness.harness".
- **Strengthened, `resilience-settings-light` (R-03):** added herness.core.audit, config, egress, redact, registry, secrets and settings; the resilience API submodules (_state, ports, policies, classify, events, metrics, breaker, retry, faults, chain); and herness.connectors, enrich, metrics and harness. Nothing was removed.
- **New, `types-jobs-light`:** herness.core.types.jobs may not import core logging, _log_pipeline, time or numbers, or the sibling type modules (decisions, memory, reports, swarm, _ownership). It checks direct imports only, because herness.core.ids itself imports herness.core.time. The rest of Herness was already closed by "core base is closed". The third-party part is still checked by the UT08-103 AST test.
- **httpx (R-06), no import-linter contract:** import-linter cannot forbid external packages here (`include_external_packages = false`), and it cannot express "constructs". The existing UT08-103 AST test (no httpx Client, AsyncClient or transport is imported or constructed under herness/core/jobs/) remains the check.
- **`ops-areas-acyclic`, new ignore:** `herness.store.ops.resilience -> herness.store.ops.jobs`, because bind_core_backends constructs SqliteJobsBackend. The jobs area never imports resilience, so the graph stays acyclic. This follows the existing `jobs -> tasks` and `resilience -> metrics` entries.

## Tests
- **New, tests/unit/store/ops/test_store_ops_bind.py** (3 `test_cv_t08_23_*` functions, docstrings "IT08-04 (cv)"):
  - both ports are bound to the SQLite classes;
  - a second call replaces both without error, and `atexit.register` is called exactly once (U08-10);
  - `herness.store.ops` re-exports bind_core_backends, record_metric_samples, purge_metric_samples and purge_events.
- **New, tests/unit/core/test_exports_08.py** (7 cases):
  - each export map equals the spec list exactly, and `__all__` equals its sorted keys;
  - every name is its owner's object; the names that match their submodule (`chat_policy`, `classify`, `breaker`) resolve to the callable module;
  - in a subprocess, `import herness.core.jobs, herness.core.resilience` loads no submodule except `_exports`;
  - in a subprocess, `import herness.core.resilience.settings` loads only `settings` and `_exports`, and no herness.core.jobs module at all.
- **Extended, tests/unit/repo/test_import_contracts_08.py (UT08-103):** 2 new tests. One checks that the settings contract forbids everything R-03 excludes; the other checks that `types-jobs-light` is declared with its forbidden set.
- **IT08-04 now binds through bind_core_backends:** tests/support/worker_bootstrap.py calls `bind_core_backends()`, and its `type: ignore` is gone. I also switched two fixtures: `jobs_db` in tests/unit/core/jobs/_queue_env.py (the cast is gone) and `ops_db` in tests/unit/core/resilience/conftest.py (per §11, "bound with U08-98").
- **Adjusted existing UT08-63** (test_jobs_ports.py::test_ut08_63_lazy_exports_resolve): the same-name export `chat_policy` now resolves to the callable module, matching the existing rule for resilience `classify` and `breaker`.

### RED (before implementation)
Command: `pytest tests/unit/repo/test_import_contracts_08.py tests/unit/core/test_exports_08.py tests/unit/store/ops/test_store_ops_bind.py`. Result: 10 failed, 6 passed. The failures:
- settings_contract_allows_only_types_and_errors
- types_jobs_contract_declared
- export_map_is_the_spec_list[resilience] and [jobs]
- every_name_resolves_to_its_owner[resilience] and [jobs]
- same_name_exports_are_callable_modules
- binds_both_ports and rebind_replaces_and_registers_atexit_once (AttributeError: module has no attribute 'bind_core_backends')
- package_reexports ('bind_core_backends' not in ops.__all__)

### GREEN
- The same command: 16 passed.
- Touched packages (tests/unit/core/resilience, tests/unit/core/jobs, tests/unit/store/ops, tests/unit/repo, test_exports_08.py), run with pandas imported first: 1719 passed, 1 skipped, 2 errors. The 2 errors (test_jobs_gpu_lock.py) came from my own runner script, which lacked a `__main__` guard for multiprocessing spawn. Under plain pytest that file passes 6/6.
- tests/integration/jobs: 36 passed, including IT08-04 (also run alone: passed).
- The pre-commit unit-test hook passed on the successful commit.

### Gates
- ruff check and ruff format --check: clean.
- mypy: 0 errors in 356 files.
- lint-imports: 14 kept, 0 broken.
- tools.check_type_ownership: exit 0.
- tools.check_module_size: exit 0.

## Concerns / notes
- **Existing crash, not caused by this card:** tests/unit/core/resilience/test_resilience_breaker.py hits a stack overflow when run alone without pandas imported first. I reproduced it on an archive of base 5f2b7c0. The cause matches the known test_jobs_gpu.py crash: freezegun walks the lazy `herness.enrich` package, which imports pandas. I did not edit the test. With pandas imported first it passes.
- **Likely load flake:** the first commit attempt failed the hook on tests/unit/harness/test_blackboard.py::test_st06_05_pii_checked_before_marker_validation. It passes alone, in its own file, and in the whole of tests/unit/harness (2072 passed). The retried commit passed. Worth adding to the flake list if it happens again.
- **Brief budget out of date:** the brief gives 70 lines for resilience/__init__, but the spec §2 row at base already says 120 (the base file was 114). It is now 30.
- **Spec row wording:** the §2 rows' section ranges (§3.3–§3.8 and §3.9–§3.15) don't match the impl §3 numbering. I split by package as described above; you may want the two rows reworded.
- **File outside the card's list:** I edited `chat_policy.py` for the callable-module fix (+12 lines, within its 170-line budget).
- **Empty final commit:** all content is in 3270d1f. The final commit is an empty marker carrying the card subject, because the report lives outside the worktree and there was nothing left to commit.

## Fix round 1 (review T08-23-review.md: I-1, M-1..M-4)

Worktree agent-af6ca4b3d433f45d9; checkpoint 912c723 `wip(T08-23): fix round 1 settings allowlist contract (I-1)`; final commit: see the end of this section.

### I-1: R-03 settings contract complete (both parts of the controller ruling)
- **pyproject.toml `resilience-settings-light`:** the forbidden list is now derived programmatically from the tree. It names every Herness top-level package except `herness.core`, every `herness.core` module except `types` and `errors`, and every `herness.core.resilience` module except `settings` (the package itself must stay importable as the parent). That is 52 entries; `_exports` is included. The derivation script is under C:\Users\santh\AppData\Local\Temp\w27-s08\fix1\derive.py. No entry was removed: all 26 previous entries are still present, only reordered. No other contract changed. `herness.core.*.settings` still matches only herness.core.resilience.settings.
- **tests/unit/repo/test_import_contracts_08.py:**
  - The hand-written `_SETTINGS_FORBIDDEN` is replaced by `_settings_forbidden()`, which derives the same set from the tree (top-level, core and resilience children minus the allowlist). The contract test now goes red if a new module is added but not listed in pyproject.
  - New test `test_ut08_103_settings_imports_allowlist` is an AST allowlist over herness/core/resilience/settings.py. Every import must be stdlib, pydantic, `herness.core.types(.*)` or `herness.core.errors`.
    - It walks the whole tree, so imports inside `TYPE_CHECKING` blocks and function bodies count as imports.
    - Relative imports are resolved against `herness.core.resilience`.
    - `from herness.core import errors` is checked as the submodule it names.
  - New test `test_ut08_103_settings_allowlist_rejects` (6 cases) and new test `test_ut08_103_settings_allowlist_accepts` pin the helper's handling of relative, TYPE_CHECKING and function-local imports.
- **Red, then green (each mutation reverted; `git diff herness` empty afterwards):**
  - **`import herness.core.redact_patterns` added to settings.py:**
    - pytest: `FAILED test_ut08_103_settings_imports_allowlist` and `FAILED test_ut08_103_lint_imports_passes` (2 failed, 12 passed).
    - `uv run lint-imports`: `resilience-settings-light BROKEN`, `Contracts: 13 kept, 1 broken`, `herness.core.resilience.settings -> herness.core.redact_patterns (l.26)`.
  - **`import herness.core.resilience.deciders` added to settings.py:**
    - `uv run lint-imports`: `resilience-settings-light BROKEN`, `Contracts: 13 kept, 1 broken`, `herness.core.resilience.settings -> herness.core.resilience.deciders (l.26)`.
    - pytest with the normal conftest: the session fails earlier, at conftest import, with a circular import (`deciders -> herness.core.config`).
    - pytest with `--noconftest`: `AssertionError: herness.core.resilience.deciders` in `FAILED test_ut08_103_settings_imports_allowlist`, plus `FAILED test_ut08_103_lint_imports_passes` (2 failed, 12 passed).
  - **Reverted:** 14 passed, and lint-imports reports 14 kept, 0 broken.

### Minor fixes
- **M-1:** new test `test_cv_t08_23_chat_policy_module_call_delegates` in tests/unit/core/test_exports_08.py.
  - It monkeypatches the module's `chat_policy` function and checks that calling `jobs.chat_policy(now)` equals calling the function, with both calls recorded. This covers `_CallableModule.__call__`.
  - Mutation check: making `__call__` return `"off"` turns this test red, then it was reverted.
- **M-2:** test_store_ops_bind.py::test_cv_t08_23_package_reexports now also asserts `ops.record_metric_samples is herness.store.ops.metrics.record_metric_samples` and `ops.purge_metric_samples is herness.store.ops.metrics.purge_metric_samples`.
- **M-3:** the three docstrings in test_store_ops_bind.py now cite `U08-98 (cv)` instead of `IT08-04 (cv)`.
- **M-4:** docs/impl/08 §2 rows changed:
  - resilience `__init__`: "all public names of the resilience API modules of §3.3–§3.9 (not `settings`, R-03)";
  - jobs `__init__`: "all public names of the jobs modules of §3.2 (`validate`), §3.3 (`ports`) and §3.10–§3.17".
  - Both were checked against the §3 headings (3.2 Configuration models … 3.17 Status, health and resume).

### Runs
- **Tests:** tests/unit/repo, tests/unit/store/ops, tests/unit/core/jobs/test_jobs_chat_policy.py and tests/unit/core/test_exports_08.py: 573 passed.
- **Gates:** all clean.
  - ruff check: clean.
  - ruff format --check: 974 files already formatted.
  - mypy: no issues in 356 files.
  - lint-imports: 14 kept, 0 broken.
  - check_type_ownership: exit 0.
  - check_module_size: exit 0.

**Fix round 1 final commit:** 47bd5a4 `fix(jobs): T08-23 review round 1 (settings allowlist contract, minors)`. Every pre-commit hook passed, including pytest-unit, and `git status` is clean.
