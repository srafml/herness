# T08-15b report (build agent, worktree agent-a3571a55e85686f72, base e41d62c)

Status: DONE_WITH_CONCERNS — commit 6425193 "fix(jobs): scrub known secrets from job and task last_error (T08-15b)"; all pre-commit hooks passed incl. pytest-unit (full unit suite, -x), no skip.

## Implemented
- herness/core/jobs/outcomes.py `_last_error`: `scrub_secrets(None, "last_error", {"m": str(err)}).get("m")`
  (known secret values -> `***`, U10-32/U10-36), then `redact_text`, then cut to 2048. A non-str
  scrub result (scrub failure returns `{"event": "log.scrub.failed"}`) or a None redaction -> MESSAGE_WITHHELD.
- herness/core/jobs/tasks.py `_last_error`: same order; failure -> "" (existing fail-closed rule).
- Import: `from herness.core.secrets import scrub_secrets` (herness.core.redact does NOT re-export it;
  same import as herness/core/resilience/events.py `_clean_str`). lint-imports: 13 kept, 0 broken.
- tests/security/test_st10_secret_leak.py: strict xfail removed from
  test_st10_14_job_error_with_known_secret_is_masked_in_ops.
- tests/unit/core/jobs/test_jobs_tasks.py: new test_ut08_70_last_error_known_secret_masked (task path:
  resolved keyring value with no credential shape is masked; scrub failure stores "").
- tests/unit/core/jobs/test_jobs_outcomes.py: new test_ut08_59_unscrubbable_message_is_withheld.
- tests/support/secret_leak.py: stale comment ("stored unmasked ... strict xfail") updated.
- docs/impl/08-resilience-and-jobs.impl.md: note under U08-50 ("error text is scrubbed of known secret
  values, then redacted, then cut"), mirrored for the task path (U08-62).
- .secrets.baseline: line numbers of two docs/impl/08 entries shifted (+2); no entries dropped; LF.

## RED
`uv run pytest tests/security/test_st10_secret_leak.py tests/unit/core/jobs/test_jobs_tasks.py tests/unit/core/jobs/test_jobs_outcomes.py -k "known_secret or unscrubbable"`
- ST10-14 pin (xfail removed): FAILED (secret in dump)
- UT08-70 task: `AssertionError: assert 'plainvaluewithoutshape' not in 'upstream rejected credential plainvaluewithoutshape'`
- UT08-59 unscrubbable: AttributeError (outcomes has no scrub_secrets)

## GREEN
tests/unit/core/jobs + tests/security/test_st10_secret_leak.py + tests/unit/store/ops/test_store_ops_tasks.py:
620 passed (pandas pre-imported, see concern). Coverage outcomes.py 100% line/branch, tasks.py 100%/100%.

## Mutation check
Replaced the scrub line with `scrubbed = str(err)` in both: ST10-14 pin and UT08-70 task test both FAILED.
Only tasks.py mutated: UT08-70 FAILED, pin passed (independent). Restored; both pass.

## Gates
ruff format (906 unchanged), ruff check clean, mypy (333 files) clean, lint-imports 13 kept,
check_module_size exit 0, check_type_ownership exit 0, detect-secrets-hook clean.
Budgets: outcomes.py 252/260, tasks.py 211/260.

## Concerns
- PRE-EXISTING (reproduced on base sources): `tests/unit/core/jobs/test_jobs_gpu.py` run in a process that
  has not yet imported pandas crashes the interpreter ("Windows fatal exception: stack overflow", exit 127)
  in test_ut08_92_health_timeout_fails_start: under freezegun (fake_clock) a lazy `herness.enrich.embed`
  -> lancedb -> pandas -> dateutil.tz.win import overflows the C stack. With `import pandas` first the file
  passes (33 passed). Not caused by this card; owed to impl 08 / T11-03 (fake_clock) owners.

## Fix round 1 (review Approved; minors M1, M2)
- M1: added order pins with known value `hunter2-jane.doe@example.com-zz` (contains an e-mail shape):
  test_ut08_59_message_scrubbed_before_redaction (job path, test_jobs_outcomes.py) and
  test_ut08_70_last_error_scrubs_before_redacting (task path, test_jobs_tasks.py); both assert the stored
  message has `***` and neither `-zz` nor `[EMAIL_`.
  Order-swap mutation (redact first, then scrub, in both outcomes.py and tasks.py): both new tests FAILED
  (also UT08-59 unredactable, a side effect of the mutant); sources restored (no MUTANT markers left).
- M2: test_ut08_70_last_error_known_secret_masked no longer calls secrets.resolve(); a `_plant_known`
  helper monkeypatches secrets._KNOWN / _KNOWN_VERSION (pattern from test_st08_events_metrics.py:44) and
  also monkeypatches secrets._scrub_version=-1 and _scrub_pattern=None. Without that, the scrub cache
  (keyed only on the version number) kept the previous test's pattern when two tests planted different
  values at the same restored version (seen as an order-dependent failure), and the planted pattern
  would outlive the test. The job-path test uses the same four patches.
- Gates: ruff format/check clean, mypy 333 files clean, detect-secrets-hook clean.
- Tests: tests/unit/core/jobs + tests/security/test_st10_secret_leak.py: 611 passed (pandas pre-imported,
  see the pre-existing gpu concern above).
- Fix-round commit: 9e88eeb "test(jobs): pin scrub-before-redact order in last_error (T08-15b)"; all hooks
  passed incl. pytest-unit, no skip.
