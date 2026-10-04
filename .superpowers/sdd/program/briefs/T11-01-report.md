# T11-01 report: pytest plugin (markers, test IDs), tests/conftest.py

Status: DONE_WITH_CONCERNS. Commit d101483 "feat(tests): add pytest plugin for markers and test IDs, root conftest (T11-01)".

## Implemented

- `tests/support/plugin.py` (U11-30, U11-31)
  - `pytest_configure` registers the 6 markers with their design descriptions.
  - A tryfirst `pytest_collection_modifyitems` runs before `-m`/`-k` deselection, so it sees every item:
    - A file whose `pytestmark` does not carry exactly 1 category (unit, integration, fault, eval) is flagged. An item whose own marks add a second category is flagged by nodeid. Any violation raises `pytest.UsageError` listing all of them.
    - `TEST_ID_PATTERN` is verbatim from the spec and compiled with IGNORECASE. `extract_test_ids` returns normalized IDs (upper case, hyphen), sorted and unique.
    - Index `{id: sorted nodeids}`. An ID on two different functions raises `UsageError`. Function identity is the nodeid without `[params]`, which also separates same-named methods in different classes, so it is slightly stricter than the spec's "originalname or module". Parametrized items share an ID.
    - `--require-test-ids` raises `UsageError` listing untagged nodeids.
    - `--collect-test-ids=PATH` writes `{"schema": 1, "ids", "untagged"}` atomically (tmp file, then `os.replace`, parent dirs created). It writes after those checks pass and before selection.
    - `--select-test-ids` normalizes entries (`ut99_01` is accepted); a bad entry or an empty list raises `UsageError`. Deselection goes through `pytest_deselected`.
- `tests/conftest.py` (U11-35)
  - `pytest_plugins = ["pytester", "tests.support.plugin"]` and `collect_ignore = ["support", "fixtures"]`.
  - Hypothesis profiles `commit` (200, derandomize, 500 ms) and `nightly` (10,000, derandomize, no deadline). `commit` is loaded at import. Hypothesis' own `pytest_configure` runs afterwards and applies `--hypothesis-profile`, and I checked that `nightly` takes effect.
  - The autouse `reset_herness_state` fixture sets `HERNESS_ENV=test` and removes `HERNESS_FAULTS` via monkeypatch.
- Tests:
  - `tests/unit/support/test_plugin.py`: UT11-31 to UT11-36 (pytester).
  - `tests/unit/support/test_conftest.py`: UT11-37.
  - `tests/integration/support/test_test_ids_repo.py`: IT11-31, which runs a real subprocess `--collect-only --collect-test-ids --require-test-ids` on the repo.

## Deviations

1. **NEEDS_CONTEXT symbols:** `T10-04 herness.core.registry.reset_registry` and `T10-03 herness.core.config.reset_config` do not exist.
   - I did not invent them, and I did not add a tolerant import.
   - The fixture does only the env half. It is a plain `return` fixture (ruff PT022) until there is teardown work.
   - UT11-37 checks the profiles and the env reset in an inner pytester session. The registry half ("entry gone in the next test") has to be added once T10-04 lands.
2. **pytest_plugins:** `fake_clock`, `builds`, `bench`, `truth` and `ops_store` do not exist yet. Their cards append them (T11-40 already lists `tests/conftest.py` in its Files).
3. **`pytester` in pytest_plugins:** it is needed for the plugin tests. `pytest_plugins` is only allowed in the top-level conftest, and pyproject `addopts` belongs to impl 00.
4. **Existing tests changed so IT11-31 passes.**
   - Marker enforcement accepted every existing file.
   - `--require-test-ids` on the repo failed on 8 untagged impl-00 review-fix tests (`test_rf_*`, `test_cv_*`). New IDs would not be cited in any spec, which breaks T00-11 traceability. Reusing an existing ID on a second function is the U11-31 duplicate error.
   - So each body moved, assertions unchanged, into the ID test of the same unit, with the RF/CV note added to that test's docstring. "RF-n"/"CV-n" do not match the pattern.
   - Mapping:
     - rf_build_id_uses_utc_date → UT00-24
     - rf_utc_day_non_utc_input → UT00-17
     - rf_parse_iso_whitespace_and_lowercase_z → UT00-16
     - rf_display_strings_are_not_numbers → UT00-79
     - rf_hits_keep_sign_and_symbol → UT00-76
     - cv_2_scrubber_failure_never_leaks → ST00-01 (`reset_logging()`, then reconfigure with the raising scrubber in `<log dir>/raising`)
     - cv_3_no_handlers_avoids_last_resort → UT00-35 (`reset_logging()`, then `stderr=False`)
     - rf_logger_created_before_configure → UT00-40 (gained a `capsys` parameter)
5. **Report location:** the report could not be written to `D:\herness\.superpowers\...`, because the worktree-isolated agent is refused writes to the shared checkout. It is at this scratchpad path instead.

## Evidence

- RED: with `plugin.py` replaced by an empty module, `uv run pytest tests/unit/support -q -p no:logging` failed with `ImportError: cannot import name 'TEST_ID_PATTERN'`.
  - Honest note: the plugin was drafted before the tests. RED was produced afterwards by removing it.
  - Before the fold, the repo-wide `--require-test-ids` run failed with "tests without a test ID:" listing the 8 tests.
- GREEN: `uv run pytest tests/unit/support tests/integration/support -q -p no:logging` gave 8 passed.
- Acceptance:
  - `pytest -m unit tests/unit/support -q`: 7 passed.
  - `pytest --collect-only --collect-test-ids=build/test_ids.json` wrote the JSON (107 collected).
  - `mypy --strict tests/support`: 0 issues.
- Full suite `-m "(unit or integration) and not slow" -p no:logging`: 103 passed, 4 deselected, before and after. `tests/unit` without `-p no:logging`: 100 passed.
- Gates: `ruff format` and `ruff check` clean; `mypy` 0 issues in 12 files; `lint-imports` 4 kept, 0 broken; `check_type_ownership` exit 0. pyproject.toml is unchanged, because UT00-57 pins mypy `files`.

## Line budgets

| File | Lines | Budget |
|---|---|---|
| plugin.py | 211 | 250 |
| conftest.py | 34 | 80 |
| `__init__.py` | 1 | 5 |

## Concerns

- The missing T10-03/T10-04 resets and the registry half of UT11-37.
- The D:\herness main checkout has uncommitted edits to `test_logging.py` and `test_numbers.py`, which the fold also touches, so a merge conflict is possible. Re-apply the mapping above if one occurs.
- Existing Hypothesis tests now run the `commit` profile (200 examples, derandomized, 500 ms deadline). The unit suite is still about 4 s.
