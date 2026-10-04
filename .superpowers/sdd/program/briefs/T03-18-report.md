# T03-18 report — Labels store and sync (impl 03)

Status: DONE_WITH_CONCERNS (spec notes only; no deviation from verbatim values)
Worktree: D:\herness\.claude\worktrees\agent-a127170ebcb91722c (base c3eee74)

## Built
- `herness/enrich/labels.py` (377 lines; budget 380, ENG 400):
  - Schemas `TEACHER_SCHEMA`, `HUMAN_SCHEMA`, `GOLD_SCHEMA` (+ `LABEL_SCHEMAS` incl. `gold_reviews` = human), exactly per U03-75 (labeled_at timestamp[us, UTC], round/fold int16, distribution map<string,double>).
  - `LabelStore(paths, qsv)` (U03-75): `append` (exact schema check -> SchemaViolation; frozen gold (qid, fingerprint) -> ConfigError("gold frozen for <qid>"); empty table -> None; atomic `part-<ulid>.parquet` through `cache.replace_atomic`, OS errors per U03-38 via `cache.io_error`), `read` (pyarrow.dataset, `ignore_prefixes=[".", "_"]`, per-fragment physical-schema check -> SchemaViolation, empty table with the schema when absent), `item_ids`, `latest_human`, `gold_hashes`, `is_gold_frozen`, `freeze_gold` (`gold/_frozen/<qid>-<fingerprint>.json` `{digest, n, frozen_at}`), `migrate_from` (unchanged (qid, fingerprint) rows of all four kinds; gold markers only for copied gold pairs; marker `_migrated_from_<old>.json` `{rows, finished_at}` written last; log `enrich.labels.migrated`). `gold_reviews` parts live in `gold/_reviews/` (DD-07).
  - `sync_label_checks(store, *, qs)` (U03-76): under `data/locks/labels.lock` via `herness.core.audit.log_lock(timeout_s=10.0)` (StoreBusy on timeout); watermark `data/labels/<qsv>/_sync.json` (default epoch, ""); keyset pages of 500 with `list_review_items(kind="label_check", statuses=("approved","rejected"), decided_after=(ts, item_id), limit=500)`; note.answer / payload.answer rule; U03-50 label sets; `enrich.labels.invalid_answer` WARNING (item_id); purpose routing; item_id dedupe against `store.item_ids(kind)`; watermark advanced atomically to the last read item; `enrich.labels.synced` INFO with counts.
  - `gold_digest(gold)` (U03-77): canonical JSON of `[question, question_fingerprint, content_hash, answer, fold]`, sorted by (question, content_hash), joined by "\n", SHA-256 hex.
- `tests/unit/enrich/test_labels.py`: UT03-72 (6 tests), UT03-73 (3), UT03-74 (4, real migrated `ops_store` + impl 02 create/decide), UT03-75 (1), PT03-09 (hypothesis permutations incl. duplicate keys).
- No shared impl 03 file edited (layout.py, cache.py, settings.py, questions.py, jev_wire.py untouched); no pyproject change needed.

## Spec notes / choices (where the spec is open)
1. Label sets (U03-50): bool {"true","false"}; score {"0".."3"}; choice = option keys; a dynamic-options choice question (`options_source` core.team/core.service, options None at question-set level) accepts any option-key-shaped answer (`^[A-Za-z0-9_.:-]{1,64}$`, not a bool word) since options resolve at run time.
2. Unknown question id, non-string payload fields or an invalid label -> skipped + `invalid_answer` log; an unknown `purpose` -> skipped without that log. Items of another `question_set_version` are neither counted nor appended (the watermark still advances past them).
3. Counts `human`/`gold_reviews` are rows actually appended (after item_id dedupe); `skipped` = rejected + invalid for this version.
4. `question_fingerprint` of human rows comes from the item payload (not re-checked against qs).
5. Added preconditions: `sync_label_checks` raises ConfigError when `qs.version != store.qsv`; `migrate_from` raises ConfigError when `old_qsv == store.qsv` or `new_qs.version != store.qsv`; unreadable `_sync.json` / migrate marker -> ConfigError.
6. `freeze_gold` with the identical digest and n is a no-op; a different digest/n for an already frozen pair, a non-64-hex digest or n < 0 -> ConfigError. qid/fingerprint are validated (Question id pattern, 16-hex) before building marker paths.
7. `latest_human` tie-breaks equal `labeled_at` by `item_id` descending (deterministic).
8. `gold_digest` breaks (question, content_hash) ties by the whole canonical line so the digest stays permutation-invariant even with duplicate keys (PT03-09 covers this).
9. `read` has an extra keyword `columns=` (additive; used by item_ids/gold_hashes).

## Carry-overs
- `migrate_from` crash between writing parts and the marker would duplicate rows on rerun (same accepted pattern as U03-39 cache migrate); consumers of human/gold_reviews dedupe by item_id.
- DD-07 row in impl 03 §12 still says "Still open"; the implementation follows it. Status update left to the controller (not edited to avoid conflicts with parallel impl 03 cards).
- IT03-15 (integration test of U03-76) is not in this card's Tests row; not built.

## Evidence
- RED: `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_labels.py -q -p no:logging` -> `ImportError: cannot import name 'labels' from 'herness.enrich'` (collection error).
- GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/enrich -q -p no:logging --cov=herness.enrich.labels --cov-branch` -> 374 passed, 1 skipped (pre-existing symlink skip); labels.py 97 % (268 stmts, 8 missed; 80 branches, 2 partial).
- ruff format/check clean; mypy (labels.py + test) clean; lint-imports 13 kept 0 broken; check_module_size exit 0; pre-commit hooks (incl. type-ownership, detect-secrets, pytest-unit) passed on the checkpoint commit.
- Line counts: herness/enrich/labels.py 377/380.

## Commits
- Final commit 0545532 feat(enrich): T03-18 labels store, label_check sync and gold digest (the wip checkpoint 12b63a1 was folded into it with a soft reset; hooks passed on both).

## Review fix round 1 (review: T03-18-review.md)
- M4 fixed: `test_ut03_75_digest_known_answer` builds the canonical lines by hand, hashes them with hashlib, and pins the literal d55758a759c8d4abaca339ac56e1b45ddf443f18926ee566ceae159231b096e9 against `gold_digest` of the same rows given in reverse order.
- M7 fixed: `test_ut03_74_last_page_ends_on_other_version`: when the last page ends on another version's item, the watermark is that item's (decided_at, item_id), and the next sync adds nothing.
- M2 fixed: `append` now rejects nulls in the key columns (content_hash, question, question_fingerprint) with SchemaViolation("... or have null keys"). UT03-72 now has human and gold null-key cases.
- M5 fixed: an unknown `purpose` now goes through the same skip path as an invalid answer and logs `enrich.labels.invalid_answer` (WARNING, item_id only). The UT03-74 count of invalid_answer log lines is now 4.
- Not done, as instructed: M1 (parked as a spec note), M3 and M6.
- labels.py is 376/380 lines. tests/unit/enrich: 376 passed, 1 skipped (existing symlink skip). labels.py coverage 97 %. ruff, mypy and check_module_size are clean.
- This round is one new commit on top of 0545532. No earlier commit was squashed or reset.
- Final round-1 commit: fb0ccc1. The known-answer literal is split over two lines, each marked `# pragma: allowlist secret`, because the detect-secrets hook flagged the hex digest. .secrets.baseline is unchanged.
