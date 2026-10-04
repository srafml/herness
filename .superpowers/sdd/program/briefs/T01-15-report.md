# T01-15 Auth — build report

Status: DONE_WITH_CONCERNS — final commit 364bc9c feat(connectors): T01-15 auth builders and token caches (all pre-commit hooks passed incl. pytest-unit)

## Files
- `herness/connectors/auth.py` (new, 280 lines; budget 280): `build_auth`, `StaticHeaderAuth`, `OAuthTokenAuth`, `MsalTokenProvider`; private `_TokenResponse`, `_OriginAuth`, `_TokenCache`, `_msal_app` (injectable MSAL factory).
- `tests/unit/connectors/_auth_data.py` (129): sentinels (all `synthetic...`, R-67), scripted mock source (`httpx2.MockTransport` via `_http_data.mock_client`), fake MSAL app + factory.
- `tests/unit/connectors/test_auth.py`: UT01-64, UT01-65, UT01-90.
- `tests/unit/connectors/test_auth_security.py`: ST01-03, ST01-15.

## Units
- U01-63 `build_auth`: dispatch per §3.13 table. `basic` and jira `api_token` -> StaticHeaderAuth with `Authorization: Basic b64(user:pass)` (test proves byte-identical to `httpx2.BasicAuth`), so the origin guard applies uniformly. dynatrace `api_token` -> `Api-Token`, `pat`/`bearer` -> Bearer, `api_and_app_key` -> DD-API-KEY / DD-APPLICATION-KEY, OAuth -> `OAuthTokenAuth(f"{base_url}/oauth_token.do")`, MSAL -> `MsalTokenProvider(scope=f"{base_url}/.default")` with client secret or `{"private_key", "thumbprint"}`. `none` -> None; `key_pair`/`connection_string` -> ConfigError("not an HTTP auth method"); missing member -> ConfigError(f"secret {name} lacks {member}"); unknown method / missing credentials / missing tenant -> ConfigError. Secrets via `secrets.resolve`/`resolve_json` only.
- U01-64 `OAuthTokenAuth`: one form POST through `token_client` (no retry_page, no loop); non-2xx -> map_http_error result, 400/401 -> AuthError; transport error -> SourceUnavailable; body validated by `_TokenResponse` (access_token SecretStr 1-8192, expires_in 1-86400, token_type str) else SchemaViolation("bad token response") (from None). threading.Lock, refresh when no token or `expires_at - margin <= clock()`; one retry after 401 (token dropped first, so a failed refetch leaves no refused token cached). DEBUG `connectors.auth.token_refreshed` with `source` only.
- U01-65 `MsalTokenProvider`: app built once lazily (inside the lock, inside the error mapping); no access_token -> AuthError(f"msal error {code}") (no description); OSError (requests' exceptions) -> SourceUnavailable(type name only, from None). Same cache/401 rule.
- Origin rule: credentials only when (scheme, host, port) equals the configured origin (base_url; token_url's origin for OAuth; scope's origin for MSAL); foreign requests get no credentials, no token fetch, no 401 retry.
- repr/str of every auth object: `ClassName(***)`.

## Tests
UT01-64 (static headers per method, OAuth form, MSAL secret/cert, none, non-HTTP methods, missing member/secret/credentials/tenant, unknown method, masked repr, foreign origin), UT01-65 (t / t+299 / t+301 + 401 retry, exact t+300 boundary, single retry, foreign origin, token endpoint 400/401/403/500, bad bodies, non-JSON, transport error, 8 threads -> 1 fetch, default clock, drop-on-401), UT01-90 (cache until exp-300, error code only, 401 retry, transport errors at acquire and at build, bad result, real factory args, log fields), ST01-03, ST01-15. UT01-66 not added (not in card's Tests row).

ST01-03 coverage: real `SyncRunner` (incremental success -> SyncResult; backfill failing with 401 -> AuthError after one refetch+retry; backfill failing with 500 -> SourceUnavailable after retry_page retries; incremental with refused token endpoint) over a test-local minimal ServiceNow-shaped connector using the real `SourceHttp` + `build_auth(oauth_password)` + temp ops store + FakeLake. Sentinels asserted absent from: capture_logs events (DEBUG included, raw, before the scrubber), exception str/repr/fields/full chained tracebacks, SyncResult repr and to_dict, lake batches and events, `sync_slice.last_error`, every byte under the ops data root (sqlite + WAL; proven scanned) and tmp_path. Carries to T01-16: the real ServiceNow connector path and its cassettes.

## Gates
ruff check/format clean; mypy --strict clean (msal imported with `# type: ignore[import-untyped]`, no pyproject change); lint-imports 13 kept; check_type_ownership 0; check_module_size 0 (auth.py 280/280); auth.py coverage 100% line / 100% branch; ST01-14 lint test passes; detect-secrets clean.

## Deviations / notes
- `OAuthTokenAuth` has an extra keyword `source: str = "servicenow"` (for the log field); MSAL logs `source="dataverse"`.
- `clock` default is a private `_now()` that reads `herness.core.time.now` per call (so a patched clock applies), equivalent to spec's `time.now`.
- The `fake_clock` fixture (freezegun) crashed the interpreter with a stack overflow in this file (freezegun module scan); tests use `FakeClock` passed as `clock` / monkeypatched `time.now` instead.
- The Basic header string (base64) is not added to `known_values` (secrets has no public API for it); the underlying user/password are.

## Process note
- The `wip(T01-15)` checkpoint commit did not land: its pre-commit run (pytest-unit hook, full unit suite: 9012 passed, ~21 min) saw working-tree edits I made to auth.py/test_auth.py during the run and pre-commit treated them as hook modifications. All work went into the single final commit; no hooks were skipped.
- RED: `pytest tests/unit/connectors/test_auth.py` -> `ImportError: cannot import name 'auth' from 'herness.connectors'`.
- GREEN: card tests 59 passed (test_auth.py 56, test_auth_security.py 3); `pytest tests/unit/connectors -q -p no:logging` 810 passed, 4 skipped.
- Line counts: auth.py 280 (budget 280); _auth_data.py 129; test_auth.py ~670; test_auth_security.py ~245.
