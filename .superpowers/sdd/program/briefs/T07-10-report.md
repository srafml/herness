# T07-10 Hybrid recall — build report (rebuild on base 3f61b67)

Status: DONE — commit 32ba376 feat(memory): T07-10 hybrid recall (all pre-commit hooks passed incl. pytest-unit)
Worktree: D:\herness\.claude\worktrees\agent-af74d44c522054d93 (branch worktree-agent-af74d44c522054d93)

## What happened
The previous build (base 984de12) finished green but its worktree was deleted before commit.
Its four recovered files were copied unchanged into this worktree, re-reviewed against the brief
and the binding rulings of the dispatch, and one test was added (fusion determinism / bounded k).

## Implemented
herness/harness/memory/recall.py (360/360 lines, budget 360): fts_query_string (U07-58),
score_candidate (U07-59), MmrCandidate + mmr_select (U07-60), RelatednessCache (U07-61),
MemoryRecaller + RecallResult (U07-62).

## Files
- herness/harness/memory/recall.py: 360 lines (budget 360; exactly at budget, no private sibling)
- tests/unit/harness/memory/test_memory_recall.py: UT07-39..UT07-45, render end-to-end
  (recall hits -> render.render_records: [UNCONFIRMED] prefix and escaping inherited), and new
  test_ut07_44_fusion_is_deterministic_and_bounded (vector+keyword+entity fusion is identical
  across repeated calls and bounded by k)
- tests/unit/harness/memory/test_memory_recall_props.py: PT07-06 (hypothesis)
- tests/security/test_st07_recall.py: ST07-06, ST07-08 (19 attack strings), ST07-13
No pyproject / import-linter / docs changes (harness L4 -> store L1 allowed).

## Re-check against the binding rulings
- Hits only; rendering stays in render.render_records (e2e test pipes hits through it).
- Query redacted (redactor.redact) before embedding and before fts_query_string; the query is
  never used in SQL except as the quoted-token MATCH parameter, never in vector filters
  (VectorIndex filters come from layer/status literals only).
- Fusion: round robin over vector / keyword / entity candidate lists, dedupe, cap 150; MMR ties
  -> higher score then lower memory_id: deterministic, <= k hits.
- Any vector-path failure (embed, search, vectors) -> degraded=True, WARNING
  memory.recall.degraded with reason=<exception type name> and run_id only; never raises.
  ToolInputError (arguments) and StoreBusy (SQLite reads) propagate.
- Relatedness build = run_ctx.build_id else warehouse.read_current(); warehouse only through
  RelatednessCache's connect_build (default warehouse.open_readonly); any failure -> ent 0.
- Logs carry no memory text, query text or memory ids (UT07-45 asserts it).
- Store rules: search limit from cfg (<=200 via config), store.py untouched; checkout_latest is
  done inside VectorIndex.search/vectors; Embedder uses the lazy enrich.embed_query facade.

## Spec readings / deviations (unchanged from the first build)
1. Step 7: vectors.vectors() is fetched for ALL kept items (not only non-ANN ones) because MMR
   (step 11) needs item vectors; sim for ANN hits stays 1 - distance, for others max(0, dot).
2. Candidate cap 150 (impl 07 §10): round-robin union then truncation, so no source starves.
3. vectors.vectors() failing after a successful search also degrades; degraded scoring ignores sim.
4. Invalid hydrated rows (MemoryItem validation fails) are skipped with WARNING
   memory.recall.invalid_row (error_type only); event not in the spec log table.
5. Constructor types: vectors: VectorIndex, embedder: Embedder, redactor: Redactor (tests pass
   fakes with type: ignore). relatedness optional (None -> ent never 0.5).
6. Redactor returning None (only for None input) -> empty text. RedactionFailed (a FatalError)
   propagates: fail closed, unredacted text is never embedded or matched. This is the one error
   besides ToolInputError/StoreBusy that can leave recall; flagged as a concern.
7. `now` must be aware (clock.ensure_utc -> SchemaViolation for naive); default clock.now().
8. Empty `layers` list -> ToolInputError.
9. Relatedness build resolved lazily only when filters.entity_ids and a RelatednessCache are set.
   Groups exactly per U07-61 (union of rows containing an id, not transitive closure).
10. Logs: memory.recall.completed (DEBUG: n_candidates, n_hits, degraded, duration_ms, run_id),
    memory.recall.degraded (WARNING: reason, run_id), memory.recall.relatedness_unavailable
    (WARNING: build_id, error_type).
11. memory.recall.fts_rejected is not emitted: fts_query_string output cannot be an FTS5 syntax
    error and fts_candidates returns [] indistinguishably from no match.
Metric: record_histogram("herness_memory_recall_latency_seconds", ..., labels={"degraded"}).

## Tests / gates (this worktree)
- card tests: 65 passed; recall.py coverage 100% line, 100% branch (253 stmts, 46 branches)
- pytest tests/unit/harness/memory + ST file: 334 passed
- ruff format --check . / ruff check . clean; mypy (4 touched files) clean; lint-imports 13 kept
  0 broken; check_module_size exit 0; check_type_ownership exit 0
- RED evidence: new fusion test first failed (n_hits 6 < 7: query vector did not match the indexed
  vectors, so keyword-only items fell below min_score 0.30 by design); fixed the test query.

## Process note
Recovered code was already green, so no separate wip checkpoint was made: the first commit is
the final `feat(memory): T07-10 hybrid recall` (one hook run instead of two ~20 min runs).

## Concerns
- recall.py is exactly at its 360-line budget: any later change needs a budget ruling or a
  private sibling.
- RedactionFailed propagates from recall (fail closed), see deviation 6.

## Fix round 1 (review T07-10-review.md, base 32ba376) — commit 1a0ca4b, all hooks passed
- I1: ANN ids from the vector index are filtered with herness.core.ids.is_valid_id(IdKind.MEMORY, id)
  before the candidate union; when any are dropped, WARNING `memory.recall.invalid_vector_ids`
  with `count` only (no ids). A corrupted/foreign LanceDB row no longer reaches
  ops.get_memory_items (which raised ToolInputError), so recall does not raise and valid hits are
  still returned. New test test_ut07_44_malformed_vector_ids_dropped (ids "not-a-memory-id",
  "mem_" + 26 x "U" (U not in the ULID alphabet), "q_0123456789abcdef"): no raise, only the valid
  hit, n_candidates 1, not degraded, one warning with count 3, no id in logs.
- M1/M2: recall docstring now states it raises ToolInputError, StoreBusy, SchemaViolation for a
  naive `now`, and RedactionFailed (fail closed, deliberate). No behaviour change.
- Budget: fitted by compaction, no sibling module: removed _SECONDS_PER_DAY (age via
  `/ timedelta(days=1)`), inlined the core.team SQL, folded the _visible docstring into a comment,
  compacted _ent's relatedness branch. recall.py 360/360; check_module_size exit 0.
- Gates: tests/unit/harness/memory + ST07 file 335 passed; recall.py 100% line / 100% branch;
  ruff format --check . and ruff check . clean; mypy clean; lint-imports 13 kept 0 broken;
  check_module_size 0; check_type_ownership 0.
