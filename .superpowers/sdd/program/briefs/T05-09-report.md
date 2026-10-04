# T05-09 report: Anthropic Message Batches (submit_batch / collect_batch)
Status: DONE

## What was built

AnthropicClient.submit_batch and AnthropicClient.collect_batch (U05-29), extending the
T05-08 Anthropic adapter with BatchCapable. Per controller ruling, batch-specific helpers were
put in a new private sibling module herness/harness/llm/_anthropic_batch.py (budget 200 lines),
receiving already-built SDK client callables from anthropic_client.py. The sibling never
constructs an anthropic, httpx or httpx2 client/transport (verified with the ST05-13(a) AST
checker directly against both new/changed files, zero hits, plus a dedicated test).

### herness/harness/llm/_anthropic_batch.py (153 lines, budget 200)

Public names: BATCH_MAX_WAIT_S (86400), check_batch_supported, build_batch_requests,
validate_batch_id, validate_poll_interval, prepare_submit, validate_collect,
poll_until_ended, collect_results.

- check_batch_supported(cfg): ConfigError unless cfg.supports.batch.
- build_batch_requests(reqs, build_params): 1-10,000 requests, unique request_key (else
  ConfigError); returns the SDK requests=[{custom_id, params}, ...] list and the
  request_key -> LLMRequest pending map.
- validate_batch_id(batch_id): ^msgbatch_[A-Za-z0-9]+$ (else ConfigError).
- validate_poll_interval(poll_s): poll_s >= 5 (else ConfigError).
- prepare_submit / validate_collect: thin wrappers bundling the preconditions of each
  public method (kept the caller side and the line budget small).
- poll_until_ended(batch_id, poll_s, *, retrieve, sleep, monotonic): loop retrieve(batch_id)
  until processing_status == "ended"; deadline is monotonic() + BATCH_MAX_WAIT_S, checked
  each iteration; raises ModelUnavailable("batch <id> not ended after 86400 s") once passed;
  sleep(poll_s) between iterations. Pure dependency injection: no SDK/HTTP objects required in
  tests, no real waiting needed to test the timeout.
- collect_results(batch_id, results, pending, map_message): iterates the results async
  iterator; succeeded items are mapped via map_message(message, pending.get(custom_id)) and
  collected; other types (errored, expired, canceled) are logged at WARNING as
  harness.llm.batch_item_failed with exactly batch_id, custom_id, result_type (no error
  text). More than 10,000 lines raises OutputValidationError (fail closed). Returns the mapped
  dict and every custom_id seen (success or not), so the caller can clear all of them from its
  pending dict. Closes results in a finally if it exposes .close() (the real SDK decoder
  does), even when the 10,000 cap trips mid-stream.

### herness/harness/llm/anthropic_client.py (400 lines, hard limit, see Concerns)

- AnthropicClient.__init__ now also sets self._pending: dict[str, LLMRequest] = {} and
  self._pending_lock = asyncio.Lock() (design's state-and-concurrency table row).
- _sdk was widened from _sdk(self, req: LLMRequest) to _sdk(self, req: LLMRequest | None):
  with a request it derives purpose/run_id/task_id/timeout exactly as before (same guard args,
  same tests green); with None (batch-admin calls) it uses purpose "reasoning", no run or
  task id, and self.cfg.timeout_s. The former separate _http_client helper was folded into
  _sdk to make room under the 400-line hard limit; behaviour is identical (same guard
  fail-closed check, same anthropic.AsyncAnthropic(..., http_client=...) construction with
  http_client always passed so the ST05-13(a) lint still passes).
- _fail was changed from _fail(self, exc, req: LLMRequest) to
  _fail(self, exc, task_id: str | None) so batch calls (no single request) can pass None;
  acomplete/astream now pass req.metadata.task_id explicitly. Behaviour unchanged.
- submit_batch(reqs): _anthropic_batch.prepare_submit (checks supports.batch, bounds,
  uniqueness, builds params via self._build_params) before any network call; then
  client.messages.batches.create(requests=...) through the guarded SDK client
  (purpose="reasoning", run_id=None, task_id=None, timeout=cfg.timeout_s); on success,
  updates self._pending under the lock and returns batch.id.
- collect_batch(batch_id, poll_s=30.0): _anthropic_batch.validate_collect, then polls via
  _anthropic_batch.poll_until_ended with retrieve=client.messages.batches.retrieve,
  sleep=asyncio.sleep, monotonic=clock.monotonic; snapshots self._pending under the lock,
  calls client.messages.batches.results(batch_id), maps via _anthropic_batch.collect_results
  with lambda msg, req: self._map_message(msg, req, 0, batch=True); removes every returned
  custom_id (succeeded or not) from self._pending under the lock; returns the responses dict.
- SDK exceptions from create/retrieve/results are caught the same way as acomplete
  (except (anthropic.AnthropicError, EgressBlocked) as exc: self._fail(exc, None)), reusing the
  existing U05-30 translation path unchanged.
- Class docstring updated to mention LLMClient, StreamCapable and BatchCapable (T05-09).

### docs/impl/05-harness-core.impl.md

Added the module-map row right after anthropic_client.py:
herness/harness/llm/_anthropic_batch.py | Anthropic batch helpers (private, U05-29) | ... | L4 |
anthropic | 200 |. uv run python -m tools.check_module_size exits 0.

## Purpose/timeout used for create/retrieve/results (ruling 3)

All three batch-admin calls (create, retrieve, results) go through self._sdk(None), which uses
purpose "reasoning" (the default egress_purpose_for bucket), payload_class="aggregated_evidence"
(unchanged), run_id=None, task_id=None and timeout=self.cfg.timeout_s (600 s for the anthropic
clients in config/models.yaml). A batch spans a set of requests (possibly different
tasks/model_roles), so it is not scoped to a single request's purpose or timeout; "reasoning" was
chosen over "reasoning_final" because it is the default bucket for every role not explicitly
listed (R-38), and using the client's own configured timeout_s (rather than any one request's
timeout_s) matches "a batch call is not scoped to one request." This is a design choice within
the latitude the ruling grants, not a spec-mandated value (the unit spec does not name a
purpose/timeout for the admin calls).

## SDK facts found

- anthropic 1.8.0's client.messages.batches: create(requests=[{custom_id, params}]) ->
  MessageBatch; retrieve(batch_id) -> MessageBatch (processing_status,
  Literal["in_progress","canceling","ended"]); results(batch_id) is async def and returns
  AsyncJSONLDecoder[MessageBatchIndividualResponse] (async-iterable, has .close()); each line
  has .custom_id: str and .result (a discriminated union on type:
  succeeded/errored/canceled/expired; succeeded.message: anthropic.types.Message).
  results() internally calls retrieve() again to get results_url before streaming; this is
  why the collect-side tests patch AnthropicClient._sdk with a fake messages.batches object
  rather than driving the real internal retrieve-then-stream chain through a mock transport
  (that chain is SDK-internal plumbing, not part of this card's units).
- mypy: a Callable[[str], Awaitable[X]] parameter type where X is a Protocol with a
  Literal[...]-typed attribute does not accept the real SDK's Literal["in_progress", ...]
  attribute (protocol attributes are checked invariantly, not covariantly); worked around by
  typing retrieve's return and the result-line's .result as Any in the sibling module.
- client.messages.batches.create(requests=...) expects Iterable[Request] (a TypedDict);
  since _build_params returns dict[str, object], the call site uses cast("Any", sdk_requests)
  rather than threading a stricter type through _build_params/build_batch_requests.

## Tests

New file tests/unit/harness/test_llm_anthropic_batch.py, 21 tests, all named and docstring'd
UT05-39, pytestmark = pytest.mark.unit:

- Pure _anthropic_batch unit tests (no SDK/HTTP): check_batch_supported both branches,
  validate_batch_id/validate_poll_interval both branches, build_batch_requests bounds
  (0, 10001) and duplicate key, poll_until_ended polls-until-ended (multi-iteration, sleep
  between) and timeout (faked sleep/monotonic, no real waiting), collect_results mapping +
  WARNING logging + .close() call, collect_results over-10,000 cap, and the sibling module
  passing the ST05-13 AST lint directly.
- AnthropicClient-level tests: isinstance(client, BatchCapable); submit_batch/collect_batch
  ConfigError preconditions (batch unsupported, invalid batch id, poll_s < 5); submit_batch
  builds custom_id/params per request and returns the batch id (real HTTP through the
  _StubGuard/httpx2.MockTransport pattern from test_llm_anthropic.py, asserting guard call
  args ("reasoning", "aggregated_evidence", None, None, 600.0)); submit_batch fails closed on
  a guard without async_http_client and on an SDK error (translated to ModelUnavailable,
  pending unchanged); collect_batch happy path (fake SDK: two retrieve calls then ended, one
  sleep, one succeeded result) asserting batch=True, cost halved (0.007000 for 1000 input/500
  output tokens on claude-opus pricing), and pending cleared; unknown custom_id (not in
  pending) maps with parsed is None; errored/expired/canceled absent from the dict and logged
  at WARNING with exactly batch_id/custom_id/result_type (asserted no message/error field);
  over-10,000 results at the client level; collect_batch timeout (faked asyncio.sleep and
  ac.clock.monotonic, no real waiting); and an SDK error from retrieve() translated to
  ModelUnavailable.

Coverage (--cov-branch, both files together with test_llm_anthropic.py):
_anthropic_batch.py 100% line/100% branch; anthropic_client.py 99% line (one pre-existing,
unrelated partial branch in _assistant_blocks, T05-08 code untouched by this card),
comfortably over the 90%/85% bar.

## Gates run

- uv run ruff format . then uv run ruff check --fix . : clean (repo-wide).
- uv run mypy (whole files list): Success, no issues found in 202 source files.
- uv run lint-imports: 13 contracts kept, 0 broken.
- uv run python -m tools.check_module_size: exit 0.
- PYTHONUTF8=1 uv run pytest tests/unit/harness -q -p no:logging: 862 passed, 1 skipped
  (pre-existing, unrelated: test_warehouse.py symlink test skipped for lack of Windows
  privilege), 1 failed: test_st05_13_ast_lint_harness_builds_no_unguarded_clients, the
  documented KNOWN-RED (2 hits, both herness/harness/llm/openai_compat.py, confirmed present
  identically before any change in this card); verified directly that neither new/changed file
  (anthropic_client.py, _anthropic_batch.py) contributes any hit.
- PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging: launched
  in the background (exceeded the 120 s foreground timeout limit of this session's shell tool);
  its final pass/fail counts are reported in the agent's reply once it completes.

## Rulings and deviations

- Ruling 1 (module split, budget): followed as specified. anthropic_client.py landed at
  exactly 400 lines (the hard limit) after folding _http_client into _sdk and trimming three
  docstrings/comments; no functionality was cut to make room.
- Ruling 3 (client construction for collect): _sdk was widened to accept req: LLMRequest |
  None rather than adding two more thin wrapper methods, to stay inside the line budget; see
  "Purpose/timeout used" above.
- No test in the brief's Tests row (UT05-39) was changed or contradicted; no spec value was
  changed from the brief's verbatim text (BATCH_MAX_WAIT_S = 86400, bounds 1-10,000, the
  msgbatch_ regex, poll_s >= 5, the log event name/fields, the 10,000 result cap all copied
  verbatim).
- The pre-existing "T05-06 carry-over" constants in anthropic_client.py
  (_MAX_RESPONSE_TEXT_CHARS etc.) were left as is: replacing them with
  base.MAX_RESPONSE_TEXT_CHARS etc. was tried but made the file longer (the base import would
  need 4 more names, forcing it onto multiple lines); the "if it saves lines" condition in the
  ruling did not hold, so they were kept.

## Concerns

- anthropic_client.py is now exactly at the 400-line ENG hard limit with zero slack. Any future
  card touching this file will need to shrink something else first or split further.
- _sdk's signature changed from _sdk(self, req: LLMRequest) to
  _sdk(self, req: LLMRequest | None); no test in test_llm_anthropic.py calls it by name, so
  this is not a breaking change, but future readers should note None now means
  "batch-admin, not scoped to one request."
- The purpose/timeout choice for the batch-admin calls is a reasonable default within the
  ruling's latitude but is not spelled out verbatim in the unit spec; a later card wanting
  per-batch run/task attribution will need to revisit it.
- results() in the SDK internally re-calls retrieve(); the collect-path tests fake
  messages.batches directly rather than driving that internal chain through a mock transport,
  so the SDK's own retrieve-before-stream behaviour is not exercised by these tests (that is
  the SDK's own concern, not U05-29's).

## Carry-overs

- None new. The pre-existing T05-06 carry-over comment in anthropic_client.py remains (see
  Deviations above).

## Broad gate result

PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging finished:
2 failed, 5616 passed, 13 skipped, 32 deselected, 2 xfailed in 444.61s. The two failures are
exactly the two documented KNOWN-RED items on this base: test_it00_01_pre_commit_run_all_files
(IT00-01) and test_st05_13_ast_lint_harness_builds_no_unguarded_clients (ST05-13(a), the same
2 pre-existing openai_compat.py hits). No new failures were introduced by this card.

## Commit

07fe0dc feat(harness): T05-09 anthropic message batches
(5 files changed: .secrets.baseline, docs/impl/05-harness-core.impl.md,
herness/harness/llm/_anthropic_batch.py, herness/harness/llm/anthropic_client.py,
tests/unit/harness/test_llm_anthropic_batch.py)
