# T04-01 re-review, fix round 1

Reviewed commit: `891b6ba`, the fix diff `9ff27a2..891b6ba`. This was a read-only review.

What I ran:
- A 50-query `fetchall`-vs-Arrow probe (`scratchpad/r1_probe.py`, `r1_probe2.py`).
- The card's literal acceptance command: **134 passed, 188 deselected, no warnings**.
- Coverage of `_encode.py`, `_encode_nested.py` and `evidence.py`: 100% line and 100% branch.
- `ruff check` and `mypy` on the changed files: both clean.
- The generator, re-run to a scratch path: its output is byte-identical to the committed fixture.

## Status of each earlier finding

| Finding | Status | Evidence |
|---------|--------|----------|
| I-1 fetchall/Arrow parity for nested values | ✅ for every case in the original review. ❌ one residual case in multi-member UNION, see I-3. | All four original probes now give equal text: `{"h":5}`, `{"u":5}`, `{"m":{"k":1}}` and `{"k":1}`. Equal text also for: HUGEINT max inside a nested STRUCT; `MAP(HUGEINT,HUGEINT)`; MAP keys of type `INTEGER[]` or STRUCT; MAP of STRUCT(HUGEINT); STRUCT of MAP of `MAP[]`; quoted and `""`-escaped STRUCT names; an ENUM containing `,`, `''` or `)`; DECIMAL, UUID, TIMESTAMP_NS, TIMESTAMPTZ, INTERVAL, BLOB, TIME and inf/nan inside a STRUCT; `INTEGER[2]` inside a STRUCT; NULL members; an empty MAP; `MAP[]`; MAP keys of type DOUBLE, DATE or BOOLEAN. |
| I-2 inf in `rows_equivalent` | ✅ | `evidence.py:109-112`. Each pair below was checked in both argument orders: inf vs 1.0, inf vs -inf, nan vs 1.0, 1e308 vs inf and nan vs inf are all False. inf vs inf, -inf vs -inf and nan vs nan are all True. UT04-11 asserts both directions. |
| M-1 U04-07 spec deltas | ✅ | Tie-break, non-finite values and NULL are written up in the report for impl 05. |
| M-2 test IDs | ✅ | `test_ut04_09_constants` matches spec row 2038 (it asserts `core.incident` is not writable). `test_ut04_05_accumulator_sample_of_120_rows` now tests the 50-of-120 ascending-digest sample. |
| M-3 integral float in samples | ✅ (kept on purpose, justification accepted) | U04-06 defines the sample value as the value of its `.9g` token, and `3` and `3.0` are the same JSON number. Consumers of `meta.evidence` should be told this (a note for impl 05 and impl 06). |
| M-4 PT04-11 symmetry | ✅ | `b` is now a permutation of `a` with the DOUBLE cells perturbed by 0, 1e-12, 1e-7 or 1e-3, so symmetry is exercised on pairs near the boundary. The test also keeps an independently drawn list. |
| M-7 generator committed | ✅ | The generator is `tests/support/result_hash_vectors.py`. Re-running it reproduces the fixture byte for byte. UT04-02 pins the fixture's `(id, name, sql)` rows to the generator's `VECTORS`. The test-first process note is acknowledged. |

## Judgement on the builder's new concerns

- **`_encode_nested.py` has no module-map row.** Acceptable.
  - The file is 116 lines, 100% covered, and imports only `herness.core.errors`, so the L3 contract holds.
  - The split keeps `_encode.py` at 277 of its 280-line budget.
  - The controller should record the proposed §2 row (`encode_nested`, `member_types`, `split_top`, L3, budget 120) as a spec delta.
- **Spec deltas to U04-01 step 14:**
  - **Type-driven STRUCT, MAP and UNION.** Acceptable. It is required by the U04-01 postcondition.
  - **`str(key)` for dict keys under unknown types.** Acceptable. It only reaches types that have no type-driven rule.
  - **`LookupError` added to the wrapped errors.** Acceptable. The MAP `value["key"]` lookup can raise `KeyError`, and the error is still wrapped `from` the original.
  - **"Multi-member UNION fails closed."** Not accurate as implemented; see I-3. The delta text must say what the code actually does once I-3 is fixed.

## Issues

### Critical
None.

### Important

**I-3. A multi-member UNION with a HUGEINT, UHUGEINT or INTERVAL member still breaks parity. `fetchall` encodes the value, but Arrow raises.** Location: `herness/metrics/_encode_nested.py:99-107`.

`_union` fails closed based on the Python type of the value, not on the member types. `fetchall` gives an `int` for HUGEINT and a `timedelta` for INTERVAL, and neither is in `_AMBIGUOUS`, so those values are encoded. Arrow gives a `Decimal` and a `MonthDayNano`, which is a tuple subclass, so those raise.

My probe results:

| Query | `fetchall` | Arrow |
|-------|------------|-------|
| `union_value(h := 5::HUGEINT)::UNION(h HUGEINT, s VARCHAR)` | `5` | `SchemaViolation` |
| `union_value(h := 5::UHUGEINT)::UNION(h UHUGEINT, s VARCHAR)` | `5` | `SchemaViolation` |
| `union_value(i := INTERVAL 1 MONTH)::UNION(i INTERVAL, s VARCHAR)` | `2592000` | `SchemaViolation` |
| `[... HUGEINT union ...]` (the same value inside a list) | `[5]` | `SchemaViolation` |

- **Impact:** the build (`fetchall`) records a `result_hash` that the harness (Arrow, `iter_batch_rows`) can never reproduce. The query fails every Verifier re-run, even though it is correct.
- **Postcondition:** this violates the U04-01 postcondition, which is the subject of I-1.
- **Spec delta:** it contradicts the stated delta "multi-member UNION values of ambiguous type fail closed".
- **Test gap:** `test_ut04_01_ambiguous_union_fails_closed` (`tests/unit/metrics/test_metrics_encode.py:212-216`) uses only synthetic values. `NESTED_SQL` has no multi-member UNION with a HUGEINT or INTERVAL member.
- **Fix:** decide by type. When `len(types) > 1` and any member type is HUGEINT, UHUGEINT, INTERVAL, DECIMAL, STRUCT, MAP, UNION or an array, raise `SchemaViolation` whatever the value is. Otherwise keep the Python-type path.
- **Test:** add the HUGEINT probe above to a DuckDB parity test. Both paths must give the same text, or both must raise.

### Minor

**M-8. BIT and BIGNUM (VARINT) break parity, both at top level and nested.** This existed before this round, was not in the original review, and is plan-mandated by step 14's "any other type: encode by Python type".
- `fetchall` gives `str` and Arrow gives `bytes`. `'101'::BIT` encodes as `"101"` from `fetchall` but `"Bf0="` from Arrow. `1::VARINT` encodes as `"1"` from `fetchall` but `"gAABAQ=="` from Arrow.
- Location: falls through to `herness/metrics/_encode.py:173` → `_encode_python`.
- Suggested fix, with a spec delta: fail closed for BIT and BIGNUM, or add type-driven rules for them. This can be a follow-up; it is outside this round's scope.

**M-9. TIMETZ drops its offset.** This existed before this round.
- A `TIME WITH TIME ZONE` value is encoded through `_encode_python` → `_time` (`_encode.py:90-92`), which ignores `tzinfo`.
- As a result, `12:00+02` and `12:00+05` hash equal. Both paths agree on the text, so this is not a parity bug.
- Suggested fix: fail closed, or record it as a spec delta.

**M-10. `tests/support/` has no `__init__.py`.** `from tests.support.result_hash_vectors import VECTORS` (`tests/unit/metrics/test_metrics_evidence.py:16`) depends on implicit namespace-package resolution. It works today, but the spec §11 helper location should have a package marker, to match the rest of `tests/`.

## Assessment

**Task quality:** Needs fixes.

**Reasoning:**
- I-2, M-1, M-2, M-3 (kept on purpose), M-4 and M-7 are resolved.
- I-1 is resolved for every case the original review probed. The new nested encoder holds parity across the 45 other nested cases I tried.
- However, the multi-member UNION fail-closed is based on the value's Python type, so a HUGEINT, UHUGEINT or INTERVAL member still encodes on one path and raises on the other (I-3). This is a one-line fix to decide by type, plus one DuckDB parity test.
