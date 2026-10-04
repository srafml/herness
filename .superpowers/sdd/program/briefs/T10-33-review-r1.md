# Re-review r1: T10-33 Source HTTP client

Scope: fix commit c0adde9 (56644e6..HEAD), addressing round-1 review findings Important #1,
Minor #1 (m1), Minor #2 (m2). Minor #3 (m3, the swallowed-URL-parse-error observation) is
explicitly parked and out of scope for this round.

## Findings re-checked

| # | Finding | Status | Evidence |
|---|---|---|---|
| Important #1 | `check_verify` used a different ConfigError message ("TLS CA bundle not found for source X") for a non-existent `Path` than for any other invalid `verify` value, contradicting U10-110's unconditional verbatim-text precondition | ✅ Fixed | `herness/core/_egress_source.py` `check_verify` now folds the `Path`-exists check into one `if` and falls through to the single `"TLS verification cannot be disabled for source {source}"` message for every other case (including a non-existent `Path`). Both `test_ut10_82_verify_missing_ca_bundle_is_config_error` and `test_st10_59_missing_ca_bundle_is_config_error` were updated to assert `match="TLS verification cannot be disabled"` — the fix is actually exercised, not just made and left unverified. |
| Minor #1 (m1) | `SourceHostTransport` didn't force `Accept-Encoding: identity` the way `LoopbackOnlyTransport._admit` does, leaving the `unsupported_encoding` fail-closed path unforced and untested | ✅ Fixed | `egress_clients.py` `SourceHostTransport.handle_request` now sets `request.headers["Accept-Encoding"] = "identity"` on every request before calling the inner transport, with a docstring explaining the T10-17 gzip-bomb-fix parallel. Two new tests: `test_ut10_82_request_carries_accept_encoding_identity` (asserts the outgoing request header is forced to `identity` even when the caller explicitly asked for `gzip`) and `test_ut10_82_unsupported_encoding_is_blocked` (a mock response with `content-encoding: br` despite the forced identity header raises `EgressBlocked(reason="unsupported_encoding")`, i.e. fail-closed against a source that ignores the header). Both pass. |
| Minor #2 (m2) | Only just-over-bound invalid values were tested; the inclusive edges (`timeout_s=600.0`, `max_connections=64`, `max_response_bytes=1 GiB`, and the lower edges `1.0`/`1`/`1`) were never asserted to succeed | ✅ Fixed | New `test_ut10_82_inclusive_bounds_succeed` builds a client at both the upper edges (600.0/64/1_073_741_824) and lower edges (1.0/1/1) and asserts success (checking `client.timeout` at the upper edge). Passes. |
| Minor #3 (m3) | Silent broad `except` around `httpx2.URL(base_url)` parsing swallows the real error into a generic `host_not_allowed` | Parked | Not addressed this round, as stated in the request; not re-flagged since it was already downgraded to a non-blocking observation in round 0 (fail-closed, not spec-mandated). |

## Regression check

Read the full diff (c0adde9): three files touched — `_egress_source.py` (message consolidation
only, no logic change to the accept/reject outcome), `egress_clients.py` (one new header-setting
line + docstring), and the two test files (assertion tightening + three new tests). No change to
signatures, allowlist logic, TLS setup, proxy/retries, logging, or the metric marker. The one
behavioral change (forcing `Accept-Encoding: identity`) is additive and matches the established
`LoopbackOnlyTransport` pattern from T10-17 — it does not alter any previously-passing test's
expectations (all 30 round-0 tests still pass unchanged alongside the 3 new ones).

Module sizes after the fix: `herness/core/_egress_source.py` 128/200, `herness/core/egress_clients.py`
263/300 — both still comfortably within budget.

## Commands run

- `PYTHONUTF8=1 uv run pytest tests/unit/core/test_egress_source.py tests/security/test_st10_source_client.py -q -p no:logging` → 33 passed (30 prior + 3 new)
- `uv run ruff check herness/core tests/unit/core tests/security` → All checks passed!
- `uv run mypy herness/core` → Success: no issues found in 61 source files
- `uv run python -m tools.check_module_size` → exit 0

## Verdict: Approved
