# T03-07 review — Embed stage (herness/enrich/embed_stage.py) @ b781a26

**Verdict: Needs fixes** (1 Important bug, 1 Important plan-mandated item for the controller)

### Spec Compliance
- UT03-30 (U03-33 lance_filter_in) ✅ — regexes verbatim, fullmatch, 1–1,000 bound, value not echoed.
- UT03-31 (U03-34 run_embed_stage) ✅ for the spec scenario — but see I-1 (a reuse case the spec's step order covers and the code does not).
- UT03-32 (U03-35 maintain_index) ✅ — <10k skipped; never built / n > 1.2×index_rows -> rebuilt with round(sqrt(n)) partitions, 64 sub-vectors, cosine, replace=True; else optimize; real-LanceDB round-trip of `herness.index_rows`.
- IT03-03 ✅ — tiny-st on cpu, hand-built warehouse, tmp LanceDB; run 2: embedded 0 and no encoder call; run 3: 1 encode + 1 reuse + orphan gone.
- ST03-09 ✅ (lance_filter_in half, per ruling; purge_record half = carry-over) — `x' OR 1=1 --` -> SchemaViolation, not echoed (message and context), 0 rows deleted.
- ST03-15 ✅ — spy: encoder inputs == distinct enrich.text_redacted.text; every non-name log field numeric (only `action` string exempted). Model id comes from the encoder only and never enters a filter string (model filtered in Python).
- FT03-06 ✅ (stage half, per ruling) — preempt: 160 rows flushed, YieldRequested("embed"), rerun encodes only the 10 remaining; fault-plan error at the 22nd embed.batch keeps 160, rerun encodes 10.
- Rulings: _Report Protocol ✅; M2 shape check before any append/write ✅ (mutation probe: check disabled -> both M2 tests red; reverted); buffer ≤ 20×batch_size rows ✅ (extra flush on shared-hash fill, tested); no GPU lock / gpu_lock import ✅; vectors only via VectorStore().table ✅; each distinct new hash encoded once per model ✅; orphan delete in 1,000-id chunks through lance_filter_in ✅; change opened_at coalesce ✅ (tested); logs counts only ✅; StoreBusy vs SchemaViolation mapping ✅ (conflict/lock/other and DuckDB tested).
- ⚠️ Cannot verify here: small_build acceptance (fixture absent; carry-over); JobOutcome(yield) mapping (T03-28); metric counter (T08-05 marker only).

### Deviations (builder)
1. Field-level metadata on `vector` instead of table schema metadata — **accept**. Scratch probe: key survives merge_insert and delete on lancedb 0.39; UT03-32 real test covers optimize/reopen; `VectorStore._verify` compares field types only, so it does not trip ensure_tables. Record as a spec note on U03-35.
2. `create_index("vector", config=IvfPq(...))` — **accept** (same parameters; legacy form warns -> error under filterwarnings).
3. No second `embed.batch` fault point — **accept** (embed_texts already fires it per batch; FT03-06 drives it).
4. embed_texts per 20-batch window, OOM halving resets per window — **accept as Minor** (M-2): bounded result matrix is worth it; cost ≤ 2 OOM retries per 2,560-text window on a GPU that cannot hold the configured batch.
5. Private import `herness.store.vectors._store_error` — **accept as Minor** (M-3): lint-imports clean; ask T02-08 for a public alias.

### Strengths
- Tight, readable module (318/320); one DuckDB query does the anti-join plus the reuse flag keyed on the encoder model, so a model change re-encodes (tested: other model re-encoded, current rows untouched).
- Width/shape/dtype/row-count check is real and precedes buffering; errors carry no text.
- Tests use real LanceDB and real tiny-st, not only fakes; 100 % line and branch coverage.

### Issues
#### Critical (Must Fix)
- none

#### Important (Should Fix)
- **I-1 Reuse vectors are read after the new-hash upserts; the source row can already be overwritten -> bare KeyError.** herness/enrich/embed_stage.py:272-273 (order) and :233-235 (`row_of[digest]`). Spec U03-34 step 5 reads reuse vectors *before* step 6 writes. Reproduced (temporary unit test, removed): table holds `A1` with H("old text"); warehouse now has A1 = "new text" and a new record B1 = "old text". B1 is flagged reuse (H in `have`); `_embed_new` upserts A1 with H("new text"), replacing the only H row; `_upsert_reuse` then finds nothing -> `KeyError: 'c9fb…'` at embed_stage.py:235 — untyped, outside the StoreBusy/SchemaViolation contract; the stage fails (a rerun recovers only because B1 then becomes "new"). Same hazard across reuse chunks (>1,000 hashes): a reuse upsert changing record X's hash can remove a hash a later chunk needs. Fix: read reuse vectors from the table version pinned at stage start (capture `table.version`, read through a checked-out handle), or read them before any write, and/or treat hashes missing at read time as `new` (encode) instead of indexing blindly. Add a UT03-31 case for the swap.
- **I-2 (plan-mandated, escalate) An orphan whose record_id is valid per spec 00 but outside the U03-33 allowlist blocks the stage on every run.** herness/enrich/embed_stage.py:274-275 with :52. `make_record_id` (herness/core/ids.py:163-176) accepts any printable source_key up to RECORD_KEY_MAX_LEN (space, `/`, `#`, `:`, quote …), and the upsert path does not validate, so such a record gets embedded; once it leaves core.*, every later run raises SchemaViolation at the orphan delete (after upserts, before maintain_index). The second ST03-09 test pins exactly this fail-closed behaviour. The U03-33 regex is binding, so this needs a controller/spec ruling (e.g. validate at upsert and skip+count, skip-and-count non-allowlisted orphans, or align the allowlist with spec 00 plus quote escaping).

#### Minor (Nice to Have)
- M-1 Vector-to-record mapping depends on embed_texts' internal stable sort by `len(text)` (embed_stage.py:155-157, 210 vs herness/enrich/embed.py:218). Documented in embed_texts' docstring, but a change there would silently misassign vectors; add a guard (test pinning the order contract, or assert/map against embed_texts' aligned return per window).
- M-2 OOM halving resets each window (deviation 4) — note in the spec.
- M-3 Private cross-package import `_store_error as store_error` (embed_stage.py:35) — request a public name in T02-08.
- M-4 `wh.register`/`wh.unregister` (embed_stage.py:264, 269) are outside `_query`, so a DuckDB error there escapes unmapped; `lance_filter_in` with a non-str element raises TypeError, not SchemaViolation (embed_stage.py:107) — typed input, low risk.
- M-5 Anti-join is on (record_id, content_hash, model) only, so a core `service_id`/`opened_at` change with unchanged text leaves stale metadata in the vector row. Spec-conformant; spec note if spec 05 filters on these columns.

### Evidence (reviewer runs)
- Card tests with `--require-test-ids`: 36 passed; coverage embed_stage.py 100 % line / 100 % branch.
- `pytest tests/unit/enrich tests/integration/enrich tests/fault/enrich -q -p no:logging`: 767 passed, 2 skipped (platform/hardware skips).
- ruff check / format --check (6 files) clean; `uv run mypy` success (273 files); lint-imports 13 kept / 0 broken; `tools.check_module_size` rc 0.
- Mutation (width check disabled) -> 2 tests red, reverted. Reuse-swap probe -> KeyError (I-1), removed. Worktree clean.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Solid, well-tested stage meeting every listed test, but the reuse read happens after writes that can remove its source row, crashing with an untyped KeyError in a realistic edit/duplicate case (I-1); I-2 needs a controller ruling.

---

## Re-review 1 (fix round 1, head 562c1ce, base b781a26)

**Verdict: Approved**

### Findings
- I-1 ✅ — reuse vectors are read (`_read_vectors`, 1,000-hash chunks through lance_filter_in) before any write (embed_stage.py:263-269); a flagged hash not found is routed to `new` and encoded (`ready = flagged AND known`, :264-269), so there is no KeyError path. My swap repro is now `test_ut03_31_text_swap_reads_reuse_vector_before_writes` (passes: 1 encode, B1 reuses the old vector); missing-hash case covered by `test_ut03_31_reuse_hash_missing_at_read_is_encoded`.
- I-2 ✅ — `_plan` (embed_stage.py:226-245) drops non-allowlisted record_ids from both the write set and the orphan list, one WARNING `enrich.embed.ids_skipped` with counts only (tests check no id in logs); `lance_filter_in` stays strict. Mutation probe (orphan filter removed) -> ST03-09 orphan test red; reverted.
- M-1 ✅ — vectors now come from `embed_texts`' input-aligned return per window (`_encode`, :171-185, `zip(..., strict=True)`); order no longer matters (out-of-order test). Mutation probes: per-batch shape check disabled -> 2 M2 tests red; alignment guard (:183) disabled -> alignment test red; both reverted.
- M-4 ✅ — `_allowed` (:90-91) returns False for non-str; test covers 5 / None / bytes -> SchemaViolation.
- Rulings still hold: M2 check before any buffering/write, buffer ≤ 20×batch_size rows, no GPU lock, VectorStore-only access, orphan chunks of 1,000.

### New findings
- Minor N-1 (regression of the fix, spec-shaped): `stored` (embed_stage.py:265) holds every distinct reuse vector in memory at once (4 KiB each; e.g. a record_id migration of 100k rows ≈ 400 MiB), whereas round 0 read them per 1,000-hash chunk. Spec step 5 has the same shape, so not blocking; if it matters later, read per chunk from a handle checked out at the start version instead.
- Minor N-2: non-allowlisted ids already present in the table (pre-fix or legacy rows) are never deleted as orphans; they are counted in the WARNING, so visible. Spec note together with the make_record_id vs U03-33 mismatch.

### Evidence
- Card tests `--require-test-ids`: 42 passed; embed_stage.py coverage 100 % line / 100 % branch (197 stmts, 36 branches).
- enrich unit/integration/fault: 773 passed, 2 skipped (platform/hardware).
- ruff check / format --check clean; `uv run mypy` clean (273 files); lint-imports 13 kept / 0 broken; check_module_size rc 0 (embed_stage.py 319/320).
- Worktree clean after all probes; no scratchpad files created this round.

**Task quality:** Approved
**Reasoning:** All four ruled findings fixed with tests that fail under mutation; only minor, spec-shaped follow-ups remain.
