# T03-12 review: OpenJev backend (head 8204f7b, base 750ec27)

**Verdict: Approved** (no Critical or Important findings; Minor items below can go into a later polish card or U03-55)

## Evidence I ran myself (read-only; one mutation pass fully reverted, file byte-identical afterwards)
- pytest (PYTHONUTF8=1, -p no:logging) on the 3 new test files and test_decider_protocol.py: 50 passed. Coverage on herness/enrich/deciders/openjev.py: 174 statements / 30 branches, 100 % line and branch.
- ruff check and ruff format --check on openjev.py, tests/unit/enrich and tests/fault/enrich: clean. mypy openjev.py: 0 issues. lint-imports: 13 kept, 0 broken. openjev.py 320 lines against a budget of 320.
- ST10-25 scan_tree(REPO): 4 findings, all in herness/harness/llm/openai_compat.py (known-red). **None come from openjev.py or the new tests.**
- Mutation pass (10 mutants): killed 4 (fault_point removed; on_rate_limited removed; samples always sent; per-request headers removed). Survived 6 (see Minor M1 to M4): close before shutdown; close only on success; shutdown(wait=False); breaker_key changed; partial answer set accepted; body forwarded into the classify mirror.

## Spec compliance
| Unit / test | Status | Notes |
|---|---|---|
| U03-52 constructor | ✅ | name, version, samples validated (bool rejected), nothing built at construction, key held as SecretStr (masked in repr). client_factory typed as httpx2 (ruling). |
| U03-53 steps 1-3 | ✅ | Runs asyncio.run. One client comes from `egress.loopback_http_client(base_url, timeout_s=settings.timeout_s, bearer=key)`, looked up at call time. The code builds a pool of `concurrency` workers and an AdaptiveLimiter. The body has steps 1 and think 0; samples is omitted when None. |
| U03-53 step 4 | ✅ | aretry_call("decider_local", breaker_key="decider:openjev") runs inside limiter.slot(). fault_point("decider.batch") fires once per send call. The blocking post runs through run_in_executor. Status mapping: 400/422 -> OVE; 401/403 -> AuthError; 429 -> on_rate_limited then RateLimited (via the classify mirror); everything else -> classify. |
| U03-53 step 4 "rejects > 1 MB" | ✅ (with ⚠️) | load_wire_body rejects bodies over MAX_BODY_BYTES as OVE, which becomes an item error after one resend. This matches the §6 table row for malformed output. The client still reads up to 50 MiB before that check runs (Minor M5). |
| U03-53 steps 5-6 | ✅ | Strict parse; a missing answer is OVE. Each item is sent at most twice, then gets error="OutputValidationError" plus an item_failed WARNING with exactly decider and error_class (the test pins the whole event). |
| U03-53 step 7 | ✅ | A TaskGroup cancels the siblings. The first failure is re-raised unwrapped with its cause kept. EgressBlocked, ConfigError and FatalError also propagate. |
| U03-53 step 8 | ✅ | Latency is timed around every post (the timer records on exceptions too). The errors counter is incremented per failed request, labelled {decider, error_class}. CircuitOpen raised by aretry_call before any send is not counted (acceptable). |
| U03-53 step 9 | ✅ | `finally: pool.shutdown(wait=True); client.close()` covers the normal path, the exception-group path and the asyncio.run path. Tests do not pin this order (M1). |
| U03-54 health | ✅ | timeout_s=5, bearer, GET /v1/models, `data[*].id` membership, client closed in finally, any failure -> ModelUnavailable("openjev health: <HTTP n / class / model not listed>"). The 64 KB check happens after the client has buffered the whole body (M5). |
| UT03-50 | ✅ | |
| UT03-51 | ✅ | Order, samples None/5, steps/think, spy on loopback_http_client (args, timeout 30, bearer), bearer header on every request, client closed. |
| UT03-52 | ✅ | Item 2 malformed twice -> 2 requests; 401/403 -> AuthError (1 request); 529/500/503 x3 -> ModelUnavailable; also 429, over-cap, transport and EgressBlocked cases. |
| UT03-53 | ✅ | Listed, unlisted, 500, bad JSON, not a list, over 64 KB, transport error. |
| ST03-16 | ✅ | The server echoes the synthetic key. The test checks traceback, str/repr/args/notes along the chain, structlog capture, stdout/stderr and repr(decider). |
| FT03-02 | ✅ (partial, ruling) | Checked at decider level plus a fault plan at decider.batch. The "next chain member" half is carried over (T08-10 / impl 11). |
| UT03-45 row | ✅ | |
| Acceptance: no client built outside egress (R-06 / ST10-25) | ✅ | |
| Rulings: T08-04b bridges | ✅ | `_classified` mirrors only the status, the path URL without its query, and the Retry-After / X-RateLimit-Reset headers. It carries no body and no request headers. `_transport_error` messages give the class name only. |

⚠️ Cannot fully verify:
- Real container behaviour (V-10, V-15): the health shape `data[*].id` is unconfirmed.
- The acceptance item "tests pass against StubDeciderServer" is carried over (impl 11).

## Findings

### Critical
None.

### Important
None.

### Minor
- **M1: exit-path ordering and close are untested.** The code at herness/enrich/deciders/openjev.py:172-174 is correct. However, these mutants all pass the suite: swapping close and shutdown, moving `client.close()` out of `finally`, and `shutdown(wait=False)`. Fix: in test_ut03_52_auth_error_propagates (tests/unit/enrich/test_openjev_decider.py:266), spy the client through the loopback_http_client spy and assert `client.is_closed`. Add one test that wraps `ThreadPoolExecutor.shutdown` and `Client.close` to record call order and the `wait=True` argument on an AuthError path.
- **M2: breaker key is not asserted** (openjev.py:270). Changing `"decider:openjev"` goes undetected. Fix: in test_ut03_52_unavailable_after_policy_retries, assert that the ops-store breaker row or process_state breaker for `decider:openjev` recorded the failures.
- **M3: no test for a partial answer set.** Changing openjev.py:77 to `if not parsed:` survives. Fix: add a `partial-answers` case to test_ut03_52_invalid_replies_become_item_errors (test_openjev_decider.py:245) that answers only `is_outage` of the three asked questions.
- **M4: ST03-16 cannot catch body forwarding through the classify mirror.** The mutant `httpx.Response(..., content=resp.content)` at openjev.py:88 survives, presumably because classify redacts or omits 5xx bodies. The code is correct today. Fix (optional): add a unit assertion that the mirror response built by `_classified` has empty content and only the retry headers.
- **M5: memory bound for the 1 MB and 64 KB caps.** openjev.py:292-296 and 305 keep the loopback default of `max_response_bytes` (50 MiB). A misbehaving local server can therefore make up to `concurrency` x 50 MiB be buffered before load_wire_body or `_listed_models` rejects it. This is legal under the spec's literal call and TH03-06 is still met, but it is weak for TH03-09. Fix:
  - In `health`, pass `max_response_bytes=_HEALTH_MAX_BYTES` (EgressBlocked is already wrapped as ModelUnavailable, so the manual check at :252 becomes a backstop).
  - For decide, pass `max_response_bytes=MAX_BODY_BYTES + 1` and map `EgressBlocked` whose reason is `response_too_large` to `OutputValidationError` inside `_send`.
  - Needs a spec note (U03-53 step 2 call) and line room; openjev.py is at 320/320, so do it with the U03-55 split.
- **M6: weak cancellation assertion.** test_ut03_52_auth_error_cancels_the_other_items (test_openjev_decider.py:277) only asserts AuthError. Fix: with concurrency=1, assert that fewer than 4 requests reached the server.
- **M7: bearer equality in the spy.** test_openjev_decider.py:133 asserts `kwargs["bearer"] is not None`. Fix: assert `kwargs["bearer"].get_secret_value() == "unit-openjev-token"`.
- **M8: traceback-only key reachability (informational).** openjev.py:216 and :318 chain the httpx2 exception (`from exc`). Its `.request` object holds the Authorization header, but it is never rendered: repr, str and traceback show only the method and URL. No change needed; if ST03-16 is extended, add a transport-error case.
