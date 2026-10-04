# Report for T07-17: Outcome statistics

Status: DONE

Commit: b18c91a28b601860f25622cf32b0c5658cec584a
Subject: feat(harness): add memory outcome statistics (T07-17)
Branch/worktree: worktree-agent-a65d277c4bb1cf9a9 (D:\herness\.claude\worktrees\agent-a65d277c4bb1cf9a9)

## What was built

`herness/harness/memory/outcome_stats.py` (161 lines, budget 240):
- `MetricWeeks` (frozen dataclass): the four resolved `outcome` week counts
  (`measure_after_weeks`, `second_measure_weeks`, `window_weeks`, `settle_weeks`).
- `Windows` (frozen dataclass): `pre`, `post` (half-open date tuples), `due`.
- `DidResult` (frozen dataclass): `n_pre`, `n_post` (int) plus `coverage`, `mean_pre`,
  `mean_post`, `did`, `se`, `t`, `rel`, `expected_rel` (all `float | None`).
- `measurement_windows(effective_at, measurement, w) -> Windows` (U07-83): implements the
  R-34 formulas exactly (pre = 7L days before `effective_at`; m=1 post = [lag, lag+L) after
  `effective_at`, due = max(measure_after_weeks, lag+L); m=2 post = [end-L, end) where
  end = second_measure_weeks, due = end).
- `did_statistics(target, control, windows, *, better, expected_delta, min_rel) -> DidResult`
  (U07-84): weeks common to `target`/`control` are differenced; `n_pre`/`n_post`/`coverage`
  from the windows; `mean_pre`/`mean_post` from `target` alone; `did`/`se`/`t`/`rel` computed
  only when `n_pre >= 2` and `n_post >= 1` (else `None`), using only `statistics.fmean` and
  `statistics.stdev`; `t` handles the `se == 0` edge case (+/-inf by sign of the improvement,
  or 0 when the improvement is 0); `expected_rel` falls back to `min_rel` when `expected_delta`
  or `mean_pre` is unavailable.
- `classify_verdict(d, *, has_control, cfg) -> Verdict` (U07-85): first-match-wins table per
  design 07 SS5.9 (`inconclusive` guards first, then `paid_off`, `worse`, `no_effect`, default
  `inconclusive`), defensively narrowing `expected_rel`/`mean_pre`/`se` around `None`.

Only stdlib imports plus `herness.harness.memory.settings.OutcomeConfig` (a sibling module,
consistent with how `types.py`/other memory submodules are not counted as "extra imports" in
the impl doc's module map). `MetricWeeks`/`Windows`/`DidResult` are module-local (not among the
07-owned shared types listed for `herness.core.types.memory`, so they do not belong there).

The brief was missing U07-84 (`did_statistics`) and test rows UT07-70..UT07-72; both were read
from `docs/impl/07-memory.impl.md` SS3.16 (~line 1844), the SS11 test table (~line 2700-2704),
R-34 (line 2588/3184) and DD14 (line 3184), as instructed.

## Tests

`tests/unit/harness/memory/test_memory_outcome_stats.py`, 13 tests, all `pytest.mark.unit`:

- UT07-69 (3 tests): default windows for m=1 (post weeks 2-12, due week 12) and m=2 (post
  weeks 16-26, due week 26) per the literal R-34 example; a per-metric override
  (`measure_after_weeks=20`) shows `due` shifts to week 20 for m=1 while pre/post and the m=2
  due date are unaffected.
- UT07-70 (4 tests): a hand-computed synthetic series (target 10/12/20/22, control all 0) checks
  exact `did`/`se`/`t`/`rel`/`coverage`/`mean_pre`/`mean_post` values; a `better="lower"` variant
  checks the sign flip; an `expected_delta` variant checks `expected_rel` overrides the
  `min_rel` fallback; a 1-pre-week series checks `did`/`se`/`t`/`rel` stay `None`.
- UT07-71 (4 tests): one `DidResult` fixture per verdict (`paid_off`, `worse`, `no_effect`,
  and `inconclusive` via `has_control=False`).
- UT07-72 (1 test): a 12-week series where target and control share an identical linear trend
  (only a constant gap survives differencing) drives `did_statistics` then `classify_verdict`
  end-to-end to `no_effect`.
- UT07-73 (1 test): `n_pre=5` (below `min_weeks=6`) is `inconclusive` even with a strong t/rel.

`PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`:
1091 passed, 5 deselected, 1 xfailed (pre-existing, unrelated
`test_it00_02_check_scripts_pass_on_repo` xfail for spec doc traceability defects) - no
regressions.

## Gates

- `uv run ruff check` - pass (after adding a named constant for the `n_pre >= 2` magic value).
- `uv run ruff format --check` - pass.
- `uv run mypy herness/harness/memory/outcome_stats.py` - pass, no issues.
- `uv run lint-imports` - 10 contracts kept, 0 broken.
- `uv run python -m tools.check_type_ownership` - exit 0 (only pre-existing "pending owner
  06/09" INFO lines, unrelated to this card).
- `uv run python -m tools.check_module_size` - exit 0 (161/240 lines).

## Concerns

- None blocking. Two minor judgment calls worth flagging for review:
  1. `OutcomeConfig` is imported from the sibling `herness.harness.memory.settings` module even
     though the module map's "extra imports" column says "none" for `outcome_stats.py`. I read
     "none" as "no cross-domain imports beyond this package's own settings/types", matching how
     `outcome.py`'s row also omits `settings` from its extra-imports list despite needing
     `OutcomeConfig` too. If the intended reading is stricter (truly zero non-stdlib imports),
     `classify_verdict`'s `cfg` parameter would need a different, decoupled parameter shape.
  2. `MetricWeeks`/`Windows`/`DidResult` are plain local dataclasses rather than pydantic models
     in `herness.core.types.memory`, since they are not in that submodule's owned-types list
     (U07-01..U07-10) and U07-83/84 explicitly call them "(frozen dataclass)". This module has
     no runtime resolver from `cfg.outcome` + a metric name to a `MetricWeeks` instance - per
     the module map that resolution belongs to `outcome.py` (T07-18), which is out of scope here.
