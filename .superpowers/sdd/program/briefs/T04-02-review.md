# T04-02 review: Settings and config files (diff f2b1819..9ff27a2)

**Verdict: Needs fixes.** The only blocking item is the file-size overrun, which the controller has already ruled on. Everything else matches the spec.

### Spec Compliance
Result: ❌ one issue found (the file-size budget). All units and test rows otherwise comply.

**Units**
- ✅ **U04-14 (literals):** all nine aliases match the spec in order. `Unit` matches the design 00 §12.1 vocabulary (settings.py:21-31, pinned by test lines 686-710).
- ✅ **U04-15 (`MetricDef`):**
  - Every field and constraint matches the spec.
  - The model is frozen, has `extra="forbid"` and is strict (settings.py:95-117).
  - It matches the design §4.1 example entry.
- ✅ **U04-16 (`MetricsDefaults`):** the defaults and bounds match, including `compute_timeout_s` 1-300 (settings.py:120-139).
- ✅ **U04-17 (`ScoringConfig` and its nested models):**
  - The bounds match the spec: tier weights in (0,1], `org.metrics` 1-40 entries each > 0, all seven template keys, templates 1-500 characters (settings.py:141-177).
  - `org` and `levers` are required. This is correct, because `metrics` and `templates` have no defaults.
- ✅ **U04-18 (`MetricsCatalogConfig`):** 1-200 metrics, and the error message "duplicate metric <name>" (settings.py:180-193).
- ✅ **U04-19 (`WeightsConfig`):**
  - All block fields and ranges match, and so do `PortfolioConfig`, `ScenarioConfig` and `SolverConfig`.
  - `mandatory ∩ excluded = ∅` and clip ordering are enforced.
  - Decimal fields are relaxed only on the `Money` fields: they accept ints, floats with at most 2 decimals and decimal text. NaN, inf and bool are rejected (settings.py:40-52, 196-340).
  - `blocks()` returns the 12 blocks, not 14 (see ⚠️ below).
- ✅ **U04-20 (`WEIGHT_USES`, `unconfirmed_blocks`):**
  - The table matches the U04-20 algorithm entry by entry. Unions keep first-seen order, and the mapping is read-only (`MappingProxyType`) (settings.py:343-376).
  - `unconfirmed_blocks` raises `ConfigError("unknown weight block <b>")` and returns a sorted, unique list.
- ✅ **U04-21 (`WeightChangePayload`, `WeightChange`):**
  - The patterns and bounds match. There is no free-text field, and `extra="forbid"` is set.
  - "`memory_id` required iff `origin == memory`" is implemented as a two-way check. That is exactly what the spec says, so it is not an open choice (settings.py:379-404).
- ✅ **U04-82 (`WeightIssue`):** a frozen, slotted dataclass. `__post_init__` checks the severity and the path and message lengths, and raises `ConfigError("bad weight issue")` (settings.py:407-421).
- ✅ **U04-22 (`check_weight_confirmations`):**
  - All parameters are positional-only.
  - It checks the hash precondition, iterates blocks in `blocks()` order, and emits the exact path and message format.
  - A block already false in `previous` passes. On a first load (previous is None), every confirmed block is gated. Only approvals whose hash equals the new hash count (settings.py:424-446).
- ✅ **`config/weights.yaml`:** the values are identical to design §7.2. `toil` is reflowed onto several lines, but the values are unchanged. 31 lines against a budget of 45.
- ✅ **`config/metrics.yaml`:**
  - `defaults` equals design §4.1 plus `compute_timeout_s: 30.0`.
  - `scoring` equals §7.1, and every template is the §5.9 example sentence.
- ✅ **C6 contract (pyproject.toml, lines 267-282):** it follows U00-52 for the current tree. It forbids every existing herness module except `core.types` and `core.errors`, with indirect imports allowed. Focused check: I listed the herness `*.py` files at 9ff27a2, and the forbidden list is complete. OWN050 in `_check_settings` also restricts herness-internal imports to the allow-list, so the guard holds even for modules added later.
- ❌ **File-size budget:** settings.py is 454 lines, against the module-map budget of 390 and the ENG hard limit of 400. The controller has already ruled on this; it is tracked under Important below.

**Test rows**
- ✅ **UT04-15:** `unit: percent` plus `better: up` gives a `ValidationError` with both locations. There is also a parametrized check for each field constraint.
- ✅ **UT04-19:** invalid values fail with their paths. Covered cases include a missing priority key, a reversed clip, a clip of 0, bad decimals, a bad time zone, the overlap between `mandatory` and `excluded`, and a scenario named `unconstrained`. The `defaults` and `scoring` sections are covered as well.
- ✅ **UT04-20:** the gate gives an issue without an approved payload and none with one. The payload schema and its bad cases are covered.
- ✅ **UT04-119:**
  - The gate returns `WeightIssue` objects with the expected path.
  - An empty message raises `ConfigError`.
  - An AST import scan checks the imports of settings.py.
- ✅ **ST04-05:** a flip without approval is refused. So is a flip approved only for another hash, for no hash, or for a different block. The test also asserts that no weight value appears in the messages.

**⚠️ Cannot verify from the diff (or deliberately deferred)**
- **"14 blocks" versus 12.** The U04-19 `blocks()` text and the §9 table row (impl spec line 2000) say 14. However, the U04-19 field list and design §7.2 (docs/specs/04-metrics-and-scoring.md:538-550) define exactly 12 blocks. Implementing 12 is correct, and "14" is a spec erratum. `business_timezone` and `portfolio` are not blocks: they have no `unconfirmed` flag. The spec owner should correct lines 455 and 2000.
- **`config/metrics.yaml` does not validate as a whole yet.** The `metrics: []` placeholder fails `min_length=1` until T04-08 lands. I grepped herness, tests and tools, and nothing loads the whole file today. Any spec 10 loader or config smoke-test card must be ordered after T04-08. The controller should track this dependency.
- **Open choices:**
  - `unconfirmed` defaults to true when the key is missing. This is fail-safe: a missing flag reads as unconfirmed and never skips the gate. Accepted.
  - `proposed_config_hash` defaults to None. This is safe, because None never matches a hash, and ST04-05 covers it. Accepted.
  - The two-way `memory_id` rule is literally the spec's "iff". Accepted.
- **Test evidence** (72 passed, 100% line and branch coverage, all gates clean) comes from the report. Per the reviewer rules, I did not re-run it.

### Strengths
- The gate is small, pure and exact.
  - The hash precondition runs before any work.
  - Messages contain only block names and the validated hash, so they cannot leak weight values (TH04-05 and TH04-11). ST04-05 also asserts this.
- `WEIGHT_BLOCKS` is derived from the model's fields, not hand-listed, so `blocks()`, the payload validation and `WEIGHT_USES` cannot drift apart. The test also checks that every `WEIGHT_USES` entry is a subset of the block list.
- The Decimal coercion is careful. It uses `repr`-based exponent checks, rejects NaN and inf through the non-int exponent, keeps bool out through `type(value) is int`, and passes any other input through so strict mode rejects it.
- The tests are behavioural and path-precise. Negative cases check the error location, not just that an error was raised. The import scan is AST-based and makes any herness import other than `core.types` and `core.errors` fail the test.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **settings.py is 454 lines, over the 390-line module budget and the 400-line ENG hard limit** (herness/metrics/settings.py:1-454). The controller ruled that the fix is to move the weights models into a private `herness/metrics/_weights_settings.py` that settings.py re-exports, with C6 and OWN050 extended to cover it. This is tracked here and not re-litigated.

   Notes on the split boundary and what it has to touch:
   - **Shared primitives must move too.** `_Model`, `_rule`, `_to_decimal`, `Money`, `Fraction`, `Positive`, `_PriorityMap` and `_is_zone` are used by both the catalog and weights models. They must live in the private module, or settings.py and the private module import each other in a cycle.
   - **Suggested boundary.** Move the shared primitives, `WeightBlock` and its 12 subclasses, `ScenarioConfig`, `SolverConfig`, `PortfolioConfig` and `WeightsConfig`, which is about 170 lines. Keep `WEIGHT_BLOCKS`, `WEIGHT_USES`, `unconfirmed_blocks`, `WeightChange`, `WeightChangePayload`, `WeightIssue` and `check_weight_confirmations` in settings.py. That keeps the TH04-05 security surface in the public module that UT04-119 names, and lands settings.py at roughly 280 lines. Once the file is under budget, the `# fmt: off` blocks (settings.py:20/32 and 356/366) should go.
   - **OWN050 coverage.** OWN050 only scans files literally named `settings.py` (tools/check_type_ownership.py:311). It also flags any herness import outside `_SETTINGS_HERNESS`, so settings.py importing `herness.metrics._weights_settings` becomes an OWN050 finding. The tool needs two changes. It must scan `*_settings.py` as well. It must also allow a settings module to import its sibling private `_*_settings` module.
   - **UT04-119 test.** The import scan (test_metrics_settings.py:431-449) must allow that one sibling module and scan it too.
   - **C6 contract.** Add `herness.metrics._weights_settings` to `source_modules`. Keep it out of `forbidden_modules`, since U00-52 exempts "the settings module itself".
   - **Import-contract test.** test_import_contracts.py:60 finds settings modules with `rglob("settings.py")`, so it will not see the new file. Extend its glob in the same way.

#### Minor (Nice to Have)
1. **Nested containers are mutable** (for example `weights.toil.effort_factor`). Frozen models stop attribute rebinding, but values like `weights.toil.effort_factor[1] = 99` or `portfolio.mandatory.append(...)` still mutate a "frozen" config after its `config_hash` is computed (settings.py:78-81, 247, 297-298). Consider `Mapping`/`tuple` types or a `MappingProxyType` after-validator for the weight maps. The spec says "Immutable", and these values are gated by TH04-05.
2. **Misleading test IDs.** `test_ut04_20_weight_uses_table` and `test_ut04_20_unconfirmed_blocks` (test_metrics_settings.py:338, 363) test U04-20. The spec assigns U04-20 to UT04-68 and UT04-99, while UT04-20 belongs to U04-21 and U04-22. So `pytest -k UT04_20` selects tests outside that row. Rename them to a neutral supplementary label, or mention U04-20 in the docstring.
3. **Import-scan allow-list is broader than R-03.** `_ALLOWED_THIRD_PARTY` in the UT04-119 scan (test_metrics_settings.py:428) admits `typing_extensions` and `annotated_types`, but R-03 says "stdlib and pydantic". settings.py imports neither, so tighten the list to `{"pydantic"}`, or `pydantic` plus `pydantic_core` at most.
4. **Extra public names.** `WEIGHT_BLOCKS`, `WeightBlock`, the block classes and the `Money`/`Fraction`/`Positive` aliases are public but absent from the module-map export list (spec §2). They are harmless. Consider underscoring the aliases, or noting `WEIGHT_BLOCKS` as an addition in the report.
5. **Decimal-string precision is unbounded.** `_DECIMAL_TEXT` (settings.py:35) accepts a decimal string of any length or precision, so `"12.345"` is accepted while the float `12.345` is rejected. The spec allows this, but the asymmetry could surprise a user. A short comment would help.

### Assessment
**Task quality:** Needs fixes

**Reasoning:** All units, config values and test rows match the spec, apart from the erratum where the spec says 14 blocks but defines 12. The gate and payload are correct and well tested. The only blocking item is the settings.py size overrun, already ruled on. Fix it with the split described above, making sure OWN050, UT04-119, C6 and test_import_contracts are all extended to the new private module.
