# T02-19b review (verify agent) — worktree agent-ade9bf6491b3cead2, head 7e352ad (base e41d62c)

### Spec Compliance
- ✅ (1) Yield from score: `_build_stages.py:177` returns `"yield"` when the scoring report's `flags` holds `yielded` (checked after the report is stored and CHECKPOINT runs). `_run_stages` then goes to `_yield`: `score` is not added to `stages_done`, the outcome is `JobOutcome(yield, {"build_id"})`, and dq/promote do not run. `_build_stages.py:166` clears `finished_at` at stage start, as enrich does at `:128`. IT02-26 `test_it02_26_score_yield_leaves_build_unfinished` (integration test file :516) asserts the yield outcome, `stages_done == []`, no `stage score done` heartbeat, status `building` with `finished_at` NULL, and CURRENT still None. On resume, step 6 sets `finished_at` again (resume test :561).
- ✅ (2) Checkpoint preserved, checked against the real JobContext. `ContextBase.load_state` (`herness/core/jobs/_context_base.py:149-152`) returns a deep copy of `self._row.result["state"]`, and `_row` is never refreshed during an attempt, so it always returns the attempt-start state. `InlineJobContext.save_state` → `save_job_state` (`herness/store/ops/jobs.py:230-233`) replaces the whole `result = {"state": …}`, and the child context does the same through `SaveStateMsg`. With these semantics a plain `{**ctx.load_state(), build keys}` merge would still lose the in-attempt `scoring` key. `run_scoring._save` (`scoring.py:293-300`) builds `{**state_loaded_at_its_start, "scoring": …}` and would also drop this attempt's `build_id`/`stages_done`. The builder's `_BuildRun.state` plus the `hook_context` view handles both directions: `save_state` merges the build keys over `run.state`, and the view's `load_state`/`save_state` go through `run.state`, with the build keys reapplied. The resume test uses `AttemptJobContext` (:549), which copies the real attempt-start `load_state` behaviour, and the real `run_scoring`.
- ✅ (3) Regression tests fail without the fix. I probed this on scratch copies under `%TEMP%\t02-19b-verify` (git archive of HEAD; the worktree was not touched):
  - pA, yield check reverted to `return "done"`: 3 failed (UT02-78[yield], IT02-26 score_yield, IT02-26 resume).
  - pB1, `save_state` reverted to the two-key save: 2 failed (UT02-78 hook_context, IT02-26 resume).
  - pB2, `hook_context` returns raw `ctx`: 2 failed (the same two).
  - pC, `clear_finished` removed: the UT02-78 cases and IT02-26 score_yield failed. My replace also removed the identical enrich line, so IT02-23 failed too, as expected.
- ✅ (4) Deviations:
  - `hook_context` forwards every JobContext member except state through `__getattr__`. That covers `job`, `job_id`, `kind`, `attempt`, `services` and `stop_reason` (properties that resolve on the underlying ctx), plus `should_yield`, `heartbeat`, `gpu_scope` and `require_gpu_class`. `run_scoring` uses only `load_state`, `save_state`, `heartbeat` and `should_yield`. The real `run_scoring` in the resume IT exercises `heartbeat` and `should_yield` through the view.
  - The view is a `cast`; it is not structurally checked against the Protocol (see Minor 1). mypy is clean.
  - Score clears `finished_at` unconditionally. This matches enrich's implementation (T02-19 ruling: "enrich clears finished_at unconditionally") and the brief's enrich-yield rule.
  - The brief's unit IDs are wrong in the spec (U02-104 is `promote_build`, U02-110 is `120_stg_jira.sql`). Moving the notes to U02-101 (yield), U02-98 (checkpoint) and a short note under U02-104 is correct.
  - `_build_state.py` is 51/60 and has a §2 row.
  - Line counts: `build.py` 396/400, `_build_stages.py` 195/200.
  - `herness/metrics/scoring.py` is unchanged; it is not in the diff stat.
- ✅ Gates run by me:
  - `pytest tests/unit/model tests/integration/model`: 325 passed, 1 skipped (symlink).
  - `ruff check` and `ruff format --check`: clean.
  - `mypy`: 334 files, no issues.
  - `lint-imports`: 13 kept.
  - `check_module_size`: exit 0.
- ⚠️ Not verified beyond the spec text:
  - Orphan consequence (T02-21 carry-over M1, now wider). A score-only rerun on a completed unpromoted build now NULLs `finished_at`. If that job is killed, or it yields and another `build_pipeline` job runs before it resumes, `delete_orphans` (`_build_support.py:117-128`, protect = the other job's build only) deletes the build.
    - Per spec this is consistent: the brief mandates the enrich-yield rule, and U02-100 has the same exposure.
    - It is a new exposure for score-only rescoring of a completed build. T02-21 must protect builds referenced by a yielded or queued job's state, not only `finished_at IS NULL` ones.
  - Whether `finish_yield` keeps `result.state` for the next attempt belongs to impl 08 and is outside this diff. The resume test models it.

### Strengths
- The real attempt-start `load_state` semantics were identified and modelled in a test double (`AttemptJobContext`). The resume test drives the real `run_scoring` and real facts.
- The fix is small and local, and scoring.py is untouched.
- The yield check runs after the report is stored and CHECKPOINTed, so the partial scoring output is durable before the yield.

### Issues
#### Critical (Must Fix)
- None.
#### Important (Should Fix)
- None.
#### Minor (Nice to Have)
1. `herness/model/_build_state.py:49-51`: `hook_context` returns `cast("JobContext", _HookContext(run))`, so mypy never checks that the view satisfies the Protocol. Everything works today through `__getattr__`. A `_conforms`-style static check, like `tests/support/build_harness.py`, or explicit forwarding would make a future Protocol change visible.
2. `herness/model/_build_state.py:37-38`: `__getattr__` has no guard for a missing `_run`. A copy, pickle or unpickle of the view would recurse. This is not reachable today.
3. `herness/model/_build_state.py:44-47`: the view's `save_state` merges (`{**run.state, **state}`) instead of replacing, so a hook can never delete a key. This differs from the real `save_state` contract. It is harmless for `run_scoring`, which always writes the full state, but it is not called out in the U02-98 note.
4. `docs/impl/02-data-model.impl.md:2396` and `:3616`: U02-101's Tests row now lists UT02-78, but the UT02-78 catalog row still names only U02-134 and U02-98, and its Given/Expect do not describe the new stage_score and hook_context cases. This is the same mislabel class as parked M6. The IT02-26 catalog row was not extended for the yield and resume cases either.
5. `herness/model/_build_stages.py:128` and `:166`: identical `clear_finished` line and comment in enrich and score. This is trivial duplication.

### Assessment
**Task quality:** Approved
**Reasoning:** Both defects are fixed correctly under the real job-context semantics (attempt-start `load_state`, whole-state replacement on save). Each fix has regression tests that I confirmed fail when it is reverted, and all budgets and gates pass. The remaining items are traceability and robustness polish, plus the already-parked T02-21 orphan protection.
