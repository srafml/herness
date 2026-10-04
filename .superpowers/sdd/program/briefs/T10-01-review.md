# T10-01 review: section models for herness.yaml (commit 955cbd0)

**Verdict: Approved.** There are no Critical or Important findings. The Minor items below can be fixed now or carried into later cards. One carry-over must reach the T10-03 brief (see M1).

### Spec Compliance
- ✅ U10-02 PathsConfig: the fields and defaults match. Only the Path fields are lax. A before-validator rejects empty and NUL values, whether the value is a str or a Path.
- ✅ U10-03 DataPolicyConfig, SecretsConfig, EgressConfig, NetworkConfig and SecurityConfig:
  - Every field, default and range matches the spec.
  - `chat_approved` is present (R-38).
  - `model_download` is excluded from `purposes` by the Literal.
  - Host tuples are lower-cased and capped at 32 entries.
  - `approved_on` takes only a date or an ISO string. A datetime is rejected.
  - `http_proxy` uses the verbatim pattern.
- ✅ U10-04 RedactionConfig:
  - The signature and defaults are correct.
  - Pattern checks run in the spec order: length over 500, then the nested-quantifier regex, then compile. The nested-quantifier regex is byte-identical to the spec text.
  - Limits: 64 patterns per list, 64 custom patterns, and custom keys match the spec pattern.
  - `extra_names` entries are 2 to 128 characters.
  - `denylist_domains` uses the C04 regex plus the not-an-IP check. It is the same text as C04 at spec line 479.
  - `key` uses the local copy of the U10-27 secret-reference pattern, as required by R-03 and R-72.
- ✅ U10-05 UiConfig, ExposeConfig and RolesConfig:
  - IP literals are checked with `ipaddress`.
  - The header pattern, port range, and the username length and 500-entry cap match the spec.
  - Usernames are lower-cased and de-duplicated in order.
  - Cross-field rules C05 and C21 are correctly left to U10-20.
- ✅ U10-06 LoggingConfig, RetentionConfig and BackupConfig: every default and range matches.
- ✅ U10-07 DeployConfig and its sub-models:
  - Every field, default and range matches.
  - Load time: the argv-safe rule or a whole-value placeholder; the dedicated patterns for `wsl_distro`, `repo`, `signer_workflow` and `duckdb_extensions`; absolute POSIX paths with no `..` and nothing under `/mnt/`; ports 1024 to 65535 and pairwise distinct.
  - Deploy time: the pin regexes are verbatim and the message is exactly `deploy.<key> is not pinned`.
- ✅ Module map: L0, and it imports only stdlib, pydantic and `herness.core.errors` (R-03). All 19 public classes are present. 365 lines is within the raised budget of 370 (ruling a) and under the ENG limit of 400.
- ✅ Tests:
  - UT10-19 (model cases) and UT10-61 are present.
  - ST10-37 is present. It is deferred as ValidationError; see M1.
  - Names and docstrings follow the ID convention, and `pytestmark` is set.
  - The acceptance selector `-k "UT10_19 or UT10_61 or ST10_37"` (ruling b) is reported as 115 passed.
  - Coverage of `settings.py` is reported at 100 % line and 100 % branch.
- ✅ pyproject contracts: C4 "core base is closed" and C6 "settings modules are leaves" match impl 00 §3.6 (lines 1229 and 1231) for the modules that exist today.
  - The tree holds only `herness.core.*`, so C4 correctly forbids only `herness.core.settings`.
  - C6 correctly forbids every existing core module except `errors` and `types`, with `allow_indirect_imports = true`.
  - The report shows `lint-imports`: 6 kept.

- ⚠️ Cannot verify from the diff:
  - ST10-37's expected `ConfigError` depends on U10-09 converting ValidationError to ConfigError, and that belongs to T10-03. The T10-03 card's test list (spec line 2676) does not include ST10-37, so nothing would ever check the end-to-end ConfigError. See M1.
  - The list→tuple base validator is needed because HernessConfig (U10-09 step 4) passes YAML lists to strict tuple fields. The spec defines no shared helper. Every later settings card (impl 01–09) will hit the same pydantic behaviour and needs a convention: copy the validator, or a ruling on where it lives. Settings modules may only import `herness.core.types` and `errors`, so a shared helper cannot live in another settings module.
  - A spec-level issue, not a builder defect. The U10-07 load-time pattern allows a leading `-`, and `service.account` and `licence_exceptions` get no deploy-time pin rule. A probe confirmed that `ServiceDeploy(account="--exec")` is accepted. TH10-05 therefore relies on the argv runner (U10-77) for these values. Worth a note for T10-23 and the U10-77 cards.
  - `id_patterns` replaces the whole default dictionary. A YAML block that sets only `USER_ID` drops the default `EMPLOYEE_ID` entry. That is ordinary pydantic behaviour and the spec says nothing about merging. Consumers must not index `["EMPLOYEE_ID"]`.

### Assessment of the builder's concerns
1. **ValidationError vs ConfigError.** Acceptable for this card, because `load_config` does not exist yet. The controller must add an ST10-37 end-to-end ConfigError assertion to the T10-03 brief. As M1 explains, the spec's card table does not do this.
2. **List→tuple base validator.** Correct and necessary. Items are still validated strictly afterwards. Recursing into dict values is harmless. See the cross-card ⚠️ above.
3. **Lax `RedactionConfig.directory_file`.** Justified. U10-04 declares a `Path` field that is read from YAML, so under strict mode it could never be set. The spec's "one strict exception" wording is inconsistent, and the fix follows the U10-02 precedent. The spec should be amended to say "Path fields are lax".
4. **`DeployConfig.unpinned_keys` and `require_pinned`.** Good design. The pin rules are owned by U10-07. They are consumed by U10-79 ("U10-07 deploy-time pin rules hold, else ConfigError"), by U10-82 and U10-83, and by C13 as a per-class warning. Keeping them in one L0 place avoids duplicated regexes. The `classes` filter serves both C13 and subset checks. The later briefs for T10-23 and the C13 card must name these helpers so they are not reimplemented. The one exception is the `duckdb_extensions.excel` pin at spec line 1616, which stays with U10-112.
5. **Two new import-linter contracts.** Required by the impl 00 §3.6 rule "a card that creates a listed module adds it" (UT00-58). The contents are correct, as shown above.

### Strengths
- The regexes are copied verbatim from the spec, and the pin rules use `fullmatch` in Python `re`, so there is no trailing-newline hole.
- Deploy-time error messages carry only the key, never the offending value.
- `_check_pattern` enforces the length cap before running the nested-quantifier scan, which bounds the scan's own backtracking. Compile errors report `exc.msg` without the pattern text.
- The tests assert the exact `(loc, type)` of each single error, not merely that an error occurred. They include injection probes (`;`, `$(...)`, backtick, `|`, quote, newline, CRLF header injection) and the three UT10-61 shapes: placeholder, tag instead of digest, and 39-hex revision.
- The `_Section` base keeps all three config flags and the list→tuple conversion in one place.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- **M1: ST10-37 is not satisfied end to end.** `tests/security/test_st10_config.py:457` asserts pydantic `ValidationError`, but the spec row (spec line 2607) expects `ConfigError`. T10-01 is the only card that lists ST10-37, so this is a carry-over the controller must add to the T10-03 brief: a `load_config` run with `custom_patterns: {bad: "(a+)+$"}` raises ConfigError with the key path. It is not blocking here.
- **M2: `\d` in the verbatim patterns matches non-ASCII digits.** These patterns go through pydantic's Rust regex engine, where `\d` is Unicode-aware. Affected: `nightly_at` (`herness/core/settings.py:273`) and the `http_proxy` port (`herness/core/settings.py:196`). A probe confirmed that `nightly_at="0\u0661:30"` and `http_proxy="http://p:\u0668\u0660"` are both accepted. The impact is low: T08-14 or the HTTP client would fail later or misread the value. Fix with `[0-9]`, and file a spec erratum noting that the spec means ASCII digits.
- **M3: `model_root` or `env_file` of `"//"` passes.** In `herness/core/settings.py:108-110` the regex matches and the filtered parts list is empty. Add `bool(parts)` to the check. A probe confirmed that `model_root="//"` is accepted.
- **M4: the test module is over the module-length limit.** `tests/unit/core/test_config_settings.py` has 467 lines. ENG §2.4 sets "Module length 400 lines" with no exemption for tests, and every other test module in the tree is under 400. Split it by section, for example into `test_config_settings_deploy.py` for the deploy and UT10-61 cases at lines 518-526 and 790-936.
- **M5: `unpinned_keys` stringifies field values.** `herness/core/settings.py:362` calls `str(getattr(section, field))`, which hides a type change if a pinned field ever becomes non-str. All pinned fields are `str` today, so `getattr` alone suffices. Cosmetic.
- **M6: the timing assertion may flake on a loaded CI runner.** `tests/security/test_st10_config.py:459` asserts `< 1.0` s for the whole `model_validate` call, which includes the first-use regex compilation. The margin is large, so the risk is low. Keep it or move it to BT.

### Assessment
**Task quality:** Approved
**Reasoning:** Every U10-02 to U10-07 field, default, range and verbatim pattern is implemented. The code stays inside the R-03 import rules and the import-linter contracts, and the tests assert exact error locations and types. What remains is edge-case polish (ASCII digits, the `//` path, test file size) and a carry-over: T10-03 must add the ST10-37 end-to-end ConfigError assertion.

## Re-review round 1 (commit 0a8fcba; diff 955cbd0..HEAD)

This round checks only the findings the builder claims to have fixed, plus any regression the fix introduces. As focused checks for this round I ran the two settings test files and the security tests (119 passed in 0.22 s) and `uv run mypy` (Success, 13 files).

- **M2 ✅ Fixed.**
  - `nightly_at` (`herness/core/settings.py:273`) and the `http_proxy` port (`:196`) now use `[0-9]`. `_ISO_DATE` (`:35`) does too.
  - New test cases: `nightly_at="0\u0661:30"` and `http_proxy="http://p:\u0668\u0660"` are both rejected with `string_pattern_mismatch`.
  - There is no test for a date written in Arabic-Indic digits. That input was already rejected before this change, by `date_type` or `fromisoformat`, so nothing is lost.
- **M3 ✅ Fixed.**
  - `_is_posix_path` (`herness/core/settings.py:108-110`) now requires at least one non-empty segment. `parts[0]` is only read after the `bool(parts)` check, so it cannot raise IndexError.
  - New test cases reject `"//"` and `"/"`. The `/mnt/`, `..` and relative-path rejections still pass.
- **M4 ✅ Fixed.**
  - The deploy and UT10-61 tests moved to `tests/unit/core/test_config_settings_deploy.py` (206 lines). `test_config_settings.py` is now 297 lines. Both are under 400.
  - Test names, docstrings and `pytestmark` are kept.
  - Nit: `PLACEHOLDER_DEPLOY` and `_errors` are now defined in both files (`test_config_settings.py:12-21` and `test_config_settings_deploy.py:15-47`). This duplication is acceptable in tests. A shared `tests/support` fixture is optional.
- **M5 ✅ Fixed.** `unpinned_keys` now passes the field value to `fullmatch` directly, without `str()`. mypy stays clean.
- **M6 ✅ Fixed.** The bound is now `< 10.0` s, with a comment explaining the choice (`tests/security/test_st10_config.py:115-116`). Catastrophic backtracking would still exceed it.
- **M1 (not in scope for this round):** still an open carry-over. The controller must add an ST10-37 end-to-end `ConfigError` assertion to the T10-03 brief.

**Regressions:** none found. `settings.py` is 366 lines, within the 370 budget. The focused test run and mypy both pass.

**Task quality:** Approved
