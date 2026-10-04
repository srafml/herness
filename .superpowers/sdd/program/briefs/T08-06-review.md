# T08-06 review: Circuit breakers and probes (head 3f868a9, base a59bb45)

### Spec Compliance
- ❌ Issues found: 1. U08-24 steps 3-4 as built let a second process claim a probe seconds after another process re-opened the breaker, because the claim uses a `probe_due` computed from a cached row that can be up to 5 s old. Details under Important I-1. The spec text allows this reading (plan-mandated in part), but it weakens TH08-05.

| Unit / test | Result | Note |
|---|---|---|
| U08-23 `breaker_transition` | ✅ | All 7 rules plus the no-op rows match; `updated_at = now` on every result; `last_error` set on failure and force_open. |
| U08-23 `probe_due` | ✅ | `min(cooldown_s * 2**(trips-1), cooldown_max_s)`; trips 0 is clamped to 1 as a defensive default. |
| U08-24 `CircuitBreaker` | ✅ with I-1 | 5 s cache, 600 s stale half-open, write-through `health_apply`, claim, only `SourceUnavailable`/`ModelUnavailable` count, redact before the 500-char cut, `reason=auth`. |
| U08-25 `breaker` | ✅ | `fullmatch` on the two alternatives; `ConfigError`; the instance is created under `ProcessState.lock`. |
| U08-26 `guard` | ✅ | `CircuitOpen("circuit open: <key>", key, retry_at = retry_at() or now + cooldown_s)`. |
| U08-27 `register_probe` / `run_due_probes` | ✅ | Open rows, due check, missing fn skipped, fail-closed off-network rule (ruling 2), claim, 30 s timeout, substitute `ModelUnavailable("probe failed: <Type>")`. |
| UT08-12 | ✅ | 14 rows + missing row; mutations killed |
| UT08-13 | ✅ | trips 1..8 -> 60,120,240,480,900... |
| UT08-14 | ✅ | |
| UT08-15 | ✅ | |
| UT08-16 | ✅ | spy: 1 get over 100 allow() in 4 s; write at once |
| UT08-17 | ✅ | |
| UT08-18 | ✅ | |
| UT08-19 | ✅ | 601 True / 599 False |
| UT08-104 | ✅ | |
| IT08-01 | ✅ | 100 keys, one race each (card: "over 100 races") |
| BT08-05 | ✅ (ruling) | Otherwise honest: both sides use the same wall clock and the timer starts after the commit returns. |

Controller rulings were checked and are implemented as ruled: `_call_with_timeout` is marked `# T08-07` (breaker.py:312); the model probe fails closed (breaker.py:297-309); the exports follow the callable-module precedent (`__init__` getattr hands out the module; `resilience.breaker is bmod`, and `resilience.breaker(k)` works); `_state.py` now uses a TYPE_CHECKING import.

- ⚠️ Cannot verify / interpretation notes:
  - `open + force_open` updates `last_error`: this follows the general sentence of U08-23 rather than the "(failure)" parenthetical in rule 7. Acceptable.
  - `half_open + success` clears `opened_at`, which matches `health_reset`. `force_open` and `half_open + failure` leave `failures` unchanged, as the spec lists no change. Acceptable.
  - The `record_success` hot path does one `health_get` when the cache has expired (at most once per 5 s). The spec says "cached row"; acceptable.
  - A caller-path probe that fails with a non-counting class (`RateLimited`, `QueryError`) leaves the row `half_open` until the 600 s stale rule. This is spec behaviour; U08-28 should handle it.
  - BT08-05 measures the ~4 s phase (A opens 1 s into B's cache window), not the 5 s + read worst case. Accepted per ruling.

Evidence from my own runs:
- Card tests: 58 passed, no warnings.
- Coverage: breaker.py 100 % line / 100 % branch; `_state.py` and `__init__.py` 100 % over tests/unit/core/resilience (394 passed).
- Gates: ruff check and format clean; mypy clean; lint-imports 13 kept / 0 broken.
- Sizes: breaker 373/380, `__init__` 92/120, `_state` 138/140.
- Mutation spot-checks: 18 mutants in a scratch copy, 14 killed. The 4 survivors are the redact/cut order, the run_due_probes `<` vs `<=` boundary, the half-open `reason`, and `kinds[:]` vs `extend` (retry-only, untestable). They are listed under Minor.

### Strengths
- The state table is pure and small, and its tests are exhaustive and parametrised, including the postcondition checks.
- Events and metrics are emitted after `health_apply` or the claim returns, never inside `fn`, never under `self._lock`, and never under `ProcessState.lock` (the locks at breaker.py:261, 284 and 353 guard dict ops only). `kinds[:] = emitted` keeps only the last attempt when `run_write` retries.
- The hot path (`allow` on a fresh closed cache; `record_success` on a closed row with 0 failures) does no I/O and no `get_config`.
- Cross-process tests use real spawned processes on one SQLite file, with a barrier start.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
- **I-1 (partly plan-mandated): a probe is claimed early from a stale cached row, which defeats the exponential cooldown across processes.**
  - Location: breaker.py:185-193 (`allow` computes `due` from `_row()`, up to 5 s old) and breaker.py:198 (that `due` is passed to `health_claim_probe`, whose SQL trusts `:probe_due`).
  - Reproduced in the scratch copy with a test (sketch below): B caches the open row (trips 1) 3 s before it is due. A wins the probe at the due time and fails it, so the row re-opens with trips 2 and a 120 s cooldown. One second later, `B.allow()` returns True and claims a second probe.
  - B also emits `breaker_half_open` with the stale `trips: 1` (breaker.py:201-203); the log shows `detail={'trips': 1}` right after the `trips: 2` open.
  - Any probe that fails fast, such as a supervisor `run_due_probes` against a refused connection, opens a <= 5 s window after every re-open for every other process. That is exactly the hammering TH08-05 is meant to stop.
  - Fix (local to breaker.py): in `allow()`, when the row is `open`, the probe looks due and the cache was read before `due`, re-read with `health_get` and recompute `due` before claiming. Build the claimed row and the event detail from that fresh row. This adds one read per probe attempt, off the hot path. The stronger alternative is `AND opened_at = :opened_at` in `_CLAIM_PROBE`, but that is a store change for the controller to rule on.
  - Add a regression test (sketch):

        _open("model:m", now, trips=1); b = CircuitBreaker("model:m"); env.advance(57); b.state()
        env.advance(3); a = CircuitBreaker("model:m"); assert a.allow(); a.record_failure(ModelUnavailable("x"))
        env.advance(1); assert b.allow() is False

#### Minor (Nice to Have)
- **M-1:** No test pins "redact, then cut to 500". The mutant `redact_text(str(err)[:500])` survives (test_resilience_breaker.py:280-284 uses an identity redactor). Fix: add a case with `test_redactor` and an error text holding a secret, with the text > 500 chars and the secret straddling the cut, and assert the secret is absent.
- **M-2:** The `run_due_probes` due boundary is untested. The mutant `now <= due` at breaker.py:356 survives, and the spec says `probe_due <= now` runs. Fix: add a row opened exactly `cooldown_s` ago and assert it is probed.
- **M-3:** One bad `source_health` key aborts the whole probe tick. breaker.py:358 calls `breaker(row.source)`, which raises `ConfigError` for a key that fails the regex (for example a hand-edited or legacy row), and every later row is skipped. Fix: check `_KEY_RE.fullmatch(row.source)` in the loop, then `continue` with a debug log.
- **M-4:** `_network_ok` swallows every `Exception` silently (breaker.py:305-308). Fail-closed is ruled, but a programming error would be invisible. Fix: log `resilience.probe.skipped` at debug with the error class name, or narrow the handler to `KeyError | HernessError`.
- **M-5:** Cache writes are last-writer-wins across threads (breaker.py:161, 202, 240). Two concurrent `_apply` or claim calls can leave the older row cached for up to 5 s. Fix (optional): only overwrite the cache when `row.updated_at >= cached.updated_at`.
- **M-6:** The half-open event `reason="probe"` is not asserted; that mutant survives. Fix: assert `"reason":"probe"` in `test_ut08_19_open_probe_claimed_once`.
- **M-7:** `_EXPORTS` entries are not in sorted order (`__init__.py`: `CircuitBreaker` sits before `ChainRegistry`, and `breaker` before `bind_ops_backend`). This is cosmetic because `__all__` is sorted. Fix: reorder.

### Assessment
**Task quality:** Needs fixes

**Reasoning:** The state table, cache, registry, guard and probes are correct, well tested (100 % line and branch coverage), within budget and correctly layered. However, the cached-`probe_due` claim (I-1) lets other processes take a second probe seconds after a re-open, which undermines TH08-05's "single probe claim with exponential cooldown". It needs a small local fix and a regression test (or a controller ruling that accepts it as plan-mandated).


---

## Re-review round 1 (head 6c19054; fix commit over 3f868a9)

Scope: the ruled fixes I-1, M-1, M-2, M-3, M-4, M-6 and M-7. M-5 and the claim-SQL `AND opened_at` hardening are parked by ruling.

Evidence from my runs:
- Tests: `tests/unit/core/resilience` + IT08-01/BT08-05 give 400 passed, 1 skipped (pre-existing), no warnings.
- Coverage: breaker.py 100 % line / 100 % branch (254 stmts, 66 branches).
- Gates: ruff check and format clean; configured mypy (`herness`, `tools`; 183 files) clean; lint-imports 13 kept / 0 broken.
- Sizes: breaker.py 379/380, `__init__.py` 92/120.
- Worktree: `git status` clean.
- Mutants, all run in a scratch copy: all 7 fix-round mutants are killed, each by its new test:
  - cut before redaction → `test_ut08_15_last_error_redacted_before_cut`
  - `now <= due` → `test_ut08_104_probe_due_boundary`
  - I-1 re-read disabled → `test_ut08_19_stale_cache_rechecks_probe_due`
  - invalid-key skip removed → `test_ut08_104_invalid_stored_key_is_skipped`
  - client_unknown log removed → `test_ut08_104_model_probe_network_rules`
  - reason `probe` → None → `test_ut08_19_open_probe_claimed_once`
  - closed short-circuit → UT08-16

### Finding status
- **I-1: ❌ partly closed.** My original scratch repro now passes (B caches the row 3 s before it is due and is refused after the re-open). Two residual paths of the same defect still let a second probe through right after a re-open.
  - **(a) allow(), breaker.py:185.** The re-read happens only when `read_at < due`. A row cached *after* it fell due is still trusted.
    - How the row gets cached: `state()`, `retry_at()`, or `record_failure`/`record_success` of a long in-flight call finishing while open. The last one is realistic for model calls longer than the 60 s cooldown.
    - Scratch repro: open at t0 (trips 1); at t0+61, `B.record_failure(ModelUnavailable)` caches the open row. A claims, fails and re-opens (trips 2, 120 s). At t0+62, `B.allow()` returns **True**. A variant that caches via `B.state()` at t0+61 also returns True.
    - Fix, validated in scratch (all 63 tests, including both repros, pass): change the condition to `read_at < now`. That is, re-read whenever a claim would be attempted from a cached open row. It costs one read per claim attempt, which is off the hot path, and it is a same-line change, so the budget is unaffected.
  - **(b) run_due_probes, breaker.py:353-366.** `due` and `_claim` use the row from `health_list` taken at the start of the tick. Earlier probes in the same tick can take up to 30 s each, so another process can claim, fail and re-open a later row in the meantime; the supervisor then claims it again with the stale `due`.
    - Scratch repro: rows `confluence` and `jira`, both open and due. During the `confluence` probe, another breaker instance claims `jira` and fails it. `run_due_probes` still probes `jira`: calls were `['confluence', 'jira']`.
    - Fix: before computing `due`, re-read the row (`health_get(row.source)`) and `continue` unless it is still `open`. Alternatively, route the claim through the same fresh-row check as `allow()`.
    - Budget: breaker.py is at 379/380, so this needs about 2 lines trimmed elsewhere, for example by folding `_split`'s two `removeprefix` branches or shortening a docstring.
  - **Regression tests to add:** the two scratch tests.

    ```
    _open("model:m", now, trips=1); b = CircuitBreaker("model:m"); env.advance(61)
    b.record_failure(ModelUnavailable("in-flight")); a = CircuitBreaker("model:m")
    assert a.allow(); a.record_failure(ModelUnavailable("probe failed")); env.advance(1)
    assert b.allow() is False
    ```

    ```
    probe for "confluence" does: other = CircuitBreaker("jira"); assert other.allow(); other.record_failure(SourceUnavailable("x"))
    _open("confluence", ...1 h ago); _open("jira", ...1 h ago); run_due_probes(now); assert calls == ["confluence"]
    ```

- **M-1: ✅ closed.** The email straddles char 500 and is replaced; the cut-first mutant is killed.
- **M-2: ✅ closed.** The 300 s / 299 s boundary is tested; the mutant is killed.
- **M-3: ✅ closed.** The invalid stored key is skipped with a DEBUG `resilience.probe.invalid_key` that carries no key text; the other rows still run.
- **M-4: ✅ closed.** DEBUG `resilience.probe.client_unknown` logs `error_type` only, and the test asserts it.
- **M-5: parked** (ruling).
- **M-6: ✅ closed.** `"reason":"probe"` is asserted.
- **M-7: ✅ closed.** `_EXPORTS` is sorted.
- **New:** none beyond the I-1 residuals.
- **Non-gate note:** mypy run directly on the test file reports 4 errors: test_resilience_breaker.py:625, where the lambda returns `Literal[False] | None`, and 649/656/658, `bmod._call_with_timeout` on the callable-module type. They already existed at 3f868a9. `tests/` is outside the configured mypy scope, so this is informational only.

### Assessment (round 1)
**Task quality:** Needs fixes

**Reasoning:** Every ruled Minor is closed with a killing test, and no regressions were found. I-1 is only closed for the "cached before due" case: a row cached after due (in `allow()`) or a row listed at the start of the tick (in `run_due_probes`) still lets a second probe through right after a re-open, which weakens TH08-05. Both fixes are small and local to breaker.py and fit the budget.


---

## Re-review round 2 (head 683dcdc; fix commit over 6c19054)

Scope: I-1a, I-1b, the new regression tests, and the test-file mypy cleanup.

### Evidence
- **Tests:** `tests/unit/core/resilience`, `tests/unit/store/ops` and `tests/integration/jobs` give 881 passed, 1 skipped. The skip already existed. There were no warnings and no regressions.
- **Coverage:** breaker.py is 100 % line and 100 % branch (255 statements, 68 branches).
- **Gates:** ruff check and format are clean. mypy is clean for the configured scope (183 files) and now also directly on both test files (the 4 earlier test-file errors are gone). lint-imports: 13 kept, 0 broken.
- **Size:** breaker.py is 377 of its 380-line budget.
- **Worktree:** `git status` is clean.
- **My four scratch repros, re-run on 683dcdc code:** all pass (67/67 with the builder's tests).
  - The original I-1 repro: the row is cached before the probe is due.
  - (a) The row is cached after due via `record_failure` of an in-flight call.
  - (a) The row is cached after due via `state()`.
  - (b) Another process claims and fails `jira` while the supervisor probes `confluence`. The supervisor now probes only `confluence`.
- **Reverting each fix in scratch:**
  - Putting back `read_at < due` is killed by `test_ut08_19_cache_read_after_due_is_rechecked[record_failure|state]`.
  - Removing the `health_get` re-read in `run_due_probes` is killed by `test_ut08_104_rechecks_rows_probed_during_the_tick`.

### Finding status
- **I-1a: ✅ closed.** breaker.py:182 re-reads whenever a claim would be attempted from a cached open row (`read_at < now`). The hot path is unchanged: a closed row gives `due is None`, so there is no read and no `get_config`.
- **I-1b: ✅ closed.** breaker.py:356-363 re-reads each listed row before computing `due`, only when a probe fn is registered. It skips the row unless it is still `open`, and it claims and builds the event from the fresh row.
- **mypy test cleanup: ✅.**
- **M-1, M-2, M-3, M-4, M-6, M-7:** remain closed. The mutants were re-checked in round 1 and the code is unchanged apart from the `_emit`/`_to_open` reshapes, which are behaviour-preserving and covered by UT08-12/14.
- **M-5 and the claim-SQL hardening:** parked by ruling.

### Builder note: run_due_probes claims with the tick's `now`
This is Minor and does not block.
- **It is safe for the claim decision.** A back-dated `now` is conservative. Because the row is fresh, a re-open that happened after the tick started gives `opened_at > now`, so `now < due` skips the row. The SQL `:now >= :probe_due` also uses the earlier time.
- **The only effect is on the stale timer.** The claim stamps `updated_at = <tick now>`, back-dated by however long the earlier probes in the tick took. That shortens the 600 s stale half-open window for that row by the same amount.
- **When it would matter.** It only matters if one tick runs about 20 or more probes that each time out at 30 s. Another process could then treat the fresh claim as stale and re-claim it.
- **Optional fix.** Claim with `max(now, clock.now())` at breaker.py:363 (`b._claim(fresh, stamp, due)` with `stamp = max(now, clock.now())`). This is one line and within budget. Alternatively, accept it as is because the supervisor ticks are short.

### New Minor findings (non-blocking)
- **R2-M1:** Two defensive details survive mutation. Neither affects behaviour, because the claim SQL is authoritative (it refuses a closed or fresh half-open row).
  - Dropping the `fresh.state != "open"` guard at breaker.py:357.
  - Claiming with the listed `row` instead of `fresh` at breaker.py:363. This only affects the cached row and the event's `trips`/`failures`.
  - Optional test: in `test_ut08_104_rechecks_rows_probed_during_the_tick`, also assert that the supervisor's half-open event carries the fresh `trips`. Alternatively, add a case where the row became `closed` mid-tick and assert that no claim is attempted, using a spy on `health_claim_probe`.

### Assessment (round 2)
**Task quality:** Approved

**Reasoning:** Both I-1 residual paths are closed. Each has a regression test that fails if its fix is reverted, and my independent repros pass. Tests, coverage (100/100), gates, budgets and layering are all clean. The two remaining items, the tick-`now` stamp and the defensive-mutant test gap, are Minor and optional.
