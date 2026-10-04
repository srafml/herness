# T09-01 report: App config section and ReportManifest

Status: DONE
Commit: b4cf985 "feat(reports): app config section and ReportManifest (T09-01)"

## Context

Resumed from a previous build agent's uncommitted work in the worktree. Reviewed the
partial diff line by line against the brief and `docs/impl/09-outputs-and-cli.impl.md`;
it was substantively sound, so I continued from it rather than redoing it, fixing the
issues found below.

## Files changed

- `herness/core/types/reports.py` (new) — `ReportManifest` (U09-01)
- `herness/core/types/__init__.py` (modified) — import + `__all__` entry for `ReportManifest`
- `herness/reports/settings.py` (new) — `AppConfig`, `AppSection`, `CacheTtl`, `ChatSection`,
  `ReportsSection`, `CliSection` (U09-02)
- `herness/reports/__init__.py` (new) — package doc + `ReportManifest` re-export
- `config/app.yaml` (new) — shipped defaults matching design §7
- `tests/unit/core/types/test_reports.py` (new)
- `tests/unit/reports/test_reports_settings.py` (new)
- `pyproject.toml` (modified) — `herness.reports` added to the layers contract and to
  `herness.eval | herness.reports` co-layer; `herness.reports` added to the
  `herness never imports app or tools` forbidden-modules list; `herness.reports.settings`
  added to the "settings modules are leaves" contract's `source_modules` (UT00-58)

## Test IDs covered

- UT09-01 — `ReportManifest`: valid round trip, extra key, naive datetime, bad/short hash,
  unknown `files` key, `numbers_linked > numbers_total`, bad `run_id`, malformed `uncited`
  entries (missing/extra keys), warning length boundary (500 vs 501 chars)
- UT09-02 — `AppConfig`/sections: all-defaults load from `{"version": 1}`, shipped
  `config/app.yaml` equals the all-defaults model, `compiled_numeral_patterns` cached and
  matches file order, `version != 1` rejected, frozen/`extra=forbid` enforced, every
  range bound in design §7's table (parametrized), plus the acceptance check that
  `herness.reports.settings` imports only stdlib/typing/pydantic (no duckdb/jinja2/streamlit)
- UT09-03 — bad pattern `"("` errors at `reports.allowed_numeral_patterns.0`; empty list,
  >50 entries, and >200-char entry all rejected
- UT09-04 — duplicate `formats` and out-of-order window `[120, 60]` both rejected as
  separate errors in one call; format outside `{html, md, pdf}` rejected; window order/
  bound edge cases (`[60,60]`, `[-1,60]`, `[0,3651]`, `[0,3650]` accepted)

`pytest -k "UT09_01 or UT09_02 or UT09_03 or UT09_04"`: 38 passed.

## Gate results

- `uv run ruff check .` — All checks passed
- `uv run ruff format --check .` — 117 files already formatted
- `uv run mypy` — Success: no issues found in 52 source files
- `uv run lint-imports` — Contracts: 10 kept, 0 broken
- `uv run python -m tools.check_type_ownership` — exit 0 (INFO pending owner 06/07, unrelated)
- `uv run python -m tools.check_module_size` — exit 0, all files within budget
- `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` —
  1050 passed, 5 deselected, 1 xfailed (pre-existing xfail for spec doc defects, unrelated
  to this card)

## Fixes made to the partial work

1. `herness/core/types/reports.py` was 47 lines against the module-map budget of 40
   (`MS001` from `check_module_size`). Folded the separate `_uncited_ok` `AfterValidator`
   into the existing `_check_invariants` `model_validator(mode="after")` (removed the
   now-unused `AfterValidator` import) to bring the file to exactly 40 lines while keeping
   the same validation behaviour and error set.
2. That same validator's `raise ValueError("...")` calls tripped ruff `EM101`; changed
   to assign the message to a local `msg` variable first, per repo convention (this is
   accounted for in the line count above).
3. `tests/unit/reports/test_reports_settings.py::test_settings_module_has_no_heavy_imports`
   had no `UTxx-xx`/`ITxx-xx` test-ID token in its name, which
   `tests/integration/support/test_test_ids_repo.py::test_it11_31_repo_test_ids_collected`
   enforces repo-wide. Renamed to `test_ut09_02_settings_module_has_no_heavy_imports` (the
   brief's acceptance check ties this assertion to UT09-02) and shortened the docstring to
   fit the 100-char line limit.

No other correctness issues found in the partial diff: `ReportManifest`'s `run_id` pattern,
hash pattern, file-key literal type, and the `numbers_linked <= numbers_total` invariant all
matched U09-01 verbatim; `AppConfig`'s field defaults, ranges, and the format/window
validators all matched U09-02's table verbatim, and `config/app.yaml` matches those defaults
(asserted by test).

## Deviations

- `ReportManifest.run_id`'s pattern is a private module constant `_RUN_ID_RE` (matching the
  exact regex `^run_[0-9A-HJKMNP-TV-Z]{26}$` given for `RUN_ID_RE` at U09-03), not an import
  of a shared `RUN_ID_RE` constant. The real `RUN_ID_RE` is owned by
  `herness/reports/contract.py` (U09-03), which does not exist yet (later card, T09-05/06).
  Importing from a not-yet-existing module isn't possible in this card; once contract.py
  lands, a follow-up could re-point this at the shared constant, but the two must stay in
  sync by value in the meantime (they're identical today).

## Carry-overs

- Per program ruling: the `HernessConfig` field mounting `cfg.app` and the
  `herness config validate --offline` acceptance check are DEFERRED to T10-03. No
  `herness.core.config`/impl 10 code was touched in this card.
