# T01-23 report — Snowflake connector

Worktree: D:\herness\.claude\worktrees\agent-a1dbbdae2d281af33 (branch worktree-agent-a1dbbdae2d281af33, base 22c64fb)
Commits: 6e31f45 `wip(T01-23): snowflake connector, fake cursor, tests, registry row` (all hooks incl. pytest-unit, no SKIP);
final `feat(connectors): T01-23 Snowflake connector` (SHA in the reply / git log).

## Built
- herness/connectors/snowflake.py (319/340; ENG 400) — `SnowflakeConnector` (U01-88, U01-89), `register("connector","snowflake")`.
  - Constructor `(settings, *, clock=clock.now, connect=None)`; default `snowflake.connector.connect`.
  - Lazy connection on first use: `resolve_json(auth.credentials)` -> `user`, `private_key` (PEM), optional `passphrase`;
    PEM loaded with `load_pem_private_key` and passed as DER PKCS#8; connect kwargs exactly per U01-88 step 1
    (account, user, private_key, warehouse, role, session_parameters {STATEMENT_TIMEOUT_IN_SECONDS, TIMEZONE=UTC},
    login_timeout/network_timeout = timeout_s, application="herness", ocsp_fail_open=True). Credential only in locals.
  - `_scan_guard`: `EXPLAIN USING JSON <sql>` with the same params; missing/unparseable `GlobalStats.bytesAssigned`
    -> SchemaViolation; `> max_scan_gb * 2^30` -> `ConfigError(f"scan guard: {gb:.1f} GB exceeds max_scan_gb")`.
  - `_map_sf_error` per step 3 (28000 -> AuthError; Operational/Interface -> SourceUnavailable; Programming 57014 ->
    RateLimited(retry_after=None); other Programming -> ConfigError("snowflake query rejected"); anything else ->
    SourceUnavailable). Every driver call goes through `_driver`, raising `from None`, no driver text/context.
  - `sync`: `ALTER SESSION SET QUERY_TAG = %(tag)s` ('herness:<entity>'), guard, SELECT with quoted upper-case
    identifiers, bound naive-UTC since/until (bound omitted when None), `AND (<filter>)`, `ORDER BY upd, key`;
    executed under `retry_page(..., source="snowflake")` with `fault_point("http.page", source="snowflake")`;
    `fetch_arrow_batches()` -> `to_batches(max_chunksize=batch_rows)`; METADATA_SCHEMA columns first, then result
    columns renamed `to_snake` with their Arrow types kept; `_source_key` cast to string (null / unusable -> SchemaViolation),
    `_source_updated_at = parse_arrow_timestamps(...)`, `_payload = json.dumps(row, default=str)` (Decimal as string).
    Errors while reading batches are mapped (not retried in place; the job retry restarts the query).
  - `list_keys`: tag, guard, `SELECT <key> FROM <table> [WHERE (<filter>)] ORDER BY <key>`, KEY_SCHEMA batches <= batch_rows.
  - `check`: `SHOW WAREHOUSES LIKE %(wh)s`; resource_monitor empty/`null`/None/absent -> ConfigError("warehouse has no resource monitor").
- tests/support/fake_snowflake.py — in-memory fake connect/connection/cursor with a call log, scripted EXPLAIN bytes,
  SHOW rows, Arrow tables, per-statement-kind error queues, mid-fetch error. Opens/binds no socket at all.
- tests/unit/connectors/_snowflake_data.py (RSA key generated at run time — no key material in tree, no baseline change),
  test_snowflake.py (UT01-86/87/88), test_snowflake_security.py (ST01-16), conftest `snowflake_env` fixture.
- herness/core/registry.py `_BUILTINS` row ("connector","snowflake") -> herness.connectors.snowflake:SnowflakeConnector (145/160).
- tests/unit/connectors/test_connector_factory.py (UT01-94): synthetic config enables snowflake, `cfg` registers
  SnowflakeConnector, built set includes it (SupportsKeyListing); "not configured" probe moved to `dataverse` (no
  _BUILTINS row, not configured); new `test_ut01_94_snowflake_resolves_through_the_builtin_table`.

## RED / GREEN
- RED: `pytest tests/unit/connectors/test_snowflake.py test_snowflake_security.py` -> collection error
  `ModuleNotFoundError: No module named 'herness.connectors.snowflake'`. GREEN after implementation: 44 passed.
- RED (UT01-94 row): registry row removed -> `KeyError: ('connector', 'snowflake')` in the new builtin test; GREEN with row.
- RED (hardening, final commit): binary non-UTF-8 key -> `pyarrow.lib.ArrowInvalid: Invalid UTF8 payload`; GREEN -> SchemaViolation.
- Card tests: `-k "UT01_86 or UT01_87 or UT01_88 or ST01_16 or UT01_94"` all pass; `pytest tests/unit/connectors -q` -> 688 passed, 4 skipped
  (before the last test was added; +1 after). tests/security/test_st10_lint.py, test_st10_socket.py, tests/unit/core/test_registry.py: 68 passed.
- Coverage herness.connectors.snowflake: 100 % line, 100 % branch (186 stmts, 42 branches).

## Gates
ruff check . clean; ruff format --check . clean; mypy 0 issues (306 files); lint-imports 13 kept 0 broken;
check_type_ownership 0; check_module_size 0. snowflake.py 319/340.

## Deviations (spec-based reasons)
1. `list_keys` also runs the QUERY_TAG and the EXPLAIN scan guard. U01-89 Invariant: QUERY_TAG "for every statement
   of the entity"; design 01 §5.9 cost guard / TH01-17 apply to Snowflake queries generally. Key-only scans are
   column-pruned so the guard rarely bites.
2. `check()` keeps only the SHOW row whose `name` equals the warehouse (case-insensitive): `LIKE` treats `_` as a
   wildcard, so `HERNESS_XS` could match another warehouse's monitored row. No row -> same ConfigError.
3. `connect` typed `Callable[..., Any]` rather than `Callable[..., SnowflakeConnection]` so the fake needs no casts;
   internal `_Cursor`/`_Connection` Protocols type the used surface.
4. Credential JSON keys: `user`, `private_key`, optional `passphrase` (spec names only `cred.user` and "passphrase when
   present"); missing user/private_key -> ConfigError("snowflake credential must hold user and private_key");
   unreadable/encrypted-without-passphrase key -> ConfigError("snowflake private key cannot be read").
5. EXPLAIN plan unparseable (not JSON / no row) treated like "bytesAssigned absent" -> SchemaViolation.
6. `until=None` drops the upper bound (spec SQL always shows it; the Protocol allows None).
7. Result keys with a control char / > 512 chars, or not castable to text -> SchemaViolation("invalid source key in result")
   (record_id rule, same RE2 check as tombstone_batch); two result columns with one `to_snake` name -> SchemaViolation.

## Notes / concerns
- No `# T01-23:` markers exist in the tree (grepped herness/, tests/, tools/, docs/impl) — nothing to resolve.
- No mapping-check column sets needed (w21-s01b ruling).
- Host: the driver derives the account host; SnowflakeSettings.hosts_error (config C20) requires it in `hosts` and the
  process socket guard enforces it (ST10-55); the connector derives/adds nothing and opens no httpx client.
- V-6 open: sqlstate 28000 / 57014 are the spec defaults; real Snowflake key-pair auth failures often surface as
  DatabaseError sqlstate 08001 (errno 250001) — those map to SourceUnavailable today (retryable) until V-6 is frozen.
- `raise ... from None` leaves the driver exception on `__context__` (suppressed), same as T01-22.
- Performance (100k rows/s) not measured: `_payload` uses per-row `to_pylist` + json.dumps; no bench in this card.
- Parallel group w22-s01a (http.py/base.py) untouched. Merge agent: registry.py gains one row (145/160).

## Fix round 1 (review Important 1 and 2)
- Important 1 (§3.13 `key_pair` shape): the connector reads `private_key_pem` (+ optional `passphrase`) per
  01-connectors.impl.md:1774; message now "snowflake credential must hold user and private_key_pem". Test helper
  `store_credential` stores the documented shape; new `store_raw` writes a verbatim JSON. New test
  `test_ut01_86_documented_key_pair_secret_shape` stores `{"user","private_key_pem"}` and
  `{"user","private_key_pem","passphrase"}` (both connect with the DER key) and proves a `private_key`-only credential
  -> ConfigError. **Deviation 4 is now conforming** (credential keys `user` / `private_key_pem` / `passphrase`, exactly §3.13).
- Important 2 (`%` in the filter): the installed driver (cursor.py:824-833, default interpolate_empty_sequences=False)
  applies `sql % params` only when params is non-empty. `_where` doubles `%` in the filter only when a window value is
  bound (bounds non-empty <=> params non-empty), so the server always receives the filter as written; list_keys and
  unbounded sync send `{}` and the text unformatted. Identifiers cannot contain `%` (`_IDENT`, table parts), so no
  escaping is needed there. tests/support/fake_snowflake.py now mirrors the driver binding (`sql % params` with quoted
  literals when params is a non-empty mapping, raising the same raw TypeError/ValueError) and logs the text in `sent`.
  New tests: `test_ut01_86_percent_in_filter_reaches_the_server_verbatim` (sync with bounds, sync without bounds,
  list_keys; EXPLAIN and SELECT each hold the single `%`) and `test_st01_16_scan_guard_explains_the_percent_filter_as_sent`.
- RED: credential-shape change -> 36 failures (`must hold user and private_key`); `%` tests -> `TypeError: not enough
  arguments for format string` from the fake's driver binding. GREEN after the fix.
- Evidence: `pytest tests/unit/connectors -q -p no:logging` 692 passed, 4 skipped; card filter 62 passed;
  snowflake.py 100 % line / 100 % branch (187 stmts, 42 branches), 322/340 lines. ruff check / format --check clean
  (excluding the verifier's untracked .agent-tmp/verify-T01-23 probe scripts, not committed), mypy 0 issues (306),
  lint-imports 13 kept, check_type_ownership 0, check_module_size 0. http.py/base.py untouched.
