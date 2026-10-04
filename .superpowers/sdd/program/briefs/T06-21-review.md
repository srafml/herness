# T06-21 review (verify agent): Run lifecycle helpers

Worktree agent-abfb1185f470fdf3d, head ab6e925, base 3f61b67. Files: herness/harness/swarm/lifecycle.py (264 lines, new), tests/unit/harness/swarm/test_swarm_lifecycle.py (524, new), tests/unit/harness/test_tools_recording.py (+1).

### Spec Compliance
- ✅ U06-76 RunRequest: fields, bounds, extra=forbid, strict=False, chat⇒question (also rejects ""). Scenario stand-in `list[str]` (≤5, 1–64) with `# T04-20:` marker at lifecycle.py:86 per ruling.
- ✅ U06-77 RunResult: all seven fields, frozen, extra=forbid.
- ✅ U06-83 request_config_hash (lifecycle.py:114-122): `"cfg_" + sha256_hex(canonical_json({"config": config_hash(cfg), "request": {...}}))[:16]`; request = model_dump(mode="json") of exactly kind, depth, question, focus, scenarios, budget_override plus `profile` = resolved argument (ruling). sha256_hex = hashlib.sha256(utf-8).hexdigest(); canonical_json is sort_keys + (",",":"). Pure: no clock, no I/O, no randomness; deterministic across processes (only canonical_json/config_hash inputs). build_id/session_id excluded.
- ✅ U06-137 create_run_record (142-197): review-kind ConfigError, resolve_knobs (ConfigError), current_build None -> NotFound("no promoted warehouse build"), all before any write; run_id = "run_" + new_ulid(); meta keys exactly request/stage/escalated_from/render_error/blocked_reason/job_id (D06-26 via meta.job_id); planner spec role/scope run:[run_id]/objective "Plan the <kind> review."/default_tools/role_budget(writer_tokens=0)/model_role/compute_dedup_key; insert_run + insert_tasks in ONE `run_write(create, op="swarm_create_run")` on the writer (run_on_writer_sync).
- ✅ U06-84 handle_budget_exhausted (200-227): select pending analyst/skeptic; per task on the writer claim_task then fail_task(BudgetExceeded("run_budget")) -> dead; set_run_status(to="verifying", allowed_from={"running","challenging"}) in one run_write (op swarm_budget_exhausted); running tasks untouched; WARNING harness.budget.exhausted with only run_id, phase="analysis", tasks_dead.
- ✅ U06-86 swarm_health (230-264): select_runs(open statuses, review kinds), HernessError -> down + class name; open runs and not worker_alive() -> degraded "no live worker for N open runs"; two list_jobs calls (queued/running, kind review, limit 50); stalled = open run with now - started_at > 24 h (injected now only) referenced by neither payload.run_id nor meta.job_id; else ok. Read-only, no side effects on any run.
- ✅ ENG §3.4: no request/agent free text in ids, log fields or error messages (ConfigError message carries only the RunKind literal; health reasons carry only class names, counts, internal run ids).
- ⚠️ Cannot verify here: IT06-03, IT06-32, ST06-13 (later cards); T06-13 binding of list_jobs/worker_alive to herness.core.jobs.queue.

Accepted rulings applied correctly: D06-26 meta.job_id; run_id ULID-based; injected list_jobs/worker_alive/now; Scenario stand-in with marker; swarm/__init__.py untouched; hash uses resolved profile; current_build -> None -> NotFound before writes; allow-list entry.

Deviations noted by the builder and acceptable: list_jobs HernessError also -> down (report note 5; consistent with "unreadable store"); op name swarm_budget_exhausted (unspecified by spec); ok carries reason "".

### test_tools_recording.py statement
The only change is one allow-list entry in `_NON_ROW_HASHES` (line 362):
```
@@ -359,6 +359,7 @@ _NON_ROW_HASHES = {
     ("memory/tokens.py", "_message_key"),  # per-message token-count cache key (U07-68)
     ("memory/store.py", "embed"),  # embedding LRU cache key (U07-48)
     ("roles/base.py", "prompt_hash"),  # prompt file version hash (U05-49)
+    ("swarm/lifecycle.py", "request_config_hash"),  # run.config_hash (U06-83)
 }
```
Confirmed by diffing 3f61b67..HEAD for that file: 1 insertion, 0 deletions, nothing else.

### Gates (run by verifier)
- `pytest tests/unit/harness/swarm/test_swarm_lifecycle.py tests/unit/harness/test_tools_recording.py -q -p no:logging --cov --cov-branch`: 148 passed; lifecycle.py 123 stmts / 22 branches, 100% line, 100% branch.
- ruff check (3 files): clean; ruff format --check: clean; mypy lifecycle.py + test file: no issues; tools.check_module_size: exit 0. (Run via worktree .venv python.)
- Test naming/IDs/docstrings/pytestmark=unit: compliant; UT06-54/55/57/91 each have several functions with real assertions.

Mutation probes (lifecycle.py restored by checkout afterwards; worktree status clean apart from .agent-tmp/):
- `now - started_at <= STALL_AFTER` -> `<`: survived (no 24 h boundary test).
- drop `statuses={"pending"}` from the select_tasks call in handle_budget_exhausted: survived, but it is an equivalent mutant in practice (claim_task returns False for non-pending tasks).
- By inspection the following would be caught: dropping depth/profile from the hash, moving resolve_knobs/current_build after the write, widening allowed_from, dropping either job reference check, dropping the log fields.

### Strengths
- Validation strictly before the single write transaction; tests prove "nothing written" for bad override, no build and chat kind.
- Health check is fully injected, read-only, and tested against a real broken store (renamed run table) as well as a monkeypatched error.
- Log event field set asserted exactly (no extra keys beyond component).

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. tests/unit/harness/swarm/test_swarm_lifecycle.py:356-365 — no boundary case at exactly 24 h; the `<=` vs `<` mutant at lifecycle.py:235 survives. Add a run at age_h=24 (ok) next to the 25 h case.
2. tests/unit/harness/swarm/test_swarm_lifecycle.py:172-214 — atomicity of run + planner (one run_write) is not tested; splitting lifecycle.py:192-196 into two transactions would pass. Optional: make insert_tasks raise and assert no run row.
3. UT06-55 (test_swarm_lifecycle.py:263-283) does not include a focus value in the stability check (focus serialisation via model_dump json is covered only indirectly). Carry-over for T04-20 Scenario objects already recorded by the builder.

### Assessment
**Task quality:** Approved
**Reasoning:** All six units match the spec and the accepted rulings, validation precedes the single create transaction, budget exhaustion and health behave as specified with no free text leaks, and gates pass with 100% line/branch coverage; only minor test-strength gaps remain.


## Re-review r1

Scope: fix round 1 (ab6e925..e5e95cd), tests only.

- lifecycle.py byte-identical to ab6e925: ✅. The diff stat for ab6e925..HEAD lists only tests/unit/harness/swarm/test_swarm_lifecycle.py (+56/-6), and the lifecycle.py diff is empty.
- M1 ✅: `test_ut06_57_exactly_24h_is_not_stalled` covers runs aged 24 h (an exact timedelta) and 24 h - 1 s, and expects ok. `test_ut06_57_just_over_24h_is_stalled` covers 24 h + 1 s and expects degraded "stalled run <id>". Verifier probe: `<=` -> `<` at lifecycle.py:235 turns red (test_ut06_57_exactly_24h_is_not_stalled; 1 failed, 31 passed).
- M2 ✅: `test_ut06_54_run_and_planner_in_one_transaction` wraps insert_tasks so that it writes and then raises, checks that the run row was visible inside the transaction (`seen == [1]`), expects FatalError, and asserts that no run or task rows remain. Verifier probe: insert_run and insert_tasks split into two run_write calls turns red (1 failed, 31 passed).
- M3 ✅: the stability case sets focus=EntityScope(team, [t1, t2], period_start 2026-01-01) and round-trips it through model_dump_json / model_validate_json. A changed focus gives a different hash.
- Rules ✅: the new functions carry the UT06-54/55/57 IDs in their names and on the first docstring line, and module `pytestmark = pytest.mark.unit` is unchanged.
- Gates: card file 32 passed; ruff check, ruff format --check and mypy on the test file are clean.
- Probes restored with checkout; git status is clean apart from .agent-tmp/. The index was not touched.

No new findings.

**Verdict: Approved**
