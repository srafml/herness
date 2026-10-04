# T07-09 Lifecycle — build report

Worktree: D:\herness\.claude\worktrees\agent-a80385e5c02781dc6 (branch worktree-agent-a80385e5c02781dc6, base e41d62c)
Status: DONE — commit 7dafa71 `feat(memory): T07-09 memory lifecycle` (all pre-commit hooks passed incl. pytest-unit)

## Implemented
- `herness/harness/memory/lifecycle.py` — `MemoryLifecycle(cfg, *, conn_factory, vectors, writer, redactor)`:
  - `approve` (U07-51): input checks → ToolInputError; load (MemoryNotFound); idempotent repeat for
    active + approved_by; else PolicyViolation("approve.not_pending") unless pending; conf =
    given or max(conf, APPROVAL_FLOOR); ONE run_write that re-checks the status, writes
    approved_by/approved_at/approval_note (redacted), supersedes only active ids listed in
    data.conflicts_with (≤10, superseded_by + expired_reason "superseded"), creates the derived
    weight_change / mapping_suggestion review item via create_review_item(conn=conn) for
    user_correction/business_rule, and decides the linked review item via
    decide_review_item(item_id, "approved", decided_by=user_ref, note=…, now=…, conn=conn).
    After commit: set_status active / expired mirror; log memory.item.approved (ids + kind only).
  - `reject` (U07-52): same shape, rejected_by/rejected_at/rejection_note (redacted, stripped,
    1–1000 chars); review item rejected in the same transaction; mirror "rejected"; log.
  - `expire` (U07-54): batches of 1000 via maintenance_rows("expirable", conn=conn) inside each
    run_write (select + update together), paged by `after`; mirror per batch; log
    memory.item.expired (count, reason "ttl"); returns the total. Deterministic for `now`.
  - `expire_item` (U07-55), `record_use` (U07-56, one run_write → touch_memory_items).
  - LanceDB mirror failure (ModelUnavailable) → `memory.vector.sync_failed` WARNING (op, count), call succeeds.
  - No purge (T07-26). `__init__.py` untouched. No pyproject / import-linter change needed.
- Tests: `tests/unit/harness/memory/test_memory_lifecycle.py` (UT07-32, -33, -34, -36, -37),
  `tests/security/test_st07_lifecycle.py` (ST07-04, ST07-12 with the real audit file, ST07-17),
  helper `tests/unit/harness/memory/_lifecycle_env.py` (FakeVectors, make_lifecycle, seed_item).

## Evidence
- RED: `uv run pytest tests/unit/harness/memory/test_memory_lifecycle.py` →
  `ModuleNotFoundError: No module named 'herness.harness.memory.lifecycle'` (collection error).
- GREEN: card tests 51 passed; lifecycle.py coverage 100 % line / 100 % branch (181 stmts, 50 branches).
- tests/unit/harness/memory + tests/security/test_st07_*.py + test_tools_recording.py: 649 passed.
- ruff format/check clean; mypy (project config, 334 files) clean; lint-imports 13 kept 0 broken;
  check_module_size exit 0; check_type_ownership exit 0.
- lifecycle.py: 271 lines (budget 340; ruling ≤ ~270 to leave room for purge).

## Spec notes (also recorded in docs/impl/07-memory.impl.md after U07-56, "T07-09 spec note")
1. The note passed to decide_review_item is the stripped, redacted note (same as data.approval_note /
   rejection_note), not the raw note (spec text says `note=note`).
2. "review item still pending": decide_review_item is called and ReviewItemConflict / NotFoundError
   (raised before any write) are ignored, as U07-57 does; impl 02 has no conn-aware read by id.
3. expire_item leaves expired AND rejected items unchanged; a non-existent superseded_by → ToolInputError.
4. expire selects inside the run_write (no overwrite of a concurrently changed status).
5. record_use: 200-id limit checked before dedupe; empty list → no write; DEBUG memory.item.used (run_id, count).
6. expire_item does not log memory.item.expired (its reason is caller free text, never logged).
7. Only the first 10 well-formed conflicts_with ids are read; the item never supersedes itself.
- `cfg` and `writer` are stored but unused by these units (constructor fixed by §3.10; purge/T07-26 may use them).

## Concerns
- none blocking.

## Process note
- The first `wip(T07-09)` checkpoint commit did not land (its hooks passed but HEAD did not move; most likely a race with files I edited while the 20-min hook run was going). The work went in as the single final commit 7dafa71.

## Fix round 1 (review D:\herness\.superpowers\sdd\program\briefs\T07-09-review.md)
- I-1: `memory.review.stale` (INFO, memory_id only) is now logged in `_done` before PolicyViolation("approve/reject.not_pending") (pre-check and in-tx re-check) and in both in-transaction concurrent-winner paths. Exact event asserted (`_stale`) in UT07-32 non-pending (x4), UT07-32 in-tx race, UT07-32/UT07-34 concurrent winner, UT07-34 non-pending.
- I-2: T07-09 spec note item (9): ST07 lifecycle tests live in tests/security/test_st07_lifecycle.py (marker integration, w21 precedent) instead of §11.5 tests/integration/memory/security/.
- M-2: new tests: exact key set of memory.item.approved/.rejected (event, log_level, component, memory_id, kind, review_item_id, derived_review_item_id); self-listed conflict never expired; eleven listed conflicts → only first ten expire; failing `shared.audit` rolls back the whole approval (item pending, no approved_by, no derived item, conflict still active, review item pending, no mirror) — kills a widened `suppress`; expire_item leaves a rejected item unchanged. The self-supersede guard was redundant (the approved item is still pending_approval when conflicts are read, so it is never "active") and was removed; the self-listed test pins the behaviour.
- M-3: `memory.item.used` DEBUG dropped; `run_id` is neither stored nor logged (`del run_id`; no public run-id pattern exists outside private `_RUN_ID_RE`s). Spec note item (5) updated; UT07-37 test asserts the run_id text is not logged.
- M-1: done (cheap): ST07-17 now seeds through the real `MemoryWriter.propose` (embed overrides at cosine 0.85, shared entity) so `conflicts_with` in the review payload is the production one; an unlisted equally-close active item about another entity stays active. The listed-but-not-active case moved to a second ST07-17 function.
- M-4: no change.
- Evidence: card tests 57 passed; lifecycle.py 184 stmts / 50 branches, 100 % line, 100 % branch; tests/unit/harness/memory + tests/security/test_st07_*.py: 536 passed; ruff format/check clean (repo), mypy 334 files clean, lint-imports 13 kept, check_module_size 0, check_type_ownership 0.
- lifecycle.py: 276 lines (≤ ~280).
- Committed: b988694 fix(memory): T07-09 review round 1 (all pre-commit hooks passed incl. pytest-unit).
