# T05-14 SQL guard — build report

Status: DONE
Commit: 7e135e5 feat(harness): add SQL guard (T05-14) (worktree branch worktree-agent-af7edb443ad06c3a8, base a633b17)

## What was built
- `herness/harness/sql_guard.py` (390 lines, budget 390, hard limit 400): `SqlGuard`, `GuardedQuery` (frozen, slots), constants `ALLOWED_SCHEMAS`, `DENIED_SCHEMAS`, `ALLOWED_TABLE_FUNCTIONS`, `CATALOG_TABLE_FUNCTIONS`, `DENIED_NODE_NAMES`, `DENIED_FUNCTION_RULES` (a `FunctionRules(prefixes, suffixes, exact)` NamedTuple with `.denies(name)`), `MAX_SQL_CHARS`, `MAX_JOINS`, `MAX_CTES`, `UNTRUSTED_TEXT_COLUMNS`, `REDACT_ON_READ_COLUMNS`, all with the spec's exact values.
- Algorithm follows U05-37 steps 1-12 in order. It stops at the first failing rule and raises `QueryError("SQL guard: <rule>", hint=...)`. Rule labels are size, parse, single_statement, select_only, denied_node, table, function, table_function, limits, column, second_parse. Every rejection logs INFO `harness.sql_guard.rejected` with `rule` and `query_hash` (the first 16 hex characters of SHA-256). The SQL text is never logged.
- Identifiers in the parsed tree go through NFKC + casefold before rules 3-6, and the schema mapping gets the same treatment. The SQL that runs is always the original text.
- Rule 6 uses `qualify(..., validate_qualify_columns=True, expand_stars=True, quote_identifiers=False)` against a `MappingSchema` built once per guard. Every `exp.Column` in the qualified tree is resolved through its enclosing scope and then the parent scopes. The two mappings in this step are:

| Input | Result |
|---|---|
| OptimizeError, or any other qualify failure | rejected (fail closed) with "did you mean" (rapidfuzz WRatio, cutoff 60, top 3) |
| Blocked column | text hint |

- Lineage walks each root output through CTE, subquery and set-operation scopes. Any exception marks every output as untrusted and redact-on-read (fail closed). Lineage only runs when a referenced column is untrusted.
- Second parse: one private in-memory DuckDB connection per thread (`threading.local`, created lazily, external access and extension autoload off). It runs `SELECT json_serialize_sql(?)`. A result is rejected on `error`, on a statement count other than 1, or on a root type outside the VI-10 set.

## Hardening beyond the letter of the spec (fail closed)
- **Table alias column lists** (`FROM core.incident t(a, b, c)`): qualify renames the columns, so `b` would otherwise escape the blocked check. I confirmed this bypass against the pinned sqlglot. The guard now treats a column read through an aliased-list table as reaching every column of that table.
- **Unresolved columns** (a correlated or unknown alias, or the catalog mode below): these count as reaching every table in the query that has that column name.
- **Function names**: checked under every spelling a function node can carry: the Anonymous name, sqlglot `sql_name()`, `key`, and the DuckDB-rendered head. For example, `range` is parsed as `GenerateSeries`, and both names are matched.
- **Table-position functions**: include `From`/`Join`/`Lateral`/`TableFromRows` sources, as well as `Table.this`.
- **Deep nesting**: a `RecursionError` is mapped to a parse rejection.
- **sqlglot warning**: its "falling back to Command" warning quotes the SQL text. A filter on the `sqlglot` logger drops it, so SQL text never reaches the logs.
- **Second-parse errors**: DuckDB or pybind cast errors (for example a lone surrogate) are rejected.

## Deviations (recorded)
1. **VI-10 (pinned DuckDB 1.5.5):** `json_serialize_sql` reports `SELECT_NODE` for plain, `WITH` and `WITH RECURSIVE` roots, and `SET_OPERATION_NODE` for set operations. `CTE_NODE` and `RECURSIVE_CTE_NODE` never appear as roots. As the default says, the spec's four-name set is kept. DuckDB serializes VALUES, DESCRIBE, SHOW, SUMMARIZE and FROM-first as `SELECT_NODE`, so rule 2 (sqlglot) is what rejects those.
2. **UT05-54:** the pinned sqlglot 30.19.0 resolves all 20 `DENIED_NODE_NAMES`, so the unresolved list is empty and the test asserts `[]`. UT05-59 therefore uses real parser differentials instead: sqlglot parses these as `Select` but DuckDB refuses them. The queries are `SELECT ... INTO core.change`, `... FOR UPDATE` and `LIMIT 1, 2`. UT05-59 also calls the second parser directly with PRAGMA, SET, INSTALL, multi-statement input and an undecodable string.
3. **Rule 3 through the public API:** at the top level, every denied statement already fails rule 2 (or parse), so the rejection message is select_only or parse. Rule 3 is tested on nested nodes directly and through `check()` with a monkeypatched tree.
4. **Hints not fully given in the spec:**

| Case | Hint |
|---|---|
| A 3-part name (catalog) | "schema <catalog>.<db> is not available" |
| A Table node that is neither an identifier nor a function | "qualify as core.<table>" |
| The column lookup finds no fuzzy match | "column not found; check names with describe_table" (instead of an empty "did you mean ?") |
| A table function that is not allowed | rule label `table_function` (hint text exactly as spec) |

5. **allow_catalog with `duckdb_tables()`/`duckdb_columns()`:** qualify cannot resolve the columns of a table function, so for these queries qualify runs with `validate_qualify_columns=False`. The blocked-column check still applies through the unresolved-column fail-closed rule. This only happens with `allow_catalog=True`, which is internal-only.
6. **Output column names in `untrusted_output_columns` / `redact_output_columns`:** these are sqlglot's qualified projection names. They match DuckDB for aliased and plain-column outputs. An unaliased computed expression gets sqlglot's `_col_<i>` rather than DuckDB's expression-text name. See the execute_recorded carry-over below.

## Tests (per ID)
- UT05-51 (tests/unit/harness/test_sql_guard.py):
  - SELECT, UNION and WITH RECURSIVE are accepted, using the schema from a real `open_warehouse(...).schema()`.
  - The ordered flag is checked on 8 shapes, including Paren, INTERSECT/EXCEPT, range, unnest and generate_series.
  - Lineage flags are checked through a CTE, a scalar subquery, a UNION branch, WHERE-only use, an aliased table column list and a UDTF, plus the forced lineage failure (fail closed).
- UT05-52: two statements are rejected; a trailing `;`/`;;` is accepted; empty input, unparsable input (hint ≤ 200 characters, ANSI stripped) and deep nesting are rejected; the rejection log carries rule and a 16-hex hash and no SQL text.
- UT05-53: VALUES, DESCRIBE, SHOW, SUMMARIZE, PIVOT and CHECKPOINT fail rule 2.
- UT05-54: the unresolved-name list is asserted empty; 18 denied statements are rejected; rule 3 is checked on nested denied nodes (9 classes) and through `check()`.
- UT05-55: stg, information_schema, pg_catalog, duckdb_*, an unknown schema, an unqualified table, a 3-part name, an unknown table, a file-path literal and `duckdb_tables()` without the flag are all rejected with hints; the CTE-name rules and a non-identifier Table node are covered.
- UT05-56: 15 denied functions × select/where/table position (45 cases), plus `columns()`, other table functions (the `repeat_row` table function and LATERAL with a scalar function) and the DuckDB-spelling check.
- UT05-57: all 7 blocked columns direct (text hint); misspelled column ("did you mean", includes `priority`); the no-match fallback; the unresolved catalog-mode column.
- UT05-58: 20 joins and 10 CTEs are accepted, 21 and 11 are rejected; 8,000 characters is accepted and 8,001 is rejected.
- UT05-59: 4 differential queries are rejected by second_parse; 5 direct second-parser rejections; the VI-10 root types of the accepted shapes; an unexpected root type; one lazy parser connection per thread.
- UT05-60: `duckdb_columns()`/`duckdb_tables()` (including a `$param`) are rejected without the flag and accepted with it.
- ST05-03 (tests/security/test_st05_sql_guard.py): hypothesis over 41 statements (DDL/DML/PRAGMA/SET/RESET/ATTACH/DETACH/USE/COPY/EXPORT/IMPORT/INSTALL/LOAD/transactions/CALL/SECRET and others) × 5 prefixes (alone, after `SELECT …;`) × suffixes × per-character case flips × random comment and whitespace separators. There is also a parametrized plain pass.
- ST05-04: hypothesis over 21 file, network and settings functions × 15 positions (select, from, join, IN-subquery, where, CTE, order by, group by, having, nested cast, lateral, union, scalar subquery, lambda) × case flips × allow_catalog. File-path string tables are tested in 5 table positions.
- ST05-05:
  - Hypothesis over the 7 blocked columns × 19 templates: alias, CTE, CTE `*`, subquery, `*`, `t.*`, `* EXCLUDE`, where, order by, function, correlated scalar, lateral, alias column list, union, aggregate, EXISTS and lambda.
  - Each identifier part is disguised in one of 5 ways: plain, quoted, mixed-case, fullwidth, or partial fullwidth including `＿`.
  - Comments are inserted and allow_catalog is varied.
  - Also: a parametrized 133-case plain pass that asserts the text hint, and a fullwidth test showing the case is caught by rule 6 rather than by a parse error.
- IT05-10 (tests/integration/harness/test_sql_guard_corpus.py): all 252 stand-in corpus queries are accepted on the schema of a real read-only warehouse, and each one also executes on the locked connection.
- BT05-03 (tests/bench/test_harness_sql_guard_bench.py, `[integration, slow]`): p95 < 25 ms over 3 passes of the corpus. Measured: p50 0.76 ms, p95 1.18 ms, max 17 ms, on 252 queries.

## Gate results
- Card tests: 320 passed. Coverage of sql_guard.py is 99% overall (line 265/268 = 98.9%, branch 82/84 = 97.6%). The misses are 3 defensive lines.
- Nightly profile: the 4 property tests pass with `--hypothesis-profile nightly` (10,000 examples each, 51 s). The commit profile uses 200 examples. No `max_examples` is hard-coded.
- `pytest -m "(unit or integration) and not slow"`: 1939 passed, 2 skipped, 1 xfailed. The skips and the xfail were already there before this card.
- `ruff format` / `ruff check` clean; `mypy` strict passes with 0 errors (75 files); lint-imports 11 kept, 0 broken; `check_module_size` exit 0 (390/390); `check_type_ownership` exit 0.
- RED: with the module moved aside, `pytest tests/unit/harness/test_sql_guard.py` stops at collection with ImportError: cannot import name 'sql_guard' from 'herness.harness'. GREEN: 139 unit tests pass.

## Files
- herness/harness/sql_guard.py (new)
- tests/unit/harness/_sql_guard_standin.py (new): stand-in schema (core/enrich/metrics/score/meta, every untrusted and redact-on-read column, the 7 default blocked columns), `make_warehouse()`, and `accepted_corpus()` (252 queries)
- tests/unit/harness/test_sql_guard.py, tests/security/test_st05_sql_guard.py, tests/integration/harness/test_sql_guard_corpus.py, tests/bench/test_harness_sql_guard_bench.py (new)
- No pyproject change: herness.harness is already in the import-linter contracts and in the mypy `files` list.

## Carry-overs
1. Re-run IT05-10 and BT05-03 on the real impl 11 fixtures (`tests/fixtures/sql_ok/` 200 queries; `small_build` and `full` schemas) once they exist. Replace the stand-in import accordingly.
2. The metric `herness_harness_sql_guard_rejections_total{rule}` is not emitted, because no metrics registry exists on base. Only the INFO log is emitted. Add the counter when the registry lands. The rule label is already at the single rejection point in `SqlGuard.check`.
3. Run the nightly profile in CI (it passed locally at 10,000 examples).
4. execute_recorded (a later card) must map `untrusted_output_columns`/`redact_output_columns` to the cursor's column names. Unaliased computed outputs carry sqlglot `_col_<i>` names, so match those by position, or require aliases.
5. UT05-60 extension (T05-xx tools): every internal SQL constant must pass `check(..., allow_catalog=True)`.

## Fix round 1 (review T05-14-review.md)

Status: DONE. Commit: 0a95357 fix(harness): close SQL guard column and lineage bypasses (T05-14)

### Fixes (each with a regression test)

**C1 — positional columns.**
- Fix: rule 6 rejects any `exp.PositionalColumn` in the query. The hint is "positional columns (#n) are not allowed; name the columns".
- Regression: ST05-05 `test_st05_05_positional_columns_rejected`, covering all 5 review repros plus `#1`. The hypothesis templates gained `SELECT #3 FROM {t}` and `... WHERE #3 IS NOT NULL`.

**C2 — unexpanded `*`.**
- Fix: after qualify, rule 6 rejects any remaining `exp.Star`, except the argument of `count(*)`. The hint is "* cannot be expanded here; list the columns by name".
- Scope: the check applies whenever the query references a warehouse table. A query over table functions only, such as `SELECT * FROM range(3)` or a catalog-only query, reads no warehouse column and still passes.
- Regression: ST05-05 `test_st05_05_unexpanded_star_rejected`, covering the 11 review shapes: range/unnest/generate_series joins, EXCLUDE, LATERAL, a subquery, `* LIKE`, `* SIMILAR TO` and `core.event, range(1)`.
- `i.* FROM core.incident i, range(1)` is also rejected, but by a different rule: qualify expands it, so it fails the blocked-column rule instead.
- Hypothesis templates gained `{t}, range(1)`, `CROSS JOIN unnest`, and `* LIKE`.

**C3 — lineage fails open through table-function aliases.**
- Fix: in `_reach`, a column whose source is not a real warehouse table or a query scope is treated as reaching every untrusted and redact-on-read column. That covers a table-function or unnest alias and an unresolvable source. The output is then marked untrusted and redact, which fails closed per the ruling.
- Regression: ST05-05 `test_st05_05_lineage_through_table_function_alias_fails_closed`, covering the 4 review repros plus a `range(2) r(x)` alias.
- The invariant "blocked column used only inside unnest args" is still rejected, pinned by `test_st05_05_blocked_column_inside_table_function_args`.
- The hypothesis templates gained `unnest([{c}]) t(u)`.

**I1 — catalog mode.**
- Fix: when a catalog table function (`duckdb_tables`/`duckdb_columns`) is used, the query may not reference any warehouse table. It is rejected as `table_function`, with the hint "catalog functions cannot be combined with warehouse tables".
- Relaxed column validation now only ever applies to catalog-only queries.
- Regression: UT05-60 `test_ut05_60_catalog_functions_never_mix_with_tables`, covering the 3 review repros, `SELECT * FROM core.incident, duckdb_tables()`, and the former UT05-57 catalog case.

**I2 — CTE exemption was not scope-aware.**
- Fix: an unqualified table name is exempt only if a CTE of that name is defined in the `WITH` of an enclosing query (sqlglot 30 arg key `with_`). Otherwise rule 4 gives the hint "qualify as core.<table>".
- Regression: UT05-55 `test_ut05_55_cte_name_only_visible_in_its_scope`. The review repro is rejected with "qualify as core.t". CTEs used from a nested subquery, from both UNION branches, and inside a derived table are still accepted.

### Minor findings
- **M1:** the filter stays on the `sqlglot` logger. It is scoped to that one logger, which is the one sqlglot's parser logs on. UT05-52 `test_ut05_52_sqlglot_command_fallback_warning_suppressed` now pins it: it fails if sqlglot rewords the message and SQL text reaches a handler. I verified that without the filter the record carries the SQL.
- **M2:** left as is. The per-thread connection is stored in `threading.local`. When the thread ends, its local storage is released and the DuckDB connection is closed on deallocation, so there is no lasting leak.
- **M3:** left as is. The over-flagging is on the safe side, and C3 widens it slightly: outputs that come from a table-function alias in a query that also references untrusted text are flagged.
- **M4:** the build report's coverage claims were wrong. Corrected claims:

| Claim | Before round 1 | After round 1 |
|---|---|---|
| Blocked-column check in catalog mode | Did not hold for table-as-row shapes | Holds; catalog mode is catalog-only (I1) |
| ST05-05 covers `*` | Only expandable stars | Also unexpanded stars (C2) |

### Structure and size
- Scope helpers moved to the new private module `herness/harness/_sql_guard_scope.py` (54 lines, default budget). It holds `visible_ctes`, `enclosing_scope`, `resolve`, `has_residual_star` and `has_positional_column`.
- `sql_guard.py` is now 381 lines (budget 390). No contract changes were needed; lint-imports reports 11 kept, 0 broken.

### Gates
- Card tests: 393 passed.
- Coverage:

| Module | Line | Branch |
|---|---|---|
| sql_guard.py | 259/261 | 83/84 |
| _sql_guard_scope.py | 31/32 | 11/12 |

- ST05-05 hypothesis with the new templates passes under the nightly profile (10,000 examples, 23 s).
- `pytest -m "(unit or integration) and not slow"`: 2012 passed, 2 skipped, 1 xfailed (all three already there).
- ruff format and ruff check clean; mypy strict passes with 0 errors (76 files); `check_module_size` and `check_type_ownership` both exit 0.

### Carry-overs (unchanged plus one new)
- Real impl 11 fixtures for IT05-10 and BT05-03.
- The rejection counter is not emitted until a metrics registry exists.
- Nightly profile in CI.
- execute_recorded output-name mapping.
- UT05-60 internal-constant extension. New from I1: internal tool SQL that uses `duckdb_tables()`/`duckdb_columns()` must be catalog-only.
- New: the review suggests `python_enable_replacements=false` on the warehouse connection (T05-13, another card).

## Fix round 2 (re-review, I2b)

Status: DONE. Commit: 22584b1 fix(harness): bind SQL guard CTE names in DuckDB order (T05-14)

### What changed
- **CTE binding order.** `_sql_guard_scope.visible_ctes` now follows DuckDB's binding order (ruling 1).
  - From a query body, every CTE of that query's `WITH` is visible.
  - From inside a CTE body, only the CTEs defined earlier in that `WITH` are visible, plus the CTE itself when the `WITH` is RECURSIVE (`with_.args["recursive"]`). Outer `WITH`s stay fully visible.
  - Later siblings are never visible. A non-recursive self-reference now fails rule 4 with "qualify as core.<name>".
  - Valid WITH RECURSIVE (UT05-51) is still accepted.
- **Leftover-star backstop.** `has_residual_star` is replaced by `has_leaky_star(q, by_expr)` (ruling 2).
  - A leftover `*` (not `count(*)`) is rejected whenever any source of its scope is not a table function, or its scope is unknown. It no longer matters whether a warehouse table is referenced.
  - Queries over table functions only still pass: `range`/`unnest`, or catalog-only `duckdb_*()`.
  - New fail-closed side effect: `WITH r AS (SELECT * FROM range(3)) SELECT * FROM r` is now rejected (star over a CTE whose columns qualify cannot expand). It is harmless but over-strict; the agent must name columns.

### Regression tests (ruling 3)
- **UT05-55** `test_ut05_55_self_and_forward_cte_references_rejected`: all 9 review repros × plain and catalog mode, each with hint "qualify as core.<name>".
- **UT05-55** `test_ut05_55_cte_binding_order`:
  - accepted: earlier sibling, outer CTE from a nested WITH, WITH RECURSIVE;
  - rejected: a forward reference under RECURSIVE, and the catalog repro `... FROM duckdb_tables(), t`.
- **UT05-55** `test_ut05_55_leftover_star_over_cte_rejected`: the backstop, with the table-function-only and catalog-only cases still accepted.
- **ST05-05** `test_st05_05_self_and_forward_cte_bypass_rejected`: 11 shapes × plain and catalog mode.
- **ST05-05 hypothesis templates** gained three templates: self-reference, forward reference and MATERIALIZED self-reference, each carrying the blocked column. The nightly profile (10,000 examples) passes.

### Gates
- Card tests: 456 passed.
- Coverage:

| Module | Line | Branch |
|---|---|---|
| sql_guard.py | 259/261 | 83/84 |
| _sql_guard_scope.py | 49/50 | 23/24 |

- `pytest -m "(unit or integration) and not slow"`: 2075 passed, 2 skipped, 1 xfailed (all three already there).
- ruff format and ruff check clean; mypy strict passes with 0 errors; lint-imports 11 kept, 0 broken; `check_module_size` and `check_type_ownership` both exit 0.
- Sizes: sql_guard.py is 381 lines (budget 390); _sql_guard_scope.py is 79 lines (budget 200).

### Carry-overs
- Unchanged from round 1.
- T05-13 should still set `python_enable_replacements=false` on the warehouse connection, as defence in depth.

## Fix round 3 (re-review round 2, C4)

Status: DONE. Commit: fea8fb0 fix(harness): reject leftover star when SQL guard query reads a table (T05-14)

### What changed
- **Leftover-star rule (ruling 1).** A leftover `*` (not `count(*)`) is now rejected in two cases:
  - the query references any warehouse table anywhere, including inside table-function arguments, so `tables` is non-empty;
  - `has_leaky_star` is true (the round-2 scope backstop).
- **Where.** `has_residual_star` is restored in `_sql_guard_scope.py`, and the check is on the line in `SqlGuard._check_columns`.
- **Effect.** The three C4 repros, plus `range(...)`/`generate_series(...)` with an argument subquery that reads a table, are rejected with "* cannot be expanded here; list the columns by name".
- **Still accepted.** Queries whose star reads only table functions (`SELECT * FROM range(3)`) or only the catalog. The stand-in corpus and WITH RECURSIVE are still accepted.

### Named forms (ruling 2)
I probed these on the round-2 code in plain and catalog mode. They were already correct, so no lineage change was needed:

| Query | Result |
|---|---|
| `SELECT u FROM unnest((SELECT list(summary) FROM core.work_item)) t(u)` | untrusted + redact on `u` (the table-function-alias fail-closed rule from round 1) |
| `SELECT (SELECT max(summary) FROM core.work_item) AS s` | untrusted + redact on `s` |
| `SELECT (SELECT max(title) FROM score.funding) AS s` | untrusted + redact on `s` |
| `SELECT unnest((SELECT list(summary) ...)) AS v` | untrusted + redact on `v` |
| `SELECT x FROM range((SELECT length(max(summary)) ...)) r(x)` | untrusted + redact on `x` |
| `SELECT (SELECT max(alert_name) FROM core.event) AS a` | untrusted on `a` |
| `SELECT unnest FROM unnest([(SELECT max(title) FROM score.funding)])` | rejected ("column not found": qualify cannot resolve the bare `unnest` column) |

### Regression tests (ST05-05)
- `test_st05_05_star_over_table_function_with_table_subquery_rejected`: 5 star shapes × plain and catalog mode.
- `test_st05_05_named_forms_through_subqueries_flagged`: the 6 named forms above × both modes.
- `test_st05_05_bare_unnest_column_name_rejected`.
- The hypothesis templates gained `SELECT * FROM unnest((SELECT list({c}) FROM {t}))` and `SELECT * FROM unnest([(SELECT max({c}) FROM {t})]) u`. The nightly profile (10,000 examples) passes.

### Gates
- Card tests: 493 passed.
- Coverage:

| Module | Line | Branch |
|---|---|---|
| sql_guard.py | 259/261 | 83/84 |
| _sql_guard_scope.py | 51/52 | 23/24 |

- `pytest -m "(unit or integration) and not slow"`: 2112 passed, 2 skipped, 1 xfailed (all three already there).
- ruff format and ruff check clean; mypy strict passes with 0 errors; lint-imports 11 kept, 0 broken; `check_module_size` and `check_type_ownership` both exit 0.
- Sizes: sql_guard.py is 382 lines (budget 390); _sql_guard_scope.py is 84 lines (budget 200).

### Carry-overs
- Unchanged, including `python_enable_replacements=false` for T05-13.
