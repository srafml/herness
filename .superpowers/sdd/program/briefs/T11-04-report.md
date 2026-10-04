# Report: T11-04 Truth model

Note: the intended report path `D:\herness\.superpowers\sdd\program\briefs\T11-04-report.md`
was refused ("Edit the worktree copy of this file instead of the shared-checkout
path" — that path is outside the worktree this agent is isolated to), so this
report is written to the scratchpad instead, as the dispatch instructions allow.

Status: DONE

## Resumption context

Picked up from a prior build agent that died mid-task (usage limit) leaving
uncommitted work: a modified `pyproject.toml` and untracked `herness/eval/`
(`__init__.py`, `truth.py`) and `tests/unit/eval/test_truth.py`. Reviewed all
three against the brief and the spec, judged the implementation and tests
correct and complete for U11-28/U11-29/UT11-43/UT11-44, then finished the
prior agent's stated next step ("check ST00-10 still works with the new
contract, rerun type-ownership and full unit+integration suite") and closed
two gaps that surfaced while doing so (see Deviations).

## What was implemented (inherited, verified correct)

- `herness/eval/__init__.py` — package docstring only (1 line, budget 5).
- `herness/eval/truth.py` (197 lines, budget 250):
  - `SuiteError(RecoverableError)` — question-level suite defect (U11-29,
    documented as living here temporarily until `golden.py`, T11-16+, exists;
    that module will import it from here rather than redeclare it).
  - Frozen, `extra="forbid"`, `strict=True` pydantic models: `T1BadTeam`,
    `T2RoiEpic`, `T2cClusterFix`, `T3ChangeCluster`, `T4NoisyService`,
    `T5Confounder`, `T6Side`, `T6Outcomes`, `Plants`, `TruthManifest` — all
    fields match the brief's U11-28 postcondition list verbatim.
  - `truth_dir_for(data_root) -> data_root.parent / "truth"`.
  - `load_truth(truth_dir)`: reads `<truth_dir>/truth.json`, enforces the
    1 MB cap (`MAX_TRUTH_BYTES`), parses JSON, validates via
    `TruthManifest.model_validate`; every failure path (`OSError`, oversize,
    `JSONDecodeError`, `ValidationError`) raises `ConfigError` naming `path`.
  - `plant_value(manifest, path)`: validates the path against
    `_PLANT_PATH_RE`, resolves full form (`plants.<key>.<field>...`) or short
    form (`T<n>`/`T2c`/`T6` via `_resolve_short_key`, unique-prefix match
    against `Plants.model_fields`), walks attributes, and returns a scalar or
    `list[str]`; every failure (malformed path, unknown key, ambiguous
    prefix, unknown field, non-scalar result) raises `SuiteError` naming
    `path`.
- `tests/unit/eval/test_truth.py`: `pytestmark = pytest.mark.unit`;
  `test_ut11_43_load_truth` and `test_ut11_44_plant_value` (IDs in both
  function name and docstring, one function per spec test ID per U11-31).
  Built its own minimal truth fixture via `tmp_path` rather than depending on
  `tests/support/truth.py` (`truth_42_tiny`), which is built by the later,
  generator-dependent card T11-16 — documented in the test module's
  docstring.

RED/GREEN evidence for the coverage gap closed in this session:

```
# before (inherited state)
uv run pytest tests/unit/eval/ -q -p no:logging --cov=herness.eval --cov-report=term-missing --cov-branch
herness\eval\truth.py   132   10   18   4   91%   Missing: 158-160, 176-177, 182-183, 193, 196-197

# after (this session's added assertions)
uv run pytest tests/unit/eval/ -q -p no:logging --cov=herness.eval --cov-report=term-missing --cov-branch
herness\eval\truth.py   132   0   18   0   100%
2 passed in 0.17s
```

## Deviations from the inherited state, and why

1. **Coverage gap closed.** The inherited `test_truth.py` left
   `herness/eval/truth.py` at 91% line / partial branch coverage (5 branches
   not exercised: the `ValidationError` path in `load_truth`; the
   regex-mismatch, unknown-full-form-key, and non-scalar-result paths in
   `plant_value`; and the list-return path). Global constraints require
   >=90% line and >=85% branch on every new/changed module. Added, inside the
   existing two test functions (no new test IDs, matching the pytest
   plugin's one-function-per-ID rule and the module docstring's stated
   design): an invalid-type payload case (`seed: "not-an-int"`) to hit the
   `ValidationError` -> `ConfigError` branch; `plant_value` cases for
   `plants.T1_bad_team.service_ids` (list return), `plants.T99_nonexistent...`
   (unknown key, full form), `"not a valid path"` (regex mismatch), and
   `T6.paid` (resolves to a non-scalar `T6Side` object). Now 100%/100%.

2. **`pyproject.toml` diff kept as inherited, after verifying it's required
   (not redundant).** I initially suspected the added `[[tool.importlinter.
   contracts]] name = "core base is closed"` block was redundant with the
   `"herness layers"` layers contract (which already places `herness.eval`
   above `herness.core`, so `herness.core` importing `herness.eval` would
   already be caught). I removed it experimentally and re-ran `lint-imports`
   — it still passed (confirming the redundancy for catching the actual
   violation) — but this broke `tests/unit/repo/test_import_contracts.py::
   test_ut00_58_contracts_match_repository` (owned by T00-09, already
   committed on this branch), which asserts that a `"core base is closed"`
   contract must exist and list exactly `{non-BASE core modules} union
   {top-level herness packages other than herness.core}` whenever that set
   is non-empty — which it now is, because of `herness.eval`. So the
   contract is not logically necessary for `lint-imports` to catch the
   violation, but it *is* required by this repo's own governance test.
   Restored it exactly as inherited (`forbidden_modules = ["herness.eval"]`,
   `source_modules` = the seven `core base order` base modules) — this
   matches what UT00-58 computes. Diff is otherwise minimal: only the one
   new layer entry plus this one new contract block.

3. **Fixed `tests/integration/repo/test_import_contracts_enforced.py`
   (ST00-10), which this card's `pyproject.toml` change broke.** That test
   (pre-existing, from T00-09, not in this card's file list) plants an
   upward import and edits a copy of `pyproject.toml` via a hard-coded
   string replace: `'layers = [\n    "herness.core",\n]'` ->
   `'layers = [\n    "herness.harness",\n    "herness.core",\n]'`. Once the
   `"herness layers"` contract had two real entries
   (`["herness.eval", "herness.core"]`), that literal substring no longer
   existed in the file, so the `.replace()` silently no-opped, the planted
   `herness.harness` layer was never added to the copied config, and
   `lint-imports` on the copy returned 0 (nothing to catch it) — the test's
   `assert result.returncode != 0` failed. Replaced the substitution with one
   anchored on the `"herness layers"` contract's own header
   (`'name = "herness layers"\ntype = "layers"\nlayers = [\n'`, asserted
   present, then insert `"herness.harness"` as the new first/highest layer),
   which is correct regardless of how many layers already exist. Verified
   this is the only thing that needed fixing — the rest of the test's logic
   (using the existing `"core base is closed"` contract instead of
   synthesizing one, since one now exists) was already written to handle
   that case and needed no change.

## Gates (all run from the worktree root)

PowerShell's cp1252 console codepage garbled `lint-imports`' Unicode banner
in subprocess capture for the ST00-10 test specifically — an environment
artifact, not a code issue — so runs touching that test were done via the
Bash tool / Git Bash's UTF-8 locale, where they pass cleanly.

```
uv run ruff format --check .        -> 38 files already formatted
uv run ruff check .                 -> All checks passed!
uv run mypy                         -> Success: no issues found in 14 source files
uv run lint-imports                 -> 5 contracts, 5 kept, 0 broken
uv run python -m tools.check_type_ownership -> exit 0 (INFO pending-owner lines only, pre-existing)
uv run pytest -m "(unit or integration) and not slow" -q -p no:logging --require-test-ids
    -> 105 passed, 4 deselected
uv run pytest tests/unit/eval/ --cov=herness.eval --cov-branch --cov-report=term-missing
    -> 100% line, 100% branch
```

Line counts vs. §2 module map budgets: `herness/eval/__init__.py` 1/5,
`herness/eval/truth.py` 197/250.

## Files changed

- `herness/eval/__init__.py` (new)
- `herness/eval/truth.py` (new)
- `tests/unit/eval/test_truth.py` (new, extended with the coverage cases
  above)
- `pyproject.toml` (import-linter: `herness.eval` added to the `"herness
  layers"` layer list; new `"core base is closed"` forbidden contract)
- `tests/integration/repo/test_import_contracts_enforced.py` (ST00-10 fix,
  collateral from the layers-contract change; not in this card's file list
  but required to keep the suite green)

## Concerns

- None outstanding. The `RecoverableError`/`FatalError` base-class choices
  (`SuiteError` on `RecoverableError`, `ConfigError` on `FatalError`, both
  pre-existing in `herness.core.errors`) match the brief's stated error
  types by name; the brief does not specify which `HernessError` subclass
  family `ConfigError` must belong to beyond "ConfigError", so I did not
  second-guess the pre-existing taxonomy.
- `SuiteError`'s placement in `truth.py` rather than `golden.py` is
  explained in the module's own docstring as intentional (golden.py's card
  depends on the full generator pipeline and hasn't landed); flagging here
  only so the reviewer of T11-16 (or whichever card lands `golden.py`) knows
  to import `SuiteError` from `herness.eval.truth` rather than redeclare it.

Commit: `ef03638` — `feat(eval): add truth manifest model and loader
(T11-04)`

## Fix round 1 (review)

Reviewer verdict: Needs fixes. One Important finding: the ST00-10 fix from
this card's initial commit (item 3 above) restored a green suite but left
the `"core base is closed"` assertion vacuous. Root cause: with a real
`"core base is closed"` contract now permanently in `pyproject.toml`
(forbidding `herness.eval`, C4), the test's `if not any(...)` guard chose
not to synthesize its own copy — but then never told the *existing*
contract about the planted `herness.harness` package either, so that
contract still only forbade `herness.eval` and reported `KEPT` for the
plant. The test's final assertions (`"herness layers" in result.stdout`,
`"core base is closed" in result.stdout`) passed anyway because
`lint-imports` prints every contract's name regardless of pass/fail
status, so the C4 half of the check was never actually exercised.

Fix (commit `a9424c0`,
`tests/integration/repo/test_import_contracts_enforced.py`): when
`"core base is closed"` already exists, extend its `forbidden_modules`
in place with `"herness.harness"`, using the same anchored-marker
string-splice technique already used for the `"herness layers"` layers
list (search from the contract's `name =` line for its
`forbidden_modules = [` line, splice before the closing `]`). This matches
C4's formal definition (impl 00 §2 row C4: `forbidden_modules` = every
existing top-level package of C1 other than `herness.core`) — the plant
adds one such package, so a spec-faithful copy of C4 must forbid it too.
Also tightened the final assertions to require
`"herness layers BROKEN"` and `"core base is closed BROKEN"` specifically,
not merely that the names appear.

Re-ran the full gate set after the fix (Bash/Git Bash, to avoid the
PowerShell cp1252-vs-Unicode-banner artifact noted above; also hit one
unrelated transient failure — `uv run ruff check` briefly errored trying
to parse `D:\herness\pyproject.toml` while another concurrent session was
mid-merge in the shared main checkout; retried moments later and it was
clean, confirming this was not caused by anything in this worktree):

```
uv run ruff format --check .        -> 38 files already formatted
uv run ruff check .                 -> All checks passed!
uv run mypy                         -> Success: no issues found in 14 source files
uv run lint-imports                 -> 5 contracts, 5 kept, 0 broken
uv run python -m tools.check_type_ownership -> exit 0 (INFO pending-owner lines only, pre-existing)
uv run pytest -m "(unit or integration) and not slow" -q -p no:logging --require-test-ids
    -> 105 passed, 4 deselected
```

Commit: `a9424c0` — `fix(tests): make ST00-10 exercise the real 'core base
is closed' contract (T11-04)`
