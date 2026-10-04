# T06-02 review: Report and chat types (commit 7a83ec1)

### Spec Compliance
- ✅ Spec compliant.
  - U06-13 Paragraph ✅: text is 1 to 4,000 chars, at most 40 numbers, FINDING_ID pattern. A paragraph with numbers must have finding ids, and NumberRef ids must be unique (drafts.py diff L218-233).
  - U06-14 Section ✅: SectionId, title 1 to 200, at most 50 paragraphs.
  - U06-15 RecommendationItem ✅: every field and bound matches. summary ≤ 400 (R-30). Every `*_ref` and `action_levers[*].delta_usd_ref` must name an id in `numbers`. The lever dict is closed to exactly 4 keys by a TypedDict with extra=forbid.
  - U06-16 RankedEntity ✅ and U06-17 Coverage ✅ (every count ≥ 0).
  - U06-18 ReportDraft ✅: `mode: Literal["full","findings_only"] = "full"` (R-49). Validators enforce ranks equal to i+1, unique section ids, flags keys limited to partial_coverage/notes, and for findings_only no recommendations and sections exactly `["executive_summary"]`. Banners are unique. `removed` and `dead_tasks` are closed TypedDicts. Caveats ≤ 600.
  - U06-19 ✅: `writer_output_model` and `writer_schema` are classmethods (allowed by D06-06). `_WriterOutput` and `_WriterRecommendation` are defined once at import, and the same class comes back on every call. `RecommendationItem` subclasses `_WriterRecommendation`, so the "same validators" rule holds by construction.
  - U06-20 ChatAnswer ✅.
  - U06-21 ChatEvent ✅: 9 members, `correction_captured` included (R-32), discriminated on `type`, and `CHAT_EVENT_ADAPTER` is a TypeAdapter.
  - UT06-08 ✅. Every Expected clause has a test: numbers without finding ids, rank gap, unknown flags key, 401-char summary, `mode` default `full`, and findings_only with a recommendation.
  - UT06-09 ✅, with the deviation discussed under concern 3.
  - UT06-10 ✅: one of each event round-trips through validate_python and validate_json, and an unknown or missing `type` is rejected.
  - Acceptance ✅: `writer_schema()` has no field named rank/rec_id/coverage.
  - Re-exports ✅ in swarm/__init__.py (29/40) and herness/core/types/__init__.py.
- ⚠️ Cannot verify from diff: that U06-142 builds a findings_only draft that passes this validator. That is a later card, but the validator requires sections exactly `["executive_summary"]`, and U06-142 must produce exactly that.

Reviewer runs in the worktree at 7a83ec1:
- Card tests plus UT00-48/UT00-80: 50 passed; the UT00-48/UT00-80 selection alone: 2 passed.
- drafts.py coverage: 100% line and branch.
- `tools.check_type_ownership`: exit 0. It prints only `pending owner 07` and `pending owner 09`, so **nothing is pending for 06**.
- check_module_size exit 0. mypy on herness/core/types: no issues. ruff check and ruff format --check: clean. lint-imports: 8 kept, 0 broken.

### Builder concerns: reviewer view
1. **Ownership table extended in impl-00 files (_ownership.py, UT00-80 EXPECTED).** Accept. The U00-45 invariant says: "Owner cards append any further helper type they place in their submodule, in the same card" (00-foundation.impl.md:1120). Doing this is required, not scope creep. Follow-up: the doc list in U00-45 should gain the 10 names so the spec matches the code (controller doc delta, not a code fix).
2. **"No rank" read as "no field named rank".** Accept. Design 06 §4.4 L306 says the Writer returns "recommendations without rec_id/rank", which is about fields. The literal string "rank" comes from the spec-05 NumberRef.unit enum (harness/evidence.py:59) and cannot be removed without breaking NumberRef. The property-name assertion is the right check. The extra string-replace assertion is fragile (Minor 1).
3. **NumberRef.row_key is the only open object.** Accept, as a spec-05-inherited exception. row_key maps column names to scalars, so it cannot be closed, and 06 does not own it. The test pins it as the only open object, so any regression shows up. Suggest a one-line spec note in UT06-09 or U06-19: "every record object; the spec-05 row_key map excepted".
4. **drafts.py at 300/300 and defaulted `type` fields.**
   - The budget is met with zero headroom. That is acceptable, but any fix round must compress or get a budget ruling.
   - Defaulting `type` is correct: U06-129 builds events without passing `type`, and model_dump includes defaults.
   - One caveat: `model_dump(exclude_unset=True)` or `exclude_defaults=True` drops `type` and breaks the discriminated round-trip (Minor 2).

### Strengths
- The Writer and Report recommendation shapes share one class hierarchy, so the "same validators" rule cannot drift.
- The dict-typed fields from the design (`action_levers`, `removed`, `dead_tasks`) are closed TypedDicts. They stay plain dicts at runtime but are strictly keyed, and the lever object is closed in the Writer schema.
- Tests are thorough. Parametrized negative cases cover every bound, and the schema test walks every object to check additionalProperties.
- ID patterns are reused from tasks.py and are consistent with herness.core.ids: q_ is 16 hex characters, run/fnd/job/mem/rec are ULIDs.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. tests/unit/core/types/test_swarm_drafts.py:301 (diff L797): `json.dumps(schema).replace('"score", "rank", "other"', "")` depends on the exact order and neighbours of the spec-05 unit enum. Any reordering of NumberRef.unit breaks this test for reasons unrelated to 06. The property-name check on file L299-300 already covers the acceptance check. Either drop the line, or assert that "rank" appears only inside `$defs.NumberRef.properties.unit.enum`.
2. herness/core/types/swarm/drafts.py:226-291 (diff L384-449): each `type` field has a default, so a consumer that serializes with `exclude_unset=True` or `exclude_defaults=True` emits events without `type`, and CHAT_EVENT_ADAPTER then rejects them. The model-level JSON schema also marks `type` as not required. Consider noting "serialize with plain model_dump(mode='json')" in the ChatEvent docstring, or use `Field(default=..., validate_default=True)` together with a serializer that always emits `type`. This is an advisory for the spec-09 SSE writer, not a bug here.
3. herness/core/types/swarm/drafts.py:243 (ToolEvent.query_id) and :251 (EvidenceEvent.query_id): tightened from the spec's `str | None` / `str` to the q_ pattern. This is consistent with the rest of 06 and correct for recorded queries. But T06-xx ObservedTool (U06-130) must pass only real query ids, or None, for tools that do not record a query. Worth noting to that card.
4. tests/unit/core/types/test_swarm_drafts.py:327 (diff L823): `ChatAnswer(numbers=[_ref()])` needs `# type: ignore[list-item]`. `ChatAnswer.model_validate({...})` would avoid the ignore.
5. herness/core/types/swarm/drafts.py (whole file) at exactly 300/300 lines, which leaves no headroom (see concern 4).

### Assessment
**Task quality:** Approved
**Reasoning:** All units U06-13 to U06-21 match the impl spec and rulings R-30, R-32, R-49 and D06-06. UT06-08 to UT06-10 pass with 100% coverage, and all gates are clean. check_type_ownership has no pending line for 06, and UT00-48 and UT00-80 pass. The four builder concerns are justified; the only follow-ups are doc deltas (the U00-45 name list and the UT06-09 row_key exception) and test polish.
