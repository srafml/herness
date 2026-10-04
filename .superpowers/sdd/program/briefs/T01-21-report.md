# T01-21 report — Datadog and Splunk adapters (U01-83, U01-84)

Status: DONE_WITH_CONCERNS (minor; see Concerns) - final commit 2860299 `wip(T01-21): Datadog and Splunk adapters with card tests` (resumed builder; no further worktree changes, so no separate feat commit)
Worktree: D:\herness\.claude\worktrees\agent-a8f201d6cf7eb3139 (branch worktree-agent-a8f201d6cf7eb3139, base feb27f7)

## Files (lines vs budget)
- herness/connectors/monitoring/datadog.py 248 / 260 (DatadogAdapter)
- herness/connectors/monitoring/splunk.py 222 / 250 (SplunkAdapter)
- herness/core/registry.py 154 / 160 (ruled outside-card edit: `_BUILTINS` rows ("monitoring_adapter","datadog") and ("monitoring_adapter","splunk"))
- tests/unit/core/test_config_validate.py (ruled: UT10-19 C03 now expects only the decider row; comment updated — splunk adapter resolves)
- tests/unit/connectors/_dd_splunk_data.py 82 (private helpers: settings builder, SourceHttp over MockTransport, `wire` swapping the egress client factory used by prometheus.source_http, `Lines` streamed export endpoint)
- tests/unit/connectors/test_monitoring_datadog.py (UT01-81, 34 cases)
- tests/unit/connectors/test_monitoring_splunk.py (UT01-82, 26 cases)
prometheus.py / dynatrace.py / base.py untouched (source_http imported as-is).

## Design decisions
- Constructor per §3.16: `Adapter(settings, *, clock=time.now, http=None, batch_rows=DEFAULT_BATCH_ROWS)`, `@register("monitoring_adapter", tool)`; SourceHttp built lazily on first use via `prometheus.source_http(settings, tool, clock)` (egress `http_client` + `build_auth`, breaker key `monitoring:<tool>`). Construction resolves no secret.
- All HTTP via SourceHttp (get_json / post_json / post_form_lines → retry_page). No httpx imports. All requests are relative paths on the base_url client.
- Datadog events: `POST /api/v2/events/search`, body `{"filter": {"query": event_query, "from": since ISO-Z, "to": until ISO-Z}, "sort": "timestamp", "page": {"limit": page_size}}`; later pages the same body with `"page": {"limit", "cursor": after}` (the cursor is the only response value that reaches a request). Stop on empty `data` or absent/null `meta.page.after`; `CursorGuard.step(after)` before each later page; non-string/empty cursor → SchemaViolation. Mapping per U01-83 (V-4 default paths): event_key=id (non-empty str else SchemaViolation "event without id"), ts=parse_source_timestamp(attributes.timestamp), inner=attributes.attributes or {}, service=inner.service else first `service:<v>` tag, host, priority→severity_raw, status, title, aggregation_key→dedup_key, end_ts/incident_ref None, payload=json.dumps(item). event_query None → no request.
- Datadog metrics: `(a,b)=complete_days`; a>=b → no request. Per query `GET /api/v1/query` with `from=a`, `to=b-1` (epoch s), `query=f"{query}.rollup({agg}, 86400)"`. `status=="error"` or no `series` list → SchemaViolation. Service = the `<service_label>:` tag value (absent → SchemaViolation); points `[ms, v]` → date UTC(ms/1000), kept when a ≤ date < b; null → NULL. agg None (settings already refuse it for datadog) → ConfigError before any request (defence in depth).
- Datadog 429: mapped in http.map_http_error through parse_retry_after (R-70), which reads `X-RateLimit-Reset` when `Retry-After` is absent; test asserts RateLimited.retry_after == 12 after retry_page gives up.
- Datadog keys: `DD-API-KEY` + `DD-APPLICATION-KEY` from build_auth (`api_and_app_key`), StaticHeaderAuth origin-scoped; tested on base_url requests and absent on a foreign absolute URL; 403 message/context/auth repr carry no key.
- Splunk: search = SPL re-validated with `validate_spl` (ValueError → ConfigError "forbidden SPL: ...", before any request; TH01-07), then `spl` if it starts with `|` else `"search " + spl`. Export via `post_form_lines("/services/search/v2/jobs/export", data={search, earliest_time, latest_time, output_mode=json})`, epoch seconds as strings. Per line: messages with type ERROR/FATAL → SchemaViolation("splunk search error"); preview==true or no `result` skipped; non-object result → SchemaViolation. Events: event_key required; `_time`/`end_ts` numeric or numeric string → epoch s, else ISO-8601 (parse_source_timestamp); optional text fields; payload=json.dumps(result). Metrics: per query over complete days; service=result[service_label] (absent → SchemaViolation); value=float(result[value_field]) (missing/non-numeric → SchemaViolation); date=UTC(_time).date() kept a ≤ date < b. check(): `GET /services/server/info?output_mode=json`, body must carry an `entry` list.
- Splunk paging (controller ruling; SPEC NOTE): the [earliest, latest) window is split at every UTC midnight into day pieces (first piece starts at `since`, last ends at `until`), each its own export request. Choice: per UTC day because (1) it is deterministic from the bound window alone (no setting needed, none exists to add without leaving card files), (2) metric searches are daily aggregates, so a day piece is the natural unit and never splits a bucket, (3) event searches must aggregate/table-shape (validate_spl), so one day of results is far below 64 MiB in practice; if a single day still exceeds the shared 64 MiB egress cap the export fails closed with SchemaViolation("response too large") rather than truncating. Bounds are floored to whole epoch seconds; pieces are contiguous (latest of one = earliest of the next; Splunk earliest inclusive, latest exclusive), and the next run starts at the same floored `until`, so there is no gap or overlap. Spec says "one streamed request per query"; this deviates by design (one request per query per UTC day).
- Every line ≤ 1 MiB (post_form_lines); a 2 MiB line → SchemaViolation("line too large").
- Rows via base.event_batch / metric_batch with kw `batch_rows`, flushed every batch_rows across pages/day pieces/queries.

## Tests
- Acceptance: `uv run pytest -k "UT01_81 or UT01_82" -q` → 60 passed (UT01-81 34, UT01-82 26).
- Coverage: datadog.py 100% line / 99% branch (one generator-exit arc); splunk.py 100% / 100%.
- `pytest tests/unit/connectors tests/unit/core/test_config_validate.py -q -p no:logging` → 1053 passed, 4 skipped.
- Gates: ruff check 0; ruff format --check clean; mypy 0 (341 files); lint-imports 13 kept; check_module_size 0; check_type_ownership 0; pre-commit hooks run individually on the card files all Passed (detect-secrets clean via `# pragma: allowlist secret`, no baseline change).
- UT01-81 covers: 3 pages ending without meta.page.after (exact bodies, cursor only added), field mapping, service tag fallback, empty data stop, no event_query, repeated cursor (CursorGuard), bad shapes/cursor/id/timestamp/field type, batching, 429 X-RateLimit-Reset: 12 → RateLimited.retry_after == 12, rollup request params (from=a, to=b-1, .rollup(sum, 86400)), date filter, null value, status error, missing service tag, bad points, agg None → ConfigError, check(), keys on base_url only (absent on a foreign origin), 403 never echoes keys, factory signature lazy.
- UT01-82 covers: preview skipped, `search ` prefix added, `|` unprefixed, ERROR/FATAL message → SchemaViolation, 2 MiB line → SchemaViolation, per-UTC-day paging windows (contiguous floored epoch seconds), event mapping incl. numeric/numeric-string/ISO `_time` and end_ts, bad results, forbidden SPL → ConfigError unsent, batching, metric rows/date filter/service/value errors, check(), bearer on every export + 401 never echoes token, factory signature lazy.
- TDD note: tests and implementation were written in one pass; no separate RED run captured.

## Spec notes
- U01-84 "One streamed request per query" → one per query per UTC day (controller ruling, above).
- Splunk adapter re-runs validate_spl and raises ConfigError (spec row 41: "ConfigError on forbidden SPL"); settings already reject it at load.
- Datadog `check()` additionally requires `{"valid": true}`; Splunk `check()` requires an `entry` list (shape checks beyond the spec's bare GET, matching the T01-20 stricter-check convention).
- Datadog metric payload = `{"query", "tag_set", "point"}`; Splunk metric payload = `{"query", "result"}` (spec gives none for metrics).
- Splunk `_time` numeric strings (Splunk JSON output renders numbers as strings) read as epoch seconds.
- Datadog `attributes.timestamp` parsed as text only (no epoch_unit), as the spec names parse_source_timestamp without a unit.

## Concerns
- First `wip(T01-21)` commit attempt failed in pre-commit (exit 1) although its pytest-unit hook showed 9219 passed; only the output tail was captured. Most likely cause: I edited datadog.py (a 2-line tag_set type tightening) while the hooks ran, which conflicts with pre-commit's unstaged-change stash/restore. Every hook re-run individually on the card files passed. Resolved by the resumed builder: hunk kept, everything staged, committed with no concurrent edits -> 2860299, all hooks Passed incl. pytest-unit.
- Splunk per-UTC-day paging is a deviation from "one streamed request per query" (controller ruling); a single day over 64 MiB still fails closed (SchemaViolation "response too large").
- `source_http` is still imported from prometheus.py (T01-20 m1: later move to base/http).
- Pre-existing (T01-20 note): tests/unit/core/test_registry.py autouse clears `_BUILTINS`, so running it before test_config_validate.py made UT10-19 C03 order-dependent. FIXED by the resumed builder (allowed minimal isolation fix): the `_reset` fixture now snapshots `_BUILTINS` and restores it after each test (+2 lines in test_registry.py); both orders pass (62 passed).

## Resumed builder (final)
- Fast checks on the leftover tree: ruff format/check clean, mypy 0, lint-imports 13 kept, detect-secrets clean, module-size/type-ownership 0.
- UT01-81 + UT01-82: 60 passed; coverage datadog.py 100% line / 49 of 50 branches, splunk.py 100% / 100%.
- tests/unit/connectors + test_config_validate.py + test_registry.py: 1061 passed, 4 skipped.
- Commit 2860299: every pre-commit hook Passed (pytest-unit `-m unit -x` included).
- Host note: `pytest tests/unit/core` run alone crashes with "Windows fatal exception: stack overflow" (GC in dateutil tz/ctypes while importing herness/store/vectors.py); not card-related; the full unit run in the hook passes.
