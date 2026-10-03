### Spec Compliance
- ✅ **U01-76 `fetch_changelogs`, `ChangelogState`: Spec pass.**
  - Signature: `herness/connectors/jira_changelog.py:129-135`. `http` is positional; `flavor`, `issues` and `state` are keyword-only.
  - `ChangelogState` (`:48-52`) is a mutable `@dataclass(slots=True)` with `bulk_available: bool = True`.
  - Return value: issue id -> histories, built by `{i: _ordered(raw[i]) for i in ids}` (`:142`). Every id gets an entry (bulk pre-seeds `[]` at `:165`). Each history goes through `project_history`, so `author` is dropped, and the list is sorted by `(created, id)` (`:124-126`).
  - Cloud bulk (`:159-180`): `POST /rest/api/3/changelog/bulkfetch` with `{"issueIdsOrKeys": ids, "maxResults": 1000}`, then `| {"nextPageToken": token}` on later calls. The loop stops when the token is absent.
  - `SourceNotFound` (`:150-156`) sets `bulk_available = False` and logs one WARNING `connectors.jira.bulk_changelog_unavailable`; that page then goes per issue. The log is "once" because the state flips and later pages skip bulk.
  - Per-issue fallback (`:183-201`): `GET /rest/api/3/issue/{id}/changelog` with `startAt` / `maxResults=100`. It stops on `isLast` true, on `startAt+len >= total`, or on empty `values`.
  - DC (`:204-222`): histories come from the search `changelog.histories`. When `total > len`, it calls `GET /rest/api/2/issue/{id}` with `expand=changelog&fields=id`.
  - Shape errors raise `SchemaViolation` with constant messages. The context holds only `source` and the numeric `issue_id`.
- ✅ **U01-77 `fetch_remote_links`: Spec pass.**
  - Signature: `jira_changelog.py:225-227`. `http` is positional; `version` and `issue_ids` are keyword-only.
  - One `GET /rest/api/{version}/issue/{id}/remotelink` per id (`:233`).
  - A body that is not a list gives `SchemaViolation("bad remote link page")` (`:234-235`). A non-mapping element gives "bad remote link" (`:240-244`). Each link is projected by `project_remote_link`.
- ✅ **jira.py wiring (card file): Spec pass.**
  - `_not_built` and the `# T01-18:` seams are gone (grep finds no match in herness/).
  - `self._cl_state = changelogs_api.ChangelogState()` is created per instance (`jira.py:221`), and `self._version` is typed `Literal["2","3"]` (`:220`).
  - `_rows` (`:278-302`) fetches changelogs and links and builds the page's full row list before `sync` adds any of them (`:270`). The "incomplete changelog or remote links" check is kept (`:294-296`).
- ✅ **Env-fix commit b4ff78a: Spec pass, matches the group-ledger rulings exactly.**
  - It touches 3 test files only: `tests/security/test_st10_socket.py` (urlopen through `build_opener(ProxyHandler({}))`), `tests/unit/connectors/test_connector_factory.py` (`PYTHON_KEYRING_BACKEND` in the UT01-94 probe's subprocess env) and `tests/unit/core/test_redact_table.py` (`monkeypatch.setenv` in `dotenv_config` for UT10-43/44).
  - No herness code is touched and no test is skipped, renamed or weakened; IDs and assertions are unchanged. `HTTPS_PROXY` / `no_proxy` are not read, set or unset; the empty `ProxyHandler` is the ruled mechanism.
  - UT01-01/UT01-02 are untouched.
- ⚠️ **Cannot verify offline**: the real Jira Cloud bulkfetch shapes (`changeHistories[].created` string vs epoch, `issueId` string vs number). V-2 tracks this; see focus E and Spec notes.

### Focus checks
- **A. Spec compliance per unit: PASS.** Verified verbatim above. One spec gap is handled stricter than the spec: a DC search issue without a `changelog` object gives `bad changelog page`, which fails closed. Two checks go beyond the spec, both fail-closed:
  - the DC refetch must echo the same `id` and carry at least `total` histories (`jira_changelog.py:217-221`);
  - issue ids must be ASCII digits before they reach a path (`:103-108`).
- **B. TH01-05 probes: PASS.** Every probe used a temporary edit reverted with `git checkout -- <file>`. `git status` is clean at the end.
  - **(1) The watermark never moves past an incomplete changelog.** `jira.py:270` iterates `self._rows(issues)`, which returns a full list (`:302`). So every fetch and check for a page runs before any row of that page reaches `batcher.add`. The runner moves watermarks only after `commit()` (`runner.py:137-138`).
    - Mutation probes, each killed by the test named:
      - DC "fewer than total" check removed: killed by `test_ut01_75_bad_dc_refetch[body4-incomplete changelog]`.
      - DC id-echo check removed: killed by `test_ut01_75_bad_dc_refetch[body3]`.
      - jira.py missing-entry check removed: killed by `test_ut01_72_incomplete_changelog_is_schema_violation[fetch_changelogs]`.
      - bulk `issueId` membership check removed: killed by `test_ut01_74_bad_bulk_pages_are_schema_violations[body3]`.
    - First-`next()` raise tests: `test_ut01_74_bad_bulk_page_fails_closed_before_any_batch`, `test_ut01_76_bad_remote_links_fail_closed_before_any_batch`, and `test_ut01_72_changelog/remote_link_failure_fails_closed_before_any_batch`.
    - "Bulk page without a token while not last": the bulkfetch response has no `isLast` in U01-76, so token absent means end, as the spec says.
  - **(2) Every paging loop is bounded.**
    - Bulk: `CursorGuard.step(token)` (`jira_changelog.py:179`). Removing it is killed by `test_ut01_74_repeated_bulk_token_is_refused`.
    - Per issue: `CursorGuard.step(str(start))` (`:191`), and `start` strictly increases (empty `values` returns). An in-memory probe (scratchpad script; guard cap patched to 25 in memory) fed a server that never sets `isLast` and keeps raising `total`. The call raised `SchemaViolation("page limit exceeded")` after exactly 25 calls, and only the changelog path was requested.
    - Removing the per-issue guard is **not** killed by any test (Minor m1).
    - DC: one refetch, no loop.
  - **(3) Paging is cursor-only.** The token travels in the POST body and `startAt` is an int computed on the client (`:199`). `self`, `nextPage` and remote-link `url`/`self` are never read as request targets: the paths are built only from constants and validated ids (`:162`, `:188`, `:212`, `:233`). The fallback cassette carries a `nextPage` URL, and `Replay` fails on any extra request.
  - **(4) All HTTP goes through `SourceHttp.get_json` / `post_json`.** The `SourceHttp`, `SourceNotFound` and `CursorGuard` imports are lazy. Neither module contains an `httpx`, `httpx2` or `requests` import (grep).
- **C. Budgets: PASS.**
  - `wc -l`: jira.py 373/390, jira_changelog.py 244/270.
  - `tools.check_module_size` exits 0.
  - `ruff check` on both modules and the test files: "All checks passed!" (C901 and PLR0913 included).
  - `ruff format --check` is clean, and `mypy` on both modules reports no issues.
- **D. Tests: PASS.** Every ID has functions with real assertions. Docstrings start with the ID, and `pytestmark` is set (`unit` / `integration`).
  - **UT01-74** (`test_jira_changelog.py:93-125`): the exact request sequence and the `startAt` queries `0, 50, 0, 0`; 57 histories equal to the oracle with no `author`; one WARNING; the state flipped; no bulk call on page 2.
  - **UT01-75** (`:291-308`): refetch with the query `expand=changelog, fields=id`; 60/2/0 complete histories.
  - **UT01-76**:
    - `:367-381`: one call per issue, exact compact projected JSON;
    - `:384-391`: the setting off gives NULL and no calls;
    - `:394-400`: Data Center uses version 2.
  - **IT01-11** (`tests/integration/connectors/test_jira_flow.py:84-216`): real `LakeWriter`, then the T02-13 build. It checks every §4.5 column and its encoding; exact rows in `stg.jira_issue`, `stg.jira_transition` and `stg.jira_link`; and `cast_stats` with failed 0 for the six contract columns.
  - My runs:
    - card tests: **90 passed in 15.66s**, no warnings;
    - coverage run (tests/unit/connectors plus IT01-11): **1180 passed, 3 skipped (pre-existing DuckDB excel / Windows junction skips)**, no warnings;
    - jira.py 100% line / 100% branch (232 statements / 70 branches) and jira_changelog.py 100% / 100% (141 / 44).
  - RED evidence:
    - Unit: an `ImportError` on `ChangelogState`, i.e. a collection error. That is the program's usual new-symbol RED. It is weak on its own, but my mutation probes in B confirm the unit tests discriminate.
    - IT01-11: written after the implementation; its RED came from an injected fault that restores the T01-17 `_not_built` seam. That shows the test depends on the T01-18 wiring, but it does not show that the lake and staging assertions discriminate.
    - My extra probe (later bulk pages' histories dropped) failed IT01-11 at the `10001` changelog assertion. Combined with the exact-value oracles, I judge the IT01-11 evidence **adequate**.
- **E. Bulk `created` as an epoch number: SPEC NOTE, not a finding.**
  - U01-94 says "`history` holds `id` (string) and `created` (string)", and a missing or non-string value gives `SchemaViolation("bad changelog history")`.
  - §4.5 fixes the `changelog` encoding as `project_history` values, and staging parses `created` as the source timestamp string. Accepting epoch numbers would mean normalising them to the ISO form inside `project_history`, which is a §4.5 contract change. That needs the matching T02-13 / T11-12 change before merge (§4.5 preamble), so no card may do it unilaterally.
  - Failing closed with a constant `SchemaViolation` (watermark unchanged, entity failed) is the correct behaviour under the binding spec. The card is "Blocked by V-2 (bulk changelog availability)". The question (and the same one for a numeric `issueId`) should be recorded under §13.3 V-2 for the spec owner.
- **F. Hygiene: PASS, with one note.**
  - Seams and `_not_built` are gone.
  - The T01-17 seam tests were rewritten meaningfully, not deleted. They now cover a failing changelog or remote-link fetch, an incomplete result parametrised over both fetches, and a "same `ChangelogState` on every page" assertion at `test_jira.py` UT01-72 Cloud paging.
  - `ChangelogState` is per instance (`jira.py:221`).
  - The `clock` shadow is not tripped: the new `__init__` line uses no `clock`, and the new code in `sync`/`_rows` uses no module alias.
  - Generator: `render_all()` twice gives equal output, equal to the committed bytes for all 11 files, and `test_ut01_96_cassettes_regenerate_byte_for_byte` passes. Hosts are only `jira.example.test` / `servicedesk.example.test`; there are no e-mail addresses; tokens and `accountId`s start with `synthetic`; the only display name is "Synthetic Person".
  - No `.python-version`, `.superpowers/` or temp files were committed.
  - Subjects: `test(...)`, `wip(T01-18)` (an established program convention, e.g. 2860299) and `feat(connectors)`.
  - Both attribution lines are present on all 3 commits, but see m3 on the model name.

### Strengths
- Errors are typed and fail-closed throughout, with constant messages and numeric-id-only context. Issue ids are validated before they enter a URL path, which closes path injection via source data.
- The DC refetch and bulk `issueId` membership checks go beyond the spec and close real TH01-05 holes: a wrong issue, or a short refetch, can no longer land silently.
- Lazy imports keep the UT01-96 "no HTTP layer on `flatten_issue` import" guarantee.
- Requests are oracle-checked: `Replay` now also checks GET query parameters, so every request (method, path, body, query) is compared against generator literals rather than the code under test.
- IT01-11 is a genuine end-to-end contract test (connector -> real `LakeWriter` -> `120_stg_jira.sql` -> `cast_stats`) with exact expected rows.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- **m1. No test pins the per-issue changelog paging bound.**
  - Where: `herness/connectors/jira_changelog.py:191`.
  - Probe: replacing `guard.step(str(start))` with `pass` left all 89 tests in test_jira_changelog.py and test_jira.py green. Coverage stays 100% because the line still runs on the normal path.
  - Risk: a server that never sets `isLast` and keeps raising `total` would loop forever with no test noticing. The bound also depends on the program-wide `MAX_PAGES_PER_STREAM = 1_000_000` (`http.py:33`).
  - Fix: add a UT01-74 case with a small `CursorGuard` cap, as my scratchpad probe did.
- **m2. Duplicate histories are not rejected.**
  - Where: per-issue `out.extend(values)` / `start += len(values)` at `jira_changelog.py:198-199` and bulk `extend` at `:173`.
  - Failure: a server that ignores `startAt` (returns page 0 again) or repeats a bulk page under a new token produces duplicate history ids in `changelog`. That gives duplicate `stg.jira_transition` rows.
  - Fix: a cheap fail-closed check, either that the echoed `startAt` equals the requested one, or that history ids are unique per issue. Not required by U01-76.
- **m3. Commit trailers name a different model than the project constraint.**
  - Where: all three commits (`git log -3 --format=%B`) carry `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. global-constraints.md requires `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`, which the base history uses (e.g. 25d9914).
  - Both trailer lines (Co-Authored-By, Claude-Session) are present. The builder probably followed its runtime attribution reminder. The controller should rule whether to keep this or amend it at the fix round.
- **m4. A test stub raises a misleading message.**
  - Where: `tests/unit/connectors/test_jira.py:206-209,223-232`. The remote-link failure test stubs `fetch_remote_links` with `_refuse`, which raises `"bad changelog page"`.
  - The test is correct (it asserts the changelog fetch ran first), but the message misleads. `"bad remote link page"` would read true.
- **m5. Latent overflow in the generator's history timestamps.**
  - Where: `tests/support/jira_pages.py:200`. `10 + k // 60 % 24` yields hours 24-33 for `k >= 840`, which is not a valid timestamp.
  - It is not reached today (max 60 histories), but a future cassette with many histories would get malformed timestamps. Fix: `(10 + k // 60) % 24` with the day derived from the same carry.

### Spec notes
1. **V-2 bulk shapes (focus E).** U01-94 requires a string `created`, and the implementation fails closed. Record under §13.3 V-2: "confirm bulkfetch `changeHistories[].created` (string vs epoch-ms) and `issueId` (string vs number) on the tenant; if numeric, rule a normalisation in U01-94 together with T02-13/T11-12 (§4.5 contract change)".
2. **Asymmetric completeness rules.**
   - The per-issue Cloud fallback stops on empty `values` or `isLast` even when fewer than `total` histories were read; U01-76 sanctions this stop, so the result is accepted as is (`jira_changelog.py:200`).
   - The DC refetch, by contrast, raises when it returns fewer than `total` histories (`:219-221`, the builder's addition).
   - The spec owner should decide whether the fallback should also fail closed on a short read, or whether the DC check should be relaxed for concurrent-edit drift.
3. **History ordering compares strings.** Ordering by `(created, id)` compares `created` strings (`:124-126`, same as `flatten_issue` `jira.py:134`). That is only correct while every timestamp carries the same offset (V-2 "service account timezone UTC"), and the `id` tiebreak is lexicographic. The spec could state this explicitly.
4. **Builder clarifications to record in U01-76/U01-77.** All are fail-closed and endorsed:
   - DC refetch id echo and "at least `total`";
   - a DC search issue without `changelog` is a shape error;
   - a 404 on a later bulk page discards the partial bulk result and refetches the page per issue;
   - a per-issue 404 propagates;
   - issue ids must be ASCII digits.
5. **Log event field.** The WARNING carries `source=jira`, while §8.1 lists no fields for this event. It is harmless, and the spec could list it.

### Assessment
**Task quality:** Approved
**Reasoning:** U01-76 and U01-77 match the binding spec verbatim and are wired into `JiraConnector` with one `ChangelogState` per instance. Every TH01-05 fail-closed path is either pinned by a test that kills its mutation or follows directly from the code path; budgets, lint, format, mypy and coverage (100/100) are green, and the env-fix commit is exactly the ruled change. The remaining items are Minor hardening and test gaps, plus a V-2 spec question that does not block this card.

## Fix round 1 re-review
Scope: commit 00f3b2a on top of 51aa96f; Minors m1, m2, m4 and m5 only. m3 is parked by ruling: the harness attribution trailer stays and history is not amended. The new commit carries the same trailers.

`git diff --stat 51aa96f..HEAD` touches 4 files:
- `herness/connectors/jira_changelog.py`
- `tests/support/jira_pages.py`
- `tests/unit/connectors/test_jira.py`
- `tests/unit/connectors/test_jira_changelog.py`

No fixture changed. Probes were temporary edits reverted with `git checkout -- <file>`; `git status` is clean at the end.

- **m1: RESOLVED.** The paging bound is now pinned by tests.
  - New tests: `test_ut01_74_issue_changelog_paging_is_bounded` and `test_ut01_74_bulk_paging_is_bounded` (`tests/unit/connectors/test_jira_changelog.py:227-266`).
  - `_small_guard` monkeypatches `herness.connectors.http.CursorGuard` to `lambda: real(max_pages=N)`. This takes effect because `jira_changelog` imports `CursorGuard` lazily at call time (`jira_changelog.py:162`, `:186`). The tests therefore do not depend on `MAX_PAGES_PER_STREAM`.
  - Per-issue test: the cap is 3 and the server never sets `isLast` while `total = startAt + 2`. Steps for `startAt` 0, 1 and 2 pass; the step for 3 raises `page limit exceeded` after exactly 3 requests, with the replay drained.
  - Probe: replacing `guard.step(str(start))` with `pass` makes this test FAIL. The 4th request hits the replay's "unexpected extra request" assertion, so the run fails instead of hanging. The other 51 tests stay green.
  - Bulk test: the cap is 2 and the token is always new; it raises on the 3rd token after 3 requests.
- **m2: RESOLVED.**
  - `_ordered(raw, issue_id)` (`jira_changelog.py:124-131`) projects the histories, then checks id uniqueness before sorting. A repeat raises `SchemaViolation("duplicate changelog history")` with a constant message and context `{source, issue_id}` only.
  - Coverage: it is called once per issue from `fetch_changelogs` (`:148`), so the bulk, per-issue and DC paths are all covered. The raise happens inside `fetch_changelogs`, so it is still before any batch (fail-closed).
  - New tests: `test_ut01_74_server_ignoring_start_at_is_refused` (page 0 answered again for `startAt=2`; checks the exact context) and `test_ut01_74_repeated_bulk_page_under_new_token_is_refused`.
  - Probe: disabling the check fails exactly those 2 tests (50 others green).
  - Normal paths are unchanged: generator history ids are unique per issue, and all UT01-74/75/76 tests and IT01-11's pages stay green.
- **m4: RESOLVED.** `_refuse_links` raises `"bad remote link page"`, and `test_ut01_72_remote_link_failure_fails_closed` now matches that message (`tests/unit/connectors/test_jira.py:211-236`).
- **m5: RESOLVED.**
  - Fix: `day, hour = divmod(10 + k // 60, 24)` (`tests/support/jira_pages.py:200-201`).
  - Checks: `history(1, 840)` gives `2026-08-02T00:00`, `k=839` gives `2026-08-01T23:59`, and `k=59` is unchanged.
  - `render_all()` equals the committed bytes for all 11 fixtures, and `git diff --stat 51aa96f..HEAD` shows no fixture change.
- **Gates**
  - `wc -l herness/connectors/jira_changelog.py` = 249/270, and `tools.check_module_size` exits 0.
  - `ruff check` on the 4 touched files: "All checks passed!" `ruff format --check`: 4 files already formatted.
  - `uv run mypy`: "Success: no issues found in 362 source files".
  - `pytest tests/unit/connectors/test_jira_changelog.py tests/unit/connectors/test_jira.py tests/unit/connectors/test_jira_flatten.py -q -p no:logging`: **116 passed in 9.58s**, no warnings.
- **New:** nothing. When a test's replay is overrun, the error surfaces as `FatalError("source call failed: AssertionError ...")` through `retry_page`. That is pre-existing test-harness behaviour and harmless.

**Task quality:** Approved
**Reasoning:** All four in-scope Minors are fixed, and each fix is pinned by a test that fails under its mutation. There are no regressions and the gates are green; m3 stays parked by ruling.
