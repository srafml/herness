# T05-14 SQL guard: review (commit 7e135e5, base a633b17)

### Spec Compliance
- ❌ Issues found: U05-37 rule 6 and lineage (TH05-05) can be bypassed. See C1–C3 below. Rules 1–5 and 7, the second parser, the hint texts, the denied lists and the constants all match the spec exactly.

| ID | Status | Note |
|---|---|---|
| U05-37 steps 1–5, 7, 8, 11, 12 | ✅ | Order is correct and stops at the first failure. Hints are verbatim; denied node, function and schema lists are verbatim. The SQL returned and executed is the original text. |
| U05-37 step 9 (rule 6) | ❌ | Positional columns (`#n`) and stars that are not expanded both get past the blocked-column check (C1, C2). |
| U05-37 step 10 (lineage) | ❌ | Lineage through a table-function column alias gives an empty set instead of failing closed (C3). |
| UT05-51..UT05-58, UT05-60 | ✅ | UT05-54's unresolved list is empty on sqlglot 30.19.0, which is correct. |
| UT05-59 | ✅ | The substitute differential cases (SELECT INTO, FOR UPDATE, LIMIT a,b) plus direct second-parser cases are acceptable. |
| ST05-03, ST05-04 | ✅ | |
| ST05-05 | ❌ | The tests pass, but the threat is not mitigated: `*` next to an allowed table function, `* LIKE` and `#n` all leak (C1, C2). |
| IT05-10, BT05-03 | ✅ | Run on the stand-in corpus (carry-over accepted). |
| VI-10 | ✅ | DuckDB 1.5.5 emits only SELECT_NODE and SET_OPERATION_NODE as roots. The four-name default is kept. |
| Test ID naming | ✅ | |
| Gates | ✅ | Re-run here: 320 passed. Coverage of sql_guard.py: line 265/268, branch 82/84 (99%). ruff check and ruff format are clean, mypy is clean, check_module_size exits 0 (390/390). |

- ⚠️ Cannot verify from diff:
  - the metric counter (carry-over);
  - the nightly profile in CI (carry-over);
  - the real `sql_ok` corpus and the `full` schema (carry-over).

### Strengths
- Close to the spec letter: verbatim hints and constants, and rules evaluated strictly in order.
- Several good hardening steps:
  - it checks a function under every name it can carry;
  - `FROM t(a,b,c)` alias lists fail closed;
  - it catches RecursionError;
  - it filters the sqlglot warning that echoes SQL.
- Checked by probe: method-call and `::` spellings of `getenv`/`current_setting`, schema-qualified `main.getenv`, quoted and mixed-case names, `FROM 'x.csv'` and `FROM "x.csv"`, `sniff_csv`, `parquet_metadata` and `which_secret` in table position, `(sniff_csv())`, and JOIN of a table function are all rejected.
- Also rejected by probe: CTE and recursive-CTE column lists, `s(x,y)` subquery aliases, QUALIFY, window functions, UNION, NATURAL, USING, POSITIONAL and ASOF joins, UNPIVOT, struct access `i.description.x`, table-as-struct (`SELECT i`, `to_json(i)`, `i::VARCHAR`) and fullwidth identifiers.
- The rejection log carries only `rule` and `query_hash`. The parser connection is per thread, created lazily, with external access off.

### Issues

#### Critical (Must Fix)

**C1. Positional column references bypass rule 6 and lineage.**
- Where: herness/harness/sql_guard.py:330-336. sqlglot parses DuckDB `#n` as `exp.PositionalColumn`, which is not an `exp.Column`, so `_check_columns` and `_reach` never see it. DuckDB resolves `#n` in the select list and in WHERE to the n-th column of the FROM clause.
- Repro, with the stand-in schema and a populated DB. Each query below is accepted, and the result is shown on the right.

| Query | Result |
|---|---|
| `SELECT #10 FROM core.incident` | returns `description` |
| `SELECT #9 FROM core.incident` | returns `short_description` |
| `SELECT upper(#11) FROM core.incident` | returns `close_notes` |
| `SELECT count(*) FROM core.incident WHERE #10 LIKE 'SECRET%'` | an oracle that reads `description` without selecting it |
| `SELECT #8 FROM core.work_item` | returns `summary` with `untrusted_output_columns = redact_output_columns = {}`, so it skips redact-on-read |

- Fix: reject `exp.PositionalColumn` anywhere (a new rule-6 hint, or "column not found"). Add it to the ST05-05 templates.

**C2. A star that qualify leaves unexpanded bypasses rule 6 and lineage.**
- Where: sql_guard.py:262-265. qualify does not expand `*` when a FROM clause contains a table function with no column-alias list, or when the star has a `LIKE`/`SIMILAR TO` filter. No `exp.Column` is produced, so nothing is checked.
- Repro. Each of these is accepted and returns every blocked column:
  - `SELECT * FROM core.incident, range(1)`
  - `SELECT * FROM core.incident CROSS JOIN unnest([1])`
  - `SELECT * FROM core.incident i JOIN generate_series(1,2) g ON true`
  - `SELECT * FROM range(1), core.incident`
  - `SELECT * EXCLUDE (number) FROM core.incident, range(1)`
  - `SELECT * FROM core.incident i, LATERAL unnest([i.number])`
  - `SELECT x.* FROM (SELECT * FROM core.incident i, range(1)) x`
- `SELECT * LIKE '%desc%' FROM core.incident` and `SELECT * SIMILAR TO '.*desc.*' FROM core.incident` return `short_description` and `description`.
- `SELECT * FROM core.event, range(1)` returns `alert_name` with no untrusted flag.
- Fix (fail closed): after qualify, reject any remaining `exp.Star` that is not the sole argument of an aggregate such as `count(*)`. This covers stars in projections, `* LIKE`/`SIMILAR TO`, and `t.*`. Alternatively, give table functions known columns so the star expands, but the reject-residual-star check is still needed as a backstop. Add these shapes to ST05-05.

**C3. Lineage fails open through table-function column aliases (redact-on-read bypass).**
- Where: sql_guard.py:321-328 and 353-372. A column whose source is an `Unnest` or UDTF alias (for example `t(u)`) resolves to neither a `Table` nor a `Scope`. It falls into the "unresolved" branch, which matches only real tables that have a column named `u`, and so returns an empty set. The spec's fail-closed rule covers only exceptions.
- Repro. Each query is accepted and returns the raw summary or title, with `untrusted = redact = {}`:
  - `SELECT u FROM core.work_item, unnest([summary]) t(u)`
  - `SELECT g FROM core.work_item w, LATERAL unnest([w.summary]) AS x(g)`
  - `SELECT x FROM core.work_item, unnest([{'a': summary}]) t(x)`
  - `SELECT u FROM core.event, unnest([alert_name]) t(u)`
- Contrast: `SELECT u.unnest FROM ... unnest([summary]) u` is flagged.
- Fix: a column sourced from a table function or an unknown source should reach every source column referenced in that function's arguments. Or, simpler, when the query references any untrusted column, mark every output column untrusted and redact.
- Blocked columns used only inside those function arguments are caught today only because the argument is itself an `exp.Column`. Keep that invariant tested.

#### Important (Should Fix)

**I1. Catalog mode's relaxed validation opens a hole.**
- Where: sql_guard.py:262 (`validate=not uses_catalog`) and 327-328. With `allow_catalog=True`, the unresolved-column fallback matches by column name only. A table alias used as a row value therefore reaches nothing.
- Repro with `allow_catalog=True`. Each is accepted and returns the whole row, blocked columns included:
  - `SELECT i FROM core.incident i, duckdb_tables()`
  - `SELECT to_json(i) FROM core.incident i, duckdb_tables() d`
  - `SELECT i FROM core.incident i WHERE EXISTS (SELECT 1 FROM duckdb_tables())`
- `SELECT * FROM core.incident, duckdb_tables()` also leaks (C2).
- Deviation 5 in the build report says "the blocked-column check still applies". That is false for these shapes.
- Mitigating factor: the flag is internal-only.
- Fix: in catalog mode, reject any query that also references a non-catalog table, or treat an unresolved name that equals a table alias or table name as reaching every column of that table.

**I2. The rule-4 CTE-name exemption is not scope-aware (plan-mandated wording, but exploitable).**
- Where: sql_guard.py:272-280. CTE names are collected from the whole tree, so an unqualified table named after a CTE defined in a different (inner) scope is allowed.
- Repro: `SELECT * FROM (WITH t AS (SELECT 1 AS a) SELECT * FROM t), t` is accepted. At run time DuckDB resolves the outer `t` through the search path or a Python replacement scan; in the probe it bound to a Python local named `t`.
- On the harness warehouse connection, this reaches any `main`-schema object or a Python object in the calling frame, bypassing rule 4. With C2, the outer `*` is not expanded, so every column of that object is returned.
- The spec text says "a name equal to a CTE name with no db part is allowed", so this is plan-mandated. Still, the check should use scope: the name must be a CTE visible from that table's scope, for example `scope.cte_sources`. Also consider `python_enable_replacements=false` on the warehouse connection (another card).

#### Minor (Nice to Have)
- **M1.** sql_guard.py:93 installs a filter on the global `sqlglot` logger at import time, matched on a message substring. This is an import side effect that could break silently if sqlglot rewords the message. Consider a test that pins the message, or a scoped filter.
- **M2.** sql_guard.py:374-379: per-thread parser connections are never closed. It is harmless for long-lived threads but leaks one connection per short-lived thread.
- **M3.** Over-flagging: `SELECT l FROM core.event, LATERAL (SELECT alert_name AS l)` marks `l` as redact-on-read even though `alert_name` is not a redact column. This is on the safe side, but it causes unnecessary redaction; it comes from the unresolved-name fallback.
- **M4.** The build report's claims ("blocked column check applies in catalog mode"; ST05-05 covers `*`) overstate coverage. Correct them when fixing.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Three trivial, reproducible bypasses leak blocked free-text and unredacted redact-on-read columns to the model: positional `#n` columns, stars that are not expanded next to table functions or with `* LIKE`, and lineage lost through table-function aliases. TH05-05 is therefore not mitigated, even though the rest of the guard is faithful to the spec and well tested.
## Re-review round 1 (fix commit 0a95357 on 7e135e5)

**Task quality:** Needs fixes.

C1, C2, C3 and I1 are fixed, and every original repro plus the variants tried to get around each fix are now rejected. The I2 fix is incomplete: a non-recursive CTE that refers to itself, or to a CTE defined later in the same `WITH`, still reads an object outside the allowed schemas. Repro and fix are below.

### Method
- Each query was run through the guard with the stand-in schema, then executed on a populated in-memory DuckDB that also holds `main.t`, `main.b` and `main.c` tables containing marker strings.
- Scripts: `scratchpad/t0514_probe_r1.py`, with query files `t0514_r1a.sql`, `t0514_r1b.sql` and `t0514_r1c.sql`.

### Disposition of round-0 findings

| ID | Status | Evidence |
|---|---|---|
| C1 positional `#n` | ✅ fixed | All 5 repros are rejected. Also rejected: `ORDER BY #1`, `GROUP BY #1`, `#n` inside a subquery, and `#n` inside a CTE. Rejecting `ORDER BY #n` / `GROUP BY #n` (which DuckDB resolves against the select list) over-rejects, which is acceptable. |
| C2 residual `*` | ✅ fixed (see I2b for the gating gap) | All 11 repros are rejected. Also rejected: a star in a CTE or nested subquery mixed with unnest, `c, range(1)` over a CTE, `r.*` from a table function, `count(i.*)`, `count(* LIKE ...)`, `count(DISTINCT *)`, `list(*)`, `struct_pack(*)`, `max(t.*)`, and `COLUMNS(*)` / `*COLUMNS(*)` (rule 5). Accepted, correctly: `count(*)` over `core.incident, range(3)`. |
| C3 lineage via table-function alias | ✅ fixed | All 4 repros are now flagged untrusted and redact. The same holds when the aliased output is wrapped in a subquery, a CTE, a UNION branch, a scalar subquery, `range(length(summary)) t(r)`, or `unnest([summary]) AS u` in the select list. |
| I1 catalog mode | ✅ fixed | The 3 repros and `SELECT * FROM core.incident, duckdb_tables()` are rejected. Also rejected: a catalog query mixed with a table in an IN-subquery, and with a CTE over `core.incident`. Catalog-only queries (`duckdb_tables()` alone, UNION, a subquery, `to_json(d)`) are accepted, which is fine. |
| I2 CTE scope | ❌ incomplete | The review repro and cross-subquery CTE references are now rejected. A self-referencing or forward-referencing CTE still bypasses rule 4 (I2b below). |
| M1 | ✅ | Pinned by a test. |
| M2, M3 | ✅ | Accepted as left, with the stated rationale. |
| M4 | ✅ | Report corrected. |

### Findings

#### Important (Should Fix)

**I2b. A non-recursive CTE that refers to itself, or to a later sibling CTE, is exempt from rule 4 and reads `main` or Python objects.**
- Where: herness/harness/_sql_guard_scope.py:14-23 and herness/harness/sql_guard.py:262 (rule 4), plus sql_guard.py:312 (the residual-star gate).
- Cause: `visible_ctes` collects every CTE in each enclosing `WITH`, including the CTE whose body contains the table and any CTE defined after it. Without `RECURSIVE`, DuckDB does not bind a CTE inside its own body or inside an earlier sibling. It binds the name through the catalog search path (`main`), or through a Python replacement scan; the harness warehouse connection does not set `python_enable_replacements=false` (grep of `herness/` finds no match).
- A second cause: the residual-star check runs only when `tables` is non-empty. A CTE-only query, whose `tables` set is empty, therefore keeps an unexpanded `*` over the unknown object.
- Repro, plain and `allow_catalog=True` alike. Each is accepted and returns the `main.*` marker:
  - `WITH t AS (SELECT * FROM t) SELECT * FROM t` returns `SECRET_MAIN_T`
  - `WITH a1 AS (SELECT * FROM b), b AS (SELECT 1 AS x) SELECT * FROM a1` returns `SECRET_MAIN_B`
  - `WITH b AS (SELECT * FROM b), c AS (SELECT * FROM c) SELECT * FROM b, c`
  - `WITH t AS (FROM t) SELECT * FROM t`
  - `WITH t AS MATERIALIZED (SELECT * FROM t) SELECT * FROM t`
  - `... SELECT t.* FROM t`
  - `... UNION ALL SELECT 'x'`
  - `... , range(1)`
- `WITH c AS (SELECT * FROM c) SELECT count(*) FROM c` works as a row-count oracle.
- In catalog mode: `WITH t AS (SELECT * FROM t) SELECT * FROM duckdb_tables(), t`.
- Named-column forms (`SELECT a FROM t`) are rejected only because qualify cannot resolve the column.
- Fix:
  1. A table name is exempt only if it names a CTE that is visible in DuckDB's binding order. That means a CTE defined earlier in the same `WITH`, or in an outer `WITH`. The CTE itself is visible only when `WITH RECURSIVE` is set (`with_.args["recursive"]`). Later siblings are never visible.
  2. As a backstop, apply the residual-star rejection whenever any source of the star's scope is not a table function, instead of only when `tables` is non-empty. Catalog-only and `range`/`unnest`-only queries still pass.
  3. Add these shapes to UT05-55 and ST05-05.
  4. Separately, T05-13 should set `python_enable_replacements=false` (already a carry-over).
- Severity: Important rather than Critical, because the reachable objects are outside the warehouse schemas: `main` tables, if any, and DataFrame or Arrow objects in the calling Python frame. It is still a rule-4 bypass that returns data, and the round-1 ruling for I2 (exemption only for visible CTEs) is not met.

#### Minor
- None new.

### Gates (re-run)
- Card tests: 393 passed.

| Module | Line | Branch |
|---|---|---|
| sql_guard.py (381 lines) | 259/261 | 83/84 |
| _sql_guard_scope.py (54 lines) | 31/32 | 11/12 |

- Both modules are above the 90% line / 85% branch gates.
- ruff check and ruff format clean; mypy clean on both modules; check_module_size exit 0.
- Test ID naming: ✅.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** C1–C3 and I1 are closed, with regression tests, and they held against the variants tried. One rule-4 bypass remains, through self- and forward-referencing CTEs combined with the residual-star check being skipped when no warehouse table is referenced, and it returns data from outside the allowed schemas.
## Re-review round 2 (fix commit 22584b1 on 0a95357)

**Task quality:** Needs fixes.

I2b is fixed. The same commit introduced a regression: relaxing the star check lets untrusted text through lineage with no flag. That is a redact-on-read bypass, the same class as C3.

### Method
- Probe `scratchpad/t0514_probe_r1.py` with query file `t0514_r2a.sql`: the stand-in schema plus `main.t`, `main.b` and `main.c` tables holding marker strings.
- Every query was run through the guard in plain mode and in `allow_catalog=True` mode, then executed on DuckDB.

### I2b: ✅ fixed
- All round-1 repros are rejected in both modes with "qualify as core.<name>":
  - self-referencing CTEs (plain, MATERIALIZED, `FROM t`, `t.*`, UNION, `range(1)`);
  - forward references to a later sibling;
  - `count(*)` over a self-referencing CTE;
  - catalog mode with `duckdb_tables(), t`.
- The variants are rejected too:
  - a forward reference from a subquery or a nested `WITH` inside a CTE body (`WITH a1 AS (WITH z AS (SELECT * FROM b) ...), b AS ...`);
  - a forward reference from a scalar or EXISTS subquery inside a CTE body;
  - `WITH RECURSIVE` with a CTE that refers to a later sibling (in both the plain and the UNION form);
  - a nested `WITH` whose inner CTE refers forward to its own sibling.
- These are accepted, correctly:
  - a valid `WITH RECURSIVE r(a) ...`, including `SELECT *` from it (UT05-51);
  - a backward sibling reference;
  - an inner non-recursive `t` that references an outer CTE `t` (DuckDB also binds it to the outer CTE: it returned 1).
- The trade-off `WITH r AS (SELECT * FROM range(3)) SELECT * FROM r` is now rejected. This is accepted and not a defect.
- The stand-in corpus (IT05-10) still passes.

### Findings

#### Critical (Must Fix)

**C4 (regression from round 2). A `*` over a table function fed by a subquery hides untrusted and redact-on-read lineage.**
- Where: herness/harness/_sql_guard_scope.py:65-74 (`has_leaky_star`) and herness/harness/sql_guard.py:319, together with `_lineage`/`_reach`.
- Cause: round 1 rejected any unexpanded `*` when the query referenced a warehouse table. Round 2 allows it whenever every source in the star's own scope is a table function. An unexpanded `*` has no `exp.Column` projections, so `_reach` finds nothing, and the output gets no untrusted or redact flag.
- Repro, plain and catalog mode alike. Each query is accepted, and the raw value comes back with `untrusted_output_columns = redact_output_columns = {}`:
  - `SELECT * FROM unnest((SELECT list(summary) FROM core.work_item))` returns `SECRET_summary`
  - `SELECT * FROM unnest([(SELECT max(summary) FROM core.work_item)])` returns `SECRET_summary`
  - `SELECT * FROM unnest((SELECT list(title) FROM score.funding)) t` returns `SECRET_title`
- Blocked columns are still caught in the same shape: `... list(description) FROM core.incident` is rejected, because the inner `exp.Column` is checked.
- Fix: any one of these works.
  1. Keep the round-1 condition as well, rejecting a residual star when `tables` is non-empty OR `has_leaky_star`.
  2. In `_lineage`, when the root projection holds an unexpanded star and the query references any untrusted column, mark every output untrusted and redact.
  3. Treat a table function whose arguments contain a subquery as not being a pure table function in `_is_table_function`.
- Option 1 is simplest and keeps `SELECT * FROM range(3)`, `SELECT * FROM range(1), unnest([1])` and catalog-only queries accepted.
- Add the three repros to ST05-05, following the C3 test.

#### Important / Minor
- None new.

### Gates (re-run)
- Card tests: 456 passed.

| Module | Line | Branch |
|---|---|---|
| sql_guard.py (381 lines) | 259/261 | 83/84 |
| _sql_guard_scope.py (79 lines) | 49/50 | 23/24 |

- ruff check and ruff format clean; mypy clean on both modules; check_module_size exit 0.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** I2b is closed and held against every variant tried. The star relaxation in the same commit reopens a redact-on-read bypass (C4): `SELECT * FROM unnest((SELECT list(summary) ...))` returns raw summaries with no flag. It is a small fix: restore the round-1 condition as well as the new check.
## Re-review round 3 (fix commit fea8fb0 on 22584b1)

**Task quality:** Approved.

### Method
- Probe `scratchpad/t0514_probe_r1.py` with query file `t0514_r3a.sql`: the stand-in schema plus `main.t`, `main.b` and `main.c` marker tables.
- Every query was run through the guard in plain mode and in `allow_catalog=True` mode, then executed on DuckDB.

### C4: ✅ fixed
The fix at sql_guard.py:319-320 rejects a leftover `*` whenever a warehouse table appears anywhere in the query, or when `has_leaky_star` is true.

- The three repros are rejected in both modes.
- These variants are also rejected:
  - `range(...)` and `generate_series(...)` with table subqueries in their arguments;
  - the same wrapped in a nested subquery or a CTE;
  - a CTE-fed `unnest((SELECT l FROM c))`;
  - EXISTS inside a CASE in the `unnest` arguments;
  - an IN-subquery in WHERE beside a star over `unnest`;
  - `range(1), unnest((SELECT list(title) ...))`;
  - `list(description)` (blocked column).
- Named-column forms are accepted, and each is flagged untrusted and redact:
  - `SELECT t.unnest ... t`
  - `SELECT u ... t(u)`
  - `SELECT upper(u) AS v ...`
  - `u` read through a subquery
  - `SELECT unnest((SELECT list(summary) ...)) AS s`
  - a scalar subquery
  - `SELECT * FROM unnest(...) t(u)`: the star expands through the alias list, and the output is flagged.

### No regressions

| Area | Status |
|---|---|
| I2b | Self- and forward-referencing CTEs are rejected. |
| C1 | `#10` is rejected. |
| C2 | `, range(1)` and `* LIKE` are rejected. |
| C3 | The unnest alias is flagged. |
| I1 | Catalog functions combined with a table, including through an IN-subquery, are rejected. Catalog-only queries are accepted with the flag and rejected without it. |
| Kept accepted | `WITH RECURSIVE` (including `SELECT *`), `SELECT * FROM range(3)`, `range(1), unnest([1])`. |
| Stand-in corpus (IT05-10) | Passes. |

### Findings

#### Critical / Important
- None.

#### Minor (residual, non-blocking)
- **M5.** Stars that fail closed over-reject a few harmless shapes, for example `SELECT * FROM range((SELECT count(*) FROM core.incident))` and `WITH r AS (SELECT * FROM range(3)) SELECT * FROM r`. This is safe, and the hint tells the agent to list the columns by name.
- **M3 (carried).** Lineage over-flags columns in some shapes (a LATERAL correlated column, a table-function alias). This is safe but causes extra redaction.
- **Carry-over (T05-13).** Set `python_enable_replacements=false` on the warehouse connection as defense in depth against unqualified-name resolution. The guard itself now blocks every repro.

### Gates (re-run)
- Card tests: 493 passed.

| Module | Line | Branch |
|---|---|---|
| sql_guard.py (382 lines) | 259/261 | 83/84 |
| _sql_guard_scope.py (84 lines) | 51/52 | 23/24 |

- ruff check and ruff format clean; mypy clean on both modules; check_module_size exit 0.

### Assessment
**Task quality:** Approved
**Reasoning:** Every Critical and Important finding from rounds 0–2 (C1–C4, I1, I2/I2b) is closed with regression tests. None of the variants tried got past the fixes, and valid queries and the corpus still pass. The only residuals are minor fail-closed over-rejection and over-flagging, plus a defense-in-depth carry-over.
