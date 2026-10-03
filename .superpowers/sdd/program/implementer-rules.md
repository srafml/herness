# Implementer rules (every card)

- Work only in the worktree directory named in your dispatch, on its branch. Never switch branches, push, rebase, merge, or touch D:\herness.
- Your brief (path in your dispatch) is your requirements, with exact values to use verbatim. Project-wide constraints: D:\herness\.superpowers\sdd\program\global-constraints.md. Binding spec: the file named in your brief header (read only the sections the brief cites, plus §2 module map and §11 layout as needed). Rulings in D:\herness\docs\impl\DECISIONS.md are binding.
- Follow the card in order, TDD: write the tests named in the brief, run them and see the expected failure, implement, run them green.
- Tools run via `uv run ...` (Windows 11, Git Bash; run `uv sync --frozen` once if `.venv` is missing). Before committing: `uv run ruff format .`, `uv run ruff check --fix .`, `uv run mypy`, `uv run lint-imports`, `uv run python -m tools.check_type_ownership`, and the card's tests; everything must be clean. Fix lint findings without changing behaviour; an unforeseen finding gets a real fix or `# noqa: <code>` with a reason. If a test in the brief contradicts the spec, do not silently change it: report DONE_WITH_CONCERNS explaining what you changed and why, or NEEDS_CONTEXT.
- Also run `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` once before committing.
- If the card names a symbol from another spec that does not exist in the tree, check with `uv run python -c "import <module>"`; if missing, report NEEDS_CONTEXT naming the symbol. Do not invent it.
- If the card adds a package or module that an import-linter contract or the mypy `files` list must name, update pyproject.toml in the same commit.
- One commit per card, Conventional Commit subject naming the card (e.g. `feat(store): add ops store core (T02-04)`), message ending with the Co-Authored-By line your harness attribution reminder gives you (use `git commit -F <file>` or a heredoc).
- Never dispatch subagents or reviewers; review happens after you report.
- If stuck, report BLOCKED or NEEDS_CONTEXT with specifics rather than guessing.
- Write the full report to the report path given in your dispatch: what you implemented, files changed, RED evidence (command + failing output) and GREEN evidence (command + passing output), gate outputs, line counts vs budgets, any deviation from the brief and why, concerns.
- Reply with ONLY (under 15 lines): Status (DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT), commit (short SHA + subject), one-line test summary, concerns, report path.
- Module line budgets are enforced by `uv run python -m tools.check_module_size` (reads each spec's §2 module map). If your module cannot fit its budget without hurting readability, keep it under the 400-line hard limit and report DONE_WITH_CONCERNS naming the file, its size and the budget; the controller rules on raising the budget in the spec.
- Git commits: set `PRE_COMMIT_ALLOW_NO_CONFIG=1` in the environment (hook installed repo-wide, config lands with T00-12); never --no-verify.

- Never edit any file while a `git commit` is running its hooks: pre-commit stashes unstaged changes and restores them after the hooks, and a concurrent edit makes that restore fail and the commit abort (seen in T01-21). Stage everything, commit, wait for the commit to finish, then edit.
