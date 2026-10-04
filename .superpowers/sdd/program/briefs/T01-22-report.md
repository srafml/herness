# T01-22 report — MongoDB connector (re-dispatch on salvage)

Worktree: D:\herness\.claude\worktrees\agent-a1dbbdae2d281af33 (branch worktree-agent-a1dbbdae2d281af33, base 3f61b67)
Commits: 1486162 `wip(T01-22): apply salvaged mongodb connector` (all hooks, no SKIP);
final 22c64fb `feat(connectors): T01-22 MongoDB connector` (all hooks passed incl. pytest-unit, detect-secrets; no SKIP).

## Salvage
- Copied salvage/T01-22 new files (herness/connectors/mongodb.py, tests/unit/connectors/_mongo_data.py,
  test_mongodb.py, test_mongodb_security.py); tracked-changes.patch applied cleanly on 3f61b67
  (registry.py row, conftest.py fixtures mongo_uri/mongo_breaker, test_connector_factory.py UT01-94).
- Right after apply: card tests 103 passed; ruff/format/mypy/module-size clean → checkpoint 1486162.

## Review against the spec (U01-86, U01-87, UT01-84, UT01-85, ST01-17, TH01-01/03/05/07/18)
Confirmed as spec-conformant:
- Constructor/default client options exactly U01-86 (tz_aware, tzinfo=UTC, secondaryPreferred,
  connectTimeoutMS=10000, server-selection/socket = timeout_s*1000, appname, retryReads=False).
- `register("connector","mongodb")` static decorator + static `_BUILTINS` row; UT01-94 resolves the real class.
- Credentials: `secrets.resolve(auth.credentials)` at first use (lazy client); URI only in locals,
  passed to client_factory; never on connector attributes, logs, messages, context or `__cause__`
  (driver errors re-raised `from None`, driver text dropped).
- URI checks before client_factory: scheme, verified TLS (tls/ssl true for non-srv non-loopback;
  tlsInsecure/tlsAllowInvalid* never true), every host (SRV name for +srv) in `settings.hosts`;
  fixed messages without host/URI.
- No httpx client anywhere; the process-wide socket guard (herness.core.egress_socket, installed
  from config, SDK_SOURCE_KINDS hosts) enforces SRV targets — the connector needs no call.
- Queries from config only (filter, updated_field, key_field); data values used only as $gt/$gte
  comparison values, never as identifiers.
- Pagination: key-set pages via `retry_page(..., source="mongodb")` with `fault_point("http.page",
  source="mongodb")` and `_map_mongo_error` inside the retried callable; the last (ts, key) pair lives
  in the generator so a retried page re-asks the same query (UT01-84 asserts queries[1]==queries[2]).
- Rows: RowBatcher (METADATA_SCHEMA first, batch_rows bound, one page in memory), relaxed-JSON payload,
  `_source_key=str(key)`. No redaction exists in any connector — spec 01 assigns none to connectors
  (the runner/_write_loop contract writes what connectors yield); nothing added.
- Error mapping = U01-86 Algorithm (UT01-85 parametrised over every class).

Changes made in this re-dispatch:
1. FIX (spec U01-87 step 3): `ts` = the aware datetime as-is, `parse_source_timestamp` only otherwise.
   Salvage parsed every value, which also applied the 1970–2100 range and rejected e.g. a 1969 aware
   datetime. RED: new `test_ut01_84_aware_datetime_is_used_as_is` →
   `SchemaViolation: unparseable timestamp in ts` (rows.py:139); GREEN after the fix.
2. TEST (ST01-17/TH01-03 evidence gap): `test_st01_17_uri_not_kept_on_the_connector_or_logged` —
   after check + sync, no connector attribute except the driver client holds the URI and no captured
   log event contains it. Passed on first run (behaviour already correct).

## Deviations (salvage's 6, reviewed) + 1 recorded
1. KEPT — loopback URI hosts need no `hosts` listing. Justified by the spec's own text: U01-86 exempts
   all-loopback non-srv URIs from TLS, yet U01-10 `_check_hosts` rejects IP literals, so a literal
   reading makes that exemption unreachable; the socket guard always allows loopback. Remote hosts strict.
2. KEPT — list_keys resume query `{"$and": [filter, {key: {"$gt": last}}]}` instead of
   `{**filter, key: {"$gt": last}}`. MongoEntity only forbids updated_field/$or in the filter, not
   key_field; the dict merge would overwrite a key_field filter and widen scope (TH01-07). Same result otherwise.
3. KEPT — `+srv` URI with explicit tls=false/ssl=false rejected. U01-86 Preconditions: the URI must
   enable verified TLS (TH01-01); +srv only defaults TLS on, so an explicit opt-out violates it.
4. KEPT — dotted key/updated/fields read nested values (`_dig`). U01-10 `_MongoName` allows dots and
   Mongo sort/query use them as paths; a literal `doc[key_field]` would raise/None for every dotted name.
5. KEPT — `_UNAVAILABLE = (ConnectionFailure, ExecutionTimeout)`: ConnectionFailure is the pymongo base of
   AutoReconnect/NetworkTimeout/ServerSelectionTimeoutError, identical mapping (UT01-85 covers each).
6. KEPT — test_connector_factory.py config enables mongodb and `cfg` registers MongoConnector because
   UT01-94 builds every `registry.available("connector")` (which includes `_BUILTINS`); the
   "not configured" probe moved to snowflake. T01-23 needs the same treatment for its row.
7. RECORDED (new, KEPT) — flattened columns are `fields` ∪ `updated_field` ∪ `key_field` (spec
   U01-87 step 3 names `fields`). Justified by U01-49 step 6: mapping check's fetched set for mongodb is
   `to_snake` of fields, key_field, updated_field — the lake must carry those columns.

## Evidence
- Card tests: `PYTHONUTF8=1 uv run pytest tests/unit/connectors -q -p no:logging -k "UT01_84 or UT01_85 or ST01_17 or UT01_94 or ST01_14"` → 105 passed.
- `pytest tests/unit/connectors -q` → 643 passed, 4 skipped.
- Coverage herness.connectors.mongodb: 100 % line, 100 % branch (203 stmts, 50 branches).
- Gates: ruff check . clean; ruff format --check . clean; mypy 0 issues (305 files); lint-imports 13 kept;
  check_type_ownership 0; check_module_size 0. mongodb.py 319/320 (hard limit 400); registry.py +1 row.

## Concerns
- mongodb.py is at 319/320 — no room for later changes without a budget ruling.
- `raise ... from None` sets `__suppress_context__`; the driver exception is still on `__context__`
  (hidden from tracebacks/logging). Not a spec breach; noted for the verifier.
