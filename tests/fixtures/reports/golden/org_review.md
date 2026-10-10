> **Not for decision**

# Org review of incident response

| Field | Value |
| --- | --- |
| Run | run\_01JA0000000000000000000002 |
| Kind | org\_review |
| Build | 20260926-100000-000001 |
| Data as of | 2026-09-25T09:30:00\+00:00 |
| Depth | standard |
| Profile | local |
| Rendered at | 2026-09-26T12:00:00\+00:00 |
| Tasks | 3 of 4 done, 1 dead |
| Must-cover | 1 of 2 covered |
| Findings | 1 verified, 1 rejected |
| Publishable | no |
| Number verification (gate 2) | passed, 0 numbers, 0 failed |

> **Note:** Dollar weights are placeholders and not yet confirmed.
> - change
> - cluster\_fix
> - cost\_per\_downtime\_hour
> - cost\_per\_engineer\_hour
> - expected\_reduction
> - hours\_per\_story\_point
> - impact\_fallback
> - priority\_impact\_multiplier
> - sla\_penalty\_usd
> - strategic\_weights
> - team\_capacity\_points\_per\_quarter
> - toil

> **Note:** Some data quality checks failed. See Data quality and caveats.

> **Note:** This run is partial: some tasks did not finish.

> **Note:** An off-network step fell back to a local model.

## Executive summary

Team mean time to repair is [4.5 h](#ev-q_5a0d200c82be173f) hours against a peer median of [3.0 h](#ev-q_5a0d200c82be173f).

## Org: top-N orgs/teams to improve

### 1. Coach the on-call rotation

Target: team t2

Repair time is [4.5 h](#ev-q_5a0d200c82be173f) hours.

Findings: fnd\_01JA0000000000000000000012

### Composite rank

| entity\_id | composite | rank |
| --- | --- | --- |
| t1 | [0.75](#ev-q_00000000000000f2) | [1](#ev-q_00000000000000f2) |
| t2 | [0.25](#ev-q_00000000000000f2) | [2](#ev-q_00000000000000f2) |

## Portfolio

### Scenario: base

- Budget: \$2.0K
- Solver status: OPTIMAL

| order\_rank | candidate\_id | expected\_impact\_usd |
| --- | --- | --- |
| 1 | epic-1 | [\$3.0K](#ev-q_00000000000000f1) |

## Org scorecards

Payments trails its peers on repair time.

### team: t1

| metric | value | peer\_group | peer\_median | z\_score | sample\_size | composite | rank | flags |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| composite | — | team:all | — | — | [5](#ev-q_00000000000000f2) | [0.75](#ev-q_00000000000000f2) | [1](#ev-q_00000000000000f2) |  |
| mttr | [4.5](#ev-q_00000000000000f2) | team:all | [3](#ev-q_00000000000000f2) | [1.25](#ev-q_00000000000000f2) | [12](#ev-q_00000000000000f2) | [0.75](#ev-q_00000000000000f2) | [1](#ev-q_00000000000000f2) | small |

### team: t2

| metric | value | peer\_group | peer\_median | z\_score | sample\_size | composite | rank | flags |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mttr | [2](#ev-q_00000000000000f2) | team:all | [3](#ev-q_00000000000000f2) | [-0.50](#ev-q_00000000000000f2) | [9](#ev-q_00000000000000f2) | [0.25](#ev-q_00000000000000f2) | [2](#ev-q_00000000000000f2) |  |

## Recommended actions

Pair on-call engineers with the platform team.

| entity\_type | entity\_id | metric | target\_kind | delta\_usd | rationale | unconfirmed | cards |
| --- | --- | --- | --- | --- | --- | --- | --- |
| team | t1 | mttr | peer\_median | [\$1.5K](#ev-q_00000000000000f2) | Payments cuts MTTR from 4.5 to 3 | yes |  |

## Did last quarter's recommendations work?

_No measured outcomes yet._

## Data quality and caveats

One analyst task did not finish.

### Failed data quality checks

| check\_name | severity | value | threshold |
| --- | --- | --- | --- |
| incidents\_service\_null | warn | 0.125 | 0.1 |

Unmapped incident share: 0.125

### Unconfirmed weights

- change
- cluster\_fix
- cost\_per\_downtime\_hour
- cost\_per\_engineer\_hour
- expected\_reduction
- hours\_per\_story\_point
- impact\_fallback
- priority\_impact\_multiplier
- sla\_penalty\_usd
- strategic\_weights
- team\_capacity\_points\_per\_quarter
- toil

### Dead tasks

| role | objective | first error line |
| --- | --- | --- |
| analyst | size the backlog | \[PERSON\_2385a2efec\] timed out |

### Contested findings

- fnd\_01JA0000000000000000000012

### Removed items

| where | reason |
| --- | --- |
| sections\[3\] | uncited numbers |

### Flags

- notes: Scores use the peer median.

## Method

- Depth: standard
- Profile: local
- Number verification (gate 2): passed, 0 numbers checked, 0 failed

| role | model calls |
| --- | --- |
| analyst | 3 |
| writer | 1 |

## Evidence appendix

<a id="ev-q_5a0d200c82be173f"></a>
### q\_5a0d200c82be173f

```sql
SELECT entity_id, value FROM score.org WHERE metric = $m AND entity_id = $e
```

- Build: 20260926-100000-000001
- Row count: 1
- Executed at: 2026-09-26T09:30:00.000000Z

| parameter | value |
| --- | --- |
| m | mttr |
| e | t2 |

| entity\_id | value |
| --- | --- |
| &lt;b onmouseover=x&gt; | 4.5 |

Used by: executive\_summary, rec-1

<a id="ev-q_fa5f7083d72030b7"></a>
### q\_fa5f7083d72030b7

```sql
SELECT count(*) AS n FROM core.incident WHERE priority <= $p
```

- Build: 20260926-100000-000001
- Row count: 0
- Executed at: 2026-09-26T09:30:00.000000Z

| parameter | value |
| --- | --- |
| p | 2 |

_The stored sample has no rows._

Used by: executive\_summary, rec-1

<a id="ev-q_00000000000000f2"></a>
### q\_00000000000000f2

```sql
SELECT * FROM score.org ORDER BY rank
```

- Build: 20260926-100000-000001
- Row count: 1
- Executed at: 2026-09-26T09:00:00.000000Z

| entity\_id |
| --- |
| t1 |

Used by: org\_table, scorecard: team=t1, scorecard: team=t2, actions

<a id="ev-q_00000000000000f1"></a>
### q\_00000000000000f1

```sql
SELECT * FROM score.funding ORDER BY rank
```

- Build: 20260926-100000-000001
- Row count: 1
- Executed at: 2026-09-26T09:00:00.000000Z

| candidate\_id | rank |
| --- | --- |
| epic-1 | 1 |

Used by: portfolio: base

## Run appendix

### Ranked entities

| rank | entity\_type | entity\_id |
| --- | --- | --- |
| 1 | team | t1 |
| 2 | team | t2 |

### Tasks by role and status

| role | status | count |
| --- | --- | --- |
| analyst | done | 1 |

- Tokens: 1200 input, 340 output
- Cost: \$0.40
- Config hash: cfg\_00000000000000aa
- Template version: 1
