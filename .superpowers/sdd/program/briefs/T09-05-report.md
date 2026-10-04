# T09-05 Report contract — build report

Status: DONE_WITH_CONCERNS (one spec-level concern on details truncation; see Concerns)
Worktree: D:\herness\.claude\worktrees\agent-a525b32efd7ec3b1e (base cd44be5)

## What was built
- `herness/reports/contract.py` (315 lines, budget 320): U09-03 constants (`SUPPORTED_SCHEMA_VERSIONS`,
  `RENDERABLE_RUN_STATUSES`, `RUN_ID_RE`, `QUERY_ID_RE`, `SECTION_IDS`, `UNCITED_TEXT_MAX`, `DRAFT_MAX_BYTES`),
  `TextField`, `UncitedHit`, U09-04 `iter_text_fields`, U09-06 `load_draft`, U09-07 `ContractLookups` protocol +
  `OpsContractLookups`, U09-08 `check_render_contract`, U09-09 `scan_draft_uncited`, U09-10 `unconfirmed_weight_keys`.
  Marker parsing / numeral scanning via `herness.core.numbers.parse_markers` / `find_uncited` only (R-16);
  the module compiles only RUN_ID_RE and QUERY_ID_RE. Ops reads via `herness.store.ops.ui_evidence_ids_present`,
  `ui_run_rec_ids`, `ui_finding_ids_with_status`; warehouse `meta.evidence` read with bound `$ids` in chunks of 500.
  Logs `reports.contract.violated` (WARNING: run_id, count, rules). No `app/` import; lint-imports 13 kept.
- `tests/support/report_drafts.py` (80 lines): minimal valid `ReportDraft` dict builders (`draft_dict`, `make_draft`,
  `paragraph`, `recommendation`, `number_ref`) until T09-11 lands fixture JSON files.
- `tests/unit/reports/test_contract.py` (615 lines), `tests/unit/reports/test_contract_scan.py` (111 lines).

## Test-ID mapping (functions per ID)
- UT09-05 (4): ordered where list/numbers/finding ids/refs incl. ref order expected_usd, expected_delta, confidence,
  effort, levers; bad lever (missing key, non-dict) -> ReportContractError "recommendations[k].action_levers[m] is missing <key>".
- UT09-06, UT09-07, UT09-08 (1 each): allowed patterns no hits; "grew 42% to $1.2M" hits (5,8),(12,17); marker no hit.
- UT09-09 (7): missing -> code draft_missing + exact message; valid load unmodified; default root from cfg.paths.data; invalid run_ids.
- UT09-10 (7, 1 skips on this host): details.where "sections.0.paragraphs.1.numbers.0.query_id"; multi-error join + bad JSON "(root)";
  symlinked draft (skips w/o privilege) + monkeypatched is_symlink variant; run dir junction/symlink outside root; too large (size_bytes); run_id mismatch.
- UT09-11 schema_version "2" via model_copy. UT09-12 (2): unresolved [[n9]] + malformed [[x1]] exactly two violations; duplicate ids.
- UT09-13 (4): fake lookups missing Q2; draft.query_ids + invalid format never looked up; OpsContractLookups union ops+warehouse over 1200 ids; duckdb error -> QueryError(build_id).
- UT09-14 (2), UT09-15 (2): fake + OpsContractLookups delegation (ops functions monkeypatched; ui_reads has its own tests).
- UT09-16 (2): wrong unit (expected_usd_ref, effort_usd_ref, lever pct) + unknown lever ref; confidence_ref unknown.
- UT09-17 (3): valid draft None, lookup call order, no-ids skips lookups, 250 violations log + 200 cap (`_listed`).
- UT09-18 (1): sorted top-level and dotted nested keys.
- UT09-93 (18 incl. params): RUN_ID_RE / QUERY_ID_RE accept/reject tables; `RUN_ID_RE.pattern == herness.core.types.reports._RUN_ID_RE`
  (sub-controller ruling; closes T09-01 carry-over); constants verbatim; AST: only two re.compile calls, into RUN_ID_RE/QUERY_ID_RE, no regex-looking string constants, imports find_uncited/parse_markers from core.numbers.
- UT09-94 (2): hits in title, summary, caveat carry where; text cut to 80 with full offsets.
- PT09-01 (hypothesis, 150 examples): hits in U09-04 fields, offsets in text, none inside allowed match or marker, every digit outside them covered.

## RED / GREEN
- RED: `PYTHONUTF8=1 uv run pytest tests/unit/reports/test_contract.py tests/unit/reports/test_contract_scan.py -q -p no:logging`
  with contract.py moved aside -> `ImportError: cannot import name 'contract' from 'herness.reports'`, 2 collection errors.
  (Note: the implementation draft was written before the test files; RED was captured by removing the module.)
  First run with the module: 2 failures (my own test expectations: offset 18->19, log `component` key), fixed in tests.
- GREEN: same command -> 58 passed, 1 skipped; coverage of herness/reports/contract.py 100 % line, 100 % branch (208 stmts, 80 branches).

## Gates
- ruff check: All checks passed; ruff format --check: 354 files already formatted; mypy: no issues in 151 source files;
  lint-imports: 13 kept, 0 broken; check_type_ownership exit 0; check_module_size exit 0.
- Card tests with --require-test-ids: 58 passed, 1 skipped. Every ID selected by underscore -k filter.
- Full `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`: 4208 passed, 6 skipped, 17 deselected, 1 xfailed.

## Deviations / choices (spec left open)
1. `OpsContractLookups(wh_con, *, build_id: str = "unknown")`: spec signature is `(wh_con)` but the DuckDB error must be a
   QueryError naming build_id, which the connection cannot give reliably after a failure. Added a keyword-only argument.
2. load_draft codes: invalid run_id -> `invalid_run_id` (message "invalid run_id", no id echoed); missing -> `draft_missing`;
   every other refusal (symlink/outside root, too large, schema, run id mismatch) -> `draft_invalid` with run_id plus
   `where` / `size_bytes` / `draft_run_id` as applicable. Bad lever in iter_text_fields -> `draft_invalid` + `where`.
   Schema error raised `from None` (pydantic input text not chained).
3. check_render_contract details: `where` = "<location>: <what>" entries (the violation strings of U09-08); `rules` = distinct
   rule names in first-seen order; plus `run_id` (user_message uses it). Rule names chosen: schema_version, malformed_marker,
   unresolved_marker, duplicate_number_id, unknown_ref, wrong_unit, invalid_query_id, unknown_query_id, unknown_rec_id,
   unverified_finding. Invalid-format query ids are reported (without echoing the id) and never passed to lookups.
4. Headline fields carry the item's finding_ids (spec: "finding_ids from the item"); refs only on the summary.
   Recommendation.query_ids are not checked (rule 3 names only NumberRef.query_id and draft.query_ids).
5. Lookups called only when there is something to look up.

## Concerns
- `HernessError` bounds each details value to MAX_DETAIL_VALUE_CHARS = 2000, so `details["where"]` is truncated with "…"
  long before the U09-08 cap of 200 entries (+ "… and N more") is reached (~40 chars/entry). The cap logic is implemented and
  unit-tested (`_listed`), but the suffix is only visible for short lists. Needs a ruling (e.g. lower the cap, or carry the full
  list elsewhere, e.g. manifest) — not changed here.
- File-symlink test skips on this host (no symlink privilege); covered by a monkeypatched is_symlink test and a junction-based
  run-dir-outside-root test that does run here.

## Line counts
herness/reports/contract.py 315/320; tests/support/report_drafts.py 80; tests/unit/reports/test_contract.py 615;
tests/unit/reports/test_contract_scan.py 111.

## Commit
a4450c7 feat(reports): add report contract checks and draft loading (T09-05) — all pre-commit hooks passed.
detect-secrets flagged ULID-shaped test ids (false positives); marked inline with `# pragma: allowlist secret`
(same pattern as tests/unit/reports/test_rules.py); .secrets.baseline not regenerated.
test_contract.py is 617 lines after the pragma edits.
