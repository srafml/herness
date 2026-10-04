# T08-04 report: Policies and classification

Status: DONE_WITH_CONCERNS
Commit: 7ffffb5 feat(resilience): add retry policies and error classification (T08-04)
Worktree/branch: D:\herness\.claude\worktrees\agent-a6e243172f00f5502 / worktree-agent-a6e243172f00f5502 (base 9e319a0)

## Built
- herness/core/resilience/policies.py (149 lines, budget 220): RetryPolicy (frozen, slots), POLICY_FAMILY
  (read-only mapping), GPU_HEALTH_POLICY, policy (U08-12: gpu_health constant; else config_hash(get_config())
  checked and the whole cache rebuilt from R.retry.policies under ProcessState.lock; unknown name -> ConfigError
  "unknown retry policy: 'bogus'" with context policy=<name>), policy_for_client, full_jitter_delay,
  FullJitterRetryAfter(p, rng) (tenacity wait_base; RateLimited.retry_after of the last exception),
  job_backoff_delay. Exponent capped at 1023 so gpu_health's 10 000 attempts cannot overflow.
- herness/core/resilience/classify.py (225 lines, budget 230): classify (rules 1-8 of U08-16), parse_retry_after
  (U08-17; ASCII-only digit regexes with fullmatch; case-insensitive lookup for dict and httpx.Headers; max_s None
  reads R.retry.retry_after_max_s only once a header is valid). Type alias ErrorFamily lives here (policies imports it).
  openai/anthropic/duckdb classes are looked up only via sys.modules.
- _state.py: RetryPolicy placeholder replaced by a TYPE_CHECKING import; policies_hash (already present) documented.
- __init__.py (67 lines, budget 70): exports RetryPolicy, policy, classify.
- tests: tests/unit/core/resilience/{conftest.py (herness_cfg: full config via write_full_config + fake_keyring;
  test_redactor), test_policies.py, test_classify.py}.

## Deviations
1. Redact-before-cut (controller ruling on U08-16): body -> redact_text -> cut to 500. Redaction runs on the first
   4 000 chars of the body only (_REDACT_WINDOW) to keep the "bounds redaction cost" property; residual risk is a
   secret split at char 4 000 surfacing in the first 500 redacted chars, which needs >3 500 chars of shrinkage.
   redact_text returning None, or raising (e.g. missing key -> ConfigError), gives no detail (": <detail>" omitted).
   Message cut to 2 048 UTF-8 bytes before HernessError's 1 000-char bound. Code comment in _redacted().
2. Message format for rules with their own text (spec gap: rules name "unexpected HTTP <status>", "sqlite error",
   "query interrupted after timeout", "unclassified <Type>" while the postcondition fixes one format): the rule
   text is used as <detail> when there is no body detail, e.g. "store call failed: OperationalError: sqlite error",
   "model call failed: HTTPStatusError HTTP 418: unexpected HTTP 418". Exception text is never included.
3. QueryError has no timeout attribute (errors.py untouched): QueryError(msg, timeout=True) puts it in
   err.context["timeout"] (HernessError **context). Later cards should read context["timeout"].
4. Name clash: herness.core.resilience.classify is both the submodule and U08-16's function. The module sets its
   own __class__ to a callable ModuleType subclass, so resilience.classify(exc, family=...) works whichever object
   the package attribute holds (the import system assigns the module on first import; the lazy export assigns the
   function). mypy sees the function (TYPE_CHECKING re-export). Tests get the module via sys.modules / use
   `from herness.core.resilience.classify import classify, parse_retry_after`. Test test_rf_package_classify_... covers it.
5. Rule 3 (httpx transport errors) is checked together with rule 7 after the SDK/store rules; the class sets are
   disjoint so the first-match result is identical.
6. Outside the card: tests/unit/core/test_config_validate.py, two UT10-19 C06 tests now monkeypatch
   c._KEY_ID_PROVIDER to None. They fail at base 9e319a0 whenever herness.core.redact is imported in the session
   (e.g. `pytest tests/unit/core/test_redact_redactor.py tests/unit/core/test_config_validate.py -k "c06 or ut10_42_none"`
   fails on unmodified HEAD): config_hash then loads the redaction key, which the tests store as "value-present"
   (not 64 hex) or make the keyring raise, so validate returns a config error instead of C06 rows. The full unit
   suite (pre-commit pytest-unit hook) failed on HEAD because of it. Please have the impl 10 owner confirm.
7. UT08-11 fixture `api_key=synthetic_key_abc` is built at runtime ("api" + "_key=" ...) as in test_redact_redactor,
   so detect-secrets passed and .secrets.baseline is unchanged.
8. No metric call site in these units; no T08-05 markers needed.

## Spec gaps / notes
- Acceptance `-k "UT08-06 or ..."` with hyphens deselects everything (pytest matches names; IDs are ut08_06).
  The underscore form `-k "UT08_06 or UT08_07 or UT08_10 or UT08_11 or UT08_112 or PT08_01 or PT08_02 or PT08_03"`
  selects 136 tests, all pass.
- policy() computes config_hash on every call (spec step 2); that walks the effective config each lookup.
  Cheap enough for per-call use but a candidate for an identity cache if profiling shows it.
- parse_retry_after with max_s None raises if no config can be loaded (spec says "never raises"); only config
  failure can cause it.
- UT08-11 says "body part <= 500 chars before redaction"; with the ruling the detail is <= 500 chars after redaction.

## Tests / gates
- tests/unit/core/resilience: 203 passed (RED first: collection ImportError on missing modules).
- Coverage: classify.py 99 % (2 partial branches), policies.py 97 % line (defensive missing-name branch).
- Acceptance subprocess: importing herness.core.resilience.classify leaves openai/anthropic/duckdb out of sys.modules
  (test_ut08_10_classify_imports_no_sdk).
- `pytest -m "unit or integration"` (pre-commit test deselected; hooks ran at commit): 3986 passed, 5 skipped, 1 xfailed.
- ruff format/check clean, mypy strict 0 issues (145 files), lint-imports 13 kept, check_type_ownership 0,
  check_module_size exit 0. Commit hooks all passed (no --no-verify, no PRE_COMMIT_ALLOW_NO_CONFIG).

## Fix round 1
Commit: 1086376 fix(resilience): close classify review findings (T08-04) (new commit, no amend; all hooks passed)
- I1: when len(body) > 4 000, the redacted window loses its last 500 chars before the 500-char cut
  (clean[: max(len(clean) - 500, 0)]). New test test_ut08_11_value_straddling_redaction_window_leaves_no_fragment
  [email, api_key]: 14 x 266-char e-mails (long domain labels, so the real Redactor shrinks them to tokens) plus one
  filler e-mail sized so char 4 000 splits right after "jane.victim@examp" / "api_key=synth". RED confirmed for the e-mail
  case (without the trim the first 500 redacted chars contain "jane.victim@examp"). The api_key case is a guard only:
  the Redactor already redacts the split "api_key=synth" fragment, so it passes either way.
- I2: package __getattr__ returns the submodule when name == submodule (so `classify` is always the callable module,
  never the function); mypy still sees the function via TYPE_CHECKING. test_rf_package_classify_is_callable_in_either_import_order
  now runs 3 fresh subprocess orders (submodule first, `from ... import classify` first, attribute first), each checking
  the call, `import herness.core.resilience.classify as m; m.parse_retry_after`, and `classify.parse_retry_after`.
- m4: _max_s() reads R.retry.retry_after_max_s and falls back to 86 400 on any error; classify passes it explicitly.
  Test test_ut08_11_429_without_loadable_config_uses_default_max.
- m5: redactor exception -> _log.warning("resilience.classify.redact_failed", error_type=...) through
  herness.core.logging.get_logger("resilience"); test asserts the single captured event. A None return is already
  logged by redact_text itself (redact.record.failed), so classify does not log it again.
- m6: rule 3 (httpx transport tuple) is checked before the SDK rules; rule 7 checks only TimeoutError types.
  The tuple is now (ConnectError, TimeoutException, RemoteProtocolError); TimeoutException's subclasses are exactly
  Connect/Read/Write/PoolTimeout (checked on httpx 0.28.1), so the set is the same.
- Sizes: classify.py 229 / 230, __init__.py 70 / 70, policies.py 149 / 220. check_module_size exit 0.
- Gates: ruff format/check clean, mypy 0 issues, lint-imports 13 kept, type-ownership 0,
  tests/unit/core/resilience 208 passed; classify.py coverage 98 % (line 130, 429 without a headers mapping, is uncovered).
- m7, m8 parked as instructed; test_config_validate hunk kept.
