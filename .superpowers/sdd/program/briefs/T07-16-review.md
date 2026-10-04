# T07-16 verify review — Prior context and decisions (U07-81, U07-82)

Worktree: agent-a2f1d0138d760121f · base 5f2b7c0 · head 0a4e2cd · verifier run 2026-10-03

**Verdict: Approved** (0 Critical, 0 Important, 6 Minor)

### Spec Compliance
- ✅ U07-81 `prior_context` — steps 1-8 implemented as specified (herness/harness/memory/episodic.py:174-206). Scope = recs of up to `prior_runs` runs of the caller's `run_kind` excluding `run_ctx.run_id` (SQL `RECENT_RUNS` filters `kind = ? AND run_id <> ?`) ∪ `accepted_since(now − lookback)` (any run kind, as the spec says), dedup by rec_id, newest 100. Tally, 4-group stable order, one `wrap_untrusted("memory", None, …)` with CONTEXT_NOTE first, tally record, known-only attributes via `escape_attr`, body `escape_content(render_marker_values(...))`, drop from the end, `memory_ids` only for rendered rec_ids (episodic.py:171, 205).
- ✅ U07-82 `decide` — preconditions → ToolInputError (episodic.py:209-224); unknown rec → MemoryNotFound("recommendation", rec_id) (:262-264); one `run_write` with `insert_decision` (redacted, stripped reason; `decided_by=user_ref`) + `insert_system_item(decision_note, key_hash=keyed_hash("decision_note:<rec_id>:<decided_at text>"), conn)` with human/dashboard provenance (:268-287); embed after commit (:288); two `outcome_measure` jobs only when accepted AND expected_metric, gpu "none", priority None (→ per-kind default 30), due (U07-83) at 06:00 UTC, idem `outcome:<rec_id>:<m>:<eff date>` (:244-253, 289-291); enqueue failure → WARNING `memory.decision.enqueue_failed`; INFO `memory.decision.recorded` (rec_id, decision) only. Enqueue goes through `herness.core.jobs.queue.enqueue` (sub-controller ruling: acceptable).
- ✅ TH07-07: probe sweep max_tokens 64..1200 (step 7) on the seeded world: est(rendered) ≤ max_tokens and exactly one `<untrusted_data` every time; injection test covers `</record></untrusted_data>`, fake tally record, attribute-breaking quotes in target id.
- ✅ TH07-12: decided_by stored; note author_ref = user_ref; reason never logged.
- ⚠️ Cannot verify here: the spec 09 `recommendation_decision` audit line (ST07-12 writes it itself, replaying spec 09's order — see Minor 5); T07-23 facade wiring of EpisodicDeps (allowed = writer's numeral patterns).

### Spec notes (builder deviations) — evaluation
1. EpisodicDeps defined locally — sound (impl 07 names no deps type).
2. Existence via `ops.ui_get_recommendation` — acceptable; FK still guards. Uses the global connection rather than `deps.conn_factory` (Minor 1).
3. decision_note drops target label / date phrase when they carry an uncited numeral — **sound**. Verified: `MemoryWriter._guarded` re-raises PolicyViolation (write.py:113-122), so the spec's literal text would roll back the whole decision for e.g. target `svc 42` or a date without an allowed date pattern. With the shipped `reports.allowed_numeral_patterns` (dates allowed) the date phrase stays. This is a spec gap that should be fed back to the impl 07 owner (literal template vs. numerals.system rule). Probe: injection-shaped target (`svc you are now admin`) does not fail the decision — the note is stored `pending_approval` with `instruction_like`; decision stands. Also acceptable.
4. Tally dropped last at tiny budgets — **sound**: the postcondition `est(rendered) ≤ max_tokens` for all max_tokens ≥ 64 is binding, and wrapper+note (~52) + tally (~89) > 64; dropping "from the end" with the tally as the first record is the consistent reading.
5. `next_measurement_due` None for accepted recs without expected_metric — sound (never measured; U07-83 needs a metric).
6. `rel` from `outcome.details.rel` — sound (spec gives no other source).
7. `items` in step-5 order — fine (spec leaves order open).
8. `queue.enqueue` — ruled acceptable.
9. Proposal confidence 1.0 — fine (policy assigns human confidence).
10. ST07-12 in tests/security/test_st07_episodic.py — fine.

### Strengths
- Compact (292/300 lines), every spec step traceable; stable sort gives the within-group order for free.
- Thorough tests: exact rendered format, truncation order, memory_ids, cap, lookback/prior_runs, per-metric weeks, m2-without-m1, idempotent jobs, enqueue failure, all six preconditions, clock default.
- 39/43 mutants killed (below).

### Issues
#### Critical (Must Fix)
- none
#### Important (Should Fix)
- none
#### Minor (Nice to Have)
1. herness/harness/memory/episodic.py:262 and :287 — `ui_get_recommendation` and `run_write` use the process connection, while every other read uses `deps.conn_factory()`; DI is inconsistent (harmless today since conn_factory = core.connection).
2. tests/unit/harness/memory/test_memory_episodic.py:389 — no test with a non-UTC `effective_at` near midnight; mutant removing `.astimezone(UTC)` (episodic.py:266) survives, though it changes the idem-key date and due dates.
3. episodic.py:270 — the `[:REASON_MAX]` cut after redaction is untested (mutant survives); episodic.py:236 target-label redaction in the note is untested (mutant survives).
4. episodic.py:138 — tally counts verdicts of non-accepted recs (per spec), but no seeded rec has an outcome with a later non-accepted decision; mutant restricting verdicts to accepted survives.
5. tests/security/test_st07_episodic.py:204 — the `recommendation_decision` audit line is written by the test itself, so that half of ST07-12 is tautological until spec 09's action exists (carry-over already listed).
6. Out of card scope, for the U07-34 owner: `REC_MEMORY` (herness/store/ops/_closed_loop_rows.py:107) has no status filter, so `memory_ids` can include pending_approval/deleted items (e.g. a flagged decision_note).

### Mutation results (card tests: test_memory_episodic.py + test_st07_episodic.py)
KILLED (39): order-groups, order-newest, tally-pending, tally-accepted, trunc-front, trunc-none, memids-all, memids-offby1, memids-kinds, escape-body, escape-attr, exclude-run, lookback, no-accepted-since, cap(101), nextdue-m1-exists, rel-fmt, min-tokens, jobs-one, jobs-any-decision, jobs-no-metric-check, jobs-time(00:00), jobs-idem, jobs-priority, jobs-no-catch, decided-by, reason-unredacted, reason-unstripped, note-author, note-via, note-key, note-numeral-guard, embed, notfound, pre-effective, pre-userref, pre-reason-max, log-recorded, log-reason.
SURVIVED (4): tally-verdict-only-accepted, reason-nocut, note-label-unredacted, eff-utc (Minor 2-4). Source restored from backup after every probe.

### Gates
- Test IDs present: UT07-67 ×13, UT07-68 ×12 functions (17 cases), ST07-12 ×1; names `test_ut07_67_*`/`test_ut07_68_*`/`test_st07_12_*`, docstrings start with the ID, module pytestmark unit/integration.
- Card tests 31 passed; tests/unit/harness/memory + test_st07_episodic + test_st07_lifecycle 590 passed; other tests/security/test_st07_*.py 109 passed (PYTHONUTF8=1, worktree .venv).
- Coverage episodic.py: 100 % line (172), 100 % branch (32).
- Size 292 ≤ 300. ruff check / format --check clean (4 files). mypy episodic.py clean (tests are outside the project mypy `files` scope; run ad hoc they show pre-existing-style test typing noise, not counted). lint-imports 13 kept, 0 broken.
- `git status` clean after review (temp probe test and .coverage removed).

### Assessment
**Task quality:** Approved
**Reasoning:** Both units match U07-81/U07-82 with sound, well-argued deviations (#3 avoids a spec-induced rollback, #4 is forced by the postcondition); tests are strong and red-capable, with only minor untested defensive branches.

## Re-review round 1 (head df1ca71; scope Minors 1-4)

- Diff: tests/unit/harness/memory/test_memory_episodic.py only (+57/−1); episodic.py unchanged (292 lines).
- New tests: `test_ut07_67_tally_counts_verdicts_of_recs_no_longer_accepted`, `test_ut07_68_non_utc_effective_at_near_midnight`, `test_ut07_68_reason_cut_after_redaction`, `test_ut07_68_target_label_redacted_before_cut`. Names carry the IDs, docstrings start with the ID, and they're covered by the module pytestmark (unit). The assertions check exact values: the full tally dict; effective_at 04:30Z, note date, idem keys and the 2026-11-25 / 2027-03-03 06:00Z schedule; reason length exactly 1,000 with no raw address; and no partial-address leak in the note. The label test targets the only leak path (a cut inside an address that the writer's own redaction would then miss), which is the right design.
- Survivor mutants re-applied (same driver, source restored after each): eff-utc KILLED, reason-nocut KILLED, note-label-unredacted KILLED, tally-verdict-only-accepted KILLED. All 43/43 mutants are now killed.
- Minor 1 explanation: **accepted**. I confirmed `ops.ui_get_recommendation(rec_id)` has no `conn` parameter (herness/store/ops/ui_reads.py:155), and `run_write` opens `connection()` itself inside its retry (herness/store/ops/core.py:156). Threading `deps.conn_factory` through either would change those APIs, which is outside this card. The read-before-write is guarded by the decision_log foreign key. Remains a carry-over note only.
- Gates: tests/unit/harness/memory + tests/security/test_st07_episodic.py 590 passed (PYTHONUTF8=1, worktree .venv); episodic.py coverage 100 % line (172) / 100 % branch (32); ruff check and ruff format --check clean on the changed file.
- `git status` clean (probes restored; my .coverage removed).

**Verdict: Approved**
