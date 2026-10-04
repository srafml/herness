# T07-18 verify review (outcome_measure job handler)

Worktree agent-a2d179ae8d924c195, head 8594fe6 (base e8634a0). I ran the gates, the card tests with coverage, a handler registration probe and 27 of my own mutation probes on outcome.py. Every probe was reverted and the worktree is clean.

## Verdict: Approved (Minor items only)

## Spec compliance
- U07-86 outcome_measure_handler: ✅ The payload is checked first (R-42, ConfigError with fixed text). Sweep and single pairs come from due_measurements + due_weeks. A single pair that is not due returns {"skipped":"not_due"}. One read-only CURRENT connection is opened only when >=1 pair is due and is closed in finally. should_yield is checked before each pair and returns JobOutcome("yield", {"measured": n}). There is one heartbeat per pair. The done result is {measured, skipped, verdicts}. QueryError/RetryableError propagate (no catch). Yield and heartbeat semantics match maintenance.py. `register_handler("outcome_measure", outcome_measure_handler)` passes mypy --strict and runs (verifier probe; registration itself stays T07-23).
- U07-87 measure_recommendation: ✅ Steps 1-12 run in order:
  - outcome_exists -> None.
  - Unknown metric -> memory.outcome.skipped with reason=unknown_metric.
  - Windows are computed.
  - The work_item owner comes from the spec'd `SELECT service_id FROM core.work_item`. This is the only own SQL; there is no SQL against metric data.
  - peer_group runs with on_evidence.
  - treated_targets covers [pre.start, post.end).
  - The peers / min_peers / prior_year branches use compute_metric only.
  - Evidence goes through record_evidence (series and peer group).
  - did_statistics, then classify_verdict.
  - One run_write does INSERT OR IGNORE + insert_system_item; nothing is written on a lost race.
  - The summary is embedded after commit; then log + counter.
- UT07-74 ✅ (14 tests; "2 peers; treated peer -> prior_year; peer excluded" = test_ut07_74_treated_peer_excluded_leaves_two_peers_so_prior_year)
- UT07-87 ✅ (8 tests + 12 parametrized bad payloads)
- IT07-05 ✅ (paid_off vs flat peers; no_effect when all peers improve; reruns add no row)
- FT07-04 ✅ The fault_env rule error:QueryError fires at fault_point("sql.query", kind="outcome_measure"). The job fails and nothing is written for the pair; the next sweep measures it once. The nth=2 variant keeps the first pair and retries only the second. loop_kill.py exists but is meant for process kills; the fault plan is the right fixture for an injected QueryError.
- ST07-18 ✅ Treated peers are never queried or listed. Recs on another metric, rejected recs and recs outside the span are kept. The method falls back below min_peers. Out-of-scope recs get no outcome or summary. Summaries pass find_uncited_numerals.
- TH07-18 ✅ held under my own probes (table below).
- §2 rows ✅ outcome.py 328/330 (ENG 400); outcome_stats.py 179/240, row updated with metric_weeks, due_weeks.

⚠️ Cannot verify here: BT07-08 (< 30 s per rec) is not a card test. Acceptance on spec 11 `tiny_build` is impossible because the fixture is absent (ruling 10).

## Gates (TMP/TEMP=...\w29-s07, PYTHONUTF8=1)
- Card tests + outcome_stats tests: 55 passed. Coverage: outcome.py 100 % line / 100 % branch (200 stmts, 40 branches); outcome_stats.py 98 % (line 179 missed, pre-existing).
- ruff check / ruff format --check (7 files): clean. mypy: 0 issues in 374 files. lint-imports: 13 kept, 0 broken. tools.check_module_size: exit 0.
- Test IDs appear in the names and in the first docstring line. pytestmark is unit, integration (IT and ST, following the existing tests/security convention) or fault (following the existing tests/fault convention).

## Rulings on builder spec readings
1. OutcomeDeps seam + configure_outcome/outcome_deps (ConfigError while unset): ACCEPT. Same R-42 pattern as T07-22 maintenance.
2. catalog_from_config() instead of load_catalog(path): ACCEPT. compute_metric and peer_group read this same config-validated catalog, so `better` cannot disagree with the queries. load_catalog would re-read the file by path.
3. metric_weeks / due_weeks in outcome_stats.py (§2 row updated, 179/240): ACCEPT. They are pure functions and keep outcome.py within budget. due_weeks derives from U07-83 Windows.due, so there is only one due rule.
4. Warehouse opened only when >=1 pair is due, closed in finally: ACCEPT (probe V23 killed).
5. Peer-group evidence saved with record_evidence; a work item with NULL service_id gets method "none", verdict inconclusive and query_id = the peer-group query id: ACCEPT. details.peer_query_id and query_id both resolve to evidence rows, and the verdict stays conservative.
6. Treated peers excluded by id regardless of target type: ACCEPT. This is conservative: an id collision only removes a peer. M4 covers the reverse gap (work_item recs on a peer's service).
7. ToolInputError (unknown target) skips the pair: ACCEPT. The skip is not silent: it logs `memory.outcome.skipped` with reason=invalid_request (outcome.py:252-254) and is counted in JobOutcome `skipped` (outcome.py:319-320; probe V20 killed). The try covers more than the reading says (M1).
8. New fault call site fault_point("sql.query", kind="outcome_measure") before each series query: ACCEPT. No new shim needed; probe V21 killed.
9. Generic target naming when target_id holds an uncited numeral; a lost insert race writes no summary and returns None: ACCEPT (V15 and V9 killed).
10. tiny_build absent, tests plant their own warehouse: ACCEPT. There is no tests/support/builds.py and no `def tiny_build` anywhere in tests/support. The only mentions are "tiny_build does not exist yet" notes in loop_standin.py, tools_standin.py, warehouse_read_build.py and warehouse_tools_build.py, so using it was not feasible. The planted warehouse uses the real metrics_tiny DDL, materialize_facts and the real compute_metric/peer_group, which tests more than fakes would.

## Mutation probes (my own; outcome.py; 4 card test files)
| # | Mutation | Result | Killing test |
|---|---|---|---|
| V1 | treated exclusion dropped | KILLED | UT07-74 treated peer |
| V2 | exclusion limited to another target_type | KILLED | UT07-74 treated peer |
| V3 | treated span starts at post.start | KILLED | UT07-74 treated peer |
| V4 | treated span ends at pre.end | KILLED | UT07-74 treated peer |
| V5 | min_peers ignored (>=1) | KILLED | UT07-74 treated peer |
| V6 | min_peers off-by-one (>) | KILLED | IT07-05 |
| V7 | spec 04 prior_year fallback ignored | KILLED | UT07-74 fallback honoured |
| V8 | outcome_exists check dropped | KILLED | UT07-74 existing outcome |
| V9 | insert_outcome result ignored (race) | KILLED | UT07-74 lost insert race |
| V10 | prior_year has_control always True | SURVIVED (equivalent: empty control -> n_pre 0 < min_weeks -> inconclusive) | - |
| V11 | unresolved-owner has_control True | SURVIVED (equivalent, same reason) | - |
| V12 | unknown metric not skipped | KILLED | UT07-74 unknown metric |
| V13 | single-pair filter dropped | KILLED | UT07-87 single/not_due |
| V14 | summary gets numeral "(measurement 1)" | KILLED (write pipeline numeral rule) | UT07-74 |
| V15 | generic target naming dropped | KILLED | UT07-74 numeral target |
| V16 | should_yield check dropped | KILLED | UT07-87 yield |
| V17 | heartbeat dropped | KILLED | UT07-87 sweep |
| V18 | series evidence not persisted | KILLED | UT07-74 |
| V19 | peer-group evidence not persisted | KILLED | UT07-74 work item no owner |
| V20 | skipped not counted | KILLED | UT07-87 skipped counted |
| V21 | fault_point removed | KILLED | FT07-04 |
| V22 | ToolInputError not caught | KILLED | UT07-74 unknown target |
| V23 | warehouse opened before empty check | KILLED | UT07-87 sweep |
| V24 | con.close() dropped | SURVIVED | - (M3) |
| V25 | peer median -> first peer value | SURVIVED | - (M2) |
| V26 | embed_after_commit dropped | KILLED | UT07-74 details/summary |
| V27 | not_due result replaced | KILLED | UT07-87 single/not_due |

TH07-18 summary: tests killed every probe on these guards:
- treated-peer exclusion (V1-V4)
- min_peers fallback (V5-V7)
- idempotency (V8, V9)
- out-of-scope recs (V12, V13, V27, plus ST07-18 rejected/deferred/no-metric)
- uncited numerals (V14, V15)

The conservative inconclusive guard also holds. V10 and V11 survived only because they are equivalent mutants: classify_verdict already returns inconclusive for an empty control.

## Findings
### Critical
none
### Important
none
### Minor
- M1 outcome.py:250-254: `except ToolInputError` wraps all of `_measure`, so a ToolInputError from ops (treated_targets bad_arg, an evidence/row check) is also skipped as reason=invalid_request. Reading 7 names only peer_group/compute_metric errors. The skip is logged and counted, but it could hide a programming error. Narrow the try to the spec 04 calls, or log the exception class.
- M2 outcome.py:158: every card test uses identical flat peers, so replacing the weekly peer median with a single peer value survives (V25). Add one test with heterogeneous peers to pin the step 7 median.
- M3 outcome.py:325-326: closing the connection is never asserted (V24). A close counter in `_outcome_env.open_current` would pin it.
- M4 outcome.py:133, 174 (spec-literal): step 4 resolves a work item's owner only from core.work_item.service_id. Spec 04 peer_group also resolves owners through core.service_map, so such a work item gets method "none" even though its peer group resolved. Also, a peer service whose work item has an accepted rec on the metric is not excluded, because the ids differ. Both follow U07-87 literally; flag for the spec owner.
- M5 outcome.py:244-254: skipped pairs (unknown metric or target) stay due and unmeasured, so every weekly sweep re-queries and re-logs them indefinitely. This matches the spec for unknown metrics; note it for operations / T07-23.
- M6 outcome.py at 328/330 lines leaves no headroom for M1-M3 without a split or a budget ruling.

### Security / logging
ConfigError texts are fixed strings (outcome.py:94, 294). Logs carry only rec_id, measurement, verdict, method and reason (outcome.py:205, 277). The heartbeat note holds the rec_id only. There is no ticket text, personal data or secret anywhere. The summary contains the rec kind and id, the target id (or a generic name) and the metric name.

## Final state
The worktree status is clean (no modified or untracked files).

## Re-review round 1 (fix commit 869a2d9 on 8594fe6; scope M1, M2, M3)

### Verdict: Approved

### Changes checked
- M1 ✅ (outcome.py:247-249): `ops.treated_targets` now runs before the try, and its result reaches `_measure` as `_Pair.excluded` (a frozenset of target ids, any target type, as before). Inside the try only spec 04 can raise ToolInputError. I also checked the other ops calls left inside the try: `record_evidence` (store/ops/evidence.py:64-70) only serializes and writes and raises no ToolInputError, and `_owner` is a DuckDB read, so the try comment is accurate. New test `test_ut07_74_ops_tool_input_error_propagates` checks that the ops error propagates and writes nothing.
- M2 ✅: new test `test_ut07_74_control_is_the_weekly_peer_median` uses peers with different values (pre 9/10/11, post 10/14/9). Only the median gives did = -2 and rel = 0.2; a single peer or the mean gives a different result.
- M3 ✅: `_outcome_env.open_current` records each cursor, and `all_closed` checks that each one rejects a query. The check is asserted on the done path (test_ut07_87_sweep_measures_every_due_pair) and on the QueryError path (FT07-04 first test).

### Probes (my own; restored after each)
| # | Mutation | Result | Killing test |
|---|---|---|---|
| V24 | `con.close()` dropped | KILLED (was SURVIVED) | UT07-87 sweep (all_closed); FT07-04 also asserts it |
| V25 | peer median -> first peer value | KILLED (was SURVIVED) | UT07-74 control is the weekly peer median |
| V25b | peer median -> mean | KILLED | UT07-74 control is the weekly peer median |
| VM1 | treated_targets read moved back inside the try | KILLED | UT07-74 ops ToolInputError propagates |
| V1 | treated exclusion dropped (re-check after refactor) | KILLED | UT07-74 treated peer |

### Gates (TMP/TEMP=...\w29-s07, PYTHONUTF8=1)
- Card tests + outcome_stats tests: 57 passed. Coverage: outcome.py 100 % line / 100 % branch (200 stmts, 40 branches); outcome_stats.py 98 % (line 179 missed, pre-existing).
- ruff check / ruff format --check (4 changed files): clean. mypy: 0 issues in 374 files. check_module_size: exit 0. outcome.py is now 327/330.
- Test IDs, docstrings and pytestmark in the new tests follow the global constraints.

### Remaining
M4 (spec-literal, for the spec owner), M5 (skipped pairs are re-queried every sweep) and M6 (327/330 lines) are unchanged and were out of scope. No new findings.

The worktree status is clean after this round.
