# T06-02 report: Report and chat types

Status: DONE_WITH_CONCERNS
Commit: 7a83ec1 feat(types): add report draft and chat types (T06-02)
Worktree: D:\herness\.claude\worktrees\agent-a65b4b3b805e83310

## Implemented
- `herness/core/types/swarm/drafts.py` (300/300 lines): Paragraph (U06-13), Section (U06-14),
  RecommendationItem (U06-15), RankedEntity (U06-16), Coverage (U06-17), ReportDraft (U06-18,
  `mode: Literal["full","findings_only"] = "full"`, R-49), classmethods
  `writer_output_model` / `writer_schema` (U06-19; private `_WriterOutput`, `_WriterRecommendation`
  defined once at import), ChatAnswer (U06-20), ChatEvent union + 9 members + `CHAT_EVENT_ADAPTER`
  (U06-21; `correction_captured` per R-32). Conventions of T06-01: `_Model` base
  (extra=forbid, frozen, strict=False), ID patterns (run, fnd, q_, n, plus job_/mem_/rec_ from
  herness.core.ids prefixes), vocabularies imported from `.tasks`.
- Structure choices: `RecommendationItem` subclasses `_WriterRecommendation` (adds `rank`, `rec_id`),
  so the Writer shape keeps the same validators. `action_levers`, `removed`, `dead_tasks` entries are
  private closed TypedDicts (`@with_config(extra="forbid")`): runtime plain dicts with exact keys, and
  the lever object is `additionalProperties: false` in the Writer schema. `flags` keys checked in the
  ReportDraft validator. findings_only requires `recommendations == []` and sections ids exactly
  `["executive_summary"]`.
- Re-exports: `swarm/__init__.py` (29/40), `herness/core/types/__init__.py` (129/150; compact sorted
  `__all__` kept).
- `herness/core/types/_ownership.py`: appended the 9 event members and `CHAT_EVENT_ADAPTER` to owner
  06 (U00-45 invariant "owner cards append any further helper type"); `tests/unit/core/test_types_ownership.py`
  EXPECTED updated to match (UT00-80).
- Tests: `tests/unit/core/types/test_swarm_drafts.py` (UT06-08, UT06-09, UT06-10; 48 tests).

## Evidence
RED: `.venv\Scripts\python.exe -m pytest -p no:logging -q tests/unit/core/types/test_swarm_drafts.py`
-> `ImportError: cannot import name 'CHAT_EVENT_ADAPTER' from 'herness.core.types.swarm'` (collection error).
GREEN: same command -> `48 passed`; drafts.py coverage 100 % line and branch.
Gates: ruff format --check (81 formatted), ruff check (All checks passed), mypy (no issues),
lint-imports (8 kept, 0 broken), check_type_ownership exit 0 (only `pending owner 07`, `09`),
check_module_size exit 0. Full suite `-m "(unit or integration) and not slow"`: 680 passed,
5 deselected, 1 xfailed (IT00-02, pre-existing xfail marker for traceability doc defects TR001..TR005;
unchanged by this card). test_ut00_48_init_reexports_only passes.

## Concerns / deviations
1. Ownership table extended (impl 00 file + UT00-80 EXPECTED): ModeEvent, TokenEvent, ToolEvent,
   EvidenceEvent, VerificationEvent, EscalatedEvent, FinalEvent, ErrorEvent, CorrectionCapturedEvent,
   CHAT_EVENT_ADAPTER. Needed because the checker rejects unregistered public names (OWN011) and
   U06-129 constructs these classes. The impl 00 U00-45 list for "06" should gain these names in
   the spec.
2. Acceptance check "writer_schema() JSON contains no `rank`": literally false, because spec 05
   NumberRef.unit enum contains the value "rank". No field/property named rank, rec_id, coverage
   exists; the test checks property names and that "rank" appears only in that enum.
3. UT06-09 "every object additionalProperties: false": holds for every record object; the one
   exception is spec 05 `NumberRef.row_key` (`_RowKey`, a free map of column names to scalars),
   which is not ours to close. The test asserts it is the only open object.
4. Spec pseudo-code in U06-129 constructs events positionally (`ModeEvent("defer", ...)`);
   pydantic models need keywords. `type` fields default to their literal so callers omit them.
5. drafts.py sits at exactly its 300-line budget.
