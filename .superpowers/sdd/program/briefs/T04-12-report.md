# Report: T04-12 Scoring context

Status: DONE

Commit: b23419e62e8fa0aa53dd7bcc50769e74e276f86b
`feat(metrics): add scoring step context (T04-12)`

## What was implemented

- `herness/metrics/context.py` (79 lines, budget 80): `StepContext`, `StepResult`,
  `ScoringReport` per U04-54.
  - `StepContext` is a frozen, `slots=True` dataclass: `build_id: str`,
    `catalog: CatalogView` (the `_binds.CatalogView` Protocol per the program ruling —
    swap to `MetricCatalog` once T04-03 lands), `weights: WeightsConfig`, `as_of: date`,
    `tz: str`, `disabled_metrics: frozenset[str]`.
  - `StepContext.binds()` returns the union of `default_binds(catalog)`,
    `weight_binds(weights)`, `default_window("t12w", as_of, tz, catalog.defaults.windows).binds()`,
    and three step binds computed by a private helper `_step_binds`:
    - `s_count_metrics`: sorted enabled metric names (`catalog.names()`) whose
      `catalog.get(name).aggregation` is `"count"` or `"snapshot"`.
    - `s_unconfirmed_models`: sorted `UsdModel` values (`mttr`, `repeat`, `reopen`,
      `reassign`, `sla`, `cfr`, `noise`) whose `WEIGHT_USES["lever_<model>"]` block set has
      at least one unconfirmed block (via `unconfirmed_blocks`).
    - `unconfirmed`: `bool(s_unconfirmed_models)` — a base/default value. Every step that
      cares overrides it explicitly per the spec text at U04-66/U04-68/U04-69 (funding:
      `bool(unconfirmed_blocks(weights, WEIGHT_USES["funding"]))`; org: `false`; levers:
      per-row via `list_contains(s_unconfirmed_models, model)`, not the scalar bind;
      portfolio: `WEIGHT_USES["portfolio"]`). This card only had to supply *a* sensible,
      catalog/weights-derived default for the base context, since no unit spec pins its
      exact formula — see Deviations below.
  - `StepResult`: frozen `slots=True` dataclass, `row_counts: dict[str, int]`,
    `warnings: list[str]`, `flags: list[str]`, `failed_checks: list[str]`.
  - `ScoringReport`: pydantic `BaseModel` with
    `ConfigDict(frozen=True, extra="forbid", strict=True)`; fields `build_id: str`,
    `steps_done: list[str]`, `row_counts: dict[str, int]`, `duration_ms: dict[str, int]`,
    `flags: list[str]`, `warnings: list[str]`. Defined locally (not reusing the
    settings-only `Model` base from `_weights_settings.py`) to keep `context.py`
    self-contained per its "none" extra-imports module-map row.
- `herness/metrics/_binds.py`: added `s_count_metrics` and `s_unconfirmed_models` to the
  `VARCHAR[]` group of `BIND_TYPES` (U04-34), as T04-04 left them for this card. No other
  change to `_binds.py`; the `CatalogView` Protocol already exposed everything
  `context.py` needed (`.defaults`, `.get`, `.names`), so it was not extended.
- `tests/unit/metrics/test_metrics_render.py`: updated the existing
  `test_ut04_26_bind_types_cover_spec_names` (T04-04) assertion that every `d_`/`s_`/`w_`
  `BIND_TYPES` name is produced by `default_binds` ∪ `weight_binds`. `s_count_metrics` and
  `s_unconfirmed_models` are step binds from `StepContext.binds()`, not `default_binds`,
  so the test now subtracts those two names before comparing. This was the expected,
  necessary fallout of adding the two names to `BIND_TYPES`, called out in the brief.
- `tests/unit/metrics/test_metrics_context.py` (new, 6 tests, all `test_ut04_110_*`,
  `pytestmark = pytest.mark.unit`): binds() union coverage, `s_count_metrics` derived from
  metric aggregation (enabled-only), `s_unconfirmed_models`/`unconfirmed` matching an
  independently-computed `unconfirmed_blocks` expectation, immutability of `StepContext`
  and `StepResult` (`dataclasses.FrozenInstanceError`), and `ScoringReport` frozen/forbid/
  strict behaviour (attribute assignment, extra field, non-strict type coercion all raise
  `pydantic.ValidationError`).

## RED/GREEN evidence

RED (before `context.py` existed):
```
ModuleNotFoundError: No module named 'herness.metrics.context'
```
GREEN (after implementation):
```
tests/unit/metrics/test_metrics_context.py ......  [100%]
6 passed in 0.13s
```
Full suite after the `_binds.py` change surfaced one pre-existing test needing an update
(`test_ut04_26_bind_types_cover_spec_names`), fixed as described above; full suite is
green afterward.

## Gate results

- `uv run ruff check .` — All checks passed.
- `uv run ruff format --check .` — 105 files already formatted.
- `uv run mypy` — Success: no issues found in 45 source files. (One real finding fixed
  along the way: `Mapping[str, int]` invariance meant `catalog.defaults.windows`
  — `dict[Literal["week","month","quarter"], int]` — needed an explicit
  `cast("Mapping[str, int]", ...)` before passing to `default_window`.)
- `uv run lint-imports` — Contracts: 8 kept, 0 broken.
- `uv run python -m tools.check_type_ownership` — exit 0 (info-only lines about pending
  owners 06/07/09, pre-existing/unrelated).
- `uv run python -m tools.check_module_size` — exit 0 (no output; `context.py` at 79/80
  lines, `_binds.py` at 175/200 lines).
- `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` —
  918 passed, 5 deselected, 1 xfailed (pre-existing, unrelated `check_traceability` xfail).

## Line counts vs budgets

- `herness/metrics/context.py`: 79 / 80 lines (module map budget).
- `herness/metrics/_binds.py`: 175 / 200 lines (module map budget), up from 173.

## Deviations / concerns

- **`unconfirmed` base default is an inference, not a pinned spec value.** U04-54 states
  the `unconfirmed` bind is "the step-specific value set by each step" and lists exactly
  four step formulas (funding, org, levers, portfolio) — but doesn't say what
  `StepContext.binds()` itself should put there before a step overrides it. I chose
  `bool(s_unconfirmed_models)` (true iff any lever model has an unconfirmed weight block)
  because it's derived the same way as `s_unconfirmed_models` (satisfying the unit's
  "computed from the catalog and `unconfirmed_blocks`" language) and because, by my
  reading of U04-66/U04-68/U04-69, every step that consumes the scalar `unconfirmed` bind
  overrides it explicitly anyway (funding and org override; levers reads
  `s_unconfirmed_models` per-row and ignores the scalar). So this default is very likely
  inert in practice, but a reviewer with sharper context on the `check` step (U04-59,
  not yet read/implemented) or `portfolio_input.sql.j2` should confirm nothing else reads
  the base `unconfirmed` scalar unoverridden.
- Did not need to extend the `CatalogView` Protocol in `_binds.py` — `.defaults`, `.get`
  and `.names` were already sufficient for `s_count_metrics` and the window binds. Left
  `_binds.py`'s `CatalogView` untouched.
- Did not touch `render.py` at all (per the "don't touch render.py" note); the one
  incidental change outside `context.py`/`_binds.py` was the one-line assertion fix in
  `test_metrics_render.py`, which was necessary fallout from the `BIND_TYPES` addition
  the brief explicitly assigned to this card.
- Used `uv run ...` throughout; `D:\herness\pyproject.toml` was not mid-merge during this
  run, so no fallback to `.venv\Scripts\python.exe` was needed.
