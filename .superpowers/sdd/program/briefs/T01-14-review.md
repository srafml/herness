# T01-14 review (verify) — HTTP layer, head 7607566 (base 3f61b67)

### Spec Compliance
- ✅ Spec compliant
  - ✅ U01-58 http_client: only `egress.source_http_client(name, base_url, timeout_s, verify=True|Path, max_connections=min(2*mc,64), max_response_bytes=MAX_RESPONSE_BYTES)` (http.py:75-92); name = stream key up to ':'; no client/transport construction and no `loopback_http_client` anywhere under herness/connectors (grep). `verify` derived from `settings.verify` (http.py:84) — equivalent to `SourceSettings.httpx_verify()` (settings_base.py:319-321); MonitoringAdapterSettings has no httpx_verify but the same validated `verify: Path|None` (settings_entities.py:117).
  - ✅ base.py re-exports `http_client` (base.py:17); UT01-63 asserts identity.
  - ✅ U01-59 get_json/post_json = `retry_page(lambda: self._once(...), source=breaker_key)` (http.py:232-251); retry_page → policy `source_http_page` (retry.py:303-305). No hand-rolled loop/sleep; `parse_retry_after` only imported from impl 08 (http.py:26, :113), never defined under herness/connectors (R-70).
  - ✅ `fault_point("http.page", source=...)` first statement of every attempt (http.py:296, :313); test asserts 2 points for 503→200.
  - ✅ Body capped while reading: `_read_capped` accumulates `iter_bytes()` chunks and raises at > 64 MiB (http.py:159-166); egress `EgressBlocked(reason=response_too_large)` → `SchemaViolation("response too large")` per ruling (http.py:153-156); other EgressBlocked re-raised. `httpx2.HTTPError` → `classify(exc, family="source")` (http.py:152). JSON failure → "malformed JSON" (http.py:169-178).
  - ✅ Messages: "<reason>: HTTP <status> <METHOD> <path>" using `request.url.path` (no query, no body, no headers) (http.py:95-114); log event `connectors.http.page_fetched` DEBUG with exactly source/status/bytes/elapsed_ms (http.py:304-305); metric `herness_connectors_pages_total{source,status_class}` (http.py:34, :288-289). No response text ever enters a message, so redaction-before-truncation does not arise.
  - ✅ U01-60 table verbatim (2xx None, 3xx "unexpected redirect", 400 ConfigError "source rejected request", 401/403 AuthError, 404 SourceNotFound, 408 SourceUnavailable, 429 RateLimited(retry_after=parse_retry_after(headers, now)), 5xx SourceUnavailable, other 4xx "unexpected status").
  - ✅ CursorGuard messages "pagination cursor repeated" / "page limit exceeded"; constants 67_108_864 / 1_048_576 / 1_000_000 verbatim.
  - ✅ U01-62 ForeignHostError(host), SourceNotFound(path) subclass SchemaViolation, copy-safe via `_extra_attrs`.
  - ✅ post_form_lines: retried until first line, later errors SourceUnavailable no retry, 1 MiB line cap, bad/non-object line SchemaViolation.
  - ✅ Threat tests assert the threat, not just happy paths:
    - ST01-01: `verify: false`/"false"/missing bundle rejected by config; real local self-signed TLS server → SourceUnavailable with ConnectError cause; CA-bundle control.
    - ST01-02: real egress client over MockNet; foreign `Link` and `@odata.nextLink` → ForeignHostError; evil host call_count 0; bearer seen only by the source host; plus bearer/query/body absent from error str/repr/context and all captured logs for 401/403/404/429/503.
    - ST01-04: same cursor forever → "pagination cursor repeated"; page limit; lazily streamed 65 MiB page via both SourceHttp cap and egress transport cap → SchemaViolation, no retry.
    - ST01-05: test-local connector over SourceHttp with real SyncRunner: `result` object and record missing `sys_updated_on` → SchemaViolation, watermark unchanged, no commit (ServiceNow rows carried to T01-16 per ruling).
    - ST01-06: `Retry-After: 1000000` → RateLimited re-raised with zero sleeps, retry_after clamped 86400, `decide_failure` → requeue at now+86400; control with 7 s sleeps once.
  - ✅ Lints unedited (no diff under tests/security, tests/unit/harness, test_connectors_http_lint.py); ST10-25 + ST01-14 + ST05-13 run: 122 passed.
  - ✅ Budgets: http.py 320/320, base.py 165/170; check_module_size exit 0.
  - ✅ Test IDs in names and docstrings; pytestmark unit on all four files.
- ⚠️ Cannot verify from diff / carry-overs:
  - post_form_lines streams share the egress client's 64 MiB `max_response_bytes`, so a Splunk export > 64 MiB total is refused (spec sets that cap for every client) — needs a ruling in T01-19.
  - `check_next_url` compares only with the `base_url` host+port (U01-62 wording); U01-59's "host is allowed" could be read to include `sources.<name>.hosts`. A next link to a listed extra host is refused (safe direction). Confirm with owner if a connector (Dataverse/Jira) needs it.
  - FT01-04/FT01-05 belong to later cards.

### Gates run (verify, worktree)
- `pytest tests/unit/connectors -q -p no:logging`: 646 passed, 4 skipped (host symlink / duckdb-excel), exit 0.
- Coverage `-k http` on herness.connectors.http: 99% (194 stmts, 1 miss line 200; branch 186->184 partial) — >= 90/85.
- ruff check: clean. mypy: Success (305 files). lint-imports: 13 kept, 0 broken. check_module_size: exit 0.

### Strengths
- Tight, single-path design: every page attempt goes fault_point → egress client → status map → capped read → decode, inside retry_page.
- Threat tests use the real egress transport (MockNet) and a real TLS server where it matters; leak test checks errors and logs of retries too.
- Added same-port requirement in check_next_url closes a real bearer-leak variant (other port on same host).

### Issues
#### Critical (Must Fix)
- None.
#### Important (Should Fix)
- None.
#### Minor (Nice to Have)
1. herness/connectors/http.py:288 — `status_class` is `f"{status // 100}xx"`, so a 3xx (or 1xx) page emits label values outside the spec's documented set (`2xx`, `4xx`, `5xx`, spec line 2820). Either document `3xx` or clamp.
2. herness/connectors/http.py:253-261 — post_form_lines neither logs `connectors.http.page_fetched` nor records bytes/elapsed; the spec's "each page" log is arguably N/A for a stream, but a single DEBUG at stream end would keep observability parity.
3. herness/connectors/http.py:218 — constructor default `clock=None` (falls back to `herness.core.time.now` at call time, http.py:290) instead of spec `clock=time.now`; behaviourally equivalent and freezegun-friendly; note only.
4. herness/connectors/http.py:84 — reads `settings.verify` instead of calling `settings.httpx_verify()` as U01-58 step 2 says; equivalent today (settings_base.py:319-321) but duplicates that rule; would drift if httpx_verify changes.
5. herness/connectors/http.py:162-165 — cap is checked after appending each decoded chunk; one decompressed chunk may overshoot the 64 MiB bound before refusal (bounded by zlib ratio on a network chunk, and the egress transport also caps). Acceptable; note only.
6. http.py is at 320/320 — any addition (e.g. item 2) needs a budget ruling.

### Assessment
**Task quality:** Approved
**Reasoning:** All units, exact values, threat rows and gates match the brief and group rulings; client comes only from egress, retries only via retry_page with impl 08 Retry-After, bodies capped while read, no secrets/query/body in messages or logs. Remaining items are minor observability/wording nits and carry-overs.
