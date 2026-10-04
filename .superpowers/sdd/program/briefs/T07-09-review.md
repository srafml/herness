# T07-09 Lifecycle — verify review

Worktree: D:\herness\.claude\worktrees\agent-a80385e5c02781dc6 (base e41d62c, head 7dafa71). Read-only verification; tree left clean.

## Evidence (re-run by verifier)
- `PYTHONUTF8=1 uv run pytest tests/unit/harness/memory/test_memory_lifecycle.py tests/security/test_st07_lifecycle.py -q -p no:logging --cov=herness.harness.memory.lifecycle --cov-branch` → 51 passed; lifecycle.py 181 stmts / 50 branches, 100 % line, 100 % branch.
- ruff check + ruff format --check (4 touched py files): clean. mypy lifecycle.py: clean; project mypy: 334 files clean. lint-imports: 13 kept, 0 broken. check_module_size: exit 0 (lifecycle.py 271/340).

## Spec per unit
| Unit | Result | Notes |
|---|---|---|
| U07-51 approve | ✅ (⚠️ log) | Preconditions → ToolInputError (memory_id, 32-hex user_ref, confidence [0,1] incl. NaN/bool, note ≤500). Load → MemoryNotFound; idempotent repeat (active + approved_by); else PolicyViolation("approve.not_pending"). Floor `max(conf, APPROVAL_FLOOR)`. ONE `run_write` (lifecycle.py:152-166) re-checks status (:154), writes approved_by/at/redacted note, supersedes only listed+active ids ≤10 (:93-104), derives weight_change/mapping_suggestion item via `create_review_item(conn=conn)` (:107-117), decides the review item with `decide_review_item(..., decided_by=user_ref, note, now, conn=conn)` (:120-125). Mirror after commit; sync failure → `memory.vector.sync_failed` WARNING (:257-262). Missing `memory.review.stale` (see I-1). |
| U07-52 reject | ✅ (⚠️ log) | note 1–1000 after strip, redacted; one run_write with in-tx re-check (:184-194); review item rejected in the same tx; mirror "rejected"; log. Missing `memory.review.stale` (I-1). |
| U07-54 expire | ✅ | `maintenance_rows("expirable", conn=conn)` inside each run_write, batch 1000, paged by `after`; `expires_at <= :now` (ops `_memory_rows.py:80-81`) so the boundary is included; deterministic for injected `now`; idempotent (second sweep 0). Logs count + "ttl" only. |
| U07-55 expire_item | ✅ | reason 1–200, superseded_by MEMORY_ID_RE + exists (in tx) → ToolInputError; expired/rejected unchanged; one run_write; mirror. |
| U07-56 record_use | ✅ | ≤200 ids, each MEMORY_ID_RE, str rejected; dedupe; one run_write → `touch_memory_items(..., conn=conn)`; unknown/non-live ignored; once per id. |
| U07-53 / U07-57 | n/a | removed (R-33) / out of scope (T07-26) — correctly not built. |

## Test rows
| Row | Result | Notes |
|---|---|---|
| UT07-32 | ✅ | floor 0.8, explicit conf, missing → MemoryNotFound(kind, ident), idempotent repeat, non-pending ×4, in-tx re-check race, concurrent winner, decided review item left alone, dangling review id, sync failure, 9 invalid inputs. |
| UT07-33 | ✅ | derived payload asserted exactly; only the active listed conflict expired; mapping_suggestion; no-derive cases; malformed conflicts. |
| UT07-34 | ✅ | rejected + review item rejected by the user, redacted note, repeat / non-pending / missing, race, invalid inputs. |
| UT07-36 | ✅ | batch size patched to 2 → batches [2,2]; `expires_at == now` included; +1 s excluded; rejected untouched; idempotent; expire_item reason/superseded_by/errors. |
| UT07-37 | ✅ | duplicates, unknown, rejected; counted once; 201 ids / malformed / str → ToolInputError. |
| ST07-04 | ✅ | full ops-table snapshot: only memory_item + review_item change, +1 review item (weight_change, pending); duckdb.connect patched to fail (no warehouse/score write). |
| ST07-12 | ✅ | real audit file: `review_decision` lines match (status, decided_by = actor) for approve and reject; approved_by / rejected_by; note text absent from audit. |
| ST07-17 | ✅ (⚠️) | only listed active ids expire with superseded_by/expired_reason; unlisted active and listed pending untouched; vector mirror excludes unlisted. "payload lists them" asserts the payload built by the test helper, not one produced by MemoryWriter (M-1). |

All test functions carry the ID in name + docstring; pytestmark `unit` (UT) / `integration` (ST, as §11.5).

## Builder spec notes (docs/impl/07-memory.impl.md:1339)
1. Redacted note to decide_review_item — ✅ correct and stricter (review_item.note never holds more PII than the memory row; audit carries note_len only). Within decide's 2000-char note cap.
2. Ignore ReviewItemConflict / NotFoundError — ✅ correct: both are raised by `decide_review_item` (store/ops/shared.py:289-301) before any write, so the outer transaction stays intact; equivalent to "only when still pending"; a memory_write item can only be decided elsewhere by the system purge, which deletes the memory row. Audit and other errors still propagate and roll back.
3. expire_item leaves expired AND rejected unchanged — ✅ reasonable reading (spec names only expired); untested (M17 survived, M-2).
4. Select inside run_write — ✅ stricter than spec; no stale overwrite; concurrent runs idempotent.
5. record_use limit before dedupe; empty → no write; DEBUG `memory.item.used` — ✅, but the new event is not added to the §8.1 log-event table (M-3).
6. expire_item does not log — ✅.
7. First 10 well-formed conflicts; never self — ✅ (untested, M-2).
- **Missing:** no spec note records that ST07-04/12/17 live in `tests/security/test_st07_lifecycle.py` instead of §11.5 `tests/integration/memory/security/` (controller accepted per w21 precedent but asked for a §11.5 note) — I-2.

## Findings

### Critical
- none.

### Important
- **I-1 `memory.review.stale` INFO not emitted.** Spec §6 error table (07-memory.impl.md:2395) and §8.1 (:2533) require `memory.review.stale` (field memory_id) when approve/reject hits an item that is no longer pending. herness/harness/memory/lifecycle.py:69-76 (`_done`) raises PolicyViolation without logging; the in-tx concurrent-winner path (:154-155, :186-187) is also silent. Fix: log `memory.review.stale` (memory_id only) before raising in `_done` (or at each raise site) and assert it in the UT07-32/34 non-pending tests.
- **I-2 §11.5 test-location spec note missing.** Add to the T07-09 spec note (or a §11.5 note) that the ST07 lifecycle tests are at `tests/security/test_st07_lifecycle.py` (marker `integration`, w21 precedent) rather than `tests/integration/memory/security/`.

### Minor
- **M-1** tests/security/test_st07_lifecycle.py:110-117 — ST07-17 "payload lists them" asserts the payload that `_lifecycle_env.seed_item` (tests/unit/harness/memory/_lifecycle_env.py:96-98) builds itself; seeding through the real MemoryWriter (`_write_steps.py:244` puts conflicts_with in the payload) would assert the production path.
- **M-2 untested branches (mutants survived):** log-field leakage (adding a note field to `memory.item.approved/rejected` survives — assert the exact event key set), self-supersede guard (lifecycle.py:99), rejected-as-terminal in expire_item (:48), broadened `suppress(Exception)` around decide (:124 — an audit failure must roll back the approval; add a test that a failing `shared.audit` leaves the item pending), conflicts cap of 10 (:98).
- **M-3** `memory.item.used` (DEBUG; run_id, count) is a new event not listed in §8.1 (only in the spec note); `run_id` is logged as unvalidated caller text (lifecycle.py:251) — validate against the run-id pattern or drop it.
- **M-4** the approve note limit is checked on the stripped text (lifecycle.py:65), so a >500-char raw note with surrounding whitespace passes; harmless, note only.

## Mutation probes (23; source restored to the original bytes after each; tree clean)
| # | Mutant | Result |
|---|---|---|
| M1 | drop approve in-tx status re-check | KILLED (2) |
| M2 | drop reject in-tx status re-check | KILLED (2) |
| M3 | supersede listed non-active ids | KILLED (2) |
| M4 | supersede every active item (unlisted) | KILLED (2) |
| M5 | skip decide_review_item | KILLED (7) |
| M6 | decided_by="system" | KILLED (5) |
| M7 | approve passes no note to decide | KILLED (1) |
| M8 | approve decides "rejected" | KILLED (2) |
| M9 | expiry `<=` → `<` (ops SQL) | KILLED (1) |
| M10 | mirror ModelUnavailable not caught | KILLED (2) |
| M11 | floor max → min | KILLED (2) |
| M12 | derived review item without conn | KILLED (3) |
| M13 | record_use without dedupe | SURVIVED — equivalent (SQL `IN` counts once) |
| M14 | record_use limit 201 | SURVIVED — ops `TOUCH_IDS_MAX=200` backstops; equivalent at the boundary |
| M15 | note added to approved/rejected log | SURVIVED (M-2) |
| M16 | self-supersede allowed | SURVIVED (M-2) |
| M17 | rejected not terminal in expire_item | SURVIVED (M-2) |
| M18 | suppress(Exception) around decide | SURVIVED (M-2) |
| M19 | skip supersede | KILLED (2) |
| M20 | skip derive | KILLED (3) |
| M21 | reject note min 1 → 0 | KILLED (1) |
| M22 | conflicts cap removed | SURVIVED (M-2) |
| M23 | expire batch without update | KILLED (timeout) |

Score: 15 killed / 23; 2 equivalent; 6 real survivors, none on the threat-critical paths (re-check, listed-only supersession, decide/decided_by, expiry boundary).

## Verdict
**Needs fixes** — I-1 (`memory.review.stale` log) and I-2 (§11.5 location spec note); both small. Minor items recommended in the same fix pass (especially the M-2 log-key and audit-rollback tests). Core logic, transactions, the R-33 decision path and threats TH07-04/12/17 are correct.

## Re-review round 1 (base 7dafa71 → head b988694)

Evidence: card tests 57 passed; lifecycle.py 184 stmts / 50 branches, 100 % line + branch. ruff check / format clean; project mypy 334 files clean; check_module_size exit 0 (lifecycle.py 276/340). Tree left clean (probe mutations restored to the original bytes).

| Finding | Status | Evidence |
|---|---|---|
| I-1 `memory.review.stale` | ✅ closed | Logged (INFO, memory_id only) in `_done` before the PolicyViolation (lifecycle.py:75) and on both in-tx concurrent-winner returns (:156, :189). It is not logged for the idempotent pre-check repeat, which is correct. `_stale()` asserts the exact event dict in the non-pending ×4, in-tx race, reject non-pending and both concurrent-winner tests. |
| I-2 §11.5 location note | ✅ closed | Spec note (9) at 07-memory.impl.md:1339 records `tests/security/test_st07_lifecycle.py` (marker `integration`, as the T07-08 tests in `test_st07_write.py`) instead of `tests/integration/memory/security/`. |
| M-1 ST07-17 payload | ✅ closed | ST07-17 now proposes through the real MemoryWriter. A close same-entity item is listed (`payload["conflicts_with"] == [listed]`, also in data). An equally close other-entity item is unlisted and stays active. Approval expires only the listed item. Listed but non-active ids are covered by a second ST07-17 test. |
| M-2 untested branches | ✅ closed | New tests cover the exact log key set on approved/rejected, self-listed conflict, the 10-conflict cap, audit-failure rollback (status, conflicts, derived item and review item all unchanged; FatalError) and rejected-as-terminal in expire_item. |
| M-3 `memory.item.used` / run_id | ✅ closed | The event was removed rather than added to §8.1: `del run_id`, nothing stored or logged. Spec note (5) was updated, and UT07-37 asserts the run_id is absent from the logs. |
| M-4 | parked | as agreed. |

**Self-supersede guard removal — confirmed redundant.** Inside the `run_write`, `_done(cur, …)` returns False only for `pending_approval`. `_supersede` runs before `update_memory_item` activates the item (lifecycle.py:160-165). When the approving item lists itself, the re-read row is therefore still `pending_approval` and fails the `status == "active"` test. `test_ut07_33_self_listed_conflict_is_not_superseded` pins this: probe R16b (supersede moved after self-activation) and R16a (pending also superseded) are both killed.

### Mutation probes, round 1
| # | Mutant | Result |
|---|---|---|
| R13 | record_use without dedupe | SURVIVED — equivalent (SQL `IN`) |
| R14 | record_use limit 201 | SURVIVED — equivalent (ops TOUCH_IDS_MAX=200 backstop) |
| R15 | note added to approved/rejected log | KILLED (2) |
| R16a | pending conflicts also superseded | KILLED (2) |
| R16b | supersede after self-activation | KILLED (1) |
| R17 | rejected not terminal in expire_item | KILLED (1) |
| R18 | suppress(Exception) around decide | KILLED (1) |
| R22 | conflicts cap removed | KILLED (1) |
| R24 | no stale log in `_done` | KILLED (6) |
| R25 | no stale log, concurrent approve | KILLED (1) |
| R26 | no stale log, concurrent reject | KILLED (1) |
| R27 | run_id logged in record_use | KILLED (1) |

All 4 real round-0 survivors that still apply are now killed (the self-supersede mutant became R16a/R16b after the guard was removed). Only the 2 equivalent mutants survive.

### Verdict round 1
**Approved.** No remaining findings except the parked M-4.
