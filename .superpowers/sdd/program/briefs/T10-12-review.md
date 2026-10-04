# T10-12 review (verify agent) — commit 03a42d2 on aca2a68

### Spec Compliance
- ✅ Spec compliant (with Minor items below)

Per unit:
- ✅ U10-20 CROSS_CHECKS / run_cross_checks: 22 rows in table order; modes offline/online/registry; C03 only with include_registry; online rows (C06, C08b, C23) skipped offline; rows read effective_dict key paths only (no owner model attrs, R-04); compose parsed with yaml.safe_load; compose_path default <config_dir parent>/docker/compose.yaml.
  - C01 roles + fallback entries ✅; C02 off-network role needs egress.enabled and <profile>_approved ✅; C03 four kinds ✅ (decider without `enabled` treated as on — ⚠️ spec note); C04 regex verbatim + IP-literal rejection over destinations, extra_allowed_hosts, every sources.<name>.hosts ✅; C05 loopback bind, expose requires trusted_proxy, proxy loopback (localhost / is_loopback IP) ✅; C06 ✅; C07 ✅; C08a profiles == [class] and URL port vs deploy port (vllm-reasoning/openjev/llamacpp-large), missing file = warn per ruling ✅; C08b argv list, no shell, 20 s, warn off Windows/without wsl.exe ✅; C09 openai_compat local reasoning/large ports ✅; C10 context_window <= max_model_len / large.ctx ✅; C11 hybrid {reasoning_final} + reasoning only with chat_approved, premium 3 purposes ✅; C12 ✅; C13 severity warn ✅; C14 ✅; C16 secret-key/_secret sweep with SECRET_NAME + "write secret:<name>" hint, and CREDENTIAL sweep via U10-36 detectors ✅; C17 ✅; C20 SDK_SOURCE_KINDS + MSAL dataverse hosts, snowflake account host, login.microsoftonline.com ✅; C21 warn ✅; C23 warn online ✅; C24 ✅; C25 ✅.
- ✅ U10-109: Protocol; name regex; _VAL_LOCK on register/reset/snapshot; duplicate with other object -> ConfigError("duplicate owner validator <name>"), same object no-op; name order; islice 500; mapping -> message[:300]; invalid object -> "validator <name> returned an invalid issue" at <name>; any exception -> "validator <name> failed: <Class>" (message dropped); sort error-first then path; reset_config keeps registrations.
- ✅ F10-01 step 3a: run_startup_validators logs config.validate.issue and raises ConfigError("owner validation failed", issues=...). CLI wiring -> T09-20 (ruling).
- ✅ U10-09 step 8: enforce_offline_checks inside the load context (C12 sees the load env, C08a the config dir); error -> ConfigError with issues; issues logged. Enforcing C12 at load matches step 8 (U10-20 offline includes C12).
- ✅ U10-13 validate: steps 1-4 as specified; ConfigError without structured issues becomes one error issue at `config`.
- ✅ UT10-19 (param case per offline row + C03/C06/C08b/C23 + model validators patterns/ports/pins + owner rows + table shape + step 8), ✅ UT10-75 (fixture compose, port alteration -> C08a), ✅ UT10-81, ✅ ST10-04, ✅ ST10-27. IDs in names, docstrings lead with ID, pytestmark set.
- ✅ Adapted UT10-31 / ST10-26 keep intent (ST10-26 now asserts the C12 load refusal and still the U10-33 runtime refusal).
- ✅ TH10-06: messages/paths hold key paths, names and rule text only; step-8 error text is str(ConfigIssue) (no values); owner exception text dropped (tested).
- ✅ Layering: config_validate/config_checks import only herness.core; added to the "core base is closed" contract; lint-imports 13 kept.

⚠️ Cannot verify from diff / notes:
- C16 sweeps effective_dict, not the "merged raw dict"; since SourcesConfig forbids unknown keys, ST10-04 injects plain values via a patched `_tree` (tests/security/test_st10_cross_checks.py:170-181); the real-file variant surfaces pydantic errors, not C16.
- C06 checks existence through the cached config's backend (secrets._backend, herness/core/secrets.py:266), not the backend of the cfg under validation; validating a dotenv (dev/synth) tree with no cached config checks the keyring.
- Acceptance "offline validate < 1 s": measured ~0.1 s per report; the test asserts < 3.0 s (tests/unit/core/test_config_validate.py:488).

Gates re-run: ruff check / format clean; mypy clean on the 3 modules; lint-imports 13 kept 0 broken; check_module_size exit 0 (config_validate 333/390, config.py 315/320, config_checks 265, budget 280 per ruling). Coverage (card + adapted tests, 115 passed): config.py 100 %, config_checks.py 99 %, config_validate.py 99 % (line and branch).

### Strengths
- Clean split: pure rows in config_checks, side-effect rows + runner + owner hook in config_validate; rows yield (path, rule) only, so TH10-06 is structural.
- Owner hook is robust (lock, islice cap, per-item conversion, exception class only) and well tested incl. leak assertions.

### Issues
#### Critical (Must Fix)
- none
#### Important (Should Fix)
- none
#### Minor (Nice to Have)
1. herness/core/config_validate.py:95-98 — when the compose file is missing, C08a returns before the resilience-URL-port vs deploy-port half, which does not need the compose file; until T10-23 ships docker/compose.yaml that port rule never runs. Run the port loop regardless of the compose read result.
2. herness/core/config_validate.py:104 — `services[name].get(...)` raises AttributeError if a compose service value is a non-mapping (e.g. list/string); U10-20 "none raised" and, at load, it escapes as a non-ConfigError. Guard with isinstance(Mapping).
3. herness/core/config_validate.py:66-69 — C03 catches only ConfigError; registry.get's getattr (herness/core/registry.py:114) or a non-ImportError raised at module import escapes validate (U10-13 postcondition "never raises").
4. herness/core/config_checks.py:210-215 — C17 "any depth" walks nested mappings only; base_url inside a list of mappings is not checked.
5. herness/core/config_validate.py:224 — uses private `view._file_of`; herness/core/config_validate.py:191 — `# type: ignore[arg-type]` on the table builder (typing the row tuple removes it).
6. herness/core/config_checks.py:24-27 — helper exports 21 public names (row_c01..., get, items, port, loopback); plus extra public names in config_validate (CrossCheckRow, enforce_offline_checks, run_startup_validators, sort_issues) beyond the §2 row — record them in the fix-round §2 update with the config_checks row.

### Assessment
**Task quality:** Approved
**Reasoning:** Every C-row, U10-109 conversion rule, U10-13 step and U10-09 step 8 match the spec; all five test rows are covered with value-leak assertions, gates pass and coverage is 99-100 %. Remaining items are minor robustness/polish.

## Re-review fix round 1 — commit 94d3f5c on c138c0d

Per finding:
1. ✅ §2: new row `herness/core/config_checks.py` (L0, none, budget 280, 24 public names); config_validate.py row adds `CrossCheckRow`, `enforce_offline_checks`, `sort_issues`, `run_startup_validators`. check_module_size resolves both (exit 0). .secrets.baseline change is line-number shifts only.
2. ✅ m1: config_validate.py `_c08a` no longer returns on a missing compose file; the port loop runs with `rule=None`. Test `test_ut10_75_missing_compose_still_checks_ports` (url error + one warn).
3. ✅ m2: non-mapping root / `services` → "compose file has no services mapping" (one error at gpu.classes, per-service rules skipped); non-mapping service → "compose service is not a mapping" via `_compose_rule`. Tests: `services-not-a-mapping`, `service-not-a-mapping`, `test_ut10_75_list_root_is_an_issue`. Note: an empty compose file / no `services` key is now one error instead of per-service "missing" errors — acceptable.
4. ✅ m3: `_c03` catches `Exception`, message ends `: <Class>`; exception text dropped. Test `test_ut10_19_c03_import_failure_is_an_issue` asserts the message text is absent.
5. ✅ m4: `_base_urls` recurses through list/tuple at any depth with `[i]` paths. New offline C17 case `mirrors[1].base_url`.
6. ✅ C06: `_backend_get` builds the backend named by the validated cfg (keyring → `_KeyringBackend()`, dotenv → `_DotenvBackend(cfg.profile, env=x.env, root=compose_path.parent.parent)`). Test `test_ut10_19_c06_uses_backend_of_validated_config` proves dotenv mode ignores a populated keyring and keyring mode is clean.
   - Coupling judgement: acceptable (Minor at most). It does not bypass the refusal: `_DotenvBackend.__init__` still enforces dev/synth (C12/U10-33), and a refusal makes every reference "missing" (no keyring fallback). C06 is an online row, so no keyring read offline. Values are discarded immediately (not `_remember`ed), matching `exists`. Same spec (§2 of impl 10), same layer.

No values in issue messages: C03 class name only; C06/C08a/C17 messages are fixed text + key paths.

### Minor (Nice to Have)
- herness/core/config_validate.py:76 — dotenv root derives from `compose_path.parent.parent`; correct for the default and for `validate` (config.py:241), but a caller passing a non-standard `compose_path` would read `.env` from the wrong dir. Consider passing the root in `CheckContext` later.
- herness/core/config_validate.py:83-84 — the dotenv-refused branch is untested (unreachable via `validate`, since load's C12 refuses first). Defensive; safe behaviour.
- herness/core/config_validate.py:72-77 — reaches into private `secrets._KeyringBackend/_DotenvBackend` (and pre-existing `view._file_of`); a public `secrets.exists_in(cfg, name, ...)` would be cleaner when secrets.py is next touched.

Gates (re-run in worktree): ruff check clean; ruff format --check 307 formatted; mypy 131 files clean; lint-imports 13 kept 0 broken; check_module_size exit 0; card tests `-k "UT10_19 or UT10_75 or UT10_81 or ST10_04 or ST10_27"` 160 passed; wider config/secret/ST10 selection 381 passed, 1 skipped (symlink privilege). Coverage (branch): config_validate 98 %, config_checks 99 %.

### Assessment
**Task quality:** Approved
**Reasoning:** All six findings are fixed as asked, each with a test; no regressions; TH10-06 holds; the private-backend coupling keeps the dotenv refusal and online-only keyring read, so it stays Minor.
