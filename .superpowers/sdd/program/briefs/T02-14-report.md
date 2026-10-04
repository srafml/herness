# Report for T02-14: Monitoring and generic staging

Status: DONE_WITH_CONCERNS (see Deviations/Concerns; all gates green, nothing outstanding
in scope except the noted card-scope correction).

## Files

New:
- `herness/model/sql/130_stg_monitoring.sql` (55 lines) - `stg.mon_event`, `stg.mon_metric_daily`, cast_stats.
- `herness/model/sql/140_stg_files.sql` (27 lines) - generic `stg.files_<entity>` per `extra_entities["files"]`.
- `herness/model/sql/150_stg_mongodb.sql` (27 lines) - generic `stg.mongodb_<entity>`.
- `herness/model/sql/160_stg_snowflake.sql` (27 lines) - generic `stg.snowflake_<entity>`.
- `tests/integration/model/test_model_stg_monitoring.py` (168 lines) - 130 tests.
- `tests/integration/model/test_model_stg_generic.py` (135 lines) - 140/150/160 tests.

Modified:
- `tests/integration/model/_stg_lake.py` - `build()` gained an optional `extra_entities:
  Mapping[str, Sequence[str]] | None = None` kwarg (default `None`, fully backward
  compatible with T02-13's callers) that drives both `scan_lake(..., extra_entities=...)`
  and the render context's `extra_entities`, so configured-entity staging can be tested
  the same way as the fixed sources. `110_stg_servicenow.sql`, `120_stg_jira.sql` and
  their test files were not touched.

None of these `.sql` files are covered by `tools/check_module_size` (it only scans
`*.py` under `herness/app/tools`), but all are far under the spec's 400-line/file budget
for `herness/model/sql/*` anyway.

## Card-scope correction (please confirm)

The brief's own "Unit specs (verbatim)" section merges U02-113/114/115 into one shared
table (`150_stg_mongodb.sql`, `160_stg_snowflake.sql`, `170_stg_dataverse.sql`) because
that is how spec 3 rows them. But the actual Task card in
`docs/impl/02-data-model.impl.md` (T02-14, "Monitoring and generic staging") lists:

```
Units | U02-111...U02-114
Files | herness/model/sql/130_stg_monitoring.sql, 140_stg_files.sql, 150_stg_mongodb.sql, 160_stg_snowflake.sql
Tests | IT02-33
```

`170_stg_dataverse.sql` (U02-115) is explicitly assigned to T02-15 ("Dataverse
staging, org/team/service, service map": `Units U02-115, U02-116, U02-117`, `Files
170_stg_dataverse.sql, 200_org_team_service.sql, 220_service_map.sql`). The brief's own
Module map row (only 130/140/150/160) agrees with this. Per the brief's explicit
instruction to flag this, I did not build `170_stg_dataverse.sql` - it belongs to
T02-15, which will presumably need it alongside `200`/`220`.

## Test ID note

Per the brief: IT02-16 and IT02-17 (the unit-spec-listed Tests for U02-111 in the older
numbering) are now the Tests row of the `260`/`270` core staging in a later card
(`docs/impl/02-data-model.impl.md` lines ~2788, ~2809), not this one - the brief told me
not to reuse them. `IT02-08` (cited by U02-112...U02-114's unit-spec rows) is already the
docstring ID used throughout T02-13's own empty-lake edge-case tests. Per the brief's
"use IT02-33 with a docstring saying so" instruction, every new test function in this
card is filed under IT02-33 (the card's own and only Tests-row ID), with a module-level
docstring in both new test files explaining the reassignment. This mirrors the acceptance
row's own wording ("IT02-33 passes; build with `extra_entities` for each source
renders") - both halves of that acceptance check are covered.

## Tests (IT02-33 -> functions)

`tests/integration/model/test_model_stg_monitoring.py`:
- `test_it02_33_mon_event_typed_columns_and_cast_stats` - ts/end_ts/severity typing +
  cast-fail counting (malformed end_ts, unmapped severity_raw), raw passthrough columns.
- `test_it02_33_mon_metric_daily_typed_columns_and_cast_stats` - date/value typing +
  cast-fail counting.
- `test_it02_33_monitoring_absent_entities_give_empty_typed_tables` - no monitoring lake
  files: both tables exist, empty, with the typed column set (TIMESTAMPTZ/DATE/DOUBLE).

`tests/integration/model/test_model_stg_generic.py`:
- `test_it02_33_files_service_costs_latest_rows_no_payload` - the spec's own IT02-33
  scenario verbatim: configured `files/service_costs`, two versions of one key plus a
  second key, latest-wins, no `_payload`/metadata columns beyond record_id/source_key/
  source_updated_at.
- `test_it02_33_files_absent_entity_metadata_columns_only` - configured entity, zero lake
  files: table exists with only the three metadata aliases.
- `test_it02_33_mongodb_and_snowflake_entities_stage` - U02-113/U02-114 same-shape check
  end to end (write, build, query) for both sources.
- `test_it02_33_extra_entities_for_each_source_renders` - the acceptance check's second
  half: renders 130-160 with `extra_entities` covering files/mongodb/snowflake together
  and asserts each output file's dynamic table name appears.

Also exercised (pre-existing, unaffected): `tests/unit/test_sql_coverage.py::
test_ut11_41_test_sql_coverage_every_created_table_is_referenced` (UT11-41) - required
adding the `stg.mon_event`/`stg.mon_metric_daily` references above, since `130`'s table
names are static (140-160's dynamic `stg.{{ ... | ident }}` names are template-time
expressions the coverage regex can't and doesn't need to match - it never flagged them).

## RED evidence

Before the coverage-test references existed, the first commit attempt failed at the
`pytest-unit` pre-commit hook (SQL files and `_stg_lake.py` already staged, new test
files not yet written):

```
$ git commit -F ...
...
pytest-unit...............................................................Failed
FAILED tests/unit/test_sql_coverage.py::test_ut11_41_test_sql_coverage_every_created_table_is_referenced
AssertionError: tables created but never referenced by a test:
['stg.mon_event', 'stg.mon_metric_daily']
```

This is the RED evidence for the coverage gate (confirms the check runs from a real
pre-commit hook, not skippable). The new test files then supplied the references.

## GREEN evidence

```
$ PYTHONUTF8=1 uv run pytest -k "IT02_33" -q -p no:logging --require-test-ids
.......
7 passed, 4763 deselected in 4.47s

$ PYTHONUTF8=1 uv run pytest tests/integration/model tests/unit/model -q -p no:logging
188 passed, 1 skipped in 11.34s

$ PYTHONUTF8=1 uv run pytest tests/unit/test_sql_coverage.py -q -p no:logging
4 passed in 0.07s

$ PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging
4740 passed, 7 skipped, 21 deselected, 2 xfailed in 330.26s
```

## Gate outputs

- `uv run ruff format .` - clean (0 files needing changes after fix-up).
- `uv run ruff check --fix .` - All checks passed! (one intentional `# noqa: PLR0913` on
  `_stg_lake.py:build()`, reason inline: the helper mirrors `RefData`/`RenderContext`
  fields one kwarg apiece; the pre-existing shape plus the new `extra_entities` kwarg
  pushed it to 7).
- `uv run mypy` (whole project, `--strict` per config) - Success: no issues found in 167
  source files.
- `uv run lint-imports` - Contracts: 13 kept, 0 broken.
- `uv run python -m tools.check_type_ownership` - exit 0 (no output; no new
  `herness.core.types` submodules).
- `uv run python -m tools.check_module_size` - exit 0.

## Deviations from the brief

1. Did not build `170_stg_dataverse.sql` - see "Card-scope correction" above. If the
   controller disagrees and wants it built under this card after all, it needs a fresh
   brief/unit read of design 02 section 4.1 for Dataverse-specific column handling (none
   was given to this card beyond "same shape as U02-112").
2. Used IT02-33 for every new test function instead of IT02-16/IT02-17/IT02-08 - per the
   brief's explicit instruction, with docstrings flagging it (see "Test ID note" above).
3. Extended `tests/integration/model/_stg_lake.py::build()` with an optional
   `extra_entities` kwarg (default `None`, non-breaking) rather than duplicating its
   scan/context/run wiring in the new test files. This is shared test infrastructure
   (not itself a T02-13-owned SQL/test file), and T02-13's own tests were re-run
   unmodified (25/25 pass) to confirm no regression.

## Concerns

- `130_stg_monitoring.sql`'s Tests-row ID history (IT02-16/17 reassigned away from it) is
  a symptom of the unit-spec table at line ~2628 not having been refreshed to match the
  card table at line ~3895-3907; worth a spec consistency pass at some point, but out of
  this card's scope to fix.
- The generic-staging tables' names are computed entirely at render time
  (`stg.{{ (...) | ident }}`), so `tools/sql_coverage`'s `tables_created()` regex cannot
  and does not discover them - meaning UT11-41 will never force a future change to
  140/150/160 to add a test. This is a pre-existing limitation of the coverage tool for
  any dynamically-named table, not something introduced by this card, but flagging it in
  case the controller wants a follow-up card to extend the coverage tool.
- `extra_entities["files"|"mongodb"|"snowflake"]` are wired through and consumed
  correctly by the SQL, but nothing in the tree yet calls `scan_lake(..., extra_entities=
  ...)` with these pairs from the real pipeline (that wiring is presumably a later
  harness/pipeline card, per the render_context.py `_configured_entities()` helper that
  already exists from an earlier card). Not a T02-14 concern, just noting the dependency
  for whichever card owns that call site.
