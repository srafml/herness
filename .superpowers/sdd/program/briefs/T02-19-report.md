# T02-19 report: Enrich and score stages, attach SQL

Worktree D:\herness\.claude\worktrees\agent-abefa7ddb7263a41d, branch worktree-agent-abefa7ddb7263a41d, base 667fcf4.

## What was built
- U02-100 `_stage_enrich` -> `herness.model._build_stages.stage_enrich`: opens the build connection, `update_build_row(clear_finished=True)` (always; a no-op in effect unless a completed unpromoted build is resumed), prev = `build_path(CURRENT)` when CURRENT names another build else None, calls `run_enrichment(con, build_id, depth=, ctx=, prev_warehouse=, stages=[enrich_stage] or None, llm_factory=run.llm_factory)` with no GPU scope (R-43), `YieldRequested` -> "yield" (enrich not in stages_done, state saved by `_run_stages`), stores `report.model_dump(mode="json")` under result `enrich`, `run_sql_range(300, 399)` (a yield between files -> "yield"), CHECKPOINT.
- U02-101 `_stage_score` -> `stage_score`: build connection, loads the scoring hook first (fail fast before the ~83 s facts step), `materialize_facts(con, build_id)` with no open transaction, CHECKPOINT, `run_scoring(build_id, steps=payload.score_steps, con=con, ctx=ctx)` on the same connection, result keys `facts_queries` (= len(query_ids); key chosen here, spec says only "store len(query_ids)") and `scoring` (report JSON), CHECKPOINT.
- U02-124 `herness/model/sql/300_attach_decisions.sql`: temp table of live IDs (union of record_id of core.incident/change/problem/work_item), `stg.build_counts` row `enrich_pruned` (delete-own-row then insert, idempotent), anti-join deletes of enrich.text_redacted/decision/cluster_member (record_id) and incident_change_link (incident_id or change_id), NULL IDs never live; UPDATE core.incident.content_hash from enrich.text_redacted; `CREATE VIEW IF NOT EXISTS enrich.decision_wide`. Static statements only (ST02-16/TH02-16).
- U02-125 `herness/model/sql/310_attach_clusters.sql`: `cluster_member_orphans` count (NULL cluster_id counts as orphan) then delete.
- U02-126: unchanged behaviour: `run_sql_range` already rejects 400-499; score stage runs 400 only through `materialize_facts` (IT02-26 asserts the runner never renders 400_facts.sql; a real-hook test runs 400 on lake_small).
- U02-134 `make_build_pipeline_handler(*, llm_factory)` in `_build_stages`, re-exported as `herness.model.build.make_build_pipeline_handler` (a `Final` alias; same object). The closure resolves `build.run_build_pipeline` at call time (lazy import).

## Seams / design
- New private sibling `herness/model/_build_stages.py` (ruling w18-s02). `build` imports it at module level; it imports `build` only inside functions (and under TYPE_CHECKING) -> no module-level cycle.
- Loader seams `_load_run_enrichment` / `_load_run_scoring` (`# T03-28:` / `# T04-13:`) use `_hook(module, name, stage)`: importlib + getattr; ModuleNotFoundError for that exact module or a missing attribute -> `ConfigError("build stage <name> is not available")` (goes through `_run_stages` failure path: status failed, `model.build.failed`, metrics); a ModuleNotFoundError of another module (a broken dependency of an installed hook) propagates. Local Protocols `_RunEnrichment`, `_RunScoring`, `_Report` type the hooks.
- `YieldRequested` lazily from `herness.enrich.gpu` (`# T03-04:` marker). `materialize_facts` lazily imported for real.
- `llm_factory` stays `object | None` (`# T03-28:`), per ruling.
- build.py change: new `_prepare(run)` (lake scan + render context + SQL files, extracted from `_stage_build`); `_sql` calls it when `run.context` is None. Needed because a resumed run (stage build done by an earlier run: yield resume, payload `build_id`) had no render context and the T02-18 `assert run.context is not None` fired. A resumed run therefore rescans the lake (cheap: inventory only).
- Layers contract: `ignore_imports` += `herness.model._build_stages -> herness.enrich.gpu`, `-> herness.metrics.facts` (comment cites impl 02 §2.2 exception). UT00-58 (tests/unit/repo/test_import_contracts.py) pinned the ignore list to the settings exception only; extended it to expect these two lines when `_build_stages` exists.

## Files and line counts
- herness/model/_build_stages.py 169 (budget 200, new §2 row)
- herness/model/build.py 396 (budget 400)
- herness/model/sql/300_attach_decisions.sql 53, 310_attach_clusters.sql 15
- docs/impl/02-data-model.impl.md: §2 module-map row for `_build_stages.py` (none (private), L2, duckdb, 200); §2.2 exception row notes the sibling.
- pyproject.toml: two ignore_imports lines.
- tests/unit/model/test_model_build_handler.py (UT02-78 + seam tests), tests/integration/model/test_model_build_enrich_score.py (IT02-23..26, ST02-16), tests/integration/model/test_model_build_pipeline.py (payload-error test now uses `dq` as the unavailable stage), tests/unit/repo/test_import_contracts.py (UT00-58 exception list).

## Spec notes / deviations
- `clear_finished` is issued unconditionally at enrich start (same effect as "when resuming a completed unpromoted build"; one UPDATE). Score does not clear (spec step 1 is only "ensure the connection").
- Scoring hook resolved before `materialize_facts` (spec order imports it at step 4): fail fast, no behavioural difference on success.
- Facts-count result key named `facts_queries` (spec: "store len(query_ids)", no key given).
- `enrich_pruned` after a re-run of 300 counts only the rows pruned in that run (0 if nothing new) - the row is replaced, never duplicated.
- ST02-16: covered stg (every stg table with a record_id column except the `stg.deleted_record` reference table, which holds the ID by design), core, and all enrich tables incl. decision_wide, with enrich rows carried forward from the previous (CURRENT) build. Gap: the vector store (impl 03 embeddings) is not part of the build file and not buildable here; lake purge is impl 10's (lake rows still present, filtered by `stg.deleted_record`).

## Tests
RED: `uv run pytest tests/unit/model/test_model_build_handler.py tests/integration/model/test_model_build_enrich_score.py` -> ImportError: cannot import name 'make_build_pipeline_handler' from 'herness.model.build'; after wiring, 4 failures from the resumed-run render context (AssertionError in `_sql`) -> fixed via `_prepare`.
GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/model tests/integration/model -q -p no:logging` -> 302 passed, 1 skipped. Coverage (branch): _build_stages 100 %, build.py 99 % (line 100 %), _build_support 99 %.
Gates: ruff format/check clean; mypy (273 files) clean; lint-imports 13 kept 0 broken; check_module_size 0; check_type_ownership 0.

## Carry-overs
- T03-28 / T04-13: replace the loader bodies with plain lazy imports (+ ignore_imports lines `-> herness.enrich.pipeline`, `-> herness.metrics.scoring`), type `llm_factory` as `LlmFactory`; the test `test_ut02_78_hook_loaders_until_hooks_land` adapts automatically.
- T03-04: import `YieldRequested` from `herness.enrich.pipeline`; change the ignore line accordingly (and UT00-58).
- T02-21: a yielded enrich leaves `finished_at` NULL (cleared at start) -> an orphan for `delete_orphans` unless protected by state/payload (same as T02-18 build-stage yields).
- Vector-store part of ST02-16 belongs to impl 03 / impl 10.

## Commits
- 085bfce wip(T02-19): enrich and score stages, attach SQL, handler factory (pre-commit incl. pytest-unit hook passed; first attempt failed on UT00-58 pinned ignore list, fixed)
- 54e8701 feat(model): enrich and score build stages, attach SQL (T02-19) (build.py docstring pointer to `_build_stages`; all 17 hooks passed, no SKIP)
Status: DONE_WITH_CONCERNS (build.py 396/400 after the `_prepare` extraction; UT00-58 test edited for the §2.2 exception; ST02-16 vector-store part not buildable here).

## Fix round 1 (base 54e8701 -> 336e84d)
- M2: 300_attach_decisions.sql header and build_counts comment (and the same wording in 310_attach_clusters.sql) now say a re-run leaves the same enrich/core rows and replaces the count row with that run's own count (0 when nothing new was pruned). No behaviour change.
- M3: `_hook` now guards only the name lookup: `import_module` runs in its own try (ModuleNotFoundError for exactly that module -> ConfigError; any other import error, including an AttributeError raised during import, propagates); `getattr` alone is wrapped for AttributeError -> ConfigError. New test `test_ut02_78_hook_import_attribute_error_propagates`.
- Gates: ruff format/check clean, mypy 273 files clean, lint-imports 13 kept 0 broken, check_module_size 0; `tests/unit/model tests/integration/model`: 303 passed, 1 skipped; `_build_stages` 100 % line and branch. Commit 336e84d, all 17 hooks passed, no SKIP.
- Parked per reviewer: M1, M4, M5, M6.
