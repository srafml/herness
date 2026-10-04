# T04-02 report: Settings and config files

Status: DONE_WITH_CONCERNS
Commit: 9ff27a2 feat(metrics): add settings models, weight gate and config files (T04-02)
Worktree: D:\herness\.claude\worktrees\agent-a30cc2fcfdd6fba0c
(Written to the scratchpad because the worktree isolation blocked writing to D:\herness\.superpowers\sdd\program\briefs\.)

## What was implemented
- `herness/metrics/settings.py`:
  - U04-14: the literal types.
  - U04-15: `MetricDef`.
  - U04-16: `MetricsDefaults`.
  - U04-17: `ScoringConfig` with its nested models `TierWeights`, `FundingScoring`, `OrgScoring`, `PeerGroupScoring` and `LeverScoring`.
  - U04-18: `MetricsCatalogConfig`, which rejects duplicates with "duplicate metric <name>".
  - U04-19: `WeightsConfig`. It has 12 block models, each inheriting `WeightBlock.unconfirmed` (default true), plus `PortfolioConfig`, `ScenarioConfig`, `SolverConfig` and `blocks()`.
  - U04-20: `WEIGHT_BLOCKS`, `WEIGHT_USES` (a read-only mapping; unions keep first-seen order) and `unconfirmed_blocks`.
  - U04-21: `WeightChange` and `WeightChangePayload`.
  - U04-82: `WeightIssue`, a frozen dataclass whose `__post_init__` raises `ConfigError("bad weight issue")`.
  - U04-22: `check_weight_confirmations` (positional-only).
  - Every model is frozen, forbids extra fields and is strict. Decimal fields accept YAML ints, floats with at most 2 decimals, and decimal strings; bool, NaN and inf are rejected. `clip` accepts a YAML list, converted to a tuple.
- `config/weights.yaml`: the values from design 04 §7.2, verbatim.
- `config/metrics.yaml`: `version`, `defaults` (design §4.1 plus `compute_timeout_s: 30.0`) and `scoring` (§7.1). Every lever template is the §5.9 example sentence. `metrics: []` is a placeholder for T04-08…T04-11.
- `pyproject.toml`: new import-linter contract C6 "settings modules are leaves" (U00-52). Its source is `herness.metrics.settings`. It forbids every other existing herness module except `core.types` and `core.errors`, with indirect imports allowed. The third-party part of the rule is covered by OWN050 in `check_type_ownership`, and by the UT04-119 import scan.
- Tests: `tests/unit/metrics/test_metrics_settings.py`, 72 tests covering UT04-15, UT04-19, UT04-20, UT04-119 and ST04-05.

## Evidence
- RED: `uv run pytest tests/unit/metrics/test_metrics_settings.py -q` gave `ImportError: cannot import name 'settings' from 'herness.metrics'` (1 error during collection).
- GREEN: the same tests gave 72 passed. Coverage of `settings.py` is 100% of lines and 100% of branches.
- Full suite: `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` gave 292 passed, 4 deselected.
- Gates:
  - `ruff format --check`: clean.
  - `ruff check`: clean.
  - `mypy`: 0 errors in 16 files.
  - `lint-imports`: 6 contracts kept, 0 broken (C6 included).
  - `check_type_ownership`: exit 0.

## Line counts
- `settings.py`: 454 lines against a budget of 390 and the ENG hard limit of 400. It is OVER both.
- `config/metrics.yaml`: 44 lines. It grows to about 760 with T04-08…T04-11.
- `config/weights.yaml`: 31 lines against a budget of 45.

## Deviations and concerns
1. **`settings.py` is 454 lines, over the 390 budget and the 400 ENG limit.**
   - I compressed it honestly: shared `Annotated` validators through `_rule`, plain defaults instead of factories, grouped aliases, no docstrings on small nested models, and `# fmt: off` only around the literal aliases and the `WEIGHT_USES` table.
   - What remains is about 350 lines of spec-mandated content (about 27 pydantic models, the payload, the gate and the table) plus 104 blank lines that `ruff format` requires between 37 top-level definitions.
   - I did not obfuscate the code or split the file outside the card's file list.
   - Suggested resolution, for the coordinator to decide:
     - (a) Move the weights models into a private `herness/metrics/_weights_settings.py` re-exported by `settings.py`. C6 and OWN050 would then need to cover it, because OWN050 only scans files named `settings.py`.
     - (b) Raise the budget for this module.
2. **The spec says "14 blocks", but only 12 exist.** U04-19 and the §9 table say 14, but U04-19's own field list and design §7.2 define 12 blocks. I implemented those 12 and exposed them as `WEIGHT_BLOCKS`.
3. **`config/metrics.yaml` does not load yet.** It has `metrics: []`, and `MetricsCatalogConfig` requires 1–200 metrics, so the whole file fails validation until T04-08 adds entries. The tests validate the `defaults` and `scoring` sections separately, and validate the root model with one test metric.
4. **Interpretations the spec leaves open:**
   - `unconfirmed` defaults to true when missing; this is the safe default, per the §9 table.
   - `WeightChangePayload.proposed_config_hash`, `memory_id` and `changes` default to None, None and [].
   - `memory_id` is required when origin is `memory`, and forbidden otherwise.
   - `MetricDef.uses_weights` is not checked against block names here; that check is U04-26 step 1.
   - `WeightIssue.__post_init__` checks severity and the path and message lengths. The path pattern holds by construction in U04-22.
   - `ScoringConfig.org` and `levers` are required because they have no spec defaults. `funding` and `peer_group` default.

## Fix round 1 (commit 254df03, on top of 891b6ba)

### Important-1: size (split, following the controller's ruling)
- **New module `herness/metrics/_weights_settings.py`, 249 lines.** It holds:
  - the shared primitives `Model`, `rule`, `Money`, `Fraction`, `Positive`, `_to_decimal`, `_is_zone` and `_PriorityMap`;
  - `WeightBlock` and its 12 block models;
  - `ScenarioConfig`, `SolverConfig`, `PortfolioConfig` and `WeightsConfig`;
  - `WEIGHT_BLOCKS`.
- **`herness/metrics/settings.py` is now 320 lines, under the 390 budget.** It holds:
  - the literals and the metrics.yaml models;
  - `WEIGHT_USES` and `unconfirmed_blocks`;
  - `WeightChange`, `WeightChangePayload`, `WeightIssue` and `check_weight_confirmations`;
  - re-exports of `WeightsConfig`, `PortfolioConfig`, `ScenarioConfig` and `SolverConfig`;
  - an explicit `__all__` that equals the module-map export list, plus the spec-named `Domain`, `FilterKey`, `Source`, the scoring sub-models and `WeightChange`.
- Both `# fmt: off` blocks are gone.
- **OWN050 in `tools/check_type_ownership.py`:**
  - It now scans `settings.py` and every `*_settings.py` file.
  - A settings module may import a private `_*_settings` module from its own package. Any other herness import is still OWN050.
  - New test: `test_ut00_73_private_sibling_settings`. It checks that the sibling is scanned (httpx flagged), and that a cross-package `_other_settings` import and a non-settings sibling import are still flagged.
  - This is a delta to impl 00 U00-47 step 6, which the impl-00 spec should record.
- **C6:** `source_modules` = [`herness.metrics.settings`, `herness.metrics._weights_settings`]. Neither module appears in `forbidden_modules`.
- **UT00-58 (`tests/unit/repo/test_import_contracts.py`):** it now globs `settings.py` and `*_settings.py`. It asserts that C6 `source_modules` equals the set of settings modules, and that no settings module is in `forbidden_modules`.
- **UT04-119 import scan:** parametrized over both files. It allows stdlib, `pydantic`, `herness.core.types`, `herness.core.errors` and the sibling module. It asserts that only settings.py imports the sibling.
- **Proposed module-map row for impl 04 §2:**
  `| herness/metrics/_weights_settings.py | Shared strict/frozen model base and validators; weights.yaml section models (U04-19) | Model, rule, Money, Fraction, Positive, WeightBlock, WeightsConfig, PortfolioConfig, ScenarioConfig, SolverConfig, WEIGHT_BLOCKS (private module; public names re-exported by settings.py) | L3 (settings exception, R-03) | only the standard library, pydantic, herness.core.types and herness.core.errors | 250 |`
  The settings.py row keeps budget 390 and adds "and its private sibling `_weights_settings`" to its allowed imports.

### Minor items
- **M-1 (mutable nested containers):** fixed.
  - The base `Model` converts YAML lists to tuples before validation and wraps validated dicts in `MappingProxyType`, with `validate_default=True` so defaults are frozen too.
  - The field types are now `tuple[...]` and `Mapping[...]`, the read-only forms of the spec's `list` and `dict`.
  - New test `test_ut04_19_loaded_config_is_read_only`.
- **M-2 (test IDs):** fixed. The two U04-20 tests are now `test_ut04_68_unconfirmed_blocks` and `test_ut04_99_weight_uses_table`, with docstrings "UT04-68 (U04-20 part)" and "UT04-99 (U04-20 part)". I checked the spec: UT04-68 lists U04-40 and U04-20, and UT04-99 covers the per-model lever `unconfirmed` flags (U04-69, U04-70), which read `WEIGHT_USES`.
- **M-3 (import-scan allow-list):** fixed. The UT04-119 scan now allows stdlib and `pydantic` only. `typing_extensions` and `annotated_types` are gone; neither module imports them.
- **M-4 (extra public names):** fixed. The block models and primitive aliases live only in the private module, and settings.py declares `__all__`. `WEIGHT_BLOCKS` is not exported from settings.py.
- **M-5 (decimal precision asymmetry):** fixed. A comment on `_DECIMAL_TEXT` now explains that quoted decimal text keeps any precision, while floats are limited to cents.

### Records for the spec owner
- **Erratum:** U04-19 (`blocks()` "returns the 14 blocks") and the §9 table ("14 blocks") should say 12. Design §7.2 and the U04-19 field list define 12 blocks.
- **Dependency:** `config/metrics.yaml` validates as a whole only after T04-08 adds catalog entries (`metrics: []` today, and the minimum is 1). Any config-loader or smoke card that loads the whole file must be ordered after T04-08.
- **Delta:** OWN050 (impl 00 U00-47 step 6) now covers `*_settings.py` and the private-sibling import.

### Evidence
- `ruff format --check`: clean. `ruff check`: clean.
- `mypy`: 0 errors in 18 files.
- `lint-imports`: 6 kept, 0 broken.
- `check_type_ownership`: exit 0.
- Card tests: 74 passed. Coverage is 100% of lines and branches for both `settings.py` and `_weights_settings.py`.
- Tool and repo tests pass.
- Full suite: `pytest -m "(unit or integration) and not slow"` gave 321 passed, 4 deselected.

## Fix round 2 (staged on top of b5871bb, NOT committed: pre-commit hook blocked)

### Commit status
- `git commit` is refused by the pre-commit hook installed in the shared `D:/herness/.git/hooks`. The hook reports "No .pre-commit-config.yaml file was found", and this branch has no such file.
- Getting past it would need `--no-verify` or `PRE_COMMIT_ALLOW_NO_CONFIG=1`, which skips hooks. That needs the user's approval, so I did not do it.
- All changes are staged. Planned subject: `fix(metrics): restore spec container types and scan settings packages (T04-02)`.

### C-1 / I-1: read-only containers reverted (controller ruling)
- I removed the wildcard validators, `validate_default` and `_DEFAULT_WINDOWS`. The fields use the spec's `list[...]` and `dict[...]` types again.
- The defaults use typed `default_factory` functions. Plain literal defaults would trigger RUF012, because ruff does not recognise the imported `Model` base as pydantic.
- `clip` keeps its spec type `tuple[float, float]`, with a before-validator that turns the YAML list into a tuple.
- `WEIGHT_USES` stays a `MappingProxyType`. It is a module constant, not model data.
- I removed `test_ut04_19_loaded_config_is_read_only`.
- **New test `test_ut04_19_models_round_trip_json_and_deepcopy`**, marked `filterwarnings("error")`. It checks that `model_dump_json` followed by `model_validate_json` gives an equal model, for:
  - `WeightsConfig`;
  - `MetricsCatalogConfig`, including `defaults` and `scoring`;
  - `WeightChangePayload`.
  It also checks that `copy.deepcopy` and `model_copy(deep=True)` give equal models for all five. The Decimal JSON form (`"95"`) and the JSON string keys of the int-keyed dicts both validate back in strict mode.
- **Parked spec note:** frozen models stop field reassignment, but nested lists and dicts can still be changed in place (for example `weights.toil.effort_factor[1] = 99`). The spec's "Immutable" means field-level only. If deep immutability is wanted, the spec should say so and choose a type that serializes, such as tuples or a frozen-dict type with a JSON serializer.

### m-1: private settings packages are now scanned
- `_is_settings_file` in `tools/check_type_ownership.py` now scans every file whose path relative to the root has a component named `settings` or ending in `_settings`. That covers settings packages and all of their submodules, not just `__init__.py`.
- New test `test_ut00_73_private_settings_package_is_scanned`: it checks that httpx in `_x_settings/__init__.py` and duckdb in `_x_settings/models.py` are flagged, while a non-settings module is not.
- UT00-58 (`test_import_contracts.py`) now also finds `settings` and `*_settings` packages, and requires them in C6 `source_modules`.

### m-2
- C6 `forbidden_modules` now includes `herness.metrics._encode_nested`, which T04-01 added.

### m-3: deltas for the spec owners
- **impl 00, U00-47 step 6 and UT00-73:** OWN050 now:
  - scans `settings.py`, `*_settings.py` and every file inside a `settings` or `*_settings` package;
  - lets a settings module import a private `_*_settings` module or package from its own package.
  UT00-73 gains two tests: `test_ut00_73_private_sibling_settings` and `test_ut00_73_private_settings_package_is_scanned`.
- **impl 00, U00-52 (C6):** `source_modules` lists every settings module, including private siblings.
- **impl 04 §2 module map:** add a row for `herness/metrics/_weights_settings.py` (proposed in fix round 1), and allow `settings.py` to import it.
- **impl 04 errata:** "14 blocks" should read 12 (U04-19 and the §9 table).
- **Dependency:** `config/metrics.yaml` validates as a whole only after T04-08.
- **Parked:** the note above on nested-container mutability.

### Evidence
- `ruff format --check` and `ruff check`: clean.
- `mypy`: 0 errors in 18 files.
- `lint-imports`: 6 kept.
- `check_type_ownership`: exit 0.
- Card, tool and repo tests: 90 passed. Coverage is 100% of lines and branches for `settings.py` and `_weights_settings.py`.
- Full suite (unit or integration, not slow): 320 passed, 4 deselected.
- Line counts: `settings.py` is 328 lines (budget 390); `_weights_settings.py` is 235.
- All tools were run from `.venv\Scripts`, because `uv run` parses `D:\herness\pyproject.toml`, which has merge-conflict markers. I did not touch that file.
