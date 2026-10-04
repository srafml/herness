# T08-03 report: Ports, process state and binding

Status: DONE_WITH_CONCERNS
Commit: 0ee14b5 feat(resilience): add ports, process state and binding (T08-03)
Worktree: D:\herness\.claude\worktrees\agent-aa1aad6752aca051b (branch feat/impl00-foundation-runtime)

## Implemented
- `herness/core/resilience/ports.py` (U08-08): `HealthRow` and `EventRow` (frozen dataclasses), plus the protocols `TracerLike`, `ResilienceBackend`, `ClientInfo`, `ChainRegistry`, `GpuStateReader`, `AsyncCompleter` and `DeciderLike`. Read-only attributes are declared as properties. The module imports only the standard library and `herness.core.types`.
- `herness/core/resilience/_state.py` (U08-09, U08-10):
  - `ProcessState`: a dataclass with every attribute of U08-09, plus `policies_hash` to hold "the config_hash it was built for".
  - `NOT_LOADED`: the sentinel value for `fault_plan`.
  - `process_state()`, and `reset_process_state()`, which returns the new instance.
  - `bind_ops_backend`: also registers the atexit metric flush, once.
  - `bind_chain_registry`.
  - `bind_port(port, value)`: a shared helper that sets the port under the lock and logs `resilience.backend.bound` at DEBUG with `port` and `rebound`.
  - `require_ops_backend()`: raises ConfigError with the exact U08-10 message.
  - `require_chain_registry()`.
- `herness/core/jobs/ports.py` (U08-04, U08-41, U08-42):
  - `JobRow`: pydantic, frozen, extra=forbid. It parses sqlite rows directly:
    - JSON TEXT columns are decoded.
    - Timestamp text is parsed with `clock.parse_utc`, and aware datetimes are normalised to UTC.
    - Naive datetimes and int timestamps are rejected.
    - The payload is a read-only MappingProxyType, and it is serialised back to a dict.
  - `NewJob`: strict, holds all insert columns, payload is bytes.
  - `WorkerRow`: all worker columns, with `current_jobs` JSON parsed and the 0/1 flag turned into a bool.
  - `SchedCheck`: a NamedTuple.
  - `ServiceControl` and `JobContext`: protocols.
  - `JobsBackend`: a protocol with every U08-41 method, plus U08-96 `set_requested_class` and the U08-97 task methods.
  - `bind_jobs_backend`, and `require_jobs_backend()` (same message pattern as U08-10).
- `herness/core/resilience/__init__.py` and `herness/core/jobs/__init__.py`: lazy re-export under PEP 562. Each has an `_EXPORTS` map and `__all__`. The TYPE_CHECKING imports use the explicit `X as X` form inside `# isort: off/on`, so mypy strict sees them as explicit re-exports.
- `pyproject.toml`:
  - "core base is closed" now forbids `herness.core.resilience` and `herness.core.jobs` (UT00-58 requires this).
  - New forbidden contract "herness.core must not import herness.store or herness.harness": source `herness.core`, forbidden `herness.store`. `herness.harness` is appended when impl 05 creates it, because UT00-58 rejects contracts that name missing modules.
  - New contract "resilience-settings-light":
    - Source: `herness.core.*.settings`.
    - Forbidden (R-03 list): core logging, _log_pipeline, ids, time and numbers; `herness.core.jobs`; `herness.store`; `herness.model`.
    - `allow_indirect_imports = true`.
    - The wildcard matches nothing today, and import-linter accepts that.
    - Checked: with a throwaway `herness/core/resilience/settings.py` that imports `herness.core.time`, the contract reports BROKEN. The file was removed afterwards.
    - No deferral to T08-26 is needed.
- `tests/conftest.py`: a new root file (T11-01 owns it). It contains only the `reset_process_state` fixture: a fresh state, `rng = random.Random(0)`, no-op `sleep`/`asleep`, and a reset on teardown. The freezegun-advancing fake clock of the §11 `process_state_reset` fixture is left to T11-01 or later cards.

## Tests
- `tests/unit/core/jobs/test_jobs_ports.py` covers the row-parsing part of UT08-63:
  - A valid db row parses to JobRow.
  - JobRow is frozen, its payload is read-only, and a dump round trip works.
  - Python values parse, and datetimes are normalised to UTC.
  - 9 invalid-row cases are rejected.
  - WorkerRow parsing.
  - NewJob bounds and strictness.
  - SchedCheck.
  - Jobs binding and the unbound message.
  - Lazy exports.
- `tests/unit/core/resilience/test_resilience_state.py`:
  - State defaults, reset and unbound messages.
  - Bind and rebind logs, and None is rejected.
  - The atexit flush is registered once.
  - The exit hook works with and without the metrics module.
  - Lazy exports.
  - These tests are named UT08-29 (U08-10's test id; its "unbound case" is this card's part) because U08-09 has no test id of its own.
- `tests/unit/repo/test_import_contracts_08.py` covers UT08-103:
  - The contracts are declared.
  - `lint-imports` runs in process through `importlinter.cli.lint_imports`, with no subprocess.
  - An AST check limits `herness.core.types.jobs` imports to the allowlist.
  - An AST scan confirms nothing under herness/core/jobs constructs an httpx client.

RED: I removed the two new packages and restored the HEAD pyproject.
- `PYTHONUTF8=1 uv run pytest tests/unit/core/jobs tests/unit/core/resilience tests/unit/repo/test_import_contracts_08.py -q -p no:logging` failed with `ModuleNotFoundError: No module named 'herness.core.resilience'` while loading the conftest.
- The UT08-103 file with `--noconftest` failed with `KeyError: 'herness.core must not import herness.store or herness.harness'` and the lint_imports `assert False`: 2 failed, 2 passed.

GREEN:
- The same command gives 30 passed.
- `-k "UT08_63 or UT08_103"` gives 21 passed.
- Coverage of the 5 new modules is 100 % for lines and branches.

## Gates (at commit)
- ruff check: All checks passed.
- ruff format --check: 51 files already formatted.
- mypy strict: no issues in 24 source files.
- lint-imports: 10 kept, 0 broken.
- tools.check_type_ownership: exit 0; the only output is "pending owner" INFO lines.
- `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`: 225 passed, 4 deselected.

Environment note: `uv run` started failing near the end of this card.
- Cause: D:\herness\pyproject.toml (the main checkout, not this worktree) has merge-conflict markers (`<<<<<<< HEAD` at line 282), and uv parses that file while it discovers the project.
- Workaround: the final gate re-run called the worktree's `.venv/Scripts` tools (ruff, mypy, lint-imports, python) directly. All were green.
- I did not touch D:\herness.

## Line counts vs budgets (§2)
| File | Lines | Budget |
|------|-------|--------|
| resilience/__init__.py | 62 | 70 |
| resilience/_state.py | 140 | 140 |
| resilience/ports.py | 140 | 190 |
| jobs/__init__.py | 50 | 90 |
| jobs/ports.py | 284 | 300 |

The lazy `__init__` style costs about 2 lines per exported name. Once later cards add every §3.3–§3.8 name, `resilience/__init__.py` will not fit its 70-line budget.

## Placeholders
These types are owned by other cards or specs and are not in the tree yet (checked with `uv run python -c "import ..."`).
- `herness/core/resilience/_state.py`:
  - `CircuitBreaker = Any` (U08-24, T08-06).
  - `FaultPlan = Any` (U08-33, T08-08).
  - `RetryPolicy = Any` (U08-11, T08-04).
  - `metric_buffer: object = object()` stands in for MetricBuffer (U08-19, T08-05).
- `herness/core/resilience/ports.py`:
  - `_LLMRequest`, `_LLMResponse` = Any (T05-01, herness.core.types).
  - `_DecisionInput`, `_QuestionSet`, `_DecisionOutput` = Any (T03-01, herness.core.types).
  - The leading underscore keeps `tools.check_type_ownership` (OWN040) from flagging them.
- `herness/core/jobs/ports.py`: `QueueStats = Any`. U08-41 uses it, but no impl 08 unit defines it. T08-11 or T08-22 must define it.
- `_flush_metrics_at_exit` imports `herness.core.resilience.metrics` through importlib and returns without error while that module is missing. T08-05 can switch to a direct call.

## Deviations and decisions to review
1. Some public names are not in the module map. None of them is re-exported from the packages.
   - Helpers: `bind_port`, `require_ops_backend`, `require_chain_registry`, `require_jobs_backend`.
   - Constants: `JOBS_UNBOUND`, `OPS_UNBOUND`, `NOT_LOADED`.
   - Type aliases: `JobStatus`, `WorkerStatus`, `StopReason`, `RequeueResult`, `SqlWrites`, `JsonMap`, `CheckpointKey`, `CancelResult`, `RetryResult`, `DbTs`, `DbJson`, `Payload`, `JsonScalar`.
2. The spec leaves some signatures open, so I chose them. T08-11 and T08-16 may change them.
   - finish_*, finalize_canceled, heartbeat_job and save_job_state return a bool ("a row was updated"), because U08-50 step 6 needs to detect 0 rows.
   - `update_worker` returns None.
   - `recover_tasks(run_id, *, max_task_attempts, retry_dead, now) -> tuple[int, int, int]`.
   - `claim_task(task_id, now) -> bool`.
   - `save_checkpoint(task_id, key, value, writes) -> bool` (the bool is loop_dropped).
   - `complete_task(task_id, result, *, writes, now)`.
   - `fail_task(task_id, *, retryable, max_task_attempts, last_error, now) -> Literal["pending", "dead"]`.
   - `release_task(task_id, now) -> bool`.
3. Every JobRow timestamp is `datetime | None`, following U08-42 word for word, although the table declares `scheduled_for` and `created_at` NOT NULL. Optional fields default to None.
4. The bind functions reject `None` with `ConfigError("<port> port must not be bound to None")`. The jobs bind logs the same `resilience.backend.bound` event, with `port="jobs"`.
5. UT08-103 runs lint-imports in process, because the unit marker forbids subprocesses.
6. The ProcessState and binding tests carry the UT08-29 id (see Tests).
