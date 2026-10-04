# Review: T11-03 Fake clock

### Spec Compliance
- ✅ Spec compliant
- Deliverable `tests/support/fake_clock.py` (117 lines, budget 150) implements `FakeClock` (U11-36) with the exact signature from the unit spec: `FakeClock(start: datetime)`, `now() -> datetime`, `sleep(seconds: float) -> None`, `async asleep(seconds: float) -> None`, `advance(seconds: float) -> datetime`, `__enter__`/`__exit__`.
- Preconditions match literal unit-spec wording: naive/non-UTC `start` → `ConfigError` (`tests/support/fake_clock.py:66-69`); negative `seconds` → `ConfigError` (`:72-75`). Verified `herness.core.errors.ConfigError` exists (`herness/core/errors.py:257`).
- Postconditions verified against `herness/core/time.py`: while entered, `clock.now`/`clock.sleep`/`clock.asleep` are patched to the fake versions (`fake_clock.py:122-133`) and restored on exit (`:141-147`), matching the module's documented patch contract (`herness/core/time.py:1-5`).
- `freezegun.freeze_time(start, tick=False)` is started on `__enter__` and moved via `.move_to()` on every `advance` (`fake_clock.py:130-131, 106-109`) — confirmed live against the installed freezegun 1.5.5: `freeze_time(...).start()` returns a `FrozenDateTimeFactory` with `.move_to`, matching the implementation's types (`freezegun.api._freeze_time`, `FrozenDateTimeFactory`).
- `asleep` yields once via `await asyncio.sleep(0)` before advancing (`fake_clock.py:118-120`), matching the spec's concurrency note.
- Fixture `fake_clock` is function-scoped (default), starts `FIXTURE_START = 2026-09-01T00:00:00Z` (`:61, 150-154`) — matches U11-36.
- `threading.Lock` guards `_current`; `now()` and `advance()` both acquire it (`:93-96, 106-110`) — matches "Algorithm"/"Concurrency" rows.
- Invariant "time never goes backwards" is structurally enforced: the only mutator (`advance`) rejects negative `seconds`.
- Tests UT11-59 and UT11-60 both present and passing; re-ran independently: `7 passed in 0.05s` (well under the < 2 s acceptance bound).
- `tests/conftest.py` registers `"tests.support.fake_clock"` in `pytest_plugins` and updates the docstring accurately (`tests/conftest.py:9-13, 23`).
- Module budget respected: 117/150 lines (verified via `wc -l`).

### Strengths
- Clean, minimal implementation; docstrings on every public method state the raised error and reference the relevant spec section.
- Test file properly tagged: every test name/docstring carries `UT11-59`/`UT11-60`, `pytestmark = pytest.mark.unit` is set, matching global-constraints test-ID conventions.
- Concurrency test uses 8 threads (spec example uses "two") — a stronger, still-conformant test.
- `# type: ignore[method-assign]` comments are correctly scoped to the three monkeypatch lines, not blanket-suppressed.
- Report is honest about the mypy-scope gap (`tests/` outside `files = ["herness", "tools"]`, confirmed at `pyproject.toml:189`) rather than hiding it.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- `tests/support/fake_clock.py:112-120` — the fake `sleep`/`advance`/`asleep` only reject `seconds < 0`; the real `herness.core.time.sleep`/`asleep` they patch also enforce an upper bound (`MAX_SLEEP_S = 3600.0`, `herness/core/time.py:37-41`) and a `math.isfinite` check, raising `SchemaViolation` rather than `ConfigError`. This is a deliberate, documented, spec-conformant choice (the unit spec's precondition text names only `seconds ≥ 0, else ConfigError`), so it is not a spec violation — but it means code under test that relies on the real clock's bound/NaN rejection will not see the same rejection when `fake_clock` is active. Worth a one-line note in the module docstring for future readers; not blocking.
- `tests/support/fake_clock.py:123-125` — re-entrant `__enter__` raises `ConfigError("fake clock already entered")`, a judgment call the spec doesn't address (also flagged by the report). Reasonable and low-risk; no fixture in the diff exercises nesting, so it's untested but also unused.

### Assessment
**Task quality:** Approved
**Reasoning:** Implementation matches the unit spec's signature, preconditions, postconditions, invariants and concurrency requirements exactly; both required tests pass in well under the acceptance bound, and independent verification against the installed freezegun API and `herness.core.time`/`errors` confirms the report's claims. Only a documented, spec-conformant fidelity gap (upper-bound sleep validation) remains, which is Minor.
