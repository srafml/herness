# T01-17 review — Jira search and keys (verify agent)

Worktree D:\herness\.claude\worktrees\agent-a51667e3d02bd34c4, head 694a8e5 (base a51221f). Read-only review.

### Spec Compliance
- ✅ U01-71 JiraConnector / JIRA_FIELDS / JIRA_ISSUE_COLUMNS: constants match the spec text exactly (jira.py:40-44); `register("connector","jira")` (jira.py:207) plus one registry row (registry.py:43); name/entities/watermark_field("updated"); check() = GET /rest/api/{3|2}/myself; custom ids validated in the ctor (ConfigError); never `*all`.
- ✅ U01-72 build_jql: built only from validated `jql_scope` (settings: <=2000 chars, no ORDER BY, no line breaks) and the bound datetimes; never from response data. UTC via astimezone, minute floor via strftime `%Y/%m/%d %H:%M`; absent clauses omitted; `ORDER BY updated ASC, id ASC` / `(<scope>) ORDER BY id ASC` / `ORDER BY id ASC` (jira.py:101-118). Naive bound -> ConfigError (accepted deviation, see notes).
- ✅ U01-73 sync: Cloud POST /rest/api/3/search/jql with body nextPageToken; `last = isLast is True or (isLast absent and no token)`; `not last` without token -> SchemaViolation("missing nextPageToken"); short non-last page continues; CursorGuard.step(token) (jira.py:340-360). DC POST /rest/api/2/search with startAt/total, expand ["changelog"] only for sync, stop on empty issues or startAt >= total (jira.py:362-378). All HTTP via SourceHttp.post_json/get_json (retry_page, egress client from http_client); no URL following; no hand-built clients; ST01-14 lint untouched. RowBatcher columns = JIRA_ISSUE_COLUMNS + custom ids; payload compact JSON. Changelog/remote-link fetch through the ruled `# T01-18:` seams; default fails closed before any batch (jira.py:73-81, 290-294; tested).
- ✅ U01-74 list_keys: order="key" JQL, fields ["id"], no expand, one KEY_SCHEMA batch per page, ids validated.
- ✅ U01-75 discover_fields/FieldCandidate: GET /rest/api/{v}/field, non-list -> SchemaViolation, rules per spec, sorted (suggested_key, name); writes nothing (ops store row counts unchanged, no raw dir).
- ✅ U01-93 flatten_issue: id (ASCII digits) / key / fields checks with spec messages, custom id ConfigError, keys exactly JIRA_ISSUE_COLUMNS + custom ids in order, absent -> None, changelog projected + sorted (created, id) compact JSON without author, remotelinks projected or None. Pure (input-not-mutated test).
- ✅ U01-94 project_history / project_remote_link: only these two in jira_changelog.py (ruling applied; no fetch_*/ChangelogState); author/application/relationship/icon dropped; idempotent; spec error messages.
- ⚠️ Cannot verify here: real Jira DC acceptance of `"expand": ["changelog"]` as a JSON array (spec-literal); T01-18 seam rebinding (out of card); end-to-end runner watermark behaviour with the fail-closed seam (covered by "raises before first batch", which under U01-39 means no commit and no watermark move).

### Focus checks
1. JQL: ✅ (see U01-72). Tests pin exact text incl. +02:00 -> UTC and 59.9 s -> minute floor.
2. Pagination/egress: ✅. DC also steps CursorGuard with str(startAt) for the page cap (harmless; startAt strictly increases).
3. jira_changelog.py: ✅ 55 lines, two functions only.
4. flatten_issue / rows: ✅ contract; RowBatcher bounded (16,16,8 test); error messages carry no ticket text (numeric issue_id context only; custom id truncated to 64).
5. Cassettes: ✅ only https://jira.example.test and https://wiki.example.test; no atlassian.net; tokens `synthetic-*`; byte-for-byte regeneration test.
6. Watermarks: ✅ watermark_field "updated"; ascending (updated, id) via JQL; seam default raises before any yield.
7. Tests: ✅ UT01-72 (37-issue isLast:false page then isLast:true, exact bodies), UT01-73 (cassette without token -> SchemaViolation, before the seam call), UT01-77 (ops store untouched), UT01-78 (Cloud 155 ids / DC 120 ids), UT01-96 (every listed case incl. ADF, out-of-order authored histories, "12a", cf_1), UT01-94 (factory with real class), UT01-58 (mapping check). Import-isolation probe runs a fresh interpreter and asserts httpx/httpx2/herness.connectors.http absent: meaningful for module import.
   Run: `PYTHONUTF8=1 uv run pytest tests/unit/connectors -q -p no:logging -k "UT01_72 or UT01_73 or UT01_77 or UT01_78 or UT01_96 or UT01_94 or UT01_58"` -> 88 passed, 861 deselected, no warnings. Coverage (branch): jira.py 100% (234 stmts, 70 branches), jira_changelog.py 100% (22 stmts, 6 branches).
8. Budgets: jira.py 378/390, jira_changelog.py 55/270, registry.py 147/160, mapping_check.py 242/260. ruff C901/PLR0912/PLR0913/PLR0915 clean. Layering: jira (L2) imports only settings/core at module level; mapping_check -> jira is L2->L2 (lint-imports 13 kept per report).

### Strengths
- Tight, spec-literal implementation; independent oracle (JQL and field lists as literals in the generator) rather than self-comparison.
- Fail-closed seam well tested (one search request, no batch, message holds no data).
- Thorough shape tests for Cloud/DC pages and field lists.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. jira.py:305 `issue["fields"]["updated"]` raises a bare KeyError when a search issue lacks `fields.updated` (flatten_issue only checks that `fields` is a mapping). Fail-closed (nothing yielded for that page), but not the section 6 `SchemaViolation` for shapes, and it escapes `except HernessError` handlers. Spec text uses the subscript literally; `issue["fields"].get("updated")` routes through parse_source_timestamp, which raises `SchemaViolation("unparseable timestamp in updated")` for None (probed). Add a test.
2. jira_changelog.py:35 and :54 raise SchemaViolation without `source="jira"` (the flatten_record collision likewise); the rest of jira.py tags the source. Error-context polish.
3. mapping_check.py:130 `_jira_custom` derives the custom ids via `model_dump().values()` while factory.py:39-41 derives them via its `_JIRA_FIELDS` key tuple; two derivations of one contract can drift if the mappings model gains a non-id member. Consider one shared helper.
4. jira.py:222 parameter `clock` shadows the module alias `clock` (default evaluated at def time, so correct, and commented). Renaming the import would remove the trap for T01-18 edits.
5. jira.py:142-143 `_history_order` compares `id` as text, so equal-`created` histories "9" and "10" sort "10" first. Spec says (created, id) without a type; deterministic either way (spec note).

### Builder deviations (judgement)
- build_jql ConfigError on a naive datetime: accepted. The precondition is "aware"; a typed guard beats silently treating local time as UTC.
- Custom id de-dup in the ctor: accepted. Two mapping keys naming one field would otherwise fail every sync with a column collision; mapping_check uses a set, the factory passes the tuple, so connector, staging and check agree. flatten_issue keeps the spec collision behaviour (tested).
- Call-time httpx2 import via rows -> base -> http: accepted for this card (module import is httpx-free and tested; no client is constructed on call). Follow-up: a lazy `http_client` re-export in base.py if T11-12 needs call-time isolation; flag on T11-12.
- Also fine: items-not-list -> "bad changelog history"; ASCII-only digits; Cloud token/isLast validated before issues are yielded; discover_fields entry shape check.

### Spec notes
- U01-72 Errors: add ConfigError for a naive bound.
- U01-93 step 5: state whether history `id` ordering is textual or numeric, and that `created` is compared as the source string (correct only under the section 13 V-2 UTC service-account precondition).
- U01-94: spec silent on `items` present but not a list; implementation raises "bad changelog history"; record it.
- U01-73 step 5: use `.get("updated")` so a missing member is a SchemaViolation (Minor 1).
- Brief extraction repeats the jira_changelog.py module-map row three times (cosmetic).

### Assessment
**Task quality:** Approved
**Reasoning:** All units match the spec and the sub-controller rulings; every Tests-row ID has real assertions and both modules are at 100% line/branch coverage. Remaining items are Minor (error typing on a missing `updated`, error context, DRY of custom-id derivation).
