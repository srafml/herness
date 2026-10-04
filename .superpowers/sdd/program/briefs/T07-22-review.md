# T07-22 verify review: memory_maintenance job handler (U07-96; UT07-84, FT07-01; TH07-13)

Worktree D:\herness\.claude\worktrees\agent-a66f9894474cca5d8 @ 16dc3b3 (base 5f2b7c0). Reviewer: verify agent. All probes reverted; the working tree is clean at the end.

### Spec Compliance
- ✅ Spec compliant (U07-96 steps 1-7 and the outcome/log/gauge contract).

| Item | Result | Evidence |
|------|--------|----------|
| Signature `memory_maintenance_handler(ctx) -> JobOutcome`, one arg (R-42) | ✅ | maintenance.py:184 |
| After each step: `save_state({"step": i+1, "counts"})` → `heartbeat()` → `should_yield()` → `JobOutcome("yield", counts)` | ✅ | maintenance.py:192-198 (P09, P10 killed; per-step heartbeat unpinned, P29) |
| Resume at `ctx.load_state()["step"]`, earlier steps not redone | ✅ | maintenance.py:187-192 (P11 killed) |
| 1. `expire(now)` | ✅ | maintenance.py:94-95 |
| 2. `promote_procedural(run_id)` for `recent_done_runs(now − 7 d)` | ✅ | maintenance.py:47, 98-103 (P05, P35 killed) |
| 3. `validate_templates` on read-only CURRENT connection (closed after) | ✅ | maintenance.py:106-116 (P31 killed); see ruling (a) and Minor M1 |
| 5. pages of 1000 of `list_ids` + `all_ids_status` merged in id order; delete orphan vectors only; `set_status` from SQLite; flag missing / hash / model mismatch | ✅ | _maintenance_vectors.py:34, 41-128 (P12-P19, P30, P37 killed; page size unpinned, P01) |
| 4. `fts_check_and_rebuild` | ✅ | maintenance.py:119-121 |
| 6. `maintenance_rows("embedding_pending", limit=5000)`, embed+upsert, clear flag, heartbeat every 256, `ModelUnavailable` stops step only | ✅ | _maintenance_vectors.py:35-36, 138-170 (P02, P04, P22, P27, P34, P36 killed) |
| 7. `business_rule_review_due`, cutoff now − 365 d; `create_review_item("memory_write", payload flags ["yearly_review"] + §4.1 fields, now, conn)` + `update_memory_item(last_review_requested_at=now)` in one `run_write` | ✅ | maintenance.py:132-156 (P08, P20, P21, P32 killed; 365 boundary unpinned, P06/P07) |
| `JobOutcome("done", counts)`, log `memory.maintenance.completed` (job_id, counts) | ✅ | maintenance.py:180-181 (P26 killed) |
| Gauges `herness_memory_items_total{status}`, `herness_memory_embedding_pending_total` via `record_metric_samples` only (R-12) | ✅ | maintenance.py:165-179 (P25 killed) |
| Review items only via `create_review_item` | ✅ | maintenance.py:145 |
| `StoreBusy` / `RetryableError` propagate | ✅ | no catch outside step 3 NotFound and step 6 ModelUnavailable (P23 killed) |
| TH07-13: step 5 never changes SQLite status / never deletes rows; orphan vectors deleted only after SQLite re-read; status repaired FROM SQLite; healthy items untouched | ✅ | _maintenance_vectors.py:98-119 (P12, P13, P14, P16 killed; test_ut07_84_written_items_stay_consistent) |
| Backfill ≤ 5000 | ✅ | selection limit 5000 + slice (P02, P34 killed; P03 equivalent) |
| Yearly review once per item | ✅ | P20, P32 killed; test_ut07_84_yearly_review_once_per_item |
| Registration via `register_handler` (registry half), composition root carried to T07-23 | ✅ | test_ut07_84_registered_handler_runs |
| MaintenanceDeps seam, `ConfigError` when unset, only module state `_SEAM` | ✅ | maintenance.py:53-80 |
| Test IDs: UT07-84 (24 fns), FT07-01 (3 fns), names + docstring first line carry IDs, pytestmark unit / fault | ✅ | |
| Coverage maintenance.py 100 % line / 100 % branch; _maintenance_vectors.py 100 % line / 97.7 % branch (94->93) | ✅ | re-measured |
| Budgets maintenance.py 199/260; _maintenance_vectors.py 170/180 (§2 row added) | ✅ | |

- ⚠️ Cannot verify here: (1) composition-root registration and `configure_maintenance` wiring (T07-23 carry-over); (2) IT07-10 is not in this card's Tests row; (3) `register_handler` is imported from `herness.core.jobs.handlers` because `herness.core.jobs` does not re-export it on base (impl 08 export gap, not this card's); (4) a yearly-review `memory_write` item refers to an item that is already `active`; U07-51 step 2 raises `approve.not_pending` when `approved_by` is absent, so how a reviewer acts on it belongs to the approve/reject units and spec 09 — flag for the integration review; (5) `VectorIndex.list_ids` (store.py:250-260) reads the whole table on every page, so step 5 is O(N²/1000) at scale (T07-09's unit, not this card's).

### Gates (re-run by the verifier)
ruff check: clean · ruff format --check: 975 files formatted · mypy (project config, strict): 0 issues in 356 files · lint-imports: 13 kept, 0 broken · check_module_size: exit 0 · check_type_ownership: exit 0 · `pytest tests/unit/harness/memory tests/fault/harness -q`: 596 passed · card tests: 27 passed.

### Strengths
- Step 5 merges the two ordered streams lazily and re-reads SQLite per 500-id batch before acting; the orphan-race test is real (the row is inserted after the SQL stream is exhausted).
- Flag clear is guarded by the embedded content_hash (no lost re-embedding when content changes mid-run).
- Yearly review re-checks status and `last_review_requested_at` inside the transaction; strong idempotency.
- FT07-01 tests use the real `fault_point("sqlite.write")` harness plus a raising embedder and a resume-after-StoreBusy case; all three go red when step 6 is disabled (P28).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- M1 herness/harness/memory/maintenance.py:109 — `except NotFoundError` also swallows `NotFoundError(kind="build")` from `warehouse._existing_path` (CURRENT points at a missing build file, herness/store/warehouse.py:93-97) and logs it as INFO `no_current_build`, hiding a broken warehouse. Fix: `except NotFoundError as exc:` and re-raise unless the error's `kind` detail is `"current"`; add one test with CURRENT pointing at a missing build file.
- M2 tests/unit/harness/memory/test_memory_maintenance.py:292-304 — the 365-day cutoff is unpinned (P06 364 d and P07 366 d both survive; fixtures use 366 d vs 100 d). Fix: seed rules at `NOW − 364 d` (not due) and `NOW − 365 d` (due, `created_at <= cutoff`) and assert only the latter gets a review item.
- M3 tests/unit/harness/memory/test_memory_maintenance.py:143-156 — page size 1000 is unpinned (P01 PAGE=999 survives). Fix: spy `ops.maintenance_rows` and `env.vectors.list_ids` in the full-run test and assert `limit == 1000` for `all_ids_status` and the `list_ids` limit argument.
- M4 tests/unit/harness/memory/test_memory_maintenance.py:107 — the per-step `ctx.heartbeat()` is unpinned (P29 deleting maintenance.py:196 survives). Fix: in the full-run test assert the step notes equal `[f"memory_maintenance step {i}" for i in range(1, 8)]`.
- M5 tests/unit/harness/memory/test_memory_maintenance.py:130-140 — step 5 adding `"embedding_pending"` to `data.flags` is unpinned (P38 survives). Fix: run only step 5 (as the TH07-13 test does) and assert the flagged row has `data.embedding_pending is True` and `"embedding_pending" in data["flags"]`.
- M6 herness/harness/memory/_maintenance_vectors.py:37 — `_CHECK = 500` must stay ≤ the `get_memory_items` C2 limit (`valid_ids(most=500)`, herness/store/ops/_memory_rows.py:185-191); P33 (501) survives because no test has > 500 ids in a batch. Fix: seed 501 rows (no vectors needed) in one test so an oversized batch would raise, or derive `_CHECK` from the ops constant.
- M7 tests/unit/harness/memory/test_memory_maintenance.py:430-433 — spy named `deletes` actually records `lifecycle.expire`; the saved-step-7 rerun never spies `vectors.delete`. Fix: rename to `expires` and add a `vectors.delete` spy asserting no call on the step-7 resume.

### Mutation probes (each applied in place, card tests run, file restored)
| # | Mutation | Result |
|---|----------|--------|
| P01 | PAGE 1000→999 | survived (M3) |
| P02 | BACKFILL_MAX 5000→5001 | killed |
| P03 | drop `[:BACKFILL_MAX]` slice | equivalent (selection limit is already 5000) |
| P04 | BEAT_EVERY 256→255 | killed |
| P05 | 7 d→8 d | killed |
| P06 | 365 d→364 d | survived (M2) |
| P07 | 365 d→366 d | survived (M2) |
| P08 | flags ["yearly_review"]→["yearly"] | killed |
| P09 | save step `index` not `index+1` | killed |
| P10 | should_yield ignored | killed |
| P11 | resume ignored (start 0) | killed |
| P12 | step 5 writes vector status into SQLite (TH07-13) | killed |
| P13 | rejected row treated as orphan, vector deleted (TH07-13) | killed |
| P14 | no vector status repair | killed |
| P15 | rejected items flagged (concern b) | killed |
| P16 | no SQLite re-read before orphan delete | killed |
| P17 | missing vector not flagged | killed |
| P18 | model change ignored (LLM03) | killed |
| P19 | content_hash change ignored | killed |
| P20 | no `last_review_requested_at` stamp | killed |
| P21 | review item written outside the row's run_write | killed |
| P22 | ModelUnavailable propagates from backfill | killed |
| P23 | per-step errors swallowed (StoreBusy/Retryable) | killed |
| P24 | step 3 NotFound not caught | killed |
| P25 | pending gauge renamed | killed |
| P26 | completed log removed | killed |
| P27 | flag cleared regardless of hash | killed |
| P28 | step 6 no-op (FT07-01 red check) | killed (12 tests incl. all 3 FT07-01) |
| P29 | per-step heartbeat removed | survived (M4) |
| P30 | merge `<=`→`<` | killed |
| P31 | DuckDB connection not closed | killed |
| P32 | review status not re-checked in tx | killed |
| P33 | _CHECK 500→501 | survived (M6) |
| P34 | backfill selection limit 10000 | killed |
| P35 | step 2 window ignored | killed |
| P36 | backfill heartbeat removed | killed |
| P37 | already-pending items re-flagged | killed |
| P38 | step 5 flag not added to `data.flags` | survived (M5) |

### Rulings on builder concerns
- (a) No CURRENT build → step 3 skipped with INFO `memory.maintenance.templates_skipped`, job continues: **accepted**. With no promoted warehouse there is nothing to EXPLAIN against; failing the job every day on a fresh install would block steps 4-7 and burn retries. Narrow the catch to the `kind="current"` case so a dangling CURRENT still fails loudly (M1).
- (b) Rejected items not flagged in step 5: **confirmed**. `_needs_embedding` returns False for `status == "rejected"` (_maintenance_vectors.py:79), consistent with the step 6 selector `status <> 'rejected'` (_memory_rows.py:76) and `pending_embedding_count`; P15 (guard removed) is killed by test_ut07_84_th07_13_step5_never_changes_sqlite. Their vector status is still repaired to `rejected` (P14 killed), which is what TH07-13 needs.
- (c) Import of `stored_flags` from T07-08's private `_write_steps.py`: **acceptable**. Same package, read-only pure helper, keeps flag normalisation identical to the writer; no layer or ops-area contract crossed (lint-imports clean). Optional: name `_write_steps.stored_flags` in the `_maintenance_vectors.py` §2 row dependency column (currently "none").
- (d) §8.1 events and spec note: **accurate**. `memory.maintenance.templates_skipped` INFO `reason` matches maintenance.py:110; `memory.maintenance.backfill_stopped` WARNING `embedded` matches _maintenance_vectors.py:168; spec note items (1)-(7) match the code (500-id re-read batches, 256-item chunks with one upsert and one clear tx each, review limit 10,000 with in-tx re-check, counts schema, bad-state restart, should_yield after step 7).

### Assessment
**Task quality:** Approved
**Reasoning:** All seven steps, the TH07-13 bounds and resume/idempotency hold under 38 probes (31 killed, 1 equivalent); the six survivors are test-pinning gaps (page size, 365-day boundary, per-step heartbeat, step 5 flag list, re-read batch cap), and the one code finding (over-broad NotFound catch) is Minor.

## Re-review round 1

Worktree D:\herness\.claude\worktrees\agent-a66f9894474cca5d8 @ 5be98d6 (fix base 16dc3b3). Reviewer: re-verify agent. Every probe applied in place, card tests run (tests/unit/harness/memory/test_memory_maintenance.py + tests/fault/harness/test_memory_maintenance_fault.py, 30 tests), file restored with `git checkout --`; `git status` clean at the end.

### Findings
| Finding | Status | Evidence |
|---|---|---|
| M1 broad `except NotFoundError` in step 3 | ✅ closed | maintenance.py:109-111 re-raises unless `exc.kind == "current"`. `NotFoundError` sets `self.kind` (herness/store/errors.py:23-25). warehouse.py:222 raises `kind="current"` only when `read_current` returns None (no CURRENT file); a CURRENT naming an absent build raises `kind="build"` from `_existing_path` (warehouse.py:93-97, reached via `read_current` line ~123 and `open_readonly` line 223). New test test_ut07_84_current_naming_missing_build_fails (test file:229) drives the real `warehouse.open_readonly` and goes red under the old broad catch (`DID NOT RAISE`). The fresh-install skip test (test file:208, `kind="current"`) still passes. Spec note (3) at docs/impl/07-memory.impl.md:2137 is accurate. |
| M2 365-day cutoff | ✅ closed | test file:332 seeds NOW−365 d (due) and NOW−364 d (not due); P06 and P07 both red. |
| M3 page size 1000 | ✅ closed | full-run test (test file:80) spies `all_ids_status` and `list_ids` limits == 1000; P01 red. |
| M4 per-step heartbeat | ✅ closed | full-run test asserts step notes 1..7; P29 red. |
| M5 step-5 `data.flags` entry | ✅ closed | test_ut07_84_step5_flag_sets_data_and_flag_list (test file:578); P38 red. |
| M6 `_CHECK` ≤ 500 | ✅ closed | test_ut07_84_step5_rereads_sqlite_in_batches_of_500 (test file:595) seeds 501 rows; P33 red (`ToolInputError: too many ids`). |
| M7 misnamed spy, no `vectors.delete` spy | ✅ closed | test file:462 renames the spy to `expires` and adds a `vectors.delete` spy; the M7 probe goes red on `assert [(([],), {})] == []`. |

### Probe table
| # | Mutation | Result |
|---|----------|--------|
| M1 | old broad catch (`if exc.kind != "current"` → `if False`) | killed (test_ut07_84_current_naming_missing_build_fails) |
| P06 | `_YEAR` 365 d → 364 d | killed (test_ut07_84_yearly_review_once_per_item) |
| P07 | `_YEAR` 365 d → 366 d | killed (test_ut07_84_yearly_review_once_per_item) |
| P01 | `PAGE` 1000 → 999 | killed (test_ut07_84_repairs_backfills_and_requests_review) |
| P29 | per-step `ctx.heartbeat(...)` → `pass` | killed (test_ut07_84_repairs_backfills_and_requests_review) |
| P38 | step 5 `data["flags"]` without `"embedding_pending"` | killed (test_ut07_84_step5_flag_sets_data_and_flag_list) |
| P33 | `_CHECK` 500 → 501 | killed (test_ut07_84_step5_rereads_sqlite_in_batches_of_500) |
| M7 | resume loop `range(min(start, 4), …)` (a saved step 7 re-runs steps 5-7) | killed (test_ut07_84_rerun_of_finished_job_is_idempotent on the `vectors.delete` spy; also FT07-01 resume test) |

### Gates (re-run)
ruff check: all checks passed · ruff format --check: 975 files already formatted · mypy (project config): no issues in 356 source files · lint-imports: 13 kept, 0 broken · check_module_size: exit 0 · `pytest tests/unit/harness/memory tests/fault/harness -q -p no:logging`: 599 passed · coverage maintenance.py 100 % line / 100 % branch (127 stmts, 16 branches); _maintenance_vectors.py 100 % line / 97.7 % branch (43/44, partial 94->93).

### Builder note: test module outside mypy scope
Does not matter for this card. pyproject `files = ["herness", "tools"]` keeps tests out of the mypy gate, and no gate reads the test module's ignore codes. A direct `mypy` run on the test file reports 7 errors: six `call-overload` not covered by `type: ignore[index]` (lines 145, 158, 478, 520, 552, 588) and one `attr-defined` (line 408, `maintenance.ops`). Correction to the builder report: line 588 is new in this fix (it copies the existing pattern), so "not introduced here" is not quite right. It is still harmless and outside this card's scope. If tests are ever brought into mypy scope, the codes should be `call-overload`.

### New findings
Critical: none. Important: none. Minor: none.

### Assessment
**Verdict:** Approved
**Reasoning:** M1-M7 are all closed. The step-3 catch is narrowed to the real `kind="current"` case, and the fresh-install skip still works. Each previously surviving probe now fails a named test, and the fix causes no gate or coverage regression.
