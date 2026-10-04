# T08-15b review (verify agent; worktree agent-a3571a55e85686f72, head 6425193, base e41d62c)

### Spec Compliance
- ✅ Spec compliant
  - ✅ 1. `scrub_secrets` runs before `redact_text`, then the cut, in both `_last_error` functions (herness/core/jobs/outcomes.py:183-185, herness/core/jobs/tasks.py:174-178). `MESSAGE_WITHHELD` is kept for a None redaction. Failures fail closed: a scrub failure returns `{"event": "log.scrub.failed"}`, so `.get("m")` is None or not a str. That gives MESSAGE_WITHHELD for a job and "" for a task. This matches the existing `resilience/events.py:_clean_str` pattern. The import comes from `herness.core.secrets` because `herness.core.redact` does not re-export it (I checked; lint-imports passes per the report).
  - ✅ 2. The strict xfail on `test_st10_14_job_error_with_known_secret_is_masked_in_ops` is removed, and the test passes.
  - ✅ 3. Task-path positive case added: `test_ut08_70_last_error_known_secret_masked` (tests/unit/core/jobs/test_jobs_tasks.py:450). It also covers a scrub failure (stores ""). The job path also gets `test_ut08_59_unscrubbable_message_is_withheld` (test_jobs_outcomes.py:402).
  - ✅ 4. Budgets: outcomes.py 252/260, tasks.py 211/260. No new modules.
  - ✅ 5. The spec note under U08-50 is present with the required wording (docs/impl/08-resilience-and-jobs.impl.md:1251) and also covers the U08-62 task path.
- ⚠️ Cannot verify from diff: none left open. I ran every check below.

### Verification evidence (re-run by verifier)
- `uv run pytest tests/security/test_st10_secret_leak.py tests/unit/store/ops/test_store_ops_tasks.py tests/unit/core/jobs --ignore=tests/unit/core/jobs/test_jobs_gpu.py` → 587 passed.
- `tests/unit/core/jobs/test_jobs_gpu.py` alone → interpreter crash, "Windows fatal exception: stack overflow", exit 127. It is **pre-existing**: I reproduced it with the 4 changed source/test files reset to e41d62c (exit 127, same crash). The rest of the diff (docs, baseline, st10 test, support comment) is not on that import path. With `import pandas` first it gives 33 passed. Not this card's problem; owed to impl 08 / fake_clock owners as the report says.
- Mutation A, outcomes.py scrub bypassed (`scrubbed = str(err)`): ST10-14 known-secret test FAILED, UT08-59 unscrubbable FAILED. Reverted.
- Mutation B, tasks.py scrub bypassed: UT08-70 known-secret test FAILED (all other tests in st10/outcomes/tasks/store_ops_tasks passed). Reverted.
- Mutation C, order swapped in both files (redact first, then scrub): **all 177 tests pass, so no test notices.** A probe script (temp dir only) shows that the order matters:
  - known value `hunter2-jane.doe@example.com-zz`: scrub->redact gives `***`; redact->scrub gives `[EMAIL_0253d7fe23]-zz`. A fragment of the secret leaks, plus a keyed pseudonym computed over secret-bearing text.
  - known values `svc.bot@corp.example.com` and `+1 415 555 0134`: redact->scrub gives `[EMAIL_…]` / `[PHONE_…]` instead of `***`.
  Reverted.
- `git status` clean at the end; all temp files are under C:\Users\santh\AppData\Local\Temp\w24-s08-T08-15b-verify\.

### Strengths
- Minimal, symmetric change in both paths, the same pattern as `events._clean_str`. Fail-closed on both scrub and redaction failure, each tested by its own test.
- The task-path test proves the value is really a resolved keyring secret (`secrets.resolve(...)`) before asserting it is masked. It also asserts the prefix survives, so it would catch an over-eager withhold.
- The stale comment in tests/support/secret_leak.py is updated. In .secrets.baseline only two line numbers moved (+2, matching the 2 inserted doc lines) plus `generated_at`; no entries dropped; LF only (0 CR).

### Issues
#### Critical (Must Fix)
- None.
#### Important (Should Fix)
- None.
#### Minor (Nice to Have)
- tests/unit/core/jobs/test_jobs_outcomes.py:402 / test_jobs_tasks.py:450: no test pins the order the brief mandates (scrub BEFORE redact). Mutation C (swap) passes the whole suite, but it would partly leak a known value that overlaps an email/phone shape (see probe above). Suggested fix: add a case whose known value contains an email shape, e.g. `"tok-" + "jane.doe@example.com" + "-zz"`, and assert that neither `-zz` nor `[EMAIL_` appears in the message.
- tests/unit/core/jobs/test_jobs_tasks.py:456: `secrets.resolve(...)` adds `KNOWN_VALUE` to the process-global `secrets._KNOWN`, and it is never reset, so the value persists for the rest of the session. tests/security/test_st08_events_metrics.py:44 does it the hygienic way: `monkeypatch.setattr(secrets, "_KNOWN", ...)`. Low risk because the value is unique, but it is cross-test state.
- Test IDs, docstrings (first line starts with the ID) and module `pytestmark` are fine (unit at test_jobs_outcomes.py:42; integration at test_st10_secret_leak.py:35). No assertion depends on wall-clock time: the outcomes test uses `fake_now`, and the tasks test asserts only on message text.

### Assessment
**Task quality:** Approved
**Reasoning:** Every brief requirement is met and verified: the scrub runs before redaction and the cut in both paths, failures fail closed, the xfail is gone and the test passes, and bypassing the scrub in either file fails a test. The one gap is that no test pins the scrub-before-redact order (Minor; a follow-up test is recommended). The test_jobs_gpu stack-overflow crash is pre-existing and reproduces on base sources.
