# T07-12 report: Scratchpad and token counting

Status: DONE_WITH_CONCERNS (minor interpretations, listed below)
Worktree: D:\herness\.claude\worktrees\agent-a96b33ab490f63b70 (branch worktree-agent-a96b33ab490f63b70, base 8bb8194)
Commit: see final line (updated after commit)

## Files
| File | Lines | Budget |
|------|-------|--------|
| herness/harness/memory/working.py (U07-66 LedgerEntry, Hypothesis, CompactionNotes, UnmatchedNumeral; U07-67 Scratchpad; SCRATCHPAD_MAX_BYTES) | 296 | 300 |
| herness/harness/memory/tokens.py (U07-68 TokenCounter, U07-69 compute_thresholds, CACHE_MAX) | 116 | 260 |
| tests/unit/harness/memory/test_memory_working.py (UT07-49) | new | - |
| tests/unit/harness/memory/test_memory_tokens.py (UT07-50, UT07-51, UT07-52, PT07-03) | new | - |

No pyproject / import-linter / module-map change needed (both rows already in §2; lint-imports 13 kept).

Also changed: tests/unit/harness/test_tools_recording.py, one allowlist line in `_NON_ROW_HASHES` for ("memory/tokens.py", "_message_key"). UT05-124 guards every hash call site under herness/harness (first commit attempt failed on it in the pre-commit unit hook); the per-message SHA-256 cache key is required by U07-68. Hashing goes through herness.core.ids.sha256_hex (no direct hashlib).

## Tests
- RED: modules moved aside -> `pytest test_memory_working.py test_memory_tokens.py` -> 2 collection errors (ModuleNotFoundError).
- GREEN: `pytest tests/unit/harness/memory` -> 164 passed; card files 26 passed with `--require-test-ids`.
- Coverage (branch on): tokens.py 100 %, working.py 100 % (line and branch).
- Gates: ruff format/check clean, `mypy` (full, 254 files) clean, lint-imports 13 kept 0 broken, check_module_size exit 0, check_type_ownership exit 0.
- PT07-03: hypothesis, 150 examples; text mixes astral chars (U+1F600, U+10348), combining mark (e + U+0301), ZWSP/ZWJ/BOM, NUL, newline, tab and random surrogate-free text (Cs excluded); asserts count == llm estimate_tokens, exact False, and monotone on append.

## Implementation notes / rulings applied
- memory/tokens.py imports `from herness.harness.llm import tokens as llm_tokens`; defaults bind `llm_tokens.count_tokens` / `llm_tokens.estimate_tokens` (test asserts identity; no re-implementation, llm/tokens.py untouched).
- Anthropic exact threshold: `t * 5 >= budget * 3` (integer form of t >= 0.6 x budget, inclusive; tested at the exact boundary 108 / 180).
- vLLM cache: SHA-256 of `canonical_json(message.model_dump(mode="json"))`, insertion-ordered dict, CACHE_MAX = 10,000, oldest inserted evicted. Only exact counts are cached; a fallback (exact=False) is not cached so it is retried; any fallback makes the sum inexact. count_state for vLLM calls exact on the whole list (per spec).
- Unknown tokenizer -> ConfigError("unknown tokenizer for client <name>") at construction (tested via model_construct).
- compute_thresholds: safety = max(1024, m*3//100) (integer floor of 0.03 m), ratios via math.floor; budget <= 0 or ordering failure -> ConfigError("context budget too small for client <name>") checked before ContextStats is built. Example 27,744 / 19,420 / 23,582 / 12,484 exact.
- from_checkpoint: `model_validate_json` (bad JSON is a ValidationError too); WARNING `memory.scratchpad.invalid` with only `error_type`; logger "harness.memory"; test plants a marker string and asserts it is absent from the event. No other logging in working.py.
- SCRATCHPAD_MAX_BYTES exported; small helper `Scratchpad.size_bytes()` (UTF-8 length of compact sorted JSON of to_checkpoint) for the compactor; enforcement left to the compactor U07-76 (ContextCompactor).

## Deviations / interpretations (for the reviewer)
1. Sample JSON in render: the impl spec says "compact JSON, sort_keys"; the design §5.5 example shows `{"value": 0.183}` (with a space). Followed the binding impl text: separators (",", ":") -> `sample=[{"value":0.183}]`. Uses json.dumps (same settings as canonical_json) rather than canonical_json so render/cite/size_bytes never raise on a non-finite float (U07-67: errors none raised).
2. build_id attribute is passed through `escape_attr` (U07-44 sibling in render.py: also escapes quotes/newlines), not `escape_content`, because it sits inside a double-quoted attribute; all other dynamic text goes through `escape_content` (applied once per whole body line; the fixed labels contain no escapable characters).
3. Placeholder entries: `cite` creates a minimal entry (tool "unknown", step 0) when the query_id is absent. A later `upsert` for that query_id takes the real tool and step instead of "keep the earlier step" (min would pin step 0 and tool "unknown" forever). Spec is silent on this case.
4. `cite` id = n<count+1>, bumped past any id already taken (a restored ledger may have gaps), so the uniqueness postcondition holds. Since fix round 1, `upsert` routes every incoming cited ref through `cite` (identical key reuses the id, otherwise a fresh unique id), so ids stay unique across the ledger.
5. Render details the format leaves open: each cited ref is its own ` cited: <id> <col>=<value> (<k>=<v>, ...)` segment (row-key pairs joined ", ", parenthetical omitted when row_key empty/None); values rendered with `str(value)` so the U07-76 invariant (`str(value)` present) holds; hypothesis query_ids joined ", " and parenthetical omitted when empty; `dead_ends`/`next_steps`/`hypotheses` items joined "; " and the line reads `label:` when empty; the `NOTES` header is always present and notes lines only when notes is set.
6. LedgerEntry fields other than query_id/tool/step have defaults (None / []) so producers and tests can build minimal entries; query_id validated by QUERY_ID_RE or "".
7. Dispatch rules said "Run only the card's tests plus tests/unit/harness/memory"; the implementer-rules full `unit or integration` run was not done (pre-commit unit hook runs on commit).

## Fix round 1 (review T07-12-review.md)
- I1: every dynamic string rendered by Scratchpad.render is flattened before escape_content: one helper `_flat` replaces runs of `[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]` (every str.splitlines break) with one space; applied uniformly to each body line (one body item = one output line; fixed labels contain no breaks) and to build_id before escape_attr. New parametrized UT07-49 test injects a payload with every break char plus a fake `- q_...` line and a `NOTES` header into tool, columns, sample value/key, cited column, row_key key/value, error, sql_head, unmatched, progress, hypothesis text/query_ids, dead_ends, next_steps, steps and build_id: line count equals the benign baseline, exactly one NOTES line, no fake ledger line, no break char left.
- m1: upsert strips incoming cited refs, inserts/merges the bare entry, then calls `cite` for each ref; test proves clashing incoming ids (n1, n1, n1, n5) become unique n1..n4.
- m2: LedgerEntry.sample validator rejects NaN/+inf/-inf at any depth (ValidationError "sample values must be finite numbers"); tested flat and nested.
- m3: cite key normalises row_key {} to None (they render alike); tested.
- m4: report and code comment now name the compactor U07-76 for the 1 MiB cap.
- Gates after fix: 303 passed (tests/unit/harness/memory + test_tools_recording.py, --require-test-ids), working.py 100 % line and branch, ruff clean, full mypy clean, lint-imports 13 kept, check_module_size 0. working.py 296/300.

## Concerns
- None open after fix round 1.

Commit: 0448988 feat(memory): T07-12 scratchpad and token counting (pre-commit hooks incl. pytest-unit passed).

Fix round 1 commit: af9f6c9 fix(memory): T07-12 review fixes for scratchpad rendering and ledger ids (hooks incl. pytest-unit passed).
