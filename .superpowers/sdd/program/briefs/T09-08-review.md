# T09-08 review — Report data plan and banners (head 8d68d5e, base c3eee74)

### Spec Compliance
- ❌ Issues found (1 Important, plan-mandated / cross-spec; see I-1). Per unit:
  - U09-16 derive_banners ✅ — table order, codes once, derived OR draft, exact texts (findings_only exact), `{profile}` substituted, synth/local give no off_network_profile, details = keys only on unconfirmed_weights (`_data.py:34-148`). Published as `ReportBanner` per ruling.
  - U09-15 load_report_data / ReportData ✅ with I-1 caveat:
    - Registration order ✅ header title → slot 2 (paragraph numbers, then finding qids; findings_only: claim numbers then finding query_ids) → slot 3 (text, cards incl. fund extras, then funding/org table) → 4 (text, custom blocks, score.portfolio by budget) → 5 → 6 → 7 → 8 → 9 → remaining draft.query_ids (`_data.py:333-371`). UT09-79 asserts the exact sequence.
    - SQL ✅ every warehouse statement is a parameterised module constant with `LIMIT $n`/`LIMIT 500`/`LIMIT 1`; no `core.` read; UT09-79 scans all `*_SQL` constants in both modules.
    - Redaction ✅ funding titles (table + bar_h labels) and dead-task `last_error` (first line, cut to 200, then `redact_text`; None → `[redacted]`) (`_data_slots.py:192-195,252-254`; `_data.py:286-287`).
    - Errors ✅ duckdb.Error → QueryError naming build_id in message and details (`_data_slots.py:123-129`); ops calls unwrapped so StoreBusy propagates; malformed portfolio_custom → ReportContractError `where=portfolio_custom[i].<key>` (`_data_slots.py:286-296`).
    - findings_only ✅ slot 2 = verified findings via `ui_list_run_findings(status="verified")`, heading "Verified findings" in `section_titles`.
    - Retrospective ✅ window `started_at − window[1] .. started_at − window[0]`, latest decision, outcomes, verdict counts, empty text exact; data_as_of = max parseable ISO in `source_watermarks`, NULL/empty/unparseable → None.
    - numbers_total / number_query_ids ✅ number segments with a formatted value + linked cells.
    - Complexity/limits ✅ sparkline ≤ 8 points per metric; C901/PLR0913 clean (PLR0913 noqa on the two fixed signatures).
  - fill_rationale ✅ per ruling (numbers `format_value(v,"plain")`, None em dash, else `str()`, unknown names kept).
  - Controller rulings (ReportBanner, unconfirmed_keys, outcomes_window_days=(60,120) via ReportsSection default, section_titles, `_data_slots.py` + §2 row, local temp warehouse) implemented as ruled ✅.
- ⚠️ Cannot verify from diff / deferred: behaviour on the real `tiny_build` warehouse (T11-17 absent; UT09-79 uses a hand-written DDL subset); StoreBusy propagation is by construction only (no test); IT09-01/07/08/27 belong to later cards.

### Gates (re-run by reviewer)
- Card tests: 33 passed; coverage `_data.py` 100 % line / 100 % branch, `_data_slots.py` 100 % / 100 %.
- `tests/unit/reports`: 248 passed, 1 skipped (symlink privilege, pre-existing).
- ruff check, ruff format --check, mypy (4 files), lint-imports (13 kept), check_type_ownership (0), check_module_size (0): all clean.
- Test IDs: every function named `test_ut09_24_*` / `test_ut09_79_*`, docstrings start with the ID, `pytestmark = unit` in both files.

### Strengths
- Collector order is asserted exactly (including `used_by` places), not just set membership.
- SQL-constant scanner test enforces LIMIT / no `core.` / no f-string placeholders across both modules.
- Redaction is tested with a real Redactor and a sentinel e-mail, plus the None-redaction fallback.
- Portfolio custom validation via TypeAdapter gives precise `where` paths, tested for top-level, union and nested row keys.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
- I-1 (plan-mandated / cross-spec conflict) `herness/reports/_data_slots.py:276-281,320-329` — `_Row` requires a per-row `query_ids: list[str]`, as impl 09 U09-15 slot 4 says. But the producer of `draft.portfolio_custom` is impl 06 U06-(plan context step 3): "`portfolio_fn(entry, persist=False, ...)` whose JSON is appended to `custom`", i.e. an impl 04 `PortfolioResult`, whose `PortfolioRow` has `candidate_id, selected, order_rank, expected_impact_usd, flags` and NO `query_ids`; `query_ids` sit on the result (docs/impl/04 line 1709). Every real custom `--budget` scenario will therefore raise `ReportContractError at portfolio_custom[0].rows[0].query_ids` and fail the render (IT09-08 / IT09-09 / design §5.2 "its numbers link to the result's input query_ids"). Fix: accept the entry-level `query_ids` (PortfolioResult shape) and make row `query_ids` optional, falling back to the entry's list for cell links and registration; add a UT09-79 case with a PortfolioResult-shaped entry. Needs a controller ruling / spec edit of the U09-15 slot 4 row since the implementation follows the impl 09 text literally.

#### Minor (Nice to Have)
- M-1 `herness/reports/_data_slots.py:321,328-329` — only `selected` custom rows are registered; ids of unselected rows (e.g. `_q(0xD1)` in the test fixture) never enter the collector, while U09-15 says "every id in its `query_ids` is registered". The test comment at `tests/unit/reports/test_data_plan.py:285` ("custom block rows (all ids)") is therefore inaccurate. Largely moot once I-1 moves ids to entry level; otherwise register all rows' ids.
- M-2 `tests/unit/reports/test_data_plan.py:293` — `numbers_total == len(number_query_ids)` is tautological (both come from `ctx.number_qids`); no test asserts the actual count (e.g. that em-dash / n/a cells and uncited spans are excluded).
- M-3 `herness/reports/_data.py` (22 `# fmt: skip`, plus `# fmt: off` blocks) and `_data_slots.py` (10) — formatter is suppressed throughout to fit the 380-line budget (379 / 375 lines); combined with functional NamedTuple/TypedDict `noqa: UP013/UP014`, this trades readability for the budget. Acceptable under the rules, but the real size is well above budget; flag for the whole-branch review.
- M-4 `herness/reports/_data.py:286` — first line of `last_error` uses `split("\n")`; a `\r\n` error keeps a trailing `\r`. `splitlines()[0]` (guarding empty) is safer.
- M-5 `docs/impl/09-outputs-and-cli.impl.md` §2 `_data_slots.py` row omits other non-underscore names the module exposes and `_data` uses (`plain`, `number`, `json_object`, `redacted`, `FUNDING_COLUMNS`, `SCORECARD_COLUMNS`); the `_data.py` row still says `Banner` (known, ruled; spec edit pending).
- M-6 History: 97f84ba "wip" holds all work and 8d68d5e is an empty card-subject commit; squash at integration.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation matches the U09-15/U09-16 text closely and all gates are green with 100 % coverage, but the portfolio_custom schema it enforces (per-row `query_ids`, literally per impl 09) contradicts the `PortfolioResult` JSON impl 06 actually stores, so every real custom-budget report would fail; needs a ruling and a small tolerant-validation fix.

## Re-review 1 (fix round 1, 8d68d5e..a0e372f)

### Findings verified
- I-1 ✅ Fixed per ruling. `_Custom.query_ids` and `_Row.query_ids` are `NotRequired`. A row without ids falls back to the entry list (`row.get("query_ids") or entry`). With no entry ids and a row lacking ids, or with no ids and no rows, the code raises `ReportContractError` at `portfolio_custom[i].query_ids` (`_data_slots.py:293-309`). The new test `test_ut09_79_portfolio_result_shaped_custom` uses a full PortfolioResult-shaped dict: cells link to the entry's first id, and the extra keys are ignored. Two parametrised cases cover the new `where`.
- M-1 ✅ After the block's cells, every entry id and every row's ids (unselected rows included) are registered at `portfolio: <scenario>`. The collector-order test now expects `QH, QI, q_…d1, QJ` and its comment is correct.
- M-2 ✅ The test now asserts `numbers_total == 37` and `len(number_query_ids) == 37`. I checked the per-slot breakdown in the comment against the fixture and it adds up. The em-dash cells and the uncited span are excluded.
- M-4 ✅ `next(iter(....splitlines()), "")` is empty-safe. The fixture now uses CRLF and the test asserts there is no trailing `\r`.
- M-5 ✅ The §2 rows now name `ReportBanner` (with the OWN040 reason) and list the `_data_slots` public helpers. M-3 and M-6 are parked by ruling.

### Builder concern: custom-row sort
Custom rows are sorted by `order_rank` only (None last); `sorted` is stable, so ties keep input order. This is acceptable against the spec:
- U09-15 slot 4 asks only for selected rows "by `order_rank`".
- Impl 04 assigns one `order_rank` per selected candidate (`order_selected`), so selected rows do not tie.
- Unselected rows (None) are not displayed.
- PortfolioResult rows are already sorted by `candidate_id`, so input order reproduces the old tie-breaker on real data. No finding.

### Regression check
- The ruff/format/mypy/module-size checks show nothing new. Only the display path changed, and the SQL constants and redaction paths are untouched.
- Minor (new, informational): to stay at the 380/380 budget, the docstrings of `plain` and `top_teams` were removed or turned into a comment (`_data_slots.py:165,336`). This is more budget pressure, to be tracked with M-3. No action needed for this card.

### Gates (re-run by reviewer at a0e372f)
- Card tests: 36 passed. Coverage: `_data.py` 100 % line and branch, `_data_slots.py` 100 % line and branch.
- `tests/unit/reports`: 251 passed, 1 skipped (symlink privilege).
- ruff check, ruff format --check and mypy are clean. check_module_size = 0 and check_type_ownership = 0. Both files are at exactly 380 lines.

### Assessment
**Task quality:** Approved
**Reasoning:** Every in-scope finding is fixed as ruled, with tests that check real behaviour. The diff introduces no regression.
