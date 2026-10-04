# T10-16 review (round 0) — commit a7e4b47 — verdict: Needs fixes (0 Critical, 2 Important, 9 Minor)
(Relayed by the sub-controller; the reviewer's write outside the worktree was refused.)
Spec: U10-50 ✅, U10-56 ✅, U10-57 ✅ (except m1), U10-107 ✅, U10-51 ❌ (I1, I2). All test rows ✅ (UT10-50 asserts "invalid" for port_not_443 until the regex ruling; UT10-52/ST10-12 halves deferred to T10-17 by ruling; BT10-05 xfail ~97 ms/MB). Coverage 100/100; sizes 257/390, 124/200, 101/120; pyproject/baseline OK. Reviewer's 16-way cross-process day-cap race: exactly 1 allowed.
Important:
- I1 _egress_scan.py:54 a body that is a single JSON string is scanned as raw text (JSON escapes hide PII: `"José García"`, `"john@corp.com"` allowed). Fix: when data is str, yield the decoded string (and raw text); test.
- I2 refusal paths without EgressBlocked / egress line / audit line (U10-51 postcondition; T08-09 ModelChain relies on EgressBlocked): (a) egress.py:134,:185 ConfigError from redactor_factory propagates; (b) egress.py:117 catches only (httpx.InvalidURL, TypeError) — UnicodeEncodeError (`https://api.anthropic.com/\ud800`) and idna.IDNAError (`https://xn--zz.com/`) escape; (c) _egress_scan.py:97 only RedactionFailed -> scan_failed. Fix: ValueError -> url_invalid; ConfigError/any Exception from factory or scan -> scan_failed via the normal blocked path; tests.
Minor:
- m1 egress.py:170-181 / egress_log.py:74 allowed/blocked lines omit §4.5 keys bytes_in, tokens_out, status_code, latency_ms; write accepts any subset.
- m2 egress.py:128 negative token_estimate lowers the day total (TH10-20).
- m3 egress.py:119,:145 blocked URL host logged as-is; URL path logged and path/query never re-scanned (spec gap).
- m4 _egress_scan.py not in card Files; §2 row cites a T10-16 ruling not in the ledger.
- m5 _egress_scan.py:90 empty-body model_download still fetches the redactor (needs HMAC key).
- m6 egress.py:246 get_guard: config_hash can raise ConfigError.
- m7 torn tail after crash merges with next append -> tokens under-counted (U10-61 limitation).
- m8 tests/security/test_st10_egress.py:103-113 cross-process test is sequential, not concurrent.
- m9 U+00AD, U+2061–U+2064, U+180E not stripped; `john­@corp.com` evades the scan (beyond spec list).
- Follow-up: after the reason regex widening, test_egress_check.py:56 asserts exc.reason == reason.

# Re-review r1 (c3df0b8): Approved (0 Critical, 0 Important)
I1 ✅ I2a/b/c ✅ regex widening + isinstance guard ✅ m1 ✅ m2 ✅ m4 ✅ m5 ✅ m6 ✅ m8 ✅ (race catches a no-op lock 10/10) m9 ✅; m3/m7 parked. TH10-22 re-probed on all new paths: no marker/email anywhere; lock scope intact.
Minor N1 _egress_scan.py:51-72 JSON encoded inside a JSON string leaf is not decoded (escaped non-ASCII / @ hide PII) — spec gap.
Minor N2 egress.py:127-128 + egress_log.py:55-56 float/bool token_estimate passes `>= 1`, logged as tokens_in but counted 0 by tokens_today (typed int; type-checked callers safe).
Nits: dead default line.get("scan_hits", {}) egress.py:140; _egress_scan.py:3 docstring still says "T10-16 ruling"; card Files lines (T10-11, T10-16) do not list the private siblings; U+034F/U+200E not stripped.
