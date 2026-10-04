# T11-13 report: Shards, workers, truth writer, inbox

Status: DONE_WITH_CONCERNS (concerns below). Worktree branch base 35eb982; checkpoint 37ea97d (all code, tests, spec rows; every pre-commit hook passed); final commit bc56ca9 `feat(synth): T11-13 shards, workers, truth writer, inbox` (empty marker commit: the report lives outside the worktree; hooks passed).

## What was built
- U11-19 `tools/synth/shards.py`: `ShardResult`, `WorkerContext`, `AggregateResult`, `plan_shards`, `run_shard`, `run_all_shards`, plus `worker_context`, `require_under` (TH11-07), `label`, `_init_worker`/`_pool_task`. Spawn pool `multiprocessing.get_context("spawn").Pool(workers, initializer=_init_worker, initargs=(root, seed, params, catalog))`; initializer runs `init_config("synth", overrides=paths.data=<root>/data)` (an initializer failure is stored and raised by the task as `FatalError`, because a raising initializer makes Pool respawn forever). Phase A = incident shards (writes per-month `.npy` index under `<root>/truth/.parts/idx/`), phase B = rest in plan order. Worker failure -> pool terminated, `FatalError` naming `source/entity/YYYY-MM`.
- Private siblings (new §2 rows added in the spec in the same commit): `tools/synth/_shard_run.py` (budget 280: per-entity generation, plants, index, `apply_dirty`, `task_sla` via `gen_task_slas` from final incident records after plants+dirty, `assign_fetch`, `to_lake_batch`, writer abort on any error), `tools/synth/_shard_io.py` (budget 220: `LakeSink` over `LakeWriter(target_bytes=128 MiB, root=<root>/data/raw)`, truth parts, index write/load, no pickles).
- U11-20 `tools/synth/truth_writer.py`: `write_truth`, `write_synth_mappings`, `write_name_directory`, `synth_redactor`, `SYNTH_HMAC_KEY`. content_hash = `herness.enrich.text.content_hash(Redactor.redact(compose_text(sd, desc)).text)` (spec 03 functions reused). Row groups of 1M; tmp-then-`os.replace`; `.parts/` removed; `truth.json` last.
- U11-21 `tools/synth/inbox.py`: `write_service_costs`.

## Pre-existing parts of shards.py
`Shard`, `IncidentTimeIndex`, `PlantOutput` (and their docstrings/fields) existed from earlier cards and are unchanged. Header docstring rewritten; `from __future__ import annotations` added; everything else is new. Generator modules are imported lazily in shards.py (import cycle: generators import `Shard` from shards).

## Files and line counts (budget)
shards.py 323 (350); _shard_run.py 256 (280); _shard_io.py 194 (220); truth_writer.py 233 (250); inbox.py 41 (100). `check_module_size` exit 0.
Tests: tests/unit/tools/synth/test_synth_truth_writer.py, test_synth_inbox.py, test_synth_shards.py; tests/integration/tools/synth/test_synth_shards_pool.py (marked integration, ~3 s).

## Tests and gates
- `pytest tests/unit/tools/synth tests/integration/tools/synth -q`: 274 passed (26 s).
- UT11-24 (4 functions + rf), UT11-25 (2 functions); rf tests: plan order/counts, no-ServiceNow plan, incident shard lake/index/parts, dimension shards incl. cmn_department, writer abort on failure (no lake/temp file left), path escape, argument checks, truth PII probe (no planted PII value in truth.json/truth_labels/t2/t3), member/pair files, ConfigError on missing name directory, parts outside truth refused, mappings validate as `MappingsConfig`; integration: no dot files, cmn_department+task_sla present with rows == counts, counts match lake, worker failure -> FatalError naming shard.
- ruff format/check, mypy (touched files), lint-imports (14 kept), check_type_ownership, check_module_size: clean. Checkpoint commit passed every pre-commit hook incl. `pytest-unit`.
- RED evidence: not captured; tests were written after the first implementation pass (deviation from TDD order), then run green.

## Tiny-generation listing (run_all_shards, seed 7, tiny, 4 spawn workers, tmp root)
```
dot-prefixed under data/raw: 0
jira/issue files 12 rows 163
monitoring/event files 5 rows 430
monitoring/metric_daily files 40 rows 7270
servicenow/change_request files 4 rows 331
servicenow/cmdb_ci files 2 rows 91
servicenow/cmdb_ci_service files 1 rows 20
servicenow/cmdb_rel_ci files 1 rows 70
servicenow/cmn_department files 1 rows 3
servicenow/incident files 42 rows 1551
servicenow/problem files 4 rows 41
servicenow/sys_user_group files 1 rows 15
servicenow/task_sla files 3 rows 1376
```

## Deviations / spec notes (for controller ruling)
1. Fixed test HMAC key: the tree holds no value for "the synth profile's fixed test HMAC key" (synth profile uses dotenv `secret:redact.hmac_key`, no committed .env). `SYNTH_HMAC_KEY = sha256(b"herness synth profile fixed test key")` is pinned in truth_writer so truth hashes are reproducible; a synth `.env` must carry the same 64-hex for pipeline content hashes to match. Redaction settings come from `load_config("synth")`.
2. Name directory format: spec 11 says `name_directory.csv` is `first_name,last_name` (written verbatim), but spec 10's `NameDirectory.from_files` reads a `display_name` column. The truth redactor loads the names as display names ("First Last" -> full, "Last, First", "Last,First" variants) via a temp file in `.parts/`. T11-16 (synth profile directory_file) must reconcile; as is, pointing the profile at this CSV would load zero names.
3. Additive fields: `ShardResult.row_counts` (per source/entity, incl. task_sla) and `ShardResult.s4_noise` (for T4 `generated_noise_ratio`); `write_truth(..., config_dir=CONFIG_DIR)` keyword. `WorkerContext`/`AggregateResult` are defined in shards.py (spec names them without fields).
4. Chunking: non-incident entities generate in sub-shards of <=131,072 records; an incident month is generated whole (incident plants T6/T1/T3/T5 need the month's background; full scale ~139k/month); all lake writes go in batches <=131,072.
5. Dimension rows: `gen_cis`/`gen_rels` emit no `sys_updated_on` (to_lake_batch would fail); the shard runner stamps missing ones at span start (servicenow.py docstring says dimensions are stamped at start).
6. `owning_team` labels are re-synced to the final `assignment_group` after T1/T5 reassignment.
7. Index built from post-plant, pre-dirty incident records; P1 days for metric_daily derived from it. Bare-priority drift coin per written incident batch (month index >= 29).
8. synth_mappings content (custom field ids, enums for incident_state codes and change_type labels, service_overrides role `delivery`) is my reading of the generator's fields; T11-16 may adjust.

## Concerns
- Items 1 and 2 are cross-spec gaps; they do not affect this card's tests but affect pipeline/truth hash agreement later.
