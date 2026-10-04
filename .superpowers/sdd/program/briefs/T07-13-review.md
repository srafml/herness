# T07-13 review — Deterministic compaction steps (verify agent)

Worktree D:\herness\.claude\worktrees\agent-abe1e4ade320b58eb @ d495fac (base 667fcf4). Tree left clean (`git status` empty).

### Spec Compliance
- U07-70 split_groups ✅ (merge-instead-of-move reading accepted, see ⚠️1)
- U07-71 entry_from_result ✅ (table-end rule accepted)
- U07-72 cited_from_group ✅
- U07-73 validate_notes ✅ (free-text query_id / malformed-token rewrite accepted: it enforces the stated postcondition)
- U07-74 deterministic_notes ✅
- U07-75 build_compacted ✅ (>200k split, local user-merge, transcript of user text and orphan results accepted)
- UT07-53..59, PT07-01 (pure part), PT07-02, BT07-04 present; ID naming, docstring prefixes, pytestmark OK.
- §2 module-map row for `_compact_text.py` (L4, 200) added at docs/impl/07-memory.impl.md:80 ✅

⚠️ Cannot fully verify / spec notes
1. U07-70 postcondition conflict: "moved with that group" vs "concatenation is 1..len, in order". Merging the spanned groups (compact_build.py:104-134) is the only reading that satisfies both; accepted. Consequence for T07-14: one late result for an early call merges everything between them into one group, so "keep last K" can keep (almost) the whole history and compaction then cannot shrink. Only pathological histories (spec 05 always places results right after calls). Merged group also carries the first member's step for all of its calls (deterministic_notes lines and ledger steps slightly off). Spec note for impl 07 §U07-70.
2. Claude transcript (compact_build.py:337-347) carries tool results and assistant/user text unescaped and unwrapped, next to the scratchpad TextPart. This is what U07-75 says ("content unchanged"); flag for open question 1 / T07-14 (untrusted wrapper is TH07-16 mitigation for notes, not for the transcript).
3. T07-14 hand-offs from the report are real and must be carried into U07-76: pass the ORIGINAL M0 (not out[0]) or scratchpads nest; in Claude mode ledger the kept groups too (they become text and are rebuilt next time); ledger orphan results with the placeholder "unknown" call. The PT07-01 driver (test_memory_compact_build_props.py:123-151) models exactly this; it is test code, not product code.
4. CompactorLike (D:\herness\herness\harness\hooks.py:65, not in this base) only needs `pressure` and async `on_context_pressure(state) -> list[Message]`; build_compacted returns list[Message] — compatible, no mismatch.

### Verification performed
- `pytest tests/unit/harness/memory -q -p no:logging`: 210 passed. Card tests + coverage: 26 passed, compact_build.py 100% line/100% branch, _compact_text.py 100%/100%. BT07-04 passed.
- ruff check: clean; ruff format --check: clean; mypy: 274 files clean; lint-imports: 13 kept, 0 broken; check_module_size: rc 0; C901/PLR0913/0912/0915 on both modules: clean. Sizes 379/390 and 172/200.
- Purity probe (own script): two builds (local + claude) byte-identical, input dumps equal before/after, zero id() overlap between output messages/parts lists/parts and inputs. Imports of both modules: decimal, json, math, re, dataclasses, pydantic, herness.core.{types,ids,numbers,errors}, memory.policy/working — no logging, clock, random, I/O or LLM.
- Adversarial validate_notes: unknown `q_…` in text -> `[[?]]`; known `q_…` survives intact (its digits are not treated as numerals); `[[n9]]`, `[[ n1 ]]` -> `[[?]]`; valid `[[n1]]` kept; stray `42`, dates -> `[[?]]`; hypothesis query_ids filtered to ledger; model `steps` dropped even when malformed; extra keys -> None; 700x"1" -> fits 600; worst case (all-numeral fields at max length) 7 ms.
- entry_from_result: empty error content, header-only, 80 columns (ledger columns cut to 50), nan/inf cells, repeated ids: no exception, entries per distinct id.
- cited_from_group: `$1.2M`, `-5`, `1,234`, `12%`, half-even `7.2` vs cell 7.25 matched; `0.5` with no cell -> unmatched; invalid `numbers` items ignored.
- Mutation probes (reverted via git checkout): removing the free-text query_id repair, the ReasoningPart filter, group merging, orphan-result transcript lines, the fit re-repair, or unmatched output — each killed by the card tests.

### Strengths
- Clean split between pure steps and the private helper; exact Decimal half-even matching with a float pre-filter (bench 23 ms vs 105 ms).
- `fit` re-repairs after each cut, so a length cut never exposes a numeral (the only place the module cuts model text; cut is on the final text).
- Property tests cover the real invariants (query_ids/cited survive across several rounds, group contiguity, no object reuse, byte-identical rebuild).
- Header `shown=` bound prevents the "rows shown are arbitrary" note line being read as a row of a one-column table.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. compact_build.py:174-177 `_sample` builds sample dicts from ALL columns while `columns` is cut to 50 (compact_build.py:192); an 80-column table with few rows yields 80-key sample rows that name columns absent from the ledger `columns`. Duplicate column names also collapse silently in the dict. Harmless for correctness (strings, not NumberRefs); cut sample keys to the same 50 for consistency.
2. compact_build.py:124-133 merged groups report the first member's step for every call in the group (see ⚠️1); spec note rather than code fix.
3. compact_build.py:44-54 re-exports `QUERY_ID_SCAN_RE`, which the §2 row (07-memory.impl.md:79) does not list as a public name. Either list it or import it in tests from `_compact_text`.
4. compact_build.py:370-379 `head` can exceed Message's 256-part cap (llm.py:83) only if M0 already has ~255 parts or text is > ~50 MB; would raise ValidationError despite "Errors: none". Theoretical.
5. tests/unit/harness/memory/test_memory_compact_build.py:307 `test_ut07_56_missing_progress` sits among the UT07-55 tests; file is 543 lines (tests are outside check_module_size, but ENG 400-line module limit is worth respecting by splitting UT07-58/59 out).

### Assessment
**Task quality:** Approved
**Reasoning:** All six units match the spec (with defensible, documented readings of the self-contradictory U07-70 postcondition and the U07-71 row-end rule); purity, citation survival and TH07-16 repair hold under adversarial probes and mutation testing, and all gates are green with 100% line/branch coverage. Remaining items are minor or hand-offs for T07-14.
