# T02-15 review (verify agent) — head e401c65, base c3eee74

**Verdict: Approved** (no Critical or Important findings; 5 Minor)

### Spec Compliance
- ✅ Spec compliant.

| Row | Status | Notes |
|---|---|---|
| U02-115 `170_stg_dataverse.sql` | ✅ | Same as 160 apart from names (checked by diffing the two files after a name substitution): `m.latest` only, lake columns, no `_payload`, no casting, so no `cast_stats` rows are needed. |
| U02-116 `200_org_team_service.sql` | ✅ | Mode switch works as `EXISTS stg.sn_department` (200:32, 62, 67, 74). Hierarchy orgs are groups that appear as some group's `parent` (200:25-27). `parent_org_id` is the parent's id only when that parent is an org in the same mode, else NULL (200:37). Department-mode team org is the department with the same non-empty cost center, picked by `arg_min` over raw `sys_id`, so the lowest sys_id wins (OI-04, 200:54-60). Hierarchy-mode team org is the parent group when it is an org, and only leaf groups become teams (200:72-74; `NOT IN` is null-safe because `core.org.org_id` is never NULL). `active` = `coalesce(active, true)`. `business_owner_team_id` = `coalesce(owned_by team, support_group team)`, both looked up in `core.team`, and `org_id` comes from the chosen team (200:85-86). Column order and types match design §4.3; the tests assert `criticality` is SMALLINT. |
| U02-117 `220_service_map.sql` | ✅ | Ranks: override 1, CMDB owner and support 2, approved suggestions 3. The support row is emitted only when its team exists and differs from the owner. Override `org_id` = `coalesce(override, team, service)`. For suggestions, `jira_component` becomes a `delivery` row with org = team org or else service org, and `team` becomes a `support` row with the team org. Identity keys follow step (2): jira rows keyed by (project, `coalesce(component, '')`), other rows by (service, role, team). The keep order is rank, then service_id, then team_id (220:59-63). The lookup table holds every alias plus each unambiguous `lower(name)` that is not already an alias (220:68-78). `MappingsConfig._check_aliases` guarantees an alias maps to only one service, so alias rows cannot be ambiguous. |
| LLM04 control | ✅ | 220 reads only `stg.approved_mapping`, which comes from `approved_mapping_suggestions` (U02-60). IT02-11 creates pending and rejected items in a real ops store, asserts only the 5 approved items come back, and shows the pending s5/t3 and NEW/GONE rows are absent from `core.service_map`. |
| IT02-09 | ✅ | Covers hierarchy mode, including a leaf with no parent, an orphan whose parent is missing, and `active=false`. Then departments are added and the build re-runs: department mode by cost center, lowest sys_id for the CC1 tie (d1 over d2), a missing parent gives NULL, and a blank cost center gives NULL. |
| IT02-10 | ✅ | Only configured classes become services, and switching to `cmdb_ci_db` changes the set. Criticality comes from `cmdb_ci_service` only: the `cmdb_ci` value "4 - low" is ignored in favour of 2, and `biz`, which has only a `cmdb_ci` row, gets NULL. With no `cmdb_ci_service` files, criticality is NULL, 200 still runs and the column types hold (R-60). This confirms the 110 `stg.sn_ci` merge is correct. |
| IT02-11 | ✅ | Tests override > cmdb (s1 owner/t1), cmdb > suggestion (s1 support/t2), override > suggestion (PAY/api) and the service_id tie-break (WEB: s3 wins over s4). Pending and rejected items are ignored. The lookup drops the ambiguous "dup" and the "search" name already taken by an alias. It also covers the D1 default of an empty lake with no suggestions, and a re-run. |
| IT02-33 (170 coverage) | ✅ | Latest rows, a tombstone dropped, an absent entity, a re-run, and a render check. |
| Test hygiene | ✅ | `pytestmark = pytest.mark.integration`, IDs in the function names, and each docstring starts with its ID. |
| Re-run / `cast_stats` | ✅ | Every table is `CREATE OR REPLACE`. None of 170, 200 or 220 inserts into `stg.cast_stats`, so there are no rows to delete first. |

- ⚠️ Cannot verify from diff: nothing blocks approval. Note that the unit table cites `Tests | IT02-08` for U02-113..115 (impl line 2669), but IT02-08 is a 110 scenario (line 3630). Filing the 170 tests under IT02-33, as T02-14 did for 150/160, is consistent. The spec mis-citation is pre-existing.

### Test evidence (re-run by the reviewer)
- `pytest tests/integration/model tests/unit/model`: 197 passed, 1 skipped (symlinks). This matches the report.
- `-k "IT02_09 or IT02_10 or IT02_11 or IT02_33"`: 15 passed.

### Strengths
- The candidate CTE maps one-to-one onto U02-117 steps (1) to (4), and the identity keys and ranks are exactly as specified.
- The org and team tables are deduplicated per primary key, and NULL ids are dropped.
- The tests are value-exact and use a real ops store for the LLM04 path.

### Builder concerns assessed
1. **Audit stub in IT02-11** (test_model_core_service_map.py:138): acceptable. It is an established pattern (tests/unit/enrich/test_review_items.py:38, tests/security/test_st02_shared.py:67), and the stub touches only the audit side effect, not the approval filter.
2. **Override rows with a NULL team**: spec-literal ("each override"). See Minor 1.
3. **No check that a suggestion's service is in `core.service`**: not required by the spec. See Minor 3.
4. **Extra tie-breaks (`org_id`, then `confidence DESC`)**: harmless. They only act after rank, service_id and team_id have tied, which makes the result deterministic.
5. **No §2 line-budget rows for the SQL files**: correct. The module-map row lists the files without budgets, and all three files are well under 400 lines.

### Issues
#### Critical (Must Fix)
- None.
#### Important (Should Fix)
- None.
#### Minor (Nice to Have)
1. **220_service_map.sql:14-19.** An override with no `team_id` and no `jira_project` still becomes a `core.service_map` row with `team_id` NULL and default role `owner`. An aliases-only override or an org-only override are both examples. This matches the spec wording, but downstream readers of owner rows must tolerate a NULL team. No test pins this behaviour; a one-row assertion in IT02-11 would document it.
2. **220_service_map.sql:31-35, 44.** A `jira_component` suggestion with no `jira_project` passes `refdata._approved_row`, which allows a NULL project. It then becomes a team-keyed `delivery` row with no Jira project. The spec does not cover this case. Consider dropping such rows in 220 or in refdata.
3. **220_service_map.sql:37-40.** Approved suggestions and overrides are not filtered to services present in `core.service`, so orphan `service_id`s can reach `core.service_map`. This is not required by the spec; note it for the facts and quality checks.
4. **200_org_team_service.sql:17, 23, 55-58.** Cost centers are trimmed before matching (`nullif(trim(...), '')`). This is a reasonable reading of "non-empty" but goes slightly beyond the literal rule. IT02-09 does not exercise the whitespace case.
5. **test_model_core_service_map.py:132.** A single test function covers precedence, pending/rejected handling and the lookup. This is fine for an integration scenario, but a failure will point less precisely at the cause.

### Assessment
**Task quality:** Approved
**Reasoning:** All three units follow U02-115, U02-116 and U02-117 exactly: mode switch, OI-04 cost-center rule, owner order, ranks, identity keys and tie-breaks, and lookup exclusions. LLM04 is shown end to end through a real ops store, and the card's tests pass on re-run. The remaining items are edge cases the spec leaves open.
