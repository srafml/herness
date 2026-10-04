# T03-32 verify review — Distillation job (head c304f06, base 7dff609)

**Verdict: Needs fixes** (one Important, test-only; production code is correct on every probed path)

## Evidence run
- `pytest tests/unit/enrich/test_distill.py tests/integration/enrich/test_distill_flow.py tests/fault/enrich/test_distill_fault.py -q -p no:logging`: 29 passed (47 s).
- Coverage (card tests, branch on): distill.py 224 stmts 0 miss, 34 br / 2 partial (224->207, 240->239); _distill_steps.py 241 stmts 0 miss, 34 br / 2 partial (222->220, 302->300). 100 % line, ~94 % branch. OK (>= 90 / 85).
- `ruff check .` clean; `ruff format --check` on the 7 touched files clean; `mypy` (project config) 344 files, no issues (mypy on explicit test-file paths trips the known module-path issue, not a finding); `lint-imports` 13 kept / 0 broken; `tools.check_module_size` exit 0.
- Lines: distill.py 377/390; _distill_steps.py 391/400 (new §2 row, private sibling, imported only by distill); labels.py 379/380.

## Spec (numbered checks)
1. ✅ Handler. `make_distill_handler(llm_factory) -> Callable[[JobContext], JobOutcome]` (distill.py:357-377), one arg (R-42), reads `ctx.job.payload.get("round_kind", "initial")`, closed set -> ConfigError (non-string too, UT03-129), YieldRequested -> `JobOutcome(status="yield")`, done -> `result=report.model_dump(mode="json")`, other HernessErrors propagate. `JobKind` includes "distill" (herness/core/types/jobs.py:34); `register_handler(kind, handler)` (core/jobs/handlers.py:28) accepts it. Registration belongs to the composition root (U03-137 Purpose); nothing else owed by this card.
2. ✅ (accepted deviation) GPU scopes. Body inside `ctx.gpu_scope("decider")` (distill.py:318); LLM teacher enters nested `reasoning` on the step-3 ExitStack (distill.py:185-186, 262), closed at step 8 before `release_cuda()` (279) and before training. OpenJev path registers `services.stop` on the same stack. Deviation: `steps.load_run` (config, check_decider_refs, question set, fingerprint registry) runs before the decider scope. Judgement: acceptable. The spec Errors row requires "ConfigError before GPU work fails the job", and entering the scope is itself a GPU swap (spec 08 `gpu_scope` -> `require_gpu_class`); loading config is not GPU body. Recorded in the T03-32 spec note. Residual ConfigErrors raised inside the scope (active round without CURRENT, invalid saved state) are cheap and fine.
3. ✅ Resumable. `_advance` saves `{"distill": state}` after prepared/teacher_done/trained/evaluated (distill.py:144-150); rerun skips completed steps; teacher rows idempotent per (round, decider, hash, question) (_distill_steps.py:216-222); spot checks via `create_if_absent`; version dir created once (exist_ok=False -> StoreBusy) and reused from state; CURRENT never written (asserted in IT03-15). See M1 (teacher-row idempotence not exercised).
4. ✅ Inputs/outputs. Uses stratified_sample/select_active (T03-29), build_training_set/select_trainer/TrainHyper (T03-31), evaluate_candidate (T03-30), request_gold/consolidate_gold. Manifest: status candidate, weights_sha256 over TrainResult.files, hyperparams trainer+seed+round+round_kind+epochs_run (_distill_steps.py:362-374); calibration.json + eval.json via evaluate_candidate; DistillReport fields exactly as U03-135. Events: teacher_selected, step_completed, question_blocked, spot_check.created, new enrich.distill.stopped (added to §8.1). Private sibling imports (`evaluate._cache_rows`, `sampling._nearest`, `laya_models._read_manifest`; _distill_steps.py:35,39,44): same package, precedent gold.py -> sampling._ordered_pool; `verify_model_dir` would hash every ancestor's weights only to walk the chain. Acceptable; M3.
5. ✅ code / ❌ test evidence (I1). Gold hashes + pending purpose=gold items excluded from sample, active pool, teacher rows and training (_distill_steps.py:121-126, 280, 317-318, 349-356); gold labeled into the cache only. Spot-check sizing `max(min, min(max, floor(1 %)))` capped by rows, half hash-order, half p<0.7 (distill.py:203-229), `create_if_absent("label_check", ...)` with U03-83 keys/blocking statuses/scope. Blocking at > block_disagreement with >= 100 reviews (distill.py:232-250); `evaluate_candidate(blocked=...)` (294); blocked questions also dropped from training (_distill_steps.py:354). Stop rule min_gain/patience/max_rounds (_distill_steps.py:137-158); stopped => version None (validator + `_report`).
6. ✅ Budgets as above. labels.py change (labels.py:176-178) returns the documented "empty table with its schema when none" for a kind dir holding only `_`-prefixed subdirs; no caller relies on the old ArrowInvalid (grep). check_module_size exit 0.
7. ✅ (with gaps, see I1/M1/M2) Tests present: UT03-128 x2, UT03-129 x5, IT03-15 x18 (gold never in training set asserted, test_distill_flow.py:88-95), FT03-05 x4 (manifest + eval.json teacher "llm", scope order). IDs in names and docstring first lines; module pytestmark (unit/integration/fault, matching sibling fault files).
8. ✅ Gates green (see Evidence).

⚠️ Cannot verify here / cross-card:
- `GPU_SLOT_KINDS` is `{"build_pipeline"}` (core/jobs/queue.py:57); a `distill` job must therefore be enqueued with `gpu_class="decider"` to run on the GPU slot ("The job runs on the GPU slot"). Owned by the enqueuer (CLI/scheduler, R-45), not this card.
- Composition-root `register_handler("distill", make_distill_handler(factory))` and the `data/models/laya/base` checkpoint convention (builder concern): owed elsewhere.
- Dynamic-option questions (owning_team) are not resolved before teacher labeling (builder concern); needs a spec ruling, spec step 0 does not list it.

## Mutation probes (each reverted with `git checkout -- <file>`)
| # | Mutation | Result |
|---|----------|--------|
| P1 | sample `exclude_hashes=frozenset()` | KILLED: IT03-15 initial, resume; FT03-05 |
| P2 | gold rows appended to teacher/ + training gold filter removed | KILLED: IT03-15 `assert not trained & gold` (line 93) |
| P3 | drop pending gold items from `gold_exclusion` | SURVIVED (22 passed) -> I1 |
| P4 | DistillReport validator disabled | KILLED: UT03-128 |
| P5 | default round_kind "active" | KILLED: UT03-129 |
| P6 | YieldRequested re-raised instead of yield outcome | KILLED: UT03-129 |
| P7 | reasoning scope skipped | KILLED: 3 FT03-05 tests |
| P8 | `evaluate_candidate(blocked=frozenset())` | KILLED: IT03-15 blocking |
| P9 | teacher-row idempotence (`key not in have`) removed | SURVIVED -> M1 |
| P10 | `>= MIN_REVIEWS` guard removed | SURVIVED -> M2 |
| P11 | min_gain stop rule disabled | KILLED: IT03-15 stop_rule |
| P12 | spot-check size ignores 1 % / max | KILLED: IT03-15 sizes |
| P13 | low-probability filter removed | KILLED: IT03-15 sizes |

## Findings
### Critical
- none

### Important
- I1 tests/integration/enrich/test_distill_flow.py (missing test) / herness/enrich/_distill_steps.py:121-126: the T03-29 carry-over "samples, teacher rows and the training set exclude pending gold-item hashes" is implemented but never exercised. No test seeds a pending `label_check` item with `purpose="gold"`, so `pending` is always empty (P3 survives; line coverage is 100 % only because the comprehension iterates nothing). The report claims "IT03-15 asserts" this; it does not. It is a TH03-04 mitigation the card lists. Fix: in an IT03-15 test create a pending purpose=gold item for a non-gold record hash before `run_distill` and assert that hash is absent from the sample/teacher rows and from `env.trainer.seen[-1]` train+val.

### Minor
- M1 herness/enrich/_distill_steps.py:216-222: teacher-row idempotence on a rerun of the teacher phase (crash after append, before `_advance`) is untested; the teacher-yield test yields before any append and the train-yield test skips the phase (P9 survives). Add a rerun-after-append case (e.g. `request_gold_items` fails once) asserting no duplicate teacher rows / spot-check items.
- M2 herness/enrich/distill.py:246: the ">= 100 reviews" threshold is untested (fixture uses exactly 100 at rate 1.0; P10 survives). Add a 99-review case that does not block.
- M3 herness/enrich/_distill_steps.py:35,39,44: three private cross-module imports; acceptable intra-package with precedent, consider public helpers in their owners later.
- M4 herness/enrich/distill.py:317 vs U03-136 "whole body": config checks precede the decider scope; justified and recorded in the spec note; spec owner to confirm wording.

## Worktree state
All probes reverted; `git status --short` clean, `git diff` empty. Temp files only under C:\Users\santh\AppData\Local\Temp\w26-s03\verify.

## Re-review round 1 (fix commit 8ab8e6b, tests only)

**Verdict: Approved**

Scope: I1, M1, M2 (M3, M4 parked by ruling). Diff: only tests/integration/enrich/test_distill_flow.py (+73/-4): `_blocking_reviews` gains `n_reviews`; three new IT03-15 tests (IDs in names and docstring first lines, module pytestmark unchanged). ruff check / format --check on the file clean.

- I1 resolved: `test_it03_15_pending_gold_item_hash_never_trains` (test_distill_flow.py:388-411) creates a pending `purpose="gold"` label_check item for a non-gold pool record through `create_if_absent` with the U03-83 keys/scope, then asserts the sample shrinks to 159, the hash is absent from teacher rows, and absent from train+val of the training set.
- M1 resolved: `test_it03_15_teacher_rerun_appends_no_duplicate_rows` (414-437) yields once from `request_gold_items`, i.e. after the teacher rows were appended and before `_advance`, then reruns and asserts an unchanged row count and unique (hash, question) keys.
- M2 resolved: `test_it03_15_blocking_needs_at_least_100_reviews` (440-447) parametrized 99 -> not blocked, 100 -> blocked.

Card tests: 33 passed (unit + integration + fault).

Probes (each reverted with `git checkout -- <file>`):
| # | Mutation | Result |
|---|----------|--------|
| P3 | drop pending gold items from `gold_exclusion` | KILLED: test_it03_15_pending_gold_item_hash_never_trains |
| P9 | teacher-row idempotence (`key not in have`) removed | KILLED: test_it03_15_teacher_rerun_appends_no_duplicate_rows |
| P10 | `>= MIN_REVIEWS` guard removed | KILLED: blocking_needs_at_least_100_reviews[99] |
| P10b | `>=` changed to `>` | KILLED: blocking_needs_at_least_100_reviews[100] and spot_check_disagreement_blocks_a_question |

New findings: none. Worktree: `git status --short` clean, `git diff` empty after all probes.
