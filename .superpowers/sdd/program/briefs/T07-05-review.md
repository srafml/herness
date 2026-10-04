# T07-05 review — Write-policy checks (verify agent)

Reviewed commit 742873d (base b18c91a) in worktree agent-a65d277c4bb1cf9a9 against impl 07 §3.6 U07-37..U07-43, §4.2, §11 (UT07-11..17, PT07-08), TH07-01..04/11/23, design 07 §4.2.

### Spec Compliance
- ✅ U07-37 normalize_content: NFKC → ZERO_WIDTH removal → casefold → `\s+`→" " → strip, in spec order (policy.py:71-74). ZERO_WIDTH = {U+200B, U+200C, U+200D, U+2060, U+FEFF} exactly as §3.6 preamble (policy.py:23).
- ✅ U07-37 content_hash / keyed_hash: `ids.sha256_hex(...)[:32]` (R-14), keyed_hash without normalization (policy.py:77-84); 32 lowercase hex asserted.
- ✅ U07-38 find_uncited_numerals = `list(core_numbers.find_uncited(text, allowed))`, no memory-own numeral rule (policy.py:90-92). UT07-12 hits exactly `["41"]` and equals the core scanner result.
- ✅ U07-39 check_markers: duplicate ids, malformed inner text (incl. `?`) → invalid, unknown, unused, `ok` ignores unused (policy.py:110-121). UT07-13 values match.
- ✅ U07-40 InjectionScanner: IGNORECASE compile, immutable tuple, scan = NFKC + zero-width removal + whitespace collapse, sorted indices; scan_payload = content + DFS string values, keys excluded, cap 2,000 (policy.py:127-168). Full-width and zero-width forms verified (test + probe: `ＩＧＮＯＲＥ　all​ previous` flagged).
- ✅ U07-41 check_limits order size.content → size.sql (sql_template, sql) → data.depth (>8, iterative) → size.data (canonical_json UTF-8 bytes) → size.numbers → data.entities → data.required:<field>; PolicyViolation(details={"rule"}), message names rule+limit (policy.py:227-249). Required fields match design 07 §4.2 for all 11 kinds; nullable fields must be present.
- ✅ U07-42 decide_policy: matrix (policy.py:275-295) matches §4.2 row-for-row (16 rows incl. sql_template/qa_pair split), roles by prefix, via, required provenance incl. `data.review_item_id`, `query_ids`/`finding_ids` ≥ 1, flags force pending, expiry from mapping; needs_review == (status == pending). No agent/chat/tool path yields `active` (asserted over all 539 combos).
- ✅ U07-43 agent_confidence (min with fmean) / merge_confidence (min(0.95, 1-(1-a)(1-b))); constants 0.9 / 1.0 / 0.8 / 0.95.
- ✅ Tests: UT07-11..UT07-17 and PT07-08 present, IDs in names/docstrings, pytestmark unit. Re-run: 50 passed; coverage policy.py 100 % line / 100 % branch (188 stmts, 70 branches) ≥ 95 %.
- ✅ Gates re-run: ruff check clean, ruff format --check clean, mypy 0 issues, lint-imports 10 kept 0 broken, check_type_ownership exit 0, check_module_size exit 0 (342/360 lines).
- ✅ Security (probes): depth-50 dict and depth-5,000 list → `data.depth` in microseconds (iterative, no recursion); 1 MB content → `size.content`; 10^6-element flat list → `size.data`; scan inputs bounded by U07-41 before scanning; no regex built from untrusted input (patterns are config, validated by U07-18).
- ⚠️ Cannot verify here: ST07-01/02/03/11/23 end-to-end (owned by T07-08 via U07-50); ReDoS safety of the shipped pattern file (U07-18 validator, not this card).

### Strengths
- Matrix and required-field tables are data, not branching; UT07-16 checks against an independent copy of §4.2 and asserts TH07-02/TH07-23 invariants on every combination.
- All walks (depth, string collection) are iterative with explicit caps; first-failure order tested.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/harness/memory/policy.py:243 — `ids.canonical_json` raises `SchemaViolation("non-finite float")` for NaN/Infinity in `data` (probe: `{"v": float("nan")}` → SchemaViolation, not PolicyViolation). U07-41 Errors lists only PolicyViolation with `details["rule"]`; Python `json.loads` and pydantic `JsonValue` accept NaN, so a tool arg could reach this. Suggest catching SchemaViolation around the canonical_json call and raising `_fail("size.data")` (or a `data.*` rule), plus one test.
2. tests/unit/harness/memory/test_memory_policy.py:452-453 — the `author_ref=None` cases construct Provenance via `model_copy` of a *system* provenance with `author_type` patched; works, but the branch keyed on `missing.get("author_ref", "") is None` is opaque. A named helper (e.g. `_prov_unvalidated`) would make intent clear.
3. herness/harness/memory/policy.py:113 — `ref_ids.count(i)` inside a comprehension is O(n²); harmless (numbers ≤ 20) but `collections.Counter` is clearer.
4. tests/unit/harness/memory/test_memory_policy.py:162 — asserts exact indices `[0, 7]` against the shipped `config/injection_patterns.txt`; any reordering of that file breaks UT07-14. Consider asserting membership of the specific pattern(s) instead.
5. (Spec-level observation, not an implementer defect) U+00AD soft hyphen and other format chars (Cf) are not in the spec's ZERO_WIDTH set, so `ig­nore all previous` evades the scan (probe). Flag to spec owner for TH07-01; implementation follows the spec verbatim.

### Assessment
**Task quality:** Approved
**Reasoning:** Every algorithm step, constant, rule name and matrix row matches impl 07 §3.6/§4.2 verbatim; tests hit the spec's expected values with 100 % coverage and all gates pass. Remaining items are minor hardening/clarity.

Verdict: Approved

## Re-review (fix round 1)

Scope: commit 1f69f4f (742873d..HEAD), Minor findings 1-4 only.

- ✅ M1 resolved: `_data_bytes` (policy.py, new helper before `check_limits`) catches `SchemaViolation` from `canonical_json` and raises `PolicyViolation` rule `size.data`. The check order is unchanged: `data.depth` still runs first, so canonical_json's own too-deep error cannot turn into `size.data`. New parametrized test `test_ut07_15_non_finite_data_is_size_data` covers NaN, +Inf and -Inf, both top-level and nested.
- ✅ M2 resolved: `check_markers` counts duplicates with `collections.Counter`. The output is the same, and UT07-13 still passes.
- ✅ M3 resolved: the named helper `_human_without_author_ref(via)` has a docstring. The branch is now `"author_ref" in missing`, and both author_ref cases are human rows, so behaviour is unchanged.
- ✅ M4 resolved: UT07-14 looks up pattern indices by their text through `_index(scanner, pattern)`, so reordering the pattern file no longer breaks the test.
- M5 (spec gap, U+00AD) was for the spec owner and needed no code change.
- No regressions: 53 tests pass; policy.py coverage is 100 % line and branch (195 statements, 70 branches); ruff check and ruff format --check are clean; mypy reports 0 issues; check_module_size exits 0 (352/360 lines).

Verdict: Approved
