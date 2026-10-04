# T05-07 report: OpenAI streaming and token counting (U05-26, U05-23)

Worktree: D:\herness\.claude\worktrees\agent-a20259ea565db046e (branch worktree-agent-a20259ea565db046e, base f43b1f9)
Final commit: fb97263 feat(harness): OpenAI streaming and token counting (T05-07)

## What was built

- `OpenAICompatClient.astream` (U05-26), `herness/harness/llm/openai_compat.py`:
  `create(**params, stream=True, stream_options={"include_usage": True})`; per chunk, raw
  `TextDelta` for non-empty `delta.content`, reasoning (`delta.reasoning` or
  `delta.reasoning_content`) buffered but not yielded, `ToolCallDelta(id, name, fragment)` per
  `delta.tool_calls[j]` (id/name from the first fragment of an index, arguments concatenated),
  last non-null `finish_reason` and `chunk.usage` remembered; at the end the completion is rebuilt
  and mapped through the same `_map_response` as `acomplete`, then exactly one `Done`.
  - Bounds enforced while consuming (ruling): text or reasoning buffer > 1,000,000 ->
    `OutputValidationError("response text exceeds limit")` before the delta is yielded; a new
    tool-call index >= 64 (the 65th call) -> `"response has more than 64 tool calls"`; one call's
    arguments > 1,000,000 -> `"tool call <id> arguments exceed limit"`; index not a non-negative
    int / first fragment without str id+name / non-str content or arguments / null delta ->
    `"malformed response"` (no data). Tool calls are kept in a dict keyed by index (no sparse
    list growth). The 200,000 truncation + marker + WARNING happen only at `Done` (via
    `_map_response` / `bound_response`), so deltas are the raw provider text.
  - Deadline (ruling): `deadline = loop.time() + req.timeout_s` at call start;
    `asyncio.timeout_at(deadline)` around `create()` and every chunk read, never across a yield;
    `next_chunk` also raises at once when the deadline has already passed (a chunk the SDK has
    already buffered completes without suspending, so consumer time would otherwise not count).
    Expiry -> `ModelUnavailable("openai call failed: APITimeoutError")`, no cause.
  - Errors (ruling): `acomplete` and `astream` share one `_translated()` context manager
    (TimeoutError -> deadline; EgressBlocked body refusal raw or wrapped ->
    `OutputValidationError("response body exceeds limit")`; `openai.OpenAIError` ->
    `translate_openai_error`). SDK client + HTTP client come from one `_sdk()` async context
    manager (AsyncExitStack; HTTP client closed even if SDK init fails); the SDK stream is used in
    `async with`, so a consumer `aclose()` closes the stream, the SDK client and the HTTP client.
  - `acomplete` refactored onto `_params` / `_translated` / `_sdk`, behaviour unchanged (all
    existing tests green).
- `herness/harness/llm/_openai_stream.py` (new private sibling, module-map row added):
  `StreamBuffers` (bounded accumulation, `completion()` rebuild) and `next_chunk`.
- `herness/harness/llm/_openai_map.py`: new `chat_messages(system, messages)` and
  `tool_dicts(tools)` shared by the adapter and `tokens.py` (no duplicated mapping); two helper
  bodies compacted to stay within 200; stale docstring text about the byte-capped client fixed.
- `herness/harness/llm/tokens.py` (U05-23):
  - `estimate_tokens(messages, tools=(), system=())` = ceil(chars / 3.5) computed as
    `(2 * chars + 6) // 7` (exact, no float drift); chars = system texts + canonical JSON of each
    message's parts list (`model_dump(mode="json")`) + canonical JSON of each tool spec.
  - `count_tokens(cfg, messages, tools, system)`: `estimate` -> `(estimate, False)` with no I/O;
    `vllm_endpoint` -> sync `egress.loopback_http_client(root, timeout_s=5, bearer=<resolved
    api_key or None>, max_response_bytes=1 MiB)`, closed after, `POST /tokenize` with
    `{"model", "messages" (system first, U05-24 mapping), "tools" (omitted when empty),
    "add_generation_prompt": true}`; root = base_url minus trailing "/" and "/v1"; `count` must be
    int, not bool, >= 0. `anthropic` -> `AnthropicClient(cfg)` (kind/egress/key checks, key
    resolved once), its `_build_params` on a throwaway `LLMRequest` (thinking off), keys
    `system` / `messages` / `tools` only, sync `anthropic.Anthropic(api_key, max_retries=0,
    timeout=10, http_client=egress.get_guard().http_client("reasoning_final",
    "aggregated_evidence"))` in an ExitStack (HTTP client closed even if SDK init fails) ->
    `input_tokens` (validated like `count`).
  - Fallback: `except Exception` (`# noqa: BLE001`, U05-23 postcondition) -> WARNING
    `harness.llm.token_count_fallback` with `client` and `error_type` (type name only; an
    SDK-wrapped EgressBlocked is reported as `EgressBlocked` via `find_egress_block`).
  - anthropic_client.py NOT edited (400/400): reuse through `AnthropicClient._build_params` and
    `_api_key` (private attribute access, commented). No NEEDS_CONTEXT needed.
- Carry-overs: m1 `ClientConfig.timeout_s` gets `le=3600`; m2 `test_ut05_25_byte_cap_while_reading`
  now uses a real streamed body with no Content-Length (plus the astream byte-cap test); m4 the
  five openai_compat tests labelled UT05-35 relabelled (loopback client use and non-body
  EgressBlocked -> UT05-25; redirect, gzip cap, wrapped body refusal -> ST05-20). The Anthropic
  UT05-35 tests are untouched.

## Sizes vs budget

| File | Lines | Budget |
|------|-------|--------|
| herness/harness/llm/openai_compat.py | 269 | 380 |
| herness/harness/llm/_openai_stream.py (new) | 138 | 160 (new module-map row) |
| herness/harness/llm/_openai_map.py | 197 | 200 (new module-map row) |
| herness/harness/llm/tokens.py (new) | 149 | 150 |
| herness/harness/llm/settings.py | 343 | 360 |

`uv run python -m tools.check_module_size` exit 0.

## Spec edits (docs/impl/05-harness-core.impl.md)

- §2 module map: rows for `_openai_map.py` (200; it had no row although its docstring cited a
  module-map note) and `_openai_stream.py` (160).
- ClientConfig signature row: `timeout_s: float = 300 (> 0, ≤ 3600)`.
- §9 row `models.clients.<key>.timeout_s`: range `> 0, ≤ 3600 (egress MAX_TIMEOUT_S; T05-07 m1)`.
- U05-25 Complexity note: `LLMRequest.timeout_s` stays `> 0`; a hand-built on-network request
  above 3600 s fails closed with ConfigError from the loopback client.
- U05-26 Complexity row: the w14-s05 streaming rulings (bounds while consuming, truncation only at
  Done, deadline semantics, error translation and closing).

## Tests (by ID)

- UT05-29 (tests/unit/harness/test_llm_openai_compat.py): deltas in order + one Done last equal to
  acomplete on the same payload (stream/stream_options sent, rest of the body identical);
  `delta.reasoning` + stream without [DONE]; truncation only at Done; text and reasoning caps
  while consuming (no later delta, no Done, client and stream closed); 64/65 tool-call indices;
  per-call arguments cap; 8 malformed chunk shapes; empty stream; SSE error event mid-stream ->
  ModelUnavailable; HTTP 401 at create -> AuthError; transport error mid-stream ->
  ModelUnavailable; wrong client -> ConfigError; per-chunk deadline; consumer time counted
  without cancelling the consumer; consumer aclose closes stream and HTTP client; byte cap while
  streaming (m2); StreamCapable.
- UT05-25 / ST05-20: relabelled m4 tests; UT05-25 byte cap now on a streamed body (m2).
- UT05-22 (tests/unit/harness/test_llm_tokens.py): estimate invariant; exact rounding at the 3.5
  boundaries; defaults/purity; estimate tokenizer makes no call; vLLM exact count (URL, bearer,
  identity encoding, body shape, 5 s, 1 MiB, client closed); root handling x3 + no tools; 10
  fallback cases (500, missing/bool/negative/text/float count, non-object, non-JSON, connect
  error, body > 1 MiB) each with exactly one WARNING {client, error_type}; non-loopback root
  (TH05-13) -> fallback with no request sent; unresolvable secret -> fallback; Anthropic exact
  count via the guard client (purpose/payload, x-api-key, mapping, client closed); minimal
  request omits system/tools; Anthropic 500 / bad count / EgressBlocked -> fallback;
  egress-disabled profile -> ConfigError fallback without a guard call.
- UT05-17 (settings): client timeout_s 3600.5 and 0 rejected.

RED: stream tests -> 24 failed (no `astream`); `test_llm_tokens.py` -> collection error (no
`herness.harness.llm.tokens`).
GREEN: openai_compat 101 passed; tokens 31 passed; settings + models_yaml 55 passed;
`PYTHONUTF8=1 uv run pytest tests/unit/harness tests/security/test_st10_lint.py -q -p no:logging`
-> 1003 passed, 1 skipped (Windows symlink privilege). Branch coverage: openai_compat 100%,
_openai_stream 100%, tokens 100%, settings 100%, _openai_map 99% (one partial branch). mypy
(all files) clean, ruff clean, lint-imports 13 kept, check_module_size 0, check_type_ownership 0;
pre-commit hooks (incl. pytest -m unit) passed on every commit, no skips.

## Deviations / notes

- The SDK coerces a tool-call index of `"0"` or `true` to an int (lax construct), so those are
  accepted exactly as acomplete would; the malformed-index tests use null, 0.5 and a word.
- `next_chunk` raises TimeoutError up front once the deadline has passed (not only through
  `timeout_at`); see Deadline above.
- The fallback log names a wrapped EgressBlocked (find_egress_block) rather than the SDK's
  APIConnectionError wrapper; still the type name only.
- Tests use `httpx2.MockTransport` (respx cannot patch httpx2) with real streamed SSE bodies; the
  spec's "respx SSE stream" / "respx loopback /tokenize" setups are met in substance.
- `.secrets.baseline`: only line-number refreshes (docs/impl/05 and test_llm_settings.py), LF; no
  audited entry dropped. New test secret strings carry `pragma: allowlist secret`.
- Checkpoint commits were squashed into one card commit on the base (local branch only).

## Concerns

- tokens.py is at 149/150 lines; later growth needs a budget change or a helper move.
- tokens.py reaches into `AnthropicClient._build_params` / `_api_key` (private) to avoid editing
  anthropic_client.py (400/400) or duplicating the U05-27 mapping; if the Anthropic adapter is
  split later, a public mapping helper would be cleaner.
