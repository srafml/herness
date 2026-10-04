# Report for T03-37: Review-item helpers

Worktree: D:\herness\.claude\worktrees\agent-a2fa711044f49450f
Base SHA: cb9aa95

## What was built

- `herness/enrich/review_items.py` (146 lines, budget 160): `iter_review_items`,
  `create_if_absent`, `open_label_counts` (U03-147 ... U03-149) - thin wrappers over
  impl 02's `herness.store.ops.list_review_items` and `create_review_item_if_absent`.
  No SQL and no `review_item` table access outside `herness.store.ops`.
  - `iter_review_items`: pages `list_review_items` at `page_size` (default 500),
    validates `1 <= page_size <= 5000` itself (ConfigError otherwise), stops when a page
    is short.
  - `create_if_absent`: validates its own preconditions before any write - non-empty
    `match_keys`, non-empty `blocking_statuses`, at most 8 distinct keys across
    `match_keys` and `scope`, every payload carrying every `match_keys` field, and every
    payload agreeing with `scope` on each scope key (all ConfigError, no payload value
    ever put in a message - TH03-03). Suppresses same-call duplicates by the
    `match_keys`-only tuple (Python equality on JSON scalars incl. `None`) before ever
    calling the store; otherwise delegates to `create_review_item_if_absent` with
    `match_keys=match_keys + tuple(scope)`. Left the required
    `# T08-05: herness_enrich_review_items_total{kind, purpose} += 1 per created item`
    marker at the call site (no metric sink exists yet, per the controller ruling).
  - `open_label_counts`: iterates pending `label_check` items per purpose via
    `iter_review_items`, counts by `payload["question"]`.
- `tests/unit/enrich/test_review_items.py` (239 lines): pytestmark `unit`.

## Tests

IDs and functions (every function name/docstring carries its ID):
- UT03-136: `test_ut03_136_iter_review_items_pages_and_orders` (1,203 pending +
  5 approved `label_check` items, distinct `created_at`; asserts exact item order and
  exactly 3 calls to `list_review_items` via a `monkeypatch.setattr` spy on the name
  `review_items` looks up), `test_ut03_136_page_size_precondition` (0 and 5001 raise
  ConfigError), `test_ut03_136_no_review_item_sql_in_enrich` (greps every `.py` under
  `herness/enrich` for `FROM|INTO|UPDATE review_item`; asserts none - the acceptance
  check for "no SQL against review_item under herness/enrich/").
- UT03-137: `test_ut03_137_create_if_absent_idempotent` (the brief's exact K1/K2/K3/K4
  scenario: qsv-A pending K1, qsv-A rejected K2, qsv-B pending K3; payloads K1,K2,K3,K4,K4
  with scope qsv=A; first run 2 created/3 suppressed - K3 because the existing K3 has a
  different scope, second K4 suppressed in-call; second run 0 created/5 suppressed;
  verifies created items are `pending`), plus `test_ut03_137_empty_match_keys`,
  `test_ut03_137_empty_blocking_statuses`, `test_ut03_137_too_many_keys`,
  `test_ut03_137_missing_match_key_field` (also asserts the payload value never appears
  in the error message), `test_ut03_137_scope_mismatch`, `test_ut03_137_none_match_value`
  (confirms impl 02's `create_review_item_if_absent` correctly treats a `None` match
  value via SQLite's null-safe IS/IS NOT, so no store-side gap was found - see
  Concerns).
- UT03-138: `test_ut03_138_open_label_counts` (the brief's exact 3x spot_check q1 /
  1x gold q1 / 2x spot_check q2 scenario -> `{"q1": 3}`), plus
  `test_ut03_138_open_label_counts_absent_default`.

Result: `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_review_items.py -q -p no:logging`
-> 12 passed. Coverage of `review_items.py`: 100% line, 100% branch (target >=90%/>=85%).

`PYTHONUTF8=1 uv run pytest tests/unit/enrich -q -p no:logging` -> 331 passed, 1 skipped
(pre-existing, unrelated symlink-privilege skip).

`PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`
(run once before the final commit, per implementer rules) -> 4735 passed, 7 skipped
(platform symlink privilege / missing coverage.json, all pre-existing), 21 deselected,
2 xfailed (both pre-existing, named in DECISIONS/TODO markers, unrelated to this card),
in 322.19s. Exit code 0.

## Gates

- `uv run ruff format .` / `uv run ruff check --fix .`: clean (ruff auto-fixed one
  formatting nit in the test file on the first pre-commit run; re-staged and committed).
- `uv run mypy herness/enrich/review_items.py tests/unit/enrich/test_review_items.py`:
  0 errors. (One --strict finding was fixed along the way: accessing
  `review_items.list_review_items` as a plain attribute tripped
  --no-implicit-reexport, since `review_items.py` only imports that name, it does not
  re-export it. Fixed by importing `list_review_items` directly into the test module for
  the "real" reference used inside the spy, and using
  `monkeypatch.setattr(review_items, "list_review_items", spy)` (string-keyed, not an
  attribute-access) to install it - the same pattern already used elsewhere in the tree
  for patching plain-imported names such as `shared.audit`.)
- `uv run lint-imports`: 13 contracts kept, 0 broken (no new contract needed; `enrich`
  importing `store.ops` is already allowed by the layering contract).
- `uv run python -m tools.check_module_size`: exit 0 (`review_items.py` 146/160).
- `uv run python -m tools.check_type_ownership`: exit 0 (no new `herness.core.types`
  submodule added).
- pre-commit hooks (ruff-check, ruff-format, mypy, import-linter, detect-secrets,
  module-size, type-ownership, pytest-unit): all green on the checkpoint commit
  (SHA 1b37f33).
- No `.secrets.baseline` change needed - no secret-looking fixtures were added.

## Deviations from the brief

- The brief's Interfaces section lists importing `ReviewKind` from `herness.store.ops`,
  but U03-147/U03-148's own signatures restrict `kind` to
  `Literal["label_check", "mapping_suggestion"]`, a strict subset of the 4-value
  `ReviewKind`. Defined a private `type _Kind = Literal["label_check",
  "mapping_suggestion"]` instead of importing/reusing the wider `ReviewKind` for that
  parameter, and did not import `ReviewKind` (it would be unused - ruff F401). Reused
  the imported `ReviewStatus` type as-is for `status`/`blocking_statuses`, since its
  three-value literal already matches the unit specs' `status` type exactly.

## Carry-overs

- None identified for this card.

## Concerns

- The controller ruling asked to check whether impl 02's `create_review_item_if_absent`
  correctly handles a `None` value in the match tuple (U03-148's invariant explicitly
  allows `None` for an absent subject field), and to report a concern rather than edit
  store code if it does not. I traced impl 02's `MATCH_LOOKUP` SQL
  (`herness/store/ops/_review_common.py`) and its use of SQLite's IS/IS NOT
  operators, which are null-safe by construction (unlike =/!=): when a match value is
  JSON null, `json_each` yields SQL NULL for it, and `x IS NOT NULL_value` correctly
  distinguishes "field also null/absent" (no exclusion) from "field has a real value"
  (excluded), regardless of whether the NULL on the right comes from a literal or from
  a bound column. Added `test_ut03_137_none_match_value` to exercise this directly against
  the real store (create once with a None match field, rerun and confirm suppression).
  It passes, so no store-side gap was found; flagging only because the ruling asked
  for a report either way.
- `open_label_counts` reads `item.payload["question"]` directly (a KeyError would
  propagate as an uncaught exception, not ConfigError) since U03-149 gives no
  precondition for malformed payloads and `create_if_absent`/`decide_review_item` are the
  only writers of `label_check` payloads in the design; documented as "raises as
  `iter_review_items`" in the docstring, matching the unit spec's Errors row exactly.

Final commit SHA: (appended below after commit)

Final commit: 0ebebd7 feat(enrich): review-item paging, idempotent creation and open counts (T03-37)
Checkpoint commit: 1b37f33 wip(T03-37): add review_items module and unit tests, green
