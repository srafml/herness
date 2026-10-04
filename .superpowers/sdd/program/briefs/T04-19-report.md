# T04-19 report: Portfolio solver (pure)

Status: DONE_WITH_CONCERNS (minor, see Concerns)
Commit: c2bc706 feat(metrics): add pure portfolio solver (T04-19)
Worktree: D:\herness\.claude\worktrees\agent-a1b97ebc42c67b5df

## Implemented
- `herness/metrics/_solver.py` (300 lines, budget 300): `PortfolioCandidate`, `SolveOutcome` (frozen, slotted dataclasses; `row_flags` is a read-only MappingProxy with an entry for every candidate), `solve_portfolio`, `order_selected`, `binding_constraints`, per U04-72..U04-75.
  - The model follows U04-73 steps 1-12: sorted by candidate_id; NULL effort is not modeled and flagged `no_estimate`; `unknown_excluded`; objective floor(impact); budget ceil(effort) <= floor(budget); per-team capacity ceil(points*100) <= floor(cap*horizon*100) when `capacities` is not None (`no_points`/`no_team` flags); mandatory = 1, excluded = 0; blockers x_B <= x_A, or x_B = 0 plus `blocked_by_unestimated`; `blocked_by_noncandidate` adds a flag only; parent + descendant <= 1.
  - Parameters: num_workers, random_seed, max_deterministic_time, max_time_in_seconds, log_search_progress=False, all taken from `SolverConfig`.
  - Status mapping: OPTIMAL; FEASIBLE plus `not_proven_optimal`; INFEASIBLE; MODEL_INVALID; UNKNOWN becomes INFEASIBLE plus `no_solution_found` (DD04-10). `wall_clock_limit` is set when wall_time >= max_time - 0.5. `selected` is empty unless the status is OPTIMAL or FEASIBLE.
  - ConfigError messages: "unknown mandatory candidate <id>", "mandatory candidate <id> has no effort estimate", and, for the overlap precondition, "candidate <id> is both mandatory and excluded" (the message U04-78 uses).
  - Points and capacity are converted through `Decimal(repr(float))` before scaling by 100. This avoids float artefacts such as ceil(0.07*100) = 8.
- Tests:
  - `tests/unit/metrics/test_metrics_solver.py` (UT04-101, 102, 104, 105, 106; 18 functions).
  - `tests/unit/metrics/test_metrics_solver_props.py` (PT04-08, PT04-09 brute force over up to 15 candidates, PT04-10; marker `unit`; `commit` profile gives 200 examples; `deadline=None` on these tests because solve time varies).
  - `tests/fault/metrics/test_metrics_solver_fault.py` (FT04-04, marker `fault`, the layout from spec §2; runs in about 1 s so it is not `slow`).

## VI04-07 evidence (ortools 9.15.6755 installed)
- `CpSolver().parameters` has `num_workers` (default 0), `max_deterministic_time` (inf), `max_time_in_seconds` (inf), `random_seed` (1) and `log_search_progress` (False). All of them were set and read back as 1 / 0.5 / 2.0 / 7.
- Setting a non-existent name (`bogus_param`) raises AttributeError, so the parameter names are checked by the library.
- Status constants OPTIMAL, FEASIBLE, INFEASIBLE, MODEL_INVALID and UNKNOWN all exist in `cp_model`.
- Behaviour: max_deterministic_time=0.1 on a 300-item knapsack returns FEASIBLE (deterministic time 0.1000); on 3000 items it returns UNKNOWN. FT04-04 (5k candidates, max_time_in_seconds=1) returns INFEASIBLE with flags (no_solution_found, wall_clock_limit) after 1.0 s of wall time, with no exception.

## RED / GREEN
- RED: `pytest tests/unit/metrics/test_metrics_solver.py tests/unit/metrics/test_metrics_solver_props.py tests/fault/metrics` gave 3 collection errors: `ModuleNotFoundError: No module named 'herness.metrics._solver'`.
- GREEN: the same command gives 22 passed. The ID filter (`-k "UT04_101 or ... FT04_04"`) also gives 22 passed. The nightly profile (10,000 examples) on the property tests gives 3 passed (375 s).
- Coverage of `herness/metrics/_solver.py`: 100% line, 100% branch.
- Hypothesis distribution check over 200 instances: about 115 have a non-empty selection, about 16 are infeasible, the rest are empty-optimal, with capacity both on and off.

## Gates
All gates were run with `.venv\Scripts\python.exe -m ...` because `uv run` fails: it resolves D:\herness\pyproject.toml, which currently has a TOML parse error at line 333 (mid-merge).
- ruff check: all checks passed. ruff format --check: 109 files already formatted.
- mypy: no issues in 46 files. lint-imports: 8 contracts kept, 0 broken. check_type_ownership: exit 0. check_module_size: exit 0.
- `pytest -m "(unit or integration) and not slow"`: 939 passed, 6 deselected, 1 xfailed (the known IT00-02 marker).
- `--require-test-ids` on the new files: 22 collected, OK.

## Line counts
- `_solver.py`: 300 / 300 (hard limit 400). It fits exactly, after the docstrings were tightened. No split was needed and no new module-map row.

## Deviations and concerns
1. `order_selected` cycle breaking. The algorithm as written ("take the remaining node with the smallest key") can place a node that sits downstream of a cycle before that cycle, which breaks the U04-74 postcondition and PT04-08. The implementation instead takes the smallest-key remaining node whose unranked blockers are all in its own strongly connected component (Tarjan, computed once). This meets the postcondition; the cycle case is covered by test_ut04_102_cycle_break_respects_upstream_cycle. A self-block counts as a cycle (`had_cycle=True`).
2. Row flags `no_team`/`no_points` are set only on modeled candidates and only when capacities are enforced; an effort-less row gets only `no_estimate` (plus `blocked_by_noncandidate` when it applies). `row_flags` has an entry, possibly empty, for every candidate. T04-20 can rely on this.
3. `binding_constraints` follows the U04-75 postcondition literally. Candidates forced to 0 by a blocker still count as "unselected, modeled, non-excluded".
4. `uv run` could not be used (parent pyproject.toml mid-merge). All gates were run with the venv tools instead.
