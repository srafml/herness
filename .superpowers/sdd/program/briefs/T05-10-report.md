# Report for T05-10: Model registry

Worktree: D:\herness\.claude\worktrees\agent-a1da3b6067592d7e8 (branch worktree-agent-a1da3b6067592d7e8, base f529d37).

## Files

- herness/harness/llm/registry.py (new, 186/200 lines): LLMRegistry (U05-31) and client_for (U05-32).
- herness/core/registry.py (142/160 lines): added the two impl 05 `_BUILTINS` rows
  (`("llm_client", "openai_compat")` -> `herness.harness.llm.openai_compat:OpenAICompatClient`,
  `("llm_client", "anthropic")` -> `herness.harness.llm.anthropic_client:AnthropicClient`)
  per the sub-controller ruling (U10-26 pattern: each owner card adds its rows).
- tests/unit/harness/test_llm_registry.py (new, 406 lines): UT05-18, UT05-40, UT05-41,
  UT05-42, UT05-123 (the U05-31 half only, per ruling: ok / down / R-52 reason / off-network
  ok-down; the U05-73 aggregator half — traces unwritable, health.py — is T05-26 scope, not
  built here).

## Design decisions

- Off-network predicate implemented exactly per sub-controller ruling 1:
  `cfg.off_network or cfg.kind == "anthropic"` (module-level `_is_off_network`), used
  identically by construction's egress guard and by `health()`.
- Construction (`__init__`) walks the union of `models.roles.values()`, every `models.fallback`
  chain entry, and every `models.depth_overrides.*.roles` value (`_used_client_names`); for each
  off-network name with egress disabled it logs `harness.llm.config_invalid` at ERROR
  (client + profile only, no secrets) then raises
  `ConfigError(f"client {name} is off-network but egress is disabled in profile {profile}")`.
  `egress_enabled=None` reads `get_config().security.egress.enabled`.
- `client(name)` constructs via `herness.core.registry.get("llm_client", cfg.kind)(cfg)` under a
  `threading.Lock`, with a per-name cache dict; unknown name raises
  `ConfigError("unknown model client <name>")` (same message reused by `config()`).
- `model_for`/`chain_for`/`role_params` implement algorithms 3-5 verbatim using `BASE_ROLE` from
  `herness.harness.llm.base`; an unknown depth key simply misses the `depth_overrides.get()`
  lookup, so it falls through to the no-override case as required.
- `health()` imports `loopback_http_client` via module attribute access
  (`from herness.core import egress`, then `egress.loopback_http_client(...)`) so tests can
  monkeypatch `registry.egress.loopback_http_client` directly, matching the pattern already used
  in `tests/unit/harness/test_llm_anthropic.py` for the egress guard. Local-client root is
  `scheme://netloc` of `cfg.base_url`; bearer is `secrets.resolve(cfg.api_key)` when set, else
  `None`; `max_response_bytes=1_048_576`, `timeout_s=2`. Any exception (transport, secret
  resolution, JSON) is caught at one point and rendered as `f"down: {type(exc).__name__}"` —
  never the exception message, per ruling 4 (no leaked hosts or tokens). The R-52 check runs only
  when `cfg.server == "vllm"`, looks up the `data[]` entry by `id == cfg.model`, and requires
  `max_model_len` to be a non-bool `int` before comparing; any other shape (missing key, string,
  bool, missing entry, missing or malformed `data`) is treated as skip-the-check, not an error —
  only a genuinely unparsable JSON body (raises inside `.json()`) becomes the generic
  `down: <ExceptionType>`.
- Did not touch `herness/harness/llm/openai_compat.py` or `anthropic_client.py` (both already
  self-register via a `register("llm_client", ...)` decorator; the new `_BUILTINS` rows are the
  lazy-import path `herness.core.registry.get` uses when those modules have not been imported
  yet).
- No httpx or httpx2 client or transport is constructed anywhere in `registry.py` (R-06); the
  only HTTP surface is `egress.loopback_http_client`.

## Test design note (off-network health "down: egress disabled")

Per the U05-31 postcondition, construction itself raises `ConfigError` whenever an off-network
client is in use (roles/fallback/depth_overrides) and egress is disabled — the exact same set
`health()` iterates over. That means a constructed `LLMRegistry` can never legitimately have
`_egress_enabled == False` while an off-network client is in its in-use set: the constructor
would already have raised. `test_ut05_123_off_network_down_when_egress_disabled` therefore builds
the registry with `egress_enabled=True` (so construction succeeds) and then flips the private
`_egress_enabled` attribute directly before calling `health()`, to unit-test that one line of the
health-to-string mapping in isolation from the constructor's own guard. This is a deliberate
test-only bypass of the invariant, not a reachable end-to-end state; flagged here for visibility.
It is not a contradiction of the spec text (the algorithm for `health()` does describe this
branch), just a note that it is dead code on any registry that already passed construction, so
the test exercises the branch directly instead.

## RED evidence

Before implementation, tests/unit/harness/test_llm_registry.py and
herness/harness/llm/registry.py did not exist; running the new test file would have failed at
collection with ModuleNotFoundError: herness.harness.llm.registry. Tests were written against
the target API and driven red-to-green in the normal TDD loop while writing the implementation.

## GREEN evidence

    $ PYTHONUTF8=1 uv run pytest tests/unit/harness/test_llm_registry.py -q -p no:logging
    ............................                                             [100%]
    28 passed in 0.08s

Coverage:

    $ PYTHONUTF8=1 uv run pytest tests/unit/harness/test_llm_registry.py -q -p no:logging --cov=herness.harness.llm.registry --cov-branch --cov-report=term-missing
    Name                              Stmts   Miss Branch BrPart  Cover   Missing
    -----------------------------------------------------------------------------
    herness\harness\llm\registry.py     128      0     40      0   100%
    -----------------------------------------------------------------------------
    TOTAL                               128      0     40      0   100%
    28 passed in 0.15s

100 percent line and 100 percent branch coverage (requirement is 90 percent / 85 percent).

Full-directory run (own tests plus tests/unit/core, tests/unit/harness):

    $ PYTHONUTF8=1 uv run pytest tests/unit/harness tests/unit/core -q -p no:logging
    1 failed, 2501 passed, 2 skipped in 102.67s
    FAILED tests/unit/harness/test_llm_anthropic.py::test_st05_13_ast_lint_harness_builds_no_unguarded_clients

That one failure is the documented pre-existing known-red (controller-notes-w07.md addendum 3:
`test_st05_13_ast_lint_harness_builds_no_unguarded_clients` in `test_llm_anthropic.py`, caused by
`herness/harness/llm/openai_compat.py`'s `httpx2.AsyncClient` construction, not touched by this
card). The 2 skips are pre-existing Windows symlink-privilege skips (WinError 1314), unrelated to
this card.

## Gate outputs

    $ uv run ruff format herness/harness/llm/registry.py herness/core/registry.py tests/unit/harness/test_llm_registry.py
    3 files left unchanged (after one reformat plus two manual fixes for PLR2004 and PT018)

    $ uv run ruff check herness/harness/llm/registry.py herness/core/registry.py tests/unit/harness/test_llm_registry.py
    All checks passed!

    $ uv run ruff check . --output-format concise | grep -v openai_compat
    Found 7 errors.
    (matches the documented known-red count exactly — 7 TID251 hits in openai_compat.py and its
    tests — no new findings introduced by this card)

    $ uv run mypy
    Success: no issues found in 218 source files

    $ uv run lint-imports
    Contracts: 13 kept, 0 broken.

    $ uv run python -m tools.check_module_size
    (exit 0, no output)

    $ uv run python -m tools.check_type_ownership
    (exit 0, no output)

## Line counts vs budgets

- herness/harness/llm/registry.py: 186 / 200 (module map row, docs/impl/05-harness-core.impl.md).
- herness/core/registry.py: 142 / 160 (module map row, docs/impl/10-config-security-deployment.impl.md).

Both within budget; no pyproject.toml module_budgets or import-linter contract changes needed
(the herness.harness package is already covered by the existing layers contract).

## Deviations from the brief

None. UT05-123 was built for only the U05-31 half (ok, down, R-52 reason, off-network
ok/down); the U05-73 health aggregator (herness/harness/health.py, "traces unwritable to down")
was explicitly out of scope per ruling 6 and was not built.

## Concerns

- None blocking. The one noted test-design subtlety (off-network "down: egress disabled" branch
  being unreachable through the public constructor once construction's own guard is honored) is
  documented above for the controller and reviewer's awareness; the code still implements the
  branch exactly as the U05-31 algorithm describes it.


## Final commit

6df047a feat(harness): add LLMRegistry and client_for (T05-10)

## Secrets baseline

tests/unit/harness/test_llm_registry.py reuses the same "secret:vllm.api_key" /
"secret:anthropic.api_key" reference-syntax fixture values already used by
tests/unit/harness/test_llm_settings.py and test_llm_anthropic.py (false positives: these are
secret-reference strings, never plain-text credentials). Ran
"uv run detect-secrets scan --baseline .secrets.baseline" per the controller-notes-w07 baseline
procedure; the regen dropped the docs/impl/00-foundation.impl.md and
docs/impl/10-config-security-deployment.impl.md audited entries as documented, so those two were
restored from the pre-scan baseline before committing (diff verified: only the new test file's
two entries added, the two docs/impl entries preserved, generated_at bumped). detect-secrets
passed on commit.

## Pre-commit note

Committed with SKIP=pytest-unit (never --no-verify), per controller-notes-w07 addendum 3: the
repo-wide "pytest-unit" hook runs "pytest -m unit -x -q" and stops at the first failure, which
on this base is the documented pre-existing test_st05_13_ast_lint_harness_builds_no_unguarded_clients
failure in tests/unit/harness/test_llm_anthropic.py (openai_compat.py, not touched by this card).
All other hooks (ruff-check, ruff-format, mypy, import-linter, detect-secrets, module-size,
type-ownership) passed normally on this commit. The full targeted unit run
(tests/unit/harness tests/unit/core) was executed manually before committing; see GREEN evidence
above for its 2501 passed / 1 known-red / 2 pre-existing skips result.
