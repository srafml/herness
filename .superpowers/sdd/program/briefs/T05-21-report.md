# T05-21 report — Prompt files (build)

Worktree: D:\herness\.claude\worktrees\agent-a2a0589b020bebead (branch worktree-agent-a2a0589b020bebead, base 4b5a7bf)
Checkpoints: dc17ad5 (_common, planner, judge), b6b9a69 (7 analysts), 721334b (skeptic, writer, writer_retrospective, chat + tests). Final commit: see controller ledger / git log (feat(harness): T05-21 prompt files).

## Files (herness/harness/roles/prompts/, all <= 150 lines)
_common.md 82 | planner.md 54 | judge.md 30 | analyst_ops 47 | analyst_change 46 | analyst_delivery 46 | analyst_org 47 | analyst_crosscheck 48 | analyst_retrospective 47 | analyst_general 45 | skeptic.md 61 | writer.md 64 | writer_retrospective.md 12 | chat.md 43 — 14 files.
Content follows each unit's postcondition order (sections as headings). Tests: tests/unit/harness/roles/test_roles_prompts.py (UT05-94), tests/security/test_st05_prompts.py (ST05-21). pyproject.toml: `artifacts = ["herness/harness/roles/prompts/*.md"]` under [tool.hatch.build.targets.wheel]. No Python module changed; uv.lock unchanged.

## Tests
- UT05-94 (64 cases): file set read via importlib.resources == union of prompt_files over get_role(ROLE_NAMES) + writer retrospective == the 14 names, no verifier_claim.md; <= 150 lines, title line, trailing newline, no tabs; required strings per unit present IN postcondition order; _common.md has [[n1]], NumberRef, <untrusted_data, unknowns, query_id and no file has <ticket_text>/<memory_context>; analysts have exactly the four ## sections and six one-line pitfalls in SkepticCheck order; numeral check via herness.core.numbers.find_uncited with the reports.allowed_numeral_patterns set plus per-unit exceptions (plus a bite test); skeptic thresholds == herness.harness.pipelines.settings.SkepticSettings defaults (13, 30, 25 %); prompt_hash 16 hex, stable per role and fresh RoleSpec, unique per role; tmp copy via monkeypatched _prompts_root hashes identically and editing one file changes exactly the roles that list it (parametrized over all 14); each real role's system block 1 (prompts + schema + real metric catalog) built under the 60,000 cap with _common.md first; only roles/base.py in herness/harness references the prompts dir.
- ST05-21 (3 cases): enumerates files from the package directory (importlib.resources.as_file), asserts 14 scanned, zero hits from (a) spec 10 U10-36 detectors `build_detectors(RedactionConfig())` (CREDENTIAL, URL_TOKEN, EMAIL, CARD, PHONE, IP; the config-pattern ones are empty by default), (b) detect-secrets in-process with default_settings (all plugins, same set as .secrets.baseline), (c) URL scheme / www. check and a hostname check outside code spans; a bite test plants an AWS-key shape, a GitHub token, a URL and a hostname (built at run time).
- RED: 63 failed / 3 passed before the prompt files existed (FileNotFoundError on prompts/). GREEN: 67 passed.
- `PYTHONUTF8=1 uv run pytest tests/unit/harness tests/security/test_st05_prompts.py -q -p no:logging`: 1468 passed, 1 skipped (symlink privilege). `tests/security -m "not slow"`: 638 passed, 1 skipped.
- Gates: ruff check pass; ruff format --check pass (737); mypy 0 (286); lint-imports 13 kept 0 broken; check_module_size 0; check_type_ownership 0. Commits ran all hooks, no SKIP.

## Wheel
`uv build --wheel` (TMP/TEMP and out-dir under the worktree's .agent-tmp on D:, deleted afterwards): before AND after the pyproject change the wheel holds all 14 herness/harness/roles/prompts/*.md (hatch `packages` already ships non-.py files; enrich and eval prompts too); file lists identical (317 entries). The `artifacts` line is the explicit include the brief asked for (belt and braces against VCS ignores). Extracted wheel on sys.path: importlib.resources lists 14 files and get_role("writer", variant="retrospective").prompt_hash equals the source tree's (9e2e72d14d50e6f1).

## Numeral handling
Allowed everywhere: the reports.allowed_numeral_patterns set (years, ISO dates, quarters, record ids; mirrored as a test constant from config/app.yaml). Per-unit exceptions: planner (0–5 scale, 400-char note limit), judge (0–5), skeptic (13, 30, 25 % — U05-55 invariant "thresholds match spec 06 §5.7 defaults" takes precedence over a numeral ban; they are also asserted equal to SkepticSettings defaults). Skeptic.md adds "when the input states other thresholds, use those" because SkepticSettings says the thresholds are passed in the Skeptic prompt input. Analysts' pitfall lines state the checks without thresholds (U05-54 bans numerals). Rule labels ("Rule 1") were dropped from _common.md to stay numeral-free; sections are headings instead. Other limits are spelled out ("at most three", "at most ten").

## Spec notes
- File count is 14, not the card's/module map's 15: verifier_claim.md removed by R-37 (not created; test asserts absence).
- Planner: PlannedTask (R-28) has no tools or budget field, so "pick tools only from the analyst allow-list" is phrased as "name only analyst allow-list tools, in notes", and "do not invent budgets" as "every task gets the budget given in the input; never state a different one". "change only inputs.notes (<= 400 chars)" maps to PlannedTask.notes (type allows 1,500; prompt states 400 per spec).
- Design §5.5 rule 5 (<ticket_text>/<memory_context>) replaced by the R-20 delimiter and its four sources (warehouse, memory, scratchpad, truncation); the prompt also tells the model the block's <, >, & are escaped.
- Skeptic "claim test" is section (5) per the postcondition order, while its text says it is done before the six checks.
- writer.md says prior_outcomes_commentary is null unless the appended retrospective instructions ask for it.
- chat cloud mode: names get_record, get_cluster, semantic_search as the text tools that may be absent (spec: "text tools read enrich.text_redacted").

## Concerns
- None blocking. Hostname check is heuristic (TLD list, code spans stripped because dotted table names like `score.org` are legitimate); the U10-36 URL_TOKEN detector and the scheme check are the strict part.
- The ST test's 14-file count is deliberate: adding a prompt file fails it until the count/set is updated (forces a review of the new file).

## Final
Final commit 1916288 feat(harness): T05-21 prompt files (pyproject artifacts line; all hooks passed, no SKIP). Tree clean.

## Fix round 1 (review Approved, Minors M1-M4; M5 parked)
- M1 planner.md: wildcard `dedup_key` is JSON `null` (both places), never "empty" (PlannedTask.dedup_key is str | None with a pattern). New test test_ut05_94_planner_wildcard_dedup_key_is_null pins it (and "empty" absent).
- M2 ST05-21 hostname check now covers code spans too: every dotted token anywhere must be a warehouse reference whose first label is a known schema (core, enrich, score, metrics) or a *.md file name; a token whose last label is a host suffix is a hit unless explicitly allow-listed (only `score.org`); localhost anywhere is a hit. Dotted tokens actually in the prompts: score.org, score.action_lever, score.portfolio.*, score.funding.*, metrics.metric_value, core.service_map.link_source, core.team.active, enrich.cluster_member, enrich.incident_change_link — all pass. Bite cases added: backticked `wh-01.corp.internal`, `vllm.gpu-pool`, `LOCALHOST`, `acme.org`; negative case lists the legit tokens.
- M3 _ALLOWED_NUMERALS now comes from the owner default ReportsSection().allowed_numeral_patterns; test_ut05_94_allowed_numerals_match_config asserts it equals config/app.yaml reports.allowed_numeral_patterns (yaml.safe_load).
- M4 removed the needless shutil.rmtree on tmp_path.
- Tests: tests/unit/harness/roles + ST05-21: 142 passed. ruff check/format clean; mypy 0 (286) and the two test files clean under mypy.
- Fix commit: c105190 (new commit on 1916288, all hooks passed, no SKIP). Tree clean.
