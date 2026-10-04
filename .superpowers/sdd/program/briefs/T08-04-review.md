# T08-04 review: Policies and classification

Reviewer: verify agent (read-only). Worktree D:\herness\.claude\worktrees\agent-a6e243172f00f5502, build commit 7ffffb5 (base 9e319a0).

**Verdict: Needs fixes** (2 Important, no Critical)

### Spec Compliance
- U08-11 RetryPolicy / POLICY_FAMILY / GPU_HEALTH_POLICY: ✅ (all fields; family map and GPU constant verbatim; frozen). Extra: defaults on the three optional fields (policies.py:36-38), harmless.
- U08-12 policy: ✅ gpu_health constant; config_hash(get_config()) checked; whole cache rebuilt from R.retry.policies under ProcessState.lock and hash recorded; unknown name -> ConfigError naming `policy` and the name (context policy=<name>). UT08-06 table cross-checked against design 08 §5.2 and §7 YAML: matches.
- U08-13 policy_for_client: ✅ off_network -> llm_cloud, large -> llm_large, else llm_local.
- U08-14 full_jitter_delay / FullJitterRetryAfter: ✅ ceiling/jitter math, retry_after clamp min(max(ra, jitter), retry_after_cap_s); tenacity wait_base subclass reading attempt_number and RateLimited.retry_after. Exponent capped at 1023 (safe for gpu_health's 10 000 attempts).
- U08-15 job_backoff_delay: ✅ uniform(0, min(cap, base*2^(a-1))) from R.retry.job_backoff.
- U08-16 classify: ✅ rules 1-8 functionally (rule 3 evaluated after 4-6 together with rule 7; class sets disjoint so the first-match result is identical); SDKs only via sys.modules; OverloadedError via getattr; sqlite/duckdb lock texts only; no exception text in messages. Controller ruling (redact BEFORE cut; None -> no detail, never sliced; <= 2 KB) implemented and tested for None, a raising redactor, an e-mail straddling char 500, the 5 000-char body and a multi-byte 2 KB bound. ❌ However the builder's 4 000-char pre-redaction window reintroduces the straddle leak at char 4 000 (Important 1).
- U08-17 parse_retry_after: ✅ delay-seconds regex (ASCII [0-9]{1,10} with fullmatch; stricter than \d, correct per RFC 9110), HTTP-date with a zone required, X-RateLimit-Reset regex, 10^12 ms / 10^9 s thresholds, clamp to [0, max_s], case-insensitive for dict and httpx.Headers; never raises when max_s is given (PT08-03 hypothesis).
- Test IDs: UT08-06, UT08-07, UT08-10, UT08-11, UT08-112, PT08-01, PT08-02, PT08-03 each have >= 1 function; names carry the ID with `_`, docstrings start with the ID, `pytestmark = pytest.mark.unit` in both files. Acceptance subprocess check present (test_classify.py:219-228: importing classify leaves openai/anthropic/duckdb out of sys.modules). No secret or personal data in messages under the tested inputs (but see Important 1).
- ⚠️ Acceptance `-k "UT08-06 or ..."` (hyphen form, as written in the card) deselects all 203 tests (reproduced); the underscore form selects 136, all pass. Spec-text gap, not a build defect.
- ⚠️ policy() calls config_hash on every lookup; with herness.core.redact imported, config_hash resolves the redaction key id (impl 10). Per spec step 2; owner-10 behaviour, not re-verified here.

### Gates (re-run by reviewer, worktree .venv)
- `pytest tests/unit/core/resilience -q -p no:logging`: 203 passed.
- Coverage (branch): classify.py 100 % line, 2 partial branches (99 %); policies.py 97 % (lines 102-103 defensive). Both >= 90 % line / >= 85 % branch.
- mypy: 0 issues (145 files). ruff check: clean. ruff format --check: clean. tools.check_module_size: exit 0 (classify.py 225/230, policies.py 149/220, __init__.py 67/70).

### Strengths
- Small focused helpers (_classes, _body, _redacted, _msg, _from_status/_from_sdk/_from_store); complexity well under limits.
- Fail-closed redaction path covers None and exceptions (e.g. missing-key ConfigError); an unreadable streamed body is handled.
- Strong property tests (PT08-01 10 000 samples + hypothesis; PT08-03 never-raises and exact integers).
- Secret fixture built at runtime so the detect-secrets baseline stays unchanged.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **Redaction window leaks a straddling fragment at char 4 000** - herness/core/resilience/classify.py:41, 88-94. `redact_text(body[:4000])` then `[:500]`: an e-mail/key split at char 4 000 is redacted only as a fragment; if the preceding 3 500+ chars shrink under redaction (long e-mails -> 18-char `[EMAIL_xxxxxxxxxx]`), the unredacted fragment lands inside the first 500 chars. Reproduced with the real Redactor: body = 14 x 266-char e-mails + one 258-char e-mail + " jane.victim@examplecorp.org ..." (split at 4 000 after "@examp") -> message ends `... [EMAIL_be6d43d7c2] jane.victim@examp` (personal data in an error message; TH08-02 and the global "no personal data in any error message" constraint). Same defect class the controller ruling removed at char 500, moved to char 4 000; an upstream server echoing request data can trigger it. Fix (simple, robust): when `len(body) > _REDACT_WINDOW`, drop the tail of the redacted window before the 500-char cut, e.g. `clean = clean[: max(len(clean) - DETAIL_CHARS, 0)]` (an unredacted straddling fragment always sits at the end of `clean`, and no e-mail/key fragment is longer than 500 chars); optionally also trim the window back to the last whitespace first. Add a UT08-11 test with the construction above (e-mail and key text straddling char 4 000 after heavy shrinkage). Catching exceptions from redact_text (classify.py:90-93) is sound: fail closed, classify must return.
2. **classify module/function name clash is import-order dependent** - herness/core/resilience/classify.py:216-225 with herness/core/resilience/__init__.py:25, 59-67. The callable-module trick keeps `resilience.classify(...)` callable either way, but the PEP 562 `__getattr__` overwrites the package attribute with the *function* when `from herness.core.resilience import classify` runs before the submodule was imported. After that, `import herness.core.resilience.classify as m` binds the **function** (IMPORT_FROM reads the package attribute first), so `m.parse_retry_after` raises AttributeError. Reproduced: `from herness.core.resilience import classify; import herness.core.resilience.classify as m` -> `type(m)` is `function`, `hasattr(m, "parse_retry_after")` is False; in the other order it is `_CallableModule`. Impl 01 is told to switch to `herness.core.resilience.classify.parse_retry_after`, so this is a real trap. Simplest compliant fix: make the package attribute deterministic - in `__getattr__`, when `name == submodule` return the module itself (callable via `_CallableModule.__call__`), never the function (e.g. `mod = importlib.import_module(...); value = mod if name == submodule else getattr(mod, name)`). mypy keeps seeing the function via the TYPE_CHECKING re-export. Extend test_rf_package_classify_is_callable_in_either_import_order (test_classify.py:231-245) with the reverse order plus `import ... as m; m.parse_retry_after`. The trick itself (module `__class__` swap, typed `__call__` delegating to the function) is acceptable, given the spec fixes both the file name and the qualified function name.

#### Minor (Nice to Have)
3. Out-of-card change superseded - tests/unit/core/test_config_validate.py:344, 620. Controller commit 47472ea (on the integration branch) already fixes the same two C06 tests differently (64-hex key; provider removed only for the store-down step). The builder's version disables `_KEY_ID_PROVIDER` for the whole test and will conflict at merge; resolve in favour of 47472ea and drop this hunk.
4. classify can raise on 429 - classify.py:113-116, 211-212. `parse_retry_after(..., max_s=None)` reads config; with no loaded config a 429 carrying a valid Retry-After raises out of classify (meant to return, not raise). Resolve max_s defensively (fallback to the 86 400 default) or catch. The report acknowledges the U08-17 "never raises" gap for max_s None.
5. Silent fail-closed - classify.py:92. A redactor exception is swallowed without a log; a one-line warning carrying only `error_type` would make a missing redaction key visible.
6. Rule order - classify.py:160-163. Rule 3 is checked after rules 4-6; identical today, but if an SDK class ever subclassed an httpx transport error the family mapping would differ. Move the httpx tuple check before `_from_sdk`, or comment the disjointness assumption (report deviation 5).
7. QueryError timeout carried as `context["timeout"]` (classify.py:148) because errors.py has no attribute; acceptable, but later 08 cards must read `err.context["timeout"]` - record it in the handoff.
8. Rule text as detail (report deviation 2): for 418 with a body, "unexpected HTTP 418" is dropped in favour of the body detail (classify.py:121-122); acceptable reading of the single-format postcondition, slightly inconsistent. UT08-11's "<= 500 chars before redaction" is superseded by the ruling (detail <= 500 after redaction) - fine.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Algorithms, constants, tests and gates are correct, but the 4 000-char redaction window reintroduces a personal-data leak (reproduced) and the classify module/function clash makes `import herness.core.resilience.classify as m` return the function depending on import order; both have small local fixes plus tests.


## Re-review round 1 (commit 1086376, diff 7ffffb5..1086376)

**Verdict: Approved**

Scope: I1, I2, m4, m5, m6 plus the redact-before-cut ruling and None handling (m3, m7, m8 ruled and parked, not re-reviewed).

- I1 ✅ closed - classify.py:87-88. When `len(body) > _REDACT_WINDOW` the last 500 redacted chars are dropped before the 500-char cut. My original reproduction (14 × 266-char e-mails + a 258-char e-mail + " jane.victim@examplecorp.org", split after "@examp") no longer leaks; the message is just "source call failed: HTTPStatusError HTTP 503". A sweep of 320 bodies (8-15 long e-mails, split offsets 0-39 before char 4 000) gave 0 leaks. No new leak path: an unredacted fragment split at the window edge always sits at the end of the redacted text, and no e-mail or key fragment exceeds 500 chars. The trim can only remove text, never add it. Side effect (acceptable, fail-closed): a >4 000-char body that shrinks heavily can lose its whole detail. New test test_ut08_11_value_straddling_redaction_window_leaves_no_fragment (e-mail case RED without the fix per report; api_key case is a guard).
- I2 ✅ closed - __init__.py:65-68. `__getattr__` hands out the submodule when name == submodule. Verified in fresh subprocesses for four orders (from-import first, submodule first, attribute first, policies first): the package attribute and `import herness.core.resilience.classify as m` are both the `_CallableModule`, `m.parse_retry_after` works, and the call returns ModelUnavailable. The test is now parametrised over three orders.
- m4 ✅ closed - classify.py:103-108, 118-120. `_max_s()` falls back to 86 400 on any config error; classify passes `max_s` explicitly. Verified in a subprocess with no config: a 429 with Retry-After 999999 gives RateLimited(retry_after=86400.0). Tested (test_ut08_11_429_without_loadable_config_uses_default_max).
- m5 ✅ closed - classify.py:84-85. The warning event `resilience.classify.redact_failed` carries only `error_type` (plus the logger's component); the test asserts the exact captured event. A None return is logged by redact_text itself, without text.
- m6 ✅ closed - classify.py:51, 164-169. Rule 3 now runs before the SDK/store rules, and rule 7 checks only TimeoutError | concurrent.futures.TimeoutError. On httpx 0.28.1, `httpx.TimeoutException.__subclasses__()` is exactly {ConnectTimeout, PoolTimeout, ReadTimeout, WriteTimeout}, so (ConnectError, TimeoutException, RemoteProtocolError) is the same set as the spec list. It stays spec-equivalent as long as httpx adds no new TimeoutException subclass; any future one would still be a transport timeout, which is the intent.
- Ruling ✅ still holds: redact_text runs before the cut; an e-mail straddling char 500 is not leaked (re-probed); redact_text returning None gives "source call failed: HTTPStatusError HTTP 503" (never sliced).

Gates (reviewer re-run): tests/unit/core/resilience 208 passed; classify.py 98 % (line 119 = 429 without a headers mapping, 2 partial branches), policies.py 97 %, __init__.py 100 %; mypy 0 issues (145 files); ruff check and format clean; check_module_size exit 0 (classify.py 229/230, __init__.py 70/70 - both at or next to budget, so later 08 cards adding exports to __init__.py will need to raise its budget or restructure).

New findings: none blocking.
- Minor (info): __init__.py is at 70/70 and classify.py at 229/230. The next card that adds a lazy export will exceed the __init__ budget; the controller should plan for that.
