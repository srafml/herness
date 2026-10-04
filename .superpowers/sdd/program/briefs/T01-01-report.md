# T01-01 report: Common settings models

Status: DONE_WITH_CONCERNS
Commit: 39c65e8 feat(connectors): add common settings models (T01-01)
Worktree: D:\herness\.claude\worktrees\agent-ab02c497cc96341fa

This report was written to the worktree copy of the briefs folder. The isolation guard blocked the shared D:\herness path. The file is not committed.

## What I built
- `herness/connectors/__init__.py`: a package marker with a docstring only.
- `herness/connectors/settings_base.py`:
  - `AuthSettings` (U01-01)
  - `ReconcileSettings` (U01-02)
  - `BackfillSettings` with `resolve_start` (U01-03)
  - `EntitySettings` (U01-04)
  - `SourceSettings` (U01-05) with `auth_key`, `entity`, `overlap_for`, `page_size_for`, `backfill_for` and `httpx_verify`
  - the constants `SECRET_REF_PATTERN`, `AUTH_METHODS`, `CONCURRENCY_DEFAULTS`, `CONCURRENCY_CAPS`, `DAILY_METRIC_NAMES` and `SERVICENOW_ENTITIES` (U01-06). They are wrapped in `MappingProxyType` or are a `frozenset`.
- The module imports only the standard library, pydantic and `herness.core.errors` (R-03).
- Every validator raises `ValueError` naming the key. `resolve_start` and `entity()` raise `ConfigError`, as the spec says.
- `pyproject.toml`:
  - C1 "herness layers" now has two layers: `herness.connectors` above `herness.core`.
  - I added C4 "core base is closed", which impl 00 U00-52 requires once a second top-level package exists (UT00-58 checks it). Its sources are the seven core-base modules, and it forbids `herness.connectors`.
- `tests/unit/connectors/test_settings_base.py`: covers UT01-01, UT01-03, UT01-04, UT01-05, ST01-01 (the config part) and the R-03 import check.
  - The R-03 check has two parts. One purges `herness*` from `sys.modules` in-process, imports the module and lists what got loaded, then restores `sys.modules`. The other does an AST check of the module's import statements.
- `tests/integration/repo/test_import_contracts_enforced.py` (ST00-10, an impl-00 test): I made it robust. It used to replace the exact text `layers = [\n "herness.core",\n]`, which stopped matching once C1 had two layers, so lint-imports passed and the test failed. It now adds `herness.harness` to the top of C1 and to the C4 `forbidden_modules`, whatever layers already exist. What it asserts has not changed.

## Settings exception decision (ruling 2)
The spec is explicit, so I did not add an ignore for `settings_base`:
- Impl 00 U00-52 says C1 `ignore_imports` "holds exactly one entry", and UT00-58 asserts that exact list.
- Impl 01 §2 (line 99) and §13 O-15 say `settings_base` goes into the **settings import contract** (C6), together with `settings`. The T01-02 card's Files row lists exactly that pyproject change.

I did not add C6 in this card. UT00-58 requires C6 to exist if and only if a `herness/**/settings.py` exists, and `settings_base.py` does not match that glob. **T01-02 must add C6 listing both modules.** The UT00-58 glob and OWN050 in `tools/check_type_ownership.py` only look at `settings.py`, so they will not cover `settings_base.py` automatically. T01-02, or a follow-up, should widen them.
- The C1 settings exception does not need a second pattern. T10-03 imports `herness.connectors.settings` (that edge is ignored), and `settings_base` is only reached through it.

## Deviations from the brief
1. **`hide_input_in_errors=True` added to the model config.** Pydantic's `ValidationError` text includes `input_value=...` by default, so UT01-01 ("message lacks synthetic-hunter2") cannot pass without it. Note for T10-03: `ValidationError.errors()` still carries `input`, so the loader must not copy it into a `ConfigError`.
2. **Float fields stay strict. They do not use `Field(strict=False)`.** Pydantic's strict float already accepts YAML ints (the stated reason for the convention). Lax float would also accept `True`, and strings like `"2.0"`. Strict mode rejects both, and the tests cover it.
3. **`SourceSettings.page_size` default is 1000, with only a lower bound of 1.** The spec gives no base value. `FilesSettings` (U01-13) does not redeclare it, so the base needs a default.
4. **How the `max_concurrency` default is filled.** It is declared `int = 0`. A `mode="before"` model validator fills in `CONCURRENCY_DEFAULTS[SOURCE]` when the key is absent, and raises when `SOURCE` is not a known connector. As a side effect, `max_concurrency` always appears in `model_fields_set`.
5. **Schedule rule.** "Five whitespace-separated fields" is implemented as space or tab separators only, anchored with `fullmatch`. A newline or a leading or trailing blank is rejected.
6. **Missing `verify` path.** It gets the TLS message, as UT01-03 expects.
7. **Messages for rules the spec leaves unworded.** Hosts errors read "hosts entry {i} is not a host name", "hosts entry {i} is a duplicate" and "hosts has more than 50 entries". An auth method outside the allowed set reads "auth.method is not allowed for {auth_key}", without the value.
8. **ST01-01.** Only the config part is covered. The TLS-connection part needs the HTTP layer and egress, which are later cards.

## TDD evidence
- RED: `uv run pytest tests/unit/connectors -q -p no:logging` failed with `ModuleNotFoundError: No module named 'herness.connectors'` and 1 collection error.
- GREEN: the same command gives 66 passed. Coverage of `settings_base` is 100% line and 100% branch (204 statements, 54 branches).
- The acceptance selection `uv run pytest -m unit -k "UT01_01 or UT01_03 or UT01_04 or UT01_05 or ST01_01"` gives 66 passed.

## Gates (all clean at the commit)
- `ruff format --check`: 32 files already formatted
- `ruff check`: all checks passed
- `mypy` (strict): no issues in 14 source files
- `lint-imports`: 5 contracts kept, 0 broken
- `check_type_ownership`: exit 0
- `pytest -m "(unit or integration) and not slow" -q -p no:logging`: 169 passed, 4 deselected

## Line counts vs budget
- `herness/connectors/__init__.py`: 1 line (budget 10)
- `herness/connectors/settings_base.py`: **312 lines against a budget of 260** (ENG hard limit 400). I already cut it from 388 lines by compacting the constant tables and sharing the range and schedule validator factories.

## Fix round 1 (commit 1efbeb5 fix(connectors): address T01-01 review round 1 (T01-01))
- **I1: done.** The R-03 runtime check now runs `import herness.connectors.settings_base` in a subprocess (a fresh interpreter). It asserts that every newly loaded module is in one of these groups:
  - the standard library
  - pydantic and its dependencies: pydantic_core, typing_extensions, typing_inspection and annotated_types
  - `herness`, `herness.connectors`, `herness.connectors.settings_base`, `herness.core`, `herness.core.errors` or `herness.core.types*`

  It also asserts that `pydantic` appears among the new modules, which proves third-party imports are actually observed. The test is in `tests/integration/connectors/test_settings_base_imports.py` with the **integration** marker, because the unit marker forbids subprocesses. The source-level (AST) check stays in the unit tests. As a result, the acceptance command `-m unit` no longer includes the runtime half.
- **M1: not reachable.** `settings_base.py` is **316 lines against a budget of 260**. The fix round added the per-subclass default hook and the read-only mapping, and the shared `_rule` validator factory absorbed part of that. Reaching 260 would mean dropping docstrings or spec-required validators. **I request a budget amendment to 320** (the hard limit is 400).
- **M2: done.** `max_concurrency` has no default on the base class, so it is required there. `__pydantic_init_subclass__` sets the default to `CONCURRENCY_DEFAULTS[SOURCE]` on each subclass and rebuilds it. `model_dump(exclude_unset=True)` now returns `{}`, and the JSON schema default is the real value. Tests cover a grandchild subclass and the schema.
- **M3: done.** A range with no upper bound reads "page_size must be >= 1". Bounded ranges read "... >= 1 and <= 600".
- **M4: done.** `entities` is converted to `_ReadOnlyDict` in the after validator, so the conversion also applies to subclasses that narrow the field. It is a dict subclass whose mutators raise `TypeError`. I chose it over `MappingProxyType`, which pydantic cannot serialize to JSON. A test covers mutation attempts and serialization.
- **M6: done.** Removed the no-op `.replace`.
- **M7: done.** The unit tests are split into `test_settings_base_auth.py` (221 lines) and `test_settings_base_models.py` (273 lines).
- **M8: done.** The R-03 tests are now named `test_ut01_01_r03_...`, and their docstrings say "(R-03 acceptance check)".
- **Gates:**
  - ruff and ruff format: clean
  - mypy: clean
  - lint-imports: 5 kept, 0 broken
  - check_type_ownership: exit 0
  - connector tests: 69 passed (68 unit and 1 integration), with 100% line and branch coverage of settings_base
  - full fast suite: 172 passed, 4 deselected

## Concerns
- `settings_base.py` is 316 lines against a budget of 260 (see M1; amendment requested), though under the hard limit.
- The R-03 runtime check is an integration test (it uses a subprocess), so `-m unit` covers only the AST half.
- T01-02 must add C6 listing both settings modules. UT00-58 and OWN050 look only at files named `settings.py`, so `settings_base.py` is not machine-checked for R-03 beyond this card's own two tests.
- I edited the impl-00 test ST00-10 to match the grown contracts; its assertions are unchanged.
- T10-03 must not copy `errors()[i]["input"]` into `ConfigError`.
