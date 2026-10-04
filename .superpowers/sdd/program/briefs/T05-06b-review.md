# T05-06b review (verify agent)

Verdict: **Approved** (no Critical or Important findings; 4 Minor findings, fix them later or in T05-07).

Worktree D:\herness\.claude\worktrees\agent-a4f55c9d4a1fe3a77, head a616f90 (base f529d37). Changed files: herness/harness/llm/openai_compat.py (+32/-81), tests/unit/harness/test_llm_openai_compat.py.

## Spec / rulings
- ✅ Ruling 1: on-network `_http_client` returns `aloopback_http_client(cast(str, cfg.base_url), timeout_s=req.timeout_s, bearer=None, max_response_bytes=MAX_RESPONSE_BYTES)` from `herness.core.egress` (openai_compat.py:169-176). The test pins the exact args (`test_ut05_35_on_network_uses_egress_loopback_client`).
- ✅ Ruling 2: off-network still goes through the guard's `async_http_client` via the local `_GuardWithHttpClient` Protocol, and the fail-closed `ConfigError` is kept (openai_compat.py:177-187).
- ✅ Ruling 3: `_CappedStream`, `_CappedTransport`, `_capped_http_client`, `_too_large`, `_IDENTITY` and the runtime `_HttpClient` alias are all removed. `httpx2` is imported only under TYPE_CHECKING. The adapter builds no httpx or httpx2 client or transport, and the ST05-13(a) lint passes with no allowlist change.
- ✅ Ruling 4: `_raise_body_refusal` (openai_compat.py:189-193) maps only `response_too_large` and `unsupported_encoding` to `OutputValidationError("response body exceeds limit", client=...)`, both when the refusal arrives directly (`except EgressBlocked`) and when the SDK wraps it (`except openai.OpenAIError`). Any other `EgressBlocked` is re-raised unchanged; the test checks identity with `info.value is blocked`, direct and wrapped. No spec edit.
- ✅ Ruling 5: all the boundary tests are still there: 200,000 / 1,000,000 chars, 64/65 tool calls, the content-length precheck (52,428,801), the mid-read overflow (stops at the 3rd 100-byte chunk with cap 250), encoding (`br` is refused unread; `gzip` is now decoded and capped on decoded bytes), redirect (307 is not followed), and the whole-call timeout.
- ✅ Rules: no edits to the lint/security tests (ST05-13, ST10-25) or to the TID251 config; only the two files above changed.
- ✅ Acceptance: ruff shows 0 findings; the targeted tests pass; `pre-commit run --all-files` exits 0 (this includes pytest-unit, so IT00-01).
- ✅ Module budget: openai_compat.py is 238 lines (limit 380). The module-size hook passes.
- n/a: there is no `astream` (T05-07 adds it); not a finding, per the controller.

## Checks on the review focus
- **Where the cap is enforced.** The adapter reads no body itself; the SDK reads through `AsyncLoopbackOnlyTransport`. `open_body` refuses an over-cap content-length and any encoding other than identity/gzip/deflate before reading. `AsyncCountingStream` counts decoded bytes per chunk and raises as soon as the count passes the cap. Nothing reads an unbounded body on the real network path.
  - Caveat: `open_body` has an "already read" branch (`response.is_closed`) that counts after the read. It is reached only when the inner transport has already materialized the body (for example `MockTransport` with `json=`/`content=`). A real `AsyncHTTPTransport` never takes it. See Minor 2.
- **How narrow the translation is.** The reason set is a frozenset of the two reasons. Everything else falls through to a bare `raise` or to `translate_openai_error`, which returns a wrapped `EgressBlocked` unchanged. Nothing is swallowed. `TimeoutError` is handled before either branch.
- **`find_egress_block`** (herness/harness/llm/errors.py:31-43):
  - It walks `exc`, then `__cause__ or __context__`, at most 11 links deep, and returns the first `EgressBlocked` it finds.
  - Could it misfire? Only if an unrelated SDK error were raised while an `EgressBlocked` body refusal was being handled. In that case OVE is still the correct outcome.
  - It ignores `__suppress_context__`, so a `raise X from None` over an `EgressBlocked` would still be matched. The SDK path does not do this.
  - The depth bound makes the exception cycle harmless (see Minor 3).
- **Resource closing.** `AsyncExitStack.push_async_callback(http_client.aclose)` runs straight after the client is built, so the client is closed on success, on SDK init failure, on timeout, and on every error path. If construction itself fails (a non-loopback URL or a bad timeout), nothing has been built. Tests check `is_closed` for success, overflow and init failure.
- **Test transport patch.** `monkeypatch.setattr(egress_clients.httpx2, "AsyncHTTPTransport", factory)` swaps only the pool transport. The real `aloopback_http_client` / `AsyncLoopbackOnlyTransport` (host check, identity header, decode, cap, redirect refusal) runs on top of it. monkeypatch restores it at teardown. It is global to `httpx2` for the length of the test; no other build happens in those tests. It references no banned attribute, so TID251 is clean without noqa. That is acceptable for a test double.
- **Test hygiene.** `pytestmark = pytest.mark.unit`. Every test has an ID-prefixed docstring.

## ⚠️ Notes (no action required)
- **Oversize maps to OVE off-network too.** The mapping also applies to off-network guarded clients, because the guard's transport uses the same `open_body` reasons (herness/core/_egress_transport.py:86). This matches TH05-20 and ruling 4, which is not scoped to on-network.
- **Double close.** The SDK's `AsyncOpenAI.__aexit__` also closes the `http_client`. The second `aclose` does nothing, so this is harmless.

## Gate results (PYTHONUTF8=1, run in the worktree)
1. `uv run ruff check .` gives `All checks passed!` (exit 0, 0 findings).
2. `uv run pytest tests/security/test_st10_lint.py tests/unit/harness/test_llm_anthropic.py tests/unit/harness/test_llm_openai_compat.py -q -p no:logging` gives `189 passed in 4.42s` (exit 0).
3. `uv run pre-commit run --all-files` exits 0. Every hook passed: merge conflicts, toml, yaml, end-of-files, trailing whitespace, mixed line ending, private key, large files, ruff-check, ruff-format, mypy, import-linter, detect-secrets, fixtures-pii-scan, module-size, type-ownership, pytest-unit.
- `git status` afterwards is clean. No files were modified, and `.secrets.baseline` was not regenerated.

## Findings
### Critical
None.
### Important
None.
### Minor
1. **Timeouts over 3600 s now fail on-network** (herness/harness/llm/openai_compat.py:171-176).
   - `aloopback_http_client` calls `check_timeout`, which rejects `timeout_s > 3600` (and NaN/inf) with a `ConfigError` that carries no `client=` context.
   - `LLMRequest.timeout_s` is `Field(gt=0)` with no upper bound (herness/core/types/harness/llm.py:141). `ClientConfig.timeout_s` is also unbounded above (herness/harness/llm/settings.py:150).
   - Result: a request with `timeout_s > 3600` used to work on-network and now fails closed with `ConfigError`. There is no test and no note.
   - Suggested fix: bound `ClientConfig.timeout_s` / `LLMRequest.timeout_s` at 3600, or document the limit. Low impact.
2. **One cap test does not exercise counting while reading** (tests/unit/harness/test_llm_openai_compat.py:799-815).
   - `test_ut05_25_byte_cap_while_reading` answers via `server.respond`, which builds `httpx2.Response(json=...)`. Through `MockTransport` that response arrives with `is_closed=True`. I verified this directly (`is_closed True ByteStream`).
   - So the exact-cap / cap+1 case runs through `open_body`'s "already read" branch (a whole-body count after the read), not the streaming counter.
   - The streaming path is covered elsewhere: `byte_cap_counts_streamed_chunks`, `byte_cap_rejects_headers_up_front` and `gzip_body_counted_after_decoding` all use `stream=`.
   - Suggested fix: give the exact-cap test a `stream=` body too, so its name matches what it proves.
3. **Wrapped non-body refusals create an exception cycle** (openai_compat.py:228).
   - For a wrapped `EgressBlocked` with a reason other than the two body reasons, `raise translate_openai_error(exc) from exc` re-raises the original `EgressBlocked` with `__cause__ = exc`. Since `exc` already chains back to that `EgressBlocked`, the chain becomes a cycle.
   - The line predates this card, and both traceback printing and `find_egress_block`'s depth bound tolerate it.
   - Informational: it could re-raise the found block with `from None`.
4. **New tests use the wrong spec ID.** The new tests are labeled UT05-35, but in impl 05 the test table defines UT05-35 as "U05-30 each Anthropic exception, status 529 -> translate mapping per table" (docs/impl/05-harness-core.impl.md:2035).
   - The tests actually exercise U05-24/TH05-20 (loopback client, redirect, gzip cap, refusal translation), which belong under UT05-25 (or ST05-20).
   - The brief's Tests field named UT05-35, so the builder followed the brief. The controller should pick the ID and have the tests relabeled, which is a docstring and name change only.

## Verdict
**Approved.** All five controller rulings and the acceptance gates are met. The Minor items can be folded into T05-07.
