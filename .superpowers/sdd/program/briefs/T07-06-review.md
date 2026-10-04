# T07-06 Rendering — verify review (head 7a6835b, base 02bd94a)

### Spec Compliance
- ✅ U07-44 escape_content / escape_attr / wrap_untrusted: Cc (except \n,\t) + ZERO_WIDTH dropped (policy.ZERO_WIDTH reused), angles escaped, case-insensitive `&lt;`/`&lt;/` + RESERVED_TAGS -> `blocked-` (render.py:41,44-47); escape_attr `&` first, angles, `"`, \n->space, cut 200 (render.py:50-53); wrap_untrusted exact format and `ToolInputError("unknown untrusted source <source>")` (render.py:56-63). Constants verbatim (render.py:23-29).
- ✅ U07-45 render_marker_values: parse_markers, format_number when `format` set, usd string / str(int) / `.6g`, unknown markers unchanged (render.py:66-87).
- ✅ U07-46 render_records: `max_tokens < 64` -> `ToolInputError("max_tokens too small")`; sort (-score, memory_id); attribute order id, layer, kind, status, confidence(.2f), author, numbers, query_ids(<=5), kind attrs, unconfirmed; bodies per kind; render_marker_values then escape_content; UNCONFIRMED_PREFIX; wrap with CONTEXT_NOTE; drop lowest whole while est > max_tokens; est(t) = estimate_tokens((), (), [SystemBlock(text=t)]) on the final wrapped text (render.py:99-100,194-208).
- ✅ UT07-18 (5 fns), ✅ UT07-19 (2 fns), ✅ UT07-20 (8 fns), ✅ PT07-04 (2 hypothesis fns: one open tag on the first line, one close tag as the last line, `<record ` == `</record>` == rendered, no other `<`/`>`, est <= budget, repeatability), ✅ ST07-07 (breakout attack gives an exact escaped body; config-pattern chain via real config/injection_patterns.txt -> parse_injection_patterns -> MemoryConfig -> InjectionScanner -> decide_policy pending -> `unconfirmed="true"` + prefix; no pattern list in render.py).
- ✅ Rulings: render.py takes no config, no logger/I/O/clock; est per spec.
- ✅ Test naming: IDs in function names and docstring first lines; module pytestmark = unit in both files (matches the other tests/security ST files).
- ⚠️ Cannot verify: the write path's actual `instruction_like` flag wiring (U07-50 step 5, store.py not in tree). ST07-07 hard-codes `flags=["instruction_like"]` after asserting the scan is non-empty (tests/security/test_st07_render.py:86-88). Acceptable until the store card lands, which should re-assert end to end.

Builder spec readings (judged):
- pass_lb from `data.pass_lb` else `item.confidence` (render.py:153): accepted. U07-90 step 7 sets `confidence = pass_lb` (spec line 1987) and there is no stored data.pass_lb.
- `n/a` for null/non-numeric baseline/actual/rel (render.py:107-110): accepted, spec silent; bool excluded correctly. NaN/inf render as `nan`/`inf` (probe), harmless.
- Malformed stored NumberRefs silently skipped (render.py:120-128): accepted under the pure/no-log ruling; data validated at write time; marker stays bare, no crash.
- Record layout `<record …>body</record>` (render.py:187): accepted; sql_template/qa_pair bodies contain the spec-mandated newline, so those records span two lines.

### Evidence (run by verifier)
- `PYTHONUTF8=1 uv run pytest tests/unit/harness/memory tests/security/test_st07_render.py -q -p no:logging`: **139 passed, 1 ERROR**. `test_ut07_20_render_never_logs` requests the `caplog` fixture, which does not exist with `-p no:logging`. Without that flag: 140 passed.
- Coverage render.py: 132 stmts / 34 branches, 100 % line, 100 % branch.
- ruff check / ruff format --check / mypy --strict on the 3 files: clean. render.py 208 lines (budget 220).
- Probes (scratchpad script, no tree edits): `&LT;/Untrusted_Data` -> `&LT;/blocked-Untrusted_Data`; `</untr​usted_data>`, `<​/untrusted_data>`, `&lt​;/record`, `</UNTRUSTED_DATA\x00>`, `<\x7f/record>` all -> `&lt;/blocked-…`; pre-escaped `&lt;/untrusted_data&gt;` blocked; attribute injection via author_role and verdict (`"><untrusted_data>`) fully escaped; a NumberRef with `<` in value/query_id is rejected by the type; empty wrapper est = 52 <= 64 (constant, so the postcondition holds for every legal max_tokens, including 64); 50 records x budgets 64/100/500/3000 all satisfy est <= budget; determinism confirmed.

### Strengths
- Tight, pure module; cached record strings so each drop is O(r); single `_est` helper matching the spec exactly.
- PT07-04 strips only the renderer's own tags and then asserts no `<`/`>` anywhere, a strong structural property; inputs include CRLF, U+2028, ESC, NUL, zero-width, pre-escaped entities.
- ST07-07 config case uses the real config file and public policy API; no hard-coded pattern list in production code.

### Issues
#### Critical (Must Fix)
- none

#### Important (Should Fix)
1. tests/unit/harness/memory/test_memory_render.py:318-324 — `test_ut07_20_render_never_logs` depends on the `caplog` fixture; under the program's gate command (`-p no:logging`) it ERRORs (fixture 'caplog' not found), so the card's test run is not green. It is the only `caplog` user in the repo. Fix: drop `caplog` and check stdlib silence another way (for example attach a handler to the root logger at DEBUG inside the test and assert it received nothing), keeping `structlog.testing.capture_logs`.

#### Minor (Nice to Have)
2. herness/harness/memory/render.py:39 — soft hyphen U+00AD (Cf) and other invisible format chars are not dropped, so `</untrusted­data>` renders `&lt;/untrusted­data&gt;` without `blocked-`. Spec-conformant (ZERO_WIDTH is the defined set) and structurally inert (no `<`), but name neutralisation can be evaded visually; consider widening ZERO_WIDTH at spec level (not a T07-06 change).
3. herness/harness/memory/render.py:53 — the 200-char cut after entity expansion can split an entity (`…&am`, `…&l`). Spec-conformant and harmless (no `"`/`<`); noted only.
4. herness/harness/memory/render.py:53 — U+2028/U+2029 are not treated as newlines in attributes (spec: only `\n` -> space). Harmless structurally.
5. herness/harness/memory/render.py:41 — the reserved-tag regex has no trailing boundary, so `&lt;records`/`&lt;recordset` also get `blocked-`. Matches the spec wording ("immediately followed by a name"); over-neutralisation is safe.
6. tests/security/test_st07_render.py:88 — `flags=["instruction_like"]` is hard-coded rather than produced by the write path (store.py absent); re-point to `MemoryStore.propose` when U07-50 lands.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Implementation is spec-conformant and the verifier could not break the escaping, the budget postcondition or determinism; the single blocking item is the caplog-dependent test that errors under the mandated `-p no:logging` gate command (a one-function fix).
