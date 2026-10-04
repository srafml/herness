# T01-22 review — MongoDB connector (verify agent)

Worktree agent-a1dbbdae2d281af33, head 22c64fb, base 3f61b67. Read-only review; no code edits.

### Spec Compliance
- ✅ Spec compliant
  - U01-86 MongoConnector ✅ — constructor/defaults exact (mongodb.py:171-183; asserted by test_ut01_85_default_client_options); `@register("connector","mongodb")` + static `_BUILTINS` row (registry.py:40); `name`/`entities`/`watermark_field`/`check` (ping + index_missing WARNING with entity, field) ✅; URI preconditions (scheme, verified TLS, no tlsInsecure/tlsAllowInvalid*, every host / SRV name in `hosts`, fixed messages without host/URI, checked before `client_factory`) ✅ mongodb.py:118-131, 198-201; lazy client ✅; `_map_mongo_error` = Algorithm table ✅ mongodb.py:66-77.
  - U01-87 sync/list_keys ✅ — base query with $gte/$lt (absent bounds omitted), $and/$or key-set resume, projection fields+updated+key, sort (updated,key), limit/max_time_ms, `retry_page(..., source="mongodb")` with `fault_point("http.page", source="mongodb")` and mapping inside the retried callable (mongodb.py:228-248); per-doc key check, aware datetime as-is else parse_source_timestamp, relaxed-JSON payload, flatten; stop on short page; list_keys key-set paging with KEY_SCHEMA batches.
  - UT01-84 ✅ (2,500 docs / page 1,000 / AutoReconnect on page 2 once -> 2,500 rows ascending, unique, `_source_key=str(_id)`, relaxed JSON; the retry re-asks the identical query).
  - UT01-85 ✅ (index_missing WARNING; non-TLS URI -> ConfigError before factory; OperationFailure(18) -> AuthError, not retried; full mapping parametrised).
  - ST01-17 ✅ (rogue.example vs hosts [db1.example] -> ConfigError, factory call log empty, host/URI absent from message/context/hint, `__cause__` None; snowflake hosts omitted and dataverse without login.microsoftonline.com rejected at validation).
  - UT01-94 ✅ (real class via shipped `_BUILTINS` row; per w21-s01b ruling). ST01-14 filter passes.
  - Threats TH01-01/03/07/18 ✅ (see Focused checks).
- ⚠️ Not verifiable in unit scope: real-driver `list_indexes()` on a missing collection and SRV target enforcement by the socket guard (spec assigns SRV targets to the guard; guard allows only listed hosts + loopback, egress_socket.py:99-133). No live MongoDB.

### Evidence (re-run by verifier)
- `PYTHONUTF8=1 uv run pytest tests/unit/connectors -q -p no:logging` -> 643 passed, 4 skipped (environment skips: DuckDB excel extension, symlink permission).
- Card filter `-k "UT01_84 or UT01_85 or ST01_17 or UT01_94 or ST01_14"` -> 105 passed, 542 deselected.
- `uv run ruff check .` -> All checks passed; `ruff format --check .` clean; `uv run mypy` -> no issues (305 files); `tools.check_module_size` rc 0; mongodb.py 319/320, registry.py 144.
- Coverage herness.connectors.mongodb: 203 stmts, 50 branches, 100% line and branch.

### Focused checks (one per named risk)
- Risk: `_uri_parts` disagreeing with pymongo so a rogue host passes `_check_uri` but pymongo connects to it (`?` before `/`, `@` after `?`). Check: pymongo 4.18 `_validate_uri` (uri_parser_shared.py:566-584) partitions at `?` first, then `/`, then rpartition `@`, exactly as mongodb.py:92-94; probe `mongodb://db1.example?tls=true&x=@rogue.example/?tls=false` -> pymongo rejects (InvalidURI). Remaining differences are fail-closed only (percent-decoded non-socket hosts: pymongo rejects `%`; `;`/`&` mix: pymongo rejects; duplicate tls: all must be true). No bypass.
- Risk: credentials leaking through the exception chain. `_driver` raises `from None` (mongodb.py:80-85): `__cause__` None, `__suppress_context__` True, `__context__` still the driver exception. structlog's ExceptionDictTransformer honours `__suppress_context__` and runs with show_locals=False (_log_pipeline.py:165); `errors.to_log_fields` reads `__context__` but copies only its type name (errors.py:378). Driver messages can carry hosts/db names, not the URI credentials. Acceptable -> Minor 1.
- Risk: URI kept on the instance. The URI is a local of `_database` (mongodb.py:199-201) passed only to the factory; the ST01-17 test asserts no attribute except `_client` holds it and no log event carries it. The driver client holding it internally is unavoidable.
- Risk: identifier from data. Only config names (filter, updated_field, key_field, fields, collection) form query keys/projection; document values appear only as comparison values (mongodb.py:276-278, 319). MongoEntity forbids updated_field and top-level $or in the filter (settings_entities.py:237-241), so `dict(filter) | {updated: bounds}` cannot drop a filter term.
- Egress: no httpx in mongodb.py; pymongo reaches the network only through the process socket guard (SDK_SOURCE_KINDS, _egress_source.py:86).
- Redaction: spec 01 assigns no redaction to connectors or the runner write loop (no "redact" in impl 01 or herness/connectors). Rows are bounded by RowBatcher(batch_rows), one page in memory. N/A.

### Strengths
- Spec-literal security posture: fixed error messages, driver text dropped, host/TLS checks before any factory call, fail-closed parsing.
- UT01-84 proves retry-resume semantics (queries[1] == queries[2]) with the real `retry_page` and a spy breaker; error mapping parametrised over every class; ST01-17 covers seed lists, userinfo, SRV, IPv6, socket path, case.
- 100% line/branch coverage; module budget respected.

### Issues
#### Critical (Must Fix)
- None.
#### Important (Should Fix)
- None.
#### Minor (Nice to Have)
1. herness/connectors/mongodb.py:80-85 — `raise _map_mongo_error(exc) from None` keeps the driver exception on `__context__` (suppressed, not rendered; `to_log_fields` exposes only its type name). Raising the mapped error after leaving the `except` block would make `__context__` None as well. Not a spec breach.
2. herness/connectors/mongodb.py:134-135 — `_dig` prefers a literal top-level key containing a dot over the path; the server uses path semantics for sort/query, so a document with a literal "a.b" key could make the resume pair differ from the server's value. Edge case (dotted literal keys are discouraged by MongoDB).
3. herness/connectors/mongodb.py — 319/320 lines; any later change needs a budget ruling (builder flagged).
4. tests/unit/connectors/test_connector_factory.py (~line 202-205 of the updated test) — the "not configured" probe moved to snowflake; T01-23 must move it again when it adds its `_BUILTINS` row. Carry-over note for the controller.

### Deviations (builder's 7)
1. Loopback URI hosts need no `hosts` entry — ACCEPTABLE. U01-86's all-loopback TLS exemption is otherwise unreachable (settings reject IP literals), and the socket guard always allows loopback (egress_socket.py:53-58, 105). Remote hosts stay strict; a mixed loopback+rogue seed list is refused (tested).
2. list_keys resume `{"$and": [filter, {key: {"$gt": last}}]}` — ACCEPTABLE. Same result as `{**filter, key: ...}` unless the filter constrains key_field, where the dict merge would widen scope (TH01-07).
3. `+srv` with explicit tls=false/ssl=false refused — ACCEPTABLE. Required by "URI must enable verified TLS" (TH01-01).
4. Dotted key/updated/fields read nested values — ACCEPTABLE. `_MongoName` permits dots and the server treats them as paths; literal `doc[key_field]` would break every dotted config (see Minor 2).
5. `_UNAVAILABLE = (ConnectionFailure, ExecutionTimeout)` — ACCEPTABLE. ConnectionFailure is the pymongo base of AutoReconnect/NetworkTimeout/ServerSelectionTimeoutError; mapping identical; each class tested; checked before OperationFailure (ExecutionTimeout subclasses it).
6. test_connector_factory enables mongodb and registers MongoConnector; "not configured" probe moved to snowflake — ACCEPTABLE (w21-s01b ruling: one `_BUILTINS` row + UT01-94 real-class test; `registry.available` includes built-ins).
7. Flattened columns = fields + updated_field + key_field — ACCEPTABLE. Matches U01-49 step 6 (mongodb fetched set) and the w21-s01b ruling; the spec's own `_id` -> `f_id` column exists only this way.

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit spec, test row and threat of T01-22 is met with passing tests, clean gates and 100% coverage; the URI checks match pymongo's own parsing (no host/TLS bypass found) and all seven deviations are spec-justified. Only Minor polish items remain.
