# T09-07 review: Evidence collection and markup

Reviewed: worktree agent-a7b0028af6f4af0af, base 7941c06, head 0487de6 (wip b12e557, 9cc4d15). Read-only.

### Spec Compliance
- ✅ Spec compliant
  - ✅ U09-13 EvidenceCollector / EvidenceEntry: frozen slotted dataclass with the 10 spec fields; `use` validates `QUERY_ID_RE` (imported from `herness.reports.contract`) -> `ReportContractError("invalid query_id")`; first-use order; `used_by` deduplicated, capped at 50 plus a single `"… and N more"` (_evidence.py:28, 69-85).
  - ✅ U09-14 load_evidence_entries: ops rows via `ops.ui_get_evidence_rows` (chunking inside U09-52); per-row TH05-12 tamper check with `herness.core.ids.query_id(sql, params, build_id)`, WARNING `reports.evidence.tampered` with `query_id` only, row dropped so the id falls through to meta.evidence (ruling 1; _evidence.py:88-102, 204); meta lookup with `list_contains($ids, query_id)`, ≤500 ids per statement, one `information_schema.columns` check per call, v1 builds select `NULL AS result_sample` with constant SQL (no f-string SQL) (32-43, 149-163); ops precedence with ops `build_id`, else the function's (166-186); sample: first `sample_rows` rows, `str()` cells, None -> "", cut 200, `sample_columns` = first-row keys, NULL/missing -> None (121-137); params dict or `{"_raw": text[:500]}` (105-118); `found=False` entries carry `sql=""` (215-217); DuckDB errors -> `QueryError` naming build_id in message and details, StoreBusy propagates (ruling 3); "evidence not found" warning left to render_run/T09-08 (ruling 2).
  - ✅ U09-17 segment_text: markers from `herness.core.numbers.parse_markers`, values from `format_number`, `n/a` -> `query_id=None`, title `"<column> · k=v, …"` / `single row`; `uncited: Sequence[UncitedHit]` typed with `herness.reports.contract.UncitedHit` (ruling 4); uncited segment text is `text[start:end]`, so the concatenation postcondition holds (_markup.py:60-104).
  - ✅ U09-18 segments_to_html / segments_to_md / md_escape / strip_images: HTML built only from constant `Markup` templates via `Markup.format` (all arguments, incl. the title attribute, escaped), `\n` -> `<br>`; MD: `strip_images` before `md_escape` on text segments, numbers `[esc](#ev-qid)`, n/a plain, uncited `_[uncited]_ ` + escaped; `md_escape` escapes `&` first, then `<`, `>`, then a backslash before exactly ``\ ` * _ { } [ ] ( ) # + ! | $ ~`` (_markup.py:34-145).
  - ✅ UT09-22 (3 functions), ✅ UT09-23 (ops only / meta only / both / none, v1 build, tamper fall-through + log, 500/500/201 chunking, QueryError, StoreBusy, sample/params edge cases), ✅ UT09-25, ✅ UT09-26, ✅ UT09-27, ✅ PT09-03 (hypothesis, 200 examples; html.parser tag set ⊆ {a, span, mark, br}, a.num href check; MD has no `<`, `>` or non-anchor link).
  - ✅ Test naming: every function carries its ID in the name and as the docstring's first token; module-level `pytestmark = pytest.mark.unit` in both files.
- ⚠️ Cannot verify from diff / plan-level:
  - GFM bare-URL autolinks: `[a](http://b)` becomes `\[a\]\(http://b\)`, which CommonMark renders as literal text (checked with markdown-it-py `commonmark`: no `<a>`), and `<http://c>` is neutralised by entity escaping. A renderer with the GFM autolink extension (GitHub, remark-gfm) would still linkify the bare `http://b`, as it would any bare URL in model text. The spec's `md_escape` table has no rule for this, so the implementation is spec-exact; whether report.md must defang bare URLs is a controller/spec question (TH09-02 assigns non-anchor link stripping to `safe_markdown` U09-57 for dashboard/chat, not to report.md).
  - IT09-06 (integration on a real build) is outside this card.

### Gate evidence (re-run by reviewer)
- `PYTHONUTF8=1 uv run pytest tests/unit/reports/test_evidence.py tests/unit/reports/test_markup.py --cov-branch`: 37 passed; `_evidence.py` 100 % line / 100 % branch, `_markup.py` 100 % / 100 % (≥90/85).
- `ruff check .` clean; `ruff format --check .` 396 files formatted; `mypy` 0 issues in 169 files; `lint-imports` 13 kept, 0 broken; `tools.check_module_size` exit 0.
- Budgets: `_evidence.py` 218/220, `_markup.py` 145/200.

### Strengths
- Escaping-first design is clean: no string concatenation of markup; every dynamic HTML value (qid, title, number text, uncited text) passes through `Markup.format`, which also escapes quotes in attributes (test asserts `&#34;&lt;x&gt;`).
- Tamper check mirrors the existing `herness.store.ops.evidence.get_evidence` pattern (SchemaViolation -> mismatch; non-dict params -> mismatch), and the test asserts WARNING level, the query_id field and absence of SQL in the log.
- Chunking test uses a connection spy and proves 500/500/201 statements; the all-in-ops test proves the warehouse is not touched.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/reports/_evidence.py:204 — `ops.ui_get_evidence_rows` raises `SchemaViolation` on invalid JSON in an ops `params`/`result_sample` column, so a corrupted (possibly tampered) ops row aborts the whole render instead of being treated as tampered and falling through to meta.evidence. The spec lists only StoreBusy/QueryError; behaviour is consistent with `get_evidence`, so this is a carry-over/controller note rather than a defect.
2. herness/reports/_evidence.py:32-35 — the `information_schema.columns` probe filters schema/table/column but not `table_catalog`; if the warehouse connection ever had another attached database with a `meta.evidence` table the probe could read the wrong one. Harmless for the spec's single read-only build connection.
3. herness/reports/_markup.py:143-145 (see ⚠️) — bare URLs in model text survive into report.md and are autolinked by GFM renderers. Spec-exact; raise with the controller if report.md must meet the "no link" postcondition under GFM.
4. tests/unit/reports/test_markup.py:189-208 — PT09-03 generates no `uncited` hits, so `mark.uncited` is not exercised by the property (UT09-26 covers it by example). Adding a random span to the strategy would strengthen it.
5. tests/unit/reports/test_evidence.py:208-222, 254-257 — some UT09-23 functions test private helpers (`_params`, `_sample`, `_executed_at`, `_trusted`) directly; acceptable for coverage but couples tests to internals.
6. herness/reports/_markup.py:92-94 — a number marker overlapped by an earlier uncited span is skipped and its `[[nK]]` text is emitted as an escaped literal run. Unreachable per R-16 masking and documented in the docstring; no action.
7. Process: final commit 0487de6 is an empty marker commit; the code landed in wip commits b12e557/9cc4d15. Fine if the controller squashes on merge.

### Assessment
**Task quality:** Approved
**Reasoning:** All four units match the binding spec values and the four controller rulings, the HTML/Markdown encoders emit only renderer-built tags and anchors with every model value escaped, and all gates, coverage (100/100) and budgets pass; the remaining items are minor or spec-level questions.
