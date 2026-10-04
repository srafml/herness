# T05-19 review — Roles, part 1 (verify, head b307abe, base a46205f)

### Spec Compliance
- ✅ U05-49 RoleSpec: frozen dataclass with the 9 spec fields; prompt_text joins files with a blank line, read once per instance (cached_property `_prompts`); prompt_hash = sha256("\n".join(f"{file}\n{content}"))[:16] (verified by reading, see Minor 1); fallback_params -> RoleParams; system_blocks block 1 cache=True = prompt text + "## Output schema" canonical JSON of model_json_schema() (only with output_model) + "## Metric catalog" enabled metrics sorted by name (only when get_metric allowed); > 60,000 -> ConfigError; block 2 cache=False with role/specialty/depth/build/step budget/token budget/sorted tools from ctx (ctx.budgets per ruling). Missing file -> ConfigError("prompt file <f> missing").
- ✅ Invariant / req 1: system prompts come only from package prompt files (one private resolver `_prompts_root`, base.py:67) and code constants; no task text reaches block 1; block 1 has no run/task/build id or timestamp (all per-run values are in block 2). Task input and resume state only in render_task; scratchpad goes through wrap_untrusted(source="scratchpad") (base.py:171), escaped (UT05-90 asserts the injected `</untrusted_data>` is escaped and exactly one block exists).
- ✅ U05-50 (part 1): PlannerOutput(tasks list[PlannedTask] 1–200, rationale ≤ 4,000, unknowns ≤ 50), JudgeOutput(choice ≥ 0, scores 1–10 each 0–5, reasons ≤ 10, validator choice < len(scores)), AnalystOutput(summary ≤ 3,000, unknowns ≤ 50, suggested_followups ≤ 20); all extra="forbid", not strict. Planner only emits PlannedTask via the output model; nothing in roles executes anything. Output models are exposed only as RoleSpec.output_model; nothing in roles calls a client or validates model output itself, so complete_validated (spec 08) remains the only enforcement path.
- ✅ U05-51: ROLE_NAMES verbatim (no verifier_claim); get_role checks name, variant (retrospective only with writer), model_role allow-lists (skeptic/chat sets, others own default, analysts "analyst") before lookup; dataclasses.replace for a different model_role; ConfigError for every bad combo. Table values for planner, judge and the 7 analysts match U05-51 exactly (tools, 0.2/high/auto planner, 0.0/medium/off judge, 0.2/medium/auto analyst, model roles, prompt files `_common.md` first, specialties ops/change/delivery/org/crosscheck/retrospective/general). Allow-lists are frozenset code constants, ⊆ TOOL_OWNERS, and resolve through a fresh T05-16 ToolRegistry.resolve with strict tools (test_ut05_92_tools_resolve_strict). skeptic/writer/chat raise "role <name> not available" after preconditions, per ruling.
- ✅ Req 4: no LLM client / HTTP client import in herness/harness/roles (grep clean; no httpx/requests/aiohttp anywhere in herness/harness).
- ✅ tools.py RoleSpec swap: `_RoleLike` removed, TYPE_CHECKING import, resolve(role: RoleSpec, ...); tools.py 386/400; `from __future__ import annotations` present.
- ✅ Provisional ruling (UT05-124 `_NON_ROW_HASHES` + ("roles/base.py","prompt_hash")) — challenged and upheld: UT05-124 guards against a second *result-row* hash; prompt_hash is a spec-mandated SHA-256 over prompt file text (U05-49), not rows. The allow-list is the test's designed extension point ("reviewed allow-list") and the entry is narrowly scoped to one function. Without it tests/unit/harness fails. Accept.
- ⚠️ Cannot verify here: prompt .md files and the TH05-21 no-secret lint test (T05-21); skeptic/writer/chat table rows (T05-20); that load_catalog()'s default relative path `config/metrics.yaml` resolves in production cwd (controller ruling; spec literal).

### Evidence (run by reviewer)
- `pytest tests/unit/harness/roles` 53 passed; coverage roles pkg line+branch 100% (all five files).
- `pytest tests/unit/harness` 1240 passed, 1 skipped (Windows symlink privilege, pre-existing).
- ruff check / ruff format --check clean; `uv run mypy` (project config: herness, tools) 0 issues in 266 files; lint-imports 13 kept 0 broken; tools/check_module_size.py exit 0; tools/check_type_ownership.py exit 0.
- Sizes: base.py 218/220, planner 41/80, judge 45/80, analyst 80/80, tools.py 386/400.
- Test IDs in names/docstrings, pytestmark = unit in both files.
- Mutation probes (scratch script, originals restored; worktree clean at b307abe afterwards):
  killed — scratchpad not wrapped; judge `>=` -> `>`; run_id appended to block 1; disabled metrics included; skeptic_final dropped from allow-list; prompt files reversed.
  survived — prompt_hash algorithm changed to "".join(content) (no file names); hexdigest()[1:17]; 60,000 limit changed to 60,001.

### Strengths
- Clean precondition-then-lazy-lookup design; ConfigError messages truncate untrusted names ([:64]).
- find_spec guard keeps genuine import errors in existing role modules visible.
- Tests exercise real behaviour (escaping, exact block text, exact table values, strict resolve through the real registry).

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. tests/unit/harness/roles/test_roles_base.py:176-188 — prompt_hash algorithm is not pinned: two mutants (drop file names / change slice) survive. Add one assertion computing `hashlib.sha256("_common.md\n<c1>\ndemo.md\n<c2>".encode()).hexdigest()[:16]` and a test that renaming a file (same content) changes the hash.
2. tests/unit/harness/roles/test_roles_base.py:131-135 — the 60,000 boundary is not pinned (test overshoots by ~40 chars; `> 1 + _MAX` survives). Size demo.md so block 1 is exactly 60,000 (ok) and 60,001 (ConfigError).
3. herness/harness/roles/base.py:152 — render_task re-dumps with json.dumps default ensure_ascii=True, so non-ASCII task text becomes \uXXXX escapes (canonical_json itself uses ensure_ascii=False). Pass ensure_ascii=False for fidelity/token cost.
4. herness/harness/roles/base.py:72-74 — load_catalog() with its cwd-relative default path is re-read and re-validated on every system_blocks call; fine per ruling, but note for T05-21/T05-24 wiring (cwd dependence; could use the process-config catalog).
5. tests/unit/harness/roles/test_roles_registry.py:182 — `# type: ignore[arg-type]` is unused under strict mypy (tests are outside the project mypy files, so no gate fails).
6. tests/unit/harness/roles/test_roles_registry.py:224-234 — JudgeOutput negative score (< 0) not covered (only > 5).

### Assessment
**Task quality:** Approved
**Reasoning:** All U05-49/50/51 part-1 requirements, invariants and threat mitigations (TH05-07, TH05-21 scope, TH05-01 scratchpad) are met with passing gates and 100% coverage; remaining items are test-pinning and polish, none blocking.

## Re-review round 1

Scope: fix commit de33367 (vs b307abe) — base.py (+2/-1), test_roles_base.py, test_roles_registry.py.

| Finding | Status | Evidence |
|---|---|---|
| M1 prompt_hash algorithm pinned | Closed | `test_ut05_91_prompt_hash_algorithm` asserts `sha256("\n".join(f"{name}\n{content}"))[:16]` over `_common.md`/`demo.md`, and that a rename with identical content (same `prompt_text()`) changes the hash. |
| M2 60,000 boundary | Closed | `test_ut05_89_block_one_too_long` sizes demo.md so block 1 is exactly 60,000 (accepted, length asserted) then 60,001 (ConfigError "60,000"). |
| M3 render_task ensure_ascii=False | Closed | base.py:154-156 adds `ensure_ascii=False`, format otherwise unchanged (`indent=2, sort_keys=True`, `"## Task\n"` prefix). `test_ut05_90_render_task_non_ascii` asserts exact text with ö/—/東京 unescaped. |
| M4 catalog re-read (note only) | N/A | Informational for T05-21/T05-24; unchanged, as expected. |
| M5 unused type: ignore | Closed | Removed at test_roles_registry.py:182; `mypy --strict --warn-unused-ignores` on that file: no issues. |
| M6 negative judge score | Closed | `{"scores": [-0.5]}` added to the JudgeOutput rejection cases. |

Mutation re-run (throwaway edit of base.py, restored byte-identical, test_roles_base.py):
- hash without file names — KILLED
- slice `[1:17]` instead of `[:16]` — KILLED
- limit 60,001 — KILLED
- `>` → `>=` on the limit (extra) — KILLED
- `ensure_ascii=True` (extra) — KILLED

Gates: base.py = 220 lines (≤ 220). `pytest tests/unit/harness/roles tests/unit/harness/test_tools_recording.py` 174 passed; `ruff check` clean; `ruff format --check` 639 files formatted; `mypy` no issues (266 files). No regressions observed.

**Verdict:** Approved
