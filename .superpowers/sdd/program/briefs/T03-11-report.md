# T03-11 report: Decider protocol and wire mapping

Status: DONE_WITH_CONCERNS
Commit: 2812e9e feat(enrich): add Decider protocol and Jev wire mapping (T03-11)
Worktree: D:\herness\.claude\worktrees\agent-affa5a39a5189981a

## Implemented
- `herness/enrich/decide.py` (38 lines; budget 260 shared with T03-17): `Decider` — `typing.Protocol`, `@runtime_checkable`, attributes `name`, `version`, methods `decide(items, questions) -> list[DecisionOutput]`, `health() -> None` (U03-48).
- `herness/enrich/deciders/__init__.py` (new package, docstring only; covered by the existing `herness.enrich` layer contract; mypy `files = ["herness", "tools"]` already includes it, so pyproject.toml is unchanged).
- `herness/enrich/deciders/jev_wire.py` (275 lines; budget 280):
  - `to_wire_questions` (U03-49): bool -> `{"type":"noul","instructions"}`; choice -> `criteria` {label: description}; score -> `criteria` [4 levels]; input order kept. Choice with unresolved options (`options is None`) or > 255 options -> `ConfigError`.
  - `parse_wire_answers` (U03-50): non-object raw, unknown qid, non-object answer, unexpected fields (strict per-type allowlist: noul {noul, confidence}; choice {choice, probabilities, confidence}; score {score, legend, probabilities, confidence}), numbers not in [0, 1] (bools, NaN, inf, huge ints rejected without float overflow), key set not equal to the label set, list length not K, sum outside 1 ± 1e-3 -> `OutputValidationError("<qid>: <rule>")`, no values echoed (unknown qid reports `answers: unknown question id`). Accepts both `probabilities` shapes (object keyed by label / list in option order) per V-10; score accepts `"0"`–`"3"` keys, level-description keys, or a list of 4. Answer = argmax (ties -> first in option order); score never uses the weighted `score` field; `legend` ignored; `choice` mismatch logs `enrich.decider.choice_mismatch` DEBUG (`decider`, `question`). `Answer` validator failures are wrapped into `OutputValidationError`. Results in question order.
  - `AdaptiveLimiter` (U03-51): as specified (`asyncio.Condition`, halve for 60 s, pause until Retry-After, wait timeout = time left to `pause_until` capped at 1 s; when not paused, 1 s so a full limiter notices capacity restoring). Logs `enrich.decider.rate_limited` WARNING with `capacity` via `herness.core.logging.get_logger("enrich.decider")`. Default clock `herness.core.time.monotonic` (imported as `from herness.core import time as clock`, per global constraints).

## Deviations
1. Extra public symbols `load_wire_body(body: bytes)` and `MAX_BODY_BYTES` (not in the §2 module-map export list). Reason: ST03-08 requires "2 MB response -> OutputValidationError", but U03-50's signature takes an already-parsed Mapping, so the 1 MB cap (TH03-06) has no home in the listed functions. `load_wire_body` rejects bodies > 1 MB before parsing, invalid UTF-8/JSON, NaN/Infinity constants, deep nesting (RecursionError) and non-objects. T03-12 (OpenJev step 4/5) can use it directly. Controller may prefer moving this into T03-12's module.
2. `AdaptiveLimiter(capacity < 1)` raises `ConfigError` (spec: precondition, "Errors: none").
3. UT03-45: no decider classes exist yet; the table holds a minimal conforming stub plus non-conforming objects (program ruling).
4. UT03-47/ST03-08 use hand-built JSON payloads in the documented OpenJev 0.4.0 shapes (inline in tests); recorded fixtures `tests/fixtures/openjev/` do not exist (program ruling). The acceptance check "tests pass with the recorded fixtures" is satisfied by these payloads.
5. Test function names use lowercase IDs (`test_ut03_45_...`), matching existing tests and the global constraint example; `-k UT03_45` selects them (case-insensitive).

## Tests
- tests/unit/enrich/test_decider_protocol.py (UT03-45)
- tests/unit/enrich/test_jev_wire.py (UT03-46, UT03-47, UT03-48, PT03-05 hypothesis `@given`, same style as PT03-01 in test_decisions.py)
- tests/unit/enrich/test_adaptive_limiter.py (UT03-49, fake clock; no test sleeps. One test sets retry_after 0.01 so the limiter's own internal 10 ms wait-timeout branch is exercised.)
- tests/unit/enrich/security/test_jev_wire_security.py (ST03-08: 2 MB body, NaN/inf probabilities, extra option key/list entry, malformed bodies)

RED: `PYTHONUTF8=1 uv run pytest tests/unit/enrich -q -k "UT03_45 or ... or ST03_08"` -> 4 collection errors, `ModuleNotFoundError: No module named 'herness.enrich.deciders'` (and `herness.enrich.decide`).
GREEN: same command -> `49 passed, 72 deselected`. Coverage (branch): decide.py 100 %, jev_wire.py 100 % (159 stmts, 50 branches). UT03-49 run 5x: stable.

## Gates
- uv run ruff check . -> All checks passed
- uv run ruff format --check . -> 60 files already formatted
- uv run mypy -> Success: no issues found in 27 source files
- uv run lint-imports -> Contracts: 9 kept, 0 broken
- uv run python -m tools.check_type_ownership -> exit 0 (initial OWN041 on submodule imports fixed by importing from `herness.core.types`)
- uv run pytest -m "(unit or integration) and not slow" -q -p no:logging -> 385 passed, 4 deselected
- tools.check_module_size does not exist in the tree; line counts checked by hand (decide.py 38/260, jev_wire.py 275/280).
- Commit needed `PRE_COMMIT_ALLOW_NO_CONFIG=1` (pre-commit hook installed but no .pre-commit-config.yaml in the repo); no hooks skipped otherwise.

## Concerns
- Strict per-answer field allowlist (TH03-06 `extra="forbid"`) may reject real OpenJev responses if the server adds fields (e.g. `confidence` on noul is allowed and ignored); revisit when V-10 records the real fixture.
- jev_wire.py is at 275/280; T03-12 additions must go in their own modules.
- `load_wire_body`/`MAX_BODY_BYTES` are extra public symbols (deviation 1).

## Fix round 1
Commit: a5f6be3 fix(enrich): make limiter release cancel-safe and reject overflow floats (T03-11)

- Important 1 (slot leak on cancel): `AdaptiveLimiter.slot` now runs `self._in_use -= 1` synchronously in `finally`, then takes the lock only for `notify_all`. A cancel while it waits for a contended lock can drop only the notify. Waiters still re-poll within 1 s, so nothing leaks. On acquire, `_in_use += 1` runs only after the wait loop exits, with no await in between, so a cancel during the wait never increments the count. New tests: `test_ut03_49_cancel_while_waiting_never_takes_a_slot` and `test_ut03_49_cancel_during_release_does_not_leak_slot`. The second one forces lock contention by holding `limiter._cond`, the reviewer's reproduction.
- Minor 2: `load_wire_body` uses one `_finite` hook for both `parse_float` and `parse_constant`. `1e999`, `NaN` and `-Infinity` are rejected, and the docstring now says so. New ST03-08 params: `neg_inf` and `overflow`.
- Minor 3: not changed. `parse_wire_answers(raw, questions)` has no decider parameter (U03-50 signature), so `choice_mismatch` still logs `decider="jev_wire"`. T03-12 can add the backend name, or log from the caller.
- Minor 5: the `AdaptiveLimiter` docstring now says wait timeouts (<= 1 s) run in real event-loop time, while `reduced_until` and `pause_until` follow the injected `clock`.
- Minor 1 and Minor 4: left as they are, per the controller.

RED against the old jev_wire.py (the new tests, run on the HEAD~ implementation): 2 failed: `test_st03_08_malformed_body_rejected[overflow]` and `test_ut03_49_cancel_during_release_does_not_leak_slot` (the latter with TimeoutError, meaning the slot leaked).
GREEN: card tests `53 passed, 72 deselected`. jev_wire.py branch coverage is 100 % (162 stmts, 52 branches). Full `-m "(unit or integration) and not slow"`: 389 passed, 4 deselected.
Gates, all run via `.venv/Scripts/python.exe -m` (lint-imports via `.venv/Scripts/lint-imports.exe`):
- ruff check: clean.
- ruff format --check: 60 files already formatted.
- mypy: no issues in 27 files.
- lint-imports: 9 kept, 0 broken.
- check_type_ownership: exit 0.

jev_wire.py is 280 lines, exactly at its budget of 280.
