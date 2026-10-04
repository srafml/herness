# T07-19 review (Procedural promotion), verify agent

Worktree D:\herness\.claude\worktrees\agent-a1b3795376e5298aa, base e41d62c, head 5d918f0 (code b03a2f1). Read-only; mutation probes restored, `git status` clean at end.

**Verdict: Needs fixes** (0 Critical, 3 Important, 5 Minor). Code matches spec and rulings; three gates/controls named in the spec are not pinned by any test (mutants survive). Fixes are test-only, in the card's test files.

### Spec Compliance
- ✅ U07-88 `parameterize_sql`: IN lists, dates (ISO / CAST), `*_id` EQ/NEQ, p<n>, kept named params, lowercase identifiers, 8,000-char cap, single statement; fingerprint = sha256(template)[:16] via `policy.keyed_hash` (plain 32-hex sha256 prefix, policy.py:83).
- ✅ U07-89 `wilson_lower_bound`: (0,0)=0, (3,0)=0.4385, (10,1)=0.6226.
- ✅ U07-90 `promote_procedural`: only `verified` = pass (ops filter returns verified + rejected-with-failed-verification); absent and passes_n==0 -> skip; create via `writer.insert_system_item` (T07-08 path); run_id idempotence; merge counts / run_ids(50) / examples(10) / recent(3); TTL renewed only on passes; qa_pairs per (question, query_id) keyed hash, only passing pairs; score / utility / confidence; promote gate min_passes, min_runs, min_pass_lb, "fail" not in recent[-3:]; demote active at demote_pass_lb with qa_pairs; candidates not expired by lb; step 9 embed + set_status.
- ✅ U07-91 `validate_templates`: bind via sqlglot literal nodes, SqlGuard, `EXPLAIN` under `threading.Timer(timeout_s, con.interrupt)`, reset on success, 2 consecutive failures expire with qa_pairs (`validation_failed`). Idle `interrupt()` after a finished EXPLAIN does not poison the next query (probed).
- ✅ Rulings: ProceduralDeps frozen dataclass in procedural.py; defense in depth (`unsafe_reason`: no Literal in template + bound query passes guard) before any write; ST07-20 covers spec row + identifier outside allowlist / column outside schema / denied schema / non-SELECT / unbound placeholder / template with literal / guard spy.
- Tests: UT07-75 ✅ UT07-76 ✅ UT07-77 ✅ UT07-78 ✅ (gaps I1, I2) UT07-79 ✅ (gap I3) PT07-05 ✅ ST07-20 ✅ (34 passed).
- ⚠️ Cannot verify here: MemoryStore facade wiring and guard construction from the CURRENT schema (T07-09 carry-over); IT07-06 / BT07-09 (other cards); ops lookup of qa_pairs by template_id (carry-over, `data.qa_ids` used meanwhile).

### Stored data (item 2)
- `sql_template` row: `sql_template` holds `$name` placeholders only (no Literal, enforced by `unsafe_reason`; mutant M18 killed); `params[*].example` holds the literal values (spec-mandated, needed for U07-91 binding); `fingerprint` is in ID_KEYS (kept).
- `qa_pair.data.sql` = the concrete verified SQL with values, exactly what U07-90 step 6 specifies; only for passing queries; redacted by the write path. No leak beyond spec (see m1 for a quality side effect).

### Redaction concern (item 6): carry-over, not blocking
Probed the real `Redactor` as the tests build it: ISO dates/timestamps, `svc_a`, `INC0012345`, `acme-prod-01` pass unchanged; phone-shaped digit runs (`4155551234`, `20260901-120000-ABCDEF`, `CHG-2026-0901-1234`), IPs and emails are masked. Numeric examples are JSON numbers (never redacted); templates hold no literals, so redaction finds nothing in them. DuckDB `EXPLAIN` accepted `'[PHONE_a]'` against BIGINT and DATE columns, inside `IN (...)`, and under explicit `CAST(... AS DATE/BIGINT)`, so masked examples do not make EXPLAIN fail and cannot expire good templates. Restoring `build_id_last_ok` after the write (procedural.py:284-285) is acceptable (a build id is not personal data). The proper fix (add `build_id_last_ok` to `ID_KEYS`) lives in `_write_steps.py` (T07-08 file): carry-over / ruling, not this card.

### Strengths
Clean split into `_procedural_sql.py` with §2 row; binding only via sqlglot nodes; the guard's output SQL is what gets EXPLAINed; logs carry ids / fingerprints / reasons only (asserted in ST07-20); one `run_write` per fingerprint; PolicyViolation handled per fingerprint.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
- **I1 "fail" not in recent[-3:] gate unpinned; the UT07-78 fail-in-recent test is vacuous.** tests/unit/harness/memory/test_memory_procedural.py:269-275 uses LOW (min_pass_lb 0.1) with 1 pass + 1 fail: wilson(1,1)=0.0945 < 0.1, so `ready` is already false. Mutant M7 (procedural.py:254, drop `"fail" not in recent`) survives. Fix: a config/data set where every other gate passes and only a recent fail blocks (e.g. min_pass_lb 0.05, or several passes then one fail), plus the case where the fail ages out of the last 3.
- **I2 `min_runs` (and `min_passes`) gates unpinned.** procedural.py:252; mutants M8 (drop min_runs) and M17 (drop min_passes) survive. min_runs matters for TH07-20: one run with 9 passing queries of one fingerprint reaches lb 0.7009 >= 0.7, so without the gate a single run promotes alone. Add: one run, 9 passes, default config -> stays candidate; and a min_passes case (e.g. min_passes 3, low lb gate, 2 passes -> candidate).
- **I3 Guard in nightly validation unpinned.** _procedural_sql.py:241; mutant M12 (EXPLAIN without `guard.check`) survives because `test_ut07_79_guard_failure_counts` (test_memory_procedural.py:433-447) swaps in `stg.raw`, which also fails EXPLAIN as the table does not exist in the test warehouse. U07-91 security note (TH07-09) requires the guard. Fix: a template whose table/column exists in the warehouse but is outside the guard schema (EXPLAIN alone would pass), or a guard spy asserting the bound SQL is checked.

#### Minor (Nice to Have)
- **m1** procedural.py:222: `qa_pair.data.sql` (spec-mandated concrete SQL) after write-path redaction can become invalid SQL: a phone-shaped numeric literal becomes a bare token (`IN ([PHONE_d963939f36], 5)`, probed). Affects LoRA export (U07-92) quality; carry-over to T07-08 / U07-92.
- **m2** procedural.py:58,269: the lookup uses (candidate, active). A template whose content/question trips the injection scan is stored `pending_approval` (policy.py:335); later runs do not find it, and since the create key includes run_id (procedural.py:185) each run adds another pending template for the same fingerprint. Include `pending_approval` in the lookup or add a spec-note reading.
- **m3** procedural.py:209-211: question examples added on update are written with `ops.update_memory_item` (redacted by `deps.redactor` in `_question`) but skip the write-path injection scan the create path gets. Low risk; spec-note it.
- **m4** _procedural_sql.py:84: `datetime.fromisoformat` (3.12) also accepts basic forms (`20260901`, `2026-W01`), so digit-string ids may be typed `date`. Fingerprint stability unaffected; parameter naming only.
- **m5** Commit b03a2f1 subject `wip(T07-19)` is not a Conventional Commits type; trailers omit "(1M context)" from global-constraints (history is mixed). The merge/squash message can fix it.

### Mutation probes (card tests `-k "UT07_75 ... ST07_20"`, each restored)
| # | Mutation | Result |
|---|---|---|
| M1 | `unsafe_reason` without `guard.check` | killed (5 ST07-20) |
| M2 | rejected findings counted as passes | killed (ST07-20 x2, UT07-78 x3) |
| M3 | drop "template has no Literal" check | killed (ST07-20 template_literal) |
| M4 | skip run_id in run_ids idempotence | killed (UT07-78 rerun) |
| M5 | failures alone create a template | killed (ST07-20 spec row) |
| M6 | `unsafe_reason` never called | killed (7 ST07-20) |
| M7 | drop `"fail" not in recent` | **survived** (I1) |
| M8 | drop `min_runs` | **survived** (I2) |
| M9 | candidates expired by pass_lb | killed |
| M10 | expire after 1 validation failure | killed |
| M11 | success does not reset validation_failures | killed |
| M12 | EXPLAIN without guard | **survived** (I3) |
| M13 | Timer never started | killed (UT07-79 timeout) |
| M14 | qa_pairs also for failing pairs | killed |
| M15 | literals not replaced (PT07-05 meaningfulness) | killed (PT07-05 + 23 others) |
| M16 | drop `min_pass_lb` | killed |
| M17 | drop `min_passes` | **survived** (I2; redundant under default config) |
| M18 | concrete SQL stored as `sql_template` | killed |

PT07-05: hypothesis varies 2 dates, an id, a string, an IN list and a number/LIMIT over 60 examples; M15 turns it red, so it is meaningful, not vacuous.

### Gates
Card tests 34 passed (14.9 s). Coverage procedural.py 98 % (3 lines, 3 partial branches), _procedural_sql.py 99 % (>= 90/85). ruff check pass; ruff format --check 911 formatted; mypy 0 issues (335 files); lint-imports 13 kept 0 broken; tools/check_module_size.py rc 0. Lines 383/390 and 252/260 (§2 row present). C901 / PLR0913 clean (max 6 args: `_proposal`). No HTTP client imports. No SQL or values in logs or error messages (constant `_UNBOUND`, reasons / rule names only). Test IDs, ID-first docstrings and `pytestmark = pytest.mark.unit` present in both test files.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Implementation is correct and spec-faithful with sound defense in depth, but three spec controls (recent-fail gate, min_runs/min_passes gates, guard in nightly validation) are not pinned by any test; removing them stays green. Test-only fixes in the card's test files.


## Re-review round 1 (head 0948452; commits a476025, 0948452 over 5d918f0)

**Verdict: Approved.** I1-I3 and rulings (a)-(d) are resolved. 2 new Minor items, no new Critical or Important. Read-only; every probe restored, `git status` clean, HEAD 0948452.

### Prior findings
- ✅ **I1** `test_ut07_78_fail_in_recent_blocks_promotion` now passes every other gate (5 passes, 1 fail, 2 runs, lb >= 0.1, recent `[pass, pass, fail]`), stays candidate, then promotes once 3 later passes push the fail out of recent[-3:]. M7 killed.
- ✅ **I2** `test_ut07_78_single_run_cannot_promote_alone` (9 passes in one run, lb 0.70, default config, stays candidate) kills M8. `test_ut07_78_min_passes_gate` (4 < 5, other gates pass, the 5th promotes) kills M17.
- ✅ **I3** `test_ut07_79_guard_rejection_alone_fails_validation` uses a blocked column that EXPLAIN accepts, so only the guard fails; it then expires on the second run. M12 killed.

### Controller rulings
- ✅ **(a)** `_write_steps.py` diff is the single line `"build_id_last_ok",` in `ID_KEYS` (file 246 lines). Regression test `test_ut07_78_build_id_passes_write_path_unredacted` runs the phone-like build id through `insert_system_item` and gets it back verbatim; A1 (remove the key) is killed by it and by `test_ut07_78_unknown_run_and_unparsable`. The post-write restore is gone: `build_id_last_ok` is now set in `_create` and in `_merge` (passes only). The exemption covers only a str / list[str] under that key, the same rule as the existing ID keys, so it opens no new class of hole. Residual: see r2. Full memory + security suites: 1173 passed, 1 skipped, 1 xfailed (pre-existing ST10-14).
- ✅ **(b)** The lookup uses `_OPEN = (candidate, pending_approval, active)` (procedural.py:59, 279). `_score` only moves `candidate` -> `active` and `active` -> `expired`, so a pending template takes counts, runs and examples but this code never changes its status. That respects the invariant (`candidate -> active -> expired`, `candidate -> expired`), and leaving `pending_approval` stays the reviewer's approve/reject. Sound against TH07-20: the pre-write guard and Literal checks still run on every fingerprint with passes, qa_pairs of a pending template are created as candidates, and validation only touches active templates. B1 (old lookup) and B2 (pending promotable) are killed by `test_ut07_78_pending_template_is_not_duplicated`. B3 (pending expirable by lb) survives: see r1.
- ✅ **(c)** `ProceduralDeps.scanner: InjectionScanner` (procedural.py:76); `_merge` adds only fresh questions where `scanner.scan(q)` is empty. The test env builds it from the same `injection_patterns` as the writer. `test_st07_20_injected_question_not_added_on_update` asserts the planted question is absent from the template data and its qa_pair is `pending_approval` (flagged by the write path). C1 (skip the scan) killed.
- ✅ **(d)** The spec note under U07-92 (07-memory.impl.md:2050) tells the T07-20 owner that `qa_pair.data.sql` is redacted concrete SQL and may not parse. It recommends parsing with sqlglot and dropping/counting failures or redaction-marker pairs, or using the template. This addresses m1. The T07-19 spec note items (1) and (4) are updated for the scanner, ID_KEYS, the `_OPEN` lookup and pending handling.

### New findings
#### Minor
- **r1** procedural.py:266 (`_score`): the claim that a pending template is never expired by this code is only half pinned. Mutant B3 (`status in ("active", "pending_approval")` for the demote branch) survives. Add failing runs to `test_ut07_78_pending_template_is_not_duplicated` (tests/unit/harness/memory/test_memory_procedural.py) and assert it stays `pending_approval`. The code is correct today.
- **r2** herness/harness/memory/_write_steps.py:47: `ID_KEYS` applies to every kind and author, so any proposal (including an agent-authored one, which policy keeps `pending_approval`) can store an unredacted string under `data.build_id_last_ok`. This is the same accepted pattern as `service_id`, `rule_id` and others, but it adds one more such key. Hardening option for T07-08: exempt that key only when the value matches the core build-id format (herness/core/ids.py:37 `_BUILD_ID_RE`). Not blocking.
- Note: procedural.py is at 390/390, with no headroom left for follow-ups.

### Mutation probes, round 1 (card tests, each restored)
| # | Mutation | Result |
|---|---|---|
| M7 | drop `"fail" not in recent` | killed (fail_in_recent) |
| M8 | drop `min_runs` | killed (single_run_cannot_promote_alone, fail_in_recent) |
| M17 | drop `min_passes` | killed (min_passes_gate) |
| M12 | EXPLAIN without guard | killed (guard_rejection_alone_fails_validation) |
| A1 | `build_id_last_ok` removed from ID_KEYS | killed (2 tests) |
| B1 | lookup back to candidate/active | killed (pending_template_is_not_duplicated) |
| B2 | pending template promotable | killed (pending_template_is_not_duplicated) |
| B3 | pending template expirable by pass_lb | **survived** (r1) |
| C1 | update skips the injection scan | killed (ST07-20 injected_question) |
| M1 | `unsafe_reason` without guard (regression) | killed (5 ST07-20) |
| M2 | rejected findings counted as passes (regression) | killed (5) |
| M4 | skip run_id idempotence (regression) | killed (UT07-78 rerun) |

### Gates
- Card tests: 40 passed.
- Coverage: procedural.py 98 % (lines 230, 354, 380), _procedural_sql.py 99 %.
- Memory + security suites: 1173 passed, 1 skipped, 1 xfailed (pre-existing).
- ruff check pass; ruff format --check 911 formatted; mypy 0 issues (335 files); lint-imports 13 kept, 0 broken.
- tools/check_module_size.py rc 0 with procedural.py at exactly 390/390.

### Assessment
**Task quality:** Approved
**Reasoning:** All three Important gaps are now pinned by tests that kill their mutants, and rulings (a)-(d) are implemented soundly with regression tests. The two remaining items are a one-assert test gap and a hardening suggestion in the T07-08 file.
