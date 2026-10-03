## Global Constraints

- Python `>=3.12,<3.13`; `[tool.uv] required-version = "==0.11.8"` (the uv on this machine; O-07).
- All installs from `uv.lock`: `uv sync --frozen`; editable install in development (R-58).
- `mypy --strict` must report 0 errors on every commit; `ruff check` and `ruff format --check` must report 0 issues.
- Module line budgets come from the owning spec's §2 module map (the rows are copied into each brief); ENG hard limit 400 lines per module. Function complexity ≤ 10 (ruff `C901`), ≤ 6 arguments (`PLR0913`).
- Coverage of each new or changed `herness` module ≥ 90 % line and ≥ 85 % branch.
- Every test function name contains its ID with `_` (for example `test_ut00_55_...`) so `pytest -k UT00_55` selects it; the first line of its docstring starts with the ID (`"""UT00-55 ..."""`). Every test file sets module-level `pytestmark` (`pytest.mark.unit`, or `integration`, or `[integration, slow]` for benchmarks).
- Ruff `EM101/EM102` is on: assign the message to `msg` before `raise`. Ruff `TRY003` is ignored globally.
- Before every commit, run `uv run ruff format .` and then `uv run ruff check --fix .`, and fix any remaining finding without changing behaviour. The `# noqa` comments in this plan are best guesses: `ruff check --fix` removes any that turn out unused (`RUF100`), and a finding the plan did not foresee gets a fix or a `# noqa: <code>` with its reason.
- Import `herness.core.time` only as `from herness.core import time as clock` (ruff `ICN003` bans `from herness.core.time import …`).
- No `print` (ruff `T20`); CLI tools write with `sys.stdout.write` / `sys.stderr.write` and return exit codes 0 pass, 1 findings, 2 usage or input error (R-73).
- No secret value, ticket text or personal data in any error message, `hint`, `details` or log field (ENG §3.4).
- Commit messages follow Conventional Commits and end with the line `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

- Layering (ENG §2.1) is enforced by import-linter contracts in pyproject.toml: L0 `herness.core` → L1 `herness.store` → L2 `herness.connectors`, `herness.model` → L3 `herness.enrich`, `herness.metrics` → L4 `herness.harness` → L5 `herness.reports`, `herness.eval`, `herness.cli`, `app/`, `herness.admin`. A card that creates a new package or module listed in a contract updates the contract in the same commit (UT00-58 fails until it does). `herness.core` never imports `herness.store` (ports, R-04). Settings modules import only stdlib, pydantic, `herness.core.types`, `herness.core.errors`.
- `herness.core.types` submodules hold data types only (R-01, R-75); after adding one, `uv run python -m tools.check_type_ownership` must exit 0.
- Rulings in docs/impl/DECISIONS.md (R-nn) are binding over the design specs; the implementation spec named in the brief is binding over everything else. Cross-spec symbols the brief names but which do not exist yet in the tree: report NEEDS_CONTEXT naming the symbol rather than inventing it.
- Existing foundation (impl 00) at the branch head: `herness.core.errors`, `time` (import as `from herness.core import time as clock`), `ids`, `numbers`, `logging`, `_log_pipeline`, `types` (+ `_ownership`), `tools/check_type_ownership.py`. Read their public API before use; do not change their behaviour except where the card explicitly says so.
- The log scrubber (impl 10 `scrub_secrets`) must be idempotent: structlog events are scrubbed early (before wrap_for_formatter) and again in the formatter chain (impl 00 deviation 10).
- Test IDs: every test function carries an ID (`test_ut11_31_…`, or `test_rf_…`/`test_cv_…` for review-focus and controller-verification tests). Several functions may share one ID; each ID in a card's Tests row must have at least one function. The repo-wide check is `uv run pytest --require-test-ids` (IT11-31).
