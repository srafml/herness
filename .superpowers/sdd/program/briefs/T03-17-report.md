# Report for T03-17: Primary rules and resolution reference

## Status

DONE

## Commit

46ee5c3f9c8cc8bbda4649728681b4551d4ce68c
feat(enrich): add primary rules and resolution reference (T03-17)

## Test summary

`pytest -k "UT03_68 or UT03_69 or UT03_70 or UT03_71 or PT03_08" tests/unit/enrich/test_decide.py`:
30 passed (9 table-driven UT03-68 cases, 5 UT03-69, 3 UT03-70, 13 UT03-71 table-driven
cases, 1 hypothesis PT03-08). Full `tests/unit/enrich/` (155 tests) and full `tests/unit`
(416 tests) also pass. `ruff format .`, `ruff check .`, `mypy` (27 files), `lint-imports`
(9/9 contracts kept), `tools.check_type_ownership` all clean.

## What was built

`herness/enrich/decide.py` (38 -> 252 lines, budget 260):

- `primary_decider_for(q, *, cfg, laya_accepted, deciders)` (U03-70): names the primary
  decider for one question, computing the D7 teacher fallback (jev if in the escalation
  chain and enabled, else openjev if enabled, else llm), resolving the question's
  `primary_decider` override by looking it up in `cfg.questions` by `q.id` (Question, the
  core type, carries no override field itself — only `QuestionConfig` does), and falling
  back to the teacher whenever the wanted backend is disabled or Laya isn't accepted for
  that question.
- `chain_after(primary, *, cfg, deciders)` (U03-71): the escalation members after
  `primary`'s position in `cfg.escalation_chain`, with `openjev` swapped for `jev` when
  `jev` is enabled, disabled members dropped, duplicates removed, and `llm` moved last.
  `primary == "laya"` returns the whole processed chain.
- `gate(p_calibrated, threshold)` (U03-73): `p_calibrated >= threshold`.
- `CachedAnswer` (frozen dataclass: `answer, p_cal, decided_at, version, agreement`) and
  `Resolution` (frozen dataclass per U03-72's field list and order) as named in the brief.
- `resolve_pair(*, human, rows, primary, chain, threshold, in_scope, pending_review)`
  (U03-74): the reference resolution algorithm — ensemble row precedence with its
  laya-disagreement escalation rule, primary-gate-then-chain machine result, human
  confirm/correct override, and the final/queue/out_of_scope split — exactly per the
  brief's algorithm text.

## Deviations (both anticipated by the brief)

1. **`deciders: DecidersSettings` added to `primary_decider_for` and `chain_after`.** The
   brief flagged this for U03-70 (its algorithm reads `deciders.jev.enabled` /
   `deciders.openjev.enabled`, which the R-76 split moved out of `DecisionsConfig` into
   `config/models.yaml`'s `DecidersSettings`). The same gap applies to U03-71's `chain_after`
   — its postcondition needs `jev`/`openjev` `enabled` to do the openjev->jev swap and to
   drop disabled members — so I added the same keyword-only `deciders: DecidersSettings`
   parameter there too, for consistency and because there is no other way to satisfy the
   literal postcondition. Both are documented as deviations in the module docstring and the
   test file's module docstring.
2. **`resolve_pair`'s 7 keyword-only parameters exceed the global 6-argument limit
   (`PLR0913`).** The signature is verbatim from U03-74. I kept it as specified and added
   `# noqa: PLR0913` with a one-line reason rather than restructuring the (binding) unit
   signature.

## Design notes worth flagging

- `primary_decider_for`'s question-override lookup (`q.id` matched against
  `cfg.questions[*].id`) is not literally in the algorithm text but is the only way to
  satisfy "the question's `primary_decider` override" given `Question` (core type) has no
  such field — only `QuestionConfig` (settings) does. Tests cover both the override-present
  and override-absent paths (UT03-68 table).
- `chain_after`'s "member at or before primary's position removed" is computed against
  `cfg.escalation_chain`'s original names/positions (i.e. the slice happens before the
  openjev->jev swap, so removing "openjev" as primary also removes the swapped "jev" at the
  same slot) — this is exercised in UT03-69 (`primary="openjev"`, jev enabled, chain
  `(openjev, llm)` -> `(llm,)`, not `(jev, llm)`).
- `resolve_pair`'s human-branch does not preserve the `escalated == (decider != primary)`
  invariant (a human correction always sets `escalated=False` with `decider="human"`), so
  PT03-08's property test restricts to `human=None`, exercising only the machine-result
  path where the invariant is meant to hold (including the ensemble carve-out).

## Concerns

None outstanding. Both deviations are narrow, match the brief's own guidance for handling
the R-76 signature gap, and are recorded above plus inline in code/test docstrings for the
next reader.

## Fix round 1

**Correction to Test summary above (Minor-1):** the line 15 count "13 UT03-71 table-driven
cases" is wrong; there are 12 distinct `test_ut03_71_*` functions in
`tests/unit/enrich/test_decide.py` (verified via `grep -c "^def test_ut03_71"`). All 12 are
meaningful, distinctly named, and pass; no coverage was actually missing.

Addressed the review findings for T03-17-review.md:

- **Critical-1 (`chain_after` mis-slicing when `primary == "jev"` against an
  `"openjev"`-authored chain):** fixed by computing the swapped/canonical chain first
  (`swapped = tuple(_swap(name) for name in cfg.escalation_chain)`), canonicalizing
  `primary` the same way (`canonical_primary = _swap(primary)`), and locating
  `canonical_primary`'s position in `swapped` before slicing, instead of slicing the
  pre-swap chain by the literal `primary` string. Added two UT03-69 cases:
  `test_ut03_69_chain_after_primary_jev_against_openjev_authored_chain` (chain authored
  with `"openjev"`, primary `"jev"`) and `test_ut03_69_chain_after_primary_jev_against_jev_authored_chain`
  (chain already literally lists `"jev"`), both asserting the result equals `("llm",)` and
  never contains `"jev"`. Verified the existing
  `test_ut03_69_chain_after_primary_openjev_leaves_llm` case (primary `"openjev"` itself,
  jev enabled) still passes with the fix, and traced other realistic chain shapes
  (dedup, disabled members, primary at the end) by hand against the new implementation —
  all consistent with the invariant that the result never contains `primary`.
- **Important-1 (PT03-08 excluded the human branch):** extended the hypothesis strategy
  with `human: tuple[str, datetime] | None` (`_human_strategy = st.none() |
  st.tuples(st.sampled_from(_ANSWERS), st.just(_T1))`), asserted `final ⇔ fields set`
  unconditionally for every drawn `human`, and now only skip the `escalated ⇔ decider !=
  primary` half of the property when `res.decider == "human"` (the case genuinely in
  tension with U03-74 step 2's hard-coded `escalated=False`).
- **Important-2 (`resolve_pair` 7 kw-only args, plan-mandated):** no action per the review
  — kept as is with the existing `# noqa: PLR0913` and inline reason.
- **Minor-2 (optional polish, long swap one-liner):** the swap is now a small nested
  `_swap` helper function inside `chain_after` rather than an inline generator
  expression, which incidentally also resolves this while implementing Critical-1.

Verification: `pytest tests/unit/enrich/test_decide.py -q -p no:logging` — 32 passed (30
prior + 2 new UT03-69 cases; PT03-08 unchanged as a single hypothesis test). `ruff check`
and `ruff format --check` on both changed files clean. `mypy herness/enrich/decide.py`
clean. `lint-imports` — 9/9 contracts kept. `decide.py` is 256 lines (budget 260).

## Fix round 1 status

DONE

## Fix round 2

Addressed T03-17-review-r1.md's finding: `chain_after`'s Critical-1 fix from round 1 used
`swapped.index(canonical_primary)`, which finds only the *first* occurrence of the
canonical name. When `cfg.escalation_chain` lists both `"openjev"` and `"jev"` (e.g.
`("openjev","jev","llm")` or `("jev","openjev","llm")`) and `jev` is enabled, both entries
canonicalize to `"jev"`, so the second occurrence survives the slice:
`chain_after("jev", ...)` returned `("jev","llm")` instead of `("llm",)`, violating "never
contains `primary`". This config shape passes `DecisionsConfig._rules` validation and is
reachable via `primary_decider_for`'s teacher fallback (`teacher = "jev"` when `jev` is in
the chain and enabled), so a caller composing `chain_after(primary_decider_for(...), ...)`
against such a chain would hit it.

Fix (`herness/enrich/decide.py` ~line 108): changed
`kept = [name for name in after if enabled.get(name, True)]` to
`kept = [name for name in after if name != canonical_primary and enabled.get(name, True)]`
— filtering out `canonical_primary` explicitly makes the "never contains `primary`"
invariant hold regardless of how many occurrences of `primary`'s canonical form appear in
the slice. `.index()` still picks the first occurrence to determine the slice start
(matching "every member at or before primary's position removed" using the first,
leftmost, position), and the added filter now catches any later duplicate.

Added two UT03-69 cases in `tests/unit/enrich/test_decide.py`:
`test_ut03_69_chain_after_primary_jev_against_both_listed_chain_openjev_first` (chain
`("openjev","jev","llm")`) and `..._both_listed_chain_jev_first` (chain
`("jev","openjev","llm")`), both with primary `"jev"` and jev enabled, asserting the
result is `("llm",)` and never contains `"jev"`.

Verification: `pytest tests/unit/enrich/test_decide.py -q -p no:logging` — 34 passed (32
prior + 2 new UT03-69 cases). `ruff check` and `ruff format --check` on both changed files
clean. `mypy herness/enrich/decide.py` clean. `lint-imports` — 9/9 contracts kept.
`decide.py` is still 256 lines (budget 260; net change was a one-line filter-condition
edit plus additive test-only lines).

## Fix round 2 status

DONE
