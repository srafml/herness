# T10-06 Secrets core: build report

Status: DONE_WITH_CONCERNS (minor, see Concerns)
Commit: 8ec7a74 feat(core): add secrets core with keyring and dotenv backends (T10-06)
Branch: worktree-agent-a4a672398dcd46850 (base b435704)

## What was built
- `herness/core/secrets.py` (288 lines, budget 360, ENG limit 400):
  - U10-27 `SECRET_NAME` (verbatim pattern), `SecretRef(str)` (`parse`, `name`; the str value is the lower-cased bare name; `ConfigError("invalid secret name")`, value never echoed), `SecretRefStr` / `SecretNameStr` (AfterValidator, ValueError -> pydantic validation error).
  - U10-28 `resolve` -> `SecretStr`, value added to `_KNOWN`; missing -> `ConfigError("secret not found: <name>")` (exact prefix kept for T10-03's key_id fallback).
  - U10-29 `resolve_json`: not an object / non-string member / invalid JSON -> `ConfigError("secret <name> is not a JSON object of strings")`, raised `from None` (no content chained); every member value remembered.
  - U10-30 `exists`: presence check, value not remembered.
  - U10-31 `set_secret` / `delete_secret(name, *, actor)`: parse name, validate value (8..16,384 chars, no CR, LF, NUL -> `ConfigError("secret value rejected: length or characters")`), refuse dotenv (`ConfigError("dotenv backend is read-only")`) BEFORE auditing, then `audit("admin_action", actor, action="secret_set"|"secret_rotate", target=<name>)`, then the backend write. Audit failure (FatalError) stops the action.
  - U10-33 `_SecretBackend` Protocol; `_KeyringBackend` (service `herness`, username lower-cased; values > 1,200 chars chunked into `<name>#1..#n` with header `chunked:v1:<n>`; `get` reassembles; `delete` removes all chunks; missing delete is a no-op via `PasswordDeleteError`; `KeyringError`/`RuntimeError` -> `ConfigError("secret backend unavailable: keyring", hint="run as the account that owns the credential")` + ERROR log `secrets.backend.unavailable` (fields `backend`, `error_type`), raised `from None`); `_DotenvBackend` (refused unless `HERNESS_ENV=dev` or profile `synth`; reads `<repo root>/.env` once per path with the U10-18 grammar; `HERNESS_SECRET__` + name with `.`/`-` -> `_`, upper-cased; set/delete read-only).
  - U10-34 `referenced_secret_names(cfg)`: walks `effective_dict(cfg)`, collects `secret:` strings, adds `redact.hmac_key`, `ui_user_ref_key`, skips mappings with `enabled: false`, lower-cases, de-duplicates, sorts.
  - Ruling 1: `_KNOWN`, `_KNOWN_LOCK`, `known_values()` (snapshot). No `scrub_secrets` (T10-07).
- `herness/core/audit.py` (ruling 2): `_known_values()` does a function-local `from herness.core import secrets  # noqa: PLC0415 - cycle: secrets imports audit`; `importlib` import dropped. 394 -> 390 lines.
- `tests/support/fake_keyring.py` (ruling 4): `MemoryKeyring` (in-memory `KeyringBackend`, settable `error` for fault tests) and the `fake_keyring` fixture (keyring.set_keyring, previous backend restored); registered in `tests/conftest.py` `pytest_plugins`. All secrets test modules request it via an autouse fixture.
- `pyproject.toml`: `herness.core.secrets` added to the "core base is closed" forbidden list (UT00-58 requires every core module there).
- `.secrets.baseline`: regenerated after staging (new test sentinels in test_secrets.py and test_st10_secrets.py); the regeneration dropped the audited `docs/impl/00-foundation.impl.md` and `docs/impl/10-config-security-deployment.impl.md` entries again; restored from HEAD; LF line endings verified (0 CR).

## Decisions (spec left open)
1. Backend selection reads `herness.core.config._Cache.config` directly (noqa SLF001). No config cached -> keyring; resolving never triggers a config load (U10-28 "before config is initialised the backend is keyring"). A test asserts `_Cache.config is None` after a resolve.
2. The `_DotenvBackend` precondition is checked on every selection, so a later profile switch via `init_config` (which runs no reset hooks) cannot keep a dotenv backend alive; only the parsed `.env` is cached (`functools.lru_cache`, cleared by the reset hook).
3. "Repo root" for `.env` = process working directory (`Path.cwd()`), consistent with `load_config`'s default `config_dir=Path("config")`, whose parent is where `FilteredDotEnvSource` reads `.env`. The constructor takes `root=` for tests.
4. Empty `.env` values (as in `.env.example`) read as unset (-> "secret not found"), never resolved to "".
5. `.env` parsing reuses `config_sources._DOTENV_LINE`, `_MAX_DOTENV_BYTES`, `_read_text` (private imports; config_sources.py is at its 390-line budget, so no public helper was extracted). Malformed line -> `ConfigError(".env line N: malformed")`.
6. Error text: U10-33's `secret backend unavailable: keyring` is the message; U10-28's "; run as the account that owns the credential" suffix is carried as `hint` (error table: "hint: run as the owning account"; FT10-05 "ConfigError with hint").
7. A chunked entry with a missing chunk reads as not found (None) rather than a new error message.
8. A short value that itself starts with `chunked:v1:` is stored chunked so it cannot be misread as a header.
9. `_KNOWN` and the dotenv cache are cleared by a `reset_config` hook (spec state table "cleared by reset_config hook", X-1), registered via `config._RESET_HOOKS.append`.
10. The dotenv write refusal happens before the audit line, so no audit line records an action that cannot happen (spec precondition "Backend keyring").

## Deviations / spec notes
- UT10-28's row shows `ConfigError("secret not found: x")`, but `x` is not a valid SECRET_NAME (needs >= 2 chars; UT10-30 lists `x` as invalid). The test uses `missing.key` and asserts the exact `secret not found: ` prefix.
- UT10-28 also carries the U10-34 tests (ruling 3); UT10-33 covers U10-31 only (ruling 3).
- Core unit tests on this tree live in `tests/unit/core/`, so the file is `tests/unit/core/test_secrets.py`. ST10-26 is in new `tests/security/test_st10_secrets.py` (marker unit, like the other st10 files); FT10-05 is appended to `tests/fault/test_security_faults.py` (marker fault, outside the unit gate; run separately, green).
- The existing `test_ut10_58_known_values_import_rules` was rewritten: the ModuleNotFoundError fallback no longer exists; it now asserts `_known_values()` returns `secrets.known_values()`.

## Evidence
- RED: with secrets.py moved aside, `pytest tests/unit/core/test_secrets.py tests/security/test_st10_secrets.py tests/fault/test_security_faults.py tests/unit/core/test_audit.py` -> 4 collection errors `ImportError: cannot import name 'secrets' from 'herness.core'`.
- GREEN: the same files + tests/security/test_st10_audit.py + tests/unit/repo -> 55 passed; coverage `herness/core/secrets.py` 198 stmts, 44 branches, 100 % line / 100 % branch; audit.py 99 %.
- Gates: ruff check clean; ruff format --check clean; mypy strict 0 issues (119 files); lint-imports 13 kept, 0 broken; check_type_ownership exit 0; check_module_size exit 0.
- `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` (pre-commit tree): 3293 passed, 5 skipped, 1 xfailed, 1 failed = IT00-01 pre-commit run, failing only because the baseline was unstaged in the uncommitted tree. After the commit, IT00-01 + fault file re-run: 4 passed; working tree clean.
- The commit ran the real pre-commit hooks (all Passed incl. detect-secrets, module-size, pytest-unit); no --no-verify, PRE_COMMIT_ALLOW_NO_CONFIG not set.

## Line counts
- herness/core/secrets.py 288 / 360 (72 lines left for T10-07's scrub_secrets; above the ~250 aim).
- herness/core/audit.py 390 / 395.

## Concerns
- secrets.py is 288 lines, above the ~250 aim; T10-07 has ~72 lines before the 360 budget (400 hard limit beyond that).
- Private imports from config_sources (`_DOTENV_LINE`, `_MAX_DOTENV_BYTES`, `_read_text`) couple secrets to config_sources internals; a public `read_dotenv` helper would be cleaner, but config_sources.py has no line budget left.
- The dotenv backend assumes repo root = cwd (decision 3).


## Fix round 1

Commit: 43ded39 fix(core): harden secrets core per T10-06 review (T10-06)

- m1: `.env` location now matches `FilteredDotEnvSource` and `_absolute_paths`. `herness/core/config.py` `_Cache` gained `root` (the parent of the config dir), set by `init_config` (`config_dir.resolve().parent`) and by `get_config`'s default load (`Path("config")`), and cleared by `reset_config`. `secrets._dotenv_root()` uses `_Cache.root`, else the active load context's `config_dir.parent`, and falls back to `Path.cwd()` only when neither exists (documented in the docstring). New tests: `test_ut10_31_dotenv_root_follows_config_dir` (a synth run on a temp tree with the developer's `.env` in the cwd does not read that file, and reads the tree's own `.env` once one is present) and `test_ut10_31_dotenv_root_fallbacks` (load context, then cwd). config.py is 298/320 lines.
- m3: `SecretRef.parse` always re-validates and lower-cases, including when given a `SecretRef`. Tests: a mixed-case `SecretRef` is lower-cased, `SecretRef("Bad Name")` is rejected, and `resolve`/`exists` on a directly built `SecretRef` (`test_ut10_30_unvalidated_ref_is_rechecked`).
- m4: `_keyring_call` maps any `Exception` (`noqa: BLE001`, with the reason) to `ConfigError("secret backend unavailable: keyring", hint=...)` plus the `secrets.backend.unavailable` event, raised `from None`. `PasswordDeleteError` is still a no-op. The `KeyringError` import was dropped. FT10-05 now also raises a stand-in for a raw `pywintypes.error` (6 events, no sentinel in the message, hint, cause or log).
- m6: `test_ut10_28_referenced_names_from_config` asserts that `vllm.api_key` and `anthropic.api_key` (real `secret:` references in the shipped models.yaml) are collected and that `typesafe_api_key` (the disabled `jev` decider) is not.
- m7: `tests/support/fake_keyring.py` has no unused ignore and no untyped `super().__init__()` call (the base only reads `KEYRING_PROPERTY_*` env). `mypy --strict` on the file: 0 issues.
- m2 and m5 parked as instructed.
- `.secrets.baseline`: one new sentinel entry (test_secrets.py). The regeneration dropped the docs/impl entries again; they are restored. LF endings (0 CR).
- Gates: ruff check, ruff format --check, mypy strict (119 files) clean; lint-imports 13 kept; check_type_ownership 0; check_module_size 0. Unit+integration (not slow): 3296 passed, 5 skipped, 1 xfailed (IT00-01 deselected while uncommitted, then re-run after the commit: passed). The secrets, st10, fault and audit files: 44 passed; secrets.py 100 % line and branch. The commit went through the real hooks, all passed.
- Line counts: secrets.py 295/360, config.py 298/320, audit.py 390/395 (unchanged).
