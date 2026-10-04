# T06-05 review (verify agent) — worktree agent-a1586c8e719045c4f @ f739f4c

**Verdict: Needs fixes** (one Important item, plan-mandated; a one-line fix. Everything else is Approved.)

### Spec Compliance
- ✅ U06-32 RunRow/TaskRow: frozen, slotted dataclasses. `_run_from_row`/`_task_from_row` present. A bad spec raises `SchemaViolation("task spec invalid: task_id=<id>")` (ValidationError also covers malformed JSON). A plain-string `last_error` becomes `{"message": text}`. build_id `str | None` and cost as a decimal string with NULL read as Decimal("0") follow the accepted rulings.
- ✅ U06-33 insert_run: `INSERT OR IGNORE`, `rowcount == 1`. See Important-1.
- ✅ U06-34 get_run / find_run_by_job / find_run_by_escalation / select_runs: SQL matches the spec, including `ORDER BY started_at DESC, run_id DESC LIMIT 1`. All read through `core.read_one`/`read_all`. select_runs adds a `run_id` tiebreak, which is harmless.
- ✅ U06-35 set_run_status: CAS `UPDATE ... CASE WHEN ? THEN ? ELSE finished_at END ... status IN (...)`. Terminal set = done/partial/failed/canceled, and every value is in the DDL status CHECK. Returns `rowcount == 1`. An empty `allowed_from` raises ValueError; the spec gives no error type.
- ✅ U06-36 update_run_fields: SELECT meta, `NotFound("run not found: run_id=<id>")` (R-19), a shallow `|` merge that stores None as JSON null (checked on the raw text), and one UPDATE with fixed column names. A no-op call issues no UPDATE.
- ✅ U06-37 insert_tasks: `ON CONFLICT DO NOTHING` with no target covers both the PK and the `task_dedup` expression index. Status 'pending', attempts 0, created_at = updated_at = now. Returns the inserted ids in input order.
- ✅ U06-38 get_task / get_task_by_dedup / select_tasks: parameterised. select_tasks orders by `created_at, task_id`.
- ✅ U06-39 ready_tasks: `select_tasks(statuses=("pending",))` and roles, then a Python sort on `(round, depth, -(priority + aging*minutes), task_id)` with minutes = `(now - created_at).total_seconds()/60`.
- ✅ U06-40 count_open counts pending+running, minus `exclude` via `NOT IN`; an empty exclude gives `NOT IN ()`, which is true in SQLite. count_tasks applies every filter given.
- ✅ __init__ 06 re-export block has all 16 names. The block runs over the 20-line budget; the doc bump to 50 is already decided.
- ✅ Acceptance: `ops.select_tasks is runs.select_tasks`, and `ui_list_tasks` is distinct (absent).
- ✅ UT06-21 (5 fns), UT06-22 (2), UT06-23 (3), UT06-24 (3), UT06-25 (1), UT06-26 (2), PT06-08 (hypothesis, 30 examples, shuffled insert order). IDs are in the function names, the first docstring lines start with the ID, and `pytestmark = pytest.mark.unit` is set.
- ⚠️ Cannot verify from diff: the full-suite figure of 3414 passed (I re-ran only tests/unit/store: 340 passed); UT02-68 parser compatibility (passes inside tests/unit/store).

Evidence I re-ran: the card tests 17 passed, runs.py coverage 100% line and 100% branch; project-scope `mypy` clean (128 files); ruff check/format clean; lint-imports 13 kept; check_module_size 0; check_type_ownership 0. runs.py is 378 lines against a budget of 380.

SQL and datetime checks: every value is bound as a parameter. The only interpolation is the constant column lists, fixed SET column names, and `?` placeholder lists. Timestamps use `clock.format_utc`, which is 27 characters and matches the DDL GLOB (UT06-26 fixed-width test). No secret or ticket text appears in error messages; they contain ids only.

### Strengths
- Clean, small helpers (`_in`, `_task_filter`, `_one_run`/`_one_task`, `_count`). No SQL is duplicated.
- The tests exercise real behaviour: CAS no-op leaves the row unchanged, raw JSON null, dedup across both conflict targets, aging overtaking priority, the task_id tiebreak, and the non-object checkpoint rejection.
- PT06-08 checks both properties: sortedness by the key with unique task_id (total), and independence from insert order (stable).

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. **Plan-mandated: `INSERT OR IGNORE` hides constraint violations as "already exists"** (herness/store/ops/runs.py:162, the `insert_run` statement). SQLite's OR IGNORE applies to NOT NULL and CHECK as well as PK. I checked this on SQLite 3.50.4: `INSERT OR IGNORE` of a row that fails a CHECK gives rowcount 0 and no error, while `INSERT ... ON CONFLICT DO NOTHING` raises `CHECK constraint failed`. A `RunRow` with a bad `kind`, `depth` or `status` (for example a typo'd `kind="funding-review"`) returns False, which the caller reads as "idempotent replay". The run is never written and nothing is raised. That is a swallowed error on the run-creation path. Fix: `INSERT INTO run (...) VALUES (...) ON CONFLICT(run_id) DO NOTHING`. It has the same idempotency key and return contract, and it matches what insert_tasks already does. Add a test that a bad kind raises `sqlite3.IntegrityError`. Because the spec names `INSERT OR IGNORE` verbatim, the controller should confirm the substitution, or rule that the spec's behaviour stays.

#### Minor (Nice to Have)
1. herness/store/ops/runs.py:240, `update_run_fields(token_usage: dict[str, object] | None)`: `dict` is invariant, so a strict caller that passes `dict[str, int]` fails mypy. The test file hits exactly this (tests/unit/store/ops/test_store_ops_runs.py:245). Prefer `Mapping[str, object]` and copy it with `dict(...)`. Tests are outside the project mypy `files`, so no gate fails, but the report's "mypy clean" holds only for the project scope.
2. tests/unit/store/ops/test_store_ops_runs.py:321: `_spec(5, priority=500.0, **skeptic)` fails mypy (arg-type), because the `**dict[str, object]` splat could bind `run`. The test still runs. Pass `run=1` explicitly or type the dict.
3. herness/store/ops/runs.py:84-86: the `_obj` error "JSON in task.checkpoint is not an object" does not name the task_id, unlike the spec error. Diagnosing a bad row is harder. This is safe (no data echoed).
4. herness/store/ops/runs.py:331-338: `ready_tasks` does not guard `aging_per_min` against NaN or inf. A NaN makes every aged priority NaN, and the order stops being total (PT06-08 then fails). Priority itself is protected by `allow_inf_nan=False`. Consider rejecting non-finite values with ValueError.
5. tests/unit/store/ops/test_store_ops_runs.py:404: `test_ut06_26_timestamps_are_fixed_width` tests timestamp formatting, which is not U06-40. The label is loose. It could carry a `test_cv_`/`test_rf_` id instead, or sit under UT06-21/24.
6. herness/store/ops/runs.py:222: `set_run_status` formats `now` even for non-terminal targets. This is harmless, and `ensure_utc` still validates `now`.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Every unit and test row matches the brief, and the gates and coverage are green. The one Important item is that the spec-mandated `INSERT OR IGNORE` hides CHECK/NOT NULL violations in `insert_run` as idempotent no-ops. Switching to `ON CONFLICT(run_id) DO NOTHING` plus one test fixes it, unless the controller rules the verbatim spec binding, in which case the card is Approved as is.
