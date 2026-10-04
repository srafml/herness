# T05-06b report: OpenAI adapter uses `egress.aloopback_http_client`

Status: DONE -- final commit a616f90 `fix(harness): OpenAI adapter uses egress.aloopback_http_client (T05-06b)`; all pre-commit hooks passed on it with no SKIP (IT00-01 hook path green).

## What changed
- `herness/harness/llm/openai_compat.py` (273 -> 238 lines):
  - Removed `_CappedStream`, `_CappedTransport`, `_capped_http_client`, `_too_large`, the runtime `_HttpClient` alias and the `_IDENTITY` constant. No runtime `import httpx2`/`httpx`; `httpx2` only under `TYPE_CHECKING` (Protocol / return annotations).
  - On-network: `aloopback_http_client(cfg.base_url, timeout_s=req.timeout_s, bearer=None, max_response_bytes=MAX_RESPONSE_BYTES)` imported from `herness.core.egress` (ruling 1; bearer None, SDK sends the key, TH05-15). `cfg.base_url` is `cast("str", ...)` (settings validator requires it for openai_compat).
  - Off-network: unchanged; guard's `async_http_client` via local Protocol `_GuardWithHttpClient` (returns `httpx2.AsyncClient`), fail-closed `ConfigError` when the guard lacks it (ruling 2). The SDK now gets `http_client=` without a cast.
  - `_raise_body_refusal(exc)`: `errors.find_egress_block(exc)`; if reason in {`response_too_large`, `unsupported_encoding`} -> `OutputValidationError("response body exceeds limit", client=self.name) from exc`. Applied in `acomplete` to both `except EgressBlocked` (then bare `raise` for other reasons) and `except openai.OpenAIError` (then `translate_openai_error`, which already returns a wrapped EgressBlocked unchanged).
  - Module docstring updated.
- `tests/unit/harness/test_llm_openai_compat.py`: new `_serve(monkeypatch, handler)` replaces the `httpx2.AsyncHTTPTransport` name on the `httpx2` module (via `monkeypatch.setattr(egress_clients.httpx2, "AsyncHTTPTransport", factory)`, factory returns `httpx2.MockTransport(handler)`), so the real loopback transport (host check, identity, cap, decode) wraps the mock. No `httpx2.AsyncHTTPTransport` attribute reference remains (TID251 clean, no noqa). `_spy_clients` now wraps `openai_compat.aloopback_http_client`.

## How EgressBlocked surfaces through openai 3.19.2
`AsyncAPIClient.request` catches only `timeout_exceptions()` (httpx2.TimeoutException), `OpenAIError`, and `request_exceptions()` (httpx2.RequestError). `EgressBlocked` is a `FatalError`, not an httpx2 error, so both the content-length/encoding precheck (raised in `handle_async_request`) and the mid-read decoded-byte overflow (raised while `client.send` reads a non-streaming body) propagate unwrapped. Asserted in the header test (`type(info.value.__cause__) is EgressBlocked`). The wrapped case (httpx2 RequestError whose cause is EgressBlocked -> `openai.APIConnectionError`) is also handled via `find_egress_block` and tested. The adapter has no `astream` (only `acomplete`/`complete`), so there is no streaming path to cover.

## Tests (tests/unit/harness/test_llm_openai_compat.py, 76 pass)
Kept, now over the real loopback client: UT05-25 byte_cap_while_reading (cap exact maps, cap+1 -> OVE, clients closed), byte_cap_rejects_headers_up_front (content-length 52,428,801 and `br` refused unread; `gzip` param replaced by `br` because the loopback client now decodes gzip), byte_cap_counts_streamed_chunks (stops at 3rd 100-byte chunk at cap 250), whole_call_deadline, http_client_closed_when_sdk_init_fails, legacy httpx guard client, UT05-28 connection error; all 200,000 / 1,000,000-char and 64-tool-call boundary tests unchanged.
Removed: `test_ut05_25_capped_client_closes_its_transport` (interim client gone).
New UT05-35: on_network_uses_egress_loopback_client (args, retries=0, `Authorization: Bearer EMPTY`, identity, closed), redirect_not_followed (307 -> one request, ModelUnavailable), gzip_body_counted_after_decoding (decoded 2,000-char body maps; cap 500 on decoded bytes -> OVE), wrapped_body_refusal_is_output_validation_error x2 reasons, other_egress_block_propagates (direct and wrapped, identity preserved).

## Gates
- `uv run ruff check .` All checks passed (0 findings); `ruff format --check` clean.
- `uv run mypy` no issues (217 files); `uv run lint-imports` 13 kept, 0 broken.
- `uv run python -m tools.check_module_size` exit 0; openai_compat.py 238 (<= 380), _openai_map.py 184, base.py 174 unchanged.
- `PYTHONUTF8=1 uv run pytest tests/security/test_st10_lint.py tests/unit/harness -q -p no:logging`: 945 passed, 1 skipped (symlink privilege, pre-existing); includes ST05-13(a) and ST10-25.
- Coverage openai_compat.py: 100% line, 100% branch (132 stmts, 38 branches).

## Concerns
- Brief says update U05-24/U05-26 error-row note to EgressBlocked; controller ruling 4 keeps OutputValidationError, so no spec edit was made.
- Test helper patches the `httpx2` module attribute (restored by monkeypatch); it is global for the test's duration, so any other httpx2.AsyncHTTPTransport build in the same test would also get the mock (none do).
