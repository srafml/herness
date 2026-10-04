# T10-14 report: config and secrets commands

Status: DONE_WITH_CONCERNS (behavior complete; the concerns below are for impl 09 / T09-24 and the sub-controller)
Branch: claude/w32-c10-T10-14 (base 3e8c337)
Commits: 0092499 wip(T10-14): config commands and herness.admin layer; 63dcfa3 feat(admin): T10-14 config and secrets commands

## What was implemented
- New L5 package `herness.admin` (R-07):
  - `herness/admin/__init__.py`: package marker only (docstring, `__all__: list[str] = []`, `# T10-19: register_handlers (maintenance job handler)`).
  - `herness/admin/commands_config.py`: `cmd_config_validate` (U10-65), `cmd_config_show` (U10-66), `cmd_config_hash` (U10-67), `OFFLINE_HASH_WARNING`.
  - `herness/admin/commands_secrets.py`: `cmd_secrets_init` (U10-68), `cmd_secrets_set` (U10-69), `cmd_secrets_status` (U10-70), `INIT_NAMES`; `# T10-30: cmd_secrets_rekey` marker.
- Every body returns `herness._cli.output.CommandResult` (ok == exit_code == 0; codes 0/1 only) and never prints; no role/elevation checks in the bodies.
- Behavior choices (binding readings):
  - validate: profile resolved with `resolve_profile(profile, os.environ)`; an unknown profile becomes the issue `error profile -: unknown profile: <name>` (exit 1, config_hash null, profile = the given name) so "none raised for config problems" holds. `issues = validate(...)`; then the config is loaded again only to compute `config_hash` (`key_id="unresolved"` when offline); a load failure gives `config_hash: null` (the load error is already in the issues). Offline + hash computed: the warn issue line `warn config_hash -: config_hash computed without key_id; not comparable to builds` is appended to `data["issues"]` and to `warnings`. Exit decision (1 on error, 1 on warn with strict, else 0) covers the validator issues only (ruling 3). A non-config `ConfigError` from `config_hash` itself (keyring backend unavailable while resolving the key id, online only) propagates.
  - show / hash: `load_config(profile, config_dir=...)`; a `ConfigError` at load propagates (impl 09 maps it to 3).
  - secrets set: name parsed first (`SecretRef.parse`; invalid name -> ConfigError before any prompt); prompts `Value for <name>` and `Repeat`; exit 1 with a warning reason and `data={"name","stored": false}` for: `values differ`; `value exceeds 16 KiB` (UTF-8 bytes > 16384); `a value starting with { must be a JSON object of strings`. Otherwise `set_secret` (which validates 8..16384 chars / no CR LF NUL, audits `secret_set` by name, then writes). ConfigError from `set_secret` (short value per UT10-33, backend unavailable, dotenv read-only) PROPAGATES (one reading applied everywhere, ruling 5): impl 09 maps it to 3.
  - secrets init: per name in order `redact.hmac_key`, `ui_user_ref_key`: present / (token_hex(32) shown once via `show` with the vault instruction, `prompt("Type ESCROWED after storing <name> in the vault")`, exact `ESCROWED` -> `set_secret` -> created, else `not created` + warning `<name> not created: escrow not confirmed`). Exit 0 when both present/created, else 1. Backend ConfigError propagates.
  - secrets status: `data={"secrets":[{"name","present","last_set"}]}` (last_set = `clock.format_utc` text or null); warnings `secret missing: <name>` (exit 1 when any) and `<name>: last set <N> days ago; rotate per policy` when `now - last_set > 90 days` (`clock.now()` from `herness.core.time`).
- Layering (ruling 1) and UT00-58 helper (ruling 1), see "Merge watch" below.

## Files changed
- A herness/admin/__init__.py (11 lines / budget 30)
- A herness/admin/commands_config.py (78 / 180)
- A herness/admin/commands_secrets.py (132 / 260)
- M pyproject.toml (import-linter: layers line, two forbidden lists)
- M tests/unit/repo/test_import_contracts.py (`_flatten` one-line change)
- A tests/unit/admin/test_admin_config.py (UT10-20 x7 funcs, UT10-15 x2, ST10-16 x2; marker unit)
- A tests/unit/admin/test_admin_secrets.py (UT10-72 x6, UT10-33 x7 incl. 6-case parametrize; marker unit)
- A tests/integration/admin/test_admin_cli.py (IT10-11 x4; marker integration)
- A tests/security/test_st10_admin.py (ST10-36 x2; marker unit, as tests/security/test_st10_audit.py)
- No `__init__.py` in the new test directories (repo convention: only tests/support has one; basenames are unique).

## RED evidence
1. tests/unit/admin/test_admin_config.py
   `uv run --frozen pytest tests/unit/admin/test_admin_config.py -q -p no:logging`
   ```
   tests/unit/admin/test_admin_config.py:18: in <module>
       from herness.admin.commands_config import (
   E   ModuleNotFoundError: No module named 'herness.admin'
   1 error in 0.32s
   ```
   (First green run then had 1 failure in my ST10-16 test: the sentinel stored as redact.hmac_key is not 64 hex and the redact key-id provider raised during load; fixed the test to store a 64-hex key.)
2. tests/unit/admin/test_admin_secrets.py (commit 0092499 present, secrets module absent)
   `uv run --frozen pytest tests/unit/admin/test_admin_secrets.py -q -p no:logging`
   ```
       from herness.admin.commands_secrets import (
   E   ModuleNotFoundError: No module named 'herness.admin.commands_secrets'
   1 error in 0.38s
   ```
3. tests/integration/admin/test_admin_cli.py and tests/security/test_st10_admin.py: written after the bodies existed, so RED was taken against a `git archive 3e8c337` snapshot in /tmp/w32-c10/builder/base with the repo .venv:
   `python -m pytest tests/integration/admin/test_admin_cli.py tests/security/test_st10_admin.py -q -p no:logging -p no:cacheprovider`
   ```
   E   ModuleNotFoundError: No module named 'herness.admin'   (both files)
   2 errors in 0.43s
   ```
   On the branch the first ST10-36 run also failed (`assert 2 == 11` for `deploy up large`, UsageError "Got unexpected extra argument(s) (large)") - a probe bug, see concern C2.

## GREEN evidence
- `uv run --frozen pytest tests/unit/admin/test_admin_config.py -q -p no:logging` -> `13 passed in 2.97s`
- `uv run --frozen pytest tests/unit/admin/test_admin_secrets.py -q -p no:logging` -> `18 passed in 15.43s`
- `uv run --frozen pytest tests/integration/admin tests/security/test_st10_admin.py -q -p no:logging` -> `6 passed in 22.68s`
- Card + touched packages: `uv run --frozen pytest tests/unit/admin tests/integration/admin tests/security/test_st10_admin.py tests/unit/cli tests/unit/repo -q -p no:logging` -> `120 passed in 93.63s`
- Checkpoint commit 0092499 ran every pre-commit hook incl. pytest-unit (whole unit suite, -x): all Passed.

## Gates (final tree)
- `ruff format .` -> 1113 files left unchanged; `ruff check --fix .` -> All checks passed!
- `mypy` -> Success: no issues found in 397 source files
- `lint-imports` -> Contracts: 15 kept, 0 broken.
- `python -m tools.check_type_ownership` -> exit 0
- `python -m tools.check_module_size` -> exit 0

## Coverage (`--cov=herness.admin --cov-branch`, card tests: 37 passed)
```
herness/admin/__init__.py               2      0      0      0   100%
herness/admin/commands_config.py       46      0      2      0   100%
herness/admin/commands_secrets.py      76      0     20      0   100%
TOTAL                                 124      0     22      0   100%
```

## Merge watch (verbatim diff vs base)
```
diff --git a/pyproject.toml b/pyproject.toml
index d5ddf5c..4ad4ce8 100644
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -283,8 +283,10 @@ include_external_packages = false
 name = "herness layers"
 type = "layers"
 layers = [
-    # impl 09 §2: the private CLI package sits above reports and eval (T09-20)
-    "herness._cli",
+    # impl 09 §2: the private CLI package sits above reports and eval (T09-20); herness.admin
+    # (impl 10 §3.7, R-07) is L5 beside it, non-independent: admin returns
+    # herness._cli.output.CommandResult and cmd_admin (T09-24) calls the admin bodies.
+    "herness._cli : herness.admin",
     "herness.eval | herness.reports",
     "herness.harness",
     "herness.metrics",
@@ -374,6 +376,7 @@ forbidden_modules = [
     "herness.enrich",
     "herness.reports",
     "herness._cli",
+    "herness.admin",
 ]
 
 [[tool.importlinter.contracts]]
@@ -546,6 +549,7 @@ type = "forbidden"
 source_modules = ["herness.core.*.settings"]
 forbidden_modules = [
     "herness._cli",
+    "herness.admin",
     "herness.cli",
     "herness.connectors",
     "herness.enrich",
diff --git a/tests/unit/repo/test_import_contracts.py b/tests/unit/repo/test_import_contracts.py
index 31d9296..59a0420 100644
--- a/tests/unit/repo/test_import_contracts.py
+++ b/tests/unit/repo/test_import_contracts.py
@@ -26,7 +26,7 @@ def _exists(module: str) -> bool:
 
 
 def _flatten(layers: list[str]) -> list[str]:
-    return [name.strip() for layer in layers for name in layer.split("|")]
+    return [name.strip() for layer in layers for name in layer.replace(":", "|").split("|")]
 
 
 def _config() -> dict[str, Any]:
```

## Deviations from the brief / dispatch
- D1 fake clock: the 90-day test uses a settable `clock.now` (monkeypatched `herness.core.time.now`, the pattern of tests/unit/core/test_audit.py `_Clock`) instead of the `fake_clock` fixture. With `fake_clock` (freezegun) entered after `init_config`, the test spun at 100 % CPU for >6 min (faulthandler showed it inside a lazy `herness.enrich.embed` -> lancedb -> pandas import). Same coverage of the rule (exactly 90 days: no warning; +1 s: warning).
- D2 checkpoint commits: each commit runs the whole unit suite in the hook (~40 min here), so after the first checkpoint (config unit) the secrets unit, IT10-11 and ST10-36 went into the final commit instead of separate wip commits.
- D3 commit trailer: the dispatch asked for `Co-Authored-By: Claude Fable 5.1 ...`; the session's harness attribution reminder (which governs commit attribution) specifies `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` plus the same `Claude-Session:` line, so both commits use the harness lines. The sub-controller may re-word on merge if the program wants the other trailer.
- D4 16 KiB: measured in UTF-8 bytes (spec says KiB); `set_secret` itself limits 16384 characters, so the body is the stricter of the two.
- D5 extra test functions beyond the rows: UT10-20 also covers load failure, unknown profile, HERNESS_PROFILE resolution and the online (non-offline) hash path; IT10-11 also runs a `denied` user (DENIED_ALLOWED) and the non-strict warning case; ST10-36 adds an admin control run (stores, audits `secret_set` by name, value never on stdout/stderr).

## Commit history notes
- Final commit attempt 1 failed in detect-secrets (`USER = "0123456789abcdef..."` audit actor id in tests/unit/admin/test_admin_secrets.py:34, Hex High Entropy); fixed with `# pragma: allowlist secret - audit actor id`.
- Attempt 2 failed in pytest-unit on an unrelated wall-clock test: `tests/unit/harness/test_tools_dispatch.py::test_ut05_70_results_in_call_order_and_concurrent` (`assert 1.57 < 0.5`, concurrency timing under load; 7585 passed). It passed 3/3 standalone; attempt 3 passed every hook. Flake note for the sub-controller.

## Concerns
- C1 (impl 09 / audit, real cross-spec conflict): impl 09 says `secrets init` runs with actor `unkeyed` on a fresh install (not in WRITE_COMMANDS, cli_actor need_ref=False), but `herness.core.audit._validate` accepts only `system`, `eval` or 32 hex as actor, and `set_secret` audits before writing. So `cmd_secrets_init(actor="unkeyed")` on a fresh install raises SchemaViolation at the first `set_secret`. The body takes `actor` as given (ruling 7); T09-24 (or a ruling) must decide what actor the wrapper passes for `secrets init` when `ui_user_ref_key` does not exist yet (e.g. `system`).
- C2 (impl 09 `guarded`, for T09-24): `guarded` sets `wrapper.__signature__` from `inspect.signature(handler)`; with `from __future__ import annotations` the annotations stay strings, typer's `inspect.signature(eval_str=True)` does not evaluate a preset `__signature__`, and its `get_type_hints` fallback drops `Annotated` metadata. Effect: `Annotated[str | None, typer.Argument()] = None` becomes an `--arg` OPTION, not an argument (my first ST10-36 probe failed with exit 2 on `deploy up large`). Plain required parameters and bool/str options with defaults still work (the test wrappers use those). T09-24's cmd_admin should either avoid string annotations with Annotated metadata or have `guarded` evaluate them (e.g. `inspect.signature(handler, eval_str=True)`).
- C3: `cmd_config_validate` loads the config twice (inside `validate` and again for `config_hash`) because `validate` (U10-13) does not return the loaded config; harmless (two `config.load.completed` log lines) but a later spec change could let `validate` return it.

## Carry-overs
- T09-24: replaces the test-local wrappers in tests/integration/admin/test_admin_cli.py (`config validate`) and tests/security/test_st10_admin.py (`secrets set`, probes for `privacy delete`, `deploy up`) with `herness._cli.cmd_admin`; it also adds the U09-98 R-71 owner-validator pass (`run_startup_validation(..., raise_on_error=False)`) when not `--offline`, the `--json` -> UserInputError rule for init/set/rekey (the ST wrapper already models it), `show` on stderr bypassing logging, and `prompt = typer.prompt(hide_input=True)`.
- T10-30: `cmd_secrets_rekey` (marker in commands_secrets.py).
- T10-19: `register_handlers` (maintenance job handler; marker in herness/admin/__init__.py).
- T10-12 carry-over "`herness config validate` CLI acceptance" is closed by IT10-11 (end to end through `herness.cli.main` on the real root app).

## Fix round 1
Commit: bbfcb30 (all hooks passed, including pytest-unit) fix(admin): T10-14 review round 1 (M1, M3-M8). It sits on 63dcfa3. M2 is parked by ruling and was not changed.

Per finding:
- M1: `test_ut10_72_init_creates_escrowed_keys` now runs `cmd_secrets_init` inside `structlog.testing.capture_logs()`, which captures events before any processor. It asserts that neither generated value appears in the captured events. No code change was needed.
- M3: new `test_ut10_20_offline_hash_uses_unresolved_key_id`. It imports `herness.core.redact` (the key-id provider) and stores a 64-hex `redact.hmac_key`. It then asserts that offline `config_hash == config_hash(cfg, key_id="unresolved")`, that online `config_hash == config_hash(cfg)`, and that the two differ. No code change was needed.
- M4: new `test_ut10_33_cmd_secrets_set_accepts_exactly_16_kib`, with two cases: ascii `"x"*16384` and two-byte `"é"*8192`, each exactly 16384 UTF-8 bytes. The test asserts exit 0 and checks the value with `resolve`, because the keyring stores long values in chunks. No code change was needed.
- M5: `cmd_secrets_init` now starts with `if get_config().security.secrets.backend != "keyring": raise ConfigError("secrets init needs the keyring backend")`. This runs before any value is generated, shown or prompted for. It uses only the public `herness.core.config.get_config`, and `secrets.py` is unchanged. `get_config()` is the config that `set_secret`'s audit already requires, and in the CLI `opts.config()` has cached it. New test: `test_ut10_72_init_refuses_non_keyring_backend` (dotenv/synth config: ConfigError; `show` and `prompt` never called). The dotenv set-up moved into a shared `_dotenv()` helper, which the existing dotenv `secrets set` test also uses now.
- M6: the two identical `_result` helpers are gone. Both modules build `CommandResult(...)` directly. This cost 5 lines in commands_config and 6 in commands_secrets.
- M7: `_hash_or_none` was replaced by `_load_hash`, which returns `(digest, load_error_issue)`. When the validator issues contain no `error` and the second load fails, `cmd_config_validate` appends `ConfigIssue("error", "config", exc.message[:300], None)`. This is the same fallback form `validate` uses, and its message names paths and keys only. The exit decision sees the new issue. New test: `test_ut10_20_second_load_failure_is_an_error` (the second `load_config` is monkeypatched to raise: exit 1, `config_hash` null, issue `error config -: config dir not found: config`).
- M8: an unknown profile now reports the name that was tried: `--profile`, else `HERNESS_PROFILE`, else `local`. New test: `test_ut10_20_unknown_profile_from_environment_is_named`.

RED evidence:
- New tests on the old code (`uv run --frozen pytest tests/unit/admin -q -p no:logging`):
  ```
  FAILED tests/unit/admin/test_admin_config.py::test_ut10_20_unknown_profile_from_environment_is_named   ({'profile': None} != {'profile': 'nope'})
  FAILED tests/unit/admin/test_admin_config.py::test_ut10_20_second_load_failure_is_an_error            (assert (True, 0) == (False, 1))
  FAILED tests/unit/admin/test_admin_secrets.py::test_ut10_72_init_refuses_non_keyring_backend          (Actual message: 'dotenv backend is read-only')
  3 failed, 34 passed in 7.69s
  ```
- M1, M3 and M4 pin behaviour that was already correct, so their RED comes from the reviewer's probes, re-applied to a temporary copy and then restored:
  - P2 (log the generated value in `_create`): `test_ut10_72_init_creates_escrowed_keys` -> `E assert not True ... any(<genexpr>)`, 1 failed.
  - P7 (`>` changed to `>=` in the 16 KiB check): `-k 16_kib` -> 2 failed.
  - P15 (offline hash with `key_id=None`): `test_ut10_20_offline_hash_uses_unresolved_key_id` -> `AssertionError: assert 'cfg_db1abea2ddb2f183' == 'cfg_16ce2ab3ccbddabf'`, 1 failed.
- My first run of the 16 KiB test also failed, because it compared the raw keyring entry (`chunked:v1:14`) instead of the resolved value. That was a test bug and it is fixed.

GREEN evidence:
- `uv run --frozen pytest tests/unit/admin -q -p no:logging` -> `37 passed in 7.17s`
- Card tests plus tests/unit/cli and tests/unit/repo -> `126 passed in 99.34s`
- Coverage (`--cov=herness.admin --cov-branch`, 43 card tests passed): `__init__` 100 %, commands_config 51 stmts / 4 branches 100 %, commands_secrets 82 stmts / 22 branches 100 %.

Gates:
- `ruff format .`: 1113 files unchanged.
- `ruff check`: All checks passed.
- `mypy`: Success, no issues in 397 source files.
- `lint-imports`: Contracts: 15 kept, 0 broken.
- `check_type_ownership`: exit 0.
- `check_module_size`: exit 0.
- `SKIP=pytest-unit pre-commit run` on the staged files: every hook passed.

Line counts against budgets: `__init__.py` 11/30, `commands_config.py` 83/180, `commands_secrets.py` 138/260.

Left unfixed: M2 only, which the ruling parks.
