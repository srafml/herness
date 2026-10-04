# T10-17 re-review, fix round 2 (12855e5..75d3fb2, worktree agent-a84dc2893ca5bcace)

Scope: I5, the partial M7, M8 and M10 under the sub-controller ruling, plus regressions caused by the fix. M7's remainder (httpcore, urllib3, http.client, aiohttp) and M9 are parked and not reviewed here.

Probes are in my scratchpad (`r2/lintprobe.py`, `r2/t0508.py`, `r2/test_drainprobe.py`) and run against the real guarded transports. Only the pool transport is replaced, by MockNet.

## Round-1 findings

- ✅ **I5: annotations and `TYPE_CHECKING`.**
  - **Annotation nodes are skipped:** argument annotations, return annotations and `AnnAssign` annotations, string annotations included (tests/security/test_st10_lint.py:63-72).
  - **Values in annotated statements are still flagged:**
    - `x: httpx2.Client = httpx2.Client()` gives `[(2,'httpx2.Client')]`.
    - `s.c: httpx2.Client = httpx2.Client()` inside a method is flagged.
    - An argument default `def f(c: httpx2.Client = httpx2.Client())` is flagged.
    - A lambda default is flagged.
  - **`TYPE_CHECKING` handling:**
    - `if not TYPE_CHECKING:` is scanned (flagged).
    - The `else:` branch, `elif` and `TYPE_CHECKING or True` are all scanned (flagged).
    - A module imported under `TYPE_CHECKING` and then used at runtime (`httpx2.Client()`) is flagged, because the import still binds the alias.
  - **Ruff:** TID251 no longer bans `httpx2.Client` / `httpx2.AsyncClient` (pyproject.toml). The httpx2 transports and request functions stay banned. `httpx.*` is unchanged.
  - **T05-08's real file** (`git show worktree-agent-a01f9cd030a2ec891:herness/harness/llm/anthropic_client.py`):
    - `ruff check --select TID251` (with the project config) prints "All checks passed!".
    - `scan_source` returns `[]`.
    - If `http_client=` is removed, the scan gives `[(286, 'anthropic.AsyncAnthropic( without http_client=')]`, so the check still bites.
  - Residual gaps: see Minor m-1 and m-2 below.
- ✅ **M7, partial as ruled.** Now flagged:
  - `from httpx2 import *` and `from httpx2._client import *`.
  - `x = httpx2; x.Client()`, including chains (`y = x`).
  - `openai.OpenAI(` / `AsyncOpenAI(` without `http_client=`.
  - `openai.OpenAI(**kw)` is also flagged. That is a harmless false positive.
  - Residual gaps: see m-3.
- ✅ **M8: the gc-time write never blocks.**
  - The finalizer now only runs `_PENDING.append` (_egress_transport.py:89).
  - Collecting an unclosed response while the egress log lock is held returns at once. The `not_closed` line is written before the next request's `allowed` line.
  - **Thread safety:** 40 unclosed responses were collected, then 8 threads each made 5 requests at the same time. Result: exactly 40 `not_closed` lines, no duplicated `egress_id`, and no errors. `popleft` is atomic, `complete()` is guarded by `_done`, and each job is popped once.
  - **Async:** `_drain` runs in the `to_thread` worker, not on the event-loop thread (spy: drain thread != loop thread). `aclose` drains too.
  - Two new Minor issues on this path: m-4 and m-5.
- ✅ **M10: `unsupported_encoding` is in the spec.**
  - U10-54 step 4 now names `Accept-Encoding: identity`, decoded-byte counting, `bytes_in` as the decoded size, and `unsupported_encoding`.
  - F10-03 step 6 has the same text.
  - The spec has no separate reason catalog. Reason codes live inline in U10-51 and U10-54, so this placement is consistent.
  - A small gap remains: see m-6.

## Gates (re-run)

- `tests/unit/core/test_egress_clients.py`, `tests/security/test_st10_lint.py`, `tests/integration/repo/test_ruff_bans.py`: 98 passed.
- `tools/check_module_size.py`: exit 0.
  - egress.py: 384 lines.
  - egress_clients.py: 289 lines.
  - _egress_transport.py: 220 lines, exactly at its 220 budget.
- `lint-imports`: 13 kept, 0 broken.
- mypy on egress.py, egress_clients.py and _egress_transport.py: clean.
- `uv lock --check`: clean.
- `ruff check .`: clean. The `noqa: TID251` comments removed from the isinstance and spy lines leave no unused-noqa warnings.

## New findings

### Critical
None.

### Important
None.

### Minor

- **m-1 A call inside an annotation is skipped, and it runs at runtime.**
  - Where: tests/security/test_st10_lint.py:63-72.
  - Undetected (`[]`):
    - `def f(c: httpx2.Client()): ...`
    - `def f() -> httpx2.AsyncClient(): ...`
    - `x: httpx2.Client() = 1`
    - `x: (c := httpx2.Client()) = 1`
    - `def f(c: list[httpx2.Client()])`
  - These annotations are evaluated at definition time unless the module has `from __future__ import annotations`. That import is not enforced: for example, herness/connectors/settings.py and herness/core/types/memory.py lack it.
  - Risk: deliberate evasion only. The socket guard still holds the hosts.
  - Fix: inside a skipped annotation subtree, still visit `ast.Call` and `ast.NamedExpr` nodes. That is, skip only `Name` / `Attribute` / `Constant` / `Subscript` / `BinOp` references.
- **m-2 `TYPE_CHECKING` is matched by name alone.**
  - Where: tests/security/test_st10_lint.py:78-82.
  - Both of these hide a construction that really runs:
    - `TYPE_CHECKING = True` followed by `if TYPE_CHECKING: httpx2.Client()`.
    - `if K.TYPE_CHECKING:` where `K` is any class or object.
  - Fix: accept only a `Name` whose alias resolves to `typing.TYPE_CHECKING`, or an `Attribute` that resolves to `typing.TYPE_CHECKING`. The `from typing import TYPE_CHECKING` from-import already records the alias `typing.TYPE_CHECKING`.
  - Risk: deliberate evasion only.
- **m-3 Remaining rebinding gaps** (low risk; part of the parked M7 remainder):
  - Tuple unpacking (`a, b = httpx2, 1; a.Client()`) and walrus (`(m := httpx2).Client()`) are not resolved.
  - `aliases.pop` (:98) is flow-insensitive. Any unresolvable assignment to an import alias anywhere in the module, even dead code (`import httpx2 as h` / `if False: h = object()` / `h.Client()`), un-tracks every later use.
- **m-4 Every response queues a job when it dies, closed or not, and the queue keeps it until the next drain.**
  - Where: herness/core/_egress_transport.py:88-89.
  - The finalizer is never detached when `complete()` runs (:97-103). So a response that was properly closed and then freed still appends a no-op `partial(complete, ...)` to `_PENDING`.
  - Each such job holds the `_Call` (request, guard) and the `StreamCounter`. The counter keeps the body for the usage parse, up to 10 MiB for JSON.
  - All of this now lives until the next guarded request or `close()`, not just until the response is freed.
  - Probe: after 200 closed requests and a final `client.close()`, 40 no-op jobs were still queued (all `_done=True`).
  - The retention is bounded by the number of responses freed since the last drain, but the memory stays pinned indefinitely once a burst is followed by idle.
  - Fix: keep the `weakref.finalize` handle on the `_Call` and call `.detach()` in `complete()`. This also removes the queue churn.
- **m-5 `_drain` does not guard its jobs.**
  - Where: herness/core/_egress_transport.py:122-129, :133 and :172-175.
  - One raising job does three things:
    - It aborts an unrelated request before admission. The caller gets the job's exception and the log gets no line.
    - It makes `close()` raise before `self._inner.close()`, so the pool is leaked.
    - It drops the popped job.
  - Probe: a planted `RuntimeError` job gave "request raised: bad job", "lines after failed request: []", and "close raised: bad job, inner closed: False". `aclose` is exposed the same way.
  - A real `complete()` rarely raises today: `_write_completed` swallows `StoreBusy`/`OSError` and `provider_usage` catches decode errors. But the queue is process-wide, so one guard's failure lands in another caller's request.
  - Fix: `try: job() except Exception: _log.error("egress.log.failed", ...)` in `_drain`, and `try: _drain() finally: self._inner.close()` in `close`/`aclose`.
- **m-6 `not_closed` is still not in the spec.**
  - Where: impl 10 U10-54 Postconditions, docs/impl/10-config-security-deployment.impl.md:1047.
  - The postcondition still reads "when the response closes or the send fails". It does not mention the `not_closed` line, that the line is written at the next request or close rather than at gc time, or that it is lost at interpreter exit (`atexit=False`, as the report states).
  - Fix: add one sentence to U10-54 Postconditions or the algorithm's step 5.
- **Nit.** `isinstance(c, httpx2.Client)` in `herness/` is still flagged by ST10-25, which was a round-1 false positive. The ruling covered only annotations and `TYPE_CHECKING`, so this is only noted.

## Verdict
**Approved.** I5, the partial M7, M8 and M10 are fixed as ruled. T05-08's real adapter passes both ruff TID251 and ST10-25, and removing `http_client=` from it still fails the scan. No regression breaks a request, the log ordering or the lock. m-4 (memory pinned by undetached finalizers) and m-5 (unguarded drain in `close`) are cheap follow-ups worth folding into the next touch. m-1 and m-2 are deliberate-evasion gaps backed by the socket guard.
