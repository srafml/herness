# T01-15 Auth — verify review (head 364bc9c, base e41d62c)

**Verdict: Approved**

### Spec Compliance
- ✅ U01-63 `build_auth` / `StaticHeaderAuth` — every §3.13 row dispatched correctly (auth.py:216-280):
  - ✅ basic `{username,password}` -> `Authorization: Basic b64(u:p)` (StaticHeaderAuth, byte-identical to httpx2.BasicAuth, ruling 4; test_auth.py:123)
  - ✅ api_token (jira) `{email,token}` -> Basic; api_token (dynatrace) plain -> `Api-Token <t>`; pat/bearer -> `Bearer <t>`
  - ✅ api_and_app_key -> `DD-API-KEY` / `DD-APPLICATION-KEY`
  - ✅ oauth_client_credentials / oauth_password -> `OAuthTokenAuth(f"{base_url}/oauth_token.do")`, right members + grant_type
  - ✅ msal_client_credentials -> client_secret, or `{"private_key": certificate_pem, "thumbprint"}`; scope `f"{base_url}/.default"`; authority `https://login.microsoftonline.com/{tenant}`
  - ✅ missing member -> `ConfigError(f"secret {name} lacks {member}")` (name via SecretRef.parse, never a value); key_pair/connection_string -> `ConfigError("not an HTTP auth method")`; none -> None
  - ✅ secrets only via `secrets.resolve`/`resolve_json` at build time; held as SecretStr inside auth objects only; repr/str `ClassName(***)`
- ✅ U01-64 `OAuthTokenAuth` — lock; refresh when `expires_at - margin <= clock()` (boundary t+300 refreshes, tested); exactly one POST via token_client, no retry_page/loop (ruling 2); non-2xx -> map_http_error, 400/401 -> AuthError; transport -> SourceUnavailable `from None`; `_TokenResponse` limits 1-8192 / 1-86400 / token_type, `hide_input_in_errors`, failure -> `SchemaViolation("bad token response") from None`; single 401 retry with token dropped first; stale-identity check avoids a redundant refetch when another thread already refreshed; DEBUG `connectors.auth.token_refreshed` with `source` only.
- ✅ U01-65 `MsalTokenProvider` — app built once under the lock; no access_token -> `AuthError(f"msal error {code}")` (no description); OSError (requests.RequestException is an OSError — verified) -> SourceUnavailable with type name only, `from None` (ruling 3); same cache/401 rule.
- ✅ Origin guard (ruling 4): (scheme, lowercased host, port) compare; httpx2 normalises default ports (probed: `https://sn.example:443` -> port None, `http://x:80` -> None); foreign origin gets no credentials, no token fetch, no 401 retry (tested for scheme/host/port).
- ✅ Thread safety: StaticHeaderAuth stateless; token caches lock-guarded; 8-thread test -> 1 fetch. No deadlock risk: SourceHttp passes `auth=` per request (http.py:283), token_client has no client-level auth.
- ✅ No HTTP client constructed outside egress in herness/ (tests use `_http_data.mock_client`); ST01-14 lint untouched (diff = 4 files).
- Tests:
  - ✅ UT01-64 — headers per table for every method, OAuth form, MSAL secret/cert, none, non-HTTP methods, missing member per JSON method (message exact + no sentinel), missing secret, unknown method, masked repr, foreign origin.
  - ✅ UT01-65 — expires_in 600, t / t+299 (1 fetch) / t+301 (refresh) / 401 -> one refetch + retry; exact t+300 boundary; second 401 not retried; 400/401/403/500 mapping; bad bodies; transport; threads.
  - ✅ UT01-90 — cached until exp-300, error result -> AuthError code only (description containing a sentinel asserted absent), 401 retry, transport errors, real factory args.
  - ✅ ST01-03 — real SyncRunner + SourceHttp + build_auth(oauth_password) over test-local connector (ruling 5): 401, 500 and refused token endpoint; sentinels asserted absent from captured logs (pre-scrubber, DEBUG), exception str/repr/fields/chained tracebacks, SyncResult repr+to_dict, lake batches/events, sync_slice.last_error, every byte under the ops data root (proven scanned) and tmp_path.
  - ✅ ST01-15 — OAuth and MSAL: token expired by frozen clock refreshed before use; tokens absent from logs, repr/str and object vars.
  - ✅ IDs in names (`test_ut01_64_...`) and docstring first lines; `pytestmark` set in both test files.
- ⚠️ The card's acceptance check literally as written (`pytest -k "UT01-64 or ..."`, hyphens) selects 0 tests (exit 5) — program-wide convention issue (global constraints use the underscore form); `-k "UT01_64 or UT01_65 or UT01_90 or ST01_03 or ST01_15"` selects 59, all pass.
- ⚠️ Real MSAL network behaviour (instance discovery through the socket guard, R-06) not exercisable in unit tests; covered by the injected factory only.

### Gates (run by verifier)
- `uv run ruff check .` — All checks passed
- `uv run ruff format --check .` — 910 files already formatted
- `uv run mypy` — Success, 334 files
- `uv run lint-imports` — 13 kept, 0 broken
- `uv run python -m tools.check_module_size` — exit 0 (auth.py 280/280)
- `PYTHONUTF8=1 uv run pytest tests/unit/connectors -q -p no:logging` — 810 passed, 4 skipped (pre-existing env skips)
- Card tests with coverage: 59 passed; auth.py 100% line / 100% branch

### Strengths
- Leak hygiene is thorough: every error constructed outside `except` or with `from None`; pydantic `hide_input_in_errors`; error-body (with a sentinel in `error_description`) proven not to reach messages.
- 401 retry drops the refused token before refetch, so a failed refetch never leaves a refused token cached (tested).
- ST01-03 asserts on raw (pre-scrubber) log events, i.e. it tests the code, not the scrubber.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
- auth.py:46-47 — `_origin` relies on httpx2's default-port normalisation, which does not happen for an upper-case scheme (probed: `HTTPS://sn.example:443` -> port 443, vs `https://sn.example` -> None). Fails closed (no credentials sent), so no leak, but a config `base_url` written as `HTTPS://host:443` would never authenticate. Normalise `port or {"http": 80, "https": 443}.get(scheme)`.
- auth.py:280 — `build_auth` does not pass `source=source` to `OAuthTokenAuth`; log field is always the default "servicenow". Correct today (only ServiceNow uses OAuth) but implicit; also `source` kwarg is an extra beyond the spec constructor (documented deviation).
- auth.py:224 / auth.py:114 — the Basic header (base64) and fetched access tokens are never added to `secrets.known_values`, so the spec-10 scrubber ("last line", TH01-03) cannot mask them if some future code logs a header. No current path logs them (verified by ST01-03/ST01-15); consider a public secrets hook to register derived values.
- auth.py:195-201 — non-OSError exceptions from MSAL (e.g. ValueError on an unparsable certificate PEM at app build) propagate raw rather than as ConfigError/AuthError; messages from cryptography carry no key material, but the error is outside the taxonomy.
- tests: spec §11 names `freezegun`; tests use `FakeClock` passed as `clock` (documented: freezegun crashed in this file). Behaviourally equivalent "frozen time"; acceptable.
- Verifier note: the coverage run left/updated the git-ignored `.coverage` file in the worktree (ignored, no tracked change).

### Assessment
**Task quality:** Approved
**Reasoning:** All three units and every shapes-table row match the spec and accepted rulings; tests assert what UT01-64/65/90, ST01-03 and ST01-15 require, gates are green and auth.py has 100% line/branch coverage. Only minor hardening items remain.
