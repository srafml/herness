# T04-13 review — Scoring runner, validate and metrics steps (base 3f61b67, head 6f88f5b)

**Verdict: Approved.** The card's own code has no Critical or Important defect. One Important item is cross-card: it lives in the T02-19 seam, which the w22-s04 ruling says not to edit here. It must be tracked as a blocker before `score` + `promote` runs in production.

### Spec Compliance
- ✅ U04-55 STEPS: fixed order (scoring.py:42).
- ✅ U04-56 run_scoring:
  - Signature: build_id positional; steps, con and ctx keyword-only; con is required.
  - Preconditions are checked in the spec order: unknown step, then read-only connection, then the `meta.build` status. The messages match the spec verbatim. The unknown step name is cut to 64 characters, which does not matter for normal names.
  - The checkpoint compares build_id and `config_hash(cfg)`. A mismatch or a malformed checkpoint resets done.
  - Validate runs on every call and is never checkpointed.
  - Each step runs in its own transaction (BEGIN, then COMMIT or ROLLBACK). QueryError and duckdb.Error become `SchemaViolation("<step> failed: ...")`.
  - After each step: save_state keeps the other keys of the job state, then heartbeat `scoring:<step>`, then the log line and the duration histogram.
  - The yielded flag and `metrics.scoring.completed` are in place.
- ✅ U04-57: `missing_required_columns` covers absent columns and all-NULL columns. Identifiers are pattern-validated and quoted, and the presence query uses bound parameters. The `score_metric_disabled` rows follow the spec: delete the old rows first, then write warn / value=len / threshold 0 / passed false / details {metric, columns}. The `metric_disabled` log is written. A missing fact table gives the verbatim SchemaViolation.
- ✅ U04-58: DDL verbatim. Loops cover sorted enabled minus disabled metrics x catalog grains x 5 periods. Each query goes through `run_recorded("metrics", into=IntoSpec(..., "append", "query_id"))`. QueryError gives the verbatim SchemaViolation.
- ✅ U04-59 / U04-60 (7 base checks per the ruling):
  - Each check is a static segment of checks.sql.j2. The only bind is `s_count_metrics`, passed through `p()`. There is no interpolation.
  - Every check runs through `run_recorded(producer="score")`. value and n_bad are copied from the recorded row. `passed` is derived by DuckDB in the INSERT.
  - A missing input table is caught by the information_schema pre-check and recorded as passed=true, details.skipped.
  - The `check_failed` log and the failures counter are in place.
- ✅ Numbers provenance (check 1): every stored metric, check value and n_bad comes from a recorded SELECT. `passed` and the disabled `value` are computed in SQL. Python only copies values.
- ✅ Seam (check 5): matches `_RunScoring` in _build_stages.py:53. stage_score calls it after `CHECKPOINT`, so no transaction is open. The UT02-78 tests (`-k UT02_78`) pass: 6 passed.
- ✅ Tests: UT04-110, 111, 112, 113, 114, 115, 116, 118 and 121, plus FT04-01 (metrics part: OS kill during `check`, per the ruling) and FT04-03 (two variants).
  - Every test name and docstring carries its ID, and pytestmark is set (`unit` / `fault`, as in the other tests/fault files).
  - The assertions check the actual rows: dq rows, evidence params, result_hash re-computation, and before/after snapshots of metric_value, dq_result, evidence and build.
- ✅ Accepted deviation (ledger): `check` is not checkpointed when an invariant fails. scoring.py:299-301 follows this, and UT04-115 asserts `saved_states == []`.
- ⚠️ Cannot verify from diff:
  - BT04-02 / BT04-06 timing. These benchmarks are not in this card.
  - Real `config_hash(HernessConfig)` behaviour in `run_scoring`: the tests patch `config_hash` to a constant. mypy confirms the call types.

### Gates re-run by the reviewer
- Card tests with `--cov --cov-branch`: 32 passed in 169 s. Coverage is 100% line and 100% branch on both scoring.py (206 statements, 56 branches) and _scoring_checks.py (50 statements, 10 branches).
- UT02-78: 6 passed.
- ruff check: clean. ruff format --check: clean.
- mypy: 0 issues in 306 files.
- lint-imports: 13 kept, 0 broken.
- check_module_size: exit 0. scoring.py is 368/390, _scoring_checks.py 111/130 (row added to §2), checks.sql.j2 47/200.
- C12: no `herness.store._warehouse_rw` import in herness/metrics, confirmed by the UT04-121 AST scan.

### Strengths
- Every step's writes (table DDL, rows and evidence) sit in one transaction. FT04-01 proves a kill leaves no partial rows and that the restart equals a clean run. FT04-03 proves the earlier table survives a failed rewrite unchanged.
- The unbuilt steps (funding, org, levers, portfolio) are never checkpointed, so T04-21 can swap them in without stale state.
- UT04-118 re-hashes the stored rows and compares them to `meta.evidence.result_hash`, which is a strong TH04-06 test.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix) — cross-card, owner T02-19 (not fixable here under the w22-s04 ruling)
1. **A yielded `run_scoring` can let a build be promoted without `check`.**
   - What happens: `stage_score` (herness/model/_build_stages.py:151-155) ignores `"yielded" in report.flags` and returns `"done"`. `_run_stages` (herness/model/build.py:354-357) then adds `score` to `stages_done` and saves it. On resume the score stage is skipped, so the remaining steps, including `check`, never run. If `promote` is in the stages, a build whose invariants were never checked gets promoted.
   - Why now: this was latent before this card, because the loader raised "not available". It is live once T04-13 merges.
   - Second gap in the same path: `build._save_state` (build.py:315-316) and `_yield` (build.py:319-320) overwrite the whole job state with `{build_id, stages_done}`. That drops the `scoring` checkpoint key, so even after the "yield" fix a resumed build reruns every scoring step. Under repeated preemption it could livelock, rerunning metrics and yielding each time. The builder's carry-over mentions only the first part.
   - Fix (T02-19): return "yield" when yielded, and merge job state instead of replacing it.

#### Minor (Nice to Have)
1. **Raw `duckdb.Error` escapes the validate step, against the U04-56 Errors row (ConfigError, SchemaViolation).**
   - Where: herness/metrics/scoring.py:115-123 (the dq write) and :90-95 (the count queries).
   - The test test_ut04_121_validate_rollback_on_write_error (tests/unit/metrics/test_metrics_scoring.py:574-583) locks this behaviour in.
   - Mitigation: the build runner wraps it as SchemaViolation anyway (build.py:347-352).
2. **A failing ROLLBACK hides the original error.**
   - Where: `_run_step` (scoring.py:225-226) and `_validate_step` (scoring.py:121-122).
   - If `ROLLBACK` itself raises (for example a connection invalidated after a fatal DuckDB error), the original error is replaced.
3. **Concern 2: a check's query_id is fixed per build, which causes a hash conflict when its input data changes. Spec-level, fails closed.**
   - Cause: `render_named` keeps only the binds that are used (only `s_count_metrics`). So a check's query_id = f(SQL, s_count_metrics, build_id) does not depend on the metric/score tables it reads (_scoring_checks.py:65-67).
   - Real production path: a check fails, the operator fixes the catalog or weights and retries the job in the same build. `config_hash` no longer matches, so the checkpoint resets and metrics reruns (new metric query_ids). The check result then differs, and `record_evidence` raises `SchemaViolation("nondeterministic result for q_...")`. The build can never pass `check` again and needs a new build. The error text points at nondeterminism rather than the real cause.
   - Not affected: normal retries with an unchanged config (identical data, identical hash), and passing-to-passing reruns (the row is `(0, 0)` both times).
   - Risk: low, because it fails closed.
   - Suggested fix: escalate to the spec owner or T04-21, for example by putting `config_hash` or the upstream evidence digest into the check render context so it becomes part of the query_id.
4. **`should_yield()` is only consulted when more steps remain and the step actually ran** (scoring.py:357-358).
   - Spec step 10 says "after a step". This is a sensible narrowing and it is documented in the report, but the spec does not record it as a deviation.
5. **`steps="metrics"` (a str instead of a sequence) iterates over characters** and raises "unknown scoring step m" (scoring.py:176-180). The error is harmless but confusing.

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit, test and ruling of the card is implemented. Every stored number is recorded evidence, each step is transactional, and fault restart is proven. Coverage is 100%. The one Important item is a T02-19 seam defect (yield ignored, job state overwritten) that becomes live with this merge. The controller must track it as a blocker for the T02-19 fix before any promote-capable scoring run.

## Re-review round 1
Scope: the round-1 diff 6f88f5b..7850c4d (briefs/T04-13-review-r1.diff), checked in worktree agent-a619b87157393b6a1 at head 7850c4d.

### Round-1 items
1. ✅ **Hash conflict after a config fix (Needs-fixes ruling).** Resolved for the case the ruling names. A residual path remains; see Important 1 below.
   - The fix: `run_check_step` (_scoring_checks.py:100) reads `config_hash(get_config())`. `_run_one` (_scoring_checks.py:68) passes `{"config_hash": ...}` as the render context. `render_named` copies the context into `params.template` (render.py:298), and `run_recorded` computes `ids.query_id(norm, p, build)` over `{sql, params, build_id}` (evidence.py, ids.py:270).
   - This follows the impl 00 query_id rule. The record_evidence ON CONFLICT DO NOTHING + hash-compare rule is unchanged, and no evidence or dq rows are cleared beyond the existing U04-59 step-1 delete. The value is an identifier, so it passes `_check_context`.
   - The new test test_ut04_115_same_build_retry_after_config_change (test_metrics_scoring.py:628):
     - uses the real `load_config` (repo config/, plus a `--set` override) and the real `config_hash`, patched into both modules;
     - asserts that the hashes differ, the checkpoint resets, metrics reruns, the check passes with a new query_id, and both evidence rows exist, each carrying its own `params.template.config_hash`.
   - Spec note: U04-59 step 2 says `render_named("checks:<name>", {}, ...)`. The non-empty context deviates from the spec literal and should be recorded as an accepted deviation in the report or ledger.
2. ✅ **Validate-step `duckdb.Error` becomes SchemaViolation.** The column reads and the dq write are now wrapped (scoring.py:127-142). Errors become `SchemaViolation("validate failed: ...")` with a `step_failed` log, and ConfigError and SchemaViolation still pass through untouched. Both the read path and the write path have tests (test_metrics_scoring.py:574, :588).
3. ✅ **`_rollback` keeps the original error.** `_rollback` (scoring.py:103) catches only `duckdb.Error` from ROLLBACK, logs `metrics.scoring.rollback_failed`, and adds a note to the original exception, which is the one re-raised. It is used at scoring.py:137 and :248. The test is at :597.
4. ✅ **A bare str for `steps` raises ConfigError.** scoring.py:195 raises `ConfigError("unknown scoring step <str>")` before anything runs. The test is at :620, and it also asserts that no table was written.
5. ✅ **Regressions.**
   - Module sizes: scoring.py is 390/390 and _scoring_checks.py 118/130. check_module_size exits 0.
   - Coverage, from `pytest tests/unit/metrics tests/fault/metrics/test_metrics_scoring_fault.py --cov --cov-branch`: 634 passed. Line and branch coverage are both 100% on scoring.py (221 statements, 58 branches) and on _scoring_checks.py (53 statements, 10 branches).
   - `-k UT02_78`: 6 passed.
   - ruff check: clean. ruff format --check: 815 files clean. mypy: 0 issues in 306 files. lint-imports: 13 kept, 0 broken.
   - Number provenance: still intact. The fix adds only a template context, an error wrapper and a rollback helper. value, n_bad and passed still come from the recorded SELECT and the DuckDB INSERT.

### Question: can a same-build_id, same-config_hash check rerun still see different input data?
Through the stages, no:
- `build_id` is new whenever stage `build` runs. A payload `build_id` is accepted only without stage `build` (build.py:85, _build_support.py:101-114), so `core.*` (000-299) is fixed for a build_id.
- `enrich` *can* rerun on an existing `building` build_id. A payload with `stages=["enrich","score"]` reaches `stage_enrich`, which says "a completed unpromoted build resumes" (_build_stages.py:118). The rerun is LLM-driven, and `depth` comes from the payload, so `enrich.*` can change under the same build_id and config_hash.
- Every check, however, reads only recorded outputs: the fact tables, `metric_value`, the closures and `meta.evidence`. Facts are rematerialized on every `stage_score` under query_ids pinned to (sql, binds, build_id) (facts.py:114-117). The metric SQL that reads `enrich.incident_change_link` directly (config/metrics.yaml:451) is pinned the same way.
- So changed upstream data trips facts' or metrics' own "nondeterministic result" first, in the same run, before `check` is reached. That upstream exposure belongs to the facts card / spec (the re-enrich-on-a-scored-build design), not to T04-13. I flag it below for the controller.

Through partial `score_steps` selections, **yes**. See Important 1.

### New findings
#### Critical
None.

#### Important
1. **A check-only rerun under a reverted config still hits "nondeterministic result"** (_scoring_checks.py:68, :100). The cause is that the check's query_id pins the config, not the input data.
   - Why it happens: `metrics.metric_value` is rewritten wholesale by any metrics step under any config (`CREATE OR REPLACE`, scoring.py:53-54). `score_steps` lets a job run `check` without `metrics` (U04-56 step 1; payload `score_steps`, build.py:73).
   - Reachable sequence on one `building` build:
     1. Config A, full run: check recorded under (sql, A, build).
     2. Config B: `score_steps=["metrics"]` (for example a trial config).
     3. Revert to config A: `score_steps=["check"]`. The check reads B's metric_value under A's query_id. If B's data changes any check result (pass to fail or fail to pass), `record_evidence` raises `SchemaViolation("nondeterministic result for q_...")` on every retry under A, and the build is bricked under A.
   - A related silent case: step 3 under config B instead records "passed under B" evidence for metrics that config B never produced (stale input labelled with the wrong config).
   - Risk: narrow. It needs an operator-chosen partial step list across a config switch, and it fails closed. The coordinator's rule classes any such residual path as Important.
   - Concrete fix, within the evidence rules (template context only, no rows cleared): in `run_check_step`, derive an input digest from the data itself and add it to the render context, keeping or replacing `config_hash`. For example:
     - In SQL, `SELECT string_agg(DISTINCT query_id, ',' ORDER BY query_id)` over the existing input tables of the U04-60 checks (`metrics.metric_value`, the fact tables, `metrics.work_item_closure`, `metrics.org_closure`).
     - Then `inputs = "in_" + sha256_hex(that)[:16]`, which is an identifier, so `_check_context` accepts it.
     - Every input row's content is pinned by its own query_id through the evidence hash rule, so the check query_id then pins its exact inputs, and any retry over the same inputs is deterministic by construction.
     - Add a UT04-115 variant for the A -> B(metrics only) -> A(check only) sequence.
   - Alternative: when `check` is requested without `metrics` and the metric_value query_ids do not match the current config's evidence, raise a ConfigError ("rerun metrics"). The digest is simpler and spec-neutral.

#### Minor
1. **The config hash is read twice per run** (_scoring_checks.py:100 vs scoring.py:369). `run_check_step` calls `get_config()` itself, while the checkpoint and `sc` use the `cfg` that `run_scoring` read at the start. If `init_config` replaces the cache mid-run (core/config.py logs `config.cache.replaced`), the check evidence is labelled with a hash different from the checkpoint and metrics config. Better: pass the run's hash in. `StepContext` is spec'd (U04-54), so this needs a spec note, or the input-digest fix above makes it moot.
2. **The spec deviation for item 1 is not recorded.** U04-59 says context `{}`. Record it as an accepted deviation in the T04-13 report or ledger.
3. **Upstream, outside this card (facts card / T02-19 / spec owner):** a rerun of `enrich` on an existing `building` build_id with changed LLM output or `depth` trips facts' own hash conflict (facts.py:114-117), and the build can never score again. Track it with the T02-19 carry-overs. Not a T04-13 defect.

### Verdict
**Needs fixes.** Items 1-5 are resolved ✅. One Important remains: the narrow residual path in Important 1 (check-only rerun after a metrics-only run under another config). The fix is an input-data digest in the check's template context. If the coordinator judges the operator sequence out of scope, it can be parked as a spec note, and the card would then be Approved.

## Re-review round 2
Scope: the round-2 diff 7850c4d..5f68d5f (briefs/T04-13-review-r2.diff), checked in worktree agent-a619b87157393b6a1 at head 5f68d5f. Covers the re-review round 1 Important 1, the two round-1 Minors this fix touches, and regressions.

### Items
1. ✅ **A -> B(["metrics"]) -> A(["check"]) partial-steps sequence (Important 1).**
   - How it works now: `_run_one` renders with `{"inputs": _inputs(con, tables)}` (_scoring_checks.py:80, :117). The digest changes whenever `metrics.metric_value` is rewritten under other query_ids. So the A-check rerun gets a new query_id and records fresh evidence, with no TH04-06 conflict.
   - The silent variant is gone. config_hash is no longer in the check context, so evidence identity is "this check over these inputs", not "this check under config X".
   - The new test `test_ut04_115_partial_steps_across_configs` (test_metrics_scoring.py, after :689) uses the real `load_config` and `config_hash` and asserts:
     - the A-check rerun succeeds;
     - its query_id is new;
     - `params.template.inputs` equals a digest recomputed in Python and differs from the first run's;
     - the fact-check digest is still pinned to the fact tables;
     - a B-check over the same inputs reuses the same query_id.

     Under the round-1 code the `second != first` assertion would fail, so the test does discriminate.
   - Independent probe (scratch tests under .agent-tmp/reverify2-T04-13/, all 4 pass):
     - **(a) Result-flip case.** A full, then B metrics with the ratio rows forced out of range, then A check-only. This raises SchemaViolation naming `score_metric_ratio_range`, not "nondeterministic result". After that, A metrics followed by A check-only goes back to the *original* A query_id and passes with no conflict.
     - **(b) Silent variant as originally stated.** A full, then B check-only over A-made inputs. All 7 check query_ids equal A's. No B-labelled evidence is created.
2. ✅ **Digest soundness.**
   - Computed in SQL: DuckDB `sha256(coalesce(string_agg(query_id, ',' ORDER BY query_id), ''))` over `SELECT DISTINCT` of a UNION of the check's tables (_scoring_checks.py:66-73). Python only adds the `in_` prefix. It is an identifier, not a stored number. `in_` + 16 lowercase hex matches `_IDENT` (render.py:46), so `_check_context` accepts it.
   - Covers every input table: the CHECKS entries (_scoring_checks.py:29-37) match exactly the tables each checks.sql.j2 segment reads. Duration -> 3 facts; 4 metric checks -> metric_value; evidence_coverage -> FACT_TABLES (both closures + 3 facts) + metric_value; work_item_cycle -> work_item_closure. All of them carry `query_id`: facts via `IntoSpec(..., "query_id")` (facts.py:115) and metric_value via `NOT NULL query_id` (scoring.py:58, :152). Probe (a) also asserts, for all 7 checks, that the recorded `inputs` equals the Python recompute over that check's CHECKS tables.
   - Deterministic: the order is fixed by ORDER BY inside the aggregate, and table order and duplicates do not matter (probe). An empty input gives `in_` + sha256('')[:16] = `in_e3b0c44298fc1c14`, stable (probe). The comma separator is safe because query_ids are identifiers.
   - 16 hex (64 bits) is acceptable. It only selects the template param. The query_id itself is still the full impl 00 hash over (sql, params, build_id). A digest collision within one build would have to coincide with a different result to matter, and even then it would hit the TH04-06 hash-conflict rule (fail closed) rather than pass silently.
   - The missing-table pre-check is unchanged. `_inputs` runs only after `set(tables) <= present` (_scoring_checks.py:114-117), so a missing table is still recorded as `skipped` without any failing statement in the transaction. Branch coverage is 100%.
   - Rules fit: the query_id is still `ids.query_id(norm, p, build)` via run_recorded. record_evidence ON CONFLICT plus the hash compare is unchanged, and no evidence or dq rows are cleared beyond the U04-59 step-1 delete. `score_evidence_coverage` also reads `meta.evidence`, which is not in the digest. That is acceptable: evidence is append-only and every input query_id is recorded together with its rows, so a result can only change through tampering, which fails closed. The report (line 67) states this.
   - Same build, same inputs is idempotent. Probe: full run + 2 check-only reruns give identical query_ids for all 7 checks. The meta.evidence row count is unchanged and equals its distinct query_id count. There is exactly 1 dq row per check.
3. ✅ **Recorded items.** The report's "Fix round 2" records:
   - the U04-59 `{}` -> `{"inputs": ...}` deviation with its rationale (report line 68);
   - the enrich-rerun carry-over to the facts card / T02-19 (line 75);
   - the T04-21 carry-over for `query_ids` list columns (lines 67, 76).

   Re-review Minor 1 (config read twice) is moot: `_scoring_checks` no longer imports get_config or config_hash, and the test patches for them were removed.
4. ✅ **No regressions.**
   - Sizes: _scoring_checks.py 127/130, scoring.py 390/390, check_module_size exit 0.
   - `pytest tests/unit/metrics tests/fault/metrics/test_metrics_scoring_fault.py tests/unit/model/test_model_build_handler.py --cov-branch`: 641 passed. This includes test_ft04_01_kill_in_check_then_restart and test_ft04_03_metric_template_error_rolls_back. Coverage is 100% line and branch on _scoring_checks.py (57 stmts / 10 br) and scoring.py (221 / 58).
   - `-k UT02_78`: 6 passed.
   - ruff check: clean. ruff format --check: 815 files clean. mypy: 0 issues in 306 files. lint-imports: 13 kept, 0 broken.

### New findings
#### Critical
None.

#### Important
None.

#### Minor
1. **The UT04-115 partial-steps test never flips a check result** (test_metrics_scoring.py, `test_ut04_115_partial_steps_across_configs`). B's metric_value still passes `score_metric_ratio_range`, so the test proves the query_id changes but not the conflict-avoidance case itself, where B's data changes the result. Its last step is a B-check over B-made inputs, not the original silent variant (B-check over A-made inputs). Both were verified by the scratch probe above. Optional: add an out-of-range UPDATE after the B metrics step, and a B check-only step straight after the A full run.

### Verdict
**Approved.** Re-review round 1 Important 1 and Minors 1-2 are resolved. There are no new Critical or Important findings, and all gates are green.
