# Funding review for the platform portfolio

| Field | Value |
| --- | --- |
| Run | run\_01JA0000000000000000000001 |
| Kind | funding\_review |
| Build | 20260926-100000-000001 |
| Data as of | 2026-09-25T09:30:00\+00:00 |
| Depth | standard |
| Profile | local |
| Rendered at | 2026-09-26T12:00:00\+00:00 |
| Tasks | 3 of 3 done, 0 dead |
| Must-cover | 2 of 2 covered |
| Findings | 1 verified, 0 rejected |
| Publishable | yes |
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

## Executive summary

Incidents for team Payments reached [42](#ev-q_13310206480f64e6) this quarter; the cache rewrite costs [\$1.2K](#ev-q_819b680380edd148).

Failed changes stayed at [7](#ev-q_8cc4f05711c1f705).

## Recommendations

Two candidates lead the ranking; both are worth funding.

### 1. Fund the cache rewrite

Target: epic epic-1

It removes [42](#ev-q_13310206480f64e6) incidents for an effort of [\$1.2K](#ev-q_819b680380edd148).

| Measure | Value |
| --- | --- |
| priority | [0.75](#ev-q_00000000000000f1) |
| wsjf | [2.50](#ev-q_00000000000000f1) |
| confidence | [high](#ev-q_00000000000000f1) |
| unconfirmed | [yes](#ev-q_00000000000000f1) |

Findings: fnd\_01JA0000000000000000000011

### 2. Fund the queue retry work

Target: epic epic-2

Failed changes would drop below [7](#ev-q_8cc4f05711c1f705).

| Measure | Value |
| --- | --- |
| priority | [0.50](#ev-q_00000000000000f1) |
| wsjf | [1.00](#ev-q_00000000000000f1) |
| confidence | — |
| unconfirmed | [no](#ev-q_00000000000000f1) |

Findings: fnd\_01JA0000000000000000000011

### Top candidates by priority

| rank | candidate\_id | title | priority | wsjf | confidence | effort\_cost\_usd |
| --- | --- | --- | --- | --- | --- | --- |
| 1 _(unconfirmed)_ | epic-1 | Cache rewrite | [0.75](#ev-q_00000000000000f1) | [2.50](#ev-q_00000000000000f1) | [high](#ev-q_00000000000000f1) | [\$1.2K](#ev-q_00000000000000f1) |
| 2 | epic-2 | Queue retry | [0.50](#ev-q_00000000000000f1) | [1.00](#ev-q_00000000000000f1) | — | [\$500](#ev-q_00000000000000f1) |

## Portfolio

The base scenario selects the cache rewrite first.

### Scenario: base

- Budget: \$2.0K
- Solver status: OPTIMAL

| order\_rank | candidate\_id | expected\_impact\_usd |
| --- | --- | --- |
| 1 | epic-1 | [\$3.0K](#ev-q_00000000000000f1) |

## Org scorecards

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

| entity\_type | entity\_id | metric | target\_kind | delta\_usd | rationale | unconfirmed | cards |
| --- | --- | --- | --- | --- | --- | --- | --- |
| team | t1 | mttr | peer\_median | [\$1.5K](#ev-q_00000000000000f2) | Payments cuts MTTR from 4.5 to 3 | yes | rec-1 |

## Did last quarter's recommendations work?

The prior cache work paid off: incidents fell to [42](#ev-q_13310206480f64e6).

- paid\_off: 1

### rec\_01JA0000000000000000000024: epic-9

Saved [42](#ev-q_13310206480f64e6) incidents

Decision: accepted

| measurement | metric | baseline | actual | delta | verdict |
| --- | --- | --- | --- | --- | --- |
| 1 | mttr | [4](#ev-q_8cc4f05711c1f705) | [3](#ev-q_8cc4f05711c1f705) | [-1](#ev-q_8cc4f05711c1f705) | paid\_off |

## Data quality and caveats

Service mapping is incomplete for some incidents.

Effort estimates are placeholders until finance confirms them.

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

## Method

Analysts queried the warehouse; the verifier checked every number.

- Depth: standard
- Profile: local
- Number verification (gate 2): passed, 0 numbers checked, 0 failed

| role | model calls |
| --- | --- |
| analyst | 3 |
| writer | 1 |

## Evidence appendix

<a id="ev-q_13310206480f64e6"></a>
### q\_13310206480f64e6

```sql
SELECT count(*) AS n FROM core.incident WHERE team_id = $team
```

- Build: 20260926-100000-000001
- Row count: 1
- Executed at: 2026-09-26T09:30:00.000000Z

| parameter | value |
| --- | --- |
| team | t1 |

| team | n |
| --- | --- |
| t1 | 42 |

Used by: executive\_summary, rec-1, retrospective, retrospective: rec\_01JA0000000000000000000024

<a id="ev-q_819b680380edd148"></a>
### q\_819b680380edd148

```sql
SELECT effort_cost_usd FROM score.funding WHERE candidate_id = $c
```

- Build: 20260926-100000-000001
- Row count: 1
- Executed at: 2026-09-26T09:30:00.000000Z

| parameter | value |
| --- | --- |
| c | epic-1 |

| candidate\_id | effort\_cost\_usd |
| --- | --- |
| epic-1 | 1200.50 |

Used by: executive\_summary, rec-1

<a id="ev-q_8cc4f05711c1f705"></a>
### q\_8cc4f05711c1f705

```sql
SELECT count(*) AS n FROM core.change WHERE outcome = $o
```

- Build: 20260926-100000-000001
- Row count: 1
- Executed at: 2026-09-26T09:30:00.000000Z

| parameter | value |
| --- | --- |
| o | failed |

| n |
| --- |
| 7 |

Used by: executive\_summary, rec-1, rec-2, retrospective, retrospective: rec\_01JA0000000000000000000024

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

Used by: rec-1, rec-2, funding\_table, portfolio: base

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

Used by: scorecard: team=t1, scorecard: team=t2, actions

## Run appendix

### Ranked entities

| rank | entity\_type | entity\_id |
| --- | --- | --- |
| 1 | candidate | epic-1 |
| 2 | candidate | epic-2 |

### Tasks by role and status

| role | status | count |
| --- | --- | --- |
| analyst | done | 2 |
| writer | done | 1 |

- Tokens: 1200 input, 340 output
- Cost: \$0.42
- Config hash: cfg\_00000000000000aa
- Template version: 1
