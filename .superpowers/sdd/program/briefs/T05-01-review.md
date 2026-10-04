# T05-01 review, round 1: Needs fixes
The spec matches U05-01..03 and UT05-01..03. Gates are green, except the expected ownership failures (exactly 25 x OWN010 plus 2 x OWN031, and UT00-48).
Important: herness/core/types/harness/llm.py:206-210 _quantize_cost raises a raw decimal.InvalidOperation for cost_usd >= ~1e22 instead of a ValidationError. That breaks "Errors: None beyond validation" and the ValidationError->OutputValidationError conversion. Fix: catch InvalidOperation and raise ValueError, then add a test.
Minor: lax coercion (step=True -> 1) with no strict mode; request_key has no format pattern; tests live in tests/unit/core (defensible); unchecked type: ignore comments in the tests.
