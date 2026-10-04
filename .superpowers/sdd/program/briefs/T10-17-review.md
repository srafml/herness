# T10-17 review: Guarded clients and lint (range a59bb45..de88922)

### Spec Compliance
- ❌ Issues found (details under Issues):
  - U10-54 / TH10-21: the 50 MiB cap and the `usage` parse run on wire bytes. A gzip response gets past the cap, and its provider token counts are lost (I-1).
  - U10-54 postcondition: an async request cancelled while inside the inner transport gets no `completed` line (I-2).
  - U10-55 / TH10-23: after the window closes, the flag stays set in asyncio tasks created inside it (I-3).
  - ST10-25: the lint misses non-call references to the builders, subclasses and private-path imports (I-4).

Per unit:
- U10-52 `http_client` ✅: `model_download` refused; certifi context with TLS 1.2 minimum; `HTTPTransport(verify=ctx, proxy=cfg, retries=0)`; `follow_redirects=False`; `trust_env=False`; `Timeout(t, connect=10)`. Timeout bounds include NaN.
- U10-53 `async_http_client` ✅: same settings over the async transport.
- U10-54 transports ⚠️: the allowed line is written before the inner transport. There is one completed line on normal close, on inner `Exception`, on overflow and on a pre-read response. Not covered: `BaseException`/cancellation, a stream that is never closed, and compressed bodies (I-1, I-2, M-1). The streaming-body check runs after steps 1-3, not as step 1 (M-2).
- U10-55 `download_window` ✅/⚠️: preconditions in spec order; audit and log lines; flag cleared in `finally`; `_window_open` wired. The flag leaks into child tasks (I-3).
- U10-59 loopback clients ✅: the construction check runs before any client exists (spy test). The per-request check covers absolute URLs, user info, scheme, case-folding (lower), and `[::1]`, which parses to `::1`. `localhost.` fails closed. Settings are correct and there is no proxy. Bearer is a default header and does not appear in logs (the log field is the host only).

Per test ID:
- ✅ UT10-52 (carry-over half), UT10-54, UT10-55, UT10-74
- ✅ ST10-07, ST10-09, ST10-12 (streaming half), ST10-32, ST10-35, ST10-40, ST10-41, ST10-54
- ✅ IT10-03, IT10-04
- ✅ ST10-39. The TLS 1.0 case asserts only `httpx.ConnectError`, with a trusted-CA control (M-4).
- ✅/❌ ST10-25: it meets its literal rows, but it is not robust (I-4).
- IDs, docstring first lines and module `pytestmark` are correct in all new test files. I re-ran the 5 new test files: 88 passed.

Carry-overs from T10-16:
- ✅ `_window_open` is wired.
- ✅ token_estimate accepts an int only; bool and float fall back to the body estimate, with a test.
- ✅ The `scan_hits or {}` nit and the docstring nit are fixed.
- ✅ The UT10-52 client half and the ST10-12 streaming half are done.

Ratified structure:
- ✅ `_egress_transport.py` builds no client or transport. It imports `egress` only under TYPE_CHECKING, so there is no cycle. It has a §2 row with budget 220 (205 used).
- ✅ The ContextVar is correct for thread isolation (tested) and for `to_thread`. Note that `async_http_client` refuses `model_download` anyway, so the `to_thread` rationale is moot. The ContextVar's real downside is I-3.

Budgets and scope:
- `check_module_size` exit 0.
- Line counts: egress.py 378/390, egress_clients.py 217/300, _egress_transport.py 205/220.
- config.py, errors.py, audit.py and secrets.py are not touched (diff stat checked).
- The ST10-25 exemption list is exactly the two spec files (tested).

⚠️ Cannot verify from diff:
- The T05-08 acceptance check (`AnthropicClient` with `http_client=get_guard().async_http_client(...)`): the module is not in the tree. IT10-03 stands in for it.
- The impl 03/05/08 local clients: none exist yet, apart from faults `_kill_service`, which is verified against the real client.

### Strengths
- Clean split: the transports sit in the sibling, and the shared TLS context and counting stream sit in `egress_clients` ready for U10-110.
- `_Call` guards against a second completion.
- Fail-closed loopback check through a single `loopback_refusal` used at both construction and request time.
- Strong tests: a `Client.__init__` spy proves nothing is built before the refusal; a real stub server; real TLS servers with a trusted-CA control; IT10-04 runs 4 methods x 3 pairs, sync and async, with zero connects.
- `_write_completed` does not mask the response, and the tests cover it.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
- **I-1 The response cap and the usage parse count compressed wire bytes, not the decoded body (TH10-21, U10-54 steps 4-5).**
  - Where: herness/core/_egress_transport.py `_Call.counter` (L80-86) and the `handle_request` / `handle_async_request` stream wraps (L154-159, L193-201); herness/core/egress_clients.py `StreamCounter` / `CountingStream` (L138-189).
  - `httpx.HTTPTransport` returns the raw stream; httpx decodes it later in `Response.iter_bytes`. The guarded clients send httpx's default `Accept-Encoding: gzip, deflate…`.
  - Probe (GuardedTransport over an inner transport that returns a gzip stream):
    - A 203,860-byte gzip body decoded to 209,715,200 bytes with no `EgressBlocked`. The completed line recorded `bytes_in=203860`. This is a decompression bomb from an allowed endpoint, which the 50 MiB cap is meant to stop.
    - A gzip JSON body with `usage` logged `tokens_in=<estimate>` and `tokens_out=null`. The provider figures are lost whenever the provider compresses.
  - Fix: send `Accept-Encoding: identity` as a default header on both guarded clients. Also either refuse a response whose `content-encoding` is not identity, or apply the cap and the usage parse to the decoded bytes.
  - Add a test for each: a gzip bomb, and gzip JSON usage.
- **I-2 No `completed` line when the send is cancelled or interrupted (U10-54 postcondition: exactly one completed line for every request that reached the inner transport).**
  - Where: herness/core/_egress_transport.py `GuardedTransport.handle_request` `except Exception` (L151) and `AsyncGuardedTransport.handle_async_request` `except Exception` (L189).
  - Probe: `asyncio.wait_for(client.post(...), 0.5)` against a slow inner transport. The result was an `allowed` line with no `completed` line.
  - `asyncio.CancelledError`, which comes from timeouts and task cancellation, is a `BaseException`, as is `KeyboardInterrupt` in the sync path.
  - Fix: catch `BaseException`. In the async path, write the line in a way cancellation cannot interrupt: call `call.complete` synchronously, since it is a small locked append, or use `asyncio.shield`. Add a test for a cancelled async send.
- **I-3 The download-window flag outlives the window in tasks created inside it (TH10-23; spec says "cleared on exit").**
  - Where: herness/core/egress.py `_WINDOW` (L80) and `download_window` (L299-321).
  - `asyncio.create_task` and `contextvars.copy_context` copy the ContextVar, and `_WINDOW.reset(token)` affects only the opening context.
  - Probe: a task created inside the window and awaited after it closed saw `_window_open() == True`, while the outer context saw `False`.
  - Result: a background task started during `deploy pull` keeps `model_download` rights for its whole lifetime.
  - Fix: store a per-window token object in the ContextVar and keep a module-level set of open tokens. `_window_open()` returns `_WINDOW.get() in _OPEN`, and exit discards the token. Stale copies then read False.
  - Add a test: a task created inside the window and checked after exit reads False.
- **I-4 The ST10-25 lint catches only direct calls of the builder names.**
  - Where: tests/security/test_st10_lint.py `_dotted` / `visit_Call` (L60-81).
  - Probe via `scan_source`. Each of the following returned `[]` (not flagged):
    - `C = httpx.Client; C()`
    - `class X(httpx.Client)`, a common SDK wrapper pattern
    - `functools.partial(httpx.Client)`
    - `getattr(httpx, "Client")()`
    - `httpx._client.Client()`
    - `from httpx._client import Client`
  - Fixes:
    - Flag any *reference* (Name or Attribute, including base classes) that resolves to a builder, not only calls.
    - Flag `httpx._*` access outside the egress files.
    - Add these cases to `PLANTED`.
  - `getattr` and `importlib` bypasses can stay out of scope; the socket guard (T10-18) backs them up.

#### Minor (Nice to Have)
- **M-1 A streamed response that is never closed writes no `completed` line.** Probe: `send(stream=True)` without `close()` wrote only `allowed`. Where: _egress_transport.py L157-159. Either register a `weakref.finalize` that writes a completed line with reason `not_closed`, or document that callers must close the response.
- **M-2 The streaming-body refusal runs after the host checks, not as U10-54 step 1.** Where: egress.py `_steps` L145-146. This is defensible, since the more specific host reason wins, and the test pins it (`test_st10_12_steps_1_to_3_come_before_the_streaming_check`). Record it as a spec note, or update the spec row.
- **M-3 `egress.log.failed` is not in the spec's log catalog.** Where: egress.py `_write_completed`. Add it to the catalog.
- **M-4 The ST10-39 TLS 1.0 case asserts only `httpx.ConnectError`.** Where: test_st10_egress_tls.py `test_st10_39_tls_1_0_handshake_fails`. Match an SSL or protocol message so that an unrelated connect failure cannot pass the test.
- **M-5 Uncovered loopback edge cases.** No test covers `LOCALHOST`, `localhost.` or `HTTP://`. The behavior is correct (lower-case matching; fail-closed for the trailing dot). Add them to pin the behavior.
- **M-6 `record_connects` relies on a frame-name check.** Where: tests/support/egress_servers.py `record_connects`. It checks `sys._getframe(1).f_code.co_name == "_fallback_socketpair"`, which depends on CPython internals. Acceptable in test code; add a comment naming the Python version it was checked on.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The structure, loopback enforcement, TLS and redirect settings, and the test coverage of the spec rows are solid. However, three security properties this card owns do not hold: the 50 MiB cap under compression (I-1), exactly one completed line on cancellation (I-2), and window-flag confinement (I-3). I confirmed each by probe. The ST10-25 lint also misses common reference and subclass patterns (I-4).
