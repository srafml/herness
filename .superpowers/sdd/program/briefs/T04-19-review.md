# T04-19 review: Portfolio solver (pure), commit c2bc706

Worktree: D:\herness\.claude\worktrees\agent-a1b97ebc42c67b5df (b23419e..c2bc706)

### Spec Compliance
- ✅ U04-72 PortfolioCandidate / SolveOutcome: field names, types and Literal statuses match (herness/metrics/_solver.py:21, :32-56). Both are frozen and slotted. `row_flags` is a MappingProxy. The invariant "`selected` empty unless OPTIMAL/FEASIBLE" holds by construction at :184-185 and is asserted in UT04-101 (MODEL_INVALID), UT04-105, UT04-106, PT04-09/10 and FT04-04.
- ✅ U04-73 solve_portfolio: steps 1-12 map line by line. Sort :152; no_estimate / unknown_excluded :94-95, :180-181; one bool var per modeled row in sorted order :157; floor(impact) objective :158-159; ceil(effort) <= floor(budget) :160-161; capacity only when `capacities` is not None, with `capacities.get(t, default)` and horizon x 100 :162-163, :108-122; mandatory = 1 and excluded = 0 :164-167; x_B <= x_A, or x_B = 0 with blocked_by_unestimated :130-134, :97-98; blocked_by_noncandidate is a flag only :103-104; parent + descendant <= 1 when both modeled :135-137; parameters :170-176; status map including UNKNOWN -> INFEASIBLE + no_solution_found :23-29; wall_clock_limit at `wall >= max - 0.5` :182-183. Preconditions and exact messages :74-87.
- ✅ U04-74 order_selected: signature, the (rank, had_cycle) return, Kahn with the heap key `(-priority | +inf, candidate_id)` :245-247. The cycle-break deviation is justified; see the ruling below.
- ✅ U04-75 binding_constraints: follows the postcondition literally. The rest set = unselected, modeled, non-excluded :286-287; budget :289-291; capacity:<team> uses the same rule on points units, only when capacities are enforced :292-297; mandatory when non-empty :298-299; output sorted :300.
- ✅ Tests: every card ID has at least one function (UT04-101, 102, 104, 105, 106, PT04-08, 09, 10, FT04-04). Names and docstrings carry the IDs. `pytestmark` is `unit` or `fault`.
- ✅ TH04-08: num_workers comes from SolverConfig (`Literal[1]`), plus a fixed seed, max_deterministic_time, max_time_in_seconds and log_search_progress=False. FT04-04 (5k candidates, 1 s) returns without an exception.
- ✅ Budget: `_solver.py` is 300/300 lines. check_module_size exits 0.
- ⚠️ VI04-07 (ortools parameter names) is taken from the report's evidence. The parameter assignments at :172-176 would raise AttributeError on a bad name, and the card tests exercise them, so the evidence is consistent.
- ⚠️ For T04-20 (U04-76 / U04-80) and T04-02: the solver assumes its numeric inputs are bounded. Scenario bounds budget_usd (<= 1e12), but `Scenario.team_capacity_points` and `TeamCapacity.teams/default` (`Positive = Annotated[float, Field(gt=0)]`, no upper bound, inf allowed) are not bounded. See Minor 1.

### Recorded ruling: order_selected cycle break (CONFIRMED)
(a) The spec step does violate the U04-74 postcondition. Counterexample: selected a (prio 1, blockers b), b (prio 1, blockers a), x (prio 9, blockers b). All three have in-degree 1, so the heap starts empty. The spec takes the smallest remaining key, which is x (priority 9), and ranks it 1. That breaks b -> x, an edge that is not inside a cycle (x is not in the a/b cycle). I ran the spec algorithm verbatim in a scratch script: `{'x': 1, 'a': 2, 'b': 3}, had_cycle=True`. PT04-08 would fail on this case.

(b) The builder's version is correct and deterministic. It picks the smallest-key unranked node whose unranked predecessors all lie in its own SCC (:258-264). Such a node always exists, because the condensation of the remaining nodes has a source SCC. Picking from a source SCC never places a node before an out-of-SCC blocker. SCCs come from an iterative Tarjan that visits roots and successors in sorted order (:196-228). The Tarjan logic is correct: the low-link update on finish comes before the root test, and on-stack means visited and not yet assigned. It is deterministic. The heap key and the tie-break are unchanged from the spec. When the heap is non-empty the behaviour is exactly the spec's Kahn step. A self-block counts as a cycle, and PT04-08 asserts `had_cycle == (graph has a cycle)`. The spec text should be amended to say this (Minor 5).

### Builder's "choices the spec leaves open"
- row_flags has an entry for every candidate: consistent with U04-80 step 9 ("rows for every candidate, flags = row flags"). Accepted.
- no_team/no_points only with effort and with capacity enforced: consistent. U04-73 step 6 sits under "when capacities is not None", and unmodeled rows have no capacity term. One sub-choice: `elif` at :101 drops no_points when team is NULL. Acceptable, because with no team there is no capacity term for points to count against (Minor 3).
- Mandatory+excluded ConfigError wording "candidate <id> is both mandatory and excluded": U04-73 gives no message for this precondition, and reusing the U04-78 message is the right choice. Accepted.
- Decimal(repr(float)) before x100: this computes the intended decimal ceil/floor and avoids float artefacts (ceil(0.07*100) = 8, floor(0.29*100) = 28). It matches the spec's mathematical formula better than naive float. Accepted.
- binding_constraints literal (blocker-forced zeros count as "unselected, modeled, non-excluded"): this is the spec postcondition verbatim. Accepted.

### Verification run (card scope)
- `pytest tests/unit/metrics/test_metrics_solver.py tests/unit/metrics/test_metrics_solver_props.py tests/fault/metrics/test_metrics_solver_fault.py`: 22 passed in 8.5 s. `--hypothesis-show-statistics` shows "Stopped because settings.max_examples=200" for all three property tests, so PT04-09 brute force runs at 200 examples.
- check_module_size: exit 0. ruff check on the four files: clean. mypy on `_solver.py`: clean. Worktree left clean.

### Strengths
- A tight, readable, integer-only model. Test instances such as SMALL exercise every flag and every constraint at once.
- PT04-09/PT04-10 use an independent violation checker plus exhaustive brute force (up to 2^15). PT04-08 checks edges against a reachability oracle rather than against the implementation.
- UT04-106 covers both FEASIBLE+not_proven_optimal and UNKNOWN->INFEASIBLE+no_solution_found. MODEL_INVALID is covered through int64 overflow.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/metrics/_solver.py:71, :122, :161: an unbounded capacity or budget crashes with a non-ConfigError. I checked: capacity 1e30 gives `TypeError: __le__(): incompatible function arguments` (right-hand side above int64). Capacity `inf` (accepted by `Positive` and by Scenario.team_capacity_points) gives `OverflowError: cannot convert Infinity to integer`. Budget 1e30 also gives TypeError, but Scenario caps budget at 1e12. U04-73 lists only ConfigError. Fix (cheap and pure): clamp each right-hand side to the total of its coefficients, e.g. `min(_capacity_units(...), sum(_points_units(c) for c in members))` and `min(math.floor(budget_usd), sum(effort units))`. A limit above the total can never bind, so the result is unchanged. Also bound `team_capacity_points` / `TeamCapacity` (finite, with an upper cap) in T04-20/T04-02, so TH04-08 "Scenario bounds" covers capacity too.
2. herness/metrics/_solver.py:48-56: the U04-72 invariant is enforced only by the one constructor. A `__post_init__` check (raise ValueError if `selected` is non-empty and status is not OPTIMAL/FEASIBLE) would make it a real invariant. It costs about 3 lines, and the file is at its 300-line budget, so this is optional.
3. herness/metrics/_solver.py:99-102: with capacity enforced, a row with team NULL and points NULL gets only `no_team`. A literal reading of step 6 could give both flags. Keep the current choice, but note it where T04-20 documents row flags.
4. tests/fault/metrics/test_metrics_solver_fault.py:55: `wall_time_s < 5` is a wall-clock assertion and may flake on a heavily loaded CI runner. Consider `< 10`, or rely on the flags assertion alone.
5. Spec follow-up (not code): amend the U04-74 algorithm text in docs/impl/04-metrics-and-scoring.impl.md to match the SCC-aware cycle break ("take the smallest-key remaining node whose remaining blockers are all in its own strongly connected component"). Otherwise the spec and PT04-08 contradict each other.

### Assessment
**Task quality:** Approved
**Reasoning:** All four units match the spec, and the one deviation (the cycle break) is a correct fix for a real contradiction in the spec. TH04-08 mitigations, exact status and flag strings, the selected-empty invariant, 200-example brute force and the 300-line budget are all verified. The remaining items are robustness polish and spec follow-ups.

Verdict: Approved
