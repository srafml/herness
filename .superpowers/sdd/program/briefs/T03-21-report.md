# T03-21 Decide stages — build report

Status: DONE_WITH_CONCERNS (deviations below are spec-note candidates)
Worktree: D:\herness\.claude\worktrees\agent-aa93fb574384e1302 (branch worktree-agent-aa93fb574384e1302, base 9c8f34a)
Commit: b063ed1 feat(enrich): decide-primary, decide-escalate and LLM escalation stages (T03-21) (all pre-commit hooks incl. pytest-unit passed)

## Built
- `herness/enrich/decide_stage.py` (387 lines / budget 390, ENG 400):
  - `ResolveArgs` (frozen, slotted dataclass; ruling 1): cfg, deciders, labels, calibration, primaries, versions, now.
  - `build_inputs` (U03-84): registers the (decider, version) partition of `cache.dataset()` as `_bi_have(content_hash, question, question_fingerprint)` and the asked non-pair questions as `_bi_questions`; anti-join on (hash, question, current fingerprint); `bootstrap` joins core.<entity>.opened_at >= $since; ordered by content_hash; streams `fetchmany(chunk)`; views unregistered in `finally`; DuckDB errors -> SchemaViolation naming the class only. Emits `herness_enrich_cache_hits_total{decider}` (one COUNT query).
  - `run_decide_primary` (U03-85): skip (no Laya / no Laya-primary question); `flush_rows = 20 x call_batch x questions`; per chunk decide -> writer.add(samples=None) -> count -> heartbeat -> yield check (flush, then `YieldRequested("decide-primary")`); ConfigError/ModelUnavailable -> `degraded` (`laya_degraded`, `enrich.stage.degraded` WARNING); FatalError propagates; Laya unloaded in `finally`.
  - `run_decide_escalate` (U03-86): resolve_frame; escalation_queue(cap); capped INFO event; teacher None -> queue + pairs deferred; else one-member `DeciderChain([teacher.name], gpu=gpu or gpu_state(), resolve=lambda _: teacher)`; chunks of 2,000; lost chunks (chain returns ([], rest)) deferred, `<name>_unavailable`, `enrich.decider.unavailable` WARNING; AuthError/EgressBlocked stop sending, all remaining deferred, ERROR log; item errors retried once in the next chunk, then deferred (`failed`); cache writes every 2,000 rows; heartbeat + yield per chunk; returns records (priority order, merged per record) then pairs.
  - `run_llm_escalation` (U03-87): None -> 0; first `cap` items; chunks of 500; writer flush 2,000 rows, `samples=llm.samples`; ModelUnavailable/CircuitOpen -> stop, `enrich.decider.unavailable` WARNING, `llm_unavailable`; yield -> flush then `YieldRequested("reasoning")`.
  - No GPU lock, no service start, no register_deciders (ruling 3). Logs carry counts/ids/codes only.
- Tests: `tests/unit/enrich/_decide_stage_support.py` (ScriptedDecider, FakeGpu, Report, warehouse helpers, `decide_env` fixture: full test config with `models.deciders.laya.call_batch=1`, migrated ops store bound as resilience backend, test redactor), `tests/unit/enrich/test_decide_stage.py` (17 tests, marker unit), `tests/fault/enrich/test_decide_stage_fault.py` (2 tests, marker fault).

## Tests
- UT03-79 (3), UT03-80 (4), UT03-81 (7 incl. parametrized auth/egress), UT03-82 (3), FT03-01 (1), FT03-03 (1): 19 passed.
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich tests/fault/enrich -q -p no:logging`: 831 passed, 1 skipped (symlink privilege).
- `--require-test-ids` on the card files: 19 passed.
- Coverage decide_stage.py: 236 stmts 0 missed, 34 branches 1 partial -> 99 % (line 100 %, branch 97 %).
- Gates: ruff check/format clean, mypy (293 files) clean, lint-imports 13 kept, check_type_ownership 0, check_module_size 0.
- FT03-01: fault plan `decider.batch` nth 2 `kill_service:openjev` -> kill_service_hook stops the stub teacher, FakeGpu reports openjev unhealthy; chunk 1 (2 records) cached under openjev, 4 records deferred and answered by run_llm_escalation in queue order; rerun sends nothing; no duplicate (decider, hash, question) keys before and after `cache_maint.compact`.
- FT03-03: fault plan `enrich.after_batch_write` nth 2 `kill` with os.kill patched to raise a BaseException; checkpoint = 20 rows (call_batch 1 x 1 question), chunk 7; 40 rows survive; rerun decides the remaining 60; rework 2 <= 1 checkpoint; 100 unique keys.

## Deviations from the literal spec (spec-note candidates)
1. `ResolveArgs` also carries `deciders: DecidersSettings` (resolve_frame's own T03-19 deviation; R-76 split).
2. `run_decide_escalate` takes an extra keyword `gpu: GpuStateReader | None = None` (None -> `gpu_state()`), for test injection (ruling 3).
3. Teacher re-ask: the spec step 1 calls `escalation_queue(max_records=cap)` only. A teacher-primary question the teacher answered below the gate is `queue` again every run; sending it again returns the same answer, which is cached, not deferred, so it would never reach the LLM. The stage therefore also calls `escalation_queue(max_records=cap, exclude_deciders={teacher.name})` and sends the teacher only questions it has no current row for; the rest go straight to the deferred list (merged per record, queue order). Edge: the exclusion query recomputes `max_scoring` on the remaining questions, so a record near the cap may rank past it there; its questions then go to the LLM instead of the teacher (never lost, never over the cap).
4. Pairs are truncated to `cfg.change_link.decider_max_pairs` defensively (TH03-09); the caller is expected to pass <= 30,000 already.
5. `call_batch` comes from `get_config().models.deciders.laya.call_batch` (LayaDecider exposes no public call_batch; same pattern as the embed stage's batch_size).
6. Report semantics (StageReport not yet built): notes `laya_degraded`, `no_work`, `teacher_unavailable` (status `skipped` when teacher is None), `<teacher>_unavailable`, `<teacher>_auth`, `<teacher>_blocked` (status `degraded`), `llm_unavailable`. `escalated` = queue records (not pairs) in chunks the teacher answered, counted once; `failed` = items deferred after their second item error (escalate) or item errors (Laya, LLM); `decided` += answered items (Laya, LLM).
7. Cache `samples`: Laya None (spec), teacher None (Decider protocol has no samples), LLM `llm.samples` (LlmDecider documents it as the value recorded with its outputs).
8. `ConfigError` from `laya.load()` also degrades the stage (F03-04 step 2), not only ModelUnavailable. Laya is unloaded in `finally` (also on yield / FatalError).
9. `enrich.decide.escalation_capped` fires when `len(queue) >= cap` (so also for cap 0).
10. Chunk sizes are module constants (`DECIDE_CHUNK` 2,000, `LLM_CHUNK` 500, `WRITE_ROWS` 2,000, `CHECKPOINT_CALLS` 20); tests monkeypatch them.
11. `build_inputs` bootstrap uses `core.<entity>.opened_at` (as resolve_decisions.sql), not the embed stage's change coalesce.
12. Carry-over: `report` typed by private `_Report` protocol (status: str, note, decided, escalated, failed) until StageReport (U03-142, T03-28); T03-28 retypes.

## Concerns
- TDD order: the module draft preceded the tests (no captured RED run); all card tests were then written against the spec rows and pass.
- The first `wip(T03-21)` checkpoint commit did not land (a pre-commit hook failed; the tail of the output only showed the passing unit suite, and the working file was edited during the 12-minute hook run). The final commit carries everything.
- Each commit runs the full unit suite in the pre-commit hook (~12 min on this host).
- The full `-m "(unit or integration) and not slow"` run was not repeated separately (per dispatch: card tests + enrich unit/fault only; the commit hook runs `-m unit`).

## Fix round 1 (review T03-21-review.md: 1 Important, 7 Minor)
- I1: `_Sender.run` now also catches `RetryableError`/`RecoverableError` per chunk, after the AuthError/EgressBlocked clause. Those two and every other FatalError still propagate. RateLimited past its policy, and any other non-fatal HernessError, loses only its chunk: the chunk is deferred, the note is `<name>_unavailable`, and `enrich.decider.unavailable` WARNING carries `error_class` and `deferred`. `writer.flush()` in `run_decide_escalate` is in a `finally`, so a propagating FatalError keeps the answers already received. Tests: `test_ut03_81_rate_limited_chunk_is_deferred`, `test_ut03_81_fatal_propagates_after_flushing`.
- M1: FT03-03 uses two Laya questions (q_bool, q_score). With checkpoint = 20 x 1 x 2 = 40 rows, the mutant without "x questions" leaves 40 rows instead of 80 and fails.
- M2: `run_llm_escalation` flushes in a `finally`. Test: `test_ut03_82_fatal_propagates_after_flushing` (AuthError on chunk 2, 10 rows kept).
- M3: lost teacher chunks now run `_checkpoint` (heartbeat and yield check). Asserted in the RateLimited test (3 heartbeats for 3 chunks).
- M5: `enrich.decider.unavailable` fields are now `decider, error_class, deferred` in both stages. For chunks lost inside the chain, `error_class` is None, because the chain swallows the class and logs `resilience.decider.fallback` itself (W3).
- M6: `escalation_capped` is logged only when `queue and len(queue) >= cap`. Test: `test_ut03_81_cap_zero_empty_queue_not_capped`.
- M7: pairs over `decider_max_pairs` log `enrich.decide.pairs_capped` INFO (`pairs`, `cap`), asserted in the no-teacher test.
- M4: parked (spec note W1).
- To stay in budget I removed the four `# --- U03-8x ---` section dividers. decide_stage.py is 389/390 lines.
- Card tests: 23 passed. Coverage of decide_stage.py: 244 statements, 0 missed; 36 branches, 1 partial (99 %). `pytest tests/unit/enrich tests/fault/enrich`: 835 passed, 1 skipped. ruff, mypy, lint-imports, type-ownership and module-size are clean.
- Fix commit: d548e6f fix(enrich): decide stages defer non-fatal teacher errors and flush in finally (T03-21); all hooks incl. pytest-unit passed.
