# T06-09 review (verify agent) — swarm tools, spawn broker, tool lists

Worktree agent-a7aeacdd88ea0c541, base e067d71, head 877f7f8. Read-only review; mutation probes restored byte-for-byte, `git status` clean.

### Spec Compliance
- ✅ U06-60 PostFindingTool — sync `Tool`, static name, strict schema (claim 1–1500, entity_type enum, entity_id 1–200, numbers 1–20 NumberRef, query_ids ≤50 QUERY_ID, confidence 0–1); result content/data/finding_ids/query_ids per spec; ToolInputError propagates.
- ✅ U06-61 ListFindingsTool — all FindingFilter fields except run_id, each nullable; bound run_id (extra keys → ToolInputError); chat mode `past_reader(entity_type, entity_ids, limit)`; line format `id | status | type:id | conf(2dp) | claim` + `numbers: nX=<v> <unit> (qid, column)`; stops before 12,000 chars (80-char reserve for the note), `truncated` set, ids list only what was shown.
- ✅ U06-62 RequestSubtaskTool — JSON `{approved,task_id}` / `{approved:false,reason}`, `ok=True` both ways, awaits only the broker decision (never the child).
- ✅ U06-63 EscalateTool — `{run_id, job_id}` JSON; ids never model inputs. Idempotency per message is delegated to the bound callable, as the spec states (U06-134 owns it).
- ✅ U06-64 build_task_tools — spec.tools ∩ four swarm tools; chat role gets chat-mode ListFindings; `ConfigError("tool <name> has no backend")`.
- ✅ U06-65 SpawnBroker/SpawnDecision — frozen decision; asyncio.Lock; rules 1–8 in design §5.5 order (spawn.py:131-162); child TaskSpec per step 9 (depth+1, budget scaled by child_budget_factor, priority ×0.9, round copied, k_samples 1, notes = reason[:1500]); insert via `bb.run_on_writer(run_write(insert_tasks))` (T06-08 writer API; no direct connection, no subprocess/multiprocessing); lost race → `duplicate:<id>`; wake only on approve; `spawn_decision` trace with the 9 structured fields (enum fields only when valid, never objective/reason text); `record_counter("herness_harness_spawn_decisions_total", labels={approved, reason-rule})`. Role is hard-coded `analyst`; specialty/entity_type outside the enums → `bad_scope`; child tools only from `default_tools` and must be ⊆ parent.tools.
- ✅ U06-97 role_prompt_name — `analyst_<specialty>`, others 1:1; checked against real spec 05 RoleSpec names.
- ✅ U06-101 default_tools / role_budget — every list matches U06-101 verbatim and sorted; `request_subtask` dropped when child_depth ≥ max_spawn_depth; crosscheck fixed list; writer without propose_memory; judge budget exact; writer = writer ledger tokens.
- ✅ UT06-43, UT06-44, UT06-45, UT06-46 (see Important #1 on order strength), UT06-47, UT06-64, UT06-68, ST06-02, ST06-03, ST06-17 — present, ID-named, docstrings, `pytestmark = pytest.mark.unit`, `--require-test-ids` passes.
- ⚠️ Spec deviations to record (all judged acceptable):
  1. `row_key` carried as nullable list (≤16) of `{column, value}` pairs and converted at the tool boundary (tools.py:67-77, 127-148). This is exactly the U05-10 / VI-7 sanctioned fallback; the in-tree `_strict_ok` (harness/_tools_schema.py:51-65) rejects any open object, so the fallback is forced regardless of the VI-7 outcome for Anthropic. Accept.
  2. NumberRef `format` (and `row_key`) required-but-nullable in the post_finding schema — required by R-26 (all properties required, optional = nullable); the model must send `format: null`. Accept; FakeLLM scripts / prompts must emit the `format` and `row_key` keys.
  3. Verifier budget max_tokens 1,000 instead of 1 (routing.py:49-51) — in-tree TaskBudget `ge=1000`; verifier makes no model call. Accept; U06-101 text should be amended.
  4. writer_tokens 1..999 → ConfigError (TaskBudget floor) and writer wall clock capped at 86,400 s (routing.py:101-106) — the cap is not in the spec; harmless bound. Accept.
  5. `default_tools("chat")` / `role_budget("chat")` → ConfigError — U06-101 has no chat entry; chat tools come from CHAT_TOOLS (§5.13). Accept.
  6. `build_task_tools(bb: Blackboard | None)` (spec: `Blackboard`) for chat; tracer typed as the `TraceEmitter` protocol; malformed args (dispatch bypassed) → `bad_scope`; approval metric label reason=`none`. Accept.

### Gates (run by verifier)
- `pytest tests/unit/harness/swarm -q -p no:logging`: 94 passed.
- Branch coverage: routing.py, spawn.py, tools.py all 100% line / 100% branch.
- ruff check: clean; ruff format --check: clean; mypy: 0 issues (286 files); lint-imports: 13 kept, 0 broken; check_module_size: exit 0 (tools 326/330, spawn 217/220, routing 106/330, __init__ 5/20).

### Mutation probes (each applied alone, the three card test files run, file restored byte-for-byte)
| Probe | Result |
|---|---|
| subset check removed (tool_escalation) | RED (UT06-46, ST06-02) |
| max_children cap dropped | RED (UT06-46, ST06-03 x2) |
| max_tasks cap dropped | RED |
| budget cap dropped | RED |
| max_depth dropped | RED |
| spawn_disabled dropped | RED |
| budget rule moved after bad_scope/dup/tools | RED |
| **rule 3/4 swap (max_tasks before max_children)** | **GREEN — survives** |
| **catalog bad_scope moved after duplicate** | **GREEN — survives** |
| asyncio.Lock removed | RED (concurrent burst) |
| wake on every decision / wake removed | RED / RED |
| objective leaked into trace | RED |
| metric reason keeps `:<id>` | RED |
| child budget factor / priority factor ignored | RED / RED |
| unknown specialty accepted | RED |
| additionalProperties false removed | RED (UT06-43, resolve, ST06-17) |
| required made partial | RED |
| denial returns ok=False | RED |
| list_findings extra-key guard removed | RED (ST06-17) |
| truncation limit doubled | RED |
| no ConfigError without backend | RED |
| request_subtask kept at child_depth == max_spawn_depth | RED |
| crosscheck gets full analyst list | RED |
| writer gets propose_memory | RED |
| writer_tokens check removed | RED |
| role_prompt_name changed | RED |
| list_findings bound run_id ordering swapped | GREEN — equivalent mutant (extra-key guard makes `run_id` unreachable) |

### Strengths
- Implementation is tight and correct against U06-65 and design §5.5; tests run on the real migrated store, real Blackboard writer, real EntityCatalog, real RunBudget, real metric buffer, real `check_tool_schema`, `ToolRegistry.resolve` and `dispatch`.
- ST06-03 includes a concurrent burst that genuinely proves the lock; the trace test asserts the full field dict.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. tests/unit/harness/swarm/test_swarm_spawn.py:143-188 — UT06-46 (the card's acceptance check "denial order matches design 06 §5.5") does not pin the full order. The docstring claims "every later rule also fails", but in the `spawn_disabled`/`max_depth` steps the children and task caps pass, in the `max_children` step `max_tasks` passes (default knobs), and in the `bad_scope` step (ids `["zz"]`) no duplicate exists. Probes: swapping rules 3 and 4, and moving the catalog `bad_scope` check after the duplicate lookup, both stay GREEN. The production order (spawn.py:131-162) is correct today, so this is test strength only. Fix: make every step fail all later rules — e.g. `max_children_per_task=0` plus a `max_tasks_per_run` at/below the current task count in steps 1–3, and insert a task whose dedup_key is the `["zz"]`-scope key so the `bad_scope` step is also a duplicate. Re-run the two probes above to confirm RED.

#### Minor (Nice to Have)
1. herness/harness/swarm/tools.py:151-154 — `_finding_query_ids` re-derives the finding's query ids from the arguments, duplicating blackboard.py:108-116 `_query_ids`; consistent today but two sources of truth.
2. tests/unit/harness/swarm/test_swarm_tools.py:379-396 — ST06-17 "binding used" only has findings in the bound run; seeding a finding in a second run and asserting it is absent would prove the binding rather than infer it.
3. herness/harness/swarm/tools.py:35-36 — `__all__` split over two statements; `render_findings` and the schema constants are used by tests but not in `__all__`.
4. herness/harness/swarm/spawn.py:138-156 — `count_tasks`/`get_task_by_dedup` are synchronous reads on the event-loop thread inside the lock; fine at spec volume (3 counts + 1 lookup), note for T06-13.
5. Pre-existing (not this card, from the report): tests/unit/harness/test_blackboard.py::test_st06_05_pii_checked_before_numerals_and_ids is flaky on random ULIDs containing "17" — track as a T06-08 follow-up.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Code is spec-compliant and all gates/coverage pass; 27 of 29 non-equivalent mutation probes go red. The one Important item is that UT06-46 — the card's named acceptance check — does not actually lock the rule order (two reorderings survive); a small test-only fix.

## Re-review round 1 (head 3e0aebb, test-only; diff 877f7f8..3e0aebb)

Scope: I-1, M2, M5 only. M3 parked by the controller (tools.py would reach 331 > 330), which is accepted.

- **Run:** `PYTHONUTF8=1 uv run pytest tests/unit/harness/swarm tests/unit/harness/test_blackboard.py -q -p no:logging` gives 131 passed. ruff check and ruff format are clean on tests/unit/harness; mypy reports 0 issues.
- ✅ **I-1 (UT06-46 rule order):**
  - Each step now fails its own rule and every later rule. It uses `no_children` (0 children allowed), `max_tasks_per_run=1` (the run already has more), `tokens_cap=1`, `["zz"]` ids (both unknown and an existing dedup key) and a narrow parent tool list.
  - Probes, each file restored byte for byte:
    - rule 3/4 swap: RED (previously GREEN)
    - catalog bad_scope moved after the duplicate lookup: RED (previously GREEN)
    - rule 1/2 swap: RED
    - rule 2/3 swap: RED
    - budget moved after bad_scope: RED
    - subset check removed: RED
- ✅ **M2 (ST06-17 binding):**
  - A second running run gets a real finding.
  - Dispatch refuses the `run_id` argument, and the valid listing returns only the bound run's finding (`other_fid` is absent from the ids and the content).
  - Calling with the other run's ctx still lists the bound run.
  - Probe: making ListFindingsTool read `ctx.run_id` turns ST06-17 RED.
- ✅ **M5 (ST06-05 flake):**
  - The test uses fixed ids `run_AAAA…` and `task_BBBB…` and a dedicated Blackboard, so no random ULID can contain "17".
  - The intent is preserved: the claim still holds the unmarked numeral "17" and the unknown entity "t9". The test asserts the PII message (so PII is checked before the numeral and id checks), asserts that no fragment appears in the error or logs, and asserts that no finding was written.
- **New findings:** none.
- Worktree left clean.

**Task quality:** Approved
