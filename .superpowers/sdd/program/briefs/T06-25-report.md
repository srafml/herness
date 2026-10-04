# T06-25 report — ChatService and chat job

Status: DONE_WITH_CONCERNS
Branch: claude/w32-c06c-T06-25 (base 3e8c337). Commits: e51fc21 `wip(T06-25): ChatService, chat turn and chat job with tests green`; 70dc1b5 `feat(harness): T06-25 ChatService and chat job` (adds the default-deps test). All pre-commit hooks passed on both commits.

## What was implemented (per unit)

- **U06-127 `ChatService` / `ChatDeps`** (`herness/harness/pipelines/chat.py`):
  `ChatService(cfg: PipelinesConfig, llms: LLMRegistry, memory: MemoryStore, *, deps: ChatDeps | None = None)`.
  `ChatDeps` is a frozen, keyword-only dataclass with the spec fields `hcfg, verifier, warehouses,
  tracer_factory, clock, metrics, jobs, current_build, data_policy_allows_cloud` plus `ops`, `vectors`,
  `past_reader` (see R3). `ChatDeps.from_config(hcfg)` builds the process defaults (WarehousePool on
  `data_layout(cfg).warehouse`, `Verifier(store.ops.evidence, pool, models.harness.verifier)`,
  `Tracer.for_run(run_kind="chat")`, `herness.core.time.now`, `herness.core.resilience.metrics`, the job
  queue + `chat_model_profile`, `store.warehouse.read_current`, `cloud_chat_allowed(hcfg)` for the active
  profile, `store.ops.evidence` as `OpsHandle`, `query_verified_findings_recent` as the chat `list_findings`
  backend). Protocols `ChatJobs` (extends the T06-24 `JobsFacade` with `chat_model_profile`),
  `AnswerVerifier`, `WarehouseSource`, `ChatMetrics`. Only ChatService writes assistant rows
  (`upsert_assistant_placeholder` / `update_chat_message`).
- **U06-128 `answer`**: `latest_user_message`; none → `ErrorEvent("NotFound", "no user message to answer", None)`;
  delegates to `_answer_message`; `text` is ignored (`del text`), the model gets `msg.content`.
- **U06-129 `_answer_message`** (`chat.py` steps 1-4, `_chat_turn.py` + `_chat_rows.py` steps 5-6):
  1 placeholder (None → NotFound "message not found in session"; reply `done` → no events);
  2 defer: `jobs.enqueue("chat", {session_id, message_id}, gpu_class="reasoning", priority=None,
  idem_key="chat:<s>:<m>")`, reply `queued` with meta `{reply_to, mode: defer, job_id}`, `ModeEvent(defer)`,
  INFO `harness.chat.turn_deferred`;
  3 `cloud` kept only when `deps.data_policy_allows_cloud(hcfg.profile)` (default = `cloud_chat_allowed(hcfg)`),
  else `small_model`; `ModeEvent(mode, MODE_MESSAGES[mode])`;
  4 worker thread running `asyncio.run(run_turn(...))`, `queue.Queue(maxsize=1000)`, sentinel `None`,
  `get(timeout=chat.budget.wall_clock_s + 30)` → `ErrorEvent("ModelUnavailable", "chat turn timed out",
  "Try again later.")` and stop flag; generator close (finally) sets the stop flag (HarnessHooks.stop).
  5a run (`kind=chat, depth=fast, status=running, meta={request: {kind: chat, question: "<redacted>"},
  session_id, user_ref}`, config_hash via `request_config_hash`) and one `chat` task (budget
  `pipelines.chat.budget`, tools `CHAT_TOOLS[mode]`, model role chat/chat_off_hours) in one
  `run_write(op="chat_create_run")`, then `claim_task`;
  5b `memory.session_load`, `memory.prior_context(MemoryRunContext(...))`;
  5c client key `jobs.chat_model_profile(mode)` (None → `ModelUnavailable`), model role `chat_off_hours` for
  small_model else `chat`, `egress_purpose=egress_purpose_for(model_role)` ("reasoning") for an off-network
  client (R-38), `RunBudget("chat", tokens_cap=chat.budget.max_tokens)`, `build_gates({key: cfg}, mode="chat")`,
  `ObservedTool` around `ListFindingsTool` (past verified findings) and `EscalateTool` sharing one `seen` set;
  5d `run_agent(get_role("chat", model_role=...), build_task_input("chat", {question, message_id, session,
  prior_context}), ctx, client, config, HarnessHooks(chain=None, on_text_delta=None, stop=..., task_id=None))`
  under `asyncio.timeout(wall_clock_s + 30)`; `EgressBlocked` in cloud → one rerun on
  `chat_model_profile("small_model")` and the answer prefixed with `EGRESS_NOTICE`;
  5e stop in {budget, task_tokens, task_budget, no_progress} + `has_review_intent(question, {})` → escalate once;
  5f `ChatAnswer` + `TokenEvent`s from `chunk_text` of the rendered marker text (`parse_markers` +
  `format_number`, other markers kept);
  5g verify in a thread; ConfigError/QueryError → `unverified`; failing → one repair `run_agent` with
  `{question, message_id, previous, verifier_report}`, re-verify, still failing → `trim_failing_claims` → `partial`;
  5h `VerificationEvent`; 5i reply `done` with rendered content, verified, run_id, query_ids, meta
  `{reply_to, mode, model, latency_ms, numbers}`; 5j `complete_task(writes=usage + run done)`,
  `FinalEvent`, INFO `harness.chat.turn_completed` (run_id, mode, status, latency_ms), metrics
  `herness_harness_chat_turns_total{mode,verified}`, `herness_harness_chat_latency_seconds{mode}`;
  5k unless unverified: `memory.session_save_turn` → `CorrectionCapturedEvent` + INFO
  `harness.chat.correction_captured`; HernessError → WARNING `harness.chat.session_save_failed`, no ErrorEvent.
  6 HernessError in 5a-5j → `ErrorEvent(type, str(e), e.hint or "Try again later." for RetryableError)`,
  reply `failed`, run `failed`, task `fail_task(max_task_attempts=1)` (dead), ERROR `harness.chat.turn_failed`
  (error_type only). A stop by the consumer (CancelledError from `after_step`) → reply `failed`, run
  `canceled`, no event.
- **U06-136 `chat_job_handler`**: payload exactly `{session_id, message_id}` strings else `ConfigError`;
  `get_chat_session` None → `NotFound("chat session not found")`; `_service_from_config()` builds
  `ChatService(hcfg.pipelines, LLMRegistry(hcfg.models, profile=hcfg.profile), get_memory_store(),
  deps=ChatDeps.from_config(hcfg))`; consumes `_answer_message(..., "live")`; returns
  `JobOutcome("done", {"run_id", "status"})` read from the reply row via `find_assistant_message`
  (closes the U09-107 lookup carry-over). Second run: reply `done` → no model call (IT06-34).
- `herness/harness/pipelines/__init__.py`: `ChatService` added to the lazy `_EXPORTS` map only (35 lines).

## Files changed
- new `herness/harness/pipelines/chat.py` (public: ChatDeps, ChatService, chat_job_handler)
- new `herness/harness/pipelines/_chat_turn.py` (private: the async turn, steps 5-5k)
- new `herness/harness/pipelines/_chat_rows.py` (private: Turn/TurnEnv, render, steps 5a, 5i-5j, 6)
- `herness/harness/pipelines/__init__.py` (one `_EXPORTS` entry + docstring)
- `docs/impl/06-swarm-and-pipelines.impl.md` §2: two new private-sibling rows (`_chat_turn.py` 360,
  `_chat_rows.py` 220) added in the same commit as the files.
- new tests: `tests/integration/harness/test_chat_service.py`, `tests/integration/harness/_chat_service_env.py`
  (helper, not a test module), `tests/security/test_st06_chat.py`.
- Nothing else under herness/, no pyproject/conftest/tests/support change.

## Line counts vs budgets
| file | lines | budget |
|---|---|---|
| chat.py | 312 | 390 |
| _chat_turn.py | 340 | 360 (new §2 row) |
| _chat_rows.py | 208 | 220 (new §2 row) |
| pipelines/__init__.py | 35 | 40 |
`tools.check_module_size` exit 0.

## RED evidence
```
$ uv run --frozen pytest tests/integration/harness/test_chat_service.py tests/security/test_st06_chat.py -q -p no:logging
tests/integration/harness/_chat_service_env.py:47: in <module>
    from herness.harness.pipelines.chat import ChatDeps, ChatService
E   ModuleNotFoundError: No module named 'herness.harness.pipelines.chat'
ERROR tests/integration/harness/test_chat_service.py
ERROR tests/security/test_st06_chat.py
2 errors in 2.94s
```
(First implementation runs then failed on real behaviour, e.g. `wall_clock` stop because the test clock was
fixed, `secret not found: redact.hmac_key` in `fail_task` without a test redactor, the hung-model case
stopping at step 0; each fixed in the test setup, not by weakening the assertion.)

## GREEN evidence
```
$ uv run --frozen pytest tests/unit/harness/pipelines tests/integration/harness tests/security/test_st06_chat.py -q -p no:logging \
    --cov=herness.harness.pipelines.chat --cov=herness.harness.pipelines._chat_turn --cov=herness.harness.pipelines._chat_rows --cov-branch
herness/harness/pipelines/_chat_rows.py     101      2     10      1    97%
herness/harness/pipelines/_chat_turn.py     190      2     42      3    98%
herness/harness/pipelines/chat.py           156      9     20      0    95%
TOTAL                                       447     13     72      4    97%
188 passed, 2 skipped in 330.14s
```
Card tests: 31 (22 in test_chat_service.py incl. parametrized, 9 in test_st06_chat.py), all pass
(the 31st, `test_it06_13_default_deps_cloud_rule_and_unbound_vectors`, was added in the final commit and run alone: 1 passed).
IDs covered: IT06-11 (6 fns), IT06-12, IT06-13 (4), IT06-14 (2), IT06-23 (4 params), IT06-34 (3), IT06-36 (2),
ST06-11 (4), ST06-12 (4 incl. control), ST06-14.

## Gates
- `ruff format .` / `ruff check --fix .`: clean.
- `mypy`: Success, no issues in 397 source files.
- `lint-imports`: Contracts: 15 kept, 0 broken.
- `tools.check_module_size`: exit 0. `tools.check_type_ownership`: exit 0 (a local `Status` alias was
  renamed `TurnStatus` after OWN040).
- Coverage (above): chat.py 95 % line / 100 % branch; _chat_turn 98 %; _chat_rows 97 %.
- Pre-commit hooks on the commits: all passed (first attempt failed UT05-124, the repo-wide hash-call
  guard; fixed by using `findings.compute_dedup_key` for the chat task's dedup key instead of a local
  `sha256_hex`, then re-committed).

## Spec readings
- R1 (TOOL_OWNERS / TH05-22 conflict, U06-129 5c): spec 05 `ToolRegistry.resolve` accepts only spec 06
  tools as `task_tools`, so `ObservedTool` wrappers around the spec 05/07 process tools cannot be passed.
  Spec 06 tools (`list_findings`, `escalate`) are `ObservedTool`s; process tools are observed by a
  `TraceEmitter` wrapper on `ctx.tracer` that turns each traced `tool_call` (name, ok, query_ids) into the
  same `ToolEvent` + new `EvidenceEvent`s, sharing the turn's single `seen` set (closes the T06-23
  shared-`seen` carry-over). A tool call rejected by dispatch pre-checks (e.g. a duplicate) of a spec 06 tool
  produces no ToolEvent (the wrapper never runs).
- R2 has_review_intent arity: called as `has_review_intent(question, {})` — `detect_entities` /
  `EntityDirectory` are not built (T06-23 left them out), so entity-count rules see no entities. The tool
  path picks the kind from the intent, else `funding_review` when the text names fund/invest/epic/
  initiative/candidate, else `org_review`; `focus=None`.
- R3 `ChatDeps` adds `ops`, `vectors`, `past_reader` (ToolContext handles and the chat `list_findings`
  backend), as T06-13 did for `RunEnv`. No `VectorHandle` implementation exists in the tree
  (`search_tickets`): the default is a fail-closed stand-in raising `NotFound` (a RecoverableError → error
  tool result), so `semantic_search` is unusable until the composition root binds a ticket index.
- R4 `data_policy_allows_cloud(profile)`: default `profile == hcfg.profile and cloud_chat_allowed(hcfg)`.
- R5 BudgetExceeded: the run ledger raises `BudgetExceeded` out of `run_agent` (spec 05) rather than returning
  `stop_reason="budget"`; it is mapped to stop reason `budget` (output None).
- R6 A loop stop without a final answer (`output None`, e.g. budget, max_steps, wall_clock) gives
  `ChatAnswer(NO_VERIFIED_ANSWER)` with status forced to `partial` (otherwise it would verify vacuously).
- R7 `unverified` VerificationEvent carries an empty `VerificationResult(passed=True, items=[])` (the model
  cannot express "not verified" with items; `status` is authoritative).
- R8 Repair input = `{question, message_id, previous, verifier_report}` (question kept for context and
  wrapped by U06-141); the repair uses the same fallback-aware `_agent` (EgressBlocked → local).
- R9 Run meta also carries `user_ref`: spec 07 memory tools build `MemoryRunContext.from_tool_ctx` from the
  chat run meta (`session_id`, `user_ref`), needed for chat corrections via `propose_memory`.
- R10 Chat task: `scope = EntityScope("run", [run_id])` (TaskSpec needs one entity), objective
  "Answer the chat question.", `dedup_key = compute_dedup_key("chat", "general", scope, objective)`.
- R11 `HarnessHooks(task_id=None, phase=None)`: no loop checkpoints for chat (a turn is never resumed);
  compactor = `memory.compactor(config, ctx=ctx)` as the swarm does.
- R12 IT06-12 "one mode event": read as one `ModeEvent` per call (the algorithm yields it on every defer
  call; both calls return the same job id). The job then completes the same assistant row.
- R13 The `chat_job_handler` result `status` is read from the reply row (`verified` when done, else the row
  status) and `run_id` from the row, so a second run returns the same result.
- R14 Defer-path errors (enqueue) propagate (spec step 6 covers 5a-5j only).
- R15 Cancellation (consumer stopped): run → `canceled`, task dead via `fail_task(FatalError)`, reply
  `failed`, INFO `harness.chat.turn_stopped`; worker-thread non-Herness crash → ERROR
  `harness.chat.turn_crashed` + `ErrorEvent("InternalError", "chat turn failed")` so the consumer never hangs.
- R16 Content stored is the rendered marker text truncated to the 20,000-char chat cap (no extra redaction:
  the model only saw redacted inputs and `text_access="redacted_only"`).

## Deviations from the brief
- Two private siblings (`_chat_turn.py`, `_chat_rows.py`) instead of one: the turn alone was ~490 lines.
  Both have §2 rows in docs/impl/06 added in the same commit.
- IT06-14 "mini run ≤ 8 tasks": the swarm run driver (`swarm/run.py`, `review_job_handler`) is not on the
  base, so the mini run cannot execute. The test asserts the escalated run's resolved knobs
  (`resolve_knobs(..., override=request.budget_override).max_tasks_per_run <= 8`), one `review` job, the
  `escalated` event, and posts the summary with the real `post_escalation_summary` (test-local
  `find_message` adapter by `meta.escalation_run_id`, as IT06-33) → one assistant message in the session.
- Commit attribution: the dispatch asked for `Co-Authored-By: Claude Fable 5.1`, the harness attribution
  reminder (which implementer-rules say to use) gives `Claude Opus 5.5`; commits use the reminder's line
  plus the given Claude-Session line.

## Carry-overs / what the composition root (T09-27) must do
- Register `chat_job_handler` for kind `chat` (`register_handler("chat", chat_job_handler)`) — not done at
  import to avoid side effects.
- Bind the jobs and ops (resilience) backends, the chain registry (for `chat_model_profile`), the process tool
  registry (warehouse tools + `register_memory_components`), a redactor, and a real `VectorHandle` (none exists).
- `find_assistant_message` (U09-107) looks up by `meta.reply_to`, not `meta.escalation_run_id`;
  `post_escalation_summary` callers still need an adapter (T06-24 carry-over, unchanged).
- `detect_entities` / EntityDirectory (U06-131) still missing.

## Concerns
1. TH05-22 vs U06-129 5c (R1): process tools observed through the trace hook, not `ObservedTool`; needs an
   owner ruling (or a spec 05 hook) if `ObservedTool` must wrap every tool.
2. `TaskBudget.to_budgets` passes `max_cost_usd=0` and the loop stops on `cost_usd > max_cost_usd`, so a
   priced off-network (cloud) client would stop with `task_budget` after its first call. Pre-existing
   (pipelines settings / spec 05); tests use zero-cost fakes. Not fixed (out of scope).
3. Per-turn `build_gates`: gates are per event loop, so chat-slot reservation across concurrent turns is not
   enforced by these gates (same as spec text, noted for TH06-11).
4. No real `VectorHandle` (R3); `semantic_search` in chat fails as a tool error until bound.
5. ST06-11 hung-model test runs ~20 s (backstop shortened via `TURN_GRACE_S=-100`; the loop's own wall clock
   cannot cut a hung call, and the first call starts only after the multi-second first prompt build).

## Fix round 1 (review: Needs fixes)

Commit: see the last line of this section.

- **Important 1 (payload test)**: replaced the payload/session test with
  `test_it06_34_job_payload_must_be_two_ids` (parametrized: missing id, non-string id, extra key) and
  `test_it06_34_unknown_session_not_found`. Both patch `_service_from_config` to raise, so the error can only
  come from U06-136 steps 1-2, and they match the messages "chat job payload needs exactly session_id and
  message_id" / "chat session not found".
- **Minor 1 (shared `seen`)**: `test_it06_11_evidence_once_per_query_id_per_turn`. In it, `run_sql` (trace
  observer path) runs twice and `list_findings` (`ObservedTool` path) runs once, and every call returns Q1;
  the test asserts 3 `tool` events and exactly one `evidence` event. Mutation check: dropping
  `seen=t.seen` (Q25) → fails; observer always emitting (Q24) → fails.
- **Minor 2 (constants)**: `test_st06_11_queue_bound_and_turn_timeout` asserts `QUEUE_MAX == 1000` and
  `QUEUE_GRACE_S == 30`. It also swaps in a recording `queue.Queue` and asserts maxsize 1000 and that every
  `get` timeout is `chat.budget.wall_clock_s + 30` (150).
- **Minor 3 (crash path)**: the worker's non-`HernessError` crash path now runs the step 6 cleanup
  (`cleanup(..., crashed=True)`: reply `failed`, run `failed`, task dead via `fail_task`), unless steps 5i-5j
  already committed (`Turn.done`). The `InternalError` event is still streamed. Test:
  `test_it06_11_worker_crash_closes_rows`.
- **Minor 5 (unverified result)**: the requested `VerificationResult(passed=False, items=[])` is rejected by
  the type's own validator (`passed == all(items)`), and `VerificationEvent` revalidates its result
  (`model_construct` crashed the turn with a ValidationError, as tried). The `unverified` result is now one
  `answer` item that has a `query_failed` check for every cited number, so `passed=False`,
  `n_numbers == n_failed == len(numbers)`. It is a valid model and survives a JSON round trip. Remaining edge:
  an answer with no numbers still has `passed=True` (nothing failed); `status` stays authoritative.
  The test asserts `passed False, 1/1 failed, ["query_failed"]`.
- **Minor 7 (rank table)**: `_chat_turn` and `_chat_rows` added to rank 1 of the §2 import-rank table.
- Gates: ruff/format clean, mypy clean (397 files), lint-imports 15 kept / 0 broken, check_module_size
  exit 0, check_type_ownership exit 0. Lines: chat.py 315/390, _chat_turn.py 352/360, _chat_rows.py 211/220.
- Tests: `pytest tests/unit/harness/pipelines tests/integration/harness tests/security/test_st06_chat.py`
  → 195 passed, 2 skipped. Coverage: chat.py 98 %, _chat_turn 98 %, _chat_rows 97 % (line); branch ≥ 85 %.

### Parked carry-overs (sub-controller ruling, unchanged)
- Minor 4: `ChatDeps.from_config` builds a new `WarehousePool` per call (per job); the composition root
  should share one pool.
- Minor 6: `_chat_rows.render` duplicates `swarm/escalation._render` (cross-card file).
- Minor 8: concurrent turns for the same message are not serialized (spec silent).
- Minor 9: `TaskBudget.to_budgets` passes `max_cost_usd=0`, which stops a priced cloud client (pre-existing,
  owner).

Fix round 1 commit: 75e2a12 `fix(harness): T06-25 review round 1 (payload test, shared evidence set, queue constants, crash cleanup, unverified result, rank table)`; all pre-commit hooks passed.
