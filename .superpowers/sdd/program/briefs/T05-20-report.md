# T05-20 report — Roles, part 2 (skeptic, writer, chat)

Status: DONE_WITH_CONCERNS (minor, see Deviations)
Worktree: D:\herness\.claude\worktrees\agent-a2a0589b020bebead (base e067d71)
Commits: wip 8df6788 amended into final 9a0b79d feat(harness): T05-20 roles part 2 — skeptic, writer, chat (same content; hooks passed again)

## Files (lines vs budget)
- herness/harness/roles/skeptic.py 60/80 — SkepticOutput + SKEPTIC
- herness/harness/roles/writer.py 79/140 — WriterRecommendation (create_model), WriterOutput, WRITER, WRITER_RETROSPECTIVE
- herness/harness/roles/chat.py 41/80 — CHAT (output_model=ChatAnswer)
- herness/harness/roles/base.py 220 -> 217/220 — removed the importlib.util.find_spec guard in _lookup (modules now exist); `import importlib.util` dropped. getattr(..., None) + "role X not available" ConfigError kept for a module lacking its constant.
- tests/unit/harness/roles/test_roles_outputs.py (new, UT05-93, 8 tests)
- tests/unit/harness/roles/test_roles_registry.py (UT05-92 extended: former "not available" parametrized test replaced by skeptic/writer/chat table checks, model_role replacement skeptic_final / chat_off_hours + defaults, retrospective variant (+ with model_role), retrospective prompt text via monkeypatched _prompts_root, tools-resolve test now over all ROLE_NAMES incl. escalate/06 task tools)
No pyproject/import-linter change needed (roles package already covered). No _roles_common.py.

## Role table (verbatim U05-51)
skeptic: 9 tools, SkepticOutput, 0.5/high/auto, skeptic; writer: list_findings,get_scores,get_metric,recall_memory, WriterOutput, 0.4/high/auto, writer; retrospective = replace(WRITER, prompt_files + writer_retrospective.md); chat: 12 tools incl. propose_memory, escalate, ChatAnswer, 0.3/medium/auto, chat.

## UT05-93 schema equality — RESULT: EQUAL
Normalizer (test-local): drops `title` keys only where the value is a string (schema annotations; a property named `title` is kept), and inlines every `$ref` from `$defs` (so def names — `_WriterRecommendation` vs `WriterRecommendation`, `_Title`/`_Caveat` aliases vs inline constraints — no longer matter). Descriptions are NOT stripped. A guard test shows a changed limit (title maxLength 201) still makes the schemas differ.
Before equality: the only remaining differences were `description` of the root and of the recommendation def (docstrings). Fixed by giving WriterOutput and WriterRecommendation (create_model __doc__) the spec 06 docstrings verbatim — the schema text the model sees is now identical; UT05-93 catches future drift.
Note: $ref inlining is slightly broader than "renaming $defs names" (it also equates `$ref` to a type-alias def with the same constraints inline) — representation only, no semantic difference hidden.

## Deviations / choices
1. WriterRecommendation carries RecommendationItem's model validators (the dangling-ref check `_refs_resolve`) via create_model __validators__, read from `RecommendationItem.__pydantic_decorators__.model_validators` (pydantic public-ish class attr). create_model from model_fields alone would silently drop it (TH05-16). If RecommendationItem ever gains a rank-dependent model validator this would break — tested.
2. SkepticOutput field limits tightened beyond the spec signature to the spec 06 Challenge limits it converts into: finding_id pattern ^fnd_<ULID>$, required_actions items <= 400 chars, <= 10 items. Spec says `finding_id: str`, `required_actions: list[str] = []`. Rationale: a SkepticOutput the swarm cannot turn into a Challenge should fail at complete_validated (retryable) rather than later. Controller may ask to revert to plain str.
3. WriterOutput.title/caveats use the writer_schema limits (title 1–200, caveat <= 600) — required for schema equality (spec says title <= 200, caveats list[str]).
4. `WriterRecommendation` is typed as BaseModel under TYPE_CHECKING (dynamic class from create_model), runtime is the created class.

## Tests / gates (head 8df6788 content)
- tests/unit/harness/roles: 71 passed, --require-test-ids ok; roles coverage 100% line / 100% branch (all 8 modules).
- RED: before the modules existed, collection failed `ModuleNotFoundError: No module named 'herness.harness.roles.chat'` (both test files).
- PYTHONUTF8=1 uv run pytest tests/unit/harness -q -p no:logging: 1399 passed, 1 skipped (symlink privilege).
- ruff check pass; ruff format --check pass (721); mypy 0 (286 files); lint-imports 13 kept 0 broken; check_module_size 0; check_type_ownership 0; pre-commit hooks all passed on commit (incl. pytest-unit).

## Concerns
- Deviation 2 (SkepticOutput tighter than spec signature) needs a ruling.
- D05-18 stays "Still open" in the spec decisions table (not touched).

## Review polish round 1 (review Approved, rulings upheld)
- M1: SkepticOutput adds Challenge's rule "a revise verdict needs at least one required action" (same message); skeptic.py 63/80. Test test_ut05_93_skeptic_revise_needs_required_actions (revise with []/missing rejected, revise with one action accepted, uphold/reject with none accepted).
- M2: guard test test_ut05_93_recommendation_item_has_no_field_validators (RecommendationItem.__pydantic_decorators__.field_validators empty).
- Gates: roles tests 73 passed (--require-test-ids), roles cov 100/100; ruff check/format clean; mypy 0 (286); check_module_size exit 0.
- New commit on top of 9a0b79d (not amended): fix(harness): T05-20 review round 1 — skeptic revise rule, validator guard.

## Review fix round 2 (re-review round 1 Approved; test-only)
- N1: test_ut05_93_skeptic_output_fields now bases `ok` on a valid revise (required_actions ["re-run q"]) and asserts each bad case's errors point only at the intended field (loc[0] == verdict / finding_id / required_actions / required_actions / round / checks). Mutation probe: dropping the fnd_ pattern at skeptic.py:25 -> test_ut05_93_skeptic_output_fields FAILS (mutant killed); file restored with git checkout (git status clean for skeptic.py).
- N2: `actions: list[str] | None` annotation before the loop; mypy on test_roles_outputs.py clean.
- Gates: roles tests 73 passed (--require-test-ids); ruff check/format clean. No production code change.
