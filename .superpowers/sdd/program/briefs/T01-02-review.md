# T01-02 review: per-source settings and `SourcesConfig`

Reviewed commit f225249 (diff d72f0bb..HEAD) in worktree agent-af2bb9964cfba20fc against brief T01-02,
impl 01 §3.2 (U01-05 ... U01-15, read in full from docs/impl/01-connectors.impl.md because the brief
only quotes U01-07 and U01-15), global constraints and reviewer rules.

Gates re-run by the reviewer (worktree .venv):
- `pytest -k "UT01_02 or UT01_05 ... or UT01_97 or ST01_07" tests/unit/connectors tests/integration/connectors`: 197 passed
- `pytest tests/unit/connectors tests/integration/connectors -W error --cov=herness.connectors --cov-branch`: 222 passed, no warnings; settings.py, settings_entities.py and settings_base.py at 100 % line and branch
- `pytest tests/unit/repo tests/unit/tools`: 17 passed
- `ruff check .` clean; `ruff format --check .` clean (53 files); `mypy` 0 issues (23 files)
- `lint-imports`: 7 kept, 0 broken; `python -m tools.check_type_ownership`: exit 0
- Line counts: settings.py 314, settings_entities.py 308, settings_base.py 321 (all under 400)

### Spec Compliance
- ✅ Spec compliant (with the four controller rulings applied)

| Unit / row | Result | Notes |
|---|---|---|
| U01-07 ServiceNowSettings | ✅ | SOURCE, base_url and auth required, page_size 1000 in 100–10000 (source and entity), overlap 60, entities non-empty and ⊂ SERVICENOW_ENTITIES (settings.py:90-114) |
| U01-07 ServiceNowEntity | ✅ | fields non-empty/pattern/unique; window_hours 1–168 default 24; classes required for cmdb_ci only; filter ≤1000 without ^NQ/^EQ/ORDERBY/\n/\r (settings_entities.py:70-82); incident slice_days=7 via model_copy keeping other backfill fields (settings.py:117-123) |
| U01-08 Jira | ✅ | flavor required, auth_key `jira:{flavor}`, page_size 100 with 1–100 / 1–1000, overlap 60, jql_scope ≤2000 without order by/\n/\r, fetch_remote_links False, entities default {issue} and must equal {issue} (settings.py:126-157) |
| U01-09 Monitoring / adapter / MetricQuery / validate_spl | ✅ | base_url/auth forbidden, entities event+metric_daily filled, reconcile via model_fields_set, adapter Literal keys, enabled needs an enabled adapter; per-tool auth, page caps (datadog 1000, dynatrace 500 default 500), concurrency default/cap, tenant prometheus-only, event_query rules, datadog agg, prometheus step = 86400 s, splunk validate_spl; validate_spl rules (a)–(d) (settings_entities.py:54-189) |
| U01-10 Mongo | ✅ | base_url forbidden, hosts non-empty, auth connection_string, database pattern, max_time_ms 60000 in 1000–600000, page_size 1000 in 1–10000; entity collection/system., key_field `_id`, fields, filter walk iterative with explicit stack, banned ops recursive, top-level updated_field/$or, ≤64 keys, depth ≤8 |
| U01-11 Snowflake | ✅ | account pattern, hosts must contain `<account.lower()>.snowflakecomputing.com` with the exact message, key_pair, warehouse/role Ident, statement_timeout 900 in 1–86400, max_scan_gb 50 in (0,10000], page_size 10000; entity three-part table, columns unique, key/updated in columns, filter tokens and word list |
| U01-12 Dataverse | ✅ | base_url and auth required (msal + GUID tenant_id enforced by T01-01 AuthSettings), hosts must contain login.microsoftonline.com with the exact message, page_size 5000 in 1–5000, entity fields, updated_field default `modifiedon` |
| U01-13 Files | ✅ (⚠️ symlink part deferred, ruling c) | base_url/auth forbidden, inbox default data/inbox (lax Path), max_concurrency cap 1, key pattern; FilesEntity pattern/sheet/key_field/updated_field/mode rules |
| U01-14 SourcesSection / SourcesConfig | ✅ | seven optional sections, extra forbid, version Literal[1] (bool rejected), no dq/build, enabled_sources fixed order, source() ConfigError "source {name} is not configured" |
| U01-15 allowed_hosts | ✅ | enabled base_url hosts, enabled adapter hosts, hosts entries; nothing derived; lower-cased, no port (settings.py:301-314) |
| UT01-02 | ✅ | unknown key at source/entity/root with path; Jira entities; required/forbidden base_url/auth; design §7 example; version; source(); fresh-interpreter sys.modules check |
| UT01-06 | ✅ | ^NQ, ORDERBYDESCx, newline, classes on incident, incident slice_days=7 |
| UT01-07 | ✅ | nested $where, $function, top-level updated_field, depth 9 |
| UT01-08 | ✅ | no stats, \| delete, backtick rejected; \| tstats accepted |
| UT01-09 | ✅ | 12h, 2d rejected; 1d, 86400s accepted |
| UT01-10 | ✅ | two-part table, column a;b, drop and -- filters |
| UT01-11 | ✅ | metric name foo rejected |
| UT01-12 | ✅ | ../*.csv, a/b.csv, *.exe, sheet on csv |
| UT01-13 | ✅ | exact set with SN, 2 adapters, Snowflake and Dataverse hosts; disabled sources contribute nothing |
| UT01-66 | ✅ | oauth_3lo "not supported in v1" (Jira; also adapters) |
| UT01-97 | ✅ (⚠️ http_client half deferred, ruling c) | IP, port, duplicate rejected; upper case lower-cased; SN hosts in allowed_hosts; three SDK messages |
| ST01-07 | ✅ | SN, Mongo, Snowflake, SPL and jql_scope injection strings rejected |

T01-01 follow-ups:
- ✅ One consolidated "settings modules are leaves" contract with sources herness.model.settings, herness.connectors.settings, settings_base, settings_entities; no ignore_imports exception; herness.model / herness.connectors forbidden (import-linter skips overlapping pairs, confirmed in importlinter/contracts/forbidden.py `_modules_overlap`) (pyproject.toml:270-296).
- ✅ UT00-58 (tests/unit/repo/test_import_contracts.py:60-70) and OWN050 (tools/check_type_ownership.py:293-335) match settings.py and settings_*.py; UT00-73 added.
- ✅ No max_concurrency redeclared in any SourceSettings subclass.
- ✅ `_ReadOnlyDict.__reduce__` (settings_base.py:149-152); reviewer probe confirmed pickle round-trip of a SourcesConfig; deepcopy/model_copy(deep=True) tested (test_settings_config.py:153-165).
- ✅ Entity page_size bounded by the source bounds (settings.py:69-74; UT01-05 test).

Controller rulings: (a) split sound, `__all__` re-exports every module-map name and SourceSettings; (b) no HernessConfig composition; (c) deferrals documented in the report; (d) example edits documented in the test comment.

- ⚠️ Cannot verify here: `herness config validate --profile synth --offline` (T10-03, ruling b); UT01-97 http_client half and inbox symlink check (ruling c).

### Strengths
- Shared `_Source` base centralises required/forbidden base_url/auth, the hosts hook and page bounds; subclasses stay declarative.
- Messages name the key path and never echo values; `hide_input_in_errors` inherited.
- R-03 acceptance checked in a fresh interpreter and proven non-vacuous (asserts pydantic is observed).
- 100 % line and branch coverage; most rejection tests assert the message.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/connectors/settings.py:180 — `model_dump()` of a SourcesConfig with monitoring does not re-validate: the dumped default `reconcile` counts as present and fails "monitoring is not reconciled" (reviewer probe; `exclude_unset=True` round-trips). Spec-mandated rule; flag for T10-03 (any dump/re-validate or `config show` flow must use exclude_unset).
2. pyproject.toml:291 — for the connectors settings sources the `herness.connectors` forbidden entry is skipped as overlapping, so lint-imports no longer stops e.g. `herness.connectors.settings -> herness.connectors.<non-settings module>`; only OWN050 (tools/check_type_ownership.py:306-313) catches it. Acceptable, worth one comment line in the contract.
3. herness/connectors/settings_entities.py:37,101 — MetricQuery `step` limited to `\d{1,9}` and max_length 16; spec says `^(\d+)(s|m|h|d)$`. Harmless but undocumented deviation.
4. herness/connectors/settings_entities.py:115,126 — a standalone `MonitoringAdapterSettings` has max_concurrency 1 and dynatrace page_size 1000; the spec-table tool defaults appear only once `MonitoringSettings` applies `with_tool_defaults`. Correct through the section, surprising if the adapter model is used alone.
5. herness/connectors/settings_entities.py:75 — `classes: []` is accepted for cmdb_ci (spec only says "required"); consider `min_length=1`.
6. herness/connectors/settings.py:87,136,268; settings_entities.py:46,113,117 — sibling modules use private settings_base names (`_rule`, `_CONFIG`, `_ReadOnlyDict`, `_check_base_url`, `_check_verify`) (report deviation 6); consider dropping the underscore now that they are cross-module API.
7. tests/unit/connectors/test_settings_sources.py:212-214, 479-481, 549-551 — several parametrised rejection tests pass no `match`, so a rejection for an unrelated reason would still pass. Baselines are proven valid elsewhere, so low risk.

### Assessment
**Task quality:** Approved
**Reasoning:** Every U01-07 ... U01-15 value, bound, key set and message matches the spec, all listed test IDs exist and assert their spec rows, the five T01-01 follow-ups are in place, and all gates pass; remaining items are minor or already deferred by controller ruling.
