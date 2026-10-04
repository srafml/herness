# T05-07 review (verify agent): worktree agent-a20259ea565db046e, base f43b1f9, head fb97263

### Spec Compliance
- ✅ U05-26 `OpenAICompatClient.astream`: create(stream=True, stream_options include_usage); raw TextDelta per non-empty delta.content; reasoning/reasoning_content buffered, not yielded; ToolCallDelta per fragment (id/name from first fragment, args concatenated); last finish_reason + usage kept; completion rebuilt and mapped through the same `_map_response` as acomplete; exactly one Done, last. UT05-29 asserts Done == acomplete on the same payload (latency zeroed) and request bodies identical except stream/stream_options.
- ✅ w14-s05 ruling (bounds while consuming): `_openai_stream.py:56-62` text/reasoning > 1,000,000 raise before the delta is appended/yielded; `:68-71` a new index >= 64 raises on first appearance; `:80-83` per-call args > 1,000,000 raise; index must be a non-bool non-negative int (`:66-67`); calls kept in a dict keyed by index, so no sparse growth. The 200,000 truncation happens only at Done via `_map_response`/`bound_response` (test `astream_truncates_at_done_only`).
- ✅ w14-s05 ruling (deadline): deadline = loop.time()+timeout_s at call start (`openai_compat.py:247`); `timeout_at` around create() (`:252`) and each `anext` in `next_chunk` (`_openai_stream.py:131-138`); no timeout context spans a yield. Expiry -> ModelUnavailable("openai call failed: APITimeoutError"), no cause (shared `_translated`, `openai_compat.py:192-204`).
- ✅ w14-s05 ruling (errors/closing): one `_translated` for acomplete and astream; EgressBlocked response_too_large/unsupported_encoding raw or wrapped -> OutputValidationError("response body exceeds limit"); OpenAIError -> translate_openai_error; mid-stream error -> no Done. `_sdk` (AsyncExitStack) closes the HTTP client even if SDK init fails; the stream is used in `async with`.
- ✅ U05-23 `estimate_tokens`: chars = system texts + canonical_json(parts) per message + canonical_json(tool spec); `(2*chars+6)//7` == ceil(chars/3.5) exactly (tests at the 3.5 boundaries).
- ✅ U05-23 `count_tokens`, vLLM path: root strips "/" and "/v1"; POST /tokenize {model, messages (system first, shared `chat_messages`), tools (omitted when empty, shared `tool_dicts`), add_generation_prompt: true}; sync `egress.loopback_http_client(root, timeout_s=5, bearer=<resolved or None>, max_response_bytes=1 MiB)` in `with` (closed); count int, not bool, >= 0.
- ✅ U05-23 Anthropic path: sync `anthropic.Anthropic(api_key, max_retries=0, timeout=10, http_client=get_guard().http_client("reasoning_final","aggregated_evidence"))` in an ExitStack (both closed); the U05-27 mapping is reused via a throwaway `AnthropicClient(cfg)._build_params`, and anthropic_client.py is not edited (the ruling permits a "_build_params-equivalent").
- ✅ Fallback ruling: `except Exception  # noqa: BLE001` citing the postcondition; WARNING `harness.llm.token_count_fallback` with exactly {client, error_type} (type name only; the test asserts the full event dict, so no message or secret leaks).
- ✅ Carry-overs: m1 `settings.py:150-151` le=3600 + spec ClientConfig signature row, §9 row, U05-25 note (LLMRequest stays gt=0), tests for 3600.5 and 0 rejected; m2 the acomplete byte-cap test now uses a streamed body without Content-Length, plus an astream byte-cap test; m4 five openai_compat UT05-35 tests relabelled to UT05-25 / ST05-20 (ST05-20 is the TH05-20 row, which fits redirect/gzip/wrapped-refusal); the Anthropic UT05-35 tests are untouched (no Anthropic test file in the diff).
- ✅ R-06 / ST05-13(a) / ST10-25: `grep -rnE "httpx2?\.(Async)?Client\(|(Async)?HTTPTransport\(|MockTransport\(" herness/harness` -> no hits; clients come only from herness.core.egress. The st10 lint tests pass.
- ✅ Sizes: openai_compat 269/380, _openai_stream 138/160 (new row), _openai_map 197/200 (new row), tokens 149/150, settings 343/360; check_module_size exit 0. The spec edits are accurate (module-map rows; the U05-26 complexity row paraphrases the rulings faithfully).
- ⚠️ Cannot verify from diff: branch-coverage figures (report: openai_compat/_openai_stream/tokens 100 %, _openai_map 99 %) were not re-measured; BT05-10 (p95 < 30 ms, gpu marker) is not in this card's Tests row.

Verification run (worktree .venv):
- `PYTHONUTF8=1 pytest tests/unit/harness tests/security/test_st10_lint.py -q -p no:logging` -> 1003 passed, 1 skipped (Windows symlink privilege, pre-existing).
- `ruff check .` -> clean; `ruff format --check herness tests` -> clean; `mypy` (configured scope herness, tools) -> 0 issues in 228 files.
- Focused probes (a scratchpad test, not committed): a mid-stream httpx2.ReadError / ReadTimeout / RemoteProtocolError each surface as ModelUnavailable (the SDK wraps them as APIConnectionError/APITimeoutError, which `_translated` translates); cancelling the consumer task while the generator awaits a chunk -> CancelledError propagates, and the HTTP client and SSE stream are both closed.

### Strengths
- Clean decomposition: `_translated` / `_sdk` / `_params` are shared by acomplete and astream, so Done-equals-acomplete and identical error translation hold by construction, not by duplication.
- Bounds are enforced in `StreamBuffers` before any append or yield; dict-keyed tool calls remove the crafted-index memory vector entirely.
- `next_chunk`'s up-front deadline check covers chunks the SDK has already buffered (no suspension, so timeout_at would never fire).
- Tests are behavioural and tight: exact event lists, closed-state asserts on client and stream, exact log event dicts, a real streamed SSE body for the byte caps.
- The mapping helpers `chat_messages`/`tool_dicts` are shared with tokens.py (no duplicated mapping).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. `herness/harness/llm/_openai_stream.py:69-71`: any first-seen index >= 64 is rejected even when there are fewer than 64 distinct calls (e.g. indices 0 and 70), with the message "response has more than 64 tool calls", which is inaccurate in that case; acomplete would accept those 2 calls. The ruling permits this ("index < a sane bound"), but the message could say "tool call index out of range", or the spec row could note the divergence.
2. `herness/harness/llm/tokens.py:113,115`: reaches into `AnthropicClient._build_params` and `._api_key` (private). Acceptable under the w14-s05 Anthropic ruling (a throwaway `AnthropicClient(cfg)._build_params`-equivalent, no edit to the 400/400 file), but the coupling is brittle; a public mapping helper should come with any future anthropic_client split. tokens.py is at 149/150, so any growth needs a budget change.
3. `herness/harness/llm/openai_compat.py:249-258`: the sync `_translated` context wraps the `yield event`, so an exception a consumer injects with `athrow(TimeoutError())` would be reported as the provider deadline. Only an unusual consumer can reach this; noted for awareness.
4. `herness/harness/llm/openai_compat.py:251,259`: astream's `latency_ms` includes the consumer's processing time between chunks (acomplete's does not). The spec does not say either way; worth a one-line docstring note, since latency feeds metrics.
5. `tests/unit/harness/test_llm_openai_compat.py:1356`: closing is tested only for a consumer `aclose()`; the ruling also names cancellation. My probe shows cancellation closes the stream and client correctly; a regression test for task cancellation would lock that in.
6. The spec test setups say "respx SSE stream" / "respx loopback /tokenize"; the tests use `httpx2.MockTransport` (respx cannot patch httpx2). This is disclosed in the report and equivalent in substance; the §11 rows could be reworded in a later spec pass.

### Assessment
**Task quality:** Approved
**Reasoning:** Both units match the spec and every w14-s05 ruling (bounds checked while consuming, a deadline per await that never spans a yield, shared error translation, closing on every exit including cancellation, the vLLM/Anthropic/fallback paths). Carry-overs m1/m2/m4 are done correctly, and tests, ruff, mypy, lints and size budgets are clean; the remaining items are polish.
