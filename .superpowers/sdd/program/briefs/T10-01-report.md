# T10-01 report: section models for herness.yaml

Status: DONE_WITH_CONCERNS
Commit: 955cbd0 feat(core): add herness.yaml section models (T10-01)
Worktree: D:\herness\.claude\worktrees\agent-a1c271915ada95249 (branch feat/impl00-foundation-runtime)

## What was implemented
- `herness/core/settings.py` (new, L0). It holds the 19 models from the module map: PathsConfig, DataPolicyConfig, SecretsConfig, RedactionConfig, EgressConfig, NetworkConfig, ExposeConfig, RolesConfig, UiConfig, SecurityConfig, LoggingConfig, RetentionConfig, BackupConfig, ReasoningDeploy, OpenJevDeploy, LargeDeploy, ServiceDeploy, ReleaseDeploy and DeployConfig. It uses the exact field names, defaults, ranges and regexes of U10-02 to U10-07.
  - All models share a private `_Section` base with `extra="forbid"`, `strict=True` and `frozen=True`.
  - `Path` fields use lax mode (`Field(strict=False)`), and a before-validator rejects empty and NUL values.
  - `approved_on` accepts only a `date` or an ISO `YYYY-MM-DD` string. A `datetime` is rejected.
  - `destinations` and `extra_allowed_hosts` are lower-cased and hold at most 32 entries.
  - Usernames are lower-cased and de-duplicated in order, with at most 500 per list.
  - `trusted_proxy` and `bind` must be IP literals (checked with `ipaddress`).
  - Redaction patterns follow U10-04:
    1. At most 500 characters.
    2. Rejected if they match the nested-quantifier regex, written verbatim from the spec.
    3. Must compile with `re.compile`.
    4. At most 64 patterns per list and at most 64 custom patterns.
    5. `extra_names` entries are 2 to 128 characters.
    6. `denylist_domains` entries must pass the C04 hostname rule and must not be IP literals.
    7. `key` uses the secret-reference pattern of U10-27, written out locally (R-03/R-72).
  - Deploy values at load time must match `^[A-Za-z0-9._:/@+-]{1,256}$` or a `<placeholder>`. `model_root` and `env_file` must be absolute POSIX paths with no `..` segment and not under `/mnt/`. Each port is 1024–65535, and the reasoning, openjev and large ports must all differ. The release `repo`, `signer_workflow` and `duckdb_extensions` (excel → 64 lower-case hex) patterns are enforced.
  - Deploy-time pin rules (UT10-61 and C13): `DeployConfig.unpinned_keys(classes=PIN_CLASSES) -> tuple[str, ...]` returns the unpinned keys. `DeployConfig.require_pinned(classes=PIN_CLASSES)` raises `ConfigError("deploy.<class>.<field> is not pinned", hint=..., key=...)` for the first one. It uses the verbatim image, model, revision, gguf, sha256, served_name and parser regexes. `PinClass` and `PIN_CLASSES` are module-level helpers.
- `pyproject.toml`: added the impl-00 contracts that UT00-58 requires once a settings module exists. C4 "core base is closed" covers the 7 base modules and forbids `herness.core.settings`. C6 "settings modules are leaves" makes `herness.core.settings` forbidden from importing logging, `_log_pipeline`, ids, time and numbers, with indirect imports allowed. The mypy `files` list needed no change.
- Tests:
  - `tests/unit/core/test_config_settings.py`: UT10-19 model cases (defaults, extra and frozen, paths, security, redaction, UI, logging, retention and backup, deploy load-time rules, distinct ports, release block) and UT10-61 (pinned passes; placeholder, tag instead of digest, 39-hex revision and other unpinned values each raise `ConfigError("deploy.<key> is not pinned")`; class filter).
  - `tests/security/test_st10_config.py`: ST10-37 (`(a+)+$`, a 600-character pattern and other nested-quantifier forms are rejected, with the key in the loc, in under 1 s, and the pattern text is not in the error).

## RED evidence
`uv run pytest tests/unit/core/test_config_settings.py tests/security -q -p no:logging`
-> `E ModuleNotFoundError: No module named 'herness.core.settings'` / `2 errors during collection`

## GREEN evidence
- `uv run pytest tests/unit/core/test_config_settings.py tests/security -q -p no:logging --cov=herness.core.settings --cov-branch` -> `115 passed`; `herness\core\settings.py 218 stmts, 0 miss, 22 branches, 0 partial, 100%`
- `uv run pytest -k "UT10_19 or UT10_61 or ST10_37"` -> `115 passed, 107 deselected`
- `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` -> `218 passed, 4 deselected`

## Gates
- `ruff format --check .`: 32 files already formatted. `ruff check .`: All checks passed.
- `uv run mypy`: Success, no issues found in 13 source files.
- `uv run lint-imports`: 6 kept, 0 broken.
- `uv run python -m tools.check_type_ownership`: exit 0 (six INFO pending owner lines only).

## Line counts
- `herness/core/settings.py`: 365 lines. The budget is 340 and the ENG hard limit is 400. **Over budget by 25.** The module was already compacted: list→tuple conversion in one base validator, shared `Annotated` aliases, a `_must` validator factory and no `__all__`. The rest is 19 one-line class docstrings plus the verbatim spec regexes. A ruling is requested to raise the budget to about 370, as was done for `_log_pipeline.py` in T00-07. The alternative is to drop the class docstrings.

## Deviations and interpretations
1. **ST10-37 and UT10-19 assert pydantic `ValidationError`, not `ConfigError`.** The spec says model violations become `ConfigError` in U10-09 `load_config` (T10-03), and `validate` (U10-13) and the C-rules (U10-20) belong to T10-03 and T10-12. At this card's level, the tests assert the ValidationError type and loc path. T10-03 should add the `ConfigError` end-to-end assertion for ST10-37.
2. **Tuple fields in strict mode reject lists, and YAML produces lists.** `_Section` has a `model_validator(mode="before")` that turns lists into tuples recursively; items are still validated strictly. Other settings cards (impl 01–09) will hit the same pydantic behaviour.
3. **`RedactionConfig.directory_file` is also a lax `Path`.** The spec calls PathsConfig "the one strict exception", but `directory_file` is a `Path` read from YAML too, so it would be unusable otherwise.
4. **Deploy-time pin rules are exposed as `DeployConfig.unpinned_keys` and `require_pinned`.** The spec names no function for them; they are for U10-79, U10-82, U10-83 and C13. The error message follows the verbatim format `deploy.<key> is not pinned`, with context `key`.
5. **Which deploy strings get which pattern.** The generic argv-safe/placeholder rule is applied to every free-form deploy string: image, model, revision, served_name, parsers, gguf, sha256, account and licence_exceptions items. Fields with their own spec pattern use that pattern instead, with no placeholder: wsl_distro, model_root, env_file, repo, signer_workflow and duckdb_extensions values. `ctx` and `openjev.gpu_util` have no range in the spec and none was invented.
6. **`denylist_domains` is not lower-cased first.** It must already match the lower-case C04 regex.

## Concerns
- The line budget overrun (365 vs 340) needs a ruling.
- The acceptance command in the brief, `pytest -k "UT10-19 or UT10-61 or ST10-37"`, selects 0 tests, because test names use `_` as the global constraints require. Use `-k "UT10_19 or UT10_61 or ST10_37"`.
- Test file locations follow the repo convention (`tests/unit/core/`), with `test_config_*` naming from spec 10 §11. The security test is in the new `tests/security/` directory from spec 10 §11, marked `unit`.

## Fix round 1 (commit 0a8fcba fix(core): tighten settings patterns and split deploy tests (T10-01))
- M2: `nightly_at`, `http_proxy` and `_ISO_DATE` now use `[0-9]`, not the Unicode-aware `\d`. New UT10-19 cases reject `"0\u0661:30"` and `"http://p:\u0668\u0660"`.
- M3: `_is_posix_path` now requires at least one non-empty path segment. New cases reject `model_root="//"` and `env_file="/"`.
- M4: the deploy load-time, ports, release and UT10-61 tests moved to `tests/unit/core/test_config_settings_deploy.py` (206 lines). `test_config_settings.py` is now 297 lines.
- M5: removed the `str()` wrapper in `unpinned_keys`.
- M6: the ST10-37 timing bound is now < 10 s. That still catches catastrophic backtracking without flaking on a busy CI runner.
- Gates: ruff check is clean and ruff format left all 33 files unchanged. mypy reports 0 errors and lint-imports kept all 6 contracts. `pytest -k "UT10_19 or UT10_61 or ST10_37"` gives 119 passed, with 100 % line and branch coverage of `settings.py`. The unit+integration run gives 222 passed.
- `settings.py` is now 366 lines, within the 370 limit set in the review.
