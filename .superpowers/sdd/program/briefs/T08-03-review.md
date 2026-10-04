# T08-03 review: Ports, process state and binding

Reviewer: verify agent. Base ef03c6a, head 0ee14b5. Worktree D:\herness\.claude\worktrees\agent-aa1aad6752aca051b (read-only).

**Verdict: Approved** (no Critical or Important findings; Minor items below can be done in this card or picked up by T08-11 or T08-16)

## Gates (re-run by the reviewer at 0ee14b5)
| Gate | Result |
|------|--------|
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 51 files already formatted |
| `uv run mypy` (strict) | no issues in 24 source files |
| `uv run lint-imports` | 10 kept, 0 broken (includes "herness.core must not import herness.store or herness.harness" and "resilience-settings-light") |
| `uv run python -m tools.check_type_ownership` | exit 0 |
| `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` | 225 passed, 4 deselected |
| Focused coverage (new modules, branch) | 100 % lines and branches for all 5 modules |

uv worked in the worktree. Its only output was the `VIRTUAL_ENV` mismatch warning. The main-checkout conflict-marker problem that the report mentions did not reproduce here.

## Spec Compliance
- U08-04 (`JobContext`, `ServiceControl`) ✅
  - Both are `typing.Protocol` classes and are re-exported from `herness.core.jobs`, not from `herness.core.types` (R-02).
  - Every member and signature matches the spec. `job`, `job_id`, `kind`, `attempt`, `services` and `stop_reason` are read-only properties.
  - `gpu_scope` returns `AbstractContextManager[None]`, which a `@contextmanager`-decorated `Iterator[None]` function satisfies.
- U08-08 (`resilience/ports.py`) ✅
  - `HealthRow` and `EventRow` are frozen dataclasses with the spec fields.
  - The protocols `TracerLike`, `ResilienceBackend` (all 13 methods, with the spec signatures), `ClientInfo`, `ChainRegistry`, `GpuStateReader`, `AsyncCompleter` and `DeciderLike` are all present.
  - The module imports only stdlib and `herness.core.types`.
  - `LLMRequest`, `LLMResponse`, `DecisionInput`, `QuestionSet` and `DecisionOutput` are `Any` placeholders (program ruling).
- U08-09 (`ProcessState`, `process_state`, `reset_process_state`) ✅
  - Every attribute is present.
  - `fault_plan` defaults to the `NOT_LOADED` sentinel.
  - The `config_hash` companion is stored as `policies_hash`.
  - `rng` defaults to `SystemRandom`, and `sleep` and `asleep` default to `time.sleep` and `asyncio.sleep`.
  - Writes are guarded by the lock.
  - `CircuitBreaker`, `FaultPlan`, `RetryPolicy` and `MetricBuffer` are placeholders (program ruling).
- U08-10 (`bind_ops_backend`, `bind_chain_registry`) ✅
  - Both reject `None` and log `resilience.backend.bound` at DEBUG.
  - The atexit flush is registered once.
  - The unbound `ConfigError` message for the ops port is exact.
- U08-41 (`JobsBackend`, `NewJob`, `WorkerRow`, `SchedCheck`, `bind_jobs_backend`) ✅
  - Every listed method is present.
  - The U08-96 methods (`update_worker` with `**fields`, `set_requested_class`, `run_row`) are present.
  - The U08-97 task methods are present, with reasonable signatures because the spec leaves them open.
  - `NewJob` holds the U08-46 step-3 columns. `payload` is `bytes`, which matches `validate_payload`.
  - `WorkerRow` has every §4.1.2 column, with the JSON parsed and the flag turned into a bool.
  - `QueueStats` is `Any`. The spec never defines it (verified: its only occurrence is spec line 439).
- U08-42 (`JobRow`) ✅
  - pydantic, frozen, `extra="forbid"`.
  - The literal types come from `herness.core.types`, and `status` is `JobStatus`.
  - Timestamps are aware UTC, and naive or int values are rejected.
  - JSON TEXT columns are decoded.
  - The payload is a read-only mapping (R-42).
- UT08-63 (row-parsing part) ✅: valid rows parse, invalid literals, JSON, ts, naive and extra columns are rejected, and the frozen and read-only payload is covered.
- UT08-103 ✅
  - `lint-imports` runs in process and returns 0.
  - The core-to-store contract is declared.
  - The R-03 settings contract is declared and matches `herness.core.*.settings`. The third-party and full-allowlist part is already enforced by `tools.check_type_ownership` OWN050 (`tools/check_type_ownership.py:293-301`). I verified this.
  - An AST check covers the `herness.core.types.jobs` allowlist, and an AST scan confirms no httpx client construction.
- Import-linter contracts ✅
  - The "core base is closed" contract now names `herness.core.resilience` and `herness.core.jobs`. UT00-58 requires this.
  - **Deferring `herness.harness` is justified.** I checked the claim: `tests/unit/repo/test_import_contracts.py:64-68` asserts that every `source_modules` and `forbidden_modules` name either exists or contains `*`, so naming a missing `herness.harness` would fail UT00-58.
  - `test_ut08_103_contracts_declared` (`tests/unit/repo/test_import_contracts_08.py:42-45`) starts requiring `herness.harness` automatically once `herness/harness/__init__.py` exists. This makes the deferral self-enforcing.
- Budgets ✅

  | Module | Lines | Budget |
  |--------|-------|--------|
  | `resilience/ports.py` | 140 | 190 |
  | `resilience/_state.py` | 140 | 140 (at the limit) |
  | `jobs/ports.py` | 284 | 300 |
  | `resilience/__init__.py` | 62 | 70 |
  | `jobs/__init__.py` | 50 | 90 |

- Acceptance: the `reset_process_state` fixture is registered in the root `tests/conftest.py` ✅ (the file holds only that fixture, per the ruling).

### ⚠️ Cannot fully verify / spec gaps
- `QueueStats` has no defining unit in impl 08. T08-11 or T08-22 has to create it, and the spec should gain a unit for it.
- U08-42 types `scheduled_for` and `created_at` as `datetime | None`, but §4.1.1 declares both columns NOT NULL. The implementation follows U08-42 word for word. A spec clarification is needed.
- The U08-10 Errors row gives a single message for "any 08 function that needs an unbound port". The chain-registry path uses its own message instead (see Minor 1).
- The §11 `process_state_reset` fixture is meant to advance a freezegun fake clock. The fixture here uses no-op sleeps and leaves the fake clock to T11-01 or later cards.
- Structural conformance of the real implementations is not checkable yet: `SqliteJobsBackend`, `LLMRegistry`, `Tracer`, and the U08-85 and U08-86 concrete classes do not exist.

## Strengths
- Lazy PEP 562 `__init__` modules keep `herness.core.resilience.settings` cheap to import, and the explicit `X as X` TYPE_CHECKING re-exports keep mypy strict happy.
- `JobRow` parses sqlite rows directly (JSON TEXT and ts text) with strict aware-UTC handling. Round-trips through `model_dump` and `model_dump_json` are tested.
- The singleton swap sits behind a holder object with a lock, so no `global` statement is needed. Each bind takes the state lock.
- The contract test for the `herness.harness` deferral fails automatically once impl 05 creates the package.
- Coverage is 100 % for lines and branches. The RED evidence in the report is plausible.

## Issues

### Critical
None.

### Important
None.

### Minor
1. **Chain-registry unbound message differs from the U08-10 Errors row.**
   - Where: `herness/core/resilience/_state.py:135-140` (the `require_chain_registry` message).
   - The message is "chain registry not bound; call herness.core.resilience.bind_chain_registry()".
   - U08-10 gives one message for any unbound port. The new message is arguably more accurate, because `bind_core_backends` does not bind the chain registry.
   - The report's deviation list does not mention it. It should be recorded as a deliberate deviation.
2. **Nested payload values stay mutable.**
   - Where: `herness/core/jobs/ports.py:77-78` (`_read_only`).
   - `_read_only` wraps the payload in a shallow `MappingProxyType`, so a nested value such as `ctx.job.payload["entity"]["id"]` can still be changed.
   - `result` and `last_error` are plain mutable dicts on a "frozen" row (`:106-107`).
   - U08-04 says only that "the payload is a read-only mapping", so this meets the letter of the spec. It is still weaker than R-42's intent.
3. **Missing columns are silently accepted.**
   - In `JobRow`, every nullable field defaults to `None` (`herness/core/jobs/ports.py:105-113`). A row that lacks a column (for example a partial SELECT) parses silently instead of failing.
   - `WorkerRow` defaults `current_jobs=[]` and `faults_enabled=False` (`:147`, `:151`), although both columns are NOT NULL.
   - `extra="forbid"` catches unknown columns but not missing ones.
   - Suggestion: make these fields required with no default. Nullable columns can still accept `None`.
4. **The exit hook can hide a real import error.**
   - Where: `herness/core/resilience/_state.py:91-97`.
   - `_flush_metrics_at_exit` catches every `ModuleNotFoundError`. Once T08-05 lands, a missing dependency *inside* `metrics` would silently skip the exit flush.
   - Suggestion: check `exc.name == "herness.core.resilience.metrics"`, or have T08-05 replace the shim with a direct import as planned.
5. **Test IDs are wider than the spec rows.**
   - UT08-63 is defined for U08-53 and U08-42. The `WorkerRow`, `NewJob`, `SchedCheck`, binding and lazy-export tests also carry UT08-63 (`tests/unit/core/jobs/test_jobs_ports.py:110-187`).
   - UT08-29 is defined for U08-18 and the U08-10 unbound case. The `ProcessState` default, reset, atexit and lazy-export tests also carry UT08-29 (`tests/unit/core/resilience/test_resilience_state.py:27-150`).
   - As a result, `-k UT08_63` and `-k UT08_29` select unrelated tests.
6. **The UT08-103 AST checks can be bypassed.**
   - `_imports` skips relative imports (`node.level == 0`, `tests/unit/repo/test_import_contracts_08.py:31`), so `from ..resilience import x` in `types/jobs.py` would pass.
   - The httpx scan misses aliased module imports such as `import httpx as h; h.Client()` (`:80-83`).
7. **`save_checkpoint` has no `now` parameter.**
   - Where: `herness/core/jobs/ports.py:252-258`.
   - Every other task method takes an injected `now`, but U08-60's UPDATE sets `updated_at = :now`, so the backend would have to read the clock itself.
   - This is an inconsistent clock-injection seam that T08-16 should settle.
8. **`bind_port` accepts any value.**
   - Where: `herness/core/resilience/_state.py:100`.
   - It is a public helper typed `value: object`, so any object can be bound to any port through it. The typed `bind_*` wrappers are the intended API.
   - Suggestion: rename it to `_bind_port`, or document that it is internal to 08.

## Assessment
**Task quality:** Approved
**Reasoning:** Every unit's members and signatures match the spec, and every gate passes on the reviewer's re-run. The `herness.harness` deferral is justified by UT00-58 and enforces itself. The remaining items are hardening and spec clarifications, not defects that block the card.
