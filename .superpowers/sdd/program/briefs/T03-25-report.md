# T03-25 report — Cluster stage (w22-s03, re-dispatch of w21-s03)

Status: DONE_WITH_CONCERNS — c813be8
Worktree: D:\herness\.claude\worktrees\agent-aac28d982f57551bb · branch worktree-agent-aac28d982f57551bb · base 3f61b67

## Implemented
- U03-103 `ClusterSnapshot` / `SnapshotMeta` (frozen dataclasses; not listed in core.types): load_current, load,
  save (all files but CURRENT, snapshot.json last), save_centroids, mark_final (snapshot.json final, then CURRENT);
  plus load_assigned / save_members / load_members for crash reruns. Atomic dot-temp + os.replace; np.load
  allow_pickle=False; *.pkl/*.bin/*.pt in snapshot or version dir refuse the load; ConfigError names the file only.
- U03-104 `is_full_recluster_due` (forced, no_snapshot, algorithm_changed, age, drift; DRIFT_MIN_NEW=500).
- U03-105 `run_cluster_stage`: rerun of an `assigned` snapshot for build_id (resume from members.parquet, window
  pinned to the snapshot's created_at); incremental (ATTACH prev warehouse READ_ONLY, changed = (record_id,
  content_hash) not in prev text_redacted, streamed vectors -> project -> nearest prototype in CHUNK blocks ->
  assign_members, drift share gauge, cadence re-check, copy kept memberships + prev cluster rows, recompute
  descriptors); full (PCA reuse / fit on lowest-sha256 sample, streamed projection, k clamp, spherical k-means,
  HDBSCAN, assign, prune, second streamed pass for centroids, match_cluster_ids with new_id "cl_"+new_ulid,
  centroid table with carried names, members.parquet + assigned snapshot, heartbeat per phase, yield after save);
  step 4 descriptors + c-TF-IDF over <=2,000 texts/cluster by sha256(cluster_id||record_id) + naming candidates;
  step 5 one-transaction DELETE+INSERT of enrich.cluster / enrich.cluster_member. Gauges
  herness_enrich_clusters_count, herness_enrich_cluster_drift_ratio via record_gauge. Logs counts only.
- U03-106 `finalize_clusters`: one UPDATE applies names, fills auto-named root cause by member majority
  (ties answer ascending; NULL without a root_cause question), full run -> named centroids saved, mark_final.

## Files (lines / budget)
- herness/enrich/cluster_stage.py 379/390
- herness/enrich/_cluster_io.py 364/400 (new private sibling, §2 row added)
- herness/enrich/_cluster_full.py 287/300 (new private sibling, §2 row added) — see concern 1
- docs/impl/03-enrichment.impl.md (two §2 rows), .secrets.baseline (line shift only)
- tests/unit/enrich/_cluster_support.py 296, test_cluster_stage.py 200 (UT03-98/99), test_cluster_stage_run.py 438
  (UT03-100/101), security/test_cluster_stage_security.py 113 (ST03-18), tests/integration/enrich/test_cluster_stage_flow.py 123 (IT03-10/11/12)

## Tests
- RED: collection failed `ImportError: cannot import name 'cluster_stage' from 'herness.enrich'` (UT03-98/99, ST03-18)
  before the module existed. UT03-100/101 and IT tests were written together with the stage (drafts from the
  previous attempt); their RED is the same missing-module state — not separately observed per test.
- GREEN: card tests 62 passed (UT03-98 15, UT03-99 15, UT03-100 16, UT03-101 4, ST03-18 9, IT03-10/11/12 1 each);
  `pytest tests/unit/enrich` + card integration: 932 passed, 1 skipped. IT03-12 ARI >= 0.80 asserted (12 planted
  clusters x 50 + 60 noise, seeded). ST03-18 includes a mutation check proving RED under allow_pickle=True.
- Coverage (card tests): cluster_stage 100 %, _cluster_io 96 %, _cluster_full 99 % (branch incl.).
- Gates: ruff check . clean, ruff format --check . clean, mypy 0 issues (307 files), mypy on test files clean,
  lint-imports 13 kept 0 broken, check_type_ownership 0, check_module_size 0.

## Rulings applied
report via module-private _Report Protocol (rows); no GPU lock / service start, device injected, cpu tests; yield =
snapshot + members saved then YieldRequested("cluster"); ids only via match_cluster_ids (new id injectable:
_cluster_full._new_cluster_id); naming via cluster_describe; SnapshotMeta/ClusterStageResult frozen dataclasses in
the cluster stage modules; gauges via record_gauge; IT03-12 seeded local generator, IT03-10/11 hand-built warehouse
+ tmp LanceDB; pyarrow null FixedSizeList -> variable lists on disk, cast back on read; streaming via
to_batches(CHUNK); allow_pickle=False; CURRENT last; enrich/__init__, embed_stage, embed, settings untouched.

## Concerns / deviations
1. TWO private siblings instead of ONE (ruling said one). Line accounting: the stage needs ~900 lines after
   compaction (snapshot IO + streaming ~360, full recluster + step 4 ~290, orchestration/incremental/finalize ~380);
   390 + 400 cannot hold it. Precedent: laya_trainer has two private siblings (_sft_loop, _train_ckpt; T03-31).
   Both rows added to docs/impl/03 §2 with spec notes. Needs a controller ruling (accept, or rule otherwise).
2. Crash rerun recomputes step 4 (descriptors, terms, naming candidates) instead of the literal "skip to step 5":
   finalize needs the naming candidates and enrich.cluster needs descriptors; costs one re-projection pass.
3. Incremental needs the previous warehouse: prev_warehouse None / not attachable -> full recluster (reason
   `no_prev_warehouse`); ids still inherited through match_cluster_ids.
4. CUDA OOM -> release_cuda + FatalError without chunk halving (fixed chunks 262,144 / 32,768).
5. Too few in-window vectors (k < max(min_cluster_size, min_samples+1), or PCA sample < pca_dims) -> ConfigError;
   empty-window / fresh-install policy belongs to T03-28.

## Spec notes (impl 03)
- Naming examples ranked in the 64-d projection space against the mean of the sampled members' projections
  (1024-d vectors are streamed, never held). NamingCandidate.service_names = service ids (no core.service lookup).
- finalize "auto-named" = NameResult source "auto", or (not renamed this run) label NULL or starting "auto: "; a row
  with no label gets `left('auto: ' || top_terms[1:3] joined ' / ', 60)`. Majority over enrich.decision
  question='root_cause', answer not NULL, no question_set_version filter.
- Incremental: previous clusters whose members all left the window keep a row with size 0 (spec: copy rows).
- Retired centroids stay in centroids.parquet (retired_at kept; newly retired get now) — cleanup is OI-13.
- Extra classmethod/methods on ClusterSnapshot: load_assigned, save_members, load_members, folder.
- Vector store opened at EnrichPaths.vectors_dir(); private import store.vectors._store_error (as embed_stage, M-3).
- Extra log events: enrich.cluster.resumed (n,k), enrich.cluster.snapshot_unusable (file name), enrich.cluster.
  prev_unavailable (error type). report.rows += member rows written.

## Carry-overs
- T03-28: retype _Report -> StageReport; JobOutcome(yield) mapping; decide policy for too-few-vectors ConfigError.
- spec 11: re-point IT03-10/11/12 to small_build and T2/T2c synthetic truths.
- Perf/GPU: OOM chunk halving for project/k-means (concern 4).

## Commits
- (a first wip attempt failed only on detect-secrets baseline line shift; nothing landed)
- final: c813be8 feat(enrich): cluster stage (T03-25) (recorded by sub-controller; builder killed by usage limit after commit)

## Fix round 1 (review T03-25-review.md; fix agent continuing c813be8)

Status: DONE. Commits: 00c1f48 wip(T03-25): fix1 ... (all code, docs, tests); final `fix(enrich): cluster stage review round 1 (T03-25)` (SHA below).

- I-1 CUDA OOM: `VectorSource.project` (`_cluster_io`) catches `torch.cuda.OutOfMemoryError` per batch,
  calls `release_cuda()`, retries at `chunk // 2` (kept for later batches) and raises
  `FatalError("cluster stage: cuda out of memory") from None` once the halved chunk drops below
  `MIN_CHUNK = 4_096` (module value; the spec names no floor). This one helper covers the rerun, incremental
  and full projections. The k-means OOM conversion in `_cluster_full` is unchanged.
  Tests: `test_ut03_100_projection_oom_halves_chunk_with_same_result` (OOM once at 64 -> retried at 32;
  identical members, clusters, prototypes; one release) and
  `test_ut03_100_projection_oom_at_minimum_chunk_is_fatal[full|incremental|rerun]` (chunks 64, 32, 16 then
  FatalError; no chained cause; no warehouse rows).
- M-1: `test_ut03_100_new_cluster_ids_are_ulids` (real factory, `^cl_[0-9A-HJKMNP-TV-Z]{26}$`).
- M-2: right after ATTACH, `_attach` runs `LIMIT 0` probes of the previous `enrich.text_redacted` / `cluster_member` /
  `cluster` columns the run reads. On `duckdb.Error` it runs `DETACH DATABASE IF EXISTS`, logs `enrich.cluster.prev_unavailable`
  (error type) and returns False, so a full recluster follows with reason `no_prev_warehouse`.
  Test: `test_ut03_100_previous_warehouse_without_enrich_tables_runs_full`.
- M-3: `_SAME` filters `p.entity = 'incident'`. Test: `test_ut03_100_other_entity_row_does_not_mask_a_change`.
- M-4: spec note added in place to the impl 03 §4.3 `clusters/...` layout row. It adds no lines, so
  `.secrets.baseline` is unchanged and detect-secrets passed.
- M-5: `test_ut03_100_rerun_after_warehouse_write_is_idempotent` (same build_id run twice: equal members/clusters,
  unchanged row counts, no id issued).
- M-6: `_read` raises `ConfigError(..., file=name) from None` (the numpy/pyarrow cause carried the path).

New tests are in `tests/unit/enrich/test_cluster_stage_recovery.py` (test_cluster_stage_run.py is already 438 lines).
RED (before the fix, MIN_CHUNK/release_cuda patched with raising=False): 6 failed, 2 passed. Failures: the halving test
saw `[64]` instead of `[64, 32, 16]` and a raw `torch.OutOfMemoryError`; `DID NOT RAISE FatalError`; M-2
`_duckdb.CatalogException: ... enrich.text_redacted does not exist`; M-3 `assert 'servicenow:incident:INC00000' not in {...}`.
M-1 and M-5 are coverage tests and pass by design.
GREEN: new file 8 passed. `pytest tests/unit/enrich tests/integration/enrich`: 955 passed, 2 skipped (both unrelated:
symlink privilege, laya CUDA). Branch coverage: cluster_stage 100 %, _cluster_io 97 %, _cluster_full 100 %.
Gates: ruff check clean; ruff format --check clean (818); mypy 0 issues (307 files, plus the new test file);
lint-imports 13 kept, 0 broken; check_type_ownership 0; check_module_size 0. The wip commit ran every hook (pytest-unit passed).
Lines: cluster_stage.py 388/390, _cluster_io.py 377/400, _cluster_full.py 287/300.
Final commit: fb11c4a fix(enrich): cluster stage review round 1 (T03-25). It is empty (--allow-empty) because all changes are in 00c1f48. It ran with hooks, and pytest-unit passed.
