# T01-04 report: Ops-store ingestion functions

Status: DONE_WITH_CONCERNS (minor; see Concerns)
Commit: 0bf033e feat(store): add ops-store ingest area (T01-04)
Branch: worktree-agent-acd2b623b895237a5 (base 8a073b4)

## Implemented
- `herness/store/ops/ingest.py` (new, 257 lines, budget 260): `Watermark`, `SliceRow`, `FileIngestRow`
  (frozen, slotted dataclasses), `get_watermark`, `set_watermark`, `list_watermarks`, `ensure_slices`,
  `mark_slice_running`, `mark_slice_done`, `mark_slice_failed`, `get_file_ingest`, `record_file_ingest`.
  SQL statements verbatim from U01-28 / U01-30 / U01-31 / U01-32; `run_write` ops named after the
  functions; `ensure_slices` reads back via `read_all` in chunks of 500 slice starts; `list_watermarks`
  uses `max_rows=10_000`; corrupt watermark text -> `SchemaViolation("corrupt watermark", source=, entity=)`;
  missing slice -> `SchemaViolation("slice not found", source=, entity=)` raised inside the callback (rollback);
  error truncated to 500 chars; fingerprint `^[0-9a-f]{64}$` checked (fullmatch) in get and record.
  Imports only `.core` from the ops package (plus herness.core.time as clock, herness.core.errors).
- `herness/store/ops/__init__.py`: new `# 01 ingest` import block and `__all__` block after `02 migrate`
  (names in module-map order). Nothing else touched; core.py untouched.
- `tests/unit/store/ops/test_store_ops_ingest.py` (new): UT01-20 (x4 incl. re-export identity), UT01-22 (x5),
  UT01-23 (x3 + parametrised missing-row), UT01-24 (x2 + parametrised bad fingerprints), UT01-95 (x2).
  Uses the interim `ops_store` fixture plus `migrate()`.

## RED / GREEN
- RED: `PYTHONUTF8=1 uv run pytest tests/unit/store/ops/test_store_ops_ingest.py -q -p no:logging`
  -> `ImportError: cannot import name 'ingest' from 'herness.store.ops'` (collection error).
- GREEN: `PYTHONUTF8=1 uv run pytest -k "UT01_20 or UT01_22 or UT01_23 or UT01_24 or UT01_95 or UT02_68" -q -p no:logging`
  -> 29 passed.
- Coverage (`--cov=herness.store.ops.ingest --cov-branch`, tests/unit/store/ops): 138 stmts, 10 branches, 100% / 100%.
- Full: `pytest -m "(unit or integration) and not slow"` -> 3418 passed, 5 skipped, 1 xfailed (pre-existing).

## Gates
ruff format/check clean; mypy: no issues (128 files); lint-imports: 13 kept, 0 broken (ops-areas-acyclic
covers ingest.py via wildcards; herness.store imports nothing from herness.connectors); check_type_ownership 0;
check_module_size 0; no `herness/store/ops_ingest.py`. All pre-commit hooks passed (no --no-verify, no
PRE_COMMIT_ALLOW_NO_CONFIG). No pyproject / secrets baseline change needed.

## Deviations / concerns
1. Test location: impl 01 §11 says `tests/unit/connectors/`; tests placed in `tests/unit/store/ops/` next to the
   other ops-area tests since the module is L1 `herness/store/ops/ingest.py`. Easy to move if the controller prefers.
2. Added beyond spec text (small, defensive): a `files` JSON column that is valid JSON but not a list of strings
   raises `SchemaViolation("invalid JSON in sync_slice.files" / "file_ingest.files")` instead of producing a bad tuple.
   Corrupt timestamps in sync_slice / file_ingest surface as `parse_utc`'s `SchemaViolation("bad timestamp text")`.
3. Not validated (spec lists them as preconditions without an error): `slices` non-empty/contiguous in
   `ensure_slices` (empty -> returns []), POSIX-relative `files` paths. `rows < 0` is rejected by the table
   CHECK, surfacing as `SchemaViolation` from `run_write` (tested).
4. Budget: 257/260 lines, reached by compacting row constructors (positional unpacking); no ruling needed.
5. Attribution: used the dispatch line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
   (global-constraints.md shows a "(1M context)" variant).

## Fix round 1 (review T01-04-review.md)

Commit: c026d32 fix(store): order ingest re-exports after migrate and tighten tests (T01-04)

- Important 1: `herness/store/ops/__init__.py` — the `# 01 ingest` import block now comes after `# 02 migrate`,
  preceded by `# isort: split` (impl 02 §2.3 rule 5 / U02-62 order). `__all__` order unchanged. ruff clean, UT02-68 passes.
- Minor 2: `test_ut01_22_reads_in_chunks` wraps `ingest.read_all` with a counter and asserts
  `max_rows` per call == [500, 500, 201] for 1201 slices; a no-chunk mutant ([1201]) fails.
- Minor 3: `test_ut01_22_longer_last_slice_resets` adds a running-row case (running row with a longer planned end
  -> pending, rows 0, files (), attempts kept at 2). Done and failed cases were already covered.
- Minor 4: the re-export identity test now covers all 12 ingest names.
- Minor 5: DONE. `_paths(row, table)` raises `SchemaViolation("invalid JSON in <table>.files", source=, entity=)`.
  Test asserts the context. ingest.py stays at 257/260 lines.
- Minor 1 (502 bound params vs "500 parameters"): no code change; spec wording note for the controller.

Evidence: `PYTHONUTF8=1 uv run pytest -k "UT01_20 or UT01_22 or UT01_23 or UT01_24 or UT01_95 or UT02_68" -q -p no:logging`
-> 29 passed. ingest.py coverage is 100% line and 100% branch (139 stmts, 10 branches). mypy clean, lint-imports 13 kept,
type-ownership 0, module-size 0. All pre-commit hooks passed, including pytest-unit.
