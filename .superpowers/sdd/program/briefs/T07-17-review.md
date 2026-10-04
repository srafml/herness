# Review for T07-17: Outcome statistics

Reviewed in worktree `D:\herness\.claude\worktrees\agent-a65d277c4bb1cf9a9` at commit
`b18c91a feat(harness): add memory outcome statistics (T07-17)`.

## Spec compliance

| Requirement | Status |
|---|---|
| U07-83 `measurement_windows`: `pre = [t0-7L, t0)`; m=1 `post=[t0+7·lag, t0+7·(lag+L))`, `due=t0+7·max(measure_after_weeks, lag+L)`; m=2 `end=t0+7·second_measure_weeks`, `start=max(end-7L, t0+7·(lag+L))`, `post=[start,end)`, `due=end` | ✅ Implemented verbatim (`outcome_stats.py:64-79`) |
| R-34 defaults: m=1 post weeks 2-12, due 12; m=2 post weeks 16-26, due 26; pre 10 weeks | ✅ UT07-69 (`test_memory_outcome_stats.py:201-235`) hand-checks all three cases plus a per-metric `measure_after_weeks` override, matching the `due=max(...)` formula |
| U07-84 `did_statistics`: `adj(w)=target(w)-control(w)` on weeks present in both; `n_pre/n_post` = adj-week counts in pre/post; `coverage=min(n_pre/weeks(pre), n_post/weeks(post))`; `mean_pre/mean_post` from raw `target` (not `adj`) per window; when `n_pre>=2` and `n_post>=1`: `did`, `se=stdev(adj_pre)·sqrt(1/n_pre+1/n_post)`, sign-adjusted `impr`, `rel=impr/max(\|mean_pre\|,1e-9)`, `t=impr/se` (se>0) else ±inf by sign of `impr` or 0 when `impr==0`; else all `None`; `expected_rel=\|expected_delta\|/max(\|mean_pre\|,1e-9)` or `min_rel` fallback | ✅ Implemented verbatim (`outcome_stats.py:92-140`); hand-verified against UT07-70's synthetic series (target 10/12/20/22 vs. control 0): `did=10.0`, `se=sqrt(2)`, `t=10/sqrt(2)`, `rel=10/11`, all match the manual computation |
| U07-85 `classify_verdict` first-match-wins table: inconclusive guard (`not has_control`, `n_pre<min_weeks`, `n_post<min_weeks`, `coverage<min_coverage`, `t`/`rel` is `None`) → `paid_off` (`t>=t_crit` and `rel>=max(min_rel, 0.5·expected_rel)`) → `worse` (`t<=-t_crit` and `rel<=-min_rel`) → `no_effect` (`\|rel\|<min_rel` and `se/\|mean_pre\|<=min_rel/2`, false when `mean_pre==0`) → otherwise `inconclusive` | ✅ Implemented verbatim (`outcome_stats.py:143-169`), including the `mean_pre==0` guard forcing the `no_effect` condition false. `coverage is None` is also guarded defensively (not explicit in the prose algorithm but consistent with TH07-18's conservative-default mandate; not a deviation since `did_statistics` never actually produces a `None` coverage) |
| UT07-69 exact values (m=1 post 2-12 due 12; m=2 post 16-26 due 26; pre 10 weeks) | ✅ verified by hand against R-34, see above |
| UT07-70 did/se/t/rel synthetic values | ✅ verified by hand, plus `better="lower"` sign-flip and `expected_delta` override sub-cases |
| UT07-71 one series per verdict | ✅ `paid_off`, `worse`, `no_effect`, `inconclusive` (no control) all present and correctly trigger their branch |
| UT07-72 seasonal shift in all peers → `no_effect` | ✅ hand-verified: constant 20-unit gap gives `did=0`, `se=0` (constant pre-series), `rel=0`, correctly classified `no_effect` |
| UT07-73 5 weeks (below `min_weeks=6`) → `inconclusive` | ✅ hand-verified |
| Card files: only `herness/harness/memory/outcome_stats.py` | ✅ no other production files touched |
| Module budget 240 lines | ✅ 161 lines |

Rulings already made and not re-raised: `OutcomeConfig` imported from sibling `herness.harness.memory.settings` is allowed; `MetricWeeks`/`Windows`/`DidResult` as module-local frozen dataclasses is allowed; the `cfg.outcome`+metric → `MetricWeeks` resolver is T07-18's job (out of scope here).

## Gates (all re-run in the worktree)

- `uv run ruff check` on the two files — pass, 0 issues.
- `uv run ruff format --check` on the two files — pass, already formatted.
- `uv run mypy herness/harness/memory/outcome_stats.py` — pass, no issues.
- `uv run lint-imports` — 10 contracts kept, 0 broken.
- `uv run python -m tools.check_type_ownership` — exit 0 (only pre-existing "pending owner 06/09" INFO lines, unrelated).
- `uv run python -m tools.check_module_size` — exit 0, no output (161/240 lines).
- `PYTHONUTF8=1 uv run pytest tests/unit/harness/memory/test_memory_outcome_stats.py -v` — 13/13 passed.
- `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q` — 1091 passed, 5 deselected, 1 xfailed (pre-existing, unrelated spec-traceability xfail) — no regressions, matches the build report.
- Coverage check (`--cov=herness.harness.memory.outcome_stats --cov-branch`): 87 stmts / 2 missed (97.7% line), 18 branches / 2 partial — both comfortably clear the global ≥90% line / ≥85% branch gate.

## ⚠️ Items

None — every requirement in the brief and the binding spec sections (§3.16 U07-83..U07-85, §11 UT07-69..UT07-73, R-34) is verifiable from the diff and confirmed correct by hand computation.

## Findings

### Critical

None.

### Important

None.

### Minor

1. `herness/harness/memory/outcome_stats.py:122` — the `t = math.inf if impr > 0 else -math.inf` branch (spec U07-84 step 5: "`t=impr/se` when `se>0`, else `+inf`/`-inf` by sign of `impr`, or 0 when `impr==0`") is never exercised by any test. UT07-70's insufficient-weeks case skips the whole `if` block, and UT07-72's seasonal-shift case hits `se==0` with `impr==0` (the `t=0.0` branch), but no test drives `se==0` with `impr!=0` (e.g. a constant pre-series with a differing post-series). Coverage report confirms this line is missed. Not a correctness concern (the one-line ternary is simple and matches the spec), but it is a specified algorithm branch with zero direct test evidence.
2. `herness/harness/memory/outcome_stats.py:161` — the final `return "inconclusive"` fallback of `classify_verdict` (U07-85 step 5, "otherwise inconclusive") is never reached by any test; all UT07-71/72/73 cases hit either the early guard, `paid_off`, `worse`, or `no_effect`. Trivial code (a single return), so risk is low, but a `DidResult` with e.g. `t` between `-t_crit` and `t_crit` and `rel` above `min_rel` would exercise this path and isn't covered.

Both items are edge-case coverage gaps rather than defects; overall module coverage still clears the required 90%/85% thresholds by a comfortable margin, and the missing branches' logic is simple enough that correctness is verifiable by inspection matching the spec text.

## Verdict: Approved
