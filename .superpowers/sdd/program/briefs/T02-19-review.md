# T02-19 review (verify): enrich and score stages, attach SQL 300/310, make_build_pipeline_handler

Worktree agent-abefa7ddb7263a41d, head 54e8701, base 667fcf4. Verdict: **Approved** (no Critical or Important findings; Minor items only).

### Spec Compliance
- U02-100 `_stage_enrich` (`herness/model/_build_stages.py:97-129`): ✅ connection via `build._connection`, `clear_finished=True` (always, see Minor 1), prev = `build_path(CURRENT)` only when CURRENT is another build (`:89-94`), lazy hook, `run_enrichment(con, build_id, depth=, ctx=, prev_warehouse=, stages=[s] or None, llm_factory=run.llm_factory)`, no GPU scope (R-43), `YieldRequested` -> `"yield"` (not appended to `stages_done`; `_yield` saves state, build.py:325-328), `report.model_dump(mode="json")` under `enrich`, `run_sql_range(300,399)` (yield-aware), CHECKPOINT.
- U02-101 `_stage_score` (`_build_stages.py:132-151`): ✅ `materialize_facts(con, build_id)` with no open transaction (autocommit, fake BEGIN proves it), CHECKPOINT, `run_scoring(build_id, steps=, con=con, ctx=)` on the same connection (never closed/reopened), CHECKPOINT. Deviation (accepted): scoring hook resolved before facts (fail fast); count key `facts_queries`.
- U02-124 `300_attach_decisions.sql`: ✅ static SQL only; the one macro parameter is a literal column name from the template, no data-derived identifiers/text (TH02-16). Live IDs = union of 4 core tables; count then anti-join delete of text_redacted/decision/cluster_member (record_id) and incident_change_link (incident_id OR change_id); content_hash UPDATE after prune; `CREATE VIEW IF NOT EXISTS enrich.decision_wide` (checked: no error when spec 03 already created it, incl. as table). `enrich_pruned` delete-then-insert matches the 270 pattern -> one row on re-run.
- U02-125 `310_attach_clusters.sql`: ✅ count + delete orphans (NULL cluster_id counted); idempotent row.
- U02-126: ✅ runner never renders 400-499 (`run_sql_range` guard; IT02-26 asserts `400_facts.sql` not rendered; real hook test fills facts on lake_small).
- U02-134 `make_build_pipeline_handler`: ✅ one-arg closure, lazy `build.run_build_pipeline`, re-exported (`build.py:279`, same object; verified importable, fresh-interpreter import of `_build_stages` then `build` works, no cycle; `herness.enrich.gpu` / `herness.metrics.facts` not loaded at import).
- IT02-23 ✅ (lake_small; live/dead + dead change link; count 5; re-run idempotent + finished_at cleared) · IT02-24 ✅ · IT02-25 ✅ (args incl. prev path, `["link"]`, same factory; `gpu_requests`/`gpu_scopes` empty, class `none` during 300-399; yield -> `yield`, enrich not in stages_done, resume; yield between 300/310) · IT02-26 ✅ (order, same con, one `open_for_build`, score-only on existing build `create=False`, real facts hook) · UT02-78 ✅ · ST02-16 ✅ for stg/core/enrich with enrich rows carried forward from CURRENT (marker integration).
- Missing-hook path ✅: `ConfigError("build stage <name> is not available")` via `_run_stages` -> `_fail` (status failed, `model.build.failed`, metrics); parametrised for enrich and score.
- build.py `_prepare` extraction ✅: `_stage_build` behaviour unchanged (all T02-18 tests pass); a later job resuming via `payload.build_id` or saved state gets a render context before 300-399 (tested by `yield_then_resume`, `rerun_is_idempotent`, `yield_inside_attach_range`, `current_is_this_build`).
- §2 module-map row for `_build_stages.py` added in the same commit; §2.2 exception row annotated. ignore_imports lines = exactly the two real lazy imports (pipeline/scoring go through importlib, invisible to the linter). UT00-58 edit minimal and conditional on the module existing (ruling accepted).
- ⚠️ Cannot verify here: ST02-16 vector-store part (impl 03 embeddings, not in the build file) and lake purge (impl 10) — stated gap, carried over; real `run_enrichment`/`run_scoring`/`LlmFactory` signatures (T03-28/T04-13 absent; Protocols match the spec text).

### Gates run (verify)
- Card tests `-k "IT02_23 or IT02_24 or IT02_25 or IT02_26 or UT02_78 or ST02_16"`: 18 passed.
- `pytest tests/unit/model tests/integration/model tests/unit/repo/test_import_contracts.py -q -p no:logging`: 303 passed, 1 skipped (symlinks), no warnings.
- Coverage (branch): `_build_stages.py` 100 % line / 100 % branch; `build.py` 100 % line, 99 % (1 partial 362->364, pre-existing).
- ruff check clean; ruff format --check clean; mypy 273 files clean; lint-imports 13 kept / 0 broken; check_module_size 0; check_type_ownership 0. build.py 396/400, `_build_stages.py` 169/200.

### Strengths
- Clean seam: loader functions + local Protocols, dependency-fault vs missing-module distinguished (`exc.name != module`).
- Tests assert real behaviour (row-level SQL outcomes on lake_small, same-connection identity, open-count, transaction probe, GPU recording).

### Mutation probes (by reasoning)
Caught: removing `except YieldRequested`; `current == run.build_id` check; `stages` mapping; yield check after `_sql(300,399)`; `clear_finished`; any of the 4 prune DELETEs or the `OR change_id` arm; content_hash UPDATE; the `DELETE FROM stg.build_counts` idempotency lines; NULL-cluster orphan; facts/scoring order; open transaction before facts; reopening the connection; `_hook` dependency re-raise. Not caught: removing either `CHECKPOINT` (unobservable; Minor 4).

### Issues
#### Critical (Must Fix)
- None.
#### Important (Should Fix)
- None.
#### Minor (Nice to Have)
1. `herness/model/_build_stages.py:110` — `clear_finished=True` is issued unconditionally (spec: only when resuming a completed unpromoted build). Same end state today, but a completed unpromoted build resumed for `enrich` that then yields is left `building` with `finished_at` NULL, and `support.delete_orphans` (`_build_support.py:117-128`) in any other build job before the resume deletes it. Spec-inherent (spec clears it too); already in the builder's T02-21 carry-over — keep it there.
2. `herness/model/sql/300_attach_decisions.sql:10,27` — "A re-run gives the same rows" holds for data rows, but `enrich_pruned` is replaced by the re-run's count (e.g. 5 -> 0 after a yield between 300 and 310 or a resume). Metric loses the original prune count; wording or accumulate if the count matters to DQ/metrics.
3. `herness/model/_build_stages.py:73-74` — `except AttributeError` also swallows an AttributeError raised while *importing* an installed hook module (a real bug) and reports it as "not available"; narrow to the `getattr` only (import first, then getattr in its own try).
4. `herness/model/_build_stages.py:128,146,150` — CHECKPOINTs not observable by any test (acceptable; note only).
5. Non-HernessError exceptions from hooks (e.g. the re-raised `ModuleNotFoundError`, a plain `ValueError` from impl 03/04) bypass `_fail` (`build.py:332-341`): build not marked failed, no `model.build.failed`/metrics. Pre-existing T02-18 design; flag for T03-28/T04-13 integration.
6. `tests/unit/model/test_model_build_handler.py:66-100` — seam tests carry ID UT02-78 though they test U02-100/101 loaders (not U02-134); harmless under the ID rule, slightly misleading.

### Assessment
**Task quality:** Approved
**Reasoning:** All units and tests match the card and spec (with benign, documented deviations), the SQL is static and idempotent, stages honour yield/no-GPU/same-connection rules, and every gate passes with 100 % coverage of the new module; remaining items are minor or carried over to T02-21/T03-28/T04-13.
