# T09-20 report: CLI core

Status: DONE_WITH_CONCERNS (see Concerns). Base 35eb982. Branch worktree-agent-a63b9bca6e1070d22.

## Files (lines vs budget)
| File | Lines | Budget |
|------|-------|--------|
| herness/cli.py | 285 | 330 (45 left for T09-27 worker_bootstrap; may need a private sibling with a §2 row) |
| herness/_cli/__init__.py | 1 | 5 |
| herness/_cli/output.py | 248 | 250 |
| herness/_cli/identity.py | 143 | 220 |

Tests: tests/unit/cli/test_cli_output.py, test_cli_identity.py, test_cli_main.py; tests/security/test_st09_cli.py; tests/support/cli_env.py (new `cli_env` fixture); tests/fixtures/cli/command_table.md (§3.12 copy for UT09-66).

## Units done
U09-84 (main/app), U09-85 (GlobalOptions + root callback), U09-86 (CliResult, emit, emit_error, json_default), U09-87 (EXIT_CODES, exit_code_for, exit_code_for_class_name), U09-89 (cli_actor, COMMAND_ROLES, DENIED_ALLOWED, WRITE_COMMANDS, ELEVATED_COMMANDS, check_command_role, guarded), U09-105 (CommandResult + `to_cli_result(path)`), U09-106 (run_startup_validation).

## Tests (IDs)
UT09-63 (2 fns), UT09-64 (8), UT09-66 (10), UT09-85 (3, one parametrized x4), UT09-95 (8, one parametrized x13), UT09-102 (2 parametrized), UT09-103 (5), ST09-21 (2), ST09-24 (1).

- RED: `uv run pytest tests/unit/cli tests/security/test_st09_cli.py` gave 4 collection errors ("cannot import name 'cli' from 'herness'").
- GREEN: same command, 64 passed. Branch coverage: output 100 %, cli 99 %, identity 95 %.
- Gates on touched files: ruff check/format clean, mypy clean, lint-imports 14 kept / 0 broken, check_module_size 0, check_type_ownership 0.
- tests/unit/repo green (UT00-58, UT08-103); tests/unit/reports, jobs validate and enrich settings green.
- Pre-commit passed on the checkpoint commit (all hooks, pytest-unit included).

## Entry point
- `uv run herness --help` exits 0.
  - stdout: "Usage: herness [OPTIONS] COMMAND [ARGS]...", then the options --config-dir [default: config], --data-dir, --profile, --set, --json, --quiet, --verbose, --version and --help, then an epilog "Exit codes: 0 ...; 1 ...; ... 130 ..." built from EXIT_CODES.
  - stderr: INFO JSON log lines only (core.logging.configured, egress.guard.installed, cli.command.completed).
- `uv run herness --version` prints `herness 0.1.0` and exits 0.

## pyproject.toml / tests/conftest.py hunks (why)
- `herness layers`: a new top layer `"herness._cli"` above `"herness.eval | herness.reports"`. `|` makes siblings independent, and _cli imports herness.reports.rules. UT00-58 requires every herness package in the list. herness.cli is a module, not a package, and UT00-58's set equality forbids listing it.
- `core base is closed` forbidden_modules: added `"herness._cli"` (UT00-58: every top package except core).
- `resilience-settings-light` forbidden_modules: added `"herness._cli"` and `"herness.cli"` (UT08-103 requires every top-level herness module).
- tests/conftest.py: `"tests.support.cli_env"` added to pytest_plugins plus one docstring line. It is the shared `cli_env` fixture for the unit and security CLI tests.
- No other pyproject change. The entry point line is untouched; typer and rich are already dependencies.

## Deviations and rulings
1. typer 0.27 vendors click as `typer._click`. The spec's `click.exceptions.UsageError` maps to `typer._click.exceptions.UsageError`. Typer itself converts KeyboardInterrupt to Exit(130), which main returns as 130.
2. main configures stderr logging BEFORE installing the socket guard (spec order is guard, then logging). install_socket_guard logs, and unconfigured structlog prints to stdout, which would break TH09-22. The guard still runs before any command code.
3. `GlobalOptions.config()` calls `herness.core.config.init_config` (load_config plus the get_config cache) instead of bare load_config. audit(), bind_core_backends() and the owner validators read get_config(), which must be the CLI's config so that --config-dir is honoured.
4. GlobalOptions has one extra field, `command` (default "herness"). `guarded` sets it to the command path. main uses it for the error envelope, the cli.command.* logs and the metric label (spaces become "." because of the label regex).
5. Bootstrap without `<config_dir>/herness.yaml`: `BootstrapConfig(resolve_profile(...), SecurityConfig(), ())`, the secure default. load_bootstrap raises on a missing file.
6. emit_error:
   - message and hint come from `user_message(exc)`;
   - usage errors keep the click text, with fix "Run `herness --help`.";
   - non-Herness, non-usage errors get error.type `InternalError`.
   - emit's `ok` = (exit_code == 0), so UT09-102 "envelope ok matches" holds.
7. CommandResult also rejects `ok != (exit_code == 0)` (U09-105 precondition) with the same SchemaViolation.
8. safe_terminal_text (U09-100, T09-25 term.py) does not exist yet. output.py has private `_clean`/`_markup` implementing the U09-100 algorithm, with a `# T09-25:` marker to switch over. stderr Error/Fix/Warning lines are plain writes through `_clean`; markup escape is used only for rich consoles.
9. The `auth` audit has no `action` field, following the existing T09-02 controller ruling (the action is in the `cli.auth.denied` log line). An `unkeyed` actor fails the audit actor check: logged as `cli.auth.audit_failed`, and the refusal stands.
10. An unknown command path fails closed (needs admin). An elevation refusal is audited and logged like a role refusal.
11. Command groups (ruling): `_register_groups` keeps the `# T09-22:` .. `# T09-25:` markers, with no try/except ImportError. --help and --version work with zero subcommands (the callback makes a TyperGroup), so no placeholder command was needed. `herness` with no args prints help and exits 2 (NoArgsIsHelpError is a UsageError).
12. Metric `herness_cli_commands_total{command, exit_code}` goes through the existing sink `herness.core.resilience.metrics.record_counter` (component `cli`). It is flushed at exit only when this run bound the ports.
13. Start-up validators, registered idempotently (same objects every call):
    - `metrics.catalog`: module-level adapter per U09-106 that reads `<config_dir>/metrics.yaml`;
    - `resilience`: validate_resilience_config;
    - `enrich.deciders`: module-level wrapper of check_decider_refs.

    Neither of the two extra validators broke the tests or the full config tree.
14. `metrics_owner_validator` (U04-83, name `metrics`, the weight-confirmation gate) is NOT registered.
    - It reads the ops review_item table. On an unmigrated store it raises `SchemaViolation: ops read failed: OperationalError`, which becomes an error issue, so every command would exit 3 before `herness init`.
    - It also repeats the catalog check of `metrics.catalog`.
    - The marker at herness/metrics/catalog.py:366 is left unchanged.
    - Needs a controller ruling: register it after init/migration, or split the weight gate out.

## Carry-overs
- T09-22 / T09-24: existence of the registered commands `deploy install`, `secrets rekey` and `gpu load large`. UT09-66's "every registered path is in COMMAND_ROLES" check is vacuous until the groups register. COMMAND_ROLES already equals the §3.12 fixture (53 paths).
- T09-22..T09-25: replace the markers in `herness.cli._register_groups` with the `register(app)` calls.
- T09-25: create herness/_cli/term.py and switch output.py `_clean`/`_markup` to safe_terminal_text.
- T09-27: worker_bootstrap in cli.py (45 lines of budget left); reuse run_startup_validation.
- T09-13 / impl 04 owner: the U04-83 weight-gate registration (deviation 14).
- Pilot blocker (shipped `local` profile, egress off, LLMRegistry ConfigError): not hit. The start-up validators build no LLM registry, and the tests use tests/support/config_tree.write_full_config.

## Concerns
- UX: every command writes INFO JSON log lines to stderr (spec: INFO to stderr), so interactive users see about three log lines even for --help.
- output.py is at 248/250 lines and cli.py at 285/330.

## Commits
- 4dfad43 wip(T09-20): CLI core green (all code and tests; pre-commit passed incl. pytest-unit)
- 27f4344 feat(cli): T09-20 CLI core (empty card commit; all hooks passed)

## Fix round 1 (review briefs/T09-20-review.md)
- I-1: the exit-code table and its mapping moved to the new private sibling `herness/_cli/_exit_codes.py` (100 lines, budget 120); `output.py` re-exports them. The sibling has a §2 module-map row in docs/impl/09-outputs-and-cli.impl.md (same commit). `_taxonomy()` now imports the taxonomy modules (herness.connectors.files, herness.connectors.http, herness.harness.memory.types, herness.model.errors, herness.store.errors) on lookup, so the mapping no longer depends on what the process already imported.
  - New test `test_ut09_63_class_names_in_a_fresh_process` runs a subprocess. It asserts that herness.harness.memory.types is not loaded yet, then that MemoryNotFound, DqGateFailed, BuildSqlError, MigrationError and NotFoundError map to 7 5 5 5 7.
  - In-process asserts added: DqGateFailed -> 5, NotFoundError -> 7.
- M-3: the --verbose traceback now passes through `scrub_secrets` (structlog processor applied to `{"event": text}`; on scrub failure the fixed `log.scrub.failed` text is written). New test `test_ut09_64_verbose_traceback_scrubbed`: a GitHub-token-shaped value added as an exception note does not reach stderr.
- M-4: `json_default` refuses a naive datetime with TypeError ("naive datetime is not serialisable ..."). `emit` turns that into SchemaViolation, so output never guesses a time zone. The json_default test covers it.
- M-8: new public property `GlobalOptions.config_loaded`; main uses it instead of `_cfg`. Tests updated (UT09-85).
- Line counts: output.py 185/250, _exit_codes.py 100/120, cli.py 290/330 (40 left for T09-27), identity.py 144/220.
- Results: card tests + tests/unit/repo: 87 passed. Branch coverage: _exit_codes 100 %, output 100 %, cli 99 %, identity 95 %. ruff, ruff format, mypy, lint-imports (14 kept / 0 broken), check_module_size and check_type_ownership all clean.

- Fix round 1 commit: 837d4da (hooks passed; .secrets.baseline: only line-number shifts for docs/impl/09 entries, LF)
