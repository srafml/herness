# T07-22 report: memory_maintenance job handler (U07-96)

Status: DONE_WITH_CONCERNS (final commit 16dc3b3; worktree clean)
Worktree: D:\herness\.claude\worktrees\agent-a66f9894474cca5d8 (branch worktree-agent-a66f9894474cca5d8, base 5f2b7c0)

## Commits
- 1be031b wip(T07-22): maintenance handler, vector sibling, unit tests green (hooks passed, no skip)
- 16dc3b3 feat(memory): memory_maintenance job handler (T07-22)

## Files (lines / budget)
- herness/harness/memory/maintenance.py 199 / 260 (handler, MaintenanceDeps seam, steps 1-4 and 7, gauges, log)
- herness/harness/memory/_maintenance_vectors.py 170 / 180 (new private sibling: step 5 check_vectors, step 6 backfill; §2 row added)
- tests/unit/harness/memory/_maintenance_env.py (FakeCtx structural JobContext, make_env, seeds)
- tests/unit/harness/memory/test_memory_maintenance.py (24 tests)
- tests/fault/harness/test_memory_maintenance_fault.py (3 tests)
- docs/impl/07-memory.impl.md: §2 row for _maintenance_vectors.py; T07-22 spec note under U07-96; §8.1 rows memory.maintenance.templates_skipped (INFO) and memory.maintenance.backfill_stopped (WARNING)

## RED / GREEN
- RED: pytest tests/unit/harness/memory/test_memory_maintenance.py -> ImportError: cannot import name 'maintenance' from 'herness.harness.memory'
- GREEN: card tests 27 passed; `pytest tests/unit/harness/memory tests/fault/harness -q` 596 passed
- Coverage: maintenance.py 100% line / 100% branch (125 stmts, 14 branches); _maintenance_vectors.py 100% line / 43 of 44 branches (97.7%; partial 94->93 = row already pending inside the flag tx, a concurrent-write case)
- Mutation probes (all red, reverted): never flag stale vectors (8 failed); never clear pending (6 failed); skip SQLite re-check for vector ids (8 failed, TH07-13 orphan pin); drop "still active" re-check in yearly review tx (1 failed)

## Gates
ruff format/check clean; mypy 0 (356 files); lint-imports 13 kept 0 broken; check_module_size 0; check_type_ownership 0; wip commit ran all pre-commit hooks incl. pytest-unit (passed).

## Tests by ID
UT07-84 (tests/unit/harness/memory/test_memory_maintenance.py):
repairs_backfills_and_requests_review (full run: orphan deleted, stale status repaired, pending backfilled, yearly review, counts, saves 1..7, completed log);
model_change_reembeds_every_item; content_hash_mismatch_and_missing_vector_flagged; step5_pages_both_streams (PAGE=2);
steps_1_to_4_compose_the_units (expire, promote only runs done within 7 d, validate on fresh connection then closed, fts);
no_current_build_skips_step3; backfill_bound_and_heartbeat_every_256 (limit=5000 passed, beats at 256 and 257);
backfill_never_exceeds_5000; model_unavailable_stops_backfill_only (step 7 still runs); vector_upsert_failure_stops_backfill;
backfill_keeps_flag_when_content_changed; yearly_review_once_per_item (not due: young, inactive, recently reviewed);
yearly_review_rechecks_inside_the_transaction; gauges_written_with_record_metric_samples;
retryable_errors_propagate[StoreBusy, ModelUnavailable]; unconfigured_seam_is_config_error; registered_handler_runs;
yield_saves_step_and_resume_skips_done_steps; rerun_of_finished_job_is_idempotent; bad_saved_state_starts_over;
th07_13_step5_never_changes_sqlite; th07_13_orphan_rechecked_before_delete; written_items_stay_consistent.
FT07-01 (tests/fault/harness/test_memory_maintenance_fault.py):
sqlite_retry_and_embed_failure_then_backfill (fault_point sqlite.write StoreBusy x1 + embed failure -> embedding_pending -> backfilled);
kill_between_commit_and_vector_upsert (insert_system_item in caller tx, embed_after_commit never runs -> backfilled);
store_busy_in_backfill_resumes_idempotently (StoreBusy exhausted on the clear tx -> job fails at saved step 5, resume at 6 clears it, one vector row).

## Rulings applied
- Seam (U07-98 not on base): frozen MaintenanceDeps(lifecycle, procedural, vectors, embedder, open_current=warehouse.open_readonly) + configure_maintenance(deps|None) / maintenance_deps() -> ConfigError when unset; only module state is _SEAM. Spec note recorded.
- Registration: registry half tested (register_handler + resolve_handler + run_handler). Composition-root registration and configure_maintenance wiring carried over to T07-23.
- FT07-01 uses the card's fault harness (fault_env sqlite.write + raising fake embedder), not T05-27 loop_kill; resume test included.
- TH07-13 pins: step 5 never changes SQLite status/data or deletes rows; only vectors deleted, and only after a SQLite re-read; fine items untouched; backfill bounded at 5000.
- Budget: maintenance.py could not hold steps 5-6 in 260 after ruff format (335 lines); split into _maintenance_vectors.py with §2 row (budget 180) + spec note in the same commit, as allowed by the dispatch.

## Spec notes / readings (see T07-22 spec note under U07-96)
- Step 3 skipped with {"skipped": "no_current_build"} when no promoted CURRENT build (NotFoundError); connection closed after validate_templates.
- Step 5 re-reads rows in batches of 500 before acting; rejected and already-pending items not flagged; status tally from all_ids_status feeds herness_memory_items_total (all five statuses, 0 when absent).
- Step 6 chunks of 256 (one upsert + one clear tx per chunk, heartbeat after each); flag cleared only if content_hash unchanged; ModelUnavailable from embedder or LanceDB stops the step only.
- Step 7 one selection with limit 10,000; tx re-reads and skips rows no longer active/due; payload = §4.2 memory_write fields of the current row with flags ["yearly_review"].
- counts schema and bad-saved-state restart documented; should_yield checked after step 7 too.

## Deviations
- New private sibling module (ruled acceptable). No edits to procedural.py, lifecycle.py, or any other card's file. _write_steps.stored_flags imported (read-only use of T07-08's private sibling helper, same package) to keep flag handling identical to the writer.
- Tests import register_handler/resolve_handler/run_handler from herness.core.jobs.handlers (not re-exported by herness.core.jobs on base).

## Carry-overs
- T07-23: build MaintenanceDeps from MemoryStore collaborators, call configure_maintenance, register "memory_maintenance" in register_memory_components; worker bootstrap (T09-27) imports it.
- IT07-10 (desync LanceDB and SQLite, integration) is not in this card's Tests row; not built.

## Fix round 1 (review verdict Approved with minors; M1-M7)

Commit: 5be98d6 fix(memory): narrow CURRENT-missing skip and pin maintenance bounds (T07-22 fix 1)

| Finding | Change | Test | Mutant red evidence (applied in place, test run, file restored) |
|---|---|---|---|
| M1 broad `except NotFoundError` in step 3 | maintenance.py `_templates`: `except NotFoundError as exc:` re-raises unless `exc.kind == "current"`; spec note (3) of U07-96 in docs/impl/07-memory.impl.md now says `kind = "current"` skips and `kind = "build"` (CURRENT names a missing file) propagates | new `test_ut07_84_current_naming_missing_build_fails` (real `warehouse.open_readonly` on a tmp `DataLayout` whose CURRENT names a valid but absent build id; asserts `NotFoundError` kind/key = build/<id>, saved step 2) | old broad catch (`if False:` re-raise guard): `Failed: DID NOT RAISE NotFoundError` |
| M2 365-day cutoff unpinned | test only | `test_ut07_84_yearly_review_once_per_item` seeds the due rule at NOW-365 d and the not-due rule at NOW-364 d | P06 364 d: review list has the 364-d rule, red; P07 366 d: `assert [] == [due]`, red |
| M3 page size 1000 unpinned | test only | full-run `test_ut07_84_repairs_backfills_and_requests_review` spies `ops.maintenance_rows` and `env.vectors.list_ids`; asserts every `all_ids_status` limit and every `list_ids` limit == 1000 | P01 PAGE=999: `{999} == {1000}`, red |
| M4 per-step heartbeat unpinned | test only | same full-run test asserts the `memory_maintenance step N` notes == steps 1..7 | P29 per-step heartbeat removed: `[] == ['memory_maintenance step 1', ...]`, red |
| M5 step-5 `data.flags` entry unpinned | test only; `_only_step5` helper (also used by the TH07-13 test) | new `test_ut07_84_step5_flag_sets_data_and_flag_list` (step 5 only; a missing vector and a stale hash; asserts `embedding_pending is True` and exactly one `"embedding_pending"` in `data.flags`) | P38 flag not added to `data.flags`: `assert 0 == 1`, red |
| M6 `_CHECK` must stay <= get_memory_items C2 limit 500 | test only | new `test_ut07_84_step5_rereads_sqlite_in_batches_of_500` (step 5 only, 501 pending rows, no vectors; asserts re-read batch sizes [1, 500] and statuses {active: 501}) | P33 _CHECK=501: `ToolInputError: too many ids: get_memory_items`, red |
| M7 misnamed spy, no `vectors.delete` spy on step-7 resume | test only | `test_ut07_84_rerun_of_finished_job_is_idempotent`: spy renamed `expires`; new `deletes` spy on `env.vectors.delete` asserted empty on the saved-step-7 resume | (pinning a non-call; no review mutant) |

Gates: ruff format/check clean; mypy (maintenance.py, _maintenance_vectors.py) clean; lint-imports 13 kept 0 broken; check_module_size exit 0 (maintenance.py 201/260, _maintenance_vectors.py 170/180); `pytest tests/unit/harness/memory tests/fault/harness -q -p no:logging`: 599 passed; coverage maintenance.py 100 % line / 100 % branch, _maintenance_vectors.py 100 % line, 1 partial branch (94->93), 99 % combined.
Note: the test module is outside the project mypy scope (`files = ["herness", "tools"]`); a direct mypy run on it shows pre-existing `type: ignore[index]` code mismatches on untouched lines, not introduced here.
