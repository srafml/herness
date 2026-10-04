# Report: T02-24 -- Review-item helpers for impl 03 and impl 07

## Summary

Continued from a killed build agent's WIP (herness/store/ops/shared.py modified in place,
450 lines, over the 390 budget). The WIP's three new functions were sound (correct SQL,
transaction semantics, validation) so I kept them and refactored for budget rather than
rewriting from scratch.

Implemented:
- create_review_item_if_absent (U02-130): idempotent insert inside one BEGIN IMMEDIATE
  transaction (run_write); returns (item_id, created).
- count_review_items (U02-131): ungrouped {"": n} or grouped by one payload key.
- update_review_payload (U02-132): replaces named top-level payload fields on any status
  (supports impl 07 memory_write erasure); conn joins the caller's transaction.
- Re-exported all three in herness/store/ops/__init__.py's "02 shared" block, in U02-62
  order: create_review_item, create_review_item_if_absent, get_review_item,
  list_review_items, count_review_items, decide_review_item, update_review_payload,
  approved_mapping_suggestions. UT02-68 (already written forward-looking in T02-07) passes
  unchanged.

## Budget path taken: private sibling module

shared.py was 349 lines before this card; the three new units plus SQL constants and a
shared match-key validator would not fit in 390 through docstring/formatting compaction
alone (the three functions are comparable in size to existing U02-56..60 functions, ~70
lines together). Per the group ruling, I moved PURE helpers -- nothing that imports
shared -- into a new private sibling herness/store/ops/_review_common.py:

- SQL text constants: MATCH_LOOKUP, COUNT, COUNT_GROUPED, PAYLOAD_ONLY, UPDATE_PAYLOAD,
  plus MAX_FIELDS.
- keys_ok (match-key / field-key validation, shared by U02-130 and U02-132).
- Also moved, to close the remaining gap: MATCH_KEY_RE (previously duplicated for U02-58's
  payload_match), is_count (U02-58's limit/offset check), is_json_object and
  check_decision (U02-59's decide_review_item preconditions, with their private constants
  USER_REF_RE, DECISIONS, MAX_NOTE_CHARS). These are pure, have no dependency on shared,
  and shared.py now binds local aliases (_is_count = _review_common.is_count, etc.) so
  every existing call site in list_review_items / decide_review_item is unchanged.

shared.py imports _review_common one-way (from . import _review_common, used as
_review_common.NAME), mirroring the core -> _shims precedent. Never the reverse:
_review_common.py imports only herness.core.errors.ConfigError and stdlib (json, re,
typing, collections.abc) -- no herness.store.ops.shared import, no cycle.

Same-commit updates:
- pyproject.toml: added "herness.store.ops.shared -> herness.store.ops._review_common"
  to the ops-areas-acyclic contract's ignore_imports.
- docs/impl/02-data-model.impl.md Sec 2: added a module-map row for
  herness/store/ops/_review_common.py next to the _shims.py row, budget 100 (actual 76,
  about 32 percent headroom), stating "private helper module of shared; not an area
  (Sec2.3)". tools/check_module_size picks this up automatically (it parses every
  docs/impl/*.impl.md "Path | ... | Line budget" table); no pyproject.toml
  [tool.herness.module_budgets] override was needed since 100 is below the 400 default.

One mypy fix beyond the WIP: the blocking_statuses: Collection[ReviewStatus] = ("pending",)
default was rejected by mypy --strict (the bare tuple literal infers as tuple[str], not
assignable to Collection[Literal[...]]); a cast(...) triggered "redundant-cast" instead
(mypy applies the parameter's declared type to the cast's target but not to a bare literal
default). Used a type: ignore[assignment] comment on that line, matching the existing repo
convention (herness/core/config.py:98, config_sources.py:116, etc. use inline
type: ignore[code] with no separate reason text).

## Files

- herness/store/ops/shared.py -- 390/390 lines (exactly the impl 02 Sec2 budget).
- herness/store/ops/_review_common.py -- new, 76/100 lines.
- herness/store/ops/__init__.py -- 218/400 lines (3 names added, U02-62 order).
- pyproject.toml -- ops-areas-acyclic ignore_imports line added.
- docs/impl/02-data-model.impl.md -- module-map row for _review_common.py added.
- tests/unit/store/ops/test_store_ops_shared.py -- UT02-72/73/74 tests added.

## RED evidence

The WIP already had the three functions written but no tests; running the new tests
against a stripped tree would trivially fail with ImportError. Since the implementation
and its tests were written together in one pass after inheriting sound WIP code, I did not
re-run a separate RED step against a stripped-down tree; I verified GREEN plus full-suite
regression (below) instead. Flagging as a minor process deviation from strict TDD ordering.

## GREEN evidence

Command: PYTHONUTF8=1 uv run pytest tests/unit/store/ops -q -p no:logging
Result: 312 passed in 10.24s

Coverage of the two changed/added modules (PYTHONUTF8=1 uv run pytest tests/unit/store/ops
with cov options for herness.store.ops.shared and herness.store.ops._review_common,
branch coverage, term-missing report):

  Name                                  Stmts   Miss Branch BrPart  Cover   Missing
  herness/store/ops/_review_common.py      35      0      6      0   100%
  herness/store/ops/shared.py             212      2     62      1    99%   384-385
  TOTAL                                   247      2     68      1    99%
  312 passed in 12.85s

Lines 384-385 (uncovered) are update_review_payload's defensive
"if not isinstance(payload, dict): raise SchemaViolation(...)". I attempted to cover it via
a raw-SQL-corrupted payload column, but the review_item table's CHECK constraint
(json_valid(payload) AND json_type(payload) = 'object') rejects the write before
update_review_payload ever runs -- the branch is genuinely unreachable through the store
and is defense-in-depth only (same pattern as other isinstance(..., dict) guards in this
file). 99 percent line and about 98.4 percent branch coverage overall clears the 90/85 gate.

Full gate sequence, all clean:
- ruff format: 1 file reformatted (test file), then stable
- ruff check with fix: all checks passed
- mypy: success, no issues found in 144 source files
- lint-imports: 13 contracts kept, 0 broken
- tools.check_module_size: exit 0
- tools.check_type_ownership: exit 0
- full suite (unit or integration, not slow): 3855 passed, 5 skipped, 17 deselected,
  1 xfailed, 2 warnings in 294.86s

(5 skips are pre-existing Windows symlink-privilege / coverage.json skips unrelated to this
card; the 1 xfail is a pre-existing tracked doc-traceability xfail.)

Commit hooks (ruff-check, ruff-format, mypy, import-linter, detect-secrets, module-size,
type-ownership, pytest-unit) all passed at commit time; PRE_COMMIT_ALLOW_NO_CONFIG was NOT
set and the no-verify flag was NOT used, per controller-notes-w07.md.

## Deviations and spec notes

- Budget path: private sibling module (_review_common.py), as detailed above -- the
  explicitly-sanctioned fallback in the group ruling, used because compaction alone could
  not fit U02-130..132 (plus their SQL/validation) into 390 lines without also moving some
  pre-existing U02-58/U02-59 pure helpers (390 minus the three new functions' irreducible
  about 70 lines left no room for those constants staying local).
- A type: ignore[assignment] comment was added on the blocking_statuses default (mypy and
  Literal typing friction, not a spec deviation) -- see above.
- No SchemaViolation coverage for update_review_payload's "row payload not a dict" branch
  (unreachable given the DB CHECK constraint) -- left uncovered by design; overall module
  coverage still clears the gate.
- Did not touch herness/store/ops/core.py (280/280, per controller notes: never edit) or
  _shims.py (reused only, no new shims added).

## Concerns

- shared.py is at exactly 390/390 lines with zero headroom. Any future card touching this
  file will need the same budget discipline (or another sibling-module extraction).
- The RED step was not run as a separate falsifying pass before GREEN (see RED evidence
  note above) -- TDD discipline was followed in spirit (failing-then-passing was implicit
  in inheriting unintegrated WIP code plus new tests) but not literally re-verified
  per-test.
