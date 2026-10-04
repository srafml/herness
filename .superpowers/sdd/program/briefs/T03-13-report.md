# T03-13 report — Hosted Jev backend (build)

Status: DONE_WITH_CONCERNS (one deliberate deviation: health payload class, see below)
Worktree: D:\herness\.claude\worktrees\agent-a1ec505ecf7351f42 · base 88a7f75

## What was built
- `herness/enrich/deciders/jev_hosted.py` (154/200 lines): `JevHostedDecider(_JevHttpBackend)`;
  `name="jev"`, `_policy="decider_cloud"`, `_breaker_key="decider:jev"`, `_path` absolute
  `<base_url>/v1/systemone` (per instance; guarded clients have no base_url), `version =
  settings.model` at construction. `_open_client` -> `egress.get_guard().http_client(
  "bulk_classification", "redacted_text", timeout=30.0)` (one client per decide, shared by the
  pool, closed by the base). openjev.py NOT edited (320/320).
- Postcondition (returned model -> decider_version): the base drops the reply's `model`, so
  the subclass overrides `_check` (reads `model` from the 200 body into a ContextVar;
  a non-string / pattern-violating model -> OutputValidationError, i.e. retried once then
  item error, since `decider_version` has pattern `^[A-Za-z0-9._:/@+-]{1,128}$`), `_send`
  (wraps answers in a dict subclass carrying the model) and `_output` (model_copy with
  `decider_version=<returned model>` when it differs). Absent `model` keeps `version`.
- `health()`: GET `<base_url>/v1/models` through a 5 s guarded client (or client_factory);
  200 + parseable listing (<= 64 KB, via openjev `_listed_models`) fixes `version` once
  (lock + flag); any failure incl. EgressBlocked -> `ModelUnavailable("jev health: <status
  or error class>")`, client always closed.
- EgressBlocked: a FatalError (not RetryableError) -> aretry_call never retries it; it is not
  an httpx2.TransportError so the base propagates it untouched; the TaskGroup cancels the
  batch. Pinned with call-count assertions (1 transport call for 1 item).
- Response bound: guard 50 MiB cap while reading (EgressBlocked("response_too_large")
  propagates; tested by patching `_egress_transport.MAX_RESPONSE_BYTES`) + load_wire_body
  1 MB (ledger ruling; no per-client cap).

## Tests
- tests/unit/enrich/test_jev_hosted_decider.py (UT03-54, 16 tests): construction builds
  nothing; samples validated; decide through the REAL guard (premium profile, destinations
  [api.typesafe.ai], purposes [bulk_classification]; MockNet below GuardedTransport): spy
  shows one http_client("bulk_classification","redacted_text", timeout=30.0), absolute
  URLs, bearer, client closed, 3 allowed egress lines; returned model recorded / absent
  kept; malformed model (3 cases) -> item error after 2 requests; response_too_large
  propagates after 1 request; guard stub raising EgressBlocked -> propagates, 1 call,
  never retried; batch of 5 -> <= 5 calls; health sets version from listing, fixed once;
  not-listed keeps settings.model; 503 / malformed listing -> ModelUnavailable; health
  EgressBlocked -> ModelUnavailable (cause kept); client_factory path for decide + health
  (trailing slash in base_url normalised).
- tests/unit/enrich/security/test_jev_hosted_security.py: ST03-03 x3 — `local` profile,
  real get_guard(): decide -> EgressBlocked(profile_forbids_egress), no request reached the
  MockNet pool, no off-host socket connect (socket.connect/create_connection recorded;
  loopback allowed because asyncio's Windows self-pipe uses a loopback socketpair),
  one blocked egress line + audit line, no key anywhere; jev forced into DeciderChain
  (`_available` patched True) -> EgressBlocked propagates from the chain, resolved once;
  health -> ModelUnavailable, no socket. Plus ST03-16 (TH03-14) x2 for jev: 401/503 reply
  echoing the key -> error text/traceback/log events never contain it; key only in the
  Authorization header, never in the body.
- tests/unit/enrich/test_decider_protocol.py: UT03-45 table extended with JevHostedDecider.
- RED: `pytest tests/unit/enrich/test_jev_hosted_decider.py` with the module absent ->
  `ModuleNotFoundError: No module named 'herness.enrich.deciders.jev_hosted'` (collection error).
- GREEN: own tests 21 + UT03-45 7 passed; coverage jev_hosted.py 100 % line / 100 % branch
  (84 stmts, 10 branches); `pytest tests/unit/enrich -q -p no:logging` 638 passed, 1 skipped.

## Gates
ruff format/check clean on touched files; mypy (touched files) clean; lint-imports 13 kept /
0 broken; check_module_size exit 0; ST10-25 / ST05-13 / UT00-58 lint tests pass (86 passed).
No module-map or import-linter contract lists individual deciders modules -> no pyproject change.

## Deviations / concerns
1. **Health payload class `redacted_text`, not `none` (spec U03-56 / brief say "none").**
   Spec 10 U10-51 step 2 (and `EgressGuard._policy`) refuses `payload_class == "none"`
   except for `model_download`, so the spec's value makes every production health() raise
   EgressBlocked -> ModelUnavailable and the chain would always skip jev. The probe is an
   empty-bodied GET under the same purpose, so `redacted_text` passes the profile gate that
   decide needs anyway. One constant (`_HEALTH_PAYLOAD`) in jev_hosted.py; revert = one token.
   Needs a spec note / ruling (U03-56 algorithm).
2. U03-56 postcondition ("version = listed id equal to settings.model, else settings.model")
   always yields settings.model, so health's listing is a liveness check and a 200 listing
   that lacks the model does NOT raise (literal reading of the postcondition; OpenJev's
   U03-54 raises there). The real served model reaches outputs via the returned `model`
   (U03-55 postcondition, design 03 §3.3 "version = returned model string").
3. Returned `model` failing the decider_version pattern is treated as an invalid reply
   (OutputValidationError) rather than ignored — strict parse of untrusted output (TH03-06).
4. The subclass reaches private helpers of openjev (`_JevHttpBackend`, `_Session`,
   `_listed_models`) — same package, by design of the T03-12 split.
5. Postcondition plumbing uses a ContextVar set in `_check` and read in `_send` (same task;
   no edit to openjev.py possible within its 320/320 budget).

## Carry-overs
- T03-16 register_deciders registers ("decider","jev"); U03-69 build_decider resolves the key.
- V-18 base URL for live use; health listing shape `data[*].id` unverified against hosted Jev.
- Parked M5 of T03-12 (per-client memory cap) unchanged: guard 50 MiB cap applies.

## Fix round 1

Review Minors 1 and 2 addressed; nothing else changed (openjev.py and herness/core/types untouched).

1. **`_RETURNED.set(None)` reset (jev_hosted.py `_send`)** — added
   `test_ut03_54_resend_without_model_drops_stale_returned_model`: attempt 1 replies 200 with
   `model: "jev-stale"` and invalid (empty) answers, the resend is valid and omits `model`;
   asserts 2 requests, no error, `decider_version == "jev-latest"`. Passes.
   **Mutant M1 (reset removed) still survives, and it is an equivalent mutant:** `_RETURNED` is
   read only after `super()._send` returns answers, which requires `_parse(self._check(resp))` to
   succeed on a 200 in that same call, and `_check` always sets `_RETURNED` (to the reply's model
   or to None when absent) before `_parse` runs. A stale value can therefore never be read within
   one item; across items each TaskGroup task has its own copied context. The reset is purely
   defensive. The new test pins the reviewed sequence as a regression guard; no test can kill M1
   without monkeypatching `_check`. Options for the controller: keep the line as defensive, or
   drop it in a later round (behaviour-neutral).
2. **Duplicated `decider_version` pattern** — removed `_MODEL_RE` (and `import re`). The returned
   model is now validated by `_MODEL_ID = TypeAdapter(Annotated[str,
   DecisionOutput.model_fields["decider_version"]])`, so the pattern has one source
   (`_DECIDER_VERSION_RE` in herness/core/types/decisions.py). Non-str -> OutputValidationError
   (explicit isinstance check, strict), pattern/length violation -> pydantic ValidationError ->
   OutputValidationError("model: not a model id"). Mutant "skip the TypeAdapter validation" is
   killed (2 parametrized malformed-model cases fail).

Verification: card tests 29 passed; jev_hosted.py coverage 100% statements / 100% branches
(86 stmts, 10 branches); ruff format + check clean; mypy clean on both touched files;
jev_hosted.py 159 lines.
