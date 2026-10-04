# T06-05 re-review, fix round 1 (verify agent): worktree agent-a1586c8e719045c4f @ 9a2729a

**Verdict: Approved**

Scope: f739f4c..9a2729a, made up of code fix 47ebc0f and docs commit 9a2729a. The builder's fix-round report is missing, so I judged from the diff and from runs I did myself.

## Findings from the first review
- ✅ **Important-1 (ruled substitution).** `insert_run` now runs `INSERT INTO run (...) VALUES (?x12) ON CONFLICT(run_id) DO NOTHING` (herness/store/ops/runs.py:159-165) and still returns `rowcount == 1`. The new test `test_ut06_21_insert_run_constraint_violation_raises` inserts `kind="bogus"` and expects an error. That error arrives as `SchemaViolation("ops constraint failed in <op>: CHECK constraint failed ...")`, not as a bare `sqlite3.IntegrityError`, because `core.run_write` maps every IntegrityError to SchemaViolation (core.py:167-169). That is the store's contract, so the check is correct. The test also confirms no row was written. The duplicate case still returns `[True]` then `[False]` (test lines 114-115).
- ✅ **Minor-1.** `update_run_fields(token_usage: Mapping[str, object] | None)`. `meta_patch` was already a Mapping.
- ✅ **Minor-2.** The skeptic splat is replaced by explicit keywords (`role=`, `round=`, `inputs=`). `uv run mypy tests/unit/store/ops/test_store_ops_runs.py` reports no issues.
- ✅ **Minor-3.** `_obj(text, field, ref)` adds `: task_id=<id>` or `: run_id=<id>` to the error. The message carries ids only, never content. The `_req_obj` helper is gone and callers use `_obj(...) or {}`, which behaves the same. The tests anchor the full message with `$` for both `task.checkpoint` and `run.meta`.
- ✅ **Minor-4.** `ready_tasks` raises `ValueError("aging_per_min must be finite")` before querying (runs.py:344-348). The test covers nan, inf and -inf.
- ✅ **Minor-5.** Renamed to `test_ut06_24_timestamps_are_fixed_width` with the docstring "UT06-24 insert_tasks stores created_at ...". This fits, because UT06-24 covers U06-37 insert_tasks.
- ⏸ **Minor-6.** Parked, as agreed. The code is unchanged and still harmless.

## Docs commit 9a2729a
- ✅ The only change in `docs/impl/06-swarm-and-pipelines.impl.md` is one line: the `herness/store/ops/__init__.py` (06 block) row budget goes from `20 (06 block)` to `50 (06 block)`. No other file or line changed.

## Budget and conventions
- ✅ runs.py is 380 lines, exactly the 380 budget. `check_module_size` exits 0.
- ✅ The new and renamed tests follow the ID convention: the name prefix `test_ut06_2x_`, a docstring whose first line starts with the ID, and `pytestmark = pytest.mark.unit` unchanged.

## Evidence (re-run)
- `PYTHONUTF8=1 uv run pytest -q -p no:logging tests/unit/store/ops/ --cov=herness.store.ops.runs --cov-branch`: 137 passed. runs.py coverage is 100% line (177/177) and 100% branch (32/32).
- `uv run ruff check .`: all checks passed. `ruff format --check`: 297 files already formatted.
- `uv run mypy`: no issues in 128 source files. The test file checked on its own is also clean.
- `uv run python -m tools.check_module_size`: rc 0. `lint-imports`: 13 kept, 0 broken.

## New findings
- Critical: none.
- Important: none.
- Minor: none. (Note only: runs.py is now at its 380-line budget, so the next change to this file needs lines trimmed first.)

## Assessment
Every finding from the first review is fixed correctly, nothing regressed, and all gates are green. **Approved.**
