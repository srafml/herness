# Sub-controller instructions (one per wave × spec group)

You coordinate a group of task cards from one Herness implementation spec. You do not write code yourself. For every card, in the order given, you run: (1) a build agent, (2) a separate verify agent, (3) a fix loop until the verify agent approves (max 3 rounds; after that, record the open findings and move on). Use the Agent tool for both; pick `model: sonnet` for size S cards and `model: opus` for size M cards; reviewers use `sonnet` for S, `opus` for M. Never dispatch two build agents at once on the same worktree.

Workspace: you run inside a dedicated git worktree (your current working directory) on your own branch. All agents you dispatch must work in that same directory (tell them the absolute path) and never in D:\herness. Never switch branches, push, merge, rebase, or touch D:\herness. Run tools with `uv run …` (run `uv sync --frozen` once first if `.venv` is missing).

Per card inputs (absolute paths, pass them to your agents):
- Brief: D:\herness\.superpowers\sdd\program\briefs\<T>.md — the card, its unit specs, test rows, threats and module-map budgets, verbatim from the binding spec. Implementers read the spec sections it cites for anything the brief leaves open (path in the brief header).
- Global constraints: D:\herness\.superpowers\sdd\program\global-constraints.md (binding; includes layering, tests naming, gates).
- Process rules for implementers: D:\herness\.superpowers\sdd\program\implementer-rules.md. Reviewer rules: D:\herness\.superpowers\sdd\program\reviewer-rules.md.

Build agent dispatch must contain: the worktree path; one line on where the card fits; the brief path ("read first — your requirements, exact values verbatim"); interfaces landed by earlier cards in this group (a few lines, not history); the report path D:\herness\.superpowers\sdd\program\briefs\<T>-report.md; and the reply contract (Status DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT, commit SHA + subject, one-line test summary, concerns).

Verify agent dispatch must contain: the brief path, the report path, a diff file you generate with `git diff -U8 <base>..HEAD > D:\herness\.superpowers\sdd\program\briefs\<T>-review.diff` (base = HEAD before the build agent ran; also include `git log --oneline base..HEAD` at the top), the global-constraints path, and the output contract (Spec ✅/❌, ⚠️ items, Critical/Important/Minor with file:line, verdict Approved | Needs fixes). Write its review to briefs/<T>-review.md.

Fix rounds: send the open findings verbatim to the same build agent (SendMessage) or, if it is gone, a fresh one with the brief + report paths. Re-review is scoped to the findings.

Card outcomes you must handle: NEEDS_CONTEXT about a symbol another spec has not built yet → try the tree first (`uv run python -c "import …"`); if it truly does not exist, mark the card `blocked:<symbol>` and continue with the next card. BLOCKED for any other reason → re-dispatch once on opus with the report; if still blocked, mark it and move on.

Ledger: keep D:\herness\.superpowers\sdd\program\groups\<group-id>.md updated after every card: `<T>: dispatched | built <sha> | review <verdict> | fix round n | done <sha> | blocked <why> | parked <finding>`. Record every ruling you make as `Ruling: … — why — cost if wrong`. The ledger is the recovery map if your context is compacted; re-read it and `git log` before re-dispatching anything.

Before finishing: run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run lint-imports`, `uv run python -m tools.check_type_ownership`, `uv run python -m tools.check_module_size`, and `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` on your branch and record the results in the group ledger. If something fails that a card in your group caused, dispatch one fix agent for it.

Final reply (under 25 lines): group id, branch name and worktree path, head SHA, per-card status line (`<T>: done <sha>` | `blocked <why>` | `parked <n> findings`), gate results, and any ruling you made.

Addenda (2026-09-25):
- Agents inside a worktree cannot use the Write/Edit tools on paths outside it; write ledgers, reports and review files under D:\herness\.superpowers\sdd\program\ with Bash heredocs instead.
- Run pytest with `PYTHONUTF8=1` set in the environment (Windows locale issue in an impl 00 test, being fixed on the integration branch).
- A card whose `-k` filter in the spec uses hyphenated IDs (`-k "UT05-01"`) selects nothing; use the underscore form (`-k "UT05_01 or UT05_02"`).
- Type-owner cards: the ownership checker (U00-47) requires every name of an owner to exist once the owner's submodule exists, so a group holding the first types card of a spec continues through that spec's remaining types cards before its final gate run; it is normal for the checker and UT00-48 to be red in between.
- Git commits: set `PRE_COMMIT_ALLOW_NO_CONFIG=1` in the environment (a pre-commit hook is installed repo-wide but its config lands with T00-12). Never use --no-verify.
- If `uv run` fails in your worktree because D:\herness\pyproject.toml is mid-merge, use the worktree's `.venv\Scripts\python.exe -m pytest` / `-m ruff` / `-m mypy` directly for a few minutes and retry.

Addenda (2026-09-26, session-kill resilience — binding for every dispatch from now on):
- Checkpoint commits: build agents commit on the card's branch at every milestone (after the first green test file, after each unit, before any long test run) as `wip(<T>): <step>`; the final commit carries the card subject. A killed agent must never leave more than ~15 minutes of uncommitted work. Merges are --no-ff, so wip commits are acceptable history; do not squash.
- No redundant full-suite runs: build agents run only the card's own tests plus the touched package(s) (`pytest tests/unit/<pkg> -q`) and the fast static gates (ruff, mypy on the touched files). The sub-controller runs the full gate list once per card after review, and the merge runs it again. This removes ~10 minutes of exposure per card.
- Resume line: after every step the ledger's LAST line is `NEXT: <exact next step, with the agent to dispatch and the base SHA>` so any resumer (the same sub-controller after a message, or a fresh one) continues without re-reading the transcript.
- Reports first: a build agent writes briefs/<T>-report.md (Bash heredoc) BEFORE its final commit and updates it after, so a kill between the two still leaves the report.
- Worktree hygiene: never `git stash`; never edit a file another agent owns; commit messages via heredoc or a per-agent file, never a shared scratchpad file.
- Kill recovery contract: if you are resumed by a controller message after a rate-limit kill, first run `git status`, `git log --oneline -5`, read the ledger's NEXT line and briefs/<T>-report.md, then continue from there. Do not redo committed work.
