# Report for T01-05: Deletion filter and lake-file helpers

Status: DONE

## Summary
Started from the prior build agent's uncommitted files (deletion.py 61,
lakefiles.py 131, test_deletion.py 106, test_lakefiles.py 247 lines). Reviewed
them against the brief (U01-33 DeletionFilter, U01-34 cleanup_orphan_temp_files,
U01-35 SchemaTracker/SchemaDrift) and found them already complete and correct:
- DeletionFilter: no-store constructor (R-10), lock-protected atomic swap,
  SchemaViolation("deletion set not loaded") before first reload, empty-set
  passthrough returning the same batch object, uses
  herness.store.ops.privacy.deleted_record_ids (re-exported at
  herness.store.ops per the module map).
- cleanup_orphan_temp_files: regex-gated, symlink/junction skip, resolved-path
  containment check, PermissionError/FileNotFoundError logged and continued,
  orphans_removed logged only when count > 0.
- SchemaTracker/SchemaDrift: name->type dict comparison ignoring column order,
  sorted added/removed/changed tuples, baseline replaced every call.

While I was validating and preparing a checkpoint commit, a second agent
instance on this same worktree (per its own message) completed the identical
work and made the final commit directly. My own `git commit` attempt (started
before that landed) ran the full pre-commit hook suite to completion
(ruff-check, ruff-format, mypy, import-linter, detect-secrets, module-size,
type-ownership, pytest-unit all Passed) against this exact content, then
found nothing left to commit once the other commit had landed — confirming
independently that this content passes every gate. No recommit was made;
working tree is clean at the existing final commit.

## Final commit
db1efe598539c36ebdeb6eecd43d83f55cb2efef
feat(connectors): add deletion filter and lake-file helpers (T01-05)
(parent cd44be5; four files added: herness/connectors/deletion.py,
herness/connectors/lakefiles.py, tests/unit/connectors/test_deletion.py,
tests/unit/connectors/test_lakefiles.py)

## Files + line counts (vs budget)
- herness/connectors/deletion.py: 61 lines (budget 90)
- herness/connectors/lakefiles.py: 131 lines (budget 160)
- tests/unit/connectors/test_deletion.py: 106 lines
- tests/unit/connectors/test_lakefiles.py: 247 lines

## Tests + coverage (independently re-verified after the commit)
- `pytest -k "UT01_25 or UT01_26 or UT01_27 or PT01_06"`: 15 passed, 1 skipped
  (real-symlink test skips when the host denies non-admin symlink creation;
  Windows CI/dev box here has no such privilege — this is the documented
  fallback path, not a gap: the symlink behavior is also covered by a
  deterministic monkeypatch test that does not skip).
- `PYTHONUTF8=1 pytest tests/unit/connectors -q -p no:logging`: 348 passed,
  1 skipped.
- Coverage (`--cov=herness.connectors.deletion --cov=herness.connectors.lakefiles --cov-branch`):
  - deletion.py: 100% line (32/32), 100% branch (4/4)
  - lakefiles.py: 94.3% line (66/70), 100% branch (20/20)
  - Missing lines 33-34, 55-56 in lakefiles.py are the `OSError` defensive
    except-branches in `_is_contained` (unresolvable path race) and
    `_remove_if_old` (lstat race) — both meet the >=90% line / >=85% branch
    floor with margin.
- ruff format/check, mypy, lint-imports, `tools.check_module_size`: all clean
  (verified independently both before and via the pre-commit hook run that
  accompanied my commit attempt).

## Deviations / spec notes
- None from the brief. `deleted_record_ids` is imported from its owning area
  submodule `herness.store.ops.privacy` rather than the top-level
  `herness.store.ops` package; both names are the same object per
  `ops/__init__.py`'s "same object" contract, consistent with other
  connectors code importing from area submodules.

## Concerns
- Two build-agent instances ended up active on this same worktree for this
  card at once (one via my dispatch, one already in flight per its own
  message), which controller-notes-w07.md's process rules say should not
  happen ("Never two build agents at once on the worktree"). No harm resulted
  here (identical content, no divergent commit), but the sub-controller may
  want to check why the dispatch/handoff for T01-05 fired twice.
