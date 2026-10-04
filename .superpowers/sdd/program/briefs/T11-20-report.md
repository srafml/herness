# T11-20 build report — Eval settings and config file

## Status: DONE

## Summary
Implemented `EvalConfig` (with nested `JudgeSettings`, `Thresholds`, `ClassifierSettings`)
typing `config/eval.yaml` per design 11 §7, and committed `config/eval.yaml` with the
exact values from the design snippet given in the dispatch (including the trailing
YAML comment on the `judge` line, which does not affect the parsed value). Added
`load_eval_config(raw: object) -> EvalConfig`, which validates via
`EvalConfig.model_validate(raw)` and re-raises pydantic's `ValidationError` as
`herness.core.errors.ConfigError(msg, hint=str(exc))`, mirroring `herness/eval/truth.py`'s
`load_truth` and the EM101 rule (message assigned to `msg` before raise).

## Files changed
- `herness/eval/settings.py` (new, 96 lines vs 150-line budget)
- `config/eval.yaml` (new, committed with design 11 §7 values verbatim)
- `tests/unit/eval/test_eval_settings.py` (new)
- `pyproject.toml` — added `"herness.eval.settings"` to `source_modules` of the
  `"settings modules are leaves"` import-linter contract (same `forbidden_modules`,
  `allow_indirect_imports = true` unchanged).

No `tests/unit/eval/__init__.py` was created: the sibling test file
`tests/unit/eval/test_truth.py` already lives there without one, and
`tests/unit/core/resilience/` (T08-26's own test dir) also has no `__init__.py`, so the
tree does not use package-style test dirs — confirmed via a repo-root check before writing.

## Design decisions
- Model shape follows `herness/core/resilience/settings.py`'s house style: a private
  `_Model(BaseModel)` base (`extra="forbid", strict=True, frozen=True`), `Annotated[...,
  Field(...)]` helper aliases (`_Fraction`, `_NonNegFloat`, `_PosFloat`, `_NonEmptyStr`,
  `_RepeatCount`), and `Literal`/`type` aliases defined locally (`RepeatPhase`).
- `_repeat_keys` (AfterValidator) enforces all three of `{"fast","standard","deep"}` are
  present; pydantic's own `dict[RepeatPhase, int]` typing already rejects any other key.
  Per-value bound `1..10` is on `_RepeatCount` (`Field(ge=1, le=10)`).
- `_floor_keys` (AfterValidator) rejects any `correctness_floor` key outside
  `{"local-fast","local-standard","local-deep","hybrid","premium"}`.
- `latency_p95_ratio_max` and `cost_ratio_max` use `_PosFloat` (`Field(gt=0)`, strictly
  positive per the "fields whose name contains `ratio`" rule). `bench_regression_max`,
  `unsupported_number_rate_max`, `correctness_drop_max_pp` use `_NonNegFloat` (`Field(ge=0)`).
- `correctness_floor` and `tool_success_min` values use `_Fraction` (`Field(ge=0, le=1)`).
- `synthetic_sample`: `Field(ge=100, le=100_000)`; `bootstrap`: `Field(ge=100, le=10_000)`.
- `gate_recompute_tolerance` uses `_Fraction` (`[0, 1]`) — reasonable bound for a tolerance
  around a fraction-valued gate; design value 0.005 fits comfortably.
- `profile`, `cache_dir`, `suite`, `baseline` use the simpler `Field(min_length=1)`
  (`_NonEmptyStr`) option the dispatch offered, rather than a `_no_nul`-style validator —
  sufficient for UT11-107/UT11-108 and keeps the module well under budget.
- `load_eval_config` is the module's only function that raises `ConfigError`; the pydantic
  models themselves only ever raise `ValueError` internally (via `_require`/`AfterValidator`),
  which pydantic converts to `ValidationError` — matching the ruling in the dispatch.
- Module imports only stdlib (`typing`), pydantic, and `herness.core.errors`. Did not import
  `herness.core.types` — nothing in `EvalConfig` needed a shared domain type, so it was left
  out to avoid an unused import (ruff F401); the ruling permits but does not require it.

## RED evidence
```
$ PYTHONUTF8=1 uv run pytest tests/unit/eval/test_eval_settings.py -q -p no:logging
...
ImportError: cannot import name 'settings' from 'herness.eval'
1 error in 0.14s
```
(captured before `herness/eval/settings.py` existed, config/eval.yaml and the test file
already in place)

## GREEN evidence
```
$ PYTHONUTF8=1 uv run pytest tests/unit/eval/test_eval_settings.py -q -p no:logging
....                                                                     [100%]
4 passed in 0.18s
```
4 tests: `test_ut11_107_config_eval_yaml_matches_design`,
`test_ut11_107_repeat_bound_rejected`, `test_ut11_108_invalid_correctness_floor_key_rejected`,
`test_ut11_108_non_positive_ratio_rejected` (UT11-107 covered by two functions, UT11-108 by
two functions, per the "each ID needs at least one function" rule).

Coverage of the new module: 100% line, 100% branch (`--cov=herness.eval.settings
--cov-branch`, 54 stmts / 2 branches, 0 missed).

## Gate outputs (all clean)
- `uv run ruff format .` — 114 files left unchanged (no reformatting needed)
- `uv run ruff check --fix .` — All checks passed!
- `uv run mypy` — Success: no issues found in 50 source files
- `uv run lint-imports` — Contracts: 10 kept, 0 broken (including `settings modules are
  leaves` with the new source module)
- `uv run python -m tools.check_type_ownership` — clean (only pre-existing "pending owner"
  INFO lines for specs 06/07/09, unrelated to this card)
- `uv run python -m tools.check_module_size` — clean, no output (no budget violations)
- `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` — 1016 passed,
  5 deselected, 1 xfailed (pre-existing xfail on `test_it00_02_check_scripts_pass_on_repo`,
  unrelated to this card)
- `uv run pytest --require-test-ids -q -p no:logging -m unit -k "ut11_107 or ut11_108"` —
  4 passed, 1018 deselected

## Line count vs budget
`herness/eval/settings.py`: 96 lines vs the 150-line module-map budget (hard limit 400).
Well within budget.

## Deviations from the brief
None. All invariants, the Errors contract (`ConfigError` via `load_eval_config`), the
Files list, and the exact design §7 YAML values were followed as specified.

## Concerns
None.
