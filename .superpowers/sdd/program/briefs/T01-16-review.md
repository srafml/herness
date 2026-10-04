# T01-16 review - ServiceNow connector (verify, group w25-s01a)

Worktree agent-ac36058302893f4a8, base a51221f, head faf2df7. Read-only; working tree clean after all checks.

### Spec Compliance
- ✅ Spec compliant (deviations below are justified and documented; none blocks).

Units:
- ✅ U01-66 ServiceNowConnector: registered `("connector","servicenow")`; name/entities/watermark_field/check per table (servicenow.py:137-167). SourceHttp(http_client(..., source="servicenow", max_concurrency), breaker_key="servicenow", auth=build_auth(...)) built lazily in `_source()` (servicenow.py:234-250) - see ⚠️ 2.
- ✅ U01-67 build_sn_query: clause order `>=since`, `<until`, `sys_class_nameIN`, filter, ORDERBY time/key; absent parts omitted; `_sn_time` converts to UTC then floors microseconds, `%Y-%m-%d %H:%M:%S` (servicenow.py:51-81).
- ✅ U01-68 sync: until/since defaults, split_range windows, fetch_fields (+sys_class_name for cmdb_ci, dedup), RowBatcher columns `f, f_display`, per-window `_audit_deletes` then `_pages` merged via merge_by_time, compact JSON payload, flatten_record(display_pairs=True), flush (servicenow.py:169-205, 326-338). `_pages` params exactly as spec; body/result shape check; stop on short page; next link via `http.check_next_url`, params None; CursorGuard stepped on every followed page (servicenow.py:252-287). `_audit_deletes`: one probe per instance with allow_status {403}, INFO `connectors.servicenow.audit_delete_unreadable` once, `[]` thereafter; window query/fields/display=false exact; >100,000 -> SchemaViolation (servicenow.py:289-319).
- ✅ U01-69 merge_by_time: two-way merge, record first on equal (ts,key) (`<=`), both inputs order-checked -> SchemaViolation("source order violated") (servicenow.py:84-115).
- ✅ U01-70 list_keys: key-order query, `sysparm_fields=sys_id`, display false, KEY_SCHEMA batches of batch_rows (servicenow.py:209-223).

Checks requested:
1. ✅ Only HTTP path is `SourceHttp.get_json` (retry_page inside) with `build_auth(source="servicenow")`; no httpx/requests import in servicenow.py; ST01-14 lint and tools untouched (diff stat: only servicenow.py + one registry row under herness/).
2. ✅ Next links only through `check_next_url` (same host+port, https, no userinfo); CursorGuard stepped (`url` for links, `url|offset` for offset paging). Offset incremented by `len(result)`, stop when `< page_size`; UT01-68 asserts exact offsets `[0]`, `[0,100]`, `[0,100]`, `[0,100,200]` and row keys == cassette records (no duplicate/missed page).
3. ✅ sysparm_query built only from validated settings: table = entity from the fixed ServiceNow entity set; fields/classes match `^[a-z][a-z0-9_]{0,79}$`; filter validated (no ^NQ/^EQ/ORDERBY/line breaks, <=1000); classes only for cmdb_ci (settings.py:105); timestamps formatted by `_sn_time`. UT01-67 pins exact strings incl. sub-second floor and a +02:00 input.
4. ✅ merge_by_time ties/out-of-order verified by UT01-70 (incl. 3 unordered cases); audit probe / 403 INFO once / cap verified (cap via monkeypatched `_MAX_DELETES`, Minor 4).
5. ✅ Rows bounded by RowBatcher `batch_rows` and written via runner -> LakeWriter. Note: no connector/runner path redacts rows for any connector (raw lake keeps `_payload` by design); ticket text appears only in lake rows. Error messages name fields/statuses only (`missing sys_id`, `unparseable timestamp in sys_updated_on`, `result is not a list`, `entity <configured name> ...`); the only log field is `source`. UT01-68 shape test and ST01-05 assert record values ("20/02/2026", "INC") are not echoed.
6. ✅ Cassettes: the only URL host is `synthetic-instance.example.com`; no token/password/secret/authorization keys; no `service-now.com` (also asserted in UT01-68). Generator foreign host `synthetic-elsewhere.example.net`. All credentials in `_servicenow_env.py` start with `synthetic` (CLIENT_ID, CLIENT_SECRET, USERNAME, PASSWORD, ACCESS); test settings use `secret:sn` references only. Committed cassettes == generator output (UT01-68).
7. ✅ Mutation experiments (monkeypatch plugins under C:\Users\santh\AppData\Local\Temp\w25-s01a-verify; no worktree edits):
   - `check_next_url` made identity -> both ST01-02 tests FAIL (production-built test: egress host guard raises EgressBlocked instead of ForeignHostError; guard-less test: the foreign request reaches the fake -> 404). The connector's guard is genuinely asserted, and defence in depth is visible.
   - CursorGuard made a no-op -> `test_ut01_68_repeated_next_link_stops_with_schema_violation` FAILS.
   - lenient `parse_source_timestamp` -> ST01-05 `no-sys-updated-on` FAILS.
   - ST01-03 proves the sentinels were in play (form secret/password, bearer 1 and refetched bearer 2) and absent from DEBUG logs, error text + tracebacks, SyncResult repr/to_dict, lake rows, sync_slice.last_error and all bytes under data root/tmp_path; the token endpoint error body echoes PASSWORD and is proven not to leak.
8. Builder concerns - rulings in ⚠️ below.
9. ✅ Every card ID has >=1 test with the ID in name and docstring first word: UT01-67 x5, UT01-68 x8, UT01-69 x2, UT01-70 x6, UT01-71 x2, IT01-02/03/04/05/06/10 x1, ST01-02 x2, ST01-03 x1, ST01-05 x1 (2 params), UT01-94 +1. `pytestmark` set in all three new test files. Mapping follows the §11 rows (UT01-69 = display pairs, UT01-70 = deletes/merge), correct even though the units' Tests columns are offset by one.
   Reproduced: the 4 test files `-m "unit or integration"` 56 passed; coverage of servicenow.py from the card tests 191/191 lines, 60/60 branches (100/100). Budgets: servicenow.py 342/390, registry.py 147/160.

Test rows:
- ✅ UT01-67, UT01-68, UT01-69, UT01-70, UT01-71
- ✅ IT01-02, IT01-03, IT01-05, IT01-06, IT01-10
- ⚠️ IT01-04 - "drift logged" clause not asserted (unreachable via ServiceNow; ⚠️ 1)
- ✅ ST01-02, ST01-03, ST01-05 (ServiceNow carry-overs), UT01-94 (servicenow row)

⚠️ Cannot verify from diff / needs controller ruling:
1. (a) IT01-04 drift log. Confirmed unreachable: RowBatcher columns are fixed from `fetch_fields` (all strings; `_text` JSON-encodes objects), flatten_record emits only those fields, and SchemaTracker is per writer, so a schema change across runs is always a new writer, never in-run drift. The drift log path itself is covered by T01-06 unit tests (tests/unit/connectors/test_runner_write.py). Recommended ruling: accept; amend the IT01-04 row wording for ServiceNow ("new writers; build succeeds"), or attach the drift clause to a connector with dynamic columns.
2. (b) U01-66 lazy build (servicenow.py:234-250). Accept as a deviation: building auth in the constructor would resolve the keyring secret, which UT01-94 ("no network, from synth config") forbids; mongodb.py:196 and snowflake.py:163 already build lazily. Behaviour difference: a missing base_url/auth surfaces as ConfigError on first call (`check()`), not at construction. Record in the spec deviations.
3. (c) UT01-94 enabled-only filter (test_connector_factory.py:154): justified - `build_connector` raises ConfigError for a disabled source (factory.py:58) and servicenow is the synth config's disabled example; the strict `set(built) == {...}` equality is kept and servicenow gets its own real-class test. Accept.
4. (d) Integration tests importing `tests/unit/connectors/_servicenow_env.py` (and `_http_data`): accept (precedent: test_auth_security -> _runner_data); move to tests/support in a later cleanup.
5. Cassettes from a card-local generator instead of T11-15 - per ledger ruling; carry-over to regenerate from write_api_pages when T11-15 lands. BT01-01 out of scope per ruling.
6. Full suite and pre-commit not re-run here (report: ruff/mypy/lint-imports/type-ownership/module-size clean; hooks passed).

### Strengths
- Tight, readable 342-line module; every request through SourceHttp with page retry; query construction confined to validated inputs.
- The fake Table API evaluates the connector's own encoded queries, so windows, audit queries, offsets and key listings are tested against real query semantics, plus strict Replay cassettes for the four paging shapes.
- Strong security tests: production-built connector over the real egress client, real build_auth OAuth, real runner and LakeWriter; mutations confirm the tests bite.
- IT01-10 genuinely reproduces offset drift mid-paging via `after_page` and checks no other record is lost.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. tests/integration/connectors/test_servicenow_flow.py:199 - IT01-04 asserts only the `connectors.sync.completed` rows; to pin the accepted deviation, also assert no `connectors.schema_drift.detected` event in `logs`.
2. herness/connectors/servicenow.py:234-250 - lazy SourceHttp/auth build deviates from U01-66 wording; add to the spec deviation list (no code change).
3. herness/connectors/servicenow.py:282-287 - CursorGuard key is the link URL in link mode vs spec `f"{url}|{offset}"`; equivalent (the link carries its offset). The full-page-without-link fallback to offset paging after links is an unspecified case; both documented in the report. Note in deviations; no change needed.
4. tests/unit/connectors/test_servicenow.py:310 - the cap test monkeypatches `_MAX_DELETES` to 1, so the literal 100,000 limit is never pinned; add `assert sn._MAX_DELETES == 100_000`.
5. tests/integration/connectors/test_servicenow_flow.py:26-27 - integration imports unit-test helpers; move `_servicenow_env` to tests/support later.
6. herness/connectors/servicenow.py:162 - `check()` indexes `self.entities[0]`; relies on settings guaranteeing >=1 entity (IndexError otherwise).

### Assessment
**Task quality:** Approved
**Reasoning:** All five units match the spec, every card test row is present and passing with 100/100 coverage, and the security tests are shown by mutation to bite. The builder's four concerns are justified deviations or a spec-wording gap (IT01-04 drift clause) that need a controller ruling but no code fix.
