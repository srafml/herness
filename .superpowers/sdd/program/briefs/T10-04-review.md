# Review: T10-04 Registry

## Spec Compliance

- ✅ `RegistryKind` (spec's `Kind`, renamed per sub-controller ruling to clear OWN040) = `Literal["connector", "monitoring_adapter", "decider", "llm_client", "tool", "embedder", "renderer"]` — `herness/core/registry.py:30-32`.
- ✅ `register(kind, name)` — name validated against `^[a-z0-9][a-z0-9_.-]{0,63}$` via `_NAME_RE.fullmatch` (anchors implicit in `fullmatch`, functionally equivalent to the spec's anchored regex) — `registry.py:36,50-56`; kind validated against the closed set — `registry.py:33-35,51-53`; both raise `ConfigError` — `registry.py:69-71`.
- ✅ Identity-based duplicate detection: same object is a no-op, different object under the same `(kind, name)` raises `ConfigError("duplicate registration <kind>:<name>")` — `registry.py:59-66`. Verified by `test_ut10_26_...` (`tests/unit/core/test_registry.py:345-362`).
- ✅ `get(kind, name)` follows the exact U10-24 algorithm: live registry → `_BUILTINS` lazy import (`importlib.import_module` + `getattr`, stored via `_store` so a self-registering decorator's identity match is a no-op) → load entry points once, retry → `ConfigError("unknown <kind> '<name>'; available: <comma list>")` — `registry.py:107-130`. Import failure of a built-in wrapped as `ConfigError("cannot import <module>")` from the `ImportError` — `registry.py:117-121`.
- ✅ `available(kind)` — loads the `herness.plugins` entry-point group once per process, logs `registry.plugin.loaded` at WARNING with `entry_point`, `distribution`, `version` on success, `registry.plugin.failed` at ERROR (via `logger.exception`) on a failed `ep.load()`, and returns the sorted union of registered ∪ `_BUILTINS` names for `kind` — `registry.py:85-104,133-139`. Idempotency verified by `test_ut10_27_...` (`test_registry.py:365-393`, second `available()` call asserts no repeated log line).
- ✅ `_BUILTINS: dict[tuple[RegistryKind, str], str]` starts empty, as the brief specifies for T10-04 — `registry.py:44`.
- ✅ `reset_registry()` clears `_REGISTRY` and the entry-point-loaded flag, keeps `_BUILTINS` — `registry.py:142-147`. Verified by `test_rf_reset_registry_keeps_builtins` (`test_registry.py:450-457`).
- ✅ Thread safety: `_REG_LOCK` is a `threading.RLock`, held across `_store`, `get`, `available`, `reset_registry`; `_load_entry_points` is only ever called from within an already-locked section, and its own re-entrant calls into `_store` (via a self-registering decorator) are safe under the same thread because the lock is reentrant — `registry.py:42,59-147`.
- ✅ Entry-point group name `herness.plugins` matches spec exactly — `registry.py:37`.
- ✅ Module map budget: 160 lines; actual 139 lines (confirmed via `wc -l`) — within budget.
- ✅ `pyproject.toml`: `herness.core.registry` added to the "core base is closed" contract's `forbidden_modules`, alongside `herness.core.settings` — `pyproject.toml:259`. Confirmed via `uv run lint-imports` (6 contracts kept, 0 broken) and `uv run python -m tools.check_type_ownership` (exit 0, only expected "pending owner" INFO lines) re-run in the worktree.
- ✅ Test selector `-k "UT10_25 or UT10_26 or UT10_27 or ST10_46"` re-run in the worktree: 4 passed, 231 deselected — matches the report's GREEN evidence.
- ✅ `RegistryKind` naming and its rationale are covered by the sub-controller ruling (OWN040 collision with impl 07's `Kind`); not re-flagged here, and the code, `__all__`, `_BUILTINS` key type, and all three function signatures consistently use the renamed alias — `registry.py:28,30,44,69,107,133`.
- ✅ ST10-46 (registry part only, per ruling (c)): WARNING `registry.plugin.loaded` verified end-to-end with a fixture plugin distribution — `tests/security/test_st10_registry.py:210-235`. Doctor `plugins` WARN listing is correctly out of scope for this card.

⚠️ Cannot verify from diff/without re-running full suite:
- Coverage numbers (100% line/branch on `herness/core/registry.py`) are taken from the report's pasted `--cov` output; not independently re-run (out of scope per reviewer rules — a coverage run isn't a "focused test for a specific doubt").
- `ruff check`, `ruff format --check`, and `mypy --strict` full-repo runs are taken from the report; not independently re-run.

## Strengths

- Faithful, line-by-line match to U10-23–U10-26's algorithms, including the subtle identity-no-op behavior when a `_BUILTINS` import's own decorator has already registered the object before `get()`'s explicit `_store` call.
- Good defensive handling of `ep.dist is None` in `_dist_info` (`registry.py:80-82`), not required by spec text but prevents a crash on a genuinely dist-less entry point without contradicting the spec.
- Test suite is well organized: the three named UT rows plus targeted `test_rf_*` tests close every algorithm branch (entry-point failure, invalid kind/name, builtin import failure, entry-point retry path in `get()`, `reset_registry` semantics) — matches the existing `test_logging.py` `test_rf_*` precedent.
- The `_fixture_registry_target.py` duck-typed `FakeEntryPoint`/`FakeDistribution` fixture is clean, reusable, deterministic, and avoids real `sys.path` dist-info gymnastics while still exercising the true `ep.load()` code path.
- The OWN040 conflict was surfaced transparently in the build report with a clear options analysis, rather than silently gamed (e.g., no `globals()[...]` trick was used), and the follow-up rename commit is minimal and scoped (type name only, no error-message text changes, no test changes needed since tests use string literals).

## Issues

### Critical (Must Fix)
None.

### Important (Should Fix)
None.

### Minor (Nice to Have)
- `_KINDS: Final[frozenset[str]] = frozenset({...})` (`registry.py:33-35`) duplicates the same seven string literals already present in the `RegistryKind` Literal definition three lines above (`registry.py:30-32`). The two can drift if a future kind is added to one but not the other (a silent validation bug — `_validate` would accept/reject differently than the type declares). Using `typing.get_args(RegistryKind)` to derive `_KINDS` at import time would remove the duplication. Not currently broken since both lists are set once in this same card and match.
- `_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")` (`registry.py:36`) omits the `^`/`$` anchors that the spec's Preconditions row states verbatim (`^[a-z0-9][a-z0-9_.-]{0,63}$`). Functionally equivalent given `.fullmatch()` is used at both call sites (`registry.py:53`, and the same in `get`'s `_validate` call), so no behavioral gap; flagged only because the spec's preamble treats regexes as literal, copy-verbatim identifiers.

## Assessment

**Task quality:** Approved

**Reasoning:** The implementation matches U10-23–U10-26 and the (brief-omitted but spec-confirmed) U10-24/U10-25/UT10-26 requirements exactly, including thread-safety, identity-based dedup, lazy `_BUILTINS` import, idempotent entry-point loading with correct WARNING/ERROR logging, and `reset_registry` semantics. The one real risk — the `Kind`/`RegistryKind` OWN040 collision — was surfaced honestly, resolved per an explicit sub-controller ruling, and re-verified clean (`check_type_ownership` exit 0, `lint-imports` 6/6 kept, target tests 4/4 passed, all re-run directly in the worktree during this review). Only cosmetic/DRY-nit findings remain.
