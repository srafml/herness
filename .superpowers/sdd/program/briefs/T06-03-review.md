# T06-03 review — Pipelines configuration (commit e546005, base e3b4881)

### Spec Compliance
- ✅ U06-22 PipelinesConfig: all §9 keys, types, defaults and bounds match settings.py and config/pipelines.yaml line by line (swarm.* incl. dedup/coverage/skeptic/crosscheck; depth.<d>.* incl. analyst_budget and deep.large_stage; pipelines.funding_review/org_review/chat; hybrid.*). Every config sub-model inherits `_Model` (extra=forbid, frozen, strict); DepthConfig's merged config verified at runtime = {extra: forbid, frozen: True, strict: True}. The `depth` model validator requires exactly fast/standard/deep. `PipelinesConfig() == load(shipped)` is asserted.
- ✅ U06-23 DepthKnobs / KSamples: fields, bounds and defaults verbatim; frozen; KSamples 1-9 with on_reject >= default; int n -> (n,n), `{reject: k}` -> (1,k), bool/other rejected.
- ✅ U06-24 resolve_knobs: steps 1-4 in order; `analyst_budget` -> TaskBudget with max_cost_usd 0; per-pipeline keys at [depth] plus window_days; structural keys (analyst_budget, k_samples, org_specialties, large_stage) and non-fields rejected with the exact message `unknown budget_override key: <key>`; bool/float/str/negative rejected with `budget_override <key> must be a non-negative int`; keyword-only override; pure.
- ✅ D06-34 / module map: `pipelines/__init__.py` imports nothing eagerly (PEP 562 lazy map, empty per ruling); subprocess test proves importing settings loads no other herness.harness module; settings.py added to the "settings modules are leaves" contract.
- ✅ UT06-11 (shipped file loads; unknown key -> ValidationError with loc path; 20 bound violations), ✅ UT06-12 (`3` -> (3,3), `{reject: 3}` -> (1,3)), ✅ UT06-13 (standard funding + max_tasks_per_run 60 -> 60, K_candidates 25; analyst_budget -> ConfigError), ✅ ST06-15 (structural + unknown keys, bad values -> ConfigError). All functions carry IDs, ID-leading docstrings, module `pytestmark = pytest.mark.unit`.
- Build concerns judged on merits: (1) wrapping DepthKnobs ValidationError as ConfigError — accepted (the override is user input on TB10; callers see a HernessError; Errors row covers only step 3 and a raw pydantic error would be worse). (2) all three depths required in per-pipeline maps — accepted (sound; otherwise K_* silently 0). (3) chat.escalation load-time allowlist check — accepted, it is what "keys per U06-24" means. BudgetSettings.to_task_budget — accepted but see Important-1. strict=False on hybrid.max_cost_usd_per_run — accepted (YAML int 15 must load; bool still rejected). Imports from herness.core.types root — accepted (OWN041).
- ⚠️ settings.py 323 lines > 260 budget: check_module_size MS001 fails (IT00-02 fails on this branch) until the ruled raise to 330 is committed in the docs. Known, not Critical.
- ⚠️ Acceptance check `herness config validate` accepts the shipped file — cannot run until T10-03/T10-14 (ruled deferral).

Gates re-run by reviewer: ruff format/check clean; mypy clean; lint-imports 10 kept 0 broken; check_type_ownership exit 0; focused pytest 45 passed; coverage (branch) __init__.py 100 %, settings.py 100 % (173 stmts, 24 branches).

### Strengths
- Exact §9 parity, with in-code defaults proven equal to the shipped YAML.
- One allowlist helper (`_override_problem`) shared by load-time escalation check and resolve-time check (DRY, TH06-15 enforced at both points).
- Good negative-test breadth (bool as int, float, str, key paths, missing/extra depth keys, lazy package).

### Issues
#### Critical (Must Fix)
- none

#### Important (Should Fix)
1. herness/harness/pipelines/settings.py:99-106 and :311 — `BudgetSettings` only enforces `>= 1` (§9), but `TaskBudget` (herness/core/types/swarm/tasks.py:113-116) requires max_steps <= 200, max_tokens >= 1000, wall_clock_s <= 86400. A pipelines.yaml with e.g. `analyst_budget.max_tokens: 500` (or chat.budget likewise) passes load/`config validate`, then every `resolve_knobs` call raises a raw `pydantic_core.ValidationError` (for TaskBudget) — reproduced. The call at :311 sits outside the `try` at :318, so the report's claim that a config budget below TaskBudget bounds is re-raised as ConfigError is false. Fix: make config load reject it (e.g. a model validator on BudgetSettings that calls `to_task_budget()` so load fails with the key path, keeping §9 ">= 1" as the floor and TaskBudget as the binding bound), and add a UT06-11 case; optionally also cover :311 by the ConfigError wrap.

#### Minor (Nice to Have)
1. herness/harness/pipelines/settings.py:237, :207-210, :170 — `frozen=True` does not freeze container values: `cfg.pipelines.chat.escalation["x"] = 1` succeeds on the process-wide config (verified), as do per-depth maps and org_specialties lists; a caller mutation would bypass the TH06-15 load-time check. Consider `Mapping`/tuple types or `MappingProxyType` for escalation at least.
2. herness/harness/pipelines/settings.py:322 — message "budget_override gives invalid knobs" is emitted even when `override` is None (only reachable if config and knob bounds diverge); wording could say "knobs invalid" or include the override keys.
3. herness/harness/pipelines/settings.py:254 — lax Decimal also accepts strings such as "15.123456" (verified); harmless but §9 type is "decimal"; could restrict to int/float/Decimal or quantize to cents.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Spec surface, tests, coverage and gates are all correct except the known module-size overrun; one Important gap — config validation accepts analyst/chat budgets that `TaskBudget` rejects, so `resolve_knobs` later fails with an uncaught non-Herness ValidationError.

---

## Re-review round 1 (commit 7654d99, diff e546005..7654d99)

### Findings status
- ✅ Important-1 resolved. At herness/harness/pipelines/settings.py:99-104, `BudgetSettings` now carries the `TaskBudget` bounds (max_steps 1-200, max_tokens >= 1000, wall_clock_s 1-86400). This applies to both `depth.<d>.analyst_budget` and `pipelines.chat.budget`, so an out-of-range budget now fails at load with its key path. Because every loaded budget is valid, `to_task_budget()` at :318 can no longer fail, so it can stay outside the `try`. Tests cover this: `test_ut06_11_budget_below_task_budget_bounds_rejected_at_load` asserts the loc path, and three new parametrized bound cases were added (deep max_steps 201, wall_clock_s 86401, chat max_tokens 500). The fields are still at least 1 as §9 requires; the extra bounds come from the binding U06-04 type, so this is not a spec deviation.
- ✅ Minor-2 resolved (settings.py:329). The message is now `invalid <kind> <depth> knobs: <paths>` and the docstring was updated to match.
- ✅ Minor-3 resolved (settings.py:250-261). The field is strict again, and the `_usd` BeforeValidator accepts only int, float or Decimal and converts through `Decimal(str(v))`. I verified by probe that "15.123456", True, inf and nan are all rejected, and that the default stays `Decimal('15')`. `test_ut06_11_cost_cap_accepts_numbers` covers the accepted inputs.
- Minor-1 (mutable containers inside frozen models): **parking accepted**. The finding is Minor. U06-22 and U06-23 declare `dict[Depth, ...]` and `list[Specialty]` field types, the config object is internal and no code path mutates it today, and fixing it would push the file past the 330 cap. Suggested follow-up (non-blocking): when ChatService (a later card) consumes `chat.escalation`, pass it through `resolve_knobs` (which re-checks the allowlist) rather than trusting the stored dict. It already does this by design.

### Gates (re-run by reviewer)
- ruff format and check are clean, mypy reports no issues, lint-imports shows 10 contracts kept and 0 broken, and check_type_ownership exits 0.
- `pytest tests/unit/harness/pipelines`: 52 passed. Branch coverage is 100 % for both `__init__.py` (14 statements) and `settings.py` (178 statements, 26 branches).
- ⚠️ settings.py is 330 lines, exactly at the ruled cap. MS001 and IT00-02 stay red until the docs commit raises the §2 budget to 330. This is known and not a blocker. The file now has no headroom left, so later cards that add to it will need a split.

### No new issues introduced.

### Assessment
**Task quality:** Approved
**Reasoning:** The fixes resolve Important-1 and Minor-2/3 at load time and give correct key paths, with tests that assert the behaviour. Parking Minor-1 is justified. The only open item is the pending module-budget docs commit.

---

## Regenerated-brief check (HEAD 2fd84bb; T06-03 final 7654d99)

I re-read the Unit specs and Test specs sections of the regenerated brief in full. It now contains U06-22, U06-23, U06-24 and UT06-11, UT06-12, UT06-13, ST06-15. Their text is identical to impl 06 (lines 380-388, 2129-2131), which is what the original review and re-review already checked against: the original review read U06-23 and UT06-12 directly from the spec.

HEAD is 2fd84bb. The two T06-23 commits after 7654d99 only add `herness/harness/pipelines/chat_support.py` and `tests/unit/harness/pipelines/test_chat_support.py`. Nothing under settings.py, `__init__.py`, config/pipelines.yaml or test_pipelines_settings.py changed (`git diff --stat 7654d99 HEAD`).

| Item | Status | Evidence |
|------|--------|----------|
| U06-22 PipelinesConfig | ✅ | Fields version/swarm/depth/pipelines/hybrid, all §9 rows, and extra=forbid, frozen, strict on every sub-model. k_samples is converted by a field validator. `depth` must have exactly the three keys. Violations raise ValidationError with the key path. |
| U06-23 DepthKnobs, KSamples | ✅ | Every DepthKnobs field, bound and default is verbatim, and the model is frozen. KSamples is 1-9 with on_reject >= default. An int n becomes (n,n) and `{reject: k}` becomes (1,k). |
| U06-24 resolve_knobs | ✅ | The signature matches, with `override` keyword-only. Steps 1-4 are in order and both ConfigError messages are exact. The function is pure. Wrapping a step-4 ValidationError in ConfigError was accepted in the first review. |
| UT06-11 | ✅ | `test_ut06_11_*`: the shipped file loads, and an unknown key raises an error carrying its loc path. |
| UT06-12 | ✅ | `test_ut06_12_int_k_samples_normalizes_to_pair` gives (3,3); `test_ut06_12_reject_k_samples_normalizes_to_pair` gives (1,3). |
| UT06-13 | ✅ | `test_ut06_13_standard_funding_with_override` gives 60 and K_candidates 25; `test_ut06_13_analyst_budget_override_rejected` raises ConfigError. |
| ST06-15 | ✅ | `test_st06_15_structural_or_unknown_override_key` (analyst_budget, unknown keys) raises ConfigError. |

At HEAD, `pytest -k "UT06_11 or UT06_12 or UT06_13 or ST06_15"` gives 52 passed. All test IDs are present, with docstrings that start with the ID and a module-level `pytestmark`.

**Findings:** none. No mismatch and no missing test ID.

**Verdict:** Approved. This is unchanged from Re-review round 1, and the module-budget item there still applies until the budget docs commit lands.
