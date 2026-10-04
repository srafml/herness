# T11-23 review — Fake LLM drivers and stub HTTP base (head ce2ef68, base a46205f)

### Spec Compliance
- ✅ Spec compliant (with the group ledger rulings applied: registry kind `llm_client`; `respx_router` returns a `ScriptRouter` built on `httpx2.MockTransport`; acceptance means one end-to-end card test per adapter; ST11-13 covers only the bind part).
  - U11-42 FakeLLMClient ✅: subclasses `ScriptedLLMClient` (which satisfies LLMClient/StreamCapable), accepts `Path | ScriptBook`, `dedup_key_resolver` is honoured, has a `book` property, never uses the network. Fixture `fake_llm_registered` is function-scoped and registers ("llm_client","fake"). The autouse `reset_herness_state` (tests/conftest.py:55-66) calls `reset_registry`. UT11-68, U11-42 half ✅ (path load, given book plus resolver, registration and reset).
  - U11-43 respx_router ✅: OpenAI and Anthropic JSON shapes; X-Herness-Role/Model-Role/Dedup-Key headers, with absent → `*`; http_500; http_429 with Retry-After: 1; malformed_json on both wires; hang → ReadTimeout; disconnect → RemoteProtocolError; mismatch → 500 `{"error":{"message":"no script","prompt_hash":16hex}}`. Acceptance ✅: OpenAICompatClient (real loopback client) and AnthropicClient (real EgressGuard, hybrid profile) each run a scripted tool→text loop end-to-end, and a scripted 429 becomes RateLimited(1.0). Anthropic malformed tool input is a string, which the adapter rejects on its own path (anthropic_client.py:133). UT11-71 ✅.
  - U11-44 StubHTTPServer ✅: binds 127.0.0.1 only, with the literal check before the socket exists and again in `server_bind`; port 0 by default; daemon handler and serve threads; poll_interval 0.05; Content-Length > 8 MB → 413 with the body never read; per-connection socket timeout of 10 s; counters and connection set under a Lock; `kill`, `kill_after(n)` and `POST /__control/kill` → 204, then the listener closes and later connects are refused; the service file merges under a lock. UT11-72 ✅.
  - U11-45 FakeLLMServer ✅: GET /v1/models; non-stream tool turn with arguments as JSON text (UT11-73 ✅); SSE with 16-char content deltas (40 chars → 3 chunks), one tool-call delta per call, a finish chunk, a usage chunk with `choices: []`, then `data: [DONE]`, and OpenAICompatClient.astream consumes it (UT11-74 ✅); hang_s (cut short by stop/kill) and disconnect.
  - ST11-13 bind part ✅: I re-ran the test file with a plugin that replaces `_stub_http_core._require_loopback` with a no-op. 6 of 7 tests failed, so the guard does the work in the test. The test counts zero socket creations and zero binds for 0.0.0.0, "", ::, 192.0.2.10 and localhost. `herness.core.egress_socket` is not touched by the diff.
- ⚠️ Cannot verify from diff: nothing open. The items above were checked by running them.

### Verification run (worktree, TMP=D:\tmp\T11-23-verify, since deleted)
- Card tests (test_fake_llm, test_stub_http, test_st11_stub_bind): 36 passed, 3 runs out of 3, about 8.4 s each. Also green with `-W error::ResourceWarning -W error::PytestUnhandledThreadExceptionWarning -W error::PytestUnraisableExceptionWarning`. After the session only MainThread is still alive, so no threads leak.
- Touched and neighbouring suites: tests/unit/eval, tests/unit/enrich, tests/security/test_st11_judge.py, test_llm_openai_compat.py, test_llm_anthropic.py → 1001 passed, 1 skipped (symlink privilege).
- ruff check . clean; ruff format --check . clean (670 files); lint-imports 13 kept; mypy (config) 261 files clean; mypy --strict on the 4 support modules clean; check_module_size exit 0 (fake_llm 188/400, _fake_llm_wire 242/250, stub_http 204/250, _stub_http_core 170/200; §2 rows added for both private siblings).
- `git grep "from tests|import tests" -- herness` finds nothing. tests/conftest.py registers `tests.support.stub_http` and `tests.support.fake_llm` once each. Every test function carries its ID.

### Carry-over swaps
The justification holds. FakeJudgeClient (tests/unit/eval/_judge_fixtures.py) raises arbitrary exceptions (EgressBlocked, OutputValidationError), repeats its last reply, records requests and reports the model name used by JudgeScore.model. The enrich `_fake_llm` answers through a seed-keyed callable and has an `as_text` mode. ScriptBook supports none of these: its faults are limited to five kinds and it keys on role/dedup_key/call index. A thin subclass would have to override `complete`/`acomplete` completely, so it would just be the existing fake under another name. Not a finding. The touched tests are green.

### Strengths
- The wire rendering (`_fake_llm_wire`) is shared by the in-process router and the HTTP server, so their matching and rendering cannot drift.
- `kill_after` closes the listener before it writes the answer that triggers the kill, so the "third connect refused" check does not race.
- The ST11-13 tests are real negative tests: they count socket creations and binds, and they also cover a widening subclass and a patched module constant.
- The adapter acceptance tests run the real guarded and loopback transports above the mock.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. tests/support/stub_http.py:195 — the service file is rewritten in place with `write_text`, and `_SERVICES_LOCK` only covers this process. A reader in another process (the T08-08 `kill_service:<name>` lookup) can see a truncated file. Write to a temp file and `os.replace` it.
2. tests/support/_stub_http_core.py:119 — `timeout` is a per-recv socket timeout, not a total deadline per request. A client that sends a byte every few seconds can hold a handler thread indefinitely. A stalled client is cut off (tested), and the threads are daemons, so this is acceptable for test infrastructure.
3. tests/support/_stub_http_core.py:124-129 — a request with no Content-Length (for example Transfer-Encoding: chunked) is treated as an empty body without any signal. Consider 411 for POST without Content-Length.
4. tests/unit/support/test_stub_http.py:128 — the 413 test sends only headers. A real client streaming more than 8 MB may see a connection reset instead of the 413, because the server closes with unread data. Worth a docstring note; there is no requirement gap.
5. tests/support/_fake_llm_wire.py:78 — the HTTP drivers hash the wire body, while FakeLLMClient hashes the canonical LLMRequest. The same prompt therefore reports different `prompt_hash` values in-process and over HTTP. The hash is only for diagnostics; document it.
6. tests/unit/support/test_fake_llm.py:150 — the registration test calls `reset_registry()` itself instead of checking in a later test that the autouse reset removed the entry. This is adequate because the autouse fixture calls the same function.
7. tests/support/fake_llm.py:72 — `ScriptRouter` inherits `install() -> MockNet`, so `respx_router(...).install(mp).requests` is typed as MockNet. Overriding `install` to return `Self` would fix the type.

### Assessment
**Task quality:** Approved
**Reasoning:** All four units and UT11-68 (U11-42 half), UT11-71..74 and the ST11-13 bind part meet the spec and the ledger rulings. The bind guard is shown to be what makes the ST11-13 test pass, tests are stable with no leaked threads, and all gates are green. The remaining items are Minor hardening only.
