# T11-23 report — Fake LLM drivers and stub HTTP base

Status: DONE_WITH_CONCERNS (spec notes below; two carry-over fakes intentionally left)
Branch: worktree-agent-adfd9b4bae80f02f2 (base a46205f). Checkpoint: efcf94c wip(T11-23). Final: see bottom.

## Files (lines vs budget)
- tests/support/fake_llm.py — 188 / 400 (FakeLLMClient U11-42, fixture fake_llm_registered, ScriptRouter + respx_router U11-43, FakeLLMServer U11-45)
- tests/support/_fake_llm_wire.py — 242 / 250 NEW private sibling (spec note, §2 row added): DD11-02 header keying, tool results rebuilt from the body, OpenAI/Anthropic JSON, SSE chunks
- tests/support/stub_http.py — 204 / 250 (StubHTTPServer U11-44, StubFault, fixture stub_services, service-file merge under a lock)
- tests/support/_stub_http_core.py — 170 / 200 NEW private sibling (spec note, §2 row added): 127.0.0.1-only LoopbackServer + bounded Handler
- tests/conftest.py — pytest_plugins += tests.support.stub_http, tests.support.fake_llm (once each)
- tests/fixtures/llm_scripts/t11_23_two_turn.yaml — 2-turn script (R-65 location)
- tests/unit/support/test_fake_llm.py (UT11-68 U11-42 half, UT11-71, UT11-73, UT11-74)
- tests/unit/support/test_stub_http.py (UT11-72)
- tests/security/test_st11_stub_bind.py (ST11-13 bind part)
- docs/impl/11-testing-eval-synthetic-data.impl.md §2: fake_llm row (deps httpx2, respx_router note, ScriptRouter/fixture exports) + two private-sibling rows
- Carry-over comments only: tests/unit/eval/_judge_fixtures.py, tests/unit/enrich/_fake_llm.py, tests/unit/enrich/test_llm_decider.py (docstring)
check_module_size: exit 0.

## Tests covered
UT11-68 (U11-42 half: path load, given book + resolver, fake_llm_registered registers llm_client:fake and reset removes it),
UT11-71 (OpenAI + Anthropic shapes over a 2-turn script; mismatch 500 {"error":{"message":"no script","prompt_hash":16hex}}; header keying; http_500/429+Retry-After:1; malformed_json both wires tool/text; hang→httpx2.ReadTimeout; disconnect→httpx2.RemoteProtocolError; unrouted host→ConnectError;
 ACCEPTANCE: OpenAICompatClient (real loopback client) and AnthropicClient (real EgressGuard from egress_harness.make_guard, hybrid profile) each drive a scripted tool→text loop end-to-end through respx_router; plus 429→RateLimited(1.0) via the adapter),
UT11-72 (127.0.0.1 ephemeral bind; kill_after(2)→third connect refused; POST /__control/kill→204 then refused; kill drops idle open connections; service file merge incl. pre-existing name and 8 concurrent writers; >8 MB→413 unread; stalled client cut by per-connection timeout; lifecycle; StubFault),
UT11-73 (non-stream tool turn, arguments JSON text, GET /v1/models; hang_s then 500; disconnect closes with no response),
UT11-74 (40-char text: 3 content chunks, finish, usage chunk, data: [DONE]; and OpenAICompatClient.astream consumes it),
ST11-13 bind part (base stub + FakeLLMServer bind 127.0.0.1 and pass the impl 10 SocketPolicy; non-loopback hosts incl. 0.0.0.0, "", ::, localhost refused with zero socket creations and zero binds via subclass bind_host or patched constant; server_bind re-checks).

## Spec notes / deviations
1. respx_router returns ScriptRouter (subclass of tests/support/egress_mock.MockNet, httpx2.MockTransport) not respx.MockRouter (ledger ruling): install(monkeypatch) or `with router:` swaps httpx2.HTTPTransport/AsyncHTTPTransport; .transport() for a test's own client; .requests records. Module map deps row now reads httpx2.
2. FakeLLMClient registered as ("llm_client","fake") (RegistryKind has no "llm").
3. Private siblings _fake_llm_wire.py and _stub_http_core.py (budgets 250/200) recorded as §2 rows, repo precedent (T03-20/T07-04 style).
4. Without DD11-02 headers every HTTP call keys as role/model_role/dedup "*" (spec default); adapters do not send the headers yet (DD11-02 open), so adapter tests use match-all scripts.
5. Anthropic malformed_json tool turn returns tool_use.input as the JSON text '{"a": ' (Anthropic has no argument-text field). FakeLLMServer hang: pause(hang_s) (cut short by stop/kill) then 500. Usage: prompt/input tokens = max(1, body bytes // 4); output tokens = estimate_tokens of the text.
6. kill_after(n) counts completed requests since construction (absolute n); the kill-triggering request closes the listener BEFORE its response is written so the next connect is deterministically refused.
7. Bind guard compares against the literal "127.0.0.1" in LoopbackServer.__init__ (before the socket exists) and in server_bind; StubHTTPServer.bind_host is a ClassVar so a widening subclass fails closed. The socket guard (herness.core.egress_socket, loopback allowed) is untouched; no global test socket guard exists in conftest.
8. StubFault (U11-46 signature) is defined in stub_http.py for T11-24.

## Carry-over swaps
- tests/unit/eval/_judge_fixtures.py FakeJudgeClient: LEFT — tests assert on recorded requests (client.requests / calls), raise arbitrary exceptions (EgressBlocked, OutputValidationError), rely on last-reply-repeats and on the response model name "judge-model-a" (JudgeScore.model); FakeLLMClient reports "scripted" and only ScriptBook fault kinds. Comment updated.
- tests/unit/enrich/_fake_llm.py (T03-15): LEFT — seed-keyed callable replies, request recording, as_text mode, arbitrary exceptions. Comment updated.
- T03-13 JevHostedDecider tests: HTTP-level via MockNet, not LLMClient shape — nothing to swap. No T03-24 LLM fake found.
- tests/unit/harness/test_llm_registry.py _FakeClient: stays (per ruling).

## Commands and results
- uv run pytest tests/unit/support tests/unit/eval tests/unit/enrich tests/security/test_st11_stub_bind.py tests/security/test_st11_judge.py tests/unit/harness/test_llm_openai_compat.py tests/unit/harness/test_llm_anthropic.py -q -p no:logging --require-test-ids → 1053 passed, 1 skipped
- card tests (test_fake_llm, test_stub_http, test_st11_stub_bind) → 36 passed, 5 consecutive runs stable (~8.4 s; the Windows refused-connect adds ~2 s per refused check)
- ruff check . / ruff format --check . → clean; lint-imports 13 kept; mypy --strict on the 4 support modules (as -m tests.support.*) → no issues; check_module_size exit 0
- checkpoint commit hooks (incl. pytest-unit) → all passed
Final commit: ce2ef68 feat(T11-23): fake LLM drivers and stub HTTP base (hooks incl. pytest-unit passed).

## Fix round 1
- m1: `_register_service` now writes the merged map to `.<name>.<pid>.tmp` in the same directory and `os.replace`s it over the service file (still under the process lock, merge behaviour kept), so a reader in another process never sees a partial file. UT11-72 service-file test also asserts no `.tmp` file is left behind.
- m7: `ScriptRouter.install(monkeypatch) -> Self` overrides `MockNet.install`, so `respx_router(...).install(mp).requests` is typed correctly (MockNet itself unchanged).
- Sizes: fake_llm.py 193/400, stub_http.py 206/250; check_module_size exit 0; ruff check/format clean; mypy --strict on the 4 support modules clean.
- Card tests (test_fake_llm, test_stub_http, test_st11_stub_bind, --require-test-ids): 36 passed.- Commit: 4ad70dd fix(T11-23): atomic stub service file, Self-typed router install (hooks incl. pytest-unit passed).
