# T02-16 review — core incident, change, problem (HEAD 80d2801, base ce7dd88)

### Spec Compliance
- ✅ Spec compliant
  - ✅ U02-118 `230_incident.sql`: all 24 columns in spec order with spec types (explicit CASTs; verified via information_schema in IT02-12). service_id = rid(business_service) else single core.service parent of an `stg.sn_rel_ci` row with child = incident CI, count(DISTINCT)=1 else NULL (230:15-22, 38). team_id = rid(assignment_group) of the latest version (stg.sn_incident is built through `m.latest`; test proves g2 over g1). sla_breached = bool_or(has_breached) over task_sla rows matched on task = incident sys_id (`source_key`) when any exist, else NOT made_sla (230:23-28, 49). acknowledged_at / customer_impact_minutes pass through staging, which emits NULL when the custom field is unconfigured (110:27-36, 46-48, 66-68); never estimated. content_hash CAST(NULL AS VARCHAR). problem_id / caused_by_change_id / ci_id rids correct.
  - ✅ U02-119 `240_change.sql`: columns/types per spec; service_id same rule as incident; outcome = coalesce(staging outcome, 'canceled' when lower(state) in canceled/cancelled) (240:92-95) — exactly "when NULL and lower(state) in (...)".
  - ✅ U02-120 `250_problem.sql`: all columns, rids for service/team, known_error BOOLEAN, root_cause_text.
  - ✅ IT02-12: with/without task_sla (breach, no breach, unmatched task row zz, no rows -> NOT made_sla, NULL made_sla), custom fields present (ack values, impact 30.5/0/"abc"->NULL) and absent (all NULL although lake has u_ack/u_impact), task_sla entity missing (R-60), full-row + types, empty lake.
  - ✅ IT02-13: CI with one service (two rels to same service -> still one), CI with two services -> NULL, business_service wins, unrelated CI, no CI, non-service parent ignored; re-run idempotent.
  - ✅ IT02-14: mapped codes, unknown -> NULL and counted in stg.cast_stats (non_null 6 / failed 2), canceled/CANCELLED -> canceled, staging outcome wins over state, re-run not double counted; full-row test incl. unknown type -> NULL and CI-relation fallback.
  - ✅ IT02-15: full columns/types/values incl. blank business_service -> NULL, bad known_error -> NULL.
- ⚠️ Cannot verify from diff: none material. "Counted" for unknown close codes is implemented by 110 staging (U02-109, §3.10), not 240 — consistent with the spec; 240 writes no cast_stats, so the INSERT-only convention is untouched.

### Strengths
- Explicit CASTs keep the empty-lake schema stable (tested for all three tables).
- Join keys trimmed consistently with `rid()` trimming; unmatched/blank keys filtered in CTEs.
- Tests assert whole rows and column types, not just spot values; edge rows (bad values, blanks, duplicate relations) included.

### Verification run (worktree)
- `PYTHONUTF8=1 uv run pytest tests/integration/model tests/unit/model -q -p no:logging` -> 207 passed, 1 skipped (symlinks).
- `-k "IT02_12 or IT02_13 or IT02_14 or IT02_15"` selects the 10 new tests.
- `uv run ruff check .` -> clean; `uv run mypy` -> no issues (202 files).
- Sizes: 230=56, 240=44, 250=20, tests 285 / 248, _core_build 45 lines (all <= 400). pytestmark = integration in both files; names/docstrings carry IDs.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
- `herness/model/sql/230_incident.sql:15-22` and `herness/model/sql/240_change.sql:70-77`: identical `ci_service` CTE duplicated (DRY). Acknowledged in the report; a `_macros.jinja` macro (U02-106, outside this card's files) would remove it — candidate for a later card.
- `tests/integration/model/test_model_core_change_problem.py:248`: `assert _columns(build_harness, table)` only checks non-empty; the empty-lake test for change/problem should compare against the expected column list (as the incident empty-lake test does at test_model_core_incident.py:220). Mitigated because the full-row tests already assert the column lists.
- `tests/integration/model/test_model_core_change_problem.py:108/121`: IT02-14 has no `standard` change type case (ENUMS lacks it); low value, the enum mapping belongs to staging.

### Assessment
**Task quality:** Approved
**Reasoning:** Every column rule of U02-118..U02-120 matches the SQL, and IT02-12..IT02-15 exercise each Expected column with whole-row assertions; gates are green and only minor DRY/test-strength polish remains.
