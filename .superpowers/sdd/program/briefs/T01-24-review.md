# T01-24 review: Dataverse connector (verify agent)

Worktree agent-a8efe2a545f0525cc, head 9431bed (base a51221f). Read-only review; mutations were run against a temp copy and the file was restored byte-for-byte (`cmp` ok). `git status` is clean. One ignored `.coverage` file was rewritten by the coverage run.

### Spec Compliance
- ✅ Spec compliant (U01-90, U01-91, UT01-89, UT01-91; UT01-90 and UT01-94 additions)
  1. ✅ select = key, updated, then configured fields, deduplicated in order (`dict.fromkeys`, dataverse.py:163). `$filter` is `<upd> ge <since>` / `<upd> lt <until>` joined by `" and "`. Each stamp goes through `astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")`, which floors to whole seconds. The filter is omitted when there are no bounds, and a naive bound raises ConfigError (:46-51, :168-173). `$orderby = "<upd> asc,<key> asc"` (:171). `Prefer` (maxpagesize + FormattedValue annotation) plus `OData-MaxVersion`/`OData-Version` go on every page, including nextLink pages, because the same `headers` object is reused in the loop (:140-142). Next pages use check_next_url, then `params=None`, then `CursorGuard.step` (:150-151). The code never sends `$skip` or `$top`. `list_keys` sends `$select=<key>` only, with no filter or orderby (:194). Body validation: an object with a list `value` of objects, else SchemaViolation (:63-69). A non-string next link also raises SchemaViolation (:147-149). The key must be a non-empty string, else SchemaViolation with no value echoed (:72-78). `parse_source_timestamp`, payload `json.dumps(row)`, `f`/`f_display` columns (:200-210). `check()` is GET `/api/data/v9.2/WhoAmI`. `name`, `entities` and `watermark_field` are correct. The class has the `register` decorator and the `_BUILTINS` row is present (registry.py:42). The default http is `http_client` + `build_auth` (msal_client_credentials gives `MsalTokenProvider(scope=f"{base_url}/.default")`, asserted in UT01-90) + `SourceHttp`, built lazily (:99-118).
  2. ✅ OData query injection: the query is built only from `_OData`-validated settings (`^[a-z_][a-z0-9_]{0,127}$`: entityset, key_field, updated_field, select) and formatted timestamps. Values go through httpx `params` encoding. Row data never reaches query construction, and next links are passed through only as validated, unmodified URLs.
  3. ✅ Same-origin next links: `check_next_url` (T01-15) accepts only absolute https on the base host and port, with no userinfo, and returns the URL unchanged. UT01-89 asserts `str(request.url) == next_link`. A foreign host raises ForeignHostError before any request (UT01-89 asserts only `org.example.com` was hit), so no bearer is ever sent to a foreign origin.
  4. ✅ No new token handling; auth is all T01-15 `build_auth`. The only client comes from `herness.connectors.http.http_client` (egress). test_connectors_http_lint.py is unchanged (no diff) and passes in the connectors suite.
  5. ✅ Error messages are static strings or carry the field and entity name only. The unknown-entity message echoes the caller's entity name truncated to 64 characters. UT01-89 shows that row text and host are absent from the bad-key, bad-timestamp and 429 errors. The module does no logging. Page size is bounded by the SourceHttp 64 MiB cap, and batches by `batch_rows` (tested: 1,200 rows give 1,000 + 200).
  6. ✅ Fixture hosts are `org.example.com` and `evil.example`. `login.microsoftonline.com` appears only as the `hosts` entry that DataverseSettings.hosts_error requires (U01-12, R-06), and MSAL is faked, so the builder's note holds. Credentials and tokens start with `synthetic`, and GUIDs are zero-pattern.
  7. ✅ The factory test change is sound; see the rulings below.
- ⚠️ Cannot verify from diff: real Dataverse nextLink shape (absolute and same-host) is assumed per Microsoft docs and matches the builder's note. The 1,500 rows/s target is for T01-25 benchmarks.

### Rulings on builder deviations
- Raw OData column names (no `to_snake`): **accepted**. `_OData` names are already lowercase snake, so `to_snake` would be the identity. mapping_check.py:116-118 already uses the raw `select` names plus `_display`, so the two are consistent.
- Private import `rows._text as text` (dataverse.py:26): **accepted as Minor**. It is the U01-23 text rule the spec names `text()`, and duplicating it would be worse. Follow-up for the rows.py owner: add a public alias.
- `_display` columns for key and updated: **accepted**. This is literally what the spec says ("each selected f", and select starts with key and updated). mapping_check's expected set is a subset, so `--check-mapping` is unaffected.
- No per-page row cap: **accepted**. The spec says the ≤ 5,000 limit is enforced by the source, and memory is bounded by the page byte cap and `batch_rows`.
- Factory test (`dataverse` block appended to SOURCES_YAML, `_NO_DATAVERSE_YAML` split for the "not configured" probe): **sound**. If the `"  dataverse:\n"` marker ever goes missing, the split returns the whole YAML and the probe fails loudly instead of passing silently. The semantics are unchanged: the probe still runs against a registered but unconfigured source. Its fragility is limited to the rule "keep dataverse last", which is documented in a comment and in the report.

### Evidence (re-run by verifier)
- `pytest -k "UT01_89 or UT01_91 or UT01_90 or UT01_94" tests/unit/connectors`: 48 passed.
- `pytest tests/unit/connectors -q -p no:logging`: 908 passed, 4 skipped. The skips are environmental: DuckDB excel and symlink permission.
- Coverage of dataverse.py: 100% line, 100% branch (125 statements, 34 branches).
- ruff check: clean. ruff format --check: clean (922 files). mypy: clean (338 files). lint-imports: 13 kept, 0 broken. check_module_size: exit 0 (dataverse.py at 210 of 290 lines).
- Mutations (each one caught, then restored):

| Mutation | Result |
|---|---|
| Headers dropped on next pages | 2 failed |
| `$top` added | 1 failed |
| params kept on next pages | 2 failed |
| check_next_url skipped | 1 failed |
| CursorGuard step removed | loops until the pytest timeout (repeated-link test) |
| `lt` changed to `le` | 2 failed |
| `$orderby` added to list_keys | 1 failed |
| `_display` read from the raw field | 1 failed |

### Strengths
- The spec algorithm is implemented step by step, and the code is compact (210 lines).
- The HTTP layer is lazy, so `build_connector` makes no network call and resolves no secrets (asserted in UT01-94).
- Tests check exact params, headers and URLs on every page, plus redaction. Every probed mutation is killed.

### Issues
#### Critical (Must Fix)
- None.
#### Important (Should Fix)
- None.
#### Minor (Nice to Have)
1. dataverse.py:26: imports the private `rows._text`. Follow-up for the rows.py owner (T01-04 area): add a public `text` alias.
2. dataverse.py:209: `f_display` goes through `text()`, but the spec says raw `row.get(...)`. Formatted values are strings, so this is harmless and keeps the column string-typed. Accepted; mention it in the spec on the next docs pass.
3. dataverse.py:164-167: there is no guard for a select name of more than 119 characters, whose `_display` name would exceed the lake's 128-character column limit. A select name equal to a metadata column (`_source`, `_payload`, and so on; `_OData` allows a leading `_`) is caught only later, by RowBatcher, as SchemaViolation rather than ConfigError. Both are edge cases; a settings validator would be the cleaner home for them.
4. test_connector_factory.py:86-98: every parallel connector card that adds a `_BUILTINS` row edits the same `set(built)` assertion and the same SOURCES_YAML. The merge agent must keep the dataverse block last (as documented).
5. Fixtures are synthetic OData pages built in code (_dataverse_data.py), not respx cassettes under `tests/fixtures/connectors/dataverse/` as brief §11 describes. No sibling connector has created that directory either. Accepted; record it as an O-16 note if cassettes stay a requirement.
6. dataverse.py:92: the parameter `clock` shadows the module alias `clock` (the default is evaluated at definition time). It works and is commented, but readability suffers. Nit.

### Assessment
**Task quality:** Approved
**Reasoning:** U01-90 and U01-91 are implemented as specified. Queries are built only from validated identifiers, next links are same-origin and unmodified, auth and egress are reused from T01-15, and coverage is 100%. Every gate and probed mutation passes. The remaining items are polish and merge coordination.
