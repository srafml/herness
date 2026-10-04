# T02-17 review: Events, daily metrics, work items

Worktree agent-af1d193f8755bf662, base ce7dd88, head 6ed7d88 (1ca89bd wip, e6f572f wip, 6ed7d88 empty close; accepted per ruling).

### Spec Compliance
- ✅ Spec compliant

| Unit / test | Result | Notes |
|---|---|---|
| U02-121 260_event.sql | ✅ | event_id = staging record_id; staging columns carried; service via `stg.service_name_lookup` on `lower(service_name)` (lookup unique: aliases deduped in refdata.py:83 and cross-service aliases rejected in settings.py:152, names HAVING count(DISTINCT)=1 in 220), NULL when absent; duration only when both set and `end_ts >= ts`, BIGINT; incident_id via `number` GROUP BY … HAVING count(*)=1 (exact, case-sensitive). Column order = design 02 §4.3. CREATE OR REPLACE → idempotent. |
| U02-122 270_metric_daily.sql | ✅ | Name lookup; drops NULL service_id/date/metric_name; QUALIFY per (date, service_id, metric_name, source_tool) by `source_updated_at DESC NULLS LAST, record_id DESC`; types DATE/VARCHAR/DOUBLE. build_counts `metric_daily_raw` (all staging rows) and `metric_daily_unmapped` (service_id OR date NULL) with DELETE-then-INSERT (ruling) → re-run stable (tested). |
| U02-123 280_work_item.sql | ✅ | Staging columns; `component = components[1]`; service: component-level delivery rows, else project-level (NULL component) only when no component-level row; single distinct non-NULL service_id else NULL; team: the matched rows' single non-NULL team_id, else unique `lower(core.team.name)` = `lower(team_value)`, else NULL. Transitions semi-joined on core.work_item, `"at"` quoted (ruling). Links `SELECT DISTINCT`. Column set/order = design §4.3. |
| IT02-16 | ✅ | alias, unique name, ambiguous name, unknown name; incident one/two/no match + case-differing ref; duration >, =, <, NULL end, NULL ts; empty/typed; re-run. Fake core.incident per ruling. |
| IT02-17 | ✅ | duplicate key by time and by record_id tie-break, separate source_tool key, unmapped service, NULL date, NULL metric_name (dropped but not counted); counts exact; re-run; empty/typed with zero counts. |
| IT02-18 | ✅ | Cloud parent, DC epic link, components (first), labels, custom fields (story points, cost DECIMAL(18,2), team plain and JSON), service component/project/ambiguous/unmapped, team by mapping vs name vs ambiguous name; re-run; empty/typed for all three tables. |
| IT02-19 | ✅ | array and object changelog forms, non-status item skipped, categories incl. unmapped → NULL, orphan transition excluded. |
| IT02-20 | ✅ | outward/inward issuelinks, remotelinks INC/CHG matches in url and title → `mentions_incident`, duplicate staged row collapsed by DISTINCT. |

Test hygiene: names carry `it02_nn`, docstrings start with `IT02-nn`, module-level `pytestmark = pytest.mark.integration`; `--require-test-ids` on both files passes; `tests/unit/test_sql_coverage.py` passes.

Builder spec readings:
1. Service fallback only when zero component-level rows — accepted. core.service_map is unique per (jira_project, coalesce(jira_component,'')), so "several" at either level is unreachable from real 220 output; both readings coincide in production. (See ⚠️.)
2. Team from matched rows' single non-NULL team_id, else name lookup (also when service is ambiguous) — accepted, consistent with "matched delivery row".
3. `metric_daily_unmapped` = NULL service_id or NULL date only — accepted (verbatim U02-122).
4. No work_item filter; transition semi-join is a safeguard — accepted.
5. CAST(double epoch diff AS BIGINT) rounds sub-second differences — acceptable; see Minor.
6. sql_coverage — confirmed passing.
7. Stale docstring in test_model_stg_monitoring.py — correct observation, out of this card's files; see Minor.

- ⚠️ Cannot verify here: 260 against the real T02-16 `core.incident` (only `record_id`, `number` used; both are in U02-118's column list) — re-run IT02-16 and a 000–299 range after T02-16 merges. The IT02-18 ambiguous-service case is produced by injecting a second (WEB, ui) delivery row that 220 cannot create; the fallback-vs-NULL choice is therefore untested against any real state.

### Strengths
- Small, readable SQL (34/36/83 lines); no temp tables; clear header comments tying each rule to its unit.
- Idempotence handled explicitly (CREATE OR REPLACE; DELETE-then-INSERT for build_counts) and asserted by re-run checks in IT02-16/17/18.
- Tests assert full row tuples and exact column types, covering every column rule including edge cases (end==ts, bad ts, NULL metric_name not counted, ambiguous team name).
- `_core_late.py` avoids touching the shared `_core_build.py` that T02-16 also edits.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. herness/model/sql/260_event.sql:28 — `CAST(epoch(end_ts) - epoch(ts) AS BIGINT)` rounds (299.6 s → 300); `date_diff('second', ts, end_ts)` or `floor(...)` would give whole elapsed seconds. Spec is silent; note only.
2. herness/model/sql/280_work_item.sql:3 — the only non-ASCII character (em dash) in herness/model/sql; harmless (Jinja FileSystemLoader reads UTF-8) but inconsistent with the other SQL files.
3. tests/integration/model/test_model_core_work_item.py:199 (IT02-19) and :283 (IT02-20) — no re-run check for transitions/links (CREATE OR REPLACE makes it safe; IT02-18 re-runs the same file, so this is covered indirectly).
4. tests/integration/model/test_model_core_work_item.py:170 — `description` only checked as NULL; a non-NULL `jira_text` description is not asserted through to core (staging's rule, carried verbatim here).
5. tests/integration/model/test_model_stg_monitoring.py (docstring, not in this diff) — says IT02-16/17 belong to "T02-16"; they are T02-17's. Controller to fix at merge or in a follow-up.

### Assessment
**Task quality:** Approved
**Reasoning:** All three units implement every column rule of U02-121..123, re-runs are idempotent, and IT02-16..IT02-20 cover the rules with exact-row assertions; only cosmetic/minor points remain.

Evidence (reviewer runs, PYTHONUTF8=1): `pytest tests/integration/model tests/unit/model` → 205 passed, 1 skipped (symlinks, pre-existing); `-k "IT02_16 or … IT02_20"` → 8 passed (2 DeprecationWarnings come from repo-wide collection, not these files: the two new files pass with `-W error`); `ruff check .` clean; `ruff format --check .` clean; `tools.check_module_size` exit 0; largest new file 308 lines (≤ 400).

## Re-review 1 (fix commit 087ea5b, scope: M2 and M4 only)

- M2 (non-ASCII em dash, 280_work_item.sql:3): ✅ resolved; replaced by "(when there is none)". The remaining "§" in the header (line 2) is kept to match 200/220 on base; accepted per controller.
- M4 (description not asserted): ✅ resolved; IT02-18 now stages `description="Card *declined* on retry"` on PAY-1 and asserts exact (key, summary, description) rows for PAY-1 (non-NULL, carried as staged) and PAY-2 (NULL) (test_model_core_work_item.py:122, :172-181).
- M1, M3, M5: parked by controller, not re-checked.
- Evidence: `PYTHONUTF8=1 uv run pytest tests/integration/model/test_model_core_work_item.py -q -p no:logging -W error` → 4 passed; ruff check on the file clean.
- New findings: none.

**Task quality:** Approved
