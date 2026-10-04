# T06-07 review: Finding rules (verify agent)

Commit 5b6e60a (base f3053da). Tests re-run for one focused doubt: `pytest tests/unit/harness/test_harness_findings.py` gives 27 passed. A scratch Hypothesis run of the PT06-02 generator under the commit settings (200 examples, derandomized) was also done (see I-1).

### Spec Compliance
- ❌ Issues found: PT06-02 hardly tests the "bijection ⇒ no errors" direction under the commit profile (I-1). All other units and test rows are compliant.

| Item | Status | Notes |
|------|--------|-------|
| U06-47 extract_markers | ✅ | Returns `parse_markers(text)` ids and malformed inner text. No local regex. Inner text is the right choice because the message template is `malformed marker [[x]]` (findings.py:52-55). |
| U06-47 validate_markers | ✅ | Order is malformed, then duplicate, then marker with no number, then unused (gated by `require_all_used`). All four message strings match the spec exactly (findings.py:80-86). Malformed markers are reported once per occurrence. The id-based errors are reported once per id. |
| U06-49 normalize_objective | ✅ | NFKC, then lower case, then `split()`/join to collapse whitespace, then `rstrip(".!?;: ")` (findings.py:93-96). |
| U06-49 compute_dedup_key | ✅ | Builds the 7-element array in spec order with sorted ids, ISO-or-null periods and the normalised objective. Then `canonical_json` and `sha1(..., usedforsecurity=False).hexdigest()[:16]` (findings.py:99-114). |
| U06-50 EntityCatalog | ✅ | The allowlist map matches the spec's 6 tables and columns exactly (findings.py:37-46). Identifiers come only from the map. Ids are bound via `IN (SELECT unnest(?))` (findings.py:178-186). `run` uses `get_run` for each uncached id (findings.py:173-175). The cache is per instance, keyed `(entity_type, id)`. There is at most one warehouse query per call, and cached ids are not re-queried (findings.py:159-170). |
| U06-143 impact_usd | ✅ | `Decimal(str(value))` over usd numbers only, `max(..., default=Decimal("0"))`. Positional-only argument (findings.py:120-123). |
| UT06-05 | ✅ | Max usd value, ignores a larger non-usd value, returns zero when there is no usd number. |
| UT06-31 | ✅ | Table covers malformed, duplicate, unknown and unused, including a combined row that checks order. `require_all_used=False` is covered. |
| UT06-33 | ✅ | Case, whitespace and id-order variants give equal keys. Scope, type, period, role and specialty each change the key. |
| UT06-34 | ✅ (carry-over) | Uses a local tmp DuckDB instead of T11-17 `tiny_build` (known and accepted). One query per call is asserted, and so is no query when everything is cached. Covers all 6 types, bound ids, and `run` via the ops store plus a get_run call count. |
| PT06-01 | ✅ | Covers entity permutation, random ASCII case, mixed whitespace (including U+3000, which exercises NFKC) and trailing-punctuation variants. |
| PT06-02 | ⚠️ weak | The property is stated correctly (both sides of the iff), but the generator almost never produces a non-trivial bijection. See I-1. |
| Layering / types | ✅ | Harness (L4) importing `herness.store.ops.runs` is a downward import, and the report says import-linter passes. Domain types come from `herness.core.types` only. The local `_Rows`/`_Cursor` Protocols are private adapters. |
| Size | ✅ | 186 lines against a budget of 260. |

Builder's four spec readings:
1. SELECT of id and name: sound. Identifiers still come only from the map, and it is still one query per call. It lets `missing` and `names` share one cache.
2. NULL name maps to the id: sound. The entity still counts as found, which keeps `missing` and `names` consistent. No postcondition is broken.
3. Trailing punctuation and spaces stripped together: sound, and arguably required. The spec's "stripped" postcondition would otherwise fail for input like `"a ."`, which would become `"a "`.
4. 50-id cap left to callers: sound. `EntityScope.entity_ids` has max_length 50, broker step 6 enforces the cap, and "≤ 50 ids" is a complexity note rather than a check the unit owns.

- ⚠️ Cannot verify from diff: the report's claims that ruff, mypy, lint-imports, type ownership and coverage (100% line and branch) pass. I only re-ran the card's test file.

### Strengths
- Tight and readable. There are no regexes, and markers go through the single parser required by R-16.
- `_ENTITY_TABLES` is a `MappingProxyType`, and the SQL is built only from its values. An injection-shaped id is asserted to be bound, not inlined.
- The cache design (None means missing, a NULL name falls back to the id) avoids ambiguity. Unknown entity types are refused before any query.
- Tests check real behaviour: query counts come from a counting wrapper around the real read-only `DuckWarehouse`.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
- **I-1 PT06-02 generator almost never produces a non-trivial bijection** (tests/unit/harness/test_harness_findings.py:356-377).
  - I replayed the same strategies under the commit settings (max_examples=200, derandomize=True). Only 7 of 200 examples had bijection=True, and only 1 of those had a non-empty number set. The rest were empty text with empty numbers.
  - So the "markers and ids form a bijection ⇒ errors empty" half of the property is effectively unexercised. Almost every example tests only the "not a bijection ⇒ errors present" half. The card's acceptance check depends on this property.
  - Fix: mix in a constructive branch. For example, draw a unique subset of `_POOL` and build text that references each id one or more times with filler between, optionally applying one mutation (drop a marker, add an unknown marker, duplicate a number id, insert a malformed token). Or use `st.one_of(bijection_case, random_case)`. Add a `hypothesis.event` or target, or an assertion in a sibling test, that non-empty bijections occur.

#### Minor (Nice to Have)
- **M-1** findings.py:161-163 raises a bare `ValueError("unknown entity type")`. The spec defines no error for this, and `ScopeEntityType` is a Literal, so this is defensive only. Consider the project error type if ENG-STANDARDS prefers one. It is acceptable as is.
- **M-2** findings.py:173-175: for `run`, one `get_run` call is made per id, so a call with several run ids issues several ops reads. This is exactly what the spec says ("get_run per id"). I note it only because the class docstring's "one query per call" applies to the warehouse only. The docstring already says so.
- **M-3** test_harness_findings.py:331-339 (`fake_get_run`): the test ids `r1` are not valid run-id format. That is harmless because `get_run` is patched, but using `run_...` ids would keep the fixture realistic.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation matches every unit spec exactly, and the builder's four readings are sound. However, PT06-02, which the card's acceptance check depends on, practically never exercises the bijection ⇒ no-errors direction under the commit profile (1 non-trivial case in 200). It needs a generator that builds bijection cases.

## Re-review round 1 (commit 7f4a100, test file only; scope I-1)

Checked by re-running `pytest tests/unit/harness/test_harness_findings.py -k pt06_02 --hypothesis-show-statistics --durations=3`, which uses the commit profile loaded by the root conftest: 2 passed in 0.60 s.

- **I-1: ✅ resolved.**
  - PT06-02 now draws from `st.one_of(_bijection_case(), _random_case())`.
  - Hypothesis statistics show 200 passing cases and 60 invalid ones. The event shares are 25.77% "non-empty bijection" and 51.15% "other".
  - Those shares are of all 260 cases, invalid ones included. That is why they add up to 76.9% (200/260). It answers the open question in the builder's report.
  - That comes to about 67 non-empty bijections in 200 valid examples, compared with 1 before. Both directions of the iff are now exercised.
  - The mutations (malformed marker, duplicate number id, dropped number, unknown `[[n99]]`) produce near-misses that must report errors.
  - `drop_number` on a single-id case leaves no numbers while markers remain. That is correctly a non-bijection, reported as "marker has no number".
  - The iff assertion is unchanged, and `_is_bijection` is the same predicate as before.
- **New `test_pt06_02_strategy_generates_non_empty_bijections`: ✅ sound and fast (0.16 s).**
  - `find` uses the loaded commit profile, which is derandomized, so the result is deterministic.
  - It shows the strategy produces a bijection with at least 2 ids that validates with no errors.
  - It shows the strategy produces a near-miss with markers that gets errors.
  - It guards against the generator regressing back to trivial cases.
- **No new issues.** The roughly 23% invalid-case rate costs nothing that matters at 0.26 s.
- M-1 to M-3 are parked by ruling and were not re-reviewed.

**Task quality:** Approved
**Reasoning:** I-1 is fixed. PT06-02 now meaningfully exercises both directions of its property under the commit profile, and the added `find` test is deterministic and quick.
