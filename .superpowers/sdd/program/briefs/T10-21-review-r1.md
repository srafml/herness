# T10-21 re-verify, fix round 1 (5a5d877; diff 8f31e86..5a5d877)

Scope: I1, M1, M2, M3 from T10-21-review.md, plus the xfail/skip check (narrowed by the coordinator: the single strict xfail is intentional, verify a/b/c and the diagnosis). Read-only. The probe plugin lived in `<worktree>\.agent-tmp\t1021-reverify\` and was deleted afterwards. `git status` is clean.

## Findings
- I1 ✅
  - ops.sqlite dump: now fed by two production paths. (1) `record_event("breaker_open", detail.reason=<error text with the resolved keyring secret>)` writes a `resilience_event` row (secret_leak.py `_connector_auth_failure`). (2) A `reconcile` job goes through the real queue (enqueue, claim, run_handler, finish_job) with `JOB_ERROR` (`password=<CRED>` and `?token=<URL>`) and writes `job.last_error` (`fail_job`).
  - Positive controls in `test_st10_14_grep_scanned_real_artefacts`: the dump must have INSERTs for `file_ingest`, `review_item`, `resilience_event` and `job`, plus `upstream rejected credential ***` (the masked breaker reason) and `reconcile failed: password=` (the redacted last_error).
  - Mutation-proven (see the probes below).
  - Review items: documented as structural only, in the ST10-14 module docstring, in the `_review_item` docstring and in the report. I checked the claim: `herness/enrich/mapping_suggest.py:297-309` `_payload` carries only subject/service ids, scores, counts and the algorithm version, and never ticket text. Accepted.
- M1 ✅ The model-request assertion for the blocked host is dropped. The docstring of `test_st10_14_blocked_host_never_gets_a_socket` gives the reason. The EgressBlocked, 0-connect and not-resolved checks remain.
- M2 ✅
  - `leak_run` is now defined once, at `tests/support/secret_leak.py:452-462`. Both copies were removed from the test modules.
  - The `tests/conftest.py` change is minimal and correct: one `pytest_plugins` entry and one docstring sentence. No existing fixture changed.
  - The plugin module has no autouse fixture, no `pytest_*` hook and no import-time side effects (only constants, defs and classes). `leak_run` is the only fixture with that name.
  - Process-global state touched by the new code is reset through the `reset_process_state` fixture that `leak_run` and the xfail test request: `register_handler("reconcile")` goes into `process_state().handlers`, and `bind_jobs_backend` does too.
- M3 ✅ `__all__` now covers every name the tests import, plus the new helpers. The only public name left out is `TICKETS_CSV`, which is used internally only (see Minor N1).
- xfail/skip (coordinator scope a/b/c):
  - (a) ✅ With `--runxfail`, `test_st10_14_job_error_with_known_secret_is_masked_in_ops` fails at `assert SENTINEL_KEY not in dump`. A dump-line spy shows exactly one hit: the `INSERT INTO "job"` row (its `last_error` JSON). No other table or row holds a sentinel.
  - (b) ✅ The marker is `strict=True`, so an XPASS after a production fix fails the run.
  - (c) ✅ No other IT10-05, IT10-10 or ST10-14 test carries an xfail or skip marker. The only other "skip" hit is a `# fmt: skip` comment at secret_leak.py:177. The 7 other tests genuinely pass.
  - Diagnosis ✅ correct.
    - `herness/core/jobs/outcomes.py:179-188` `_last_error` does `redact_text(str(err))` only.
    - `herness/core/jobs/tasks.py:170-177` `_last_error` does the same.
    - Neither consults `known_values()` or `scrub_secrets`. Contrast `herness/core/resilience/events.py` `_clean_str`, which scrubs first and then redacts.
    - A known secret without a credential shape therefore reaches `job.last_error` unmasked, which is a real ST10-14/TH10-07 leak. It needs a coordinator ruling and a follow-up owner (impl 08/10). The report proposes the fix: scrub_secrets before redact_text in both functions.

## New issues
- Critical: none.
- Important: none in the test change. The production leak above is pre-existing and correctly pinned by the strict xfail; it needs to be tracked as its own follow-up.
- Minor:
  - N1 `tests/support/secret_leak.py:122` `TICKETS_CSV` is public but not in `__all__`. It is used only internally, so it is cosmetic.
  - N2 Under the redact_text-off-in-jobs.outcomes probe, `grep_scanned` stays green: its prefix `reconcile failed: password=` survives either way. Only the zero-hit test catches this mutation, which is enough. A stronger control would assert a redaction placeholder in the job row. Optional.

## Probes (`-p probe_plugin`, monkeypatch at fixture setup; files: tests/security/test_st10_secret_leak.py + tests/integration/test_security_e2e.py)
| Mutation | Result |
|---|---|
| none | 7 passed, 1 xfailed |
| scrub_secrets -> identity at all 4 holder sites | RED zero_sentinel_hits (logs, traces and `ops.sqlite .dump`, the `resilience_event` breaker reason) and grep_scanned. 5 pass, xfail stays xfail. |
| scrub_secrets -> identity in `herness.core.resilience.events` only | RED zero_sentinel_hits with exactly one hit, `ops.sqlite .dump: ops.sqlite`, and grep_scanned (no masked reason). This isolates the dump sink. |
| redact_text -> identity in `herness.core.jobs.outcomes` only | RED zero_sentinel_hits: 2 hits in `ops.sqlite .dump` (CRED and URL in job.last_error). |
| redact_text -> identity at every holder site | RED zero_sentinel_hits (ops dump + model requests) |
| install_socket_guard -> no-op (2 sites) | RED `test_st10_14_blocked_host_never_gets_a_socket` (`blocked is None`) |
| --runxfail + dump spy | the xfail test FAILS. The only hit is the keyring sentinel in `INSERT INTO "job"`. |

## Runs
- `PYTHONUTF8=1 uv run pytest -k "IT10_05 or IT10_10 or ST10_14" --require-test-ids -q` 5x: 7 passed, 1 xfailed on every run (14-22 s). Not flaky.
- tests/security + tests/integration/test_security_*.py together: 657 passed, 1 skipped (unrelated ST05-17 Windows symlink privilege), 1 xfailed. No pollution.
- `ruff check` on the 4 touched files: all checks passed. `ruff format --check`: 4 files already formatted.

## Verdict: Approved
I1, M1, M2 and M3 are resolved. The dump sink is now non-vacuous and mutation-proven, and review items are documented as structural only, with the claim checked. The single strict xfail pins a real, correctly diagnosed production leak in `job.last_error` (outcomes/tasks `_last_error` skip `scrub_secrets`). The coordinator needs to rule on it and open a follow-up.
