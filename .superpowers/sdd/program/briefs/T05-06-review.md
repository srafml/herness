# T05-06 review (verify, opus) — round 0
Worktree agent-a0dfd235925f64aa6, base 9b794b5, head cc13410. (Saved by sub-controller; reviewer isolation refused the write.)

Verdict: Needs fixes (0 Critical, 5 Important, 6 Minor)

Ran: card tests 43 passed / 1 skipped (IT05-08, no HERNESS_LLM_URL); ruff + format clean; mypy --strict clean; coverage base.py 100/100, openai_compat.py 100% line, 53/54 branches (106->108 unreachable by Message validator); budgets openai_compat.py 336/380, base.py 174/180.

## Spec
- ✅ Boundary ruling: >200,000 truncated to exactly 200,000 incl. `[truncated]` (base.py bound_response), WARNING harness.llm.response_truncated with client+length only (test :431-437); 200,000 verbatim, 200,001 cut (:423-429); 64 tool calls ok, 65 -> OutputValidationError (:447-453), count checked at openai_compat.py:262 before arg decode at :265; 1M check first (test :440-444 pins 1,000,001). Message always valid (<=66 parts; TextPart 200k).
- ✅ Preconditions (kind :172; off_network + egress disabled :175-182; req.client :309); message mapping + goldens; _build_params per server (Ollama auto / omit for none); thinking extra_body; response_format; UT05-27 ConfigError; _map_response mapping; model_mismatch WARNING (actual[:128]); acomplete per-call SDK client, max_retries=0, translate_openai_error; complete() in loop -> RuntimeError (UT05-30).
- ✅ TH05-15 secrets: resolved once, SecretStr, only Authorization header; never in body/exception/logs.
- ✅ Off-network fail-closed: guard without async_http_client -> ConfigError, nothing sent (:665-673); purpose/payload/run/task passed (:635-649); EgressBlocked not retried.
- ❌ TH05-20 response size cap covers message.content only (I1-I4).
- ⚠️ IT05-08 live not run; T10-17 client runtime compat unverifiable.

## Resource-bound placement (controller scope)
1. On-network body read/decoded by SDK with no byte ceiling (:313-323) — CONFIRMED (I2)
2. Reasoning text unbounded (_reasoning :142-148; 5,000,000 accepted) — CONFIRMED (I3)
3. Tool-call argument size unbounded before json.loads (:127-128, :114) — CONFIRMED (I4)
- json.loads before 64-call cap — NOT confirmed (cap :262 first)
- parsed json.loads before 1M check — NOT confirmed (:279 on bounded text)
- Whole-call timeout — CONFIRMED (I5): timeout=req.timeout_s is httpx per-phase, not a deadline
- Stream iteration — N/A (astream is T05-07)

## Important
- I1 openai_compat.py:256-291, :120-139, :151-160: malformed responses (missing model -> TypeError :271; usage without prompt_tokens -> pydantic ValidationError; tool-call arguments null -> TypeError :114; id null -> TypeError :125/:130; message null -> AttributeError :261; list content -> TypeError; int id -> ValidationError) escape the error taxonomy, and pydantic messages echo input_value. Fix: convert TypeError/AttributeError/ValidationError in mapping to OutputValidationError("malformed response") from None; handle non-str raw.model; parametrized UT05-26 test.
- I2 :313-323: no byte ceiling on on-network response body. Fix: byte-capped httpx2 client (MAX_RESPONSE_BYTES) / bounded read, or ruling to carry over.
- I3 :142-148: reasoning text unbounded. Apply content bounds before building ReasoningPart; test boundaries.
- I4 :127-128: per-call tool argument size unbounded before decode. Raise OutputValidationError above a cap (well above MAX_TOOL_ARGUMENT_CHARS 32,000; MAX_RESPONSE_TEXT_CHARS fits); boundary test.
- I5 :318/:323: no whole-call deadline. Wrap create in asyncio.timeout(req.timeout_s), translate like APITimeoutError; slow-transport test.

## Minor
- M1 pin exactly 1,000,000 (truncated) and order case (1,000,001 chars + 65 calls -> "response text exceeds limit").
- M2 httpx2 imported but undeclared (pyproject declares httpx, openai); R-06 banned-api list covers httpx.* not httpx2.* — carry-over impl 10/00.
- M3 guard Protocol returns httpx2.AsyncClient; openai 3.19.2 (_base_client.py:1617-1625) also accepts legacy httpx.AsyncClient — widen or retype when T10-17 merges.
- M4 guard client created (:313) before AsyncOpenAI(...); leak if constructor raises — AsyncExitStack.
- M5 builder interpretations acceptable; record as spec notes (tool_choice/parallel only with tools; assistant text "\n\n"; no is_error; extra hardening; client key in translated errors belongs to errors.py/T05-05).
- M6 builder squashed wip with reset --soft (noted, no code impact).

Builder concerns: respx cannot patch httpx2 — httpx2 transport patch justified; httpx2 vs httpx guard client is typing-only.

# Round 1 re-review (scoped, head 38ad66c) — Verdict: Approved
Ran: card tests + test_llm_base 95 passed / 1 skipped; ruff clean; mypy --strict herness/harness/llm clean (7 files); lint-imports 13 kept; coverage openai_compat.py 100/100, _openai_map.py 100% line 23/24 branches (81->83 unreachable), base.py 100/100; lines _openai_map.py 184/200, openai_compat.py 273/380, base.py 174/180.
- I1 CLOSED (openai_compat.py:209-215 wrap -> OutputValidationError("malformed response") from None; HernessError not swallowed; non-str model _openai_map.py:156-158; 8 shapes via _map_response and acomplete)
- I2 CLOSED (_capped_http_client openai_compat.py:66-113; _CappedStream counts while reading; over-cap Content-Length and non-identity encoding refused before read; Accept-Encoding identity; exact-cap / cap+1 tested; client+transport closed; env proxies do not bypass)
- I3 CLOSED (bound_response field="reasoning", _openai_map.py:120-130)
- I4 CLOSED (len check before decode, _openai_map.py:106-107; 1,000,000 decodes, +1 raises)
- I5 CLOSED (asyncio.timeout openai_compat.py:248-249 covers body read; ModelUnavailable("openai call failed: APITimeoutError") from None :260-261)
- M1, M3, M4 CLOSED. 200k/64 ruling still holds (text bound before tool-call decode; parsed from bounded text; <=66 parts).
- Bound placement: no new issues; worst decode 64 x 1M args capped by 50 MB body cap.
New Minor: N1 adapter builds an httpx2 client at L4 (R-06: clients only from herness.core.egress / loopback_http_client; httpx2 not on ban list) — carry-over to T10-17 loopback_http_client + ban list (round-0 M2); N2 stale docstring _openai_map.py:4-5; N3 _fail without client= (_openai_map.py:95-96); N4 over-1M reasoning message says "response text"; capped client does not follow redirects (SDK default did) — spec note.
