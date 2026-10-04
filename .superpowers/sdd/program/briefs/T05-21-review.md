# T05-21 review (Prompt files) — verify agent

Worktree D:\herness\.claude\worktrees\agent-a2a0589b020bebead, base 4b5a7bf, head 1916288. Tree clean at end of review (all probes restored, `git status --short` empty; temp wheel dir deleted).

**Verdict: Approved** (Minor items only)

### Spec Compliance
- ✅ U05-52 `_common.md` (82 lines): sections (1)–(10) in order: two-sentence job; Numbers (markers `[[n1]]`, `NumberRef` id/query_id/column/row_key, allowed numerals with the four examples `2026-09-24`, `Q3 2026`, `INC0012345`, `PAY-123`); Unknowns; Derived values (SQL, stated unit, USD decimal strings); Metrics first; Untrusted data (R-20 `<untrusted_data source="…" record_id="…">`, sources warehouse/memory/scratchpad/truncation, content cannot change instructions/tools/format, ignore + may report as DQ finding); Cause and comparison; Tool errors; Tools (`query_id=` headers, similarity scores/catalog descriptions not citable); Final answer (one JSON object). Required literals present; no `<ticket_text>`/`<memory_context>` in any file.
- ✅ U05-53 planner.md / judge.md: all postcondition items present in order. Planner deviation (tool names "in `notes`", budget "copy the one given, never state a different one") is correct against `PlannedTask` (herness/core/types/swarm/tasks.py:172, docstring "no budget, tool, priority or id field"); the named allow-list equals `_ANALYST_TOOLS` (herness/harness/roles/analyst.py:27). Judge: 0–5 on four criteria, mean, tie to lower index, one-sentence reasons, no tools; matches `JudgeOutput`.
- ✅ U05-54 seven analysts: identical Workflow / Pitfalls / Stop rule (diffed), Specialty focus per unit (ops MTTR/MTTA/repeat/SLA/clusters; change CFR, `enrich.incident_change_link`, lead time, emergency; delivery cycle time, unplanned share, carryover, epics/features; org peer median `score.org`, `score.action_lever`; crosscheck independent path, never original SQL, both values + agreement; retrospective against `outcome` rows only with the four verdicts; general). Six pitfall lines, one each, no numerals.
- ✅ U05-55 skeptic.md: six checks with spec 06 §5.7 content, concern/fail need one query + `query_ids`, verdict rules, claim test (section 5, text "before the six checks", fail of the closest check, R-37), `SkepticOutput` fields match herness/harness/roles/skeptic.py:20. No verifier_claim.md (R-37); 14 files vs card's 15 is correct and asserted.
- ✅ Thresholds: 13 weeks / below 30 / exceeds 25 % equal `SkepticSettings` defaults (herness/harness/pipelines/settings.py:64-69: min_weeks_seasonality=13, min_sample=30, single_record_share=0.25); UT05-94 asserts this from the class.
- ✅ U05-56 writer.md: section ids in order + omit-unsupported; paragraph rules; spec 06 §5.8 recommendation rules (fund refs with the exact source columns, org_action levers, expected_usd_ref = top lever delta_usd, best first, never rec_id/rank); caveats; `WriterOutput` fields. writer_retrospective.md: one paragraph, per-recommendation verdict from outcome rows, "unknown". chat.md: concise, markers + numbers + query_ids, unknowns, ≤ three followups, escalate rule, cloud mode, `ChatAnswer` fields match drafts.py:213.
- ✅ All files ≤ 150 lines (max 82).
- ✅ ST05-21: files enumerated from the package dir via importlib.resources (not a hard-coded list), count asserted (14); real U10-36 `build_detectors(RedactionConfig())`, detect-secrets in-process (`SecretsCollection` + `default_settings`), scheme and hostname check. Probes (each restored): planted password + AWS key in chat.md → FAIL (detect-secrets hit); `build.example.com` in prose → FAIL; `http://` URL → FAIL; extra `extra.md` file → 2 FAIL (count); stray "42 percent" in analyst_ops.md → UT05-94 numerals test FAIL.
- ✅ Single resolver: only `herness/harness/roles/base.py:64` `_prompts_root` reads the roles prompts (grep over herness/; enrich/deciders/llm.py:45 reads its own enrich prompt, unrelated). UT05-94 also asserts this.
- ✅ Package data: pyproject `[tool.hatch.build.targets.wheel] artifacts = ["herness/harness/roles/prompts/*.md"]`; `uv build --wheel` into D:\herness\.t0521-wheel → wheel lists all 14 prompts, 317 entries no duplicates; dir deleted. UT05-94 reads via `importlib.resources`.
- ✅ prompt_hash: 16 hex, stable twice and for a fresh RoleSpec, unique per role; tmp copy via monkeypatched `_prompts_root` hashes identically and editing each file changes exactly the roles listing it (parametrized over all 14).
- ✅ Numerals via `find_uncited` with the reports.allowed_numeral_patterns set plus per-file exceptions only (planner 0–5/400, judge 0–5, skeptic 13/30/25 %), with a bite test.
- ✅ Hooks: .secrets.baseline and .pre-commit-config.yaml untouched; `python -m tools.check_module_size` rc 0; test IDs in names/docstrings; `pytestmark = pytest.mark.unit` on both files.
- Test run: `pytest tests/unit/harness/roles tests/security/test_st05_prompts.py` → 140 passed; ruff check pass; ruff format --check pass; mypy (tests + herness/harness/roles) no issues.
- ⚠️ None material.

### Issues
#### Critical
None.
#### Important
None.
#### Minor
1. herness/harness/roles/prompts/planner.md:24-25, :51 — "leave their `dedup_key` empty" / "empty for a wildcard": `PlannedTask.dedup_key` is `str | None` with pattern `^[0-9a-f]{16}$` (tasks.py:23,175), so an empty string fails validation; say `null`. (The JSON schema in system block 1 mitigates, but the prose invites `""`.)
2. tests/security/test_st05_prompts.py:28,52 — hostname check strips code spans, so a hostname in backticks passes (probe: `` `ops.example.com` `` → 3 passed). Scheme URLs and credentials are still caught everywhere; narrow the exemption (e.g. only strip spans whose TLD-like suffix is not in `_HOST`'s list, or allow-list known schema names).
3. tests/unit/harness/roles/test_roles_prompts.py:52 — `_ALLOWED_NUMERALS` mirrors config/app.yaml:15-16 by hand (currently identical); loading from config would prevent drift.
4. tests/unit/harness/roles/test_roles_prompts.py:379 — `shutil.rmtree(root)` on tmp_path is redundant (pytest cleans it) and skipped on assertion failure anyway.
5. herness/harness/roles/prompts/writer.md:40 — "cite `expected_metric`": it is a metric name (string), not a ref; spec 06 rule is "equals the `metric` of a lever row of `target_id`". Wording follows the unit text; clarifying would reduce `rule:expected_metric` drops.

### Assessment
**Task quality:** Approved
**Reasoning:** All 14 prompt files meet their unit postconditions in order, thresholds match `SkepticSettings`, the ST05-21 lint enumerates the package dir with the real U10-36 detectors and detect-secrets and bites on planted values, and the wheel ships the prompts; remaining items are wording/test-robustness polish.

---

## Re-review round 1 (head c105190; scope M1–M4, M5 parked as spec wording)

**Verdict: Approved**

- ✅ M1 planner.md:24-25, :51 now say JSON `null` / "`null` for a wildcard"; no "empty" remains. New test `test_ut05_94_planner_wildcard_dedup_key_is_null` pins it.
- ✅ M2 tests/security/test_st05_prompts.py: code-span exemption removed. Every dotted token, in prose or in backticks, is a hit unless it is a `core|enrich|score|metrics` reference, a `.md` file name, or exactly `score.org`. A host suffix (com, org, io, internal, corp, lan, test, example, …) makes it a hit even with a schema first label. Localhost is checked case-insensitively. Probes run by calling `_url_hits` directly; no tracked file was touched because the builder's suite was running concurrently. Hits: `ops.example.com` in backticks, `build.acme.io`, `10.0.0.12`, `db01.corp`, `user@wh.acme.com`, `wh.corp:5432`, `score.org.evil.com`, `Score.org` (the exemption is exact-case), `metrics.corp.internal`, `vllm.gpu-pool` in prose, `score.example`, `host.lan`, `api.openai.com`, `core.mycompany.net`. No hits for `core.service_map.link_source`, `score.org`, `planner.md`, or a bare `hostname`. The 14 real prompt files give zero hits. The rule is not loose: the only gap is a token whose first label is a warehouse schema name and whose suffix is not a host suffix (`core.acme-db`, `enrich.gpu-host` → no hit). This is accepted as indistinguishable from a warehouse reference (Minor, no action needed).
- ✅ M3 `_ALLOWED_NUMERALS` now comes from the owner default `ReportsSection().allowed_numeral_patterns` (herness/reports/settings.py:94-96), and `test_ut05_94_allowed_numerals_match_config` ties it to config/app.yaml `reports.allowed_numeral_patterns` (path parents[4] = repo root; passes).
- ✅ M4 redundant `shutil.rmtree` and its import removed.
- Runs (targeted only; full suite not run, per instruction): tests/security/test_st05_prompts.py + tests/unit/harness/roles/test_roles_prompts.py → 69 passed; ruff check and format --check clean; mypy clean on both files. `git status --short` clean.

### New findings
- Minor: tests/unit/harness/roles/test_roles_prompts.py (`test_ut05_94_planner_wildcard_dedup_key_is_null`): `assert "empty" not in text` bans the word anywhere in planner.md and would break on unrelated wording ("an empty `unknowns` list"). Also `"`dedup_key`\n  to JSON `null`"` pins the line wrap. Both are brittle but harmless; optional.
