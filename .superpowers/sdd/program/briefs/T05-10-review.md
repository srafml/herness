# Review for T05-10: Model registry

Worktree: D:\herness\.claude\worktrees\agent-a1da3b6067592d7e8 (branch worktree-agent-a1da3b6067592d7e8, base f529d37, head 6df047a).

## Spec compliance (per unit/test row)

| ID | Result | Notes |
|----|--------|-------|
| U05-31 `LLMRegistry.__init__` | ✅ | Signature, `egress_enabled=None` reads `get_config().security.egress.enabled` (registry.py:154-161); construction walks `roles ∪ fallback ∪ depth_overrides.*.roles` (`_used_client_names`, registry.py:122-129) and raises `ConfigError` for an off-network name with egress disabled, logging `harness.llm.config_invalid` (ERROR, client+profile only) first — matches ruling 2's message verbatim. |
| U05-31 `client` | ✅ | Lock-protected cache (registry.py:179-189); `test_ut05_41_client_same_instance_across_threads` exercises 8 concurrent threads, same instance returned. Unknown name -> `ConfigError("unknown model client <name>")` via shared `config()`. |
| U05-31 `config` | ✅ | registry.py:171-177, same message reused by `client`. |
| U05-31 `model_for` | ✅ | Algorithm 3 verbatim (registry.py:191-203): depth override applied, base-role fallback, `ConfigError("no model for role <r>")`. `test_ut05_40_*` cover override, base-role, unknown-role, unknown-depth (no-op). |
| U05-31 `chain_for` | ✅ | Algorithm 4 verbatim (registry.py:205-219): head dedup, order kept, explicit empty-list fallback distinguished from missing key via `.get`. `test_ut05_40_chain_for_*` cover dedup, base-role fallback, no-fallback head-only. |
| U05-31 `role_params` | ✅ | Algorithm 5 verbatim (registry.py:221-227). |
| U05-31 `health` | ✅ | Covers `_used_client_names` (roles+fallback+depth_overrides) per ruling; local client GET /v1/models via `loopback_http_client(root, timeout_s=2, bearer=<resolved or None>, max_response_bytes=1_048_576)`, client closed via `with` (registry.py:242-257); off-network -> ok without a call when egress enabled; R-52 check only for effective server == "vllm" (already materialized by settings.py's before-validator, so no re-inference needed in registry.py), entry matched by id == cfg.model, max_model_len must be non-bool int, reason string verbatim "context_window <n> above server max_model_len <m>". All UT05-123 U05-31-half cases (ok, down-on-non-200, transport/JSON exception -> generic "down: <ExceptionType>", R-52 triggered, R-52 skipped for missing/non-int/bool/missing-entry, R-52 not checked for non-vLLM, off-network ok, off-network down) pass and match. See Minor note below on the off-network-down reason text. |
| U05-32 `client_for` | ✅ | Profile check first (`ConfigError("client_for profile <p> does not match loaded profile <q>")` verbatim), then builds a fresh `LLMRegistry` from `root_cfg.models` -- no module-level cache, matches the invariant. `test_ut05_42_*` cover match/mismatch. |
| `_BUILTINS` rows (herness/core/registry.py) | ✅ | Exactly the two import-string rows from ruling 3; both openai_compat.py:116 and anthropic_client.py:158 already self-register via @register(...), so these rows are purely the lazy-import fallback path -- verified by reading both files. No upward import edge (string literals only); lint-imports confirms 13/13 contracts kept. |
| UT05-18 | ✅ | Off-network (claude-opus) + egress off -> ConfigError; a separate test asserts the ERROR log event and its fields; a counterpart test confirms egress-enabled construction succeeds; egress_enabled=None path tested via monkeypatched get_config. |
| UT05-40 | ✅ | See model_for/chain_for/role_params rows above. |
| UT05-41 | ✅ | Same-instance-across-threads; unknown-name ConfigError for both client and config. |
| UT05-42 | ✅ | Matching/mismatching profile. |
| UT05-123 | ✅ (U05-31 half only, per ruling 6) | Correctly scoped to the U05-31 half (ok/down/R-52 reason/off-network ok-down); the U05-73 aggregator half (traces unwritable) is correctly left to T05-26, as the report states. |

## Verified independently

- `uv run ruff check herness/harness/llm/registry.py herness/core/registry.py tests/unit/harness/test_llm_registry.py` -> All checks passed.
- `uv run mypy` -> Success: no issues found in 218 source files.
- `uv run lint-imports` -> Contracts: 13 kept, 0 broken.
- `PYTHONUTF8=1 uv run pytest tests/unit/harness/test_llm_registry.py -q -p no:logging --cov=herness.harness.llm.registry --cov-branch --cov-report=term-missing` -> 28 passed, 100% line, 100% branch (my rerun showed 129/129 stmts vs the report's 128/128 -- a trivial 1-line accounting difference between coverage.py runs, both 100%, not a real discrepancy).
- Line counts: herness/harness/llm/registry.py 186/200, herness/core/registry.py 142/160, tests/unit/harness/test_llm_registry.py 406 -- match the report exactly.
- R-06: grepped herness/harness/llm/registry.py and tests/unit/harness/test_llm_registry.py for "httpx" -- zero hits. The only HTTP surface is egress.loopback_http_client, whose real signature (base_url, *, timeout_s, bearer=None, max_response_bytes=MAX_RESPONSE_BYTES) in herness/core/egress_clients.py matches the call site exactly.
- herness.core.registry.get() is protected by the module's own _REG_LOCK (RLock), so concurrent LLMRegistry.client() calls across registries resolving the same builtin are safe in addition to the per-registry lock.
- Test-registry duplicate-registration risk (the autouse fixture re-registers a fresh _FakeClient class per test) is resolved by tests/conftest.py's autouse reset_herness_state fixture, which calls reset_registry() before and after every test -- verified this fixture exists and is autouse.
- Head/base verified: worktree HEAD is 6df047a on f529d37, working tree clean, matches the dispatch.
- Health never leaks: non-200 -> plain "down"; any exception (transport, secret resolution, JSON) -> f"down: {type(exc).__name__}", never str(exc); a test injects a message containing an IP and a fake token and asserts neither appears in the result.
- Exception-handling scope: the single `except Exception` (registry.py:239, noqa: BLE001) is confined to `_health_one`'s call into `_health_local`; no other method (client, model_for, chain_for, role_params, config) swallows exceptions -- programming errors in those paths still propagate.
- Test IDs/docstrings/pytestmark: `pytestmark = pytest.mark.unit` at module level; every test function name embeds its ID with an underscore (e.g. test_ut05_18_..., test_ut05_123_...); every docstring's first line starts with the hyphenated ID. Confirmed for all functions in the diff.

## Builder's flagged concern: UT05-123 flipping private _egress_enabled

Assessed and correct as flagged. Because construction itself raises ConfigError for any off-network client in the in-use set (roles/fallback/depth_overrides) when egress is disabled, a LLMRegistry that has successfully finished __init__ can never legitimately have _egress_enabled False while an off-network client is in that same in-use set -- the branch _health_one takes for that combination ("down: egress disabled") is dead code, reachable only by directly violating the constructor's own invariant. The test (test_ut05_123_off_network_down_when_egress_disabled, tests/unit/harness/test_llm_registry.py:671-680) constructs with egress_enabled=True (so construction succeeds) and then sets registry._egress_enabled = False before calling health(), explicitly to unit-test that one string-mapping line in isolation. This is an encapsulation-breaking test technique (private attribute mutation) but a defensible one here: it is the only way to exercise a line the U05-31 algorithm explicitly describes without either (a) leaving it uncovered (failing the 100% branch requirement) or (b) restructuring the algorithm to remove the branch, which is not this card's call to make since the branch matches the spec's algorithm text. Not a defect; flagged as Minor below for visibility, matching the builder's own framing.

## Findings

### Critical
None.

### Important
None.

### Minor
1. herness/harness/llm/registry.py:236 -- for an off-network client with egress disabled, health() returns "down: egress disabled", but U05-31's algorithm text for that branch reads only "off-network client -> ok without a call when egress is enabled, else down" (no reason). The sub-controller ruling's general format ("ok", "down" / "down: <reason>", R-52 reason verbatim) does not forbid this, and it is harmless (still satisfies T05-26's startswith("down") contract), but it is an elaboration beyond the literal algorithm text worth the controller's awareness in case a downstream consumer ever matches on the exact string.
2. tests/unit/harness/test_llm_registry.py:671-680 (test_ut05_123_off_network_down_when_egress_disabled) -- reaches an otherwise-unreachable branch by mutating the private _egress_enabled attribute post-construction. Acceptable and well-documented (see above), but a minor test-design smell; a future refactor of LLMRegistry's internals would silently break this test's premise without a compile-time signal.

## Verdict

**Approved**

All U05-31/U05-32 methods, both ConfigError messages, the off-network predicate, the _BUILTINS rows, and the health algorithm (including the R-52 reason format and the bounded/closed loopback call) match the brief, the spec, and every binding sub-controller ruling. R-06 holds (no httpx/httpx2 anywhere in this card's files). No secrets or response text leak through health reasons. Exception handling is narrowly scoped to the health path only. Coverage, ruff, mypy, and lint-imports claims all reproduce exactly. Only two Minor, non-blocking notes above.
