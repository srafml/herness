# Review: T05-05 Client protocols, parameter resolution, error translation

Base cd44be5, commit 6e45de8.

## Spec compliance

- U05-20 (`LLMClient`, `StreamCapable`, `BatchCapable`, `TextDelta`, `ToolCallDelta`, `Done`, `StreamEvent`) - PASS. All three protocols `@runtime_checkable` (base.py:72-98); signatures match verbatim, including `collect_batch(batch_id, poll_s: float = 30.0)`. Frozen `slots=True` dataclasses for the three stream-event types. `StreamEvent` is the PEP 695 union alias. UT05-29/UT05-38 (protocol runtime-checkable / stream postcondition tests) are not in this card's Tests row and are correctly not required here.
- U05-21 (`resolve_request_params`, `egress_purpose_for`, `EGRESS_PURPOSE_BY_MODEL_ROLE`, `BASE_ROLE`) - PASS. Hand-verified all 6 algorithm steps (base.py:121-155) against every one of the 17 UT05-19 table rows (test_llm_base.py:366-505): auto-thinking by depth/base-role bucket, openai `thinking_toggle` override, anthropic `adaptive_always` forced-on + `downgraded` flag (only set when step-3 result was `off`), effort always-explicit-when-supported with `"low"` on downgrade, and VI-5's temperature-drop for `thinking_mode == "budget"` only when thinking is `on`. All 17 rows check out arithmetically against the spec text; none of the resolved values are wrong. `egress_purpose_for` matches R-38 exactly (writer/skeptic_final -> reasoning_final, else reasoning). Controller rulings on the public name `resolve_request_params` and the VI-5 default are respected (module-map's stale `resolve_thinking` name correctly not used).
- U05-30 (`translate_openai_error`, `translate_anthropic_error`, `find_egress_block`) - PASS. Verified against the actual installed `openai==3.19.2` / `anthropic==1.8.0` exception hierarchies (read `_exceptions.py` directly in both packages installed in the worktree venv):
  - `APITimeoutError` subclasses `APIConnectionError` in both SDKs - confirmed, so the explicit tuple listing both is redundant but not wrong.
  - Anthropic's `OverloadedError` (529), `ServiceUnavailableError` (503), `DeadlineExceededError` (504) are all plain `APIStatusError` subclasses with no special-casing needed beyond the `status == 529 or status >= 500` branch (errors.py:238) - correct, and covered by UT05-35's 529/503/504 rows.
  - `RateLimited(retry_after=parse_retry_after(exc.response.headers, clock.now()))` matches `parse_retry_after`'s actual signature (`herness/core/resilience/classify.py:203-204`) and `RateLimited.__init__`'s positional-message/keyword-retry_after signature (`herness/core/errors.py:169-179`).
  - `find_egress_block` hand-traced against the depth-10/depth-12 tests: `range(_MAX_CAUSE_DEPTH + 1)` checks nodes at depth 0..10 inclusive (11 checks), which finds an `EgressBlocked` exactly 10 hops away and correctly misses one at 12 hops - matches "depth <= 10" and the acceptance check. `current.__cause__ or current.__context__` correctly prefers explicit cause and falls back to implicit context, verified by the `__context__`-only test.
  - Both translators call `find_egress_block` first and return the hit as is (errors.py:232-234) - TH05-13 satisfied; a wrapped `EgressBlocked` is proven never retryable by the "returned as is" tests on both SDKs.
  - Message content: `"<provider> call failed: <ExceptionTypeName>[ HTTP <status>]"` (errors.py:213-216) - never a body/header/key; verified by `test_ut05_28_message_never_carries_the_body` with a real response body containing a marker string.
  - The client-key/operation wording vs. the fixed no-client-key signature: per the controller ruling to keep the U05-30 signatures, using the provider name in the "operation" slot is a reasonable, safe reading (no leak). Not a defect.

## Other checks performed (read-only, in-worktree)

- `uv run pytest tests/unit/harness/test_llm_base.py tests/unit/harness/test_llm_errors.py -q` -> 59 passed (matches report).
- `uv run pytest ... --cov=herness.harness.llm.base --cov=herness.harness.llm.errors --cov-branch` -> 100% line, 100% branch on both modules (well above 90/85 gate).
- `uv run mypy herness/harness/llm/base.py herness/harness/llm/errors.py` -> Success, no issues.
- `uv run ruff check` on the two source files and two test files -> all checks passed.
- `uv run lint-imports` -> 13 contracts kept, 0 broken.
- Line counts: base.py 148/180, errors.py 119/140 - both under budget.
- Test IDs: every test function name contains its ID (`test_ut05_19_...`, `test_ut05_20_...`, `test_ut05_28_...`, `test_ut05_35_...`); docstrings lead with the ID; both files set `pytestmark = pytest.mark.unit`. All four card Tests-row IDs (UT05-19, UT05-20, UT05-28, UT05-35) have at least one test function.

## Items to note (not defects)

- The `find_egress_block` acceptance-check tests and the `__context__`-fallback test are labeled `test_ut05_30_...`, but "UT05-30" is not a defined row in the spec's test table (U05-30's own Tests row is UT05-28/UT05-35/ST05-13; the card's Tests row is UT05-19/20/28/35). Not a violation of any stated rule - it's the brief's "Acceptance checks" item ("`find_egress_block` finds a nested cause") given its own ID rather than folded into UT05-28/35 - but flagging since it's an invented ID not traceable to the spec's test-ID catalog. Purely cosmetic; does not affect pass/fail or coverage.

## Findings

### Critical
None.

### Important
None.

### Minor
- `herness/harness/llm/base.py:29,35` - `EGRESS_PURPOSE_BY_MODEL_ROLE` and `BASE_ROLE` are documented as "constant" in the unit spec but are annotated `Mapping[str, ...]` without `Final`, unlike the module's other true constants (`_AUTO_ON_FAST_STANDARD`, `_AUTO_ON_DEEP`, `_MAX_CAUSE_DEPTH` all use `Final`). Cosmetic inconsistency only; mypy/ruff raise nothing and the values are never reassigned.
- `herness/harness/llm/errors.py:253` - the `unavailable` tuple for both translators lists `APIConnectionError` and `APITimeoutError` even though the latter subclasses the former in both installed SDK versions (confirmed by reading `_exceptions.py`), making the second entry redundant. Harmless (isinstance with a tuple containing a redundant subtype is a correctness no-op) and arguably clearer to read explicitly per the spec's literal list; not worth changing.

## Verdict

Approved

All three units (U05-20, U05-21, U05-30) are implemented per the binding spec, controller rulings are respected, module budgets and layering are clean, mypy/ruff/import-linter are clean, and the reported 100% coverage was independently reproduced. Hand-verification of the full 17-row U05-21 parameter table and the U05-30 mapping table against the actual installed openai 3.19.2 / anthropic 1.8.0 exception hierarchies found no discrepancies. Only cosmetic/minor observations, no Critical or Important findings.
