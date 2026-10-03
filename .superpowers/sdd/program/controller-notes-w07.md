# Controller notes for wave w07 (base 6b70189) — binding for every sub-controller in this wave

Read with: subcontroller-instructions.md (incl. addenda), global-constraints.md, and the group ledgers named in your dispatch.

## Process rules (apply, do not re-decide)
- PYTHONUTF8=1 for pytest. The repo has a real .pre-commit-config.yaml: commits run the hooks (ruff, whitespace, detect-secrets with .secrets.baseline, detect-private-key, module-size, unit tests, …). Never --no-verify. Do NOT set PRE_COMMIT_ALLOW_NO_CONFIG.
- Secret-looking fixtures: `uv run detect-secrets scan --baseline .secrets.baseline` in the same commit, then diff the baseline's file list against the previous version and restore any dropped audited entries (the regen drops the docs/impl entries every time); keep LF endings.
- If `uv run` fails because D:\herness\pyproject.toml is mid-merge, use `<worktree>\.venv\Scripts\python.exe -m ...` and retry uv later.
- Several test functions may share one test ID, but every test function needs one (T11-01 plugin, `--require-test-ids`).
- Program gate: `uv run python -m tools.check_module_size` exit 0 before reporting. Budget overruns only by recorded ruling mirrored in the spec §2 row in the same commit; if that docs commit is denied by the permission system, stop, record it, report it; never retry through another agent. ENG hard limit 400 lines. Private sibling modules to stay in budget are acceptable when recorded with a module-map spec note.
- Files outside the worktree (ledger, reports, reviews under D:\herness\.superpowers\sdd\program\) are written with Bash heredocs, never Write/Edit (worktree isolation refuses them).
- Do not hand back while your own build/verify agents are still running; wait for them. Never two build agents at once on the worktree.
- Briefs are regenerated with every unit spec and test row of any ID range (.., –, …). Builders and reviewers still read the spec sections the brief cites for anything left open.

## What is on the base (6b70189)
- Config: herness.core.config (HernessConfig with every owner section field, load_config, get_config, config_hash; config_view; config_sources 390/390 no room; config.py 315/320), config_checks.py + config_validate.py (T10-12 cross-checks, register_owner_validator hook — owner validator registration itself is T09-20's), audit (T10-05), secrets (T10-06/07: known_values, scrub_secrets; secrets.py 360/360), redact (T10-08/09/10: redact_text, EntityType, NameDirectory; redact_patterns.py 395/395; redact.py 313/390 with 77 lines reserved for T10-11 redact_table). `herness config validate` CLI acceptance defers to T10-14. Importing herness.core.redact early matters for config_hash key_id (CLI bootstrap carry-over).
- Ops store: herness/store/ops: core.py 280/280 (NEVER edit; new code goes in area modules), _shims.py (`# T08-07` retry_call / `# T08-08` fault_point — reuse, never add new shims), migrate.py + migrations 001–006 + 090_chat.sql (owner ranges: 09 owns 90–99; non-consecutive numbers allowed; each later owner adding an index lists it in UT02-32 `_LATER_INDEXES`), areas ingest (01), shared (02, 8 names), evidence (05), runs (06), chat (09), privacy (10). ops/__init__.py = one import block + one __all__ block per area, block order per U02-62: core, migrate, shared, ingest, evidence, runs, chat, privacy, then new areas in card-number order; blocks separated by `# isort: split`; __all__ block-ordered with `# noqa: RUF022` (UT02-68 checks it); budget 400. `ops-areas-acyclic` import-linter contract: an area must not import another area (wildcard covers new areas).
- Tests: the REAL `ops_store` fixture (T11-40, tests/support/ops_store.py) returns OpsStoreHandle (frozen dataclass + path-like) and MIGRATES the store; for an empty store use a local `ops_store` shadow fixture (pattern in tests/unit/store/ops/test_store_ops_migrate.py) or `fresh_ops_store` (bench) or tmp_path + `core.reset_connections(path=...)`. Autouse reset_herness_state resets registry, config and owner validators. `fake_keyring` and `fake_clock` plugins exist.
- Types: owners 05, 06, 07, 09 complete in herness.core.types (OWN040/OWN041: import from herness.core.types, not submodules; names owned elsewhere get a prefixed public alias, e.g. UiRole, RegistryKind).
- Impl 06 T06-03 (PipelinesConfig) and T06-23 (chat support) are built but NOT merged (held on a budget decision) — do not depend on them; use a minimal local Protocol if a card needs PipelinesConfig field names and record a carry-over.
- Metric sink T08-05 does not exist: leave `# T08-05:` markers at counter call sites. Fault hook T08-08 / retry T08-07 do not exist: use the _shims.py functions.
- Layers (import-linter): core base closed → store → model | connectors → enrich | metrics (L3) → harness (L4) → eval | reports (top). Settings modules are leaves.

## Addendum (after cdadffe)
- Base for w08 groups is cdadffe: adds T08-04 policies/classification (herness/core/resilience/classify.py 229/230, resilience/__init__.py 70/70 — next export needs a ruling; classify redacts before the 500-char cut; retry-after parsing).
- Commit messages: build agents must write them to a per-agent file (e.g. scratchpad/<T>-commit-msg.txt or a heredoc), never to a shared scratchpad msg.txt — two commits already got another card's subject that way.
- Card `-k` filters with hyphenated IDs select nothing; use the underscore form.

## Addendum 2 (session-kill resilience)
See subcontroller-instructions.md addenda 2026-09-26: checkpoint `wip(<T>)` commits, no redundant full-suite runs in builders, ledger `NEXT:` line, report-before-final-commit, no shared scratchpad files, kill-recovery contract.
- Merge agents: NEVER amend a merge commit (other worktrees may already have branched from it); give the message with -m at merge time.

## Addendum 3 (merge throughput)
- Merge agents run the FAST gates after every merge (ruff check, ruff format --check, mypy, lint-imports, check_type_ownership, check_module_size — ~2 min) and the FULL pytest suite + `pre-commit run --all-files` once at the END of the batch (and after any merge that touched tests/conftest.py, tests/support/, pyproject.toml or herness/core/). If the end-of-batch suite fails, bisect by re-running pytest on the merge commits in order and fix as a follow-up commit (never amend).
- (resolved at f43b1f9) The former known-red set (ST05-13(a), ST10-25, TID251 hits, IT00-01) is fixed by T05-06b: integration is green; commit with NO SKIP; `ruff check .` and `pre-commit run --all-files` must be clean.

## Addendum 4 (2026-09-26 late) — IT00-01 under host load
- IT00-01 wraps `pre-commit run --all-files` in a 600 s subprocess limit. With many concurrent suites the nested run exceeds 600 s (observed 626 s) and the test fails on TimeoutExpired while the hooks pass.
- Merge agents: run `uv run pre-commit run --all-files` directly on the merged head; exit 0 is the gate evidence during load. Still run IT00-01 alone and report its result; a TimeoutExpired with a passing direct pre-commit run is recorded as LOAD-TIMEOUT, not as red. Never edit the test or its limits.
- Controller: re-run IT00-01 on the integration head in every quiet-host window and record the pass in progress.md.

## Addendum 5 (2026-09-27) — shared scratchpad
- Build/verify/merge agents write only under a folder named for their own agent or group (e.g. `.superpowers/sdd/program/merge16/`, `<worktree>/.agent-tmp/`) and never delete or clear the shared session scratchpad root or another agent's folder. A cleared scratchpad has already lost one merge agent's suite output and run a stray mutation script from another session.

## Addendum 6 (2026-09-27) — worktree pruning
- Disk: each agent worktree carries a ~7 GB .venv; prune merged worktrees regularly (after each merge batch) — but ONLY worktrees whose branch has at least one commit not on integration's first-parent base AND whose branch is an ancestor of HEAD, or whose group is recorded "Merged into integration" in its ledger. A branch with zero commits looks "merged" to merge-base and belongs to a running group. Exclude every worktree of a group in state dispatched/built. Never prune during a merge batch's gate run.
- Amendment to addendum 5: temp folders go under the system TEMP on C: (or a C:\ folder named for the agent), never inside a worktree (UT11-37's nested pytest picks up the repo conftest) and never as a new D:\ root folder.
- Known flakes under load (rerun the file alone; never edit): ST10-54 (egress TLS), UT03-61[3] (test_llm_decider peak-concurrency timing), UT11-05 (timing), PT11-04, ST04-13, UT10-54, PT04-12, test_cv_t08_21_run_once_and_nothing_to_do (test_jobs_supervisor.py). ST10-55 was made hermetic by T10-21. Determinism follow-ups are owed to the owning impls.
- Gates and mutating verifiers never overlap on one worktree: a verify agent reverts its probes only when it finishes, so a suite started meanwhile may see mutated code. Run the full gates only after the verifier has reported and `git status` is clean.
