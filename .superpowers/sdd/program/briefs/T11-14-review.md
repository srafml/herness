### Spec Compliance
- ✅ U11-22 `tools.synth.verify` (verify.py 212/250). `VerifyReport` is a frozen dataclass. The in-memory DuckDB is limited to `<root>/data/raw` (`allowed_directories`, then `enable_external_access=false` and `lock_configuration`). The glob `**/[!.]*.parquet` uses hive and union_by_name, with the glob passed as a bound parameter. The 8 metadata columns are checked with their spec 02 types; NULL keys, `_record_id = _source:_entity:_source_key` (plus a path check) and the tombstone/payload rule are checked; dot files are walked. Row counts are compared with `truth.row_counts`, and `duplicate_rows`, `later_versions` and `tombstones` are recomputed and compared with `truth.dirty`. `content_hashes` follows design §5.1.8 with `coalesce`, and the `ORDER BY 1` → `ORDER BY v` reading is sound.
- ✅ U11-23 `generate` covers:
  - unknown override → `SynthUsageError`;
  - the defaults: 2023-09-01..2026-08-31, `SOURCES` (4), dirty `default`, fetch `initial`, `workers=os.cpu_count()`;
  - steps 1-9 in order: `load_params` runs before the root is touched; `RootNotEmpty(ConfigError)`; `--overwrite` needs a regular, non-symlink `.synth_root`; links are unlinked, never followed;
  - `write_service_costs` only when `files` is a source;
  - `generator_version = GENERATOR_VERSION`, `question_set_version` through `load_config("synth")` (= `qs-2026-10-01.1`);
  - the marker `{seed, scale, generator_version, params_hash}`;
  - `verify` not ok → `SchemaViolation`;
  - `synth.generate.completed`.
- ✅ U11-24 `main` follows the R-46 table as ruled by the controller. Codes: 0; 2 only from argparse (no `choices=`/`type=`); 3 for every `ConfigError`; 1 for everything else. Both subcommand forms parse (their bodies come with T11-15). The default root is `data/synth/<seed>-<scale>` with `5m`→`full`. There is one JSON line on stdout through `sys.stdout.write`. Logs go to stderr, with `synth.generate.failed` and `synth.verify.failed`.
- ✅ UT11-26: the clean root is ok, and each corruption (dot file, dropped column ×2, extra row) yields a problem. rf extras cover the record-id mismatch, wrong `_deleted` type and missing truth.
- ✅ UT11-27: `RootNotEmpty` and files byte-identical. Overwrite without the marker raises `SynthUsageError` and deletes nothing.
- ✅ UT11-28: overwrite clears the stray file and gives identical `content_hashes`, a byte-identical `truth.json` and an equal manifest.
- ✅ UT11-29: exits 0 / 3 / 3 / 1 / 2. The valid run also asserts the summary keys and values and that no directory name appears in stdout or stderr.
- ✅ IT11-01 tiny: `main` runs with `--workers 1` and `--workers 8` through the real spawn pool (`run_all_shards` is not patched in the integration file, and `workers=1` also uses `spawn.Pool`). The test compares the real `content_hashes`. The small variant is marked `slow` and deselected (2/4 collected with `-m "not slow"`).
- ✅ IT11-02 tiny: the subprocess exits 0 with exactly one JSON line of the 4 keys, and `verify_root` is ok. The small variant is `slow`.
- ✅ ST11-07: exit 3, tree unchanged, stdout empty, sentinel text not echoed. A directory or symlink `.synth_root` does not authorise deletion.
- ✅ TH11-07: checked by hand (6 attacks, see Evidence) and by static review of every output string.
- ⚠️ Cannot verify here: IT11-01/IT11-02 at `small` were not run (slow; nightly). `full` < 30 min and verify `full` < 5 min (BT11-01..03 belong to later cards).

### Strengths
- Root safety is careful. `load_params` and the option checks run before the root is touched, so every bad value exits 3 with the root untouched. The marker must be a regular non-symlink file. Children are unlinked or rmtree'd without following links. Paths are resolved before any action. All 6 hand attacks were refused, with exit 3 and files intact.
- Every Plants field comes from real data. On the tiny root:
  - the T2 epic SEQU-100 is `jira:issue:30100`, an Epic;
  - the T2 decoy is SEQU-101, an Epic;
  - the T6 paid side is LANT-102 / `jira:issue:10102` and the unpaid side SEQU-103 / `jira:issue:30103`, all Epic rows in the lake;
  - the team id form `servicenow:sys_user_group:<sys_id>` and the service id form `servicenow:cmdb_ci:<sys_id>` match `herness/model/sql/200_org_team_service.sql:11,50,81`.
- The verify DuckDB sandbox is properly locked, and every problem string names only keys, columns, types, counts and paths.
- The tests discriminate: 10 of 13 mutation probes went red, often in several tests (ConfigError→1 turned 13 tests red).
- The `shards.py` worker change is the minimal correct fix for stdout cleanliness. Before it, structlog's default PrintLogger in spawn workers wrote to the inherited stdout. ERROR level only drops warnings the parent repeats, and workers log nothing at ERROR today, so it hides nothing that existed. Worker failures still reach the parent as `FatalError`.
- All gates are clean, and the card tests run without warnings.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. **No test checks that `content_hashes` reacts to `_payload`.** `tools/synth/verify.py:65` / `tests/unit/tools/synth/test_synth_verify.py:177`: replacing `md5(coalesce(_payload,''))` with `md5('')` leaves all 35 card tests green, IT11-01 included. The determinism proof would then miss payload divergence between worker counts. Add one assertion: rewrite one row's `_payload` in a copy, and only that entity's hash changes.
2. **The non-Herness failure path's message hygiene is untested.** `tools/synth_data.py:227`: changing `"unexpected error"` to `str(exc)` leaves every test green. Add a UT that patches `generate` to raise, for example, `ValueError("<payload text>")`, then asserts exit 1 and that the text is absent from stderr.
3. **A worker failure's cause is not surfaced anywhere.** `tools/synth_data.py:226-228`: `_failed` logs only `error_type` and writes `exc.message`. For a worker `FatalError` the cause class sits in `exc.context["cause"]` (`shards.py:257-259`) and is dropped. Spec §7's `synth.shard.failed` (ERROR: `source`, `entity`, `month`, `error_type`) is emitted nowhere in `tools/synth/`. The report says "`synth.shard.failed` (ERROR) still gets through", but no such event exists, so that claim is inaccurate. Fix: log the HernessError's `cause`/`shard` context in `synth.generate.failed`, or emit `synth.shard.failed` in `_pool_task`; worker ERROR level lets it through. The `_pool_task` part is T11-13 scope, so it is also listed as a carry-over.
4. **The check for dot-prefixed directories is untested.** `tools/synth/verify.py:124`: restricting the walk to files leaves the tests green. The spec only requires files, so this is optional.
5. **A test name and docstring overstate its coverage.** `tests/unit/tools/synth/test_synth_verify.py:136`: `test_rf_record_id_mismatch_and_live_null_payload_are_problems` only corrupts `_record_id`. The live-NULL-payload check is actually covered by the `_payload` case of the parametrized `test_ut11_26_dropped_column_is_a_problem` (probe 3e went red there). Rename the test or add the payload corruption.
6. **The in-process pool stand-in is duplicated.** `_inline_run` is copied verbatim in `tests/unit/tools/synth/test_synth_verify.py:31` and `tests/unit/tools/test_synth_data.py:33`, and T11-13's truth_writer test has a third copy per the report. A shared helper in `tests/support/` or a conftest would remove the drift risk.
7. **The parent loads the `synth` config twice per run.** One load is `tools/synth/_manifest.py:127-130`, the other comes from `write_truth`. The acceptance stderr shows 18 duplicated `config.validate.issue` warnings and two `config.load.completed`, at ~0.13 s each against the 5 s budget. Passing one loaded config, or the version, through would halve the noise.
8. **The `5m` normalisation is duplicated.** `tools/synth_data.py:211` re-implements the `5m`→`full` rule that `load_params` owns (`params.scale`), so the default root and the manifest scale could drift if aliases change.
9. **Any regular `.synth_root` file authorises deletion.** `tools/synth_data.py:96` does not check the marker's content. This matches U11-23 ("require ... to exist"). Parsing it as JSON with the 4 keys would cost about 3 lines, which `synth_data.py` cannot spare at 250/250. It could live in `_manifest.py` as `is_marker(path)`.
10. **The RED evidence is collection errors only** (`ModuleNotFoundError`). That shows the tests depend on the new modules, not that each fails for the right reason. The mutation probes in Evidence make up for most of this, but the 3 green probes (items 1, 2, 4) are tests that never had a red phase on their behaviour.

### Assessment
**Task quality:** Approved
**Reasoning:** All three units match the spec under the controller's R-46 ruling. Acceptance holds (exit 0, ~4.1-4.2 s), and TH11-07 holds under hand attacks and static review. The remaining gaps are missing test assertions and a debuggability improvement for worker failures, none of which makes the delivered behaviour incorrect.

### Evidence
**Acceptance (check A):** `uv run python tools/synth_data.py --seed 7 --scale tiny --verify --root /tmp/w32-c11/verifier/acc{,2}` → exit 0 / 0, wall times 4.23 s / 4.06 s (4 CPUs, 4 workers).
- stdout is exactly 1 line: `{"root": "/tmp/w32-c11/verifier/acc", "rows": 11361, "seconds": 3.063, "params_hash": "sha256:b55e…c7fb"}`. stderr has 23 JSON log lines (`synth.generate.started`, the config warnings, `synth.generate.completed`).
- 0 dot-prefixed paths under `data/raw`. `servicenow/cmn_department` (3 rows) and `servicenow/task_sla` (1376 rows) exist, both under `dt=2026-09-01`.
- `load_truth(<root>/truth)` validates: `generator_version` 2.0.0, `question_set_version` qs-2026-10-01.1.
- `.synth_root` = `{"generator_version":"2.0.0","params_hash":"sha256:b55e…","scale":"tiny","seed":7}`.
- `data/inbox/service_costs/service_costs.csv`, `name_directory.csv` and `synth_mappings.yaml` are present.

**Card tests (check C):** `uv run pytest tests/unit/tools/synth/test_synth_verify.py tests/unit/tools/test_synth_data.py tests/security/test_st11_07_overwrite_marker.py tests/integration/tools/synth/test_synth_data_cli.py -q -p no:logging -m "not slow"` → `35 passed, 2 deselected in 25.54s`, with no warnings.

**Collection (check B):** IDs UT11_26 (×6), UT11_27 (×2), UT11_28, UT11_29 (×5), IT11_01, IT11_02 and ST11_07 (×2) are all collected. `--require-test-ids --collect-only` on the 4 files: 37 collected, no error.

**Mutation probes (check D):** each probe was restored with `git checkout --` and followed by an empty `git status --short`.

| # | Edit (one line) | Result |
|---|---|---|
| 1a | `synth_data.py:96` marker check → `if False:` | RED: UT11-27 overwrite, ST11-07 ×2 |
| 1b | `not marker.is_file()` → `not marker.exists()` (a directory is accepted) | RED: ST11-07 dir/link |
| 2a | `ConfigError` → `_failed(ns, exc, 1)` | RED: 13 tests (UT11-29 scale and non-empty, rf ×9, ST11-07 ×2) |
| 2b | `_verify` raises `ConfigError` (exit 3) instead of `SchemaViolation` | RED: UT11-29 corrupted root `--verify` |
| 2c | `--scale` gets `choices=(tiny,small,full,5m)` | RED: UT11-29 bad scale |
| 3a | `_record_id <> …` check → `WHERE false` (path check kept) | RED: rf record-id mismatch (UT11-26 itself stays green) |
| 3b | `duplicate_rows` `+ 1` | RED: UT11-26 clean root |
| 3c | dot walk skips directories | **GREEN** (gap, Minor 4) |
| 3d | `content_hashes` ignores `_payload` (`md5('')`) | **GREEN**, IT11-01 included (gap, Minor 1) |
| 3e | `_deleted <> (_payload IS NULL)` check → `WHERE false` | RED: UT11-26 dropped `_payload` |
| 4a | non-Herness failure writes `str(exc)` | **GREEN** (gap, Minor 2) |
| 4b | JSON summary adds `short_description` | RED: UT11-29 valid (key set) |
| 4c | log line with a directory name | RED: UT11-29 valid (name scan) |

**TH11-07 by hand (check E):**

| Attack | Result |
|---|---|
| `--overwrite` on a sentinel dir with no marker | exit 3, stdout empty, `sentinel.txt` intact |
| `.synth_root` symlinked to a real marker | exit 3, files and the target marker intact |
| root is a symlink to a dir without a marker | exit 3, target intact |
| `acc/../sentinel` | exit 3 |
| `--root .` (the repo) | exit 3, `git status` clean |
| root is a file | exit 3 ("root: is not a directory") |

The stderr line is always `synth_data: SynthUsageError: overwrite: needs the .synth_root marker of a generated root` (or the "not a directory" variant), so it carries keys and paths only. In the static review, every output string is either a fixed `HernessError.message` (`load_truth` and `load_params` messages are fixed strings) or a problem string made of keys, columns, types, counts and paths. Raw argv `seed`/`scale` are echoed in `synth.generate.failed`, which is user input.

**Budgets (check F):** `synth_data.py` 250/250, `verify.py` 212/250, `_manifest.py` 169/180, `shards.py` 326/350. `check_module_size` exit 0. The `_manifest.py` §2 row is accurate:
- its only importer is `tools/synth_data.py:27`;
- its non-`tools.synth` imports are `herness.eval.truth` and `herness.core.config`, the same convention as the `_shard_run`/`_shard_io` rows.

**Gates (check I):**

| Gate | Result |
|---|---|
| `ruff check` | All checks passed |
| `ruff format --check` | 1086 files already formatted |
| `mypy` | no issues in 389 source files |
| `lint-imports` | 15 kept, 0 broken |
| `check_type_ownership` | exit 0 |
| `check_module_size` | exit 0 |

The final `git status --short` is empty, at HEAD 31e311c.

### Spec notes / carry-overs
- **Exit-code wording is stale.** U11-22 Errors ("exits 3"), F11-01 step 8 and F11-03 contradict U11-24/R-46. The code follows U11-24 (verify failure → 1, every `ConfigError` → 3). Correct the spec text.
- **`dataset_root` is the absolute `<root>/data` of the generating machine** (`_manifest.py:147`). Committed `tests/fixtures/truth/7-tiny/` (T11-15) would carry a machine path. Rule on DD11-06 semantics (relative, or stripped on copy).
- **Service id form (T11-16 carry-over, not a card defect).** The manifest uses `servicenow:cmdb_ci:<sys_id>`, which matches `core.service.service_id` (`herness/model/sql/200_org_team_service.sql:81`). `tools/synth/truth_writer.py:188` (T11-13) writes `service_overrides.service_id` as `servicenow:cmdb_ci_service:<sys_id>`. Reconcile when wiring the synth profile.
- **T11-15 budget.** `synth_data.py` is at 250/250. The pii-corpus/api-pages bodies plus `--n`/`--rows`/`--source` validation need about 10-15 lines. That is feasible only with a budget increase, or by moving the parser helpers (`_default_parser`, `_sub_parser`, `_value`, `_overrides`, ~50 lines) into a private sibling (for example `tools/synth/_cli.py`) with its own §2 row. Decide before dispatching T11-15.
- **T11-13 carry-over.** Spec §7 `synth.shard.failed` (ERROR) is not emitted by `_pool_task` (`tools/synth/shards.py:250-259`), and the worker cause type is not surfaced in `synth.generate.failed` (Minor 3).
- **T4 `generated_noise_ratio` = 1.0 at tiny** (design example 0.953). It is within (0, 1], but every S4 noise event has a NULL `incident_ref` at tiny. Worth a glance at `small` in the nightly run (T11-13 behaviour).
- **Report accuracy.** The "`synth.shard.failed` still gets through" claim (deviation 1) is false: no such event exists.
