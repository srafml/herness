# Report for T05-12: Ops evidence functions

Status: DONE

Commit: `11560c6a16fed6cc63f3f1aeaa9747edaf2733ce` -- feat(store): add ops evidence area (T05-12)
Branch: worktree-agent-a091d7071442bc8d1 (worktree D:\herness\.claude\worktrees\agent-a091d7071442bc8d1)

## What was built

`herness/store/ops/evidence.py` -- the `herness.store.ops.evidence` area, the only writer of
the ops `evidence` and `evidence_use` tables (migration 003, already on base):

- `record_evidence(ev: Evidence) -> bool` -- `INSERT OR IGNORE` on PK `query_id` in one
  `run_write` transaction; params and result_sample JSON-encoded with `core.dump_json`;
  `executed_at` formatted with `clock.format_utc`. Returns True iff a row was newly inserted
  (`cursor.rowcount == 1`).
- `record_evidence_use(query_id, run_id, task_id, used_at) -> bool` -- `INSERT OR IGNORE` on PK
  `(query_id, run_id, task_id)`; `task_id=None` stored as `""`.
- `get_evidence(query_id) -> Evidence | None` -- reads the row, recomputes `query_id` from the
  stored `sql`/`params`/`build_id` via `herness.core.ids.query_id`, and returns `None` with a
  WARNING log `harness.evidence.tampered` (`query_id` only, never raw sql/params) on any
  mismatch -- including when recompute itself raises `SchemaViolation` (e.g. a corrupted
  `build_id`), which is also treated as tampered rather than propagated.
- `finding_statuses(finding_ids: Sequence[str]) -> dict[str, str]` -- filters ids against
  `^fnd_[0-9A-HJKMNP-TV-Z]{26}$`, caps at 500, then one `SELECT ... WHERE finding_id IN (...)`
  with one bound `?` per id (empty list short-circuits to `{}` without querying, since SQLite
  rejects `IN ()`).
- `scrub_record_from_evidence(record_id, /) -> int` -- impl 10 privacy-deletion hook (U05-75).
  Validates `record_id` via `herness.core.ids.split_record_id`, re-raising any failure as
  `SchemaViolation("invalid record_id")`. One `run_write` transaction: `SELECT` rows whose
  `result_sample` text contains the JSON-escaped record id (`instr`), decode each with
  `load_json`, drop only the sample rows whose string cells contain the raw record id, and
  `UPDATE` only the rows that actually changed. Idempotent (second call returns 0). Logs INFO
  `harness.evidence.scrubbed` with `record_id_hash` (16 hex of SHA-256) and `rows`; the raw
  record id is never logged.

`herness/store/ops/__init__.py` -- added a `# 05 evidence` re-export block (imports + `__all__`
names) after the `# 02 migrate` block, per the docstring's ordering rule. Ruff's isort wanted
to sort the three `from .<area> import` blocks alphabetically (`core, evidence, migrate`),
which would violate the "core, migrate, shared, then areas in row order" rule, so I added
`# noqa: I001` to the `from .core import (` line, matching the existing precedent in
`herness/core/types/harness/__init__.py`.

## Files

| File | Lines | Budget |
|------|-------|--------|
| `herness/store/ops/evidence.py` | 182 | 220 (ENG hard limit 400) |
| `herness/store/ops/__init__.py` | 65 | n/a (re-export only) |

## Tests

`tests/unit/store/ops/test_store_ops_evidence.py` (new; `pytest.mark.unit`):

- `test_ut05_47_record_evidence_is_idempotent_and_keeps_first_run` -- UT05-47 store part: two
  `record_evidence` calls with the same `query_id` but different `run_id` keep the first
  `run_id`; only one row exists.
- `test_ut05_47_record_evidence_use_dedupes_by_pk_two_tasks` -- UT05-47 store part: two tasks'
  uses insert two rows, a repeat of one key is ignored, `task_id=None` stores as `""`.
- `test_ut05_48_get_evidence_none_for_missing_and_tampered` -- UT05-48: missing query_id, a
  directly-corrupted row (sql/query_id mismatch) and a row with a corrupted `build_id` (forces
  the `SchemaViolation` branch inside `get_evidence`) all return `None` with exactly one
  `harness.evidence.tampered` WARNING each; a genuine row round-trips unchanged.
- `test_ut05_48_finding_statuses_dict` -- UT05-48: known ids return their status, an unknown
  valid-pattern id is absent, a bad-pattern id is dropped, empty input returns `{}`.
- `test_ut05_126_scrub_removes_only_matching_rows_and_is_idempotent` -- UT05-126: one row with
  the record id as a whole cell value, one with it as a substring inside a larger string, one
  clean row, one row whose sample is not a list (defensive skip), and one row where the record
  id occurs only in a JSON key (not a value -- `instr` still matches at the SQL level, but no
  cell is removed, exercising the "matched but no change" branch). First call rewrites exactly
  the two true-positive rows, `query_id`/`result_hash`/`row_count` unchanged, one scrub log
  with `rows=2` and no raw record id in the log; second call returns 0.
- `test_ut05_126_scrub_rejects_invalid_record_id` -- malformed id raises
  `SchemaViolation("invalid record_id")`.

Both `migrated` fixture parametrizations (`fresh`: one `migrate()` call; `stepwise`: 001-002
applied first via a monkeypatched `_migrations_root`, then 003-006 to latest) run every test
in this file, satisfying the "fresh and upgraded fixture DB" acceptance check -- so the 6 test
functions above run as 10 parametrized cases (the two `scrub` tests use `pytest.raises`/direct
assertions independent of migration path but still run under both params), for 12 total.

`tests/bench/test_store_ops_bench.py` (existing file, appended to; `[integration, slow]`):

- `test_bt05_08_evidence_and_use_pair_write_p95` -- BT05-08: 10,000 `record_evidence` +
  `record_evidence_use` pairs (unique `sql` per iteration so every pair is a real insert, not
  an `INSERT OR IGNORE` no-op) on a WAL ops store; asserts p95 < 10 ms. Passed locally
  (single run; no CI hardware guarantee).

## Coverage

`herness/store/ops/evidence.py`: 100% line, 100% branch (80 statements, 12 branches; via
`pytest tests/unit/store/ops/test_store_ops_evidence.py --cov=herness.store.ops.evidence
--cov-branch`), against the >=90%/>=85% requirement.

## Verification run

- `uv run ruff format .` / `uv run ruff check --fix .` -- clean (one pre-existing S608 finding
  in `finding_statuses`'s dynamic `IN (...)` placeholder count suppressed with `# noqa: S608`
  and a comment; only the placeholder count is interpolated, ids are bound as params).
- `uv run mypy` -- clean.
- `uv run lint-imports` -- all 13 contracts kept, including `ops-areas-acyclic` (evidence.py
  imports only `from . import core`).
- `uv run python -m tools.check_module_size` -- clean.
- `PYTHONUTF8=1 uv run pytest tests/unit -q -p no:logging -m "unit and not slow"` -- 2990
  passed, 4 skipped (pre-existing, platform-specific symlink skips unrelated to this change),
  1 deselected, no regressions.
- Committed via `git commit`; the repo's pre-commit hooks (ruff, mypy, import-linter,
  detect-secrets, module-size, `type-ownership`, and the full `pytest -m unit` suite) all ran
  and passed as part of the commit -- no `--no-verify`, no baseline edits needed.

## Deviations / spec notes

- FT05-04 (listed under U05-71 in the impl doc but not in this card's Tests row) was not
  built, per the brief's explicit instruction.
- `get_evidence`'s tamper check only compares recomputed vs. stored `query_id` (from `sql`,
  `params`, `build_id`); it does not detect tampering of `result_sample` alone, because the
  unit spec's postcondition and U05-75's postcondition both say the Verifier re-runs queries
  instead of trusting samples -- sample tampering is out of scope for TH05-12 by design, and
  `scrub_record_from_evidence` explicitly rewrites `result_sample` without touching
  `query_id`/`result_hash`/`row_count`, confirming samples are not covered by the tamper hash.
- `finding_statuses`'s "<= 500 ids" is enforced defensively by silently truncating the
  pattern-valid ids to the first 500 (slice), rather than raising, since the unit spec's
  Preconditions/Errors fields name no error for exceeding it -- consistent with how
  `ConfigError.issues` and `_bound_details` cap elsewhere in the codebase rather than raising.
- The "fresh and upgraded fixture DB" acceptance check is satisfied by a local `migrated`
  fixture (parametrized `fresh`/`stepwise`) in the new test file, built the same way
  `tests/unit/store/ops/test_store_ops_migrate.py`'s `mig_dir` fixture does (monkeypatching
  `herness.store.ops.migrate._migrations_root` to a temp copy of the migration files, applying
  001-002 first, then adding 003-006 and migrating again) -- the public `migrate()` API takes
  no target version, so this is the only way to build a genuinely stepwise-upgraded store.

## Concerns

- None outstanding. The only friction was ruff's isort wanting to alphabetize the three
  `__init__.py` import blocks; resolved with the same `noqa: I001` convention already used in
  `herness/core/types/harness/__init__.py`, so a future area (e.g. one that sorts before
  `core` or `migrate` alphabetically) should double-check block order survives `ruff check
  --fix` before committing.
- BT05-08's p95 < 10 ms threshold was measured once locally and passed comfortably; as with
  BT02-06/BT02-07 in the same file, there's no CI performance gate wired up here, so a slower
  CI runner could in principle flake this test (inherent to the benchmark, not something this
  card introduces).
