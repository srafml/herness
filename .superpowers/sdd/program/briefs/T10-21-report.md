# T10-21 report: Security end-to-end suite (IT10-05, IT10-10, ST10-14)

Status: DONE (report written before the final commit and updated after it)
Worktree: D:\herness\.claude\worktrees\agent-af7d6dcf70bba364f (branch worktree-agent-af7d6dcf70bba364f, base 9c8f34a)
Commits: 3bd9e68 wip(T10-21): sentinel run, IT10-05, IT10-10, ST10-14 green; final test(T10-21): security end-to-end suite (SHA in the FINAL section)

## Files (tests only, no production change)
- tests/support/secret_leak.py (new): run_sentinel_pipeline(tmp_path, db_path, monkeypatch) -> LeakRun, artefacts(run), RecordingFakeLLM (a FakeLLMClient that records its requests), the sentinel constants.
- tests/integration/test_security_e2e.py (new): IT10-05 x2, IT10-10 x2; pytestmark integration.
- tests/security/test_st10_secret_leak.py (new): ST10-14 x3; pytestmark integration.
- .secrets.baseline: not changed. detect-secrets scan --baseline found nothing in the new files. The regenerated baseline only dropped the audited docs/impl entries, so I restored the file as it was. The single hook hit (USER_REF, a 32-hex user_ref flagged as a Hex High Entropy String) carries an inline "pragma: allowlist secret". LF endings.
- No existing test edited.

## What the run does
Sentinels (R-67 "synthetic" prefix):
- SENTINEL_KEY: stored with the real secrets.set_secret (fake_keyring, local profile, keyring backend) under jira_api_token. That is the name the connector config references (sources.jira.auth.credentials: secret:jira_api_token). It is resolved at the point of use (secrets.resolve), so it is in known_values().
- SENTINEL_CRED (password=<v>, CREDENTIAL shape) and SENTINEL_URL (https://wiki.synthetic-corp.test/kb?token=<v>, URL_TOKEN shape). The record text also carries token=<SENTINEL_KEY>.
- BLOCKED_HOST tracker.synthetic-blocked.invalid: the base_url and hosts of a disabled jira connector in sources.yaml.

Steps:
1. Config tree via sync_env (local profile, paths under tmp).
2. configure_logging(log_dir=data/logs, scrubber=secrets.scrub_secrets, stderr=False).
3. A real Tracer writing data/traces/<run_id>.jsonl, payload sampling 1.0.
4. set_secret, then record_config_change(cfg), which writes data/config_snapshots/<hash>.yaml, LAST and a config_change audit line.
5. install_socket_guard(cfg) from the full config.
6. The files connector runs handle_sync on the ticket CSV: raw lake plus file_ingest rows.
7. A connector auth failure echoes the resolved secret into a log.exception line and a trace retry field.
8. One chat turn (a get_record tool turn, then a text turn), run twice:
   - (a) through registry.get("llm_client","fake"), the recording FakeLLMClient;
   - (b) through the real OpenAICompatClient on ScriptRouter, capturing the wire bodies.
   The tool result is the synced record read back from the raw lake and passed through redact_text, the warehouse tools' redact-on-read (TH05-05). llm_call and tool_call trace events are emitted with payloads.
9. The blocked host: socket.create_connection((BLOCKED_HOST, 443)) under the guard, with socket.socket.connect recorded.
10. One mapping_suggestion review_item is created and approved via decide_review_item. The full review items are serialised for the grep.

Teardown: tracer closed, reset_logging, reset_socket_guard. The offline environment variables are restored by monkeypatch.

## Tests and what they assert
- IT10-05 approval_writes_exactly_one_review_decision_line:
  - creating the item writes no audit line;
  - approving it writes exactly one line with exactly the section 4.6 keys: event review_decision; actor = user_ref; fields {item_id, kind, status approved, decided_by, note_len 0};
  - prev_hash is 64 zeros, the id starts with aud_, and verify_chain is ok with lines == 1.
- IT10-05 second_decision_is_refused_and_chain_links: a repeated decision raises ReviewItemConflict and writes no line. The next line has prev_hash = sha256 of the previous line and note_len 3; the chain verifies.
- IT10-10 sentinel_run_completes_end_to_end:
  - 1 row synced;
  - positive controls: every sentinel is in the raw lake, and SENTINEL_KEY is in known_values;
  - both chat paths return the scripted answer, from 2 recorded requests and 2 wire bodies;
  - the redacted record reached the model, and the wire tool content equals the in-process one.
- IT10-10 audit_trail_of_the_run: one review_decision, one config_change, and one admin_action secret_set that names the secret only; the chain verifies.
- ST10-14 zero_sentinel_hits_in_every_artefact: zero hits for all 3 sentinels in:
  - every file under data/logs (herness-*.jsonl, audit-*.jsonl), data/traces and data/config_snapshots;
  - the sqlite3 iterdump of ops.sqlite;
  - data/reports, if it exists;
  - the FakeLLMClient requests, the wire bodies and the review items.
- ST10-14 grep_scanned_real_artefacts (guard against a vacuous pass): every category except reports is non-empty and holds content from this run:
  - logs: the herness and audit files, the connectors.auth.failed line and the *** mask;
  - traces: llm_call, tool_call, retry and payload;
  - snapshot: the reference secret:jira_api_token;
  - dump: INSERTs for file_ingest and review_item;
  - both model request kinds: the record text;
  - review items: mapping_suggestion.
- ST10-14 blocked_host_never_connects_nor_reaches_the_model: EgressBlocked names the host, there are 0 connect calls, the host was never resolved or cached, and it is absent from every model request.

Evidence:
- 7 passed (about 2.5 s). -k "IT10_05 or IT10_10 or ST10_14" --require-test-ids: 7 passed.
- tests/security + tests/integration/test_security_* + tests/integration/connectors: 664 passed, 1 skipped (symlink privilege).
- ruff check . is clean; ruff format --check . reports 751 files formatted.
- The mypy files setting covers only herness and tools, so the tests are not type-checked.
- The checkpoint commit ran every pre-commit hook: the unit suite passed 7892 on the retry. The first try failed on an unrelated Hypothesis FlakyFailure in PT11-04 (tools/synth params) under load, plus the USER_REF detect-secrets hit, fixed with the pragma.

## Mutation probes (temporary source edits, reverted with git checkout, never committed)
- A. scrub_secrets returns event_dict unchanged: ST10-14 zero-hits FAILS (hits in data/logs herness-*.jsonl and data/traces run_*.jsonl). grep-scanned FAILS (no *** mask).
- B. redact_text returns its text unchanged: ST10-14 zero-hits FAILS with 6 hits (FakeLLMClient request 1 and wire body 1, 3 sentinels each). The traces stay clean because the trace cleaner scrub also masks CREDENTIAL/URL_TOKEN spans, which is defence in depth.
- C. install_socket_guard made a no-op: ST10-14 blocked-host FAILS (assert None is not None, no EgressBlocked). The real resolver took about 19 s for the .invalid name; only the probe does that lookup, and the guarded run never resolves.

## Spec notes / substitutions
- The impl 11 fixture connector has no symbol on the tree. Per the controller ruling, the files connector over a tmp inbox CSV stands in (sync_env pattern). The synth profile has no files entry yet (T11-16 carry-over), so the test writes sources.yaml.
- There is no ChatService on the tree. The chat turn goes through the registry llm_client:fake (FakeLLMClient) and through OpenAICompatClient on ScriptRouter, with Tracer and file logging active. The model-facing record text goes through redact_text, the production redact-on-read boundary of the warehouse tools, because the real get_record needs a warehouse build.
- No report renderer exists on the tree (only the reports/_markup and _data pieces). data/reports/ is scanned if present, with no non-empty guard. Add a render step to the run when the renderer lands.
- Observation, not a leak: a non-credential-shaped known value inside ticket text would not be masked on the local model path. redact_text does not consult known_values; only the log and trace scrubber does. This matches the spec as I read it: the scrubber is the last line of defence for logs and traces, and the model path relies on redaction. The planted key is therefore given token= shape in the record.
- The blocked host is modelled as a disabled connector's base_url/hosts. A disabled source never widens the allowlist, so the guard blocks at getaddrinfo, before any resolution.
- The trace retry event carrying the error text is adversarial input to the trace writer (ST05-14 precedent), not a production emitter.


## FINAL (commits)
- 3bd9e68 wip(T10-21): sentinel run, IT10-05, IT10-10, ST10-14 green
- 19b58a5 test(T10-21): security end-to-end suite
- 8f31e86 test(T10-21-followup): make ST10-54/ST10-55 hermetic
- 5a5d877 test(T10-21): fix round 1 — non-vacuous ops.sqlite and review-item sinks

## Flake follow-up (8f31e86)
- ST10-55 (test_st10_55_sdk_base_url_never_allowlisted_hosts_key_admits_it)
  - Before: after listing acme.snowflakecomputing.com in hosts, check_getaddrinfo("acme...", 443) did a REAL DNS lookup (_resolve_and_cache). That is the non-hermetic step: a gaierror or a slow resolver under load fails or stalls the test.
  - After: a monkeypatched socket.getaddrinfo answers only acme.snowflakecomputing.com with TEST-NET-1 192.0.2.55. Every other name delegates to the real, audited getaddrinfo, where the guard still refuses it before any lookup. The stub is installed only after the unlisted-phase assertions, so the "blocked at getaddrinfo" half is unchanged.
  - Assertions kept: acme is not derived from base_url; acme and evil are blocked while unlisted; listing admits acme; evil is still blocked.
  - Assertion added: es._fresh("192.0.2.55"), which proves the admit path really resolved and cached the address.
  - 4/4 pass.
- ST10-54 (test_st10_54_real_request_line_has_no_egress_line): unchanged. It already talks only to a 127.0.0.1 ThreadingHTTPServer (no real hostname, no DNS, no TLS) with a 5 s client timeout. I could not isolate a deterministic cause of the load failure. Replacing the real loopback server with a mock transport would weaken what it asserts (a real local request writes no egress line), so I left it alone. The commit subject follows the brief; the body says only ST10-55 changed.

## Fix round 1 (5a5d877) — review findings I1, M1, M2, M3
- I1, ops.sqlite dump:
  - The connector auth failure is now also recorded through the production herness.core.resilience.events.record_event("breaker_open", detail.reason=<error text with the resolved secret>), which writes a resilience_event row.
  - A reconcile job fails through the real queue (enqueue, claim, run_handler, finish_job) with JOB_ERROR (password=<CRED> and ?token=<URL>), which writes job.last_error.
  - Positive controls in ST10-14 grep_scanned: the dump has INSERTs for resilience_event and job, the text "upstream rejected credential ***" (the masked breaker reason) and "reconcile failed: password=" (the redacted last_error).
- I1, review items: documented as structural only, in the ST10-14 module docstring and in _review_item. Production review payloads carry ids, scores and counts, never ticket text (TH03-03, enrich.mapping_suggest._payload), so no production path exists to route a sentinel there.
- FINDING (real leak, not patched; needs a ruling): a job error whose message holds a bare known secret value (no credential shape) is stored UNMASKED in job.last_error in ops.sqlite. The cause is that herness.core.jobs.outcomes._last_error and herness.core.jobs.tasks._last_error apply redact_text only, not known_values/scrub_secrets; contrast record_event, which does both.
  - Pinned by test_st10_14_job_error_with_known_secret_is_masked_in_ops, xfail(strict=True). With --runxfail it fails with "synthetic-sentinel-keyring-... is contained" in the job row.
  - Suggested fix (impl 08/10 owner): scrub_secrets before redact_text in both _last_error functions, then remove the xfail marker.
  - The green run therefore gives the job error credential/URL shapes only (JOB_ERROR).
- M1: the by-construction assertion "blocked host absent from model requests" is dropped. The test is renamed test_st10_14_blocked_host_never_gets_a_socket, and its docstring explains why: the host lives only in connector config, which no production path puts into a prompt. The EgressBlocked, 0-connect and not-resolved assertions stay.
- M2: leak_run is one fixture in tests/support/secret_leak.py, registered in tests/conftest.py pytest_plugins as the other support fixtures are (docstring line added). The duplicate fixtures are removed.
- M3: __all__ lists every public name (sentinels, SECRET_NAME, USER_REF, JOB_ERROR, SOURCES_YAML, TICKET_TEXT, LeakRun, RecordingFakeLLM, artefacts, fail_job, known_secret_job_dump, leak_run, ops_dump, run_sentinel_pipeline).

Probes after the fix (throwaway -p probe_plugin under .agent-tmp, monkeypatch at import sites; no tracked file edited):
- None: 7 passed, 1 xfailed.
- scrub_secrets identity (core.secrets, harness.tracing, harness._trace_clean, core.resilience.events): zero-hits RED, with hits in data/logs herness-*.jsonl, data/traces run_*.jsonl and "ops.sqlite .dump" (the resilience_event reason). grep_scanned RED.
- redact_text identity (12 import sites): zero-hits RED, with hits in "ops.sqlite .dump" (job.last_error), FakeLLMClient request 1 and wire body 1.
- install_socket_guard no-op: blocked_host RED (assert None is not None).
- IT10-05 and IT10-10 stay green under every probe; they assert audit and positive controls only.

Result: 7 passed, 1 xfailed. ruff check and ruff format are clean. The fix commit ran all pre-commit hooks (unit suite included) green. Parked per the review: M4 (es._fresh and r._State private use) and M5 (the stderr sink is not grepped). Temp dir D:\t1021-build-tmp could not be deleted (removal blocked by the tool policy); please delete it manually.