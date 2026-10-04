# T04-01 re-review, fix round 2

Reviewed commit: `b5871bb`, the fix diff `254df03..b5871bb`. The review was read-only. Tools ran from the worktree's `.venv\Scripts`, because `uv run` is blocked by the conflict markers in the main checkout's `pyproject.toml`.

## What I ran

- **Round-1 probe sets, re-run.**
  - `r1_probe.py`: 50 queries, plus the `rows_equivalent` inf/nan matrix and the sample check.
  - `r1_probe2.py`: 8 queries.
- **New probe set `r2_probe.py`: 35 queries, 34 ran.**
  - The unsupported types at every depth: top level, list, MAP key, MAP value, STRUCT, single-member UNION, and multi-member UNION when the selected member is a different one.
  - Multi-member UNION with a nested member: STRUCT, `INTEGER[2]`, UNION and `DECIMAL(38,0)`.
  - A path-dependent UNION nested inside a STRUCT and inside a MAP.
  - Multi-member UNION with a path-independent member: TIMESTAMP_S, TIMESTAMP_MS, TIMESTAMP, BOOLEAN, nan DOUBLE, DOUBLE 3.0, UBIGINT, JSON, ENUM, a quoted tag, TIME and TIME_NS.
  - NULL `BIT` and empty `BIT[]`.
- **Card acceptance command:** `pytest -m unit -k "UT04-0 or UT04-11 or UT04-12 or PT04-0 or PT04-11 or ST04-12"` gives **133 passed**, 190 deselected.
- **`tests/unit/metrics` with branch coverage:** 215 passed. `_encode.py`, `_encode_nested.py` and `evidence.py` are each at 100% line and 100% branch.
- **Non-slow unit and integration suite:** 320 passed, 4 deselected, when run with `PYTHONUTF8=1`. Without that setting, ST00-10 fails on this machine. The cause is the environment: the Windows code page cannot decode the `lint-imports` output, so `stdout` is `None`. It is not related to T04-01.
- **Static gates:**
  - `ruff format --check`: clean.
  - `ruff check`: clean.
  - `mypy` with the project config: 18 files, no issues.
  - `lint-imports`: 6 kept.
  - `check_type_ownership`: exit 0.
- **Vector generator:** re-run to a scratch path, its output is byte-identical to `tests/fixtures/result_hash_vectors.json`.
- **Line counts:** `_encode.py` is 277 of 280. `_encode_nested.py` is 127.

## Status of each earlier finding

| Finding | Status | Evidence |
|---|---|---|
| **I-3** multi-member UNION with a HUGEINT, UHUGEINT or INTERVAL member | ✅ | See the I-3 notes below the table. |
| **M-8** BIT and BIGNUM/VARINT | ✅ (fail closed) | `UNSUPPORTED_TYPES` is at `_encode_nested.py:22-24` and is checked at `_encode.py:173-174`. BIT, BITSTRING (which reports as BIT) and BIGNUM raise on both paths at top level, in a list, in a STRUCT, as a MAP key, as a MAP value and in a single-member UNION. |
| **M-9** TIMETZ drops its offset | ✅ (fail closed) | Confirmed: Arrow returns `datetime.time(12, 0)` with no tzinfo, so no encoding that includes the offset can be the same on both paths. TIMETZ raises on both paths at top level, in a STRUCT, as a MAP value and as a UNION member. |
| **M-10** `tests/support/__init__.py` | ✅ | Added, with a docstring only. The `tests.support.result_hash_vectors` import and `python -m` both still work. |

**I-3 notes.**
- **The decision is now made from the member types.** `_union` (`_encode_nested.py:115-122`) raises `unsupported_type(norm)` when any member type is:
  - in `_PATH_DEPENDENT` (HUGEINT, UHUGEINT, INTERVAL or an unsupported type);
  - a `DECIMAL(...)`;
  - compound, meaning STRUCT, MAP, UNION or a list/array.
- **All 4 of my round-1 I-3 probes now raise on both paths:** `UNION(h HUGEINT, s VARCHAR)`, the UHUGEINT variant, the INTERVAL variant and the list-wrapped variant.
- **Both paths also raise when the selected member is a different one.** Example: `s := 'x'` in `UNION(h HUGEINT, s VARCHAR)`. The same holds inside a STRUCT and inside a MAP.
- **Every path-independent multi-member UNION I probed encodes to the same text on both paths.** That is 14 cases, including UUID, TIMESTAMP_NS, BLOB and TIME_NS.
- **The new test runs on both paths.** `test_ut04_01_path_dependent_types_fail_closed_on_both_paths` (`tests/unit/metrics/test_metrics_encode.py:228-243`) runs real DuckDB fetchall and Arrow values through the encoder.

## Probe summary

- **Round-1 probe sets (58 queries).** Every case gives equal text or raises on both paths. In round 1, 5 of these cases raised on only one path or gave different text: HUGEINT UNION, UHUGEINT UNION, INTERVAL UNION, BIT and BIGNUM. All 5 now raise on both paths. Every I-1 nested case that was equal in round 1 is still equal. The `rows_equivalent` matrix is unchanged, with I-2 still correct in both directions.
- **Round-2 probe set (34 queries that ran).** 0 cases differ between the paths. 1 query had a setup error in my SQL (`'12:00+02'::TIMETZ` without seconds).

## Judgements

**Is failing closed on BIT, BIGNUM and TIMETZ at any depth an acceptable spec delta? Yes.**
- For these types, U04-01 step 14 ("encode by Python type") cannot meet the U04-01 postcondition that fetchall and Arrow give equal tokens.
  - BIT and BIGNUM: fetchall gives text, but Arrow gives DuckDB-internal bytes.
  - TIMETZ: Arrow loses the offset, which is data loss, so no rule can recover it.
- Decoding the BIT and BIGNUM bytes would tie `result_hash` to DuckDB's storage format across upgrades. That would be worse than a loud `SchemaViolation` at build time.
- Metric templates do not select these types, so the practical impact is nil. A metric author who does select one gets a clear error.
- **Wording for the spec delta.** The delta should say the rule applies to non-NULL values. A NULL cell or member, and an empty list or MAP of these types, still encode as `null`, `[]` or `{}`. That is equal on both paths, so parity is not affected, but a column fails only once it holds a real value.

**Is failing closed applied consistently, so that one path never encodes while the other raises? Yes.**
- Every raise is decided from the column or member type string, which is the same on both paths. It never depends on the Python value.
  - `_encode_typed` checks `norm in UNSUPPORTED_TYPES` before any value is read.
  - `_union` checks every member type before it looks at the value.
- As a result, no value shape can make only one path raise.
- Across 93 probe queries, every raising case raised on both paths, with the same message.
- The `_PATH_DEPENDENT` set is deliberately conservative. For example, `s := 'x'` in `UNION(h HUGEINT, s VARCHAR)` raises. That is the correct trade-off for an untagged value.

**Module-map row for `_encode_nested.py`: approve with budget 130.** The file is 127 lines, 100% covered, and imports only `herness.core.errors`. That meets the L3 contract, and `lint-imports` keeps all 6 contracts. The row proposed in round 1 is out of date: its public names and its budget of 120 no longer match the module, and the round-2 report does not restate the row. The controller should record:

> `herness/metrics/_encode_nested.py` | Type-driven STRUCT/MAP/UNION member encoding; fail-closed types | `encode_nested`, `member_types`, `split_top`, `COMPOUND_PREFIXES`, `UNSUPPORTED_TYPES`, `unsupported_type` | L3 | — | 130

A budget of 130 leaves 3 lines of headroom. That is tight, but it is enough for a private helper whose scope is now closed.

## Issues

### Critical
None.

### Important
None.

### Minor

**M-11. The module-map row proposed for `_encode_nested.py` is out of date.** It is in `T04-01-report.md`, under the round-1 I-1 section.
- The proposed budget of 120 is below the file's 127 lines.
- The export list leaves out `COMPOUND_PREFIXES`, `UNSUPPORTED_TYPES` and `unsupported_type`. `_encode.py:172-176` uses all three.
- This is a documentation fix for the controller. Use the row given under Judgements. No code change is needed.

**Observation, not a finding (recorded for impl 05 and impl 06).** Path-independent multi-member UNION values are encoded without their tag, so values from different members can produce the same token:
- `UNION(i INTEGER, d DOUBLE)`: `3` and `3.0` both encode as `3`.
- `UNION(s VARCHAR, t TIMESTAMP)`: a VARCHAR holding an ISO timestamp can encode the same as a real TIMESTAMP.

Such values then hash the same. This is inherent in step 14 and is symmetric between the paths, so parity holds. It is a limit on how precise the hash is. It is not a bug.

## Assessment

**Verdict: Approved.**

- I-3, M-8, M-9 and M-10 are resolved.
- I found no regressions. Coverage is 100%, the vectors are byte-identical, the gates are green and the acceptance command gives 133 passed.
- Failing closed is decided from the types, so both paths always raise together. It is an acceptable spec delta against step 14.
- The only remaining item is the module-map row text (M-11), which the controller records. No code change is needed.
