# T02-19b report — honour score-stage yield, preserve the scoring checkpoint

Status: DONE_WITH_CONCERNS (see Deviations/Concerns). Worktree agent-ade9bf6491b3cead2, base e41d62c.
Commits: wip e73793f (code+tests); final 7e352ad fix(model): honour score-stage yield and preserve scoring checkpoint (T02-19b) (docs §2 row + spec notes). Both commits passed the real pre-commit hooks.

## What changed
1. `stage_score` (herness/model/_build_stages.py) returns "yield" when the scoring report's `flags`
   contains `yielded` (checked on `report.model_dump(mode="json")`, after storing the report and
   CHECKPOINT). `score` is then not in `stages_done`, the job returns JobOutcome(yield), no later
   stage (dq, promote) runs -> a build with unfinished scoring/`check` is never promotable.
   The stage now starts with `update_build_row(clear_finished=True)` like enrich, so a yielded score
   leaves `finished_at` NULL (enrich-yield rule); end-of-run sets it again when all stages finish.
2. Checkpoint preservation: new private sibling herness/model/_build_state.py (51/60):
   - `save_state(run)` saves `{**run.state, build_id, stages_done}` (replaces build.py `_save_state`).
   - `_BuildRun.state` holds the latest job state (initialised from `ctx.load_state()` once).
   - `hook_context(run)`: the ctx passed to `run_scoring` — delegates everything to ctx via
     `__getattr__`, but `load_state`/`save_state` go through `run.state` (merge, then build keys re-applied).
   WHY a view and not a plain `ctx.load_state()` merge: real job contexts (`_context_base.load_state`)
   return `row.result["state"]` from the attempt START, not what was saved during the attempt. A plain
   merge would still wipe the in-attempt `scoring` key; and `run_scoring`'s own `_save` (state loaded at
   its start) would wipe this attempt's build_id/stages_done. The view fixes both directions.
   The scoring key format (`scoring: {build_id, config_hash, steps_done}`) is untouched (scoring.py not edited).

## Files / line counts
- herness/model/build.py 396/400 (was 398; `_save_state` removed, `state` field, state read once)
- herness/model/_build_stages.py 195/200
- herness/model/_build_state.py 51/60 (new; §2 row added in docs/impl/02-data-model.impl.md)
- tests/unit/model/test_model_build_handler.py (+3 UT02-78 test cases)
- tests/integration/model/test_model_build_enrich_score.py (+2 IT02-26 tests; 2 existing IT02-26
  assertions changed from `ctx` identity to `ctx.job` identity, because run_scoring now receives the view)
- docs/impl/02-data-model.impl.md: §2 row `_build_state.py` (budget 60); spec notes (T02-19b) under
  U02-101 (yield propagation + clear_finished + hook view), U02-98 (checkpoint preservation),
  U02-104 (not reachable after a yielded score).

## Tests
- UT02-78: `test_ut02_78_stage_score_returns_yield_when_scoring_yielded[yield|done]`,
  `test_ut02_78_hook_context_keeps_scoring_checkpoint`.
- IT02-26: `test_it02_26_score_yield_leaves_build_unfinished` (completed build -> score-only job with
  yielded report: outcome yield, stages_done [], finished_at NULL, status building, CURRENT None);
  `test_it02_26_scoring_checkpoint_survives_yield_and_resume` (FT04-03-style resume, REAL run_scoring and
  real materialize_facts on lake_small, metrics/check step functions replaced by recorders; ctx subclass
  `AttemptJobContext` whose load_state returns the attempt-start state like real contexts; job 1 yields
  after metrics with state {build_id, stages_done:[build,enrich], scoring:{.., steps_done:[metrics]}};
  job 2 resumes at score, metrics skipped, only check runs, done, finished_at set).
- RED (before fix): 5 failed — 'done' == 'yield'; finished_at not cleared; scoring key missing from
  saved state; ('done', ['metrics']) == ('yield', ['metrics']).
- GREEN: `pytest tests/unit/model tests/integration/model -q` 325 passed, 1 skipped (symlink).
  Coverage: _build_state 100 %, _build_stages 100 % (branch), build.py 99 % (361->363 pre-existing).
- Gates: ruff format/check clean, mypy (herness, tools) clean, lint-imports 13 kept, check_module_size 0;
  wip commit ran the full pre-commit hook set (incl. pytest-unit) green.
- Resume test did NOT hit the facts hash conflict (facts rematerialised on unchanged data -> same hash).

## Deviations
- Brief said spec notes "under U02-104 (yield propagation) and U02-110 (checkpoint preservation)".
  In docs/impl/02 U02-104 is `promote_build` and U02-110 is `120_stg_jira.sql`. Notes placed where the
  behaviour lives: yield -> U02-101 (plus a short "not reachable" note under U02-104); checkpoint -> U02-98.
- `run_scoring` receives a delegating view of ctx (not `ctx` itself) — U02-101 text says `ctx=ctx`;
  recorded in the spec note. Two existing IT02-26 identity assertions adjusted to `ctx.job`.
- score now clears finished_at at stage start (mirrors enrich, per dispatch); a killed score job on a
  previously completed build leaves it orphan-eligible — same parked M1 risk as enrich (T02-21).

## Concerns / carry-overs
- T03-28: impl 03's own checkpoint key is still saved through raw `ctx` and wiped by the build's
  next save within the same attempt (same defect class); route enrich through `_build_state.hook_context`
  when run_enrichment lands (IT02-25 asserts ctx identity and would need the same adjustment).
- T02-21: M1 orphan protection now also covers a yielded score (finished_at NULL).
- Facts hash conflict on enrich rerun (w22-s04) untouched (did not block).
