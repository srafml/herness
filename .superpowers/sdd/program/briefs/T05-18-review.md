# T05-18 review (verify agent): Warehouse tools, part 2

Base 9c8f34a, head 0caa121 (wip 78ef483 + feat). Read-only review; one scratch dir, now removed; tree clean.

### Spec Compliance
- ✅ U05-42 GetMetric (herness/harness/_warehouse_tools_metric.py): window rules at :102-116 (both-or-neither, start <= end, <= 1,100 days, ToolInputError). Catalog lookup at :229-233. The unknown name returns an error result: `from_error` text plus one `name — unit, better, grains; description` line per enabled entry, stopped before 12,000 chars (:127-135). compute_metric runs on `ctx.warehouse.cursor()` under `threading.Timer(timeout_s)` (:138-166); an interrupt from our timer becomes `QueryError("timeout after Ns", hint=...)` and other errors pass through. `mr.build_id != ctx.build_id` raises ConfigError before recording (:237-239). The Evidence follows step 4 exactly (normalize_sql(mr.sql), mr.params, mr.result_hash, row_count, result_sample, now, duration_ms) and is recorded with retry_call("sqlite_write") for the evidence and its use (:169-185). The content has the header, the catalog line and the rows under format_result, capped at 12,000. `data` is model_dump without `sql`, and `query_ids=[mr.query_id]`. The tool never builds SQL itself: the only path to SQL is `compute_metric`.
- ✅ Deviation 1, `catalog_from_config()` instead of `load_catalog()`: accepted. compute_metric itself validates against `catalog_from_config()` (herness/metrics/compute.py:225, `validate_metric_request` at :135), so the tool's `catalog.get` and listing use the same validated catalog that computes. `load_catalog()` re-reads config/metrics.yaml relative to the CWD on every call. It could then list or accept a name that compute rejects, or the reverse; it would also add file I/O to each call. Choosing `load_catalog` would carry the divergence risk; this choice removes it. It needs a spec note on U05-42 step 2 (the report records it).
- ✅ Deviation 2, the 8-key `filters` schema: accepted. The key set equals `FilterKey` (herness/metrics/settings.py:71-80), so the schema is strict-compatible under R-26. Null keys are dropped in `_filters` (:84-88). compute's `normalize_filters` enforces the per-metric allow-list, 1-500 items and the enum or ID item rules, with messages that never echo values. The schema is looser than compute (no enums for severity, work_item_type or change_type, and no maxItems), which is harmless because compute re-validates.
- ✅ U05-44 GetCluster (_warehouse_tools_read.py:103-144): CLUSTER_ROW, COUNTS and SAMPLE SQL follow the spec. Sample is 0-20 in the schema and in code. Samples come only from `enrich.text_redacted` and are wrapped with `record_id`, and the label is wrapped. A call makes 2 or 3 recorded queries, and an unknown cluster raises ToolInputError.
- ✅ U05-45 GetRecord (:150-204): LOCATE is a UNION ALL of four selects. The row SQL is built from the allowlisted schema columns minus `blocked_columns()`, compared under the guard's NFKC and casefold normalisation and DuckDB-quoted. Redact-on-read cells are redacted (`work_item.summary` is verified redacted in content, data and the evidence sample), untrusted cells are wrapped, and there are 5 query ids. The internal guard (allow_catalog) is a second line of defence: a blocked column would raise ConfigError.
- ✅ U05-46 SemanticSearch (warehouse_tools.py:274-347): k is 1-50 in the schema and in code, and `[:k]` bounds the hits a fake returns. The code also checks text 3-500, the entity enum and service_id <= 200 before any embedding. `redact_text` runs before `enrich.embed_query`, and a None result fails closed. `.tolist()` hands the vector over, and only validated entity and service_id filters are passed. Hits without a snippet are dropped. Order is similarity desc, then record_id. Similarity appears only in content and data, never in evidence; the test asserts this on the evidence dump. `embed_query` loads lazily through the PEP 562 facade. Probe: `import herness.harness.warehouse_tools` loads neither torch, sentence_transformers nor herness.enrich.embed; only herness.enrich and enrich.settings load. ModelUnavailable propagates to dispatch, which retries it under `tool_store` (_tools_dispatch.py:136/139).
- ✅ U05-47 register_warehouse_tools: all eight tools are registered with owner "05", in U05 order in `_TOOLS`, and registration is idempotent (tested twice on one registry).
- ✅ UT05-83, UT05-85, UT05-86, UT05-87, UT05-88 and the UT05-60 extension: every ID has functions, docstrings start with the ID, and `pytestmark = pytest.mark.unit` is set. The bench sets `[integration, slow]`.
- ✅ BT05-06 and BT05-07 pass (numbers below).
- ✅ Every internal SQL constant, and the get_record row SQL of all four core tables, passes `SqlGuard(allow_catalog=True)`; the public `*_SQL` set is pinned.
- ✅ Budgets: warehouse_tools.py 363/400, _warehouse_tools_metric.py 250/250, _warehouse_tools_read.py 204/250. The §2 rows are in the same commit (0caa121). warehouse.py, tools.py, sql_guard.py and _tools_record.py are untouched (empty diff). This card adds no HTTP client; httpx2 in llm/ predates it. Layering holds: L4 imports L3 metrics and enrich; lint-imports reports 13 kept.
- ⚠️ BT05-06 and BT05-07 ran on local synthetic "full-like" data with a test-side LanceDB adapter (flat scan). There is no production VectorHandle adapter yet, and BT05-06 used the tiny-st model, not bge-m3. Both carry over: re-run on spec 11 `full` with the real adapter.
- ⚠️ tiny_build is a stand-in (tests/support/warehouse_read_build.py, MemoryWarehouse over metrics_tiny). Re-point it when spec 11 lands, per the ruling.

### Verification run (this agent)
- `pytest tests/unit/harness/test_warehouse_tools_part2.py tests/unit/harness/test_warehouse_tools.py -p no:logging`: 80 passed, 38.8 s.
- Coverage (line + branch): _warehouse_tools_metric 100 %, _warehouse_tools_read 100 %, warehouse_tools 99 %. The one missed line, :112, is the pre-existing T05-17 defensive skip.
- Gates: ruff check clean and ruff format --check clean; `mypy` (project config) reports no issues in 294 files; lint-imports 13 kept, 0 broken; check_module_size exit 0.
- BT05-07 (re-run): p95 **82.2 ms** over 100 calls (4 metrics), threshold 2 s. The builder reported 83.9 and 94.5 ms.
- BT05-06 (re-run): p95 **299.3 ms**, k=20, 100 queries, 100,000 vectors x 1024, 100,000 snippets, threshold 300 ms. It passed by 0.7 ms. The builder reported 271.8 and 293.2 ms. My first combined run's BT05-06 line was cut off by the output tail, so I re-ran BT05-06 alone for the number.
- TH05-01: tests show a delimiter-closing injection in a sample, the label and a snippet escaped to `&lt;/untrusted_data&gt;`, with the block count exact. The `record_id` attribute is sanitised by `_ATTR_DROP_RE`. TH05-05: tests cover a fullwidth lookalike blocked column on incident, change and problem, and a planted name and e-mail redacted in content, data and the evidence sample. Error messages carry only IDs, names and rule text; none carries ticket text or secrets.

### Strengths
- The timer-fired flag separates our interrupt from other compute errors; this mirrors `_tools_record.run_query`.
- The catalog choice is consistent with compute; build mismatch is checked before any evidence is written.
- Missing enrich tables give a clean QueryError before internal SQL runs, instead of the fatal internal-guard ConfigError.
- Tests are behavioural: a real slow metric proves the timeout, and the tests read real evidence dumps and check exact wrapped strings.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. tests/bench/test_harness_warehouse_tools_part2_bench.py:199 — BT05-06 p95 was 299.3 ms against a 300 ms limit on this re-run, after 271.8 and 293.2 ms in the builder's runs. It will be flaky whenever slow benches run on a loaded host. The margin comes from the test-side flat LanceDB scan, not from the tool. Carry-over: re-measure on `full` with the real adapter and index. Until then, treat a BT05-06 miss on this stand-in as load, not as a regression.
2. herness/harness/_warehouse_tools_read.py:119 and :180 — `cluster_id` and `record_id` patterns are enforced only by the schema at dispatch. They are not re-checked in code on a direct call, unlike get_metric and semantic_search. The impact is small: both are bound params, `record_id` is sanitised in the wrap attribute, and `cluster_id[:64]` is echoed only in the not-found message. The code-side checks are inconsistent across tools.
3. herness/harness/_warehouse_tools_read.py:21 — imports the private `sql_guard._norm`. This is the same parked Minor as T05-17 and a candidate for a public alias.
4. herness/harness/_warehouse_tools_metric.py — at 250/250, its full budget, with no room for a fix-loop change without a ruling.
5. tests/unit/harness/test_warehouse_tools_part2.py — no test covers ModelUnavailable from `embed_query` (propagation and the dispatch `tool_store` retry). No test pins "importing warehouse_tools does not import torch or enrich.embed"; I verified it by probe. The spec's Errors column names ModelUnavailable.
6. herness/harness/_warehouse_tools_read.py:39-44, :184-187 — LOCATE has no ORDER BY. If one record_id existed in two core tables, the table would be chosen arbitrarily. The record_id namespace makes this practically impossible.
7. Spec notes to fold into docs/impl/05 U05-42: `catalog_from_config()` in step 2; the 8-key `filters` schema; start == end passes the tool check but compute rejects it (compute requires start < end). The tool's "≤ 1,100 days" differs from compute's "≤ 36 calendar months" by up to 4 days; compute is the final authority.

### Assessment
**Task quality:** Approved
**Reasoning:** All five units match the spec and the rulings (get_metric evidence from the MetricResult, the private siblings within §2 budgets with rows in the same commit, protected modules untouched). Tests, coverage, gates and both benchmarks pass on re-run. The findings are Minor carry-overs, the main one being BT05-06's thin margin on the stand-in adapter.
