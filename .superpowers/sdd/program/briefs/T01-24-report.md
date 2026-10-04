# T01-24 report: Dataverse connector (build agent)

Status: DONE (spec notes below are informational). Worktree branch worktree-agent-a8efe2a545f0525cc, base a51221f.

## Implemented
- `herness/connectors/dataverse.py` (210 lines, budget 290): `DataverseConnector` (U01-90, U01-91), `register("connector", "dataverse")`.
  - Constructor `(settings, *, http: SourceHttp | None = None, clock=clock.now)`. The default HTTP layer is built on first use (no network and no secret resolution at construction, so `build_connector` stays network-free): `http_client(settings, source="dataverse", max_concurrency=...)` + `build_auth(...)` (msal_client_credentials -> `MsalTokenProvider(scope=f"{base_url}/.default")`) + `SourceHttp(breaker_key="dataverse")`.
  - `check()`: `GET /api/data/v9.2/WhoAmI` (OData version headers).
  - `sync`: `$select` = key, updated, then configured `select` (deduplicated); `$filter` = `<upd> ge <since>` / `<upd> lt <until>` joined by ` and ` (UTC, `%Y-%m-%dT%H:%M:%SZ`, floored to seconds; omitted when no bounds); `$orderby = "<upd> asc,<key> asc"`; headers `Prefer: odata.maxpagesize=<page_size>,odata.include-annotations="OData.Community.Display.V1.FormattedValue"`, `OData-MaxVersion: 4.0`, `OData-Version: 4.0` on every page. Body must be an object with a list of objects `value`; key must be a non-empty string; `parse_source_timestamp(row[upd])`; `_payload = json.dumps(row)`; per selected `f` columns `f` (U01-23 text rule) and `f_display` (FormattedValue annotation). `@odata.nextLink`: must be a string -> `check_next_url(next)` -> `params=None` -> `CursorGuard.step(next)`. Batches via `RowBatcher` (<= batch_rows). Never `$skip`/`$top`.
  - `list_keys`: same paging/headers, `$select=<key>` only, no `$filter`/`$orderby`; one `KEY_SCHEMA` batch per non-empty page; raises, never ends early.
  - Errors: unknown entity / naive bound / select colliding with a `_display` column -> ConfigError; shape, key, timestamp, next link -> SchemaViolation (no row text, token or host in messages); HTTP mapping (429 RateLimited with Retry-After seconds, 401/403 AuthError, ...) from `SourceHttp`.
- `herness/core/registry.py` (147/160): one `_BUILTINS` row `("connector","dataverse") -> "herness.connectors.dataverse:DataverseConnector"`.
- Tests: `tests/unit/connectors/test_dataverse.py` (UT01-89 x 12 functions, UT01-91 x 2, UT01-90 x 4), `tests/unit/connectors/_dataverse_data.py` (settings, synthetic OData pages, scripted mock server). Hosts: `org.example.com`, `evil.example`; credentials/tokens all start with `synthetic` (R-67). `login.microsoftonline.com` appears only as the required `hosts` entry (U01-12 validator); MSAL is faked, nothing reaches it.
- `tests/unit/connectors/test_connector_factory.py` (UT01-94): see factory-test change below; new `test_ut01_94_dataverse_resolves_through_the_builtin_table`.

## Factory-test change (for the merge agent)
Adding the builtin row made `test_ut01_94_every_registered_connector_builds_without_network` fail (`registry.available("connector")` now lists `dataverse`, and the cfg fixture had no dataverse section -> "source dataverse is not configured"). Fix: a dataverse section was added at the end of `SOURCES_YAML` (marked `# T01-24:`), the fixture registers `DataverseConnector`, the every-registered test expects `dataverse` and asserts `SupportsKeyListing` for it. The cfg fixture now leaves no source unconfigured, so the "not configured" probe keeps `dataverse` but runs against a second config loaded from `_NO_DATAVERSE_YAML` (SOURCES_YAML without the dataverse block) under `tmp_path/"bare"`; assertion semantics unchanged. If another group adds a source to SOURCES_YAML, append it before the dataverse block (the split relies on dataverse being last).

## Spec notes
1. check_next_url same-origin confirmation: Dataverse `@odata.nextLink` is an absolute https URL on the `base_url` host (e.g. `https://org.example.com/api/data/v9.2/cr123_projects?$select=...&$skiptoken=...`). `SourceHttp.check_next_url` accepts exactly absolute https URLs on the base host+port without userinfo and returns it unchanged; `get_json(url, params=None)` sends it unmodified (UT01-89 asserts `str(request.url) == next_link`). A foreign-host link raises ForeignHostError before any request (UT01-89). No change to http.py was needed.
2. Column names: spec says "columns f and f_display"; implemented with the raw OData names (not `to_snake`), matching `mapping_check._fetched` (raw `select` names + `_display`). OData identifiers (`^[a-z_][a-z0-9_]{0,127}$`) satisfy the lake COLUMN_RE, except that a 120+ char name plus `_display` would exceed 128 chars (lake would reject; not guarded here). Values use the U01-23 text rule, imported as `rows._text` (private name; no public alias exists - a public `text` in rows.py would be cleaner, owned by T01-04 area).
3. mapping_check._fetched names `key_field`/`updated_field` without `_display`; the connector also emits `<key>_display`/`<updated>_display` (spec: every selected field, and select starts with key and updated). Harmless superset.
4. Rows per page are not checked against page_size (spec: enforced by the source; memory bounded by the 64 MiB page cap).

## Evidence
- RED: `pytest tests/unit/connectors/test_dataverse.py` without the module -> `ModuleNotFoundError: No module named 'herness.connectors.dataverse'` (collection error).
- GREEN: `pytest -k "UT01_89 or UT01_91"` (test_dataverse.py) 22 passed; test_dataverse.py + test_connector_factory.py 39 passed; `pytest tests/unit/connectors -q -p no:logging` 907 passed, 4 skipped; registry/mapping_check selection 127 passed.
- Coverage dataverse.py: 100% line, 100% branch (125 stmts, 34 branches).
- Gates: ruff check . clean; ruff format --check . clean; mypy (338 files) clean; lint-imports 13 kept 0 broken; check_module_size exit 0; check_type_ownership exit 0. test_connectors_http_lint.py untouched and passing.

## Commits
- 6540dc0 wip(T01-24): Dataverse connector, tests and registry row (all pre-commit hooks passed incl. pytest-unit, detect-secrets)
- 9431bed feat(connectors): T01-24 Dataverse connector (adds the guard test; all hooks passed)
