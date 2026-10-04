### Spec Compliance
- ✅ U10-65 `cmd_config_validate`: profile resolved per U10-09 step 1 (unknown profile becomes an `error profile -:` issue, not an exception); `issues = validate(...)`; `data = {profile, config_hash, issues}`; offline uses `key_id="unresolved"` and appends the warn line `warn config_hash -: config_hash computed without key_id; not comparable to builds`; exit 1 on error, 1 on warn with strict, else 0. Following ruling 3, the offline note does not count toward strict (commands_config.py:83-103).
- ✅ U10-66 `cmd_config_show`: `effective_dict(cfg, redact_secrets=True)`; a `ConfigError` at load propagates (ruling 6) (commands_config.py:106-109).
- ✅ U10-67 `cmd_config_hash`: `{"config_hash": config_hash(cfg)}`; a load `ConfigError` propagates (commands_config.py:112-115).
- ✅ U10-68 `cmd_secrets_init`: present -> `present`; otherwise `token_hex(32)`, shown once through `show` with the vault instruction, then `prompt("Type ESCROWED after storing <name> in the vault")`. Any other answer gives `not created` with nothing stored. Exit 0 only when both keys are present or created. The value never goes into `data` or `warnings` (commands_secrets.py:163-189).
- ✅ U10-69 `cmd_secrets_set`: the name is parsed first; a bad name raises ConfigError before any prompt. Prompts are `Value for <name>` and `Repeat`. A mismatch exits 1 with "values differ". A value starting with `{` must be a JSON object of strings. The value is limited to 16 KiB (UTF-8 bytes). Then `set_secret` runs (audit by name, then write). Backend ConfigError propagates (commands_secrets.py:195-223).
- ✅ U10-70 `cmd_secrets_status`: `{"secrets":[{name,present,last_set}]}` from `referenced_secret_names`, `exists` and `last_secret_set_times`. Any missing secret exits 1. A secret last set more than 90 days ago gets the warning `"<name>: last set N days ago; rotate per policy"`. Output carries names only (commands_secrets.py:232-253).
- ✅ Package marker `herness/admin/__init__.py`. `register_handlers` (T10-19) and `cmd_secrets_rekey` (T10-30) are markers only (ruling 4). Layering follows ruling 1 (pyproject.toml:272, 290, 308; test_import_contracts.py:33).
- ✅ UT10-20: 7 functions (9 cases) cover the clean, warning-only and error configs, with and without strict, and match the issue-line regex `^(error|warn) \S+ \S+: .+$` (test_admin_config.py:709-790).
- ✅ UT10-72: 6 functions cover init escrowed, `no`, present and backend error, plus status names/presence/last_set from audit, missing -> 1 and the 90-day boundary (test_admin_secrets.py:952-1081).
- ✅ IT10-11: 4 functions run `--offline --strict` on clean, warning-only and error configs (0/1/1) through the real root app `herness.cli.main`, with a test-local `guarded` wrapper (ruling 2). The JSON envelope is validated (test_admin_cli.py:428-477).
- ✅ ST10-36: a viewer runs `secrets set`, `privacy delete` and `deploy up` and gets exit 11 `PermissionDenied`. The keyring is unchanged, no prompt is shown, no probe runs, only 3 `auth denied` audit lines are written and there is no `admin_action`. There is also an admin control run (test_st10_admin.py:585-637).
- ✅ Extra IDs per ruling 5: ST10-16, UT10-15 and UT10-33 are present.
- ✅ RED evidence is credible. Every new test file imports `herness.admin`, which does not exist at 3e8c337 (`git ls-tree 3e8c337 herness/` has no `admin` entry). The report shows a `ModuleNotFoundError` per file; IT/ST RED was taken against a `git archive` snapshot of the base.
- ✅ §2 budgets: `__init__.py` 11/30, `commands_config.py` 78/180, `commands_secrets.py` 132/260 (`wc -l`). `uv run --frozen python -m tools.check_module_size` exited 0.
- ⚠️ Cannot verify from diff: the ruff, mypy and lint-imports results (15 kept / 0 broken) are not re-run. They are plausible from the diff: the test `type: ignore`s are scoped and the source code needs none. The whole-unit-suite pre-commit run is also not re-run. A focused re-run of the card tests gave 37 passed in 31 s, with no warnings.
- ⚠️ Parked (ruling 7, not a finding): `secrets init` with actor `unkeyed` would fail audit `_validate`.

### Strengths
- The bodies are thin and do not print. They return `CommandResult` with exit codes 0 or 1 only, and role and elevation checks are left to impl 09 as §3.7 requires. Rejection reasons are module constants and never interpolate a value (commands_secrets.py:151-153, 203-211).
- The escrow ordering is right: the value is generated and shown, then confirmed, then `set_secret` runs. Nothing is written on a non-`ESCROWED` answer (commands_secrets.py:167-175). Probe P3 confirms a test pins this.
- The tests assert concrete behaviour: exact `data` equality, the prompt question lists, keyring store equality, audit targets, value absence from the audit text and from data/warnings, and the exact 90-day boundary (90 d: no warning; +1 s: warning).
- ST10-36 is non-vacuous by inspection. A body or role bypass would turn exit 11 into 0, append to `ran` or `prompts`, or add an `admin_action` line, and each of these is asserted.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
- M1 Test gap for TH10-07 "never logged" (U10-68 security note). Probe P2 logs the generated key through structlog before `set_secret`, and all card tests stay green. `scrub_secrets` masks only known values, and a fresh key is not known yet. Fix: add a caplog or structlog capture assertion to `test_ut10_72_init_creates_escrowed_keys` (test_admin_secrets.py:952-978) for the code at commands_secrets.py:167-175.
- M2 ST10-16 does not exercise the masking that `cmd_config_show` requests. Probe P12 (`redact_secrets=False`) survives, because the test tree has no plain value under a secret-named key, only `secret:` refs, which are never resolved. This is probably an equivalent mutant for a valid `HernessConfig`, since secret fields are refs. To pin the call anyway, monkeypatch `effective_dict` and assert `redact_secrets=True` (test_admin_config.py:831-845; commands_config.py:109).
- M3 The offline `key_id="unresolved"` is not pinned. Probe P15 (always `key_id=None`) survives, because the fixture keyring has no `redact.hmac_key`, so `_key_id` falls back to `"unresolved"` anyway. Fix: store a 64-hex key and assert that the offline digest equals `config_hash(cfg, key_id="unresolved")` and differs from the online one (test_admin_config.py:709-790; commands_config.py:80).
- M4 The 16 KiB boundary is not pinned. Probe P7 (`>=`) survives, because no case stores a value of exactly 16384 bytes (test_admin_secrets.py:1102-1111; commands_secrets.py:207).
- M5 The U10-68 precondition "Backend `keyring`" is not checked before the value is generated. With `backend: dotenv` the operator is shown a key and escrows it, and only then does `set_secret` raise the read-only ConfigError. A fast `ConfigError` before `show` would avoid an escrowed value that was never stored (commands_secrets.py:178-189). The spec does not say who checks it, so this is Minor.
- M6 DRY: `_result` is defined twice, identically (commands_config.py:64-65, commands_secrets.py:156-157). It could be a single shared helper, or `CommandResult` could be built directly.
- M7 `_hash_or_none` silently maps a load `ConfigError` to `None`. This is safe only because `validate` has already reported the same load failure. If the second load fails alone (e.g. the file changes between the two loads), the result is exit 0 with `config_hash: null` and no issue. The double load is the C3 concern in the report (commands_config.py:74-80, 92-95).
- M8 When an unknown profile comes from `HERNESS_PROFILE` and no `--profile` is given, `data["profile"]` is `None` instead of the unknown name. The issue line still names it (commands_config.py:89-91).

### Mutation probes
- P1 Extra key added to the init `data` rows -> `uv run --frozen pytest tests/unit/admin/test_admin_secrets.py tests/security/test_st10_admin.py` -> 3 failed (caught).
- P2 Generated value logged via structlog in `_create` -> same command -> 20 passed (SURVIVED, M1).
- P3 `set_secret` moved before the ESCROWED check -> same command -> 1 failed (caught).
- P4 Value added to the "values differ" reason -> same command -> 1 failed (caught).
- P5 Mismatch check disabled -> same command -> 1 failed (caught).
- P6 JSON rule weakened to "any dict" -> same command -> 2 failed (caught).
- P7 16 KiB check `>` changed to `>=` -> same command -> 20 passed (SURVIVED, M4).
- P8 16 KiB measured in chars instead of UTF-8 bytes -> same command -> 1 failed (caught).
- P9 Rotation check `>` changed to `>=` 90 d -> same command -> 1 failed (caught).
- P10 A missing secret no longer exits 1 -> same command -> 1 failed (caught).
- P11 Resolved value added to the status rows -> same command -> 1 failed (caught).
- P12 `config show` with `redact_secrets=False` -> `uv run --frozen pytest tests/unit/admin/test_admin_config.py tests/integration/admin/test_admin_cli.py` -> 17 passed (SURVIVED, M2; likely an equivalent mutant).
- P13 `--strict` ignored -> same command -> 3 failed (caught).
- P14 Offline hash note counted toward strict -> same command -> 3 failed (caught, ruling 3 pinned).
- P15 Offline hash computed with `key_id=None` -> same command -> 17 passed (SURVIVED, M3).
- P16 Load `ConfigError` no longer swallowed in `_hash_or_none` -> same command -> 2 failed (caught).
- TH10-01: no admin-body mutation can reach it. The control is `herness._cli.identity.guarded`/`check_command_role` (impl 09), and the ST10-36 assertions are non-vacuous by inspection.
- After every probe `git checkout -- herness/admin` was run, and `git status --short` was empty.

### Assessment
**Task quality:** Approved
**Reasoning:** All six units match U10-65..U10-70 and the sub-controller rulings, all four card test IDs plus ST10-16, UT10-15 and UT10-33 have real tests with credible RED evidence, and 12 of 16 mutation probes are caught. The four survivors are test gaps (log leak, show masking, offline key id, 16 KiB boundary), not behaviour defects, so they are Minor.

### Re-review r1
Scope: bbfcb30 on top of 63dcfa3 (`git diff -U8 63dcfa3..bbfcb30`, 4 files, +131/-33).

Per finding:
- M1 ✅ Resolved. `test_ut10_72_init_creates_escrowed_keys` now runs `cmd_secrets_init` inside `structlog.testing.capture_logs()` and asserts that neither value appears in the captured events (test_admin_secrets.py:362-381). Re-running P2 (generated value logged in `_create`) gives 1 failed, so the probe is now caught.
- M2: parked by ruling (equivalent mutant); not re-reviewed.
- M3 ✅ Resolved. New `test_ut10_20_offline_hash_uses_unresolved_key_id` stores a real 64-hex key and pins three things: the offline hash equals `config_hash(cfg, key_id="unresolved")`, the online hash equals `config_hash(cfg)`, and the two differ (test_admin_config.py:278-291). Re-running P15 (`key_id=None` offline) gives 1 failed, so the probe is now caught.
- M4 ✅ Resolved. New `test_ut10_33_cmd_secrets_set_accepts_exactly_16_kib` covers an ASCII value and a two-byte value, each exactly 16384 UTF-8 bytes. Each is stored and resolves back to the same value (test_admin_secrets.py:409-419). Re-running P7 (`>=`) gives 2 failed, so the probe is now caught.
- M5 ✅ Resolved. `cmd_secrets_init` raises `ConfigError("secrets init needs the keyring backend")` before any value is generated, shown or prompted for (commands_secrets.py, start of `cmd_secrets_init`). The new test asserts that `show` and `prompt` are never called. Probe P17 (check disabled) gives 1 failed. The check uses the public `get_config()`, which is the same cached config `set_secret`'s audit already needs.
- M6 ✅ Resolved. Both `_result` helpers are removed and `CommandResult` is built directly. `ok` is still `code == 0` everywhere, and the 43 card tests pass.
- M7 ✅ Resolved. `_load_hash` returns `(digest, issue)`. A second load that fails on its own is now appended as `error config -: <message[:300]>` only when the validator found no error, so a first-load failure is not reported twice. The exit decision sees the new issue (commands_config.py, `_load_hash` and `cmd_config_validate`). The new test monkeypatches the second load. Probe P18 (append dropped) gives 1 failed.
- M8 ✅ Resolved. An unknown profile now reports the name that was tried: `--profile`, else `HERNESS_PROFILE`, else `local`. This mirrors `resolve_profile` (config_sources.py:189). New test `test_ut10_20_unknown_profile_from_environment_is_named`.

Regression checks:
- Budgets: `commands_config.py` 83/180, `commands_secrets.py` 138/260, `__init__.py` 11/30.
- `herness/core/secrets.py` has no change vs 3e8c337 (`git diff --quiet` passed).
- Focused card run: `uv run --frozen pytest tests/unit/admin tests/integration/admin tests/security/test_st10_admin.py` gives 43 passed, no warnings.
- The report shows RED for the three new behaviour tests (M5, M7, M8) on the old code, and probe-based RED for M1, M3 and M4. Both are credible and consistent with my re-runs.
- After every probe, `git checkout -- herness/admin` was run, and `git status --short` is empty at the end.

New findings:
- Minor N1 (commands_config.py:65): the comment says "The exit decision covers the validator issues only". After M7, the exit decision also covers the appended second-load error. Reword it to "the validator issues and a second-load failure; the offline note stays advisory".
- Minor N2 (commands_config.py:48 and 53): `tried` repeats `resolve_profile`'s fallback rule, and it puts the raw, untruncated `HERNESS_PROFILE` value into `data["profile"]`, while the issue line uses `_MAX_KEY_CHARS`. This is cosmetic: a profile name is not secret.

Mutation probes r1:
- P2 (value logged) gives 1 failed: caught.
- P7 (16 KiB `>=`) gives 2 failed: caught.
- P15 (offline `key_id=None`) gives 1 failed: caught.
- P17 (M5 precondition disabled) gives 1 failed: caught.
- P18 (M7 load error not appended) gives 1 failed: caught.

**Task quality (r1):** Approved
