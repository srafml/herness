# T07-11 verify review: memory tools (recall_memory, propose_memory)

Worktree D:\herness\.claude\worktrees\agent-a3db8db23c2dcf888, head 28ae0fe (base 7dff609). Verified by reading the code, running the card tests and gates, running 35 in-place mutation probes and one temporary probe test. Every probe was reverted byte-for-byte and the worktree is clean.

**Verdict: Needs fixes.** One Important finding. It is plan-mandated and needs a sub-controller ruling or a small fix. Everything else passes.

### Spec Compliance
- U07-63 RecallMemoryTool: ✅. Matches steps 1–9: role gate, Kind check, `get_run(ctx.run_id)` returns `None` → `ToolInputError("run not found: …")`, null defaults, chat-only `include_pending_for = run_ctx.user_ref`, `recall_with_status`, `render_records(…, 2000)`, `record_use` with `StoreBusy` logged and ignored, the data.items shape from design §3.5 (score to 3 decimals, `format_utc`), and `row_count` / `truncated`. `query_ids` is `[]`. `MemoryNotFound` becomes `ToolInputError` with the same message.
- U07-64 ProposeMemoryTool: ✅ with one finding (I-1). Role gate, KIND_LAYER, and run_ctx plus `meta.message_id` for chat only are all correct. Provenance comes only from ctx and the run row (`author_type="agent"`, `author_ref=None`, `via="tool"`, `analyst_<specialty>`, run, task and build from ctx, session from run_ctx). Kind data matches step 5. Confidence defaults to 0.5. The tool calls `store.propose(…, run_ctx)`. `PolicyViolation` becomes `"<rule>: <message>"` and `MemoryNotFound` becomes `ToolInputError`. Both message texts are verbatim and `content == message`.
- U07-65 register_memory_tools: ✅. Registers with owner "07". A repeat call is a no-op because the names are checked first. `ConfigError` propagates. The U07-98 "handlers registered" half is a carry-over to T07-23 (ruling).
- Acceptance: listed tests pass ✅ (62 card tests; 617 passed across memory, security and tools). Strict-compatible ✅: `check_tool_schema` and `_strict_ok` are true for both schemas. Snapshot equals the §3.12 tables ✅: every field matches the tables, including nullable optionals, enums, bounds, patterns, `uniqueItems` on layers, `maxItems` 11 / 20, entity `id` ≤ 200, and `$defs.NumberRef` = `NUMBER_REF_SCHEMA`. Descriptions are verbatim ✅. The real `WRITER` RoleSpec cannot resolve `propose_memory` ✅: `ConfigError`, while the analyst resolves it (R-27). RECALL_ROLES and PROPOSE_ROLES are enforced at the tool as well as by the role allow-lists ✅.
- Threats: TH07-02 ✅. Strict schema plus a tool-side refusal of any argument outside the schema; `author_type`, `author_ref`, `via`, `run_id`, `task_id` and `provenance` are all refused and nothing is stored. TH07-06 ✅. Chat sees only its own user's pending items; analyst, writer, planner and skeptic on a run that carries a user_ref see none; `include_pending_for` / `user_ref` arguments are refused. TH07-23 / LLM01 ✅. Every kind through the tool lands as `pending_approval` with a review item, even at confidence 1.0. Injection-shaped content is flagged `instruction_like`, stays pending, and after approval renders escaped inside one `<untrusted_data>` block through `render_records`; the tools have no escaping of their own.
- Gates ✅: tools.py 239/280 lines, _tools_args.py 153/170 lines. Coverage is 100% line and 100% branch on both modules. `ruff format --check` and `ruff check` are clean, `mypy` (344 files) is clean, `lint-imports` keeps 13 of 13 contracts, and C901 / PLR0913 are enforced through ruff.
- ⚠️ Cannot verify yet: that the T07-23 `MemoryStore` satisfies `MemoryToolStore`. The Protocol has the §3.21 signatures, so mypy will check this when T07-23 lands. The UT07-48 / U07-98 handler half is a carry-over.

### Strengths
- Defence in depth behind the schema. The tool refuses extra arguments, a non-proposable kind, a non-string query or content, and malformed numbers, all without echoing the argument values.
- Real-composition security tests: MemoryRecaller, MemoryWriter, MemoryLifecycle and the ops store, with a real redactor and real rendering.
- The tests kill the probes well: 31 of 35 mutants killed. The four survivors are covered below.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- **I-1 (plan-mandated): `business_rule.rule_id` stores personal data unredacted.** Location: herness/harness/memory/_tools_args.py:102-107 and :119.
  - Cause: U07-64 step 5 builds `rule_id` from the first 8 words of the raw content. U07-50 then exempts `rule_id` from redaction, because `rule_id` is in `ID_KEYS` (herness/harness/memory/_write_steps.py:43-47).
  - Probe: the tool proposed "notify alice.smith@example.com before closing payments tickets" through the real write path and the real redactor. Stored `content` was "notify [EMAIL_f6f7c39013] before …", but stored `data.rule_id` was `notify_alice_smith_example_com_before_closing_payments_tickets`.
  - Impact: model or user text reaches `memory_item.data` and the review item without redaction. This breaks the brief's "content from the redacted write path" requirement and ENG §3.4.
  - Fix options, which need a ruling:
    - (a) Build the slug from the redacted content: either the write path recomputes `rule_id` for `via="tool"`, or the write path redacts `rule_id` when `via="tool"`.
    - (b) Have the tool emit a non-content id, such as `rule_` plus a short hash of the content.
    - Either way, add a ST07-02 / ST07-01-style test that plants an email in a business_rule.

#### Minor (Nice to Have)
- **M-1:** herness/harness/memory/tools.py:208. The `kind not in ta.PROPOSE_KINDS` guard has no test (probe M10 survived). Nothing calls the tool with `kind="mapping"`, `"sql_template"` or `"qa_pair"` while bypassing dispatch. The write path's policy matrix still rejects these: it has no `(mapping|sql_template|qa_pair, agent)` row (policy.py:284-303). Add one parametrised test for the LLM06 note.
- **M-2:** herness/harness/memory/tools.py:97-101. The explicit kinds check is redundant with `RecallFilters` validation (probe M22 was an equivalent mutant, because the message still contains "kinds"). No action needed. Similarly, `layers` and `k` are not re-checked behind the schema (tools.py:132-141); the store validates them.
- **M-3:** herness/harness/memory/tools.py:91. The `run_meta` reading `{**run.meta, "kind": run.kind}` is sound (the row's kind is authoritative) but untested (probe M23 survived). Add a run whose meta carries `"kind"`.
- **M-4:** herness/harness/memory/tools.py:207-210. On a dispatch bypass, the error message echoes up to 40 characters of model-supplied kind and layer text. The spec mandates this template, and the schema enum makes it unreachable through dispatch. Acceptable.
- **M-5:** herness/harness/memory/tools.py:178, 183, 184. Three `# type: ignore[arg-type]`. They are acceptable with mypy clean, but a `cast` would be more explicit.

### Mutation probes (tests: test_memory_tools.py + test_st07_tools.py)
| # | Mutation | Result | Killed by |
|---|----------|--------|-----------|
| M01 | role check off | KILLED | ut07_46_disallowed_role, ut07_47_writer_role_rejected |
| M02 | writer added to PROPOSE_ROLES | KILLED | ut07_46_constants, ut07_47_writer_role_rejected |
| M03 | include_pending_for for every role | KILLED | ut07_46_include_pending_only_for_chat, st07_06_pending_items_only_for_own_chat_user |
| M04 | include_pending_for never set (chat too) | KILLED | ut07_46_include_pending_only_for_chat, st07_06 |
| M05 | extra-argument check off | KILLED | 9 tests incl. all st07_02_forged_provenance params |
| M06 | author_role drops specialty | KILLED | ut07_47_analyst_glossary_pending |
| M07 | via="chat" | KILLED | 12 tests incl. st07_02_provenance_from_ctx_and_run_row |
| M08 | message_id read for all roles | KILLED | ut07_47_non_chat_ignores_message_id |
| M09 | KIND_LAYER check off | KILLED | ut07_47_kind_layer_mismatch |
| M10 | PROPOSE_KINDS widened to all kinds | SURVIVED | (M-1; the write path still rejects these kinds) |
| M11 | glossary format check off | KILLED | ut07_47_glossary_without_term (3 params) |
| M12 | glossary term max 2000 | KILLED | ut07_47_glossary_without_term[81 chars] |
| M13 | null confidence → 1.0 | KILLED | ut07_90_null_values_take_defaults, ut07_47_analyst_glossary_pending |
| M14 | null k → 20 | KILLED | ut07_90_null_values_take_defaults |
| M15 | null rationale kept | KILLED | ut07_90_null_values_take_defaults (+2) |
| M16 | register not idempotent | KILLED | ut07_48_register_twice_registers_once |
| M17 | StoreBusy not swallowed | KILLED | ut07_46_store_busy_on_record_use_is_logged |
| M18 | PolicyViolation not mapped | KILLED | ut07_47_policy_violation_names_rule |
| M19 | MemoryNotFound (recall) not mapped | KILLED | ut07_46_not_found_becomes_tool_input_error |
| M20 | render budget unbounded | KILLED | ut07_46_truncated_when_render_drops |
| M21 | truncated always False | KILLED | ut07_46_truncated_when_render_drops |
| M22 | explicit kinds check off | SURVIVED | equivalent (RecallFilters rejects; M-2) |
| M23 | run_meta in the spec's literal order | SURVIVED | equivalent unless meta has "kind" (M-3) |
| M24 | task_id dropped from provenance | KILLED | 12 tests |
| M25 | kinds not deduplicated | KILLED | ut07_46_arguments_reach_the_store |
| M26 | slug "rule" fallback removed | KILLED | ut07_47_rule_id_slug_bounds |
| M27 | slug uses all words | KILLED | ut07_47_kind_data |
| M28 | data.items from all hits (not rendered) | KILLED | ut07_46_truncated_when_render_drops |
| M29 | record_use skipped | KILLED | 4 tests |
| M30 | run-None check off | KILLED | ut07_46/47_run_not_found, st07_02_unknown_run_refused |
| M31 | score not rounded | KILLED | ut07_46_data_shape_and_render |
| M32 | user_correction suggested_action changed | KILLED | 3 tests |
| M33 | "judge" added to RECALL_ROLES | KILLED | ut07_46_constants, ut07_46_disallowed_role |
| M34 | propose without run_ctx | KILLED | 2 tests |
| M35 | author_type="human" | KILLED | 12 tests |

Red-then-green: the builder's unit RED was a collection ImportError before tools.py existed. The ST tests came after the implementation and were shown to catch faults by mutation. These probes confirm that every security branch is pinned: role gates, the chat-only pending owner, the provenance source, KIND_LAYER, the glossary format and the null defaults.

### Builder concern: NUMBER_REF_SCHEMA imported from herness.harness.swarm.tools
- **Layering: OK.** Both modules are in L4 `herness.harness`. The import-linter "herness layers" contract only orders top-level packages, and lint-imports keeps 13 of 13 contracts. The import is recorded in the §2 row of `_tools_args.py`. Spec 05 publishes no NumberRef schema object, and the swarm one implements the U05-10 row_key fallback, so importing it is better than copying it.
- **Cycle today: none.** No module under `herness.harness.swarm` or `herness.harness.blackboard` imports `herness.harness.memory`, and `memory/__init__.py` imports nothing. A fresh `import herness.harness.memory.tools` succeeds. It does pull in harness.swarm, swarm.routing, swarm.spawn, swarm.tools and blackboard.
- **Latent risk: real, but not for this card.** Once T07-23 makes `memory/__init__` import `tools` (`register_memory_components`), and spec 06 swarm modules import `herness.harness.memory.MemoryStore` at module level (`build_swarm_from_config`, T07-23 dependencies), a chain swarm → memory → tools → _tools_args → swarm.tools appears. Recommendation (⚠️ for T07-23 or a spec 05 follow-up): move `NUMBER_REF_SCHEMA` into `herness.harness.tools` (spec 05) and have both swarm and memory import it from there. Otherwise T07-23 should import the tools lazily inside `register_memory_components`.

### Spec readings assessed
- (5) run_meta `{**run.meta, "kind": run.kind}`: agreed. The run row's kind is authoritative, and it is safer against a meta key overriding it. The result is equivalent for current rows.
- (6) kinds deduplicated: agreed. The schema has no `uniqueItems` on kinds and `RecallFilters` rejects duplicates, so deduplicating avoids a spurious error.
- (7) Glossary strip: agreed. A "rule" slug fallback is needed because policy requires a non-empty `rule_id`. But see I-1: the slug itself is the privacy problem.
- (8) A null rationale is omitted and entities is always present: agreed and consistent with the U07-64 step 5 "copied into data".
- (9) `numbers` come from stored `data.numbers` dicts: correct, since the writer stores them there (_write_steps.py:216-218).

### Assessment
**Task quality:** Needs fixes.
**Reasoning:** The implementation, schemas, role and scope enforcement and tests are strong, with every security branch pinned by a killed mutant. One plan-mandated privacy defect remains: `rule_id` is derived from unredacted content and exempt from redaction. It needs a ruling and a fix with a planted-PII test. The minor items are optional.

Worktree status after verification: clean (all probes reverted; the temp probe test was deleted).


## Re-review round 1 (head b4b50d1, base 28ae0fe; diff T07-11-fix1.diff)

**Verdict: Approved.**

### Scope checked
- **I-1 fixed ✅.** `_tools_args.py:119-121` now slugs `rule_id` from `redact.get_redactor().redact(content).text`. A `RedactionFailed` is not caught, so it fails closed. That matches the write path, which also lets `RedactionFailed` from its own redaction propagate. `_write_steps.py` is untouched. The `redacted else ""` branch is only reachable for `None` text, and `content` is always a str by then (tools.py:211).
- **M-1 fixed ✅.** `test_ut07_47_unproposable_kinds_rejected_before_store` covers `mapping`, `sql_template`, `qa_pair` and `run_summary`. It checks for a `ToolInputError` before the run lookup and confirms no store call is made.
- **M-3 fixed ✅.** `test_ut07_46_run_row_kind_wins_over_meta` covers both tools.
- **New tests are correctly named and docstringed.** ST07-05 is the TH07-05 test ("planted email/name absent from SQLite"): `test_st07_05_rule_id_carries_no_planted_personal_data` runs through the real write path and asserts that neither the stored `rule_id` nor the stored content holds the planted email or name.
- **Gates:**
  - 70 card tests pass; 625 pass across memory, security and tools.
  - Coverage is 100% line and 100% branch on tools.py and _tools_args.py.
  - ruff format and check are clean on herness and tests.
  - mypy (344 files) is clean.
  - lint-imports keeps 13 of 13. The `herness.core.redact` import is L4 → L0, which is allowed.
  - Line budgets: tools.py 239/280, _tools_args.py 155/170.

### Probes (all reverted byte-for-byte)
| # | Mutation | Result | Killed by |
|---|----------|--------|-----------|
| M10 | PROPOSE_KINDS guard widened to all kinds | KILLED | ut07_47_unproposable_kinds_rejected_before_store (4 params) |
| M23 | run_meta in the spec's literal order (meta kind wins) | KILLED | ut07_46_run_row_kind_wins_over_meta |
| R1 | I-1 revert: `slug(content)` | KILLED | ut07_47_rule_id_slug_is_redacted, st07_05_rule_id_carries_no_planted_personal_data |
| R2 | catch RedactionFailed, empty slug | KILLED | ut07_47_rule_id_redaction_failure_propagates |
| R3 | catch RedactionFailed, slug the raw content | KILLED | ut07_47_rule_id_redaction_failure_propagates |
| M11, M26, M27 | glossary check off / slug fallback / slug word limit (regression) | KILLED | as in round 1 |

That leaves M22 as the only round-1 survivor. It is an equivalent mutant (M-2, parked).

### The get_redactor monkeypatch: does it hide a gap in the real path?
- **Real path: no gap by design.** Impl 07 §3.21 (`get_memory_store`, line 2160) builds `MemoryStore.from_config` with `get_redactor()`. That is the same process-wide singleton (`herness.core.redact._State.redactor`, U10-45) that the tool now calls. In production, the tool's slug and write step 3 therefore use the same redactor object.
- **The test patch mirrors production; it does not hide anything.** `real_store` points `get_redactor` at the write path's own `base.redactor`. The unit file's autouse fixture installs a redactor that masks the planted name. Without the patch, `get_redactor()` would build a config redactor after the `ops_store` config reset, which is a different object from the writer's. The patch restores the production identity.
- **Divergence is possible in only two cases, and neither leaks data:**
  - (a) A `MemoryStore` built with an injected redactor that is not `get_redactor()` (tests, eval harnesses).
  - (b) The process redactor is reset after the store is built (a config reload), so the tool uses the new redactor and the writer the old one.
  - In both cases the slug is still redacted by a valid, config-derived redactor. The worst outcome is that the two pseudonym sets differ (a different directory or key), not unredacted data.
- **⚠️ for T07-23 (Minor, not blocking):** keep `from_config` on `get_redactor()` as the spec says. If the facade ever accepts an injected redactor, consider having the tool reach the store's redactor instead of the global. `MemoryToolStore` does not expose one today.

### Spec note accuracy
- Note (7) under U07-63 (docs/impl/07-memory.impl.md:1500) is accurate:
  - The slug comes from `get_redactor().redact(content)`, "the process redactor the write path uses". This is true per §3.21.
  - `rule_id` is an `ID_KEYS` value that write step 3 does not redact. This is true per `_write_steps.py:43-47`.
  - TH07-05 is the right threat ("Personal data stored in memory…", line 2434).
  - `RedactionFailed` propagates (fail closed), and the "rule" fallback now applies to the redacted words. Both match the code.

### Remaining (parked by ruling, unchanged)
- M-2, M-4, M-5.
- The NUMBER_REF_SCHEMA import cycle carry-over to T07-23 or spec 05.

Worktree status after re-verification: clean.
