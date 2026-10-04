# T03-30 Evaluation — review (verify agent)

Worktree agent-a55bd54dd4a377644, head b357bf0 (base bdb61b7). Reviewed diff T03-30-review.diff, brief, report, design 03 §4.4/§5.6/§5.8, impl 03 U03-126/U03-136/U03-138 and §8, impl 11 F11-09/U11-67.

Commands I ran:
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_evaluate.py tests/eval/enrich/test_enrich_eval_crosscheck.py -q -p no:logging -m ""`: 15 passed in 2.10s
- `uv run mypy herness/enrich/evaluate.py`: no issues
- `uv run python -m tools.check_module_size`: exit 0; evaluate.py is 374/380 lines
- `ruff check` and `ruff format --check` on the 4 files: clean

## Spec Compliance
- ✅ U03-127 question_metrics: calls cross_fit, then apply_temperature(probs, T, qtype). accuracy is taken on argmax of P'. macro_f1 uses sklearn f1_score(average="macro") for choice only. For score, MAE and within-one are computed. For bool, the three optional metrics are None. ece = cal.ece. coverage is the share of rows with max P' >= threshold. accuracy_at_threshold is measured on covered rows, and is None when no row is covered. The frozen dataclass has exactly the 10 spec fields. The empty-input case gives n=0 and uncalibrated.
- ✅ U03-128 macro_metric: mean over scoring_use questions present in metrics. The primary metric is accuracy for choice/bool and within_one for score (OI-11). No question gives 0.0.
- ✅ U03-129 steps 1-7: frozen gold at the current fingerprint; latest cache row per (question, hash) at the current fingerprint for Laya and the teacher; question_metrics for both; system accuracy via gate plus teacher; passed per rule (min_* >=, max_* <=, max_gap_to_teacher = teacher.acc - laya.acc <= bound, missing metric -> false); macro_metric; eval.json written atomically (replace_atomic, canonical JSON); Laya calibration.json saved; teacher calibration saved. CalibrationStore.save merges, so the teacher file is updated, not overwritten.
  - eval.json keys match design §4.4 exactly at every level: top, question, laya, teacher, system, criteria/passed.
  - accepted_proposed requires all passed and not uncalibrated.
  - Log event `enrich.distill.candidate_evaluated` carries version, accepted_proposed and macro_metric (§8.1 catalog line 3426).
  - Gauge `herness_enrich_gold_metric_ratio` has labels question/metric/decider (§8.2 line 3452).
  - With no frozen gold, `questions` is empty, macro is 0.0 and nothing is accepted.
- ❌ U03-129 / ET03-02 gold_sha256 row set: frozen rows only. This conflicts with U11-67/F11-09 step 2 and U03-138 (see Important 1).
- ✅ UT03-123 (5 tests: choice/bool/score exact, cross_fit path, empty), UT03-124, UT03-125 (7 tests: keys, values/passed/uncalibrated, calibration files, failed criterion + blocked, gap to teacher, no frozen gold, missing teacher, no cache). ET03-02 has marker `eval`. Every test name/docstring carries its ID, and both modules set pytestmark.
- ⚠️ Cannot verify from diff: the run_distill (U03-136, T03-32/33) wiring of `blocked=` and the evaluate call order (step 11 before step 12). This is a carry-over.

## ⚠️ Items
- The `blocked` keyword (evaluate.py:326) is extra. It is the only way to honour the U03-129 invariant, because the blocked set is computed in U03-136 step 6 and the signature has nothing to carry it. I judge it acceptable: it is optional, defaults to none blocked, and keeps the spec call shape. It needs a spec note on U03-129's signature (controller/DECISIONS).
- The `criteria` = exclude_none dump means score questions show max_mae/min_within_one. This is consistent with "criteria = acceptance_for"; the design example only shows the choice set. OK.

## Strengths
- Clean decomposition (_fit_and_measure shared by question_metrics and evaluate, so ET03-02 and the writer use the same arithmetic).
- Careful handling of edge cases: duplicate cache rows (latest wins), stale fingerprints, other questions' fingerprints, all-zero distributions, a missing teacher, a missing cache and dynamic choice options. The fixture exercises most of them.
- 100% line/branch coverage is claimed. Test assertions check real values (0.99 / 0.9 / 57/60 system accuracy, passed flags), not just shapes.

## Issues

### Critical (Must Fix)
None.

### Important (Should Fix)
1. **gold_sha256 is computed over a row set that spec 11 and `laya accept` will not reproduce.** herness/enrich/evaluate.py:358 (`gold_digest(frozen)`, with rows chosen by `_frozen_gold` at :151) hashes only the frozen-question rows at the current fingerprint.
   - Impl 11 F11-09 step 2 / U11-67 recomputes `gold_sha256` "of `data/labels/<qsv>/gold/` ... over the gold table read from that directory", i.e. all gold rows. U03-138 step 2 says "gold_digest of the current gold".
   - Gold normally holds rows of not-yet-frozen questions and of older fingerprints; the fixture itself has both (business_impact, and the "0"*16 rows). In that state the spec 11 classifier gate fails on every run, and `laya accept` fails too if it follows U11-67.
   - The ET03-02 test (tests/eval/enrich/test_enrich_eval_crosscheck.py:60,80) and UT03-125 (tests/unit/enrich/test_evaluate.py:173) recompute the digest over the same frozen-only subset, so they hide the mismatch.
   - Fix: hash `store.read("gold")` (the whole directory), as U11-67/U03-138 do, and make ET03-02 recompute the digest from the full gold table as spec 11 does. Alternatively, get a controller ruling that changes U11-67 and U03-138 to the frozen-only selection. Either way, the three specs must agree before T03-3x / T11 consume this.

### Minor (Nice to Have)
1. herness/enrich/evaluate.py:364: `calibration.save("laya", ...)` merges into any existing candidate calibration.json. On a re-evaluation with a different blocked/frozen set, stale entries for questions now absent from eval.json survive. This is harmless for a fresh candidate, but worth a comment or a replace mode later.
2. herness/enrich/evaluate.py:290: `bool(passed) and ...` makes empty criteria never proposed. That path is unreachable (AcceptanceCriteria requires at least one criterion), so it is fine. A one-line comment would help readers.
3. ET03-02 recomputes through `question_metrics`, which is the same function evaluate uses. This is what the spec asks for, but it does not cross-check the arithmetic independently. The independent value checks live in UT03-125, which is acceptable.
4. A gold answer outside the fixed bool/score label set raises ValueError from `space.index` (evaluate.py `_score`). Upstream validation (U03-76) makes this unlikely, but the error would not be a HernessError. This is noted in the report and acceptable.

## Assessment
**Task quality:** Needs fixes
**Reasoning:** The metrics, eval.json shape, acceptance rules, atomic writes, calibration saves and observability all match the spec, and the tests are strong. However, gold_sha256 hashes only the frozen rows, while spec 11's cross-check (U11-67) and `laya accept` (U03-138) hash the whole gold directory. That makes the downstream digest check fail whenever gold holds any unfrozen or stale rows. Align the digest with those specs or get a ruling. The `blocked` keyword is acceptable pending a spec note.
