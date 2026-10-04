# T05-09 review: AnthropicClient.submit_batch / collect_batch (U05-29, UT05-39)

Worktree: D:\herness\.claude\worktrees\agent-a01cd70b74a277613, branch head 07fe0dc (base 221fd6e).

## Gates run (verified directly, not just report claims)

- `PYTHONUTF8=1 uv run pytest tests/unit/harness/test_llm_anthropic_batch.py tests/unit/harness/test_llm_anthropic.py -q -p no:logging`: 77 passed, 1 failed - `test_st05_13_ast_lint_harness_builds_no_unguarded_clients`, and the failure is exactly the documented known-red (2 hits, both `herness\harness\llm\openai_compat.py:113`). Confirmed neither new/changed file contributes a hit.
- Coverage (`--cov-branch`, both new/changed modules): `_anthropic_batch.py` 100% line / 100% branch; `anthropic_client.py` 99% line, 1 branch partial at line 92->85 (pre-existing `_assistant_blocks`, untouched by this card). Matches the report.
- `uv run mypy herness/harness/llm/anthropic_client.py herness/harness/llm/_anthropic_batch.py`: Success, no issues.
- `uv run ruff check` on both modules + the new test file: All checks passed.
- `uv run python -m tools.check_module_size`: exit 0. `wc -l`: `anthropic_client.py` = 400 (hard limit, exact), `_anthropic_batch.py` = 153 (budget 200).

## Spec compliance (U05-29)

- OK Batch helpers live in private sibling `herness/harness/llm/_anthropic_batch.py` (153/200 lines); a section-2 module-map row was added to `docs/impl/05-harness-core.impl.md` right after `anthropic_client.py`'s row. `anthropic_client.py` alone constructs the SDK client; `_anthropic_batch.py` imports only `LLMRequest`/`LLMResponse`/`ClientConfig` under `TYPE_CHECKING` and never imports `anthropic`/`httpx`/`httpx2` - confirmed by reading the file and by the dedicated `test_ut05_39_batch_module_passes_st05_13_ast_lint` test, which runs the real ST05-13(a) AST checker (`_violations`, imported from `test_llm_anthropic.py`, not duplicated) directly against it.
- OK `BATCH_MAX_WAIT_S` is reachable from `anthropic_client.py` (`from herness.harness.llm._anthropic_batch import BATCH_MAX_WAIT_S`, re-exported via `__all__`).
- OK Preconditions, all raised as `ConfigError` before any network call: `cfg.supports.batch` (`check_batch_supported`, first in both `prepare_submit` and `validate_collect`); unique `request_key` within the batch (`build_batch_requests`, dict-membership check per item; `RequestMeta.request_key` is a required `str` field, so "missing" is not a distinct runtime state - only uniqueness is spec-required and is enforced); `batch_id` matches `^msgbatch_[A-Za-z0-9]+$` (`validate_batch_id`); `poll_s >= 5` (`validate_poll_interval`). All of these run synchronously before the first `await`/`async with self._sdk(...)` in `submit_batch`/`collect_batch` (`herness/harness/llm/anthropic_client.py:399-436`).
- OK 1-10,000 request bound: `build_batch_requests` checks `len(reqs)` up front, before the per-request loop that calls `build_params` - bound enforced before doing any unbounded work, not after.
- OK `collect_batch` polls `retrieve` on a monotonic clock (`clock.monotonic` injected as the `monotonic` callable), deadline = `monotonic() + BATCH_MAX_WAIT_S` computed once, checked each iteration after `retrieve`, `ModelUnavailable("batch <id> not ended after 86400 s")` raised once past deadline, `sleep(poll_s)` between iterations otherwise. Verified against `poll_until_ended` (`_anthropic_batch.py:158-179`) and the two dedicated timeout/multi-iteration tests, both driven with fake clocks/sleeps (no real waiting).
- OK Results cap: `collect_results` raises `OutputValidationError` as soon as the running count exceeds 10,000, checked per line while iterating the async results stream (before the line is added to `out`/`seen`) - cap enforced while unbounded work is in progress, not after buffering everything. `.close()` on the results iterator is called in a `finally`, exercised even when the cap trips mid-stream (test asserts `wrapper.closed is True`; real SDK decoder exposes `.close()`).
- OK Each succeeded result is mapped via `self._map_message(msg, req, 0, batch=True)` (elapsed 0, `batch=True`) exactly per the algorithm; `req` is `pending.get(custom_id)`, so an unknown `custom_id` (another process collecting) maps with `parsed=None` - confirmed both by reading `_map_message`'s handling of `req: LLMRequest | None` and by `test_ut05_39_collect_batch_unknown_custom_id_parsed_none`.
- OK `batch=True` reaches `cost_usd(usage, prices, batch=True)`, which applies the documented 0.5 factor (`herness/harness/llm/pricing.py:32-33`); test asserts the exact halved cost (`0.007000` for 1000 in / 500 out tokens on `claude-opus` pricing from `config/models.yaml`, which does have `supports.batch: true` and `timeout_s: 600`, matching the test's asserted guard-call timeout).
- OK `errored`/`expired`/`canceled` results are absent from the returned dict and logged once each at WARNING as `harness.llm.batch_item_failed` with exactly `batch_id`, `custom_id`, `result_type` - no message/error text (TH05-15). Verified in both the pure `_anthropic_batch` test and the `AnthropicClient`-level test, both asserting the exact three-field payload and absence of `message`/`error` keys.
- OK Pending-dict concurrency: `self._pending` is only mutated (`update` in `submit_batch`, `pop` in `collect_batch`) inside `async with self._pending_lock`; the read-snapshot before `collect_results` is also taken under the lock. Matches "guarded by an asyncio.Lock."
- OK Batch requests never stream: neither `submit_batch` nor `collect_batch` calls `messages.stream`; only `batches.create`/`retrieve`/`results`.
- OK SDK/egress errors from all three batch-admin calls are caught with the same `except (anthropic.AnthropicError, EgressBlocked) as exc: self._fail(exc, None)` pattern used by `acomplete`, reusing the U05-30 translation path unchanged. `_fail`'s signature was widened from `req: LLMRequest` to `task_id: str | None`; `acomplete`/`astream` now pass `req.metadata.task_id` explicitly, behavior-preserving (confirmed by reading the diff and the still-green `test_llm_anthropic.py` suite, 77/78 passing with only the known-red failure).
- OK Tests: 21 new tests in `tests/unit/harness/test_llm_anthropic_batch.py`, all IDed/docstring'd `UT05-39`, `pytestmark = pytest.mark.unit`, covering every row of the UT05-39 setup/action/expected description (succeeded/errored/expired via a fake batch API; submit+collect; only-succeeded-keys; batch price multiplier; failures logged; timeout raises) plus the module-split and precondition checks the controller ruling asked for.

## Controller ruling 3 (`_http_client` folded into `_sdk(req | None)`)

- OK Behavior preserved for `acomplete`/`astream`: with a non-`None` request, `_sdk` derives `purpose = egress_purpose_for(meta.model_role)`, `run_id/task_id/timeout = meta.run_id/meta.task_id/req.timeout_s` - identical to the old `_http_client(req)` body - then does the same `hasattr(guard, "async_http_client")` fail-closed check, the same `guard.async_http_client(purpose, "aggregated_evidence", run_id=..., task_id=..., timeout=...)` call, and constructs `anthropic.AsyncAnthropic(..., max_retries=0, timeout=timeout, http_client=http_client)` exactly as before, with `http_client` always passed (so the ST05-13(a) lint still passes - confirmed, zero hits in `anthropic_client.py`). `test_llm_anthropic.py`'s existing guard-args/timeout/EgressBlocked tests all still pass unchanged.
- With `req=None` (batch-admin calls), `_sdk` uses `purpose="reasoning"`, `run_id=None`, `task_id=None`, `timeout=self.cfg.timeout_s` - a defensible design choice within the ruling's latitude, and not spec-mandated (the unit spec doesn't name a purpose/timeout for batch-admin calls), consistent with the report's stated rationale.
- WARN Minor, stale doc: `docs/impl/05-harness-core.impl.md:613` (the U05-27/U05-28 signature table, from the prior T05-08 card) still explicitly lists `_http_client(req: LLMRequest) -> httpx.AsyncClient` as one of `AnthropicClient`'s "Private, tested" methods. This diff removes that method entirely (folded into `_sdk`) but does not update that table row - the spec doc is now stale relative to the code. This is acceptable per the controller's ruling authorizing the fold, but the doc drift itself was not cleaned up in this diff and should be fixed in a follow-up doc-sync (flagging per the review brief's explicit ask to "verify... the spec-listed private `_http_client` removal is acceptable or flag it" - flagging the doc, not the code change).

## Quality

- Clean separation: pure, dependency-injected helpers in the sibling module (`retrieve`/`sleep`/`monotonic`/`map_message` all passed in) make the hard-to-test parts (86,400 s timeout, 10,001-item cap) testable without real waiting or a live SDK - good design given the constraints.
- `submit_batch`/`collect_batch` are appropriately thin: validate via the sibling, do the guarded network call, delegate mapping to the sibling, touch `_pending` only under the lock.
- No swallowed errors: every failure mode (ConfigError preconditions, ModelUnavailable timeout, OutputValidationError cap, SDK error translation) is exercised by a specific test with a specific assertion, not just "does not raise."
- `_MAX_RESULTS`/`_MAX_BATCH_REQUESTS` constants are private to `_anthropic_batch.py` (not re-exported), keeping the numbers in one place; `BATCH_MAX_WAIT_S` is the one constant the spec names as needing external reachability and is exported correctly.

## Findings

### Critical
None.

### Important
None.

### Minor
1. `docs/impl/05-harness-core.impl.md:613` - stale signature table still lists the removed `_http_client(req: LLMRequest) -> httpx.AsyncClient` private method after this diff folded it into `_sdk`; not updated in this commit. Recommend a follow-up doc-sync edit (not blocking, ruling-authorized code change).

### Cannot fully verify from diff/worktree alone
- None - all claimed test/coverage/lint/mypy/module-size results were independently reproduced in the worktree and matched the report.

## Verdict

**Approved.**

All U05-29 preconditions, algorithm steps, bound placements (checked before/while unbounded work, never after), concurrency (asyncio.Lock around `_pending`), logging contract (exactly `batch_id`/`custom_id`/`result_type` at WARNING, no error text), pricing (0.5 batch multiplier), and the "never streams" invariant are implemented and tested correctly. The module split honors the controller's ruling (sibling <=200 lines, no SDK/httpx construction in the sibling, `BATCH_MAX_WAIT_S` reachable), `anthropic_client.py` lands at exactly the 400-line hard limit with `acomplete`/`astream` behavior unchanged (guard args, timeout, fail-closed EgressBlocked all verified byte-for-byte equivalent to before the `_http_client`-into-`_sdk` fold). All requested gates (targeted pytest, coverage, mypy, ruff, check_module_size) were re-run directly in the worktree and matched the build report's claims, including the pre-existing known-red ST05-13(a) failure being unrelated to this card's files. The only finding is a Minor stale-documentation item (the spec's private-method signature table not updated after the ruling-approved `_http_client` removal), which does not block approval.
