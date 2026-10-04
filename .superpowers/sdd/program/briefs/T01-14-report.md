# T01-14 report — HTTP layer (impl 01 U01-58, U01-59, U01-60, U01-62) — re-dispatch

Builder: worktree agent-a2e2968b96a9d11d5 (branch worktree-agent-a2e2968b96a9d11d5, base 3f61b67).
Source: salvage of the lost builder (agent-a73ed476d73f84d98, base 984de12) from
.superpowers/sdd/program/salvage/T01-14/, applied unchanged, then reviewed against the spec and fixed.
Status: DONE_WITH_CONCERNS (see Concerns). Final commit: 7607566 feat(connectors): T01-14 HTTP layer (http_client, SourceHttp, error mapping); all pre-commit hooks incl. pytest-unit Passed

## What was built (salvaged design, reviewed)
- herness/connectors/http.py (320/320): http_client, SourceHttp, JsonPage, CursorGuard, map_http_error,
  ForeignHostError, SourceNotFound, MAX_RESPONSE_BYTES 67_108_864, MAX_LINE_BYTES 1_048_576,
  MAX_PAGES_PER_STREAM 1_000_000, PAGES_METRIC.
  - http_client -> herness.core.egress.source_http_client(name = stream key up to ':', base_url, timeout_s,
    verify True|Path, max_connections=min(2*max_concurrency, 64), max_response_bytes=MAX_RESPONSE_BYTES).
    Constructs nothing; loopback_http_client never used. verify is derived from settings.verify directly
    (equivalent to SourceSettings.httpx_verify(); MonitoringAdapterSettings has no httpx_verify()).
  - get_json/post_json = retry_page(lambda: _once(...), source=breaker_key) (policy source_http_page);
    _once: fault_point("http.page", source=...) every attempt; client.stream with literal "GET"/"POST";
    default User-Agent herness/<version> + Accept JSON, caller headers merged; status not in allow_status ->
    map_http_error raised; body read via iter_bytes capped at 64 MiB (SchemaViolation "response too large");
    transport EgressBlocked(reason=response_too_large) -> same SchemaViolation (ruling w21-s01a), other
    EgressBlocked re-raised; httpx2.HTTPError -> classify(exc, family="source"); bad JSON -> SchemaViolation
    "malformed JSON"; DEBUG log connectors.http.page_fetched (source,status,bytes,elapsed_ms);
    herness_connectors_pages_total{source,status_class} via herness.core.resilience.metrics.record_counter.
  - post_form_lines: request + first line inside retry_page; later stream errors SourceUnavailable
    (no retry); line cap 1 MiB; invalid / non-object line -> SchemaViolation.
  - check_next_url: absolute, https (http only when base is loopback http), no userinfo, same host AND
    (new) same port as client.base_url; else ForeignHostError(host).
  - map_http_error: U01-60 table; 429 -> RateLimited(retry_after=impl 08 parse_retry_after(headers, now));
    messages "<reason>: HTTP <status> <METHOD> <path>" (no query/body/credentials).
  - No parse_retry_after and no retry loop under herness/connectors (R-70).
- herness/connectors/base.py (165/170): re-exports http_client.

## Changes vs. the salvage
1. check_next_url now also requires target port == base_url port (TH01-02: a Link to the same host on
   another port would otherwise get the bearer). Test row added to test_ut01_63_check_next_url_refusals
   ("https://sn.example:8443/api/x").
2. New threat test test_st01_02_bearer_and_query_never_in_errors_or_logs[401,403,404,429,503]: with a
   bearer auth and a query string, the raised error (str/repr/context/__dict__) and every captured log
   event (including retry events) contain neither the token, "Bearer", the query key/value nor the body.
3. ruff format/--fix normalisation only otherwise. Dependencies re-checked at 3f61b67: source_http_client
   (egress_clients.py:201), retry_page(fn,*,source) (retry.py:303), classify, parse_retry_after,
   fault_point, record_counter, settings_base all present with the salvage's signatures.

## Rulings applied (w21-s01a, unchanged)
httpx2 everywhere (SourceHttp takes httpx2.Client/Auth; tests use httpx2.MockTransport / MockNet instead of
respx); EgressBlocked "source response too large" -> SchemaViolation("response too large"); ST01-05 via a
test-local connector over SourceHttp with the real SyncRunner (ServiceNow-specific rows carry to T01-16).

## Tests (tests/unit/connectors, pytestmark unit; each name + docstring carries the ID)
- UT01-60 test_http_errors.py: test_ut01_60_status_table[302,400,401,403,404,408,429,500,529,418],
  _success_maps_to_none, _redirect_and_unexpected_status_reasons, _429_retry_after_seconds,
  _429_without_header_has_no_retry_after, _404_is_source_not_found_with_path, _response_without_request_still_maps.
- UT01-62 test_http_pages.py: test_ut01_62_page_over_64_mib_is_schema_violation, _invalid_json_is_schema_violation,
  _503_twice_then_200_retries_only_that_page, _json_page_fields_and_default_headers, _post_json_and_allow_status,
  _page_logs_metrics_and_fault_point, _transport_error_is_classified, _form_lines_yields_objects,
  _form_lines_limits_and_errors, _form_lines_later_stream_error_not_retried.
- UT01-63 test_http_client.py (+ test_http_errors.py): _servicenow_https_arguments, _monitoring_loopback_arguments,
  _missing_base_url_is_config_error, _base_reexports_http_client, _request_to_other_host_is_egress_blocked,
  _redirect_is_not_followed, _check_next_url_to_other_host, _check_next_url_refusals[6],
  _check_next_url_accepts_own_host, test_ut01_63_error_classes_are_fatal_and_copy.
- ST01-01 test_http_client.py: test_st01_01_verify_false_rejected_by_config[3],
  _self_signed_server_is_source_unavailable (real local TLS server), _ca_bundle_path_trusts_the_server.
- ST01-02 test_http_pages.py: test_st01_02_foreign_link_and_odata_next_link, test_st01_02_bearer_and_query_never_in_errors_or_logs[5].
- ST01-04: test_st01_04_repeated_cursor_is_schema_violation, _page_limit_is_schema_violation, _egress_client_caps_65_mib_page.
- ST01-05 test_http_runner.py: test_st01_05_unexpected_shape_keeps_watermark_and_commits_nothing[2].
- ST01-06: test_st01_06_huge_retry_after_reraised_and_job_rescheduled, _small_retry_after_is_waited_then_retried.

## Gates (worktree, .venv python)
- pytest tests/unit/connectors + ST10-25 (tests/security/test_st10_lint.py) + ST05-13 (tests/unit/harness/test_llm_anthropic.py -k st05_13)
  + ST01-14 lint (tests/unit/connectors/test_connectors_http_lint.py): 731 passed, 4 skipped (symlink/duckdb-excel host skips).
- HTTP subset (-k http): 104 passed at salvage apply; +6 new cases after review, all pass.
- ruff format / ruff check: clean. mypy (full, 305 files): Success. lint-imports: 13 kept, 0 broken.
  check_type_ownership: 0. check_module_size: 0 (http.py 320/320, base.py 165/170).
- pre-commit (fast hooks, staged): all Passed incl. detect-secrets (token fixture carries pragma allowlist; no baseline change).
- Full unit hook suite on the salvage tree: 8361 passed, 11 skipped (first commit attempt; that commit did not land — see concerns).

## Concerns / carry-overs
- http.py is exactly at budget (320/320); any later addition needs a ruling.
- The wip(T01-14) checkpoint commit attempt ran the hooks (16 min unit suite, green) but did not land
  (a formatting hook most likely modified a salvaged file; output was truncated). Work went straight into the final commit.
- post_form_lines streams (Splunk export) share the egress client's 64 MiB max_response_bytes, so an
  export larger than 64 MiB total is refused by the transport (SchemaViolation "response too large"). The
  spec sets max_response_bytes=MAX_RESPONSE_BYTES for every client; flag for T01-19 (Splunk).
- check_next_url checks only the base_url host (U01-62 wording), not the extra sources.<name>.hosts.
- ServiceNow-specific ST01-05 rows carry over to T01-16; FT01-04/FT01-05 belong to later cards.
