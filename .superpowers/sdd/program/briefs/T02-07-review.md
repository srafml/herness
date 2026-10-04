# T02-07 review (Review items) — head 0a8b482, base 8a073b4

**Verdict: Needs fixes** (one Important: a missing argument check. The rest is solid.)

### Spec Compliance
- ❌ Issues found: U02-59 does not check that `note` for a `label_check` item is a JSON object string (see Important 1).
- Per unit:
  - U02-55 `ReviewItem` ✅ frozen and slotted, payload is a `MappingProxyType`, the pending⇔decided_at⇔decided_by invariant is checked in `__post_init__`, and `from_row` raises SchemaViolation for bad JSON, a non-object payload, an unknown kind or status, or a bad timestamp.
  - U02-56 `create_review_item` ✅ `rev_`+ULID, `dump_json(field="payload")` with its byte cap, the fixed INSERT, runs on `conn` or in `run_write(op="review_item_create")`, logs `review_item_created` with `item_id` and `kind` only.
  - U02-57 `get_review_item` ✅ The format is checked before the read. A malformed ID gives `NotFoundError(key="invalid")` rather than `key=item_id`. This is a deliberate no-echo deviation and is acceptable (Minor 4).
  - U02-58 `list_review_items` ✅ Two fixed SQL constants, verbatim to the spec, including the `json_each` payload predicate. Statuses and `payload_match` are bound as JSON. A plain datetime cursor is bound as `(t, "rev_"+"Z"*26)`. Enforced: limit 1..5000, `offset` must be 0 with a cursor, status and statuses are exclusive, statuses has 1-3 distinct values, match keys follow the regex (at most 8), values are at most 1,024 characters. `read_all` is capped at 5000.
  - U02-59 `decide_review_item` ✅ except the note check. The steps run in the spec's order: SELECT → NotFound → memory_write/conn precondition (the R-54 system rejection is exempt) → conflict (logs `review_conflict`) → UPDATE … AND status='pending' → audit("review_decision", decided_by, item_id, kind, status, decided_by, note_len) inside the transaction → re-read. `review_item_decided` is logged after success, and `audit_orphan` only when there is no `conn` and the audit line was already written.
  - U02-60 `approved_mapping_suggestions` ✅ One SELECT ordered by `decided_at` and `item_id`, under the default 100k `read_all` cap.
  - U02-62 / `__init__` ✅ The "02 shared" block is third with 8 names in U02-62 order. The UT02-68 check now requires this block.
- Tests: UT02-43 ✅, UT02-44 ✅ (spy plus the real T10-05 audit file line), UT02-45 ✅, UT02-46 ✅, UT02-47 ✅ (ties on `decided_at` broken by `item_id`; rejected and non-suggestion items excluded), UT02-75 ✅ (6 decided incl. ties, 2 pending, limit=2 to empty, offset+cursor → ConfigError, a plain datetime equal to a tie returns only later items, decided-only with all statuses), UT02-76 ✅ (see Minor 2), UT02-79 ✅ (`'` and `%` match literally, `a.b` and 9 keys → ConfigError, also on the decided query), ST02-06 ✅ (with and without `conn`), ST02-07 ✅ (70 KiB → SchemaViolation "exceeds"; 3,000-character note → ConfigError; a direct insert fails the CHECK for both the payload and the note case. I checked the migration 005 CHECKs: `rev_A` and `rev_B` fail only on the length CHECKs, so the test is not vacuous).
- ⚠️ Could not verify from the diff: the IT02-11 service-map consumer (T02-15's).

### Evidence I ran
- The card's tests plus UT02-68: 53 passed. `shared.py` coverage is 100% statements and 100% branches (177 statements, 50 branches).
- ruff check ✅, ruff format --check ✅, mypy (configured files `herness` and `tools`, strict) ✅, lint-imports 13 kept ✅, tools/check_module_size.py ✅, tools/check_type_ownership.py ✅. shared.py is 336 of 390 lines. `core.py` is unchanged against the base.
- SQL injection: keys and values reach SQL only as bound JSON text. `json_extract(payload, '$.' || m.key)` can use only regex-restricted keys because validation happens first, and the test confirms `'`/`%` payloads match literally. Safe.
- Audit and rollback: `audit` is called inside the `run_write` callback. If it raises, `run_write` rolls back and re-raises (FatalError is not a `sqlite3.Error`, so it is not remapped). With `conn`, the error propagates to the caller's `run_write`. Correct.
- Keyset: `(decided_at > ? OR (decided_at = ? AND item_id > ?))` on fixed-width ts-text (27 characters, enforced by a CHECK) is a correct lexical keyset. `"rev_"+"Z"*26` is at least as large as any valid Crockford ID and no valid ID is greater, so it is a correct upper sentinel.

### Strengths
- Tight, readable module. Fixed SQL constants. Every precondition fails before any write. No payload or note text appears in any message or log field.
- The ST02-07 CHECK test and the UT02-76 audit_orphan test exercise real failure paths through the real `run_write`.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. `herness/store/ops/shared.py:272-284` (and the callback at ~308-314): U02-59's `note` constraint says "for `label_check` a JSON object string per spec 03 §4.6", and the preconditions say "Arguments valid, else ConfigError". Only type and length (≤2,000) are checked, so an approved `label_check` with a free-text or non-object note is stored and later breaks the impl 03 label sync that parses it. The kind is only known after step 1, so the check belongs in `decide` right after the SELECT and before the UPDATE: `json.loads(note)` must be a `dict`, else `ConfigError`, with no echo of the note. Add a UT02-45-style case for it.

#### Minor (Nice to Have)
1. `herness/store/ops/shared.py:303-327`: `run_write` retries `fn` on StoreBusy (`_shims` `sqlite_write` policy). If a retryable error ever hit after `audit` (for example a BUSY COMMIT), a retry would write a second `review_decision` line, and a later success would never log `audit_orphan` for the first line. The spec says this cannot happen under BEGIN IMMEDIATE, so this is residual risk only. Consider a comment, or logging the orphan when `audited` is already non-empty at the start of a retry attempt.
2. `tests/unit/store/ops/test_store_ops_shared.py:414`: `audit_calls.clear()` after the raising callback discards the attempt line without asserting it. Add `assert len(audit_calls) == 1` first so the documented §7.7 behaviour (one attempt line, then exactly one committing line) is pinned rather than hidden.
3. `herness/store/ops/shared.py:328`: with `conn`, `store.ops.review_item_decided` is logged before the caller commits, so it can appear for a decision that later rolls back. This matches the spec's placement and the §7.7 residual risk. Consider stating it in the docstring.
4. `herness/store/ops/shared.py:138-140`: `NotFoundError.key="invalid"` for malformed IDs differs from the spec's `key=item_id`. It is justified by the no-echo rule, but record it as a deviation for impl 09, which maps `not_found`.
5. The test files are outside the mypy gate (`files = ["herness","tools"]`) but produce 6 strict errors when checked, for example `test_store_ops_shared.py:190` (`shared.audit` not exported) and `test_st02_shared.py:87` (lambda inference). No gate fails. Informational.
6. The commit trailer is `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, but global-constraints.md:14 specifies `Claude Opus 5.5 (1M context)`. Align it if the program enforces the constraints-file form.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation and tests are correct, secure and fully covered, and every gate passes. The one missing piece is the U02-59 precondition that a `label_check` note is a JSON object string, which should fail with ConfigError before the write. It is a small, local fix plus one test.

## Re-review r1: fix commit e9e779e

**Verdict: Approved.**

### Fixes checked
- Important 1 ✅ `herness/store/ops/shared.py` `decide`: the new check sits after the SELECT and the memory_write rule and before the conflict check, the UPDATE and the audit call. When the kind is `label_check` and a note is given, `_is_json_object` parses the note and requires a `dict`. If parsing fails it catches ValueError, which covers JSONDecodeError, and RecursionError, so a deeply nested array returns False instead of crashing. The ConfigError message has a fixed text and never echoes the note. Other kinds keep accepting free text.
  - New test `test_ut02_44_label_check_note_must_be_a_json_object` covers five notes: plain text, an array, a string, malformed JSON, and `"["*1999` (the recursion case). For each it asserts ConfigError, that the note does not appear in the message, that the status stays `pending`, and that there are no audit calls. It also checks that a `weight_change` item still stores the same note.
  - Ordering: a decided `label_check` with a bad note now gets ConfigError before ReviewItemConflict. That is consistent with "Arguments valid, else ConfigError" and acceptable.
- Minor 2 ✅ `test_store_ops_shared.py`: the UT02-76 test now asserts `len(audit_calls) == 1` before it clears the capture.
- Minor 3 ✅ The `decide_review_item` docstring now says the decided log line comes before the caller's commit when `conn` is given (§7.7).
- Minors 1, 4, 5 and 6 are parked and not re-raised.

### Evidence I ran
- The card's tests plus UT02-68: 58 passed. `shared.py` coverage is 100% statements and 100% branches (186 statements, 52 branches).
- ruff check ✅, ruff format --check ✅, mypy (configured, strict) ✅, lint-imports 13 kept ✅, check_module_size ✅, check_type_ownership ✅. shared.py is 349 of 390 lines. `core.py` is unchanged against the base 8a073b4.

### New findings
- None.

**Task quality:** Approved
