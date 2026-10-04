# T01-16 report - ServiceNow connector (build, group w25-s01a)

Status: DONE_WITH_CONCERNS (see Concerns). Worktree D:\herness\.claude\worktrees\agent-ac36058302893f4a8, base a51221f.
Commits: 018e827 wip(T01-16) checkpoint; faf2df7 feat(connectors): add ServiceNow Table API connector (T01-16). Both committed with all pre-commit hooks passing (incl. pytest-unit), no SKIP.

## Implemented
- herness/connectors/servicenow.py - 342 / 390 lines (no split): ServiceNowConnector (U01-66,
  @register("connector","servicenow")), build_sn_query (U01-67), sync (U01-68: windows via split_range,
  offset paging or same-host Link rel=next via http.check_next_url, CursorGuard, display pairs via
  flatten_record(display_pairs=True), RowBatcher, audit-delete tombstones with 403 probe + one INFO log,
  >100,000 deletes -> SchemaViolation), merge_by_time (U01-69, record first on ties, order check),
  list_keys (U01-70, KEY_SCHEMA batches of batch_rows). All HTTP via SourceHttp.get_json (retry_page,
  caps, map_http_error); auth only via build_auth(source="servicenow"); no httpx import.
  sysparm_query only from validated settings (classes, filter, entity name) and formatted timestamps.
  Shape/key/timestamp errors -> SchemaViolation without record values.
- herness/core/registry.py: one _BUILTINS row ("connector","servicenow") -> 147 / 160.
- tests/support/sn_cassettes.py (card-local seeded generator per ruling; `python -m tests.support.sn_cassettes`):
  FakeServiceNow (evaluates the connector's encoded queries, offsets, fields, display mode, audit table,
  Link next incl. foreign host, after_page hook for mid-paging updates, per-path failure statuses) and
  Replay (strict in-order page cassettes checking path/params).
- tests/fixtures/connectors/servicenow/: incident_table.json (240 incidents over 60 h + 5 audit rows, 4
  for incident), pages_empty, pages_one_full_then_empty, pages_short_last, pages_link_next. Host
  synthetic-instance.example.com; secrets/tokens all `synthetic...`; sys_ids low entropy so detect-secrets
  finds nothing (scan clean, .secrets.baseline unchanged).
- Tests: tests/unit/connectors/test_servicenow.py, test_servicenow_security.py, _servicenow_data.py,
  _servicenow_env.py; tests/integration/connectors/test_servicenow_flow.py; UT01-94 extension in
  tests/unit/connectors/test_connector_factory.py.

## Tests per ID (function count; all pass)
UT01-67: 5 (exact query strings incl. sub-second floor + non-UTC input; 60 h / 24 h -> 3 windows with
exact queries and audit queries; cmdb_ci classes/filter + backfill start; members/check; unknown entity /
missing auth). UT01-68: 8 (4-way parametrized cassettes empty / one full then empty / short last / Link
next: rows == cassette records, offsets exact, no dup/missed page; next link requested as given; batch
columns + batch_rows; repeated next link -> CursorGuard SchemaViolation; committed cassettes == generator
output; write_all; 5 shape errors; non-object body). UT01-69: 2. UT01-70: 6 (ascending merge with
tombstones; 403 -> no tombstones, probe once, INFO once; too many deletes; delete without documentkey;
merge ties; unordered input x3). UT01-71: 2. UT01-94: new test_ut01_94_servicenow_resolves_through_the_builtin_table
(real class via shipped _BUILTINS, Connector + SupportsKeyListing, no network, lazy http) and the
every-registered test now builds only enabled sources (servicenow is disabled in its synth config).
ST01-02: 2 (production-built connector over egress client: foreign Link -> ForeignHostError, no request
to the foreign host, no lake file, no watermark; plus the connector's own check over a guard-less client).
ST01-03: 1 (ServiceNow end to end, OAuth password grant with sentinel secrets via real build_auth +
egress client + SyncRunner + real LakeWriter: success, 401 (token refetched, then AuthError), 500
(retried, SourceUnavailable), refused token endpoint; sentinels absent from DEBUG logs, error
text/tracebacks, SyncResult repr/to_dict, lake rows, sync_slice.last_error, every file under data root
and tmp_path; sentinels proven in play). ST01-05: 1 x 2 params (result as object; record without
sys_updated_on after a written batch: writer aborted, no commit, watermark unchanged).
IT01-02, IT01-03, IT01-04, IT01-05, IT01-06, IT01-10: 1 each (real config, ops store, runner, LakeWriter,
110_stg_servicenow staging build; IT01-03 builds files 0..299 and compares every core.* count).

Runs: card selection `-k "UT01_67 or ... or UT01_94 or ST01_02 or ST01_03 or ST01_05"` 62 passed;
`-m integration -k "IT01_02 or ... or IT01_10"` 6 passed; `tests/unit/connectors tests/integration/connectors`
934 passed, 4 skipped. Coverage of servicenow.py from card tests: 100 % line (191/191), 100 % branch (60/60).
Gates: ruff check . 0; ruff format --check clean (926); mypy 0 (338 files); lint-imports 13 kept;
check_type_ownership 0; check_module_size 0. Full suite not run (dispatch rule).

## Deviations / rulings applied
1. SourceHttp + auth are built lazily on first request (U01-66 says "when http is None it builds ...",
   not when): construction makes no network call and resolves no secret, so build_connector works for
   UT01-94 without a keyring secret or loaded egress config (mongodb/snowflake are lazy the same way).
2. Paging: offset is counted on every page; a followed next link is a CursorGuard step of the link URL
   (a repeated link stops), offset paging steps `url|offset`; a full page without a link after links falls
   back to offset paging on the base URL with the original params (spec leaves this case open).
3. Every record field read accepts a pair (`value`) or a plain value (list_keys / audit use display=false).
4. Cassettes: card-local generator (ruling) instead of T11-15 output; two kinds (table state served by a
   query-evaluating fake; strict page replays) rather than respx (respx does not patch httpx2).
5. UT01-94 every-registered test filtered to enabled sources (its synth config disables servicenow, used
   as the disabled example elsewhere).
6. IT01-04 "drift logged": not reachable for ServiceNow - RowBatcher columns are fixed per run from the
   configured fields (all strings), and SchemaTracker compares batches within one stream, so a new /
   removed field arrives as a new writer in the next run, never as in-run drift. The test asserts the
   new-writer file schemas (added short_description*, removed priority*), the retyped value, staging build
   success and cast failure, and no drift claim. Needs a controller ruling (concern 1).
7. BT01-01 out of scope (ruling).

## Concerns
1. IT01-04 drift-log clause unreachable through servicenow.py (deviation 6) - spec/test row wording.
2. tests/unit/connectors/test_connector_factory.py edited (UT01-94); parallel jira/dataverse groups add
   rows to the same _BUILTINS dict and test - trivial merge conflicts expected.
3. Integration tests import helpers from tests/unit/connectors/_servicenow_env.py (same pattern as
   test_auth_security importing _runner_data); move to tests/support if preferred.
4. Cassettes are ~630 KB total (indent=1 for review); tests/fixtures is excluded from the large-file hook.
