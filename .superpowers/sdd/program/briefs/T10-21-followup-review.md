# T10-21 follow-up review: 8f31e86 "test(T10-21-followup): make ST10-54/ST10-55 hermetic"

Reviewer: verify agent (read-only). Range 19b58a5..8f31e86; only `tests/security/test_st10_socket.py` changed.
Worktree: `D:\herness\.claude\worktrees\agent-af7d6dcf70bba364f` (the builder's uncommitted T10-21 fix-round edits to conftest/secret_leak/security_e2e/st10_secret_leak were present and left alone; the file under review has no uncommitted changes).

**Verdict: Approved** (0 Critical, 0 Important, 3 Minor)

## What changed

ST10-55 (`test_st10_55_sdk_base_url_never_allowlisted_hosts_key_admits_it`, test_st10_socket.py:102-137):
after installing the guard from the config that lists the host, the test stubs `socket.getaddrinfo`. It returns
TEST-NET-1 `192.0.2.55` for `acme.snowflakecomputing.com` only and passes every other name through to the real
function. It then calls `es._POLICY.check_getaddrinfo("acme…", 443)`, which previously used real DNS, and adds
`assert es._fresh(_ACME_IP)`. ST10-54 is unchanged.

## Assertions, before and after

| # | Assertion (spec ST10-55 / TH10-48) | Before | After |
|---|---|---|---|
| 1 | `acme…` (SDK `base_url` host) not in `boot.source_hosts` when `hosts` omits it | ✅ | ✅ unchanged (l.107) |
| 2 | Guard installed from that config: `create_connection` to `acme…` raises `EgressBlocked` | ✅ | ✅ unchanged, and it runs before the stub is installed |
| 3 | Same for `evil.snowflakecomputing.com` | ✅ | ✅ unchanged |
| 4 | With `hosts: [acme…]`: `source_hosts == ("acme…",)` | ✅ | ✅ unchanged |
| 5 | Listed host admitted: `check_getaddrinfo("acme…",443)` does not raise | ✅ (real DNS) | ✅ (stubbed resolution) |
| 6 | Admit path really resolved and cached the address for the later connect check | n/a | ✅ **new** (`_fresh("192.0.2.55")`, l.131). This makes the test stronger. |
| 7 | Unlisted sibling `evil…` still raises `EgressBlocked` under the listed config | ✅ | ✅. With the stub installed it goes through `real_getaddrinfo`, the real audited C call. |

No assertion was removed or weakened.

## Question-by-question

1. **Stub scope and masking.** The stub matches the exact string `"acme.snowflakecomputing.com"`. Every other name goes through `real_getaddrinfo`, which raises the `socket.getaddrinfo` audit event. The guard hook refuses it there, before any lookup. The stub cannot hide a guard bug:
   - The allowlist decision (`name in self.allowed_hosts`, egress_socket.py:124) runs before `_resolve_and_cache` (egress_socket.py:126) reaches the stub.
   - All refusal assertions (2, 3) run before the stub exists. Assertion 7 uses the real audited path.
   - The stub is only reached for a host the guard has already decided to admit.

   `socket.getaddrinfo` is looked up on the module at call time (egress_socket.py:86), so the monkeypatch takes effect. It is undone at teardown. The `_RESOLVED` cache is cleared before and after each test by the autouse `reset_herness_state` → `reset_config` → `reset_socket_guard`, so a stale cached `acme…` entry cannot skip resolution and fail #6.
2. **Is any real network still touched?** No. An audit-hook recorder (probe plugin) over ST10-55 saw exactly three `socket.getaddrinfo` events: `acme…` and `evil…` refused (pre-stub), and `evil…` refused (post-stub). All three were blocked in the hook, which fires at the top of CPython's `socket_getaddrinfo`, before resolution. It saw zero `socket.connect` events.
   - The admitted path never opens a socket. The test calls `check_getaddrinfo` directly and never calls `create_connection` for `acme…`, so no TCP connect to 192.0.2.55 is attempted.
   - `timeout=1` only appears on calls that raise synchronously in the audit hook, so it is never reached.
   - No timing dependence remains. The 300 s `_RESOLVED` TTL is far longer than the few microseconds between the admit and the `_fresh` check.
3. **ST10-54 left unchanged.** The builder's claim holds. The audit recorder over the three ST10-54 tests saw only `127.0.0.1:<ephemeral>` getaddrinfo/connect events. `example.org` is refused by `LoopbackOnlyTransport` without any DNS. `loopback_stub()` binds and listens inside the `ThreadingHTTPServer` constructor, so there is no startup race. The only timing knob left is `timeout_s=5.0` on the loopback client. The ledger flake (w15-s11: `test_st10_54_real_request_line_has_no_egress_line`, once under full-suite load) came with no traceback, so I can't isolate a deterministic cause either. My view: leaving it unchanged is acceptable, but it should stay on the carry-over list (see m3).

## Findings

**Critical:** none.
**Important:** none.

**Minor**
- m1 `tests/security/test_st10_socket.py:130-131`: the admit half ("second: allowed" in spec ST10-55) is still shown at the getaddrinfo check only. Adding `es._POLICY.check_connect((_ACME_IP, 443))` (no raise) would cover the connect side of the admission. It is hermetic (a policy call, no socket) and would further strengthen the test. `assert es._fresh("acme.snowflakecomputing.com")` could be added as well. This is optional; the behaviour was the same before the change.
- m2 `tests/security/test_st10_socket.py:131`: this relies on the private `es._fresh` (next to the existing `es._POLICY` use at l.129-130). This is acceptable test-only white-boxing, but it couples the test to the cache internals.
- m3 `tests/security/test_st10_egress_tls.py:123-128` (ST10-54): the flake's root cause is still unknown. A non-weakening hardening option is a larger `timeout_s` (e.g. 30 s) on the loopback clients in l.95/114/126. Load latency would then show up as slowness rather than failure. Keep the w15-s11 carry-over open until a failure trace is captured.

## Probes (plugins under `.agent-tmp/t1021-fu-verify/`, loaded with `-p`, deleted afterwards; no tracked file edited)

| Mutant | Result on ST10-55 |
|---|---|
| Guard admits every host (`check_getaddrinfo`/`check_connect`/`check_sendto` no-ops) | ❌ red: `DID NOT RAISE EgressBlocked` |
| Guard admits every host at getaddrinfo only (resolve and cache anything) | ❌ red: `DID NOT RAISE EgressBlocked` |
| Hosts derived from base_url, config level (`cs.SDK_SOURCE_KINDS = frozenset()`) | ❌ red: `'acme…' not in ('acme…',)` |
| Hosts derived from base_url, guard level (`es._source_hosts` adds `acme…`; `boot.source_hosts` clean) | ❌ red: `DID NOT RAISE EgressBlocked` |
| Admit without resolving or caching (skip `_resolve_and_cache`) | ❌ red: `_fresh('192.0.2.55')` False. Only the new assertion catches this; the old test would have passed. |

All mutants were killed.

## Determinism runs

- `PYTHONUTF8=1 uv run pytest tests/security/test_st10_socket.py tests/security/test_st10_egress_tls.py -q`: 5/5 green (10 passed each).
- Under load (the same pair run in a loop while 2, then 3, full `tests/security` runs went in parallel): 18/18 green. All five parallel `tests/security` runs, which include ST10-54/55, were green: 646 passed, 1 skipped, 1 xfailed (the pre-existing T10-21 ST10-14 marker).
- `uv run pytest --require-test-ids -k "ST10_55 or ST10_54" -q`: 16 passed, 8109 deselected.
- `ruff check`, `ruff format --check` and `mypy` on the file: clean.
