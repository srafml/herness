# T10-06 Secrets core: review

Reviewed head 8ec7a74 (base b435704), worktree agent-a4a672398dcd46850. Read-only review.

### Spec Compliance
- ✅ Spec compliant.
  - U10-27 ✅ `SECRET_NAME` is the verbatim pattern. `SecretRef.parse` strips `secret:`, fullmatches the name, stores it lower-cased, and raises `ConfigError("invalid secret name")` without echoing the value. `SecretRefStr` and `SecretNameStr` are AfterValidator checks that raise ValueError, which pydantic turns into a validation error (secrets.py:49-81).
  - U10-28 ✅ Order is parse, select backend, get. A missing name gives `secret not found: <name>` with the exact prefix. Only then is the value added to `_KNOWN` under `_KNOWN_LOCK`, and a `SecretStr` is returned with no caching (secrets.py:215-223). The backend is keyring while no config is cached, and no config load is triggered (secrets.py:196-201; covered by a test).
  - U10-29 ✅ `resolve_json` gives one message for all three failure cases, raises `from None`, adds every member value to `_KNOWN`, and returns `SecretStr` values (secrets.py:226-238).
  - U10-30 ✅ `exists` discards the value and never remembers it (secrets.py:241-243).
  - U10-31 ✅ Order is: validate the name and value (8-16,384 chars, no CR, LF or NUL), refuse dotenv, audit (`secret_set` / `secret_rotate`, name only), then write to the backend. An audit FatalError stops the action. Deleting a missing name is a no-op (secrets.py:253-270).
  - U10-33 ✅ Keyring: service `herness`, lower-cased username. Values of 1,200 chars or fewer are stored directly. Longer values go into 1,200-char chunks `<name>#i` with the header `chunked:v1:<n>`. `get` reassembles the chunks and `delete` removes all of them. A shorter overwrite drops stale chunks (`_drop(user, len(parts), old)`). `KeyringError` or `RuntimeError` becomes `ConfigError` plus the `secrets.backend.unavailable` log event. Dotenv: refused unless `HERNESS_ENV=dev` or profile `synth`, and the check runs on every selection. It is read-only, and names map as `HERNESS_SECRET__` + upper-cased name with `.`/`-` turned into `_`, parsed with the U10-18 grammar (secrets.py:107-192).
  - U10-34 ✅ Walks `effective_dict(cfg)`, whose `mask_secrets` keeps `secret:` strings. Skips mappings with `enabled: False` (`is False`). Adds `redact.hmac_key` and `ui_user_ref_key`, then lower-cases, de-duplicates and sorts (secrets.py:273-288). Spot check on `write_full_config`: returns anthropic.api_key, openjev_api_key, redact.hmac_key, ui_user_ref_key, vllm.api_key. The disabled `jev` decider's TYPESAFE_API_KEY is skipped as expected.
- ⚠️ Cannot verify from diff:
  - Real Windows Credential Manager behaviour (the 2,560-byte blob limit, and the `pywintypes.error` paths of WinVaultKeyring). Only the in-memory fake is exercised.
  - C12 config-time refusal of dotenv (TH10-11's other half) belongs to another card. ST10-26 here proves only the U10-33 runtime refusal. This is correct for this card.

Controller rulings respected: `_KNOWN`, `_KNOWN_LOCK` and `known_values()` only, no scrub_secrets; the static function-local import in audit._known_values; U10-34 tests under UT10-28; UT10-33 covers only U10-31; fake_keyring lives in tests/support; exact "secret not found: " prefix; budgets met (secrets.py 288/360, audit.py 390/395, errors.py untouched).

TH10-07 (no secret value in any error, hint or log field): verified. Every ConfigError message holds only a name or fixed text. The log event carries only `backend` and `error_type`. Keyring and JSON errors are raised `from None`. Tests assert sentinel values are absent from messages, the log kwargs, `__cause__` and `known_values` (FT10-05, UT10-29, UT10-33).

### Strengths
- Tight, readable module. Chunking handles every edge case the brief listed: shrink across the chunk boundary, chunked to direct, a direct value that looks like a header, a lost chunk, and deleting a missing name.
- The dotenv precondition is re-checked on every backend selection, so re-running `init_config` with a different profile cannot keep a dotenv backend alive.
- Refusing dotenv writes before the audit line means no audit line records an action that cannot happen.
- Strong tests: exact-message regexes, sentinel-absence checks, audit-before-write proven by monkeypatching audit to raise, and ST10-26 checks all five entry points plus "no audit file, keyring untouched".
- .secrets.baseline: no entries dropped against b435704, all 20 audited `is_secret` entries preserved, 4 new unaudited sentinel entries, 0 CR bytes.
- Test IDs and markers follow the tree's conventions: ST10-26 in tests/security with marker unit like the other st10 files; FT10-05 with marker fault. The import-linter change (secrets added to the "core base is closed" list) is correct: 13 contracts kept.

### Evidence (re-run by reviewer)
- `pytest test_secrets.py test_st10_secrets.py fault/test_security_faults.py test_audit.py test_st10_audit.py`: 44 passed. Coverage: secrets.py 100% line and 100% branch; audit.py 99% (3 partial branches not in the changed code).
- tests/unit/repo: 11 passed.
- Checks run by the reviewer:
  - `ruff check` on the changed files: clean.
  - `ruff format --check herness tests`: clean.
  - mypy (configured scope herness and tools, strict): 0 issues in 119 files.
  - lint-imports: 13 kept, 0 broken.
  - tools.check_module_size: exit 0.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/core/secrets.py:185 finds `.env` at `Path.cwd()`, but `FilteredDotEnvSource` (config_sources.py:309) and `_absolute_paths` (config.py:190) use `config_dir.resolve().parent`. The config's `.env` layer and the secrets `.env` can therefore be different files:
   - when `--config-dir` points elsewhere;
   - in a synth run with a temp config tree, where the dotenv backend would read the developer's real `<cwd>/.env`.

   Impact is limited to dev and synth. Suggested fix: record the resolved config root at load (for example on `_Cache`, or derive it in `load_context`) and use it here, so doctor's `acl_data` `<repo root>\.env` check, the config layer and the secret backend all agree.
2. herness/core/secrets.py:139-145 overwrites chunks in place with the old header still live. If keyring fails after chunk i of a chunked-over-chunked write:
   - `get` returns a silent mix of new and old chunks, a corrupted secret with no error, and the audit line already says `secret_set`;
   - if `_drop` fails after the header is replaced, chunks of the previous (rotated) secret stay in Credential Manager and are never cleaned up later, because the next `old` count comes from the new header.

   Cheap mitigation: write the header as a direct tombstone (or delete it) before writing chunks, or use generation-suffixed chunk names.
3. herness/core/secrets.py:57-58: `SecretRef.parse` returns any `SecretRef` instance unchanged, but `SecretRef("Bad Name")` can be built directly without validation. That breaks the U10-27 postcondition "name is lower-case" for `resolve`, `exists`, `set_secret` and `delete_secret`. Validate in `__new__`, or re-validate in `parse`.
4. herness/core/secrets.py:103-110: only `KeyringError` and `RuntimeError` are mapped (as the spec says), but WinVaultKeyring re-raises non-not-found `pywintypes.error` (for example access denied) unwrapped. It would escape as a raw exception with no hint and no `secrets.backend.unavailable` event. Consider a broad `except Exception` mapping for the backend call only, or note it for the Windows integration card.
5. herness/core/secrets.py:197 and :25 reach into private names of sibling modules (`_config._Cache.config`, `config_sources._DOTENV_LINE`, `_MAX_DOTENV_BYTES`, `_read_text`), and the `.env` loop at secrets.py:162-174 duplicates config_sources.py:313-322. This was an accepted builder decision because of the size budgets. Track it as debt: a public `read_dotenv(path)` and a `cached_config()` peek once budgets allow.
6. tests/unit/core/test_secrets.py:85-91: `test_ut10_28_referenced_names_from_config` asserts only that the two fixed names are present. It would still pass if `secret:` collection from a real `effective_dict` broke; only the monkeypatched walk test covers collection. Asserting that `vllm.api_key` and `anthropic.api_key` are present, and `typesafe_api_key` (disabled `jev`) is absent, would pin the real path.
7. tests/support/fake_keyring.py:19,22: under strict mypy the file reports an unused `type: ignore` and a `no-untyped-call` on `super().__init__()`. Tests are outside the configured mypy scope, so no gate fails; this is cosmetic only.

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit matches its spec, including the audit-before-write order, the dotenv refusal and read-only behaviour, chunking with stale-chunk cleanup, and known-values locking. No secret value can reach an error, hint or log field. Coverage is 100% and all gates and the baseline are clean. The remaining items are hardening and tidy-up.


## Re-review r1

Scope: commit 43ded39 on top of 8ec7a74. I checked fixes m1, m3, m4, m6 and m7, looked for regressions, and reviewed the new `config._Cache.root`. m2 and m5 are parked.

### Fix status
- m1 ✅ `.env` is now found where the config loader finds it. `secrets._dotenv_root()` (secrets.py:177-182) tries these in order:
  1. `_Cache.root`, which is `config_dir.resolve().parent`, the same base as `FilteredDotEnvSource` and `_absolute_paths`;
  2. the active load context's `config_dir.parent`;
  3. the working directory, as a last resort.

  `test_ut10_31_dotenv_root_follows_config_dir` proves that a synth run on a temp tree no longer reads the developer's `.env` in the working directory.
- m3 ✅ `SecretRef.parse` always re-validates and lower-cases, including when given a `SecretRef` (secrets.py:63-68). It is tested through `parse`, `resolve` and `exists`.
- m4 ✅ `_keyring_call` maps any `Exception` to `ConfigError` with the hint, logs the event, and raises `from None`; `PasswordDeleteError` is still a no-op (secrets.py:107-117). This is a superset of the spec's KeyringError/RuntimeError, so it is acceptable. FT10-05 adds a stand-in for a raw `pywintypes.error`: 6 events, and the sentinel appears in no message, hint, cause or log.
- m6 ✅ The real-config test asserts that `vllm.api_key` and `anthropic.api_key` are present and `typesafe_api_key` is absent (test_secrets.py:88-96).
- m7 ✅ `mypy --strict tests/support/fake_keyring.py`: 0 issues. Skipping `super().__init__()` is safe, because the base constructor only reads `KEYRING_PROPERTY_*` from the environment.

### config._Cache.root (outside the card's file)
- **Thread safety:** `root` is written together with `config` under `_CACHE_LOCK`, in `init_config` (config.py:254), `get_config` (config.py:260) and `reset_config` (config.py:267). Readers take no lock (`_backend` at secrets.py:208-210 and `_dotenv_root`). Each read is an atomic attribute read, but the two reads are not one snapshot. A concurrent `reset_config` or `init_config` between them could pair one config with another config's root. At worst that means the working-directory fallback or the other tree's `.env`, in dev or synth only. Acceptable; see r1-m2.
- **Reset:** cleared under the lock, and the reset hook clears the `_dotenv_values` cache. A config replaced by a second `init_config` gets a new path key, so the file is read fresh. OK.
- **Precedence:** `_Cache.root` beats the load context. Through the public API this makes no difference, because `_backend()` builds a dotenv backend only when a config is cached, and `root` is always set with it. If a future owner validator or cross-check calls `exists()` inside a `load_config` for a different directory, the stale cached root would win over the more specific active context. See r1-m1.
- **get_config default:** `Path("config").resolve().parent` matches `load_config`'s default `config_dir`. OK.
- config.py is 298/320 lines. Its own unit tests give 100% line and branch coverage.

### Evidence (re-run by reviewer)
- Card files: `test_secrets.py`, `test_st10_secrets.py`, `fault/test_security_faults.py`, `test_audit.py` and `test_st10_audit.py`: 47 passed. `secrets.py` has 100% line and branch coverage.
- `tests/unit/core -k "config or audit or secrets"` plus `tests/security`: 310 passed, 1 skipped (a symlink privilege skip that already existed).
- `tests/unit/core -k config`: 238 passed, with config.py at 100%.
- Checks run by the reviewer:
  - `check_module_size`: exit 0.
  - `ruff check`: clean.
  - mypy (configured scope, strict): 0 issues in 119 files.
  - lint-imports: 13 kept, 0 broken.
- `.secrets.baseline`: nothing dropped against b435704, all 20 audited `is_secret` entries identical, 5 new unaudited test sentinels, 0 CR bytes.

### New findings (all Minor)
1. r1-m1 secrets.py:179-181: precedence is cached root first, then the active load context. When a load context is active, it is the more specific source and should win: `ctx.config_dir.resolve().parent if ctx else _Cache.root`. This cannot happen through today's API, so it is a latent issue only.
2. r1-m2 secrets.py:208-210 with :180: `_backend()` reads `_Cache.config` and `_dotenv_root()` reads `_Cache.root` separately, with no snapshot. Reading both in one place, or passing the root from `_backend()`, would remove the race. It affects dev and synth only.
3. r1-m3 secrets.py:63: `str(value)` makes a non-str argument parse. For example, `resolve(None)` looks up the secret `none` instead of failing. mypy catches this at call sites, but a check like `if not isinstance(value, str): raise ConfigError("invalid secret name")` would keep the runtime contract.

### Verdict r1
**Task quality:** Approved
**Reasoning:** m1, m3, m4, m6 and m7 are fixed and tested, with no regressions. The `_Cache.root` addition is lock-consistent and reset correctly. The three new items are latent or dev-only edge cases.
