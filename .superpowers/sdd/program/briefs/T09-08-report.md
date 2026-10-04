# T09-08 report — Report data plan and banners

Status: DONE_WITH_CONCERNS (draft written before the final commit; updated after)
Worktree: D:\herness\.claude\worktrees\agent-aa3531eeb3b8851f0 (branch worktree-agent-aa3531eeb3b8851f0, base c3eee74)

## What was built
- `herness/reports/_data.py` (379 lines, budget 380): `ReportBanner` (see deviation 1), `ReportData`
  (frozen dataclass), slot shapes `Header`, `Card`, `LeverRow`, `RetroItem`, `Retrospective`, `Caveats`,
  `MethodInfo`, `RunAppendix` (functional NamedTuples, `# noqa: UP014` with reason, same precedent as
  ui_reads' functional TypedDicts), `derive_banners` (U09-16), `fill_rationale`, `load_report_data` (U09-15);
  re-exports `Cell`, `TextBlock`, `Table`, `PortfolioBlock`, `Scorecard` via `__all__`.
- `herness/reports/_data_slots.py` (375 lines, budget 380; the ONE allowed private sibling; module-map row
  added to docs/impl/09 §2 in the same commit): every warehouse SQL constant, `Cell`, `TextBlock`, `Table`,
  `PortfolioBlock`, `Scorecard`, `Ctx` (per-render state: connection, collector, back-link `place`,
  `number_qids`, `charts`; `rows()` maps duckdb.Error -> QueryError naming build_id), header/DQ loaders,
  funding table + fund card extras, org table + dot_z, portfolio blocks (custom first, validated with a
  TypeAdapter over functional TypedDicts; unknown keys ignored), scorecards + sparklines.
- Tests: `tests/unit/reports/test_data_banners.py` (UT09-24 + fill_rationale rows of UT09-79),
  `tests/unit/reports/test_data_plan.py` (UT09-79: local temp DuckDB warehouse + real `ops_store` +
  fixed-key test redactor).

## Per-unit notes
- U09-16: table order, each code once, derived OR draft; `off_network_profile` for profile not in
  {local, synth}; text `{profile}` substituted; details = unconfirmed keys only on `unconfirmed_weights`.
  Signature fixed by spec has 7 params -> `# noqa: PLR0913` with reason (also on load_report_data).
- U09-15 registration order: header (title segments) -> slot 2 (paragraph numbers then finding query ids
  via one `ui_finding_query_ids` call; findings_only: each verified finding's claim numbers then its
  query_ids) -> slot 3 (section text, cards: headline, summary, finding ids, fund extras; then funding
  table (funding_review) or org table (org_review)) -> slot 4 (section text, draft custom blocks with all
  row query ids, then score.portfolio by budget_usd) -> 5 (section text, scorecards) -> 6 (section text,
  levers) -> 7 (retrospective paragraphs + prior_outcomes_commentary, prior rec summaries, outcome cells)
  -> 8 (section text, draft.caveats blocks) -> 9 (section text) -> remaining draft.query_ids
  (where `query_ids[i]`). UT09-79 asserts the exact order.
- `used_by` places: section id for paragraphs, `rec-<rank>` for cards (so U09-28 back-links resolve),
  `funding_table`, `org_table`, `portfolio: <scenario>`, `scorecard: <type>=<id>`, `actions`,
  `retrospective: <rec_id>`, `risks_and_caveats`, `header`.
- numbers_total = number segments with a formatted value + cells with a query_id; a cell whose text is
  the em dash (None) or `n/a` is not linked (matches _format.format_value / segment rules).
- Every warehouse SQL is a module-level constant with `LIMIT $n` (top_n) or `LIMIT 500/1`; UT09-79 scans
  every `*_SQL` constant in both modules (starts with SELECT, has LIMIT, no `core.`, no `{`).
- Titles (funding title cells and the bar_h labels) and dead-task `last_error` (first line, cut to 200,
  then redact) pass `redact_text`; a failed redaction (None) shows `[redacted]`.

## Deviations / open choices (need controller ruling where marked)
1. RULING NEEDED — `Banner` name: impl 09 §2 names `Banner` as a `_data` public symbol, but impl 06 owns
   `Banner` (a Literal) in `herness.core.types`, and `tools.check_type_ownership` fails with
   `OWN040 Banner redefined outside core.types` for any definition or alias named `Banner`. The dataclass is
   therefore `ReportBanner` (fields code/text/details as specified). The §2 row of `_data.py` still says
   `Banner`; T09-09/10/11 must use `ReportBanner`. Suggest a spec edit of that row.
2. `unconfirmed_keys`: the U09-15 table lists "unconfirmed weight keys" but `_data` reads no config and
   the fixed signature has no weights input. Added kw-only `unconfirmed_keys: Sequence[str] = ()`
   (T09-11 passes `unconfirmed_weight_keys(weights)`), alongside the ruled `outcomes_window_days`.
3. `fill_rationale`: spec says each `template_params[name]` is formatted with `format_value(v, "plain")`,
   but impl 04 LEVER_PLACEHOLDERS include text values (entity_name, metric_label, unit, target_kind,
   peer_group, period) that format_value would turn into "n/a". Numbers use format_value(v, "plain"),
   None the em dash, other values `str()`; unknown names stay as written.
4. `ReportData.section_titles: dict[str, str]` added (draft section titles; "Verified findings" for
   executive_summary in findings_only mode) — the templates need the headings; spec lists no field for it.
5. Sub-types (Card, Table, PortfolioBlock, Scorecard, LeverRow, Retrospective, Caveats, MethodInfo,
   RunAppendix, Header) are unspecified beyond their names; shapes chosen as documented in the code.
   Charts live in `ReportData.charts` keyed `funding_priority`, `org_z`, `portfolio-<n>`, `spark-<i>-<j>`;
   blocks refer to them by key.
6. Slot 3 table by kind: funding table + bar_h for funding_review; org table + dot_z for org_review
   (cards filter on `fund` / `org_action` likewise). Other slots load for both kinds.
7. Portfolio custom errors: `where` = `portfolio_custom[i].<key>` (nested row keys as
   `portfolio_custom[i].rows[j].<key>`); scenario union branch names are dropped from the path.
8. Prior recommendation summaries (retro) are segmented with their stored numbers; if markers do not
   resolve (or numbers do not validate) the summary is shown as plain text instead of raising.
9. `run.started_at`: `get_run` returns `RunRow` whose `started_at` is already an aware datetime
   (`clock.parse_utc` in runs.py), so `_data` only calls `clock.ensure_utc` — no text parsing needed.

## Carry-overs
- Switch UT09-79's local warehouse fixture (and report_drafts builder) to `tests.support.builds.tiny_build`
  (T11-17) once it exists.
- T09-11 render_run: pass `unconfirmed_keys` and `outcomes_window_days`; "evidence not found" warnings and
  sample_rows (T09-07 carry-overs) remain there.
- Spec edit for deviation 1 (module map row) and, if accepted, 2–4.

## Gates / results
- Card tests: `pytest tests/unit/reports/test_data_plan.py tests/unit/reports/test_data_banners.py` ->
  33 passed; coverage `_data.py` 100% line / 100% branch, `_data_slots.py` 100% / 100%.
- `pytest tests/unit/reports -q` -> 248 passed, 1 skipped (symlink privilege).
- ruff format --check / ruff check: clean; mypy (touched files): clean; lint-imports: 13 kept;
  check_module_size: exit 0; check_type_ownership: exit 0.
- RED evidence: `pytest tests/unit/reports/test_data_banners.py` -> ModuleNotFoundError
  herness.reports._data before implementation.

## Final status (updated after commit)
Status: DONE_WITH_CONCERNS. Final commit 8d68d5e feat(reports): add report data plan and banners (T09-08).
- 97f84ba wip(T09-08): all code, tests, module-map row and a `.secrets.baseline` line-number refresh
  (the doc row shifted three allow-listed hashes by one line). Test ULIDs are derived from
  `report_drafts.ULID` so detect-secrets does not flag them.
- 8d68d5e: the card-subject commit, empty because the wip commit already held all the work.
- All pre-commit hooks passed, including pytest-unit, mypy, module-size and type-ownership.

## Fix round 1 (review D:\herness\.superpowers\sdd\program\briefs\T09-08-review.md)
- I-1 (controller ruling applied): `portfolio_custom` entries accept the impl 04 PortfolioResult shape
  (U04-77: `query_ids` on the result, rows without ids). Both the row-level and entry-level `query_ids`
  are optional (`NotRequired` in the TypedDicts). A row without ids uses the entry's list. An entry with
  no ids at either level (entry has none and some row has none, or there are no rows) raises
  ReportContractError at `portfolio_custom[i].query_ids`. Cells link to the row's first id, falling back
  to the entry's first. New UT09-79 case `test_ut09_79_portfolio_result_shaped_custom` uses a full
  PortfolioResult-shaped dict (selected, totals, binding_constraints, flags ignored). Two new malformed
  cases cover the `.query_ids` path.
- M-1: the entry-level ids and every row's ids (unselected rows included) are registered after the
  block's cells. The collector-order test now expects `QH, QI, q_…d1, QJ` and its comment is fixed.
- M-2: the collector test asserts `numbers_total == 37` and `len(number_query_ids) == 37`. The per-slot
  breakdown is in a comment. The em-dash cells and the uncited span are excluded.
- M-4: the first line of `last_error` now comes from `splitlines()` (empty-safe via `next(iter(...), "")`).
  The fixture error uses CRLF and the test asserts there is no trailing `\r`.
- M-5 (optional, done): the impl 09 §2 rows now name `ReportBanner` (with the OWN040 reason) for
  `_data.py`, and list `_data_slots.py`'s public helpers (`FUNDING_COLUMNS`, `SCORECARD_COLUMNS`,
  `plain`, `number`, `json_object`, `redacted`). Both edits stay inside the existing table rows, so no
  lines shifted.
- Custom rows are now sorted by `order_rank` (None last, stable for ties) inside `_custom`. The
  `_custom_order` helper was removed to stay within budget. No `# fmt: skip`/`off` markers were added.
- Line counts: `_data.py` 380, `_data_slots.py` 380 (budget 380 each).
- Results: card tests 36 passed; `tests/unit/reports` 251 passed, 1 skipped; coverage 100 % line and
  branch on both modules. ruff, format, mypy, lint-imports (13 kept), check_module_size 0 and
  check_type_ownership 0 are all clean.
