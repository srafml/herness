# Review: T09-01 App config section and ReportManifest

## Spec Compliance

- ✅ U09-01 `ReportManifest` (herness/core/types/reports.py): all 11 fields present with exact types/defaults/constraints from the brief's table — `run_id` pattern `_RUN_ID_RE = r"^run_[0-9A-HJKMNP-TV-Z]{26}$"` matches `RUN_ID_RE` verbatim (docs/impl/09-outputs-and-cli.impl.md:184); `kind` Literal; `files` dict keyed by `Literal["report.html","report.md","report.pdf"]` with `Field(pattern=_HASH_RE)` values (`^[0-9a-f]{64}$`) enforces both the key-subset and hash-format constraints; `numbers_total`/`numbers_linked`/`evidence_entries` `ge=0`; model_validator enforces `numbers_linked <= numbers_total` and `uncited` dicts have exactly `{where, text}`; `warnings` items `max_length=500`; `model_config = ConfigDict(extra="forbid", frozen=True)`. `rendered_at: AwareDatetime` matches the unit's Algorithm ("rejects naive datetimes") — the table's "timezone-aware UTC" constraint is a postcondition of the writer (U09-24), not this unit; no over- or under-validation here.
- ✅ U09-02 `AppConfig`/sections (herness/reports/settings.py): every key path, type, default and range bound in the brief's table matches exactly — `CacheTtl.{warehouse,ops}`, `AppSection.{current_recheck_s,page_row_limit}`, `ChatSection.{max_question_chars,poll_queued_s}`, `ReportsSection.{formats,top_n,strict_numbers,evidence_sample_rows,allowed_numeral_patterns,prior_outcomes_window_days}`, `CliSection.poll_interval_s`. `formats` validator rejects duplicates; `allowed_numeral_patterns` compiles each entry via `AfterValidator(_compiles)` at path `reports.allowed_numeral_patterns.<i>`, bounded `min_length=1,max_length=50`, each `max_length=200`; `prior_outcomes_window_days` validator enforces `0 <= first < second <= 3650`; `compiled_numeral_patterns` is a `cached_property` on a frozen model (bypasses `__setattr__`, computed once — confirmed by identity-check test and by test run below). `AppConfig.version: Literal[1] = 1`.
- ✅ `config/app.yaml`: byte-for-byte match to `docs/specs/09-outputs-and-cli.md:328-345` (design §7), plus `version: 1` added per the impl spec's U09-02 table row (impl spec is binding over the design doc, which omits `version`) — not a deviation.
- ✅ `herness/reports/__init__.py`: re-exports `ReportManifest` from `herness.core.types` as required; `render_run` correctly deferred to a later card, noted in the module docstring.
- ✅ `herness/core/types/__init__.py`: `ReportManifest` imported and added to `__all__` in correct sorted position.
- ✅ pyproject.toml: `herness.reports` added to the `herness.eval | herness.reports` layer, to the "herness never imports app or tools" forbidden list (herness core base closed), and `herness.reports.settings` added to "settings modules are leaves" source_modules — all required by UT00-58 for a new package/module.
- ✅ Test IDs: UT09-01 (9 test functions covering valid round-trip, extra key, naive datetime, bad/short hash, unknown files key, linked>total, bad run_id, uncited key validation, warning length boundary), UT09-02 (defaults, shipped yaml equality, cached compiled patterns, version!=1, frozen/closed, parametrized range bounds, no-heavy-imports acceptance check), UT09-03 (indexed error path, count/length bounds), UT09-04 (duplicate formats + bad window in one call, formats subset, window order boundary) — all present, correctly named/tagged (`test_ut09_0X_...`, `pytestmark = pytest.mark.unit`).
- ✅ Program rulings respected: no `herness.core.config`/impl 10 code touched; `HernessConfig` mounting and `herness config validate --offline` correctly deferred to T10-03; settings module imports only stdlib + pydantic (confirmed by both the ast-based test and manual read); private `_RUN_ID_RE` copy in core/types/reports.py is the structurally-required layering workaround (core/types cannot import herness.reports), value confirmed identical to the spec's `RUN_ID_RE`.
- ⚠️ None — nothing in this card's scope is unverifiable from the diff.

## Gate results (re-run in the worktree)

- `PYTHONUTF8=1 uv run pytest -q -p no:logging tests/unit/reports tests/unit/core` — 461 passed
- `uv run ruff check .` — All checks passed
- `uv run mypy` — Success: no issues found in 52 source files
- `uv run lint-imports` — Contracts: 10 kept, 0 broken
- `uv run python -m tools.check_type_ownership` — exit 0 (INFO pending owner 06/07, pre-existing/unrelated)
- `uv run python -m tools.check_module_size` — exit 0

All match the build report's claims.

## Findings

None — Critical / Important / Minor: no findings.

## Verdict

**Approved**
