# T03-21 Decide stages: independent review

Reviewer: verify agent · worktree agent-aa93fb574384e1302 · build commit b063ed1 (base 9c8f34a) · tree left clean (`git status` empty).

## Verdict: Needs fixes (0 Critical, 1 Important, 7 Minor)

Evidence I ran myself:
- `pytest tests/unit/enrich/test_decide_stage.py tests/fault/enrich/test_decide_stage_fault.py --cov=herness.enrich.decide_stage --cov-branch`: 19 passed. Coverage is 236 stmts with 0 missed and 34 branches with 1 partial (254->256), 99 % overall.
- `ruff check` and `ruff format --check` on the 4 files: clean. `mypy` (strict, project config) on `herness/enrich/decide_stage.py`: clean. Module is 387 lines (budget 390).
- Mutation run: 9 mutants, each loaded in place of the module under `.agent-tmp/`, which I removed afterwards.

| Mutant | Result |
|---|---|
| m1 drop the fingerprint in the `have` anti-join | killed (UT03-79) |
| m2 yield without flush | killed (3 tests) |
| m3 `flush_rows` without the `× questions` factor | **survives** (see M1) |
| m4 no teacher exclusion (`fresh = queue`) | killed (UT03-81 below-gate) |
| m5 deferred merge in reverse order | killed (3 tests incl. FT03-01) |
| m6 no item retry | killed |
| m7 LLM cap ignored | killed (UT03-82) |
| m8 pairs not capped | killed |
| m9 pair questions allowed in `build_inputs` | killed (2 tests) |

## Spec compliance per unit

| Unit | Result | Notes |
|---|---|---|
| U03-84 `build_inputs` | ✅ | The partition comes from `cache.dataset()`, filtered by decider and version. The anti-join matches on (hash, question, **current fingerprint**). `PAIR_QUESTIONS` are excluded. The bootstrap `opened_at >= $since` join is present. Output is ordered by `content_hash` and streamed with `fetchmany(chunk)` (2,000). Views are unregistered in `finally`. A DuckDB error becomes `SchemaViolation` naming the class only. It emits `herness_enrich_cache_hits_total{decider}` (§9 metric). |
| U03-85 `run_decide_primary` | ✅ | Skipped when there is no Laya or no Laya-primary question. `load()`/`unload()` run in `finally`. `flush_rows = 20 × call_batch × len(asked)`. Each chunk runs decide, writer.add(samples=None), count, heartbeat, then the yield check (flush, then `YieldRequested`). `ModelUnavailable` (and `ConfigError`, deviation 8) mark the stage `degraded` and log `enrich.stage.degraded` WARNING. `FatalError` propagates. |
| U03-86 `run_decide_escalate` | ✅ with I1 | Order is `resolve_frame`, then `escalation_queue(cap)`, then the capped INFO event, then the teacher-None return of queue + pairs. The teacher runs through a one-member `DeciderChain([teacher.name], gpu=…, resolve=…)` in chunks of 2,000. Lost chunks are deferred. AuthError/EgressBlocked stop sending and log the right ERROR events. Item errors are retried once in the next chunk. Writes go through `CacheWriter` (flush 2,000). Heartbeat and yield run per answered chunk. Records sent stay ≤ cap and pairs ≤ `decider_max_pairs`. The gap: "none propagate except FatalError" does not hold for `RateLimited` and other non-fatal `HernessError`s (I1). |
| U03-87 `run_llm_escalation` | ✅ | None returns 0. It takes the first `cap` items: records come first, so pairs count after records. Chunks are 500, cache flush is 2,000, and order is kept. ModelUnavailable/CircuitOpen stop the run with `enrich.decider.unavailable` WARNING and leave the rest as misses. Yield flushes, then raises `YieldRequested("reasoning")`. |

## Spec compliance per test row

| Row | Result | Notes |
|---|---|---|
| UT03-79 | ✅ | Proves only missing questions are asked, with chunk sizes [2,2,1]. Covers other-version rows, a stale fingerprint, pair-only question sets, the bootstrap filter, the ConfigError/SchemaViolation paths and view cleanup. |
| UT03-80 | ✅ | Proves Laya None gives skipped, and a yield after chunk 1 raises only after the flush (4 rows on disk, heartbeat recorded). The rerun resumes at record 3. Also covers the degraded path and the FatalError path with unload. |
| UT03-81 | ✅ | Real `resolve_frame`, `escalation_queue` and `DeciderChain`. ModelUnavailable on chunk 2 with cap 3: chunk 1 is cached, `inc_003` is deferred, and the capped event carries `queued`/`cap`. Also covers teacher None, retry-once, AuthError/EgressBlocked (parametrized), yield and the below-gate exclusion. |
| UT03-82 | ✅ | 30 deferred with cap 20 gives 20 answered, in the same order, with `samples=3`. Also covers the unavailable stop and the yield. |
| FT03-01 | ✅ | Uses the real registry point `decider.batch` nth 2 → `kill_service:openjev` through `kill_service_hook`. Chunk 1 is cached under openjev, the other 4 records go to the LLM in queue order, and the rerun sends nothing. No key is duplicated before or after `cache_maint.compact`. The test is not vacuous: without the fault, `llm.calls` would be empty and the test fails. |
| FT03-03 | ✅ | Uses the real `enrich.after_batch_write` nth 2 → `kill`, with `os.kill` patched to raise a BaseException. Exactly 2 × checkpoint rows survive, the rerun decides the remaining 60, rework is ≤ 1 checkpoint, and there are 100 unique keys. The test is not vacuous: with a wrong flush size the kill never fires and `pytest.raises` fails. It does not pin the `× questions` factor (M1). |

## Focus checks

1. **Store APIs.** All pass. Rows are written only through `cache.writer(...)` (`CacheWriter.add`/`flush`). Nothing writes parquet or ops SQL by hand in `herness/`; `write_part` appears only in test setup. Cache hits are skipped by the fingerprint-aware anti-join, and pairs never reach `build_inputs`.
2. **Restartability.** Pass. See FT03-01 and FT03-03 above; both fault points are in `faults.py`'s registry (line 38) and fire at `cache.py:260` and `deciders.py:46`.
3. **Degrade matrix.** Pass, except I1.
4. **No text in logs or errors.** Pass. Events carry counts, ids and codes. `SchemaViolation` carries the class only. UT03-81 asserts that no "ticket text" appears in the captured logs.
5. **Bounds.** All pass: 2,000 per chunk, 500 per LLM chunk, the night cap, the LLM cap, `decider_max_pairs`, and `flush_rows = 20 × call_batch × questions`.
6. **Yield.** Pass: heartbeat per chunk, then flush, then `YieldRequested` at chunk boundaries. No GPU lock is taken. Lost teacher chunks skip the heartbeat (M3).
7. **Deviation 3 (teacher re-ask exclusion).** Justified; recorded as ⚠️ W1 below, not a defect.
   - **Why it is needed.** `enrich_cand` holds only current-version, current-fingerprint rows. A teacher-primary question the teacher answered below the gate therefore stays `queue` (chain_after(teacher) = llm) every night. Re-sending it only writes another below-gate teacher row, so the item never reaches the LLM. That defeats design §5.7 step 4 and §6 ("the queue moves to the LLM decider").
   - **Spec support.** U03-80 already defines `exclude_deciders`, and this is its natural consumer.
   - **Cap.** `send` is a subset of the queue's records (≤ cap). `fresh` records outside `queue` are ignored.
   - **No loss.** Every queued (record, question) ends up either in `send` or in `parts`. Sent items that end up deferred are merged back.
   - **Order.** `_merge` ranks by queue position; mutant m5 is killed.
   - **Edge case (M4).** A queue record that falls outside `fresh`'s own top-cap (because `max_scoring` is recomputed) sends its non-teacher questions to the LLM instead of the teacher. Nothing is lost or over cap, but teacher capacity is used slightly less.
8. **Globals.** Pass. Test IDs are in names and first docstring lines. `pytestmark` is unit/fault. C901 and ruff are clean; the binding signatures carry `PLR0913` noqa with a reason. `EM101` is respected. 387/390 lines. Coverage is 100 % line and 97 % branch.

## ⚠️ Spec notes and conflicts

- W1 (deviation 3): record in impl 03 U03-86 step 1 that the teacher is sent only queued questions it has no current row for (`escalation_queue(exclude_deciders={teacher})`), and that the rest go straight to the deferred list. This matches design §5.7 step 4 intent.
- W2 (deviations 1, 2, 4–12): acceptable carry-overs or clarifications. Deviation 2 (`gpu` kwarg) and deviation 12 (`_Report` protocol) fall under the sub-controller rulings. Deviation 9 (capped event at cap 0) is flagged as M6.
- W3: the §9 log table fixes the `enrich.decider.unavailable` fields as `decider, error_class, deferred`. The chain swallows the exception class, so U03-86 cannot supply `error_class` without a chain change (see M5).

## Findings

### Critical
None.

### Important

**I1: non-fatal `HernessError`s escape `run_decide_escalate` and lose unflushed answers.**
- Where: `herness/enrich/decide_stage.py:293-297` (`_Sender.run`) and `:345-349`.
- Problem: `DeciderChain.decide` catches only `ModelUnavailable`/`CircuitOpen` (`herness/core/resilience/deciders.py:117`). `OpenJevDecider.decide` documents raising `RateLimited` when Retry-After is over the policy cap (`herness/enrich/deciders/openjev.py:149`). The chain's `retry_call` re-raises the last `RetryableError` once its policy stops, and a batch-level `RecoverableError` passes straight through. Such an error therefore propagates out of the stage. That breaks the U03-86 contract "Errors: none propagate except `FatalError`", and it discards up to 2,000 answered rows that are still unflushed in the writer.
- Fix: in `_Sender.run`, after the `AuthError`/`EgressBlocked` clause, add `except (RetryableError, RecoverableError) as exc:` and treat the chunk as lost: `deferred.update(batch)`, `_mark(..., f"{self.name}_unavailable")`, log WARNING with `error_class=type(exc).__name__`, then `continue`. Also wrap the send loop in `try/finally: writer.flush()` so a propagating `FatalError` keeps the rows already answered. Add a UT03-81 case where the teacher raises `RateLimited` on chunk 2.

### Minor

- **M1: FT03-03 does not pin the `× questions` factor.** `tests/fault/enrich/test_decide_stage_fault.py:122-123` uses a single Laya question, so mutant m3 (`flush_rows = 20 × call_batch`) survives. Fix: use two Laya-primary questions (`{"q_bool": "laya", "q_score": "laya"}`) with `checkpoint = CHECKPOINT_CALLS * 1 * 2`.
- **M2: `run_llm_escalation` has no `finally` flush.** At `herness/enrich/decide_stage.py:373-385`, a propagating `AuthError`/`EgressBlocked`/`FatalError` from `llm.decide` discards the unflushed rows (≤ 2,000). Fix: move `writer.flush()` into a `finally`.
- **M3: no heartbeat for lost teacher chunks.** At `herness/enrich/decide_stage.py:298-302` the `continue` skips `_checkpoint`, so chunks lost to ModelUnavailable/CircuitOpen get neither a heartbeat nor a yield check. The spec says heartbeat and yield check per chunk, and each lost chunk can spend the whole retry policy first. Fix: call `self.ctx.heartbeat("decide-escalate")` (or `_checkpoint`) before `continue`.
- **M4: `fresh` is capped separately (deviation 3 edge).** At `herness/enrich/decide_stage.py:342` a queue record ranked past `fresh`'s own cap loses its teacher questions to the LLM. Fix: derive the exclusion over the same record set, for example by filtering `fresh` from an `escalation_queue(max_records=len(all queued))` restricted to the queue's record keys, or by a record-keyed exclude query. Otherwise document it in the W1 spec note.
- **M5: log field names drift from the §9 table.** `enrich.decider.unavailable` uses `items=` (`:301`) and `left=` (`:380`) instead of `deferred=`, and has no `error_class` in U03-86. Fix: rename to `deferred=` and add `error_class` where it is known (the I1 fix supplies it for non-chain errors).
- **M6: `escalation_capped` fires with nothing queued.** At `herness/enrich/decide_stage.py:336` it fires for cap 0 even when nothing is queued. Fix: `if queue and len(queue) >= cap:` or `if cap and len(queue) >= cap`.
- **M7: pairs over the cap are dropped silently.** At `herness/enrich/decide_stage.py:338` pairs beyond `decider_max_pairs` are neither logged nor counted. Fix: log `enrich.decide.escalation_capped`-style INFO (`pairs`, `cap`) when truncating.

## Assessment

**Task quality:** Needs fixes.
**Reasoning:** The stages match U03-84..87 and the tests are strong: 8 of 9 mutants are killed, and FT03-01 and FT03-03 really exercise the registry fault points. Only I1 blocks: a `RateLimited` (or other non-fatal) error from the teacher escapes the stage, which breaks the U03-86 error contract and discards unflushed answers. It is a small, local fix.
