# Review: T05-12 Ops evidence functions

Worktree: D:\herness\.claude\worktrees\agent-a091d7071442bc8d1 (branch worktree-agent-a091d7071442bc8d1, base 8a073b4, head 11560c6)

## Spec compliance

### U05-71 record_evidence, record_evidence_use, get_evidence, finding_statuses

- Spec OK -- Signatures exact: record_evidence(ev: Evidence) -> bool; record_evidence_use(query_id: str, run_id: str, task_id: str | None, used_at: datetime) -> bool; get_evidence(query_id: str) -> Evidence | None; finding_statuses(finding_ids: Sequence[str]) -> dict[str, str]. (herness/store/ops/evidence.py:136,145,161,197)
- Spec OK -- record_evidence: INSERT OR IGNORE on PK query_id inside one run_write transaction; cursor.rowcount == 1 maps to True (first row kept on repeat, confirmed by INSERT OR IGNORE semantics and test test_ut05_47_record_evidence_is_idempotent_and_keeps_first_run). (evidence.py:115-142)
- Spec OK -- record_evidence_use: INSERT OR IGNORE on PK (query_id, run_id, task_id); task_id=None stored as empty string (task_key = task_id if task_id is not None else empty string). Confirmed by test_ut05_47_record_evidence_use_dedupes_by_pk_two_tasks. (evidence.py:145-158)
- Spec OK -- get_evidence: returns None for a missing row, and for a tampered row (recomputed query_id via herness.core.ids.query_id mismatches the stored one), logging WARNING harness.evidence.tampered with query_id only, never raw sql or params. The SchemaViolation branch (corrupted build_id) is folded into the same tamper path rather than propagated, matching the spec wording (or when the stored query_id does not recompute). Confirmed by test_ut05_48_get_evidence_none_for_missing_and_tampered. (evidence.py:161-194)
- Spec OK -- finding_statuses: ids filtered against the pattern fnd_ plus 26 Crockford-base32 chars (via .fullmatch, so the omitted explicit anchors behave identically), invalid ids dropped, then capped at 500 (slice), a SELECT with finding_id IN (...) and one bound placeholder per id; unknown ids absent from the result. Empty input short-circuits to an empty dict (SQLite rejects IN with zero items -- a correct defensive addition, not in conflict with the spec). Confirmed by test_ut05_48_finding_statuses_dict. (evidence.py:197-206)
- Spec OK -- Parameterised SQL only; JSON columns via core.dump_json / core.load_json; timestamps via clock.format_utc / clock.parse_utc (fixed-width spec 00 section 8, confirmed against herness/core/time.py:74-93 and migration 003's length-27 CHECK constraints).
- Spec OK -- Only core API used: evidence.py imports "from . import core" plus herness.core.* helpers; nothing beyond T02-04's run_write / read_one / read_all / dump_json / load_json.

### U05-75 scrub_record_from_evidence

- Spec OK -- Signature scrub_record_from_evidence(record_id: str, /) -> int, positional-only per spec.
- Spec OK -- record_id validated via herness.core.ids.split_record_id; failure re-raised as SchemaViolation with message "invalid record_id" (a fixed message, not echoing the raw failure detail). Confirmed by test_ut05_126_scrub_rejects_invalid_record_id. (evidence.py:242-252)
- Spec OK -- One run_write transaction: selects query_id and result_sample from evidence where instr(result_sample, ?) > 0, with the JSON-escaped record_id as the bound parameter; decodes via load_json; keeps rows where no string cell equals or contains record_id; updates only rows that actually changed; returns the count of updated rows. query_id, sql, params, result_hash, row_count all left untouched (confirmed by test assertions on the unchanged fields after scrub).
- Spec OK -- Idempotent: second call returns 0 (confirmed by test, and structurally, since once matching cells are removed, instr no longer finds those rows).
- Spec OK -- INFO log harness.evidence.scrubbed with record_id_hash (16 hex chars of SHA-256 of the record id) and rows count; the raw record id is never logged (confirmed by test asserting the raw id does not appear in the log event's repr).
- Spec OK -- Correctly distinguishes a JSON-key-only occurrence (matched by SQL instr at the text level, but no dict value equals or contains the id) from a true cell match -- the "matched but no change" row is left alone and not counted as updated. This is a subtle correct edge case, verified by the dedicated test row for it.

### Module map and files

- Spec OK -- herness/store/ops/evidence.py: 182 lines (report says 182; check_module_size exits 0) against the 220-line budget and 400-line hard limit.
- Spec OK -- herness/store/ops/__init__.py: one re-export block added (imports at lines 34-40, __all__ names at 60-64) after core, then migrate, matching the required order "core, migrate, shared, then areas in row order" -- no shared block exists yet, so evidence directly follows migrate, which is correct. The "noqa: I001" on the core import block is pre-existing convention (matches herness/core/types/harness/__init__.py); it sits on the core block, not on a new block introduced by this card, so it does not mask any new violation. Cannot-verify item: whether a hypothetical alphabetically-earlier future area would still survive ruff check --fix (the report itself flags this as a known future risk, not a defect here).

### Tests

| ID | Result |
|----|--------|
| UT05-47 | Pass -- both store-side behaviours (idempotent insert keeping first run_id; two-task use dedup with task_id=None storing as empty string) covered and passing. |
| UT05-48 | Pass -- missing / tampered-sql / tampered-build_id rows all return None plus exactly one WARNING each; finding_statuses dict behaviour (known / unknown / bad-pattern / empty) covered and passing. |
| UT05-126 | Pass -- cell-equal, string-contains, clean, non-list-defensive, and key-only-match rows all exercised; first call rewrites exactly the two true positives, query_id / result_hash / row_count unchanged, log has no raw id; second call returns 0; invalid id raises SchemaViolation. Covered and passing. |
| BT05-08 | Pass (present, not re-executed here) -- in tests/bench/test_store_ops_bench.py, module-level pytestmark = [integration, slow] applies to the new test too; 10,000 unique-SQL insert pairs, p95 under 10 ms assertion. Report states it passed locally once; no CI perf gate is wired, a pre-existing pattern shared with BT02-06/07 in the same file, not a defect introduced by this card. |
| FT05-04 | Expected-absent -- listed under U05-71 in the impl doc's Tests field but not in this card's own Tests row; report explicitly states it was skipped per the brief's instruction. Consistent with the brief given to this review (Tests row lists only UT05-47, UT05-48, UT05-126, BT05-08). |

Fresh and upgraded DB acceptance check: Pass -- the migrated fixture is parametrized fresh/stepwise (001-002 applied via a monkeypatched _migrations_root, then 003-006 to latest) and every test in the file runs under both, satisfying "tests pass on a fresh and an upgraded fixture DB."

### Coverage

Verified independently (re-run in the worktree): pytest on the new test file with branch coverage on herness.store.ops.evidence gives 100% line (80/80 statements), 100% branch (12/12), above the 90%/85% gate. 12 tests passed, 0 failed.

### Lint, types, imports, module size (independently re-run, not merely trusting the report)

- ruff check on the touched files: clean.
- ruff format --check: clean (2 files already formatted).
- mypy on evidence.py: clean, 0 errors.
- check_module_size: exit 0.
- lint-imports: 13 of 13 contracts kept, including ops-areas-acyclic (evidence.py imports only "from . import core", as claimed).

### Secrets and personal data in logs and errors

Pass -- harness.evidence.tampered logs query_id only (an opaque hash-derived id, not raw sql or params). harness.evidence.scrubbed logs a truncated SHA-256 hash of the record id and a row count, never the raw record id -- confirmed both by code reading and by the test's assertion that the raw id does not appear in the log event. SchemaViolation with message "invalid record_id" does not echo the malformed input.

## Findings

No Critical or Important findings.

### Minor

- herness/store/ops/evidence.py line 37 -- the finding-id regex is written without explicit start/end anchors, relying on .fullmatch() to anchor it; functionally identical to the spec's anchored pattern but slightly less self-documenting if the call site ever changes from fullmatch to search or match. No behavioural defect.
- herness/store/ops/evidence.py lines 209-220 (the kept-sample helper) silently treats a non-list result_sample as leave-untouched rather than logging or surfacing it; such a row would pre-date this card's invariants (the Evidence model guarantees a list on write), so this is a reasonable defensive branch, but it does mean a corrupted row that also happens to contain a to-be-deleted record id would silently survive a privacy scrub. Worth a follow-up note for the impl 10 privacy job, not a defect in this card since the row shape is guaranteed by record_evidence itself.

## Verdict

Approved

All U05-71 and U05-75 postconditions were verified against the diff and against direct reads of herness/core/ids.py, herness/store/ops/core.py, herness/core/time.py, herness/core/logging.py, herness/core/types/harness/evidence.py, and migration 003_runs_evidence.sql. Independently re-ran the unit test file (12 passed, 100 percent line and branch coverage), ruff check and format, mypy, check_module_size, and lint-imports (13 of 13 contracts, ops-areas-acyclic intact) -- all clean, matching the build report's claims. No Critical or Important issues found; two Minor stylistic and defensive-code notes only.
