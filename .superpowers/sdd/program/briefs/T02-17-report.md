# T02-17 report: Events, daily metrics, work items

Worktree: D:\herness\.claude\worktrees\agent-af1d193f8755bf662 (branch worktree-agent-af1d193f8755bf662, base ce7dd88).

## Implemented
- `herness/model/sql/260_event.sql` (U02-121, 34 lines): `core.event` (event_id = staging record_id,
  source_tool, ts, service_id, host, severity, alert_name, status, dedup_key, duration_s BIGINT,
  incident_id). Service via `stg.service_name_lookup` on `lower(service_name)`; incident = the
  `core.incident.record_id` whose `number` equals `incident_ref` when exactly one row matches
  (exact, case-sensitive match); duration = `epoch(end_ts) - epoch(ts)` only when both set and
  `end_ts >= ts`.
- `herness/model/sql/270_metric_daily.sql` (U02-122, 36 lines): `core.metric_daily` (date, service_id,
  metric_name, value, unit, source_tool); drops NULL service_id/date/metric_name; per key keeps
  latest `source_updated_at` (NULLS LAST) then highest `record_id`. `stg.build_counts` rows
  `metric_daily_raw` and `metric_daily_unmapped` via DELETE-then-INSERT (idempotent re-run). No temp
  tables (a Jinja macro inlines the resolved subquery twice).
- `herness/model/sql/280_work_item.sql` (U02-123, 83 lines): `core.work_item` (design §4.3 column
  order), `core.work_item_transition` (semi-join on core.work_item; `"at"` quoted),
  `core.work_item_link` (SELECT DISTINCT from stg.jira_link). All CREATE OR REPLACE.
- Tests: `tests/integration/model/_core_late.py` (44 lines; helper `build_late`: runs 000..220, a
  `setup(con)` hook, then only the requested later files, so 230-250 are never run),
  `tests/integration/model/test_model_core_event_metric.py` (220 lines; IT02-16 x2, IT02-17 x2),
  `tests/integration/model/test_model_core_work_item.py` (~310 lines; IT02-18 x2, IT02-19, IT02-20).
  All module-level `pytestmark = pytest.mark.integration`.

## Evidence
- RED: `PYTHONUTF8=1 uv run pytest tests/integration/model/test_model_core_event_metric.py -q -p no:logging`
  -> 4 failed (CatalogException: core.event / core.metric_daily do not exist); same for
  test_model_core_work_item.py -> 4 failed (core.work_item* do not exist).
- GREEN: `PYTHONUTF8=1 uv run pytest tests/integration/model tests/unit/model -q -p no:logging`
  -> 205 passed, 1 skipped (symlinks unavailable, pre-existing).
  Card IDs: `-k "it02_16 or it02_17 or it02_18 or it02_19 or it02_20"` -> 8 passed.
  `--require-test-ids` on the two new files + tests/unit/test_sql_coverage.py -> 12 passed.
- Gates: ruff format / ruff check clean; mypy (project) no issues; mypy --explicit-package-bases on
  the 3 new test files clean; lint-imports 13 kept 0 broken; check_type_ownership exit 0;
  check_module_size exit 0. Full suite not run (per dispatch).
- Commits: `SKIP=pytest-unit` (pre-existing red ST05-13(a) / IT00-01 in that hook), no --no-verify,
  PRE_COMMIT_ALLOW_NO_CONFIG not set (dispatch overrides implementer-rules on this point).

## Deviations / rulings applied
- IT02-16 does NOT run 230 (T02-16 in parallel): per controller ruling the test creates a minimal
  `core.incident (record_id VARCHAR, number VARCHAR)` in the `setup` hook before running 260.
  After T02-16 merges, 260 reads the real table; only `record_id` and `number` are used.
- IT02-18 / IT02-19 / IT02-20 use hooks to exercise rules the staged lake cannot produce:
  a second `core.service_map` delivery row for (WEB, ui) (service_map is unique per key, so
  "several" is otherwise unreachable), an orphan `stg.jira_transition` row (all staged issues are
  in core.work_item), and a duplicate `stg.jira_link` row (staging is already DISTINCT).
- Did not edit shared `_core_build.py` (possible conflict with T02-16); new `_core_late.py` instead.

## Spec notes / interpretations (for review)
1. U02-123 service fallback: I read "matching (project, component); else matching (project, NULL
   component)" as: fall back to the project level only when there are zero component-level
   delivery rows; several component-level services give NULL (no fall-through). Tested (WEB).
2. U02-123 team: "team_id of the matched delivery row" = the single distinct non-NULL team_id of
   the chosen rows when the service resolved; otherwise the core.team name lookup (single team
   whose lower(name) = lower(team_value)).
3. U02-122: `metric_daily_unmapped` counts rows with NULL service_id or NULL date (verbatim);
   rows dropped only for NULL metric_name are not counted in it (tested, m7).
4. U02-123 lists no filter for core.work_item, so every stg.jira_issue row is kept; the
   "transitions only for issues in core.work_item" rule is then a semi-join safeguard.
5. duration_s uses CAST(epoch diff AS BIGINT) (DuckDB rounds fractional seconds).
6. tools/sql_coverage: the new tables are all named in tests; no render-time table names here.
7. test_model_stg_monitoring.py docstring says IT02-16/17 were reassigned to "T02-16"; they are
   T02-17's (this card). Left untouched (not my file).

## Concerns
- None blocking. On this branch alone, running the build range 000..299 fails at 260 until 230
  (T02-16) lands, since 260 reads core.incident; no existing test runs that range.

## Final
- Commits: 1ca89bd wip (260, 270), e6f572f wip (280), 6ed7d88 `feat(model): T02-17 events, daily
  metrics, work items` (empty closing commit; all code is in the two wip commits — squash on merge
  if a single card commit is wanted).

## Fix round 1
- M2: `herness/model/sql/280_work_item.sql` header: em dash replaced by "(when there is none)".
  Note: the header still contains `§` (as do 200/220/260/270 headers on base); not changed.
- M4: IT02-18 fixture PAY-1 now has `description="Card *declined* on retry"` (DC wiki text,
  returned unchanged by jira_text); the test asserts PAY-1's non-NULL description and PAY-2's NULL
  in core.work_item. test_model_core_work_item.py is 316 lines.
- Tests: `PYTHONUTF8=1 uv run pytest tests/integration/model -q -p no:logging` -> 49 passed;
  ruff format/check clean; check_module_size exit 0.
- Commit: 087ea5b `fix(model): T02-17 review round 1`.
