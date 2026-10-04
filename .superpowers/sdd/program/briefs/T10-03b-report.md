# T10-03b report: root config sections split, version key, owner stub replacement, missing config files

Worktree: D:\herness\.claude\worktrees\agent-abb2a7231fb8f968c (branch worktree-agent-abb2a7231fb8f968c, base 9b794b5)
Status: DONE_WITH_CONCERNS (see Concerns)

## What changed
- `herness/core/_config_sections.py` (new, 70 / 200 lines): `SourcesFileConfig`, `ModelsFileConfig` (moved verbatim from config.py; config.py re-exports both, `c.SourcesFileConfig is sections.SourcesFileConfig`), `memory_with_patterns` (U10-16 step 4 through T07-02 `parse_injection_patterns`), `PATTERNS_FILE`.
- `herness/core/config.py` (312 -> 285 / 320 lines): `_Stub`, `_MemoryStub`, `_AppStub` removed; `memory: MemoryConfig` (herness.harness.memory.settings), `app: AppConfig` (herness.reports.settings); new `field_validator("memory", mode="before")` calling `memory_with_patterns`. `pipelines: PipelinesConfig` was already real (verified: equals `PipelinesConfig.model_validate(pipelines.yaml)` through load_config).
- `config/sources.yaml`, `config/mappings.yaml`, `config/resilience.yaml` (new; `version: 1` + spec defaults, see below).
- `pyproject.toml`: `herness.core._config_sections` added exactly where `herness.core.config` appears: "core base is closed" forbidden_modules; settings-exception ignore line `herness.core._config_sections -> herness.**.settings` in "herness layers", "store-no-upward" and "herness.core must not import herness.store or herness.harness". (config.py is not a source of "settings modules are leaves", so neither is the sibling.)
- `docs/impl/10-config-security-deployment.impl.md` §2: new `_config_sections.py` row (budget 200); settings-exception bullet and the core import-order bullet name the sibling (`config_sources` -> `_config_sections` -> `config`).
- `tests/unit/repo/test_import_contracts.py` (UT00-58): the "one settings exception" assertion now expects the second ignore line when `herness/core/_config_sections.py` exists (see Deviations).
- `tests/support/config_tree.py`: `write_repo_config` now copies sources, mappings, resilience, app, pipelines, weights, herness.yaml, profiles and injection_patterns.txt verbatim; stand-ins for sources/mappings/resilience/app/memory removed. Kept: `version: 1` prepended to the test copies of decisions/eval/models/memory (owner files cannot carry it, see gaps) and the metrics.yaml one-entry stand-in (T04-08). `write_full_config` unchanged.
- `tests/unit/core/test_config_templates.py`: stand-in assertion test replaced by `test_ut10_76_owner_files_ship_with_version_1`; new parametrised `test_ut10_76_every_profile_loads_with_c13_placeholder_warnings_only` (4 profiles).
- `tests/unit/core/test_config_sections.py` (new): per replaced stub (app, pipelines, memory incl. parse_injection_patterns: physical line numbers with BOM/comments/indented comment, CRLF, trailing space, higher-layer override, inert outside a load).
- `tests/security/test_st10_config.py`: ST10-37 end to end on the repo tree: wrong/missing `version` in each of the 11 owner files (44 cases) -> `ConfigError("<stem>.yaml: version must be 1")`; ReDoS custom pattern in the shipped herness.yaml -> ConfigError with issue path; 600-char injection pattern -> ConfigError naming the line, no echo.
- `tests/unit/core/test_config_load.py`: docstring of `test_ut10_84_stub_sections_are_closed` updated (owner models, not stubs).

## parse_injection_patterns wiring and line numbers
config_sources.py is frozen (390/390) and its step 4 stores `line.strip()` of non-empty non-`#` lines, which drops trailing spaces (U07-19 keeps them, e.g. shipped `(act|behave) as (an?|the) `) and loses line numbers. `HernessConfig._injection_patterns` (before-validator on `memory`) calls `memory_with_patterns`, which re-reads `<config_dir>/injection_patterns.txt` from the active load context (the source already checked it exists, <= 1 MiB, UTF-8; decoded `utf-8-sig` like the source) and passes the whole text to `parse_injection_patterns`. Line numbers are therefore the 1-based physical lines of the file (the parser enumerates `text.split("\n")` and strips only `\r`): a bad pattern raises the owner's `ConfigError("injection_patterns line N: ...", key="injection_patterns", line=N)`, which propagates unchanged out of `load_config` (not a ValidationError). The parsed tuple replaces the file layer's list only when the merged value still equals that list (recomputed exactly as step 4 does); a list set by a higher layer (profile, env, `--set memory.injection_patterns`) is kept and validated by `MemoryConfig`. The file is always parsed, so a bad file line fails the load even when overridden.

## Version rule (U10-16, brief rule 1)
Already implemented by frozen config_sources `_check_version`: every owner file's top-level `version` must be int 1 (bool/str/2/missing rejected, ConfigError names the file), stripped before the owner model except `sources.yaml` (SourcesConfig declares it). config.py restores it for metrics/weights (owner models declare it); AppConfig and PipelinesConfig declare `version: Literal[1] = 1`, so their default fills it. Verified end to end by ST10-37 for all 11 owner files.

## YAML defaults used (spec line refs = docs/impl/*.impl.md at this branch)
config/sources.yaml
- `version: 1` (U10-16; SourcesConfig `version: Literal[1]`).
- `dq.*` all ten keys = impl 02 §9 lines 3462-3471 (0.05, 0.30, 0.60, 0.40, 0.005, 0, 0.001, 0, 0.95, 0.05).
- `build.keep_last: 3`, `build.memory_limit: 75%`, `build.threads: null`, `build.service_ci_classes` [cmdb_ci_service, cmdb_ci_service_business, cmdb_ci_service_technical] = impl 02 §9 lines 3458-3461.
- Left out: the whole `sources:` map (impl 01 §9 lines 2842-2874). Every per-source key default there applies inside a `sources.<s>` section, and no section can be written without keys the spec gives no default for (`base_url`, `auth.method`, `auth.credentials`, `jira.flavor`, `mongodb.database`, `snowflake.account/warehouse/role`, entity lists). `sources.files.inbox` (default `data/inbox`) also sits inside such a section. Result: `cfg.sources.enabled_sources() == []`.
config/mappings.yaml
- `enums` = impl 02 §9 "Shipped enum template" lines 3478-3488, all seven domains, every source value quoted (numeric ServiceNow codes stay strings under strict models).
- `service_overrides: []` = line 3473.
- `custom_fields.servicenow.customer_impact_minutes: null`, `acknowledged_at: null`, `custom_fields.jira.{story_points,team,estimate_cost_usd,epic_link}: null` = lines 3474-3476 (synth value `u_customer_impact_minutes` is T11-16's overlay, left out).
config/resilience.yaml
- `resilience.*` and `schedule.*` = the design 08 §7 block (docs/specs/08-resilience-and-jobs.md lines 608-689), which impl 08 §9 (lines 2320-2343) names as the default for policies, max_attempts, gpu.*, windows and jobs, with the impl 08 amendments R-43 (`nightly.job.gpu_class: none`, §9 line 2342) and R-53 (OpenJev `bearer_secret: "secret:OPENJEV_API_KEY"`, §9 line 2335); R-51 ports 8000/8100/8200 as in design. Identical (model-equal) to the T08-26 fixture tests/unit/core/fixtures/resilience.yaml (UT08-03 `cfg_default`).
- Added `resilience.retry.retry_after_max_s: 86400` = impl 08 §9 line 2322 (not in the design block).
- Left out: nothing.
- detect-secrets: inline `# pragma: allowlist secret - reference` on the OpenJev `bearer_secret` line (same precedent as herness.yaml:34); baseline untouched.

## Acceptance
- `load_config(profile=p)` for local, hybrid, premium, synth on `write_repo_config` (every shipped file copied; adaptations: `version: 1` prepended to the copies of decisions/eval/models/memory; metrics.yaml `metrics: []` -> one catalog entry; for all four profiles the copy's herness.yaml gets `hybrid_approved/premium_approved: true`, `approved_by: ops-lead`, `approved_on: 2026-09-01` because the shipped file records no approval and U10-09 step 5 must fail hybrid/premium as shipped (still tested by `test_ut10_76_hybrid_fails_the_data_policy_gate`)): all load; offline cross-checks give only `warn` issues with ids in {C13, C08a} (C08a = docker/compose.yaml absent, T10-23).
- `validate(cfg_dir, "synth", offline=True)`: no error issues (`test_ut10_76_offline_validate_reports_no_errors_for_synth`).
- UT04-13 strict xfail and metrics.yaml untouched.

## Rulings / deviations
1. Brief names `AppSettings` (herness.reports.settings); the owner module has no such symbol. Its root model for config/app.yaml is `AppConfig` ("Root of `config/app.yaml`, mounted by spec 10 at `cfg.app` (U09-02)"); used that. No new symbol invented.
2. UT00-58 (tests/unit/repo/test_import_contracts.py, impl 00) pinned the "herness layers" ignore list to exactly the config.py exception. Adding the sibling's settings exception (required by the brief's layering rule and the dispatch) needs that test to accept the second line; changed to expect it iff `herness/core/_config_sections.py` exists. No wildcard exception was used (a `herness.core.* -> herness.**.settings` pattern would open the exception to all of core).
3. `_config_sections.py` holds the two composite classes plus the memory step; `HernessConfig` stays in config.py (spec U10-08 declares it there).
4. The memory override rule (higher layer list kept) is my reading of U10-16/U10-09 precedence; the spec does not say patterns are file-only.

## Remaining config-load gaps (owner)
- decisions.yaml, eval.yaml, models.yaml, memory.yaml ship without `version: 1`, so the bare repo `config/` still fails `load_config` with `decisions.yaml: version must be 1`; adding the line breaks the owners' direct-load tests (UT03-08 `DecisionsConfig.model_validate`, UT11-107 eval, UT05-125 raw key-set, UT07-04 `MemoryConfig.model_validate(_raw())`). Owners impl 03 (T03-02), 11 (T11-20), 05 (T05-04), 07 (T07-02) must pop/accept `version` in those tests, then add the line (test harness `_UNVERSIONED_STEMS` then shrinks). Not changed here: files outside card Files and owner tests.
- metrics.yaml `metrics: []` -> T04-08 (UT04-13 strict xfail).
- sources.yaml configures no source; synth sources/mappings content -> T11-16.
- premium.yaml: swarm caps and its stale comment naming `_PipelinesStub` -> T06-03 carry-over (pipelines is real now; overlay content not invented here).
- docker/compose.yaml (C08a warn) -> T10-23.
- `herness config validate` CLI acceptance -> T10-14.

## Line counts vs budgets
config.py 285/320; _config_sections.py 70/200; config_sources.py 390/390 (untouched); errors.py untouched.

## Evidence
RED (new tests run against base 9b794b5 `herness/` extracted with `git archive` and imported first; scratchpad runner):
- tests/unit/core/test_config_sections.py: collection ImportError `cannot import name '_config_sections' from 'herness.core'`.
- 10 failed, 64 passed across test_config_templates.py + test_st10_config.py: every UT10-76 load/validate test with `ConfigError: invalid config (12 issues ...): memory.compaction (memory.yaml): Extra inputs are not permitted; ...` / `app.app (app.yaml): Extra inputs are not permitted` (stubs reject the real files); both new ST10-37 repo-tree tests (same memory/app stub errors instead of the expected issue path / `injection_patterns line 18`).
GREEN:
- `PYTHONUTF8=1 pytest tests/unit/core tests/security tests/unit/repo tests/unit/harness/memory tests/unit/harness/pipelines tests/unit/reports tests/unit/model tests/unit/connectors -q -p no:logging --cov=herness.core.config --cov=herness.core._config_sections --cov-branch`: 2876 passed, 8 skipped (symlink/DuckDB-excel host skips); coverage of both changed modules 100% line, 100% branch (190 stmts, 26 branches, 0 missed).
Gates: ruff format --check (448 files unchanged), ruff check clean, mypy 0 issues (190 files), lint-imports 13 kept / 0 broken, check_module_size exit 0, check_type_ownership exit 0. Pre-commit on the final commit: all hooks passed incl. detect-secrets and pytest-unit (a first checkpoint attempt was stopped by detect-secrets on the resilience.yaml `bearer_secret` reference; fixed with the inline pragma, so no wip commit landed and the card is a single commit).
The full `(unit or integration) and not slow` suite was not run separately (dispatch: card tests + touched packages only); the pre-commit pytest-unit hook passed.

## Commit
3c520b2 feat(config): real app/memory sections and shipped sources, mappings, resilience (T10-03b)

## Fix round 1 (review T10-03b-review.md: I1, m3, m5, m6)
- I1: `version: 1` added to config/decisions.yaml, eval.yaml, models.yaml, memory.yaml (placed after the header comments; models.yaml 154 lines, under its <= 180 check). Owner direct-load tests now pop the root key (and assert it equals 1) before validating: tests/unit/enrich/test_enrich_settings.py UT03-08 (`test_ut03_08_shipped_config_files_validate`), tests/unit/eval/test_eval_settings.py `_raw` (UT11-107), tests/unit/harness/test_models_yaml.py `_raw` (UT05-125 incl. the raw key-set assertion), tests/unit/harness/memory/test_memory_settings.py `_raw` (UT07-04). Owner models untouched. No other test reads those four files raw (grep of tests/, herness/, tools/).
- tests/support/config_tree.py: `_versioned` and `_UNVERSIONED_STEMS` removed. `write_repo_config` copies every owner file byte for byte (public `OWNER_STEMS`), only metrics.yaml keeps the T04-08 one-entry stand-in (shared helper `_with_metric_entry`). `write_full_config` now copies decisions/eval/models/weights and the shipped resilience.yaml verbatim (was the T08-26 fixture plus a prepended version line; the two are model-equal, the shipped file adds only the explicit default `retry_after_max_s: 86400`). `RESILIENCE_FIXTURE` stays (test_jobs_validate uses it). The hybrid/premium approval edit of the copy stays in UT10-76.
- New `test_ut10_76_bare_repo_tree_fails_only_on_the_empty_metric_catalog`: `load_config` on the bare repo `config/` (local and synth) raises ConfigError whose only issue is `("metrics.metrics", "metrics.yaml")` and no "version must be 1". `test_ut10_76_owner_files_ship_with_version_1` now covers all 10 owner stems plus metrics.
- m3: new `test_ut10_76_file_layer_matches_config_sources_step_4` runs the real `FilesYamlSource` inside a load context on a tricky patterns file (CRLF, indented comment, blank, leading/trailing spaces, form feed, U+2028, tab) and asserts `layer["memory"]["injection_patterns"] == _file_layer(text)` (key presence asserted too). config_sources.py untouched.
- m5: `test_ut10_84_stub_sections_are_closed` renamed `test_ut10_84_owner_sections_are_closed`.
- m6: config/profiles/premium.yaml comment reworded: pipelines is PipelinesConfig; the swarm-cap overlay stays a carry-over to T06-03.
- Tests: card tests + touched owner packages (tests/unit/core, tests/security, tests/unit/repo, tests/unit/enrich, tests/unit/harness, tests/unit/reports, tests/integration/core, not slow): 3155 passed, 5 skipped (symlink privilege); tests/unit/eval + tests/fault/test_security_faults.py: 83 passed. ruff format/check clean, mypy 0 issues (190 files), lint-imports 13 kept, check_module_size exit 0. Production code unchanged in this round (config.py 285, _config_sections.py 70; coverage 100%/100% from round 0).
- Remaining gaps now: metrics.yaml `metrics: []` (T04-08) is the only failure of the bare repo tree; synth sources/mappings (T11-16); premium swarm caps (T06-03); docker/compose.yaml (T10-23); CLI validate (T10-14).
- .secrets.baseline: line numbers only (config/models.yaml entries +1 after the version line; tests/unit/enrich/test_enrich_settings.py entries +1), rewritten by the detect-secrets hook, LF kept; no entry dropped.
- Commit: 30817bb fix(config): ship version 1 in every owner file, pin step 4 filter (T10-03b). All pre-commit hooks passed.
- Gate fix (IT00-01): 57b088f fix(config): refresh secrets baseline line numbers (T10-03b). Six docs/impl/10-config-security-deployment.impl.md entries +1 line (the new §2 row) plus generated_at; LF kept, 78 entries before and after, none dropped.
