# T06-07 report: Finding rules (build agent)

Status: DONE_WITH_CONCERNS (minor spec interpretations below; no blockers)
Commit: 5b6e60a feat(harness): finding rules and entity catalog (T06-07)
Worktree: D:\herness\.claude\worktrees\agent-abd7a537665317d61

## Built
- `herness/harness/findings.py` (186 lines, budget 260, hard 400):
  - `extract_markers(text)` -> (valid ids in order, malformed inner strings), straight from
    `herness.core.numbers.parse_markers` (no regex defined here).
  - `validate_markers(text, numbers, *, require_all_used=True)`: errors in spec order
    malformed -> duplicate number id -> marker without number -> unused number; exact
    messages from U06-47. Each id reported once (first occurrence order).
  - `normalize_objective`, `compute_dedup_key` (canonical_json of the 7-element array,
    sha1(usedforsecurity=False)[:16]).
  - `impact_usd(f, /)` -> max Decimal(str(value)) over usd numbers, Decimal("0") otherwise.
  - `EntityCatalog(warehouse)`: allowlist map (service, team, org, work_item, cluster,
    candidate) -> `SELECT <id>, <name> FROM <table> WHERE <id> IN (SELECT unnest(?))` on
    `warehouse.cursor()`; `run` via `herness.store.ops.runs.get_run` per uncached id.
    Cache `(entity_type, id) -> name | None` per instance; at most one query per call (only
    uncached ids), none when all cached or ids empty. Unknown entity type -> ValueError
    before any query.
- `tests/unit/harness/test_harness_findings.py` (27 test cases): UT06-05, UT06-31, UT06-33,
  UT06-34, PT06-01, PT06-02.

## Spec notes / interpretations
1. The query selects `<id>, <name>` (spec text shows `SELECT <id>`) so `missing` and
   `names` share one cache and one query; identifiers still only from the map.
2. A found entity whose name column is NULL maps to its own id in `names` (spec silent).
3. Malformed marker "strings" are the inner text of the token (MalformedMarker.text), so the
   message is `malformed marker [[x]]` as specified.
4. normalize_objective strips trailing `.!?;:` and spaces together (`rstrip(".!?;: ")`) after
   collapsing whitespace, so "done ." == "done." (needed for PT06-01 whitespace invariance).
5. The <= 50 ids per scope limit is not enforced in the catalog (callers check it: broker
   step 6, EntityScope max_length=50).
6. No metric counter called for; no `# T08-05` markers needed.

## Evidence
- RED: `pytest tests/unit/harness/test_harness_findings.py` -> ImportError: cannot import
  name 'findings' from 'herness.harness'.
- GREEN: 27 passed; coverage herness/harness/findings.py 100% line, 100% branch (82 stmts,
  12 branches).
- Full: `pytest -m "(unit or integration) and not slow" -q -p no:logging` -> 3898 passed,
  5 skipped, 17 deselected, 1 xfailed (pre-existing IT00-02 xfail).
- ruff format/check clean, mypy 0 issues (145 files), lint-imports 13 kept 0 broken,
  check_type_ownership 0, check_module_size 0. Pre-commit hooks all passed (no --no-verify).
- No import-linter contract change needed (herness.harness package already listed).

## Carry-overs
- UT06-34 uses a local DuckDB build in tmp_path (core.service, core.team, core.org,
  core.work_item, enrich.cluster, score.funding) opened with the real read-only
  `open_warehouse`, wrapped in a counting WarehouseHandle; switch to spec 11 `tiny_build`
  (T11-17) when it lands.
- `run` entity test uses the real `ops_store` fixture + `insert_run`; call-count check for
  run lookups uses a monkeypatched `findings.get_run`.

## Concerns
- Items 1, 2 and 4 above are small readings of open points; reviewer may want them in the
  spec text.

## Fix round 1 (review I-1)
Commit: 7f4a100 test(harness): generate bijection cases in PT06-02 (T06-07). Only the test file changed.
- PT06-02 now draws `case` from `st.one_of(_bijection_case(), _random_case())`. The bijection
  branch draws 1-6 unique ids and references each one 1-3 times, mixed with plain text in a
  random order. It then applies one mutation or none: none (weight 2/6), malformed marker,
  duplicate number id, dropped number, or unknown marker `[[n99]]`. The random branch is the
  original segment generator. The iff assertion is unchanged, and `event()` labels each case.
- With `--hypothesis-show-statistics` under the commit profile: 25.8% non-empty bijections,
  51.2% other. These don't add up to 100%; I did not look into why (possibly phases counted
  separately).
- New `test_pt06_02_strategy_generates_non_empty_bijections`: uses `hypothesis.find` to show
  the strategy yields (a) a bijection with at least 2 ids, which validates with no errors, and
  (b) a mutated near-miss with markers, which gets errors.
- PT06-02 tests ran in 0.63 s, well within the deadline. Gates: ruff and mypy clean,
  lint-imports 13 kept, check_module_size 0. Full run: 3901 passed, 5 skipped, 1 xfailed.
  Pre-commit hooks passed.
- Minor findings M-1..M-3 left as they are (parked).
