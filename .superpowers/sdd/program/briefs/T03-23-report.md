# T03-23 report — Clustering numerics (build agent)

Status: DONE_WITH_CONCERNS (minor spec notes, below)
Worktree/branch: D:\herness\.claude\worktrees\agent-ac157186da94f1c61 / worktree-agent-ac157186da94f1c61 (base c3eee74)
Commits: ca37731 wip(T03-23): clustering numerics and tests green; 72ab1ef feat(enrich): clustering numerics (T03-23) (empty marker commit; code is in ca37731)

## Built
- herness/enrich/cluster.py (300 lines, budget 390): PcaModel, KMeansResult (frozen dataclasses, local to the module
  per §2 module map; not in the spec's shared-types list, check_type_ownership exit 0), fit_pca (U03-90), project (U03-91),
  spherical_kmeans (U03-92, private helpers _kmeans_pp_init/_lloyd_pass/_update_prototypes), hdbscan_prototypes (U03-93),
  assign_members (U03-94), prune_clusters (U03-95). torch imported lazily in functions (TYPE_CHECKING import for hints);
  sklearn silenced locally with `# type: ignore[import-untyped]` (pyproject untouched).
- tests/unit/enrich/test_cluster.py (19 tests, marker unit, all device="cpu"): UT03-85 x6, UT03-86 x5, PT03-11 (hypothesis,
  25 examples), UT03-87, UT03-88 x3, UT03-89 x3.
- No import-linter / pyproject changes needed (herness.enrich already covered by the layer contracts).

## Spec notes / deviations
1. assign_members raises ConfigError when full_sim <= assign_min_sim. Spec lists the precondition but "Errors: none"; the check
   guards a division by zero (would produce inf/nan silently). Tested in UT03-88.
2. PcaModel/KMeansResult use `dataclass(frozen=True, eq=False)`: ndarray fields make default __eq__ raise; spec says only "frozen dataclass".
3. k-means++ weights draws by D^2 with D = 1 - cos (standard k-means++; spec says "distance 1 - cos"). Sample size is
   max(min(init_sample, n), k) so k seeds always fit when init_sample < k (spec: min(init_sample, n)). Seeds drawn on device via
   cumsum+searchsorted with the numpy rng (default_rng(seed)) for reproducibility; all-zero distances fall back to a uniform draw.
4. "Empty" prototype also includes a vanishing member sum (norm <= 1e-12), so the unit-norm invariant always holds.
5. hdbscan_prototypes passes copy=True explicitly (silences sklearn 1.9 FutureWarning; never mutates input).
6. device is typed `str` (spec leaves it untyped; settings use Literal["cuda","cpu"]).
7. PT03-11 checks argmax with a 1e-5 tolerance (torch vs numpy fp rounding on near ties) plus counts == bincount(assign).
8. UT03-86 forced empty case monkeypatches the private `_kmeans_pp_init` to return a duplicated seed.

## Carry-overs
- None for this card. Cluster stage card (U03-96+) wraps project/spherical_kmeans in U03-22 OOM backoff by halving chunk.

## Evidence
- RED: `uv run pytest tests/unit/enrich/test_cluster.py` -> ImportError: cannot import name 'cluster' from 'herness.enrich'.
- GREEN: tests/unit/enrich/test_cluster.py 19 passed; coverage herness/enrich/cluster.py 100% line, 100% branch.
- tests/unit/enrich: 378 passed, 1 skipped (symlink privilege).
- Gates: ruff check/format clean; mypy Success (196 files); lint-imports 13 kept 0 broken; check_module_size exit 0;
  check_type_ownership exit 0; pre-commit hooks (incl. pytest-unit) passed on the checkpoint commit.
- Module line counts: herness/enrich/cluster.py 300/390.

## Fix round 1
- M1: `project` now casts `pca.components` / `pca.mean` to float32 via `_to_device` (unused torch import removed); new test
  test_ut03_85_project_accepts_float64_pca_model. Card tests 20 passed, cluster.py 100% line/branch, 299/390 lines; ruff, mypy clean.
  Commit: fix(enrich): cast PCA model to float32 in project (T03-23).
