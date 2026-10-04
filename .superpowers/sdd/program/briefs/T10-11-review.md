# T10-11 review (round 0) — commit 949496c — verdict: Needs fixes
(Relayed by the sub-controller; the reviewer's write outside the worktree was refused.)
Spec: all U10-47 / U10-49 steps and rows UT10-43, UT10-44, UT10-47, FT10-07, ST10-30 met, except the extra allow rules (I1, I2) and a crash (I3).
Critical: none.
Important:
- I1 redact_scan.py:41,:61 PHONE decimal allow rule uses _DECIMAL.match (not anchored at end): `30.123456 7890`, `12.345678-9012`, `41.555012 34`, `01.234567 89` allowed. Fix: fullmatch plain decimal `\d{1,2}\.\d{6,}` plus fractional-seconds form `\d{2}\.\d{6,9}[+-]\d{2}`; negative tests.
- I2 redact_scan.py:68 CARD "starts with 0" allow rule lets zero-padded real cards through (`04532015112830366`, `0 4532 0151 1283 0366`, `0-4532-...`, `0378282246310005`, `0004532015112830366`). Fix: strip separators and leading zeros; allow only when < 12 digits remain; negative tests.
- I3 redact_scan.py:105,:123-124 a line > 4,000,000 chars raises RedactionFailed("text too long") uncaught -> traceback, no finding line. Fix: catch per line / parquet cell and report as finding; test.
Minor:
- m1 _redact_pool.py:42 only RedactionFailed caught per row (spec: any raising row -> NULL); non-pool errors abort the table.
- m2 _redact_pool.py:59 private config._Cache.root / "config" — wrong dir when config dir not named `config`; retryable StoreBusy loop on permanent misconfig (linked to accepted carry-over).
- m3 _redact_pool.py:112-113 in-flight limit branch uncovered.
- m4 test_redact_table.py:195-202 single-chunk test does not prove no pool started.
- m5 redact.py:352-353 one warning per failed row (6M lines on systemic failure).
- m6 _redact_pool.py:112 initargs include a Path; X-2 says built-in types only (use str).
⚠️ full-suite/mypy/lint/pre-commit taken from builder report; card tests re-run: 32 passed. Coverage _redact_pool 98%, redact_scan 99%.

# Re-review r1 (73ff103): Approved
I1 ✅ I2 ✅ I3 ✅ m1 ✅ m3 ✅ m4 ✅ m6 ✅; no regression; --scan tests/fixtures exit 0; 55 card tests pass; coverage _redact_pool 100%, redact_scan 99%.
Minor n1 redact.py:353 redact.record.failed logs error_type="RedactionFailed" for every failed row (hides unexpected exception class; no leak).
Minor n2 (info) decimal forms like `1.4155550142` still allowed — accepted narrowed deviation.
