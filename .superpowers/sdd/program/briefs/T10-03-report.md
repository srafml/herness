# T10-03 build report: Root config, load, cache, hash

Status: DONE_WITH_CONCERNS
Commit: e4ca326 `feat(core): add root config load, cache and hash (T10-03)` on branch worktree-agent-ae55295df95126ca3 (base 2b2e44d)

## What was built
- `herness/core/config.py` (295 lines, budget 320). Contents: `ProfileName` and `SDK_SOURCE_KINDS` re-exported from config_sources; `GATED_PROFILES`; `SecurityConfig` re-export; `ConfigIssue` re-exported from config_view. Closed stubs `_PipelinesStub`, `_MemoryStub` (holds `injection_patterns`) and `_AppStub` per R2. `SourcesFileConfig(SourcesConfig)` adds `dq` and `build` (R-69). `ModelsFileConfig(ModelsConfig)` adds `deciders` (R-76). `HernessConfig` (BaseSettings, the exact field set of design 10 §3.1, source order init > GuardedEnv > FilteredDotEnv > ProfileYaml > FilesYaml). `load_config` covers U10-09 steps 1-7 and 9. `init_config`, `get_config` and `reset_config` use `_CACHE_LOCK` and `_RESET_HOOKS`. Also `effective_dict`, `config_hash` and the `_KEY_ID_PROVIDER` hook.
- `herness/core/config_view.py` (131 lines, NEW helper, ENG default budget). Holds `ConfigIssue`, the ValidationError to ConfigError conversion, `mask_secrets`, `posix_paths`, `directory_sha256` and `hash_effective`. It imports only errors, ids, pydantic and stdlib.
- `herness/core/errors.py` (380 lines; the impl 00 budget is 380, so 0 lines of headroom are left). `ConfigError` takes `issues: Sequence[object] = ()` and stores it as a tuple capped at 1000, listed in `_extra_attrs` so it survives pickling. `EgressBlocked` exposes `egress_id` and `reason` as read-only properties (see N2).
- `herness/store/layout.py` (46 lines). Carry-over 1: the Protocol and importlib lookup are replaced by `from herness.core.config import HernessConfig, get_config`.
- `pyproject.toml`. The same settings-exception ignore `herness.core.config -> herness.**.settings` is added to "store-no-upward" and to "herness.core must not import herness.store or herness.harness". `herness.core.config` and `herness.core.config_view` are added to "core base is closed", which UT00-58 requires for new core modules. No new contract.
- Tests:
  - `tests/support/config_tree.py` (91 lines, `write_full_config` = the tmp_config fixture)
  - `tests/unit/core/test_config_load.py` (337)
  - `tests/unit/core/test_config_hash.py` (302)
  - `tests/unit/core/test_config_errors.py` (53)
  - `tests/security/test_st10_config.py` (+90, now 130)
  - `tests/unit/store/test_store_layout.py` (79)
  - `tests/bench/test_bt10.py` (36)

## Test-ID mapping
| ID | Functions (file) |
|---|---|
| UT10-01 | test_ut10_01_profile_names_and_reexports, _root_model_needs_load_context, _file_value_loses_to_profile_and_paths_are_absolute, _absolute_paths_kept_and_model_frozen, _config_dir_and_profile_errors, _profile_from_env_and_os_environ (test_config_load) |
| UT10-02 | test_ut10_02_env_beats_profile |
| UT10-03 | test_ut10_03_cli_beats_env |
| UT10-04 | test_ut10_04_maps_merge_lists_replace |
| UT10-06 (nested half) | test_ut10_06_unknown_nested_field_names_path_not_value, test_ut10_06_invalid_values_never_echoed |
| UT10-09 | test_ut10_09_hybrid_with_gate |
| UT10-10 (--set half) | test_ut10_10_set_security_and_profile_are_file_only (4 params), test_ut10_10_env_security_is_file_only |
| UT10-11 | test_ut10_11_gate_requires_recorded_approval (5 params incl. chat), test_ut10_11_chat_approval_with_record_loads |
| UT10-12 | test_ut10_12_local_forbids_egress (3 params, local and synth), test_ut10_12_synth_overlay_enabling_egress |
| UT10-15 | test_ut10_15_hash_ignores_key_order_and_excluded_sections, _hash_excludes_ui_and_deploy_service, _path_values_hash_as_posix_text (test_config_hash) |
| UT10-16 | test_ut10_16_one_weight_changes_hash, _key_id_and_directory_file_are_inputs, _unreadable_directory_file |
| UT10-17 | test_ut10_17_hash_input_holds_references_only, _missing_key_secret_is_unresolved |
| UT10-18 | test_ut10_18_plain_secret_values_masked, _loaded_config_keeps_references |
| UT10-22 | test_ut10_22_cache_and_reset_hooks, _init_config_replaces_and_logs |
| UT10-80 | test_ut10_80_egress_blocked_attributes, _malformed_reason_reads_invalid, _config_error_issues (test_config_errors) |
| UT10-84 | test_ut10_84_sibling_sections_composed, _unknown_top_level_key_names_file_and_key (5 stems), _stub_sections_are_closed |
| PT10-01 | test_pt10_01_hash_is_order_independent (hypothesis, 60 examples) |
| BT10-01 | test_bt10_01_load_config_p95 (measured p95 about 60 ms; the target is < 1 s) |
| BT10-02 | test_bt10_02_config_hash_p95 (measured p95 about 1 ms; the target is < 50 ms) |
| ST10-02 | test_st10_02_set_cannot_change_security (3 params) |
| ST10-16 | test_st10_16_effective_dict_and_hash_hold_no_resolved_secret |
| ST10-37 (e2e, carry-over 3) | test_st10_37_invalid_security_value_via_load_config (3 params), test_st10_37_invalid_paths_value_via_load_config |
| RF | test_rf_shipped_owner_files_load_yaml_shaped (carry-overs 4 and 9), test_rf_owner_config_error_passes_through, test_rf_config_issue_renders_design_format |
| UT02-66 | test_ut02_66_layout_from_config (updated), test_ut02_66_layout_from_loaded_config (new) |

## Evidence
- RED: base errors.py with `pytest tests/unit/core/test_config_errors.py` gave "3 failed (AttributeError)". Without config.py, test_config_load and test_config_hash gave "2 errors during collection" (ModuleNotFoundError). Parts of config.py were written before their tests (not strict TDD order); every unit has tests.
- GREEN: `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` gave 1455 passed, 7 deselected, 1 xfailed (the pre-existing IT00-02 marker). The BT10 benchmarks passed (2 passed). `--require-test-ids --collect-only` passed.
- Gates: ruff format and ruff check were clean. mypy --strict: "no issues found in 68 source files". lint-imports: 11 kept, 0 broken. check_type_ownership exit 0. check_module_size exit 0.
- Coverage (line / branch): config.py 100/100, config_view.py 100/100, errors.py 100/100, layout.py 100/100.

## Carry-overs 1-18
1. DONE. herness/store/layout.py imports get_config and HernessConfig directly (R1). The UT02-66 test was updated and a real-config variant added.
2. DONE. config_view.validation_error uses `errors(include_input=False, include_url=False, include_context=False)` and raises `from None`. Tests: UT10-06 and ST10-37 (the sentinel never appears in str() or repr(issues)).
3. DONE. The test_st10_37_* e2e tests in tests/security/test_st10_config.py cover ReDoS, a 600-character pattern, a plain key, and a NUL in paths.data.
4. DONE. Every owner section loads from YAML lists through load_config (test_rf_shipped_owner_files_load_yaml_shaped, UT10-84 with the design 01 sources example, UT10-04 list replace on a tuple field).
5. DONE. Regexes use `[0-9]` (`^cfg_[0-9a-f]{16}$` in tests; the reason code `[a-z_]{1,40}`); there is no `\d` in config.py or config_view.py.
6. RE-DEFERRED to the deploy cards (U10-79/82/83). Not built.
7. DONE. Nothing in config.py re-validates owner models. effective_dict and config_hash dump only, and the dump over the full design 01 sources example is tested (UT10-84 and the UT10-15 posix test).
8. NOTE. No change; YAML native date parsing kept.
9. DONE-partial. Every existing owner model is a HernessConfig field, and a load test runs over the shipped decisions/eval/metrics/models/weights plus minimal sources, mappings and resilience (the tests/unit/core/fixtures/resilience.yaml fixture). RE-DEFERRED: the `herness config validate` CLI goes to T10-14. The cross-checks, `validate` and owner validators go to T10-12 (a `# T10-12:` marker sits at U10-09 step 8 in load_config). The synth template goes to T10-13.
10. RE-DEFERRED to T10-33 / U01-58.
11. RE-DEFERRED to the owning impl 01 files-connector card (get_config now available).
12. DONE. UT10-02, 03, 09 and 12 are end-to-end through load_config. The UT10-06 nested half, the UT10-10 --set half and ST10-02 are covered. `GATED_PROFILES` is declared. ProfileName and SDK_SOURCE_KINDS are re-exported from config_sources. The T10-02 test_rf_* harness tests were left in place.
13. RE-DEFERRED to w02-s02 integration / T02-11 (render_context.py is not on base).
14. RE-DEFERRED to T11-40 (R3). The T10-03 tests use a local autouse reset fixture.
15. RE-DEFERRED to T07-02 + T10-12. `_MemoryStub.injection_patterns` receives the U10-16 list.
16. NOTE applied. pipelines, memory and app use extra="forbid" frozen stubs (private names, to avoid a type-ownership or name clash when the owners land).
17. NOTE. No action; the root loader wraps ValidationError anyway.
18. NOTE. Field wiring done (`cfg.resilience` = ResilienceConfig).

## Spec notes / deviations
- N1 config split. config.py came to 375 lines, over its 320 budget, so view, hash and issue helpers moved to `herness/core/config_view.py`. It sits between config_sources and config in the module order and imports no settings module. A module-map row is needed in impl 10 §2 (`herness/core/config_view.py`, L0, `ConfigIssue`, `validation_error`, `mask_secrets`, `posix_paths`, `directory_sha256`, `hash_effective`, budget about 140). ConfigIssue's home is config_view; `herness.core.config.ConfigIssue` is a re-export.
- N2 errors.py budget. The impl 00 module map gives errors.py 380 lines, and check_module_size enforces that because impl 10's "+30 lines" is not parsed as a number. The base had 354 lines. To fit, `EgressBlocked` keeps no explicit `__init__`. `egress_id` and `reason` pass through HernessError's `**context` (both are scalars) and are exposed as read-only properties, so they pickle and appear in to_log_fields without `_extra_attrs`. As a result, the keyword types are `Scalar` rather than `str | None` at the call site. A non-str `egress_id` reads as None. A reason not matching `[a-z_]{1,40}` reads as "invalid", so a malformed code never becomes a payload channel. If the controller raises the errors.py budget to about 391, an explicit typed `__init__` is a drop-in. `ConfigError` has the full typed signature and keeps `**context`, because existing callers pass `key=`.
- N3 version for metrics and weights. U10-16 drops `version` except for sources.yaml, but impl 04's `MetricsCatalogConfig` and `WeightsConfig` require `version: Literal[1]`. A before-mode field validator on HernessConfig (`metrics`, `weights`) restores `version: 1`, which the loader has already checked. The spec should say "dropped unless the owner model declares it (sources, metrics, weights)".
- N4 ModelsFileConfig. It sets `_PREFIX = None` (the owner's ModelsConfig has `""`). An error at the root of models.yaml then stays a ValidationError, and load_config names `models.yaml` (UT10-84). Errors inside `models.*` and `harness.*` are still converted by the owner's nested `_PREFIX` into its own ConfigError, which passes through unchanged and without a file name. `deciders` is required, following the spec signature.
- N5 ConfigError message format. The message is `invalid config (<n> issues, first 20 listed): <path> (<file>): <msg>; ...`. The file is inferred from the section: root sections map to herness.yaml, file stems to `<stem>.yaml`, and anything else to `-`. HernessError bounds the message at 1000 characters; `issues` holds every entry. `hint="herness config validate"`.
- N6 POSIX paths in effective_dict. `model_dump(mode="json")` renders Path with `str()`, which gives backslashes on Windows. effective_dict therefore walks the python-mode dump in parallel and writes every Path leaf as `as_posix()`. Without this, `config_hash` differs between Windows and Linux for `security.redaction.directory_file` and `sources.files.inbox`, which breaks the card's cross-OS acceptance check and BT10-02. Consistent with U10-11 ("Path as POSIX text").
- N7 construction without context. `HernessConfig()` is refused in `settings_customise_sources`, because the sources would otherwise fail first with a different message. `HernessConfig.model_validate` is refused in the before validator. Both give "HernessConfig must be built by load_config".
- N8 chat gate message. The message is "profile chat requires recorded approval in herness.yaml security.data_policy", reading "chat in place of the profile" literally.
- N9 overrides. Any override whose first segment is `security` or `profile` is rejected, including `security=...` and `profile.x=...`, not only paths beginning with `security.`.
- N10 get_config. The first load happens under `_CACHE_LOCK` (no double load under a race) instead of calling `init_config()`; the behaviour is otherwise the same.

## Concerns for other cards
- C1 (T10-13 / owners 03, 05, 11). The shipped `config/decisions.yaml`, `eval.yaml` and `models.yaml` have no `version: 1` line, and U10-16 rejects them ("<file>: version must be 1"). The repo `config/` dir therefore does not load as-is; `herness.yaml` and the profiles are also missing (T10-13). The test tree prepends the line to copies. Owner tests that validate those files directly (test_models_yaml, test_enrich_settings UT03-08, test_eval_settings UT11-107) must drop `version` when the line is added.
- C2 (T04-08). The shipped `metrics.yaml` has `metrics: []`, which MetricsCatalogConfig rejects (min_length=1). The test tree inserts one catalog entry.
- C3 (T11-40). The `reset_config` wiring into tests/conftest.py is still owed (R3); the T10-03 tests reset locally.


## Fix round 1 (commit e683a51 `fix(core): address T10-03 review round 1 (T10-03)`)
- m1 (errors.py:292-303). `EgressBlocked` now has an `__init__` that stores the two values as masked attributes, listed in `_extra_attrs` so they pickle and reach log fields. A non-str `egress_id` becomes None. A `reason` that does not match `[a-z_]{1,40}` becomes "invalid". The raw values never enter `context`, `to_log_fields` or the pickle. `egress_id` and `reason` are typed `str | None`. `hint` and `details` go through `**kw: _Kw`, a module alias `str | Mapping[str, str] | None`, with one `type: ignore[arg-type]` on the `super()` call; that is what keeps errors.py at exactly 380/380 lines. Side effects: `message` is no longer positional-only, the constructor takes no free context keywords, and a `details` that is not a Mapping is not caught statically. The attributes are now plain instance attributes, like RateLimited and JobStateError, instead of read-only properties. New test: test_ut10_80_malformed_values_are_masked_where_stored. test_ut10_80_egress_blocked_attributes also covers passing `details`.
- m2 (config.py `_key_id`). The fallback to "unresolved" now happens only when the key secret is missing, identified by the U10-28 message prefix `secret not found: `. Any other ConfigError, such as a backend failure, propagates (U10-11: "when no provider is registered or the secret is missing, K = unresolved"). New test: test_ut10_17_other_key_id_failures_propagate. Note: the match relies on the U10-28 message text; the secrets card should keep that prefix.
- m3. test_ut10_15_pinned_hash_is_os_independent pins `cfg_700ea630db05d0a7` for a fixed input. The input has a `Path("data") / "inbox"` leaf, whose json-mode form is rendered by `pydantic_core.to_json` (backslashes on Windows), plus Decimal, date, tuple and int dict keys. I checked the value against a hand-built POSIX dict run through canonical_json. It fails on any Windows/Linux difference in effective_dict or canonical JSON. A pin over the fully loaded config was not added, because every owner template change would break it.
- Gates:
  - ruff format --check and ruff check: clean.
  - mypy: no issues in 68 files.
  - lint-imports: 11 kept, 0 broken.
  - check_type_ownership and check_module_size: exit 0 (errors.py 380/380, config.py 297/320).
  - `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`: 1458 passed, 7 deselected, 1 xfailed.
  - Coverage: config.py, config_view.py and errors.py 100% line and branch.
- Fix round 2 (commit c189c5c): test_config_errors.py:37 now asserts `.reason == "invalid"` before the `# type: ignore` comment; the egress_id line was already correct.
