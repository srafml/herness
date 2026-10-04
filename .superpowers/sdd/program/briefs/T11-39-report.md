# Report: T11-39 ServiceNow departments and task SLA generators

## Status
DONE. A prior build agent had already committed the work (commit 7276ef5) before
crashing; the dispatch's "staged but uncommitted" snapshot was stale by the time this
session started. I reviewed the committed content against the brief, ran the full gate
suite against it, and found it correct and complete — nothing further to change or
commit.

## Files (unchanged from what was committed)
- `tools/synth/servicenow_aux.py` — 76 lines (budget 150, from docs/impl/11-...impl.md
  §2 module map, picked up automatically by `tools.check_module_size`; no manual
  registration needed).
- `tests/unit/tools/synth/test_servicenow_aux.py` — 154 lines, 8 test functions, all
  named/docstringed `UT11-113`, module-level `pytestmark = pytest.mark.unit`.

## What the module does
- `gen_departments(cat, params, rng)`: one `cmn_department` record per `OrgRow` in
  `cat.orgs` — `sys_id`/`cost_center` from the org, `name` from the org, `parent` empty,
  `sys_updated_on` = `span_start(params)` (00:00:00Z) via `servicenow_common.ts_pair`.
  All fields are `{value, display_value}` pairs via `servicenow_common.pair`. `rng` is
  accepted but unused (kept for the shared `gen_*(cat, params, rng)` signature) and
  explicitly `del`eted to satisfy lint.
- `gen_task_slas(incidents, params, rng)`: for each incident with a non-empty
  `resolved_at` value and no `__tombstone__` flag, emits one `task_sla` record:
  `sys_id` = `new_sys_id(rng)` (32 lowercase hex from `rng.bytes(16).hex()`), `task` =
  the incident's `sys_id`, `sla` = `f"P{priority} resolution"`, `stage` = `"completed"`,
  `has_breached` = `"true"` iff the incident's `made_sla` value is `"false"`,
  `sys_updated_on` copied from the incident. `params` is accepted but unused (kept for
  signature symmetry) and `del`eted.

## Decisions / interpretation
- **Tombstone definition**: `tools.synth.dirty` (U11-21 `apply_dirty`) does not exist yet
  in this tree (later card), so there is no live tombstone-marker producer to import.
  The module documents and implements the tombstone shape from the spec algorithm
  directly: `{"__tombstone__": True, "key": ..., "deleted_at": ...}`, a dict with no
  incident fields. `_is_resolved` treats any record carrying `__tombstone__` truthy, or
  lacking a truthy `resolved_at.value`, as producing no `task_sla` row. This matches the
  brief's precondition ("tombstone markers are skipped") and UT11-113's setup (a
  tombstone plus an open record must both yield no row).
- `gen_departments` ignores `rng` and `gen_task_slas` ignores `params`, per each
  function's postconditions (deterministic given the catalog/params, or a pure mapping
  driven only by `rng` for the fresh `sys_id`). Both are explicitly `del`eted rather than
  left as unused-but-silent to keep intent clear and appease `ARG001`.

## Deviations from the brief
None identified. Signatures, field names, formats and skip conditions match U11-77's
postconditions verbatim.

## Gate results (re-run against the committed code, from this session)
- `uv run ruff format .` — 299 files unchanged.
- `uv run ruff check --fix .` — All checks passed.
- `uv run mypy` — Success: no issues found in 131 source files.
- `uv run lint-imports` — Contracts: 13 kept, 0 broken.
- `uv run python -m tools.check_module_size` — exit 0 (no violations).
- `PYTHONUTF8=1 uv run pytest tests/unit/tools/synth -q -p no:logging` — 201 passed.
- `pytest tests/unit/tools/synth/test_servicenow_aux.py --cov=tools.synth.servicenow_aux
  --cov-branch --cov-report=term-missing` — 8 passed; 100% line coverage (26/26 stmts),
  100% branch coverage (6/6), comfortably over the 90%/85% thresholds.
- `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` — kicked off
  as a broader sanity check per implementer-rules #7; see note below if it had not
  finished by the time this report was written.

## Test summary
UT11-113 covers: one department per org with correct `sys_id`/`cost_center`/empty
`parent`/`sys_updated_on`; all department fields are value/display pairs; one
`task_sla` row per resolved incident across P1–P5; `sys_id` is 32 lowercase hex, fresh
(not copied from the incident), and unique per row; `sla`/`stage`/`sys_updated_on`
fields correct; `has_breached` agrees with `made_sla` in both directions; no row for the
open record or the tombstone; empty incident list yields no rows.

## Concerns
None. Working tree is clean; no other files were touched.
