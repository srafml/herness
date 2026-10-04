### Spec Compliance
- ❌ Issues found: one test does not verify the requirement it claims to cover. `test_it06_34_job_payload_and_session_errors` passes for the wrong reason, so the U06-136 step 1 payload check has no working test (Important 1). Every unit's behaviour matches the spec text.
- Per unit:
  - ✅ U06-127 `ChatService` / `ChatDeps` (chat.py:125-180). It has the spec signature, and `deps` is additive. `ChatDeps` adds `ops`, `vectors` and `past_reader` (R3). Only `ChatService` writes assistant rows: `upsert_assistant_placeholder` and `update_chat_message` are called only from chat.py, `_chat_rows.py` and `_chat_turn.py`.
  - ✅ U06-128 `answer` (chat.py:182-195). It reads `latest_user_message`; when there is none it yields `ErrorEvent("NotFound","no user message to answer",None)`. `text` is deleted unused and the model gets `msg["content"]`. Probe P23 confirms that sending anything else to the model fails IT06-11.
  - ✅ U06-129 `_answer_message`, steps 1-6:
    - 1: placeholder; None → NotFound; reply `done` → no events (chat.py:201-211).
    - 2: defer: `enqueue("chat", {...}, gpu_class="reasoning", priority=None, idem_key="chat:s:m")`, then `queued` with meta, `ModeEvent`, INFO log (chat.py:266-275).
    - 3: cloud gate through `data_policy_allows_cloud(hcfg.profile)`, which defaults to `cloud_chat_allowed(hcfg)` (chat.py:155-156, 215-217).
    - 4: worker thread runs `asyncio.run`; `Queue(maxsize=1000)`; `None` sentinel; `get(timeout=wall_clock_s+30)` → `ModelUnavailable` "chat turn timed out" with hint "Try again later."; generator close runs `finally: stop.set()` (chat.py:218-254).
    - 5a: run (`chat`, `fast`, `running`, redacted request meta, `session_id`) and one `chat` task in one `run_write(op="chat_create_run")` (_chat_rows.py:123-150).
    - 5b: `session_load`, `prior_context(MemoryRunContext(...))` (_chat_turn.py:149-154).
    - 5c: `chat_model_profile(mode)`, model role `chat_off_hours`/`chat`, `egress_purpose` "reasoning" when off-network (payload class `aggregated_evidence` is fixed by the off-network adapters), `RunBudget("chat", tokens_cap, run_id)`, `build_gates(mode="chat")`, observed tools (R1, see below) (_chat_turn.py:200-256).
    - 5d: `run_agent` with `HarnessHooks(chain=None, on_text_delta=None)`; `EgressBlocked` in cloud → one local rerun with `EGRESS_NOTICE` (_chat_turn.py:185-197, 283-291).
    - 5e: service escalation on `ESCALATE_STOPS` plus review intent, once per turn under a lock (_chat_turn.py:161-162, 264-280).
    - 5f: `ChatAnswer` and `TokenEvent`s from `chunk_text(render(...))`; `render` composes `parse_markers` and `format_number` in place, as U06-125 says (_chat_rows.py:114-120).
    - 5g: verify in a thread; `ConfigError`/`QueryError` → `unverified`; one repair, then re-verify, then trim → `partial` (_chat_turn.py:294-327).
    - 5h-5j: `VerificationEvent`, reply `done` with rendered content/verified/run_id/query_ids/meta, `complete_task` with run `done`, `FinalEvent`, INFO `turn_completed`, both metrics (_chat_turn.py:169-181, _chat_rows.py:157-185).
    - 5k: after final, unless `unverified`, `session_save_turn` → `CorrectionCapturedEvent` + INFO log; a `HernessError` → WARNING `session_save_failed`, no ErrorEvent (_chat_turn.py:136-137, 330-340).
    - 6: `HernessError` in 5a-5j → ErrorEvent with the hint rule, reply `failed`, run `failed`, ERROR `turn_failed` with `error_type` only (_chat_rows.py:188-208).
  - ✅ U06-136 `chat_job_handler` (chat.py:286-312): payload checked (but untested, see Important 1); unknown session → `NotFound("chat session not found")`; service built from `get_config()`; consumes `_answer_message(...,"live")`; returns `JobOutcome("done", {"run_id","status"})`. A second run makes no model call (P12 killed).
- Per test row (each ID is in the function name; RED confirmed):
  - ✅ IT06-11 (6 fns)
  - ✅ IT06-12
  - ✅ IT06-13 (4)
  - ✅ IT06-14 (2)
  - ✅ IT06-23 (4 modes)
  - ✅ IT06-34 (3), but the payload half is vacuous (Important 1)
  - ✅ IT06-36 (2)
  - ✅ ST06-11 (4)
  - ✅ ST06-12 (4)
  - ✅ ST06-14
- RED: I put the base pipelines back (`git checkout 3e8c337 -- herness/harness/pipelines/` plus removing the 3 new files). Both card test files then fail collection with `ModuleNotFoundError: No module named 'herness.harness.pipelines.chat'` ("2 errors in 0.28s"). Restoring with `git checkout HEAD -- herness/harness/pipelines/` left a clean tree. This matches the report.
- GREEN: `pytest tests/integration/harness/test_chat_service.py tests/security/test_st06_chat.py` → 31 passed in 311 s.
- Acceptance checks:
  - ✅ IT06-23: exactly one assistant row in live, small_model, cloud and defer (P11 writes a second row: killed).
  - ✅ IT06-36: `correction_captured` follows `final` (P13 captures before final: killed).
- Budgets:
  - ✅ chat.py 312/390, pipelines/__init__.py 35/40, _chat_turn.py 340/360, _chat_rows.py 208/220 (new §2 rows at docs/impl/06:111-112).
  - ✅ `tools.check_module_size` exit 0.
- Static gates on the head:
  - ✅ `lint-imports`: 15 kept, 0 broken.
  - ✅ `ruff check .`: all passed.
  - ✅ `ruff format --check .`: 1112 files formatted.
  - ✅ `mypy`: no issues in 397 files.
  - ✅ `check_type_ownership`: exit 0.
- ✅ Lazy package: `import herness.core.config` loads only `herness.harness`, `.llm`, `.llm.settings`, `.memory`, `.memory.settings`, `.pipelines` and `.pipelines.settings`. This is identical at the base and the head. `ChatService` is added to the lazy `_EXPORTS` only.
- Coverage (card tests, `--cov-branch`):

  | Module | Line | Branch |
  |---|---|---|
  | chat.py | 96.2 % (150/156) | 100 % (20/20) |
  | _chat_turn.py | 98.9 % (188/190) | 92.9 % (39/42) |
  | _chat_rows.py | 98.0 % (99/101) | 90.0 % (9/10) |

  All three are at or above 90 % line and 85 % branch.
- ⚠️ Cannot verify:
  - IT06-14 "mini run ≤ 8 tasks" is not executed, because the swarm run driver is not on the base. The test asserts the resolved knobs (`max_tasks_per_run <= 8`), one `review` job, the `escalated` event and the posted summary (test_chat_service.py:412-451).
  - IT06-13 "hybrid with planted PII tool result": the scripted cloud client raises `EgressBlocked` itself (test_chat_service.py:356), so the real egress guard's PII detection is not exercised. The fallback path is.

### Strengths
- The thread/queue hand-off is correct and probe-tight. Removing the get timeout (P01), the close-time stop (P02), the hooks stop flag (P03) or the turn backstop (P04) each fails ST06-11.
- The cloud gate is enforced at both layers: the mode downgrade and the default `allows_cloud` rule (P07 and P08 killed). When downgraded, `chat_model_profile("cloud")` is never asked for and the off-network client is never called.
- Logging discipline: no log call in chat.py, _chat_turn.py or _chat_rows.py carries question or answer text, only ids, mode, status, latency and error_type (checked by grep; P09 and P10 killed). Error messages reach only the ErrorEvent.
- Step 6 and step 5k error semantics are pinned by tests (P14, P15, P22 killed). So are the unverified/no-save rule (P18, P20), the repair (P19), the egress notice (P16), the service escalation (P17) and the defer idempotency key (Q26).
- Tests run the real loop, store, job queue and `chat` role prompts. Only the model, verifier and memory are scripted, and the assertions check real rows, events and logs.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **The payload-validation test passes without exercising the payload check.**
   - Where: tests/integration/harness/test_chat_service.py:302-311, guarding chat.py:295-299.
   - Probe P21 disabled the check (`if False and not (...)`) and the test still passed.
   - Tracing the mutated run shows the `ConfigError` comes from `_service_from_config` → `LLMRegistry(...)`: "client claude-opus is off-network but egress is disabled in profile local" (chat.py:282). It does not come from the U06-136 step 1 check.
   - Fix: patch `_service_from_config` (as the other IT06-34 tests do), or assert the message "chat job payload needs exactly session_id and message_id". Add a case with a non-string id and one with an extra key.

#### Minor (Nice to Have)
1. **The shared per-turn `seen` set (the T06-23 carry-over) is implemented but untested at turn level.**
   - Where: _chat_turn.py:114 and :242.
   - Q24 (the observer always emits evidence) and Q25 (`ObservedTool` without `seen=t.seen`) both survived, although ST06-11's endless loop returns Q1 on every call.
   - Fix: assert one `evidence` event per query id across repeated calls.
2. **The timeout and queue constants are not pinned by any test.**
   - `QUEUE_GRACE_S = 30` (chat.py:68): P05 survived because ST06-11 monkeypatches it.
   - `QUEUE_MAX = 1000` (chat.py:67): P06 survived.
   - The timeout is per `get`, so a turn with an egress rerun plus a repair can run up to about 3 × (wall_clock_s + 30) in total (_chat_turn.py:220). This is spec-literal, noted for TH06-11.
3. **A non-`HernessError` crash leaves state open.** In the worker crash path (chat.py:256-264) the client gets an `InternalError` event, but the reply row stays `streaming`, the run stays `running` and the task stays leased. Nothing calls `cleanup`.
4. **`ChatDeps.from_config` builds a fresh `WarehousePool` on every call and never calls `close_all`.**
   - Where: chat.py:150. It runs once per `ChatService()` without deps and once per `chat_job_handler` run.
   - Each deferred job leaves its DuckDB handles to garbage collection, although `WarehousePool` is documented as process-wide (herness/harness/warehouse.py:192).
5. **The `unverified` `VerificationEvent` carries `VerificationResult(passed=True, items=[])`** (_chat_turn.py:319-323, R7). A consumer that reads `result.passed` instead of `status` would treat an unverified answer as passed. `passed=False` with empty items would be safer.
6. **The marker renderer is duplicated.** `_chat_rows.render` (_chat_rows.py:114-120) is the same code as `swarm/escalation._render` (escalation.py:159-166). U06-125 permits composing in place, but one shared helper would avoid drift.
7. **The new siblings are missing from the §2 import-rank table.** `_chat_turn` and `_chat_rows` got module-map rows (docs/impl/06:111-112) but are not in the rank table (docs/impl/06:117-123). In practice they are rank-1 siblings of `chat`. Separately, and already true at the base, pyproject.toml has no harness-internal "layers" contract.
8. **Two concurrent `answer` calls for the same message both run a turn** (chat.py:210 only short-circuits `done`). There is one assistant row but two runs. The spec says nothing on this; noted for TH06-11's "one run per turn".
9. **Pre-existing, outside this card (the builder's concern 2):** `BudgetSettings.to_task_budget` sets `max_cost_usd=0` (pipelines/settings.py:106-108), and the loop stops on `cost_usd > max_cost_usd` (loop.py:124). A priced cloud chat client would stop with `task_budget` after its first call. This needs an owner ruling or a fix in T06-0x/spec 05. Tests use zero-cost fakes.

### Probes
All probes restored the tree with `git checkout -- .`; `tree_clean=True` after each one.

Command template: `python3 /tmp/w32-c06c/verifier/probe.py <spec>.json`. It applies one exact replacement and runs `uv run --frozen pytest <file> -q -p no:logging -x -k "<k>"` under the env exports.

| Probe | Mutation | -k selection | Result |
|---|---|---|---|
| P01 TH06-11 | `events.get()` with no timeout (chat.py:245) | `ST06_11 and queue_timeout` | killed |
| P02 TH06-11 | no `stop.set()` on generator close (chat.py:238) | `ST06_11 and consumer_close` | killed |
| P03 TH06-11 | `HarnessHooks(stop=None)` (_chat_turn.py:215) | `ST06_11 and consumer_close` | killed |
| P04 TH06-11 | `asyncio.timeout(None)` (_chat_turn.py:220) | `ST06_11 and hung_model` | killed (159 s) |
| P05 TH06-11 | `QUEUE_GRACE_S = 3000` | `ST06_11` | **survived** (Minor 2) |
| P06 TH06-11 | `QUEUE_MAX = 0` (unbounded) | `IT06_11 and order or ST06_11 and endless` | **survived** (Minor 2) |
| P07 TH06-12 | cloud never downgraded (`if False:`) | `ST06_12 or IT06_13 and local` | killed |
| P08 TH06-12 | `allows_cloud` drops `cloud_chat_allowed` | `ST06_12` | killed |
| P09 TH06-14 | INFO `turn_completed` gets `q=t.question` | `ST06_14` | killed |
| P10 TH06-14 | INFO `turn_completed` gets `a=answer.text` | `ST06_14` | killed |
| P11 IT06-23 | `finish` inserts a second assistant row | `IT06_23` | killed |
| P12 IT06-34 | `done` reply no longer short-circuits | `IT06_34 and twice` | killed |
| P13 IT06-36 | session save before `FinalEvent` | `IT06_36 and captured` | killed |
| P14 IT06-36 | save failure emits an ErrorEvent | `IT06_36 and failure` | killed |
| P15 step 6 | hint = `exc.hint` only | `IT06_11 and turn_error` | killed |
| P16 5d | no `EGRESS_NOTICE` on local rerun | `IT06_13 and egress_blocked_retries` | killed |
| P17 5e | service escalation removed | `IT06_14 and budget_stop` | killed |
| P18 5g | `ConfigError` not mapped to unverified | `IT06_11 and unverified` | killed |
| P19 5g | repair turn removed | `IT06_11 and repair_fixes` | killed |
| P20 5k | save even when unverified | `IT06_11 and unverified` | killed |
| P21 U06-136 | payload check disabled | `IT06_34 and payload` | **survived** (Important 1) |
| P22 step 6 | reply not marked failed | `IT06_11 and turn_error` | killed |
| P23 U06-128 | model gets a constant, not `msg.content` | `IT06_11 and order` | killed |
| Q24 seen | observer ignores shared `seen` | `IT06_11 or ST06_11 and endless` | **survived** (Minor 1) |
| Q25 seen | `ObservedTool` without `seen=t.seen` | `IT06_11 or IT06_14` | **survived** (Minor 1) |
| Q26 step 2 | `idem_key=None` | `IT06_12` | killed |

Totals: 21 killed, 5 survived.

### Builder readings
- **R1 ACCEPTED.** `ToolRegistry.resolve` raises `ConfigError` for any task tool whose `TOOL_OWNERS` owner is not "06" (herness/harness/tools.py:318-321; spec 05 line 828 step 3, TH05-22, ST05-22). Process registration rejects a second tool under an existing name (tools.py:296-300).
  - So `ObservedTool` wrapping of the spec 05/07 tools in `CHAT_TOOLS[mode]` cannot be done as U06-129 5c states without breaking TH05-22. It is not achievable, so this is not an Important finding.
  - The tracer wrapper (`_ToolObserver` on the `tool_call` trace from _tools_dispatch.py:187) gives equivalent `ToolEvent`/`EvidenceEvent` order. The spec conflict should go to the 05/06 owners.
  - The shared `seen` set holds by reading (_chat_turn.py:114, 242), but it is untested (Minor 1).
- **R2 ACCEPTED.** `has_review_intent(question, {})`: `detect_entities` is not in the tree (a T06-23 carry-over). The kind fallback is a sensible stand-in.
- **R3 ACCEPTED.** The extra `ChatDeps` fields are needed to build `ToolContext` and the chat `list_findings`. The fail-closed `_UnboundVectors` is correct.
- **R4 ACCEPTED.** Default `profile == hcfg.profile and cloud_chat_allowed(hcfg)` implements R-38 exactly (egress.py:90-97).
- **R5 ACCEPTED.** `account` re-raises the run ledger's `BudgetExceeded` out of the loop (_loop_steps.py:106-110); mapping it to stop reason `budget` matches 5e.
- **R6 ACCEPTED.** With no final output, the turn falls back to `NO_VERIFIED_ANSWER` and forces status `partial`, which avoids a vacuous `verified`.
- **R7 ACCEPTED with note.** `status` is authoritative, but `passed=True` is misleading (Minor 5).
- **R8 ACCEPTED.** Adding `question` to the repair input keeps U06-141 wrapping.
- **R9 ACCEPTED.** `MemoryRunContext.from_tool_ctx` reads `user_ref` from the run meta (core/types/memory.py:172-184).
- **R10 ACCEPTED.** `TaskSpec` needs an entity scope; the dedup key goes through `compute_dedup_key` (UT05-124 guard).
- **R11 ACCEPTED.** A chat turn is never resumed, so it has no loop checkpoints. The compactor matches the swarm's.
- **R12 ACCEPTED.** One `ModeEvent` per defer call, and the same job through the idem key (Q26 killed).
- **R13 ACCEPTED.** Reading the result from the reply row makes the second run identical (the IT06-34 assertion).
- **R14 ACCEPTED.** Step 6 covers only 5a-5j; defer errors propagate to the spec 09 caller.
- **R15 ACCEPTED.** Consumer stop → run `canceled`, reply `failed`, no event. The crash path keeps the consumer from hanging, but leaves rows open (Minor 3).
- **R16 ACCEPTED.** Content is cut to the 20,000-character `chat_message` cap. The model saw only redacted inputs.
- Deviation: two private siblings with new §2 rows. Accepted; the rank table is not updated (Minor 7).

### Assessment
**Task quality:** Needs fixes
**Reasoning:** All four units follow the spec, all ten test IDs exist and went RED, and the threat rows TH06-11, TH06-12 and TH06-14 and both acceptance checks are probe-tight (21 of 26 probes killed); gates, budgets, layering and coverage all pass. One Important test gap remains: the IT06-34 payload test passes through an unrelated `LLMRegistry` `ConfigError`, so the U06-136 step 1 payload check has no working test.

Tree state after the review: `git status --short` empty, `git diff` empty (no probe left behind, nothing committed).

## Re-review round 1 (75e2a12)

Scope: Important 1 and Minors 1, 2, 3, 5 and 7 from the first review. Fix commit 75e2a12 sits on top of 70dc1b5.

Probe command template: `python3 /tmp/w32-c06c/verifier/probe.py r1/<probe>.json`. It applies one exact replacement, runs `uv run --frozen pytest <file> -q -p no:logging -x -k "<k>"`, then restores the tree with `git checkout -- .`.

### Per-finding status
- **Important 1 (payload test): resolved.**
  - `test_it06_34_job_payload_must_be_two_ids` is parametrized over a missing id, a non-string id (`message_id: 7`) and an extra key. `test_it06_34_unknown_session_not_found` covers the unknown session.
  - Both patch `_service_from_config` to raise `AssertionError` and match the exact messages, so the error can only come from U06-136 steps 1-2 (test_chat_service.py:307-349).
  - P21 (payload check disabled) is now killed.
- **Minor 1 (shared `seen`): resolved.**
  - New test `test_it06_11_evidence_once_per_query_id_per_turn`: `run_sql` runs twice through the trace observer and `list_findings` once through `ObservedTool`, all returning Q1. It expects 3 `tool` events and exactly 1 `evidence` event.
  - Q24 (observer always emits) and Q25 (`ObservedTool` without `seen=t.seen`) are both killed.
- **Minor 2 (queue constants): resolved.**
  - `test_st06_11_queue_bound_and_turn_timeout` pins `QUEUE_MAX == 1000` and `QUEUE_GRACE_S == 30`.
  - Through a recording `Queue`, it also checks the actual behaviour: `maxsize == 1000`, and every `get` timeout equals `wall_clock_s + 30`.
  - P05 and P06 are both killed.
- **Minor 3 (crash cleanup): resolved.**
  - `_work` now calls `cleanup(..., crashed=True)` unless steps 5i-5j have already committed (`Turn.done`) (chat.py:264-265, _chat_rows.py:189-210).
  - The cleanup marks the reply `failed` and the run `failed`, and makes the task dead through `fail_task(FatalError("chat turn crashed"))`. No extra event is sent; the `InternalError` event is still streamed.
  - `test_it06_11_worker_crash_closes_rows` asserts all of this.
  - P27 (cleanup call removed) and P28 (a crash ends the run `canceled` instead of `failed`) are both killed.
  - Residual, not blocking: a non-`HernessError` crash after `t.done`, inside `_save_turn`, still streams `InternalError` after `final`. Step 5k excludes an error event only for `HernessError`, so this is acceptable.
- **Minor 5 (unverified result): resolved; the variant is sound.**
  - I confirmed the builder's claim in herness/core/types/harness/evidence.py:
    - `ItemResult._passed_consistent` (lines 152-163) requires `passed == (clean and not unverified_findings and all checks match)`.
    - `VerificationResult._totals_consistent` (lines 177-187) requires `passed == all(items)` and `(n_numbers, n_failed) == (len(checks), non-match count)`.
    - `VerificationEvent.result` is a required `VerificationResult` (types/swarm/drafts.py:254-260).
  - So `passed=False` with `items=[]` is invalid by construction.
  - One `answer` item with a `query_failed` check per cited number (_chat_turn.py:321-335) is a valid, consistent model, and "query_failed" is an honest description of "the verifier could not run".
  - The only cleaner alternative is making `VerificationEvent.result` optional for `unverified`. That is an owner change to U06-21, so it is not required here.
  - The no-number answer that reads `passed=True` is truthful (nothing failed), and `status` stays authoritative.
  - The test asserts `(False, 1, 1)` and `["query_failed"]`.
- **Minor 7 (rank table): resolved.** Rank 1 of the §2 import-rank table now lists `herness.harness.pipelines._chat_turn` and `herness.harness.pipelines._chat_rows` (docs/impl/06-swarm-and-pipelines.impl.md:119).
- **Minors 4, 6, 8 and 9:** parked as carry-overs by sub-controller ruling. Unchanged and out of scope.

### Gates and tests at 75e2a12
- `lint-imports`: 15 kept, 0 broken.
- `tools.check_module_size`: exit 0. chat.py 315/390, _chat_turn.py 352/360, _chat_rows.py 211/220.
- `ruff check .`: clean.
- `ruff format --check .`: 1112 files already formatted.
- `mypy`: no issues in 397 files.
- `pytest tests/integration/harness/test_chat_service.py tests/security/test_st06_chat.py -q -p no:logging`: 37 passed in 145 s.

### Probes (round 1)

| Probe | Mutation | -k selection | Result |
|---|---|---|---|
| P21 | payload check `if False and not (...)` (chat.py:296) | `IT06_34 and payload` | killed |
| Q24 | observer `if True:` instead of the `seen` check | `IT06_11 and evidence_once` | killed |
| Q25 | `ObservedTool(tool, t.emit)` without `seen` | `IT06_11 and evidence_once` | killed |
| P05 | `QUEUE_GRACE_S = 3000` | `ST06_11 and queue_bound` | killed |
| P06 | `QUEUE_MAX = 0` | `ST06_11 and queue_bound` | killed |
| P27 | crash-path `cleanup(...)` removed (chat.py:265) | `IT06_11 and crash` | killed |
| P28 | crash ends run `canceled` (`"failed" if exc else ...`) | `IT06_11 and crash` | killed |

Totals: 7 killed, 0 survived. `tree_clean=True` after each probe.

### Assessment (round 1)
**Task quality:** Approved
**Reasoning:** Every finding in scope is fixed, and each fix is tied to a test that fails when it is reverted (7 of 7 probes killed). Gates, budgets and the card tests all pass, and the remaining Minors are parked carry-overs.

Tree state after the re-review: `git status --short` empty, `git diff` empty, nothing committed.
