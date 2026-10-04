# T11-22 report: Script rendering and scripted client

Branch `worktree-agent-a7dee3e9984a3f162` (base 02bd94a). Status: DONE_WITH_CONCERNS (small, documented rulings below).

## Commits
- b68d8e0 `wip(T11-22): script rendering (U11-39) with UT11-64, UT11-65, PT11-05`
- dab1c7d `wip(T11-22): scripted LLM client and registry (U11-40, U11-41) with UT11-68, UT11-69`
- 3f88f88 `feat(eval): add script rendering and scripted LLM client (T11-22)` (module docstring note on the call_index addition)

## Files
| File | Lines | Budget |
|------|-------|--------|
| `herness/eval/scripted_render.py` (U11-39) | 290 | 300 |
| `herness/eval/scripted_client.py` (U11-40, U11-41) | 236 | 300 |
| `tests/unit/eval/test_scripted_render.py` | UT11-64, UT11-65, PT11-05 | - |
| `tests/unit/eval/test_scripted_client.py` | UT11-68 (U11-40 half), UT11-69 | - |

No pyproject / import-linter / module-map change needed: both modules already have §2 rows, and `herness.eval` is already in the L5 layer contract. `lint-imports`: 13 kept, 0 broken. Nothing under `herness/eval` imports from `tests/`.

## Network / no-HTTP guarantee
- `ScriptedLLMClient` builds no HTTP client, no SDK client and no adapter; it only calls `ScriptBook.next_action`, `render_turn` and `count_tokens`.
- `count_tokens(cfg, ...)` (T05-07, unchanged; tokens.py untouched) picks its path from `cfg.tokenizer`; only `vllm_endpoint` / `anthropic` reach a network path. The client passes a module constant `_ESTIMATE_CFG` (`kind=openai_compat`, `tokenizer="estimate"`, a loopback base URL that is never contacted, `off_network=False`), so `count_tokens` returns `estimate_tokens(...)` directly (R-17: still the one estimator).
- UT11-68 fixture `no_network` traps `tokens._vllm_count`, `tokens._anthropic_count`, `egress.loopback_http_client`, `egress.aloopback_http_client`, `egress.get_guard`, `httpx.Client.__init__` and `httpx.AsyncClient.__init__`; the complete/stream tests assert none was reached. `test_ut11_68_estimator_config_is_offline` pins the config.

## Script validation
Scripts reach the client only as a `ScriptBook` (T11-21 `load_scripts` / `LLMScript` models: 256 KB file cap, 500 scripts, 200 turns, alias-bomb guard). Nothing in this card parses YAML or bypasses the loader.

## Determinism
`render_turn` is pure: tool call ids `call_<call_index>_<i>`, `request_id=fake-<call_index>`, `final.output` text is `canonical_json` (sorted keys, compact), NumberRefs in marker first-appearance order. No randomness, clock or ids. UT11-65 asserts identical renders; UT11-68 asserts `acomplete == complete` and `Done.response == acomplete`.

## Rulings / deviations (for the controller)
1. `render_turn(turn, messages, *, call_index: int = 0)`: the spec signature has no `call_index`, but step 4 needs it for `call_<call_index>_<i>`; added keyword-only with default 0.
2. `render_turn` raises `ScriptMismatch` with placeholder identity (`role/model_role/dedup_key="*"`, real `call_index`); `ScriptedLLMClient` re-raises with the real role, model role, dedup key and prompt hash (16 hex of SHA-256 over canonical system + messages).
3. U11-41 says `get_task(connection(), task_id)`; the real T06-05 `herness.store.ops.runs.get_task(task_id)` takes only the id and reads on the per-thread connection itself (R-10). The resolver calls `ops.get_task(task_id)` (no direct SQLite access from `herness.eval`).
4. DD11-02 default applied: dedup key = `RequestMeta.dedup_key` if that attribute exists (read via `getattr`; the field does not exist yet) → `eval_judge` question id from `judge:<qid>:<hash>` (malformed → `*`) → resolver(task_id) when a resolver is set and `task_id` is non-empty (a None result falls through) → `"chat"` for role `chat` → `*`.
5. `parse_tool_table`: skips the spec 05 "rows shown are arbitrary; add ORDER BY" line (imports `ARBITRARY_ROWS_LINE` from `herness.harness.tools`), takes at most `shown` rows, un-escapes `\|` and `\n` (inverse of spec 05 `_escape`), returns None when the header regex fails or a names/types/row line has the wrong column count. A numeric cell that does not parse (e.g. a cut `12…`) stays a string. DECIMAL cells parse to `decimal.Decimal`.
6. NumberRef values: `usd` → finite decimal string quantized to 2 places; DECIMAL non-usd → `float`; NULL / non-numeric / non-finite cited value, marker index beyond the numeric columns, or an explicit `numbers.nK` that forms an invalid NumberRef (e.g. unknown unit) → `ScriptMismatch`. NumberRefs are stored as `model_dump(mode="json")` (includes `format: null`). `row_key` Decimal values are stringified. Unit rule: suffixes `_usd/_hours/_minutes/_days/_pct/_ratio/_rate`, then name contains `count` or `incidents` or starts with `n_` → `count`, else `other`. `{{row.<col>}}` of a NULL cell renders `NULL`. Only `{{row.*}}` / `{{last.*}}` templates are recognised; `{{last.<other>}}` is a gap.
7. Usage: `input_tokens = count_tokens(system + messages, tools=[])`, `output_tokens = count_tokens(text)` (0 for empty text, so tool-call-only turns count 0 output tokens, literal to the spec).
8. `ScriptedRegistry` also delegates `role_params` (pure config lookup used by the loop); `health` is not wrapped (not in the spec signature). It is a plain class with the LLMRegistry method set, not a subclass, so callers typed `LLMRegistry` need a cast or protocol (for the `--mock-llm` wiring card).
9. Fault `hang` raises `ModelUnavailable` immediately in process (per spec), no sleep. `malformed_json` past the last turn is treated as a non-tool turn (truncated text response).
10. PT11-05 formats with the real spec 05 formatter `herness.harness.tools.format_result` (RecordedResult); types BIGINT/INTEGER/DOUBLE/DECIMAL(18,2)/VARCHAR plus NULLs; DOUBLE values generated with at most 5 significant digits (format shows 6), VARCHAR from an alphabet including `|` and newline, excluding surrounding whitespace and the literal `NULL` (the format's own ambiguities).

## Tests (RED then GREEN)
- RED: `uv run pytest tests/unit/eval/test_scripted_client.py` before the module existed → `ImportError: cannot import name 'scripted_client' from 'herness.eval'` (collection error). Render tests were written together with the module; the first run failed `test_ut11_64_parses_spec05_sample` (the sample's arbitrary-rows line), fixed in the parser.
- GREEN: `uv run pytest tests/unit/eval -q` → 150 passed (card tests: 34 render incl. PT11-05 at 200 examples, 14 client).
- Coverage (branch): scripted_render 100 % line / 100 % branch; scripted_client 100 % line / 100 % branch.
- Gates: ruff format / ruff check clean; mypy (herness/eval + test files) 0 errors; lint-imports 13 kept; check_module_size exit 0; pre-commit (incl. pytest-unit) passed on each commit.

## Environment note
C: had ~0.6 GB free; the first pre-commit run failed `test_ut01_50_sparse_file_over_limit_rejected` with `OSError: No space left on device` (1 GB sparse file in %TEMP%). Commits were made with `TMP`/`TEMP` pointed at `D:\tmp\T11-22` (no hook skipped, no SKIP env). Other agents may hit the same.

## Fix round 1 (review m1: PT11-05 generators too narrow)
- Test-only change in `tests/unit/eval/test_scripted_render.py`; `scripted_render.py` unchanged (290 lines); no parser fix was needed.
- PT11-05 now draws SMALLINT/INTEGER/BIGINT/HUGEINT over their full signed ranges (2^15/2^31/2^63/2^127), DOUBLE/FLOAT/REAL from any float incl. +-inf, NaN, subnormals and exponent forms, pre-rounded with `float(format(v, ".6g"))` to what the 6-significant-digit format can carry (NaN compared as NaN), plus DECIMAL(18,2) (+-1e16) and DECIMAL(38,6) (+-1e32). VARCHAR exclusions unchanged (stripped cells, literal `NULL`).
- Results: PT11-05 green at 200 examples for seeds 0-8; `pytest tests/unit/eval` 150 passed; coverage still 100 % line / branch on both modules; ruff, mypy clean.
- Observation (not this card): `tests/unit/eval/test_scripted.py::test_st11_12_cyclic_and_deep_documents_rejected` (T11-21) failed once in a coverage run of the whole eval folder and passed on every rerun (3x under coverage, plain suite) - looks timing/stack sensitive under the coverage tracer.
