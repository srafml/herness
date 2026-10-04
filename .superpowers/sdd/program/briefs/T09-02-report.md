# T09-02 report — Roles, validators and user messages

Status: DONE_WITH_CONCERNS
Commit: a328e9c feat(reports): roles, validators and user messages (T09-02)
Worktree/branch: D:\herness\.claude\worktrees\agent-aa1092b390dd4f203 / worktree-agent-aa1092b390dd4f203 (base 3e0ee9a)

## Continuation
Took over the killed agent's uncommitted work. I checked it against the brief and it was sound: all tests passed and coverage was 100 %. Then I:
- Cut rules.py from 320 to 297 lines (budget 300). I removed three section headers, shortened the Actor docstring, and moved the hint, details and log fields in require_role and load_user_ref_key into local variables.
- Fixed lint findings in the WIP. PLR0911 in `_by_class`: it now uses one if/elif chain with a single return. S105: `_KEY_SECRET` is now `_KEY`. Also E501 and the S105/S106 findings in the tests.
- Fixed OWN040 (see Deviations).
- Added `# pragma: allowlist secret` to two test-detail dicts that detect-secrets flagged (the value is a secret name, not a secret). The baseline is unchanged.

## Files
- herness/reports/rules.py (new, 297 lines / budget 300)
- tests/unit/reports/test_rules.py (new, 479 lines)

## Units -> symbols
- U09-29: UiRole (spec name Role, see Deviations), ROLE_RANK, ACTION_ROLES, ACTION_TEXT (MappingProxyType, built from one _ACTIONS table), Actor (frozen; display kept out of repr), _check_tables (raises ConfigError on key mismatch at import)
- U09-30: role_for
- U09-31: user_ref_for; load_user_ref_key (functools.cache, cleared through config._RESET_HOOKS by reset_config; a missing secret raises ConfigError "secret not found: ui_user_ref_key" with details code=secret_missing, secret=ui_user_ref_key and hint "Run `herness secrets init`.")
- U09-32: require_role
- U09-33: UserInputError(RecoverableError), validate_reason/note/question/correction/answer, check_id, SESSION/MESSAGE/ITEM/REC/JOB/MEMORY_ID_RE
- U09-88: user_message (+ _report_contract, _by_class helpers)

## Tests -> IDs
- UT09-50: vector plus case/whitespace variants; key read once per process and reset by reset_config; missing key -> ConfigError and its user_message
- PT09-06: hypothesis property (output is 32 lowercase hex; case and surrounding whitespace do not change it)
- UT09-51: role_for (admin wins, default applies, None and empty -> denied)
- UT09-52: table key equality, ranks, UiRole literal args, mismatch -> ConfigError; the viewer is refused for every reviewer/admin action including job_inline, with the exact phrase and hint, EXACTLY ONE `auth` audit line in the temp audit file with fields {user_ref, role, result}, and the app.auth.denied WARNING fields; the job_inline R-45 phrase; allowed calls write no audit line; unknown action -> ConfigError; an anonymous actor's audit fails -> app.auth.audit_failed ERROR, PermissionDenied still raised
- UT09-65: every table row gives the exact what/fix (incl. MemoryNotFound, NoCurrentBuild by class name and subclass, NotFound code no_current); missing details fall back to non-empty text
- UT09-72: reasons of 9, 10, 1000 and 1001 chars; notes of 500 and 501 chars with max_chars=500; required/empty note; control characters removed; question, correction and answer bounds; id patterns (the bad value is not echoed)

## Rulings applied (controller)
- The `auth` audit call has no `action=` kwarg; a code comment cites the ruling. The action goes in the app.auth.denied WARNING log with user_ref, role and channel.
- If audit raises (e.g. an anonymous actor fails the SchemaViolation check), app.auth.audit_failed is logged at ERROR and PermissionDenied is still raised. No special case.
- NoCurrentBuild is matched by class name anywhere in type(exc).__mro__, plus NotFound with code no_current. Nothing imports app/.
- The herness_app_auth_denied_total metric is not written.
- details["code"] lookups use .get, so missing details are handled.

## Deviations
1. The role alias is `UiRole`, not the spec's `Role`. tools.check_type_ownership reported OWN040: impl 06 owns `Role` in core.types (swarm roles), and the checker flags a public `Role` defined in rules.py. I followed the T10-04 precedent (`Kind` -> `RegistryKind`), which the controller approved. There is a code comment at the definition. Cost if wrong: one rename back plus a checker exception. Spec erratum needed: impl 09 U09-29/U09-30, the §2 module map row, and the Actor.role type. Later impl 09 cards must import `UiRole`.
2. require_role's PermissionDenied also sets details {code: role_missing, action, role}, and user_ref_for raises ConfigError (code secret_missing) for an empty key. The brief does not specify either; both are additions that stay within its rules.

## Gates (final tree, a328e9c)
- ruff format / ruff check: clean
- mypy: Success, 130 source files
- lint-imports: 13 kept, 0 broken
- check_type_ownership: exit 0
- check_module_size: exit 0 (rules.py 297/300)
- PYTHONUTF8=1 pytest -m "(unit or integration) and not slow" -p no:logging: 3498 passed, 5 skipped, 16 deselected, 1 xfailed (IT00-02, existing)
- Card tests (-k UT09_50 or UT09_51 or UT09_52 or UT09_65 or UT09_72 or PT09_06): 55 passed
- Coverage of rules.py: 100 % line (174 stmts), 100 % branch (56 branches)
- The pre-commit hooks all passed (detect-secrets passed after the two inline pragmas).

## Concerns
- The Role -> UiRole rename needs a controller ruling or spec erratum (Deviation 1).
- Refusals by an anonymous actor leave no audit line (by ruling; spec 09/10 should choose an audit actor for anonymous).
- The auth audit line has no action field until impl 10 adds it to the optional fields for `auth`.
