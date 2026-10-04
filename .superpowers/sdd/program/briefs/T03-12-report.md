# T03-12 report: OpenJev backend

Status: DONE_WITH_CONCERNS (carry-overs only; all gates green apart from the pre-existing known-red set)
Worktree: D:\herness\.claude\worktrees\agent-ac976b1dc41f198eb (branch worktree-agent-ac976b1dc41f198eb, base 750ec27)
Commits: c53ee00 wip(T03-12): OpenJevDecider with UT03-50..53 green; f27f019 wip(T03-12): ST03-16, FT03-02 and UT03-45 conformance row; final: 8204f7b feat(enrich): T03-12 OpenJev backend.

## Files (lines)
- herness/enrich/deciders/openjev.py: 320 / budget 320 (new). `_JevHttpBackend` (shared request logic, U03-53; reusable by U03-55 via `_open_client`, `_policy`, `_breaker_key`, `_path`, `name`, `version`) and `OpenJevDecider` (U03-52, U03-54). No sibling module needed; no §2 row change.
- tests/unit/enrich/_openjev_support.py: 132 (new; `ScriptedNet` = MockNet subclass answering per request body, hand-built Jev payloads, `jev_env` fixture: migrated ops store bound as resilience backend + full test config + fixed-key redactor + fresh ProcessState)
- tests/unit/enrich/test_openjev_decider.py: 456 (new)
- tests/unit/enrich/security/test_openjev_security.py: 79 (new)
- tests/fault/enrich/test_openjev_fault.py: 64 (new, marker `fault`)
- tests/unit/enrich/test_decider_protocol.py: 81 (UT03-45 table: `_openjev` row added)

## Test IDs -> functions
- UT03-50: test_ut03_50_version_from_pinned_image (tag cut from an OpenJevDeploy pin `razorback16/openjev:0.4.0@sha256:...` -> `openjev-0.4.0/openjev-latest`), test_ut03_50_samples_must_be_1_3_or_5 (0, 2, 4, 7, True -> ConfigError), test_ut03_50_construction_builds_no_client
- UT03-51: test_ut03_51_three_items_in_order[None|5] (order; samples omitted/sent; steps 1 / think 0; spy on herness.core.egress.loopback_http_client: called once with (base_url,), timeout_s=30.0, bearer set; bearer header on every request; client closed), test_ut03_51_no_key_sends_no_authorization, test_ut03_51_client_factory_and_question_ids, test_ut03_51_latency_and_error_metrics
- UT03-52: test_ut03_52_malformed_item_retried_once_then_error (item 2: 2 requests, error output, one item_failed WARNING with decider, error_class), test_ut03_52_malformed_once_then_ok, test_ut03_52_invalid_replies_become_item_errors[400|422|missing-answers|no-answers|out-of-range], test_ut03_52_auth_error_propagates[401|403] (1 request), test_ut03_52_auth_error_cancels_the_other_items, test_ut03_52_unavailable_after_policy_retries[529|500|503] (3 requests = decider_local attempts), test_ut03_52_other_status_classified (404 -> ConfigError via classify), test_ut03_52_transport_errors_are_model_unavailable, test_ut03_52_unknown_transport_error_is_fatal, test_ut03_52_rate_limited_then_ok (limiter.on_rate_limited called, retried), test_ut03_52_rate_limited_over_cap_propagates (Retry-After 120 > cap 30), test_ut03_52_non_loopback_base_url_is_blocked (EgressBlocked, no request)
- UT03-53: test_ut03_53_health_ok_when_model_listed (GET /v1/models, timeout_s=5.0, bearer, closed), test_ut03_53_health_failures[unlisted|500|bad-json|not-a-list|over-64kb], test_ut03_53_health_transport_error_and_factory
- ST03-16: test_st03_16_key_absent_from_exceptions_and_logs[case0..3] (401/403/500 on decide, 401 on health; the server echoes `synthetic-openjev-key` in its body; checks traceback.format_exception plus str/repr/args/__notes__/vars along the cause/context chain, structlog capture_logs, rendered stdout/stderr, repr of the decider)
- FT03-02: test_ft03_02_malformed_json_for_one_item (decider level, truncated JSON for item 2: one retry, then error output, others succeed), test_ft03_02_fault_plan_malformed_json_at_decider_batch (impl 08 fault plan `malformed_json` at `decider.batch`, count 2)
- UT03-45: test_ut03_45_decider_classes_conform[_openjev]

## Rulings applied
- No registry decorator (T03-16 registers). UT03-45 row added (constructed, no HTTP).
- Clients only via `herness.core.egress.loopback_http_client` (looked up as `egress.loopback_http_client` at call time so the spy sees it); tests build clients only through it; `ScriptedNet` reuses `MockNet.install` (no transport or client class referenced in test code).
- T08-04b carry-over (httpx2 vs classify): private helpers marked `# T08-04b:`.
  `_transport_error`: httpx2 ConnectError / TimeoutException / RemoteProtocolError -> ModelUnavailable("decider call failed: <Class>") (classify rule 3 parity); other httpx2 transport errors go to classify (-> FatalError "unclassified").
  `_classified`: non-mapped statuses (and 429) are mirrored into an `httpx.HTTPStatusError` (status plus only the Retry-After / X-RateLimit-Reset headers; no body, no request headers) and passed to `classify(..., family="decider")`, so 5xx/529 -> ModelUnavailable, 429 -> RateLimited(retry_after) with classify's Retry-After parsing, anything else -> ConfigError exactly as classify.
- FT03-02 lives in tests/fault/enrich/ (marker `fault`; the `fault_env` plugin exists and is used by the second function). "Reaches the next chain member" is a carry-over (no DeciderChain / StubDeciderServer).
- Hand-built payloads; no tests/fixtures/openjev.
- ST03-16 in tests/unit/enrich/security (marker unit), key `synthetic-openjev-key`; detect-secrets did not flag it (no baseline change).
- Metrics: the T08-05 sink exists: `timed(herness_enrich_decider_latency_seconds, component="enrich", labels={decider})` per request, `record_counter(herness_enrich_decider_errors_total, labels={decider, error_class})` per failed request (parse failures included). No `# T08-05:` markers needed.

## Deviations / spec notes
1. `client_factory` is typed `Callable[[], httpx2.Client]` (spec says `httpx.Client`): the loopback client is httpx2 per the T10-17 ruling. The module imports `httpx` only for `Request` / `Response` / `HTTPStatusError` (the classify mirror), inside the §2 allowance "httpx for types and exceptions only".
2. Parsing (step 5) runs inside the retried `send` (spec step 4 "200 -> parse"); `OutputValidationError` is not retryable for aretry_call, so the item-level "send once more" loop (step 6) is the only OVE retry. The breaker counts only ModelUnavailable (unchanged breaker semantics).
3. An answer set missing any asked question is an `OutputValidationError` (same rule as LayaDecider), so every successful output answers all asked questions. Items with no asked questions return `answers={}` without a request.
4. The bearer header is also passed per request (`headers=` on each post), so it reaches the server with a test `client_factory` too; with the production client it equals the client's default header.
5. Status bodies are never forwarded into errors (400/422 -> OutputValidationError("openjev: HTTP n"), 401/403 -> AuthError("openjev: HTTP n"), others via the body-less classify mirror): stricter than classify's redacted-body detail, for TH03-14 (a server echoing the key).
6. 1 MB rule: enforced by `load_wire_body` (OutputValidationError, item level); the client keeps its default `max_response_bytes` (50 MB, EgressBlocked above it). Passing `max_response_bytes=1 MB` would turn an oversize reply into a backend-level EgressBlocked, so it is not done.
7. Health: after a 200, an unreadable listing (bad JSON, > 64 KB) reports the error class (`openjev health: OutputValidationError`); an unlisted model or non-list `data` reports `openjev health: model not listed`; statuses report `HTTP n`; transport errors their class.
8. Backend-level failures cancel the gather via `asyncio.TaskGroup`; the first failure is re-raised unwrapped (cause preserved). On every exit the pool is shut down (wait=True), then the client closed.
9. `samples=True` is rejected (bool is an int).

## Carry-overs
- T08-04b: replace `_transport_error` / `_classified` with classify once it understands httpx2.
- FT03-02 second half (item reaches the next chain member) needs T08-10 DeciderChain + impl 11 StubDeciderServer; the acceptance "tests pass against StubDeciderServer" likewise.
- Recorded OpenJev 0.4.0 fixtures (tests/fixtures/openjev/, impl 11): tests use hand-built payloads.
- Real container: V-10, V-15 (health endpoint shape `data[*].id`).
- openjev.py is at 320/320: U03-55 (jev_hosted.py, budget 200) should subclass `_JevHttpBackend` without growing openjev.py.

## Coverage
herness/enrich/deciders/openjev.py: 174 statements, 0 missed, 30 branches, 0 partial -> 100 % line / 100 % branch (UT03-45, UT03-50..53, ST03-16, FT03-02).

## Gates
- ruff format --check .: clean. ruff check .: exactly the 7 known-red openai_compat TID251 hits.
- mypy (strict, configured files): 0 issues; mypy --explicit-package-bases on the new/changed test files: 0 issues.
- lint-imports: 13 kept, 0 broken. check_type_ownership: exit 0. check_module_size: exit 0 (openjev.py 320/320).
- PYTHONUTF8=1 pytest tests/unit/enrich tests/fault/enrich -q -p no:logging: 577 passed, 1 skipped. --require-test-ids on the new files: pass.
- ST10-25 scan: only the known-red openai_compat findings (an early `type ClientFactory = Callable[[], httpx2.Client]` alias was flagged and removed).
- Commits used `SKIP=pytest-unit` because the pytest-unit hook stops on the known-red tests/security/test_st10_lint.py::test_st10_25_repository_passes (openai_compat only); the ruff-check hook was NOT skipped and passed; detect-secrets passed.

## Fix round 1 (review minors M1-M4, M6, M7; test-only)
Commit: d201f95 test(enrich): address T03-12 review minors (T03-12). openjev.py unchanged (320/320).
- M1: test_ut03_52_auth_error_propagates now uses a client_factory and asserts the client is closed; new test_ut03_51_pool_shut_down_then_client_closed[normal|exception] records `("shutdown", True)` then `("close", None)` (pool identified by max_workers=2, so asyncio's default executor is ignored).
- M2: test_ut03_52_unavailable_after_policy_retries asserts the `decider:openjev` health row has failures == 3.
- M3: `partial-answers` case added to test_ut03_52_invalid_replies_become_item_errors (2 requests, item error).
- M4: new test_st03_16_classify_mirror_has_no_body_or_request_headers[429|500|529|404] spies `classify` in openjev: the mirror response has empty content, headers == {"retry-after": "120"} (an `x-echo` header with the key is dropped), no Authorization on the mirror request; key absent from exception text and logs.
- M6: cancel test uses 10 items at concurrency 1 and asserts <= 3 requests reached the server.
- M7: spy compares `bearer.get_secret_value()` to the key.
Mutants checked (all killed): close before shutdown; shutdown(wait=False); no client.close; breaker_key=None; completeness check weakened to `if not parsed`; body forwarded to the mirror; all response headers forwarded; gather(return_exceptions=True) without cancel; wrong bearer passed to loopback_http_client.
Gates: pytest tests/unit/enrich tests/fault/enrich -p no:logging: 584 passed, 1 skipped; ruff format/check clean on touched files; mypy --explicit-package-bases on touched tests: 0 issues. Commit used SKIP=pytest-unit (known-red ST10-25 only).
