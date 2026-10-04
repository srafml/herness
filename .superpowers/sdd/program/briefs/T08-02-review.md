# T08-02 review: Cron parser and resilience config validator

Reviewed: worktree agent-ae5fbbfe1d761ce45, base cdadffe, head c711f9f (read-only).
Evidence: `PYTHONUTF8=1 uv run pytest tests/unit/core/jobs -q -p no:logging` gave 55 passed. Branch coverage: cron.py 98% (only the unreachable 180-step raise, lines 182-183), validate.py 99%. Spot checks used a scratch script: parser edge cases, London fold and gap, 6 000 random PT08-04 trials over London, New York, Lord_Howe, Santiago, Tehran and Chatham, and a 3 000-trial brute-force "no fire between t and next" check against minute enumeration. All passed with 0 failures. Timing: a never-firing cron takes about 3 ms, validate_windows about 1-2 ms.

### Spec Compliance
- ✅ Spec compliant

| Unit / test | Status | Notes |
|---|---|---|
| U08-64 `CronExpr.parse` | ✅ | Needs exactly 5 fields. Items `*`, `*/n` (n>=1), `a`, `a-b` (a<=b), `a-b/n`. `a/n` is rejected, which matches the grammar. MON-SUN names are case-insensitive and allowed only in day-of-week. `7` is read as 0. Ranges are 0-59, 0-23, 1-31, 1-12, 0-7. The error is `invalid cron '<text>': <field> <reason>`, with field names minute, hour, day-of-month, month, day-of-week. A wrong field count gives `... : fields expected 5, got N`. `dom_star` / `dow_star` are set only for a literal `*`. |
| U08-64 matching | ✅ | Vixie rule, `cron.py:122-130`. |
| U08-64 `next_after` | ✅ | Walks 1 830 dates. The first resolved fire > t is returned. Otherwise `ConfigError("cron never fires: <text>")`. Output is aware UTC. |
| U08-64 `latest_at_or_before` | ✅ (justified deviation) | See the deviation note below. |
| U08-65 `resolve_local` | ✅ | fold=0 round trip, then up to 180 forward steps. |
| U08-67 `validate_windows` | ✅ | 10 080-minute wall-clock array, Monday 00:00 = 0. `end<=start` wraps through `% 10080`, including SUN into MON (spot-checked). Issues come in minute order, and equal runs merge with an en dash. Also reports duplicate names and a `chat` count other than 1. Capped at 20 messages. |
| U08-99 `validate_resilience_config` | ✅ | Paths checked, in order: `schedule.windows`, `schedule.jobs[i].cron`, `schedule.rekey.cron`, and for enabled sources `sources.<name>.schedule` (when set) and `.reconcile.schedule` (always has a default). Each cron gets parse + `next_after(clock.now(), tz)`. `backup.nightly_at` is checked against the regex. Only `ConfigError` is caught, and it is the only error parse or next_after can raise for config input. `offline` is ignored. The signature fits the `OwnerValidator` protocol. |
| `__init__` lazy exports | ✅ | 4 names added to `_EXPORTS` and the TYPE_CHECKING block. UT08-63 was generalised correctly to check each owning submodule. |
| Layering | ✅ | cron.py imports only `clock` and `errors`. validate.py imports `config_view` at runtime and `HernessConfig`/`WindowSpec` under TYPE_CHECKING only. Sources are read through `cfg.sources.enabled_sources()`, so there is no connectors or store import. |
| UT08-72 | ✅ | All 4 valid and 4 invalid crons from the spec table, plus extra cases. |
| UT08-73 | ✅ | `0 0 13 * FRI` OR rule, DOM-only, DOW-only, strictly-after, and <= t. |
| UT08-74 | ✅ | Gap fires at 02:00 local. The fold fires once, at the first (fold=0) occurrence, and the second-pass `latest` case is covered. |
| UT08-04 | ✅ | Gap, overlap and two chats give messages with ranges on `schedule.windows`. The defaults give no issue (acceptance). |
| UT08-05 | ✅ | Model rejections with their paths. U08-99 reports `schedule.jobs[0].cron` and `sources.files.schedule` on a loaded config. The R-71 registration and `run_owner_validators` path is exercised. |
| PT08-04 | ✅ | Hypothesis over UTC, London, New York and Sydney. My brute-force check agrees. |
| PT08-05 | ✅ (provisional) | Uses a U08-66-style lookup local to the test, because `window_at` is a later card (carried over). |
| pytestmark / IDs / docstrings | ✅ | Both files set `pytestmark = pytest.mark.unit`. Every function name has its ID and every docstring starts with it. |

**`latest_at_or_before` deviation (`_FOLD_MARGIN`, `cron.py:29,152-164`): the fix is correct and keeps the PT08-04 invariants.**
- The literal "mirror" algorithm prunes by naive wall time. When `t` is in the second pass of a fold, it skips the fold=0 fire, for example latest(2026-10-25 01:20Z, London) should be 00:30Z. That violates the "latest fire ≤ t" postcondition.
- `resolve_local` is monotone non-decreasing in naive time: fold=0 is monotone, and a gap shifts forward. So scanning in descending naive order and returning the first fire <= t gives the maximum.
- The 3 h ceiling is enough, because real UTC-offset jumps are at most 2 h.
- In `next_after` the margin is harmless: any naive time before `start` resolves to <= t.
- 6 000 randomized trials (including Lord_Howe's 30-min shift and Chatham) showed no violation of `next>t`, `latest(next)==next`, or `next(latest(t))==next(t)`.

- ⚠️ Cannot verify from diff:
  - `herness config validate` listing U08-99 issues via the CLI depends on T10-14. Composition-root registration is T09-20/T09-13/T09-27. Here it is covered only through `run_owner_validators`.
  - PT08-05 needs a rerun against the real U08-66 `window_at` when that card lands.
  - The builder flagged cron text in issue messages. U08-99 step 2 says the issue carries "the error message", and U08-64 defines that message as including `'<text>'`. A cron expression is operator config, not a secret, ticket text or personal data, so this is spec-mandated and acceptable. ENG §3.4 is not breached.

### Strengths
- Parser is small and table-driven. `_FieldError` is used internally, and `raise ... from None` keeps messages clean.
- The DST fold fix is sound and has its own test (`test_ut08_74_fold_fires_once_first_occurrence`, last assert).
- Tests assert exact messages, paths and files. UT08-05 runs through a real `load_config` and the R-71 hook.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. **`herness/core/jobs/validate.py:84`: the 20-message cap truncates after the coverage issues.** Issues are cut as `issues[:20]` with coverage first. A config with 20 or more coverage runs therefore hides the duplicate-name and `chat`-count issues, so the operator needs a second validation round. This follows the spec ("in minute order, at most 20"), but it hides the most structural issue.
   - Fix: reserve room for the name issues, e.g. `coverage[: 20 - len(name_issues)] + name_issues`.
   - Also assert in `test_ut08_04_at_most_twenty_messages` (test_jobs_validate.py:101) that the returned 20 messages are the first 20 in minute order, not just that there are 20. That docstring ("no chat window is reported when there is room") is also confusing and should be reworded.
2. **`herness/core/jobs/cron.py:120`: `dom_star`/`dow_star` are set only for a literal `*`.** This follows the spec wording, but real Vixie cron sets DOM_STAR/DOW_STAR whenever the field starts with `*`, so `*/2`, `*,5` and similar count as star. The two readings give different fires, e.g. `0 0 */2 * MON`: Vixie fires on odd days that are Mondays (AND), while this code fires on odd days OR Mondays. No default cron is affected.
   - Fix: pick one reading explicitly. Either keep the literal rule and document it in the docstring (and suggest the spec note it), or use `fields[2].startswith("*")` to match Vixie.
   - Add a test either way.
3. **`herness/core/jobs/cron.py:34,80`: names in ranges.** `SUN` = 0, so `MON-SUN` / `FRI-SUN` fail with "range reversed". Other names and ranges work. The spec does not require name ranges.
   - Fix: when the range end is the name `SUN` (or the number `7`) in day-of-week, map it to 7 before the reversed check. Otherwise note in the docstring that `SUN` cannot end a range.
4. **`herness/core/jobs/cron.py:57-58`: misleading error for large numbers.** `*/99999` gives "has a non-numeric value '99999'", because `_NUMBER` allows at most 4 digits.
   - Fix: use `[0-9]+` for the regex and let the range/step checks reject large values. For steps, a bound check with message "step out of range" would do.
5. **`herness/core/jobs/cron.py:158`: `latest_at_or_before` walks 367 dates (`_LATEST_DAYS + 1`), not 366.** This is defensible: it keeps a yearly cron findable across a leap year. The spec says "within 366 days".
   - Fix: add a one-line comment explaining the +1.
6. **`herness/core/jobs/cron.py:141,146`: `floor = start - _FOLD_MARGIN` in `next_after` is never needed.** Candidates before `start` always resolve to <= t, so it only adds up to 3 h of extra `resolve_local` calls on the first date. The comment at lines 27-28 implies it matters for both directions.
   - Fix: drop the floor in `next_after` (use `naive >= start`), or reword the comment to say it is kept for symmetry.

### Assessment
**Task quality:** Approved
**Reasoning:** All four units match the binding spec, and every test row has real, exact assertions. The one deviation (fold-aware `latest_at_or_before`) fixes a genuine spec-algorithm defect, and randomized and brute-force checks show it preserves the PT08-04 invariants. The remaining items are polish.

## Re-review round 1 (head 4da181e, scoped to m1, m4, m5, m6)

Evidence: `PYTHONUTF8=1 uv run pytest tests/unit/core/jobs -q -p no:logging` gave 57 passed. I re-ran the round-0 spot-check script against the new head: London gap and fold results are unchanged, 6 000 random PT08-04 trials over 6 DST-heavy zones had 0 failures, and the 3 000-trial brute-force "no fire between t and next" check had 0 failures. Extra parse probes: a 5 000-digit step, a 5 000-digit range end, and `0 0 29 2 *`.

| Finding | Status | Notes |
|---|---|---|
| m1: 20-message cap | ✅ Closed | `validate.py:79-89`: the name issues are built first, and coverage is cut to `20 - len(name_issues)`. The output is still coverage (in minute order) followed by the name issues, at most 20 in total. The test now asserts the exact list kept, both without name issues (first 20 of 21) and with them (18 + 2). |
| m4: number length | ✅ Closed | `_NUMBER = [0-9]+`. Tokens longer than 9 digits are clamped to 10**9, so `int()` never sees more than 4 300 digits (a 5 000-digit token is handled with no ValueError). Values out of range are now reported as "value out of range", not "non-numeric". `*/99999` is accepted and gives {0}. That is fine: the spec only requires n >= 1, it matches common cron behaviour, and the fire set is still valid. Not flagged. |
| m5: 367-date comment | ✅ Closed, with one new wording nit (r1-1 below) | |
| m6: `next_after` margin | ✅ Closed | `cron.py:146` prunes on `naive >= start`, as the spec says. The margin comment (`cron.py:27-30`) now describes only `latest_at_or_before`. PT08-04 and the UT08-74 DST tests pass, and the spot checks are unchanged (gap fires at 01:00Z = 02:00 BST, fold fires once at 00:30Z, and latest(01:20Z) is 00:30Z). |

m2 and m3 are parked, as instructed.

### New finding
#### Minor
- **r1-1 `herness/core/jobs/cron.py:162`: the comment "a yearly cron such as Feb 29 stays findable" is wrong.** Feb 29 fires only once every four years, so a 367-date walk does not find it in general. Probe: `CronExpr.parse("0 0 29 2 *").latest_at_or_before(2027-06-01Z, UTC)` returns `None`. The code is correct. The example is what fails.
  - Fix: reword the comment to use a real yearly cron, e.g. "a yearly cron such as `0 0 1 3 *` stays findable across a leap year".

### Assessment (round 1)
**Task quality:** Approved
**Reasoning:** m1, m4 and m6 are fixed, with exact-assertion tests, and the DST behaviour and PT08-04 invariants are unchanged. The only open item is a one-line comment wording nit (r1-1), which does not block approval.
