# T09-10 review (Markdown report templates) — worktree agent-a03d134cb492941ae, head 3e13d5e

### Spec Compliance
- ✅ Files per card: `base.md.j2` (162 lines), `funding_review.md.j2` (13), `org_review.md.j2` (13), `partials/components.md.j2` (128); all under the 200-line budget. No other production file touched (diff stat: 4 templates + 1 test).
- ✅ Outline order per design §5.2: watermark, header (slot 0, incl. data-as-of, coverage and gate-2 stats), banners (1), executive summary (2), recommendations (3: section text, `r.cards`, then funding/org table), portfolio (4), org scorecards (5), actions (6), retrospective (7), caveats (8), method (9), evidence appendix (10), run appendix (11). Default section titles match §5.2; draft titles win.
- ✅ Headings only `#`–`###` (verified by the test and by my hostile fuzz: headings seen = `#`, `##`, `###`).
- ✅ Watermark: leading `> **Not for decision**` line only when `not r.publishable` (base.md.j2:6-9).
- ✅ Every interpolation goes through `md` (via `one`/`segs`/`num`/`ev`); no `|safe` anywhere; the only raw HTML is `<a id="ev-{{ e.query_id }}"></a>` (components.md.j2:95-97), emitted only for ids passing `ok` (`q_` + 16 lowercase hex, components.md.j2:7). Evidence links `(#ev-…)` are gated the same way.
- ✅ Fence rule (sub-controller ruling: pure Jinja, no new filter): components.md.j2:104-108 computes the longest backtick run and uses `max(3, run+1)`. Verified: SQL containing 3 backticks gets a 4-fence, 4 → 5, SQL ending in 6 backticks → 7, a 4-backtick-only line between SQL lines → 5, empty SQL → 3; no fence ever closes early.
- ✅ StrictUndefined: missing `cards`/`evidence`/`method`/`publishable` × both kinds → `SchemaViolation("report template error: <name>")` via the ruled test-local env + wrapper (UT09-92).
- ✅ Context names per ruling (`r.<ReportData field>`, `r.evidence`; `r.manifest` not read). All `SECTION_IDS` keys are always present in `ReportData.sections` (_data.py:343), so `r.sections[sid]` cannot fail on a draft that omits a section.
- ✅ TH09-02 / "no raw HTML besides evidence anchors": my hostile fuzz (scratch script, not committed) put a value containing newline, `<img src=x onerror=1>`, CRLF, `| a | b |`, `|---|`, `# h`, a backtick fence, `~~~`, `<!-- c -->`, `&lt;&#60;`, `![i](http://e)`, `[l](http://e)`, `<http://e>` and a trailing backslash into every field (title segments, section texts and titles, card headline/summary/target/extras/finding ids, table columns and cells with valid, upper-case-hex and bogus query ids, banners and details, portfolio scenario/budget/status, scorecards, levers, retro, all caveat sub-lists and flags, method role_calls, run appendix, evidence build id / executed_at / params keys+values / sample columns+cells / used_by, invalid evidence query id). Result outside SQL fences: zero `<tag` sequences, zero unescaped `![`, no foreign links, no raw entity (`&#60;` → `&amp;\#60;`), no broken table lines (newlines folded in cells), no setext underline lines, headings within #–###, all fences balanced. Invalid ids produce neither anchor nor link.
- ⚠️ Cannot verify: outline equivalence with the HTML set (T09-09 built in parallel, absent here). HTML U09-28 says kind templates fill slots 2–9 while the MD set puts 2 and 4–9 in base with a `recommendations` block; T09-11 should reconcile block names/structure. Sample row limit and 200-char cell truncation (§5.2 slot 10) belong to the data layer, not verifiable here. GitHub rendering specifics (`user-content-` id prefixing, GFM extended autolinks) not verified.

### Strengths
- Tight allow-list approach: one escaping path (`one` = string → fold CR/LF → `md`), id validation in pure Jinja, anchors/links gated on it.
- Fence computation correct for all adversarial cases tried; SQL lives only inside the fence.
- Tests build real `ReportData`/`EvidenceEntry` objects, cover both kinds, slot order, watermark, fences, anchor order, invalid id, empty states, and cross-check `segs` against `segments_to_md`.
- Gates re-run by me: `uv run pytest tests/unit/reports -q -p no:logging` → 280 passed, 1 skipped (pre-existing symlink-privilege skip); ruff check clean; ruff format --check clean; mypy clean on the test file; lint-imports 13 kept / 0 broken.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. `strip_images` divergence — components.md.j2:12-24 (`segs`) re-implements `segments_to_md` in Jinja but cannot call `strip_images`, so a text segment `![alt](http://x/?d=secret)` renders as the escaped literal `\!\[alt\]\(http://x/?d=secret\)` instead of U09-18's `alt`. Severity decision: Minor, not Important. The U09-28 MD row does not mandate `segments_to_md`; the U09-18/TH09-02 security postcondition (no image syntax, no foreign Markdown link, nothing fetched) still holds; and the fix belongs to the T09-11 context builder. Residual risk: the URL survives as visible text and GitHub's GFM extended autolink may make it clickable (already true of any bare URL in model text under `segments_to_md`, a U09-18 spec-level gap). DRY cost: security-relevant encoding now lives in two places. Carry-over to track in T09-11: mirror the HTML `text(block)` design and pass precomputed `segments_to_md` strings (or add an `md_segments` filter via a spec change), then replace `segs`.
2. Test gap for newline/CR folding — tests/unit/reports/test_templates_md.py:45 (`HOSTILE`) and all fixtures contain no `\n`/`\r`, so the fold in `one` (components.md.j2:6) and in inline `segs` (components.md.j2:14) is untested; removing either keeps the suite green while letting a cell value break a table row or a heading line. Add a hostile value with `\n`, `\r\n`, a `|---|` line and a `# x` line to a cell, the header title and a card headline, asserting every `|` line ends with `|` and no extra heading appears.
3. Test gap in fence cases — test_templates_md.py:218-226: no SQL that ends with a backtick run, and no backtick-only line in the middle of the SQL (e.g. `"x\n````\ny"`). Both pass in my fuzz, but they are the cases the acceptance check names.
4. Link-check regex false negative — test_templates_md.py:268 `(?<!\\)\]\(` skips links whose text ends in an escaped backslash (`\\](`), so a foreign link after such text would be missed. Match `](` preceded by an even number of backslashes instead.
5. Fence computation is quadratic in backticks — components.md.j2:105 loops `range(1, count+1)` with a substring search each time. Measured (incl. template compile): realistic 5 KB SQL with 20 backticks ≈ 0.03 s/entry; hostile 20,000-char SQL (impl 05 cap) of alternating backtick/space ≈ 0.29 s/entry → ~86 s for 300 such entries, over BT09-05's 30 s. Only reachable with adversarial evidence SQL, so Minor; a cheaper pure-Jinja bound (e.g. derive runs from `sql.split('`')`) removes it.
6. Test-ID tagging — every test is labelled UT09-92 (test_templates_md.py:167-319) although UT09-92's spec row is only "missing variable → SchemaViolation"; the other 10 tests are extra U09-28 coverage. Acceptable; T09-11/T09-26 may re-map when IT09-/ST09- rows land.
7. Carry-overs recorded by the builder (not defects): datetime display `isoformat()` vs T09-09's format; `Used by` and lever `cards` as plain text (no `rec-N` anchors in MD — correct under the "only evidence anchors" rule); test-local `_environment`/`_render` to be replaced by `render.build_environment` + U09-24 conversion in T09-11. Pre-existing U09-18 limitation for the spec owner: `md_escape` does not escape `-`, `=`, `1.` or leading indentation, so multi-line paragraph text can still form lists/thematic breaks/setext headings (no raw HTML, no links); not this card's scope.

### Assessment
**Task quality:** Approved
**Reasoning:** All U09-28 Markdown rules hold under a hostile-input fuzz of every field (no raw HTML besides validated anchors, correct fences, #–### headings, watermark, StrictUndefined → SchemaViolation), files are within budget and gates are green; remaining items are test gaps, a hostile-only perf edge and the strip_images carry-over that belongs to T09-11.

## Re-review 1 (fix round 1, commit 18496d1 on 3e13d5e; scope M2, M3, M5 only)

Checked with `git diff 3e13d5e..18496d1` (components.md.j2 fence loop + test_templates_md.py). Re-ran `PYTHONUTF8=1 uv run pytest tests/unit/reports -q -p no:logging` → 284 passed, 1 skipped (pre-existing symlink skip); ruff check / format --check clean; mypy clean on the test file.

- ✅ M2 (fold tests) — `test_ut09_92_newlines_folded_in_headings_and_cells` feeds LF, CRLF and CR into the header title (`#`), a section title (`##`), a card headline with a number segment (`###` via inline `segs`), and plain and linked table cells. It asserts exact folded lines, no `\r`, no line starting with a continuation fragment, and every `|` line ends with `|`. This exercises both `one` (components.md.j2:6) and inline `segs` (:14); removing either fold now fails the test.
- ✅ M3 (fence tests) — added `"SELECT 1 -- x````"` (trailing 4-backtick run → 5-fence) and `"SELECT 1\n````\nFROM t"` (backtick-only middle line → 5-fence); the existing assertion checks the body is the exact SQL followed by the closing fence.
- ✅ M5 (fence perf) — components.md.j2:97-101 now stops checking at the first absent run length (`namespace(done)`). Correct because run lengths are prefix-closed (if n backticks occur, so do n-1). I checked the fence length against the true longest run for 5 cases, and it matched in all 5. Measured per entry (after compile): alternating 10k single backticks 0.0056 s (was ~0.29 s), runs 1..199 0.0074 s, realistic 5 KB SQL 0.003 s. The new test `test_ut09_92_fence_scan_stops_at_first_missing_run` pins the alternating case (< 1.0 s, 3-fence).
  - Residual (Minor, non-blocking): cost is still quadratic in the *longest single run*. One 20,000-backtick run takes ~0.19 s/entry (~57 s for 300 such entries); a 10,000-backtick run after 10 KB of text takes ~0.13 s/entry (~40 s for 300). That needs a >10k-backtick run inside evidence SQL, which is pathological; accept.
  - Minor: the new test's wall-clock bound (`< 1.0` s, including environment and template compile) could flake on a very slow CI host; measured run is well under 0.1 s, so the margin is wide.

M1, M4, M6, M7 parked as stated (M1 remains a tracked T09-11 carry-over).

**Re-review 1 verdict:** Approved — M2, M3 and M5 are resolved; no new Critical or Important findings.
