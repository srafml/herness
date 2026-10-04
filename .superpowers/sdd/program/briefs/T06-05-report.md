# T06-05 report: Run and task store access

Status: DONE_WITH_CONCERNS
Commit: f739f4c feat(store): add ops runs area (T06-05) on branch worktree-agent-a1586c8e719045c4f

## Built
- `herness/store/ops/runs.py` (378 lines, budget 380): U06-32..U06-40.
  - `RunRow`, `TaskRow` are frozen, slotted dataclasses. `build_id: str | None`. `cost_usd` is stored as a decimal string, and NULL reads as `Decimal("0")`. JSON columns are typed `dict[str, object]`.
  - `_task_from_row`: `TaskSpec.model_validate_json`; a failure raises `SchemaViolation("task spec invalid: task_id=<id>")`.
  - `last_error`: a JSON object is returned as is; a JSON string or non-JSON text becomes `{"message": text}`.
  - Write functions take `conn` first: `insert_run` (INSERT OR IGNORE), `set_run_status` (CAS; sets `finished_at` for the terminal statuses done/partial/failed/canceled; empty `allowed_from` raises ValueError), `update_run_fields` (SELECT meta, NotFound if missing, shallow merge where None is kept as JSON null, one UPDATE), `insert_tasks` (ON CONFLICT DO NOTHING, covering both the PK and `task_dedup`; returns the ids actually inserted, in input order).
  - Read functions go through `core.read_one` / `read_all`: `get_run`, `find_run_by_job`, `find_run_by_escalation` (ORDER BY started_at DESC, run_id DESC LIMIT 1), `select_runs` (ORDER BY started_at, run_id), `get_task`, `get_task_by_dedup`, `select_tasks` (ORDER BY created_at, task_id), `ready_tasks` (sorted in Python by the design 06 §5.4 key: round, depth, -(priority + aging*minutes), task_id), `count_open`, `count_tasks`.
  - The only dynamic SQL is the `IN (?, ...)` placeholder list. An empty collection gives `IN ()`, which matches nothing in SQLite.
- `herness/store/ops/__init__.py`: new "# 06 runs" import block and `__all__` block (16 names, standard one-per-line layout).
- `tests/unit/store/ops/test_store_ops_runs.py` (411 lines): UT06-21..UT06-26 and PT06-08 (hypothesis, 30 examples, random insert order). Tests migrate the tmp DB on top of `ops_store`.

## Evidence
- RED: before runs.py existed, pytest failed at collection with `ImportError: cannot import name 'runs' from 'herness.store.ops'`.
- GREEN: `pytest -k "UT06_21 or ... or PT06_08"`: 17 passed. Coverage of runs.py is 100% line and 100% branch.
- Full `pytest -m "(unit or integration) and not slow"`: 3414 passed, 5 skipped, 1 xfailed (pre-existing).
- Gates: ruff format/check, mypy, lint-imports (13 kept; `ops-areas-acyclic` already covers runs through its wildcard, so pyproject.toml was not edited), check_module_size 0, check_type_ownership 0. All pre-commit hooks passed.

## Deviations / concerns
1. The `__init__.py` 06 block is 36 lines against its 20-line budget (the tool does not enforce this row). UT02-68's parser needs one `__all__` name per line, so the `__all__` part alone takes 17 lines for runs. The import block follows the ruff/isort layout of the 02 blocks. The findings area will also land in this block, so the spec budget of 20 for both areas looks infeasible. The controller should rule on raising it.
2. check_type_ownership (OWN041) requires `TaskSpec` to be imported from `herness.core.types`, not from `herness.core.types.swarm`. Done.
3. detect-secrets flagged the Crockford alphabet constant in the test file. I used an inline `# pragma: allowlist secret` instead of a baseline update, so `.secrets.baseline` is unchanged.
4. The spec gives no error type for an empty `allowed_from` in `set_run_status`; I chose ValueError.
5. No missing columns: the migrated schema has every column the units need. `select_tasks` and `get_run` are exported from `herness.store.ops` under their own names. `ui_list_tasks` does not exist yet, so nothing collides.
