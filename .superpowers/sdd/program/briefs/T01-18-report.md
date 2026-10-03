# T01-18 report: Jira changelog and remote links (build agent)

Worktree /home/user/herness, branch claude/w28-s01-T01-18, base bff6f4b.
Commits (oldest first): b4ff78a env fix, 3cddeb0 wip, 51aa96f final (adds IT01-11).

## What I implemented
- `herness/connectors/jira_changelog.py` (244/270 lines). U01-76 `ChangelogState` (a mutable slots dataclass with `bulk_available: bool = True`) and `fetch_changelogs(http, *, flavor, issues, state)`:
  - Cloud: `POST /rest/api/3/changelog/bulkfetch` with `{"issueIdsOrKeys": ids, "maxResults": 1000}`, plus the body `nextPageToken` after the first call, guarded by `CursorGuard`. Histories are appended to their `issueId`, which must be one of the page's ids. The loop stops when there is no token.
  - On `SourceNotFound`, from any bulk page: `state.bulk_available = False`, one WARNING `connectors.jira.bulk_changelog_unavailable` (`source=jira`; logged once, at the flip), and the partial bulk result is discarded. Every issue of that page is then read per issue with `GET /rest/api/3/issue/{id}/changelog` (`startAt=<n>`, `maxResults=100`, `startAt += len(values)`, `CursorGuard` on `startAt`). Paging stops on `isLast` true, `startAt + len(values) >= total`, or empty `values`.
  - DC: histories come from the search `changelog.histories`. When `changelog.total > len(histories)`, the issue is refetched with `GET /rest/api/2/issue/{id}?expand=changelog&fields=id` and its `changelog.histories` are used.
  - Postconditions: every issue id has an entry; each history goes through `project_history` (`author` dropped) and the list is sorted ascending by `(created, id)`.
- U01-77 `fetch_remote_links(http, *, version, issue_ids)`: one `GET /rest/api/{version}/issue/{id}/remotelink` per id. The body must be a list (else `SchemaViolation("bad remote link page")`). Each element must be a mapping (else "bad remote link") and is projected by `project_remote_link`. URLs are stored as data and never requested.
- Shared checks: an issue id must be ASCII digits before it enters a request path ("bad issue id"). Pages and changelog objects must be mappings, lists must be lists of mappings, `total` must be a non-negative int, `isLast` must be a bool or absent; anything else is "bad changelog page". Every error is a `SchemaViolation` with context `source="jira"` and, where known, the numeric `issue_id` only (no ticket text). `SourceHttp` and `CursorGuard` are imported lazily inside the functions, so importing `flatten_issue` still loads no HTTP layer (the UT01-96 fresh-interpreter probe passes).
- `herness/connectors/jira.py` (373/390 lines):
  - `_not_built` and the `_fetch_changelogs` / `_fetch_remote_links` seams are deleted.
  - The module imports `jira_changelog as changelogs_api` and calls `changelogs_api.fetch_changelogs(http, flavor=..., issues=..., state=self._cl_state)` and `changelogs_api.fetch_remote_links(http, version=self._version, issue_ids=ids)`, per search page, before any `batcher.add`.
  - `self._cl_state = ChangelogState()` is created per connector instance. `self._version` is typed `Literal["2", "3"]`.
  - The fail-closed check (an issue missing from either result gives "incomplete changelog or remote links") is kept, so any shape error or incomplete changelog raises before the page's batch is yielded (TH01-05).
  - The `clock` parameter shadow in `__init__` is untouched; the new code does not use the module alias.
- Cassettes: I extended the card-local generator `tests/support/jira_pages.py` and committed the output under `tests/fixtures/connectors/jira/`. All hosts are `*.example.test`, tokens start with `synthetic`, and history authors are `Synthetic Person` (dropped by the projection). The existing 6 files are byte-identical; the regeneration test is green.
  - `cloud_changelog_bulk.json`: bulk changelog over 2 pages with `nextPageToken`. Issue 1's histories are split across the pages and given newest first; issue 3 has none.
  - `cloud_changelog_fallback.json`: 2 search pages; bulk answers 404. Issue 1 has 57 histories, read over pages of 50 + 7 (the server caps at 50; the body carries `self`/`nextPage` URLs that are never followed). Issue 2 has 3 histories with `isLast` absent. The second search page makes no bulk call, and issue 3 returns empty `values`.
  - `dc_changelog.json`: DC search over 2 `startAt` pages; issue 1 has 40 of `total` 60 histories, followed by the refetch.
  - `cloud_remote_links.json`: one remote-link call per issue (2, 1 and 0 links, each with application, relationship, self and icon members, all dropped).
  - `cloud_sync_it.json`: IT01-11. Two search pages with the synth profile's custom fields, bulk changelog pages, remote links, issue links and status walks.
  - New generator constants: `CLOUD_BULK`, `BULK_TOKEN`, `SYNTH_CUSTOM_FIELDS` (story_points `customfield_10016`, team `customfield_10060`, estimate_cost_usd `customfield_10050`, epic_link null; impl 11 U11-08) and `SYNTH_FIELD_IDS`.
- Test helper `tests/unit/connectors/_jira_data.py`:
  - `Replay` now also checks the GET query against the recorded `params` (no query when none is recorded) and records `queries`.
  - New helpers: `gets(...)` and `source_http(replay)`.
  - `Seams` now monkeypatches `jira_changelog.fetch_changelogs` / `fetch_remote_links` (accepting `state`) and records the states it was passed.

## Tests
- New `tests/unit/connectors/test_jira_changelog.py`, 48 tests:
  - UT01-74:
    - the 404 fallback with 57 histories over 2 pages: exact request sequence, bodies and queries; 57 entries with no author; one WARNING; the state flipped; no bulk call on the next page;
    - bulk pages appended and sorted, with `[]` for an issue without histories;
    - a 404 on a later bulk page refetches the whole page;
    - state off makes no bulk call, and an empty page makes no call;
    - malformed bulk pages (8 cases) and a repeated token;
    - malformed per-issue pages (7 cases, context `{source, issue_id}`);
    - a per-issue 404 is not swallowed;
    - bad issue ids never reach a path;
    - fail-closed before the first batch.
  - UT01-75: DC refetch (exact requests and query, 60/2/0 complete histories, no author); bad DC search changelog (5 cases); bad refetch (5 cases, including id mismatch and "incomplete changelog").
  - UT01-76:
    - one call per issue, exact projected compact JSON with application, relationship, self and icon absent;
    - with the setting false, the column is NULL and no call is made;
    - DC uses version 2;
    - bad bodies (4 cases);
    - bad ids;
    - fail-closed before the first batch.
- New `tests/integration/connectors/test_jira_flow.py`, IT01-11. The migrated ops store is bound as the resilience backend. `JiraConnector` replays `cloud_sync_it.json` through `SourceHttp` with remote links on and the synth custom-field ids (derived from `CustomFieldsConfig` in mappings order), and the batches are written by the real `LakeWriter`. Checks:
  - lake: every §4.5 column is present in contract order, contract columns are strings, the metadata is right, `_payload` is the compact issue, object and array fields are compact JSON, timestamps are unchanged strings, the ADF description is JSON, custom fields follow the U01-23 text rules, the changelog is compact, ascending and author-free, and remote links are projected or `[]`;
  - after the T02-13 build (`120_stg_jira.sql`), exact rows in `stg.jira_issue` (types, categories, timestamps, story points, cost, team), `stg.jira_transition` (6 status transitions; a labels-only history yields none) and `stg.jira_link` (issue link plus 3 INC mentions);
  - `stg.cast_stats`: failed 0 for type, status_category, created_at, resolved_at, story_points and estimate_cost_usd.
- Updated `tests/unit/connectors/test_jira.py` (T01-17 tests):
  - the seam fail-closed tests are now "a changelog / remote-link failure raises before any batch";
  - the incomplete-result test is parametrised over the changelog and remote-link results;
  - the Cloud paging test asserts that one `ChangelogState` is passed on every page.

### RED evidence
- `uv run pytest tests/unit/connectors/test_jira_changelog.py -q -p no:logging -k "UT01_74 or UT01_75 or UT01_76"`, run before the implementation:
  `E   ImportError: cannot import name 'ChangelogState' from 'herness.connectors.jira_changelog'` / `1 error in 0.37s`
- IT01-11 was written after the unit implementation. To show it is meaningful, I ran it with a temporary pytest plugin kept outside the repo (/tmp/w28-s01/builder/mut_plugin.py) that restores the T01-17 fail-closed seam default:
  `PYTHONPATH=/tmp/w28-s01/builder uv run pytest -p mut_plugin -m integration -k IT01_11 tests/integration/connectors`
  Result: `E herness.core.errors.ConfigError: jira changelog and remote link fetch is not available in this build` / `1 failed`.

### GREEN evidence
- `uv run pytest tests/unit/connectors/test_jira_changelog.py -k "UT01_74 or UT01_75 or UT01_76"`: 48 passed.
- `uv run pytest -m integration -k IT01_11 tests/integration/connectors`: 1 passed.
- Jira unit files (test_jira, test_jira_flatten, test_jira_changelog): 112 passed.
- `uv run pytest tests/unit/connectors --cov=herness.connectors.jira --cov=herness.connectors.jira_changelog --cov-branch`: 1178 passed, 1 failed (environmental, see below), 3 skipped. Coverage: jira.py 100% line / 100% branch (232 statements, 70 branches); jira_changelog.py 100% / 100% (141 statements, 44 branches).
- `tests/integration/connectors` + `tests/integration/model/test_model_stg_jira.py`: 23 passed, 2 failed (environmental, see below).

### Gates
`ruff format .` and `ruff check --fix .` are clean. `mypy` (362 files) reports 0 errors. `lint-imports`: 13 contracts kept. `check_type_ownership` and `check_module_size` both exit 0. detect-secrets and fixtures-pii-scan pass with no baseline change.

### Line counts vs budgets
- jira.py: 373/390
- jira_changelog.py: 244/270

## Deviations / clarifications (spec notes)
1. Env fixes, following the coordinator's rulings. All are pre-existing at bff6f4b and environmental (Linux cloud runner). They landed in ONE commit, b4ff78a, before the card commits: the pytest-unit hook runs with -x, so either half alone fails on the other's test. The coordinator agreed to the single commit.
   - `tests/security/test_st10_socket.py::test_st10_08`: the runner's HTTPS_PROXY points at a loopback proxy. urlopen tunnelled through it, the guard admits loopback, and the proxy answered 403, so the test got URLError instead of EgressBlocked. The urlopen case now goes through `build_opener(ProxyHandler({}))`. Name, ID and assertion are unchanged.
   - `tests/unit/connectors/test_connector_factory.py::test_ut01_94_build_connector_files_without_prior_import`, `tests/unit/core/test_redact_table.py::test_ut10_44_workers_1_and_4_give_identical_output` and `::test_ut10_43_pool_keeps_at_most_two_chunks_per_worker_in_flight`: their child processes (a fresh-interpreter probe and spawn pools) hit the Linux default keyring backend (`keyring.backends.fail`), which raises. Those children now get `PYTHON_KEYRING_BACKEND=tests.support.fake_keyring.MemoryKeyring`: through the subprocess env for the probe, and through `monkeypatch.setenv` in the `dotenv_config` helper for the pools. No herness code or runner environment change. Names, IDs and assertions are unchanged.
   - While that commit ran, the untracked card files were parked under /tmp/w28-s01/builder/park and restored straight after; `git status` confirmed every card file was back.
2. U01-76 DC: the spec does not say what happens when a search issue lacks a `changelog` object (`expand=changelog` was requested). I treat that as a shape error ("bad changelog page"), which is fail-closed.
3. U01-76 DC refetch: the refetched body must carry the same `id` (else "bad changelog page") and at least the search's `total` histories (else `SchemaViolation("incomplete changelog")`). The spec just says "use its histories"; these checks add the TH01-05 fail-closed guarantee.
4. U01-76 Cloud bulk:
   - an `issueId` not in the request, or a non-string id, is "bad changelog page";
   - a 404 on a later bulk page discards the partial result and refetches the whole page per issue ("go to step 2 for this page");
   - a 404 on the per-issue endpoint is not caught (entity failed, watermark unchanged).
5. Per-issue paging: `isLast` must be a bool or absent; `total` must be a non-negative int. The next `startAt` is computed client-side, and the server's `nextPage` URL is ignored.
6. U01-77 validates each issue id as ASCII digits before building the path (the spec precondition only says "fetch_remote_links is true"), because the ids come from source data and go into a URL path.
7. Tests: `Replay` now also checks GET query parameters; the T01-17 seam tests were rewritten as described above.

## Concerns
- Unverified spec risk (V-2): from memory, the Atlassian docs example for `POST /rest/api/3/changelog/bulkfetch` shows `changeHistories[].created` as an epoch number, not an ISO string. developer.atlassian.com is blocked by the egress proxy, so I could not check. U01-94 `project_history` requires a string `created`, so if the real tenant returns numbers, every bulk sync fails closed with "bad changelog history" (no wrong data, but no sync). Recommend confirming during V-2 / Q2 and, if needed, a spec ruling to normalise epoch-ms `created` to the ISO form in `project_history`.
- Environment failures, not touched (coordinator ruling: carry-over for the spec owner): `tests/integration/connectors/test_settings_base_imports.py::test_ut01_01_r03_settings_base_imports_in_fresh_interpreter` and `tests/integration/connectors/test_settings_imports.py::test_ut01_02_r03_settings_imports_in_fresh_interpreter`. The R-03 fresh-interpreter probe finds the extra module `_sysconfigdata__x86_64-linux-gnu`. Every Linux CPython imports a `_sysconfigdata__*` module through sysconfig; Windows has none. Whether to allow it in the R-03 list is the spec owner's call.
- Full unit suite, run once on this runner before the env fixes with ST10-08 deselected: 9772 passed, 3 failed (the three keyring tests above, since fixed). After the fixes, the pytest-unit hook passed on b4ff78a and on 3cddeb0.

## Commit status
- b4ff78a `test(security,core,connectors): proxy-hermetic ST10-08 and fake keyring for spawned children (T01-18 env fix)`: all 13 applicable hooks passed, including pytest-unit; no SKIP.
- 3cddeb0 `wip(T01-18): fetch_changelogs, fetch_remote_links, cassettes and unit tests`: all 15 hooks passed, including detect-secrets, fixtures-pii-scan and pytest-unit; no SKIP; no baseline change.
- 51aa96f `feat(connectors): T01-18 Jira changelog and remote links`: all hooks passed, including pytest-unit; no SKIP. It adds `tests/integration/connectors/test_jira_flow.py` (IT01-11).

## Fix round 1 (review Approved; 5 Minors; m3 parked by ruling)
- m1, the paging bound is now pinned by tests. `CursorGuard` is already injectable: `jira_changelog` imports it lazily from `herness.connectors.http` at call time, so the tests monkeypatch `herness.connectors.http.CursorGuard` to `CursorGuard(max_pages=N)`. No code change was needed.
  - `test_ut01_74_issue_changelog_paging_is_bounded`: a per-issue server that never sets `isLast` and keeps raising `total`, with a cap of 3. Expected: `SchemaViolation("page limit exceeded")` after exactly 3 requests.
  - `test_ut01_74_bulk_paging_is_bounded`: always a new token, cap 2. Expected: the same error after 3 requests.
  - Mutation probe: replacing the per-issue `guard.step(str(start))` with `pass` fails the per-issue bound test (the run stops on the replay's unexpected-request assertion, not a hang).
- m2, duplicate histories are refused. `_ordered(raw, issue_id)` checks that the assembled, projected history ids of an issue are unique before sorting. Otherwise it raises `SchemaViolation("duplicate changelog history", source="jira", issue_id=<id>)`. One check covers the bulk, per-issue and DC paths.
  - New tests: `test_ut01_74_server_ignoring_start_at_is_refused` (the server answers page 0 again for startAt=2) and `test_ut01_74_repeated_bulk_page_under_new_token_is_refused`.
  - Mutation probe: disabling the check fails both tests.
- m4: the remote-link failure stub in `test_jira.py` now raises "bad remote link page" (`_refuse_links`), and the test matches that message.
- m5: the generator's history timestamps now carry the hour into the day: `day, hour = divmod(10 + k // 60, 24)`. Outputs are identical for every committed history, regeneration leaves all 11 fixtures byte-identical (`git status` shows no fixture change), and k=840 gives 2026-08-02T00:00.
- Gates:
  - ruff format/check clean; mypy 0 errors in 362 files; lint-imports 13 kept; check_type_ownership and check_module_size both 0.
  - Card tests (`-k "UT01_74 or UT01_75 or UT01_76"`): 52 passed. IT01-11: 1 passed.
  - `tests/unit/connectors`: 1183 passed, 3 skipped.
  - Coverage: jira.py 100% line / 100% branch (232 statements, 70 branches); jira_changelog.py 100% / 100% (145 statements, 46 branches).
- Sizes: jira.py 373/390, jira_changelog.py 249/270.
- Commit: 00f3b2a `fix(connectors): T01-18 review round 1 (paging bound test, duplicate histories, stub message, generator carry)`. All hooks passed, including pytest-unit; no SKIP.
