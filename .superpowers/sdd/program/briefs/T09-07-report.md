# T09-07 report: Evidence collection and markup

Status: DONE_WITH_CONCERNS (minor; see Concerns)
Worktree: D:\herness\.claude\worktrees\agent-a7b0028af6f4af0af (branch worktree-agent-a7b0028af6f4af0af, base 7941c06)
Commits: 0487de6 feat(reports): add evidence collection and markup (T09-07) (final); b12e557 wip(T09-07): evidence collector and loader; 9cc4d15 wip(T09-07): escaping-first markup for HTML and Markdown; final: feat(reports): add evidence collection and markup (T09-07) (empty marker commit; code landed in the wip checkpoints)

## Implemented
- herness/reports/_evidence.py (218/220): EvidenceEntry (frozen), EvidenceCollector (use/ordered_ids/used_by; QUERY_ID_RE check -> ReportContractError("invalid query_id"); used_by capped at 50 then "… and N more"), load_evidence_entries(collector, wh_con, *, build_id, sample_rows).
  - Step 1 ops rows via ops.ui_get_evidence_rows (chunks of 500 inside U09-52).
  - Controller ruling 1: each ops row is tamper-checked (herness.core.ids.query_id(sql, params, build_id); non-dict params or SchemaViolation count as mismatch); mismatch logs WARNING "reports.evidence.tampered" with query_id only and the row is dropped, so the id falls through to meta.evidence (found=False if absent there).
  - Step 2 meta.evidence with list_contains($ids, query_id), ≤500 ids per statement; information_schema.columns check once; v1 builds select NULL AS result_sample (two constant SQL strings, no f-string SQL).
  - Steps 3-6 as spec: ops precedence and ops build_id, else the function's build_id; sample parsed (JSON text or already-parsed list), first sample_rows dict rows, cells str() (None -> ""), cut to 200; sample_columns = first row keys; NULL/missing/unparsable/non-list -> None; params dict or {"_raw": text[:500]}; executed_at datetime -> fixed-width UTC text (naive treated as UTC), str otherwise.
  - DuckDB errors -> QueryError("warehouse evidence lookup failed for build <id>", details={"build_id"}); StoreBusy propagates untouched. Empty collector short-circuits (no store access).
- herness/reports/_markup.py (145/200): Segment, segment_text, segments_to_html, segments_to_md, md_escape, strip_images, exactly per U09-17/U09-18. Markers from herness.core.numbers.parse_markers, values from format_number; n/a -> query_id None; title "<column> · k=v, ..." or "single row". HTML built from constant Markup templates with Markup.format (all arguments escaped), text newlines -> <br>. Uncited/number cuts sorted; a cut overlapping an earlier one (or out of range) is ignored defensively. Uncited segment text is text[start:end] of the field (not the 80-char-cut hit text) so the concatenation postcondition holds.

## Tests (tests/unit/reports/test_evidence.py, test_markup.py; pytestmark unit)
UT09-22 (3 fns), UT09-23 (14 fns incl. tamper fall-through + log, chunking spy 500/500/201, v1 build, QueryError, StoreBusy), UT09-25 (5), UT09-26 (3), UT09-27 (3), PT09-03 (hypothesis 200 examples: html.parser sees only a/span/mark/br, a tags are class=num href=#ev-<qid>; also Markdown has no <, > or non-anchor link).
RED: `PYTHONUTF8=1 uv run pytest tests/unit/reports/test_evidence.py -q` -> ImportError: cannot import name '_evidence' from 'herness.reports'; same for test_markup.py (collection error) before each module existed.
GREEN: card tests 37 passed; coverage _evidence 100% line/100% branch, _markup 100%/100%. `PYTHONUTF8=1 uv run pytest tests/unit/reports -q` -> 215 passed, 1 skipped (pre-existing symlink skip).
Gates: ruff format --check (396 files formatted), ruff check (clean), mypy (169 files, no issues), lint-imports (13 kept), tools.check_module_size exit 0, tools.check_type_ownership exit 0; pre-commit hooks (incl. pytest-unit) passed on both commits. No full-suite run per dispatch.

## Line counts vs budgets
_evidence.py 218 / 220; _markup.py 145 / 200.

## Deviations / concerns
- Manifest warning "evidence not found: <id>": spec assigns emitting it to render_run (U09-24 algorithm, "warnings: evidence not found ids"); this unit only returns found=False entries with sql="". Left to the renderer T09-08.
- Ops invalid JSON: ui_get_evidence_rows raises SchemaViolation on invalid JSON in ops columns (core.load_json); that propagates. The "_raw" fallback applies to meta.evidence text (and anything not a dict).
- meta.evidence rows are not tamper-checked (spec U09-14 does not ask for it; the ruling covers ops rows only).
- EvidenceCollector backs the spec's dict[str, list[str]] with dict[str, dict[str, None]] (ordered set) to keep use() O(1) with dedupe.
- ReportContractError from use()/segment_text carries only the message (spec names no details code).
- Final commit is an empty marker commit (--allow-empty) because the code was already committed in wip checkpoints per the session-kill process.
