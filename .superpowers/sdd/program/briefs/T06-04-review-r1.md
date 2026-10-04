# T06-04 re-review, fix round 1 (commit a6f5b58 on 6e34a6b)

Scope: Minors 1-3 of T06-04-review.md. Minors 4 and 5 are parked by the controller.

## Evidence (re-run by reviewer)
- `pytest tests/unit/harness/test_budget.py tests/unit/harness/test_gates.py --cov-branch`: 59 passed in 1.85 s. budget.py 100% (100 stmts, 24 branches). gates.py 99% (the only partial is 43->exit, the defensive branch that was already there).
- ruff check: clean. ruff format --check: 4 files already formatted. mypy on the 4 files: no issues.
- Lines: budget.py 170/170, gates.py 142/170. Working tree is clean and HEAD is a6f5b58.
- Probe `TaskSlots.for_run`: (6,1.5)->9, (5,1.1)->6, (1,0.1)->1, (100,1.1)->110, (3,4.0)->12, (7,1.3)->10. All are exact.

## Prior findings
1. Minor 1, float ceil in `TaskSlots.for_run` (gates.py:124-126): **Resolved.** The product is now `Decimal(n) * Decimal(str(o))`, the same pattern as `new_phase_budgets`. Regression test: `test_ut06_20_for_run_exact_product_does_not_round_up` (100, 1.1 -> 110).
2. Minor 2, exponent-form decimals in snapshot (budget.py:97-107): **Resolved.** `cost_cap`, `cost_used` and `cost_remaining` all use `format(d, "f")`. Regression test: `test_ut06_16_snapshot_decimal_strings_are_plain` ("10", "0.0000001", "9.9999999"). The round-trip test still passes because `_parse_cost` accepts the plain form.
3. Minor 3, restore checked only 7 of 12 keys, and `bad` was built on every call (budget.py:124-129): **Resolved.** `_EXTRA_KEYS` covers the 5 derived keys, and presence is checked through `absent`. Together with the name, the 3 counters, cost_used and the 2 flags, all 12 keys are now required. The derived values are still only checked for presence, not validated. That is correct because they are recomputed and not read. `SchemaViolation` is now built only at the raise sites. `test_ut06_16_missing_key_is_schema_violation` is parametrized over all 12 keys, and the ledger stays untouched on failure (existing test).

## The `# noqa: EM102` question
The builder added `# noqa: EM102` at 2 raise sites (budget.py:127, 129) so the file stays within 170 lines.

Ruling: **acceptable as Minor, not blocking.** The rule is:
- global-constraints.md:9 says to assign the message to `msg` before `raise`.
- global-constraints.md:10 allows `# noqa: <code>` *with its reason* only for a finding the plan did not foresee.

These two suppressions do not meet that bar:
- They carry no reason. The one precedent, `herness/core/redact.py:206`, has `- entity type is the message`.
- EM102 on an f-string raise is a foreseen finding, not an unforeseen one.
- A compliant form that adds no lines exists. It merges the flag check into the single condition and keeps one `msg` and one `raise`, all within 100 columns:

```python
        flags = (snap.get("exhausted"), snap.get("cost_cap_reached"))
        absent = any(k not in snap for k in _EXTRA_KEYS) or any(type(f) is not bool for f in flags)
        if snap.get("name") != self.name or len(counts) != len(_INT_KEYS) or cost is None or absent:
            msg = f"budget snapshot invalid: run_id={self.run_id}"
            raise SchemaViolation(msg)
```

That is 5 lines in place of the current 5 (`absent` line, if/raise, if/raise). budget.py stays at 170. Behaviour is identical because both paths raise the same exception before any mutation.

The impact is style only: the message is correct and there are no secrets, so this does not justify another fix round alone. It is recommended if the card is touched again, or as a one-commit follow-up.

## New findings
- Critical: none.
- Important: none.
- Minor:
  - N1. herness/harness/budget.py:127,129: `# noqa: EM102` without a reason, where a compliant line-neutral form exists (see above). Recommended fix: the 5-line rewrite above.
- No regressions found. The diff touches only the 4 scoped files.

## Verdict
**Approved.** Minors 1-3 are resolved with regression tests. N1 is a non-blocking style Minor, left to the controller: fix it now or park it.
