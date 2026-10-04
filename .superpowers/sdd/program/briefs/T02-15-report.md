# T02-15 report — Dataverse staging, org/team/service, service map

Status: DONE_WITH_CONCERNS — final commit e401c65 `feat(model): T02-15 dataverse staging, org/team/service, service map` (checkpoints ad6f14b, 98adf16). Card tests: `-k "IT02_09 or IT02_10 or IT02_11 or IT02_33"` 15 passed. All pre-commit hooks (ruff, mypy, import-linter, module-size, pytest-unit) passed on every commit.

## Implemented
- `herness/model/sql/170_stg_dataverse.sql` (U02-115): generic staging `stg.dataverse_<entity>`, identical shape to 150/160 (latest rows via `m.latest`, lake columns, no `_payload`, no casting; absent entity -> metadata aliases only).
- `herness/model/sql/200_org_team_service.sql` (U02-116): `core.org`, `core.team`, `core.service`.
  - Org mode `department` iff `stg.sn_department` has a row, else `group_hierarchy` (group with a child group = org; leaf = team).
  - `parent_org_id` = parent's ID only when the parent is an org of the same mode; team org in department mode = department with same non-empty (trimmed) cost_center, lowest department sys_id (OI-04 default); hierarchy mode = parent group when it is an org. `active = coalesce(active, true)`.
  - `core.service`: `stg.sn_ci` rows whose `ci_class` is in `stg.service_ci_class`; owner = first of rid(group, owned_by), rid(group, support_group) found in `core.team`; `org_id` = that team's org.
  - Defensive: NULL IDs dropped and one row per org_id/team_id (lowest record_id) so the declared PKs hold.
- `herness/model/sql/220_service_map.sql` (U02-117): `core.service_map` candidates override (rank 1) / CMDB owner + support (rank 2) / approved suggestions (rank 3); identity key jira (project, coalesce(component,'')) or team (service, role, team); keep lowest rank, then service_id, team_id (extra deterministic tiebreakers org_id, confidence desc — do not change spec outcomes). `stg.service_name_lookup(name_lc, service_id)`: every alias + unambiguous lower(service name) not already an alias.
- LLM04: 220 reads only `stg.approved_mapping` (approved items via `approved_mapping_suggestions`, U02-60); IT02-11 creates pending/rejected items in a real ops store and shows they never reach `core.service_map`.
- No 110 defect found: IT02-10 confirms criticality from `cmdb_ci_service` only (cmdb_ci `busines_criticality` ignored) and NULL without `cmdb_ci_service` files, build succeeds (R-60). 110 unchanged.

## Tests
- `tests/integration/model/test_model_core_org_service.py`: IT02-09 (hierarchy mode; department mode by cost center after adding departments, i.e. rebuild), IT02-10 (configured classes only, class switch, criticality source, no cmdb_ci_service files, empty-lake column types).
- `tests/integration/model/test_model_core_service_map.py`: IT02-11 (precedence override > cmdb > suggestion, pending/rejected ignored, lookup excludes ambiguous names and names shadowed by aliases; empty build + re-run column contract).
- `tests/integration/model/test_model_stg_generic.py`: 170 coverage filed under IT02-33 (the ID the base uses for 150/160), incl. tombstone, absent entity + re-run, render check.
- Helper `tests/integration/model/_core_build.py` (build 000..hi with mappings, CI classes, approved items).

RED evidence (files temporarily moved away):
- IT02-33 without 170: 3 failed, 2 passed.
- IT02-09/10 without 200: `CatalogException: Table with name org does not exist` (4 failed).
- IT02-11 without 220: `Table with name service_map does not exist` (2 failed).
GREEN: `pytest tests/integration/model tests/unit/model` -> 197 passed, 1 skipped (symlinks); `tests/unit/test_sql_coverage.py` + metrics catalog -> 89 passed, 1 xfailed.

## Gates
ruff format/check clean; check_module_size exit 0 (SQL files have no §2 budget rows; SQL 27/92/78 lines); mypy/lint-imports via pre-commit hooks.

## Deviations / concerns
- IT02-11 stubs `herness.store.ops.shared.audit` (monkeypatch) because decide_review_item's audit needs a full config tree; the unit tests of shared.py do the same.
- An override without team/jira (e.g. org-only or aliases-only) still yields a service_map row with NULL team (spec: "each override"), as specified.
- Approved suggestions are not filtered to services present in `core.service` (spec does not ask for it).
