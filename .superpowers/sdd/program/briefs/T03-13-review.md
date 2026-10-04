# T03-13 review — Hosted Jev backend (verify)

Reviewed: worktree-agent-a1ec505ecf7351f42 @ 7c59023 (base 88a7f75). Files: herness/enrich/deciders/jev_hosted.py (154/200),
tests/unit/enrich/test_jev_hosted_decider.py, tests/unit/enrich/security/test_jev_hosted_security.py, tests/unit/enrich/test_decider_protocol.py.

Evidence I ran: card tests + UT03-45 -> 28 passed; coverage jev_hosted.py 100 % line / 100 % branch (84 stmts, 10 branches).
Greps: no `httpx(2).Client(` in herness/ outside core/egress; no hostname literal in jev_hosted.py (only settings.base_url; the
default lives in JevSettings). Mutants (scratch copy, 8): 6 killed, 2 survived (see Minor).

### Spec Compliance
- U03-55 JevHostedDecider ✅ — signature matches (client_factory typed httpx2 per ledger ruling); name "jev"; client =
  get_guard().http_client("bulk_classification","redacted_text",timeout=30.0) (jev_hosted.py:90-94), one per decide, shared by
  the pool, closed by base; absolute URLs from settings.base_url (:84-85); policy decider_cloud, breaker decider:jev (:65-66);
  EgressBlocked is a FatalError, not an httpx2.TransportError -> never retried, propagates through TaskGroup (verified in
  openjev.py:169-171, 215-221 and by call-count tests). Postcondition (returned model -> decider_version) ✅ (:96-116).
- U03-56 health ✅ with ruled deviation — GET <base>/v1/models, 5 s guarded client, any failure incl. EgressBlocked ->
  ModelUnavailable with cause kept, message carries only status/error class; version fixed once under a lock (:118-154).
  Payload class `redacted_text` instead of spec `none` (:35) — accepted per ledger ruling (spec 10 U10-51 refuses `none`
  outside model_download); needs the spec note to U03-56.
- UT03-54 ✅ — guard stub raising EgressBlocked (1 transport call, 1 guard call, never retried; batch variant), real guard
  allowing (premium profile) for decide + health, version from listing, fixed once.
- ST03-03 ✅ — real get_guard() under `local` profile (asserted profile == "local"), EgressBlocked(profile_forbids_egress),
  ScriptedNet below GuardedTransport saw no request, socket.connect (off-host) / socket.create_connection recorded none,
  blocked egress line + audit reason; jev forced into DeciderChain (_available patched) -> propagates, resolved once.
- TH03-14 ✅ — key only in Authorization header; error texts "jev: HTTP <status>", "model: not a model id",
  "jev health: <class|status>" carry no body/key/ticket text; extra ST03-16 test (401/503 echoing the key) checks traceback,
  exception vars and captured logs.
- Response bound ✅ per ruling — guard 50 MiB cap while reading (tested via MAX_RESPONSE_BYTES patch -> EgressBlocked
  response_too_large, 1 request, propagates) + load_wire_body 1 MB, listing 64 KB.
- Tests hygiene ✅ — IDs in names, docstrings start with IDs, module pytestmark. Budget 154/200 ✅. Coverage ≥ 90/85 ✅.
- Concurrency of the ContextVar ✅ — `_check` runs on the event loop inside the item's own task (the blocking post is the
  only thing in the pool; `_check` is called after the await returns), and set (`_check`) -> get (`_send`) has no await in
  between; each item is its own TaskGroup task with a copied context, so values cannot cross items. (A plain module global
  would also be correct today — mutant M8 survives for that reason, not because of a test gap that matters.)

⚠️ Cannot verify / needs follow-up outside this card:
- ⚠️ Health payload class deviation (`redacted_text` vs spec `none`) — ruled in ledger; spec note to U03-56 still owed.
- ⚠️ Health listing shape `data[*].id` and reply `model` field unverified against real hosted Jev (V-18).
- ⚠️ A single oversized reply (> 50 MiB) aborts the whole decide batch via EgressBlocked(response_too_large) rather than
  becoming an item error; consistent with "EgressBlocked propagates" and the guard's design, and up to 16 x 50 MiB buffered
  (ruling carry-over with T03-12 M5).

### Strengths
- Minimal subclass over the shared _JevHttpBackend; openjev.py untouched (320/320).
- Tests exercise the real guard (profile gate, allowlist, re-scan, egress/audit lines) with the network mocked below
  GuardedTransport, plus an independent socket tripwire — ST03-03 genuinely proves no socket.
- Strict validation of the untrusted returned `model` before it reaches `model_copy` (which skips pydantic validation);
  mutant M2 confirms the test bites.

### Issues
#### Critical (Must Fix)
- none

#### Important (Should Fix)
- none

#### Minor (Nice to Have)
1. jev_hosted.py:104 — `_RETURNED.set(None)` at the start of `_send` is untested (mutant M1 survives). It matters on the
   invalid-reply resend path: attempt 1 has a valid `model` but bad `answers`, attempt 2 omits `model` -> without the reset
   the stale model would be recorded. Add one test for that sequence.
2. jev_hosted.py:36 duplicates `_DECIDER_VERSION_RE` from herness/core/types/decisions.py:26 (comment says so); drift risk if
   the type's pattern changes. Consider validating via the type (e.g. a TypeAdapter on the field) or a shared constant.
3. jev_hosted.py:49 + openjev.py:75 — each 200 reply body (≤ 1 MB) is JSON-parsed twice (`_returned_model` then `_parse`).
   Cheap at this size; note only.
4. jev_hosted.py:132-134 — any parseable 200 listing counts as healthy even if `settings.model` is not listed (differs from
   OpenJev U03-54). Follows the literal U03-56 postcondition (builder concern 2); worth a spec note alongside the payload one.

### Assessment
**Task quality:** Approved
**Reasoning:** All U03-55/U03-56 behaviours, UT03-54 and ST03-03 are met through the real egress guard with no raw client,
no hard-coded host and no key/body leakage; coverage 100/100 and mutants bite on the load-bearing paths. Remaining items
are a small test gap and spec notes for the ruled health deviations.
