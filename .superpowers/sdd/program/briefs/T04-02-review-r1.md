# T04-02 re-review, fix round 1 (891b6ba..254df03)

**Verdict: Needs fixes.** Important-1 and M-2 through M-5 are resolved correctly. The M-1 fix, however, introduces a Critical regression: loaded configs can no longer be serialized to JSON.

## Gates I re-ran (worktree at 254df03; T04-01 files had uncommitted edits, which I ignored)

| Gate | Result |
|------|--------|
| `pytest` on test_metrics_settings.py, test_check_type_ownership.py and test_import_contracts.py | 86 passed |
| `lint-imports` | 6 kept, 0 broken |
| `tools/check_type_ownership.py` | exit 0 |
| `ruff check`, `ruff format --check` and `mypy` on the two settings modules | clean |
| Line counts | settings.py 320 (budget 390); _weights_settings.py 249 (proposed 250) |

## Prior findings

**Important-1 (size split): ✅ resolved**
- **Split:** the split follows the ruling. Shared primitives and the weights models are in the private module. The gate, the payload, `WEIGHT_USES` and `WeightIssue` stay in settings.py. There is no import cycle, and both `# fmt: off` blocks are gone.
- **OWN050:**
  - The rule now scans `*_settings.py`. It allows a settings module to import a private `_*_settings` module only when that module is in the same package (tools/check_type_ownership.py:282-299, 323).
  - The new test covers three cases: the sibling module is scanned, a cross-package `_other_settings` import is flagged, and a non-settings sibling import is flagged.
- **C6:** `source_modules` lists both modules, and neither is forbidden.
- **UT00-58:** the test globs `*_settings.py` and asserts that `source_modules` equals the set of settings modules.
- **UT04-119:** the scan is parametrized over both files, and only settings.py may import the sibling.
- **C1 still holds.** The `herness.core.config -> herness.**.settings` ignore only removes that one edge, so there is no core → `_weights_settings` chain. core.config does not need the private module, because settings.py re-exports its names.

**M-1 (mutable nested containers): ❌ resolved, but it caused a regression** (see C-1 and I-1 below)
- **Tuples:** the list fields are now tuples, which is fine.
- **Maps:** the `MappingProxyType` wrapping breaks serialization and copying.

**M-2 (test IDs): ✅.** UT04-68 lists U04-40 and U04-20, and UT04-99 covers the per-model `unconfirmed` flags (impl 04 lines 2070 and 2095). Both test names are now correct.

**M-3 (import-scan allow-list): ✅.** The allow-list is now stdlib and `pydantic` only.

**M-4 (extra public names): ✅.** `__all__` is declared (settings.py:31), the block classes and aliases stay private, and `WEIGHT_BLOCKS` is not exported.

**M-5 (decimal precision comment): ✅.** The comment is at _weights_settings.py:24-25.

## Builder's concerns

**1. Is OWN050 loosened safely?** Yes. The net effect is stricter, not looser:
- **More files scanned.** Every `*_settings.py` file is scanned now, where before only `settings.py` was.
- **One narrow new allowance.** A settings module may import a leading-underscore `*_settings` module from its own package, and that module is itself scanned under the same rule. A non-settings module cannot get through:
  - `_private_sibling_settings` requires `leaf.startswith("_")` and `leaf.endswith("_settings")`, with the same parent package.
  - A public sibling such as `herness.metrics.settings`, imported from the private module, is still flagged.
  - `from herness.metrics import _weights_settings` resolves to `herness.metrics` and is flagged. This is a conservative false positive, which is acceptable.
- **One small gap** (Minor m-1): a *package* named `_foo_settings/` (with an `__init__.py`) would be allowed as an import target but never scanned. `_is_settings_file` tests `path.name`, and UT00-58 uses `rglob("*settings.py")`, so both miss a package.
- **Spec delta:** the change must be recorded as a delta to impl 00 U00-47 step 6, which says "every file named `settings.py`", and to UT00-73.

**2. Types changed to tuple and Mapping: do they still meet the spec, and do dump and hash still work?**
- **Spec fit:** `tuple` and `Mapping` are read-only forms of `list` and `dict`, and every consumer that iterates or reads them keeps working.
  - Equality with list literals now fails, for example `defaults.exclude_incident_states == ["canceled"]`.
  - `isinstance(x, dict)` checks now fail too.
  - Record this as a delta to U04-15 and U04-19, which say "fields exactly as design 04 §3.1: `list[...]`/`dict[...]`".
- **Serialization and copying:** they do **not** work. I probed this against config/weights.yaml and pydantic in this venv:

| Operation | Pre-fix (891b6ba) | Post-fix (254df03) |
|-----------|-------------------|--------------------|
| `WeightsConfig.model_dump()` | dict | returns `mappingproxy` values and emits 8 `PydanticSerializationUnexpectedValue` warnings |
| `model_dump(mode="json")` | ok | **PydanticSerializationError: Unable to serialize unknown type: mappingproxy** |
| `model_dump_json()` | ok | **same error** |
| `MetricsDefaults().model_dump_json()` | ok | **same error**, so even the defaults cannot be serialized |
| `pickle`, `copy.deepcopy`, `model_copy(deep=True)` | ok | **TypeError: cannot pickle 'mappingproxy'** |
| `hash(config)` | TypeError (unhashable dict) | TypeError, so no change |
| `==`, and re-validating `model_dump()` output | ok | ok |

## New issues

### Critical
**C-1. Loaded config can no longer be serialized to JSON**
- **Where:** herness/metrics/_weights_settings.py:88-90 (`_read_only_maps`) and herness/metrics/settings.py:91 and 126 (`_DEFAULT_WINDOWS`).
- **What breaks:** every model that has a map field now fails `model_dump(mode="json")` and `model_dump_json()`. That covers `WeightsConfig`, `MetricsDefaults`, `ScoringConfig` and `MetricsCatalogConfig`.
- **Why it matters:** specified downstream contracts depend on this call:
  - U10-12 `effective_dict` starts with `cfg.model_dump(mode="json")`, and it feeds `config_hash`, `config show` and snapshots (impl 10 lines 326-336 and 318).
  - The U04-23 `MetricCatalog.version` is `sha256_hex(canonical_json(config.model_dump(mode="json")))[:12]` (impl 04 line 548).
  - With this change, spec 10 cannot hash any config that contains `weights` or `metrics`.
- **Why the gates missed it:** no test serializes a model. The pydantic warnings come from the `pydantic` module, so `filterwarnings = ["error:::herness"]` does not turn them into errors.
- **Fix, option (a), preferred and simplest:** keep the tuples, which serialize, pickle and copy fine, and revert the map fields to plain `dict` with no proxy.
  - M-1 was only a Minor, and frozen models already block attribute rebinding.
  - Record "nested dicts remain mutable" as a known limit, or deep-freeze a copy at `config_hash` time in spec 10.
- **Fix, option (b):** keep `MappingProxyType` and add a wrap serializer to `Model`. I verified that this makes `model_dump`, `mode="json"` and `model_dump_json` work:

  ```python
  @field_serializer("*", mode="wrap")
  def _plain_maps(self, value: Any, handler: SerializerFunctionWrapHandler) -> Any:
      return handler(dict(value) if isinstance(value, types.MappingProxyType) else value)
  ```

  Option (b) still leaves I-1 open.
- **Regression tests, needed for either option:**
  - `model_dump(mode="json")` and `model_dump_json()` round-trip for a full `WeightsConfig`, `MetricsDefaults()` and a `ScoringConfig` built from config/metrics.yaml.
  - `copy.deepcopy` of a full config.
  - Run the round-trip test under `warnings.simplefilter("error")` so serializer warnings fail it.

### Important
**I-1. `pickle`, `copy.deepcopy` and `model_copy(deep=True)` now raise TypeError on any config with a map field**
- **Where:** same lines as C-1.
- **Impact:** configs could be copied and pickled before this change. Test fixtures that derive a config with a deep `model_copy`, and any process-spawn path that pickles a config, will fail.
- **Fix:** option (a) resolves it. With option (b), `Model` would also need `__deepcopy__` and `__reduce__` handling, or a read-only `dict` subclass (with `__reduce__`, and mutators that raise) instead of `MappingProxyType`.

### Minor
- **m-1. A private settings *package* can bypass the scan** (tools/check_type_ownership.py:282-290, tests/unit/repo/test_import_contracts.py:62).
  - A package `herness/<pkg>/_x_settings/__init__.py` passes `_private_sibling_settings`, but neither `_is_settings_file` nor the UT00-58 glob finds it, so its imports are never checked.
  - The fix is either to reject the case (a sibling must be a `.py` module) or to treat `__init__.py` inside a `*_settings` directory as a settings file.
  - No such package exists today.
- **m-2. The C6 `forbidden_modules` list is incomplete again** (pyproject.toml:272-280). It lacks `herness.metrics._encode_nested`, which the T04-01 fix (891b6ba) added.
  - OWN050 still guards this, because any herness import outside the allow-list is flagged, so it is not a hole.
  - It does make the "complete for the current tree" property from the round-0 review false. Add the module to the list, or have UT00-58 assert completeness.
  - This belongs to T04-01, or whichever card merges last.
- **m-3. Records for the spec owner:** add the following to the report's records.
  - The `list`/`dict` → `tuple`/`Mapping` type delta to U04-15, U04-17, U04-18, U04-19 and U04-21, or its reversal if option (a) is taken for maps.
  - The OWN050 delta to U00-47 step 6 and UT00-73. The builder already noted this one.

## Assessment
- **Resolved:** Important-1, M-2, M-3, M-4 and M-5.
- **Still open:** M-1 traded a Minor mutability gap for a Critical serialization break (C-1) and an Important copy/pickle break (I-1).
- **Recommendation:** keep the tuples, revert the maps to `dict` (or add the wrap serializer and fix copying), and add JSON round-trip and deepcopy tests.
