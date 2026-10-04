# T00-11 review: Traceability check (U00-56)

**Verdict: Needs fixes.** There are two Important items. Both are small, and neither is a defect in the tool itself.

Scope reviewed: commit 42d3448 (the diff file). Focused checks run in the worktree:
- `python -m tools.check_traceability --root .` exits 1: 134 TR001, 5 TR002, 6 TR004, 66 TR005, 3 TR007, 0 TR008. Summary line: `defined=3950 referenced=3816 implemented=107`.
- `--require-implemented 00` reports only the TR009 items and TR007 items the build report lists.
- `pytest tests/unit/tools/test_check_traceability.py tests/integration/repo/test_check_scripts.py` gives 8 passed and 1 xfailed.
- Spot checks of the doc findings (02:2659 U02-114/115 in a multi-ID heading; 03:2978 U03-78 in a module-map first cell; 03:3296 TH03-17 with no ST; 04:2065 a range-defined test row) are all true positives under the spec algorithm read literally.

### Spec Compliance

| Requirement / test row | Status | Notes |
|---|---|---|
| U00-56 signature `main(argv: Sequence[str] \| None = None) -> int`, `--root PATH`, `--require-implemented NN[,NN...]` | ✅ | tools/check_traceability.py:295-299; `_parse_specs` rejects anything that is not exactly two digits (:271-276) |
| ID pattern, test kinds, name pattern (verbatim) | ✅ | :18-20; the regexes match the spec text character for character |
| Docs are `docs/impl/*.impl.md`, spec number is the first two characters of the file name, fenced lines skipped | ✅ | :179-183, :142-148 |
| Definitions: first token of a heading, or the whole first cell of a table row (spaces and backticks trimmed, separator rows skipped); every other occurrence is a reference | ✅ | :121-130, :150-162 |
| Task-card section runs to the next heading of the same or higher level; its `Tests` row | ✅ | :168-172, :163-165 |
| TR001 `undefined` | ✅ | :201-204 |
| TR002 `duplicate` (once per ID) | ✅ | :189-191 |
| TR003 `wrong spec` | ✅ | :192-194 |
| TR004 `threat without security test` (table rows only) | ✅ | :158-160, :206-208 |
| TR005 `test not on a card` | ✅ with a deviation | Ranges in card `Tests` rows are expanded (:108-118). See ruling 2. |
| TR008 `unresolved cross-spec reference` | ✅ | :166, :209-210. Fenced lines are skipped, which follows the doc-wide Definitions rule. |
| Code scan: `tests/**/*.py` parsed with `ast`, `FunctionDef`/`AsyncFunctionDef` named `test_*`, IDs from the name and the docstring | ✅ | :213-242 |
| TR006 `unknown test id`, TR007 `test id used twice`, TR010 `several ids on one test` | ✅ | :245-258 |
| TR009 `not implemented` with `--require-implemented` | ✅ | :261-268 |
| Summary line `defined=<n> referenced=<n> implemented=<n>`; exit 1 on any violation, else 0 | ✅ | :287-292, :313-316 |
| Errors: missing `docs/impl` exits 2; a test file that fails to parse gives `TR090 syntax error` | ✅ | :305-307, :232-235. Usage and read errors also exit 2, per R-73. |
| TR codes and message wording checked against the spec | ✅ | Every message starts with the spec's exact phrase. Extra context (`defined at …`, `(also …)`, `on <func>`, `in spec NN`) comes after the phrase. |
| Module budget 390 (map row) | ✅ | 320 lines |
| UT00-61 | ✅ | test_check_traceability.py:46. The ID is built at run time; the test also covers fence-skipping. |
| UT00-62 | ✅ | :59 |
| UT00-63 | ✅ | :70, uses `00-x.impl.md` and `U05-01` as the spec says |
| UT00-64 | ✅ | :83 (negative case included) |
| UT00-65 | ✅ | :104. Covers `UT00-07` not being on any card, a unit `Tests` row not counting, and the `…` range. |
| UT00-66 | ✅ | :124. TR006, TR007 and TR010 as specified, plus TR090. |
| UT00-67 | ✅ | :151 (placeholder built at run time) |
| UT00-68 | ✅ | :162. TR009 for `UT00-02` only; exit 2 on a bad value and on a missing root. |
| IT00-02 | ❌ (xfail) | tests/integration/repo/test_check_scripts.py:14-30. Carries `xfail(strict=True)`. See ruling 1 and Important I-1 and I-2. |
| BT00-04 | ✅ | tests/bench/test_core_bench.py:85-92. The repo run takes about 0.3 s against the 5 s limit. |
| Card acceptance: `pytest tests/unit/tools/test_check_traceability.py` passes | ✅ | Verified |
| Card acceptance: `--require-implemented 00` exits 0 once T00-01 … T00-16 tests exist | ⚠️ | Deferred by the card's own wording. It currently fails only on the TR009 items of cards not built yet, plus the T00-10 TR007 items (I-1). |
| Card acceptance: IT00-02 passes | ❌ | Not met. Partly external (specs 01-11) and partly internal (I-1). |

⚠️ Cannot verify from the diff:
- The ruff, mypy, import-linter, module-size and type-ownership gates. The report says all are clean. Only the module-size result was checked here, indirectly: 320 lines is under the 390 budget.
- Whether ranges in card `Tests` rows are the intended notation for specs 01-11. This needs an owner decision to record (ruling 2).

### Rulings on the builder's flagged items

1. **The IT00-02 xfail and the T00-10 TR007 items.**
   - **Doc defects in specs 01-11: the xfail is acceptable.** The card is "Blocked by" the consistency pass. The literal blocker is gone (TR008 = 0 on the repo), but the remaining TR001, TR002, TR004 and TR005 items are real doc defects in other specs' files, outside this card's scope. I spot-checked four and all were true positives. `strict=True` is the right choice because the marker turns into a failure as soon as the repo is clean. The consistency-pass backlog must track these items.
   - **The TR007 items in `tests/unit/tools/test_check_module_size.py` are a genuine defect** in T00-10, not a false positive. `test_ut00_59_*` appears twice (:36, :79) and `test_ut00_60_*` three times (:59, :93, :100). This breaks the global constraint that each test function carries its ID so that `-k UT00_59` selects exactly that test's function, and it breaks the spec's own TR007 rule. It falls within spec 00's scope and should not be hidden behind the same xfail as the external doc defects. See I-1.
2. **Range expansion in TR005: justified. Accepted with conditions.** The spec says only "appears in no task card's Tests row". Yet the specs themselves write card `Tests` rows as ranges more than 1,100 times, including spec 00's own acceptance text (`T00-01 … T00-16`). Without expansion, about 475 false TR005 items would appear, and the consistency pass would have to rewrite every range. The expansion is also narrow:
   - it applies only inside a card `Tests` row
   - both ends must have the same kind and spec
   - it does not change definitions, references or TR001

   So it cannot hide an undefined ID. It only accepts a range as listing its members.

   Conditions:
   - (a) Record it as a deliberate deviation in global-constraints.md or in spec 00 / DECISIONS, so later cards and reviewers know about it. At the moment it exists only in the build report.
   - (b) Test the en dash and `...` forms (M-2).
   - Note for the consistency pass: 2 test rows in spec 04 *define* IDs through a range (04:2065 `UT04-36…UT04-63`, 04:2094 `UT04-92…UT04-98`). The tool correctly does not treat these as definitions, so they surface as TR001 now and will surface as TR006 once those tests are written. They must be expanded in the doc; the tool should not be widened for them.
3. **Weak RED evidence: noted as a process deviation (Minor M-1). It does not block approval.** The tool was written first, so the only RED is `ModuleNotFoundError`. The tests are strong, though. They assert exact `path:line: CODE message` strings, include negative cases (the covered threat, fenced IDs, a unit `Tests` row, the in-range ID) and check the exact TR009 list. Reading them shows that each would fail if its rule were removed or wrong. For example, UT00-65 fails if the range expansion or the card-section boundary is removed. The deficit is in the process, not in what the tests cover.

### Strengths
- The implementation follows the algorithm closely and is easy to map to it. The scan passes (`_DocScanner`, `scan_tests`) are separate from the checks (`check_docs`, `check_code`, `check_implemented`), and it uses the same Violation/Report/argparse pattern as `check_module_size.py`.
- Exit codes follow R-73 (0/1/2), including argparse usage errors and read errors. It uses no `print`, and test files are parsed but never imported.
- The test file avoids polluting itself: every tmp ID is built through `_id(...)`, so running the checker on its own test file finds exactly one ID per function. Confirmed on the repo run: this file produces no TR006, TR007 or TR010.
- The output order is deterministic (sorted Violations with numeric line order).

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
- **I-1: TR007 in the T00-10 test file (genuine defect; blocks IT00-02 for reasons internal to spec 00).** tests/unit/tools/test_check_module_size.py:79 reuses UT00-59 (first used at :36); :93 and :100 reuse UT00-60 (first used at :59). Merge each ID's cases into a single `test_ut00_59_*` and a single `test_ut00_60_*` function. Parametrize if needed: one function carrying one ID is enough for TR007. After that, remove "TR007" from the xfail reason at tests/integration/repo/test_check_scripts.py:17-19. This is a T00-10 fix-up. The controller can assign it here as a separate commit or as a T00-10 follow-up, but it should land before T00-11 closes, so that the only remaining cause of the xfail is outside spec 00.
- **I-2: the xfail also hides failures in the other two scripts IT00-02 covers.** tests/integration/repo/test_check_scripts.py:14-30. One strict xfail covers all three results. If `check_type_ownership` or `check_module_size` regress and start returning 1, the test still reports XFAIL. IT00-02 is the only repository-level run of those two gates in the suite. Fix: assert that the two other scripts return 0 with a plain `assert`. Make only the traceability result the expected failure, for example by raising a dedicated exception type on a non-zero traceability code and using `xfail(strict=True, raises=TraceabilityDebt)`. Any other failure then still fails the test, and a clean repo still turns into an XPASS failure.

#### Minor (Nice to Have)
- **M-1:** The TDD RED was written after the tool (see the build report). This is a process deviation. The tests are strong enough that it does not block approval.
- **M-2:** tests/unit/tools/test_check_traceability.py:108 tests only the `…` range separator. The en dash and `...` forms, and the rule that a range mixing kinds or specs is not expanded, are untested, although tools/check_traceability.py:25-30 and :114-117 implement them.
- **M-3:** tools/check_traceability.py:144. The fence toggle treats ```` ``` ```` and `~~~` as the same marker. A `~~~` line inside a ```` ``` ```` block closes that block early. No spec is affected today. Tracking the opening marker would fix it.
- **M-4:** tools/check_traceability.py:117 pads expanded IDs with `{n:02d}`, which does not keep a three-digit zero-padded start (`UT05-099`). No spec uses that form today (checked with grep), so this is latent only.
- **M-5:** tests/integration/repo/test_check_scripts.py:22-30. Spec §11 describes `tests/integration/repo/` as "subprocess runs of the real tools". IT00-02 calls `main` in-process, which matches the IT00-02 row ("Run the three scripts' `main`") but not the layout note. This is acceptable. If the subprocess form is wanted, add `python -m` smoke runs later.
- **M-6:** tests/bench/test_core_bench.py:85-92. BT00-04 does not assert anything about the return code. That is acceptable for a timing test, but a comment saying the code is deliberately ignored while the docs still have defects would make the intent clear.

### Assessment
**Task quality:** Needs fixes

**Reasoning:** The tool matches U00-56 faithfully: the TR codes, messages, exit codes and the 320/390 line budget all check out. The range-expansion deviation is justified but must be recorded. IT00-02 must not stay xfailed for a defect inside spec 00 (the T00-10 TR007 items, I-1), and its xfail must not hide regressions in the other two gates (I-2).

---

## Re-review round 1 (fix commit 4903fb3, diff T00-11-fix1.diff)

**Verdict: Approved.**

Scope: I-1, I-2, M-2, M-3, M-4 and M-6. M-1 and M-5 were accepted as they are. The sub-controller ruled that TR005 range expansion is a deliberate deviation and recorded it in the group ledger. The docstring at tools/check_traceability.py:109-110 now cites that ruling.

Checks run in the worktree at HEAD 4903fb3:
- **Traceability on the repo:** `uv run python -m tools.check_traceability --root .` exits 1.
  - It reports 134 TR001, 5 TR002, 6 TR004 and 66 TR005. All of them are in docs/impl/01-11.
  - It reports **0 findings under `tests/`** and 0 in `docs/impl/00-foundation.impl.md`.
  - TR007 went from 3 to 0.
- **Non-slow suite:** `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` gives **113 passed, 5 deselected, 1 xfailed**. Before the fix it was 116 passed. The 3 fewer are the three merged module-size functions; no test was lost, because every scenario still runs inside the merged functions.
- **Linters:** `ruff check` passes, `ruff format --check` passes (23 files), and `mypy tools` passes.

### Spec Compliance (re-reviewed items)

| Item | Status | Evidence |
|---|---|---|
| I-1: T00-10 test IDs reused (TR007) | ✅ Fixed | tests/unit/tools/test_check_module_size.py: now exactly one `test_ut00_59_*` and one `test_ut00_60_*`. The clean-repo, missing-default (exit 2) and non-UTF-8 (MS004) scenarios moved into them. Each scenario runs under its own `tmp_path` subdirectory, so the scenarios don't affect each other (`_pyproject` now creates the dir). The repo run shows no TR007. "TR007" is removed from the xfail reason. |
| I-2: xfail hid regressions in the other two scripts | ✅ Fixed | tests/integration/repo/test_check_scripts.py. `check_type_ownership` and `check_module_size` are now plain `assert … == 0`. Only a non-zero traceability code raises `TraceabilityDebtError`, and the marker is `xfail(strict=True, raises=TraceabilityDebtError)`. A plain `AssertionError` from either of the other two gates is not an instance of that subclass, so pytest reports it as a real failure. A clean repo still turns into a strict XPASS failure. |
| M-2: range separators untested | ✅ Fixed | test_check_traceability.py UT00-65 now covers all six range forms: the ellipsis, an en dash with surrounding spaces, `...`, a zero-padded three-digit range, a mixed-kind range (not expanded, so UT00-51 is flagged) and a mixed-spec range (not expanded, so UT00-61 is flagged). It asserts the exact TR005 set. |
| M-3: fence toggle mixing the two markers | ✅ Fixed | tools/check_traceability.py `_DocScanner.scan`: a fence now closes only on the marker that opened it. The UT00-61 fixture puts a `~~~` line inside a ```` ``` ```` block, and the old code would have leaked the fenced ID into the output (a failing test). |
| M-4: expanded IDs lost zero-padding | ✅ Fixed | `_card_test_ids` pads to the width of the start token. The `UT00-098–UT00-100` case in UT00-65 fails under the old `{n:02d}`. |
| M-6: BT00-04 ignored the exit code without saying so | ✅ Fixed | tests/bench/test_core_bench.py: a comment now explains the exit code is deliberately ignored. |
| IT00-02 | ⚠️ Still xfail, as ruled | The remaining cause is entirely external: doc defects in specs 01-11, owned by the consistency pass. That is acceptable under the card's "Blocked by" field. |

⚠️ Not verified here: the slow bench suite (BT00-04). The fix touched only a comment in that test.

### Issues (round 1)

#### Critical
None.

#### Important
None.

#### Minor
- **M-7 (optional):** `TraceabilityDebtError` subclasses `AssertionError` (tests/integration/repo/test_check_scripts.py). This is correct, since the `raises=` filter still tells it apart from plain assertions. Subclassing `Exception` would make the separation obvious to a reader. This is cosmetic; no action is required.
- **M-8 (edge, latent):** `_DocScanner.scan` compares only the first three characters of a fence line, so a four-backtick fence closes on a three-backtick line. CommonMark says a closing fence must be at least as long as the opening one. No spec uses four-backtick fences, so this does not need fixing now.

### Assessment
**Task quality:** Approved

**Reasoning:** Every re-reviewed finding is fixed and each fix has a test that would fail without it. The repo run has no traceability findings under `tests/` or in spec 00. The non-slow suite is green, with IT00-02 as the only xfail, and that xfail is now scoped strictly to the doc defects in specs 01-11.
