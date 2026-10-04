# T10-17 re-review, fix round 3 (75d3fb2..7b8baba, worktree agent-a84dc2893ca5bcace)

Scope: the coordinator-binding loopback ceiling for T05-06b, m4, m5, m1, m2 and m6, the new `_egress_streams.py` sibling, and regressions in the guarded path from the shared `open_body` helper. m3 and the isinstance nit stay parked, as ruled.

Probes are in my scratchpad under `r3/`:
- `loop_probe.py` uses a real socket server on 127.0.0.1 with no mocks.
- `test_guarded_probe.py` runs MockNet under the real guarded transports.
- `lintprobe.py` runs the ST10-25 `scan_source`.

## Items

- ✅ **1) Loopback ceiling (binding for T05-06b).**
  - **Signatures:** both match the ruling exactly (egress_clients.py:79-85 and :107-113).
  - **Invalid ceilings:** `0`, `-1`, `MAX+1`, `True`, `1.5` and `"10"` all raise `ConfigError` for both variants, before any client exists.
  - **Real-socket probes, sync and async, buffered and streamed, ceiling 1000:**
    - **Chunked 3000 B with no content-length:** `response_too_large`.
    - **Read-until-close 3000 B:** `response_too_large`.
    - **Chunked 300 B:** passes with 300 B.
    - **`content-length: 5000`:** `response_too_large` before any byte is read.
    - **Gzip bomb (20 MB of zeros):**
      - Chunked or with content-length: `response_too_large`.
      - At a 10 MB ceiling: refused.
      - At 30 MB or the default ceiling: decodes to 20 MB.
    - **Lying `content-length: 200` on a 5000 B body:** the h11 framing reads 200 B, which is counted, so the ceiling is never passed.
    - **`br`:** `unsupported_encoding`.
    - **302:** returned as-is, not followed.
    - **204:** passes with 0 B.
  - **What reached the wire:** `Accept-Encoding: identity` on every request, including when the caller sets `gzip, br`.
  - **Client settings:** `Timeout(connect=5.0, read=7.0, write=7.0, pool=7.0)` for `timeout_s=7`, `follow_redirects=False`, `trust_env=False`.
  - **Host checks are unchanged:** an absolute off-loopback URL and a userinfo URL both give `not_loopback`.
  - **`openai.AsyncOpenAI` through `aloopback_http_client`:**
    - A real chat completion returns "hi".
    - A 5 KB completion at ceiling 1000 gives `EgressBlocked(response_too_large)` to the caller.
    - The same completion at the default ceiling passes.
  - **Parity with `c663632:openai_compat.py` `_CappedStream`/`_CappedTransport`:** content-length precheck, identity, counting while reading and refusal of non-identity encodings are all kept. Gzip and deflate are now decoded under the cap instead of refused. That is strictly within the ruling.
  - **Spec:** the U10-59 Signature, Preconditions, Postconditions and Errors rows are updated (impl.md:1129-1135). See the doc nit n-3.
- ✅ **m4: the finalizer is detached.** `_Call._unclosed` holds the `weakref.finalize` handle, and `complete()` detaches it (_egress_transport.py:61, :80-81, :96-97).
  - **Probe:** 50 closed streams followed by `gc.collect()` leave `PENDING` empty.
  - One unclosed streamed response queues exactly one job, and `close()` writes it as `not_closed`. That gives 51 completed lines, with none duplicated.
- ✅ **m5: the queue drain is guarded.**
  - `drain()` wraps each job and catches `Exception`, which it logs as `egress.log.failed` with a T08-05 marker, then goes on draining (_egress_streams.py:202-213).
  - `close()` and `aclose()` drain inside `try/finally`, so the inner transport is always closed (_egress_transport.py:156-161, :203-208).
  - The tests cover a raising job and a `KeyboardInterrupt` job.
- ✅ **m1: calls in annotations are still scanned.** These are all flagged:
  - `list[httpx2.Client()]`
  - a walrus in a variable annotation
  - a call in a return annotation
  - a lambda-call inside an annotation

  Plain and subscripted annotations are not flagged (test_st10_lint.py:72-82).
- ✅ **m2: `TYPE_CHECKING` is resolved to `typing`.**
  - `TYPE_CHECKING = True` after `from typing import ...` is flagged.
  - A `TYPE_CHECKING` imported from another module is flagged, and so is `K.TYPE_CHECKING`.
  - `if not typing.TYPE_CHECKING` and the `else:` branch are both flagged.
  - The real `typing.TYPE_CHECKING`, including through `import typing as t`, stays quiet.
- ✅ **m6: the spec postcondition is updated.** The U10-54 Postconditions row now covers the following (impl.md:1048):
  - `not_closed`
  - queued at gc time
  - written at the next guarded request or on close
  - lost at process exit
  - "or is cancelled"
- ✅ **3) `_egress_streams.py` (280/300).**
  - It builds no `Client`, `AsyncClient`, `HTTPTransport` or `AsyncHTTPTransport`. It only subclasses `BaseTransport`, `AsyncBaseTransport`, `SyncByteStream` and `AsyncByteStream`.
  - Both pool transports and both clients are built in egress_clients.py:96-124.
  - The ST10-25 `ALLOWED` list is still exactly `egress.py` and `egress_clients.py`, and the repo scan passes. `ruff check .` (TID251) is clean.
  - **§2 row:** present (impl.md:102, budget 300) and cites the ruling.
  - **Import-linter:** the module is in "core base is closed" (pyproject.toml:346).
  - **Imports:** it imports only `errors` and `logging`. `egress_clients` imports it, and `_egress_transport` imports both. There is no cycle, and lint-imports reports 13 kept, 0 broken.
- ✅ **4) Guarded-path regressions from `open_body`.**
  - **Content-length over 50 MiB:**
    - Sync buffered, sync streamed and async each write exactly one `allowed` and one `completed` line (`response_too_large`, status 200).
    - `PENDING` stays empty, because no finalizer is registered on that path and the unwrapped `response.close()` triggers no second `on_close`.
    - The completed line is never skipped: `complete()` runs before the re-raise (_egress_transport.py:71-75).
  - **Gzip with a content-length:**
    - The precheck compares the wire size against the decoded ceiling. That is conservative.
    - `content-length` and `content-encoding` are dropped.
    - The completed line has the decoded `bytes_in` and the provider tokens.
  - **204:** one completed line with status 204 and `bytes_in` 0.
  - **HEAD with a small content-length:** passes.
  - **HEAD with a content-length over the ceiling:** refused (see n-1).

## Gates (re-run at 7b8baba)

- **Tests:** `tests/unit/core` plus these files: 1616 passed, 1 skipped (Windows symlink).
  - `tests/security/test_st10_lint.py`
  - `test_st10_egress_clients.py`
  - `test_st10_egress.py`
  - `test_st08_faults.py`
  - `tests/integration/repo/test_ruff_bans.py`
  - `tests/unit/repo/test_pyproject.py`
- **`test_st10_egress_tls.py`:** 6 passed.
- **`tools/check_module_size.py`:** exit 0.
- **lint-imports:** 13 kept, 0 broken.
- **mypy on `egress.py`, `egress_clients.py`, `_egress_transport.py` and `_egress_streams.py`:** clean.
- **`ruff check .`:** clean.
- **`uv lock --check`:** clean.

## New findings

### Critical
None.

### Important
None.

### Minor

- **n-1 A bodiless response with a large `content-length` is refused.**
  - Where: _egress_streams.py:132-134.
  - A HEAD response, or a 304, whose `content-length` is over the ceiling gets `EgressBlocked("response_too_large")` on both the loopback and guarded paths, although it has no body.
  - T05-06's own `_CappedStream` behaved the same way, so parity is kept.
  - No provider call uses HEAD.
  - Optional fix: skip the precheck when `request.method == "HEAD"` or the status is 1xx, 204 or 304.
- **n-2 A pre-read (already closed) response over the cap now logs `bytes_in=0`.**
  - Where: _egress_streams.py:128-131 and _egress_transport.py:71-75.
  - Before this round, the completed line carried the counted size.
  - This only happens with mock transports, because a real pool never returns a closed response. The reason code is still `response_too_large`.
- **n-3 U10-59 still names `httpx` in three rows.**
  - Where: impl.md:1130, :1133 and :1136.
  - Preconditions says "parses with `httpx.URL`", Algorithm step 2 says `httpx.HTTPTransport(retries=0)` / `httpx.AsyncHTTPTransport`, and Concurrency says `httpx.Client`.
  - The Signature and Postconditions rows now say `httpx2`.
  - Doc nit: fold it in at the next touch.

## Verdict
**Approved.** The loopback clients give T05-06b everything its own byte-capped client did, as the ruling requires:
- the `max_response_bytes` parameter, checked with `ConfigError`
- the content-length precheck
- identity forced on every request
- decoded bytes counted and capped while the caller reads, including a gzip bomb
- unsupported encodings refused
- no redirects
- all four timeouts set
- unchanged host checks

This is verified with real sockets, sync and async, and through `openai.AsyncOpenAI`.

m1, m2, m4, m5 and m6 are fixed. The new sibling builds no client or pool transport, the ST10-25 exemption list is unchanged, and all gates pass. n-1 to n-3 are optional.
