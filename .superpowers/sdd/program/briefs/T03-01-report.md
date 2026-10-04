# Report for T03-01: Shared decision types

Status: DONE_WITH_CONCERNS (fix round 1 applied)

Commits:
- `2d215d9` — `feat(core): add shared decision types (T03-01)` (initial build)
- `f6287c6` — `build: trim decisions.py and address review findings (T03-01)` (fix
  round 1, addressing `T03-01-review.md`)

Worktree branch `worktree-agent-a4770fba0cd4a9ec1` (base:
`feat/impl00-foundation-runtime` at `079088a`).

## What was implemented

- `herness/core/types/decisions.py` (177 lines after fix round 1, was 212): `QuestionType`,
  `Entity`, `Question`, `QuestionSet` (+ `.get`, `.for_entity`), `DecisionInput`, `Answer`,
  `DecisionOutput` — U03-01 through U03-08, per design 03 §3.2 as given verbatim in
  the brief and in `docs/impl/03-enrichment.impl.md` lines 146-288. All five models
  use `model_config = ConfigDict(frozen=True, extra="forbid", strict=True)` (now
  centralised in a shared `_Frozen` base class — see fix round 1 below). The module
  imports only `herness.core.errors` (no need for `herness.core.ids`: the
  `content_hash == content_hash(text)` invariant is enforced by construction sites
  outside this file, per the spec's own Invariants row for U03-06).
- `herness/core/types/__init__.py` (modified in the initial commit): added the
  owner-03 import block and `__all__` re-export, satisfying impl 00's
  `_check_types_init`/`check_type_ownership` rules (this was previously empty,
  pending an owner).
- `tests/unit/enrich/test_decisions.py` (261 lines, 18 tests): unit tests for
  UT03-01 through UT03-07 and the PT03-01 property test, plus a handful of
  supporting tests (now all test-ID-conformant — see fix round 1) to close out
  branch coverage on the validators.

## RED evidence (initial build)

Command: `uv run pytest tests/unit/enrich/test_decisions.py -q -p no:logging` with
`herness/core/types/decisions.py` temporarily removed:

```
ERROR collecting tests/unit/enrich/test_decisions.py
...
ModuleNotFoundError: No module named 'herness.core.types.decisions'
1 error in 0.21s
```

(Note: implementation and tests were developed together rather than strictly
tests-first; this RED check was captured after the fact, by removing the
already-committed `decisions.py`, then restoring it — working tree confirmed clean
via `git status --short` afterward.)

## GREEN evidence (after fix round 1)

```
$ uv run pytest tests/unit/enrich/test_decisions.py -q -p no:logging
..................
18 passed in 0.26s

$ uv run pytest tests/unit/enrich/test_decisions.py -q -p no:logging --cov=herness.core.types.decisions --cov-report=term-missing --cov-branch
Name                              Stmts   Miss Branch BrPart  Cover   Missing
-----------------------------------------------------------------------------
herness\core\types\decisions.py     122      0     16      0   100%
18 passed in 0.59s

$ uv run pytest -m "(unit or integration) and not slow" -q -p no:logging
121 passed, 4 deselected in 1.83s
```

## Gate outputs (after fix round 1)

```
$ uv run ruff format .
31 files left unchanged

$ uv run ruff check --fix .
All checks passed!

$ uv run ruff check herness/core/types/decisions.py
All checks passed!

$ uv run mypy
Success: no issues found in 13 source files

$ uv run mypy --strict herness/core
Success: no issues found in 10 source files

$ uv run lint-imports
herness layers KEPT
herness never imports app or tools KEPT
core base order KEPT
types import only errors and ids KEPT
Contracts: 4 kept, 0 broken.

$ uv run python -m tools.check_type_ownership
INFO pending owner 05
INFO pending owner 06
INFO pending owner 07
INFO pending owner 08
INFO pending owner 09
(exit 0 — no OWN0xx violation for owner "03")
```

No `pyproject.toml` changes were needed: `mypy.files = ["herness", "tools"]` already
covers the new module, and the import-linter "types import only errors and ids"
contract is generic over `herness.core.types` (no new contract row required).

## Fix round 1 (response to `T03-01-review.md`)

### Important: module-size overage

Trimmed `herness/core/types/decisions.py` from **212 to 177 lines** (63% over the
130-line module-map budget down to **36% over**; still well under the ENG 400-line
hard limit). Two mechanical, behaviour-preserving changes:

1. Factored a shared `_Frozen(BaseModel)` base class holding
   `model_config = ConfigDict(frozen=True, extra="forbid", strict=True)`; all five
   models now inherit from it instead of repeating the `model_config = _CONFIG`
   line, removing one field-assignment line and one blank line per class (net -7
   lines after adding the 3-line base class).
2. Replaced every `if <cond>: msg = "..."; raise ValueError(msg)` block (2-3 lines
   each) with a single `_raise_if(<cond>, "...")` module-level helper call. Ruff's
   `EM101` still requires the message to reach `raise` as a variable, not a
   literal — that constraint is satisfied inside `_raise_if` itself (`raise
   ValueError(msg)` with `msg` a parameter); the call site passing a string
   literal as an ordinary function argument is not a `raise` statement, so `EM101`
   does not apply there. Wrote all calls as single-line source and ran
   `uv run ruff format .` so it auto-wraps only the ones that exceed the 100-char
   line length, minimizing line count without hand-tuning wraps.

No semantic change: all 18 existing tests pass unmodified in behaviour (only two
docstrings/names touched per the naming fix below), 100% line/branch coverage is
unchanged, and `mypy --strict` / `ruff check` / `lint-imports` /
`check_type_ownership` all stay clean.

I did not find a way to close the remaining 47-line gap to the 130-line budget
without a real quality cost: the field declarations for 5 models (~30 fields, one
per line by convention) and the ~20 individual postcondition rules named verbatim
in the brief are close to their practical floor at one rule (one `_raise_if` call)
per line; compressing further would mean either merging semantically distinct
rules into single conditions (losing the specific error message TH03-06 wants per
failure mode) or inlining the `_raise_if` helper back at each call site (undoing
the trim and pushing individual validator methods' cyclomatic complexity back up
toward the ruff `C901` ceiling). Flagging the remaining 36% overage for a program
ruling per the review's suggestion, rather than trimming further at the expense of
per-rule error-message clarity.

### Minor 1: PT03-01 only perturbed upward

`test_pt03_01_distribution_sum_tolerance` now draws a random sign
(`st.sampled_from([1.0, -1.0])`) alongside the magnitude and value, so the
property exercises both a sum pushed above `1 + 1e-3` and a sum pushed below
`1 - 1e-3`, matching PT03-01's "any perturbation beyond 1e-3 is rejected" (not
just "any increase").

### Minor 2: test naming/ID conformance

- `test_question_dynamic_options_and_descriptions` (no test ID anywhere) renamed
  to `test_ut03_02_question_additional_shape_rules` (attributed to UT03-02, the
  "bad question shapes" test it extends) with a docstring now opening
  `"""UT03-02 (supporting U03-02): ...`.
- `test_ut03_04_question_set_limits`, `test_ut03_06_answer_probability_mismatch`
  and `test_ut03_06_answer_out_of_range_values` already carried a UT-id in their
  function names; their docstrings previously opened with `"Supporting U0x-xx"` (a
  *unit* ID). Changed each to open with the UT-id already in the function name
  (e.g. `"""UT03-04 (supporting U03-03): ...`), per global-constraints' rule that
  the first docstring line starts with the test ID.

### Minor 3: unrequested `answers` default

`DecisionOutput.answers` dropped `Field(default_factory=dict, ...)` down to
`Field(max_length=_MAX_ANSWERS)` with no default, matching the brief's U03-08
signature (`answers: dict[str, Answer]`, no default shown) exactly. `answers` is
now a required field; no existing test relied on the default (all construct
`DecisionOutput` with an explicit `answers=` argument), so this was a no-op for
the test suite.

## Deviations from the brief's verbatim test rows (unchanged from initial build,
carried forward — not part of the review's findings, still applicable)

The brief's "Test specs (rows from §11)" section only reproduced UT03-01 and
UT03-07 verbatim (plus PT03-01), while the Task card's "Tests" field lists the full
range UT03-01–UT03-07. I read the full test table in
`docs/impl/03-enrichment.impl.md` (lines 3597-3603) for the missing rows and found
three of them exercise symbols that do not exist in this card's scope (Files:
`herness/core/types/decisions.py` only; Depends on: T00-03, T00-08 only). The
review's Spec Compliance table confirms both sub-controller rulings (adapting
UT03-02/03/05 to exercise `Question`/`DecisionInput` directly, and implementing
UT03-06/PT03-01 to the 1e-3 tolerance rather than the literal "1.002 accepted")
were correctly applied and raised no dispute:

- **UT03-02, UT03-03** ("... `load_question_set` | `ConfigError` naming the
  question ..."): `load_question_set` is U03-16, built in T03-03 (this card does not
  depend on T03-03). Adapted: `test_ut03_02_question_shape_rules` and
  `test_ut03_03_bool_word_option_labels_rejected` construct `Question` directly and
  assert the underlying `pydantic.ValidationError` (the error `load_question_set`
  would later wrap into `ConfigError`), covering the same shape rules (1 option,
  256 options, 3-tuple `levels`, `levels` on a `bool` question, and the four
  bool-word option-key cases) without inventing the loader.
- **UT03-05** ("inputs built by `build_inputs`, `pair_inputs` on `tiny_build` |
  recompute hash | `content_hash == content_hash(text)`"): `build_inputs` and
  `pair_inputs` are U03-26/U03-27, built in T03-05 (not depended on here), and
  `tiny_build` is impl 11's fixture. Adapted: `test_ut03_05_content_hash_matches_text`
  constructs `DecisionInput` directly with `content_hash = sha256_hex(text)[:32]`
  (the U03-26 definition, confirmed at line 3619 of the impl spec:
  `"abc" -> first 32 hex of sha256("abc")`) and asserts the invariant, i.e. it
  exercises what a construction site must do without invoking the not-yet-built
  construction sites themselves.
- **UT03-06**'s literal setup/expected text ("distributions summing to 0.99,
  1.002, ... 1.002 accepted") is internally inconsistent with three other binding
  statements in the same spec, all agreeing on a **1e-3** sum tolerance: U03-07's
  own Postconditions (`abs(sum − 1) ≤ 1e-3`), TH03-06's mitigation ("distributions
  sum to 1 ± 1e-3"), and PT03-01 ("any perturbation beyond 1e-3 is rejected"). A
  sum of 1.002 deviates by 2e-3, twice the stated tolerance, and under a literal
  reading would have to be *rejected*, not accepted. I judged the "1.002" in the
  test row to be a transcription error and implemented the validator to the
  tolerance stated three times elsewhere (1e-3). In
  `test_ut03_06_answer_distribution_rules` the "accepted" case uses a sum within
  1e-3 (0.5005 + 0.5 = 1.0005) instead of the literal 1.002, and
  `test_pt03_01_distribution_sum_tolerance` (now bidirectional, see fix round 1)
  is a direct hypothesis-based check of the 1e-3 boundary per PT03-01's own
  wording.

All three deviations are documented in the test module's own docstring as well, so
they stay visible to whoever implements T03-03/T03-05 and can re-home the
loader/construction-site-specific parts of UT03-02/03/05 into those cards' test
files when `load_question_set`/`build_inputs`/`pair_inputs` land.

## Concerns

1. Line budget: 177 vs 130 lines (36% over; was 63% over before fix round 1). Not
   a gate failure (no automated line-budget tool exists yet in `tools/`), but
   still worth a program ruling — see the fix-round-1 rationale above for why I
   judged the remaining gap not further closeable without a real clarity cost.
2. UT03-02/03/05 as adapted here test `Question`/`DecisionInput` directly rather
   than the loader/construction-site path the spec's test table describes; T03-03
   and T03-05 should each re-verify (and can reuse) these scenarios against the
   real `load_question_set`/`build_inputs`/`pair_inputs` once those exist, per the
   spec's original intent. (Confirmed by the review as a correctly-applied,
   undisputed ruling.)
3. UT03-06's "1.002 accepted" literal was treated as a spec typo rather than
   implemented as written, given three other binding 1e-3 statements would
   otherwise be violated. Flagging for a ruling if intentional. (Confirmed by the
   review as a correctly-applied, undisputed ruling.)
