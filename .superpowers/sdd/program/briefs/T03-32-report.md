# T03-32 report — Distillation job (run_distill, DistillReport, make_distill_handler)

Status: DONE_WITH_CONCERNS — committed c304f06 (hooks passed; builder killed by outage after commit; status line finalised by sub-controller).

## What was built
- herness/enrich/distill.py (U03-135..137): DistillReport (pydantic, frozen, extra=forbid; validator stopped => version is None), run_distill, make_distill_handler, re-exports accept_model, rollback_model (laya_admin) and YieldRequested (gpu). LlmFactory alias in the block "# T03-28 LlmFactory (local until pipeline.py merges; identical alias)". Orchestration: resume state saved after each step (prepared, teacher_done, trained, evaluated), teacher selection (OpenJev start + build_decider(samples_override=3) + health, else LlmDecider(votes=3) in a nested reasoning scope through one ExitStack), spot-check creation and question blocking, teacher phase, evaluation phase, report.
- herness/enrich/_distill_steps.py (private sibling per ruling): config and questions (check_decider_refs error -> ConfigError before any GPU scope), label sync + gold consolidation, gold exclusion (gold hashes + pending gold items), stop rule over the CURRENT chain, version dir (exist_ok=False -> StoreBusy), initial sample, active scoring + select_active, chunked teacher labeling into the cache (flush 2,000; yield -> flush + YieldRequested), teacher rows, request_gold, LanceDB vector reader, training set + train + candidate manifest, candidate gold inference.
- herness/enrich/labels.py: LabelStore.read returns an empty table when a kind dir has no part (the known ArrowInvalid on gold/ with only _reviews/_frozen).
- docs/impl/03-enrichment.impl.md: section 2 row for _distill_steps.py (budget 400), section 8.1 row enrich.distill.stopped, T03-32 spec note after U03-137.

## Files and line counts (budgets)
- herness/enrich/distill.py 377 / 390
- herness/enrich/_distill_steps.py 391 / 400 (new section 2 row, private sibling)
- herness/enrich/labels.py 379 / 380 (+3: empty kind dir -> empty table)
- tests: test_distill.py (UT03-128 x2, UT03-129 x5), test_distill_flow.py (IT03-15 x18), test_distill_fault.py (FT03-05 x4), _distill_support.py

## RED / GREEN
- RED: pytest tests/unit/enrich/test_distill.py -> ImportError: cannot import name 'distill' from 'herness.enrich' (collection error).
- GREEN: card tests 29 passed; tests/unit/enrich + integration/enrich + fault/enrich: 1067 passed, 2 skipped (platform/GPU skips).
- Coverage (card tests): distill.py 100% line, 32/34 branches; _distill_steps.py 100% line, 32/34 branches.
- Gates: ruff check/format clean, mypy (344 files) clean, lint-imports 13 kept, check_module_size 0, check_type_ownership 0.

## Deviations / spec notes (all recorded in the T03-32 spec note after U03-137)
- DistillReport defined in distill.py (spec types section does not list it under herness.core.types).
- LlmFactory local alias in the required block comment; pipeline.py not created.
- Config checks (load, check_decider_refs, question set, fingerprint registry) run BEFORE ctx.gpu_scope("decider"); everything else inside. Gives "ConfigError before GPU work"; reviewers may read "whole body" strictly.
- Resume state carries extra keys (parent, teacher_version, n_sample, blocked, gold_items_created, n_train, n_val, accepted_proposed, macro_metric, durations); invalid state -> ConfigError.
- Off-network rule: process_state().chains.config(client.name).off_network; unknown client/no registry -> treated as local (scope entered). CONCERN: no dedicated helper existed.
- Base checkpoint location undefined in spec: data/models/laya/base (base_checkpoint "convaiinnovations/laya"). CONCERN for the composition root / install docs.
- Round numbers: initial 0; active = CURRENT manifest round + 1; stop rule walks parent_version from CURRENT.
- Active rounds: rank once with per_round=max(per_round, per_round_llm_teacher), keep the prefix for the chosen teacher (select_active's greedy walk makes the prefix exact). Pool scoring results of CURRENT are not cached (rescored on resume).
- uncertainty uses CURRENT's laya calibration temperatures (softmax(log p / T)).
- Spot checks: this round's teacher rows in content-hash order; low-prob half uses p(answer).
- Blocking compares latest human label with latest-round teacher answer per (hash, question, fingerprint).
- labels.py edited (outside card files, justified): LabelStore.read on a kind dir with no parts raised ArrowInvalid; evaluate_candidate and gold_hashes() need an empty table.
- New log event enrich.distill.stopped (INFO; round, stop_reason) added to section 8.1.
- Private imports from siblings: evaluate._cache_rows, sampling._nearest, laya_models._read_manifest (precedent gold -> sampling._ordered_pool).

## Carry-overs honoured
- T03-30: evaluate_candidate called with blocked=<blocked questions>.
- T03-31: manifest.hyperparams records trainer name + seed (+ round, round_kind, epochs_run); weights_sha256 hashes TrainResult.files; checkpoints/ live in the version dir (excluded from hashing by verify_model_dir); YieldRequested("train") propagates -> handler yield.
- T03-29: samples, teacher rows and the training set exclude all gold hashes AND pending gold-item hashes (IT03-15 asserts).

## Concerns / carry-overs owed
- Dynamic-option questions (owning_team, options_source) are not resolved by run_distill (spec step 0 lists no resolve_dynamic_options); a real config with owning_team would hand the teacher a choice question without options. Owed to T03-28/T03-32 follow-up or a spec ruling.
- Laya base checkpoint directory convention (above) needs a spec/ops decision.
- Composition-root registration of make_distill_handler (register_handler("distill", ...)) is not done here (owned by the composition root).

## Commit
- c304f06 feat(enrich): T03-32 distillation job run_distill and handler (all pre-commit hooks passed incl. pytest-unit; no --no-verify, no SKIP).
Status: DONE_WITH_CONCERNS (concerns above: base checkpoint dir convention, off-network rule without a dedicated helper, dynamic options not resolved, config checks placed before the decider scope).

## Fix round 1 (review T03-32-review.md: test gaps only; no production change)
- Correction: the original report claimed IT03-15 asserted the pending-gold-item exclusion; it did not (no test seeded a pending purpose="gold" item). Now covered.
- I1: test_it03_15_pending_gold_item_hash_never_trains seeds a pending gold label_check for a NON-gold record hash; asserts n_sample 159 (pool minus that hash) and the hash is absent from teacher/ rows and from the training set (train + val).
- M1: test_it03_15_teacher_rerun_appends_no_duplicate_rows yields after teacher rows were appended (in the gold-request step), reruns the teacher step, asserts the teacher row count is unchanged and (hash, question) keys are unique.
- M2: test_it03_15_blocking_needs_at_least_100_reviews[99|100]: full disagreement with 99 reviews -> not blocked; 100 -> ["q_b"].
- Mutation probes (applied, test run red, reverted): P3 drop pending hashes from gold_exclusion -> pending_gold test fails; P9 drop the `key not in have` guard -> rerun test fails; P10a threshold 99 -> [99] case fails; P10b `>` instead of `>=` -> [100] case fails. All 4 killed.
- Results: card tests 33 passed; tests/unit/enrich + integration/enrich + fault/enrich 1071 passed, 2 skipped; ruff format/check clean, mypy clean, lint-imports 13 kept, check_module_size 0.
- Parked per sub-controller: M3 private imports, M4 decider-scope wording.
- Commit: 8ab8e6b test(enrich): T03-32 fix round 1 pending-gold exclusion, teacher rerun, review threshold (hooks passed incl. pytest-unit; no SKIP, no --no-verify).
