# T11-22 review: Script rendering and scripted client

Worktree `agent-a7dee3e9984a3f162`, base 02bd94a, head 3f88f88 (b68d8e0, dab1c7d, 3f88f88). Read-only review; probes run from a scratchpad script.

**Verdict: Approved** (Minor items only).

### Spec Compliance
- ✅ U11-39 `parse_tool_table` / `render_turn` (`herness/eval/scripted_render.py`): header regex, `|` split with the spec 05 `\|` / `\n` escapes undone, types (BIGINT/INTEGER/SMALLINT/HUGEINT → int, DOUBLE/FLOAT/REAL → float, DECIMAL → `Decimal`, NULL → None), None for anything that is not a table; `last` = last parseable `ToolResultPart`; `{{row.<col>}}` / `{{last.query_id}}` in every nested string; gaps → `ScriptMismatch`; `numbers_from` removed, markers in first-appearance order, K-th numeric column or explicit `numbers.nK.column`/`unit`, suffix units, USD as 2-place decimal string, `row_key` None for 1 row else `{columns[0]: row1[0]}`, `query_ids` set only when absent; `call_<call_index>_<i>` ids, `tool_use`/`end_turn`, `final.output` = canonical JSON + parsed, `final.text` text only, `final.<tool>` one call.
- ✅ U11-40 `ScriptedLLMClient`: dedup key order (DD11-02 field → judge qid from `judge:<qid>:<hash>` → resolver(task_id) → `chat` → `*`), `book.next_action`, faults (`http_500`/`hang`/`disconnect` → `ModelUnavailable`, `http_429` → `RateLimited(retry_after=1.0)`, `malformed_json` → `OutputValidationError` on a tool turn, else `text='{"truncated": '`, `parsed=None`), response fields per step 5 (`cost_usd=0`, `model="scripted"`, `provider="openai_compat"`, `latency_ms=0`, `request_id=fake-<call_index>`, `batch=False`), usage via `count_tokens` (R-17); `astream` 16-char `TextDelta`s, one `ToolCallDelta` per call, exactly one `Done` equal to `acomplete`; `complete` via `asyncio.run`, `RuntimeError` inside a running loop (checked before the coroutine is created, so the book is not advanced and there is no un-awaited-coroutine warning); thread safety via the book's lock (no other shared mutable state; `_ESTIMATE_CFG` is an immutable module constant).
- ✅ U11-41 `ScriptedRegistry` (client → scripted for any key; `config`/`model_for`/`chain_for` (+ `role_params`) delegate) and `ops_dedup_key_resolver` (via `herness.store.ops.get_task`, no direct SQLite; missing task → None; None task id → None).
- ✅ UT11-64 (`test_ut11_64_*`, 3 functions: the spec 05 sample with its arbitrary-rows line; DECIMAL/NULL/escapes/cut cells; 5 non-table cases).
- ✅ UT11-65 (`test_ut11_65_*`: 1-row and 3-row tables, n1/n2(/n3), row_key None vs `{team_name: ...}`, units by suffix (8 cases), `{{row.team_name}}` filled, explicit numbers map, final text/tool, 11 gap cases, determinism).
- ✅ UT11-68 (U11-40 half; `test_ut11_68_*`, 11 functions: tool/output turns, acomplete == complete, all five fault kinds incl. `RateLimited.retry_after == 1.0`, malformed_json both branches, stream shape and one `Done` == `acomplete`, running-loop RuntimeError, ScriptMismatch identity, dedup rules, DD11-02 field precedence, offline estimator config). The U11-42 `FakeLLMClient` half belongs to T11-23.
- ✅ UT11-69 (`test_ut11_69_*`: tmp ops store with a `task` row → dedup key, unknown → None; registry routes `local-30b`/`local-large`/`claude-opus` to the scripted client; `chain_for`/`model_for`/`config` delegated).
- ✅ PT11-05 (`test_pt11_05_format_then_parse_round_trips`, 200 examples): 1–5 columns, 1–20 rows, BIGINT/INTEGER/DOUBLE/DECIMAL(18,2)/VARCHAR + NULL, formatted by the real spec 05 `format_result`, asserts that the qid, columns, types and every value round-trip. See Minor 1 on generator width.
- ⚠️ Not verifiable here: the `--mock-llm` wiring (a later card) will need to accept `ScriptedRegistry` where `LLMRegistry` is typed (plain class, not a subclass). See Minor 4.

### Verification performed
- `pytest tests/unit/eval -q` (TMP/TEMP on D:): 150 passed. The two card files alone with `-W error`: 48 passed, no warnings.
- Branch coverage on the two modules: scripted_render 198 stmts / 64 branches, 100 %; scripted_client 127 stmts / 30 branches, 100 % (the gate is ≥ 90/85).
- `mypy --strict herness/eval` plus both test files: 0 errors. `ruff check` / `ruff format --check` clean. `tools.check_module_size` exit 0 (290 and 236 lines against the 300 budget). `lint-imports`: 13 kept, 0 broken. No `from tests` / `import tests` in `herness/eval/*.py`.
- Probe 1 (network): I trapped `socket.getaddrinfo`, `socket.create_connection`, `httpx.Client.__init__` and `httpx.AsyncClient.__init__` *before importing* `scripted_client`. Importing the module (which builds `_ESTIMATE_CFG`), `complete` and `astream` touched none of them. `tokens.count_tokens` returns `estimate_tokens` directly when `cfg.tokenizer == "estimate"`: in tokens.py the `!= "estimate"` branch is the only path to `_vllm_count` / `_anthropic_count`. `ClientConfig` validation (settings.py:158-206) resolves no secret and does no I/O. `_ESTIMATE_CFG.api_key is None`, the base URL is loopback and `off_network=False`. The UT11-68 `no_network` fixture also traps the tokenizer paths, the loopback clients and the egress guard.
- Probe 2 (determinism): 20 renders of the same turn and messages gave 1 distinct output. The markers `[[n2]] [[n1]]` produce n2 (the 2nd numeric column) and then n1, in first-appearance order.
- Probe 3 (PT11-05 robustness beyond the test's generators): 500 examples, 0 round-trip failures. The probe covered full-range BIGINT (±2^63), HUGEINT (±2^127), SMALLINT, DOUBLE/FLOAT/REAL (any finite or infinite float rounded to 6 significant digits, exponent notation included) and DECIMAL(38,6). The parser is correct over a wider range than the test proves.
- Script validation: the client only accepts a `ScriptBook`. Its `LLMScript`/`ScriptTurn` models (strict, frozen, extra=forbid, exactly one of tool_calls/final, 1–200 turns) validate at construction, and `load_scripts` enforces the 256 KB / 500-script / alias-expansion caps. Nothing in this card parses YAML or builds turns outside those models. `render_turn`'s `((key, value),) = final.items()` relies on the model validator.

### Builder deviations: rulings
1. `render_turn(..., *, call_index: int = 0)`: **accept**. Step 4 needs it; it is keyword-only and documented in the module docstring.
2. Placeholder identity in a render-side `ScriptMismatch`, re-raised by the client with the real identity and prompt hash: **accept**.
3. `ops.get_task(task_id)` (the real T06-05 signature, with the per-thread connection inside): **accept**. It meets the intent of R-08/R-10.
4. DD11-02 via `getattr(meta, "dedup_key", None)`: **accept**. DD11-02 is still open and this matches its stated default. Letting a None resolver result fall through to `chat`/`*` is sensible.
5. The parser skips `ARBITRARY_ROWS_LINE`, caps rows at `shown`, unescapes, and returns None on wrong column counts: **accept**.
6. NumberRef value rules (usd as a 2-place string; non-usd DECIMAL → float; invalid → `ScriptMismatch`): **accept**. See Minor 3.
7. Usage (tools left out of the input count; 0 output tokens for empty text): **accept**. The spec says "system and messages".
8. `ScriptedRegistry` as a plain class, with `role_params` delegated and `health` not wrapped: **accept** for this card. See Minor 4; no `.health()` callers exist in `herness/` today.
9. `hang` raises immediately; malformed_json past the last turn takes the text branch: **accept**.
10. PT11-05 generator choices: **accept**. See Minor 1.

### Strengths
- The renderer is small and pure, and every gap becomes a typed `ScriptMismatch`. NumberRefs are validated through the real `NumberRef` model before they are emitted.
- PT11-05 uses the real spec 05 formatter, not a re-implementation in the test, so it pins parser/formatter agreement.
- Network isolation is tested with traps rather than taken on trust. `complete` checks for a running loop first, so a refused call does not use up a script turn, and a test asserts this.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. `tests/unit/eval/test_scripted_render.py:366-369`: the PT11-05 generators are narrower than the format's real ambiguities require.
   - BIGINT is capped at ±10^15, although the format prints any int exactly.
   - DOUBLE is limited to hundredths with ≤ 5 significant digits (`n / 100`), so exponent forms (`1e-05`, `1.23457e+06`) and `inf` are never exercised.
   - SMALLINT, HUGEINT, FLOAT and REAL are never drawn.

   The only real ambiguity is "6 significant digits", e.g. `st.floats(...).map(lambda v: float(format(v, ".6g")))`. Probe 3 shows the parser passes the wider generators, so this is test-strength polish, not a defect. The VARCHAR exclusions (backslash, `\r`, surrounding whitespace, literal `NULL`) are real format ambiguities: spec 05 `_escape` does not escape `\`, and cells are stripped.
2. `herness/eval/scripted_render.py:103-108`: cells of `untrusted_columns` arrive wrapped, e.g. `<untrusted_data source="warehouse" record_id="">A&amp;B</untrusted_data>`, and are not unwrapped. So `{{row.<untrusted col>}}` puts the wrapper and HTML-escaped text into script output (probe 4). Spec U11-39 is silent on this. It is worth a note for script authors, or a follow-up ruling if golden scripts template free-text columns. PT11-05 does not exercise it (no `untrusted_columns`).
3. `herness/eval/scripted_render.py:202-203`: non-usd DECIMAL values become `float` (probe: `12345678.1234567891` → `12345678.12345679`). U11-39 step 3 says "value = row 1 value", and DECIMAL parses to a "`Decimal` string", so the literal reading is a lossless decimal string. The float does no harm to the Verifier's tolerance check at normal precision. The builder documented this as deviation 6.
4. `herness/eval/scripted_client.py:194-220`: `ScriptedRegistry` is not an `LLMRegistry` subtype and has no `health()`. The `--mock-llm` wiring card must cast it or introduce a protocol, and any future `registry.health()` call would raise `AttributeError`. Flag this for that card.
5. Out of scope (T11-21, not this diff): `tests/unit/eval/test_scripted.py:269` `test_st11_12_cyclic_and_deep_documents_rejected` failed once under `--cov` (2.13 s against its 2.0 s bound) and passed when re-run without coverage. It is a wall-clock assertion that is flaky under instrumentation or load.

### Assessment
**Task quality:** Approved
**Reasoning:** All three units match U11-39/40/41, with sound and documented deviations. All five test IDs are present and meaningful, and the client provably reaches no network or HTTP client. Every gate passes (tests, 100 % line/branch coverage, mypy --strict, ruff, module size, import contracts). The remaining items are test-strength and follow-up notes.
