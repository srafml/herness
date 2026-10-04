# T11-40 review (Ops store fixture): Needs fixes

Reviewed b859959 against base 8a073b4 (worktree agent-a571b23e612b62889), read-only.

### Spec Compliance
- U11-78 fixture algorithm: ✅ The steps run in spec order. `data_root = tmp_path/"data"` is created, then `HERNESS_PATHS__DATA` is set through monkeypatch, then `reset_config()`, then `reset_connections(path=data_root/"ops.sqlite")`, then `migrate()`, then the handle is yielded. Teardown runs in `finally`: first `reset_connections()`, then `reset_config()` (tests/support/ops_store.py:62-75). Autouse `reset_herness_state` is set up before `ops_store` and torn down after it, so the precondition holds and the config is cleared once more after the test.
- U11-78 OpsStoreHandle (ruling 1): ✅ It is a frozen, slotted dataclass `(data_root, db_path, migration)` with `__fspath__`, `__truediv__` and `__getattr__` delegation. Checked: `copy.copy`, `copy.deepcopy` and a pickle round-trip all work, because the frozen+slots dataclass supplies `__getstate__`/`__setstate__`. Recursion happens only on an instance that has not been initialized (see Minor 1).
- U11-35 conftest (ruling 3): ✅ `reset_registry()` and `reset_config()` run before and after every test. `HERNESS_ENV=test` is set and `HERNESS_FAULTS` is removed. `reset_process_state` is unchanged. `pytest_plugins` is pytester, tests.support.plugin, fake_clock and ops_store, which follows the T11-04 rule.
- Ruling 4: ✅ `tests/support/ops_core_store.py` is deleted and no references remain (grep finds only docstring mentions).
- Ruling 2 (consumers): ✅ No existing test is weaker. The assertions in test_store_ops_core, test_st02_ops_core, test_store_ops_fault, test_store_ops_migrate, test_store_ops_migrations and test_store_ops_bench match the old ones. The two UT02-25/UT02-28 tests that need a first open on the thread moved to `tmp_path` with the same assertions, and their cleanup is now in `finally`, which is an improvement. The local `ops_store` shadow and `fresh_ops_store` match the interim fixture exactly.
- UT11-117: ❌ Only partly asserted:
  - ✅ Two sequential tests. The first writes a `run` row, the second counts 0 rows. `unlink()` of the first file after teardown works, which is real evidence on Windows. `PRAGMA database_list` shows `connection()` no longer points at the first file.
  - ❌ "`schema_version()` equals the highest migration" is tautological. `MigrationReport.version` is set by `migrate()` from `schema_version()` itself (herness/store/ops/migrate.py:225), so `schema_version() == ops_store.migration.version` (test_ops_store.py:33, :48) holds even after a partial migration.
  - ❌ "each test's `db_path` is under its own `tmp_path`" is never asserted. The tests only check that the two handles differ (test_ops_store.py:46-47).
  - ⚠️ The config `paths.data` check (test_ops_store.py:34-35) passes by coincidence (Important 3).
- Budgets: ✅ ops_store.py has 61 lines (≤ 80) and conftest.py has 63 (≤ 80).
- Test IDs, docstrings and pytestmark: ✅ `test_ut11_117_*` ×3 with docstrings that start `UT11-117`, and `pytestmark = pytest.mark.unit`. The inner pytester file uses UT99 placeholder IDs, which is fine.
- ⚠️ Could not verify from the diff: the full-suite pass and the lint/mypy results come from the report only. I re-ran just tests/unit/support/test_ops_store.py and test_conftest.py: 4 passed.

### Strengths
- The fixture is small and follows the spec exactly, and teardown is correct and exception-safe.
- The merge-compat handle is tested directly (test_ops_store.py:57-63).
- Consumer changes stay minimal and document why each local fixture exists. The `finally` cleanup added to UT02-25/UT02-28 fixes a small leak in the old code.
- UT11-37 is extended with a real check across a test boundary for the registry and config reset.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **The highest-migration check is tautological.** Location: tests/unit/support/test_ops_store.py:33 and :48. `MigrationReport.version` is `schema_version()` read after migrating (herness/store/ops/migrate.py:225), so the assertion compares a value with itself. A fixture that applied only some migrations, or none, would still pass. Fix: compare against the highest discovered migration, for example `max(m.version for m in _MOD._discover())`, or at least assert `pending_migrations() == []` and `ops_store.migration.applied`.
2. **The UT11-117 clause "each test's `db_path` is under its own `tmp_path`" is missing.** Location: tests/unit/support/test_ops_store.py:30-39 and :42-47. Neither test asserts `ops_store.db_path.is_relative_to(tmp_path)` or `== tmp_path/"data"/"ops.sqlite"`. The second test does not even request `tmp_path`. Fix: request `tmp_path` in both tests and assert `ops_store.data_root == tmp_path / "data"` and `ops_store.db_path == tmp_path / "data" / "ops.sqlite"`.
3. **The config `paths.data` postcondition check passes without the fixture's env override.** Location: tests/unit/support/test_ops_store.py:34-35. `write_full_config(tmp_path)` writes `paths: {data: data}`, which resolves against `tmp_path` to `tmp_path/data`. That is the same as `ops_store.data_root`. I checked with no `HERNESS_PATHS__DATA` set: `load_config(config_dir=write_full_config(t)).paths.data == t/"data"` is True. So the assertion does not prove U11-78 step 2. The override itself does work: with the config tree in a different root, the env value wins. Fix: build the config tree under a separate root, for example `write_full_config(tmp_path / "cfgroot")`, so only the env override can produce `data_root`.

#### Minor (Nice to Have)
1. **Attribute access on an uninitialized handle recurses.** Location: tests/support/ops_store.py:45-46. On an instance made with `object.__new__(OpsStoreHandle)`, any missing attribute, and even `repr`, raises `RecursionError` instead of `AttributeError`, because `__getattr__` reads `self.db_path`, which fails and calls `__getattr__` again. Normal copy and pickle are not affected. Fix: guard with `if name in {"data_root", "db_path", "migration"} or name.startswith("__"): raise AttributeError(name)`.
2. **The second UT11-117 test fails when run alone.** Location: tests/unit/support/test_ops_store.py:45. Running with `-k second` gives `IndexError` on `_seen[0]` (reproduced). This is plan-mandated sequencing, and it is fine under `-k UT11_117` and without xdist. A `pytest.skip` when `_seen` is empty, or a pytester inner session with two tests, would make it robust to test selection and to xdist or random ordering.
3. **Redundant migrate call in BT02-06.** Location: tests/bench/test_store_ops_bench.py:39. BT02-06 still calls `migrate()` on the store the fixture already migrated. It is a harmless no-op but misleading now.
4. **The test's import-time registration changes global state.** Location: tests/unit/support/test_conftest.py INNER UT99-04. It sets `cfgmod._Cache.config = object()` with a `type: ignore`, reaching into a private cache. This is acceptable for a pytester inner test, but it is coupled to the internal `_Cache` name.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The fixture, conftest reset, plugin swap and consumer updates are correct, and no consumer test is weaker. But the UT11-117 test proves less than its row claims: the highest-migration check is tautological, the `tmp_path` containment check is missing, and the config `paths.data` check passes without the fixture's env override. All three are small test-only fixes.

## Re-review round 1 (6d8290d, diff b859959..6d8290d)

### Findings closed
- **I1** ✅ The inner tests now assert `schema_version() == HIGHEST == ops_store.migration.version`. `HIGHEST` is the maximum `NNN_` file in `herness.store.migrations`, the same package as `_PACKAGE` in migrate.py:39 (currently 006). They also assert `migration.applied` is non-empty and `pending_migrations() == []`. The value is now independent of the report.
- **I2** ✅ Both inner tests check `data_root == tmp_path/"data"` and `db_path == tmp_path/"data"/"ops.sqlite"` against their own `tmp_path`.
- **I3** ✅ The config tree is now written under `tmp_path/"cfgroot"`. I reran the check with `HERNESS_PATHS__DATA` unset: `paths.data` resolves to `<t>/cfgroot/data`, which is not `<t>/data`. The assertion now depends only on the override.
- **M1** ✅ `__getattr__` now raises `AttributeError` for the field names and for dunder names. My earlier probe gives `AttributeError` (not `RecursionError`) for missing attributes and `repr` on an uninitialized handle. `copy`, `deepcopy` and pickle still work. The new assertions in test_ops_store.py cover this.
- **M2** ✅ The two sequential tests now run in a pytester inner session with `assert_outcomes(passed=2)`, so selecting tests from the outer session cannot break the order. `-k UT11_117` gives 2 passed. `--require-test-ids -k UT11_117` gives 2 passed, 3418 deselected.
- **M3** ✅ The redundant `migrate()` is removed from BT02-06. The `migrate` import is still used by BT02-07.
- **M4** is parked by the sub-controller, so I did not re-check it.

### Other checks
- Ran tests/unit/support/test_conftest.py, tests/unit/store/ops, tests/fault/store and tests/bench/test_store_ops_bench.py: 124 passed.
- `ruff check` and `ruff format --check` are clean on the changed files.
- ops_store.py is 63 lines (≤ 80).

### New issues
Critical: none. Important: none.

Minor:
1. **Duplicated file-name pattern.** Location: tests/unit/support/test_ops_store.py:15-19. `_HIGHEST` repeats the migration file-name regex instead of reusing `_FILE_RE` from migrate.py. If the naming rule changes, the two could drift, but any drift would make the test fail rather than pass silently. Optional.
2. **Placeholder test IDs in the inner session.** The inner tests use IDs UT99-11 and UT99-12, the same convention as the existing test_conftest inner file, so this is acceptable.

### Assessment
**Task quality:** Approved
**Reasoning:** Every Important and Minor finding from round 1 that was in scope is fixed and verified by rerunning the checks. The fix introduced no new Critical or Important issue.
