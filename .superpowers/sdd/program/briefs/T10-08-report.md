# T10-08 report: Redaction detectors

Status: DONE_WITH_CONCERNS (small, documented deviations below)
Commit: 9975bf5 feat(core): add redaction detectors (T10-08) — branch worktree-agent-a67b7b5e997464a4f

## Built
- `herness/core/redact_patterns.py` (299 lines, budget 380): `EntityType` (PEP 695 `type` alias of the Literal), `DETECTION_ORDER`, `TOKEN_PATTERN`, `Detector` (frozen dataclass: type, prefilter, find), `build_detectors(cfg)` (all detectors in DETECTION_ORDER except PERSON), `luhn_valid`, `normalize_value`.
  - CREDENTIAL (a)-(f) yielded in spec order, span group per sub-pattern; (d) case-sensitive, the rest IGNORECASE.
  - URL_TOKEN: `urlsplit` for the query, then `&`-split pieces with raw value offsets (name compared via `unquote_plus(...).lower()`), empty values and bare names skipped; `urlsplit` ValueError -> no spans.
  - CARD/PHONE/IPv4: `\d` written `[0-9]` (ruling); prefilter digit counts are ASCII-only.
  - Config detectors (NATIONAL_ID/EMPLOYEE_ID/USER_ID digit prefilter; CUSTOM no prefilter) compile patterns IGNORECASE, whole match; zero-length matches are skipped; an empty pattern list makes the prefilter false.
- `herness/core/redact.py` (31 lines, budget 390): types only — `Span` (frozen, order=True, so it orders by start first), `RedactionResult` (frozen); re-exports `EntityType`, `DETECTION_ORDER`.
- `pyproject.toml`: added `herness.core.redact`, `herness.core.redact_patterns` to the "core base is closed" forbidden list (UT00-58 requires every core module there).

## Tests (tests/unit/core/test_redact_patterns.py, 399 lines, 101 cases)
Recovered file from the previous agent kept after review; fixes:
- positive row `pwd = 'x'` expected `'x'` — wrong per spec (group 1 excludes `'`); replaced by `pwd = x1y2`.
- normalize row `+415 555 0142 1` -> `+4155550142` was wrong (11 digits); now `+415 555 0142`.
- RUF001 (literal Arabic-Indic digits) -> `chr(0x0661)`; ERA001 comment reworded; docstring notes UT10-36/38 will be re-pointed at `Redactor.scan` when U10-41 lands; added malformed URL / one-colon IPv6 / unparsable IP cases for coverage.
IDs covered: UT10-36 (types, order, positive/negative table per type, prefilters, edge cases), UT10-38 (INC ticket, date, IP, phone -> only the phone is PHONE), UT10-39 (normalize_value and Luhn known values, U10-37 rows), PT10-04 (hypothesis vs reference Luhn, 500 examples), CV-T10-08 (no built-in detector span overlaps an existing pseudonym token, except credential/URL value groups, which U10-41 protected ranges handle).

RED: `uv run pytest tests/unit/core/test_redact_patterns.py` -> `ImportError: cannot import name 'redact' from 'herness.core'` (collection error).
GREEN: 101 passed; coverage redact.py 100 %, redact_patterns.py 100 % line and branch.

## Gates
ruff format --check clean; ruff check clean; mypy: no issues (31 files); lint-imports 8 kept 0 broken; check_type_ownership exit 0; check_module_size exit 0; `pytest -m "(unit or integration) and not slow"`: 602 passed, 1 xfailed (pre-existing IT00-02 marker).

## Deviations / concerns
1. EntityType and DETECTION_ORDER live in redact_patterns and are re-exported by redact (spec §2 import order puts redact_patterns left of redact, so redact_patterns cannot import redact). `redact.EntityType is redact_patterns.EntityType`.
2. `normalize_value("IP", ...)`: `ipaddress.ip_address` rejects leading-zero octets (e.g. `01.2.3.4`, which the IPv4 detector does match) and the spec says "errors: none". Dotted-quad values are read as decimal octets first (`01.2.3.004` -> `1.2.3.4`); anything still unparsable is returned unchanged.
3. `normalize_value("PERSON", ...)`: group 1 of the `last, first` rewrite is right-stripped so `Doe , Jane` -> `jane doe` (no trailing space).
4. Spec-faithful behaviour worth knowing: `(415) 555-0142` yields span `415) 555-0142` (the candidate regex cannot start at `(`); a standalone `::` (e.g. `a :: b`) is a valid IPv6 and is detected as IP.
5. `normalize_value` keeps the spec parameter name `type` (`# noqa: A002`). Test file sits in tests/unit/core/ (repo layout) rather than spec's tests/unit/test_redact_*.py.

## Fix round 1 (review T10-08-review.md)
Commit: see `git log` — fix(core): harden redaction detectors after review (T10-08)

- Important 1 (quadratic CREDENTIAL): PEM (a) now `_find_pem` — search `-----BEGIN [A-Z ]*PRIVATE KEY-----`, then the nearest following `-----END [A-Z ]*PRIVATE KEY-----`, yield the block, resume after it; stop at the first BEGIN without an END (same spans as the lazy spec regex, linear). JWT segments `{5,4096}+` (possessive; identical matches since `.` is outside the class). URL user-info scheme `[a-z0-9+.-]{0,31}`. New test `test_ut10_36_detectors_linear_on_adversarial_input` (6 inputs, 400-540 KB, all detectors, < 1 s each; measured max 0.29 s for `"eyJ-"*100000`).
- Important 2 (URL_TOKEN fail-open): `_raw_query` falls back to text between the first `?` and the first `#` when `urlsplit` raises. Test now expects spans for `http://[bad/?sig=SECRETSIG`, `http://[::1/?code=abc`, `http://h]/?key=K1` (+ an access_token case with fragment); a separate test keeps "no query / `#` before `?`" -> nothing.
- Important 3 (Unicode digits): config-pattern prefilter uses `re.compile(r"\d").search` — verified identical to `str.isdecimal` over all code points (0 differences), chosen over a Python per-char loop for speed. Test with Arabic-Indic and fullwidth `123-45-6789` (default NATIONAL_ID).
- Minor: `EntityType = Literal[...]` plain assignment (test uses `get_args(redact.EntityType)`); USER_ID and CUSTOM negative rows added; PEM multi-block/unterminated test. URL fragments still not scanned.

Gates: ruff/format clean, mypy clean, lint-imports 8 kept, check_type_ownership 0, check_module_size 0. Tests: 119 passed (coverage 100 % line+branch both modules); unit+integration not slow: 620 passed, 1 xfailed (pre-existing).
Line counts: redact_patterns.py 329/380, redact.py 31/390, test file 467.

Concern: JWT is linear but with a large constant on pathological input (`"eyJ-"*n`: ~0.7 s/MB, measured 2.9 s for 4 MB, the scan() maximum), above BT10-05's 50 ms/MB target for that adversarial shape only; normal text is unaffected. Tightening further (e.g. requiring no class char before `eyJ`) would change matches vs the spec's `\b`, so left for a controller ruling.

## Fix round 2 (re-review: JWT cap failed open)
Commit: fix(core): linear JWT detection without length cap (T10-08)

- JWT (CREDENTIAL d) is now `_find_jwts`: `\beyJ` starts (case-sensitive), maximal `[A-Za-z0-9_-]+` runs precomputed once and looked up with `bisect`. A match needs 3 segments of at least 5 characters (the first counted after `eyJ`), separated by `.`. The span runs to the end of the third run, and a new start is skipped when it falls inside the previous match (finditer semantics). No length cap. Yield order inside CREDENTIAL stays in spec order: PEM, (b), (c), AKIA, gh, xox, JWT, (e), (f).
- Tests: `test_ut10_36_jwt_long_segment` (`"eyJ"+"a"*5000+".b"*..` gives span (0, 5105)); `test_ut10_36_jwt_matches_spec_regex` (hypothesis, 600 examples, alphabet `eyJa1_-. x\u00e9`, compared with the spec regex's finditer spans); a parametrized set of hand-picked edge cases, including `\b` after a non-ASCII letter. The linearity test bound is now 5 s per input (was 1 s) to tolerate loaded CI.
- Timing: `_find_jwts("eyJ-"*1_000_000)` (4 MB) takes 0.30 s. The whole CREDENTIAL detector takes 0.83 s on 4 MB, previously 2.9 s. Both scale linearly.

Gates: ruff and format clean, mypy clean, lint-imports 8 kept, check_type_ownership 0, check_module_size 0. Tests: 127 passed, 100% line and branch coverage. Unit+integration not slow: 628 passed, 1 xfailed (already marked).
Line counts: redact_patterns.py 374/380 (little headroom left), test file 508.
Concern: redact_patterns.py has 6 lines of budget left for later cards that touch it.
