# T05-08 review: Anthropic adapter (head e106f4c, base 9b794b5)

### Spec Compliance
- ✅ Spec compliant (with the controller rulings and the accepted deviations below)
  - U05-27 construction ✅: kind check; `security.egress.enabled` false → exact ConfigError text (`anthropic_client.py:157-163`); key resolved once via `secrets.resolve` and kept as `SecretStr` (`:169`); registered `("llm_client","anthropic")` (`:149`).
  - U05-27 `_to_anthropic_messages` ✅ (`:68-88`, `:171-186`): user → text blocks; assistant → opaque dict replayed only for `provider=="anthropic"` with `opaque` set, empty text omitted, tool_use `{id,name,input}`; tool → `tool_result {tool_use_id, content, is_error}`; consecutive user-role turns merged with a stable sort putting tool results first.
  - U05-27 `_build_params` ✅ (`:188-220`): model/max_tokens; `cache_control` only on `cache=True` system blocks; top-level `cache_control` ephemeral (VI-9); tools as `{name, description, input_schema, strict}` in name order (the LLMRequest validator sorts them); `tool_choice` auto/none only, never forced; thinking by mode: adaptive_always omits it, adaptive_optional adaptive/disabled, budget `max(1024, max//4)` or explicit, clamped to `1024 <= n < max`, ConfigError when that is impossible (`:91-101`); `output_config.effort` only when supported, and `format` when a schema is set; `stop_sequences`.
  - U05-27 `_map_message` ✅ (`:222-262`): text concatenated; tool_use → ToolCall; input that is not a dict → OutputValidationError; thinking/redacted_thinking → ReasoningPart with `to_dict()` opaque unchanged; five stop reasons map 1:1, anything else → `other` with `raw_stop_reason` kept; `refusal_category` from `stop_details` (a declared field in SDK 1.8.0, `types/message.py:77`); `parsed` from the first text block when a schema is set and the block is an object; cache usage mapping; `cost_usd(..., batch=batch)`; `request_id` from `_request_id`.
  - Adapter-boundary ruling ✅: >1,000,000 raises; 200,000 < len <= 1,000,000 is cut to exactly 200,000 chars with `[truncated]` and a WARNING that logs lengths only (`:104-111`); >64 tool calls raise (`:236-238`); private `_MAX_RESPONSE_TEXT_CHARS` with a carry-over comment, and base.py is untouched.
  - U05-28 ✅: `AsyncAnthropic(api_key, max_retries=0, timeout=req.timeout_s, http_client=guard client)` (`:278-284`); `max_output_tokens > 16,000` → `messages.stream` + `get_final_message` (`:292-296`); SDK errors → `translate_anthropic_error(exc)` `from exc` (`:310`); `complete` follows the U05-25 rule; `astream` yields TextDelta and ToolCallDelta(id, name, partial_json), no thinking deltas, and exactly one Done.
  - Guard client ruling ✅: a local Protocol over `get_guard()`, which fails closed with EgressBlocked when the guard lacks the method (`:264-276`); the Protocol is typed `httpx2.AsyncClient`; herness/harness never builds an httpx client. The arguments match U10-53 (purpose from `egress_purpose_for`, `"aggregated_evidence"`, run_id, task_id).
  - Tests ✅: UT05-31, UT05-32, UT05-33, UT05-34, UT05-35 (adapter-level 529 and 400; the table tests already exist in test_llm_errors.py), UT05-36, UT05-37 (transport type asserted, x-api-key only in the header), UT05-38, IT05-09 (skips unless `HERNESS_ANTHROPIC_IT=1`, as ruled), ST05-13 (a), (b) and (c).
- Deviations judged acceptable:
  1. temperature via `extra_body`. Verified: `AsyncMessages.create` in SDK 1.8.0 has no `temperature` keyword but does accept `cache_control` and `output_config`.
  2. tools and tool_choice are sent only when tools exist.
  3. thinking="auto" omits the key.
  4. effort is sent only when it is supported and set.
  5. An invalid tool name or id raises OutputValidationError (`:126-130`, `from None` hides the pydantic text).
  6. The §8 events `egress_blocked` (WARNING, client, task_id) and `bad_request` (ERROR, client, status) match the spec rows at lines 1840-1841.
- EgressBlocked is re-raised `from None` instead of `from exc` (`:307`). Accepted: the translated error already sits in `exc`'s cause chain, so `from exc` would create a `__cause__` cycle, and the guard's own object surfaces (`info.value is blocked`). Zero retries were observed in both acomplete and astream.
- ⚠️ Cannot verify from diff:
  - IT05-09 against the real API, which needs T10-17 `async_http_client` and a secret.
  - The httpx2 compatibility of the real guard (T10-17 carry-over, spec note for impl 10).

### Gates (re-run, focused)
- ruff check and format are clean on 3 files; mypy strict reports 0 issues.
- Card filter: 52 passed, 1 skipped (IT05-09). Coverage of `anthropic_client.py` is 99% (213 statements, 0 missed; 74 branches, 1 partial `83->76`), which meets the >=90/85 bar.
- Module size is 348/400.
- Test naming is correct: every function has an ID prefix, every docstring starts with its ID, and both files set `pytestmark` (unit; integration + skipif).

### Strengths
- The guard fail-closed path, zero retries and key hygiene are covered by real HTTP-level tests (httpx2.MockTransport through a stub guard) rather than by mocking the SDK.
- The opaque replay is gated on the provider, and the dropped cases are tested (`test_llm_anthropic.py:313-356`).
- No log call carries text, bodies, opaque blocks or keys (TH05-14, TH05-15). The bad-request and truncation tests assert that the payload is absent from the log entries.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. The ST05-13(a) AST lint can be bypassed through the SDK's own public aliases and through imported names (`tests/unit/harness/test_llm_anthropic.py:686-712`).
   - `anthropic` exports `Client = Anthropic` and `AsyncClient = AsyncAnthropic` (`.venv/Lib/site-packages/anthropic/_client.py:1148,1150`). `anthropic.AsyncClient(api_key=k)` builds an SDK client with its own httpx2 client, yet it passes: the owner `anthropic` is not in `_HTTPX` and the name `AsyncClient` is not in `_SDK_CLIENTS`.
   - These also pass: `from httpx import AsyncClient; AsyncClient()` (owner None), `import httpx as hx; hx.AsyncClient()`, and `from anthropic import AsyncAnthropic as A; A()`.
   - This lint is the only static control for TH05-13.
   - Fix:
     - Resolve import aliases per module (map the names that `Import`/`ImportFrom` bind to `anthropic.*`, `httpx.*`, `httpx2.*`).
     - Treat `anthropic.{Anthropic, AsyncAnthropic, Client, AsyncClient}` (and ideally the Bedrock/Vertex variants) as SDK constructors that need `http_client=`.
     - Flag any `httpx`/`httpx2` `Client`/`AsyncClient`/`*Transport` construction however it was imported.
     - Extend `test_st05_13_ast_lint_detects_violations` with these cases.

#### Minor (Nice to Have)
1. `harness.llm.response_truncated` (`anthropic_client.py:110`) is not in the spec §8 event catalogue (lines 1840-1842). Record it as a spec carry-over alongside the adapter-boundary ruling so T05-07 uses the same name.
2. The adapter sends empty content blocks.
   - An empty user `TextPart(text="")` is forwarded as an empty text block (`:71`), which the Messages API rejects with a 400 → ConfigError, so the task dies.
   - An assistant message whose only parts are dropped (non-anthropic reasoning, empty text) produces `content: []` (`:74-88`). This can occur after a chain fallback from a local model.
   - The spec is silent on both. Consider omitting empty user text and skipping empty turns, or document why not.
3. `_http_client` does not pass `timeout=req.timeout_s` to the guard (`:271-276`), so the guard client keeps its default of 120 s. The SDK's per-request timeout overrides it in practice, so there is no behaviour gap today.
4. `_to_anthropic_messages` silently discards parts that do not fit the role (`:178-180`). The Message validator already forbids these, so this is only defensive.
5. Carry-overs are correctly recorded in the report: T10-17 (`async_http_client`, httpx2), T05-06 (base constant), and T05-09 (52 lines of budget left).

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The adapter matches U05-27/U05-28 and every controller ruling, and it is well tested at 99% coverage with clean gates. The ST05-13(a) lint is the task's security acceptance check, and it can be bypassed through `anthropic.AsyncClient` and imported or aliased names, so it should be hardened before approval. The fix is test-only and small.

## Re-review round 1 (e106f4c..4e81dae)

### Important 1: ST05-13(a) lint bypasses
Fixed.
- The lint now resolves each module's `import`/`from ... import` aliases to dotted names.
- Calls to any of the 8 SDK constructors (including `Client`/`AsyncClient` and the Bedrock/Vertex variants) are flagged when they lack `http_client=`. This holds however they are imported, and also for bare names such as `Anthropic()`.
- Every httpx/httpx2 Client, AsyncClient, HTTPTransport, AsyncHTTPTransport and MockTransport construction is flagged (`test_llm_anthropic.py:686-805`).
- The self-test covers 18 bad and 8 good snippets.

I ran the review's bypass list plus extra forms against `_violations`:
- Flagged: `**kw` with no `http_client`, `import anthropic._client as m; m.AsyncAnthropic()`, and `from anthropic import *; AsyncAnthropic()`.
- Not flagged:
  - `from httpx import *; AsyncClient()` and `from anthropic import *; AsyncClient()`. Star imports are already banned by ruff F403/F405, so these are low risk.
  - Dynamic forms: `getattr(anthropic, "AsyncAnthropic")()`, `C = anthropic.AsyncAnthropic; C()`, `functools.partial(...)`, `importlib.import_module("httpx").AsyncClient()` and `__import__("httpx").Client()`.
  - Other HTTP libraries (aiohttp, urllib). These are outside ST05-13(a)'s stated scope. At runtime, the U10-58 socket guard and the egress audit catch them.

Judgement: what remains is dynamic or indirect construction, which a static name-based lint cannot catch in general. It is acceptable for this lint.

Optional hardening, Minor, non-blocking: flag any `getattr(<anthropic|httpx|httpx2 module>, ...)` call and any bare reference (not a call) to `anthropic.*Anthropic*` or `httpx*.Client`/`AsyncClient`. That would close the assign-then-call and partial forms cheaply.

### Minor 3: guard timeout pass-through
Fixed.
- `_http_client` passes `timeout=req.timeout_s` (`anthropic_client.py:283`).
- The Protocol declares `timeout`, matching the U10-53 signature.
- UT05-37 now asserts 30.0.

### Gates (re-run)
- Card filter: 77 passed, 1 skipped (IT05-09).
- ruff clean; mypy 0 issues.
- `anthropic_client.py` is 355 lines, within the 400 budget.

Parked per controller: the §8 catalogue entry for `response_truncated`, and empty user text blocks. These are carry-overs, not blockers.

**Task quality:** Approved
