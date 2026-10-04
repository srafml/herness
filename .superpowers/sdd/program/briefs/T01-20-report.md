# T01-20 report — Prometheus/Mimir and Dynatrace adapters (U01-82, U01-85)

Status: DONE_WITH_CONCERNS (minor; see Concerns)
Worktree: D:\herness\.claude\worktrees\agent-a8f201d6cf7eb3139 (branch worktree-agent-a8f201d6cf7eb3139, base a51221f)
Commits: c69204a wip(T01-20): Prometheus/Mimir and Dynatrace adapters with card tests; feb27f7 feat(connectors): T01-20 Prometheus/Mimir and Dynatrace adapters (all pre-commit hooks passed incl. pytest-unit)

## Files (lines vs budget)
- herness/connectors/monitoring/prometheus.py 187 / 210 (PrometheusAdapter, plus public helper `source_http`)
- herness/connectors/monitoring/dynatrace.py 236 / 250 (DynatraceAdapter)
- herness/core/registry.py 152 / 160 (ruled outside-card edit: two `_BUILTINS` rows `("monitoring_adapter","prometheus")`, `("monitoring_adapter","dynatrace")`)
- tests/unit/connectors/_prometheus_data.py 106 (shared helpers for both test files: settings builder, scripted MockTransport `Source`, `SourceHttp` over it, retry/breaker stub, body builders)
- tests/unit/connectors/test_monitoring_prometheus.py 233 (UT01-80)
- tests/unit/connectors/test_monitoring_dynatrace.py ~336 (UT01-83)
No fixtures/cassettes on disk (inline synthetic bodies; hosts *.example.invalid; tokens start with `synthetic`).

## Design decisions
- Constructor per impl 01 §3.16 convention: `<Adapter>(settings, *, clock=time.now, http: SourceHttp | None = None, batch_rows=DEFAULT_BATCH_ROWS)`, decorated `register("monitoring_adapter", tool)` and resolvable lazily through the `_BUILTINS` rows (factory.py:44 `get(...)(a, clock=clock)` works unchanged).
- `http=None` → `SourceHttp` is built lazily on first use (functools.cached_property) by `prometheus.source_http(settings, tool, clock)`: `http.http_client(settings, source="monitoring:<tool>", max_concurrency=settings.max_concurrency)` (egress factory) + `auth.build_auth(settings.auth, source=<tool>, base_url=settings.base_url, token_client=client)`; breaker key `monitoring:<tool>`. Construction therefore resolves no secret and builds no client (keeps UT01-94-style construction side-effect free). Dynatrace imports `source_http` from prometheus.py (shared helper kept in card modules, base.py untouched).
- All requests are relative paths on the egress client of the configured base_url (Mimir `/prometheus` prefix kept by URL merge) via `SourceHttp.get_json` → `retry_page` per page. No httpx/requests imports.
- Prometheus: `(a,b)=complete_days`; start=a+1d, end=b; `[start,end]` split into chunks of ≤10,000 days (each ≤10,000 points); params `query,start,end` (epoch s) and constant `step=86400` (no other step ever sent); `X-Scope-OrgID: <tenant>` passed as a per-request header (relative URLs only, so only to the configured origin). Body must be status success + resultType matrix + list; series without `service_label` → `SchemaViolation("series without service label")`; `[t, v]` checked (t number not bool, v string); date=(UTC(t)−1d).date(); value=float(v) (NaN/Inf → None in metric_batch); payload `{"query","metric","t","v"}`. check(): `GET /api/v1/query?query=vector(1)`, non-success body → SchemaViolation. events(): empty iterator.
- Dynatrace events: first page `from,to` (epoch ms), `pageSize=page_size`, `problemSelector=event_query` when set; later pages params `{"nextPageKey": key}` only; `CursorGuard.step(key)` on each key; absent/null key stops; non-string/empty key → SchemaViolation. Row mapping per U01-85 (end_ts None for -1/absent; service rootCause.name → first affectedEntities[].name → None; host/incident_ref None; dedup_key displayId; payload = json.dumps(problem)). Timestamps must be int ms (bool/str/overflow → SchemaViolation("bad timestamp")). Type checks of text fields delegated to event_batch (SchemaViolation "monitoring row").
- Dynatrace metrics: per query `metricSelector, resolution=1d, from=a, to=b` (epoch ms); non-null nextPageKey → `SchemaViolation("unexpected pagination")`; missing dimension → `SchemaViolation("series without service dimension")`; timestamps/values length mismatch → SchemaViolation; bucket date = UTC(timestamp).date() − 1 day (V-5 default), kept when a ≤ date < b; null → None. No request when a ≥ b.
- Rows go through base.metric_batch/event_batch with keyword `batch_rows`, flushed every `batch_rows` rows across queries/pages; `fetched_at=clock()`.
- Error messages are fixed strings (no response data, URLs or credentials); context `source="monitoring", tool=<tool>`.

## Tests
- Acceptance: `uv run pytest -k "UT01_80 or UT01_83" -q` → 58 passed.
- Coverage: prometheus.py 100% line / 100% branch; dynatrace.py 100% line, 99% branch (2 generator-exit arcs).
- `pytest tests/unit/connectors -q -p no:logging` → 939 passed, 4 skipped.
- Gates: ruff check . 0; ruff format --check . clean (924); mypy 0 (339 files); lint-imports 13 kept; check_module_size rc 0; check_type_ownership rc 0. wip commit c69204a passed all pre-commit hooks incl. pytest-unit and detect-secrets (no baseline change needed: `# pragma: allowlist secret` on the synthetic tokens).
- Covered: params (step, start=a+1d, end=b, tenant header, /prometheus prefix), date=t−1d, missing label error, 10,000-day chunking, batching, shape errors, check/events; Dynatrace 2 pages (second request only nextPageKey), endTime -1/absent → NULL, service fallbacks, repeated key (CursorGuard), metric params, bucket date rule incl. dropped edge buckets, unexpected pagination, Api-Token on every page through real build_auth + fake keyring, 401 → AuthError without token in message/context/auth repr, factory-signature construction makes no client.

## Deviations / spec notes
- TDD order: tests and implementation were written in the same pass; no separate RED run was captured (all tests green on first full run after two test-data fixes: metric names must be DAILY_METRIC_NAMES entries).
- Extra public name `source_http` in prometheus.py (module map lists only `PrometheusAdapter`) — shared adapter page-fetcher builder, reusable by T01-21; module-map row should list it.
- Lazy SourceHttp build (spec: "when http is None it is built"): timing is first use rather than in `__init__`; same result, no secret/config access at construction.
- Prometheus dates are not filtered to [a, b) (spec has no filter; points are exactly start..end at step 86400). Dynatrace filters per spec.
- No `# T01-20:` markers existed in settings*.py; nothing to remove.

## Concerns
- Pre-existing test isolation issue (not touched): tests/unit/core/test_registry.py's autouse fixture `_BUILTINS.clear()`s permanently after each test, so if it runs before tests/unit/core/test_config_validate.py (e.g. `pytest tests/unit/core/test_registry.py tests/unit/core/test_config_validate.py`) UT10-19 C03 fails because the monitoring connector row is gone. Normal alphabetical order is fine. Owner: impl 10 follow-up (restore a copy instead of clear).
- UT10-19 C03 still expects splunk to be unresolved — fine for this card; T01-21 must update it.
