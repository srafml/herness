# T10-17 re-review, fix round 1 (e88be52..12855e5, worktree agent-a84dc2893ca5bcace)

Scope: I1-I4, M1, M4, M5, M6, the httpx2 ruling, and regressions caused by the fix. M2 and M3 are parked and not reviewed here. Every claim below was checked with my own probe scripts (scratchpad probe.py, lintprobe.py, reentry.py, urldiff.py) against the real guarded transports, with only the pool transport replaced by `httpx2.MockTransport`.

## Round-0 findings

- ✅ **I1 (gzip bomb / usage on wire bytes).**
  - Gzip bomb, 203,860 wire bytes that expand to 200 MiB:
    - Sync, 64 KiB chunks: `EgressBlocked response_too_large`, `bytes_in=53,477,376`.
    - Sync, the whole body as one chunk: same result. The 1 MiB inflate pieces hold, so memory stays bounded.
    - Async: same result.
  - Raw-deflate bomb: `response_too_large`.
  - Provider tokens come through decoded for gzip, x-gzip, zlib-deflate, raw deflate and `"GZIP "` (case and space tolerated): `tokens_in=11`, `tokens_out=7`.
  - Refused: `br` and `gzip, gzip` give `unsupported_encoding` with one completed line.
  - Corrupt gzip gives `httpx2.DecodingError`, with one completed line.
- ✅ **I2 (cancellation).** `asyncio.wait_for(post, 0.3)` against a slow inner transport writes one completed line with `reason=CancelledError` and `status_code=null`. The sync `KeyboardInterrupt` case is tested.
- ✅ **I3 (window leak).** A task created inside the window reads True inside and False after exit. The outer context reads False. A `copy_context()` taken inside the window and run after exit reads False. Another thread reads False. The `_OPEN_WINDOWS` add, discard and `in` operations are atomic under the GIL on CPython 3.12, and a mark is an `object()` hashed by identity, so thread safety is fine.
- ✅ **I4 (lint bypasses).** Caught:
  - `C = httpx.Client; C()`
  - `class X(httpx.Client)`, and the same through a from-import alias
  - `functools.partial(h.AsyncClient)` through aliases
  - `httpx._client.Client()` and `from httpx._client import Client`
  - `from httpx2 import _transports`
  - The httpx2 versions of all of the above
  - The 25-case PLANTED set passes.
  - New precision problem: see **I-5**.
- ✅ **M1 (never-closed stream).** `send(stream=True)` followed by `del` and `gc.collect()` writes one completed line with `not_closed` and status 200. A response that was closed and then collected, sync or async, writes exactly one completed line: no duplicate.
  - The finalizer references only the counter, not the response, so the response is still collectable.
  - New hazard from how the finalizer writes: see **M-8**.
- ✅ **M4.** The TLS 1.0 case matches `(?i)ssl|tls|protocol|handshake|version` (tests/security/test_st10_egress_tls.py:85).
- ✅ **M5.** `LOCALHOST` and `HTTP://127.0.0.1` are accepted and `http://localhost.:8000` is refused (tests/unit/core/test_egress_clients.py:543, 561).
- ✅ **M6.** The comment names CPython 3.12.13 (Windows) (tests/support/egress_servers.py:40).

## Regression checks

- ✅ **No double decoding.** On a gzip or deflate JSON response, `r.json()` works, and `content-encoding` and `content-length` are removed from the headers the caller sees.
- ✅ **No content-length mismatch.** The wire `content-length` is removed only when the body is decoded here.
- ✅ **Redirects are still not followed.** A 302 is returned with `next_request` set, one completed line, and no second hop.
- ✅ **Accept-Encoding cannot be overridden.** With the client default set to `br` and the per-request header set to `gzip`, the inner transport still received `accept-encoding: identity`.
- ✅ **The finalizer writes no duplicate completed line** when the response is closed (see M1).
- ✅ **The window mark set is thread-safe** (see I3).
- ⚠️ **Multi-member gzip decodes only the first member.** Trailing data is dropped silently. This matches httpx's own `GZipDecoder`, so it is not a regression; nit only.

## httpx2 ruling: ✅

- **Factories.** `http_client` returns `httpx2.Client` and `async_http_client` returns `httpx2.AsyncClient` (herness/core/egress.py:268-304). `loopback_http_client` and `aloopback_http_client` return httpx2 clients (herness/core/egress_clients.py:103-128). The inner transports are `httpx2.HTTPTransport` / `AsyncHTTPTransport` with `retries=0`, the timeouts are `httpx2.Timeout`, and the guarded and loopback transports subclass `httpx2.(Async)BaseTransport`.
- **Names and signatures** are unchanged. They match the T05-08 `_GuardedClientFactory` Protocol: `async_http_client(purpose, payload_class, *, run_id, task_id, timeout) -> httpx2.AsyncClient`.
- **Dependencies.** `httpx2>=2.13` is a direct dependency. `uv lock --check` passes. `uv sync --frozen --dry-run` reports "Would make no changes". Installed versions: anthropic 1.8.0, openai 3.19.2, httpx2 2.13.1.
- **SDK acceptance.** `anthropic.Anthropic` / `AsyncAnthropic` and `openai.OpenAI` / `AsyncOpenAI` construct with the guard clients (test_egress_clients.py:160-171; passes).
- **Bans.** Ruff TID251 bans cover the httpx2 builders and request functions (pyproject.toml:195-208), with exemptions only for egress.py and egress_clients.py. ST10-25 covers httpx and httpx2.
- **Test doubles.** Tests use `tests/support/egress_mock.py` (`httpx2.MockTransport` with streamed bodies), stub servers and TLS servers.
- **URL parsing, residual.** Step 3 re-parses `str(request.url)` with `httpx.URL`, not `httpx2.URL`. The two `_urls.py` files differ, but in a probe of 15 tricky URLs (userinfo, `\@`, `#@`, `:443@`, IDN, `%2e`, trailing dot, IPv6, short-form IPv4) the host was identical every time. Informational.
- **classify.py concern: confirmed, Minor, correctly parked as a T08-04 carry-over.**
  - `classify._HTTPX_UNAVAILABLE` (herness/core/resilience/classify.py:51) and the `httpx.HTTPStatusError` check (:162) do not match the httpx2 classes.
  - No caller is affected today:
    - The SDK adapters classify SDK exceptions through `_classes("anthropic", ...)`.
    - The only loopback caller in the tree is `faults._kill_service`, which does not classify.
  - It must land before the impl 05 local-server clients call `loopback_http_client` under a retry policy. Otherwise httpx2 `ConnectError` or `HTTPStatusError` would be "unknown" (not retried).

## Gates (re-run)

- Card and related test files: 145 passed (`test_egress_clients`, `test_st10_egress_clients`, `test_st10_egress_tls`, `test_st10_lint`, `test_security_egress`, `test_pyproject`, `test_ruff_bans`, `test_st10_egress`, `test_st08_faults`).
- `check_module_size`: exit 0.
- `lint-imports`: 13 kept.
- mypy on egress.py, egress_clients.py and _egress_transport.py: clean.
- `ruff check .`: clean.

## New findings

### Critical
None.

### Important
- **I-5 The ST10-25 scan and the new ruff httpx2 bans flag type annotations. The T05-08 adapter the ruling protects would fail both on merge.**
  - Where:
    - tests/security/test_st10_lint.py:92-103 (`visit_Name` / `visit_Attribute` flag every reference, including annotations and `TYPE_CHECKING` imports)
    - pyproject.toml:196 (`"httpx2.AsyncClient"` TID251)
  - Evidence:
    - `scan_source` on `worktree-agent-a01f9cd030a2ec891:herness/harness/llm/anthropic_client.py` returns `[(71, 'httpx2.AsyncClient'), (270, 'httpx2.AsyncClient')]`: the Protocol return type and the `_http_client` return type.
    - `ruff check --select TID251` on the same file: 2 errors.
    - Other false positives: `def f(c: httpx2.AsyncClient)`, `if TYPE_CHECKING: from httpx2 import AsyncClient`, and `isinstance(c, httpx2.Client)`.
    - ST10-25 has no suppression mechanism, so T05-08 cannot noqa its way past the scan.
  - Fix:
    - ST10-25: skip nodes inside annotation positions (`arg.annotation`, `FunctionDef.returns`, `AnnAssign.annotation`) and inside `if TYPE_CHECKING:` blocks. Add pass cases for these, and a pass case for the T05-08 Protocol shape.
    - Ruff: either export annotation aliases from `herness.core.egress` (for example `GuardedClient = httpx2.Client` and `AsyncGuardedClient = httpx2.AsyncClient`, which are exempt there) for callers to annotate with, or rule that callers put `# noqa: TID251 - annotation` on these lines. Tell the T05-08 integrator which one.

### Minor
- **M-7 ST10-25 residual gaps (low risk).** None of the following is flagged:
  - `from httpx2 import *` followed by `Client()`
  - `x = httpx2; x.Client()`
  - `httpcore` / `httpcore2.ConnectionPool()`, `urllib3.PoolManager()`, `http.client.HTTPSConnection`, `aiohttp.ClientSession()`
  - `openai.OpenAI()` without `http_client=` (only the anthropic constructors are checked; the spec names only anthropic, but the openai SDK is now locked too)
  - Where: test_st10_lint.py `_forbidden`, `visit_ImportFrom` and `visit_Call`.
  - Suggest flagging star-imports of httpx/httpx2, adding `httpcore2`/`httpcore` to the banned modules, and adding `openai.OpenAI`/`AsyncOpenAI` to the constructor check. The socket guard backs up the rest.
- **M-8 The `not_closed` finalizer writes synchronously from inside gc.**
  - Where: herness/core/_egress_transport.py:105.
  - A `weakref.finalize` callback runs wherever gc fires, including inside `EgressLog.write` on the same thread while `audit.log_lock` holds its non-reentrant per-process `threading.Lock`.
  - Probe (reentry.py): an unclosed streamed response was collected while this thread held `log_lock(egress lock)`. The result was a 10.0 s stall, then `StoreBusy`, then `egress.log.failed`, and the `not_closed` line was lost.
  - Such a callback can also run on the event-loop thread (file I/O plus a lock wait).
  - Fix: make the finalizer non-blocking. Options: hand the line to a deferred queue that the next `_write_completed` or a daemon thread drains, or acquire the lock with a zero timeout and defer on failure.
- **M-9 An async request cancelled during `asyncio.to_thread(_admit)` leaves an `allowed` line with no `completed` line.**
  - Where: herness/core/_egress_transport.py:195.
  - Probe: `_admit` was slowed to 0.5 s and cancelled at 0.1 s. The log held only `['allowed']`.
  - This does not break the spec: the request never reached the inner transport. But the log reads as an admitted call that never finished.
  - Either run `_admit` shielded and complete with `reason=CancelledError`, or record in the spec that an `allowed` line without a `completed` line means the send never started.
- **M-10 `unsupported_encoding` is a new reason code.** Add it to the impl 10 reason catalog, as the builder noted.

## Verdict
**Needs fixes.** I1-I4, M1 and M4-M6 are fixed and the httpx2 migration is complete and consistent. I-5 remains: the httpx2 lint and bans reject type annotations, so the ruled T05-08 adapter would fail ST10-25 and ruff on integration. M-7 to M-10 are optional in this round.
