# T09-20 review (CLI core): verify agent, head 27f4344 (code 4dfad43), base 35eb982

### Spec Compliance
- ❌ Issues found: one. U09-87 `exit_code_for_class_name` only finds classes whose modules are already imported. In a real CLI process it maps `MemoryNotFound` and `DqGateFailed` to 1 when the table says 7 and 5 (Important I-1).
- ⚠️ Cannot verify, or accepted under a ruling:
  - The `auth` audit has no `action` field, per the T09-02 ruling; the action is in the `cli.auth.denied` log line.
  - UT09-66 "every registered path is in COMMAND_ROLES" passes vacuously, and the existence of `deploy install`, `secrets rekey` and `gpu load large` is carried to T09-22/T09-24 (ruling).
  - The U04-83 weight gate is not registered (ruling).
  - `init_config` is used in place of `load_config` (deviation 3). This is reasonable: audit, ports and validators read `get_config()`.
  - Elevation on Windows (`IsUserAnAdmin`) was checked only as "returns a bool". A real elevated or non-elevated run was not observed.

| Unit / row | Result | Note |
|---|---|---|
| U09-84 main/app | ✅ | Typer flags match. Guard runs before groups are registered and before the app is invoked. Outcomes: int, usage → 2, Exit, Ctrl+C → 130 (typer turns it into Exit(130)), HernessError → emit_error, Exception → `cli.command.failed` + InternalError + 1. Completion log and counter present. Logging is configured before the guard (M-1). |
| U09-85 GlobalOptions | ✅ | Fields, `--set` regex (fullmatch), `--quiet`/`--verbose` → exit 2, eager `--version` with no config load (observed: `herness 0.1.0`, exit 0), `--data-dir` → `paths.data`, config cached, startup_validation flag. Extra field `command` (deviation 4). |
| U09-86 emit/emit_error/json_default | ✅ (M-2) | Envelope shape `cli/1`, one compact line, `ensure_ascii=False`; warnings go to stderr; Error/Fix lines go to stderr; traceback only with `--verbose`; unserialisable output → SchemaViolation. `ok` is derived from exit_code (M-2). |
| U09-87 EXIT_CODES / exit_code_for | ✅ | Keys are exactly 0–14 and 130. Table order 3→13; CircuitOpen routed by key. |
| U09-87 exit_code_for_class_name | ❌ | I-1 |
| U09-89 identity | ✅ | COMMAND_ROLES has 53 paths and equals the §3.12 fixture. DENIED_ALLOWED, WRITE_COMMANDS and ELEVATED_COMMANDS match the spec. `init` bootstrap, `--inline` needs admin, elevation check, refusal audited and logged, unknown path fails closed. |
| U09-105 CommandResult | ✅ | Frozen; 2, 130, a code not in the table, or `ok` disagreeing with the code → SchemaViolation with the spec text. |
| U09-106 run_startup_validation | ✅ | Registers metrics.catalog (adapter: ≤ 1 MiB, safe_load, model_validate, one `error` issue at `metrics`), resilience and enrich.deciders, all as stable objects, so a repeat registration is a no-op. Logs counts. ConfigError details string ≤ 2000. Returns issues. |
| UT09-63 | ✅ | Full table, both usage classes, interrupt, ValueError, class names, EXIT_CODES keys. It masks I-1 by importing MemoryNotFound at test_cli_output.py:19. |
| UT09-64 | ✅ | JSON and human modes, success and error, stdout one line, warnings on stderr, json_default. |
| UT09-66 | ✅ (ruling) | Fixture table equality, R-47 rows in the table, denied user refused everywhere except doctor and config validate, viewer refusals, inline and elevation, guarded. |
| UT09-85 | ✅ | `--quiet --verbose` and three malformed `--set` values → 2, handler not run. |
| UT09-95 | ✅ | 13 classes through main; event order `guard` before `handler`; JSON envelope on error; InternalError with no secret in the logs; metric and completion log. |
| UT09-102 | ✅ | Codes 0, 1 and 3 convert and emit with matching `ok`; 2 and 130 raise. |
| UT09-103 | ✅ | Error → ConfigError with a string `details["issues"]`; raise_on_error=False; warn never raises; registered once; config() with and without the hook; CLI exit 3. |
| ST09-21 | ✅ | Viewer and denied users: build and sync exit 11 with PermissionDenied and the handler never runs; doctor allowed; admin control case. |
| ST09-24 | ✅ | `capfd` (fd level) with `--json --verbose` and a logged warning: stdout is one JSON line, logs are on stderr. |
| TH09-22 | ✅ | Observed: `herness --json nosuch`, `--json --set Bad=1 x` and `--json` put exactly one JSON object on stdout; JSON logs go to stderr only. |
| TH09-27 | ✅ | Role check, `--inline` admin, elevation check. |
| TH09-28 | ✅ | `install_socket_guard` with bootstrap or secure default runs first in `_run`, before any group or handler. |
| Secrets | ✅ (M-3) | `--set` values are not echoed in the BadParameter message; unexpected exceptions become "Unexpected error."; HernessError details are str-only. |
| Budgets | ✅ | cli.py 285/330, output.py 248/250, identity.py 144/220, `__init__` 1/5. |
| Layering | ✅ | `lint-imports`: 14 kept, 0 broken; pyproject and conftest hunks match the rulings. |

Runs (TMP=C:\Users\santh\AppData\Local\Temp\w30-s09):
- `uv run pytest tests/unit/cli tests/security/test_st09_cli.py -q -p no:logging`: 64 passed.
- `ruff check` on the touched files: clean.
- `herness --help`: exit 0 and help on stdout.
- `--version`: `herness 0.1.0`, exit 0.
- `--json nosuch` / `--json`: exit 2 with a UsageError envelope.
- `--json --set Bad=1 x`: exit 2 with a BadParameter envelope.
- `git status`: clean.

### Strengths
- The error boundary is tight. Usage, interrupt, Herness and unexpected errors are separated, and stdout purity is proved at fd level.
- The exit table is a data-driven tuple in table order, and CircuitOpen is routed by its key.
- The identity table is checked against a fixture copy of §3.12. The denied and viewer sweeps go over every path.
- Owner validators are registered with stable module-level objects, so a repeat registration is a real no-op under the impl 10 duplicate rule.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
- **I-1 `herness/_cli/output.py:94-108`: `exit_code_for_class_name` depends on import state.**
  - **Cause:** `_taxonomy()` walks `HernessError.__subclasses__()`, which lists only classes already imported. Probe in a fresh process after `import herness.cli`:
    - `MemoryNotFound` → 1 (spec: 7, named explicitly in the U09-87 row).
    - `DqGateFailed`, `BuildSqlError` and `MigrationError` → 1 (spec: 5, "a `build_pipeline` job failed by DQ").
    - `NotFoundError` (store) → 1 (spec: 7).
    - `herness.harness.memory.types` and `herness.model.errors` are not in `sys.modules`.
  - **Impact:** U09-91 job waits (`build`, `memory ...`) will exit 1 instead of 5 or 7.
  - **Why the tests miss it:** `tests/unit/cli/test_cli_output.py:19` and `tests/unit/cli/test_cli_main.py:21` import `MemoryNotFound`, so the class is loaded.
  - **Fix, either of:**
    - inside `_taxonomy()`, lazily import the modules that define taxonomy subclasses (`herness.harness.memory.types`, `herness.model.errors`, `herness.store.errors`, `herness.connectors.files`);
    - or keep a static name → parent-class table for the subclasses defined outside core.
  - **Test:** add a fresh-interpreter (subprocess) case for `MemoryNotFound` → 7 and `DqGateFailed` → 5.

#### Minor (Nice to Have)
- **M-1 `herness/cli.py:249-251`:** logging is configured before the guard is installed, while the spec order is guard (step 2) then logging (step 3). The rationale (deviation 2: unconfigured structlog writes to stdout) is sound, and no command code runs before the guard. Needs a spec note.
- **M-2 `herness/_cli/output.py:205`:** `emit` sets `ok = exit_code == 0`, but the spec literal for `emit` is `"ok": true`. So a job result with exit 6 (partial run, "report still written") gives `ok: false`. This reconciles UT09-102 "envelope ok matches". Needs a controller or spec note so automation knows that exit 6 gives `ok:false` with `data` present.
- **M-3 `herness/_cli/output.py:246-247`:** the `--verbose` traceback goes to stderr without `scrub_secrets`. An exception message from third-party code could carry a secret value (ENG §3.4). Consider scrubbing it.
- **M-4 `herness/_cli/output.py:176-177`:** a naive `datetime` is read as local time by `astimezone(UTC)`. Spec: ISO-8601 UTC. Treat a naive value as UTC, or reject it.
- **M-5 `herness/cli.py:266`:**
  - **Behaviour:** `EOFError` inside a command turns into click `Abort` (typer/core.py), which is re-raised under `standalone_mode=False` and becomes `InternalError`, exit 1, with a `cli.command.failed` ERROR log.
  - **Fix:** consider mapping `Abort` explicitly once prompts exist (T09-22+).
- **M-6 `herness/_cli/identity.py:99-105`:** without `ui_user_ref_key` the actor is `unkeyed`. The audit's actor check then rejects it, so the refusal is logged (`cli.auth.audit_failed`) but not audited. This is only reachable before `secrets init`. Document it.
- **M-7 `herness/_cli/identity.py:72-75`:** `doctor` and `config validate` are mapped to `viewer`, not the table's `denied (allowed for all)`. This has no effect (the DENIED_ALLOWED short-circuit runs first), but the mapping diverges from the table text.
- **M-8 `herness/cli.py:283`:** `main` reads the private `opts._cfg`. Expose a small `loaded` property instead.
- **M-9 budget:**
  - `herness/_cli/output.py` is at 248/250 lines.
  - `herness/cli.py` is at 285/330 with `worker_bootstrap` (T09-27) still to come.
  - Flag this for T09-25/T09-27 planning: a private sibling module needs a §2 row.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Every unit, test row and threat is met, with the deviations covered by rulings or sound reasons. The exception is `exit_code_for_class_name`, which silently returns 1 for taxonomy subclasses defined outside `herness.core.errors` in a real CLI process (MemoryNotFound → 7 and DQ failure → 5 are spec-named), and its test hides this by importing the class. One focused fix and a fresh-process test close it.

## Re-review round 1 (commit 837d4da)

Scope: I-1, M-3, M-4 and M-8. The sub-controller parked M-1, M-2, M-5, M-6, M-7 and M-9.

| Item | Result | Evidence |
|---|---|---|
| I-1 | ✅ Fixed | New private sibling `herness/_cli/_exit_codes.py` (100 lines; budget 120 in the new §2 row of docs/impl/09). It holds EXIT_CODES, the table and the two mappers. `_taxonomy()` (lines 74-94) imports connectors.files, connectors.http, harness.memory.types, model.errors and store.errors on first lookup. output.py re-exports all three names; `__all__` is unchanged. New test `test_ut09_63_class_names_in_a_fresh_process` runs a subprocess and asserts memory.types is not preloaded, then gets 7 5 5 5 7 for MemoryNotFound, DqGateFailed, BuildSqlError, MigrationError and NotFoundError. |
| M-3 | ✅ Fixed | output.py:181-184 runs the traceback through `scrub_secrets`, falling back to a fixed text when the scrubber fails. The new test injects a bearer token as an exception note and asserts the token is not on stderr. |
| M-4 | ✅ Fixed | output.py:109-111: a naive datetime raises TypeError, so `_write_json` turns it into SchemaViolation. Tested. |
| M-8 | ✅ Fixed | `GlobalOptions.config_loaded` property (cli.py:90-93), used in main:288. Tests updated. |

Checks:

- Layering: `_exit_codes` imports core.errors, reports.rules and typer, and lazily importlibs L1-L4 modules from L5. `lint-imports`: 14 kept, 0 broken.
- The re-export breaks nothing: cli.py still imports from `herness._cli.output`, and the tests import through output.
- Card tests: 66 passed.
- ruff check, ruff format --check, mypy (5 files) and check_module_size are clean.
- `.secrets.baseline` only shifts line numbers for the inserted §2 row.
- Sizes: cli.py 290/330, output.py 185/250, _exit_codes.py 100/120.
- `git status` is clean.

New findings: none.

**Task quality (round 1):** Approved
