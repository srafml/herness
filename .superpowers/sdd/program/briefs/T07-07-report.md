# T07-07 report — Embedding and vector adapters (build agent)

Status: DONE_WITH_CONCERNS — final commit 82b3258 feat(memory): embedding and vector adapters (T07-07)
Worktree: D:\herness\.claude\worktrees\agent-a2fc976cb383d6bf3 (branch worktree-agent-a2fc976cb383d6bf3, base 667fcf4)

## Built
- herness/harness/memory/store.py (259 lines / budget 260; check_module_size exit 0)
  - `Embedder(embed_fn=None, *, model_name, cache_size=2048)`: embed / embed_item / model_name (U07-48).
    Thread-safe OrderedDict LRU keyed by `sha256_hex(text)` (text never kept); embed_fn runs outside
    the lock; output validated (ndarray, shape (1024,), float dtype, finite, non-zero) -> float32 unit
    vector, `flags.writeable=False`; norm computed in float64 after peak rescale (no overflow).
  - `VectorRow` / `VectorMeta` frozen slotted dataclasses; `VectorIndex(store_factory=VectorStore)` with
    ensure_table / upsert (merge_insert on memory_id, update-all / insert-all) / set_status (update, 200
    ids per chunk) / delete (VectorStore.delete_ids, 200 per chunk) / search (cosine, prefilter
    layer IN + status IN, limit 1..200) / vectors (filtered scans, 200 ids per chunk, read-only arrays)
    / list_ids (memory_id > after, id order, limit 1..1000) (U07-49).
- tests/unit/harness/memory/test_memory_store_adapters.py (UT07-22, UT07-23; local `vector_tmp` fixture and FakeEmbed)
- tests/security/test_st07_vectors.py (ST07-09; SpyFactory store_factory proves LanceDB is never reached)
- tests/unit/harness/test_tools_recording.py: one-line `_NON_ROW_HASHES` addition
  `("memory/store.py", "embed")` (UT05-124 allowlist; the embed cache key uses herness.core.ids.sha256_hex).

## Decisions / spec readings
1. Default embed_fn: signature takes `embed_fn: Callable | None = None`; None resolves
   `herness.enrich.embed_query` through the lazy facade at call time (`from herness import enrich`
   at import is light). A subprocess test proves importing store.py does not import herness.enrich.embed.
2. ToolInputError from embed_fn (empty text after normalization) PROPAGATES unchanged, like ConfigError
   and ModelUnavailable (caller input error, not model unavailability). Only OSError/RuntimeError/
   ValueError/MemoryError -> ModelUnavailable("memory embedding failed", error_type=<class name>).
   Invalid output (NaN/inf/zero norm/wrong shape/non-float/non-ndarray) -> ModelUnavailable("memory
   embedding invalid output"). Nothing is cached on failure.
3. Error mapping in VectorIndex: OSError/ValueError/RuntimeError plus VectorStore's own mapped classes
   (SchemaViolation, StoreBusy, NotFound incl. store NotFoundError) -> ModelUnavailable("memory vector
   store unavailable: <op>") with ops ensure_table/upsert/set_status/delete/search/vectors/list_ids.
   No logging at all in store.py (so no text/vector can leak through logs).
4. Filter allowlists: every filter value must fullmatch MEMORY_ID_RE or a pattern built from the
   Layer/Status literal sets (anchored via fullmatch; a trailing "\n" is rejected); bare strings in
   place of sequences and non-str values are rejected; message "invalid id in vector filter" (never
   echoes the value). Validation happens before the store factory is even called.
   Row fields (upsert) validated the same way (+ content_hash/model 1..256 chars, vector finite,
   non-zero, (1024,)) -> ToolInputError("invalid vector row"); last row per memory_id wins.
5. Limits: search limit int 1..200 (bool rejected). list_ids limit int 1..1000, NOT 200: U07-96
   maintenance step 5 pages `vectors.list_ids(after, 1000)`; capping at 200 would break that caller.
   `after=""` is the start sentinel (U07-97 health calls list_ids("", 1)); any other `after` must match
   MEMORY_ID_RE.
6. Freshness: the table handle is cached, but reads call `table.checkout_latest()` first (other
   handles — VectorStore.delete_ids opens its own — and other processes write too; without it a
   deleted row was still visible). Writes serialize on an instance threading.Lock; reads take no lock.
7. `metric("cosine")` is written as `distance_type("cosine")` (lancedb 0.39 documents metric as an
   alias of distance_type); `_distance` is selected explicitly (avoids lance's autoprojection warning).
8. Query vector for search is normalized (cosine is scale-invariant); a zero/non-finite/misshaped query
   -> ToolInputError("invalid query vector"). Empty layers or statuses -> [] without calling LanceDB.
9. list_ids scans matching rows' 4 metadata columns and sorts in Python (LanceDB has no ORDER BY);
   cost O(rows after `after`) per page.

## Concerns
- store.py sits at 259/260 lines: the module was compacted hard (allowlist regexes, itertools.batched,
  one-line docstrings) to fit; any future addition needs a budget ruling.
- list_ids paging is O(n) per page (O(n^2/1000) for a full maintenance sweep); fine for thousands of
  items, worth revisiting if memory grows to 100k+ vectors.
- list_ids limit cap 1000 differs from the sub-controller's "limit <= 200" ruling for search (see 5).

## Evidence
(filled in below)
- RED: `pytest tests/unit/harness/memory/test_memory_store_adapters.py tests/security/test_st07_vectors.py`
  -> 2 collection errors, `ModuleNotFoundError: No module named 'herness.harness.memory.store'`.
- GREEN: same command -> 73 passed (UT07-22 + UT07-23: 59 incl. params; ST07-09: 14 incl. params).
- Coverage store.py: 100% line (193/193), 100% branch (46/46).
- `pytest tests/unit/harness/memory -q -p no:logging` -> 242 passed.
- ruff format/check clean; mypy (strict) clean on the 3 touched files; lint-imports 13 kept 0 broken;
  check_type_ownership exit 0; check_module_size exit 0 (store.py 259/260).
- Pre-commit hooks (incl. pytest-unit, detect-secrets, module-size) all passed on the staged set.
  detect-secrets flagged the Crockford alphabet constant in the unit test; marked inline
  `# pragma: allowlist secret` (baseline untouched).
- Also touched: tests/unit/harness/test_tools_recording.py (one `_NON_ROW_HASHES` line, see Built).

## Commit
- 82b3258 feat(memory): embedding and vector adapters (T07-07) — all pre-commit hooks passed (no wip commit landed: the first attempt failed on detect-secrets, the retry lost its message file, so the work went straight into the final commit).

## Fix round 1 (review Approved with Minors; m2/m5/m6 parked)
- m1 (store.py `_rows_table`): upsert now writes the already-checked float32 unit vector
  (`_unit_vector(row.vector)`, computed in float64 after peak rescale) instead of a float32 cast of
  the raw input, so a finite float64 vector beyond float32 range (1e300) can no longer be written as
  inf. Chosen behaviour: store finite (not reject) — cosine is scale-invariant, U07-48 vectors are
  already unit-norm (so stored values are unchanged for them), and the row check already accepted the
  input as finite and non-zero. VectorRow docstring says "stored unit-norm". New test
  `test_ut07_23_out_of_float32_range_row_stored_finite` (1e300/-1e300 components -> stored
  [.., 0.7071, -0.7071, ..], norm 1, search distance 1 - 0.7071).
- m3: `test_ut07_23_upsert_search_roundtrip` asserts an orthogonal hit has cosine distance ≈ 1.0
  (L2 would give 2).
- m4: ST07-09 attack list adds fullwidth-digit and Arabic-Indic-digit ids, Cyrillic "аctive",
  "ACTIVE", "Semantic", "active\n", "active\x00", NUL inside and after an id; all rejected with
  ToolInputError before the spy store factory is called (spy.calls == 0), for every op.
- Gates: card tests 83 passed; tests/unit/harness/memory 243 passed; store.py coverage 100% line /
  100% branch; ruff, mypy (3 files), check_module_size exit 0; store.py 260/260 lines.
