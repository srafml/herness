# Report: T11-03 Fake clock

Status: DONE

## Commit
`79f73d6` — feat(test-infra): add FakeClock fixture for herness.core.time (T11-03)

## What was implemented
- `tests/support/fake_clock.py` (117 lines, budget 150): `FakeClock` class (U11-36) and
  the `fake_clock` pytest fixture (function-scoped, start `2026-09-01T00:00:00Z`,
  exposed as `FIXTURE_START`).
  - `FakeClock(start)`: rejects a non-UTC-aware `start` with `ConfigError`.
  - `now() -> datetime`, `advance(seconds) -> datetime`, `sleep(seconds) -> None`,
    `async asleep(seconds) -> None` — all reject negative `seconds` with `ConfigError`
    (`SchemaViolation`/`ConfigError` from `herness.core.errors`; used `ConfigError` per
    the unit spec's literal precondition text).
  - `asleep` does `await asyncio.sleep(0)` (a single yield) before advancing, per spec.
  - `__enter__`/`__exit__`: saves and restores `herness.core.time.now/sleep/asleep`
    module attributes (imported as `from herness.core import time as clock`, per
    ICN003), and starts/stops `freezegun.freeze_time(start, tick=False)`, moving the
    freezer with `move_to` on every `advance` — all inside the same `threading.Lock`
    that guards `_current`, so freezegun and the internal clock can never observe
    different values or apply advances out of order under concurrency.
  - Time only moves forward (advances are `seconds >= 0`, enforced).
- `tests/conftest.py`: added `"tests.support.fake_clock"` to `pytest_plugins` and
  updated the docstring (fake_clock is no longer in the "remaining" list, since this
  card lands it); no other change.
- `tests/unit/support/test_fake_clock.py`: 7 test functions covering UT11-59 and
  UT11-60, plus two extra precondition-rejection tests reusing the UT11-59 ID (several
  functions may share an ID, per global-constraints) and an `asleep` no-block test.

## RED evidence
Command (with `tests/support/fake_clock.py` temporarily moved aside):
```
PYTHONUTF8=1 uv run pytest tests/unit/support/test_fake_clock.py --select-test-ids=UT11-59,UT11-60 -q -p no:logging
```
Output (tail):
```
ImportError: Error importing plugin "tests.support.fake_clock": No module named 'tests.support.fake_clock'
```

## GREEN evidence
Command:
```
PYTHONUTF8=1 uv run pytest tests/unit/support/test_fake_clock.py --select-test-ids=UT11-59,UT11-60 -q -p no:logging
```
Output:
```
.......                                                                  [100%]
7 passed in 0.15s
```

Acceptance check from the card, timed end-to-end:
```
PYTHONUTF8=1 uv run pytest --select-test-ids=UT11-59,UT11-60 -q -p no:logging
```
```
.......                                                                  [100%]
7 passed, 573 deselected in 0.33s
```
(elapsed_ms=1103 including uv/Python startup overhead; pytest's own reported runtime is
0.33 s — well under the < 2 s bound.)

## Gate outputs
- `uv run ruff format .` (scoped to changed files): no changes needed after fixup.
- `uv run ruff check --fix .` (scoped to changed files): 1 finding (PLC0415, imports
  not at top-level in the test file) — fixed by moving the imports to the top; 0
  remaining.
- `uv run mypy`: `Success: no issues found in 35 source files` (mypy's `files` list is
  `["herness", "tools"]`; `tests/` is out of scope, so `fake_clock.py` itself isn't
  type-checked by this gate — noted as a concern below, matches the repo's existing
  convention for all `tests/support/*` modules).
- `uv run lint-imports`: `Contracts: 8 kept, 0 broken.`
- `uv run python -m tools.check_type_ownership`: clean (only pre-existing `pending
  owner 06/07/09` info lines, unrelated).
- `uv run python -m tools.check_module_size`: no output (clean); `fake_clock.py` is
  117 lines against a 150-line module-map budget.
- `uv run pytest --collect-only -q --require-test-ids`: exit 0, `580 tests collected`,
  no untagged tests reported.
- `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`:
  `573 passed, 1 skipped, 5 deselected, 1 xfailed in 6.41s` (the skip and xfail are
  pre-existing and unrelated to this change — `coverage.json not provided` and a
  known `check_traceability` doc-defect xfail).

## Deviations from the brief
None. Used `ConfigError` (not `SchemaViolation`) for all precondition violations, per
the unit spec's literal wording ("`start` timezone-aware UTC, else `ConfigError`";
"`seconds ≥ 0`, else `ConfigError`").

## Concerns
- `tests/` is outside mypy's `files` scope (`["herness", "tools"]`), so
  `fake_clock.py`'s two `# type: ignore[method-assign]`-free module-attribute
  reassignments (`clock.now = self.now`, etc.) are untyped by the strict gate. This
  matches every other `tests/support/*` module in the tree today; flagging in case the
  controller wants `tests/support/` added to mypy's `files` list in a later card.
- `FakeClock.__enter__` raises `ConfigError` on re-entry (nesting) as a defensive
  guard; the brief/spec is silent on this case, so it's a judgment call, not a
  contradiction.

Report path: D:\herness\.superpowers\sdd\program\briefs\T11-03-report.md
