# Review: T02-24 — Review-item helpers for impl 03 and impl 07

Commit reviewed: e51533e ("feat(store): review-item helpers create_if_absent, count, update_payload (T02-24)")
Base: 6b70189

## Spec compliance

- U02-130 `create_review_item_if_absent` — Yes/compliant. Signature, defaults and constraints match (match_keys 1-8 distinct, present in payload, pattern-checked via `_review_common.keys_ok`; blocking_statuses 1-3 distinct via reused `_statuses_json`). Algorithm matches exactly: builds `{key: payload[key]}` match object via `dump_json`, runs the specified `MATCH_LOOKUP` SQL text verbatim inside one `run_write(op="review_item_create_if_absent")` (confirmed `BEGIN IMMEDIATE` in `core.run_write`), binds `(kind, statuses, match)` in the spec's order, logs `store.ops.review_item_exists` at DEBUG with `item_id`, `kind` on the found branch, else calls `create_review_item(kind, payload, now=now, conn=tx)` and returns `(new_id, True)`. Keys reach SQL only as bound JSON data. UT02-72's two-thread race test passed on every run (verified 3x in isolation plus in the full suite).
- U02-131 `count_review_items` — Yes/compliant. Kind/status validated first (ConfigError on bad values). Ungrouped path uses the spec's fixed `COUNT` SQL; grouped path uses `COUNT_GROUPED` with the JSON path bound as a parameter (`'$.' + group_by_payload`), and `coalesce(CAST(... AS TEXT), '')` correctly maps a missing or JSON-null value to `""`. Row cap is impl 02's existing `read_all` default (100,000), unchanged. Caveat: `COUNT_GROUPED` in `_review_common.py` appends `ORDER BY k` to the literal SQL text quoted in the brief -- needed to satisfy the unit's own documented postcondition ("Returns ... keys sorted"), semantically equivalent, and keys are still only ever bound as data. Not treated as a spec violation, just noted as a literal-text deviation.
- U02-132 `update_review_payload` — Yes/compliant. Positional/keyword shape, `item_id` pattern check, `fields` 1-32 keys via `keys_ok` (no `payload` arg so "present in an existing payload" is correctly not required), `conn` join-vs-own-transaction behavior verified by test (`with_conn` test rolls back correctly on caller failure). Algorithm steps 1-4 match verbatim (`PAYLOAD_ONLY` select, `NotFoundError` on missing row, `load_json`/merge/`dump_json`, `UPDATE_PAYLOAD`, then `store.ops.review_payload_updated` at INFO with `item_id`, `keys` -- values never logged, confirmed by `test_ut02_74_log_has_key_names_only`). `NotFoundError(kind="review_item", key=item_id)` shape preserved via the existing `_not_found` helper.
- Group ruling (shared.py budget via `_review_common.py` split) — Yes/compliant. `shared.py` is exactly 390 lines (the stated cap, satisfied at the boundary). `_review_common.py` (76 lines) holds only pure, stateless helpers (`keys_ok`, `is_count`, `is_json_object`, `check_decision`, SQL text constants) and never imports `.shared` (grep-verified; import runs one-way `shared -> _review_common`, mirroring `core -> _shims`). The T02-07 functions (`create_review_item`, `get_review_item`, `list_review_items`, `decide_review_item`, `approved_mapping_suggestions`) are behavior-preserved -- the extracted helpers (`_is_count`, `_is_json_object`, `_check_decision`, `_MATCH_KEY_RE`) are re-aliased at module level with identical bodies/regexes, confirmed line-by-line in the diff.
- `pyproject.toml` `ops-areas-acyclic` contract gained the required `"herness.store.ops.shared -> herness.store.ops._review_common"` ignore_imports line. `docs/impl/02-data-model.impl.md` Sec2 gained the `_review_common.py` module-map row next to `_shims.py`, budget 100 (actual 76). Both required by the ruling -- present in the same commit.
- U02-62 order in `ops/__init__.py` "02 shared" block — Yes/compliant. Import block is alphabetized (isort convention, as before); `__all__` block order is `ReviewItem, ReviewKind, ReviewStatus, create_review_item, create_review_item_if_absent, get_review_item, list_review_items, count_review_items, decide_review_item, update_review_payload, approved_mapping_suggestions`, which matches both the brief's module-map "Public names" column and the pre-existing (unmodified by this commit) `_SHARED_BLOCK` constant in `tests/unit/store/ops/test_store_ops_package.py` used by UT02-68. `test_ut02_68_review_item_only_in_shared` and the other UT02-68 tests all pass (8/8).
- Test IDs/names/docstrings/pytestmark — Yes/compliant. `test_ut02_72_*`, `test_ut02_73_*`, `test_ut02_74_*` names/docstrings correctly ID'd; module docstring updated to mention the new unit/test IDs; module-level `pytestmark = pytest.mark.unit` unchanged and present.
- Errors — Yes/compliant. `ConfigError`/`SchemaViolation`/`NotFoundError`/`StoreBusy` used as specified (`StoreBusy` arrives via `run_write`/`read_all`, not re-raised locally, consistent with pre-existing pattern).

## Verification run (this worktree, commit e51533e)

- `PYTHONUTF8=1 uv run pytest tests/unit/store/ops -q -p no:logging --cov=herness.store.ops.shared --cov=herness.store.ops._review_common --cov-branch --cov-report=term-missing` -> 312 passed. Coverage: `_review_common.py` 100% line/branch; `shared.py` 99% line (212 stmts, 2 miss -- the defensive `SchemaViolation` branch in `update_review_payload` for a corrupted non-dict payload, lines 384-385, an untested but harmless defensive branch), 98%+ branch (62 branches, 1 partial). Both comfortably clear the 90%/85% gate.
- `uv run lint-imports` -> 13 contracts kept, 0 broken (`ops-areas-acyclic` kept with the new ignore_imports line in effect).
- `uv run mypy herness/store/ops/shared.py herness/store/ops/_review_common.py herness/store/ops/__init__.py` -> Success: no issues found in 3 source files (the `# type: ignore[assignment]` on `blocking_statuses`'s default is live/necessary -- mypy strict's `warn_unused_ignores` would otherwise flag it, and it did not).
- `uv run python -m tools.check_module_size` -> exit 0, no output (all modules within budget; `shared.py` at 390/390).
- `tests/unit/store/ops/test_store_ops_package.py -k UT02_68` -> 8 passed.
- Bonus (not on the required list but cheap): `uv run ruff check` and `ruff format --check` on the changed files -> all clean.
- UT02-72's concurrent race test re-run 3x in isolation plus once in the full suite run -- passed every time, no flakiness observed.

## Findings

### Critical
None.

### Important
None.

### Minor
- `herness/store/ops/_review_common.py:113-116` (`COUNT_GROUPED`) appends `ORDER BY k` to the SQL text the brief quotes verbatim for U02-131. This is required to satisfy the unit's own "keys sorted" return contract and keeps values bound as data/parameters (no injection concern), so it's a defensible, intentional deviation from the literal quoted SQL rather than a defect -- flagging only so it's a recorded, deliberate choice rather than an unnoticed drift from the spec text.
- `herness/store/ops/shared.py:384-385` -- the defensive `SchemaViolation` branch in `update_review_payload` (row's stored `payload` not a dict) is uncovered by tests. Total coverage is still 99%/98%+, well above the 90%/85% gate, so this is cosmetic only.

## Verdict

Approved
