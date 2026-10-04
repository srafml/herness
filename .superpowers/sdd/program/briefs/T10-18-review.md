# Review: T10-18 Socket guard

Reviewed range: de88922..e88be52 (commit e88be52). Read-only review, no edits made.

## Spec conformance (U10-58)

- Allowlist algorithm (R-06): PASS
  - loopback (`LOOPBACK_HOSTS`) union source hosts union egress allowlist union proxy host - `_allowlist` (egress_socket.py:181-193) matches exactly.
  - SDK sources contribute only `hosts`, never `base_url`: PASS - `_source_hosts` (line 160-178) skips `base_url` extraction via `if name in SDK_SOURCE_KINDS: continue` before reading `base_url`. `SDK_SOURCE_KINDS` imported from `herness.core.config_sources`, not redeclared - PASS.
  - `security.egress.destinations` only when `security.egress.enabled`, `security.network.extra_allowed_hosts` unconditional - PASS (lines 189-191), matches the spec's parenthetical scoping exactly (subtle point the code gets right: destinations gated, extra_allowed_hosts not).
  - proxy host derived via `urlsplit(...).hostname` and unioned - PASS.
  - profile `synth` collapses to loopback only, proxy `None` - PASS (line 183-184).
  - `herness.core` does not import `herness.connectors`: PASS, confirmed via `lint-imports` (13 contracts kept, 0 broken; `core base is closed` lists `herness.core.egress_socket`); `_source_hosts`'s full-config branch reads via `getattr`/`type(...).model_fields`, no connectors import.
- Hook correctness: PASS
  - `_hook` dispatches `socket.getaddrinfo`->`check_getaddrinfo(args[0], args[1])`, `socket.connect`->`check_connect(args[1])`, `socket.sendto`->`check_sendto(args[1])`, no-op for unmapped events, no-op when `_POLICY is None` or thread-local re-entrancy flag set (lines 152-159).
  - IPv6 tuple shapes: `_check_address` only reads `address[0]`, so both 2-tuple (IPv4) and 4-tuple (IPv6: host, port, flowinfo, scopeid) addresses work - PASS.
  - `AF_UNIX` str/bytes addresses unconditionally allowed via `isinstance(address, str | bytes)` - PASS, matches spec ("AF_UNIX addresses (str or bytes)").
  - Re-entrancy flag: thread-local (`threading.local()`), set around the internal `socket.getaddrinfo` call in `_resolve_and_cache`, checked in `_hook` - PASS.
  - Resolution cache 300 s expiry: `_RESOLVED_TTL_S: Final = 300.0`, `_fresh` drops expired entries - PASS. `_RESOLVED` mutated only under `_RESOLVED_LOCK` (`_fresh`, `_remember`) - PASS.
  - `_POLICY` swap: single atomic name rebind under `_STATE_LOCK`, read without lock in `_hook` (safe under CPython's GIL for a single attribute read) - PASS.
- Fail-closed: PASS - any host not loopback/allowlisted/resolved-cached raises `EgressBlocked` (`_blocked`); no silent passthrough branch found other than the documented "malformed address" no-op (empty tuple / non-tuple-non-str-bytes), matching the spec's implicit scope and asserted by `test_ut10_56_check_address_ignores_malformed_addresses`.
- Env vars: `HF_HUB_OFFLINE`, `HF_HUB_DISABLE_TELEMETRY`, `DO_NOT_TRACK` set to `"1"` via `os.environ.update(_ENV_VARS)` on every `install_socket_guard` call - PASS, verified by ST10-29.
- One hook per process: `_hook_installed` flag under `_STATE_LOCK`, `sys.addaudithook(_hook)` called only on first `install_socket_guard` - PASS.
- Reset behavior / test isolation: `reset_socket_guard` clears `_POLICY` and `_RESOLVED`, hook stays registered and becomes a no-op - PASS, matches U10-10. Registered into `herness.core.config._RESET_HOOKS` (line 219), same pattern as `egress.py`'s `reset_guard`, `redact.py`, `secrets.py` - the autouse `reset_herness_state` fixture picks it up automatically once the module is imported anywhere in the session. No dedicated fixture needed, as claimed.
- Log fields, no secrets: `egress.socket.blocked` logs only `host` (line 100-102); `egress.guard.installed` logs `profile`, `allowed_hosts_count` (line 215) - matches spec fields exactly, no secrets.
- Budget: `egress_socket.py` is 219 lines vs. the 220-line module-map budget - PASS (confirmed by `wc -l` and a clean `uv run python -m tools.check_module_size`).
- `.secrets.baseline`: diff is only `generated_at` plus six pre-existing `line_number` +1 shifts under `docs\impl\10-config-security-deployment.impl.md` (unrelated concurrent doc edit) - confirmed via `git show e88be52 -- .secrets.baseline`; no entries added or dropped. PASS

## Test spec conformance

| ID | Status | Notes |
|----|--------|-------|
| UT10-56 | PASS | `SocketPolicy` exercised directly: loopback, allowed-host resolve+cache with call-counter proof of skip-on-fresh, resolved-IP re-check, malformed addresses, AF_UNIX str/bytes, blocked host + masked `EgressBlocked` (`egress_id is None`), cache expiry, `_hook` dispatch incl. unmapped event and re-entrant no-op, `_source_hosts` real-settings union + two defensive fallbacks, `_allowlist` union/disabled-egress/synth cases. All assert the spec's Expected outcomes, not just "doesn't crash". |
| PT10-07 | PASS | Hypothesis property, random lowercase-alnum labels appended to a domain suffix disjoint from the one allowed host - always blocked, no possible collision with `_ALLOWED`. |
| ST10-08 | PASS | `socket.create_connection`, `http.client.HTTPSConnection`, `urllib.request.urlopen` (with `# noqa: TID251` acknowledging the banned-client lint) to a non-allowed host all raise `EgressBlocked`; guard installed via real `load_bootstrap` over a temp config tree so no real DNS happens. |
| ST10-29 | PASS | huggingface.co blocked; all three offline env vars asserted `== "1"`; autouse fixture restores env after. |
| ST10-55 | PASS | Two functions: (1) SDK base_url-not-derived + unlisted-host-blocked + listed-host-allowed, using real `load_bootstrap`/`BootstrapConfig`; (2) C20 fires for an SDK source with no `hosts`, via the real `config_checks.row_c20` (pre-existing check, not part of this diff, correctly not re-owned here). |
| IT10-15 | PASS | Real subprocess (`sys.executable -c`), profile `local`, raw-IP connect and hostname urlopen both blocked; matches spec exactly. |
| BT10-06 | PASS | `xfail(strict=False)` at function level, module-level `[integration, slow]`, matches the pre-made ruling; measurement methodology (200-iter warm-up, 10k loopback connects with/without hook, real TCP) is reasonable given real-clock jitter noted in the report. |

## Test hygiene

- Test IDs/docstrings: every test function name and docstring correctly cites its spec ID (UT10-56, PT10-07, ST10-08, ST10-29, ST10-55, IT10-15, BT10-06) - PASS.
- `pytestmark`: `tests/security/test_st10_socket.py` uses `pytest.mark.unit`, consistent with sibling `tests/security/test_st10_*.py` files (`unit` is the house convention for security tests that stay in-process); `tests/integration/test_security_socket.py` uses `pytest.mark.integration` correctly (real subprocess); `tests/unit/core/test_egress_socket.py` uses `pytest.mark.unit`. All consistent. PASS

## Verification run (executed by this reviewer)

- `PYTHONUTF8=1 uv run pytest tests/unit/core/test_egress_socket.py tests/security/test_st10_socket.py tests/integration/test_security_socket.py -q -p no:logging` -> **20 passed**.
- `uv run lint-imports` -> 13 contracts kept, 0 broken.
- `uv run mypy herness/core/egress_socket.py herness/core/egress.py` -> 0 errors.
- `uv run python -m tools.check_module_size` -> clean (no output / exit 0).
- `uv run ruff check` on all changed files (module + 4 test files) -> all checks passed.

## Findings

None rising to Critical or Important. No warnings identified - the allowlist algorithm, hook dispatch, cache/locking, reset behavior, env vars, budget and secrets-baseline diff all match the spec and the pre-made rulings exactly.

Minor (non-blocking, informational only):
- `egress_socket.py:88-91` (`_resolve_and_cache`) caches the original hostname string itself in `_RESOLVED` alongside its resolved IPs, which is slightly broader than the algorithm's literal wording ("add the addresses to `_RESOLVED`"). This is harmless: the hostname is already unconditionally allowed via `self.allowed_hosts` regardless of `_RESOLVED` membership, and caching it is exactly what lets UT10-56 prove "second call skips re-resolution." No behavior or security change; not a fix item.

## Verdict

**Approved**

## Note on write location

The review-review.md target path (D:\herness\.superpowers\sdd\program\briefs\T10-18-review.md) is outside this reviewer's worktree (D:\herness\.claude\worktrees\agent-a84dc2893ca5bcace), and the sandbox refused both a Bash heredoc and a direct Write to that path ("worktree-isolated agent... must target its own worktree"). This file was written to the scratchpad fallback location instead, as instructed.
