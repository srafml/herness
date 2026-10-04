# T01-02 report: per-source settings and `SourcesConfig`

Status: DONE_WITH_CONCERNS
Commit: f225249 feat(connectors): add per-source settings and SourcesConfig (T01-02)
Worktree: D:\herness\.claude\worktrees\agent-af2bb9964cfba20fc

## Implemented
- `herness/connectors/settings.py` (314 lines): `_Source` (shared per-source rules: required or
  forbidden `base_url`/`auth`, `hosts_error()` hook for the R-06 SDK rules, source and entity
  `page_size` bounds, M5), `ServiceNowSettings`, `JiraSettings`, `MonitoringSettings`,
  `MongoSettings`, `SnowflakeSettings`, `DataverseSettings`, `FilesSettings`, `SourcesSection`,
  `SourcesConfig` (`enabled_sources()`, `source()`), `allowed_hosts`; re-exports
  `SourceSettings` and every entity/adapter name (`__all__`).
- `herness/connectors/settings_entities.py` (308 lines, NEW, see deviation 1): `ServiceNowEntity`,
  `JiraEntity`, `MetricQuery`, `MonitoringAdapterSettings`, `validate_spl`, adapter tool rules
  (`with_tool_defaults`, `adapter_error`), MongoDB filter walk (iterative, explicit stack),
  `MongoEntity`, `SnowflakeEntity`, `DataverseEntity`, `FilesEntity`.
- `herness/connectors/settings_base.py`: `_ReadOnlyDict.__reduce__` (N1) so copy/deepcopy/
  `model_copy(deep=True)` (and pickle) work for non-empty mappings. No other change.
- `max_concurrency` is not redeclared anywhere; per-source defaults come from the T01-01 hook.
  Monitoring adapters (not SourceSettings subclasses) get `CONCURRENCY_DEFAULTS[<tool>]` and the
  dynatrace page default 500 via `model_copy(update=...)` when the key is unset.
- `pyproject.toml`: one "settings modules are leaves" contract with sources `herness.model.settings`,
  `herness.connectors.settings`, `herness.connectors.settings_base`,
  `herness.connectors.settings_entities`; the duplicate `model-settings-light` contract is gone.
  Forbidden list adds `herness.model` and `herness.connectors` (import-linter skips entries that
  overlap a source, so these forbid connectors settings -> model.settings and the reverse; no
  exception). Verified: appending `import herness.model.settings` to settings.py breaks the
  contract ("herness.connectors.settings -> herness.model.settings").
- `tools/check_type_ownership.py` OWN050: settings modules are `settings.py` and `settings_*.py`;
  a settings module may import a sibling settings module of its own package. New test
  `test_ut00_73_settings_modules_include_settings_parts`.
- `tests/unit/repo/test_import_contracts.py` UT00-58: the leaves contract's `source_modules` must
  equal the set of `settings.py`/`settings_*.py` modules under `herness/`.

## Tests
- `tests/unit/connectors/test_settings_sources.py`: UT01-02, UT01-05 (M5 entity page bounds),
  UT01-06 ... UT01-12, UT01-66, ST01-07.
- `tests/unit/connectors/test_settings_config.py`: UT01-02 (design 01 §7 example YAML via
  `yaml.safe_load`, version, `source()`), UT01-05 (N1 copy/deepcopy/model_copy(deep=True)),
  UT01-13, UT01-97.
- `tests/unit/connectors/_settings_data.py`: shared valid minimal sections (not collected).
- `tests/integration/connectors/test_settings_imports.py`: R-03 acceptance, fresh interpreter,
  `sys.modules` diff after `import herness.connectors.settings` (integration marker: subprocess).

RED: `uv run pytest tests/unit/connectors/test_settings_sources.py tests/unit/connectors/test_settings_config.py ...`
-> `ImportError: cannot import name 'settings' from 'herness.connectors'` (2 collection errors).

GREEN:
- `pytest -k "UT01_02 or UT01_06 or UT01_07 or UT01_08 or UT01_09 or UT01_10 or UT01_11 or UT01_12 or UT01_13 or UT01_66 or UT01_97 or ST01_07"` -> 151 passed
- `pytest tests/unit/repo` -> 4 passed
- `pytest -m "(unit or integration) and not slow"` -> 418 passed
- Coverage (connectors tests, branch): settings.py 100 %, settings_entities.py 100 %, settings_base.py 100 %.

Gates: `ruff check .` clean; `ruff format --check .` clean; `mypy` 0 issues (23 files);
`lint-imports` 7 kept, 0 broken; `python -m tools.check_type_ownership` exit 0.

## Line counts vs budget
- settings.py + settings_entities.py = 622 lines against the §2 budget of 390 for settings.py.
  One module came to 574 lines after formatting (450 non-blank), over the 400 hard limit, so it
  was split (deviation 1). Each module is now under 400 (314 / 308).
- settings_base.py 321 (budget 260; already over at T01-01, +5 lines here for `__reduce__`).

## Deviations
1. New module `herness/connectors/settings_entities.py` (not in the §2 module map / card Files).
   Needed to respect the ENG 400-line hard limit; `herness.connectors.settings` re-exports all its
   public names, so the public API is the one the module map lists. The leaves contract, OWN050
   and UT00-58 were widened to cover it. Controller to rule on adding it to the spec's module map
   (and on the budgets).
2. Design 01 §7 example: validated with `version: 1` added (U01-14 makes it required), `hosts` added to
   `snowflake` and `dataverse` (R-06, U01-11/12 require them), a GUID for the `<guid>`
   placeholder, and `backfill.start: 2023-09-01` unquoted. The example quotes the date; YAML then
   yields a str, which strict mode rejects (T01-01 UT01-05 asserts that on purpose). I left
   T01-01's strict date rule alone. T10-03 should decide whether the loader accepts quoted ISO
   dates.
3. `hosts` is required for mongodb/snowflake/dataverse even when the section is disabled (spec text has no
   enabled-only exception); likewise required keys (`base_url`, `entities`, ...) apply to disabled sections.
4. Forbidden `auth` on files/monitoring is reported by the T01-01 base validator
   ("auth.method is not allowed for files"), which runs before the subclass validator.
5. `SourcesConfig.version` rejects YAML `true` (Literal[1] alone accepts True == 1).
6. Private helpers of settings_base (`_rule`, `_CONFIG`, `_check_base_url`, `_check_verify`,
   `_ReadOnlyDict`) are used from the sibling settings modules via `sb.` rather than being
   duplicated.
7. The Jira `jql_scope` rule matches `order\s+by` case-insensitively (catches `ORDER  BY`, tabs).
   The spec says "order by". Snowflake/ServiceNow filters follow the spec literally
   (ServiceNow tokens are case-sensitive).

## Deferred / not covered
- `herness config validate --profile synth --offline` acceptance check: deferred to T10-03
  (`herness.core.config.load_config` / `HernessConfig` do not exist yet, per program ruling).
- UT01-97 "while `http_client` still targets only the `base_url` host": `http_client` (U01-58)
  does not exist yet. That half is left to the HTTP-layer card.
- `FilesSettings.inbox` "resolved inbox must not be a symlink": resolution needs
  `cfg.paths.data` (T10-01), so the files connector must do it. Only the lax Path type is here.
- ValidationError -> ConfigError conversion is T10-03's job.

## Concerns
- Deviation 1 (extra module) and the budgets need a controller ruling.
- Deviation 2: whether quoted YAML dates should be accepted is for the T10-03 loader to settle.
