# T02-01 Store and model foundations: build report

Status: DONE_WITH_CONCERNS
Commit: 6f2ca35 `feat(store,model): add store and model foundations and settings (T02-01)` on branch worktree-agent-a0ccbcfed583b480d

## Files
- herness/store/__init__.py (1 line, budget 5)
- herness/store/errors.py (67 lines, budget 60, over by 7): NotFoundError (U02-01), ReviewItemConflict (U02-02), LakeContractError (U02-03), LakeStateError (U02-04), MigrationError (U02-05)
- herness/store/layout.py (68, budget 70): DataLayout (U02-06), data_layout (U02-07)
- herness/model/__init__.py (1, budget 5)
- herness/model/errors.py (40, budget 40): BuildSqlError (U02-76), DqGateFailed (U02-77)
- herness/model/settings.py (217, budget 240): ServiceOverride (U02-71), CustomFieldsConfig with ServiceNowCustomFields and JiraCustomFields (U02-72), MappingsConfig and ENUM_DOMAINS (U02-73), DqSettings (U02-74), BuildSettings (U02-75)
- pyproject.toml: import-linter contracts. C1 `herness layers` is now model, store, core. Added C4 `core base is closed` (forbids store and model), C6 `settings modules are leaves`, `model-settings-light` and `store-no-upward` (forbids herness.model)
- tests/unit/store/test_store_layout.py, tests/unit/store/test_store_errors.py, tests/unit/model/test_model_settings.py, tests/unit/model/test_model_errors.py
- tests/integration/repo/test_import_contracts_enforced.py (ST00-10, impl 00): the test assumed C1 held only `herness.core` and that no `core base is closed` contract existed. It now inserts the planted package generically: at the top of the first layers contract and into the existing C4 forbidden list. Without this change, the pyproject update breaks ST00-10.

## Tests
- UT02-53, UT02-54, UT02-66: all implemented, 52 test cases.
- The error classes have no test ID on this card. For coverage, I added tests for the error-class part of their unit test IDs: UT02-45, UT02-02, UT02-10, UT02-34, ST02-14 and IT02-29. Each docstring says "(error-class part)". Later cards add the full behaviour tests.
- RED: `uv run pytest tests/unit/store tests/unit/model` failed with `ModuleNotFoundError: No module named 'herness.model'` (4 collection errors).
- GREEN: 60 passed in the new files.

## Gates (all clean)
- ruff format --check: 39 files already formatted. ruff check: All checks passed.
- mypy: no issues in 18 files. mypy --strict herness/store herness/model: no issues in 6 files.
- lint-imports: 8 kept, 0 broken (includes model-settings-light).
- tools.check_type_ownership: exit 0.
- `pytest -k "UT02_53 or UT02_54 or UT02_66"`: 52 passed.
- Unit suite `-m "(unit or integration) and not slow"`: 163 passed, 4 deselected.
- Coverage of the new modules: 100% line and 100% branch.

## Deviations and interpretations
1. **data_layout without HernessConfig (T10-03 missing).** `cfg` is typed with a local structural Protocol, `_ConfigWithPaths` (`cfg.paths.data`). When both `cfg` and `root` are None, the function calls `importlib.import_module("herness.core.config").get_config()` at call time. There is no static import and no stub. Until T10-03 lands, that one path raises ModuleNotFoundError. Every current caller and test passes `root` or `cfg`, and the test covers the lazy path with a fake module. T10-03 should replace the Protocol with `HernessConfig` and the lazy lookup with a direct import.
2. **Record-ID pattern for org_id.** R-03 bars the settings module from importing herness.core.ids. So `org_id`, `team_id` and `service_id` share the U02-71 pattern restated locally: `^[a-z][a-z0-9_]*:[a-z0-9_]+:[^\s]{1,200}$`.
3. **"No control characters"** means Unicode Cc: U+0000–U+001F and U+007F–U+009F.
4. **BuildSqlError sanitising** runs in the constructor, in this order: take the first line; mask every literal, including an unclosed trailing quote, as `'?'`; then cap the text after the first `": "` at 300 characters. Masking before truncating keeps a cut literal from leaking.
5. **DqGateFailed** joins the check names with ", ".
6. **model-settings-light and C6** can only forbid herness modules, because `include_external_packages = false`. The stdlib-and-pydantic-only part is enforced by tools.check_type_ownership (OWN050), as the impl 00 C6 row says.
7. **N818 on ReviewItemConflict** is silenced inline with a reason (the spec fixes the name). The error classes keep their extra attributes in `_extra_attrs`, so pickling works, as in core errors.
8. **Hyphenated `-k` filter.** The acceptance command `pytest -k "UT02-53 or ..."` selects 0 tests: pytest's -k does not match hyphenated IDs, and the same is true of the existing impl 00 tests (for example `-k UT00-55`). The underscore form (`-k UT02_53`), which the global constraints give, selects all of them.

## Concerns
- herness/store/errors.py is 67 lines against a 60-line budget. Five classes with their required fields and ruff-format spacing cannot be made shorter.
- Deviation 1 (lazy config lookup) needs follow-up in T10-03.
- The shared impl 00 test ST00-10 was changed (see Files).
