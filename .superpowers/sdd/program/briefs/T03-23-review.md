# T03-23 review — Clustering numerics (verify agent)

Worktree D:\herness\.claude\worktrees\agent-ac157186da94f1c61, base c3eee74, head 72ab1ef (code in ca37731).
Re-run by reviewer: `pytest tests/unit/enrich/test_cluster.py --cov=herness.enrich.cluster --cov-branch` -> 19 passed,
cluster.py 131 stmts / 20 branches, 100 % line, 100 % branch, no warnings. ruff check + format --check clean; mypy (2 files) clean.
sklearn 1.9.1.

### Spec Compliance
- ✅ Spec compliant

| Row | Result | Notes |
|-----|--------|-------|
| U03-90 fit_pca | ✅ | randomized PCA with random_state=seed; components/mean float32 contiguous; `fit_id = sha256(components.tobytes()+mean.tobytes()).hexdigest()[:12]` exactly (cluster.py:80-82); m < dims -> ConfigError (cluster.py:76-78). |
| U03-91 project | ✅ | per-chunk `(x-mean) @ components.T` in fp32 on device, row norm clipped at 1e-12 (zero rows stay zero) (cluster.py:97-102, 297-300); OOM propagates (no try). |
| U03-92 spherical_kmeans | ✅ | default_rng(seed); k-means++ on a uniform sample; `iters` passes, each followed by update; empty -> distinct lowest-sim points, lowest first, in ascending empty-index order (cluster.py:218-219); a final pass after the last update produces assign/sim (cluster.py:139), so assign matches prototypes; prototypes unit norm (empties include vanishing sums); counts = bincount int64; assign int32, sim float32; k outside 1..n -> ConfigError. |
| U03-93 hdbscan_prototypes | ✅ | euclidean, eom, labels_/probabilities_ copied. |
| U03-94 assign_members | ✅ | member = label>=0 & sim>=assign_min_sim; prob = proto_probs[p] * min(1, (s-min)/(full-min)); noise -> -1, 0. |
| U03-95 prune_clusters | ✅ | bincount on non-noise, mask < min_incidents, lookup renumber in ascending original index. |
| UT03-85 | ✅ | stable fit_id, exact sha256 formula, float32 dtypes, unit rows, chunk invariance, zero row stays zero, m<dims error, empty input. |
| UT03-86 | ✅ | 3 blobs k=3 on cpu recovered (partition + counts 40/40/40); forced empty case (duplicate seed) re-seeded; determinism; k range errors. |
| UT03-87 | ✅ | 60 prototypes in 3 groups + 4 outliers -> labels {0,1,2} matching groups, outliers -1. |
| UT03-88 | ✅ | assign_min_sim=0.60, full_sim=0.85 (the §config defaults, impl 03 line 3530): 0.55 -> noise/0; 0.60 -> member, (0)/0.25 -> prob 0; 0.725 -> 0.125/0.25 = 0.5 -> 0.5·p; 0.9 -> min(1, 1.2) = 1 -> p. With p=0.8 expected [0, 0, 0.4, 0.8]; asserted, and idx [-1, 2, 2, 2]. Holds under float32 sims (0.6f = 0.60000002 >= 0.6). |
| UT03-89 | ✅ | clusters of 30 (idx 4) and 10 (idx 1), min 25 -> 10 become noise, 30 renumbered to 0; ascending-renumber case; all-noise and empty inputs. |
| PT03-11 | ✅ | hypothesis (25 examples, n<=40, k in 1..n, iters 0..3, chunk 1..16): assign is argmax (1e-5 tol), sim equals max, counts == bincount, unit prototypes. |

Conventions: every test name carries its ID (`test_ut03_85_…`, `test_pt03_11_…`), each docstring first line starts with the ID, module `pytestmark = pytest.mark.unit`. Module 300/390 lines. Only noqa: PLR0913 on spherical_kmeans (signature fixed by spec, 7 keyword params), PLC0415 on lazy torch imports (sub-controller ruling). EM101/EM102 respected (msg assigned before raise). Error messages carry only counts/params, no data.

- ⚠️ Cannot verify from diff:
  - CUDA path not exercised (tests force CPU per ruling). The code is device-agnostic (all tensors created with `.to(device)`, `index_add_`, `searchsorted` exist on CUDA); acceptance check is "tests pass on CPU", so accepted.
  - Scale limits (200k×1024 PCA sample, n=5M, k=20k) not measured here; memory shape of the chunked design matches the spec (chunk × k sims per pass; k-means++ sample m×64 held on device).
  - hdbscan_prototypes with k <= min_samples raises sklearn ValueError (spec "Errors: none"); the stage clamps k >= k_min (1,000), so unreachable in practice — owned by the cluster-stage card.

### Builder departures (report lists 8, not 9)
1. assign_members raises ConfigError when full_sim <= assign_min_sim — acceptable spec note (enforces the stated precondition; avoids silent inf/nan). Config already validates `assign_min_sim < full_sim`.
2. `eq=False` on the frozen dataclasses — acceptable (ndarray `__eq__` is ambiguous).
3. k-means++ D² weighting with D = 1 − cos; sample `max(min(init_sample, n), k)` — acceptable; D² is standard k-means++, and the `≥ k` clamp only matters when a caller passes init_sample < k.
4. Vanishing member sum counts as empty — acceptable; keeps the unit-norm invariant.
5. HDBSCAN `copy=True` — acceptable (sklearn 1.9.1 default-change warning; behaviour unchanged).
6. `device: str` — acceptable (settings use Literal["cuda","cpu"]; gpu_scope yields nothing device-typed).
7. PT03-11 1e-5 tolerance — acceptable (torch vs numpy float rounding on near ties).
8. UT03-86 monkeypatches private `_kmeans_pp_init` — acceptable for a "forced case".
None is must-fix.

### Strengths
- Numerics match postconditions exactly; last-pass-after-update ordering is correct and PT03-11 checks it as a property.
- Clean separation: public units thin, private helpers `_kmeans_pp_init/_lloyd_pass/_update_prototypes` each small.
- Tests check real values (exact UT03-88 probabilities, exact prune output, fit_id against an independent sha256), not just shapes.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
- herness/enrich/cluster.py:97-98 — `project` feeds `pca.components`/`pca.mean` to torch without a float32 cast. A PcaModel reloaded by ClusterSnapshot (U03-1xx) as float64 would make `(xc - mean) @ comp_t` fail with a dtype mismatch (loud, not silent). Cheap to harden via `_to_device(pca.components, device)`; alternatively the snapshot card must guarantee float32 on load.
- herness/enrich/cluster.py:165-169 — k-means++ draw: `total` (d2.sum) and `cum[-1]` (cumsum) can differ in the last ulp, and `rng.random()` may return 0.0; either edge can pick an index whose d2 is 0 (an already-chosen seed, via the `m - 1` clamp or left searchsorted). Probability negligible and the duplicate is repaired by empty re-seeding; comment-worthy at most.
- herness/enrich/cluster.py:139 — the final pass computes `sums` (index_add over all n) that are discarded; negligible cost (n×64 adds) vs the n×k matmul. Optional flag to skip.
- Commit hygiene — code lives in `ca37731 wip(T03-23)…` and `72ab1ef feat(enrich)…` is empty. Squash (or rely on the integration merge message) so history does not carry a `wip` commit as the only code commit.
- Builder report says "9 departures" in the dispatch summary but lists 8 (report accuracy only).

### Assessment
**Task quality:** Approved
**Reasoning:** All six units meet their postconditions (fit_id formula, float32 dtypes, zero-row handling, final-pass assignment, lowest-sim distinct re-seeding, assignment/prob formula, ascending renumbering), all test IDs are present with exact expected values, and gates (100 % line/branch, ruff, mypy, 300/390 lines) pass; remaining items are minor hardening.
