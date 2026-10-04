# T03-29 Sampling and gold — build report

Worktree: D:\herness\.claude\worktrees\agent-a6aa5c1fe127adcdd (branch worktree-agent-a6aa5c1fe127adcdd, base e41d62c)

## Files
| File | Lines | Budget |
|---|---|---|
| herness/enrich/sampling.py (U03-120..123: stratum_of, allocate, stratified_sample, select_active; private `_ordered_pool` for the gold top-up) | 314 | 360 |
| herness/enrich/gold.py (U03-124..126: fold_of, request_gold, consolidate_gold, GoldStatus frozen dataclass) | 303 | 320 |
| tests/unit/enrich/_sampling_support.py (hand-built warehouse, fake snapshot, vector readers) | new | - |
| tests/unit/enrich/test_sampling.py (UT03-115..118, PT03-15) | new | - |
| tests/unit/enrich/test_gold.py (UT03-119..122) | new | - |
| tests/unit/enrich/security/test_sampling_security.py (ST03-05) | new | - |

No change to herness/enrich/__init__.py, pipeline.py, labels.py or core types. GoldStatus lives in gold.py (ruling 1); check_type_ownership exit 0.

## Tests
- Card tests: 24 passed (test_sampling 14, test_gold 9, ST03-05 1).
- Coverage (card tests only, branch): sampling.py 100 %; gold.py 99 % line, 3 partial branches (>= 90/85).
- Gates: ruff format/check clean, mypy 0 (335 files), lint-imports 13 kept / 0 broken, check_type_ownership 0, check_module_size 0.
- tests/unit/enrich: 1006 passed, 1 skipped (symlink privilege). Pre-commit pytest-unit (full `-m unit`): 8971 passed, 11 skipped.

## Spec notes / interpretations
1. Match keys (U03-125/126): `match_keys=("purpose","question","content_hash")` + `scope={"question_set_version": qsv}`, blocking `("pending",)` — same as the T03-20 spec note on U03-148: the literal four keys plus the scope key duplicate `question_set_version` and impl 02 `create_review_item_if_absent` rejects duplicate keys with ConfigError. The store still matches on all four keys.
2. stratified_sample: records need a text row and a non-NULL `opened_at` (stratum_of requires a datetime); problems have no priority -> band p45; quarter/year taken from `opened_at` in UTC both in SQL (`timezone('UTC', ...)`) and Python (naive = UTC); text length = character length of the redacted text. Top 50 services by incident count, ties by service_id.
3. Prototype cap = `max(1, floor(max_proto_share x size))` (avoids a cap of 0 for tiny sizes; for size >= 50 it equals the spec value). Without a snapshot only `n_h` candidates per stratum are fetched (no cap to apply). Accepted rows are not refilled when the cap rejects candidates, so the sample can be smaller than the allocation (spec gives no refill step).
4. allocate: the precondition branch (`total < min_per x strata` -> every stratum gets `min(min_per, N_h)`) is applied literally, so Σ n_h may exceed `total`; PT03-15 is generated inside the precondition domain (total >= min_per x strata). Removal of excess uses the same order as the remainder (largest fractional part, then key), as written. CONSEQUENCE: with real data (50 services x 3 bands x many quarters x 3 lengths) the number of non-empty strata can exceed gold_size/5 = 300, so a "1,500" gold draw becomes Σ min(5, N_h) records — possibly several thousand. Same for the 30k teacher sample only above 6,000 strata. Flag for the spec owner (e.g. coarsen quarters or allow min_per < 5).
5. request_gold: entities per question (incidents plus problems when that question applies to problems); "training hashes" = every content_hash of the teacher parts; pair questions (`change_caused_pair`) are skipped (no record-level gold, as spot-checks). Teacher prevalence = share of the question's teacher rows (current fingerprint) per answer; classes >= 1 % with < 30 in the draw are topped up by walking the same pool (sha256(salt||hash) order, training excluded) calling `teacher_answers` until each need is met or the pool ends. Hashes already in gold or gold reviews for (qid, fingerprint) get no first-round item, so reruns are idempotent even after items were decided (blocking status is only `pending`). `teacher_answers` gives only the answer: payload `probability`, `decider`, `decider_version` are null.
6. consolidate_gold: each reviewer counts once (their first answer by labeled_at, item_id). Agreement of the first two distinct reviewers -> row with labeled_by/at/item_id of the second, adjudicated false. On disagreement the FIRST later distinct reviewer (3rd, 4th, ...) whose answer equals one of the first two adjudicates (generalises "third reviewer" so a 3-way split does not ask forever). Otherwise a follow-up item is created with the original payload, found among approved gold items of the version (`iter_review_items`, no `get_review_item`); none found -> no follow-up. Freeze when nothing is pending and n_gold >= min(gold_size, 1000) — "class top-up exhausted" is read as "no gold item pending" (every requested item decided). Frozen questions are skipped (no append). Runs under data/locks/labels.lock (same lock as sync_label_checks).
7. Logs: `enrich.sampling.sampled`, `enrich.gold.requested`, `enrich.gold.consolidated` (INFO, per question: n_gold, new, follow_ups, pending, frozen) — counts and ids only.

## Concerns
- labels.py (T03-18) bug, not fixed here (not a card file): `LabelStore.read("gold")` raises pyarrow ArrowInvalid when `gold/` holds only `_reviews/` or `_frozen/` (an empty dataset has no columns to project). Hit as soon as gold reviews are synced before any gold row exists; `gold_hashes()` is affected too. gold.py works around it with `_read_gold` (empty GOLD_SCHEMA table when no part file). Owed fix in labels.py.
- Allocation minimum can blow the gold draw past gold_size (note 4).
- Gold candidates that are still pending (not yet gold rows) are not excluded from later training samples by `gold_hashes()`; run_distill (T03-30) should exclude pending gold hashes too.

## Process note
- The first checkpoint commit (`wip(T03-29): sampling unit tests green`) was rejected by pre-commit ("files were modified by this hook") because I edited/formatted card files while the 21-minute pytest-unit hook ran; every hook itself passed (8971 unit tests green). The work therefore lands as one commit.
- Final commit: f4af09f feat(enrich): T03-29 sampling and gold (all pre-commit hooks passed).

## Fix round 1 (review T03-29-review.md, base f4af09f)
Changes:
- I-3 (defect): the freeze rule now follows U03-126. A question freezes only when no gold item is pending AND (n_gold >= gold_size OR (n_gold >= 1,000 AND the class top-up is exhausted)). This replaces spec note 6's lenient "min(gold_size, 1000)" reading.
- Spec note (I-3): "class top-up exhausted" = every class with >= 1 % teacher-predicted prevalence (the question's teacher rows at its current fingerprint, same rule as request_gold) has >= 30 gold rows. consolidate_gold has no warehouse handle, so it cannot judge whether the pool still holds candidates of a class. This is the simplest form, as the sub-controller allowed. Consequence: when the pool really has fewer than 30 records of a >= 1 % class, a set between 1,000 and gold_size rows stays unfrozen until it reaches gold_size. That is the safe direction, because freezing cannot be undone. No teacher rows means no class needs.
- m6: gold reviews are sorted once in consolidate_gold, no longer once per question.
- m4: ST03-05 is no longer circular. A control draw without `exclude_hashes` picks >= 10 seeded gold hashes, and their teacher parts contain them. The real draw's teacher parts equal the sample and contain no gold hash. The training set is built from both teacher sets (real and control), and build_training_set drops every gold hash.
- Spec note m5: the prototype cap is `max(1, floor(max_proto_share x size))`. It differs from the spec value only when size < 50 (share 0.02), where floor would be 0 and nothing could be drawn.
- New tests:
  - UT03-116: largest fractional part first, a:100/b:400 total 10 -> {a:3, b:7}.
  - UT03-117: a capped stratum still fills from the 3 x candidates.
  - UT03-120: half the pool is training -> no training hash requested (draw or top-up).
  - UT03-120: the top-up applies to a class at exactly 1 % and not to one at 0.9 %.
  - UT03-122: gold_size rows plus one pending item -> not frozen; the item is rejected -> frozen.
  - UT03-122: rejected items leave 1,059 rows with a 1.5 % class at 29 -> not frozen; at 30 -> frozen.
- Mutants re-run with C:\Users\santh\AppData\Local\Temp\w24-s03b\builder\mutate.py (apply, run the card tests, restore): M8, M11, M16, M18 (gold draw `exclude_hashes=frozenset()`), M23 are all killed now. A new mutant, M24 (freeze floor without the top-up condition), is also killed.
- Sizes: sampling.py 314/360, gold.py 311/320.
- Card tests: 30 passed. Coverage: sampling.py 100 %; gold.py 99 % (3 partial branches). ruff, ruff format, mypy, lint-imports, check_type_ownership and check_module_size are all clean.
- Parked by the sub-controller (not changed): I-4 allocation inflation, m1 labels.py ArrowInvalid, m2 pending gold hashes, m3 private `_ordered_pool` import.
- Commits: 19da353 `wip(T03-29): fix1 freeze rule and mutation-killing tests` (all code and tests; pre-commit full `-m unit` passed), then 94ea7a5 `fix(enrich): T03-29 review round 1`. 94ea7a5 is an `--allow-empty` closing commit, because everything was already in the wip commit. Its hooks passed too.
