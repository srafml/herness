# T05-19 report — Roles, part 1 (RoleSpec, get_role, planner/judge/analyst)

Worktree: D:\herness\.claude\worktrees\agent-af5bff587a444053c (branch worktree-agent-af5bff587a444053c, base a46205f)
Checkpoint: ab5a647 wip(T05-19): RoleSpec, get_role, planner/judge/analyst with tests
Final commit: b307abe feat(harness): roles part 1 — RoleSpec, get_role, planner/judge/analyst (T05-19)

## Implemented
- herness/harness/roles/base.py (218/220): RoleSpec frozen dataclass (fields per U05-49), prompt_text
  (files joined with a blank line, read once per instance via functools.cached_property on private
  `_prompts`), prompt_hash (16 hex SHA-256 over "\n".join(f"{file}\n{content}")), fallback_params
  (RoleParams from herness.harness.llm.settings), system_blocks (block 1 cache=True: prompt text +
  "## Output schema" canonical JSON of model_json_schema() when output_model set + "## Metric
  catalog" enabled metrics sorted by name `name — unit, better, grains; description` when get_metric
  in allowed_tools; > 60,000 chars -> ConfigError. Block 2 cache=False: role/specialty/depth/build/
  step budget (ctx.budgets.max_steps)/token budget (ctx.budgets.max_tokens)/sorted tools),
  render_task (one user Message; part 1 "## Task\n" + sorted-key 2-space JSON validated through
  canonical_json; part 2 when resuming: fixed restart sentence, query_ids, finding_ids with "do not
  post them again", scratchpad through wrap_untrusted(source="scratchpad")). ROLE_NAMES verbatim
  (no verifier_claim). get_role: preconditions first (name in ROLE_NAMES; variant retrospective only
  for writer; model_role allow-list skeptic->{skeptic,skeptic_final}, chat->{chat,chat_off_hours},
  others -> own default, analysts -> "analyst"), then lazy lookup (importlib; module absent or
  constant absent -> ConfigError("role <name> not available")), then dataclasses.replace for a
  non-default model_role.
  Private seams for tests: `_prompts_root()` (the one prompt resolver, importlib.resources
  files("herness.harness.roles")/"prompts"), `_catalog_describe()` (load_catalog().describe()),
  `_catalog_lines()`, `_LOCATIONS` (role -> module/constant table).
- planner.py (41/80): PlannerOutput(tasks: list[PlannedTask] 1-200, rationale <= 4,000, unknowns <= 50),
  PLANNER (tools list_tables, describe_table, get_scores, get_metric, recall_memory; 0.2/high/auto; planner).
- judge.py (45/80): JudgeOutput(choice >= 0, scores 1-10 items each 0-5, reasons <= 10; validator
  choice < len(scores)), JUDGE (no tools; 0.0/medium/off; judge).
- analyst.py (80/80): ANALYST_SPECIALTIES (7), AnalystOutput(summary <= 3,000, unknowns <= 50,
  suggested_followups <= 20), analyst_role(specialty) over a MappingProxyType of 7 RoleSpecs
  (analyst_<s>, prompts _common.md + analyst_<s>.md, 13 tools, 0.2/medium/auto, model role analyst);
  unknown specialty -> ConfigError.
- roles/__init__.py (5 lines, new, no budget row): re-exports ROLE_NAMES, RoleSpec, get_role only.
  T05-20 may add more.
- Output models: extra="forbid", frozen, not strict.
- tools.py 392 -> 386: `_RoleLike` Protocol deleted; `if TYPE_CHECKING: from herness.harness.roles.base
  import RoleSpec`; resolve(role: RoleSpec, ...). Unused `Collection` import dropped. (Closes the T05-16
  carry-over.) _tools_dispatch.py untouched.
- tests/unit/harness/test_tools_recording.py: added ("roles/base.py", "prompt_hash") to the UT05-124
  reviewed `_NON_ROW_HASHES` list (the spec-mandated prompt version hash is not a row hash).

## Tests
- tests/unit/harness/roles/test_roles_base.py: UT05-89 (block 1 cached, no run/task/build id or
  date; exact schema + catalog sections; optional sections omitted; > 60,000 -> ConfigError; real
  config/metrics.yaml catalog lines), UT05-90 (resume part lists ids, one escaped
  `<untrusted_data source="scratchpad" record_id="">` block; plain / no-scratchpad cases), UT05-91
  (prompt_hash stable/changes with tmp copy content, cached per instance; missing file ConfigError;
  fallback_params; resolver points at "prompts").
- tests/unit/harness/roles/test_roles_registry.py: UT05-92 (ROLE_NAMES, table values for planner,
  judge, the 7 analysts; default model_role override; 13 bad combos incl. verifier_claim, bad variant,
  disallowed model_role incl. skeptic/chat allow-lists; skeptic/writer/chat -> "not available";
  allowed replace path via monkeypatched `_LOCATIONS`; missing constant; unknown specialty; every
  part-1 role's allow-list subset of TOOL_OWNERS and resolved through a fresh ToolRegistry with strict
  stub tools (05/07 registered, 06 as task tools); PlannerOutput/JudgeOutput/AnalystOutput limits).
- RED: `uv run pytest tests/unit/harness/roles -q` -> "ModuleNotFoundError: No module named
  'herness.harness.roles'", 2 errors during collection.
- GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/harness/roles -q` -> 53 passed.
  `pytest tests/unit/harness -q` -> 1240 passed, 1 skipped (symlink privilege), after the UT05-124 list update.
- Coverage (roles package, line+branch): __init__ 100%, base 100%, planner 100%, judge 100%, analyst 100%.

## Gates
ruff format/check clean; mypy strict 0 issues (266 files); lint-imports 13 kept 0 broken;
check_module_size exit 0; check_type_ownership exit 0; pre-commit hooks passed on the wip commit.
No import-linter / pyproject change needed (herness.harness already in the layer contracts; roles
imports metrics (L3) and core only; no llm client/registry import; no HTTP client).

## Deviations / spec notes
- Metric catalog line: grains joined with ", " (spec gives `grains` without a separator).
- "## Output schema" and "## Metric catalog" headings are omitted together with their content.
- Task JSON: canonical_json validates the input (non-finite/unsupported values raise), then
  json.dumps(indent=2, sort_keys=True) renders it (ensure_ascii default).
- fallback_params uses RoleParams.model_validate (RoleSpec.effort is `str | None` per spec; RoleParams
  narrows it to a Literal, so an invalid effort would raise a ConfigError-free ValidationError).
- get_role's lazy lookup checks importlib.util.find_spec before import, so a genuine import error
  inside an existing role module still propagates.
- analyst.py sits exactly at its 80-line budget.

## Carry-overs
- T05-20: add skeptic.py/writer.py/chat.py (SKEPTIC, WRITER, WRITER_RETROSPECTIVE, CHAT; `_LOCATIONS`
  already maps them) and extend UT05-92 (the "not available" parametrization then flips to table checks).
- T05-21: prompt .md files under herness/harness/roles/prompts/ + package data (the resolver reads
  files("herness.harness.roles")/"prompts"); TH05-21 prompt no-secret lint test.
- tests/unit/harness/test_tools_registry.py still uses its local `Role` stand-in (works structurally at
  runtime); may switch to RoleSpec later.
- tools.py RoleSpec swap done — T05-16 carry-over closed.

## Review round 1 (fix commit: de33367 fix(harness): T05-19 review round 1 — prompt hash pin, 60k boundary, non-ASCII task JSON)
- M1: new test_ut05_91_prompt_hash_algorithm pins sha256("\n".join(f"{file}\n{content}")).hexdigest()[:16]
  and checks that renaming a file (same content, same prompt_text) changes the hash.
- M2: test_ut05_89_block_one_too_long now checks block 1 at exactly 60,000 chars (accepted) and 60,001 (ConfigError).
- M3: render_task JSON uses ensure_ascii=False (consistent with canonical_json); new
  test_ut05_90_render_task_non_ascii. base.py now 220/220.
- M5: unused `type: ignore[arg-type]` removed from test_roles_registry.py.
- M6: negative judge score case (-0.5) added to test_ut05_92_judge_output_model.
- M4 parked (no change).
- Gates: ruff/mypy clean, check_module_size exit 0; roles tests + test_tools_recording 174 passed;
  roles package coverage 100% line/branch.
