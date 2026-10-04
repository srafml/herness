# T07-16 report — Prior context and decisions (U07-81, U07-82)

Status: DONE. Checkpoint 7a13590 (all code + tests); final commit `feat(memory): T07-16 prior context and decisions` = 0a4e2cd (empty marker commit on top; all pre-commit hooks passed on both commits).

## What was built
- `herness/harness/memory/episodic.py` (292 lines, budget 300, ENG limit 400):
  - `EpisodicDeps` (frozen dataclass, RecommendDeps pattern): `conn_factory`, `writer: MemoryWriter`,
    `redactor: Redactor`, `allowed: Sequence[re.Pattern]` (must be the writer's allowed numeral
    patterns), `episodic: EpisodicConfig`, `outcome: OutcomeConfig`.
  - `prior_context(run_ctx, max_tokens=3000, *, deps, now=None) -> PriorContext` (U07-81): steps 1-8
    as specified; reads only; `max_tokens < 64` -> ToolInputError("max_tokens too small").
  - `decide(rec_id, decision, reason, user_ref, effective_at=None, *, deps, now=None) -> None` (U07-82):
    preconditions -> ToolInputError("decide: invalid <arg>"); unknown rec -> MemoryNotFound("recommendation");
    one run_write (insert_decision + writer.insert_system_item(decision_note, key_hash=keyed_hash(
    "decision_note:<rec_id>:<decided_at text>"), conn)); embed_after_commit; when accepted AND
    expected_metric set: two `herness.core.jobs.queue.enqueue("outcome_measure", {rec_id, measurement},
    "none", None, <U07-83 due> 06:00 UTC, idem_key="outcome:<rec_id>:<m>:<eff date>")`, HernessError ->
    WARNING memory.decision.enqueue_failed (rec_id, measurement, error class); INFO memory.decision.recorded
    (rec_id, decision). Reason is stripped, redacted, cut to 1,000; decided_by = user_ref (TH07-12).
- Tests: `tests/unit/harness/memory/test_memory_episodic.py` (UT07-67 x13, UT07-68 x17 incl. params),
  `tests/unit/harness/memory/_episodic_env.py` (deps factory, closed-loop seeds),
  `tests/security/test_st07_episodic.py` (ST07-12, marker integration).

## Tests
- UT07-67: order (4 groups, newest first within), items/tally, rendered block exact format (CONTEXT_NOTE first,
  tally record, attribute order, known-only attributes, marker values), memory_ids of rendered recs only,
  truncation from the end, 64-token budget (wrapper only), tally-only budget, max_tokens precondition,
  prior_runs/lookback config, cap 100 newest, m1 still due when only m2 recorded, per-metric weeks,
  TH07-07 injection (`</record></untrusted_data>`, fake tally record, attribute-breaking quotes in target id:
  exactly one wrapper, 2 records, escaped/blocked), malformed stored NumberRef.
- UT07-68: accepted -> one row (decided_by, decided_at, effective_at), redacted reason, one active
  decision_note (human/dashboard provenance, keyed hash, embedded after commit), two queued outcome_measure jobs
  (priority 30 default, gpu none, payloads, 06:00 UTC due dates 2026-11-24 / 2027-03-02, idem keys);
  date-pattern writer keeps the date; numeral-bearing target left out; repeat decide -> still 2 jobs (idem);
  rejected/deferred -> no jobs; no expected_metric -> no jobs; enqueue failure logged, decision stands;
  unknown rec; 6 precondition cases; 1,000-char reason bound; clock default.
- ST07-12: approve (approved_by + review_decision audit line by REVIEWER) and decide (decided_by in
  decision_log, note author_ref; decide writes no audit line itself; spec 09 order audit->decide leaves one
  recommendation_decision line with actor = decided_by; reason text never in the audit file).
- RED evidence: first run of the test module -> `ModuleNotFoundError: No module named
  'herness.harness.memory.episodic'` (collection error). ST07-12 red shown by mutating decided_by to "0"*32:
  `AssertionError ... ('deferred', '000...') != ('deferred', 'ddd...')`, then restored.
- GREEN: `pytest tests/unit/harness/memory tests/security/test_st07_episodic.py tests/security/test_st07_lifecycle.py`
  -> 590 passed. Coverage of episodic.py (card tests): 100 % line, 100 % branch (172 stmts, 32 branches).
- Gates: ruff format/check clean, mypy (355 files) clean, lint-imports 13 kept, check_module_size exit 0,
  check_type_ownership exit 0.

## Spec notes / deviations
1. EpisodicDeps is not defined by impl 07; defined here as above (only what the algorithm needs).
2. Recommendation existence (U07-82 step 1) uses `ops.ui_get_recommendation` (impl 09 ui_reads; closed_loop has
   no by-id read). It runs before the run_write; the decision_log foreign key still guards the insert.
3. Numerals: decision_note is system text (numerals.system rejects any uncited numeral, which would roll back
   the decision). The ULID rec_id never trips NUMERAL_RE (preceded by `_`), but the YYYY-MM-DD date does unless
   an allowed pattern covers it (the default reports.allowed_numeral_patterns does), and a target id such as
   `svc 42` can. So the redacted target label (cut to 150) is replaced by `<target_type> target` and the
   ` with effect from <date>` phrase is omitted when they hold an uncited numeral under `deps.allowed`.
   `deps.allowed` must equal the writer's patterns. No `[[...]]` text is produced, so parse_markers is not
   involved.
4. Truncation: when no recommendation record fits, the tally record is dropped last (wrapper + CONTEXT_NOTE
   alone is ~52 tokens; with the tally ~89 > 64), keeping the postcondition est <= max_tokens.
5. `next_measurement_due` is None for accepted recs without `expected_metric` (they are never measured).
   MetricWeeks resolved from cfg.outcome + per_metric override (no shared helper exists yet).
6. Outcome `rel` comes from `outcome.details.rel` (non-numeric -> None, attribute omitted).
7. `PriorContext.items` is returned in the step 5 order.
8. Enqueue goes through `herness.core.jobs.queue.enqueue` (not re-exported by `herness.core.jobs`).
9. decision_note proposal confidence 1.0 (the policy assigns the human confidence).
10. ST07-12 lives in tests/security/test_st07_episodic.py (as T07-09 note (9)).

## Carry-overs
- T07-23: MemoryStore facade wiring `prior_context` / `decide` (+ EpisodicDeps construction from config,
  allowed = reports compiled numeral patterns).
- Spec 09 decide_recommendation action writes the `recommendation_decision` audit line (U07-82 security notes).
- A shared MetricWeeks resolver could move to outcome_stats when T07-18 needs the same.

## Fix round 1 (review T07-16-review.md, mutants m1-m4)
New tests in tests/unit/harness/memory/test_memory_episodic.py, each shown red against its mutant
(applied by a temp script, file restored afterwards), then green:
- m2 `test_ut07_68_non_utc_effective_at_near_midnight`: effective_at 2026-09-01T23:30-05:00 pins
  decision_log effective_at 2026-09-02T04:30Z, note date 2026-09-02, idem keys `...:2026-09-02`,
  scheduled_for 2026-11-25 / 2027-03-03 06:00Z. Mutant (no `.astimezone(UTC)` on eff): red (note date 2026-09-01).
- m3a `test_ut07_68_reason_cut_after_redaction`: 979-char reason of 140 addresses is inflated by redaction,
  stored reason is exactly 1,000 chars. Mutant (no `[:REASON_MAX]`): red (SchemaViolation insert_decision: invalid reason).
- m3b `test_ut07_68_target_label_redacted_before_cut`: target id with 8 planted emails; the raw label cut at 150
  ends inside an address. Mutant (no label redaction): red (`jane.doakes` leaked in the note).
- m4 `test_ut07_67_tally_counts_verdicts_of_recs_no_longer_accepted`: recs with outcomes and a later
  rejected / deferred decision still count their verdicts (paid_off 1, worse 1, accepted 0, pending 0), per
  "latest outcome verdicts" in U07-81 step 4. Mutant (verdicts only for accepted): red.
- m1 (optional) not done: `ops.ui_get_recommendation` (impl 09) takes no `conn`, and `ops.run_write`
  opens its own write transaction on the store's connection (core API has no connection-factory argument),
  so `deps.conn_factory` cannot be threaded through either without changing those APIs; the read happens before
  the write and the decision_log foreign key guards the insert.
- m5, m6 left as carry-overs as instructed.
Results: tests/unit/harness/memory + tests/security/test_st07_episodic.py + test_st07_lifecycle.py 594 passed;
all ST07 security tests 114 passed; episodic.py 100 % line / 100 % branch; episodic.py unchanged at 292 lines.
