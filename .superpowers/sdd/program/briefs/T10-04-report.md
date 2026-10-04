# Report: T10-04 Registry

Status: DONE_WITH_CONCERNS
Commit: 50327b4 "feat(core): add implementation registry with entry-point loading (T10-04)"
Worktree: D:\herness\.claude\worktrees\agent-a1c271915ada95249

## What was implemented

`herness/core/registry.py` (139 lines, budget 160), L0, implementing U10-23..U10-26:

- `Kind = Literal["connector", "monitoring_adapter", "decider", "llm_client", "tool", "embedder", "renderer"]`
- `register(kind: Kind, name: str) -> Callable[[T], T]` — validates name against
  `^[a-z0-9][a-z0-9_.-]{0,63}$` and kind membership (`ConfigError` otherwise), stores under
  `_REG_LOCK`; re-registering the identical object is a no-op, a different object raises
  `ConfigError("duplicate registration <kind>:<name>")`.
- `get(kind: Kind, name: str) -> Any` — checks the live registry, then `_BUILTINS`
  (`importlib.import_module` + `getattr`, `ConfigError("cannot import <module>")` on
  `ImportError`), then loads entry points once and retries, then raises
  `ConfigError("unknown <kind> '<name>'; available: <comma list>")`.
- `available(kind: Kind) -> list[str]` — loads the `herness.plugins` entry-point group once
  per process (`importlib.metadata.entry_points(group="herness.plugins")`), logging
  `registry.plugin.loaded` at WARNING (`entry_point`, `distribution`, `version`) or
  `registry.plugin.failed` at ERROR (`entry_point`, `error_type`, via `logger.exception` so
  `record.exc_info` stays unset per the TH00-01 fix in `herness.core.logging`) on a failed
  `ep.load()`, then returns the sorted union of registered and `_BUILTINS` names for `kind`.
- `_BUILTINS: dict[tuple[Kind, str], str]` — empty in this card, as the brief specifies; later
  cards (impl 01, 03, 05, 09) add their own rows.
- `reset_registry() -> None` — clears `_REGISTRY` and the entry-point-loaded flag; keeps
  `_BUILTINS`.

All registry state (`_REG_LOCK: threading.RLock`, `_REGISTRY`, `_entry_points_loaded`) is
module-level, matching the ENG §2.3 exception already used by `herness.core.logging._State`.
`_REG_LOCK` is an `RLock` because `get()` holds the lock across a call into `available()` for
the unknown-name error message.

## Files changed

- `herness/core/registry.py` (new)
- `tests/unit/core/test_registry.py` (new) — UT10-25, UT10-26, UT10-27 plus RF (coverage) tests
- `tests/security/test_st10_registry.py` (new) — ST10-46 registry part
- `tests/support/_fixture_registry_target.py` (new) — shared fixture: a real importable
  `FixtureBuiltin` class for the `_BUILTINS` lazy-import test, and duck-typed
  `FakeDistribution` / `FakeEntryPoint` / `entry_points_stub` for monkeypatching
  `importlib.metadata.entry_points`
- `pyproject.toml` — added `herness.core.registry` to the `core base is closed` contract's
  `forbidden_modules` (alongside `herness.core.settings`), so the core base modules
  (logging, _log_pipeline, types, ids, time, numbers, errors) never import registry.py back;
  required by UT00-58, which recomputes this list from the modules that exist under
  `herness/core/` and asserts it matches exactly.

## Design choices / deviations from the dispatch note

- Test location: put the test file at `tests/unit/core/test_registry.py`, matching the
  existing `tests/unit/core/` layout (test_errors.py, test_logging.py, etc.), not the flat
  `tests/unit/test_registry.py` the brief's "Common fixtures" paragraph names. The repo's
  actual layout groups impl-00/10 foundation tests under `tests/unit/core/`.
- Entry-point fixture: used the monkeypatch option (`importlib.metadata.entry_points`
  replaced with a stub returning duck-typed `FakeEntryPoint`/`FakeDistribution` objects),
  not a real dist-info on sys.path. This keeps the tests deterministic and fast, avoids
  `importlib.metadata` cache/discovery timing concerns, and still exercises the exact
  algorithm (`ep.load()` is called and its side effect — the `register()` decorator running
  — is what makes the name appear in `available()`/`get()`).
- Added five tests beyond the four named in the brief's acceptance selector
  (`test_rf_entry_point_failure_logged_and_skipped`,
  `test_rf_invalid_kind_and_name_raise_config_error`,
  `test_rf_builtin_import_failure_raises_config_error`,
  `test_rf_get_resolves_via_entry_point_after_loading`,
  `test_rf_reset_registry_keeps_builtins`) to reach the required 90%/85% coverage — the four
  named tests alone left the `registry.plugin.failed` branch, the bad-kind/bad-name
  preconditions, the built-in import-failure branch, and `get()`'s own entry-point retry
  path uncovered. Named `test_rf_*` following the existing precedent in
  `tests/unit/core/test_logging.py` (`test_rf_exception_does_not_reach_foreign_handlers`,
  `test_rf_logger_created_before_configure`).

## RED evidence

Before `herness/core/registry.py` existed, importing it failed:

```
$ uv run pytest -k "UT10_25 or UT10_26 or UT10_27 or ST10_46" -q
ERROR tests/unit/core/test_registry.py
ERROR tests/security/test_st10_registry.py
ModuleNotFoundError: No module named 'herness.core.registry'
```

The module and its tests were authored together per the implementer-rules TDD step; this
was the failing state confirmed before `herness/core/registry.py` was written.

## GREEN evidence

```
$ uv run pytest -k "UT10_25 or UT10_26 or UT10_27 or ST10_46" -q -p no:logging
....                                                                     [100%]
4 passed, 230 deselected in 0.47s

$ uv run pytest tests/unit/core/test_registry.py tests/security/test_st10_registry.py -q -p no:logging
.........                                                                [100%]
9 passed in 0.15s

$ uv run pytest tests/unit/core/test_registry.py tests/security/test_st10_registry.py \
    -q -p no:logging --cov=herness.core.registry --cov-report=term-missing --cov-branch
Name                       Stmts   Miss Branch BrPart  Cover   Missing
----------------------------------------------------------------------
herness\core\registry.py      89      0     16      0   100%
9 passed in 0.15s

$ uv run pytest -k "UT00_58" -q
.                                                                        [100%]
1 passed, 234 deselected in 0.32s

$ uv run pytest -m "(unit or integration) and not slow" -q -p no:logging
231 passed, 4 deselected in 1.97s
```

## Gate outputs

```
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
37 files already formatted

$ uv run mypy
Success: no issues found in 14 source files

$ uv run lint-imports
Contracts: 6 kept, 0 broken.
(herness layers, herness never imports app or tools, core base order,
 core base is closed, settings modules are leaves, types import only errors and ids)

$ uv run python -m tools.check_type_ownership
INFO pending owner 03
INFO pending owner 05
INFO pending owner 06
INFO pending owner 07
INFO pending owner 08
INFO pending owner 09
herness/core/registry.py:22: OWN040 Kind redefined outside core.types
exit=1
```

Every gate is clean except `check_type_ownership`, which fails with exactly one finding.
See "Concern" below.

## Line counts vs budgets

- `herness/core/registry.py`: 139 / 160 lines (module map row budget).
- No other module map budgets apply to this card's files.

## Concern (the reason for DONE_WITH_CONCERNS): OWN040 name collision on `Kind`

`tools/check_type_ownership.py` walks every `*.py` file under `herness/` and `app/`
(excluding `herness/core/types/`) and flags any top-level `class`/`def`/assignment whose
name appears in `herness/core/types/_ownership.py`'s `TYPE_OWNERS` table, anywhere in the
file, at any nesting level (U00-47 step 5(a), `docs/impl/00-foundation.impl.md:1160`). This
is a flat, whole-repository namespace, not scoped per module.

`TYPE_OWNERS` already reserves the bare name `"Kind"` exclusively for owner `"07"` (memory),
per `herness/core/types/_ownership.py`:

```python
"07": ("Layer", "Kind", "Status", "KIND_LAYER", "Provenance", "MemoryItem", ...),
```

and `docs/impl/07-memory.impl.md` U07-01 confirms this is a live, still-current requirement:
`Kind = Literal["run_summary","outcome_summary","decision_note","glossary","business_rule",
"mapping","insight","user_correction","sql_template","qa_pair","analysis_recipe"]` — a
completely different concept (a memory item's kind) from this card's `Kind` (an
implementation-registry kind: connector/monitoring_adapter/decider/llm_client/tool/
embedder/renderer). They are unrelated types that happen to share the English word "Kind".

T10-04's own binding spec (`docs/impl/10-config-security-deployment.impl.md`) requires the
symbol verbatim: U10-23's Signature row is literally `Kind = Literal["connector",
"monitoring_adapter", "decider", "llm_client", "tool", "embedder", "renderer"]`, and the
§2 module map row for `herness/core/registry.py` lists `Kind` as one of the module's public
symbols. `register`, `get` and `available` all take `kind: Kind` as their first parameter,
so this name is central to the registry's public API and will be imported
(`from herness.core.registry import Kind`) by every later card that registers a connector,
monitoring adapter, decider, LLM client, tool, embedder or renderer (impl 01, 03, 05, 09
per the brief's own U10-26 note).

I did not find a way to satisfy both the brief's verbatim signature and a clean
`check_type_ownership` run without one of:

1. Rename registry's `Kind` (e.g. to `RegistryKind`) — deviates from the brief's exact,
   verbatim signature, and would need every downstream card (impl 01/03/05/09, none of
   which exist yet) to use the new name instead of the name the spec currently gives them.
2. Edit `herness/core/types/_ownership.py` to rename or remove impl 07's `"Kind"`
   reservation — `_ownership.py` is not one of this card's files (T10-04 lists only
   `herness/core/registry.py`), owner 07 (memory) has not been implemented yet so I cannot
   judge whether renaming its `Kind` is safe, and the global-constraints rule about updating
   contracts "in the same commit" is scoped to import-linter contracts, not the ownership
   table.
3. Bypass the AST-visible top-level assignment (e.g. `globals()["Kind"] = ...`) — this
   would hide `Kind` from mypy too, breaking the actual typed public API the brief asks for,
   and is exactly the kind of gate-gaming the process rules warn against.

I judged renaming a foundational, multi-card public symbol, or editing another spec's
already-landed ownership table, to be decisions above a single card's authority, so I kept
`Kind` exactly as the brief specifies (options 1 and 2 both rejected) and flagged this
instead of inventing a resolution. Every other requested gate is clean; only this one
`OWN040` finding (`herness/core/registry.py:22`, the `Kind = Literal[...]` line) remains.

Recommendation for the coordinator: either (a) rename impl 07's memory `Kind` to
something more specific (e.g. `MemoryKind`) before T07 is implemented — it has no code yet,
so this is a documentation-only change with no runtime cost — or (b) teach
`check_type_ownership` to scope OWN040 to names actually imported from `herness.core.types`
(or to names that shadow a *used* import) rather than any same-spelled top-level name
anywhere in the tree, or (c) explicitly accept `herness.core.registry.Kind` as a documented,
narrow exception (e.g. a small allowlist in `_ownership.py` for names that are legitimately
reused outside the shared-types system).

## Card acceptance check

"tests pass" — confirmed: `uv run pytest -k "UT10_25 or UT10_26 or UT10_27 or ST10_46"` → 4
passed; full unit+integration suite → 231 passed, 0 failed, 0 regressions.

## Ruling applied (follow-up commit)

Sub-controller ruling on the OWN040 conflict above: rename the registry's Literal alias from
`Kind` to `RegistryKind` in `herness/core/registry.py` (and its `_BUILTINS` key type, the
`kind` parameter annotation on `register`/`get`/`available`, and `__all__`), instead of
touching `herness/core/types/_ownership.py` or `tools/check_type_ownership.py`.

Applied as commit 4dfea1f "fix(core): rename registry Kind to RegistryKind to clear OWN040
(T10-04)". Only the type name changed; `ConfigError` message texts are unchanged (still
`"invalid registry kind: <kind>"`, `"unknown <kind> '<name>'; available: <comma list>"`,
etc. — these use the lowercase `kind` parameter, never the type name). No test file
referenced `Kind` directly (tests pass plain string literals), so no test changes were
needed.

Gate results after the rename:

```
$ uv run python -m tools.check_type_ownership
INFO pending owner 03
INFO pending owner 05
INFO pending owner 06
INFO pending owner 07
INFO pending owner 08
INFO pending owner 09
exit=0

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
37 files already formatted

$ uv run mypy
Success: no issues found in 14 source files

$ uv run lint-imports
Contracts: 6 kept, 0 broken.

$ uv run pytest -k "UT10_25 or UT10_26 or UT10_27 or ST10_46" -q -p no:logging
4 passed, 231 deselected

$ uv run pytest -k "UT00_58" -q
1 passed, 234 deselected

$ uv run pytest -m "(unit or integration) and not slow" -q -p no:logging
231 passed, 4 deselected

$ uv run pytest tests/unit/core/test_registry.py tests/security/test_st10_registry.py \
    -q -p no:logging --cov=herness.core.registry --cov-report=term-missing --cov-branch
herness\core\registry.py: 100% line, 100% branch
```

All gates are now clean. Status upgrades to **DONE**.

**Spec erratum**: `docs/impl/10-config-security-deployment.impl.md` U10-23's Signature row,
U10-26 (`_BUILTINS: dict[tuple[Kind, str], str]`), and the §2 module map row for
`herness/core/registry.py` should read `RegistryKind` wherever they currently say `Kind`,
to match the code and avoid the OWN040 collision with impl 07's (memory) `Kind`
(`docs/impl/07-memory.impl.md` U07-01). This is a documentation-only correction; no other
card's committed code references the old name yet.
