# T01-20 review — Prometheus/Mimir and Dynatrace adapters (U01-82, U01-85)

Reviewed: worktree agent-a8f201d6cf7eb3139, HEAD feb27f7 (base a51221f). Read-only.
Gates re-run by verifier: `pytest -k "UT01_80 or UT01_83" -q -p no:logging` 58 passed; coverage prometheus.py 100% line/100% branch, dynatrace.py 100% line/99% branch (199->exit, 215->exit generator exits); `ruff check .` clean (C90 max-complexity 10, PLR max-args 6 enabled); `mypy` 0 issues / 339 files; `tools.check_module_size` rc 0 (prometheus 187/210, dynatrace 236/250). Diff touches no tools/, pyproject or import-linter config (ST01-14 lint untouched).

### Spec Compliance
- ✅ Spec compliant
  - ✅ U01-82 tool/check/events: `tool="prometheus"`; `check()` GET `/api/v1/query?query=vector(1)` (prometheus.py:148-153); `events` yields nothing (prometheus.py:155-158).
  - ✅ U01-82 algorithm: `(a,b)=complete_days`, start=a+1d, end=b, return when start>end (prometheus.py:163-166); chunks <= 10,000 days inclusive (prometheus.py:59-65; test asserts 9,999-day span + 1-day tail); params query/start/end epoch s + constant `step=86400` only (prometheus.py:32,181); `X-Scope-OrgID` only when tenant set (prometheus.py:136-137, sent on check and query_range); status=="success" and resultType=="matrix" else SchemaViolation (prometheus.py:68-77); missing label -> `SchemaViolation("series without service label")` (prometheus.py:103-106); date=(UTC(t)-1d).date(), value=float(v), payload {query,metric,t,v} (prometheus.py:80-117); metric_batch chunks of batch_rows (prometheus.py:167-187).
  - ✅ U01-85 check: GET `/api/v2/problems?pageSize=1&from=now-5m` (dynatrace.py:175-177).
  - ✅ U01-85 events: first page from/to epoch ms, pageSize=page_size, problemSelector only when event_query set (dynatrace.py:182-185); later pages `{"nextPageKey": key}` alone, stop on absent/null, `CursorGuard.step(key)` per key (dynatrace.py:194-198); mapping event_key=problemId, ts=startTime ms, end_ts None on -1/absent, title/status/severity_raw, service rootCause.name -> first affectedEntities[].name -> None, host None, dedup_key displayId, incident_ref None (dynatrace.py:92-119).
  - ✅ U01-85 metrics: metricSelector, resolution=1d, from=a, to=b epoch ms (dynatrace.py:219-221); non-null nextPageKey -> `SchemaViolation("unexpected pagination")` (dynatrace.py:223-225); missing dimension -> SchemaViolation (dynatrace.py:128-130); bucket date = UTC(ts).date()-1d kept when a<=date<b (V-5 default) (dynatrace.py:136-137); null -> None (passed through; metric_batch maps None).
  - ✅ Requests built only from validated settings and bound windows; the only response-derived request value is Dynatrace nextPageKey, sent alone after CursorGuard (check 1).
  - ✅ All HTTP via T01-14 `SourceHttp.get_json` -> `retry_page`, relative paths on the egress client of `base_url` (egress client follow_redirects=False); no httpx/requests import in herness/connectors/monitoring (check 2).
  - ✅ Api-Token / bearer via `auth.build_auth(..., base_url=settings.base_url)`; error messages are fixed strings; 401 test asserts token absent from message/context/auth repr (check 3).
  - ✅ Rows via base `event_batch`/`metric_batch` with keyword `batch_rows`, base.py untouched (check 5).
  - ✅ Registry: only the ruled `_BUILTINS` rows added (registry.py +6).
  - ✅ Tests: UT01-80 / UT01-83 in every test name and docstring; `pytestmark = pytest.mark.unit`; hosts `*.example.invalid`; tokens/keys start with `synthetic` (check 6).
- ⚠️ Cannot verify from diff:
  - V-5 (Dynatrace slot-end timestamp semantics) still rests on the default; no recorded fixture was frozen ("T01-20 fixture freeze", §13). Needs a real-tenant capture before closing V-5.
  - §11 names respx cassettes under `tests/fixtures/connectors/<source>/` and UT01-80 setup says "query_range cassette"; the card uses inline synthetic bodies instead (no cassette dir exists in the repo yet for any source, so this matches current practice).
  - Performance limits (3 y x 50 metrics < 10 min; 500 events/s) not benchmarked by this card (no BT listed for it).

### Strengths
- Small, flat helpers; every shape check yields a fixed-text SchemaViolation with no response data; numeric edge cases (bool timestamps, overflow, NaN/Inf, length mismatch) covered.
- Lazy `SourceHttp` keeps adapter construction side-effect free (factory call makes no client, resolves no secret) and is tested.
- Tests assert exact request params/paths/headers, including the Mimir `/prometheus` prefix merge and second-page `nextPageKey`-only params, and run auth through the real `build_auth` + fake keyring.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. prometheus.py:29,42-52 / dynatrace.py:29 — extra public `source_http` (not in the module-map row, which lists only `PrometheusAdapter`), and Dynatrace imports it from a sibling adapter module; T01-21 (Datadog, Splunk) will presumably do the same, making prometheus.py a de facto shared module. Acceptable for this card given base.py (T01-19) and http.py (T01-14) are closed and no extra file is listed for the card — the alternatives (private name imported cross-module, or duplicating 8 lines) are not better. Record it: add `source_http` to the module-map row for prometheus.py now, and open a spec follow-up to relocate it to `monitoring/base.py` (or `connectors/http.py`) when those cards are next amended.
2. prometheus.py:139-143 / dynatrace.py:166-170 — lazy build is a timing deviation from §3.16 ("when http is None it is built"): missing secret / ConfigError now surfaces on the first `check()`/fetch, not at `build_connector`. Behaviourally equivalent for the runner (check() is called first); note it in the spec as the convention for T01-21.
3. tests/unit/connectors/_prometheus_data.py — shared by both adapters' tests (and likely T01-21); a neutral name such as `_monitoring_data.py` would read better.
4. dynatrace.py:76 — `# type: ignore[return-value]` where `cast("dict[str, Any]", body)` would state the checked type without suppressing mypy.
5. dynatrace.py:118 — payload is the whole problem object (`evidenceDetails`, `impactAnalysis` can be large); bounded only by the 64 MiB page cap (TH01-04) — acceptable per spec silence, worth a note if lake payload size becomes a concern.
6. prometheus.py:151-153 — `check()` also requires `status=="success"`; stricter than U01-82 (which only names the request). Harmless, keep.
7. Process: report states tests and code were written in one pass, no RED run captured (TDD order deviation).
8. Report concern (pre-existing, outside card): tests/unit/core/test_registry.py autouse fixture clears `_BUILTINS` permanently, so order-dependent failure of UT10-19 C03 is possible; route to impl 10 follow-up. UT10-19 C03 must be updated by T01-21 when splunk lands.

### Assessment
**Task quality:** Approved
**Reasoning:** Both adapters implement U01-82/U01-85 exactly (params, step invariant, paging rule with CursorGuard, date rules, error shapes) through the T01-14 client and base row builders, with 99-100% coverage and all gates clean; remaining items are placement/naming/documentation polish and the open V-5 fixture freeze.
