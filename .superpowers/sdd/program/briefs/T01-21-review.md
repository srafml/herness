# T01-21 review — Datadog and Splunk adapters (U01-83, U01-84)

Reviewer: verify agent · worktree agent-a8f201d6cf7eb3139 · HEAD 2860299 (base feb27f7)

### Spec Compliance
- ✅ Spec compliant (with the rulings in force: registry `_BUILTINS` rows, UT10-19 C03 update, Splunk per-UTC-day export cut, test_registry isolation fix).
  - Splunk window cut (splunk.py:70-77): pieces start at `since`, cut at each UTC midnight via `floor_day(low)+1d`, end at `until`; each bound floored to whole epoch seconds with the same rule, so latest(n) == earliest(n+1) and the next run's floored `since` equals this run's floored `until` -> contiguous, no gap/overlap, earliest inclusive / latest exclusive; derived from the bound window only. Tested (test_monitoring_splunk.py UT01-82 export_paged_per_utc_day, metrics_per_day_rows).
  - Splunk fail-closed: response cap is the egress transport (`max_response_bytes=MAX_RESPONSE_BYTES`, http.py:91); a breach mid-stream raises `EgressBlocked(response_too_large)` inside `_translating(later=True)` -> `SchemaViolation("response too large")` (http.py:145-157), never truncation. Lines > 1 MiB -> `SchemaViolation("line too large")` (http.py:192-205); 2 MiB line tested.
  - `validate_spl` re-run before any request (splunk.py:59-67, inside `_export` before the first post); `search ` prefix unless leading `|`; preview / result-less lines skipped; ERROR/FATAL messages -> SchemaViolation; metric filter `a <= date < b` (splunk.py:205); missing service label -> SchemaViolation (splunk.py:127-130). All tested.
  - Datadog: keys via `build_auth` (`api_and_app_key`, origin-scoped) through `prometheus.source_http`; never in logs/errors (403 test checks message, repr, context, auth repr); cursor-only pagination with `CursorGuard.step` (datadog.py:203-214); body = settings + window + cursor only; stop on empty `data` / absent `after`; 429 -> `RateLimited.retry_after == 12` from `X-RateLimit-Reset` via `map_http_error`/`parse_retry_after` (R-70); `.rollup(agg, 86400)` appended; `to = b - 1`; `status == "error"` -> SchemaViolation; event mapping per V-4 default.
  - All HTTP via `SourceHttp` (get_json / post_json / post_form_lines -> retry_page); no httpx/requests import in either module; ST01-14 lint untouched.
  - Rows via `event_batch` / `metric_batch` with kw `batch_rows`, flushed every `batch_rows`.
- ⚠️ Cannot verify from diff:
  - Datadog field paths remain the §13 V-4 default (verified in Phase 6 per spec) — not checkable against a live tenant.
  - The streamed (post_form_lines) 64 MiB overflow path is verified by code reading only; the existing ST01-04 test covers `get_json`, not the export stream (see m3).

### Strengths
- Small, flat helpers; complexity and arg limits respected (ruff C901/PLR0913 clean).
- Lazy `SourceHttp` (`cached_property`) means construction resolves no secret; factory tests assert `_http` absent.
- Tests are behavioural: exact request bodies/forms/params per page, window bounds as epoch strings, batch sizes, error messages, header presence on own vs foreign origin.
- Verified locally: `pytest -k "UT01_81 or UT01_82"` 60 passed; coverage datadog.py 99% (one generator-exit arc, datadog.py:226->exit), splunk.py 100% (line+branch); `ruff check .` clean; `mypy` 0 issues (341 files); `check_module_size` clean (datadog 248/260, splunk 222/250).
- test_registry.py change is minimal (+2 lines) and restores `_BUILTINS` after each test.
- Test hosts under `example.invalid`; every credential starts with `synthetic`; IDs in test names and docstrings; `pytestmark = pytest.mark.unit` in both files.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- m1 herness/connectors/monitoring/splunk.py:73-77 — `_windows` can emit a zero-length window (`earliest == latest`) when `since`/`until` (or the last piece after a midnight) fall within the same whole second (e.g. until = midnight + 0.5 s). Harmless (Splunk returns nothing) but sends a wasted export request; skip pieces whose floored bounds are equal.
- m2 herness/connectors/monitoring/splunk.py:132 — `float(raw)` accepts a JSON boolean (`true` -> 1.0) as a metric value; Datadog points reject bool timestamps but Splunk values do not. Refuse `bool` explicitly for symmetry.
- m3 tests/unit/connectors/test_monitoring_splunk.py — no test drives a > 64 MiB export stream through the egress cap to prove `SchemaViolation("response too large")` on the `post_form_lines` path (ST01-04 covers only `get_json`, tests/unit/connectors/test_http_pages.py:280). The fail-closed claim in the report rests on code reading (`_translating(later=True)`, http.py:145-157). Worth one fault/ST test when the shared http tests are next touched.
- m4 tests/unit/connectors/test_monitoring_datadog.py (rate_limited test) — `assert len(seen) >= 1` is a weak assertion; asserting the retry count would pin retry_page's behaviour on 429.
- m5 herness/connectors/monitoring/{datadog,splunk}.py — `source_http` still imported from prometheus.py (already parked as T01-20 Minor; carried, not new).

### Assessment
**Task quality:** Approved
**Reasoning:** Both adapters match U01-83/U01-84 and the rulings in force (per-UTC-day Splunk cut is contiguous, floored, window-bound, and fails closed under the shared cap); security controls (SPL re-validation, origin-scoped keys, cursor guard, line cap) are in place and tested, and all gates pass. Only polish items remain.
