# Review: T03-17 fix round 1 — Primary rules and resolution reference

Fix diff base `46ee5c3` -> head `c594c28` (worktree `agent-affa5a39a5189981a`). Files
changed: `herness/enrich/decide.py`, `tests/unit/enrich/test_decide.py`.

### Findings verification (T03-17-review.md)

- **Critical-1 (`chain_after` mis-slicing when `primary == "jev"` against an
  `"openjev"`-authored chain)** — ✅ Resolved for the reported scenario. The fix computes
  `swapped = tuple(_swap(name) for name in cfg.escalation_chain)` and
  `canonical_primary = _swap(primary)` before locating the slice point
  (`decide.py:97-104`), so `chain_after("jev", cfg=..., deciders=...)` against a chain
  authored as `("openjev", "llm")` now correctly returns `("llm",)`, not `("jev", "llm")`.
  Verified independently by re-running the two new UT03-69 cases and by direct probe
  (`chain_after("jev", ...)` against both `("openjev","llm")` and `("jev","llm")` chains
  each return `("llm",)`, no longer containing `"jev"`). New tests
  `test_ut03_69_chain_after_primary_jev_against_openjev_authored_chain` and
  `..._against_jev_authored_chain` (`test_decide.py:198-213`) cover exactly the composition
  the original finding named (`primary_decider_for` override → `chain_after`).
  ⚠️ **But see new finding below — the fix does not fully close the invariant for chains
  that literally list both `"jev"` and `"openjev"`.**
- **Important-1 (PT03-08 excluded the human branch entirely)** — ✅ Resolved correctly.
  `_human_strategy = st.none() | st.tuples(st.sampled_from(_ANSWERS), st.just(_T1))`
  (`test_decide.py:561`) now draws `human` from the same answer pool as `rows`, so both the
  "confirms" and "corrects" branches of `resolve_pair` are exercised by the property test,
  not just the example tests. `final ⇔ fields set` is now asserted unconditionally
  (`test_decide.py:583`); the `escalated ⇔ decider != primary` half is skipped only when
  `res.decider == "human"` (`test_decide.py:585`) — correctly scoped, since the "confirms"
  branch returns `_final(m, review_status="confirmed")` with `m`'s original decider name
  (not `"human"`), so that branch still gets the full escalated check while only the
  "corrects"/no-machine-result branch (which hard-codes `decider="human"`,
  `escalated=False` per U03-74 step 2) is exempted. No regression; logic traced against
  `resolve_pair` (`decide.py:204-243`) and confirmed sound.
- **Minor-1 (report test count off by one)** — ✅ Resolved. `T03-17-report.md`'s "Fix round
  1" section explicitly corrects the "13" to "12 distinct `test_ut03_71_*` functions"
  (report lines 85-88). The original wrong count at report line 15 is left as-is with the
  correction appended below it rather than edited in place, which still satisfies the
  intent (a future reader sees the correction).
- **Minor-2 (dense one-liner swap expression)** — ✅ Resolved. The inline generator
  expression is now the small nested `_swap(name)` helper (`decide.py:99-100`), used
  consistently for both `cfg.escalation_chain` and `primary` — cleaner than before and
  incidentally what made the Critical-1 fix possible.

### ⚠️ New finding — invariant still breakable with a chain authored to list both `jev` and `openjev`

The fix computes `swapped.index(canonical_primary)`, which finds only the *first*
occurrence of the canonical name in the swapped chain. If `cfg.escalation_chain` literally
contains **both** `"jev"` and `"openjev"` as separate entries (they both canonicalize to
`"jev"` when `deciders.jev.enabled`), the slice starts after the first occurrence and a
second, later occurrence of the same canonical name survives into `after`, so the result
still contains `primary`.

Verified by direct probe against the actual functions in this worktree:

```
chain=("openjev", "jev", "llm"), jev enabled, primary="jev"
  -> chain_after(...) == ("jev", "llm")   # contains "jev" == primary
chain=("jev", "openjev", "llm"), jev enabled, primary="jev"
  -> chain_after(...) == ("jev", "llm")   # same violation, order-independent
```

This is **not** a purely pathological, unreachable authoring:

1. `DecisionsConfig._rules` (`herness/enrich/settings.py:281-287`) only rejects duplicate
   *literal* strings and rejects the chain containing `primary_decider` itself. It does
   **not** reject a chain that lists both `"jev"` and `"openjev"` — they are distinct
   strings, so `len(set(chain)) == len(chain)` passes trivially, and neither name need
   equal `primary_decider` for the chain to be otherwise legal.
2. This exact chain shape, `("openjev", "jev", "llm")`, is already used and asserted valid
   elsewhere in this same test file —
   `test_ut03_69_chain_after_dedupes_and_keeps_llm_last` (`test_decide.py:222-227`, testing
   `primary="laya"` against it). The round-1 fix simply never re-checked this already-
   exercised chain shape against `primary="jev"`.
3. It is directly reachable through the **default teacher-fallback** path of
   `primary_decider_for`, not just an obscure override: U03-70 step 1 sets
   `teacher = "jev"` whenever `"jev" in cfg.escalation_chain and deciders.jev.enabled`
   (`decide.py:66`) — true for `("openjev", "jev", "llm")` — and any question not
   laya-accepted (or with `primary_decider="laya"` unaccepted, the common default) falls
   through to `teacher`. So `primary_decider_for(...)` can return `"jev"` for this chain
   shape via the ordinary default path, and the documented
   `chain_after(primary_decider_for(...), ...)` composition (design 03 §5.7) then violates
   its own "never contains `primary`" postcondition.

This is a genuine, still-open (if narrower) instance of the same invariant violation
Critical-1 named, not a new defect introduced by the fix — the round-1 patch fixed the
single-entry swap case but didn't generalize to a chain with duplicate canonical names.
Given global-constraints' config validation has no rule against this shape and the shape is
already treated as valid elsewhere in this task's own tests, I do not consider this
out-of-scope pathological authoring.

**Recommended fix (cheap, ~1 line):** filter the canonical primary out of `kept` directly
instead of relying solely on the index-based slice, e.g. at `decide.py:106`:
```python
kept = [name for name in after if name != canonical_primary and enabled.get(name, True)]
```
This makes "never contains primary" hold unconditionally regardless of how many times the
canonical name appears in the authored chain, without needing a new config-validation rule.
A UT03-69 case with `chain=("openjev", "jev", "llm")`, `primary="jev"` would pin it down.

### Test run

`PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_decide.py -q -p no:logging`: **32
passed** (re-verified independently, matches report). `ruff check` and
`ruff format --check` on both changed files: clean (re-verified independently).
`decide.py`: 256 lines (budget 260).

### Assessment

**Task quality:** Needs fixes
**Reasoning:** All four round-1 findings (Critical-1, Important-1, Minor-1, Minor-2) are
correctly resolved for the scenarios they named, with no regressions — `resolve_pair`,
`gate`, `Resolution`, and the previously-verified `primary_decider_for` remain correct. But
the Critical-1 fix generalizes only to the single-swap case; a chain authored to list both
`"jev"` and `"openjev"` (a shape config validation permits and this task's own test suite
already treats as valid for other primaries) still lets `chain_after` return a result
containing `primary`, reachable through the ordinary default teacher-fallback composition —
this must be closed (recommended one-line filter above, plus a pinning test) before
approval.
