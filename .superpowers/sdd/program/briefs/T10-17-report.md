# T10-17 report: Guarded clients and lint

Worktree: D:\herness\.claude\worktrees\agent-a84dc2893ca5bcace (branch worktree-agent-a84dc2893ca5bcace, base a59bb45).
Checkpoints: e811cb4, e0722d5, ae9ebbb, 2d01426, b69868a, 4f6161b; final commit de88922 feat(core): guarded egress clients, transports, download window and loopback clients (T10-17).

## What was built
- U10-52/U10-53 `EgressGuard.http_client` / `async_http_client` (egress.py): `model_download` -> `EgressBlocked("model_download only inside deploy pull")`; a timeout outside (0, 3600] (or NaN) -> `ConfigError`; TLS context = `ssl.create_default_context(cafile=certifi.where())` with minimum TLS 1.2; inner `httpx.(Async)HTTPTransport(verify=ctx, proxy=security.network.http_proxy, retries=0)`; wrapped in `(Async)GuardedTransport`; client built with `follow_redirects=False, trust_env=False, timeout=httpx.Timeout(t, connect=10.0)`.
- U10-54 `GuardedTransport` / `AsyncGuardedTransport` (private sibling `_egress_transport.py`, re-exported from `herness.core.egress`):
  - A request whose `request.content` raises `httpx.RequestNotRead` takes the guard's blocked path with reason `streaming_body`. This runs after steps 1-3, so a foreign host still reports its host reason.
  - `guard._admit` calls `_check_and_log` (the async version runs it with `asyncio.to_thread`).
  - The response stream is wrapped in a counting stream. Past `MAX_RESPONSE_BYTES` it raises `EgressBlocked("egress blocked (<id>): response_too_large", egress_id=..., reason=...)`.
  - When the stream closes, exactly one `completed` line is written with `status_code`, `bytes_in`, `latency_ms` (monotonic), `tokens_in` (provider figure or the estimate), `tokens_out` (figure or null), and reason `response_too_large` when the response was cut.
  - JSON bodies up to 10 MiB are read for `usage.input_tokens/output_tokens` or `prompt_tokens/completion_tokens`. Only an int of at least 0 counts; a bool is rejected.
  - An inner exception writes the `completed` line with `status_code` null and reason = the exception class, then re-raises.
  - An inner transport that returns a response it has already read (for example `httpx.MockTransport`) is counted and completed at once (`_Call.settle`).
  - Logs `egress.call.completed` at INFO. `# T08-05:` markers are left for bytes, tokens and latency.
- U10-55 `EgressGuard.download_window(*, allow_download, actor)`:
  - Checks the preconditions in spec order: allow_download, then synth, then `HERNESS_WORKER=1`.
  - Sets the flag, logs opened/closed, and audits `admin_action` with action=deploy_pull, target=download_window.
  - Clears the flag in `finally`.
  - `_window_open()` now reads the flag (carry-over wired).
- U10-59 `loopback_http_client` / `aloopback_http_client` (egress_clients.py, re-exported from egress):
  - Preconditions are checked before any client exists. A non-http(s) scheme, user info, a non-loopback host or an unparsable URL raises `EgressBlocked("loopback client used for <host>", reason="not_loopback")`. A bad `timeout_s` raises `ConfigError`.
  - `LoopbackOnlyTransport` / `AsyncLoopbackOnlyTransport` re-check every request, including absolute URLs and forced redirects.
  - A refusal logs `egress.loopback.blocked` with the host only, plus a `# T08-05` marker.
  - `bearer` becomes the default `Authorization` header.
  - Client settings: `base_url`, `follow_redirects=False`, `trust_env=False`, `Timeout(t, connect=min(t, 5.0))`, `HTTPTransport(retries=0)`, no proxy.
  - `LOOPBACK_HOSTS` moved into egress_clients.py and is re-exported from egress. This avoids an import cycle; `eg.LOOPBACK_HOSTS` is unchanged.
- Shared parts in egress_clients.py, used by the guarded clients and ready for the source client (U10-110): `tls_context()`, `check_timeout()`, `StreamCounter`, `CountingStream`, `AsyncCountingStream`.
- ST10-25 AST lint (tests/security/test_st10_lint.py):
  - Scans herness/, app/ and tools/, resolving `import x as y` and `from x import y as z` aliases.
  - Flags calls to the four httpx builders, any import or attribute use of `requests` / `urllib.request`, and `anthropic.Anthropic(` / `AsyncAnthropic(` without `http_client=`.
  - Only `herness/core/egress.py` and `herness/core/egress_clients.py` are exempt; there is no connector allowance.
  - The repository passes.
- Carry-overs:
  - `_window_open` is wired to the download window.
  - token_estimate accepts an int only; a bool or float falls back to the body estimate, with a test.
  - `_refuse` uses `line["scan_hits"] or {}`; the dead `.get` default is gone.
  - The `_egress_scan` docstring now cites the w07-s10 ruling.
  - The faults.py comment is corrected with no line change (340/340).
  - `kill_service` is verified against the real `loopback_http_client` with a stub server.
- pyproject: TID251 per-file ignore for `herness/core/egress_clients.py`. `herness.core.egress_clients` and `herness.core._egress_transport` are added to the "core base is closed" forbidden list.
- Spec: new §2 module-map row for `herness/core/_egress_transport.py` (budget 220) in docs/impl/10-config-security-deployment.impl.md, in the card's commits.

## Files and line counts (budget)
- herness/core/egress.py 378 (390)
- herness/core/egress_clients.py 217 (300)
- herness/core/_egress_transport.py 205 (220, new row; see deviation 1)
- herness/core/_egress_scan.py 108 (120), docstring only
- herness/core/resilience/faults.py 340 (340), comment only
- tests/unit/core/test_egress_clients.py 420
- tests/security/test_st10_egress_clients.py 350
- tests/security/test_st10_egress_tls.py 125, marker integration
- tests/security/test_st10_lint.py 174
- tests/integration/test_security_egress.py 125, marker integration
- tests/support/egress_servers.py 163: `record_connects` fake socket (sync connect plus asyncio `create_connection`), loopback stub server, self-signed certificate, TLS server
- Pointer docstrings updated in tests/unit/core/test_egress_check.py and tests/security/test_st10_egress.py.

## Tests per ID
- UT10-52 (client-config half and N2):
  - Sync and async client settings: GuardedTransport, `follow_redirects` and `trust_env` False, `Timeout(t, connect=10)`, TLS 1.2 minimum, CERT_REQUIRED, check_hostname, certifi used, retries 0, no proxy.
  - Proxy taken from config (HTTPProxy / AsyncHTTPProxy).
  - Timeout bounds, including NaN.
  - `model_download` refused.
  - A bool or float token_estimate is ignored.
- UT10-54:
  - allowed then completed with the same egress_id, for Anthropic and OpenAI usage names.
  - No usage or invalid usage: tokens_in is the estimate, tokens_out null.
  - JSON over 10 MiB is not parsed.
  - Inner failure (sync and async): completed with status null and the exception class, then re-raised.
  - A failing completed-line write is reported, not raised.
  - Async client lines.
- UT10-55:
  - The three refusals, with no audit line.
  - A valid window admits huggingface.co model_download and audits deploy_pull.
  - Another thread does not see the flag.
  - The flag is cleared on exception.
- UT10-74:
  - Both variants are built with `follow_redirects` and `trust_env` False, loopback transports and retries 0.
  - `/health` reaches the transport (respx).
  - The connect timeout is capped.
  - 10.0.0.5, ftp:// and unparsable URLs give not_loopback before any client exists (checked with a `Client.__init__` spy).
  - `timeout_s=0` gives ConfigError.
  - The bearer header is set.
- ST10-07: a local-profile client is blocked with zero connects.
- ST10-09: the 4 blocked cases, plus the case-folded host that is allowed.
- ST10-12: sync and async streaming bodies are blocked and audited with zero connects; a foreign host reports its host reason first.
- ST10-32: a 302 is not followed; a forced follow is blocked with host_not_allowed; the evil route gets 0 calls.
- ST10-35: `http_client("model_download", "none")` and a window inside a worker are both blocked.
- ST10-40: the order is allowed, then inner, then completed, and completed is written once; async version; pre-read response.
- ST10-41: a 60 MiB response is cut, sync and async.
- ST10-54:
  - 3 base_url cases for each of the 2 variants: zero connects, host-only logs, bearer absent.
  - Absolute URL, sync and async.
  - Stub server: its 302 is not followed; a forced follow gives not_loopback; the bearer reaches the stub but not the logs.
  - Loopback calls write no egress line.
- ST10-39:
  - A self-signed certificate gives CERTIFICATE_VERIFY_FAILED.
  - With the CA overridden to trust it, the call succeeds even with HTTPS_PROXY, HTTP_PROXY and ALL_PROXY set to a dead port.
  - A server offering only TLS 1.0 gives a handshake failure.
- ST10-25:
  - The repository passes.
  - The two egress files are seen as builders, so the rule is live.
  - 12 planted violations fail.
  - Vendor SDKs, `http_client=` and httpx value types pass.
  - The exemption covers only the two files.
- IT10-03: an adapter-like call through `get_guard().async_http_client` in hybrid writes allowed and completed lines with provider tokens, and the marker is in no log file.
- IT10-04: in the local profile, 4 methods × 3 purpose/class pairs × sync and async are all EgressBlocked, with zero connects and the respx route never called.
- UT08-49 (extra): faults `kill_service` works through the real loopback client against a stub server.

## Gates
- `PYTHONUTF8=1 uv run pytest tests/unit/core tests/security/test_st10_egress_clients.py tests/security/test_st10_egress_tls.py tests/security/test_st10_lint.py tests/security/test_st10_egress.py tests/security/test_st08_faults.py tests/integration/test_security_egress.py -q -p no:logging --cov=... --cov-branch`: 1540 passed, 1 skipped. The skip is the existing Windows symlink-privilege test.
- Coverage, branch included: egress.py 99 %, egress_clients.py 95 %, _egress_transport.py 98 %, _egress_scan.py 100 %.
- ruff format/check clean; mypy clean (184 files); lint-imports 13 contracts kept; check_module_size exit 0.
- Pre-commit hooks, including detect-secrets and pytest-unit, passed on every commit. No full-suite run, per dispatch.
- TDD note: each unit's implementation was written first and its tests right after, so no RED run was captured. This departs from the implementer-rules TDD order.

## Deviations and spec notes
1. Size: egress.py could not hold the transports; with everything inline it reached 462 lines.
   - The transports and the completed-line logic moved to the private sibling `herness/core/_egress_transport.py`, with a new §2 row (budget 220, 205 used).
   - `GuardedTransport`, `AsyncGuardedTransport` and `MAX_RESPONSE_BYTES` stay importable from `herness.core.egress`. MAX_RESPONSE_BYTES is now defined in the sibling and re-exported.
   - The sibling builds no client or transport, so ST10-25's exemption list stays exactly the two spec files.
   - The row needs controller ratification.
2. The download-window flag is a module-level `contextvars.ContextVar`, not `threading.local`.
   - It is local per thread and per asyncio task.
   - It carries into `asyncio.to_thread`, where the async transport runs the check; a thread-local would be invisible there.
   - Nested windows restore the outer state via the token.
3. A failed `completed`-line write (StoreBusy/OSError) is logged as `egress.log.failed` (ERROR, egress_id and error_type) and not raised.
   - The call has already happened, and raising from `Response.close()` would mask the response.
   - This event name is not in the spec's log catalog.
4. `http_client`'s `model_download` refusal has no `reason`; the spec gives only the message. The timeout precondition raises `ConfigError`; the spec says only "as preconditions".
5. ST10-39 stubs `EgressGuard._admit` and overrides `certifi.where` for the trusted-CA control and the TLS 1.0 case. The stub is needed because the test destination is a local port, which U10-51 step 3 refuses. This is the row's "test-only override of the CA".
6. faults.py `_is_loopback_url` accepts any 127/8 address, but `loopback_http_client` accepts only `LOOPBACK_HOSTS`. A stub URL such as `http://127.0.0.2:x` now fails closed with `not_loopback`. Not changed, because faults.py has no room.

## Acceptance checks
- T05-08 `herness.harness.llm.anthropic_client` is not in the tree; herness/harness/llm holds only settings.py and pricing.py.
  - There is no AnthropicClient call site to switch.
  - IT10-03 uses `get_guard().async_http_client(...)` the way T05-08 will.
  - ST10-25 will fail T05-08 if it constructs `AsyncAnthropic(` without `http_client=`.
- impl 03/05/08 local clients: no module in herness/, app/ or tools/ builds an httpx client or uses requests/urllib.request, and ST10-25 passes on the repository.
  - The only existing loopback caller is faults.py `_kill_service`. It already calls `egress.loopback_http_client(url, timeout_s=5)` and now works against the real function (test added).
  - No call sites were changed.
- `source_http_client` / `SourceHostTransport` (U10-110) belong to T10-33, and `install_socket_guard` (U10-58) to T10-18; neither was built.
  - Re-exporting them from egress.py (§2) is left to those cards.
  - egress_clients.py has 83 lines left for T10-33; the counting stream and TLS context it needs are already there.

## Concerns
- The new §2 row and budget for `_egress_transport.py` (220) need controller ratification.
- The `egress.log.failed` event is not in the spec's log catalog.
- The ST10-39 TLS 1.0 case relies on the local OpenSSL failing a TLS 1.0 handshake, asserted as `httpx.ConnectError`. A passing trusted-CA control shows the failure comes from the protocol, not the certificate.
- tests/unit/core/test_egress_clients.py is 420 lines; test files are outside the module-size roots.


## Fix round 1 (base e88be52; checkpoint 06c8f01; final commit: see the last line)

### Findings
- **I1 (gzip bomb), fixed.**
  - The guarded transports force `Accept-Encoding: identity` on every request, in `_Call.__init__`, whatever the caller or SDK set. This also covers every redirect hop.
  - The transport undoes `content-encoding` itself, handling `gzip`, `x-gzip` and `deflate` (zlib-wrapped, or raw on fallback). It inflates in pieces of 1 MiB at most, and removes `content-encoding` and `content-length` so the client does not decode again.
  - The 50 MiB cap counts decoded bytes, and the `usage` parse reads the decoded body.
  - Any other encoding (br, zstd, stacked codings) is refused unread with `EgressBlocked(... "unsupported_encoding")`, with a completed line. A corrupt body raises `httpx2.DecodingError`.
  - `bytes_in` now means decoded body bytes, as the caller reads them (documented on `StreamCounter`).
  - Tests: a gzip bomb of about 200 KiB that expands to 200 MiB gives `response_too_large`, for sync (octet-stream and JSON) and async. Gzip, x-gzip, deflate and raw-deflate JSON keep the provider tokens and a decoded `bytes_in`. Corrupt gzip and `br` are also tested.
- **I2 (cancellation), fixed.** Both transports catch `BaseException` around the inner send and write the completed line synchronously before re-raising (the async path does not await). Tests: an async send cancelled by `wait_for` (reason `CancelledError`), and a sync `KeyboardInterrupt`.
- **I3 (window leak), fixed.**
  - `_WINDOW` now holds a per-window mark object, and the module-level `_OPEN_WINDOWS` holds the marks of open windows. Exit discards the mark and resets the ContextVar token.
  - `_window_open()` returns `_WINDOW.get() in _OPEN_WINDOWS`, so a task context copied inside the window goes stale at exit.
  - Tests: a task created inside the window reads True inside it and False after exit; nested windows.
- **I4 (lint), fixed.**
  - The scanner now flags any Name or Attribute reference, and any from-import, that resolves to a builder in `httpx` or `httpx2`: `Client`, `AsyncClient`, `HTTPTransport`, `AsyncHTTPTransport` and the module-level request functions. This covers calls, `C = httpx.Client`, `class X(httpx.Client)`, `partial(httpx.Client)` and aliases.
  - Any `httpx._*` / `httpx2._*` use is flagged, as are `requests`, `urllib.request`, and anthropic constructors without `http_client=`.
  - PLANTED has 25 cases; the pass cases add `httpx2.BaseTransport` subclassing, `httpx2.MockTransport` and `except httpx.ConnectError`.
  - `import httpx as h` is resolved rather than flagged by itself: the aliased builder use is caught (`h.Client`).
  - The exemption list is still exactly egress.py and egress_clients.py. `_egress_transport.py` references only `httpx2.BaseTransport`/`AsyncBaseTransport`, byte streams, `Request` and `Response`, none of which is a builder, and the repository passes.
- **M1, fixed with a finalizer.**
  - Each streamed response gets a `weakref.finalize` (with `atexit=False`) that writes the completed line with reason `not_closed` if the response is collected unclosed. After a normal close the finalizer is a no-op, because of `_done`.
  - The close contract is documented in the `_egress_transport` docstring. Test: a `stream=True` response is deleted, then `gc.collect()`, then the completed line shows reason `not_closed` and status 200.
- **M4, fixed.** The TLS 1.0 case now matches `(?i)ssl|tls|protocol|handshake|version` on the `httpx2.ConnectError`.
- **M5, fixed.** `LOCALHOST` and `HTTP://127.0.0.1` are accepted for both variants; `http://localhost.:8000` is refused with `not_loopback`.
- **M6, fixed.** A comment names CPython 3.12.13 (Windows) for the `_fallback_socketpair` frame-name check.
- **M2, M3:** parked by ruling; no change.

### httpx2 ruling
- `http_client` returns `httpx2.Client` and `async_http_client` returns `httpx2.AsyncClient`.
- `GuardedTransport`/`AsyncGuardedTransport` now subclass `httpx2.BaseTransport`/`AsyncBaseTransport`. The inner transports are `httpx2.HTTPTransport`/`AsyncHTTPTransport` (retries=0, certifi TLS 1.2 context, configured proxy), with `httpx2.Timeout` and `httpx2.RequestNotRead`.
- The loopback clients and `LoopbackOnlyTransport`/`AsyncLoopbackOnlyTransport` are on httpx2 too.
- Names and signatures are unchanged.
- Step 3 URL parsing in `check` still uses `httpx.URL`, via `_egress_scan.host_reason` (spec text; the parser is the same).
- pyproject:
  - `httpx2>=2.13` added to `[project] dependencies`. `uv lock` changed 2 lines (the herness dependency list; still 2.13.1, no upgrades) and `uv lock --check` passes.
  - tests/unit/repo/test_pyproject.py RUNTIME gained the `httpx2` entry. **Spec note:** the impl 00 table 14.1 should list it.
  - Ruff TID251 bans were added for `httpx2.Client`, `AsyncClient`, `HTTPTransport`, `AsyncHTTPTransport`, `request`, `stream`, `get`, `post`, `put`, `patch`, `delete`, `head`, `options` and `query`.
- Tests:
  - respx is replaced by `tests/support/egress_mock.py` `MockNet`. It swaps `httpx2.HTTPTransport`/`AsyncHTTPTransport` for `httpx2.MockTransport` and streams the bodies (not pre-read), so the real guarded and loopback transports run above it.
  - IT10-04 now runs on the real httpx2 pools with `record_connects` (zero connects).
  - The local stub-server and TLS tests run unchanged on httpx2.
- Acceptance: `test_ut10_52_sdk_clients_accept_the_guarded_clients` builds `anthropic.AsyncAnthropic`/`Anthropic` and `openai.AsyncOpenAI`/`OpenAI` with `http_client=guard.(async_)http_client(...)`. All four construct.
- The §2 rows of egress.py, egress_clients.py and _egress_transport.py now name `httpx2`.

### Sizes
- egress.py 384/390 (includes T10-18's re-export line)
- egress_clients.py 269/300
- _egress_transport.py 218/220
- faults.py, config.py, errors.py, audit.py and secrets.py are untouched.

### Gates
- Tests: `PYTHONUTF8=1 uv run pytest tests/unit/core <card security/integration files> tests/security/test_st10_egress.py tests/security/test_st08_faults.py tests/unit/repo/test_pyproject.py tests/integration/repo/test_ruff_bans.py`: 1590 passed, 1 skipped (Windows symlink).
- Coverage: egress.py 99 %, egress_clients.py 94 %, _egress_transport.py 98 % (branch included).
- Static checks: ruff clean, mypy clean (185 files), lint-imports 13 kept, check_module_size 0, `uv lock --check` ok, all pre-commit hooks pass.

### Concerns
- **`classify.py` does not know httpx2 exceptions.** `herness/core/resilience/classify.py` (T08-04) maps `httpx.ConnectError`/`TimeoutException`/`RemoteProtocolError`/`HTTPStatusError`, but the guarded and loopback clients now raise the `httpx2` versions, which are separate classes. The SDK adapters map SDK errors themselves, but a direct caller of the loopback clients, such as impl 05 local servers, would get "unknown" classification. This needs a T08-04 follow-up (add the httpx2 classes); classify.py is outside this card.
- **`unsupported_encoding` is a new reason code**, not yet in the spec's reason list.
- **`_egress_transport.py` is at 218/220**, so there is almost no room left.

Fix round 1 final commit: 12855e5 fix(core): T10-17 review round 1 and httpx2 egress clients (code in checkpoint 06c8f01; the final commit carries the subject and message).


## Fix round 2 (base 12855e5; the code is in the final commit, see the last line)

### Findings
- **I5, fixed.**
  - ST10-25 no longer visits annotation nodes: argument annotations, return annotations and `AnnAssign` annotations, string annotations included.
  - Inside `if TYPE_CHECKING:` (a `Name` or `typing.TYPE_CHECKING`), imports are recorded quietly: they bind aliases but are not flagged, and other statements in that block are skipped. The `else:` branch is scanned normally.
  - Ruff TID251 no longer bans `httpx2.Client` / `httpx2.AsyncClient`. The transports and request functions stay banned; `httpx.*` is unchanged.
  - New pass cases: annotation-only use, string annotations, TYPE_CHECKING import plus alias, and `openai` with `http_client=`.
  - New failing cases: a construction next to a TYPE_CHECKING block, and an `else:` import followed by construction.
  - `test_st10_25_t05_08_adapter_shape_passes` checks an inline T05-08-shaped snippet: it passes, and dropping `http_client=` makes it fail.
  - Probe: the real `worktree-agent-a01f9cd030a2ec891:herness/harness/llm/anthropic_client.py` passes both `ruff --select TID251` and `scan_source` (the probe file was deleted afterwards).
- **M7 (partial, as ruled), fixed.**
  - `from httpx import *` / `from httpx2 import *` are flagged.
  - Module rebinding is resolved through `Assign` (`x = httpx2; x.Client()`, including chains). Rebinding to something unresolvable drops the alias.
  - `openai.OpenAI(` / `AsyncOpenAI(` without `http_client=` are flagged, like the anthropic constructors.
  - PLANTED now has 34 cases.
- **M8, fixed.**
  - The `not_closed` finalizer only appends a `partial(complete, ...)` to the module-level deque `_PENDING`. That append is atomic and never blocks, so gc never writes.
  - `_drain()` writes the queued lines at safe points: before each guarded request is admitted (the async version runs it inside the same `to_thread`), and in `close()` / `aclose()`.
  - Tests:
    - The existing test now shows the line is queued at gc and written at `close()`.
    - Collecting an unclosed response while this thread holds `guard._log.locked()` returns in under 2 s (0.17 s measured). The next request then writes `not_closed` before its own `allowed` line.
    - `aclose` drains the queue.
  - To stay in budget, `provider_usage` (the usage-token parse) moved to egress_clients.py; no new module.
- **M10, fixed.** In the spec, U10-54 step 4 and flow F10-03 step 6 now name `Accept-Encoding: identity`, decoded-byte counting, `bytes_in` as the decoded size, and `unsupported_encoding`.
- **M9:** parked by ruling; no change.

### Sizes
- egress.py 384/390
- egress_clients.py 289/300
- _egress_transport.py 220/220

### Gates
- Tests: `PYTHONUTF8=1 uv run pytest tests/unit/core <card security/integration files> tests/security/test_st10_egress.py tests/security/test_st08_faults.py tests/unit/repo/test_pyproject.py tests/integration/repo/test_ruff_bans.py`: 1606 passed, 1 skipped (Windows symlink).
- Coverage: egress.py 99 %, egress_clients.py 94 %, _egress_transport.py 98 %.
- Static checks: ruff, mypy (185 files), lint-imports (13 kept), check_module_size 0 and `uv lock --check` are all clean.

### Concerns
- **`_egress_transport.py` is exactly at its 220 budget**, and egress_clients.py has 11 lines left. T10-33's `source_http_client` will need its own room.
- **Still open: `classify.py` does not map httpx2 exceptions** (T08-04 follow-up), carried over from fix round 1.
- **Queued `not_closed` lines are written only at safe points.** A process that stops making guarded requests and never closes its client leaves them unwritten until exit. There is no atexit flush; `atexit=False` is kept so nothing is written during interpreter teardown.

Fix round 2 final commit: 75d3fb2 fix(core): T10-17 review round 2 lint scope, gc-safe not_closed lines (contains the code).


## Fix round 3 (base 75d3fb2; checkpoint c3e1ec5; the final commit carries the rest of the code, see the last line)

### 1) Loopback response ceiling (binding coordinator ruling for T05-06b), done
- Final signatures:
  - `loopback_http_client(base_url: str, *, timeout_s: float, bearer: SecretStr | None = None, max_response_bytes: int = MAX_RESPONSE_BYTES) -> httpx2.Client`
  - `aloopback_http_client(base_url: str, *, timeout_s: float, bearer: SecretStr | None = None, max_response_bytes: int = MAX_RESPONSE_BYTES) -> httpx2.AsyncClient`
- Checks and limits:
  - `max_response_bytes` must be an int (not bool) with `0 < n <= MAX_RESPONSE_BYTES`, else `ConfigError` before any client exists.
  - `LoopbackOnlyTransport` / `AsyncLoopbackOnlyTransport(inner, max_response_bytes)` keep the host check unchanged and force `Accept-Encoding: identity` on every request (also a client default header).
  - Through the shared `open_body` helper, a `content-length` over the ceiling is refused before any byte is read (`response_too_large`). Gzip, x-gzip and deflate are decoded with the same bounded inflater. Any other encoding gives `unsupported_encoding`. Decoded bytes are counted while the caller reads, and passing the ceiling gives `response_too_large`.
  - No redirects; `trust_env=False`; `httpx2.Timeout(timeout_s, connect=min(timeout_s, 5.0))`, so read, write and pool are `timeout_s` (stated in the docstring).
  - Local calls still write no egress line and pass no re-scan.
- Structure: the loopback transports, the streams (`StreamCounter`, `CountingStream`, `AsyncCountingStream`, `open_body`, `DECODABLE`, `MAX_RESPONSE_BYTES`, `LOOPBACK_HOSTS`, `loopback_refusal`) and the gc job queue (`PENDING`, `drain`) moved to a new private sibling, `herness/core/_egress_streams.py` (280 lines).
  - It is named for its content, streams and transports, rather than `_egress_loopback.py`.
  - Every public name is re-exported from `egress_clients.py` (and `egress.py`).
  - Both client factories stay in `egress_clients.py` (143 lines), so the ST10-25 exemption list is unchanged.
  - New §2 row with budget 300, citing this round's ruling; the module is added to the "core base is closed" import-linter list.
  - The guarded path uses the same `open_body` helper, so it gains the `content-length` precheck too.
- Spec: the U10-59 Signature, Preconditions, Postconditions and Errors rows are updated (httpx2, ceiling, identity, decoded counting, `unsupported_encoding`, `DecodingError`).
- Tests:
  - UT10-74: identity is forced even when the caller asks for gzip or br; `content-length` over the ceiling is refused with no byte read; a streamed body over custom ceilings (1000 and 4096) is refused sync and async while a small body passes; br is refused; invalid ceilings (0, -1, MAX+1, True, 1.5) give `ConfigError` for both variants.
  - UT10-74: `openai.AsyncOpenAI(base_url=.../v1, http_client=aloopback_http_client(...))` returns a chat completion through MockNet, with identity on the wire.
  - ST10-54: a gzip bomb through the sync and async loopback clients gives `response_too_large`, and a small gzip body decodes once.

### 2) m4, done
`_Call` keeps the `weakref.finalize` handle and `detach()`es it in `complete()`. Test: a stream is read, closed and collected, and `PENDING` stays empty.

### 3) m5, done
`drain()` guards each job: an `Exception` is logged as `egress.log.failed` with `error_type`, a `# T08-05:` marker is left, and draining continues. `close()` / `aclose()` flush inside `try/finally`. Tests: a planted raising job is logged and skipped while the next job runs, and a planted `KeyboardInterrupt` job still lets `close()` and `aclose()` close their inner transports.

### 4) m1 and m2, done
- ST10-25 still visits `Call` and `NamedExpr` nodes inside annotations. New planted cases: `def f(c: httpx2.Client())`, `x: httpx2.Client() = 1`, and a walrus in a return annotation.
- `if` is treated as TYPE_CHECKING only when the test resolves to `typing.TYPE_CHECKING` (`from typing import TYPE_CHECKING`, `typing.TYPE_CHECKING`, `import typing as t; t.TYPE_CHECKING`). New planted cases, both flagged: `TYPE_CHECKING = True` and `K.TYPE_CHECKING`. The pass case now uses `t.TYPE_CHECKING`.

### 5) m6, done
The U10-54 Postconditions row names `not_closed`: gc only queues the line, it is written at the next guarded request or on close, and it is lost at interpreter exit.

m3 and the isinstance nit are parked by ruling; no change.

### Sizes
- egress.py 384/390
- egress_clients.py 143/300
- _egress_transport.py 208/220
- _egress_streams.py 280/300 (new row)

### Gates
- Tests: `PYTHONUTF8=1 uv run pytest tests/unit/core <card security/integration files> tests/security/test_st10_egress.py tests/security/test_st08_faults.py tests/unit/repo/test_pyproject.py tests/integration/repo/test_ruff_bans.py`: 1628 passed, 1 skipped (Windows symlink).
- Coverage: egress.py 99 %, egress_clients.py 97 %, _egress_transport.py 96 %, _egress_streams.py 93 %.
- Static checks: ruff, mypy (186 files), lint-imports (13 kept), check_module_size 0 and `uv lock --check` are all clean.

### Concerns
- **New private module `_egress_streams.py`**, named differently from the suggested `_egress_loopback.py` because it also holds the shared streams and the gc queue; the new §2 row needs ratifying.
- **Still open: `classify.py` does not map httpx2 exceptions** (T08-04 follow-up).
- **T05-06b must pass its own byte limit.** When it switches to `aloopback_http_client`, it should pass `max_response_bytes=` if its limit is smaller than 50 MiB. Its current `OutputValidationError` becomes `EgressBlocked` (`response_too_large` / `unsupported_encoding`).

Fix round 3 final commit: 7b8baba fix(core): T10-17 review round 3 loopback response ceiling and queue hygiene. The production code and spec edits are in checkpoint c3e1ec5 of the same round; 7b8baba adds the tests, the ST10-25 changes and the report.
