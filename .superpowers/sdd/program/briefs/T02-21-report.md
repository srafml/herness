# T02-21 report — Promotion and cleanup (impl 02 U02-103 … U02-105)

Worktree: D:\herness\.claude\worktrees\agent-ade9bf6491b3cead2 (branch worktree-agent-ade9bf6491b3cead2, base 7e352ad)
Status: DONE_WITH_CONCERNS (see Concerns). Final commit: ebfc91a feat(model): promotion and build cleanup (T02-21) — all pre-commit hooks passed (no SKIP). The earlier wip checkpoint attempt was rejected by the mixed-line-ending hook (docs file normalised), so its content went into this single commit.

## What changed
- NEW herness/model/promote.py (238/260): `promote_build` (U02-104), `cleanup_builds` (U02-105), `CleanupReport`,
  `NON_TERMINAL_RUN_STATUSES`, `ORPHAN_UNREADABLE_AGE_S`. Pinned builds via `ops.select_runs(statuses=NON_TERMINAL_RUN_STATUSES)`;
  builds of live `build_pipeline` jobs via `ops.SqliteJobsBackend().list_jobs(status in queued/running, kind=build_pipeline)`.
- NEW herness/model/_build_promote.py (58/80, private sibling, §2 row added): `stage_promote` (U02-103) and
  `cleanup_before_build` (the U02-98 step 4 `cleanup_builds(mode="pre", protect={build_id})` call, moved here for build.py's budget).
- herness/model/build.py 396 -> 399/400: stage table gains `"promote"`; pre-cleanup call replaces `support.delete_orphans`;
  `_fail` no longer marks the build failed for a promote-stage error once this job's gate passed (`"dq" in run.result`).
- herness/model/_build_support.py 166 -> 152: `delete_orphans` stand-in (`# T02-21:`) removed, docstring and §2 row updated.
- docs/impl/02-data-model.impl.md: §2 row for `_build_promote.py`; `_build_support` row; spec notes (T02-21) under U02-103,
  U02-104, U02-105; log row `model.build.retire_failed`.
- _build_stages.py untouched (195/200). check_module_size exit 0.

## Behaviour
- Atomic promotion: `stage_promote` runs `_build_dq.stage_dq` first unless `dq` ran in this attempt (`"dq" in run.result`):
  covers `--from-stage promote`, retried and resumed jobs; gate failure -> DqGateFailed (build marked failed by stage_dq),
  empty meta.dq_result -> SchemaViolation (fail closed, build marked failed); a yield inside the re-run dq -> `yield`, no promotion.
  Earlier-stage yields return before promote (_run_stages).
- promote_build: fault_point("pipeline.before_promote") (already in the impl 08 registry) -> status promoted + finished_at -> CHECKPOINT
  -> close -> write_current -> retire previous (StoreBusy -> `model.build.retire_deferred`; other HernessError -> `model.build.retire_failed`
  WARNING, not raised since CURRENT already switched) -> cleanup_builds(post, keep_last, protect={build_id}) -> `model.build.promoted`.
- cleanup_builds: keep = protect ∪ CURRENT ∪ runs-pinned ∪ live-job builds; rules (2) both modes, (3) post, (4) retire remaining
  non-current promoted (quietly); `model.build.orphan_deleted` per pre-mode deletion; `model.build.cleanup` with mode + counts.
  Bad mode / keep_last outside 1-20 -> ConfigError.
- Carry-over M1 (req 4): a build named by a queued (incl. yielded) or running build_pipeline job — payload build_id, or state
  build_id when state stages_done holds `build` — is never deleted. A yield inside stage build stays a true orphan (U02-99).

## Tests (all IDs in function names, docstrings start with ID, pytestmark set)
- tests/unit/model/test_model_cleanup_builds.py (unit): UT02-48 x6 (running-run pin via select_runs spy, constant, orphan/retention
  rules, locked never deleted, bad args, yielded-build regression for req 4).
- tests/integration/model/test_model_build_promote.py (integration): IT02-32 x4 (5 promoted + pinned, keep_last=3; first promotion +
  unreadable previous -> retire_failed; full pipeline promotes and next retires; yielded enrich build survives another job's pre cleanup,
  deleted once its job is canceled).
- tests/security/test_st02_promote.py (integration): ST02-17 x5 (duplicate_key planted then --from-stage promote blocks; empty
  dq_result blocks; passing gate re-run then promotes; no promotion after score yield; no promotion after yield in dq re-run).
- tests/fault/model/test_model_promote_fault.py (fault): FT02-03, FT02-04 (child process tests.support.build_kill with HERNESS_ENV=test +
  HERNESS_FAULTS plan), FT02-05 (reader subprocess; Windows only, skipif POSIX).
- tests/integration/model/test_model_build_dq.py: IT02-29 / IT02-30 "promoted" halves added (T02-20 carry-over).
- tests/integration/model/test_model_build_pipeline.py: the "build stage promote is not available" assertion switched to: the full
  payload validates and every STAGE_ORDER stage has a unit.
- Support: tests/support/promote_builds.py (synthetic builds, run/job rows), tests/support/build_kill.py (child).
- Mutation evidence: dropping `_active_job_builds()` from keep fails both req-4 regression tests; skipping the dq re-run in
  stage_promote fails 4 ST02-17 tests and FT02-04.

Results (PYTHONUTF8=1, -p no:logging): new card tests 27 passed + fault 3 passed; tests/unit/model + tests/integration/model +
ST02 build/promote + tests/fault/model + tests/fault/connectors: 351 passed, 1 skipped (symlinks); tests/security -k st02: 11 passed;
--require-test-ids collect on new files OK.
Gates: ruff format/check clean, mypy clean (336 files), lint-imports 13 kept 0 broken, check_type_ownership 0, check_module_size 0.

## Deviations / spec notes
1. Live-job protection read path: `herness.store.ops.SqliteJobsBackend.list_jobs` (impl 08 ops area, L1) rather than
   `herness.core.jobs.queue.list_jobs`, because no jobs backend is bound outside the worker (composition root absent; unbound -> ConfigError).
   Spec note under U02-105.
2. `retire_failed` (new WARNING log) — a non-StoreBusy retire error after the CURRENT switch is not raised. Spec note under U02-104.
3. `_fail` skips the failed-mark for a promote-stage error after this job's gate passed, so the retry resumes at promote (FT02-04
   also covers a fault `error` action). Spec note under U02-103.
4. `cleanup_before_build` lives in `_build_promote` (budget), §2 row says so.
5. FT02-05: DuckDB 1.5.5 readers open the file with FILE_SHARE_DELETE on Windows 11 (verified), so a DuckDB reader never defers a
   deletion; the reader process holds a DuckDB read-only connection (blocks the retire -> retire_deferred) AND a plain file handle
   (blocks unlink -> deferred). Skipped on POSIX (unlink succeeds). Spec note under U02-105.
6. Test file basenames: test_model_cleanup_builds.py (unit) and test_model_build_promote.py (integration) — pytest rootdir import
   mode needs unique basenames.

## Carry-overs
- Closed: M1 (T02-19 review, widened by T02-19b) yielded enrich/score build protection; T02-20: promoted halves of IT02-29/30,
  ST02-17, dq re-run before promote, "not available" test switched.
- Remaining / open point: StoreBusy from write_current leaves the build `promoted` but not CURRENT; U02-98 step 3 resumes only
  `building` builds, so the retry starts a new build instead of "resuming at promote" (spec owner decision; noted under U02-104).
- Acceptance "herness pipeline on lake_small promotes and herness status shows the build" not exercised (CLI T09-23 out of scope here).

## Concerns
- build.py at 399/400: no room left; the next change to build.py needs another sibling split.
- Deviations 1-3 and 5 above need controller acceptance.

## Fix round 1 (review briefs/T02-21-review.md: I1, M1-M4; base ebfc91a)
Checkpoint: 46e034c wip(T02-21): fix round 1 - resume a promotion that failed after the status update. Final commit: 8e7af9b fix(model): resume a promotion that failed after the status update (T02-21) — all hooks passed, no SKIP.

- I1 (a): `_build_support.resolve_build(..., stages=STAGE_ORDER)` (new 5th parameter; build.py passes `payload.stages`)
  resumes a build whose status is `building`, or `promoted` when `promote` is the only requested stage not yet done.
  This applies to the saved-state resume and the payload `build_id` (a `--from-stage promote` retry no longer fails with "is promoted").
- I1 (b): `_build_promote.stage_promote`: if CURRENT already names this `promoted` build (`_promoted_current`, via list_builds),
  run only `promote.finish_promotion` (post cleanup + `model.build.promoted` with previous=None): no DQ re-run, no second switch.
  Otherwise the gate runs in this job (unless dq ran in this attempt) and `promote_build` runs in full, so a build is never
  switched in without a passing gate in the switching job. `promote.finish_promotion` = U02-104 steps 5-6, also used by promote_build.
- Failure path: `_fail` now asks `_build_promote.keeps_status(run, stage)` = stage promote and (dq ran in this attempt or the build
  is the promoted CURRENT), so the shortcut path's errors never mark the CURRENT build failed.
- M1: FT02-04 variant `pipeline.before_promote action: error:StoreBusy` in the child process: the build stays `building`, the retry
  resumes at promote and promotes. Mutation (always mark failed) fails it.
- M2: cap kept (impl 08 list_jobs has no paging, max 1000); documented in the U02-105 spec note.
- M3: `mode` added to the `model.build.cleanup` log row (the code already logged it).
- M4: pyproject.toml store-rw-restricted comment updated (both importers exist).
- Spec: the open-point sentence in the U02-104 note is replaced by the recovery rule; U02-103 note names keeps_status; §2 rows
  updated (promote.py exports finish_promotion; _build_promote lists keeps_status; _build_support mentions the promoted resume).
- New tests (IT02-32 in tests/integration/model/test_model_build_promote.py): write_current StoreBusy once, full pipeline -> retry
  resumes at promote, re-runs the gate, promotes; same for --from-stage promote; post cleanup StoreBusy once after the switch ->
  retry finishes with no gate re-run, no `store.warehouse.current_switched`, no failure; resolve rules for promoted builds.
- Mutations: no promoted resume -> 4 IT02-32 tests fail; no CURRENT shortcut -> the post-cleanup test fails; always mark failed ->
  the FT02-04 error variant and the post-cleanup test fail.
- Sizes: build.py 399/400, _build_promote.py 77/80, promote.py 254/260, _build_support.py 166/200, _build_stages.py 195/200; check_module_size 0.
- Results: unit+integration model, ST02 build/promote, fault model+connectors: 356 passed, 1 skipped. Gates clean (ruff, mypy 336,
  lint-imports 13 kept, type ownership 0, module size 0).
- Remaining concern: an explicit `--from-stage promote` on an old `promoted` build that is not CURRENT re-runs the gate and switches
  CURRENT back to it (a gated rollback). Retention retires such builds after every promotion, so this happens only in the I1 window.
  _build_promote.py is at 77/80 and promote.py at 254/260.

## Fix round 2 (review M5; base 8e7af9b)
Final commit: 91f634b fix(model): never resume promotion of a build older than CURRENT (T02-21) - all hooks passed, no SKIP.
- `_build_support._resumable(build_id, layout, done, stages)`: a `promoted` build is resumable only when `promote` is the only
  requested stage left AND CURRENT is absent, unreadable (HernessError from read_current -> write_current replaces it anyway)
  or not newer than the build (`current <= build_id`; IDs sort by time). A stale payload build_id -> ConfigError
  "build <id> is promoted"; a stale saved state -> new build. `building` builds unchanged.
- Spec: one sentence added to the U02-104 T02-21 spec note.
- Test: IT02-32 `test_it02_32_promoted_build_older_than_current_not_resumed` (older -> ConfigError / new build; CURRENT and newer
  promoted -> resumed; invalid CURRENT -> resumed). Mutation (drop the guard) fails it. All I1 recovery tests still pass.
- Sizes: _build_support.py 178/200; check_module_size 0. Gates clean.
- Results: unit+integration model, ST02 build/promote, fault model: 355 passed, 1 skipped (connectors fault tests not rerun: untouched path).
