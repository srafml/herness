# T07-07 review (verify agent) — Embedding and vector adapters

Worktree agent-a2fc976cb383d6bf3, base 667fcf4, head 82b3258. Read-only review; probes ran from a D: temp dir (since removed).

### Spec Compliance
- ✅ Spec compliant (with the sub-controller rulings: list_ids limit 1..1000, search 1..200, `after=""` sentinel, lazy default embed_fn via the `herness.enrich` facade, local fixtures, one `_NON_ROW_HASHES` line).
  - U07-48 Embedder ✅: signature (embed_fn default resolved lazily, kw-only model_name, cache_size=2048), `embed`/`embed_item`/`model_name`. Algorithm steps 1–6 in order (sha256_hex key, lookup + move_to_end under lock, embed_fn outside lock, shape/float/finite check, float32 unit normalization, insert + evict under lock) store.py:57-76. Read-only arrays store.py:95. Error mapping store.py:66-71 (ConfigError/ToolInputError/ModelUnavailable propagate; OSError/RuntimeError/ValueError/MemoryError → "memory embedding failed"; bad output or zero norm → "memory embedding invalid output"). Nothing is cached on failure.
  - U07-49 VectorIndex ✅: all 7 methods plus VectorRow/VectorMeta frozen dataclasses. ensure_table via `ensure_tables()` + `table("memory_embedding")` store.py:176-181. upsert uses merge_insert("memory_id") with update-all/insert-all store.py:195-196. set_status runs update in 200-id chunks store.py:205-206. delete goes through `VectorStore.delete_ids` in 200-id chunks store.py:213-214. search uses cosine + prefilter `layer IN … AND status IN …` + a bounded limit store.py:220-234 (probe: orthogonal unit vectors give distance 1.0 and a scaled query gives 0.0, so the metric is cosine, not L2). vectors does 200-id chunked scans store.py:240-246. list_ids does `memory_id > after`, ordered, limit rows store.py:249-259. The schema comes from `MEMORY_EMBEDDING_SCHEMA` (checked by VectorStore._verify). Error mapping: OSError/ValueError/RuntimeError + VectorStore's SchemaViolation/StoreBusy/NotFound → `ModelUnavailable("memory vector store unavailable: <op>")` store.py:158-164. A write lock covers upsert/set_status/delete; reads take no lock and use checkout_latest.
  - UT07-22 ✅: repeat (cache hit, same object), overflow/LRU eviction, RuntimeError/OSError/ValueError/MemoryError, NaN/inf/zero/shape/int/list/None, proof the lock is not held during the model call, lazy default.
  - UT07-23 ✅ on a real tmp LanceDB: upsert/search/status/delete/vectors/list_ids paging, chunk counts (3 updates for 450 ids, [200,200,1] deletes), bad id → ToolInputError, error mapping per op.
  - ST07-09 ✅: quote / `--` / `OR 1=1` / trailing newline / truncated id / literal-set breakouts / non-str / bare string, across set_status (ids + status), delete, vectors, search (layers + statuses), list_ids(after) and upsert rows. SpyFactory proves the store factory (the only path to LanceDB) is never called.
- ⚠️ Cannot verify here: TH07-05 "callers pass redacted text" is the caller's job (U07-50 step 3 redacts before embed_item). store.py cannot enforce it and does not need to. It keeps no text: the cache is keyed by SHA-256 hex, the module has no logger, and error messages are fixed (only `error_type` context). TH07-13 is also a caller concern (final filter on SQLite rows, in the T07 recall/maintenance cards).

### Independent checks (all run in the worktree)
- `pytest tests/unit/harness/memory tests/security/test_st07_vectors.py tests/unit/harness/test_tools_recording.py -q -p no:logging` → 376 passed, no warnings.
- Coverage store.py: 100% line (193/193), 100% branch (46/46).
- ruff check clean; ruff format clean on the 3 files; mypy: no issues in 273 files; lint-imports 13 kept / 0 broken; check_module_size exit 0 (store.py 259/260).
- Injection probes (my script): a fullwidth-digit id, an Arabic-Indic-digit id, `ACTIVE`, Cyrillic `аctive`, `active\n` and `active\x00` were all rejected with ToolInputError for set_status ids/status, search statuses and list_ids(after). MEMORY_ID_RE uses explicit ASCII classes and fullmatch, so lookalikes and trailing-newline tricks cannot pass. set_status/vectors on 5,000 ids work (chunked). Writes on a stale handle after a delete through VectorStore's own handle did not bring the deleted row back. 6 concurrent upserts gave 30 rows and no errors.

### Strengths
- Validation happens before the store factory is touched, so "rejected before LanceDB" holds by construction, and the ST test proves it with a factory spy.
- Filter building lives in one place (`_filter_values` + `_in`), error text never echoes the value, and the allowlists for layer/status/kind come from the owning Literal types.
- Float64 peak-rescaled normalization avoids overflow; the cache and `vectors()` return read-only arrays.
- `checkout_latest` on reads is a good catch (the handle would otherwise miss deletes made through `delete_ids`' own handle).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. store.py:146 + store.py:153: upsert validates `_unit_vector(row.vector)` (float64 path) but writes `np.asarray(row.vector, dtype=np.float32)`. A finite float64 vector beyond float32 range (probe: `np.full(1024, 1e300)`) passes validation and is stored as `inf`. You get RuntimeWarning "overflow encountered in cast", `vectors()` returns non-finite data, and under the repo's `filterwarnings = error:::herness` it would surface as an uncaught RuntimeWarning. It is only reachable when a caller bypasses Embedder, but the check does not protect what it claims to. Fix: in `_rows_table`, compute `unit = _unit_vector(row.vector)` once per row, reject on None, and build `flat` from those unit vectors (cosine does not depend on scale, so storing the unit vector is harmless). Add a UT07-23 case with a 1e300 float64 vector.
2. store.py:179-181, 183-185: lazy `ensure_table()` is not guarded. A probe with 8 concurrent first reads created 8 VectorStore instances (the last one wins). This is harmless today (VectorStore tolerates the create race) but wasteful and racy on `_store`/`_table`. Fix: take a small init lock (or the write lock) around the `self._table is None` → ensure_table path (double-checked).
3. tests/unit/harness/memory/test_memory_store_adapters.py:263: the roundtrip asserts distance ≈ 0 for an identical vector, which L2 would also give, so nothing pins the cosine metric. Fix: also assert an orthogonal hit has distance ≈ 1.0 (L2² would be 2.0), and/or search with a scaled query.
4. tests/security/test_st07_vectors.py:16-27: ATTACKS has no unicode lookalikes or NUL. They are rejected (confirmed by probe) but no test pins it. Fix: add e.g. a fullwidth-digit id, `"аctive"` and `"active\x00"`.
5. store.py:31: the allowlist regexes are built with `"|".join(get_args(t))` and no `re.escape`. That is safe for the current `[a-z_]` literals but brittle if a literal ever contains a metacharacter. Fix: `"|".join(map(re.escape, get_args(t)))` (or use frozenset membership instead of a regex).
6. store.py:68, 164: `raise ... from exc` keeps the original exception as `__cause__`. Its message may contain the embed input or a LanceDB filter (ids) and would appear if a caller logs with exc_info. The ModelUnavailable message itself is clean. Acceptable (memory ids are not secret and embed_query errors carry no text); callers should be told not to log `__cause__` text.

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit, algorithm step, error mapping, limit and chunk size matches the spec (with the recorded rulings). Every injection class tried is rejected before LanceDB, cosine and prefilter are confirmed, and all gates pass with 100% coverage. What remains is Minor hardening; #1 is cheap to fix now if the 260-line budget allows.
