# T05-02 review, round 1: Needs fixes
Spec: U05-04..U05-12 and UT05-04..07 and PT05-01 match. UT05-47 and UT05-69 are covered only in part (types only); later cards must finish them. Gates: clean, apart from the accepted ownership/UT00-48 failures.
Important 1: evidence.py:76 NumberRef calls math.isfinite on an int -> a raw OverflowError for huge ints (10**400), not a ValidationError. Model input crashes instead of raising a recoverable error.
Important 2: tooling.py:18-20 TOOL_CONTENT_MAX_CHARS was made private, which deviates from §3.9; U05-36 uses the name. REPEAT_NUDGE (T05-03) has the same problem. -> Controller ruling (see ledger).
Minor:
1. SqlLimits.timeout_s accepts True and inf.
2. VectorHit.similarity accepts NaN.
3. NumberCheck.claimed/actual accept bools.
4. UncitedSpan allows end<start and bools.
5. _Frozen is defined twice; tooling imports the private _UtcDatetime.
6. A test uses pytest.raises(Exception) where it should use BudgetExceeded.
7. The Tool/AsyncTool isinstance tests cannot tell sync from async.
8. UT05-47/69 are partial.
9. No test covers a naive VectorHit.opened_at.
Ruling on layout: accept the compact fmt: off in harness/__init__.py and the fmt: skip in evidence.py, because they are budget-driven.
