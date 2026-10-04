# T06-01 report: Shared swarm types

Status: DONE_WITH_CONCERNS
Commit: c49da43 feat(types): add shared swarm task, finding and challenge types (T06-01)
Worktree: D:\herness\.claude\worktrees\agent-a65b4b3b805e83310

## What was implemented
- `herness/core/types/swarm/tasks.py` (299 lines, budget 300): U06-01 vocabularies (PEP 695 `type` aliases; `SKEPTIC_CHECKS = get_args(SkepticCheck.__value__)`), U06-02 EntityScope, U06-03 TaskInputs, U06-04 TaskBudget (`to_budgets(now)`, `scaled(factor)`), U06-05 TaskSpec, U06-06 PlannedTask (R-28), U06-07 Finding, U06-09 CheckResult, U06-10 Challenge, U06-11 CrossCheck, U06-12 VerificationRecord, U06-140 SwarmTaskState. All models `extra="forbid", frozen=True, strict=False`. No `impact_usd` (R-75).
- `herness/core/types/swarm/__init__.py` (21 lines, budget 40): re-exports only.
- `herness/core/types/__init__.py` (150 lines, budget 150): swarm import block added (compact, `# fmt: skip`, `# noqa: I001` on the first import of the block) and names added to `__all__`.
- `tests/unit/core/types/test_swarm_tasks.py` (inherited from the cut-off agent, reviewed and kept): fixes: renamed `test_t06_01_reexports_are_identical` -> `test_ut06_01_...` (test-ID rule); replaced a confusing local-time `astimezone()` construct with an explicit +02:00 datetime; added the `"withdrawn:"` (empty detail) reason case.
- No pyproject change needed (mypy `files = ["herness", "tools"]`; `herness.core.types` already in the import contracts).

## RED / GREEN
- RED: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/unit/core/types/test_swarm_tasks.py -q -p no:logging` -> `ModuleNotFoundError: No module named 'herness.core.types.swarm'` (collection error).
- GREEN: same command -> `80 passed`; coverage (line+branch) of herness.core.types.swarm 100 %.
- `-k "UT06_01 or UT06_02 or UT06_03 or UT06_04 or UT06_06 or UT06_07 or UT06_93"` -> 80 passed.

## Gates
- ruff format --check: 79 files already formatted; ruff check: all checks passed.
- mypy (strict): Success, 35 source files.
- lint-imports: 8 kept, 0 broken.
- tools.check_module_size: exit 0.
- tools.check_type_ownership: exit 1, only OWN010 (missing ChatAnswer, ChatEvent, Coverage, Paragraph, RankedEntity, RecommendationItem, ReportDraft, Section) and the resulting OWN031 — the T06-02 names, expected per dispatch.
- Full suite `-m "(unit or integration) and not slow"`: 628 passed, 2 failed: `test_ut00_48_init_reexports_only` and `test_it00_02_check_scripts_pass_on_repo` — both only because the 8 T06-02 names are absent (the latter runs check_type_ownership). Expected.
- `--require-test-ids` option is not available on this branch (T11 plugin not merged); all new test names follow `test_ut06_NN_...`.

## Deviations / decisions
1. `SwarmTaskState.from_envelope(checkpoint, *, task_id) -> Self | None` (classmethod, from the inherited tests): the single read-back point for U06-140 — returns None when the `state` key is absent, `model_validate` otherwise, and maps `ValidationError` to `SchemaViolation("task state invalid: task_id=<id>")`. The spec describes this behaviour but names no method; a method on the type (like `TaskBudget.to_budgets`) is not a module-level function, so R-75 is respected. Callers in U06-53/U06-89/writer can use it.
2. Patterns applied where the spec lists no explicit pattern but the field is an id of known kind: Finding `run_id` (RUN_ID), `task_id` (TASK_ID), `supersedes`/`merged_into` (FINDING_ID); PlannedTask `entity_ids` items bounded 1–200 chars like EntityScope. `SwarmTaskState.phase` has `min_length=1` (kept `str`, not a Literal of run statuses, per the spec).
3. `TaskBudget.scaled` with factor outside (0, 1] (incl. NaN) raises `ValueError` (spec states the precondition but no error class).
4. VerificationRecord.reason: `<RejectReason>` or `<RejectReason>:<non-empty detail>`, ≤ 200 chars.
5. RUF022 (isort-style `__all__` sort puts `SKEPTIC_CHECKS` first) conflicts with OWN032 (plain `sorted()`); `herness/core/types/__init__.py` `__all__` carries `# noqa: RUF022 - OWN032 needs plain sorted order`. The swarm package `__all__` uses the RUF022 order (OWN032 does not check subpackage order).
6. To fit budgets: `ValidationError` imported from `pydantic_core` and `AfterValidator`/`model_validator` from `pydantic.functional_validators` (both allowed by OWN020); `to_budgets` builds `Budgets(**self.model_dump(), deadline=...)`.

## Concerns
- `herness/core/types/__init__.py` is exactly at its 150-line budget (impl 00). T06-02 (8 names) and T07/T09 owners will exceed it unless they use a compact layout or the budget is raised — controller ruling likely needed.
- `tasks.py` is at 299/300 lines; T06-02 goes into `drafts.py`, so no further growth expected here.
- `TaskBudget` accepts `max_tokens >= 1` (spec), but spec 05 `Budgets.max_tokens` is `ge=1_000`; `to_budgets` on a budget below 1000 tokens (e.g. after `scaled`) raises pydantic `ValidationError`. Spec inconsistency between U06-04 and 05 Budgets; not changed here.

## Fix round 1

Commit: 78e790c fix(types): align TaskBudget with Budgets bounds, review fixes (T06-01)

1. TaskBudget bounds now match spec 05 Budgets (tooling.py:100-103): `max_steps` 1..200, `max_tokens >= 1000`, `wall_clock_s` 1..86400, `max_cost_usd >= 0` (2 decimals kept). `scaled()` floors tokens at 1000 (steps and wall clock at 1; upper bounds cannot be exceeded because factor <= 1). New `test_ut06_02_task_budget_outside_budgets_bounds_rejected` (999 tokens, 201 steps, 86401 s); `test_ut06_02_to_budgets_and_scaled` now checks that scaled small budgets (1/1000/1 x 0.1 and 3/1500/10 x 0.25 -> 1/1000/2) still convert via `to_budgets`, as does the widest budget (200 steps / 86400 s). This resolves the earlier Budgets-mismatch concern.
2. `herness/core/types/__init__.py`: the block-wide `# noqa: I001` is removed; the swarm import is back to the normal isort layout (one name per line, SKEPTIC_CHECKS first) and the block sorts cleanly.
3. `CrossCheck.values` items are `Annotated[float, Field(allow_inf_nan=False)]`; UT06-07 test covers nan, inf, -inf.
4. `SwarmTaskState.from_envelope` returns None for `{"state": None}` (like `LoopCheckpoint.from_envelope`); pinned in the UT06-93 test.
5. `__all__` in `herness/core/types/__init__.py` is laid out compactly under `# fmt: off` / `# fmt: on`, still plain sorted (OWN032 passes), keeping `# noqa: RUF022`. The file is now 108/150 lines.

Gates: ruff check clean, ruff format --check (79 files formatted), mypy (35 files, 0 errors), lint-imports (8 kept), check_module_size exit 0 (tasks.py 300/300). check_type_ownership reports only the pending T06-02 names (OWN010 x8 plus OWN031). Card tests: 83 passed, 100 % line and branch coverage of herness.core.types.swarm. Full suite: 631 passed, 2 expected failures (UT00-48, IT00-02, both caused by the T06-02 names).

Remaining concern: tasks.py is exactly at its 300-line budget.
