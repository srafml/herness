# T01-17 report — Jira search and keys (build agent)

Worktree: D:\herness\.claude\worktrees\agent-a51667e3d02bd34c4 (branch worktree-agent-a51667e3d02bd34c4, base a51221f)
Checkpoint: b294aea `wip(T01-17): jira connector, flatten_issue, projections and cassettes`
Final commit: 694a8e5 `feat(connectors): T01-17 Jira search, keys and raw column contract` (all pre-commit hooks passed, no SKIP)

## Implemented
- `herness/connectors/jira.py` (378/390): `JIRA_FIELDS`, `JIRA_ISSUE_COLUMNS`, `build_jql`, `flatten_issue`,
  `FieldCandidate`, `JiraConnector` (`register("connector","jira")`): check (`GET /rest/api/{3|2}/myself`),
  sync (Cloud `POST /rest/api/3/search/jql` + body nextPageToken under `CursorGuard.step`; DC `POST /rest/api/2/search`
  startAt/total with `expand: ["changelog"]`), list_keys (`fields=["id"]`, `order="key"` JQL, one KEY_SCHEMA batch per
  page), discover_fields (`GET /rest/api/{v}/field`), RowBatcher with `JIRA_ISSUE_COLUMNS + custom ids`.
- `herness/connectors/jira_changelog.py` (55/270): `project_history`, `project_remote_link` only (U01-94).
- `herness/core/registry.py` (147, +1 row): `("connector","jira"): "herness.connectors.jira:JiraConnector"`.
- `herness/connectors/mapping_check.py` (242/260): `# T01-17:` skip removed; jira fetched =
  `JIRA_ISSUE_COLUMNS` ∪ configured `mappings.custom_fields.jira` values (`_jira_custom`). The shipped
  `120_stg_jira.sql` reads only contract columns (check returns `[]`); SQL untouched.
- Cassettes (§13 O-16): `tests/support/jira_pages.py` card-local deterministic generator (T11-15 api_pages is absent)
  → `tests/fixtures/connectors/jira/{cloud_search, cloud_search_no_token, cloud_keys, dc_keys, dc_search, fields}.json`.
  Host `jira.example.test`; tokens start with `synthetic`; JQL/field lists are literals in the generator (independent
  oracle). Regeneration byte-for-byte test included. detect-secrets and fixtures-pii-scan pass (no baseline change).

## Tests
- `tests/unit/connectors/test_jira.py` (UT01-72, UT01-73, UT01-77, UT01-78, UT01-94), `test_jira_flatten.py` (UT01-96 incl.
  the fresh-interpreter import probe: importing `herness.connectors.jira.flatten_issue` loads none of httpx, httpx2,
  herness.connectors.http; and the cassette regeneration check), `_jira_data.py` (cassette Replay over
  httpx2.MockTransport → SourceHttp, seam stubs).
- `test_connector_factory.py`: new `test_ut01_94_jira_resolves_through_the_builtin_table` (real class, custom ids passed,
  SupportsKeyListing, no network, http lazy). `test_mapping_check.py`: skip test narrowed to files;
  new `test_ut01_58_jira_checked_against_raw_column_contract`.
- Acceptance `pytest -k "UT01_72 or UT01_73 or UT01_77 or UT01_78 or UT01_96"`: 59 passed.
- `PYTHONUTF8=1 uv run pytest tests/unit/connectors -q -p no:logging`: 945 passed, 4 skipped.
- Coverage: jira.py 100% line / 100% branch; jira_changelog.py 100% / 100%.
- Gates: ruff format/check clean, mypy 0, lint-imports 13 kept, check_module_size 0, check_type_ownership 0;
  ST01-14 lint test green (unedited).
- RED evidence: before implementation, collection failed `ImportError: cannot import name 'jira' from 'herness.connectors'`.

## Rulings applied
1. No fetch_changelogs / fetch_remote_links / ChangelogState. Seams `_fetch_changelogs`, `_fetch_remote_links` in jira.py
   (marked `# T01-18:`), default `_not_built` raises `ConfigError("jira changelog and remote link fetch is not available in
   this build", source="jira")` (no data). It is called per page before any `batcher.add`, so the first `next()` raises
   before any batch (tested: one search request, no batch). Seam contract used: `_fetch_changelogs(http, *, flavor, issues)`
   and `_fetch_remote_links(http, *, version, issue_ids)` → `Mapping[id, Sequence[history|link]]`; a page result missing an
   issue → `SchemaViolation("incomplete changelog or remote links")`. T01-18 rebinds them (adding its ChangelogState).
2. All HTTP via SourceHttp (get_json/post_json → retry_page, egress client from `http_client`); no URL following;
   CursorGuard.step(token) on Cloud (and step(str(startAt)) on DC for the page limit).
3. JQL only from `jql_scope` + bound datetimes; UTC, strftime `%Y/%m/%d %H:%M` (floors to minute).
4. Registry row added; UT01-94 re-checked with the real class.
5. mapping_check done as above (minimal edit).
6. Cassettes as above.
7. flatten_issue drops changelog author (projection); RowBatcher bounded (tested with batch_rows 16 → 16,16,8); error
   messages carry no ticket text (issue_id only as context, numeric).
8. Budgets met; complexity/args within ruff limits.

## Deviations / clarifications (spec notes)
- Import isolation: `herness.connectors.rows` imports `base`, which re-exports `http_client` from `http` (httpx2). So
  jira.py imports rows/base/http/auth/pyarrow lazily inside functions (`# noqa: PLC0415`). Importing the module is
  httpx-free (subprocess-tested), but CALLING `flatten_issue` imports rows → base → http → httpx2 (no client is built).
  A cleaner fix is a lazy `http_client` re-export in base.py (module `__getattr__`); out of this card's file scope —
  recommend as a follow-up if T11-12 needs call-time isolation.
- `JiraConnector` builds its SourceHttp lazily on first use (construction makes no call and resolves no secret, as
  UT01-94 requires); `build_auth(token_client=)` gets the same egress client.
- Custom field ids are validated in the constructor (ConfigError) and de-duplicated preserving order (two mapped keys
  may name the same column; 120_stg_jira.sql reads such a column once). `flatten_issue` itself keeps the spec behaviour
  (duplicate → flatten_record collision SchemaViolation).
- `build_jql` raises ConfigError for a naive bound (spec says Errors: none; precondition guard).
- `project_history`: `items` present but not a list → SchemaViolation("bad changelog history") (spec silent).
- Ids/keys checked ASCII-only (`[0-9]+`, re.ASCII for `\d`).
- Cloud: a page's token/isLast are validated (and CursorGuard stepped) before its issues are yielded, so a bad page
  never reaches the changelog seam.
- discover_fields: an entry that is not a mapping with string id and name → SchemaViolation("bad field list").

## Concerns
- jira.py is 378/390: T01-18 has 12 lines to rebind the seams (it should import fetch_* from jira_changelog; the two
  seam lines + `_not_built` (6 lines) can be deleted, net room ~20 lines).
- The existing `_settings_data.jira()` helper (not mine) uses the real-looking host acme.atlassian.net.

## Fix round 1 (review Approved; two Minors)
- m1 `jira.py:305`: `issue["fields"]["updated"]` -> `issue["fields"].get("updated")` (fields already validated as a
  mapping by `flatten_issue`), so a missing `updated` gives the typed `SchemaViolation("unparseable timestamp in
  updated")` instead of a bare KeyError (TH01-05). New test `test_ut01_73_issue_without_updated_is_schema_violation`
  (Cloud page, issue without `updated` -> SchemaViolation on the first `next()`, no batch).
- m2 `jira_changelog.py`: both `SchemaViolation`s carry `source="jira"` (module constant `_SOURCE`); messages unchanged
  and data-free. The UT01-96 projection error tests now assert exact messages and `context == {"source": "jira"}`.
- Sizes: jira.py 378/390, jira_changelog.py 56/270. Card tests 63 passed; tests/unit/connectors 946 passed, 4 skipped;
  ruff, format, mypy, lint-imports, check_module_size, check_type_ownership clean.
- Fix commit: 922ab18 `fix(connectors): T01-17 review round 1 (typed missing updated, source on projections)` (real hooks, no SKIP)
