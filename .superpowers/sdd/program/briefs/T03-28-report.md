# T03-28 Pipeline — build report (w24-s03a)

Status: DONE_WITH_CONCERNS (job-state merge, see Carry-overs)
Commit: f07d06c feat(enrich): T03-28 run_enrichment pipeline composition root (single commit on e41d62c; hooks incl. full unit suite passed)
Worktree: D:\herness\.claude\worktrees\agent-a657e1ceca419c08f (branch worktree-agent-a657e1ceca419c08f, base e41d62c)

## What was built

- `herness/enrich/pipeline.py` (386/390): `StageName` + `STAGE_ORDER` (U03-141), `StageStatus`,
  `StageReport` (U03-142; pydantic, `validate_assignment`, `note` max 200 chars matching
  `^[a-z][a-z0-9_]*$` so only codes fit), `EnrichReport` (U03-143; frozen, `stages` always
  re-ordered to `STAGE_ORDER`), `LlmFactory` (the U03-136 alias, defined here), `YieldRequested`
  re-export (U03-152), `run_enrichment` (U03-144: the seam's call shape plus kw-only
  `force_full_recluster=False`), the run state `Run`, the stage wrapper `_Driver` (started /
  completed / degraded / failed / yielded logs, metrics, checkpoint), the GPU scopes, and steps
  9-10 (reasoning phase, deep ensemble pooling, `_llm_band`).
- `herness/enrich/_pipeline_stages.py` (371/390, private sibling; §2 row and spec note added):
  `prepare` (F03-01 steps 1-2) and the bodies of `text`, `embed`, `decide-primary`, link
  candidates + `decide-escalate`, `cluster`, `link`, `suggest`, `resolve`; `decide_members`
  (deep band members), `frame`, `degrade`, `device`.
- StageReport retypes in eight stage modules (table below).
- Spec (docs/impl/03-enrichment.impl.md): §2 row for `_pipeline_stages.py`; "Spec notes (T03-28)"
  paragraph at the end of §3.24.
- `.secrets.baseline`: the line number of the audited 03-impl entry shifted (3618 -> 3629) with
  the two doc insertions; no entry dropped, LF kept.

## Rulings / decisions made (each also in the spec note)

1. Composition-root duties. `register_deciders()` once in `prepare` (step 1); `build_decider` for
   laya / openjev / jev / llm in step 2 so `versions` is complete for every resolution of the run;
   OpenJev per F03-01 step 7 (`guard("decider:openjev")`, `release_cuda()`,
   `ctx.services.start`), stopped in `finally`; `ModelUnavailable` / `CircuitOpen` on start ->
   degraded `openjev_unavailable`, teacher None, everything deferred. `gpu_scope("decider")`
   around steps 4-10, nested `gpu_scope("reasoning")` for step 9 only (ExitStack: left on every
   path, also on YieldRequested and errors); no scope when no GPU stage runs (also when every GPU
   stage is resume-skipped). Stages take no GPU lock. `ResolveArgs` built by the pipeline.
   `llm_factory is None`, a factory `ModelUnavailable` / `CircuitOpen`, or `ModelUnavailable` on
   the reasoning switch -> `reasoning_unavailable`: auto cluster labels, no LLM call. Laya CURRENT
   missing / corrupt / tampered -> warning `laya_degraded`, teacher-primary, build completes
   (FT03-04). Never-swallow: the wrapper logs `enrich.stage.failed` (class only), marks the stage
   failed and re-raises unchanged; unknown `stages` value -> `ConfigError("unknown enrichment
   stage <name>")` before config load or any state write.
2. Too-few-vectors policy (cluster). U03-105 lists no ConfigError for a small window and degraded
   modes never block promotion (§6): a `ConfigError` from `run_cluster_stage` becomes degraded
   `too_few_vectors` (`cluster_run="skipped"`) only when the in-window incident count is below
   `max(pca_dims, min_cluster_size, min_samples + 1)`; otherwise it propagates (a real config
   fault). Classified by count, not by message text. IT03-01 exercises it (24 incidents < 64).
3. mark_final half-write window. A crash after `snapshot.json` says final but before `CURRENT`
   moved: the rerun of the same build ignores that snapshot (`load_assigned` wants `assigned`),
   runs against the old `CURRENT` (incremental, or full when due / no snapshot) and publishes on
   `finalize_clusters`. Not stuck; no `force_full_recluster` needed. Proven by
   `test_ft03_06_crash_between_mark_final_writes_rerun_is_not_stuck`.
4. Resume / idempotence. Checkpoint after every completed stage:
   `ctx.save_state({**entry_state, "enrich": {"build_id", "stages_done" (STAGE_ORDER order),
   "started_at"}})`. A rerun of the same build skips done stages except producers whose in-memory
   results a pending stage needs (`embed`->`suggest`, `decide-escalate`->`reasoning`/`ensemble`,
   `cluster`->`reasoning`/`resolve`, `reasoning`->`resolve`; fixed point) and reuses the first
   attempt's `started_at` as U03-83 `run_started_at`, so rows decided in a crashed run are
   spot-check candidates (w13-s03c carry-over closed). `text`, `link` and `resolve` empty their
   tables before running (§4.1 "a rerun rebuilds the table"): `enrich.decision` is never inserted
   twice on a rerun of the same build file (carry-over closed). `started_at` is an extra key next
   to the spec's `build_id`, `stages_done`.
5. Yield mapping. Stages flush then raise `YieldRequested`; the wrapper logs
   `enrich.stage.yielded`, the scopes exit, `run_enrichment` re-raises it unchanged; impl 02's
   `stage_enrich` returns `yield` and the handler `JobOutcome(status="yield")`. Shown end to end
   through the real `build_pipeline` handler (FT03-06): the first embed window is flushed (8 rows
   in LanceDB), `JobOutcome(yield)`, the rerun of the job embeds only the remaining 19 hashes.
   The pipeline checkpoint replay is shown at the `run_enrichment` boundary (text `resumed`,
   embed continues, `started_at` kept).
6. Same-night LLM phase (new finding). For a teacher-primary question the teacher's
   below-threshold answer stays `queue` (its chain is `llm` only); per the T03-21 note it goes
   "straight to the LLM phase", but `run_decide_escalate` only defers it on the NEXT run, so an
   unchanged lake never reached zero decider calls (IT03-04 failed with 42 LLM calls on run 2).
   After the teacher ran, the pipeline re-runs `resolve_frame` and defers every still-queued
   record (then the deferred pair items) to tonight's reasoning phase. Cost: one extra resolve
   SQL per night.
7. Execution order vs STAGE_ORDER. STAGE_ORDER lists `ensemble` before `cluster`; F03-01 pools at
   step 10 after `cluster` (8) and `reasoning` (9). F03-01 is followed: the OpenJev band member
   runs inside `decide-escalate` while OpenJev is up, the LLM member in `reasoning`, pooling in
   `ensemble`. `stages_done` and the report use STAGE_ORDER order; subsets never reorder.
8. The reasoning scope is entered whenever there is LLM work and an `llm` decider; the
   "off-network LLM profile -> no switch" rule is not detected (no such field reaches the
   pipeline) — carry-over.
9. OI-06: Laya `embed_fn` is the CPU-encoding fallback, built only when a choice question has more
   than 20 options; the precomputed option-vector lookup is not built — carry-over.
10. `EnrichReport.coverage` / `escalation_share` come from `enrich_resolved` right after
    `run_resolve` (U03-83's definitions); empty / None when `resolve` did not run in the call.
11. `Depth` is `herness.core.types.Depth` (OWN040: owner 06 defines the same literal).
12. Metrics: `herness_enrich_stage_duration_seconds` (record_histogram) and
    `herness_enrich_records_total` (record_counter) per completed stage, labels `{"stage"}`.

## Stage `_Report` retypes (for the merge agent)

Each: `class _Report(Protocol)` deleted; `from typing import ...` gains `TYPE_CHECKING` (drops
`Protocol` where unused); after the imports:
`if TYPE_CHECKING: from herness.enrich.pipeline import StageReport as _Report` (the alias keeps
every annotation line unchanged). No runtime import of `pipeline` from a stage module (no cycle).

| File | Import line | Lines (base -> now) |
|------|-------------|---------------------|
| herness/enrich/text.py | 29 | 259 -> 250 |
| herness/enrich/embed_stage.py | 34 | 320 -> 314 |
| herness/enrich/decide_stage.py | 53-54 (+ `StageStatus`; `_mark(..., status: StageStatus, ...)` line 104; module doc lines 7-8 reworded) | 389 -> 383 |
| herness/enrich/ensemble_stage.py | 36 (module doc lines 9-10 reworded) | 260 -> 255 |
| herness/enrich/cluster_stage.py | 49 | 388 -> 385 |
| herness/enrich/resolve.py | 45 | 349 -> 342 |
| herness/enrich/link_changes.py | 37 | 312 -> 305 |
| herness/enrich/mapping_suggest.py | 32 | 358 -> 354 |

Test stand-ins `tests/unit/enrich/_cluster_support.py` and `_embed_support.py` (`Report`
dataclasses) stay (tests are outside the mypy `files`; structural at runtime).

## Model seam

`herness/model/**` not edited. The `_hook("herness.enrich.pipeline", "run_enrichment")` seam now
loads the real module. The model tests replace `_load_run_enrichment`, so they are unaffected; no
model test was changed (see test summary).

## Carry-overs

Closed: `_Report` -> StageReport (eight modules); JobOutcome(yield) end to end; too-few-vectors
policy; mark_final window; crashed-run resume (spot-check window, `enrich.decision` re-insert);
`register_deciders` at the composition root; OpenJev lifecycle; reasoning scope; w13-s03c.

Controller note (mid-build, T02-19b job-context view) folded in: the checkpoint goes only
through `ctx.load_state()` / `ctx.save_state(...)` of the ctx passed to `run_enrichment` (no
module-level handle or other store); the pipeline writes only the `enrich` key on top of the state
the ctx returned on entry, so it clobbers no other key. Resume tests now use `MergingContext`
(a fake whose `save_state` merges like the view): UT03-139
`test_ut03_139_crash_then_resume_through_a_merging_context` (crash in `cluster`, build keys
kept, rerun skips `text`/`decide-primary`) and FT03-06 `test_ft03_06_checkpoint_resume_skips_done_stages`
(preempt in embed, build keys kept, resume continues). With T02-19b merged, the concern below is
resolved on impl 02's side (to be verified by the merge / verify agent).

Remaining (owner):
- T02-19 owner / impl 08 (CONCERN until T02-19b lands): the job state is one blob with replace semantics. impl 02
  saves `{"build_id","stages_done"}`; the pipeline saves `{**entry_state, "enrich": {...}}` with
  `entry_state = ctx.load_state()`. The real `JobContext.load_state()` returns the CLAIM-TIME state
  (impl 08 `_context_base.load_state`), so on the FIRST attempt of a `build_pipeline` job the
  pipeline's checkpoint drops the build keys impl 02 saved during that attempt; a crash inside
  enrichment then makes the next attempt start a new build (the old file is deleted as an
  orphan). On a yield impl 02's `_yield` rewrites only its own keys, dropping `enrich` (the rerun
  repeats enrichment: cheap and idempotent, FT03-06). Fix options: impl 08 `load_state()` returns
  the latest saved state, or impl 02 `_save_state` merges / owns the `enrich` key. Not fixable in
  impl 03 without hard-coding impl 02's state schema.
- T02-19 owner: seam -> plain `from herness.enrich.pipeline import run_enrichment`; `llm_factory`
  annotation -> `LlmFactory | None`; `stage_enrich` may now import `YieldRequested` from
  `herness.enrich.pipeline`.
- spec 11: re-point IT03-01/04/05/08 and FT03-04/06 from the card-local
  `tests/integration/enrich/_pipeline_env.py` (lake_small + 20 synthetic incidents through the
  real build handler) to `small_build`.
- impl 03 follow-ups: off-network LLM profile detection for the reasoning switch; OI-06 option
  vector lookup; F03-07 StoreBusy split (labels sync -> `labels_sync_skipped`, spot checks ->
  `spot_check_skipped`) needs `run_resolve` to expose the two steps — today a `StoreBusy` from
  `run_resolve` propagates (the job is retried).

## Tests

New files (56 tests): tests/unit/enrich/test_pipeline.py (17: UT03-132 x2, UT03-139 x15),
tests/unit/enrich/test_pipeline_stages.py (28, UT03-139 stage bodies),
tests/integration/enrich/test_pipeline_flow.py (5: IT03-01 x2, IT03-04, IT03-05, IT03-08),
tests/fault/enrich/test_pipeline_fault.py (6: FT03-04 x3 params, FT03-06 x3),
helper tests/integration/enrich/_pipeline_env.py (card-local small build).
FT03-01 (on U03-144) not built: no FT03-01 row was in the brief's test table.

- Card tests: 56 passed. Coverage: pipeline.py 99% line / branch 33 of 34; _pipeline_stages.py
  99% line (missing 139-140: ATTACH failure on an unreadable previous build) / branch 35 of 36.
- Mandated sweep `pytest tests/unit/enrich tests/integration/enrich tests/fault/enrich
  tests/unit/model tests/integration/model -q -p no:logging`: 1382 passed, 3 skipped
  (platform/CUDA skips). No model test changed (they replace `_load_run_enrichment`).
- Gates: ruff check / format clean, mypy (project) clean, card tests also clean under
  `mypy --explicit-package-bases`, lint-imports 13 kept, check_module_size 0,
  check_type_ownership 0, detect-secrets hook clean.
- The commit hook ran the whole unit suite on the first attempt: 8973 passed, 11 skipped.
- Acceptance: IT03-04 second run 0 Laya / OpenJev / LLM calls, 0 ticket embeddings, identical
  `enrich.decision`; UT03-139 GPU work only inside `gpu_scope`, `stages` validated before work.
- IT03-04 note: the encoder still encodes the in-memory mapping-suggestion texts each run
  (§4.7 MappingVectors are per call), never ticket texts.


## Fix round 1 (review briefs/T03-28-review.md, Needs fixes)

Commit 6345cee `fix(enrich): T03-28 review round 1` on top of f07d06c (no amend; hooks incl. full unit suite passed).

- I-1 fixed. `_teachers` catches only `AuthError` (jev without a key, §6): it logs
  `enrich.decider.auth_failed` ERROR (`decider`), adds the warning `<name>_auth` and drops the
  backend for the run. ConfigError, SchemaViolation, FatalError and the rest from `build_decider`
  now propagate. A disabled backend is never built (it is filtered by `.enabled`). LLM-factory
  `ModelUnavailable`/`CircuitOpen` keeps its degraded handling (F03-01 step 9).
  Tests:
  - UT03-139 `test_ut03_139_prepare_teacher_build_errors_other_than_auth_propagate`
    (ConfigError, SchemaViolation).
  - IT03-01 `test_it03_01_config_error_building_a_decider_fails_the_build`, through the real
    handler: the job returns ConfigError, `meta.build.status = 'failed'`, there is no GPU scope
    and no service call.
  - The existing step-2 test now expects `jev_auth`.
- I-2 fixed. `enrich.stage.degraded` WARNING is emitted from `_Driver._completed` whenever the
  final status is `degraded`. It fires once per stage, also for statuses set inside stage modules
  (decide_stage `_mark`: `openjev_unavailable`, `llm_unavailable`, `<name>_auth`/`_blocked`,
  `laya_degraded`). Fields are `stage`, `note`, `build_id`, `job_id` (codes only).
  `steps.degrade` now only sets status/note and logs the cause class at DEBUG
  (`enrich.stage.degrade_cause`). One exception remains: run_decide_primary (decide_stage.py:217,
  not edited) still logs its own `enrich.stage.degraded` (stage, error_class) per the §6
  Laya-timeout row (caught in U03-85). For a Laya timeout, decide-primary therefore has that event
  plus the wrapper's one. Test: UT03-139 degraded test, with the status set directly the way the
  stage modules do, asserting stage/note/build_id/job_id/level.
- M-1 fixed. An existing previous build that cannot be ATTACHed logs
  `enrich.pipeline.prev_unavailable` WARNING (`error_class` only, no path); the F03-16 migration
  is skipped. Test in the UT03-139 migrate test.
- M-2 fixed. The decider scope is entered only when a selected GPU stage can work:
  - `ensemble` counts only at `deep`.
  - `reasoning` counts only when `decide-escalate` or `cluster` runs in the same call (its only
    producers). `_reasoning_phase` applies the same rule, so `stages=["reasoning"]` is
    `skipped`/`no_work` without any scope.
  Test: parametrized UT03-139 `test_ut03_139_decider_scope_only_when_a_gpu_stage_can_work`
  (ensemble standard/deep, reasoning alone, reasoning+link deep, cluster+reasoning).
- M-3 confirmed identical, no code change. `run_cluster_stage` computes
  `now = clock.now() if rerun is None else snapshot.created_at`, then
  `since = now - window_days`, then `_WINDOW_SQL`: core.incident joined to
  enrich.text_redacted (entity 'incident') on `opened_at >= since`. `_window_size` uses the same
  `clock.now() - window_days` anchor and the same join/predicate. The rerun path (assigned
  snapshot) resumes from members.parquet and never reaches the too-few check, so the snapshot
  anchor never applies to it. The only difference is the milliseconds between the two
  `clock.now()` calls.
- Line budgets: `_llm_band` moved to the sibling as `llm_band` (the `pyarrow.dataset` import moved
  with it). pipeline.py 383/390, _pipeline_stages.py 388/390.

Tests after round 1:
- Card tests: 64 passed. Coverage: pipeline.py 99% line (2 partial branches);
  _pipeline_stages.py 99% line (142-143, the prev qsv SELECT error path, uncovered).
- Mandated sweep (tests/unit/enrich, tests/integration/enrich, tests/fault/enrich,
  tests/unit/model, tests/integration/model): 1392 passed, 3 skipped.
- Gates: ruff check and format clean, mypy 335 files clean, lint-imports 13 kept,
  check_module_size 0, check_type_ownership 0.
