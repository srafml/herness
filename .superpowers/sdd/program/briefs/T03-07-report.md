# T03-07 report — Embed stage (herness/enrich/embed_stage.py)

Status: DONE_WITH_CONCERNS (minor; see Concerns). Worktree agent-a9f79661d37812af6, base 667fcf4.
Final commit b781a26 (checkpoint 6ca52eb wip(T03-07) squashed into it (soft reset of the own wip commit) under
`feat(enrich): embed stage with anti-join upsert, orphan delete and index maintenance (T03-07)`.

## Built
- `lance_filter_in` (U03-33): allowlist regexes verbatim (fullmatch, so a trailing newline fails),
  1–1,000 values, unknown column / bare str -> SchemaViolation("invalid <column> for vector filter");
  value never echoed.
- `run_embed_stage` (U03-34): VectorStore() + ensure_tables + table("ticket_embedding"); reads
  (record_id, content_hash, model) as Arrow, registers it in DuckDB; one query does the anti-join
  on (record_id, content_hash, model) joined to core.incident/change/problem (change opened_at =
  coalesce(opened_at, planned_start, actual_start)) and flags `reuse` (hash stored for the model);
  a second query finds orphans. New: deduped by hash, sorted by text length, fed to `embed_texts`
  (T03-06) with an `on_batch` callback that maps vectors back via a cursor (the stage pre-sorts
  stably by length so embed_texts' own stable sort is the identity). Every 20 batches: flush
  (merge_insert on record_id), ctx.heartbeat("embed"), YieldRequested("embed") if should_yield().
  Reuse: vectors read in 1,000-hash chunks via lance_filter_in (model filtered in Python, so the
  model id never enters a filter string). Orphans deleted in 1,000-id chunks. maintain_index.
  Report: embedded / cache_hits / rows (module-private `_Report` Protocol, ruling 1).
- `maintain_index` (U03-35): <10k skipped; never built or n > 1.2 x index_rows -> IVF_PQ cosine,
  num_partitions=round(sqrt(n)), num_sub_vectors=64, replace=True, store index_rows; else optimize().
- Ruling 2 (M2): each batch checked (2-D, 0 < n <= remaining, width 1024, floating dtype) BEFORE any
  buffer append -> SchemaViolation, no text.
- Ruling 3: buffer bounded at 20 x batch_size rows (extra flush when hash-sharing records fill it);
  comment states <= 20 x 512 x 4 KiB = 40 MiB (10 MiB at batch 128). embed_texts is called once per
  20-batch window so its returned result matrix is bounded too.
- Ruling 4: no GPU lock / gpu_lock import. Ruling 12 log events: enrich.embed.batch_flushed(rows),
  enrich.embed.completed(embedded, cache_hits, rows, deleted, index_rebuilt, duration_ms),
  enrich.embed.index_maintained(action, rows) — counts only. `# T08-05:` marker at counter site.
- LanceDB errors -> reuses `herness.store.vectors._store_error` (StoreBusy for conflict/busy/lock,
  else SchemaViolation); DuckDB errors -> SchemaViolation("embed stage: <ErrorClass>").

## Line counts
embed_stage.py 318 / 320 budget (hard 400). Tests: _embed_support.py 157, test_embed_stage.py 470,
security 89, integration 112, fault 76. No import-linter / pyproject change needed (enrich wildcard).

## Deviations from spec
1. `herness.index_rows` is stored as field metadata of the `vector` column (part of the LanceDB
   schema), not schema-level metadata: pinned lancedb 0.39.0 has no table schema-metadata update
   without pylance (OI-12). Round-trips on a real table (reopen, after optimize/merge).
2. create_index uses the non-deprecated API `create_index("vector", config=IvfPq(distance_type=
   "cosine", num_partitions=..., num_sub_vectors=64), replace=True)`; the legacy keyword form in the
   spec emits a DeprecationWarning, which is an error under filterwarnings "error:::herness".
3. fault point "embed.batch": not called again in the stage — embed_texts already calls it per batch
   (run_batches_with_oom_backoff fault_name="embed.batch"); a second call would double plan counters.
   FT03-06 drives it through that existing per-batch site.
4. embed_texts is called per 20-batch window (memory bound). After a CUDA OOM halving, the next
   window starts again at cfg batch_size (one extra OOM retry + halving per window on a GPU that
   cannot hold 128). Checkpoints stay every 20 batches (counted across windows).
5. `_store_error` is a private name of herness.store.vectors imported cross-package (reuse over
   duplication); a public alias would need a T02-08 change.

## Tests (25 functions; all green)
- UT03-30 (4 fns, 12 cases) tests/unit/enrich/test_embed_stage.py
- UT03-31 (11): 3 encodes + reuse + orphan delete + change opened_at coalesce; current rows untouched
  / other model re-encoded; empty; flush every 20 batches + heartbeat [160, 10]; shared-hash row
  bound [160, 40]; yield after flush; wrong width / int dtype / extra rows -> SchemaViolation, 0 rows;
  LanceDB conflict/lock -> StoreBusy, other -> SchemaViolation; DuckDB error; default VectorStore()
  + config batch_size.
- UT03-32 (4): fake table 5k/20k/23k/25k -> skipped/rebuilt/optimized/rebuilt (+ log events,
  IvfPq params); unreadable metadata -> rebuilt; error mapping; real LanceDB 10k rows: rebuilt,
  herness.index_rows round-trips, list_indices == 1, then optimized (~3 s).
- ST03-09 (2) tests/unit/enrich/security/test_embed_stage_security.py: `x' OR 1=1 --` via
  lance_filter_in on a tmp table (not echoed, 0 rows deleted); planted orphan id stops the stage's
  orphan delete, no row deleted. purge_record half = carry-over.
- ST03-15 (1): encoder inputs == distinct enrich.text_redacted.text; every stage log field numeric.
- IT03-03 (1) tests/integration/enrich/test_embed_stage_flow.py: tiny-st on cpu, hand-built
  warehouse, tmp LanceDB: run 1 embeds 60 (61 rows), run 2 embedded == 0 and no encoder call; run 3
  after one edit, one core delete, one duplicate-hash add: 1 encode, 1 reuse, orphan deleted.
- FT03-06 (2) tests/fault/enrich/test_embed_stage_fault.py (marker fault): preempt -> 160 rows
  flushed, YieldRequested("embed"), rerun encodes exactly the other 10; fault plan
  error:ModelUnavailable at nth=22 embed.batch -> 160 kept, rerun encodes 10.
Coverage embed_stage.py: 100 % line, 100 % branch (card tests).
Gate: `pytest tests/unit/enrich tests/integration/enrich tests/fault/enrich -q -p no:logging`
767 passed, 2 skipped; ruff format/check clean; mypy clean; lint-imports 13 kept; check_module_size
0; check_type_ownership 0; commit hooks (incl. pytest-unit, detect-secrets) passed.

## Open concerns / carry-overs
- JobOutcome(yield) mapping and StageReport retype: T03-28.
- small_build acceptance: re-run IT03-03 on small_build when impl 11 lands.
- ST03-09 purge_record half: when spec 10 purge_record exists.
- Spec notes to record: deviations 1, 2 (U03-35 wording), 3 (FT03-06 fault point location).

## Fix round 1 (review T03-07-review.md; controller ruling) — commit 562c1ce on top of b781a26
- I-1: reuse vectors are now read (`_read_vectors`, 1,000-hash chunks) BEFORE any write, per
  U03-34 step 5; a reuse-flagged hash not found at read time is moved to the `new` set and encoded
  (no KeyError). Tests: test_ut03_31_text_swap_reads_reuse_vector_before_writes (A1 text changes,
  new B1 gets A1's old text -> 1 encode, B1 reuses the old vector),
  test_ut03_31_reuse_hash_missing_at_read_is_encoded.
- I-2: `_plan` keeps only U03-33-allowlisted record_ids: non-allowlisted records are not embedded
  and non-allowlisted orphans are skipped (never put in a filter); one WARNING
  `enrich.embed.ids_skipped` (records, orphans counts; no ids). lance_filter_in stays strict.
  Tests: test_ut03_31_non_allowlisted_record_is_not_embedded; ST03-09 second test replaced by
  test_st03_09_injected_orphan_id_is_skipped_and_not_deleted_by_filter (stage completes, planted
  row kept, valid orphan deleted, warning count 1, attack string absent from logs).
- M-1: vector->hash mapping no longer depends on embed_texts' batch order: `_encode` uses the
  input-aligned result embed_texts returns (one call per 20-batch window); the on_batch callback
  only validates (2-D, width 1024, float, cumulative rows <= texts) before anything is used, and
  after return rows received == texts and result shape == (n, 1024), else SchemaViolation (no
  text). Consequence: the checkpoint (flush, heartbeat, yield) runs once per full 20-batch window
  (= every 20 batches without OOM halving; after halving, every window of 20 x batch_size texts).
  Tests: test_ut03_31_vectors_map_by_aligned_result_not_batch_order,
  test_ut03_31_result_not_aligned_with_texts_is_schema_violation.
- M-4: non-str value to lance_filter_in -> SchemaViolation (test_ut03_30_non_str_value_is_schema_violation).
- Compaction to stay in budget: `_Sink` is a NamedTuple, upsert moved into `_Buffer.flush`, shared
  `_read` helper, DuckDB queries inlined into `_plan`, shorter docstrings; `enrich.embed.completed`
  now logs the report counters directly (StageReport is per stage) instead of per-call deltas.
- Line count: embed_stage.py 319 / 320.
- Gates: card tests 42 passed with --require-test-ids, embed_stage.py 100 % line / 100 % branch;
  enrich unit+integration+fault 773 passed, 2 skipped; ruff format/check clean; mypy clean;
  lint-imports 13 kept; check_module_size 0; detect-secrets clean.
- Parked per ruling: M-2 (OOM halving resets per window), M-3 (private _store_error import),
  M-5 (service_id/opened_at changes without text change not re-upserted).

## Spec notes (add)
- make_record_id (spec 00, herness/core/ids.py) admits source keys outside the U03-33 record_id
  allowlist (`[A-Za-z0-9._-]{1,128}`); such records are skipped by the embed stage (WARNING count).
  Either widen/escape the allowlist or constrain make_record_id.
- U03-35: herness.index_rows stored as `vector` field metadata (lancedb 0.39.0, OI-12); IvfPq config API.
- F03-03 / FT03-06: the "embed.batch" fault point fires inside embed_texts (U03-32).
