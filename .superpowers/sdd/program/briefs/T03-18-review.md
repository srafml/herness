# T03-18 review — Labels store and sync (impl 03)

Reviewed: worktree agent-a127170ebcb91722c, commit 0545532 (base c3eee74). Files: `herness/enrich/labels.py` (377/380), `tests/unit/enrich/test_labels.py`.

Commands run (reviewer):
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_labels.py -q -p no:logging --cov=herness.enrich.labels --cov-branch --cov-report=term-missing` → 15 passed; labels.py 97 % (268 stmts, 8 missed; 80 branches, 2 partial). Gate ≥90/85 met.
- `uv run ruff check herness/enrich/labels.py tests/unit/enrich` → clean; `ruff format --check` → clean.
- `uv run mypy herness/enrich/labels.py` → clean.
- `uv run python -m tools.check_module_size` → exit 0 (377 ≤ 380 budget, ≤ 400 ENG).

### Spec Compliance
- ✅ Spec compliant

| Item | Status | Notes |
|------|--------|-------|
| U03-75 LabelStore signature/methods | ✅ | `append/read/item_ids/latest_human/gold_hashes/is_gold_frozen/freeze_gold/migrate_from` as specified; extra keyword `read(columns=)` is additive. |
| U03-75 schemas | ✅ | teacher: `distribution map<string,double>` (MAP<VARCHAR,DOUBLE>), `round int16` (SMALLINT), `stratum/purpose string`; human: `labeled_at timestamp[us, UTC]` (TIMESTAMPTZ), `item_id`; gold = human + `fold int16`, `adjudicated bool`; gold_reviews = human (labels.py:39-52). |
| U03-75 layout | ✅ | `part-<ulid>.parquet` atomic via `replace_atomic` (tmp `.<name>.tmp`, fsync, `os.replace`); `gold/_reviews/`; `gold/_frozen/<qid>-<fp>.json {digest,n,frozen_at}`; readers use `ignore_prefixes=[".","_"]` (verified: `_reviews` and `_frozen` invisible to gold reads, UT03-72/73). |
| U03-75 errors | ✅ | `SchemaViolation` on schema mismatch (append and foreign on-disk part); `ConfigError("gold frozen for <qid>")` (labels.py:151); IO via `cache.io_error` (U03-38: EACCES/EBUSY → StoreBusy, else FatalError). |
| U03-75 migrate_from | ✅ | unchanged (qid, fingerprint) rows of all kinds; gold markers only for copied gold pairs (labels.py:251-256); `_migrated_from_<old>.json` written last. |
| U03-76 sync algorithm | ✅ | lock `data/locks/labels.lock` via `log_lock(timeout_s=10.0)` → StoreBusy; watermark `_sync.json` {last_decided_at,last_item_id}, default epoch; keyset pages of 500 with `(decided_at,item_id)` cursor, stop on short page; qsv filter; rejected → skipped; `note.answer` JSON fallback to `payload.answer`; U03-50 label sets; `enrich.labels.invalid_answer` WARNING with `item_id` only; `labeled_by=decided_by`, `labeled_at=decided_at`; purpose routing; `item_id` dedupe; watermark written atomically after appends; `enrich.labels.synced` INFO counts. |
| U03-77 gold_digest | ✅ | sort (question, content_hash), canonical JSON of `[question, question_fingerprint, content_hash, answer, fold]`, `"\n"` join, SHA-256 hex; whole-line tie-break keeps permutation invariance with duplicate keys (spec-compatible refinement). |
| TH03-04 | ✅ | gold reviews never land in `gold/`; nothing here feeds training; rows carry record_id/hashes only; logs/errors carry no text (file names, qid, item_id, counts). |
| TH03-11 | ✅ | `labeled_by` (user_ref) and `item_id` kept on every human/gold_reviews row. |
| UT03-72 | ✅ | 6 tests: round-trip each kind, empty reads, wrong schema, foreign part, latest_human, IO mapping. |
| UT03-73 | ✅ | frozen append → ConfigError; migrate twice copies once; preconditions. |
| UT03-74 | ✅ | real migrated ops store; note answer / no note / note w/o answer / rejected / invalid / other qsv / gold / ensemble / score / unknown q / unknown purpose; second run adds nothing; paging (page=3) and lost-watermark dedupe; StoreBusy lock. |
| UT03-75 | ✅ | shuffled parts → equal digests. |
| PT03-09 | ✅ | hypothesis permutations including duplicate keys. |
| Naming / pytestmark / docstrings | ✅ | `test_ut03_7x_…`, `test_pt03_09_…`; docstrings start with the ID; `pytestmark = pytest.mark.unit`. |
| Coverage / budget / lint / types | ✅ | 97 %; 377/380; ruff, mypy, module size clean. |

Builder spec-open choices (report "Spec notes"): all judged acceptable. (1) label sets match U03-50 (bool true/false, score "0".."3", choice option keys; dynamic choice accepts option-key-shaped answers per Question validator rule (e)); (2)-(3) counting semantics reasonable; (4) fingerprint from payload is fine since qsv already pins fingerprints; (5)-(6) extra preconditions are defensive ConfigErrors; (7) deterministic tie-break; (8) digest tie-break needed for PT03-09 with duplicates; (9) additive keyword.

- ⚠️ Cannot verify from diff:
  - IT03-15 (integration of U03-76) is not in this card's Tests row; not built — confirm it is owned elsewhere.
  - DD-07 row in impl 03 §12 still "Still open"; implementation follows it — controller to update status.
  - `gold_hashes()` reads only `gold/`; records currently under gold review (`gold/_reviews/`, not yet adjudicated) are not excluded from training samples by this API. Spec-defined; the consumer card (sampling/training) should decide whether to also exclude `_reviews` hashes (TH03-04).

### Strengths
- Tight, spec-literal implementation; schema checks both on write and on each on-disk fragment.
- Crash between append and watermark is safe (item_id dedupe; watermark written last; test deletes the watermark and proves no duplicates).
- Watermark advances past other-qsv items correctly (those belong to another version's store with its own watermark); lock released through `log_lock`'s ExitStack on any error; decided_at is aware UTC from `parse_utc`, round-trips through isoformat and `_ts_arg`.
- UT03-74 runs against the real ops `review_item` table, so the keyset SQL is exercised, not a fake.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. `migrate_from` is not crash-idempotent (labels.py:245-257): a crash after some `_write` calls but before the marker makes a rerun append the same teacher/gold rows again. For gold this doubles rows and changes `gold_digest`/`n` versus the frozen marker (false tamper signal). U03-39's cache migrate tolerates this because cache readers dedupe by key; label readers don't. Fix: make migrated part names deterministic per (old_qsv, kind) (e.g. write an intent file `_migrating_from_<old>.json` holding one ULID per kind first and reuse it as `part-<ulid>.parquet` on rerun so `os.replace` overwrites), or delete the parts listed in the intent file before re-copying.
2. Null key values crash with TypeError instead of a clean error: `append("gold", …)` does `sorted(set(_pair_list(rows)))` (labels.py:149) and `gold_digest` sorts `(question, content_hash, …)` (labels.py:269); a null in `question`/`question_fingerprint`/`content_hash` mixed with strings raises `TypeError`. Fix: in `append`, reject tables with `null_count > 0` in the key columns (`content_hash, question, question_fingerprint, answer`, plus `item_id` for human kinds) with `SchemaViolation`.
3. Keyset watermark can skip a late-committing decision (labels.py:373-375; plan-mandated by U03-76 steps 2/5): `decided_at` is the caller's `now`, computed before the ops write; if decision A (t1) commits after decision B (t2 > t1) was already synced, the watermark is past A and A is never synced. Window is small (single node, serialized SQLite writes). Fix (raise as spec note): re-read with a small overlap (cursor = watermark − N seconds); item_id dedupe already makes the overlap safe (skipped counts would need to exclude re-seen items).
4. No known-answer test pins the exact digest bytes spec 11 relies on (tests/unit/enrich/test_labels.py:443-467 only compare digests with each other). Fix: assert `gold_digest(_gold([one row]))` equals `hashlib.sha256(canonical_json([...]).encode()).hexdigest()` computed in the test.
5. Unknown `purpose` is skipped without any log (labels.py:298-300), unlike other invalid items; a malformed producer would go unnoticed. Fix: log `enrich.labels.invalid_answer` (or a distinct WARNING event) with `item_id` there too.
6. DRY: `_SCORE_LABELS` duplicates `jev_wire._SCORE_LABELS` (labels.py:58 vs herness/enrich/deciders/jev_wire.py:39). Fix: expose one public constant (e.g. in `herness.enrich.questions`) and import it in both.
7. No test for the "last page item is other-qsv" watermark case (logic at labels.py:359-362 is correct but untested). Fix: in UT03-74 make the last decided item an other-qsv item and assert `_sync.json`'s `last_item_id` equals it.

### Assessment
**Task quality:** Approved
**Reasoning:** All units, schemas, paths, error classes/messages, sync and digest algorithms, threats and test rows match the brief; gates (97 % coverage, ruff, mypy, size 377/380) pass. Remaining items are robustness polish (migrate crash window, null-key TypeError, keyset race inherent in the spec) that do not block the card.
