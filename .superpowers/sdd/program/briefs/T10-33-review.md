# Review: T10-33 Source HTTP client

Base f529d37, head 56644e6 (branch worktree-agent-a6ce4c4e128c38df8).

## Spec compliance (U10-110, T10-33 brief)

| Item | Status | Notes |
|---|---|---|
| Signature (positional `source`/`base_url`; keyword-only rest; order) | ✅ | `herness/core/egress_clients.py:412-421` matches verbatim |
| Precondition: unknown/disabled source → `ConfigError("unknown or disabled source <source>")` | ✅ | `_egress_source.py:194-202`; ruling "enabled disabled only by explicit `enabled: false`" honored given `SourceSettings.enabled: bool = False` default (`herness/connectors/settings_base.py:238`) |
| Precondition: `base_url` scheme/userinfo → `EgressBlocked` | ✅ | `_egress_source.py:217-229`, exercised via ST10-58 |
| Precondition: `verify` True or existing file, else `ConfigError("TLS verification cannot be disabled for source <source>")` | ❌ | see Important #1 below — wrong message for the "Path that doesn't exist" case |
| Precondition: `0<timeout_s<=600`, `1<=max_connections<=64`, `1<=max_response_bytes<=1 GiB`, bool/NaN rejected | ✅ | `_egress_source.py:161-178`; UT10-82 bound tests pass, including `True`/`4.0`/NaN |
| Postconditions: `follow_redirects=False`, `trust_env=False`, `timeout=Timeout(timeout_s, connect=min(timeout_s,10))`, `limits`, `auth`, `headers`, `base_url` | ✅ | `egress_clients.py:461-474`; asserted in `test_ut10_82_servicenow_client_configuration` |
| Source allowlist ruling (SDK vs non-SDK; non-SDK `hosts` widens socket guard only) | ✅ | `_egress_source.py:205-214`; matches the binding sub-controller ruling, tested by `test_st10_58_hosts_widens_only_the_socket_guard_never_the_client` and `test_st10_58_snowflake_base_url_host_not_in_hosts_is_blocked` |
| Process allowlist: installed `SocketPolicy.allowed_hosts` else `egress_socket._allowlist(cfg)[0]` | ✅ | `_egress_source.py:247-254` |
| TLS: TLS1.2 min, `CERT_REQUIRED`, `check_hostname`, certifi or CA path | ✅ | `egress_clients.py:448-451` |
| Proxy from `security.network.http_proxy`; `retries=0` | ✅ | `egress_clients.py:452-453` |
| Every request (absolute URL, next link, redirect, userinfo, scheme) checked against both allowlists before the inner transport | ✅ | `SourceHostTransport.handle_request` (`egress_clients.py:390-396`) checks before calling `self._inner.handle_request`; ST10-58 exercises all six required scenarios with zero real connects (`tests/support/egress_servers.py:30-52` genuinely blocks `socket.socket.connect`) |
| `response_too_large` while reading, content-length precheck, decoded-byte counting, compressed body handling | ✅ | reuses already-approved `open_body`/`StreamCounter` from `_egress_streams.py` (T10-17); UT10-82 tests the boundary (10 bytes allowed, 11 blocked) |
| Logs `egress.source.blocked` (source, host, reason) | ✅ | `_egress_source.py:104-109` |
| Metric marker `# T08-05: herness_socket_blocked_total{event="source_client"}` | ✅ | `_egress_source.py:107` |
| No egress JSONL line | ✅ | confirmed by inspection — no `EgressLog`/`EgressGuard` reference anywhere in the new code |
| Thread safety | ✅ | transport state is immutable frozensets set at construction; relies on `httpx2.Client`'s own thread safety |
| Circular-import workaround (function-local import) | ✅ | `_egress_source.py:121-123`, `# noqa: PLC0415`, matches the pattern used in `audit.py`/`config.py`/`redact.py` |
| `pyproject.toml` `forbidden_modules` contract updated | ✅ | `pyproject.toml:488` adds `herness.core._egress_source` |
| New private sibling `_egress_source.py` constructs no `httpx`/`httpx2` client or transport | ✅ verified explicitly | grepped the file: no `httpx2.Client(`, `httpx2.HTTPTransport(`, or `SourceHostTransport(` instantiation anywhere in it — only allowlist/precondition helpers and `EgressBlocked` construction. ST10-25 exemption list intact (`egress.py` + `egress_clients.py`); confirmed by running `test_st10_lint.py` (55 passed, only the pre-existing `test_st10_25_repository_passes` known-red on `openai_compat.py`, unrelated to this diff) |
| Test ID naming / docstring-starts-with-ID / `pytestmark` | ✅ | all 20 new test functions carry `ut10_82`/`st10_58`/`st10_59` in the name; `pytestmark = pytest.mark.unit` set in both files |
| Module sizes | ✅ | `_egress_source.py` 126/200, `egress_clients.py` 259/300, `egress.py` 385/390 (measured directly, matches report) |

⚠️ Cannot independently re-verify from the diff alone (trusted from report, spot-checked where practical): full-suite green claim (2215 passed / 1 known-red) — re-ran only the two new test files plus `test_st10_lint.py`, `ruff`, `mypy`, `lint-imports`, `check_module_size` myself, all consistent with the report.

## Commands run

- `PYTHONUTF8=1 uv run pytest tests/unit/core/test_egress_source.py tests/security/test_st10_source_client.py -q -p no:logging` → 30 passed
- `uv run ruff check herness/core tests/unit/core tests/security` → All checks passed!
- `uv run mypy herness/core` → Success: no issues found in 61 source files
- `uv run lint-imports` → Contracts: 13 kept, 0 broken
- `uv run python -m tools.check_module_size` → exit 0
- `uv run pytest tests/security/test_st10_lint.py -q` → 55 passed, 1 failed (`test_st10_25_repository_passes`, pre-existing `openai_compat.py` TID251 red, unrelated)
- Focused coverage check on the two new test files against the two touched modules: `_egress_source.py` 98% line / one branch uncovered (see Minor #2); `egress_clients.py` 56% in isolation, expected since these two files don't exercise the pre-existing loopback-client code path (out of scope for this card)

## Findings

### Critical
None.

### Important

1. **`check_verify`'s ConfigError text for a non-existent CA bundle path does not match the spec's verbatim wording.** `herness/core/_egress_source.py:181-191`:
   ```python
   def check_verify(verify: object, source: str) -> Path | None:
       if verify is True:
           return None
       if isinstance(verify, Path):
           if verify.is_file():
               return verify
           msg = f"TLS CA bundle not found for source {source}"
           raise ConfigError(msg)
       msg = f"TLS verification cannot be disabled for source {source}"
       raise ConfigError(msg)
   ```
   U10-110's Preconditions row is explicit and unconditional: "`verify` is `True` or an existing CA bundle file; **any other value**, including `False`, raises `ConfigError("TLS verification cannot be disabled for source <source>")`." A `Path` that does not point to an existing file is "any other value" under that sentence — it should raise the same verbatim message, not a different one ("TLS CA bundle not found for source X"). Neither new test catches this because both `test_ut10_82_verify_missing_ca_bundle_is_config_error` (`tests/unit/core/test_egress_source.py:151`) and `test_st10_59_missing_ca_bundle_is_config_error` (`tests/security/test_st10_source_client.py:175`) assert only `pytest.raises(ConfigError)` with no message match — so the deviation is real but silent. The builder's report claims "error reasons/messages match the brief verbatim (verified against the ST10-58/ST10-59 test rows one by one)," which is not quite accurate for this one case. Fix: use the same "TLS verification cannot be disabled for source {source}" message for both the wrong-type and missing-file branches (or fold them into one `else`).

### Minor

1. **`body_error`'s non-`response_too_large` branch is untested and produces an `EgressBlocked` reason not listed in U10-110's Errors row.** `herness/core/_egress_source.py:112-116` has a fallback `EgressBlocked(f"source client refused: {reason}", reason=reason)` for any reason other than `response_too_large` (reachable via `open_body`'s `unsupported_encoding` path, e.g. a source server responding with `br`/`zstd` content-encoding — `SourceHostTransport` does not force `Accept-Encoding: identity` the way `LoopbackOnlyTransport._admit` does). Coverage confirms this line (116) is never hit by the new tests. Not a functional bug (still fails closed as `EgressBlocked`), but it's undocumented behavior relative to the spec's four-reason Errors list and untested.
2. Boundary-exact valid values are not tested for success — only invalid values are covered for `timeout_s` (601.0 fails, but 600.0 passing isn't asserted), `max_connections` (65 fails, but 64 passing isn't asserted), and `max_response_bytes` (only 10/11-byte content-size boundary is tested, not the 1 GiB parameter bound itself). Code inspection confirms the comparisons are inclusive (`<=`/`>=`) and correct, so this is a test-thoroughness gap rather than a suspected bug.
3. `source_http_client`'s `try: httpx2.URL(base_url) except (httpx2.InvalidURL, ValueError, TypeError): url = httpx2.URL()` (`egress_clients.py:439-441`) silently swallows a malformed-`base_url` parse failure into a generic empty URL, which then surfaces as `EgressBlocked(..., reason="host_not_allowed")` rather than a more specific error. This fails closed (safe) but obscures the real cause; not required to change since the spec doesn't define a distinct "malformed base_url" precondition, but worth a comment or a `ConfigError` if a controller wants more precise diagnostics later.

### Strengths

- Source/process allowlist ruling implemented exactly as directed, with a dedicated regression test for the "hosts widens only the socket guard" case referencing UT01-97.
- Every request path (construction-time check and per-request `handle_request`) reuses the same `source_refusal` helper — no duplicated allowlist logic.
- ST10-58/ST10-59 tests are genuine: `record_connects` really monkeypatches `socket.socket.connect`/`connect_ex` and asyncio's `create_connection`, and ST10-59 uses a real local TLS server with a freshly generated self-signed certificate rather than a mock.
- Circular-import avoidance is minimal (single function-local import, documented, consistent with existing precedent) rather than a larger structural workaround.
- Module budgets respected with headroom; the new private sibling file builds no client/transport, keeping the ST10-25 exemption list unchanged as required.

## Verdict: Needs fixes
