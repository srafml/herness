# T07-10 Hybrid recall - review (verify agent)

Reviewed: worktree agent-af74d44c522054d93, commit 32ba376 (base 3f61b67). 4 files: herness/harness/memory/recall.py (360 lines), tests/unit/harness/memory/test_memory_recall.py, tests/unit/harness/memory/test_memory_recall_props.py, tests/security/test_st07_recall.py. store.py untouched (diff stat).

Gates re-run by reviewer: card tests 65 passed (no warnings); ruff check + format --check clean on all 4 files; mypy recall.py clean; lint-imports 13 kept / 0 broken; tools.check_module_size exit 0. Focused probe (temp file under .agent-tmp, removed): a vector index returning a malformed memory_id makes recall raise ToolInputError (see I1).

### Spec Compliance
- ❌ Issues found: [U07-62 Errors "LanceDB ... failure never raises" violated for a malformed id coming back from the vector index (I1)]

Per unit:
- ✅ U07-58 fts_query_string - NFKC, casefold, \w+ tokens, len >= 2, first 32 distinct, double-quoted, joined by " OR ", None when empty (recall.py:57-61). The match is a bound ? parameter in ops.fts_candidates; the query never reaches SQL text or vector filters (vector filters are layer/status literals, recall.py:282-289).
- ✅ U07-59 score_candidate - kw = -kw_raw/max_kw clamped; rel normal and degraded (w_kw*kw + w_ent*ent)/(w_kw + w_ent); rec = exp(-ln2*age/half_life) with negative age clamped; conf halved when pending; final with conf_floor/rec_floor (recall.py:68-85). Settings defaults (0.55/0.25/0.20, 0.6, 0.7, min_score 0.30, lambda 0.8, candidates 50/50/50) match design.
- ✅ U07-60 mmr_select - greedy lam*score - (1-lam)*max cos; cos 0 for None / nothing selected; ties higher score then lower id; distinct ids (recall.py:104-118).
- ✅ U07-61 RelatednessCache - default connect_build = warehouse.open_readonly (only warehouse access), two reads, closed in finally, per-row union groups, related(a,a) false, LRU max_builds, failure -> WARNING relatedness_unavailable + empty map for 60 s, never raises (recall.py:121-175).
- ❌ U07-62 MemoryRecaller.recall - steps 1-12 implemented (redact first, vector/FTS/entity candidates, dedupe, cap 150, SQLite-row visibility filter, sim, ent incl. relatedness, age from max(created, last_used), min_score, MMR, DEBUG log + latency histogram, no record_use). Gap: I1.

Per test row:
- ✅ UT07-39 (operators, quotes, empty, NFKC/cap)
- ✅ UT07-40 (formula, zero relevance dropped, pending halves conf, clamps)
- ✅ UT07-41 (degraded formula; zero kw+ent weights)
- ✅ UT07-42 (near-duplicates diversify, tie rule, duplicate ids)
- ✅ UT07-43 (team<->org via service_map, core.team, LRU, 60 s empty map, close on error)
- ✅ UT07-44 (filter matrix: candidate / pending own+other / expired / rejected / layer / kind / confidence / created_after; redaction before embed+FTS; argument validation; StoreBusy; render e2e; fusion determinism)
- ✅ UT07-45 (embedder down -> degraded, FTS hits, exact degraded score; any search/vectors exception degrades; logs carry no text/ids)
- ✅ PT07-06 - hypothesis over wide inputs incl. out-of-range sim/ent/kw, negative age, arbitrary weights/floors: components in [0,1] and final non-decreasing in confidence. Real property test.
- ✅ ST07-06 - B's chat and the review pipeline (no include_pending_for) never see A's pending item; A does, flagged unconfirmed; a pending item with null author_ref is never shown. The vector fake ignores its prefilter, so the SQLite filter is what is exercised.
- ✅ ST07-08 - 19 attack strings (*, NEAR, column filter, quote breakout, NUL, ' OR 1=1 --, homoglyphs, ZWSP, fullwidth) against real FTS5: output grammar asserted, no error, hits a subset of rows sharing a literal token (no widening), at ops level and through recall.
- ✅ ST07-13 - a lying vector index returns rejected / expired / candidate / past-expiry / wrong-layer rows as active: not recalled; the live row is still recalled.

Dispatch checklist:
1. ✅ Query redacted before embedding and FTS; never in SQL text or vector filters (UT07-44 redaction test, ST07-08).
2. ✅ Deterministic fusion (round robin + dict dedupe + MMR total-order tie rule), bounded by k and 150 candidates; PT07-06 is a real property test.
3. ✅ Hits go through render.render_records; recall.py does no rendering; unconfirmed set from pending status; e2e test asserts the [UNCONFIRMED] prefix and escaping are inherited.
4. ✅ Warehouse only via RelatednessCache connect_build (default open_readonly) plus warehouse.read_current for the CURRENT id.
5. ❌ embed/search/vectors exceptions degrade (reason = type name), but a malformed vector id leaks as ToolInputError (I1). RedactionFailed propagating is judged CORRECT: fail closed, the unredacted query must never be embedded or matched, and it is a FatalError rather than a vector-path failure; document it (M1).
6. ✅ Logs: completed (counts, flag, duration, run_id), degraded (reason type, run_id), relatedness_unavailable (build_id, error_type), invalid_row (error_type). No text / query / memory ids; asserted in UT07-45.
7. ✅ Scoring, MMR, FTS string, RelatednessCache match U07-58..U07-61 exactly.
8. ✅ Every test function carries its ID in name and docstring first line; pytestmark set; tests assert behaviour.
9. ✅ store.py untouched, public seams only (ops.*, VectorIndex, Embedder, warehouse), no HTTP client, 360/360 lines, lint-imports clean.

Build-report deviations:
1. vectors() for all kept items - accepted (MMR needs vectors; sim for ANN hits is still 1 - cosine distance = dot). ⚠️ extra LanceDB read per recall; BT07-01 latency not in this card.
2. Round-robin union then cap 150 - accepted (spec gives only the <=150 bound; no source starves).
3. vectors() failure after search degrades - accepted (spec: LanceDB failure never raises).
4. Invalid hydrated row skipped with WARNING invalid_row - accepted (fail safe, no content logged); add the event to the spec log table (M3).
5. Constructor types, relatedness optional - accepted.
6. RedactionFailed propagates - accepted as fail-closed; see M1.
7. Naive now -> SchemaViolation (not ToolInputError) - accepted; M2.
8. Empty layers -> ToolInputError - accepted.
9. Relatedness groups per U07-61 (not transitive) - accepted, matches spec text.
10. Log events - accepted.
11. fts_rejected not emitted - accepted (quoted-token grammar cannot produce an FTS5 syntax error; ops returns [] indistinguishably).

- ⚠️ Cannot verify from diff:
  - conn_factory lifecycle: recall.py:264 takes a connection and never closes it. Correct if conn_factory is core.connection (per-thread shared, as in the tests); a leak if the future MemoryStore.from_config (U07-98) passes a factory returning fresh connections. Check when the U07-98 wiring lands.
  - BT07-01 / BT07-02 latency (not in this card).
  - TH07-06 caller side: include_pending_for comes only from the chat run's own user_ref - enforced by callers (specs 06/09), not here.

### Strengths
- Visibility is decided solely on hydrated SQLite rows; the security tests use a deliberately lying vector index, so the SQLite filter is genuinely exercised.
- FTS injection tests run against real FTS5 with a no-widening subset oracle, not just string shape.
- Clean degradation path with type-name-only logging, asserted by log-content tests.
- Pure functions (score / MMR / FTS) are small and match the spec formulas line for line; tie rules give a total order.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
- I1 recall.py:289-301 with recall.py:304-306 - a memory_id returned by the vector index that does not match the ops id pattern (corrupted or foreign LanceDB row, the TH07-13 stale-index class of fault) reaches ops.get_memory_items, whose valid_ids raises ToolInputError("invalid id: get_memory_items") (herness/store/ops/_memory_rows.py:192-194). Reproduced by a probe: FakeVectors row "not-a-memory-id" -> recall raises ToolInputError. This violates U07-62 Errors ("LanceDB ... failure never raises"), mislabels an index fault as a caller-argument error, and one bad row disables recall for every query. Fix: in _candidates keep only ANN ids that pass the public id validator (herness.core.ids.is_valid_id with the memory id kind) before the union, e.g. scan.ann = {i: d for i, d in found_ann if is_valid_id(...)}, with a WARNING carrying only a count when any are dropped (or treat it as a degrade); add a UT07-45 / ST07-13 case with a malformed id asserting no raise and the valid hits still returned. recall.py is at 360/360: fold the filter into the existing scan.ann = dict(found_ann) line or move a helper to a private sibling.

#### Minor (Nice to Have)
- M1 recall.py:258 - docstring says "raises ToolInputError, StoreBusy"; RedactionFailed (fail-closed, deliberate) also propagates. Add it to the docstring and record the reading in the spec reconciliation list (U07-62 Errors row).
- M2 recall.py:260 - a naive now raises SchemaViolation via clock.ensure_utc rather than ToolInputError like the other argument checks. Acceptable (spec silent); document it or map it to ToolInputError for a uniform argument-error contract.
- M3 recall.py:310 - memory.recall.invalid_row is not in the spec 07 log-event table; add it in the spec reconciliation pass.
- M4 recall.py (360/360 lines) - at budget; any fix (including I1) needs a compaction, a private sibling or a budget ruling.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation matches U07-58..U07-62 and the security tests are genuine, but a malformed id from the vector index escapes recall as ToolInputError, contradicting the spec's guarantee that a vector-store failure never raises; the fix is a one-line id filter plus a test.


## Re-review round 1 (commit 1a0ca4b, diff 32ba376..1a0ca4b)

Scope: I1, M1, M2 and regressions (M3/M4 parked as spec notes by the sub-controller).

- ✅ I1 fixed - recall.py:289-291: ANN ids are filtered with herness.core.ids.is_valid_id(IdKind.MEMORY, i) before the union; WARNING memory.recall.invalid_vector_ids carries only count. Validator check: is_valid_id (mem_ + [0-7][Crockford]{25}, length 26) is strictly narrower than ops MEMORY_ID_RE (mem_ + [Crockford]{26}, fullmatch), so every id that passes can no longer trip valid_ids' ToolInputError in get_memory_items; the extra strictness only drops ids that could never be real ULIDs. Filtering sits inside the vector try-block, so a non-string id from a broken index also degrades rather than raises. New test test_ut07_44_malformed_vector_ids_dropped covers three malformed shapes (garbage, non-Crockford U, other id kind), asserts no raise, valid hit returned, not degraded, n_candidates 1, exact count-only log event and no id text in logs. Behavioural, ID-tagged.
- ✅ M1 fixed - recall.py:256-257 docstring now names RedactionFailed (fail closed, deliberate).
- ✅ M2 fixed - same docstring names SchemaViolation for a naive now (documented option chosen).
- ✅ Budget-neutral refactors reviewed, behaviour unchanged: _TEAM_SQL inlined, _SECONDS_PER_DAY replaced by (now - touched) / timedelta(days=1) (identical value), _visible docstring folded to a comment, _ent collapsed to "0.5 if rel and build and rel.related_any(...)" (RelatednessCache defines no __len__/__bool__, so truthiness equals "is not None"; build ids are non-empty strings).
- ✅ Regression gates re-run: card tests 66 passed (65 + new test), no warnings; ruff check and ruff format --check clean (recall.py, test_memory_recall.py); mypy recall.py clean; check_module_size exit 0; recall.py 360/360 lines.

Open findings: none (M3, M4 parked as spec notes).

**Re-review verdict:** Approved
