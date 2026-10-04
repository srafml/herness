# T10-08 review (Redaction detectors): worktree agent-a67b7b5e997464a4f @ 9975bf5

**Verdict: Needs fixes**

### Spec Compliance
- U10-35 EntityType / DETECTION_ORDER / Span / RedactionResult: ✅ (members and order verbatim; Span frozen, order=True with start first; re-export from redact per ruling). EntityType is a PEP 695 `type` alias rather than the spec's plain `EntityType = Literal[...]` (see Minor 1).
- U10-36 TOKEN_PATTERN: ✅ verbatim (markdown `\|` correctly read as `|`).
- U10-36 CREDENTIAL (a)-(f): ✅ regexes verbatim, (d) case-sensitive, rest IGNORECASE, span groups correct. Prefilter ✅ (every sub-pattern needs `-----`, `:`, `=`, or a listed literal).
- U10-36 URL_TOKEN: ⚠️ matches spec for well-formed URLs; fails open on URLs that `urlsplit` rejects (Important 2).
- U10-36 EMAIL, CARD (13-19 + Luhn), PHONE (9-15 digits, date/IPv4 exclusions, lookbehind), IP (IPv4 + IPv6 >=2 colons + `IPv6Address`): ✅ verbatim (`[0-9]` per ruling). Prefilters ✅.
- U10-36 NATIONAL_ID / EMPLOYEE_ID / USER_ID / CUSTOM: ❌ digit prefilter is ASCII-only but config patterns (including the shipped default `\b\d{3}-\d{2}-\d{4}\b`) match any Unicode digit, so the prefilter rejects text `find` would match (Important 3).
- U10-36 complexity "linear in text length for the shipped patterns": ❌ three shipped CREDENTIAL patterns are quadratic (Important 1, plan-mandated regex text vs plan-mandated linearity).
- U10-36 build_detectors order without PERSON: ✅.
- U10-37 luhn_valid: ✅. normalize_value: ✅ EMAIL, PHONE, ID/CARD, CUSTOM; ⚠️ PERSON `rstrip` of group 1 and IP leading-zero decimal reading are small deviations (both justified, see below).
- UT10-36: ✅ positive/negative table per type (EMAIL, CREDENTIAL x6, URL_TOKEN, CARD, NATIONAL_ID, EMPLOYEE_ID, USER_ID, CUSTOM, PHONE, IP), prefilters, value==span text. USER_ID/CUSTOM have no negative rows (Minor 3). Driven from build_detectors per ruling.
- UT10-38: ✅ the phone is the only PHONE; ticket and date yield nothing. The test also asserts `10.1.2.3` is detected as IP (test_redact_patterns.py:268). That is the only reading consistent with the IPv4 detector (the spec row means "only the phone is a phone"); accepted.
- PT10-04: ✅ hypothesis vs independent table-based reference, 500 examples, lengths 0-40.
- Import-linter: ✅ adding `herness.core.redact` and `herness.core.redact_patterns` to C4 "core base is closed" matches the impl 00 C4 target state ("every other existing module of herness.core ... redact"). herness/core/config.py absent ✅. Budgets: redact_patterns.py 299/380, redact.py 31/390 ✅.
- ⚠️ Cannot verify from diff: gate outputs (ruff, mypy, lint-imports, coverage 100%, 101 passed) are report claims; not re-run.

### Builder concerns, evaluated
1. EntityType/DETECTION_ORDER placement: covered by ruling. OK.
2. IP leading zeros: the spec's own IPv4 regex (`1?\d?\d`) accepts `01`/`004`, and `ipaddress.ip_address` rejects them, while the spec says "errors: none". Reading octets as decimal keeps one pseudonym per address and "unchanged" is a safe fallback, since the value is still replaced by a token. Accept. Record it as a spec clarification. Octal-style `010.1.2.3` becomes `10.1.2.3`, not 8.1.2.3. That is fine for pseudonym stability.
3. PERSON rstrip: literal spec gives `"jane doe "` for `Doe , Jane`, which breaks "one token per person across formats" (U10-37 security note). rstrip serves the spec intent. Accept; record as deviation.
4. `(415) 555-0142` -> span `415) 555-0142`: spec-faithful; the leftover `(` leaks nothing. Accept.
5. `::` as IP: spec-faithful (`::` is a valid IPv6Address); verified `a :: b` -> span `::`. The only cost is harmless over-redaction. Accept (Minor 4 suggests a spec note).
6. Contract change: correct per impl 00 C4. Accept.

### Strengths
- Regexes transcribed exactly; per-sub-pattern span groups are explicit data, not code branches.
- Every detector's prefilter is correct for the built-in patterns. I checked each hint against each sub-pattern.
- URL_TOKEN value offsets are computed on the raw query, so spans point at the exact substring.
- The CV test for existing pseudonym tokens is a good addition, and it documents the credential/URL value-group exception for U10-41.
- No error paths put a value into messages or logs (TH10-14 / ENG 3.4 clean).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **Quadratic CREDENTIAL regexes, violating U10-36's "linear in text length" (plan-mandated: the spec's regex text itself is superlinear).** Measured on this commit with `find` on adversarial input:
   - `herness/core/redact_patterns.py:77` PEM `[\s\S]*?` with repeated `-----BEGIN PRIVATE KEY-----` and no END: 5k reps 7.2 s, 10k 17 s, 20k (540 KB) 46 s.
   - `herness/core/redact_patterns.py:91` JWT, unbounded `{5,}`: `"eyJ-"*20000` (80 KB) takes 6.0 s. Each `\beyJ` start scans to the end.
   - `herness/core/redact_patterns.py:93` URL user-info scheme `[a-z0-9+.-]*`: `"a."*20000` (40 KB) takes 2.3 s.

   Ticket bodies and log text are attacker-influenced, so this is an in-process DoS on the redaction path that every model call depends on (TH10-14: availability of the control). Fixes keep the same matches for realistic input:
   - PEM: find `-----END ... PRIVATE KEY-----` once, and skip BEGINs after the last END. Or use a `str.find`-driven scan, or bound the body (for example `{0,16384}?`).
   - JWT: bound the segments (for example `{5,4096}`).
   - Scheme: bound it to `{0,31}`.

   Add a linearity test, for example 1 MB of each adversarial string under a time bound. Escalate the spec text to the controller.
2. **URL_TOKEN fails open on URLs that `urlsplit` rejects** (`herness/core/redact_patterns.py:151-156`). `except ValueError: return` drops every span. Verified on this commit, neither URL_TOKEN nor CREDENTIAL yields anything for:
   - `http://[bad/?sig=SECRETSIG`
   - `http://[::1/?code=abc`
   - `http://h]/?key=K1`

   CREDENTIAL (c) catches only `token`/`password` names, and not `access_token`, because `_` defeats `\b`. So `sig`, `key`, `code`, `signature`, `sv`, `se` and `access_token` pass through, and the egress re-scan uses the same detectors. This is a TH10-14 leak. The test `tests/unit/core/test_redact_patterns.py:239-242` enforces the fail-open behaviour. Fix: on ValueError (or always), take the query as the text between the first `?` and the first `#`. `urlsplit` gains nothing here, because offsets are already computed manually. Change the test to expect the span.
3. **Config-pattern prefilter rejects text the find matches** (`herness/core/redact_patterns.py:138-139, 212`). `_has_digit` counts only ASCII digits, but config patterns compile without `re.ASCII`, so `\d` matches any Unicode decimal. The shipped default NATIONAL_ID `\b\d{3}-\d{2}-\d{4}\b` (herness/core/settings.py:200) is affected. Verified: `find` on the Arabic-Indic (U+0660..0669) and fullwidth (U+FF10..FF19) forms of `123-45-6789` returns a span, but `prefilter` returns False, so the ID is not redacted. The ASCII-digit ruling applies to the spec's own `\d` regexes, not to operator config patterns. Fix: use `any(ch.isdecimal() for ch in text)` for the config-pattern prefilter, or compile config patterns with `re.ASCII`, and add a test.

#### Minor (Nice to Have)
1. `herness/core/redact_patterns.py:33`: `type EntityType = Literal[...]` differs from the spec's `EntityType = Literal[...]`. `typing.get_args(EntityType)` returns `()`, and the test has to use `.__value__` (test:437). Later cards that call `get_args(EntityType)` or use it in validation would get it wrong silently. Prefer the plain assignment, with `TypeAlias` if needed.
2. Test location is `tests/unit/core/test_redact_patterns.py`, not the spec's `tests/unit/test_redact_*.py`. This is the repo convention; note only.
3. UT10-36 negative table has no USER_ID or CUSTOM rows (test ~170-195). The empty-config test partly covers this. Add, for example, `("U1234", "USER_ID")` and `("PRJ-42", "CUSTOM")`.
4. Spec gaps to escalate (spec-faithful here, not defects of this card):
   - URL_TOKEN ignores fragments, so OAuth implicit-flow `#access_token=...` leaks. Verified: `https://h/?x=1#access_token=SECRETAT` gives no span.
   - A bare `::` is redacted as IP.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The build is faithful and tidy, but it has three security-relevant correctness gaps in a TH10-14 control: quadratic CREDENTIAL patterns (DoS), URL_TOKEN failing open on malformed URLs, and an ASCII-only prefilter that suppresses Unicode-digit config matches. Each needs a small fix and a test.

---

## Re-review round 1 (fix commit 908a5e1)

**Verdict: Needs fixes** (one new Important, caused by the accepted JWT bound; everything else is resolved).

### Round-0 findings
- Important 1 (quadratic CREDENTIAL): ✅ resolved.
  - `_find_pem` (redact_patterns.py `_find_pem`) is linear and gives the same spans as the spec regex. I fuzzed 30,000 random PEM-fragment strings against the spec regex's `finditer`, including lower-case, multi-block, unterminated and END-only cases: 0 differences.
  - The adversarial BEGIN-only, BEGIN-letters and END-only inputs (about 500 KB) each take about 0.05 s.
  - The URL user-info scheme is bounded to `{0,31}`.
  - JWT uses the possessive `{5,4096}+`. On normal-size input it matches the spec exactly (30,000 random strings, 0 differences).
2. Important 2 (URL_TOKEN fail-open): ✅ resolved. `_raw_query` falls back to the text between `?` and `#`. Verified that `http://[bad/?sig=S1`, `http://[x]/?sig=S2` and `http://[::1]/?sig=S3` each yield their value. If `#` comes before `?`, the result is correctly nothing. The fallback's offsets match `url.index("?")`. The test was flipped and now expects the span.
3. Important 3 (Unicode digits): ✅ resolved. The prefilter now uses the Unicode `\d` search (`_has_decimal`), consistent with how config patterns compile, and there is a test for the Arabic-Indic and fullwidth forms of the default NATIONAL_ID.
- Minor 1 (EntityType as a plain Literal): ✅. Minor 3 (USER_ID/CUSTOM negative rows): ✅. Minors 2 and 4: covered by rulings.
- Tests: `tests/unit/core/test_redact_patterns.py` gives 119 passed in 1.35 s (PYTHONUTF8=1, worktree venv). redact_patterns.py is 329/380 lines.

### New findings
#### Critical
None.

#### Important
1. **New fail-open: a JWT with a segment longer than 4096 characters is no longer redacted** (`herness/core/redact_patterns.py` `_CREDENTIAL` JWT entry, `{5,4096}+`).
   - The spec regex matches the whole token. The bounded one matches nothing, because the possessive run stops at 4096 and `\.` then fails.
   - Verified: `"eyJ" + "a"*5000 + "." + "b"*50 + "." + "c"*50` gives spec span `(0, 5105)`, while CREDENTIAL now yields `[]`.
   - A signature segment over 4096 characters is only partly covered (`(0, 4201)` of 5105). This part is harmless, because the payload is covered.
   - This is realistic: Entra ID/Azure AD access tokens carrying group claims routinely exceed 4 KB in the payload segment. Outside an `Authorization:` header or a `token=` context, nothing else catches them (TH10-14).
   - It follows from the accepted `{5,4096}` ruling, so the controller decides. Suggested linear fix that keeps the spec's spans exactly:
     - finditer `\beyJ` for candidate starts;
     - precompute the end of each maximal `[A-Za-z0-9_-]` run, once per run;
     - accept when run1 is at least 8 characters, then `.`, then run2 of at least 5, then `.`, then run3 of at least 5, with span = start to the end of run3.

     This is exact because `.` is outside the class, so the greedy spec match always equals the maximal runs. It is also O(n) on `"eyJ-"*n`, which fixes the BT10-05 constant too.
   - Cheaper alternative: raise the bound (for example to 65536), accepting a larger pathological constant.

#### Minor
1. `test_ut10_36_detectors_linear_on_adversarial_input` uses a wall-clock bound under 1.0 s. It measures about 0.3 s locally and may flake on a loaded CI runner. Consider marking it `slow`/bench, or comparing the time on n against 2n.
2. The scheme bound `{0,31}` means a pure-letter scheme of 33 or more characters has no `\b` start, so the user-info leaks. This is unrealistic; note only.

**Task quality:** Needs fixes. The only open item is Important 1 (the JWT bound fail-open), unless the controller accepts it with its ruling.

---

## Re-review round 2 (fix commit e9944b6)

**Verdict: Approved**

- Round-1 Important 1 (JWT cap fail-open): ✅ resolved. `_find_jwts` scans maximal `[A-Za-z0-9_-]` runs with no length cap.
  - Exactness: 200,000 random strings (tokens `eyJ`, partial e/y/J, runs, `.`/`..`, `-`, `_`, space, U+00E9, U+0661, `/`, newline; up to 25 tokens) showed 0 differences from the spec regex's `finditer` spans. That includes the `\b` behaviour after non-ASCII letters, starts inside a previous match (skipped, as finditer skips them), and `eyJeyJ` overlaps. This is exact because `.` is outside the segment class, so a greedy spec match always ends at the end of a maximal run.
  - The Entra-size case (`"eyJ"+"a"*5000+...`) yields `(0, 5105)`, matching the spec.
  - Linear: `"eyJ-"*n` takes 0.074, 0.148, 0.29 and 0.60 s for n = 0.25M, 0.5M, 1M and 2M. The worst case of many real matches (`"eyJabcde."*n`) takes 0.60 s at 9 MB.
  - Yield order within CREDENTIAL is still in spec order (PEM, b, c, AKIA, gh, xox, JWT, e, f).
  - No new fail-open: a failed attempt only skips that start, which is the same as the regex.
- Round-1 Minor 1 (timing test flakiness): ✅. The bound is now 5 s against measured times of about 0.3 s, which keeps a big gap to the old quadratic times of 6-46 s.
- Tests: `tests/unit/core/test_redact_patterns.py` gives 127 passed (PYTHONUTF8=1). It includes the hypothesis comparison against the spec regex and hand-picked edge cases.
- Note: redact_patterns.py is 374/380 lines. Later cards that touch this module have 6 lines of headroom. This is not a defect.

No open Critical or Important findings.
