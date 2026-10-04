# T04-14 Funding attribution — verify review

Worktree agent-ae7822f5390fb1264, head ff2ee16 (base e41d62c). Read-only; probes only under %TEMP%\t0414-verify.

### Spec Compliance
- ✅ Spec compliant (with accepted sub-controller rulings: steps 1-2 + row counts only, `# T04-15:` marker, STEPS untouched, observed_days BEGIN/END block, local graphs)

| Row | Result | Notes |
|-----|--------|-------|
| U04-64 cand / cand_services / cand_desc | ✅ | sql.j2:51-79; max confidence per pair; depth>=1, both ends in cand |
| U04-64 inc / rec_cost | ✅ | sql.j2:80-104; usd formulas per spec, window via p(as_of_ts, s_window_days); first UNION branch CAST DOUBLE |
| U04-64 t1 / t1_counts / t2c | ✅ | sql.j2:105-137; both link directions; self closure row; distinct incidents per (candidate, cluster) |
| U04-64 dec / t2r | ✅ | sql.j2:138-161; latest decided_at, then probability DESC, then decider; list_has_any on services(k) |
| U04-64 t3 | ✅ | sql.j2:162-167; w = s_service_weight×conf, q = 0.5×conf |
| U04-64 allocate (a)-(d) | ✅ | sql.j2:23-50; (a) lowest tier, max w; (b) min tier per record; (c) leaf-most over the peers remaining after (b) (correct order); (d) share/pain DOUBLE |
| U04-64 pass1 / cf / t2f / pass2 | ✅ | sql.j2:168-197; linked = pass-1 pain at tier<=2 of C's incidents; linked_share 0 when pain 0; three thresholds; observed_days() per impl §3.9 |
| U04-64 output columns/types/order, DECIMAL cast | ✅ | sql.j2:198-200; pinned by DESCRIBE in UT04-75 |
| U04-64 invariant: no text column (TH04-04) | ✅ | only `d.answer` read, renamed and compared; never output |
| U04-66 (attribution part) | ✅ | funding.py:21-41; positional (con, sc, /) matches StepFn (scoring.py:73); unconfirmed bind computed; run_recorded into IntoSpec("score.funding_attribution","replace","query_id"); no Python arithmetic |
| UT04-75 | ✅ | shares 0.5/0.5, tier 1, 5000.00 each; evidence row (producer score, row_count, sql < 20k, repeatable query_id); schema; bind set |
| UT04-76 | ✅ | only tier 1 kept; tier-2 cluster beats tier 3 with w=0.8×0.6 |
| UT04-77 | ✅ | w = 0.6×0.9×0.5; older matching decision ignored |
| UT04-78 | ✅ | only epic path; initiative via component map when epic does not match |
| UT04-79 | ✅ | CA only; CB (n), CC (pain 0), CD (linked share), CE (annual) rejected |
| UT04-86 | ✅ | tier 3 only; event 5.00, change 400.00; out-of-window/linked/info/successful excluded; bind-driven cost 10.00 |
| PT04-05 | ✅ (see Minor 1) | Σ share = 1 ± 1e-9 per record; own pain bound; per-row rounding |
| Rendered SQL < 20,000 | ✅ | report 9,198 chars; asserted in UT04-75 tests |
| Budgets | ✅ | template 200/320, funding.py 41/150, _macros 146/260 |
| IDs / docstrings / pytestmark | ✅ | all functions carry IDs; module pytestmark = unit |

- ⚠️ Cannot verify from diff: BT04-03 (< 45 s with U04-65) — not part of this card; T04-15/T04-16 merge of the shared observed_days block (must keep one copy).

### Builder deviations — judgement
1. (record_kind, record_id) keying: accepted, strictly safer than record_id alone; no spec output changes.
2. Quality DESC tie-break in (a): accepted; spec "lowest candidate" is a no-op inside the (record, candidate) partition; this makes the pick (and result_hash) deterministic.
3. Dropping non-positive / NULL weight paths before (a): accepted; required for the Σ share = 1 postcondition (otherwise 0/NULL division). Side effect: a zero configured tier weight lets a lower-quality tier win instead of leaving the record unattributed — reasonable.
4. NULL service_id service_map rows ignored: accepted (they can never match t3; would only add NULL to services(k)).
5. ST04-04 allowance `d.answer AS category` (test_metrics_catalog.py:552-569): SAFE. Exact substring, must occur exactly once, removed only from this one file before the raw scan; every other forbidden word in the file is still scanned; the alias `category` is not on the list; value is only compared (sql.j2:158) and output columns are pinned by the UT04-75 DESCRIBE test. The runtime catalog validator (catalog.py:239) applies only to catalog metric SQL, and `_recorded` checks only comment/`;` tokens, so no runtime path is affected. DD04-16 remains open for the controller to record.
6. PT04-05 unrounded own-pain interpretation: accepted. The literal per-row-rounded sum can exceed total + 0.01 (e.g. 0.02 USD split 4 ways rounds to 4×0.01), so the unrounded form plus the per-row |pain_usd − share×usd| ≤ 0.005 check is the correct reading.

### Strengths
- Template is fully static; all runtime values via p(); the only literals are spec constants (0.5, 365, 60.0, 'cluster_fix:').
- Leaf-most rule applied after the min-tier filter (the builder's own smoke run caught the reverse-order bug).
- Tests assert exact spec Expected values (shares, weights, quality, DECIMAL pain), not just shapes.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. tests/unit/metrics/test_metrics_funding_props.py:31,103-108 — under the derandomized commit profile the 25 examples never produce a tier-2 root-cause (t2r) path or a cluster_fix (t2f) path (`--hypothesis-show-statistics`: tier 1 59 %, tier 3 45 %, tier 2 34 %, no cluster_fix event; the report's "cluster_fix 12 %" does not reproduce). A 150-example random probe gave t2r 2 %, t2f 7 %. The property is not vacuous, but the two pass-2 / root-cause paths are only covered by UT04-77/79. Also `MAX_EXAMPLES = min(settings().max_examples, 25)` caps the nightly profile at 25 too. Suggest: bias the generator (e.g. decisions on candidates whose service matches the cluster's service_ids, fewer links) and cap only the commit profile.
2. test_metrics_funding_props.py:148-149 — the `event()` label "tier 2" does not distinguish t2c from t2r; add a label so coverage can be read.

### Gate evidence (verifier runs)
- `PYTHONUTF8=1 uv run pytest tests/unit/metrics -q -p no:logging` → 646 passed (274 s).
- Card tests (funding + props) → 14 passed; coverage herness/metrics/funding.py 100 % line / 100 % branch.
- ruff check + format --check on touched Python files: clean. `uv run mypy` (project): 0 issues in 334 files.

### Assessment
**Task quality:** Approved
**Reasoning:** U04-64 is implemented step-for-step in a static, bind-only template; all stored values flow through run_recorded with evidence; the builder's deviations are safe or required by the postconditions, and the ST04-04 allowance is exact and narrow. Remaining items are property-test coverage polish.

## Re-review round 1 (sub-controller, scoped: ff2ee16..b279c9f)
Diff read: test-only change in tests/unit/metrics/test_metrics_funding_props.py. Minor 1 resolved — generator forces a root-cause path and a qualifying cluster_fix cluster on independent booleans; cap applies only when profile max_examples <= 200 (commit = 200, nightly = 10,000 keeps its own). Minor 2 resolved — events labelled by path kind. Independent run (commit profile, --hypothesis-show-statistics): tier1_direct 52%, tier3_service 48%, tier2_cluster_fix 32%, tier2_cluster 24%, tier2_root_cause 20%; 14 passed in 21.8 s. Verdict: Approved.
