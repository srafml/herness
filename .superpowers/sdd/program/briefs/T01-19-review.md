# T01-19 review: Monitoring connector base (verify agent)

Worktree D:\herness\.claude\worktrees\agent-aaca957a1034161b7, head 9e2c1ec on base e41d62c.

### Spec Compliance
- U01-78 MonitoringAdapter ✅: Protocol with `tool`, `check`, `events`, `daily_metrics`. The docstrings say rows come from `event_batch`/`metric_batch`, events and daily aggregates only, `[since, until)`, and `complete_days` (base.py:77-95).
- U01-79 MonitoringConnector ✅:
  - `@register("connector","monitoring")` (base.py:238), plus `name` and `entities` (the configured subset, in spec order).
  - `watermark_field` gives ts/date. `check` runs the adapters in order and the first error propagates. `tools()` keeps config order; `stream_key` is `monitoring:<tool>`.
  - `sync_tool`: since=None → `backfill_for(entity).resolve_start(until)`, then routes by entity. `sync` chains the tools; until=None → clock().
  - ConfigError for an unknown tool or entity, and for zero adapters.
  - Implements Connector and SupportsToolStreams, not SupportsKeyListing (tested).
- U01-80 row builders ✅:
  - EVENT_COLUMNS and METRIC_COLUMNS match the spec exactly.
  - Event: `_source_key` = `tool:event_key`, `_source_updated_at` = `ts`.
  - Metric: `_source_key` = `tool|metric|service|date.isoformat()`, `_source_updated_at` = date + 1 day at 00:00 UTC (tested across month and year ends).
  - ts/end_ts are ISO with Z (offset input converted). value is `json.dumps(float)` (int 3 → "3.0"), and None for None/NaN/±inf.
  - Columns: metadata first, then the tuple, all strings.
  - A violated precondition raises SchemaViolation("monitoring row") without echoing values.
- U01-81 floor_day / complete_days ✅: UTC date floor; naive or non-datetime input → ConfigError; the empty range case is tested.
- UT01-79 ✅: every name contains ut01_79, docstrings start "UT01-79", and `pytestmark = unit`. The acceptance selection `-k "ut01_79 or ut01_92 or ut01_93"` gives 73 passed.

### Focus points
1. No network or HTTP client ✅: base.py imports only pyarrow and rows/base/settings/time/errors/registry. There is no httpx or socket import under herness/connectors/monitoring/. The module docstring (base.py:2-3) points adapters to `herness.connectors.http` (T01-14). It does not name `retry_page`; see Minor 5.
2. Per-tool watermarks ✅:
   - The real SyncRunner + FakeLake + migrated ops store test (test_monitoring_connector.py:210-249) asserts `monitoring:<tool>` × {event: field ts = latest event ts; metric_daily: field date = 2026-03-01T00:00Z, the end of the last complete day}.
   - It also asserts there is no unkeyed `monitoring` watermark, and that sync_slice rows are keyed per tool and done.
   - The no-rows tool test (test_monitoring_connector.py:252-265) gets an event watermark = end on the first (backfill) run, as U01-43 requires (backfill with no committed rows → wm = end).
   - Design §5.3 "one watermark for all adapters" is superseded by R-62 / design §6, and the code follows those.
3. Bounds and validation ✅:
   - `1 <= len(rows) <= batch_rows`, a known tool and an aware fetched_at are checked before building (base.py:191). Every row is validated before the batch exists, so nothing reaches the lake write unvalidated.
   - The builder's claim of no redaction or truncation is correct, and that is not a finding. RowBatcher/flatten_record (rows.py:53-91, 198-283) do none. TH01-05 (impl 01:2705) asks for shape checks, strict timestamps and `record_id` validation, not redaction.
   - Keys are bounded (512 characters, no control characters) by record_id. A 10 MB `service` string is accepted; memory is bounded by the TH01-04 64 MiB page cap in the HTTP layer.
4. No hostnames ✅.
5. Injection ✅:
   - `source_tool` always comes from the validated `tool` argument; a row-supplied `source_tool` is ignored (tested).
   - Entity names are fixed constants, and keys pass `record_id`.
   - ConfigError context names a tool only when it is in TOOLS, and entity text in messages is cut to 64 characters.
6. Exact values ✅: all checked above. `register("connector","monitoring")` is present. ConfigError cases: no adapter, unknown or duplicate tool, unknown tool or entity, naive bounds, naive input to the day helpers.
7. Deviations ✅ acceptable:
   - The `batch_rows` keyword (default DEFAULT_BATCH_ROWS) is additive and needed to bound the spec precondition.
   - `TOOLS` duplicates `_Tool` at settings.py:44 (Minor 4).
   - The SchemaViolation context keywords follow the HernessError API.
   - `# fmt: skip` is used only to meet the line budget (Minor 6).
   - The outside-Files edits are minimal and correct:
     - registry `_BUILTINS` gets one row.
     - mapping_check drops monitoring from `_SKIPPED` and adds a `_fetched` branch.
     - UT01-58 gets a positive and a negative test.
     - UT01-94 gets a real-class test using the shipped row; the autouse reset_registry prevents leaks.
     - The UT10-19 edit removes only the `sources.sources.monitoring` tuple, which became false once the connector exists. The test keeps its intent: C03 still flags `models.deciders.jev` and the unregistered `adapters.splunk`, and a comment explains the change.
8. Gates ✅, re-run by the reviewer:
   - pytest tests/unit/connectors + test_config_validate.py: 869 passed, 4 skipped (host symlink and Excel-extension skips).
   - Coverage of monitoring/base.py: 100% line (183 statements), 100% branch (40).
   - ruff check and ruff format --check: clean.
   - mypy: 335 files clean.
   - lint-imports: 13 contracts kept.
   - check_module_size: rc 0.

⚠️ Cannot fully verify: whether the T01-20..22 adapters will actually use `herness.connectors.http` and `retry_page`. The base cannot enforce this; the ST01-14 lint covers httpx imports.

### Strengths
- Strict validation: row values are never echoed, and the error context names the tool only when it is a known tool.
- The real-runner test proves per-tool watermarks and slices.
- Adapter batches are re-checked for `_source`, `_entity` and `source_tool` before the runner sees them.
- Tight, parametrized tests.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. base.py:222-223 `_same` uses `pc.all(...)`, which skips nulls by default, plus `is not False`. A batch whose `_source`, `_entity` or `source_tool` column is all null therefore passes `_checked`; a probe confirmed this. The guard goes beyond the spec and adapters build rows through event_batch, so the risk is low. Fix: also require `null_count == 0`, or use `pc.all(..., skip_nulls=False)`.
2. base.py:182: `metric_batch` with `date(9999,12,31)` raises a raw `OverflowError` instead of `SchemaViolation` (ENG §3.4 says no built-in exceptions escape). Catch OverflowError in `_build` (base.py:199) or range-check the date.
3. base.py:144-147 and 182: event `ts` and metric `date` are not range-checked. Year 1 and far-future values are accepted, unlike the 1970-2100 rule in `parse_source_timestamp` (TH01-05 asks for strict timestamp parsing). The runner caps the watermark at `min(end, max)`, so the watermark stays safe. Consider rejecting values outside 1970-2100 for consistency.
4. base.py:31: `TOOLS` repeats `_Tool = Literal[...]` from settings.py:44. Derive it with `typing.get_args(_Tool)` or use a shared constant to avoid drift. It is also an extra public export not in the module map; record it as a spec note.
5. base.py:1-5 and 77-81: the protocol docstring does not mention `retry_page` or the egress-based client. One sentence would steer adapter authors (focus point 1).
6. base.py is at 319/320 lines, reached with `# fmt: skip` and a `_DT` alias added only to fit the budget. There is no headroom; if T01-20..22 need hooks here, a budget note from the controller will be required.
7. base.py:191 and 194: a non-int `batch_rows` (for example "5") raises a raw TypeError. The parameter is mypy-typed, so this is cosmetic.

### Assessment
**Task quality:** Approved
**Reasoning:** All four units match the spec exactly, and the real-runner test proves the per-tool `event` and `metric_daily` watermarks, including for a tool with no rows. The outside-Files edits are minimal and correct, and all gates pass with 100% line and branch coverage. The remaining items are minor hardening of a defensive extra check and of error types.
