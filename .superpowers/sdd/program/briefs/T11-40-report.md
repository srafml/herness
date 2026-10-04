# T11-40 report (Ops store fixture)

Status: DONE

Commit: `b859959 feat(tests): real ops_store fixture and config reset (T11-40)` (worktree agent-a571b23e612b62889, base 8a073b4). Pre-commit hooks all passed on commit.

## Files
- `tests/support/ops_store.py` (61 lines, budget 80): `OpsStoreHandle` frozen dataclass (`data_root`, `db_path`, `migration`) plus `os.PathLike` (`__fspath__`), `__truediv__`, `__getattr__` delegation to `db_path` (merge-compat, ruling 1); `ops_store` fixture per U11-78 algorithm (HERNESS_PATHS__DATA env override, reset_config, reset_connections(path=...), migrate(), teardown reset_connections + reset_config in finally).
- `tests/conftest.py` (63 lines, budget 80): plugin swap to `tests.support.ops_store`; autouse `reset_herness_state` calls reset_registry + reset_config before and after each test; reset_process_state unchanged.
- `tests/support/ops_core_store.py`: deleted (interim plugin).
- `tests/unit/support/test_ops_store.py`: UT11-117 (two sequential tests + Path-compat test). The first test now verifies the postcondition on a real loaded config: `load_config(config_dir=write_full_config(tmp_path)).paths.data == ops_store.data_root` (replaces the earlier env-var-only assertion; the repo has no `config/profiles`, so bare get_config() is not usable, but tests.support.config_tree.write_full_config builds a loadable tree).
- `tests/unit/support/test_conftest.py`: UT11-37 extended (registry and config cache reset across a test boundary).

## In-tree `ops_store` consumers updated (ruling 2)
- `tests/unit/store/ops/test_store_ops_core.py`: uses `OpsStoreHandle`/`.db_path`; `test_ut02_25_old_sqlite_rejected` and `test_ut02_28_busy_while_opening_is_retried` need "first open on this thread" semantics and use `tmp_path` + `core.reset_connections(path=...)` directly.
- `tests/security/test_st02_ops_core.py`, `tests/fault/store/test_store_ops_fault.py`: annotated `OpsStoreHandle`; `.db_path` where a raw path is needed.
- `tests/unit/store/ops/test_store_ops_migrate.py`, `tests/unit/store/ops/test_store_ops_migrations.py`: local `ops_store` fixture (same name, shadows the plugin) giving an un-migrated store.
- `tests/bench/test_store_ops_bench.py`: `fresh_ops_store` local fixture for BT02-07 (fresh migration timing).

## Empty-store fixture names
The fixture name for an EMPTY/un-migrated store is `ops_store` (locally shadowed, same name) in `test_store_ops_migrate.py` and `test_store_ops_migrations.py`, and `fresh_ops_store` in `test_store_ops_bench.py`.

## Final test results
- `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`: 3400 passed, 5 skipped (symlink privilege / no coverage.json), 15 deselected, 1 xfailed (pre-existing IT00-02 marker), 0 failed. `test_it00_01_pre_commit_run_all_files` passed (earlier failure did not recur).
- `pytest tests/security/test_st02_ops_core.py tests/fault/store tests/bench/test_store_ops_bench.py`: 5 passed.
- `pytest --require-test-ids -k UT11_117`: 3 passed.
- ruff format (clean after 1 reformat), ruff check (pass), mypy (127 files, no issues; tests/ outside mypy scope), lint-imports (13 kept, 0 broken), tools.check_module_size (exit 0).

## Concerns
- None blocking. `tests/` is outside `[tool.mypy] files`, so the new fixture module is not type-checked by the gate (written strict-clean by hand).

## Fix round 1 (review T11-40-review.md)
Commit: `6d8290d test(support): tighten UT11-117 and handle delegation (T11-40 review)` (new commit, hooks passed).
- I1: highest-migration check now compares `schema_version()` to the max version parsed from the `herness.store.migrations` files (currently 6), plus `pending_migrations() == []` and `migration.applied` non-empty.
- I2: both sequential tests assert `data_root == tmp_path/"data"` and `db_path == tmp_path/"data"/"ops.sqlite"`.
- I3: config tree written under `tmp_path/"cfgroot"` (its `paths.data: data` resolves to cfgroot/data), so only `HERNESS_PATHS__DATA` can make `cfg.paths.data == data_root`.
- M1: `OpsStoreHandle.__getattr__` raises AttributeError for field names and dunder names before delegating; the Path-compat test checks an `object.__new__` handle.
- M2: the two sequential tests run in a pytester inner session (`test_ut11_117_sequential_tests_get_fresh_isolated_stores`, asserts 2 passed); selection/order no longer matters.
- M3: redundant `migrate()` dropped from BT02-06.
- `tests/support/ops_store.py` now 63 lines (budget 80).
- Results: full suite 3399 passed, 5 skipped, 15 deselected, 1 xfailed, 0 failed (one fewer test because the two sequential tests are now one outer test); `--require-test-ids -k "UT11_117 or UT11_37"` 3 passed; security/fault/bench 5 passed; ruff format/check clean; check_module_size exit 0.
