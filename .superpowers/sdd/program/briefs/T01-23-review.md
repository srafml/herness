# T01-23 review: Snowflake connector (verify agent)

Worktree agent-a1dbbdae2d281af33, head 245bb90, base 22c64fb. Read-only review; no code edits.

### Spec Compliance
- ❌ Issues found (2 Important, see Issues):
  - U01-88 Algorithm step 1 / §3.13 shapes table (01-connectors.impl.md:1774): the `key_pair` secret is JSON `{"user", "private_key_pem", "passphrase"?}`. The connector reads `private_key` instead (snowflake.py:169-173), and the tests store `private_key` too (_snowflake_data.py:79). A credential written to the documented shape is refused with ConfigError.
  - U01-89 / TH01-07: the configured `filter` is spliced into a statement that the driver formats client-side with pyformat (`command % params`). A filter containing `%` (for example `NAME LIKE 'A%'`) raises a raw `TypeError` whenever the window binds a value. That error is not a driver `sfe.Error`, so it is not mapped.
- Per unit / test row:
  - U01-88 SnowflakeConnector ❌ (one item): constructor (settings, clock, connect=None → `snowflake.connector.connect`) ✅ snowflake.py:142-153; `register("connector","snowflake")` ✅ plus the static `_BUILTINS` row (registry.py:41) ✅; name/entities/watermark_field ✅; check (`SHOW WAREHOUSES LIKE %(wh)s`; empty, "null", None or absent monitor → ConfigError) ✅ :195-207; lazy connect with the exact kwargs (DER PKCS#8, session params, timeouts, application, ocsp_fail_open) ✅ :162-189, but the credential member name is wrong ❌; `_scan_guard` ✅ :209-222; `_map_sf_error` exactly as in step 3 ✅ :72-83; identifiers upper-cased and quoted, values bound ✅.
  - U01-89 sync/list_keys ❌ (one item): QUERY_TAG bound for every statement ✅; SELECT/WHERE/ORDER BY shape, naive-UTC bounds, absent bounds omitted ✅ :245-271; guard before the SELECT ✅; `retry_page(..., source="snowflake")` + `fault_point("http.page")` ✅ :232-236; `to_batches(max_chunksize=batch_rows)` ✅; to_snake names with Arrow types kept, `_source_key` cast (null → SchemaViolation), `parse_arrow_timestamps`, `_payload` json.dumps(default=str) ✅ :277-308; errors during iteration mapped and not resumed ✅ :237-239; list_keys KEY_SCHEMA batches ✅ :310-319. Filter `%` handling ❌.
  - UT01-86 ✅ (3 Arrow tables 1500/700/2300, batch_rows 1000 → [1000,500,700,1000,1000,300]; exact call log tag → EXPLAIN → SELECT with bound params; types kept).
  - UT01-87 ✅ (60 GB vs 50 → exact ConfigError text, no SELECT; check without a monitor → ConfigError, parametrised over "", "null", None, absent, "NULL").
  - UT01-88 ✅ (28000 → AuthError at connect, 57014 → RateLimited(None), network → SourceUnavailable on execute and mid-fetch; full mapping parametrised; no driver text).
  - ST01-16 ✅ (10× max_scan_gb → call log is ALTER and EXPLAIN only; the credential is not on the instance, not logged and not in the message, context, hint, details or `__cause__`).
  - UT01-94 ✅ (factory test enables snowflake, built set includes it and it satisfies SupportsKeyListing; the "not configured" probe moved to dataverse, which has no _BUILTINS row and no config, so the intent is kept; new real-class `_BUILTINS` test per the w21-s01b ruling).
  - Threats: TH01-17 ✅ (guard, statement timeout, monitor check); TH01-03 ✅; TH01-07 ✅ for identifiers and values (filter validated by U01-11, no `;`, `--`, comments or DML words); TH01-18 ✅. The connector never derives a host: `SnowflakeSettings.hosts_error` requires `<account>.snowflakecomputing.com` in `hosts` (ST10-55), `snowflake` is in SDK_SOURCE_KINDS (config_sources.py:27), and the driver reaches the network only through the process socket guard. There is no httpx anywhere in the module.
- ⚠️ Not verifiable in unit scope:
  - Real-driver behaviour: the EXPLAIN JSON shape, the SHOW WAREHOUSES column names, the Arrow types of NUMBER and TIMESTAMP_TZ, and the 100k rows/s target (no bench in this card; `_payload` is per-row `to_pylist` + json.dumps).
  - A 57014 statement timeout is mapped to RateLimited, which `retry_page` retries. Each retry re-runs a statement of up to `statement_timeout_s`, so credit spend is bounded only by the `source_http_page` policy's max_elapsed. This is spec-mandated; flag it to the V-6 freeze.

### Evidence (re-run by verifier)
- `PYTHONUTF8=1 uv run pytest tests/unit/connectors -q -p no:logging`: 689 passed, 4 skipped (environment skips).
- Card filter `-k "UT01_86 or UT01_87 or UT01_88 or ST01_16 or UT01_94"`: 59 passed, 634 deselected.
- tests/security/test_st10_lint.py, test_st10_socket.py, tests/unit/core/test_registry.py: 68 passed.
- `ruff check .` passes and `ruff format --check` is clean. `mypy` reports no issues (306 files). `tools.check_module_size` rc 0. snowflake.py is 319/340 lines and registry.py 145/160.
- Coverage herness.connectors.snowflake: 186 stmts, 42 branches, 100% line and branch.

### Focused checks (one per named risk)
- Risk: credential member name drifts from the spec. Check: grep `private_key_pem` → only spec §3.13 table 01-connectors.impl.md:1774 names it, and no code or test uses it. Confirmed → Important 1.
- Risk: the filter collides with pyformat client-side binding. Check: the driver's snowflake/connector/cursor.py:807-833 does `query = command % processed_params` when params is non-empty, outside its try/except. Probe (.agent-tmp/verify-T01-23/probe_pct2.py) of `... >= %(since)s AND (NAME LIKE 'A%') ...` % params gives `TypeError: not enough arguments for format string`. With empty params (list_keys, or sync with no bounds) the text is sent unformatted, so writing `%%` cannot fix both paths. FakeCursor records SQL without formatting, so the tests cannot see this. The U01-11 filter validator does not forbid `%`. Confirmed → Important 2.
- Risk (V-6): key-pair auth failures surface as 08001 and are retried as SourceUnavailable. Check: in the installed driver 4.7.5, auth/_auth.py:478-486 sets sqlstate 28000 for every GS code in CREDENTIAL_REJECTION_GS_CODES (network.py:157-165: 390100 AUTHORIZATION_FAILURE, 390144 JWT_TOKEN_INVALID, 394300-394304 JWT invalid, expired or fingerprint mismatch). An invalid key-pair login therefore becomes DatabaseError 28000 → AuthError, as the spec intends. 08001 remains for HTTP 403 (wrong account), 502 and 504 (auth/_auth.py:291-316), which are reasonably SourceUnavailable. The builder's concern is mostly unfounded for the installed driver. The residual risk is the version floor: pyproject.toml:39 pins `snowflake-connector-python[pandas]>=3.12`, and older drivers may not map rejections to 28000 → Minor 2.
- Risk: the credential leaks through the exception chain. `_driver` raises `from None` (snowflake.py:86-91). `__cause__` is None and the context is suppressed, but `__context__` keeps the driver error (same as T01-22 Minor 1, accepted there). The ST01-16 tests assert message, context, hint, details and `__cause__`. `cred` and `der` are locals only; `vars(conn)` holds neither (tested).
- Risk: an identifier comes from data. Every identifier comes from SnowflakeEntity (`_IDENT` `^[A-Za-z_][A-Za-z0-9_$]{0,254}$`, `table` exactly three such parts, key and updated in columns), so it cannot contain `"`. Result column names only become lake names via to_snake; a leading `_` becomes `f_`, so they cannot shadow metadata columns; collisions → SchemaViolation.
- Rows bounded: every yielded batch is ≤ batch_rows (to_batches max_chunksize), with one Arrow result chunk in memory, per the runner contract.
- Fake: in-memory only, no socket or bind (fake_snowflake.py). The w17-s11 loopback rule is met.

### Strengths
- Close to the spec letter: the exact connect kwargs, the error mapping and the guard message are each asserted. Driver text is never surfaced, and the keys are generated at run time, so there is no key material in the tree.
- The UT01-86 call-log assertions prove the order tag → EXPLAIN (same SQL and params) → SELECT. Retry re-issues the identical statement, and a mid-fetch failure is not retried in place.
- Hardening is sound: the exact-name check defends against LIKE wildcards, keys are checked with RE2 per the record_id rule, binary keys that are not UTF-8 → SchemaViolation, and coverage is 100%.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. herness/connectors/snowflake.py:169-173 (also tests/unit/connectors/_snowflake_data.py:79, and the message at :171): the key-pair secret member is `private_key`, but the binding secret-shape table (docs/impl/01-connectors.impl.md:1774, §3.13) defines `key_pair` as JSON `{"user", "private_key_pem", "passphrase"?}`. Operators who store the documented shape get `ConfigError("snowflake credential must hold user and private_key")` and the source cannot connect. Builder deviation 4 claims the spec names only `cred.user`, which is incorrect. Fix: read `private_key_pem` (and `passphrase`), update the message and the test helper, and add a test that stores the documented shape.
2. herness/connectors/snowflake.py:241-243 with :211, :234 (and :315 list_keys): `cfg.filter` is inserted verbatim into SQL that the snowflake driver formats with Python `%` when params is non-empty (driver cursor.py:824-833). A valid filter with a `%` literal (`NAME LIKE 'A%'`) makes sync, and its EXPLAIN, raise an unmapped raw `TypeError`/`ValueError` from inside the driver, so the source fails permanently with a non-Herness error. With empty params (list_keys, or sync with no bounds) the same text is sent unformatted, so users cannot work around it consistently. The fake never formats SQL, so no test can catch it. Fix: escape `%` in the filter as `%%` and make every statement go through formatting consistently (for example always pass a non-empty mapping, or escape only when params is non-empty). Alternatively, reject `%` in the U01-11 filter validator via a ruling. Add a test whose fake applies the driver's formatting rule (`sql % params` when params is non-empty).

#### Minor (Nice to Have)
1. herness/connectors/snowflake.py:86-91: `raise ... from None` keeps the driver exception on `__context__` (suppressed, not rendered). This was accepted for T01-22; raising after the except block would clear it.
2. pyproject.toml:39 (`snowflake-connector-python>=3.12`) vs V-6: AuthError for real key-pair rejections depends on the driver mapping CREDENTIAL_REJECTION_GS_CODES to 28000 (present in the installed 4.7.5). Either raise the floor to a version known to do this or add errno 390144/394300-394304 handling at the V-6 freeze. HTTP 403 "verify account name" (08001 → SourceUnavailable, retried) could arguably map to ConfigError at V-6.
3. herness/connectors/snowflake.py:288: `json.dumps(row, default=str)` emits `NaN`/`Infinity` for float columns, which is not valid JSON in `_payload`. Edge case.
4. herness/connectors/snowflake.py is at 319/340 lines; the fixes above should fit, but there is little headroom.

### Deviations (builder's 7)
1. list_keys also runs QUERY_TAG and the scan guard: ACCEPTABLE. The U01-89 invariant puts the tag "for every statement of the entity", and TH01-17 covers all Snowflake queries.
2. check() keeps only the SHOW row whose name equals the warehouse: ACCEPTABLE. `LIKE` treats `_` as a wildcard, so this is strictly safer; no row yields the same ConfigError (tested).
3. `connect: Callable[..., Any]` with internal Protocols: ACCEPTABLE. A typing-only change; the default is still `snowflake.connector.connect`.
4. Credential keys `user` / `private_key` / `passphrase`: NOT ACCEPTABLE as built. The spec (§3.13, impl:1774) names `private_key_pem`, see Important 1. The ConfigError mapping for a missing member or unreadable key is fine.
5. An unparseable EXPLAIN plan (not JSON, or no row) → SchemaViolation: ACCEPTABLE. It is the same failure class as "bytesAssigned absent" and fails closed before the SELECT.
6. `until=None` drops the upper bound: ACCEPTABLE. The connector Protocol (w12-s01) allows `until=None`, the same as T01-22.
7. A key with a control char, more than 512 chars or not castable → SchemaViolation, and to_snake name collisions → SchemaViolation: ACCEPTABLE. This follows the record_id rule and the tombstone_batch check, and fails closed.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The scan guard, error mapping, credential hygiene, egress posture and registry row all meet the spec, with 100% coverage and clean gates. However, the connector reads the wrong secret member (`private_key` instead of the spec's `private_key_pem`), and any filter containing `%` fails with an unmapped driver TypeError. Both break real deployments and need a fix plus tests.


---

## Re-review round 1 (head 836f1a3, fix on top of 245bb90)

Scope: Important 1 and Important 2 from the review above. Read-only; probes live under .agent-tmp/verify-T01-23/.

### Findings status
- Important 1 (key_pair secret shape) is CLOSED. snowflake.py:165-173 now requires `user` and `private_key_pem` (plus an optional `passphrase`), exactly as §3.13 (impl:1774) specifies. The message now reads "snowflake credential must hold user and private_key_pem". `store_credential` writes the documented shape. The new `test_ut01_86_documented_key_pair_secret_shape` connects with a plain PEM and with an encrypted PEM plus `passphrase` (both pass the DER key). It also proves a `private_key`-only credential is refused with ConfigError and that no connect happens. Deviation 4 is now conforming.
- Important 2 (`%` in the filter) is CLOSED. `_where` (snowflake.py:237-243) doubles `%` in the filter only when a window value is bound. Bounds are non-empty exactly when params is non-empty: params are only `since`/`until`, and identifiers cannot contain `%` (`_IDENT`). The server therefore always receives the filter as written.
  - Check that the fake mirrors the driver: the installed snowflake-connector 4.7.5 runs `cursor.py:824-833`, which applies `command % processed_params` only when processed params are non-empty. `interpolate_empty_sequences` defaults to False (connection.py:342). The fake's `_bind` (fake_snowflake.py) applies `sql % {quoted values}` only for non-empty params, so the rule matches.
  - Real-driver probe (.agent-tmp/verify-T01-23/probe_real_bind.py) ran the connector against the driver's own `SnowflakeCursor._preprocess_pyformat_query` with filter `NAME LIKE 'A%'`. For bounded sync, open sync and list_keys, the ALTER, EXPLAIN and SELECT texts sent were all well-formed. Each SELECT and EXPLAIN contains exactly `(NAME LIKE 'A%')`, and the datetime literals are rendered `'2026-01-01 00:00:00'`, the same as the fake. There was no TypeError.
  - Tests: `test_ut01_86_percent_in_filter_reaches_the_server_verbatim` covers sync with bounds, sync without bounds and list_keys, asserting the EXPLAIN and SELECT text for each. `test_st01_16_scan_guard_explains_the_percent_filter_as_sent` checks that the EXPLAIN text equals the SELECT actually sent, with `'%A%'`.

### No regression (re-run by verifier)
- `PYTHONUTF8=1 uv run pytest tests/unit/connectors -q -p no:logging`: 692 passed, 4 skipped.
- Card filter `-k "UT01_86 or UT01_87 or UT01_88 or ST01_16 or UT01_94"`: 62 passed.
- `ruff check .` passes and `ruff format --check .` is clean (run with `--extend-exclude .agent-tmp`; the only untracked files are this verifier's own probe scripts, which are not committed). `mypy`: no issues (306 files). `tools.check_module_size`: rc 0. snowflake.py is 322/340 lines.
- Coverage herness.connectors.snowflake: 187 stmts, 42 branches, 100% line and branch.

### Remaining
- Critical: none. Important: none.
- The earlier Minor items (1: `__context__` kept; 2: driver version floor vs V-6; 3: NaN/Infinity in `_payload`; 4: budget headroom, now 18 lines) stand as non-blocking notes.

### Assessment (round 1)
**Task quality:** Approved
**Reasoning:** Both Important findings are fixed and tested. The fake's binding rule matches the installed driver, and a probe through the driver's real pyformat path confirmed it. All gates are green with no regression.
