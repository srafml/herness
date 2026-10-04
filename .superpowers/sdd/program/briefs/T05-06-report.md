# T05-06 report: OpenAI-compatible adapter (build)

Worktree: D:\herness\.claude\worktrees\agent-a0dfd235925f64aa6 (branch worktree-agent-a0dfd235925f64aa6, base 9b794b5)
Status: DONE_WITH_CONCERNS (concerns: httpx2 vs guard client type; respx cannot intercept the SDK)
Final commit: cc13410 feat(harness): T05-06 OpenAI-compatible adapter (checkpoint 817f971 squashed into it via git reset --soft 9b794b5).

## What was built
- `herness/harness/llm/openai_compat.py` (336/380 lines): `OpenAICompatClient` registered as
  `register("llm_client", "openai_compat")`. Attributes `name`, `cfg`, `server`; key resolved once
  with `herness.core.secrets.resolve` into a `SecretStr` on the instance (`"EMPTY"` without a key).
  Private `_to_openai_messages`, `_build_params` (split into `_tool_params`, `_thinking_params`),
  `_map_response`, `_guarded_http_client`; `acomplete` (one SDK client per call inside `async with`,
  `max_retries=0`, `timeout=req.timeout_s`, `openai.OpenAIError` -> `translate_openai_error`),
  `complete` (RuntimeError inside a running loop, else `asyncio.run`).
- `herness/harness/llm/base.py` (148 -> 174/180): `MAX_RESPONSE_TEXT_CHARS = 1_000_000`,
  `MAX_RESPONSE_PART_CHARS = 200_000`, `MAX_TOOL_CALLS = 64`, `TRUNCATION_MARKER = "[truncated]"`,
  and `bound_response(text, n_tool_calls, *, client) -> str` (shared-ready for T05-07/T05-08):
  1M check first (OutputValidationError "response text exceeds limit"), then > 64 tool calls
  (OutputValidationError), then truncation to exactly 200,000 chars incl. marker + WARNING
  `harness.llm.response_truncated` (fields: client, length; never text).
- Golden requests: `tests/fixtures/harness/golden_requests/{vllm,ollama,llamacpp}.json` (116/114/111
  lines) - one full request (system, multi-turn history with raw and canonical tool-call arguments,
  tool results, dropped reasoning, 2 tools, schema, thinking on, seed, stop, temperature).
- Tests: `tests/unit/harness/test_llm_openai_compat.py` (673 lines, 43 tests, pytestmark unit),
  `tests/integration/harness/test_openai_compat_live.py` (88 lines, IT05-08, integration, skipped
  unless HERNESS_LLM_URL; HERNESS_LLM_MODEL / HERNESS_LLM_SERVER optional).

## Tests
- IDs: UT05-23 (4), UT05-24 (8 incl. construction preconditions + registration), UT05-25 (18 incl.
  mapping, bounds, acomplete round trip, off-network guard), UT05-26 (6), UT05-27 (1), UT05-28 (5,
  translated errors through acomplete, no retry, TH05-15 key never in exception/context/logs),
  UT05-30 (1), IT05-08 (1, skipped).
- RED: with openai_compat.py absent: `ImportError: cannot import name 'openai_compat' from
  'herness.harness.llm'` (collection error). (The module was drafted before the test file; RED was
  demonstrated afterwards by moving the module aside.)
- GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/harness tests/integration/harness/test_openai_compat_live.py -q -p no:logging`
  -> 758 passed, 2 skipped. `--require-test-ids` on the card files: 43 passed, 1 skipped.
- Coverage (card tests + test_llm_base): openai_compat.py 100% line, 53/54 branches (99%; the
  missing branch is a non-ToolResultPart in a tool message, impossible by the Message validator);
  base.py 100%/100%.
- Gates: ruff format/check clean, mypy --strict clean (herness/harness/llm), lint-imports 13 kept,
  check_module_size exit 0, type-ownership pass, all pre-commit hooks passed incl. pytest-unit.

## Rulings applied
1. Adapter boundary: truncate > 200,000 to 200,000 total incl. `[truncated]`, WARNING event;
   > 64 tool calls -> OutputValidationError; 1M check first. Boundaries tested exactly (200,000
   verbatim / 200,001 cut; 64 ok / 65 raises; 1,000,001 raises). Constants + helper in base.py.
2. Off-network: `get_guard().async_http_client(egress_purpose_for(model_role), "aggregated_evidence",
   run_id=, task_id=)` via local Protocol `_GuardWithHttpClient`; `hasattr` check -> ConfigError
   ("... egress guard has no HTTP client"), nothing sent. Fake guard tests: purpose/payload/run/task
   passed, request goes through the guard client, EgressBlocked raised in the guard transport
   surfaces as EgressBlocked (find_egress_block through the SDK's APIConnectionError).
3. respx: NOT usable - openai 3.19.2 sends through `httpx2`/`httpcore2`, which respx does not patch
   (probe: APIConnectionError with respx.mock active). Tests replace
   `httpx2.AsyncHTTPTransport.handle_async_request` with a recording fake (`_FakeServer` in the
   test file); the real SDK still builds requests, parses responses and raises its status errors.
4. On-network calls pass `http_client=None` (the SDK default = omitted); `max_retries=0` (tested:
   one request per failing call).
5. Construction check uses `get_config().security.egress.enabled` and names `config.profile`.
6. Log fields: model_mismatch (client, expected, actual[:128]) and response_truncated (client,
   length); no prompt/response text or key anywhere.
7. Test naming per brief; UT05-30 is the running-loop test.

## Deviations / interpretations (spec notes)
- `tool_choice` and `parallel_tool_calls` are sent only when tools are sent (the spec ties
  parallel_tool_calls to tools; tool_choice without tools is rejected by OpenAI/vLLM). Ollama: "auto"
  sent, key omitted for "none".
- Assistant text parts are joined with "\n\n" (spec says "joined text or None" without a separator).
- `ToolResultPart.is_error` is not sent (OpenAI tool messages have no such field).
- Extra hardening beyond spec, all OutputValidationError: no choices; a non-function tool call; a
  tool call whose id/name fails ToolCall validation; JSON nested too deep (RecursionError) treated
  as not-an-object. Tool ids in messages are cut to 128 chars.
- `finish_reason` null (SDK types it non-null) handled via cast -> "other", raw "".
- Client key in translated errors (U05-30 postcondition): NOT added; messages stay
  "openai call failed: <Type>[ HTTP <status>]" from errors.py. ConfigErrors raised by the adapter
  carry `client=<name>` context.
- `StreamCapable` is not implemented yet (astream is T05-07).

## Carry-overs
- T10-17: when `EgressGuard.async_http_client` lands, drop the hasattr fallback and type against the
  real API. CONCERN: openai 3.19.2 requires `http_client: httpx2.AsyncClient`; the T10-17 guarded
  transports seen in progress use `httpx` (httpx.BaseTransport). A guard client built on `httpx`
  will not be accepted/usable by the SDK; T10-17 must return an httpx2 client (or the adapter needs
  an adapter layer). The Protocol here is typed `-> httpx2.AsyncClient`.
- T11-23: `respx_router` cannot fake this SDK (httpx2); impl 11 needs an httpx2 transport fake. The
  `_FakeServer` pattern here can move to tests/support/fake_llm.py.
- T05-07: 44 lines of budget left in openai_compat.py for astream; use `base.bound_response`.
- T05-08: reuse `bound_response` and the constants from base.py.
- VI-2/VI-3 defaults used (response_format json_schema; extra_body.think; chat_template_kwargs).

## Files
- herness/harness/llm/openai_compat.py (new, 336)
- herness/harness/llm/base.py (changed, 174)
- tests/unit/harness/test_llm_openai_compat.py (new, 673)
- tests/integration/harness/test_openai_compat_live.py (new, 88)
- tests/fixtures/harness/golden_requests/vllm.json, ollama.json, llamacpp.json (new)

## Fix round 1 (review T05-06-review.md, Needs fixes)
Checkpoint a8bb300 wip(T05-06): review round 1 fixes and tests; final commit 38ad66c fix(harness): T05-06 review round 1 (bounds, byte cap, deadline) (no squash/reset this round).

Module layout (module-map spec note): mapping helpers moved to the private sibling
`herness/harness/llm/_openai_map.py` (L4, 184/200 lines): `message_dicts`, `_json_object`,
`_tool_calls`, `_reasoning`, `_usage`, `map_response`. `openai_compat.py` now 273/380 (about 107
lines left for T05-07 astream); `base.py` 174/180. Spec §2 module map needs a row:
`herness/harness/llm/_openai_map.py | OpenAI-compatible wire mapping (private) | - | L4 | none | 200`.

- I1 fixed: `_map_response` wraps `_openai_map.map_response`; TypeError/AttributeError/ValueError
  (incl. pydantic ValidationError) -> `OutputValidationError("malformed response", client=)` from
  None (no response data). Non-str `raw.model` -> malformed. Parametrized UT05-26 over 8 shapes
  (model missing/int, usage without prompt_tokens, arguments null, tool-call id null, message null,
  list content, int id), both via `_map_response` and via `acomplete` (fake server).
- I2 fixed: on-network calls use a byte-capped `httpx2.AsyncClient` (`_CappedTransport` wrapping
  `httpx2.AsyncHTTPTransport`, `_CappedStream` counting bytes while read) against
  `herness.core.egress.MAX_RESPONSE_BYTES` (52,428,800). Over-cap Content-Length and any
  non-identity Content-Encoding (the client sends `Accept-Encoding: identity`, so a compressed body
  cannot be bounded) are refused before any byte is read. The over-cap case surfaces as
  `OutputValidationError("response body exceeds limit")` (the SDK propagates the stream error
  unwrapped; verified). The client is closed (tested with `is_closed`). Off-network keeps the guard
  client (its cap is T10-17's). Tests: cap bytes pass / cap-1 limit raises (monkeypatched cap; caught by the Content-Length pre-check), a chunked body without Content-Length stops at the chunk passing the cap (3 of 10 chunks read), and
  Content-Length cap+1 and gzip refused with zero body reads. Carry-over: R-06 says loopback model
  clients come from `egress.loopback_http_client` (T10-17); move the capped client there when it lands.
- I3 fixed: reasoning goes through `bound_response(..., field="reasoning")` (1M raise, >200k cut +
  WARNING with `field`). `bound_response` gained `field: str = "text"` (logged). Boundary tests.
- I4 fixed: `len(arguments) > MAX_RESPONSE_TEXT_CHARS` -> `OutputValidationError("tool call <id>
  arguments exceed limit")` before `json.loads`; exactly 1,000,000 chars decode (tested both).
- I5 fixed: `create` runs inside `asyncio.timeout(req.timeout_s)`; expiry -> `ModelUnavailable(
  "openai call failed: APITimeoutError", client=)` from None, i.e. the same class and message
  `translate_openai_error` gives `openai.APITimeoutError`. Test: slow fake transport, timeout_s=0.05.
- M1 fixed: exactly 1,000,000 chars -> truncated to 200,000; 1,000,001 chars + 65 tool calls ->
  "response text exceeds limit".
- M3 fixed: guard Protocol returns `httpx.AsyncClient | httpx2.AsyncClient` (type alias with
  `# noqa: TID251`, type-only import); passed to the SDK via cast. Test: a guard returning a legacy
  `httpx.AsyncClient` with MockTransport carries the call end to end.
- M4 fixed: HTTP client created inside an `AsyncExitStack` with `aclose` pushed before
  `AsyncOpenAI(...)`; test: AsyncOpenAI raising -> client closed.
- M2 (httpx2 undeclared; banned-api covers httpx.* only) not in scope: carry-over impl 10/00.
  `openai_compat.py` now imports httpx2 at runtime (capped transport).
- M5 interpretations recorded above; M6 noted.

Tests: card file 70 passed; `tests/unit/harness` + IT05-08 with `--require-test-ids`: 785 passed,
2 skipped. Coverage: openai_compat.py 100% line and branch,
_openai_map.py 100% line, 23/24 branches, base.py 100%. ruff, mypy --strict, lint-imports (13 kept),
check_module_size exit 0.
