# T03-25 review — Cluster stage (w22-s03, verify of c813be8)

**Verdict: Needs fixes** (1 Important, 6 Minor). The design, ids, restartability, streaming, gauges and no-pickle handling are solid. The blocker is how CUDA OOM is handled in the incremental and rerun paths.

### Spec Compliance
- U03-103 ClusterSnapshot ✅ (atomic dot-temp + os.replace, snapshot.json last, CURRENT only in mark_final, allow_pickle=False on npz/npy, *.pkl/*.bin/*.pt refused in the snapshot and version dirs, ConfigError names the file only)
- U03-104 is_full_recluster_due ✅ (order forced > no_snapshot > algorithm_changed > age > drift; DRIFT_MIN_NEW=500)
- U03-105 run_cluster_stage ❌ partial. Every algorithm step is present (see below), but the Errors row "CUDA OOM at the minimum chunk -> FatalError" is met only inside full_recluster (I-1).
- U03-106 finalize_clusters ✅ (names applied; auto-named root cause by member majority with ties ascending, NULL with no root_cause question; full run: named centroids, then mark_final, CURRENT last)
- UT03-98 ✅ · UT03-99 ✅ · UT03-100 ✅ · UT03-101 ✅ · IT03-10 ✅ · IT03-11 ✅ · IT03-12 ✅ (ARI >= 0.80 asserted; 12x50 planted + 60 noise, seeded) · ST03-18 ✅. Every test carries its ID; pytestmark is unit/integration as specified.
- ⚠️ IT03-10/11/12 run on a hand-built warehouse and a local planted generator rather than small_build/T2/T2c. This is a sub-controller ruling; carry over to spec 11.
- ⚠️ Lake layout row (impl 03 line 3047) says `centroid FLOAT[1024]`. On disk, centroid/named_centroid are variable lists because pyarrow cannot read back a null fixed-size list; they are cast back on read. Not yet recorded as a spec note (see M-4).
- ⚠️ Edge window: mark_final writes snapshot.json as final, then replaces CURRENT. A crash between those two writes leaves a `final` snapshot for build_id that is not CURRENT. load_assigned skips it, so a cluster-stage rerun would not resume it. This is the spec's own ordering and only matters if the stage, not resolve, is rerun. Flag for T03-28 stage checkpointing.

### Focus checks (evidence)
1. **Determinism ✅.** UT03-100 deterministic test: two independent runs with the injected id factory give identical memberships, prototypes and fit_id. Mutation probe (k-means seed varied per call) turns that test RED.
2. **Ids only via match_cluster_ids ✅.** `_cluster_full.py:88-89` `"cl_"+new_ulid()` is the only minting and is passed as `new_id=` at `:187`. No other `cl_`/new_ulid in the enrich cluster code. The incremental path asserts `issued == []`.
3. **Restartable ✅.** members.parquet, then save() (snapshot.json last), then a yield check (`_cluster_full.py:144-151`), all before any warehouse write. The rerun resumes from status `assigned` with the window pinned to created_at (`cluster_stage.py:164-174`). `_write` does DELETE+INSERT in one transaction, so a rerun cannot duplicate rows. Mutation probe (save() also calls mark_final) turns 4 tests RED, including the os.replace order spy in UT03-101.
4. **Bounded memory ✅.** `VectorSource.batches` uses `to_batches(chunk)`. Only 64-d projections are concatenated. The PCA sample is capped at pca_sample. Centroids take a second streamed pass (generator into compute_centroids). Incremental `x @ prototypes.T` is chunked by CHUNK.
5. **Gauges ✅.** `record_gauge("herness_enrich_clusters_count"|"herness_enrich_cluster_drift_ratio", component="enrich")`. Spec §metrics lists no labels.
6. **ST03-18 ✅.** Probe forcing allow_pickle=True in `_cluster_io` makes 4 tests RED (both ST03-18 payload tests). Probe disabling `_refuse_pickles` makes 6 ST03-18 tests RED. The canary proves no payload ran.
7. **No ticket text ✅.** Logs carry counts, ids and file names. Warehouse errors are re-raised as `type(exc).__name__` with `from None`. ConfigError messages carry the file name only.
8. **Coverage of rows ✅.** All rows are covered (see Spec Compliance).
9. **Gates ✅.** ruff check clean; format clean (817); mypy 0/307; lint-imports 13 kept 0 broken; check_type_ownership 0; check_module_size 0. `pytest tests/unit/enrich tests/integration/enrich`: 947 passed, 2 skipped (both unrelated: symlink privilege, laya CUDA). Coverage with branches: cluster_stage 100%, _cluster_io 96%, _cluster_full 99%.
10. **Builder concerns:**
   - (2) Recomputing step 4 on a rerun is **acceptable**: step 4 output is not persisted, and finalize needs the naming candidates.
   - (3) No or unattachable prev_warehouse leading to a full run (reason `no_prev_warehouse`, a log value only) is **acceptable**: change detection needs prev.enrich.text_redacted, and ids are still inherited.
   - (4) No chunk halving is a **defect**. The spec Errors row and the `cluster.project` docstring ("caller halves chunk") require it. Worse, the incremental and rerun paths do not convert OOM at all (I-1).
   - (5) ConfigError on too few vectors is **acceptable** as an interim, with the T03-28 carry-over: the spec is silent and the fresh-install policy is T03-28's. This matters for small pilot customers, so keep it visible in the carry-over.

### Strengths
- Clean split. Streaming by window position makes results independent of LanceDB scan order. The rerun pins `now`. The single-transaction write is idempotent. The tests assert real behaviour, including the replace-order spy, the `issued == []` id invariant, and an ARI check with noise counted against the score.

### Issues
#### Critical
- none

#### Important
- **I-1. CUDA OOM handling is incomplete** (`herness/enrich/cluster_stage.py:172`, `herness/enrich/cluster_stage.py:267`, `herness/enrich/_cluster_io.py:318-330`, `herness/enrich/_cluster_full.py:97-111`).
  - Only full_recluster converts `torch.cuda.OutOfMemoryError`. The rerun projection (`cluster_stage.py:172`) and the incremental projection (`_assign_new`, `:267`) let a raw torch OOM escape, outside the error taxonomy.
  - No path halves the chunk before failing, although the spec says "OOM at the minimum chunk -> FatalError" (U03-105 Errors, F03-10 line 3195).
  - Fix: in `VectorSource.project`, on OOM call `release_cuda()` and retry `project(vectors, chunk=c // 2)` down to a module minimum (e.g. 4,096). At the minimum raise `FatalError("cluster stage: cuda out of memory")`. All three paths then share it; keep the k-means OOM conversion as is.
  - Add a UT03-100 case: project raises OOM once, then succeeds at half the chunk. Add another: the incremental path with OOM at the minimum raises FatalError.

#### Minor
- **M-1.** `herness/enrich/_cluster_full.py:88-89`: the real id factory is never exercised, because every test injects fixed_ids (coverage miss at line 89). Fix: add one test without fixed_ids that asserts each cluster_id matches `^cl_[0-9A-HJKMNP-TV-Z]{26}$`.
- **M-2.** `herness/enrich/cluster_stage.py:181-187` together with `:218-223` and `:295-301`: a previous warehouse can attach yet lack the enrich tables, or be from an older schema. Then `_CHANGED_SQL`/`_KEPT_SQL` raise a raw `duckdb.Error`. Fix: catch `duckdb.Error` around `_try_incremental`'s SQL, log `enrich.cluster.prev_unavailable` (error type) and return `"no_prev_warehouse"` so a full recluster follows.
- **M-3.** `herness/enrich/cluster_stage.py:78-79`: `_SAME` does not restrict `p.entity = 'incident'`. Fix: add `AND p.entity = 'incident'` so a same-id row of another entity can never mask a change.
- **M-4.** `herness/enrich/_cluster_io.py:155-169`: centroid columns are stored as variable lists, while the layout row says FLOAT[1024]. Fix: add a spec note to the impl 03 lake layout row, or under DD-10 (line 4386).
- **M-5.** No test reruns the stage after the warehouse write, i.e. the same build_id twice before finalize. Fix: add a UT03-100 case that runs `run_stage(wh, paths, "b-0001")` twice and asserts member and cluster row counts are unchanged and no new ids are issued.
- **M-6.** `herness/enrich/_cluster_io.py:177`: `ConfigError(...) from exc` chains the original numpy/pyarrow exception, whose message holds the full path. The ConfigError text itself is clean. Fix (optional, consistent with `_write`): `from None`, or keep the chain and confirm that the error logger never renders `__cause__` text.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Everything else matches U03-103..106 and the test rows, and the mutation probes confirm the tests bite. The exception is the U03-105 OOM Errors row: it is only partly met, and two projection paths can leak a raw torch OOM. Fix I-1 (with tests); the Minor items can ride along.

---

## Re-review round 1 (head fb11c4a; changes in 00c1f48)

**Verdict: Approved.** I-1 and M-1..M-6 are closed. The fix introduced no new defect.

### Findings
- **I-1 ✅ Closed.**
  - `herness/enrich/_cluster_io.py:320-338`: `VectorSource.project` catches `torch.cuda.OutOfMemoryError`, calls `release_cuda()` and halves the chunk. The halved chunk carries over to later batches. Once the chunk would drop below `MIN_CHUNK = 4_096` it raises `FatalError(...) from None`. That is "OOM at the minimum chunk -> FatalError": the 4,096 attempt is made, and the next halving fails.
  - All three paths call it: rerun (`cluster_stage.py:177`), incremental (`_assign_new`) and full (`_cluster_full.py:101`). The k-means OOM conversion is unchanged.
  - Tests: halving retry (64 -> 32, identical result, one release) and the fatal case for full, incremental and rerun (chunks 64, 32, 16, no cause, no rows).
  - Mutation probe `mut_nohalve` (first OOM fatal, no retry): **4/4 new OOM tests RED.**
- **M-1 ✅ Closed.** `test_ut03_100_new_cluster_ids_are_ulids` runs the real factory and checks `^cl_[0-9A-HJKMNP-TV-Z]{26}$`. `_cluster_full.py` is now at 100% coverage.
- **M-2 ✅ Closed.**
  - `cluster_stage.py:80-84` and `:210-224`: `LIMIT 0` column probes run on the three previous enrich tables after ATTACH. On `duckdb.Error` the stage runs `DETACH DATABASE IF EXISTS`, logs the error type and returns False. That leads to a full run with reason `no_prev_warehouse`.
  - The test asserts a full run with inherited ids, and that the previous warehouse is no longer attached.
  - The mutation that removes the probes turns the test RED.
- **M-3 ✅ Closed.** `cluster_stage.py:78-79`: `p.entity = 'incident'` is added. The test inserts a `problem` row with the same id and hash, and asserts the incident is still treated as changed. The mutation that drops the filter turns it RED.
- **M-4 ✅ Closed.** The spec note sits in place in the impl 03 lake layout row (`clusters/<algorithm_version>/<snapshot_id>/`).
- **M-5 ✅ Closed.** `test_ut03_100_rerun_after_warehouse_write_is_idempotent` runs the same build twice before finalize. Members, clusters and row counts are equal, and no id is issued.
- **M-6 ✅ Closed.** `_cluster_io.py:177-179`: `_read` raises `ConfigError(..., file=name) from None`.

### Builder concerns 2 and 3 (from the original report)
- **Concern 2 (rerun recomputes step 4):** still **acceptable**. Nothing in step 4 is persisted, and finalize needs the naming candidates. M-5 now proves the rerun is idempotent.
- **Concern 3 (no or unusable prev warehouse leads to a full run):** still **acceptable**, and now also covers a warehouse that attaches but lacks the enrich tables (M-2). Ids are still inherited through `match_cluster_ids`.

### New defects
None.
- Line budgets are kept: `cluster_stage.py` 388/390, `_cluster_io.py` 377/400.
- Spherical k-means OOM is still fatal without halving. k-means takes no chunk argument, so that matches the spec ("OOM at the minimum chunk").

### Gates (run on fb11c4a)
- `ruff check .`: clean.
- `ruff format --check .`: 818 files formatted.
- `mypy`: 0 issues in 307 files.
- `lint-imports`: 13 kept, 0 broken.
- `check_module_size`: 0. `check_type_ownership`: 0.
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich tests/integration/enrich -q -p no:logging`: **955 passed, 2 skipped**. Both skips are unrelated: symlink privilege, and the laya/CUDA test.
- Branch coverage: `cluster_stage` 100%, `_cluster_io` 97%, `_cluster_full` 100%.

**Task quality:** Approved
