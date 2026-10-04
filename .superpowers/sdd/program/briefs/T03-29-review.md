# T03-29 Sampling and gold — verify review

Reviewer: verify agent · worktree `D:\herness\.claude\worktrees\agent-a6aa5c1fe127adcdd` · head f4af09f (base e41d62c) · tree left clean (`git status` empty after all probes).

**Verdict: Needs fixes.** The code matches the units. Five binding rules have no test that fails when the rule is broken (mutation probes M8, M11, M16, M18 and M23 survived). One of them is the TH03-04 training exclusion in the gold draw. The freeze rule also needs a ruling.

### Spec Compliance
- ❌ Issues found: the code is correct per unit, but the tests leave five binding rules unpinned (see Important 1–2). The freeze rule is a lenient reading of U03-126 (Important 3).

| Unit / test | Status | Note |
|---|---|---|
| U03-120 stratum_of | ✅ | Bands p12/p3/p45 (NULL→p45), `s`<120, `m` 120–600 inclusive, `l`>600 in Python (sampling.py:109-113) and SQL (sampling.py:60-63). Quarter from UTC in both. Session-TZ safe because the real columns are TIMESTAMPTZ (model/sql/230_incident.sql:32, problem :12). DuckDB `length` counts code points like Python `len` (probe: "é" 2/2, ZWJ emoji 5/5). Top 50 by count, then id (:53-56). |
| U03-121 allocate | ✅ | sqrt share, max(min_per, floor), capped; remainder and excess in (−frac, key) order, skipping caps or `min(min_per,N_h)` (:116-153). Precondition branch is literal (Σ may exceed total). Deterministic (keys sorted). |
| U03-122 stratified_sample | ✅ | Dedupe keeps the lowest record_id (:71). Exclusion via a registered view (:70). `sha256($salt‖hash)` order with a hash tiebreak (:79). 3×n_h only with a snapshot (:263). Walk is stratum then hash order with quota and prototype cap (:214-232). Cap is `max(1, floor(share×size))`, a deviation only for size<50 (documented). No unseeded randomness. |
| U03-123 select_active | ✅ | lexsort (−u, hash), cut to `candidates`, per-prototype walk (:290-314). |
| U03-124 fold_of | ✅ | gold.py:57-59 |
| U03-125 request_gold | ✅ (⚠️ probability null) | Independent salted draw that excludes all teacher hashes; class top-up in pool hash order; create_if_absent with ("purpose","question","content_hash") plus scope qsv, blocking pending. This matches ensemble_stage.py:41/225-226. sync_label_checks maps `purpose` to `gold_reviews` and only takes `approved` items (labels.py:362), which is consistent with `_follow_up_payloads` reading approved items. The store still matches on all 4 keys (review_items.py:109). Accepted. `probability`/`decider` are null because the signature only gives the answer and gold excludes training, so no teacher row exists. Acceptable. |
| U03-126 consolidate_gold | ✅ (Important 3) | First-answer-per-reviewer dedupe, agreement, adjudication (generalised to the first later reviewer who matches; sensible). Follow-ups use the original payload. Append only for hashes not yet gold; frozen questions skipped; digest recorded; `labels.lock` held. |
| UT03-115 | ✅ | Edge records × 3 session TZs; Python == SQL |
| UT03-116 | ✅ | {5,1,44} sum 50 |
| UT03-117 | ✅ (gap: 3× factor, M11) | |
| UT03-118 | ✅ | |
| UT03-119 | ✅ | |
| UT03-120 | ⚠️ weak | Training-exclusion assert does not bite (M18); the ≥1 % threshold is not distinguishable (M23) |
| UT03-121 | ✅ | |
| UT03-122 | ⚠️ weak | Freeze with pending>0 not covered (M16) |
| PT03-15 | ✅ | Inside the precondition domain; adds a ≥ min(min_per,N_h) check and order invariance. Fractional ordering not pinned (M8) |
| ST03-05 | ✅ (teacher-part leg circular) | Real LabelStore gold → exclude → sample disjoint. build_training_set drops seeded gold teacher rows. Logs carry no sample text. A spy proves fixed SQL, only `entities`/`salt` bound, a hostile salt stays a parameter, and no gold hash appears in the SQL. The "teacher parts" check appends the sample and re-asserts, so it only restates the sample check. |

- ⚠️ Cannot verify here: the T03-30 caller must pass gold-plus-pending-gold hashes as `exclude_hashes` for retraining samples. The size of real-data strata (inflation, see Important 4).

### Strengths
- All SQL is fixed text; values are bound and row sets are registered under fixed view names. ST03-05 checks this with a spy and a hostile salt.
- Errors are mapped: IOException → StoreBusy; Catalog/Binder → SchemaViolation with the first line; anything else gives the class name only (no row values).
- Payloads use `text_ref`. Logs carry counts and ids only.
- The gold tests run against the real ops store, decide_review_item and sync_label_checks, so the whole flow is exercised end to end.
- Coverage: sampling 100 %; gold 99 % (3 partial branches).

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. **The TH03-04 gold-draw exclusion is not tested.** Mutating gold.py:167 to `exclude_hashes=frozenset()` passes all 24 tests (M18). test_gold.py:118 seeds a single training hash that is never drawn anyway. Fix: make a large share of the pool training hashes (for example every 2nd record) and assert none are requested, including through the top-up pool.
2. **Freeze-with-pending and other binding rules are not pinned by tests.**
   - (a) gold.py:298: dropping `pending == 0` survives (M16). No test reaches a complete set while an item is still pending. Add one with n_gold ≥ gold_size and one pending item, and assert it is not frozen. Freeze is irreversible.
   - (b) sampling.py:150: smallest-fraction-first ordering survives (M8). No case has unequal fractional parts with a remainder. Add an allocate case where (−frac, key) and (frac, key) differ.
   - (c) sampling.py:31: CANDIDATE_FACTOR 3→1 survives (M11). Add a sample where the prototype cap rejects early candidates and assert the stratum still fills from the 3× candidates.
   - (d) gold.py:131: dropping the ≥1 % prevalence condition survives (M23). The sub-1 % class `d` is never returned by `_teacher_answer`, so the test cannot distinguish. Let `_teacher_answer` return `d` for some hashes and assert `d` gets no top-up.
3. **Freeze rule reading (gold.py:297-298; spec U03-126 invariant).** The spec says "≥ gold_size rows (or ≥ 1,000 when the class top-up is exhausted)". The builder freezes at ≥ min(gold_size, 1000) whenever nothing is pending, which drops the "class top-up exhausted" condition. This differs from the spec when requested items are **rejected** (they leave no review and are not pending). Example: 1,500 requested, 300 rejected → frozen at ~1,200, possibly with a ≥1 % class under 30. Under the spec it would not freeze, because the top-up is not exhausted. Freezing is permanent (labels.py:209-221). This is a real deviation, not just wording. Fix options: also require the class needs (computed from teacher prevalence against gold rows, which consolidate_gold can read from `store`) to be met before applying the 1,000 floor; or get a controller ruling recording the lenient reading as a spec note. The 1,000 floor itself matches spec 11's gate (docs/specs/11 §Phase 4 "≥ 1,000 records").
4. **Allocation inflation (plan-mandated; sampling.py:145-146, spec U03-121 precondition).** When `total < 5 × strata`, every stratum gets min(5, N_h). With real strata (50 services × 3 bands × N quarters × 3 lengths) the gold draw of 1,500 can become several thousand `label_check` items per question, against a design budget of 1,500 × 2 reviewers. The code is correct per spec, so this is labelled plan-mandated and goes to the spec owner (coarsen quarters for gold, or scale min_per down). The training sample (30k) is affected only above 6,000 strata.

#### Minor (Nice to Have)
1. **labels.py `read("gold")` / `gold_hashes()` raise a raw pyarrow `ArrowInvalid`** (not mapped to SchemaViolation/StoreBusy) when `gold/` holds only `_reviews/` or `_frozen/`. Reproduced in a probe: both calls fail once a single gold_reviews row exists. The local `_read_gold` workaround (gold.py:62-68) is acceptable for this card. However, `gold_hashes()` is what T03-30 uses for exclusion, so a labels.py fix (T03-18 owner) is owed before T03-30. Track it as a follow-up card.
2. **Pending, rejected or under-review gold candidates are not in `gold_hashes()`.** A later retraining sample (`train:<v2>`) can draw a hash that becomes gold afterwards. Forward to T03-30: exclude the gold-purpose `label_check` hashes (pending and decided) as well.
3. **Cross-module private import:** gold.py:32 imports `sampling._ordered_pool`. Consider a public name, or note it in the module map.
4. **The ST03-05 "teacher parts" leg is circular** (test_sampling_security.py:446-447 appends the sample and re-checks it). The real guarantee is the sample exclusion plus `build_training_set`.
5. **Prototype cap `max(1, …)`** (sampling.py:269) deviates from the spec for size < 50 (documented). It is harmless but should be recorded as a spec note.
6. **`_consolidate_question` re-sorts the whole reviews table once per question** (gold.py:227). O(Q·R log R); sort once in `consolidate_gold`.

### Mutation probes (applied in place, reverted with `git checkout --` after each)
| Probe | Result |
|---|---|
| M1 SQL `<120`→`<=120` | killed |
| M2 SQL `<=600`→`<600` | killed |
| M3 SQL band 3 mis-mapped | killed |
| M4 SQL quarter without UTC (session TZ) | killed |
| M5 drop exclude view | killed |
| M6 salt ignored in order | killed |
| M7 dedupe keeps highest record_id | killed |
| M8 allocate smallest fraction first | **survived** |
| M9 allocate without min_per | killed |
| M10 prototype cap off by one | killed |
| M11 candidate factor 3→1 | **survived** |
| M12 top 51 services | killed |
| M13 select_active tie order | killed |
| M14 repeated reviewer counted twice | killed |
| M15 adjudicated flag false | killed |
| M16 freeze ignores pending | **survived** |
| M17 freeze floor = gold_size only | killed |
| M18 gold draw without training exclusion | **survived** |
| M19 no class top-up | killed |
| M20 rewrite already-gold rows | killed |
| M21 fold inverted | killed |
| M22 no follow-up items | killed |
| M23 ≥1 % prevalence ignored | **survived** |

### Gates
- Card tests: 24 passed (`PYTHONUTF8=1 uv run pytest … -q -p no:logging`).
- Coverage (branch): sampling.py 100 %; gold.py 99 % (partials 173→176, 179→184, 229→228). Meets the ≥90/85 bar.
- ruff check: clean. ruff format --check: 6 files formatted. mypy: no issues (305 files). lint-imports: 13 kept / 0 broken. check_module_size: 0. check_type_ownership: 0.
- Sizes: sampling.py 314/360, gold.py 303/320.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation follows U03-120..126 closely, and the SQL is safe and deterministic. Five binding rules have no test that fails when the rule is broken, including the TH03-04 training exclusion in the gold draw and freeze-while-pending. The freeze rule also needs tightening or an explicit ruling, because freezing is irreversible. Allocation inflation is plan-mandated and goes to the spec owner.

---

## Re-review round 1 (f4af09f..94ea7a5)

Scope: I-1, I-2 (a–d), I-3 (judged against the sub-controller ruling that "class top-up exhausted" means every ≥1 % teacher-prevalence class has ≥30 gold rows), m4, m5 (spec note) and m6. I-4, m1, m2 and m3 are parked by the sub-controller and are not re-raised.

**Verdict: Approved.**

| Item | Status | Evidence |
|---|---|---|
| I-1 training exclusion in the gold draw | ✅ | New UT03-120 case: half of the pool is training hashes, and none are requested through either the draw or the top-up. M18 is now killed. |
| I-2a freeze while pending | ✅ | gold_size rows plus one pending item are not frozen; after that item is rejected, the set freezes. M16 is killed. |
| I-2b fractional order | ✅ | `{a:100, b:400}`, total 10 → `{a:3, b:7}`. M8 is killed. |
| I-2c 3× candidates | ✅ | A capped stratum still fills from the 3× candidates. M11 is killed. |
| I-2d ≥1 % threshold | ✅ | A class at exactly 1 % is topped up; a class at 0.9 % is not. M23 is killed. |
| I-3 freeze rule | ✅ | gold.py now computes `complete = n_gold >= gold_size or (n_gold >= 1000 and exhausted)`, where `exhausted = not _class_needs(teacher, q, gold answers)`. It uses the same ≥1 % / ≥30 rule as request_gold, at the current fingerprint, and the freeze still requires `pending == 0`. This matches the ruling. When there are no teacher rows, there are no class needs, which is safe. The new UT03-122 case (1,059 rows with a 1.5 % class at 29 → not frozen; at 30 → frozen) pins the rule. Probes: M24 (floor without the top-up condition), M25 (exhausted without the 1,000 floor), M26 (no exhausted branch) and M27 (MIN_CLASS 29) are all killed. |
| m4 ST03-05 circular | ✅ | A control draw without the exclusion leaks ≥10 seeded gold hashes into its teacher parts. The real teacher parts equal the sample and contain no gold hash. `build_training_set` drops the leaked gold hashes from the combined teacher table. |
| m5 prototype cap | ✅ | Recorded as a spec note in the report. |
| m6 sort once | ✅ | Reviews are sorted once in `consolidate_gold` (gold.py, right after the read). |

Mutation probes, round 1 (applied in place, each reverted with `git checkout --`; the tree is clean): M8, M11, M16, M18, M23, M24, M25, M26 and M27 are killed. M28 (removing the reviews sort) survives, because the tests append reviews in chronological order. This gap predates the round and is Minor.

Gates: 30 card tests passed. Coverage: sampling.py 100 %; gold.py 99 % branch (3 partials). ruff check and ruff format are clean. mypy reports no issues. lint-imports: 13 kept. check_module_size 0, check_type_ownership 0. Sizes: gold.py 311/320, sampling.py 314/360.

Open findings (Minor, non-blocking):
- M28: no test pins the review order. Add a case where reviews are appended out of `labeled_at` order.
- `_mask` (gold.py) calls `_fingerprint(q)` once per gold row; compute it once per question.
