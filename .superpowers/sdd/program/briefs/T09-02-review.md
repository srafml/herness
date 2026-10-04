# T09-02 review — Roles, validators and user messages (head a328e9c, base 3e0ee9a)

### Spec Compliance
- ✅ U09-29 — UiRole (published name per the OWN040 ruling), ROLE_RANK 0..3, all 15 ACTION_ROLES rows with ACTION_TEXT phrases verbatim, immutable MappingProxyType built from one table, `_check_tables` raises ConfigError on mismatch (no assert). Actor is frozen, and `display` is repr=False.
- ✅ U09-30 — role_for: None or blank -> denied; strip().lower() on both sides; admins before reviewers; otherwise default_role.
- ✅ U09-31 — user_ref_for = HMAC-SHA256(key, strip().lower() UTF-8).hexdigest()[:32]. I recomputed the test vector independently (5c737e39...5a16) and it matches. A blank username raises UserInputError. load_user_ref_key uses functools.cache and is cleared through config._RESET_HOOKS, the same pattern as secrets.py. A missing secret raises ConfigError "secret not found: ui_user_ref_key" with hint "Run `herness secrets init`."
- ✅ U09-32 — require_role: unknown action -> ConfigError("unknown action <a>"); allowed -> return; refused -> audit("auth", user_ref, user_ref=, role=, result="denied"), with no `action` kwarg per the ruling and a comment at rules.py:154. It then logs app.auth.denied at WARNING with user_ref/role/action/channel and raises PermissionDenied with the exact message and hint. If audit fails, app.auth.audit_failed is logged at ERROR and the refusal still stands. No metric is written (per ruling).
- ✅ U09-33 — UserInputError(RecoverableError); control-char regex verbatim; bounds 10–1000, ≤max_chars, 1–max_chars, 1–1000, 1–200; the spec-fixed messages are exact; the six id regexes are prefix + `[0-9A-HJKMNP-TV-Z]{26}$` used with fullmatch; check_id gives "invalid <kind> id" without echoing the value.
- ✅ U09-88 — user_message: every what/fix row matches the table. NoCurrentBuild is matched by class name in the MRO (per ruling). MemoryNotFound is a NotFound subclass, so it gets the list fix. PermissionDenied gives message + hint through the generic HernessError row. Non-Herness exceptions give "Unexpected error." plus the log fix.
- ✅ Tests — UT09-50, UT09-51, UT09-52, UT09-65, UT09-72 and PT09-06 are all present with real asserts. UT09-52 asserts exactly one `auth` audit line by tuple unpack of the real audit file (test_rules.py:170), with the exact fields, and checks that the display name is absent from the log and audit output.
- ⚠️ Cannot verify from diff: ST09-07/ST09-09 (later cards). The rulings (no `action` in the audit line; anonymous actor leaves no audit line; no denied metric) are carry-overs for impl 10 and the metrics card.

Gates I re-ran in the worktree: ruff check and format clean; mypy (configured scope) clean; lint-imports 13 kept; check_type_ownership 0; check_module_size 0 (rules.py 297/300); the card tests pass (55 passed); rules.py coverage is 100 % line and 100 % branch. Layering: rules.py imports only herness.core.* (L5 -> L0 is fine), with no app/ import.

### Strengths
- One `_ACTIONS` table feeds both mappings, so the keys cannot drift.
- Logs carry only user_ref, role, action and channel. Actor.display stays out of repr, and the tests assert it never reaches the log or audit output (TH09-21).
- The audit-failure path is tested with the real SchemaViolation, not a mock.
- details lookups use .get throughout, so missing details still give non-empty what/fix; there is a fallback test for this.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/reports/rules.py:132-137 — load_user_ref_key relabels every ConfigError from resolve() as code secret_missing and replaces the hint with "Run `herness secrets init`". This includes "secret backend unavailable: keyring" (core/secrets.py:117, which has its own _HINT) and a malformed .env line. user_message then shows "The user reference key is missing." for a broken backend, and the original hint is lost. Suggest relabelling only when the message is "secret not found: ui_user_ref_key" and re-raising other errors unchanged.
2. herness/reports/rules.py:71 — the job_inline phrase is spec-verbatim, but it puts "(R-45)", an internal ruling ID, into a user-facing refusal ("…with `--inline` (R-45)."). This is plan-mandated. Suggest a spec erratum to drop the citation from the phrase.
3. tests/unit/reports/test_rules.py:256 — MemoryNotFound("memory", "mem_1") passes a kind that is not in its Literal; mypy on the test file reports arg-type. tests/ is outside the configured mypy `files`, so this is not a gate failure. Suggest using "memory_item".
4. herness/reports/rules.py:164 / :125 — these are extras beyond the brief, already disclosed: PermissionDenied details {code, action, role}, and ConfigError secret_missing for an empty key. Both are harmless. For the second, an empty key is not really a "missing" secret; see item 1.

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit matches the brief's exact strings, bounds, regexes and HMAC derivation, and the controller rulings are applied as recorded. The tests are meaningful: exactly one auth audit line, 100 % branch coverage. What remains is minor error-path polish.
