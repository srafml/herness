# T02-08 review: Vector store (commit 4a6e296)

Reviewer: verify agent. Scope: 6592a39..4a6e296 only (uncommitted warehouse.py work in the worktree ignored).

### Spec Compliance
- ✅ Spec compliant (U02-63…U02-70, TH02-08, TH02-09).
  - U02-63: schemas match verbatim (names, order, types, nullability; `timestamp("us", tz="UTC")`, `fixed_size_list(float32, 1024)`); `EMBEDDING_DIM = 1024`.
  - U02-64: default path `data_layout().vectors`, mkdir + `lancedb.connect(str(path))`, connect failure -> `SchemaViolation("cannot open vector store")`, no table created on init.
  - U02-65: create missing tables with the declared schema, verify names+types both ways, exact error text `vector table <name> schema mismatch: <field>`, "already exists" race tolerated by re-listing, `store.vectors.table_created` logged.
  - U02-66/U02-69: name allowlist -> `ConfigError`; missing -> `NotFoundError(kind="vector_table", key=name)`.
  - U02-67: column allowlist per table; ids validated (pattern `[A-Za-z0-9_:.|-]{1,256}` with `fullmatch`, 1..100,000) before any deletion; 500-id `IN` chunks; returns step-2 count; `store.vectors.deleted` (table, rows); conflict -> `StoreBusy`.
  - U02-68: uses the spec's own fallback `optimize(cleanup_older_than=timedelta(0), delete_unverified=True)`; `store.vectors.history_purged`; conflict -> `StoreBusy`, other -> `SchemaViolation`.
  - U02-70: down / degraded / ok, never raises, class name as the reason.
- Builder deviations, judged:
  1. OI-12 fallback: **accepted**. U02-68 explicitly allows `optimize(...)` to replace both steps. Confirmed: lancedb 0.39.0 is pinned, and `pylance` is absent from uv.lock. My own probe (scratch, deleted): I inserted a row with a distinctive id, deleted it and purged. Before the purge the id bytes were present in a data file, a `.txn` file and a manifest. After the purge **no file under the vectors dir contains the id**, and exactly 1 version remains. TH02-09 is mitigated at the file level, not only through checkout. Recommend closing OI-12 in the spec with this call.
  2. `list_tables()` with page tokens in place of `table_names()`: **accepted**. Same semantics; it avoids a DeprecationWarning, which would become an error under `filterwarnings=error:::herness`, and the default limit of 10. Pagination is tested. The spec text of U02-65/U02-70 should be updated.
  3. Commit conflict detected by message text -> `StoreBusy`: **accepted with ⚠️**. LanceDB 0.39 has no typed conflict exception. My probe of a delete through a stale second handle rebased automatically without error, so real conflicts are rare and this path is only tested with monkeypatched exceptions.
  4. Extra `ConfigError` checks in `delete_ids` (a bare `str` for ids, non-str items): **accepted**. They enforce the spec's "every id matches the pattern" precondition. A bare `str` would otherwise be split into single-character ids, a real hazard.
  5. A non-"not found" `ValueError` on open -> `SchemaViolation`: acceptable (it stops corruption being mislabelled as missing).
- Test IDs present: UT02-49 (13 functions), UT02-50 (4 functions + 9 parametrized bad inputs), UT02-51 (2), UT02-52 (2), ST02-08 (`test_st02_08_filter_injection_rejected`, unit), ST02-09 (`test_st02_09_deleted_vector_absent_in_every_version`, integration marker). All brief rows covered.
- ⚠️ Cannot verify: the real LanceDB conflict error text contains "conflict" (message-based mapping, vectors.py:114). No reproducible real conflict was found.

### Gates (run in worktree)
- Card tests: 33 passed (unit + integration), 1.84 s, no warnings.
- `ruff check`: pass. `ruff format --check`: pass. `mypy` (configured scope herness+tools): no issues in 24 files. `lint-imports`: 8 kept, 0 broken. `tools.check_type_ownership`: exit 0.
- Module budget: herness/store/vectors.py = 232 lines (budget 260, ENG limit 400). OK.

### Strengths
- Defense in depth for TH02-08: a table allowlist, a per-table column allowlist, a quote-free id pattern with `fullmatch`, and validation of every id before any I/O. The rejected id is never echoed (index only), and this is tested.
- A no-match delete creates no new version (count-before-delete), and this is tested.
- The schema check catches missing, extra and retyped fields, each with its own test.
- `health` never raises; covers ok, degraded and down.
- 100% line and branch coverage (per the report). The tests assert real behaviour against real LanceDB tables, not mocks, except the error-mapping paths.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. tests/unit/store/test_store_vectors.py:92 and :95 have mypy `arg-type` errors (a `str` loop variable passed as `TableName`). They are outside the configured mypy scope (`files = ["herness", "tools"]`), so no gate fails. A `TableName` annotation on the tuple would fix both.
2. herness/store/vectors.py:179-185: `_open` maps only `ValueError`. An `OSError`/`RuntimeError` from `open_table`, or from `count_rows()` in `count` (vectors.py:221), leaks as a raw LanceDB exception rather than a `HernessError`. The spec lists no error for this case, but routing it through `_store_error` would match `delete_ids`/`purge_history`.
3. herness/store/vectors.py:83-86: `_check_name` raises `TypeError` (not `ConfigError`) for an unhashable or non-str name, through `in` or `name[:64]`. It is typed as `TableName`, so the impact is low.
4. herness/store/vectors.py:114: conflict detection by substring. Pin it by adding a comment/test naming the actual lance error text once one is observed, or revisit when lancedb exposes a typed error.
5. tests/integration/store/test_store_vectors_history.py:40-47: after the purge only one version exists, so the "every remaining version" loop runs once. It could also scan the table directory's bytes for the deleted id (my probe shows this passes). That would make ST02-09 stronger evidence for TH02-09.
6. Spec follow-up (owner action, not builder): close OI-12 with the `optimize(...)` form, and change `table_names()` to `list_tables()` in U02-65 and U02-70.

### Assessment
**Task quality:** Approved
**Reasoning:** All units match the spec and the threats are mitigated. TH02-09 was verified independently at the file level, and every test ID is present and passing, with all gates clean. The four stated deviations are justified by the pinned lancedb 0.39.0 or tighten the spec's preconditions. The remaining items are minor polish.

### Re-review round 1 (commit 7618573, dd05904..7618573)

Scope: Minor findings 1, 2 and 5 (3 and 4 parked). Warehouse files ignored.

- **Finding 1 (mypy in tests): ✅ resolved.** The loop cases are typed `tuple[tuple[TableName, pa.Schema], ...]` (test_store_vectors.py:89-93), with no type-ignore. `mypy` on the three card files reports no issues; the configured `mypy` (herness+tools) reports no issues in 25 files.
- **Finding 2 (raw LanceDB errors): ✅ resolved.**
  - `_open` (vectors.py:185-192) now catches OSError, RuntimeError and ValueError. Only a ValueError containing "not found" becomes `NotFoundError`, which keeps U02-66 and U02-69. Everything else goes through `_store_error`.
  - `count` (vectors.py:222-228) wraps `count_rows()` the same way.
  - The mapping fits the spec. U02-68 says conflict -> `StoreBusy` and other -> `SchemaViolation`. U02-64 says "retry on commit conflict is mapped to `StoreBusy`". U02-69 lists only `NotFoundError`, and reusing the module's `SchemaViolation` for other failures is consistent and better than leaking raw exceptions.
  - No values leak: the messages carry only the allowlisted table name, a fixed action word and `error_type` (class name). Exception text is never copied into the message or details; it stays on the `__cause__` chain.
  - New tests cover the mapping: `test_ut02_49_open_os_and_runtime_errors_mapped` (4 cases, table and count) and `test_ut02_49_count_errors_mapped` (3 cases).
- **Finding 5 (ST02-09 strength): ✅ resolved.** The test uses a distinctive deleted id and content hash that appear nowhere else. It asserts that their bytes exist in some file under `memory_embedding.lance` after the delete and before the purge. That positive control stops the post-purge check from passing vacuously, for example if the files were compressed or the path were wrong. After `purge_history` it asserts that no file contains either needle, and it keeps the per-version checkout loop. The scan is limited to the table's own directory and reads whole files, which is fine at this size. Sound.
- **No regression:** card tests 40 passed (1.69 s, no warnings); `ruff check` clean; `ruff format --check` clean; `lint-imports` 8 kept, 0 broken; `tools.check_type_ownership` exit 0. vectors.py is 239 lines (budget 260). The working-tree copies of the three card files match the commit.

New Minor (non-blocking):
- vectors.py:81 `_BUSY_MARKERS = ("conflict", "busy", "lock")`: `"lock"` is a substring match, so it also catches unrelated words such as "block" (for example "corrupt data block") or "deadlock". Such an error would be classed as retryable `StoreBusy` rather than fatal `SchemaViolation`, and a job could retry against corruption. Suggest word-boundary matching (`re.search(r"\b(conflict|busy|locked?)\b", text)`) when finding 4 is revisited.

**Task quality:** Approved
**Reasoning:** All three targeted findings are fixed and tested without regressions. The one new observation is a minor heuristic-breadth point that belongs with the parked finding 4.
