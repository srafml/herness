# T05-05 report: Client protocols, parameter resolution, error translation

## Status
DONE

## Commit
`6e45de8` -- `feat(harness): add LLM client protocols, param resolution and error translation (T05-05)`

## What was built

### herness/harness/llm/base.py (148 lines, budget 180)
- `LLMClient`, `StreamCapable`, `BatchCapable` -- `@runtime_checkable` protocols per U05-20.
- `TextDelta`, `ToolCallDelta`, `Done` frozen dataclasses; `StreamEvent` union alias.
- `BASE_ROLE` and `EGRESS_PURPOSE_BY_MODEL_ROLE` constants, exactly as given in the U05-21
  signature (kept the controller-ruled name `resolve_request_params`; did not add the
  module-map row's stale `resolve_thinking` name).
- `ResolvedParams` frozen dataclass `(temperature, effort, thinking, downgraded)`.
- `resolve_request_params(*, model_role, depth, client, role_params, fallback)` implementing
  the U05-21 algorithm steps 1-6 verbatim (auto-thinking by depth/base-role, openai_compat
  `thinking_toggle` override, anthropic `adaptive_always` forced-on + downgrade, effort
  always-explicit-when-supported, temperature dropped for `thinking_mode == "budget"` with
  thinking on -- VI-5's stated default).
- `egress_purpose_for(model_role)` -- `reasoning_final` for `writer`/`skeptic_final`,
  `reasoning` for everything else (R-38).

### herness/harness/llm/errors.py (119 lines, budget 140)
- `find_egress_block(exc)` -- walks `exc`, `__cause__`, `__context__`, bounded to depth <= 10,
  returns the first `EgressBlocked` found or `None`.
- `translate_openai_error` / `translate_anthropic_error` -- both call `find_egress_block`
  first and return a hit as is (TH05-13: an egress refusal wrapped by the SDK's
  `APIConnectionError` can never come back as a retryable error). Otherwise map via a shared
  `_translate` helper to the U05-30 table: `RateLimitError` -> `RateLimited` (retry-after via
  `herness.core.resilience.classify.parse_retry_after`); `APIConnectionError`,
  `APITimeoutError`, `InternalServerError`, or any `APIStatusError` with status 529 or >= 500
  -> `ModelUnavailable`; `BadRequestError`/`NotFoundError`/`UnprocessableEntityError` ->
  `ConfigError`; `AuthenticationError`/`PermissionDeniedError` -> `AuthError`; anything else
  (e.g. `ConflictError` 409, `RequestTooLargeError` 413) -> `ModelUnavailable`, matching the
  spec's literal "any other" rule.
- Message format: `"<provider> call failed: <ExceptionTypeName>[ HTTP <status>]"` -- names only
  the provider, the exception type and the HTTP status; never a body, header or key.

## Deviation / interpretation flagged
U05-30's postcondition text says the message "names the client key, the operation and the
HTTP status" -- but the given signatures (`translate_openai_error(exc)`, matching module-map
row deps `openai`/`anthropic`, no client-key/operation param) do not carry a client key or a
distinct "operation" value, and neither SDK's exception classes expose one (checked
`openai==3.19.2` and `anthropic==1.8.0` sources: `APIError`/`APIStatusError` carry only
`request`/`response`/`body`/`status_code`, no client identifier). Per the controller ruling
("keep the U05-30 signatures"), I did not add parameters; the message instead names the
provider (`"openai"`/`"anthropic"`, playing the "operation" role) and the exception type,
mirroring `herness.core.resilience.classify._msg`'s established convention in this codebase.
Flagging for controller confirmation since this is a plausible read but not the only one.

## Tests
- `tests/unit/harness/test_llm_base.py` -- UT05-19 (17-row depth x role x client table
  covering every branch of `resolve_request_params`, including the "Opus off -> effort low,
  downgraded" case and the VI-5 budget-temperature case) + UT05-20 (`egress_purpose_for`).
- `tests/unit/harness/test_llm_errors.py` -- UT05-28 (11 openai exception rows + rate-limit
  retry-after + body-never-leaked + egress-block-returned-as-is) and UT05-35 (13 anthropic
  exception rows including status 529/503/504 + rate-limit + egress-block-returned-as-is),
  plus the acceptance check: `find_egress_block` finds a cause nested exactly 10 deep, does
  not find one nested 12 deep, walks `__context__` when `__cause__` is unset, and finds the
  exception itself at depth 0.
- `-k "UT05_19 or UT05_20 or UT05_28 or UT05_35"`: 54 passed.
- `tests/unit/harness/test_llm_base.py tests/unit/harness/test_llm_errors.py`: 59 passed.
- Full `tests/unit/harness`: 484 passed, 1 pre-existing skip (Windows symlink privilege,
  unrelated).
- Full `-m unit` suite: 4181 passed, 5 pre-existing skips, 0 failures.
- Coverage on the two new modules: 100% line, 100% branch (both well above the 90%/85%
  thresholds).

## Gate results
`ruff format .`, `ruff check --fix .`, `mypy` (152 files), `lint-imports` (13 contracts kept),
`tools.check_module_size`, `tools.check_type_ownership` -- all clean. Real pre-commit hooks
ran on commit (no `--no-verify`); one iteration was needed because `detect-secrets` flagged a
test fixture body string containing `"...key=..."`-shaped text (a false positive meant to
prove the response body is never leaked into the error message) -- replaced with a neutral
marker string and it passed cleanly on retry.

## Concerns
- The client-key/operation wording in U05-30's postcondition vs. the fixed signature (see
  Deviation above) is the one open judgment call; everything else in the card followed the
  brief's verbatim algorithm text directly.
- `base.py`/`errors.py` have no runtime dependency on adapters (later cards), so this card is
  otherwise self-contained and adds no new import-linter exceptions.

## Fix round 1

Commit `d52fd949a818d3afda4bc7417c58c91c09ef310c` — `fix(harness): relabel egress tests and mark constants Final (T05-05)`.

Addressed the three T05-05 review findings:

1. (Important, controller) `tests/unit/harness/test_llm_errors.py` had five `find_egress_block` tests named `test_ut05_30_...` with docstrings starting "UT05-30". UT05-30 is the real spec test ID for U05-25 (sync `complete` called from a running loop -> `RuntimeError`), so the old names falsely implied that test was covered. Renamed all five to `test_ut05_35_...` (and their leading docstring line to "UT05-35 ..."), since none of them exercise an OpenAI-specific path — they call `find_egress_block` directly with generic `RuntimeError`/`EgressBlocked` chains, so per U05-30's own Tests row (UT05-28, UT05-35, ST05-13) they land under UT05-35 alongside the existing Anthropic-path egress test. Confirmed no `ut05_30`/`UT05-30` string remains anywhere under `herness/` or `tests/` (the only remaining hits are in `docs/impl/05-harness-core.impl.md`, which is the untouched spec source defining the real UT05-30 row for U05-25).
2. (Minor) `herness/harness/llm/base.py`: `EGRESS_PURPOSE_BY_MODEL_ROLE` and `BASE_ROLE` are now annotated `Final[Mapping[...]]`, matching the module's other true constants (`_AUTO_ON_FAST_STANDARD`, `_AUTO_ON_DEEP`). Line count unchanged in effect (148/180 budget).
3. (Minor) `herness/harness/llm/errors.py`: dropped the redundant `APITimeoutError` from both `unavailable` tuples (openai and anthropic translators). Verified directly in the worktree venv that `openai.APITimeoutError` and `anthropic.APITimeoutError` both subclass their respective `APIConnectionError`, so `isinstance(exc, (APIConnectionError, InternalServerError))` already matches a timeout — behaviour is unchanged. Left an explanatory comment in place of each dropped entry. Line count 122/140.

Verification: `uv run ruff format` / `ruff check --fix` clean on all three touched files; `uv run mypy herness/harness/llm/base.py herness/harness/llm/errors.py` -> Success, no issues; `PYTHONUTF8=1 uv run pytest tests/unit/harness/test_llm_base.py tests/unit/harness/test_llm_errors.py -q -p no:logging` -> 59 passed (re-run after the commit's pre-commit hooks too, still 59 passed). Only the three intended files were staged (`git add <paths>`, no `-A`); real pre-commit hooks ran on commit (no `--no-verify`). `herness/harness/tracing.py` and its tests were not touched.
