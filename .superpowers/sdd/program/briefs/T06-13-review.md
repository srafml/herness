# T06-13 review: Routing, hooks and tool context

Reviewer: verify agent. Worktree agent-a91fdf51a82d3b12e, base 10f9733, head 1d4b2eb. The worktree is clean after the review (every probe reverted).

**Verdict: Approved** (no Critical or Important findings; 7 Minor findings)

## Evidence I ran (PYTHONUTF8=1, TMP/TEMP=...\w31-s06)
- `pytest -k "UT06_63 or UT06_65 or UT06_66 or UT06_67 or UT06_92 or ST06_19"`: 41 passed.
- `pytest tests/unit/harness/swarm tests/unit/harness/pipelines tests/security/test_st06_task_input.py`: 298 passed.
- Coverage with branches, card tests only: routing.py 100 %, _routing_ctx.py 100 %, _task_input.py 100 %, swarm/__init__.py 100 %.
- `ruff check` and `ruff format --check` on the 7 touched files: clean. Project `mypy` (pyproject `files = ["herness","tools"]`): 0 issues in 384 files.
- `lint-imports`: 15 kept, 0 broken. `tools.check_module_size`: exit 0. `tools.check_type_ownership`: exit 0.
- Import probe: `import herness.harness.swarm` loads only 2 `herness.harness` modules and not lifecycle or routing. The first attribute access loads lifecycle. Importing `_routing_ctx`, `_task_input` and `routing` each in a fresh process works, so there is no cycle.

## Spec compliance per unit
| Unit | Status | Note |
|---|---|---|
| U06-139 RunEnv | ✅ ⚠️ | Has every spec field. It adds `warehouses`, `ops` and `vectors`, which U06-100 needs and U06-139 does not list (a spec-text gap). `metrics` uses a private `_MetricSink` stand-in. IT06-32 is not on this card. |
| U06-96 map_agent_result | ✅ | Matches the postcondition: input tokens = input + cache_read + cache_write, `stop_cause` keeps any stop reason, `extra` is merged last. |
| U06-98 route_task, Route | ✅ ⚠️ | Steps 1 to 5 are implemented as written. Off-network also counts the `config.off_network` flag. That only errs on the safe side and matches spec 05 `_is_off_network`. |
| U06-99 build_hooks | ✅ | Chain is None on fallback, otherwise `ModelChain(model_role, registry, run.depth, gpu_state())`. Compactor is built per task, phase is the run status, and stop and tracer are passed through. |
| U06-100 build_tool_context | ✅ ⚠️ | All fields are set. A hybrid off-network route keeps three tools. `egress_purpose` follows the rule. Uses `WarehousePool.get`, because spec 05 has no `handle`. Extra: `task_tools` outside the final tool names are dropped (least privilege). A missing `build_id` raises ConfigError. |
| U06-141 build_task_input | ✅ | Wraps every listed field with its source and record_id. Extra, on the safe side: `claim` is matched at any path. `prior_context` and `session` pass through. Unknown roles (judge, verifier) raise ConfigError. This matches the spec: the judge input in U06-89 step 6 is not routed through `build_task_input`. |

## Spec compliance per test row
| Test | Status |
|---|---|
| UT06-63 | ✅ partial: true, tokens summed (112/20), any stop reason, extra merged |
| UT06-65 | ✅ Real registry: a missing `skeptic_final` routes to the skeptic's key. Strict registry: `model_role` becomes the base role for all three entries. Hybrid with the cost cap reached gives local-30b with `fallback_local=True`. Also covered: below the cap, non-hybrid profile, no local key (ConfigError), off-network detection matrix, anthropic. |
| UT06-66 | ✅ Chain None on fallback (and the gpu reader is not called), stop and tracer are the same objects, ModelChain built for the routed model role |
| UT06-67 | ✅ Exactly three tools, `reasoning_final` for the writer and for skeptic_final, `reasoning` for the analyst, None on a local route |
| UT06-92 | ✅ notes, revision objective, claims, required actions at two paths, challenge_summary, chat question with an escaped closing tag, prior_context and session unchanged, unknown role raises, tuples kept |
| ST06-19 | ✅ Uses the real `run_agent` with a recording client. Each of 3 texts sits in exactly one `<untrusted_data>` block with source and record_id checked. The closing tag is escaped, the raw attack string is absent, no tool calls are made, tool names and budgets are unchanged, and one charge is recorded. |

## Judgement on the builder's deviations
1. **Module split.** routing.py is 226/330, _routing_ctx.py 179/200, _task_input.py 109/130. Both new §2 rows are present in the same commit (docs line 88-89 in the diff). The `_routing_ctx` dependency column matches its run-time imports. Accepted. The routing.py row's dependency column omits `herness.harness.roles` (M3).
2. **RunEnv extra fields and `WarehousePool.get`.** Both are needed. `herness/harness/warehouse.py:201` defines `get(build_id)` and there is no `handle`. Accepted as a spec-text note (⚠️).
3. **`_MetricSink`.** No `MetricSink` type exists in `herness/`. The only other occurrence is the private stand-in at `herness/harness/blackboard.py:74`. Confirmed absent. The stand-in is duplicated (M4).
4. **BASE_MODEL_ROLE path.** Spec 05 `model_for` and `chain_for` (`herness/harness/llm/registry.py:109-137`) already fall back to `BASE_ROLE`, so with the real registry this path is unreachable. The `_StrictRegistry` test exercises it, and mutation P4 is killed. UT06-65 asserts the brief's outcome: the skeptic's key, and a local key with `fallback_local`. ⚠️ With the real registry `model_role` stays `skeptic_final`, so `egress_purpose` is `reasoning_final`. A literal spec registry would switch to `skeptic`, which gives `reasoning`. Suggest a spec note.
5. **off_network flag, task_tools filtering, claim at any path, judge and verifier ConfigError.** All are fail-closed or safe-side, and none contradicts the spec. Accepted.
6. **C1.** The claim is correct. Spec §2 (docs line 113-121) puts `herness.harness.swarm` at rank 2 and the pipelines modules at rank 3, and rank 3 may import only lower ranks. `_review_common` serves rank-3 pipelines, so it cannot import `swarm.routing`. That contradicts the "stand-ins until T06-13" wording in the §2 row for `_review_common` (docs line 108). The new UT06-68 parity test guards drift. pyproject has no harness-internal "layers" contract yet, so the rank rule is enforced by the spec only, not by lint-imports (M6). Leaving C1 open is correct, and it needs a spec-owner ruling.
7. **C2.** Lazy `__getattr__` re-exports, 20/20 lines, a cheap package import, no cycle (probe above). Accepted.
8. **Import-linter.** "memory-no-callers (T07-23)" has source `herness.harness.memory` and forbids memory importing swarm, blackboard, pipelines and eval (pyproject.toml lines 622-636). swarm importing memory (TYPE_CHECKING only) is allowed. Result: 15 kept, 0 broken.
9. **Security.** ST06-19 and TH06-06 are satisfied; see the test rows above.

## Findings
### Critical
None.
### Important
None.
### Minor
- M1 `tests/unit/harness/swarm/test_swarm_routing_ctx.py:235,237` and `:291-292`: the same assert is repeated (`route.client.name ==`). This looks like a leftover from the detect-secrets rewrite. Replace one copy with `route.model_role == "skeptic_final"` in the real-registry case, which also pins the behaviour from deviation 4.
- M2 `herness/harness/pipelines/_review_common.py:56`: the marker `# T06-13: replace with herness.harness.swarm.routing.default_tools` is now stale, because the replacement is illegal per §2 ranks. The §2 row (docs `06-swarm-and-pipelines.impl.md:108`, "until T06-13") has the same problem. Reword both to point at the UT06-68 parity guard and the pending spec ruling.
- M3 `docs/impl/06-swarm-and-pipelines.impl.md:88`: the routing.py §2 dependency column omits `herness.harness.roles` (`get_role`) and `herness.harness.pipelines.settings`. Documentation drift only.
- M4 `herness/harness/swarm/_routing_ctx.py:49` duplicates `herness/harness/blackboard.py:74` (`_MetricSink`). Track as a carry-over for the spec 08 `MetricSink` type.
- M5 UT06-65: no single case combines both setup conditions (models.yaml without `skeptic_final` plus hybrid with the cap reached). They are covered separately (`test_swarm_routing_ctx.py:227`, `:285`). A combined case would mirror the test row literally.
- M6 `pyproject.toml`: the harness-internal "layers" contract that spec 06 §2 (docs line 113) calls for does not exist. The rank rule that blocks C1 is therefore not machine-checked. This belongs to the spec 06 owner, not this card.
- M7 The test files are outside mypy's scope (`files = ["herness","tools"]`). Run directly under `--strict`, they report 27 errors, for example an unused ignore at `test_swarm_routing_ctx.py:599` and errors in the pre-existing `test_swarm_routing.py:66,106`. This matches project convention, so it is noted only.

## Mutation probes (19 run, 19 killed, 0 survived)
P1 `question` not wrapped: killed. P2 off-network tool filter removed: killed. P3 skeptic_final not treated as final: killed. P4 base model_role not returned: killed. P5 fallback ignores the cost cap: killed. P6 loopback check removed: killed. P7 chain always built: killed. P8 cache_write tokens dropped: killed. P9 `_PASS` emptied: killed. P10 nested `finding.finding_id` ignored: killed. P11 local fallback takes the chain head: killed. P12 task_tools not filtered: killed. P13 required_actions items not wrapped: killed. P14 `extra` not merged: killed. P15 phase hard-coded: killed. P16 egress purpose always None: killed. P17 hybrid filter applied to every profile: killed. P18 revision objective not wrapped: killed. P19 unknown role accepted: killed.
(Script: C:\Users\santh\AppData\Local\Temp\w31-s06\verifier\mut.py; each probe was restored from a backup copy, and the worktree is clean.)

## Assessment
**Task quality:** Approved
**Reasoning:** All six units match the spec or deviate only on the safe side with documented spec notes. Every card test row is covered and every mutation probe was killed. Coverage is 100 % with branches, and the lint-imports, module-size and type-ownership gates pass. The remaining items are documentation and test polish, plus a spec-owner ruling for C1.


## Re-review round 1 (head da65120, diff 1d4b2eb..da65120)

Scope: M1, M2, M3 and M5 only. Environment as before.

- **M1 ✅** `test_swarm_routing_ctx.py:237` now asserts `route.model_role == "skeptic_final"`, which pins deviation 4. `:292` now asserts `route.role_spec.name == "skeptic"`. No duplicate asserts remain.
- **M2 ✅** The marker at `_review_common.py:56-58` now says the stand-in stays because of the §2 import-rank rule (pipelines are rank 3, swarm is rank 2), that UT06-68 parity guards it, and that replacing it needs a spec-owner ruling. The §2 row for `_review_common` (docs line 108) says the same and no longer says "until T06-13".
- **M3 ✅** The routing.py row now lists `herness.harness.roles` (`get_role`) and `herness.harness.pipelines.settings` (`DepthKnobs`), with resilience marked as reached via `_routing_ctx`. The `_routing_ctx.py` row lists its run-time imports (loop, resilience, jobs) and its type-only imports. Both match the code.
- **M5 ✅** The new `test_ut06_65_hybrid_without_skeptic_final_cost_cap_reached` combines the brief setup: models.yaml without `skeptic_final`, hybrid profile, cost cap reached. Through the skeptic's routing (claude-opus) it gets the local key local-small-cpu with `fallback_local=True`. The strict-registry variant gives `model_role == "skeptic"` and the same local key.
  - Probes, each run against this test alone:
    - Q1 hybrid fallback branch disabled: killed.
    - Q2 local fallback takes the chain head: killed.
    - Q3 `fallback_local` flag not set: killed.
  - Baseline: the test passes. Every probe was reverted. A CRLF artefact my restore introduced was normalised back to LF, and the worktree is clean.
- **Gates:** `pytest tests/unit/harness/swarm tests/unit/harness/pipelines tests/security/test_st06_task_input.py` gives 299 passed. `ruff check` and `ruff format --check` on the 2 touched code files are clean. `check_module_size` exits 0.

**Re-review verdict: Approved.** M4, M6 and M7 remain open as carry-overs, outside the scope of this round.
