# T10-02 report: YAML reading and layer sources

Status: DONE_WITH_CONCERNS
Commit: 0311abc feat(core): add YAML reading and config layer sources (T10-02)
Worktree: D:\herness\.claude\worktrees\agent-a67b7b5e997464a4f (base 2ef442a)

## Implemented (herness/core/config_sources.py)
- `load_yaml_file` (U10-15): `_StrictLoader(yaml.SafeLoader)`. `compose_node` rejects any event that has an anchor, and any alias (`YAML anchors and aliases are not allowed: <name>:<line>`). `construct_mapping` rejects repeated keys (`duplicate key '<k>' at <name>:<line>`). The `st_size` cap is checked before anything is read. The BOM is stripped (utf-8-sig). An empty document gives {}. A top level that is not a mapping, `YAMLError` and non-UTF-8 input each raise their own ConfigError.
- `parse_overrides` (U10-19): split at the first `=`. Segments must match `[A-Za-z0-9_-]{1,64}`, with at most 12. Values are ≤ 4096 characters and parsed with the strict loader. At most 100 overrides. A later override replaces an earlier one; descending into an earlier scalar raises ConfigError. Every error names the key path only; value parse errors are re-raised as `--set <key>: invalid YAML value`.
- `FilesYamlSource` (U10-16), `ProfileYamlSource` (U10-17), `GuardedEnvSource` and `FilteredDotEnvSource` (U10-18): implemented as the spec describes. They read a ContextVar load context, and outside a context they raise ConfigError.
  - The env rules compare the upper-cased name, so `HERNESS_Security__...` is still rejected.
  - An env var whose first segment is `profile` (for example `HERNESS_PROFILE__X`) is also rejected as file-only. This enforces the U10-18 postcondition "no profile key".
  - `.env` parsing: `KEY=VALUE` lines, `#` comments, optional double quotes, 64 KiB cap. A malformed line gives `.env line N: malformed`.
  - More than 500 HERNESS_ variables raises ConfigError.
- Ruling 1: `ProfileName` (a PEP 695 `type` alias), `PROFILES`, `SDK_SOURCE_KINDS`, `ROOT_SECTIONS`, `FILE_STEMS` and `SECTION_NAMES` are declared here.
- Helpers for T10-03 to reuse:
  - `resolve_profile(profile, env)`: U10-09 step 1
  - `check_profile_egress(profile, security)`: U10-09 step 6
  - `check_overlay_security`
  - `deep_merge`
  - `LoadContext`, `load_context(profile, config_dir, env)` (context manager) and `current_load_context()`, for the "must be built by load_config" check
  - `GATED_PROFILES` was left out; T10-03 can declare it without a cycle.
- `BootstrapConfig` (frozen dataclass) and `load_bootstrap` (U10-21):
  - The overlay's `security` is merged over the `security` of herness.yaml and validated with SecurityConfig. A ValidationError becomes a ConfigError listing `<loc>: <msg>` with the input dropped.
  - The overlay also gets the data_policy / synth-egress check (stricter than the spec, which is harmless).
  - Source hosts follow R-06: `hosts` of enabled sources, plus `base_url` hosts found at any depth for non-SDK names, skipping mappings with `enabled: false`. Hosts are lower-cased, sorted and deduplicated.
  - Step 6 is then applied.
- Lists → tuples (T10-01 carry-over): the sources emit plain YAML types and never convert. The `_Section` before-validator in herness.core.settings already does the conversion. This is documented in the module docstring. Converting in the sources would break strict `list` fields in other owners' models.
- pyproject.toml: `herness.core.config_sources` added to the forbidden list of "core base is closed" (UT00-58 requires it).

## Tests
- tests/unit/core/test_config_sources.py: 44 test cases after parametrisation, covering UT10-02/03/05/07/08/09/10/12/13/14/23/24 and PT10-06 (hypothesis round trip).
- tests/security/test_st10_config_sources.py: ST10-01, ST10-03 and ST10-05. Billion laughs, a 6 MiB file and a duplicate `enabled` key are each rejected and each check asserts < 1 s.
- tests/support/config_harness.py: the test-local `Harness(BaseSettings)` wiring the four sources in U10-08 order (ruling 2), plus `write_config` / `load`.
- Test names use the lowercase `test_ut10_07_...` form. The global constraint's example uses it, the tools/check_traceability `_NAME_ID_RE` only matches lowercase, and ruff N802 forbids the uppercase form. The dispatch said `test_UT10_07`; this is a deliberate deviation.

RED: `pytest tests/unit/core/test_config_sources.py tests/security/test_st10_config_sources.py` → `ImportError: cannot import name 'config_sources' from 'herness.core'`, 2 collection errors.
GREEN: same command → 50 passed in 2.45 s. Coverage of config_sources is 99 % line and 100 % branch (5 lines missed, 0 partial branches).

## Gates
- ruff check: pass. ruff format --check: pass. mypy (strict): 0 issues. One `type: ignore[no-untyped-call]` on `peek_event`, which is untyped in types-PyYAML.
- lint-imports: 8 kept, 0 broken. check_type_ownership: exit 0.
- check_module_size: FAIL. MS001: config_sources.py has 398 lines against a budget of 360 (hard limit 400).
- Full `pytest -m "(unit or integration) and not slow"`: 492 passed, 1 failed. The failure is IT00-02 (check scripts pass on repo), for the same module-size reason.
- Tools were run via `.venv\Scripts\python.exe -m ...` per the program ruling (uv run unavailable while a merge is open).

## Concerns
1. Module budget: 398 lines against 360. I already tightened it (merged the env/dotenv sources, a `_fail` helper, dropped banners and section comments). Getting to 360 would mean moving public units out of the module-map file, or cramming code. Request: raise the config_sources.py budget to 400 in the §2 module map. That also turns IT00-02 green.
2. Deferred to T10-03 (they need U10-09 step 3, which is not built at source level per ruling 2):
   - UT10-10 `--set security.egress.enabled=true` and `--set profile=hybrid` parts
   - ST10-02 `--set security.egress.destinations=[evil.com]`
   - `parse_overrides` deliberately does not ban `security.*` / `profile` (U10-19 security notes). The env half of UT10-10, and ST10-01 in env and dev .env, are covered here.
3. The load context holds no `overrides` field (nothing in the sources reads it). T10-03 can add one if U10-09 step 2 needs it.
4. Whether a source's `base_url` is ignored depends on the source's *name* being in SDK_SOURCE_KINDS, as U10-21 and U10-58 literally say. If sources gain a `kind` field distinct from their name, this needs revisiting.
5. Lines of test ids reported by tools/check_traceability TR007 ("id used twice") match the pre-existing repo-wide pattern. The current global constraint allows several functions per ID.

## Fix round 1

Status: DONE
Commit: 214cd19 fix(core): harden config sources after review (T10-02)

### Important
1. Deep nesting:
   - `_StrictLoader.compose_node` now keeps a depth counter. Nesting deeper than 64 nodes raises `<name>:<line>: invalid YAML (nested deeper than 64)`.
   - `_parse` also maps `RecursionError` to `<name>:<line>: invalid YAML` as a backstop.
   - Tests: ST10-05 `test_st10_05_deep_nesting` covers 50,000 `[`, 20,000 `{b:` and 79 block-mapping levels, each a ConfigError in under 1 s. UT10-23 `test_ut10_23_deeply_nested_value_is_config_error` covers `"["*2000+"]"*2000` and depth 65, both giving `--set a: invalid YAML value`, and checks that depth 10 still parses.
2. UT10-06: `test_ut10_06_unknown_keys_name_path_without_value` covers an unknown root key in herness.yaml and an unknown overlay section. It asserts the exact messages and that no value is echoed.
   - Deferred to T10-03: the "unknown nested field" half, which is pydantic `extra="forbid"` validation converted by U10-09 step 4.

### Minor
1. `_read_text` opens the file and reads at most `max_bytes + 1` bytes, then rejects anything longer. There is no `stat`-then-unbounded-read any more.
2. The `--set` key is truncated to 100 characters in all messages. The profile name and the env var name are truncated too. The test asserts that a 5,000-character key gives a message under 200 characters.
3. The wording is now "`<path>` is already set to a non-mapping value". The test covers both a scalar and a list.
4. The overlay version error now reads `profiles/<name>.yaml: version must be 1`, and the test is updated.
5. `enabled` must be a YAML boolean, checked by `_enabled`: `"false"`, `0`, `0.0` and `"no"` raise `sources.yaml: sources.<name>[.<path>].enabled must be true or false`. This applies both at the source level and in nested mappings. The bootstrap allowlist can therefore never be wider than the full config's. Test: `test_ut10_24_non_bool_enabled_rejected`.
6. Renamed to the `test_rf_` form, with docstrings starting "RF". These rows move to T10-03 as carry-over:

   | Test | Row |
   |---|---|
   | `test_rf_profile_overlay_merges_over_files` | UT10-09 |
   | `test_rf_synth_overlay_cannot_enable_egress` | UT10-12 |
   | `test_rf_check_profile_egress` | UT10-12 |
   | `test_rf_env_layer_and_skips` | UT10-02 |
   | `test_rf_dotenv_only_in_dev` | UT10-02 |
   | `test_rf_dotenv_missing_malformed_and_large` | UT10-02 |
   | `test_rf_init_overrides_env` | UT10-03 |

7. `test_ut10_10_harness_validation_is_pydantic` is removed.
8. The message is now asserted in:
   - `test_ut10_08_alias_without_anchor_rejected`: the exact message.
   - `_timed_reject`: each ST10-05 case pins its message (anchors/aliases, too large, `duplicate key 'enabled' at sources.yaml:5`, invalid YAML).
9. `test_ut10_08_merge_keys_rejected` pins both forms of `<<`: with an alias it gives the anchor error, and with an inline mapping it gives `invalid YAML`.

### Budget
- The spec module map (line 87) now gives config_sources.py a budget of 390, and the module is exactly 390 lines.
- Trims used:
  - `_fail` raises `from None`, and the former two-line raise pairs were collapsed into it.
  - Set differences with the walrus operator replace the root-key and overlay-section loops.
  - `_PROFILE_BY_NAME` is dropped; mypy narrows `value in PROFILES` itself.
  - The `what` and `keys` temporaries are inlined.
  - The limit constants are grouped.
  - The env filtering is now two list comprehensions.
  - `suppress(ValueError)` is used in `_base_url_hosts`.
  - A dotenv regex handles the quotes.
  - The module docstring is shorter.

### Gates (via .venv\Scripts\python.exe -m ...)
- ruff check / format --check: clean. mypy: 0 issues. lint-imports: 8 kept. check_type_ownership: 0. check_module_size: 0.
- Card tests: 59 passed. config_sources coverage is 99 % line and 100 % branch.
- Full `-m "(unit or integration) and not slow"`: 501 passed, 1 xfailed. The xfail is IT00-02, which carries an existing xfail marker for the spec traceability defects.

### Carry-over to T10-03
- UT10-02, UT10-03, UT10-09 and UT10-12: the tests above use the rf form.
- UT10-06: the nested-field half.
- The `--set` half of UT10-10.
- ST10-02.
