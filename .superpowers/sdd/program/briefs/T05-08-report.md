# T05-08 report: Anthropic adapter

Status: DONE_WITH_CONCERNS (cross-card concern on httpx vs httpx2, see Concerns)
Worktree: D:\herness\.claude\worktrees\agent-a01f9cd030a2ec891 (base 9b794b5)
Commits: 3eb6947 wip (adapter + unit tests green), f063bd8 wip (compaction + IT05-09), e106f4c `feat(harness): Anthropic adapter (T05-08)` (adapter-level section 8 log events + tests).

## What was built
- `herness/harness/llm/anthropic_client.py` (348 lines; budget 400; ~52 lines of room left for T05-09):
  `AnthropicClient` registered via `@register("llm_client", "anthropic")`; implements `LLMClient` and
  `StreamCapable` (runtime_checkable isinstance asserted in tests). BatchCapable methods left to T05-09.
  - `__init__`: kind check; `get_config().security.egress.enabled` else
    `ConfigError("anthropic client <name> cannot be constructed in profile <p>: egress disabled")`;
    `api_key` ref required; key resolved once via `herness.core.secrets.resolve` -> `SecretStr`.
  - `_to_anthropic_messages`, `_build_params`, `_map_message`, `_http_client` per U05-27.
  - `acomplete` (create; `messages.stream` + `get_final_message` when max_output_tokens > 16,000),
    `complete` (U05-25 rule: RuntimeError inside a running loop, else asyncio.run), `astream`
    (TextDelta, ToolCallDelta(id, name, partial_json), thinking not yielded, one Done last).
  - Every SDK client: `AsyncAnthropic(api_key=<secret>, max_retries=0, timeout=req.timeout_s,
    http_client=self._http_client(req))`.
  - Errors: SDK exceptions and EgressBlocked go through `_fail`: translate_anthropic_error (U05-30);
    an EgressBlocked is re-raised as the guard's own object (`from None`, avoids a cause cycle) with
    WARNING `harness.llm.egress_blocked` (client, task_id); 400/404/422 -> ConfigError with ERROR
    `harness.llm.bad_request` (client, status) (section 8 rows). No body, prompt or key in messages or logs.
- `tests/unit/harness/test_llm_anthropic.py` (32 tests), `tests/integration/harness/test_llm_anthropic_it.py` (IT05-09).

## Tests per ID (all green; card filter: 52 passed, 1 skipped = IT05-09 without env var)
- UT05-31: Opus golden (no thinking, effort, no temperature, top-level cache_control); Sonnet adaptive/disabled (auto omitted); Haiku enabled budget (max(1024, max/4), explicit value clamped to max-1, ConfigError when impossible, off omitted); output_config.format, temperature, stop_sequences.
- UT05-32: cache_control on the cached system block only; tools sorted with strict flag; tool_choice auto/none, omitted without tools; message mapping (opaque anthropic reasoning replayed, other reasoning dropped, empty text omitted, tool_use, tool_result, consecutive user turns merged with tool results first).
- UT05-33: thinking/redacted_thinking opaque kept unchanged; refusal category; unknown stop reason -> other; cache usage fields; cost and batch halving; parsed from first text block; non-object tool input and invalid tool name -> OutputValidationError. Ruling tests: text > 1,000,000 -> OutputValidationError (value 1_000_000 pinned); 200,000 < text <= 1,000,000 -> truncated to exactly 200,000 chars ending `[truncated]` + WARNING `harness.llm.response_truncated` (length, kept only; no text); > 64 tool calls -> OutputValidationError (64 accepted).
- UT05-34: max_output_tokens=20000 -> SSE stream request (`stream: true`); 16000 -> create (request id from header kept).
- UT05-35: translation-table tests already exist in tests/unit/harness/test_llm_errors.py (test_ut05_35_*, including OverloadedError 529); added adapter-level 529 through acomplete (ModelUnavailable, one attempt, no body or key in the message, cause kept) and 400 -> ConfigError + bad_request log.
- UT05-36: local profile -> ConfigError (exact message); wrong kind / missing api_key; key resolved once as SecretStr; registry.get("llm_client", "anthropic") after module reload.
- UT05-37: stub guard (monkeypatched herness.core.egress.get_guard) -> the SDK sent through its httpx2.MockTransport; guard args (reasoning_final, aggregated_evidence, run_1, task_1); x-api-key only in the header, not the body; guard without async_http_client -> EgressBlocked(reason="guard_client_unavailable").
- UT05-38: fake SSE stream -> deltas in order, thinking not yielded, one Done last (opaque signature kept, usage from message_delta); error before any event -> ModelUnavailable, no Done; complete() works synchronously and raises RuntimeError inside a running loop.
- IT05-09: real Haiku request with a tool and a schema; cost > 0 and <= $0.50; skipped unless HERNESS_ANTHROPIC_IT=1 (also needs the T10-17 guard client and the secret).
- ST05-13: (a) AST lint over herness/harness: no Anthropic/AsyncAnthropic call without http_client=, no httpx/httpx2 Client/AsyncClient construction (plus a self-test that the lint flags violations); (b) guard raising EgressBlocked inside the transport -> the same EgressBlocked object surfaces from acomplete and astream, exactly one request each (zero retries), WARNING egress_blocked logged; (c) local profile with socket.connect patched -> ConfigError, zero connects.

## Gates
- ruff format/check clean; mypy (repo strict config) 0 issues; lint-imports 13 kept; check_module_size exit 0; check_type_ownership exit 0.
- pytest tests/unit/harness: 747 passed, 1 skipped (pre-existing symlink skip).
- Coverage of anthropic_client.py: 99% (213 statements, 0 missed; 74 branches, 1 partial: the impossible fall-through of the assistant-part if/elif chain).
- RED: with the module absent, `pytest tests/unit/harness/test_llm_anthropic.py` -> ImportError "cannot import name 'anthropic_client'" (collection error). GREEN: 32 passed.
- Pre-commit hooks ran on every commit (no --no-verify, no PRE_COMMIT_ALLOW_NO_CONFIG). No full-suite run (controller ruling).

## Rulings applied
- Adapter boundary limits (1,000,000 raise / 200,000 truncate + marker + WARNING / 64 tool calls), all three tested.
- Private `_MAX_RESPONSE_TEXT_CHARS: Final = 1_000_000` with `# carry-over: use base.MAX_RESPONSE_TEXT_CHARS once T05-06 lands`; base.py untouched; a unit test pins the value.
- `_http_client` calls the guard through the local Protocol `_GuardedClientFactory`; a guard without the method fails closed with EgressBlocked (chosen over ConfigError so spec 08 falls back to a local entry, as for any guard refusal). herness/harness never builds an httpx client.
- U05-25 complete rule implemented locally (`_run_sync`), since base.py has no helper.

## Deviations (for review)
1. temperature: anthropic SDK 1.8.0 `messages.create/stream` have no `temperature` keyword; it is sent as `extra_body={"temperature": t}` (only when not None). UT05-31 asserts it.
2. tools and tool_choice are sent only when req.tools is non-empty (the API rejects tool_choice without tools); the spec lists them unconditionally.
3. thinking == "auto" reaching the adapter (normally resolved upstream by resolve_request_params): adaptive_optional and budget omit the key.
4. output_config.effort is sent only when supports.effort and req.effort is not None.
5. ToolCall construction failures (name outside the ToolCall pattern, id > 128) -> OutputValidationError("tool call id or name is invalid") rather than a raw pydantic ValidationError.
6. The adapter emits the section 8 events harness.llm.egress_blocked (WARNING) and harness.llm.bad_request (ERROR); harness.llm.config_invalid (key_path) is left to the registry (U05-31), per section 6 "registry construction".

## Carry-overs
- T10-17: `EgressGuard.async_http_client` is not on the base; the adapter uses a local Protocol and fails closed (EgressBlocked, reason guard_client_unavailable) until it lands. IT05-09 cannot pass before T10-17.
- T05-06: switch `_MAX_RESPONSE_TEXT_CHARS` to `base.MAX_RESPONSE_TEXT_CHARS`.
- T05-09: submit_batch/collect_batch go into this file; 348/400 lines used. `_map_message(raw, None, latency, batch=True)` is already supported.

## Concerns
- httpx vs httpx2 (cross-card, affects T10-17): the locked anthropic 1.8.0 (and openai 3.19.2) SDKs depend on `httpx2`, not `httpx`, and reject any `http_client` that is not an `httpx2.AsyncClient` (TypeError in `_base_client`). U10-53 specifies `-> httpx.AsyncClient`. The adapter's Protocol therefore types the return as `httpx2.AsyncClient`; T10-17 must return httpx2 clients (GuardedTransport on httpx2.AsyncBaseTransport) or the adapters cannot use them. respx 0.23.1 patches httpx only, so the tests use httpx2.MockTransport through the stub guard. The ST05-13(a) lint covers both httpx and httpx2.
- Line budget: 348/400 leaves ~52 lines for T05-09 (aim was ~300); if T05-09 needs more, a budget ruling or a private sibling module will be needed.

## Line counts
- herness/harness/llm/anthropic_client.py: 348 (budget 400)
- tests/unit/harness/test_llm_anthropic.py: ~790
- tests/integration/harness/test_llm_anthropic_it.py: 88

## Fix round 1 (review T05-08-review.md)
1. (Important) ST05-13(a) lint hardened: resolves per-module import aliases (`import x as y`, `from x import n as m`) to dotted names. Anthropic SDK constructors Anthropic, AsyncAnthropic, Client, AsyncClient, AnthropicBedrock, AsyncAnthropicBedrock, AnthropicVertex, AsyncAnthropicVertex (all present in SDK 1.8.0), from any `anthropic.*` module, need `http_client=`; an unimported bare Anthropic* name is flagged too. Any construction of httpx/httpx2 Client, AsyncClient, HTTPTransport, AsyncHTTPTransport or MockTransport is flagged however it is imported. Self-test: 18 positive snippets (each bypass from the review plus the old cases), each giving exactly one finding, and 8 negative snippets (guarded SDK clients incl. aliased, a local `class Client`, an unimported `AsyncClient()`, httpx Request/Response, the guard call) giving none.
2. (Minor) `_http_client` passes `timeout=req.timeout_s` to `async_http_client` (the Protocol declares `timeout`); UT05-37 asserts 30.0.
Parked per controller: section 8 catalogue entry for response_truncated; empty user text blocks.
Commit: 4e81dae fix(harness): harden ST05-13 lint and pass guard timeout (T05-08).
Results: card filter 77 passed, 1 skipped (IT05-09); tests/unit/harness 772 passed, 1 skipped; coverage anthropic_client.py 99%; ruff/mypy clean; module size exit 0. anthropic_client.py now 355 lines (budget 400); tests/unit/harness/test_llm_anthropic.py 845 lines.
