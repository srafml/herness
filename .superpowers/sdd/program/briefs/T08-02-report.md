# T08-02 report: Cron parser and resilience config validator

Status: DONE_WITH_CONCERNS (minor; see Concerns)
Commit: c711f9f feat(jobs): add cron parser and resilience config validator (T08-02)
Worktree/branch: D:\herness\.claude\worktrees\agent-ae5fbbfe1d761ce45 / worktree-agent-ae5fbbfe1d761ce45 (base cdadffe)

## What was built
- herness/core/jobs/cron.py (183/290): `CronExpr` frozen dataclass (text + minutes, hours, days, months, weekdays frozensets + dom_star, dow_star), `parse` (5 fields; `*`, `*/n`, `a`, `a-b`, `a-b/n`, comma lists; MON-SUN case-insensitive in day-of-week; 7 -> 0), `matches_date` (Vixie OR rule), `next_after` (<= 1 830 days, "cron never fires: <text>"), `latest_at_or_before` (<= 366 days, else None); `resolve_local` (U08-65: fold=0 round-trip, gap -> step forward <= 180 minutes).
  Errors: `ConfigError("invalid cron '<text>': <field> <reason>")`, field names minute/hour/day-of-month/month/day-of-week; wrong field count -> `... : fields expected 5, got N`.
- herness/core/jobs/validate.py (124/180): `validate_windows` (U08-67: 10 080-minute cover array, Monday 00:00 = 0, wall clock; merged runs `minute <DAY HH:MM>` / `minute <DAY HH:MM>–<DAY HH:MM> not covered|covered by a, b`; duplicate names; chat count != 1; at most 20 messages, coverage first) and `validate_resilience_config(cfg, *, offline=True)` (U08-99: windows -> path `schedule.windows`; crons `schedule.jobs[i].cron`, `schedule.rekey.cron`, and for each enabled source `sources.<name>.schedule` (when set) and `sources.<name>.reconcile.schedule`, each parsed + `next_after(clock.now(), tz)`; `backup.nightly_at` regex). All issues severity `error`, `file` set to resilience.yaml / sources.yaml / herness.yaml. Not registered at import (composition roots' job).
- herness/core/jobs/__init__.py (59/90): `CronExpr`, `resolve_local`, `validate_resilience_config`, `validate_windows` added to `_EXPORTS` and the TYPE_CHECKING block.

## Design decisions
- tz for U08-99 is `clock.zone(cfg.weights.business_timezone)` (already validated by the weights model).
- Layering: validate.py imports `HernessConfig` only under TYPE_CHECKING; sources are read through the typed method `cfg.sources.enabled_sources()` — no import of herness.connectors. `ConfigIssue` imported from herness.core.config_view (same as config_validate). lint-imports: 13 kept, 0 broken.
- `latest_at_or_before` / `next_after`: on the first date, candidates within 3 h of the start minute are resolved and compared as instants instead of being pruned by naive wall time. The literal mirror algorithm misses the fold=0 fire when `t` lies in the second pass of a DST fold (e.g. latest(2026-10-25 01:20Z, London) must be 00:30Z, not the previous day). This keeps PT08-04 true in DST zones; results equal the spec algorithm everywhere else.
- dom_star/dow_star are true only when the field text is exactly `*` (spec wording), so `*/2` counts as restricted.
- `resolve_local` refuses an aware datetime with SchemaViolation; `next_after`/`latest` reject naive `t` via `clock.ensure_utc`.
- The en dash in merged messages is built with chr(0x2013) (ruff RUF001).

## Deviations / notes
- UT08-63 (T08-03 test) asserted every lazy export lives in `ports`; generalised to resolve each name from its owning submodule per `_EXPORTS`.
- Cron issue messages contain the cron text (spec says "the error message" of the ConfigError, whose format includes '<text>'); cron text is not secret, but note TH10-06 says messages hold key paths and names only.
- `backup.nightly_at` check is unreachable through the loader (BackupConfig already enforces the same regex); tested via model_copy.
- `sources.<name>.reconcile.schedule` is checked for every enabled source (the section always has a default); monitoring's default reconcile is parsed too (harmless, valid default).

## Tests (tests/unit/core/jobs/)
test_jobs_cron.py: test_ut08_72_valid_crons (4 params), test_ut08_72_forms_and_names, test_ut08_72_invalid_crons (13 params), test_ut08_73_vixie_or_rule, test_ut08_73_strictly_after_and_at_or_before, test_ut08_73_never_fires_and_naive, test_ut08_74_gap_fires_at_next_valid_minute, test_ut08_74_fold_fires_once_first_occurrence, test_ut08_74_resolve_local, test_pt08_04_next_and_latest_agree (hypothesis; UTC, London, New York, Sydney).
test_jobs_validate.py: test_ut08_04_defaults_are_valid (acceptance: design §7 defaults give no issue, on cfg_default windows and a loaded HernessConfig), test_ut08_04_gap_overlap_and_two_chats, test_ut08_04_ranges_merge_across_days_not_week, test_ut08_04_at_most_twenty_messages, test_ut08_05_model_rejects_with_path (4 params: 4-field cron, then kind foo, preload, class), test_ut08_05_unparsable_crons_reported_with_paths (loaded config: `60 * * * *` -> schedule.jobs[0].cron, never-firing rekey, enabled files source `*/0 * * * *` -> sources.files.schedule), test_ut08_05_nightly_at_checked, test_ut08_05_registered_owner_validator (register_owner_validator("resilience", ...) + run_owner_validators — acceptance via the R-71 hook), test_pt08_05_valid_config_has_one_window (hypothesis; local U08-66-style lookup, converse checked for perturbed configs), test_pt08_05_default_windows_every_minute.
RED/GREEN: implementation was written before the tests (not strict TDD); tests were run green after; one pre-existing test (UT08-63) failed red on the new exports and was generalised.

## Gate results
- ruff format / ruff check: clean. mypy: Success, 147 files. lint-imports: 13 kept, 0 broken. check_type_ownership: exit 0. check_module_size: exit 0.
- `pytest -k "ut08_04 or ut08_05 or ut08_72 or ut08_73 or ut08_74 or pt08_04 or pt08_05 or ut08_63"` + branch coverage: cron.py 98% (only the unreachable 180-step failure raise), validate.py 99%, __init__ 100%.
- Full `pytest -m "(unit or integration) and not slow"`: 4021 passed, 5 skipped, 1 xfailed.
- Commit ran all pre-commit hooks (no --no-verify, no PRE_COMMIT_ALLOW_NO_CONFIG): all passed.

## Line counts vs budgets
cron.py 183/290, validate.py 124/180, __init__.py 59/90.

## Carry-overs
- `herness config validate` CLI listing U08-99 issues: defers to T10-14; covered here via run_owner_validators.
- Registration `register_owner_validator("resilience", validate_resilience_config)` belongs to T09-20 / T09-13 / T09-27 (not built).
- PT08-05 uses a local U08-66-style lookup in the test; the real `window_at` (U08-66, UT08-75) is a later card and should rerun PT08-05 against it.
- Consider whether `latest_at_or_before`'s fold handling (3 h margin) should be written back into the spec algorithm text.

## Fix round 1 (review T08-02-review.md, Approved with Minors)
Commit: 4da181e fix(jobs): close T08-02 review findings (T08-02)
- m1: `validate_windows` builds the duplicate-name and chat-count issues first and truncates coverage to `20 - len(name issues)`; still at most 20 in total, and coverage still comes first in the output. The cap test now asserts the exact kept list (first 20 of 21 gap messages; with a duplicate `chat`: 18 gap messages + both name issues). Docstring reworded.
- m4: `_NUMBER` is `[0-9]+`. Tokens longer than 9 digits are clamped to 10**9 so `int()` never sees > 4300 digits, and the range check rejects them. New rows: `99999 * * * *` -> "minute value out of range 0-59"; `* * 1-99999999999999999999 * *` -> day-of-month out of range. Note: `*/99999` is now accepted as a step beyond the range (minutes == {0}, asserted); no step upper bound is in the spec.
- m5: comment on the 367-date walk (today + 366 back keeps a yearly/Feb 29 cron findable).
- m6: `next_after` no longer uses the margin (prunes on `naive >= start` as the spec says); the margin comment now describes only `latest_at_or_before`.
- Parked, unchanged: m2, m3.
Gates: ruff format/check clean; mypy Success (147 files); lint-imports 13 kept, 0 broken; check_type_ownership 0; check_module_size 0; full `pytest -m "(unit or integration) and not slow"`: 4023 passed, 5 skipped, 1 xfailed; hooks all passed. Coverage: cron.py 98%, validate.py 99%. Lines: cron.py 187/290, validate.py 126/180.

## Fix round 2
Commit 1633057 docs(jobs): correct latest_at_or_before walk comment (T08-02) — comment-only (r1-1): the 367-date walk is justified with `30 23 1 3 *` at 2024-03-01 23:00 finding 2023-03-01, 366 dates back (verified); the wrong Feb 29 claim is removed. ruff format/check clean, check_module_size 0, hooks passed.
