# T02-01 review: store and model foundations (HEAD 6f2ca35, base 079088a)

**Verdict: Approved.** There are no Critical or Important findings. The Minor findings below need an orchestrator decision or a follow-up note, but none of them blocks this card.

Focused checks I ran (read-only):
- The `-k` selection, for concern (4). `pytest --collect-only -k "UT02-53 or UT02-54 or UT02-66"` collected 0 tests (167 deselected). The underscore form collected 52 of 167.
- The record-ID rules in `herness/core/ids.py:29-36,164-173`, for concern (5).
- The C4 and C6 definitions in impl 00 (`docs/impl/00-foundation.impl.md:1228,1230`) and the ST00-10 row (`:2036`), for concern (2).
- Pickling of the error classes through `herness/core/errors.py:110-130`. `_rebuild` skips `__init__`, so the positional constructors pickle correctly.

## Spec compliance

### Units
| Unit | Result | Notes |
|---|---|---|
| U02-01 `NotFoundError` | ✅ | It subclasses `NotFound`, has keyword-only `kind` and `key`, sets `details={"kind","key"}`, and leaves `hint` as None (`herness/store/errors.py:19-26`). |
| U02-02 `ReviewItemConflict` | ✅ | `RecoverableError`, and the message matches the spec exactly (`:29-36`). |
| U02-03 `LakeContractError` | ✅ | `SchemaViolation`, and the message matches the spec (`:39-48`). The spec lists `bad_rows ≥ 0` as a precondition and gives no Errors, so the class does not enforce it. That is acceptable. |
| U02-04 `LakeStateError` | ✅ | `FatalError`, with `state` typed as a `Literal` (`:51-58`). |
| U02-05 `MigrationError` | ✅ | `SchemaViolation`, all six reasons, and a `03d` message (`:61-67`). |
| U02-06 `DataLayout` | ✅ | Frozen and slotted. It resolves the root once with `strict=False` and detects the consecutive `data`, `synth` pair case-insensitively on the resolved parts (`herness/store/layout.py:31-53`). |
| U02-07 `data_layout` | ✅ with a deviation | The logic `root` if given, else `cfg.paths.data`, else `get_config()` is correct (`layout.py:63-68`). The `HernessConfig` type is a structural Protocol, and the fallback looks up `herness.core.config` at call time. See Minor M1. |
| U02-71 `ServiceOverride` | ✅ | The patterns, bounds, default role and the three invariants all match (`herness/model/settings.py:80-103`). The spec's "record-ID pattern" for `org_id` is ambiguous. See M5. |
| U02-72 `CustomFieldsConfig` | ✅ | Nested ServiceNow and Jira models, the column pattern `^[a-z_][a-z0-9_]{0,127}$`, and forbid, strict, frozen (`:106-133`). |
| U02-73 `MappingsConfig`, `ENUM_DOMAINS` | ✅ | All seven domains with the exact canonical sets, held in a read-only `MappingProxyType`. The checks cover unknown domain, canonical value, source values of 1-200 characters, case-variant conflicts, alias conflicts compared lower-cased, and at most 10,000 overrides. The messages name the domain and position, not the value (`:57-74,136-176`). |
| U02-74 `DqSettings` | ✅ | All ten defaults match. It checks fractions in [0, 1] with no NaN, counts as `int ≥ 0`, and `warn ≤ error` (`:179-200`). |
| U02-75 `BuildSettings` | ✅ | `keep_last` 1-20, the `memory_limit` pattern exactly as written, `threads` 1-256 or None, and `service_ci_classes` with 1-50 items, each matching `^[a-z][a-z0-9_]{0,79}$` (`:51,203-217`). |
| U02-76 `BuildSqlError` | ✅ | The message format matches. Literals are masked before the 300-character cap. The regex is a superset of the spec's because it also masks an unclosed trailing quote, which is the safer choice (`herness/model/errors.py:8-29`). See M6 for gaps in the spec itself. |
| U02-77 `DqGateFailed` | ✅ | `SchemaViolation`, the exact message, the first 20 checks joined with ", ", and the full tuple kept (`errors.py:32-40`). |

### Tests
| Test | Result | Notes |
|---|---|---|
| UT02-53 | ✅ | Covers bad domain, bad canonical value, conflicting alias (case-insensitive), bad custom field name (including an SQL-injection string), and `delivery` without a project. Also covers the field bounds, the override count bound and the valid YAML sample (`tests/unit/model/test_model_settings.py`). |
| UT02-54 | ✅ | `warn > error` is rejected and `warn == error` is accepted. `75%` and `48GB` are accepted and `abc` is rejected, along with more edge cases (`test_model_settings.py`, UT02-54 tests). |
| UT02-66 | ✅ | Both spec roots, the child paths, `synth_marker` true and false, a relative root, the `cfg` path and the lazy `get_config` path (`tests/unit/store/test_store_layout.py`). |
| Extra "(error-class part)" tests for UT02-45, 02, 10, 34, ST02-14 and IT02-29 | ✅ | These are extra, but in scope: the error units have no test on this card. |

### Acceptance checks and constraints
| Check | Result | Notes |
|---|---|---|
| `pytest -k "UT02-53 or UT02-54 or UT02-66"` | ❌ as the card writes it (a spec defect, not a builder defect) | It selects 0 tests, which I confirmed. The test names follow global-constraints (`test_ut02_53_…`), and pytest `-k` matches names, not docstrings. The underscore form selects 52 tests. See M4. |
| `mypy --strict herness/store herness/model` | ⚠️ | Taken from the report. I did not re-run it. |
| `lint-imports` with `model-settings-light` | ⚠️ | Taken from the report (8 kept). The contract is present in the diff (`pyproject.toml`, the model-settings-light block). |
| herness.store and herness.model in import-linter in the same commit | ✅ | C1 layers are model, store, core. C4 forbids store and model, as impl 00 C4 requires. C6 and model-settings-light are added, and store-no-upward forbids herness.model. |
| R-03 settings imports | ✅ | `settings.py` imports only the stdlib and pydantic. |
| Module line budgets | ❌ `herness/store/errors.py` has 67 lines against a budget of 60 | See M2. The other files are within budget: layout 68/70, settings 217/240, model/errors 40/40, the `__init__` files 1/5. |
| Coverage | ⚠️ | Reported as 100% line and branch. I did not re-run it. |

### ⚠️ Cannot verify from the diff
- The gate results (mypy, ruff, lint-imports, coverage, and the 163 unit and integration tests passing) are taken from the report and were not re-run.
- T10-03 has to follow up on U02-07. It must type `cfg` as `HernessConfig`, replace the `importlib` lookup with a direct import, and make sure `ConfigError` propagates. Until then, `data_layout()` with no arguments raises `ModuleNotFoundError`. No current caller hits this.
- Where the loader in spec 10 converts pydantic `ValidationError` into `ConfigError`, it must drop the `input_value=` part. pydantic's `str(ValidationError)` includes the offending value, and the tests only check the text before `[type=` (`tests/unit/model/test_model_settings.py`, bad-canonical and alias tests).

## Strengths
- The validation messages never echo the offending values, and the tests check this.
- `BuildSqlError` masks literals before truncating, so a cut literal cannot leak. It also closes the unclosed-quote gap in the spec's regex.
- The error classes reuse the core `_extra_attrs` and `__reduce__` pattern, so pickling round-trips through multiprocessing.
- The ST00-10 edit fails safe. If C1 ever stops being the first layers contract, the assertion that "herness layers" is named fails. It does not pass silently.
- The contracts follow the impl 00 C4 and C6 definitions exactly: C4 forbids every existing top-level package other than core, and C6 forbids every herness module except `core.errors`, `core.types` and the module itself.

## Issues

### Critical (Must Fix)
None.

### Important (Should Fix)
None.

### Minor (Nice to Have)
- **M1: the dynamic `get_config` lookup (concern 1)** (`herness/store/layout.py:11,56-60`). This is acceptable as an interim. It imports downward (L1 to L0), so no layer is violated, and it avoids a stub or an invented symbol, which global-constraints forbid. The spec's signature (`cfg` optional, defaulting to `get_config()`) is kept. Making the argument required until T10-03 would change a spec'd public signature and push churn onto every later caller.
  - Costs: the lookup is invisible to import-linter and to mypy (`get_config()` is `Any`), and before T10-03 the no-argument path fails with `ModuleNotFoundError` instead of a `HernessError`.
  - Required disposition: add an explicit replacement item to the T10-03 brief so the Protocol and the `importlib` call are removed.
- **M2: `store/errors.py` is over budget, 67 lines against 60 (concern 3)** (`herness/store/errors.py:1-67`). The report says it "cannot be made shorter", which is not quite true. The five class docstrings (lines 20, 30, 40, 52, 62) are optional because ruff `D` is not selected, and removing them brings the file to 62. Removing them would cost readability, though. Two options:
  - Recommended: amend the impl 02 §2.1 budget to 70.
  - Or trim the docstrings.
- **M3: an impl-00-owned test was edited (concern 2)** (`tests/integration/repo/test_import_contracts_enforced.py:27-41`). The edit is necessary. Adding C4 moves the test into its "C4 exists" branch, and the old exact-text replacement of C1 no longer matches, so ST00-10 would fail. The edit is correct: it still plants `herness.harness` above core in C1 and in the C4 forbidden list, and it still asserts that both contracts are named. It is generic enough for later cards.
  - Remaining fragility: it depends on the textual order of the contracts (C1 must be the first `layers = [`).
  - Action: record this change against impl 00 (ST00-10 row, `docs/impl/00-foundation.impl.md:2036`) so the owning spec stays in line with the test.
- **M4: the hyphenated `-k` acceptance command (concern 4)** (`docs/impl/02-data-model.impl.md:3707`). This is a spec defect. The builder is right, and I confirmed it: 0 tests are collected. Global-constraints requires `_` in test names so that `-k UT00_55` selects them. That matches the impl 00 convention and the tests here. Fix the card text to `-k "UT02_53 or UT02_54 or UT02_66"`, and sweep the other impl 02 cards for the same pattern.
- **M5: the restated record-ID pattern and its comment (concern 5)** (`herness/model/settings.py:38-39`). Restating the pattern locally is right, because R-03 bars importing `herness.core.ids`. But the comment calls it "herness.core.ids, restated", and it is not the core.ids rule. core.ids allows a source of up to 32 characters, an entity starting with a letter and up to 64 characters, and a key of up to 512 characters that may contain internal spaces (`herness/core/ids.py:29,35-36,164-166`). This pattern allows an unbounded source, an entity that may start with a digit, and a key of 1-200 non-whitespace characters. For `service_id` and `team_id` the spec fixes this exact pattern, so the code is compliant. For `org_id`, "record-ID pattern" is ambiguous. The stricter reading is reasonable, since ServiceNow department sys_ids fit it.
  - Action: change the comment to "U02-71 pattern (stricter than core.ids record IDs)", and have the spec pin `org_id` to the same pattern.
- **M6: gaps in the spec behind BuildSqlError (TH02-14)** (`herness/model/errors.py:8-17`). Two gaps:
  - Only single-quoted literals are masked. DuckDB also puts values in double quotes, for example `Duplicate key "id: 5"`, and those pass through unmasked.
  - When `": "` first appears late in the line, the part before it is not truncated, so the 300-character cap does not bound the message.

  Both behaviours come from the spec, so this is not a builder defect. Raise them for ST02-14 and T02-12 or the build card.
- **M7: C6 and model-settings-light are identical** (`pyproject.toml`, the two forbidden contracts `settings modules are leaves` and `model-settings-light`). Both are mandated, by impl 00 C6 and impl 02 §2.2, but future settings cards will have to keep both lists in step. Add a note in one of them that points to the other, or have the spec merge them.
- **M8: strict mode and YAML scalars** (`herness/model/settings.py:78`, `enums: dict[str, dict[str, str]]`). This is plan-mandated (`strict=True`). ServiceNow `incident_state` source codes are numeric, so a user who writes `1: open` without quotes gets a `ValidationError`, because YAML reads the key as an int. The same happens to a Jira project key such as `NO`, which YAML 1.1 reads as False. The example `mappings.yaml` or the docs should quote these keys, or spec 10 should note this.
- **M9: the bad-canonical test only checks that the source value is absent** (`tests/unit/model/test_model_settings.py`, `test_ut02_53_bad_canonical_rejected`). It should also assert that `catastrophic` (the offending canonical value) is not in the message.

## Assessment
**Task quality:** Approved
**Reasoning:** All 14 units and the three required tests match the spec, and the import contracts match the impl 00 and impl 02 definitions. The deviations the builder raised are acceptable as interim solutions or are defects in the spec itself. What is left is Minor: a budget amendment, a T10-03 follow-up, and spec text fixes.
