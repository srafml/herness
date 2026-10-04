# T04-17 Peer groups: verify review

Worktree `agent-a73d386da299b6d89`, base 7dff609, head a6334f5. Files reviewed: `herness/metrics/peers.py`, `herness/metrics/sql/peer_group.sql.j2`, `herness/metrics/compute.py` (re-export), `tests/unit/metrics/test_metrics_peers.py`.

**Verdict: Needs fixes.** The implementation matches the spec. Two explicitly specified work_item resolution rules have no test: the highest-confidence owner map row, and `service_id` taking precedence over the map. Both mutants survive. The fixes are test-only.

### Spec Compliance
- ✅ U04-61 `PeerGroupInfo`: frozen, forbid, strict. The `size == len(member_ids)` validator is at peers.py:49-54, and `size` is `ge=0`.
- ✅ U04-62 `peer_group`:
  - Validation (peers.py:57-78) covers the allowlisted type, a printable id of 1-256 characters, and an enabled metric that needs grains ⊇ {team, org} only for team and org.
  - The connection and build/as_of handling follow U04-52.
  - There is one `render_named("peer_group", {entity_type, has_metric}, ...)`, with binds = `default_binds` + t12w window + `pg_entity_id`/`pg_metric`.
  - There is one `run_recorded(..., None, timeout_s=compute_timeout_s)`.
  - Columns are copied by name. `entity_found` false raises `ToolInputError("unknown <type> <id>")`.
  - `on_evidence` is called after a successful result. The DEBUG log `metrics.peer_group.resolved` carries entity_type/key/size/fallback/query_id only (no entity_id).
  - Python does no arithmetic on results beyond `len(members)`, which is the model invariant.
- ✅ U04-63 `peer_group.sql.j2`: one SELECT, output `entity_found, key, fallback, member_id` ordered by member_id.
  - team: the `team_bucket` macro is reused (sql:12,17), the same as org_score.sql.j2:25.
  - org: `max(org_closure.depth)`, identical to org_score.sql.j2:31.
  - The fallback count uses the same `x` as org_score (non-NULL, not insufficient_sample).
  - service: `coalesce(...,'none')`, so NULL matches NULL.
  - work_item: cluster_fix (window [as_of_ts - s_window_days, as_of_ts), ties lowest service_id), then `service_id`, then the owner map row (highest confidence, ties lowest service_id), then `work_item:unresolved`/`prior_year`.
  - The entity and the owning service are excluded (sql:61, sql:109).
  - Thresholds come only from `p('s_min_peer_group')`, `p('s_min_service_peers')`, `p('s_window_days')` and `p('as_of_ts')`/`p('window_start_date')`. The only rendered literals are allowlisted `entity_type`, `'t12w'` and the fixed prefix length 13.
- ✅ compute.py re-exports `PeerGroupInfo` and `peer_group` (compute.py:25) and lists both in `__all__`. There is no cycle: lint-imports passes with 13 kept, and peers.py does not import compute.
- ✅ U04-62 postcondition: `test_ut04_71_same_key_as_score_org` runs `run_org_step` with min_peer_group 1/2/3/5 and checks all 9 rows.
- ✅ TH04-01: id and metric are typed binds (`CAST($pg_entity_id AS VARCHAR)` is asserted). Injection-shaped ids never appear in the SQL text. Candidates are scoped per entity_type (core.team active / core.org / core.service), and the tables stay intact.
- ✅ UT04-71: 11 functions, meaningful.
- ✅ UT04-72: 2 functions, meaningful.
- ⚠️ UT04-73: 4 functions, but the coverage is partial (see Important).
- ✅ UT04-74: 1 function, meaningful.
- ✅ Every test name contains its ID with underscores, every docstring's first line starts with the ID, and `pytestmark = pytest.mark.unit` is set.
- ⚠️ Cannot fully verify:
  - The p95 < 2 s target (BT04-07) and IT04-04 are outside this card's scope.
  - Callers in spec 07 might pass `entity_id=` as a keyword; see deviation 6.

### Build-report deviations (judged)
1. Fallback count excludes insufficient_sample: **Accepted.** This is required for the score.org postcondition, and the wrapper NULLs such values anyway. Its test does not discriminate it, though (M09).
2. An inactive team is found and gets a key; candidates are active only: **Accepted.**
3. cluster_fix ignores NULL service_id, and `cluster_fix:` is always found: **Accepted.**
4. An owning service missing from core.service is treated as unresolved: **Accepted.** Edge: if the top-confidence map row points to a missing service, the item becomes unresolved rather than falling back to the next row. This is defensible and recorded here as a note.
5. A metric is validated for all types, but grains only for team/org: **Accepted.**
6. `entity_type`/`entity_id` are positional-only (`/`): **Accepted with note.** Design §3.1:75 shows them as positional-or-keyword. If spec 07 calls `entity_id=` it will break; see Minor 4.
7. Private Literal alias: **Accepted.**
8. An org without closure rows gives a NULL key, so validation fails. This mirrors score.org on a malformed build: **Accepted.**

### Gates (re-run by verifier)
- Line counts:
  - peers.py: 171/180
  - peer_group.sql.j2: 126/130
  - compute.py: 338/340
- `tools.check_module_size`: exit 0.
- ruff check, ruff format --check, mypy (343 files) and lint-imports (13 kept) are all clean.
- Coverage of peers.py: 100% line, 100% branch (100 statements, 22 branches).
- `PYTHONUTF8=1 uv run pytest tests/unit/metrics -q -p no:logging`: **694 passed**, with no warnings or noise.

### Mutation probes (`tests/unit/metrics/test_metrics_peers.py -x`; all restored, git clean)

| # | Mutation | Result |
|---|----------|--------|
| M01 | team/org fallback `<` to `<=` s_min_peer_group | CAUGHT |
| M02 | drop team/org self-exclusion (sql:61) | CAUGHT |
| M03 | drop service/owning-service exclusion (sql:109) | CAUGHT |
| M04 | map tie-break service_id to DESC | CAUGHT |
| M05 | cluster tie-break service_id to DESC | CAUGHT |
| M06 | service fallback `<` to `<=` s_min_service_peers | CAUGHT |
| M07 | map confidence DESC to ASC (sql:100) | **SURVIVED** |
| M08 | drop the cluster window lower bound | CAUGHT |
| M09 | count ignores the insufficient_sample flag (sql:38) | **SURVIVED** |
| M10 | cluster keeps excluded incidents (sql:79) | **SURVIVED** |
| M11 | NULL criticality not coalesced | CAUGHT |
| M12 | map applied even when service_id is set (sql:99) | **SURVIVED** |
| M13 | inactive teams as candidates | CAUGHT |
| M14 | org depth max to min | CAUGHT |
| M15 | drop the work_item:unresolved row | CAUGHT |
| M16 | map drops role='owner' | CAUGHT |
| M17 | cluster upper bound `<` to `<=` as_of_ts | **SURVIVED** |
| M18 | map drops the component filter | CAUGHT |
| M19 | entity not added to the count group (sql:35) | SURVIVED (equivalent for active entities; affects only inactive teams, which score.org does not score) |
| M20 | peers.py skips on_evidence | CAUGHT |
| M21 | peers.py ignores entity_found | CAUGHT |
| M22 | peers.py grain check off | CAUGHT |
| M23 | peers.py printable check off | **SURVIVED** |

### Strengths
- One recorded query covers all four entity types. It reuses the macro and the org depth expression verbatim from org_score, so the team/org key is equal by construction.
- The score.org equality test across four thresholds is strong.
- The TH04-01 tests check both the SQL text and the bind map.
- 100% coverage, and 17 of 23 mutants are killed.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **The highest-confidence map rule is untested (M07 survives).** In tests/unit/metrics/test_metrics_peers.py:288-293 the only matching owner rows (S6, S5) both have confidence 0.9, so the test exercises only the tie-break. Reversing `ORDER BY m.confidence DESC` at herness/metrics/sql/peer_group.sql.j2:100 passes all tests.
   - Fix: add a matching owner row with higher confidence and a higher service_id (or a lower-confidence row with a lower service_id), and assert which one wins.
2. **The "service_id before map" precedence is untested (M12 survives).** Dropping `wi.service_id IS NULL` at herness/metrics/sql/peer_group.sql.j2:99 passes. The item in `test_ut04_73_item_with_service` (test file:267-271, W1) has no matching owner map row. If this mutant shipped, `own` could hold two services, giving two `res` rows, duplicated members and a nondeterministic key.
   - Fix: give an item that has a `service_id` a matching owner map row that points to a service of a different criticality, and assert that the `service_id` group wins.

#### Minor (Nice to Have)
1. tests/unit/metrics/test_metrics_peers.py:159: the comment "does not count, as in score.org" is not proven. With min_peer_group 5 the count is below the threshold, and with 2 it is above, whether or not T4's flagged value counts (M09 survives). Use min_peer_group=3, so that counting T4 flips the key.
2. Two cluster_fix rules have no test, though both are plain spec rules. `NOT f.excluded` (peer_group.sql.j2:79) has none (M10), and the window upper bound `< as_of_ts` (peer_group.sql.j2:82) has none (M17). Add an excluded clone that would change the winner, and an incident exactly at as_of_ts.
3. tests/unit/metrics/test_metrics_peers.py:223: the "T1\n" case passes only through "unknown team", so the printable check (peers.py:67-69) is not isolated (M23). Use `match="printable"`.
4. herness/metrics/peers.py:130: positional-only `/` is stricter than design §3.1:75, which shows a plain signature. Confirm that spec 07 callers pass both positionally.
5. herness/metrics/peers.py:81-104 duplicates `_connection`/`_read_build` from compute.py:154-185. It is justified by the re-export cycle, but a shared private helper (for example, in `_request.py`) would remove about 25 duplicated lines.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The production code is correct and matches U04-61...U04-63, TH04-01 and the score.org postcondition, and every gate is green. However, two explicitly specified work_item resolution rules (highest-confidence map row; service_id precedence) are not protected by any test, and both mutants survive. The fix is test-only.

## Re-review round 1 (de4f9ca, test-only; a6334f5..de4f9ca)

**Verdict: Approved.**

The commit changes only `tests/unit/metrics/test_metrics_peers.py`: +42/-4 lines. There are no production changes.

| Finding | Fix | Mutant re-run | Status |
|---------|-----|---------------|--------|
| I1 highest-confidence map row | New `test_ut04_73_item_via_map_highest_confidence`. It uses S5 api at 0.5 and S6 NULL at 0.9, and the higher-confidence, higher-id row wins. | M07 CAUGHT | ✅ Fixed |
| I2 service_id beats the map | `test_ut04_73_item_with_service` adds a matching owner map row S2 (PAY, 1.0), and the item still resolves to service:crit_1. | M12 CAUGHT | ✅ Fixed |
| m1 insufficient_sample not counted | With min_peer_group=3, the 2 counted values give `team:all`. | M09 CAUGHT | ✅ Fixed |
| m2 cluster_fix excluded / as_of upper bound | New `test_ut04_73_cluster_fix_ignores_excluded_and_as_of`. It adds excluded clones and clones placed exactly at as_of_ts (local midnight, America/New_York). | M10 CAUGHT, M17 CAUGHT | ✅ Fixed |
| m3 printable check isolated | `"T1\n"` now uses `match="printable"`. | M23 CAUGHT | ✅ Fixed |
| m4 positional-only signature | Parked as a spec note by the builder. | n/a | Open (Minor, accepted as parked) |
| m5 duplicated `_connection`/`_read_build` | Parked by the builder. | n/a | Open (Minor, accepted as parked) |

**Evidence**
- `pytest tests/unit/metrics/test_metrics_peers.py`: 33 passed.
- `PYTHONUTF8=1 uv run pytest tests/unit/metrics -q -p no:logging`: 696 passed.
- ruff check and ruff format --check on the test file are clean.
- Project `mypy` is clean (343 files).
- Mutations were restored exactly. `git status` and `git diff` were empty at the end.

**Info, not a finding.** Running mypy directly on the test file reports 3 errors: unused-ignore at lines 214 and 523, and var-annotated at line 372. All three were present before this round, and the project mypy gate does not cover tests/.

The new tests carry UT04-71/UT04-73 IDs in their names and docstrings.
