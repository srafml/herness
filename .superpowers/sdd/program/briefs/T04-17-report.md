# T04-17 Peer groups: build report

Worktree: D:\herness\.claude\worktrees\agent-a73d386da299b6d89 (branch worktree-agent-a73d386da299b6d89, base 7dff609)
Commits: b154d5a wip(T04-17) checkpoint; a6334f5 feat(metrics): add peer groups (T04-17) (final; makes the entity-type alias private, all pre-commit hooks passed).

## Implemented
- herness/metrics/peers.py (171/180): U04-61 `PeerGroupInfo`, a frozen, extra-forbid, strict pydantic model. Its validator enforces size == len(member_ids).
- peers.py also has U04-62 `peer_group(entity_type, entity_id, /, *, metric=None, con=None, on_evidence=None)`:
  - It validates the entity type, a printable id of 1-256 characters, and an enabled catalog metric, which must have grains including {team, org} for team or org.
  - Connection, build_id and as_of are handled as in U04-52: private `_connection` and `_read_build` mirror the compute.py helpers, because importing them would create a cycle (compute re-exports peers).
  - Binds are `default_binds` plus the t12w window binds plus pg_entity_id and pg_metric.
  - Rendering is `render_named("peer_group", {entity_type, has_metric}, ...)`, followed by one `run_recorded(..., None, timeout_s=compute_timeout_s)`.
  - Columns are copied by name. entity_found false raises ToolInputError("unknown <type> <id>").
  - It then calls `on_evidence` and logs DEBUG `metrics.peer_group.resolved` (entity_type, key, size, fallback, query_id).
- herness/metrics/sql/peer_group.sql.j2 (126/130): U04-63 as one SELECT with team, org, service and work_item branches.
  - Team keys use the `team_bucket` macro. Org depth is max(org_closure.depth), as in org_score.
  - service and work_item follow §5.8.1: cluster_fix, then service_id, then the owner service_map row, then `work_item:unresolved`.
  - Output: entity_found, key, fallback, member_id, ordered by member_id.
- herness/metrics/compute.py (339/340): the line-30 marker is replaced by `from herness.metrics.peers import PeerGroupInfo, peer_group`, and both names are added to `__all__`.
- tests/unit/metrics/test_metrics_peers.py: 31 tests covering UT04-71…UT04-74 and TH04-01.

## Tests
- RED: `pytest tests/unit/metrics/test_metrics_peers.py` gave `ImportError: cannot import name 'peers' from 'herness.metrics'`, which is a collection error.
- GREEN: test_metrics_peers.py has 31 passed. `PYTHONUTF8=1 uv run pytest tests/unit/metrics -q -p no:logging` has 694 passed.
- Coverage of herness/metrics/peers.py is 100% line and 100% branch (100 statements, 22 branches).
- UT04-71 has an equality test against score.org. It runs run_org_step on the same warehouse with min_peer_group 1, 2, 3 and 5, and asserts that peer_group(...).key equals score.org.peer_group for all 9 team and org rows.
- TH04-01: injection-shaped ids ("S1' OR '1'='1", "x'); DROP TABLE ...; --", "$1", "{{ p('tz') }}") are stored as service ids.
  - They are resolved correctly, are absent from the recorded SQL, and appear as bind pg_entity_id. incident_fact is intact.
  - Injection-shaped metrics are rejected by the catalog allowlist. A valid metric is the pg_metric bind and never appears in the SQL text.
- Gates: ruff format, ruff check, mypy (343 files), lint-imports (13 kept), check_module_size (exit 0) and check_type_ownership (exit 0) are all clean. The pre-commit hooks passed on the checkpoint commit.

## Longest rendered SQL
- work_item: 2,033 characters.
- team with metric: 1,981. org with metric: 1,426. service: 714.
- All are far below the 20,000-character cap.

## Spec notes and deviations
1. Team and org fallback count: the SQL counts a value only when it is non-NULL and not flagged insufficient_sample, the same `x` that org_score.sql.j2 counts.
   - The U04-63 text says "count non-NULL metric_value.value". The metric wrapper already NULLs values flagged insufficient_sample, so both readings agree on real data.
   - Mirroring org_score guarantees the U04-62 postcondition (same key as score.org).
2. Inactive team: it is found, and its own key is computed. Candidates are active teams only, as the spec says. score.org has no row for an inactive team.
3. cluster_fix: incident_fact rows with a NULL service_id are ignored when picking the most frequent service.
   - A `cluster_fix:` id always counts as found. With no incidents it gives `work_item:unresolved`.
4. A resolved owning service that is missing from core.service (no criticality) is treated as unresolved: `work_item:unresolved`, size 0, prior_year.
5. A metric is validated (known and enabled) for every entity type, but the grains requirement {team, org} is applied only to team and org. For service and work_item, the metric does not affect the query.
6. `entity_type` and `entity_id` are positional-only (`/`), per "entity_id: str (positional)".
7. The Literal alias `_PeerEntityType` is private, so the public names are only PeerGroupInfo and peer_group, as in the §2 row.
8. An org with no org_closure rows would give a NULL key, and PeerGroupInfo would fail validation. score.org would also produce a NULL group then. This is a malformed build and is left as is.

## Concerns
None blocking.

## Fix round 1 (review T04-17-review.md: tests only, production code unchanged)
- I1: new test_ut04_73_item_via_map_highest_confidence. Owner map rows have confidence 0.5 (component match) and 0.9 (NULL component); the 0.9 row wins. Kills M07 (confidence DESC -> ASC).
- I2: test_ut04_73_item_with_service adds a matching PAY owner map row that points to S2. W1 keeps its own service_id S1: one key and no duplicate members. Kills M12 (dropping `wi.service_id IS NULL`).
- m1: test_ut04_71_team_buckets_and_all_fallback adds a min_peer_group=3 case in which the insufficient_sample-flagged value decides the key: team:all. Kills M09 (ignoring the flag).
- m2: new test_ut04_73_cluster_fix_ignores_excluded_and_as_of.
  - Cluster C3 has two S2 incidents opened exactly at as_of_ts (2026-04-01 04:00 UTC); cluster C4 has two excluded S2 incidents.
  - Each cluster also has one in-window S1 incident, which still owns: service:crit_1.
  - Kills M10 (dropping `NOT f.excluded`) and M17 (dropping the `< as_of_ts` bound).
- m3: the "T1\n" case asserts match="printable". Kills M23 (removing the printable check).
- Mutants were run one at a time with the file restored byte for byte; all 6 were killed (script C:\Users\santh\AppData\Local\Temp\w26-s04\T04-17-mutants.py).
- Tests: test_metrics_peers.py has 33 passed; tests/unit/metrics has 696 passed. mypy, ruff format and ruff check are clean.
- `_clone_incident` gained a keyword-only `excluded` flag. ruff removed the PLR0913 noqa as unused.
