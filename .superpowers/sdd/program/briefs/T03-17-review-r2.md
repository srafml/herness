# Review: T03-17 fix round 2 — Primary rules and resolution reference

Fix diff base `c594c28` -> head `c88a99e` (worktree `agent-affa5a39a5189981a`). Files
changed: `herness/enrich/decide.py`, `tests/unit/enrich/test_decide.py`.

### Finding verification (T03-17-review-r1.md)

- **⚠️ round-1 finding (`chain_after` still contains `primary` when the chain literally
  lists both `"jev"` and `"openjev"`)** — ✅ Resolved, exactly via the recommended fix.
  `decide.py:106` now reads
  `kept = [name for name in after if name != canonical_primary and enabled.get(name, True)]`
  (previously `kept = [name for name in after if enabled.get(name, True)]`), explicitly
  filtering the canonical primary out of the kept list regardless of how many times it
  appears in the swapped/sliced chain. `.index()` is still used only to find the slice
  start (first, leftmost, occurrence, matching "every member at or before primary's
  position removed"); the new filter independently catches any later duplicate that
  survives the slice.

  Verified independently with a direct probe against the live functions in this worktree
  (not just the new unit tests):
  ```
  chain=("openjev","jev","llm"), jev enabled, primary="jev" -> ("llm",)   # was ("jev","llm")
  chain=("jev","openjev","llm"), jev enabled, primary="jev" -> ("llm",)   # was ("jev","llm")
  chain=("openjev","llm"),        jev enabled, primary="jev" -> ("llm",)  # round-1 case, still holds
  chain=("jev","llm"),            jev enabled, primary="jev" -> ("llm",)  # round-1 case, still holds
  ```
  All four now satisfy "never contains `primary`".

- **No regression** — re-checked the other primaries against the same double-listed chain:
  `chain_after("openjev", cfg=cfg(("openjev","jev","llm")), deciders=...)` still returns
  `("llm",)` and `chain_after("laya", ...)` still returns `("jev","llm")` (the full
  processed chain, as `primary == "laya"` always returns everything) — both unchanged from
  before the round-2 fix, confirming the new filter only removes `canonical_primary` and
  nothing else. The existing `test_ut03_69_chain_after_dedupes_and_keeps_llm_last`
  (`test_decide.py:222-227`, `chain=("openjev","jev","llm")`, `primary="laya"` ->
  `("jev","llm")`) still passes, so the fix does not disturb the other already-tested chain
  shape it shares roots with.

  New tests added: `test_ut03_69_chain_after_primary_jev_against_both_listed_chain_openjev_first`
  and `..._both_listed_chain_jev_first` (`test_decide.py:216-233`), covering both orderings
  of the double-listed chain, both asserting `("llm",)` and `"jev" not in result`. These are
  exactly the pinning cases the round-1 review asked for.

- **Report** — "Fix round 2" section (`T03-17-report.md:127-163`) accurately describes the
  root cause, the one-line fix, the two new tests, and re-verification; consistent with the
  diff.

### Test run

`PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_decide.py -q -p no:logging`: **34
passed** (re-verified independently; 32 prior + 2 new UT03-69 cases, matches report).
`ruff check` and `ruff format --check` on both changed files: clean (re-verified
independently). `herness/enrich/decide.py`: 256 lines (budget 260, unchanged — the fix was a
one-line condition edit plus additive tests).

### Findings

No Critical, Important or Minor findings outstanding. Both prior review rounds' findings
(Critical-1/Important-1/Minor-1/Minor-2 from round 1, and the chain_after
double-listed-chain gap from round 1's own re-review) are now closed with matching tests and
no observed regression.

### Assessment

**Task quality:** Approved
**Reasoning:** The `chain_after` "never contains primary" invariant now holds for every
chain shape checked, including the double-listed `jev`/`openjev` case that survived round
1's fix; the fix is minimal, targeted, and verified with new pinning tests plus an
independent live-code probe, and all card tests plus ruff remain clean.
