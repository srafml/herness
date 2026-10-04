# T03-11 review: Decider protocol and wire mapping (base 602bf07, head 2812e9e)

**Verdict: Needs fixes.** There is one Important finding: `AdaptiveLimiter.slot` can leak `in_use` when the task is cancelled. Everything else is spec-compliant.

### Spec Compliance
- U03-48 `Decider` ✅ Uses `typing.Protocol` with `@runtime_checkable`. Has `name`/`version`, `decide(items, questions) -> list[DecisionOutput]` and `health() -> None`. Defined in `herness/enrich/decide.py` (38/260 lines, budget shared with T03-17).
- U03-49 `to_wire_questions` ✅ Maps bool to noul, choice to `criteria` {label: desc} and score to `criteria` [4 levels], in input order. More than 255 options raises ConfigError. Unresolved options also raise ConfigError, which is a sensible reading of the precondition.
- U03-50 `parse_wire_answers` ✅
  - Unknown qid is rejected.
  - noul must be in [0, 1]. The distribution is true/false, the answer is true when p >= 0.5, and backend_confidence is None.
  - Both `probabilities` shapes are accepted (V-10). The key set must equal the label set and a list must have length K.
  - Values must be in [0, 1]. bool, NaN, inf and 10**400 are rejected without float overflow.
  - `fsum` must be 1 +/- 1e-3.
  - The argmax is recomputed, and ties go to the first option in option order.
  - A `choice` mismatch logs DEBUG and the argmax wins.
  - Score accepts "0"-"3" keys, level-description keys (with a duplicate-description guard) or a list of 4. The weighted `score` field is never used and `legend` is ignored.
  - A `ValidationError` from `Answer` is wrapped into `OutputValidationError`.
  - Absent questions are absent from the result, and the result is in question order.
  - Error messages are `"<qid>: <rule>"` and never echo values. An unknown qid reports `answers: ...`, so the untrusted id is not echoed.
- U03-51 `AdaptiveLimiter` ✅ on algorithm, ❌ on cancellation robustness (Important 1).
  - Capacity is halved to `max(1, c//2)` while `clock() < reduced_until`, with `reduced_until = now + 60`.
  - `pause_until` is `max(pause_until, now + retry_after)`.
  - A WARNING `enrich.decider.rate_limited` is logged with `capacity`.
  - The wait uses a Condition with timeout = time left to `pause_until`, capped at 1 s.
  - On exit it decrements and calls `notify_all`.
  - No notifications are lost: the release path notifies, and pause expiry and capacity restoration are covered by the <= 1 s timed re-check.
- Tests ✅ UT03-45 to UT03-49, PT03-05 and ST03-08 are all present. Names carry the IDs, docstrings start with the ID, and each file sets `pytestmark`. I re-ran them: `49 passed` with `-W error`, no warnings.
- Line budgets ✅ `jev_wire.py` is 275/280 and `decide.py` is 38/260.
- Deviations judged:
  - Deviation 1, extra public `load_wire_body`/`MAX_BODY_BYTES`: **accepted**. ST03-08 needs the 2 MB -> `OutputValidationError` check to live somewhere, and U03-50 takes an already-parsed Mapping. `jev_wire` is the module the Jev-shaped deciders share, so it is the right home. The §2 export list should be amended when the spec is next touched. This is a Minor finding and does not block.
  - Deviation 2, ConfigError on capacity < 1: **accepted**. It enforces a stated precondition and matches how the program handles construction-time misconfiguration.
  - Deviations 3 and 4 (stub decider, hand-built payloads) and 5 (lowercase IDs) are program rulings or constraints, not defects.
- ⚠️ Cannot verify from the diff:
  - Whether real OpenJev 0.4.0 responses carry fields outside the strict per-type allowlist (`jev_wire.py:40-44`). That waits on V-10 and the recorded fixtures.
  - Whether the `enrich.decider.choice_mismatch` `decider` field should name the real backend. See Minor 3.
  - The gates (ruff, mypy, lint-imports, type ownership, full-suite run) are taken from the report.

### Adversarial checks run (PT03-05 by hand)
I ran about 100k random nested answers from a scratch script against questions that include:
- a dynamic choice question with `options=None`,
- a score question whose levels are a permutation of "0"-"3",
- a score question with duplicate levels.

Other inputs: 10**400, 1e308*10, nested `choice` values, `confidence: None`/`True`, a sum of 0.9995, and the `Answer` sum-tolerance boundary. Only `OutputValidationError` escaped, and no values appeared in the messages.

`load_wire_body` checks:
- Deep nesting, a 5000-digit integer (int max-str-digits), invalid UTF-8 and NaN raise `OutputValidationError`.
- `1e999` passes as `inf` (Minor 2).
- A UTF-8 BOM and a lone-surrogate string are accepted, which is harmless here.

### Strengths
- The parser is compact, strict and total: every path ends in `_fail` or a validated `Answer`. Using `NoReturn` helpers keeps it readable within budget.
- The number check is careful. It rejects bools and non-finite values, and it compares ints before converting to float, so huge ints cannot overflow.
- The score description-key shape is guarded against duplicate level descriptions.
- The PT03-05 strategy is well aimed: structured answers with the real field names reach deep branches, not just the top-level type check.
- The UT03-49 test checks exact concurrency counts (4 -> 4 with 5 acquired -> 8 after 60 s) without real sleeps, and asserts the WARNING event with its field.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **`herness/enrich/deciders/jev_wire.py:268-275`: a cancelled task can leak a slot, and the limiter can then deadlock.**
   - What happens: the release path is `finally: async with self._cond: self._in_use -= 1`. If the condition lock is contended when a task leaves its slot, `acquire()` suspends; contention happens routinely right after `notify_all`, when the woken waiters queue on the lock. A `CancelledError` delivered during that suspension skips both the decrement and `notify_all`. A job timeout or TaskGroup cancel landing after the body completed is enough.
   - Reproduced: with capacity 1, holding the condition lock while a holder exits and then cancelling the holder once leaves `in_use == 1` for good. Every later `slot()` then blocks forever, re-polling each second.
   - Why it matters: the leak is permanent and silent, and it wears the limiter down to zero concurrency.
   - Fix: decrement `self._in_use` synchronously before awaiting the lock (single event loop, so no lock is needed), then notify under the lock. A cancel there only costs a wake-up, which the 1 s re-check covers.
   - Add a UT03-49 test that cancels a task while it is releasing.

#### Minor (Nice to Have)
1. `jev_wire.py:28-34`: `load_wire_body` and `MAX_BODY_BYTES` are exports that are not in the §2 module map (accepted deviation). Record them in the spec's §2 row.
2. `jev_wire.py:88-102`: `load_wire_body` rejects the `NaN`/`Infinity` literals but accepts overflowing floats (`1e999` -> `inf`), because `parse_float` is not overridden. `parse_wire_answers` still rejects them, so there is no exposure. The docstring's "rejects ... NaN/Infinity" could mislead a future caller that uses `load_wire_body` output directly. Consider `parse_float` with a finiteness check, or narrow the docstring.
3. `jev_wire.py:186`: `choice_mismatch` logs `decider="jev_wire"`, a module name rather than the backend (`openjev`/`jev`/`laya`). The spec field `decider` is presumably meant to name the backend. An optional `decider` parameter, or logging from the caller, would fix it; T03-12 can decide.
4. `jev_wire.py:40-44`: the strict per-answer field allowlist goes beyond U03-50's literal algorithm. It is justified by TH03-06 `extra="forbid"`, but it could reject valid OpenJev responses that carry an extra field such as `reasoning`. Re-check once V-10 freezes the fixture (the report already raises this).
5. `jev_wire.py:239-275`: the limiter's `asyncio.wait_for` timeouts run in real loop time while `pause_until` is computed from the injected clock. This is fine with the default monotonic clock, but only approximate for a fake clock. Consider a short docstring note.
6. `tests/unit/enrich/test_adaptive_limiter.py`: no test covers cancellation during the wait or the release (see Important 1).

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The parser, wire mapping and protocol match U03-48 to U03-50 exactly, and they held up under adversarial fuzzing: only `OutputValidationError` escaped, with no values echoed. The limiter's release path can permanently leak a slot when a task is cancelled while waiting for the lock. That is a small, local fix plus one test.


---

## Re-review round 1 (fix commit a5f6be3, diff 2812e9e..a5f6be3)

**Verdict: Approved.** No Critical or Important findings remain open.

### Findings status
- **Important 1 (slot leak when a task is cancelled on release): Resolved.**
  - The fix is at `jev_wire.py:276-280`. The slot is now freed synchronously before the lock is taken, and `notify_all` still runs under the lock.
  - I re-ran my forced-contention probe (hold the lock, let the holder exit its body, cancel it once). `in_use` was 0 afterwards, and the next `slot()` got a slot immediately.
  - Two new tests cover this: `test_ut03_49_cancel_while_waiting_never_takes_a_slot` and `test_ut03_49_cancel_during_release_does_not_leak_slot`. The second one would fail on the old code, because its 0.5 s timeout would expire.
- **Minor 2 (`1e999` accepted as `inf`): Resolved.** `parse_float` and `parse_constant` now both go through `_finite`.
  - Rejected: `1e999`, `-1e999`, `NaN` and `-Infinity`.
  - `1e-999` underflows to 0.0 and is accepted, which is correct.
  - Normal floats and ints are unchanged.
  - The ST03-08 parametrisation gained `neg_inf` and `overflow` cases.
- **Minor 5 (wait timeouts run in real time while pauses use the injected clock): Resolved.** A docstring note was added.
- **Minor 3 (`decider="jev_wire"` in the mismatch log): accepted as kept.** U03-50 has no decider parameter; revisit in T03-12 if needed.
- **Minor 1 and Minor 4: recorded only.** They are unchanged and still open for the spec edit and for V-10.
- **Minor 6 (no cancellation tests): Resolved** by the two new tests.

### Regression check: no new slot or wake-up bug
- **No over-admission.** The decrement happens in the same event-loop step as the body's exit, and there is no suspension point between them. A newcomer that takes the freed slot before `notify_all` still sees an accurate `in_use`.
- **Prompt wake-up.** A blocked waiter is woken by `notify_all` in 0.000 s, not by the 1 s re-poll.
- **Stress probe.** Capacity 4, 400 tasks, 150 random cancels across the wait, body and release phases, run 3 times. Peak concurrency was 4 and the final `in_use` was 0 each time.
- **Card tests.** 53 passed with `-W error`; the 4 extra tests are the two cancel tests and the two new ST03-08 body cases.
- **Parser fuzz.** A further 50k random answers raised only `OutputValidationError`.
- **Line budget.** `jev_wire.py` is now 280/280. It is at the budget, not over it. There is no headroom left, so T03-12 must put its code in its own modules.

### New Minor (non-blocking)
- `tests/unit/enrich/test_adaptive_limiter.py` (the release-cancel test): the test reaches the private `limiter._cond` to force lock contention. That is acceptable for a white-box concurrency test, but it breaks if the internals are renamed.

**Task quality:** Approved
**Reasoning:** The slot leak is fixed simply and correctly, and it is covered by a test that fails on the old code. The probes found no over-admission, lost wake-up or leak under random cancellation, and the parser hardening is complete.
