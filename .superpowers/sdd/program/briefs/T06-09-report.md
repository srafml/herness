# T06-09 report: swarm tools, spawn broker, tool lists

Status: DONE_WITH_CONCERNS (spec notes below; nothing blocking). Final commit 877f7f8.
Worktree: D:\herness\.claude\worktrees\agent-a7aeacdd88ea0c541 (branch worktree-agent-a7aeacdd88ea0c541, base e067d71)
WIP commits: c99b0ee (code + routing tests), 3bd3c1b (tool tests), 7176418 (spawn tests). Final commit: see the reply (subject `feat(harness): T06-09 swarm tools, spawn broker, tool lists`).

## Built
- `herness/harness/swarm/routing.py` (pure part only; no loop/llm imports): `role_prompt_name` (U06-97), `default_tools`, `role_budget` (U06-101). Room is left for T06-13 (map_agent_result, Route, route_task, build_hooks, build_tool_context).
- `herness/harness/swarm/spawn.py`: `SpawnDecision` (frozen dataclass) and `SpawnBroker` (U06-65). One asyncio.Lock per broker. Rules 1-8 run in design 06 §5.5 order. On approval, a `TaskSpec` child is inserted `pending` via `insert_tasks` inside `run_write` on the Blackboard writer (`bb.run_on_writer`), then `wake()` runs. A lost race gives `duplicate:<id>`. Every decision emits trace `spawn_decision` (parent_task_id, approved, reason, child_task_id, depth, dedup_key, specialty, entity_type, n_entities). Enums are included only when they are valid values, and model text is never included. Every decision also increments the real `record_counter("herness_harness_spawn_decisions_total", component="harness", labels={approved, reason})` (reason = rule name; `duplicate` without the id; `none` when approved). No subprocess or multiprocessing.
- `herness/harness/swarm/tools.py`: `PostFindingTool` (sync), `ListFindingsTool`, `RequestSubtaskTool` and `EscalateTool` (async), `build_task_tools` (U06-60..U06-64). Strict schemas: `POST_FINDING_SCHEMA`, `LIST_FINDINGS_SCHEMA`, `REQUEST_SUBTASK_SCHEMA`, `ESCALATE_SCHEMA`, `NUMBER_REF_SCHEMA`. Also `render_findings`.
- `herness/harness/swarm/__init__.py`: still docstring-only; the docstring now lists the submodules.
- Not touched: blackboard.py, harness/tools.py, pipelines/_review_common.py. Its `default_tools` stand-in is the T06-13 carry-over and matches `routing.default_tools` for the non-crosscheck analyst.

## Line counts vs budgets (tools.check_module_size exit 0)
- tools.py 326/330, spawn.py 217/220, routing.py 106/330 (T06-13 adds the rest), __init__.py 5/20.

## Tests (tests/unit/harness/swarm/)
- test_swarm_routing.py:
  - UT06-64: every role and specialty. Analyst names are checked against the real spec 05 `analyst_role(...).name`, and planner against `PLANNER.name`.
  - UT06-68: fast mode has no request_subtask; standard/deep depth boundaries; revision child_depth = max_spawn_depth; all role lists; planner list equals PLANNER.allowed_tools; analyst list is within the analyst RoleSpec allow-list; writer budget equals the writer ledger cap from the real `new_phase_budgets`; ConfigError cases.
- test_swarm_tools.py:
  - UT06-43: the real `check_tool_schema` passes on all four tools. A recursive walk confirms additionalProperties false, every property required and no id fields. The real `ToolRegistry().resolve` accepts the built task tools with the analyst RoleSpec.
  - UT06-44: real Blackboard post and list over the migrated store. Covers content format, row_key pairs becoming the stored object, rejections raising ToolInputError, filters, truncation before 12,000 chars (ids list only what was shown), one line per claim and chat-mode past_reader.
  - UT06-45: fake broker JSON results (approved and denied); escalate JSON and argument check; build_task_tools including the chat-mode reader; ConfigError "tool <name> has no backend" for each tool.
  - ST06-17: schema_hint rejects run_id/session_id on all four tools. The real `dispatch` refuses a list_findings call that carries run_id: ToolInputError, and the tool is not run. A valid call returns only the bound run's findings. Calling the tool directly with run_id raises ToolInputError.
- test_swarm_spawn.py:
  - UT06-46: one request per rule, with every later rule also failing, relaxed one at a time: spawn_disabled, max_depth, max_children, max_tasks, budget, bad_scope, duplicate:<id>, tool_escalation, then approval. The metric counts one decision per rule. Malformed args give bad_scope; a lost race gives duplicate:<id>.
  - UT06-47: child depth +1, budget == parent.scaled(0.5), priority x0.9, round copied, k_samples 1, pending row, the full trace field dict, the metric, and wake called once. A denial trace carries no model text.
  - ST06-02: a crosscheck parent requesting a general child gets tool_escalation, with no row and no wake. A PlannedTask with a `tools` or `budget` field raises ValidationError.
  - ST06-03: a loop of 10 requests gets 3 approvals, then max_children. A child's own request gets max_depth. 8 concurrent requests give exactly 3 approvals. The task cap across parents stops at max_tasks_per_run. A budget denial charges nothing.
- Results:
  - `pytest tests/unit/harness/swarm --require-test-ids`: 94 passed.
  - `pytest tests/unit/harness -q -p no:logging`: 1452 passed, 1 skipped.
  - Branch coverage: routing, spawn and tools all 100% line and 100% branch.
- Gates: ruff format and ruff check clean; mypy strict 0 errors (286 files); lint-imports 13 kept, 0 broken; check_type_ownership 0; check_module_size 0. Commits ran the real pre-commit hooks (no --no-verify, no SKIP).
- RED evidence: the implementation was drafted before the tests on this card, so there is no RED run. The tests were then written against the spec and caught real bugs:
  - An empty or overlong objective reached TaskSpec and raised ValidationError instead of a denial. It is now bad_scope.
  - The strict schema requires `format` in every NumberRef.

## Spec notes / deviations
1. row_key (U05-10 fallback, verification item 7): the in-tree strict checker (`_tools_schema._strict_ok`) rejects any object schema without `additionalProperties: false`, so an open `row_key` object could never pass `resolve`. `post_finding` therefore carries `row_key` as a nullable list (at most 16) of `{column, value}` pairs, and `PostFindingTool` converts it to the object before `Blackboard.post`. A duplicate column raises ToolInputError. A consequence of R-26: every NumberRef property, including `format` and `row_key`, is required (nullable), so the model must send `format: null`.
2. Verifier budget: U06-101 says `TaskBudget(max_steps=1, max_tokens=1, wall_clock_s=900)`, but the in-tree `TaskBudget.max_tokens` has `ge=1_000` (the U06-04 text says >= 1). `role_budget("verifier")` uses max_tokens=1_000; verifier tasks make no model call. Writer:
   - writer_tokens < 1 gives ConfigError, per the spec.
   - writer_tokens of 1..999 also gives ConfigError (the TaskBudget floor).
   - The wall clock is 4x the analyst's, capped at the TaskBudget bound of 86,400.
3. `default_tools("chat")` and `role_budget("chat")` raise ConfigError. Chat tools come from CHAT_TOOLS by mode and the chat budget from `pipelines.chat.budget` (design 06 §5.13); U06-101 lists no chat entry.
4. `build_task_tools(bb=...)` is typed `Blackboard | None` (the spec says `Blackboard`) so that ChatService can build chat tools without a Blackboard. post_finding or review-mode list_findings without bb gives ConfigError.
5. The SpawnBroker `tracer` parameter is typed as the `TraceEmitter` protocol (the real `Tracer` implements it). Events are emitted with `task_id=parent.task_id`.
6. Arguments that fail the schema shape (not a list, duplicate ids, unknown enum, empty or overlong objective) are denied `bad_scope`, following "everything else is a denial". Such arguments only reach the broker if dispatch is bypassed.
7. `list_findings`:
   - It rejects keys outside its schema with ToolInputError (defense in depth for TH06-17), and turns a FindingFilter ValidationError into ToolInputError.
   - Chat mode calls `past_reader(entity_type=, entity_ids=, limit=)` with keyword arguments (U06-44 signature) and ignores the other filters.
   - Claims are rendered on one line (line breaks become spaces). A truncated listing ends with `truncated: <shown> of <n> findings shown`.
8. The metric reason label for approvals is `none`; the spec names only the denial rule names.

## Concerns
- A pre-existing flaky test (T06-08; this card did not change it): tests/unit/harness/test_blackboard.py::test_st06_05_pii_checked_before_numerals_and_ids asserts that "17" is absent from logs that contain random ULID task ids. It failed once in the pre-commit pytest-unit hook (task id `...P217QV...`) and passed on retry. Suggested follow-up: use fixed ids, or check only the fields derived from the claim.
- T06-13 carry-overs: replace `pipelines/_review_common.default_tools` with `routing.default_tools`, and add the §2 re-exports in swarm/__init__.

## Fix round 1 (commit 3e0aebb)
- I-1 UT06-46: each step now also fails every LATER rule: steps 1-3 use max_children_per_task=0 and max_tasks_per_run=1 (the run already has more tasks), steps 1-5 use tokens_cap=1, steps 1-6 request the unknown scope ["zz"] whose dedup key is pre-inserted (so the bad_scope step is also a duplicate), and every step uses a parent with narrow tools (tool_escalation). Mutation probe on spawn.py: (a) swapping rules 3 and 4 made the test fail (red); (b) moving the catalog bad_scope check after the dedup lookup made the test fail (red); spawn.py restored with `git checkout` (no production diff in the fix commit).
- M2 ST06-17: a second running review run with its own posted finding; the bound list_findings tool never lists it (through dispatch, and when called with a context of the other run).
- M3 skipped: one `__all__` statement is 104 chars, so ruff formats it to 7 lines and tools.py would be 331 > 330 (the condition in the fix request).
- M5: test_st06_05_pii_checked_before_numerals_and_ids now posts through a Blackboard with fixed run/task ids ("run_" + "A"*26, "task_" + "B"*26); the PII rejection happens before any store lookup, and the intent (no digit, email or entity id echoed) is unchanged.
- Tests: tests/unit/harness/swarm + tests/unit/harness/test_blackboard.py: 131 passed (--require-test-ids). Gates: ruff, mypy (286 files), lint-imports 13 kept, check_module_size 0, check_type_ownership 0; commit hooks incl. pytest-unit passed.
