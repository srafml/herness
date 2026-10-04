# T03-28 Pipeline — verify review (head f07d06c, base e41d62c)

**Verdict: Needs fixes** (2 Important, both small; everything else verified)

### Spec Compliance
1. ✅ Stage order. `STAGE_ORDER = get_args(StageName)` matches the spec verbatim (pipeline.py:59-63). Execution follows F03-01: prepare (steps 1-2) → text → [decider scope: embed, decide-primary, decide-escalate, cluster, reasoning, ensemble] → link, suggest, resolve. Subsets never reorder. Unknown stage → `ConfigError("unknown enrichment stage <name>")` before load_state/prepare (:159-165, :366). Ensemble only at deep (:335). The builder's choice to pool ensemble at step 10 is correct: F03-01 step 10 explicitly comes after cluster (8) and reasoning (9), STAGE_ORDER is the identifier/report order (U03-141), and the §3.24 spec note records this.
2. ✅ Yield/resume. YieldRequested is the same class re-exported from gpu. The wrapper logs `enrich.stage.yielded` and re-raises it unchanged (:224-226). impl 02 `stage_enrich` catches it (_build_stages.py:124-132). The checkpoint goes only through ctx.load_state/save_state, and the "enrich" key goes on top of the entry state (:249, :368). This works with the T02-19b `_HookContext` view (7e352ad: merges, then rewrites the build keys), checked by reading it. Resume and crash tests use MergingContext and pass.
3. ✅ GPU. The decider scope is a `with` around steps 4-10 (:376). The reasoning scope sits in an ExitStack for step 9 only (:276-283). Both are left on exception, yield and degraded paths (UT03-139 failure/yield tests). No scope when no GPU stage runs (:375). OpenJev: guard → release_cuda → services.start, with stop in `finally` (_pipeline_stages.py:261-283). Stages take no lock.
4. ✅ register_deciders is called once, in prepare (_pipeline_stages.py:110). It is idempotent.
5. ✅ 8 `_Report` retypes via TYPE_CHECKING `StageReport as _Report`. No runtime import of pipeline. Every stage module only shrank. All stage notes match `^[a-z][a-z0-9_]*$`, so validate_assignment cannot trip at runtime.
6. ❌ (partial) Counters, histogram, started/completed/failed/yielded events: OK, codes and counts only, no text (IT03-01 checks the dump and the logs). `enrich.stage.degraded` misses `stage`, and degraded statuses set inside stage modules are never logged (see I-2). The note is ≤200 chars of fixed vocabulary. The report stays under 64 KB (UT03-132).
7. ❌ (partial) The wrapper never swallows: it logs the class and re-raises. Degraded handling is as the table says for laya_degraded, openjev_unavailable, reasoning_unavailable, too_few_vectors (count-classified) and ops_busy. Exception: `_teachers` swallows every HernessError (see I-1). The other excepts are justified: _resumed ValueError, _sql→SchemaViolation, _laya (ConfigError/ModelUnavailable per the §6 Laya row), the LLM factory, decide_members, the reasoning switch, cluster and suggest.
8. ✅/⚠️ The extra `resolve_frame` + requeue after the teacher (:286-291) is consistent with the T03-21 "straight to the LLM phase" note and F03-05 step 5. Arguably it belongs in run_decide_escalate (T03-21), but it is documented in the §3.24 note. Clearing tables before text/link/resolve is safe: same build file, done stages are not rerun, and the full rebuild matches §4.1. The mark_final rerun is proven by the FT03-06 test. Keeping `started_at` is sound: it is an extra key.
9. ✅ IT03-01/04/05/08 run end to end through the real build_pipeline handler on a tmp warehouse. IT03-04 asserts 0 laya/openjev/llm calls, 0 embeddings and identical enrich.decision. IT03-08 asserts ConfigError with no gpu_scopes, gpu_requests or service calls.
10. ✅ Gates on f07d06c. Card tests: 56 passed. Coverage: pipeline.py 99% (missed branch 338->341), _pipeline_stages.py 99% (139-140 missed). Model unit+integration: 320 passed, 1 skipped. ruff check and ruff format --check: clean. mypy: 335 files clean. lint-imports: 13 kept. check_module_size and check_type_ownership: 0. Mutation probes, all red, all restored, tree clean:
   - (a) reasoning scope entered without the ExitStack → test_ut03_139_all_stages_decider_scope_with_nested_reasoning failed.
   - (b) save_state skipped → 5 UT03-139 tests failed.
   - (c) OpenJev stop skipped → test_ut03_139_escalate_starts_and_stops_openjev failed.

### ⚠️ Cannot verify here
- In IT03-01 cluster is always `too_few_vectors` (24 incidents), so naming, finalize_clusters and the LLM namer path are covered only by unit tests until spec 11 small_build lands.
- The builder wrote two spec notes itself (requeue, too_few_vectors rule). The controller should ratify them.
- The claim-time `load_state` concern holds until T02-19b merges. On a yield, impl 02's `_yield` drops the `enrich` key, so enrichment is redone. That is idempotent but costs work.

### Issues
#### Critical
none
#### Important
- **I-1** herness/enrich/_pipeline_stages.py:184-190. `_teachers` catches `HernessError`. That swallows ConfigError (bad secret ref, bad image tag), SchemaViolation and FatalError from `build_decider`. The spec says a ConfigError/FatalError fails the pipeline. Disabled backends are already filtered by `.enabled`, so only `AuthError` (jev without a key) is a legitimate degraded case. For AuthError the §6 row wants note/warning `<name>_auth` and an `enrich.decider.auth_failed` ERROR log, not `<name>_unavailable` and `enrich.decider.unavailable`. Fix: catch only AuthError (plus ModelUnavailable/CircuitOpen if needed) and use the §6 code and event. Add a test showing that a ConfigError propagates.
- **I-2** herness/enrich/_pipeline_stages.py:90 and pipeline.py:235-238. `enrich.stage.degraded` is logged without `stage`, `build_id` and `job_id` (§8.1: fields `stage`, `note`; §8.1 header: job_id/build_id when known). Degraded statuses set by stage modules (decide_stage `_mark`: openjev_unavailable, llm_unavailable, jev_auth/blocked, laya_degraded) never emit the event at all. Fix: emit `enrich.stage.degraded` (stage, note, plus the wrapper fields) from `_Driver._completed` when `report.status == "degraded"`. `degrade()` should only set status and note, or log error_class at DEBUG.
#### Minor
- **M-1** _pipeline_stages.py:131-134. An ATTACH failure on an existing previous build silently skips the F03-16 migration, with no log. A qsv change then re-decides everything with no trace. Log a WARNING code. Lines 139-140 are uncovered.
- **M-2** pipeline.py:375. The decider scope is entered when the only selected GPU stage is a no-op (e.g. `stages=["ensemble"]` at standard, or reasoning with no work). This switches the GPU class for nothing.
- **M-3** _pipeline_stages.py:321-323. The too_few_vectors classification anchors the window at `clock.now()`. Confirm that run_cluster_stage uses the same anchor, so the count matches the stage's own refusal.

## Re-review round 1 (fix commit 6345cee)

**Verdict: Approved**

- **I-1 closed.** `_teachers` now catches only `AuthError` (_pipeline_stages.py:189). It logs `enrich.decider.auth_failed` ERROR and adds the warning `<name>_auth`. ConfigError, SchemaViolation and FatalError now propagate.
  - New tests: UT03-139 (ConfigError, SchemaViolation) and an IT03-01 test through the real handler (build `failed`, no GPU scope or service call).
  - Mutation probe: re-widening the except to `HernessError` turned 3 tests red. Restored; tree clean.
- **I-2 closed.** `_Driver._completed` emits `enrich.stage.degraded` WARNING with stage, note, build_id and job_id whenever the final status is degraded. That includes statuses set by `_mark` in stage modules. `degrade()` now logs the cause at DEBUG only. The test sets the status directly, the way stage modules do.
- **M-1 closed.** An ATTACH failure logs `enrich.pipeline.prev_unavailable` WARNING with error_class only, no path. Tested.
- **M-2 closed.** `_gpu_work` drops `ensemble` below deep, and drops `reasoning` unless `decide-escalate` or `cluster` runs in the same call. `_reasoning_phase` applies the same rule. This is consistent with resume, because the `_CONSUMERS` producers rerun. Parametrized test covers it.
- **M-3 closed, no code change.** Confirmed: cluster_stage.py:167-169 uses `clock.now() - window_days` with the same join and predicate. The rerun path resumes from members.parquet and never reaches the too-few check.
- **Builder concern (decide_stage.py:217 double event): acceptable, park it.**
  - On a Laya timeout, decide-primary emits two `enrich.stage.degraded` WARNINGs: its own (stage, error_class) and the wrapper's (stage, note, ids).
  - Both carry codes only (TH03-03), so this is only log noise.
  - decide_stage.py belongs to T03-21; leave it out of this card. Carry-over for T03-21: drop or downgrade that line to a DEBUG cause event.
- **Gates on 6345cee:**
  - Card tests: 64 passed. Coverage 99% for both files (142-143 and 3 partial branches uncovered).
  - Model unit+integration: 320 passed, 1 skipped.
  - ruff check and ruff format --check: clean. mypy: 335 files clean. lint-imports: 13 kept. check_module_size and check_type_ownership: 0.
