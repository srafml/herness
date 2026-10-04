# T06-23 report: Chat support

Status: NEEDS_CONTEXT (all but `detect_entities` built and committed)
Commit: 8218ae1 feat(pipelines): add chat constants and pure helpers (T06-23)

## Built
- `herness/harness/pipelines/chat_support.py` (218 lines, budget 300):
  - U06-126 `MODE_MESSAGES`, `CHAT_TOOLS` (read-only `MappingProxyType`), `EGRESS_NOTICE`, texts verbatim.
    `cloud` = full list minus get_record, get_cluster, semantic_search; `defer` = ().
  - U06-130 `ObservedTool`: same name/description/input_schema; async inner awaited, sync inner via
    `asyncio.to_thread` (detected with `inspect.iscoroutinefunction(inner.__call__)`); emits
    `ToolEvent(name, query_id=first or None, ok)` then one `EvidenceEvent` per query id not seen before;
    raised `Exception` emits `ToolEvent(ok=False, query_id=None)` and re-raises.
  - U06-131 `has_review_intent` only (regexes verbatim, case-insensitive). Entity count = total ids across
    types; candidate entities read from key `"candidate"`. Also exports `type ReviewKind`.
  - U06-132 `trim_failing_claims`, plus constant `NO_VERIFIED_ANSWER`.
  - U06-133 `chunk_text` (raises ValueError for size < 1).
- `tests/unit/harness/pipelines/test_chat_support.py`: 19 tests covering UT06-86..UT06-90.
- `__init__.py` lazy map unchanged (as instructed).

## NEEDS_CONTEXT
`detect_entities(question, directory: EntityDirectory)` (U06-131) is not implemented: `EntityDirectory` is
not in the tree and no spec unit defines it (only mentioned in the U06-131 row: "loaded once per build
(core.team, core.service, score.funding, core.work_item.key)"). Need its owner/shape (fields: team/service
names+ids, candidate ids+titles, work-item key -> candidate id map; loader location). Then detect_entities
plus its UT06-86 cases are a small follow-up (~30 lines, fits budget). UT06-86 currently tests intent only.

## Interpretations / deviations
- ObservedTool dedups evidence per instance ("this turn"): ChatService must build fresh wrappers per turn.
  Dedup across different wrapped tools in the same turn would need a shared seen-set (not in the spec
  signature, so not added) - reviewer may want an optional `seen` param.
- trim_failing_claims: uncited spans taken from `v.items[*].uncited` (offsets into answer.text), so
  `find_uncited` is not called directly (the signature has no allowed-pattern argument). Kept sentences
  rejoined with a single space; empty sentences (trailing whitespace) skipped. `query_ids` recomputed by
  dropping ids that were used only by dropped numbers (ids not tied to any number are kept, order kept).
  Answer without removed sentences is returned unchanged (same object).
- Catches `Exception` (not BaseException) so cancellation emits no tool event.

## Evidence
- RED: `pytest tests/unit/harness/pipelines/test_chat_support.py` -> ModuleNotFoundError chat_support.
- GREEN: `pytest -k "UT06_86 or UT06_87 or UT06_88 or UT06_89 or UT06_90" --cov --cov-branch` -> 19 passed,
  chat_support.py 114 stmts / 26 branches, 100% line and branch.
- ruff format/check clean; mypy: no issues (61 files); lint-imports 10 kept 0 broken;
  check_type_ownership exit 0 (initially OWN041 for submodule imports, fixed by importing from herness.core.types).
- check_module_size: only the known settings.py 330 > 260 failure; chat_support.py 218 <= 300.
- Full `-m "(unit or integration) and not slow"`: 1296 passed, 1 failed = IT00-02 test_check_scripts (the
  same known settings.py module-size failure).

## Fix round 1
Commit: 2fd84bb fix(pipelines): address T06-23 review round 1 (T06-23)
- Important-1: `ObservedTool.__init__(inner, emit, *, seen: set[str] | None = None)`; ChatService shares one
  set across all wrappers of a turn (default: private set). Docstring corrected. New UT06-90 test: two
  wrappers sharing `seen` -> Q2 emitted once, set ends {Q1,Q2,Q3}.
- Minor-1: `_sentences` now returns each sentence's following separator; new `_join` keeps the original
  separators between kept sentences (when sentences between them are removed, the separator with the most
  line breaks wins, so paragraph breaks and bullet items survive). New UT06-87 test with paragraphs/bullets.
- Minor-2: async `__call__` awaited directly; otherwise run in a worker thread and, if the result is
  awaitable, awaited. A non-`ToolResult` return raises TypeError (emits ToolEvent(ok=False)). Typed via
  casts to `Callable[..., object]` / `Awaitable[object]` + isinstance narrowing; mypy --strict clean.
  Two new UT06-90 tests (awaitable from a plain __call__; non-ToolResult return).
- Minor-3: fallback text -> `query_ids = []` (fallback test now includes an unrelated Q3 that is dropped).
- Gates: ruff format/check clean; mypy no issues; lint-imports 10 kept; check_type_ownership exit 0;
  check_module_size only the known settings.py failure. chat_support.py 255 lines (budget 300).
- Tests: 23 passed (-k UT06_86..UT06_90); chat_support.py 134 stmts / 40 branches, 100% line and branch.
