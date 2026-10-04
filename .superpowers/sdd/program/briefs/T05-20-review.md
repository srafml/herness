# T05-20 review — Roles, part 2 (skeptic, writer, chat)

Reviewer: verify agent (opus). Worktree agent-a2a0589b020bebead, base e067d71, head 9a0b79d. Working tree clean after probes.

### Spec Compliance
- ✅ U05-51 skeptic: 9 tools exactly per table, SkepticOutput, 0.5/high/auto, model_role skeptic, prompts (_common.md, skeptic.md). Test asserts frozenset equality (strict, no extras).
- ✅ U05-51 writer: list_findings, get_scores, get_metric, recall_memory only (no propose_memory, R-27, asserted); WriterOutput, 0.4/high/auto, writer. WRITER_RETROSPECTIVE = replace(WRITER, prompt_files + writer_retrospective.md); prompt_text test proves the append order.
- ✅ U05-51 chat: 12 tools incl. propose_memory and escalate; output_model is ChatAnswer (identity asserted); 0.3/medium/auto, chat.
- ✅ get_role model_role replacement: skeptic_final and chat_off_hours via dataclasses.replace (equality with replace(constant, model_role=...) tested), defaults, retrospective + model_role; existing bad-combination tests still cover disallowed ones.
- ✅ Every allowed tool resolves through ToolRegistry.resolve with strict schemas — tools-resolve test now parametrized over all ROLE_NAMES (06 task tools incl. escalate passed as task_tools).
- ✅ U05-50 SkepticOutput: checks list[CheckResult] (T06-01 type, imported), verdict Literal, exactly-one-per-SkepticCheck validator (sorted multiset compare); missing, duplicate, swapped-duplicate, empty and reordered cases tested.
- ✅ U05-50 WriterOutput / WriterRecommendation: create_model from RecommendationItem.model_fields minus rank, rec_id; field order preserved; extra=forbid, frozen, not strict (all models).
- ✅ Number-citation path: WriterRecommendation re-attaches `_refs_resolve` (decorators show `refs_resolve`); probe: unlisted expected_delta_ref / confidence_ref / action_lever delta_usd_ref all rejected "refs name no number"; Paragraph `_cited` (numbers need finding_ids) runs inside WriterOutput (probe). Mutation (drop `__validators__`) -> test_ut05_93_writer_recommendation_validation fails.
- ✅ UT05-93 schema equality: raw (title-stripped) diff shows only $defs naming (WriterRecommendation vs _WriterRecommendation) and alias-def inlining (_Title, _Caveat); the two recommendation def bodies are identical minus title. Normalizer does not strip description/additionalProperties/required (probes: each change is detected). Negative test (maxLength 201) is meaningful.
- ✅ Output models enforced through RoleSpec.output_model (consumed by complete_validated / schema hash in base.py:128).
- ✅ Budgets: skeptic 60/80, writer 79/140, chat 41/80, base 217/220; check_module_size rc 0; check_type_ownership rc 0.
- ✅ find_spec guard removal safe: all three modules exist; getattr(..., None) + "not available" ConfigError path retained.
- ✅ Test IDs: all new/changed tests carry UT05-92/UT05-93; --require-test-ids passes.
- ⚠️ TH05-16 / chat redaction: ChatAnswer is a T06-02 type and redaction is spec 10 / ChatService (spec 06) scope (design 05 §1 out-of-scope list). T05-20 owes nothing here beyond using ChatAnswer directly — satisfied.
- ⚠️ D05-18 remains "Still open" in the spec decisions table (not this card's to close).

### Gates run by reviewer
- tests/unit/harness/roles: 71 passed (--require-test-ids).
- ruff check + ruff format --check (roles src + tests): clean. mypy (11 files): 0 issues.
- Mutation probes: (1) drop re-attached validators -> 1 fail; (2) skeptic validator as set (duplicates allowed) -> 1 fail. Both restored; `git status --short` clean.

### Strengths
- Validator carry-over is generic and tested by behaviour, not by structure.
- Schema-equality normalizer is narrow and has a guard test; docstrings aligned so descriptions compare equal too.
- Registry test now covers all 12 roles against strict resolution.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. herness/harness/roles/skeptic.py:25-28 — SkepticOutput carries Challenge's field limits (stated rationale: fail at complete_validated where it retries, not at conversion) but not Challenge's `verdict == "revise" needs >= 1 required_action` rule (herness/core/types/swarm/tasks.py Challenge._complete). A revise-with-no-actions output passes SkepticOutput and then fails the swarm's Challenge construction — exactly the gap the rationale is meant to close. Either add that rule to `_one_per_check` (and a test) or document that the swarm handles it.
2. herness/harness/roles/writer.py:35-38 — only `model_validators` are carried from RecommendationItem; a future `field_validator` on `_WriterRecommendation` would be silently dropped and UT05-93 (schema-only) would not notice. Suggest a one-line test asserting `RecommendationItem.__pydantic_decorators__.field_validators` is empty (or carry them too).
3. Process note: builder amended its own unmerged wip commit (8df6788 -> 9a0b79d); local only, nothing branched from it. No action.

### Rulings
- Provisional ruling "SkepticOutput carries Challenge's limits beyond plain str/list[str]": UPHELD. Spec 06 D06-02 gives Challenge defaults precisely so the Skeptic output validates as a Challenge; the limits are the same contract, tighten the model-facing JSON schema, and move failure into the retryable complete_validated path. Condition: see Minor 1 (the rationale implies the revise rule too).
- Provisional ruling "WriterRecommendation re-attaches validator; WriterOutput limits follow writer_schema": UPHELD. Required for UT05-93 equality and for the number-citation invariant; verified by probe and mutation.

### Assessment
**Task quality:** Approved
**Reasoning:** Role table, output models, validators and UT05-93 equality all match the spec and are proven by behaviour tests that fail under mutation; only minor consistency hardening remains.

## Re-review round 1 (head 0e1a88c; scope: M1, M2 only)

- M1 (skeptic revise rule): ✅ RESOLVED. herness/harness/roles/skeptic.py:35-37 adds `verdict == "revise" and not required_actions -> ValueError`, same wording as Challenge._complete. New test test_ut05_93_skeptic_revise_needs_required_actions covers [] and omitted (default) actions, a valid revise, and uphold/reject with no actions. skeptic.py 63/80.
- M2 (field-validator guard): ✅ RESOLVED. test_ut05_93_recommendation_item_has_no_field_validators asserts RecommendationItem has no field validators.
- Gates: tests/unit/harness/roles 73 passed (--require-test-ids); ruff clean; project mypy gate (`mypy`, 286 source files) clean.

New findings (Minor):
1. tests/unit/harness/roles/test_roles_outputs.py (test_ut05_93_skeptic_output_fields, `ok` has verdict "revise" and no required_actions) — since the revise rule landed, every `bad` case except the required_actions ones now fails on the revise rule as well, so those cases no longer test what they name. Mutation probe: removing the finding_id `fnd_` pattern from skeptic.py:25 leaves the whole file green (10 passed). Fix: base `ok` on verdict "uphold" (or give it required_actions) in that test. File restored; worktree clean.
2. tests/unit/harness/roles/test_roles_outputs.py:185 — `for actions in ([], None)` gives mypy `var-annotated` when tests are type-checked explicitly (the project mypy gate does not cover tests, so no gate failure). Optional: annotate or restructure.

Re-review verdict: Approved (Minor 1 recommended before merge since it restores coverage of the finding_id limit that the upheld ruling depends on; cheap fix).
