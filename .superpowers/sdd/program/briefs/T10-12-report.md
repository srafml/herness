# T10-12 report: Cross-checks and full validation

Status: DONE_WITH_CONCERNS
Commit: 03a42d2 feat(core): add cross-checks and full config validation (T10-12)
Worktree/branch: D:\herness\.claude\worktrees\agent-a8ac72aa070bb1537 / worktree-agent-a8ac72aa070bb1537 (base aca2a68)

## Implementation per unit

- U10-20 `herness/core/config_validate.py`: `CROSS_CHECKS` is a tuple of `CrossCheckRow(id, severity, mode, check)`. It has 22 rows in table order: C01-C07, C08a, C08b, C09-C14, C16, C17, C20, C21, C23-C25. The modes are offline, online and registry. `run_cross_checks(cfg, *, offline, include_registry, compose_path=None)` evaluates the rows that apply. Rows read `effective_dict(cfg, redact_secrets=False)` key paths only (via `_tree`) and never use an owner model attribute (R-04). Each issue is `ConfigIssue(severity, path, "<row id> <rule>", file-of-section)`. Messages hold key paths, names and rule text only, never values (TH10-06). When `load_config` calls it, the env for C12 and the default compose path (`<config_dir parent>/docker/compose.yaml`) come from the active load context. Otherwise they come from `os.environ` and `./config`.
- U10-109:
  - `OwnerValidator` is a Protocol.
  - `register_owner_validator` checks the name against the regex. A duplicate name with another object raises `ConfigError("duplicate owner validator <name>")`. Registering the same object again does nothing. Registration holds `_VAL_LOCK`.
  - `run_owner_validators` runs validators in name order and keeps at most 500 results each. A mapping becomes a ConfigIssue with message[:300]. An invalid result becomes `validator <n> returned an invalid issue`. Any exception becomes `validator <n> failed: <ExceptionClass>`, and the exception message is dropped. Issues are sorted as U10-13.
  - `reset_owner_validators` clears the registrations.
- F10-01 step 3a: the spec has no named unit for this step; the behaviour is described only inside U10-109. I added `run_startup_validators(cfg)`. It runs the owner validators offline, logs each issue as `config.validate.issue`, and raises `ConfigError("owner validation failed", issues=...)` if any issue is an error. T09-20 (herness.cli.main) must call it after registering the validators; that wiring stays with T09-20.
- U10-09 step 8 (config.py): `config_validate.enforce_offline_checks(cfg)` is called at the `# T10-12:` marker. Steps 5-8 now run inside the load context, so C12 sees the load env and C08a sees the config dir. An error issue raises `ConfigError("invalid config (<n> issues, first 20): ...", issues=<all issues including warns>, hint="herness config validate")`. Every issue is logged as `config.validate.issue`.
- U10-13 `validate(cfg_dir, profile, *, offline=False)` in config.py:
  1. Load the config. On `ConfigError`, return its ConfigIssue list, or one `error` issue at path `config` when the error carries none.
  2. Run the cross-checks with the registry.
  3. Run the owner validators.
  4. Return the issues sorted.
- `sort_issues` is shared by `validate` and `run_owner_validators`.

## Per C-row notes

- C01: roles and fallback entries. depth_overrides is not checked because the rule does not name it.
- C02: the gate is `security.data_policy.<profile>_approved`, which is false for local and synth.
- C03: covers the kinds connector, monitoring_adapter, llm_client (the first client of each kind gives the path) and decider. A decider section with no `enabled` key (laya, llm) counts as enabled.
- C04: hostname regex, and not an IP, for destinations, extra_allowed_hosts and every source `hosts` list.
- C05: bind must be loopback; expose requires trusted_proxy; the proxy must be loopback.
- C06: `referenced_secret_names` plus `exists`. A store error counts as missing.
- C07: `deploy.reasoning.served_name` must equal `local-30b.model`.
- C08a (per the ruling):
  - A missing compose file gives one `warn`, "not checked: compose file missing".
  - An unreadable file or invalid YAML is an error.
  - So is a missing service, profiles other than [class], or a URL port that differs from the deploy port (vllm-reasoning, openjev, llamacpp-large).
- C08b: gives the warn "not checked on this host" unless `os.name == "nt"` and wsl.exe is on PATH. Otherwise it runs the argv `compose_cmd + -f <compose_file> config --quiet` with a 20 s timeout.
- C09 and C10: local clients, grouped by gpu_class.
- C11: hybrid allows {reasoning_final}, plus reasoning when chat_approved. premium allows the 3 purposes.
- C12: dotenv only with HERNESS_ENV=dev or profile synth.
- C13: `DeployConfig.unpinned_keys()` on the tree's deploy section (warn).
- C14: duplicate names, case-insensitive.
- C16 walks the merged dict:
  - Strings under the U10-12 secret keys or `*_secret` must be `secret:<SECRET_NAME>`. A bare name gets the hint "write secret:<name>".
  - Any other string that is not a valid reference and holds a U10-36 CREDENTIAL span is an error.
- C17: https unless loopback. Applies to enabled sources at any depth; disabled sub-mappings are skipped.
- C20: SDK sources and MSAL dataverse need `hosts`. Snowflake needs `<account>.snowflakecomputing.com`; dataverse needs `login.microsoftonline.com`.
- C21, C23, C24 and C25 follow the table. C23 is a warn when the file is unset, missing or unreadable.

## Files

- New: herness/core/config_validate.py (333 lines, budget 390).
- New: herness/core/config_checks.py (265 lines). It is not in the §2 module map, so the default 400 applies. See Concerns.
- herness/core/config.py: 315 lines (budget 320).
- pyproject.toml: `herness.core.config_checks` and `herness.core.config_validate` added to "core base is closed" (UT00-58).
- New tests:
  - tests/unit/core/test_config_validate.py: UT10-19, UT10-75.
  - tests/unit/core/test_config_owner_validators.py: UT10-81.
  - tests/security/test_st10_cross_checks.py: ST10-04, ST10-27. ST tests go under tests/security by repo convention.
  - tests/unit/core/fixtures/compose.yaml: U10-80 test copy, design 10 §5.6.2 verbatim plus `environment: *offline` on llamacpp-large.
- tests/support/config_tree.py:
  - A PREMIUM overlay with egress, so premium loads under C24.
  - `write_checked_config`: pinned deploy values plus the compose copy.
  - `register_checked_names`: registry stand-ins for C03.
- Adapted tests:
  - tests/unit/core/test_config_load.py and test_config_hash.py: the reset fixture now calls `reset_owner_validators`.
  - tests/unit/core/test_secrets.py and tests/security/test_st10_secrets.py: C12 now refuses dotenv at load when the load env lacks HERNESS_ENV=dev. UT10-31 now loads with env {"HERNESS_ENV": "dev"}. ST10-26 now asserts the C12 load refusal, then keeps its U10-33 runtime-refusal half by loading under dev.
- .secrets.baseline: line-number updates only (made by the hook); no entry dropped; LF line endings.

## RED / GREEN

- RED: `PYTHONUTF8=1 uv run pytest tests/unit/core/test_config_validate.py tests/unit/core/test_config_owner_validators.py tests/security/test_st10_cross_checks.py -q -p no:logging` gave 3 collection errors: `ImportError: cannot import name 'config_validate' from 'herness.core'`.
- GREEN: the same command gave `62 passed in 4.08s`.
- Branch coverage: config.py 100 %, config_validate.py 99 %, config_checks.py 99 %.

## Gates

- ruff format and ruff check --fix: clean.
- mypy: `Success: no issues found in 131 source files`.
- lint-imports: 13 kept, 0 broken.
- check_type_ownership: exit 0. The row class is named `CrossCheckRow` because `CrossCheck` is already an impl 06 type in herness.core.types (OWN040).
- check_module_size: exit 0.
- pre-commit: every hook passes on `--all-files`, and the commit ran them without --no-verify.
- `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`: 3509 passed, 5 skipped, 16 deselected, 1 xfailed.
- Offline validate on the checked template tree (tests/support/config_tree.py) took 100 ms cold and 47 ms warm, under the 1 s target. The unit test asserts `< 3.0 s` to leave margin (test_ut10_19_offline_validate_under_one_second).

## Deviations

1. New helper module herness/core/config_checks.py. With all 22 rows in one file, config_validate.py formatted to 571 lines, over the 400 hard limit. Spec §1 allows helper modules forced out by the size limit to sit next to the design modules.
   - Moved to config_checks: the pure tree rows (C01, C02, C04, C05, C07, C09-C12, C14, C17, C20, C21, C24, C25) and the tree helpers.
   - Kept in config_validate: the side-effect rows (C03, C06, C08a, C08b, C13, C16, C23), the table, the runner and the owner hook.
   - Controller: add a §2 module-map row (suggested budget 280) or rule otherwise.
2. Public names added beyond the §2 row: `CrossCheckRow`, `enforce_offline_checks` (the step 8 entry point config.py calls), `run_startup_validators` (F10-01 step 3a) and `sort_issues`.
3. config.py imports config_validate lazily (`# noqa: PLC0415`) in load_config and validate. config_validate imports config and secrets and is last in the spec's core import order, so a module-level import would create a cycle.
4. Issue messages start with the row ID, for example `C05 must be a loopback address (R-50)`, so tests and operators see which rule fired.
5. C16 skips strings that are valid `secret:` references before the CREDENTIAL sweep, because detector (c) matches `secret:NAME` itself. Plain values that happen to match SECRET_NAME, such as `hunter2hunter2` and the `api_key_secret` test value, also get the "write secret:<name>" hint, as the rule text says.

## Carry-overs

- The repository docker/compose.yaml half of UT10-75 moves to T10-23. The test uses tests/unit/core/fixtures/compose.yaml.
- Memory (T07-02) is not done:
  - config.py is at 315/320 after this card.
  - The `parse_injection_patterns` half needs the injection-pattern reading in config_sources.py, which is at 390/390.
  - config_sources may not import an owner settings module under the layers contract; only config.py has that settings exception.
  - `_MemoryStub` stays. The owning card should move pattern parsing into config.py or raise the budgets.
- T09-20 must register the owner validators before `init_config` (for example `enrich.deciders`, per the comment in herness/enrich/settings.py). It must then call `run_startup_validators(cfg)`, except for config validate, doctor, secrets and deploy.
- Test reset: T11-40 should add `reset_owner_validators` to the shared `reset_core` fixture in tests/conftest.py.

## Spec notes

- F10-01 step 3a has no named unit; the raising behaviour is described only inside U10-109.
- C03 says "decider with enabled: true", but laya and llm have no `enabled` field; they are treated as enabled.
- C08b calls subprocess directly, because core has no U10-76 `run_cmd` (admin is L5). The tests monkeypatch `_wsl_available` and `subprocess.run` in the module.
- C06 resolves secrets through the backend of the cached config (`secrets._backend`), not the backend of the config being validated.

## Concerns

- config_checks.py is a module that is not in the spec §2 map (see deviation 1). The controller needs to rule on it.
- This card adapted other cards' tests: ST10-26 and UT10-31 for the new load-time C12 rule, and the shared test tree now has a premium overlay for C24. The intent of those tests is preserved.


## Fix round 1

Commit: 94d3f5c fix(core): harden cross-checks per T10-12 review (T10-12). It sits on top of c138c0d. secrets.py was not touched.

1. Spec §2 (docs/impl/10-config-security-deployment.impl.md):
   - Added the row `herness/core/config_checks.py`: pure offline rows of U10-20, size-forced helper, its public names, L0, none, budget 280.
   - Added `CrossCheckRow`, `enforce_offline_checks`, `sort_issues` and `run_startup_validators` to the config_validate.py row.
   - `tools.check_module_size` now resolves config_checks.py to (280, 10-config-security-deployment.impl.md), and exits 0. config_checks.py is 268 lines; config_validate.py is 353/390.
2. m1 C08a: when the compose file is missing, the single `warn` covers only the compose-service and profiles half. The resilience URL port vs deploy port half still runs. Test: `test_ut10_75_missing_compose_still_checks_ports`.
3. m2 C08a: each of these is now one `error` issue and never raises:
   - a compose root that is not a mapping, which gives "compose file has no services mapping";
   - `services` that is not a mapping;
   - a service entry that is not a mapping, which gives "compose service is not a mapping".
   Tests: the `test_ut10_75_compose_service_rules` cases `services-not-a-mapping` and `service-not-a-mapping`, and `test_ut10_75_list_root_is_an_issue`.
4. m3 C03: any exception from `registry.get` becomes `no <kind> implementation resolves under this name: <ExceptionClass>`, with no exception message. Test: `test_ut10_19_c03_import_failure_is_an_issue`.
5. m4 C17: `_base_urls` now also walks lists of mappings at any depth. Test: the new offline C17 case with `mirrors[1].base_url`.
6. C06: done without touching secrets.py. `_backend_get` builds the backend the validated config names:
   - `keyring`: `secrets._KeyringBackend()`.
   - `dotenv`: `secrets._DotenvBackend(cfg.profile, env=<check env>, root=<config dir parent>)`.
   A dotenv refusal counts every reference as missing; C12 reports the cause. The value is discarded at once, as in `exists`. The catch is that it uses two private secrets classes from a sibling module in the same spec. Test: `test_ut10_19_c06_uses_backend_of_validated_config`.

Gates:
- ruff format and check: clean.
- mypy: no issues in 131 files.
- lint-imports: 13 kept.
- check_type_ownership: 0. check_module_size: 0.
- pre-commit on all files: passes.
- Full `(unit or integration) and not slow` suite: 3518 passed, 5 skipped, 1 xfailed. The one first-run failure was IT00-01 pre-commit, caused by the unstaged baseline line-number update. It passed after staging.
- Coverage: config_validate 98 %, config_checks 99 % (branch).
- .secrets.baseline: line-number updates only, LF.

Left: nothing from this round. Earlier carry-overs are unchanged (T07-02 memory, T10-23 compose file, T09-20 wiring, T11-40 reset_core).
