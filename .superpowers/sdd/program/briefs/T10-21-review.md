# T10-21 verify review (Security end-to-end suite: IT10-05, IT10-10, ST10-14)

Reviewed: 3bd9e68, 19b58a5 (base 9c8f34a). Files: tests/support/secret_leak.py, tests/integration/test_security_e2e.py, tests/security/test_st10_secret_leak.py. No production change, no existing test edited, .secrets.baseline untouched (diff --stat confirms).

### Spec Compliance
- IT10-05 ✅ Approving a review_item through `decide_review_item` on the fixture ops store writes exactly one `review_decision` line with the §4.6 key set and fields; creating the item writes nothing; a repeated decision is refused with no line; the chain links and verifies (test_security_e2e.py:60-100).
- IT10-10 ✅ with substitutions. Files connector sync (controller ruling), one chat turn through the registry `llm_client:fake` (RecordingFakeLLM) and through OpenAICompatClient on ScriptRouter, real Tracer, file logging with scrub_secrets, record_config_change, socket guard, review item approval. Positive controls: all 3 sentinels are in the raw lake, SENTINEL_KEY is in known_values, and the record text reaches both model paths (test_security_e2e.py:119-147).
- ST10-14 ✅ for logs, traces, config_snapshots and model requests: the sinks hold real run content and the mutation probes turn them red. ❌ (partial) for the ops.sqlite dump and review items: they are non-empty, but no sentinel can reach them by construction (see Important I1). Reports are not covered because no renderer exists (see ⚠️).
- ⚠️ Cannot verify / deviations:
  - Rendered reports: there is no renderer on the tree (herness/reports has no render entry point). `data/reports/` is scanned only if it exists, so this spec-listed sink has no coverage now. The run needs a render step once the renderer lands.
  - Model-path protection: the helper itself calls `redact_text` (secret_leak.py:226) in place of the production record tool (`_tools_record.sample_rows` / `_redacted`), because there is no ChatService or warehouse build. So the tests prove what `redact_text` does to these shapes, not that production wires it in.
  - Builder observation, confirmed: `redact_text` does not consult `known_values()`. SENTINEL_KEY is planted in `token=` shape (secret_leak.py:105), so the model path catches it through the CREDENTIAL detector. A known secret without a credential shape inside ticket text would reach a local model. That path is TH05-05 and outside the ST10-14 sink list. Coordinator decision whether to track it.

### Strengths
- The run uses real production pieces: configure_logging plus scrub_secrets, Tracer plus _trace_clean, set_secret/resolve (known values), record_config_change, install_socket_guard (audit hook), handle_sync, decide_review_item, and the OpenAICompat wire bodies.
- `test_st10_14_grep_scanned_real_artefacts` is a real guard against a vacuous pass. It checks for the `***` mask, the llm_call/tool_call/retry/payload trace events, the secret reference in the snapshot, and the INSERTs in the dump.
- Offline and deterministic. respx intercepts at 127.0.0.1 with no socket. The blocked `.invalid` host is refused by the guard at getaddrinfo, before any DNS (probe: resolved_blocked=False, 0 connects). The inbox mtime is fixed. Nothing asserts on wall-clock values, and the audit/log globs tolerate a date rollover.
- Teardown is clean: tracer.close, reset_logging and reset_socket_guard in `finally`; the redactor, env vars and socket.connect go through monkeypatch; the autouse reset_config clears `_KNOWN`. The pytest-captured stdlib log line is scrubbed too (checked with --log-cli-level=ERROR: 0 sentinel hits).

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- I1 The ops.sqlite dump and review-item sinks are vacuous for sentinels (secret_leak.py:274-277, :297, :314).
  - The review item payload is built from constants (source/entity/key/field/value=printer). No record text or secret ever enters it.
  - `handle_sync` runs under FakeJobContext, and the planted auth failure (secret_leak.py:242-251) goes only to log/trace. So nothing secret-bearing is written to ops.sqlite: the dump holds only file_ingest, review_item and schema rows.
  - Result: the ST10-14 zero hits for "an ops.sqlite .dump" and "review items" are true by construction. Scrubbing or redaction on those paths could be removed and the tests would stay green. The grep-scanned test proves the sinks are non-empty, not that sentinels could reach them.
  - Fix: route one secret-bearing text into ops.sqlite through a production path. For example, the connector failure recorded through `herness.core.resilience.events.record_event` (it scrubs and redacts, events.py:96-114; the SqliteResilienceBackend is already bound at secret_leak.py:291), or a failing files sync whose recorded failure lands in ops. Then add a positive control that the event/failure row exists in the dump.
  - For review items: derive the payload from the synced record through the production redact-on-read path, like the sample value of a real mapping suggestion. If no production path exists, say explicitly in the ST10-14 docstring and the report that these two sinks are covered only structurally, as is done for reports.

#### Minor (Nice to Have)
- M1 The blocked-host "absent from every model request" assertion (test_st10_secret_leak.py:99-102) is true by construction. The host exists only in sources.yaml (secret_leak.py:99-100) and nothing tries to put it in a request. The EgressBlocked / 0-connect / not-resolved assertions are the meaningful part (mutation-proven).
- M2 The `leak_run` fixture is duplicated verbatim (test_security_e2e.py:106-116, test_st10_secret_leak.py:43-53). Move it into tests/support/secret_leak.py (a fixture plugin) or a shared conftest.
- M3 `__all__` (secret_leak.py:73) omits names the tests import: SENTINEL_KEY, SECRET_NAME, USER_REF, SENTINEL_CRED, SENTINEL_URL, RecordingFakeLLM.
- M4 Private production API use: `es._fresh` (secret_leak.py:271) and `r._State` (secret_leak.py:290). This is acceptable for a test, but it couples the test to internals.
- M5 `stderr=False` (secret_leak.py:336): the stderr log sink is not part of the grep. This is low risk because the same processor chain applies, and the stdlib propagation was checked scrubbed.

### Mutation probes (a throwaway -p plugin under .agent-tmp/t1021-verify with monkeypatch; no tracked file touched; folder deleted afterwards)
| Mutation | Result |
|---|---|
| none | 7 passed |
| scrub_secrets -> identity (core.secrets, harness.tracing, harness._trace_clean, core.resilience.events) | RED: ST10-14 zero_sentinel_hits (hits: data/logs herness-*.jsonl and data/traces run_*.jsonl); ST10-14 grep_scanned_real_artefacts (no `***`). 5 others pass. |
| redact_text -> identity at all 10 import sites | RED: ST10-14 zero_sentinel_hits (6 hits: FakeLLMClient request 1 and wire body 1, 3 sentinels each). The traces stay clean because the _trace_clean scrubber also applies the CREDENTIAL/URL_TOKEN detectors. |
| redact_text -> identity at herness.core.redact only (the site the helper uses) | RED: same 6 hits |
| install_socket_guard -> no-op (egress_socket and egress) | RED: ST10-14 blocked_host (blocked is None, unguarded gaierror after a real DNS lookup, about 12 s; this happens only in the probe) |
| all three | RED: all 3 ST10-14 tests (10 hits) |

The IT10-05 and IT10-10 tests stay green under every mutation. That is expected: they assert audit and positive controls, not leakage.

### Determinism / hygiene runs (PYTHONUTF8=1)
- `uv run pytest -k "IT10_05 or IT10_10 or ST10_14" --require-test-ids -q`, 5 times: 7 passed each time (14-21 s, mostly collection).
- Both files together: 7 passed (1.9 s). Isolated single tests (ST10-14 blocked_host; IT10-10 audit_trail): pass.
- pytest-randomly and xdist are not installed, so there is no random ordering to toggle.
- Pollution check: the new files first, then tests/security plus tests/integration/test_security_*.py: 657 passed, 1 skipped (the symlink-privilege skip in ST05-17, pre-existing).
- ruff check and ruff format --check on the 3 files: clean. detect-secrets scan on the 3 files: no results (only the USER_REF line carries the allowlist pragma).
- Test IDs: every function has _it10_05_ / _it10_10_ / _st10_14_, docstrings start with the ID, and module-level pytestmark = pytest.mark.integration matches the other tests/security integration files.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The core threat assertions for logs, traces, config snapshots and model requests are real and mutation-proven. Two of the spec-listed sinks (the ops.sqlite dump and review items) receive no sentinel-bearing input, so their zero-hit checks are vacuous (I1). Either add a production path that can carry a sentinel into them, or document them explicitly as structural-only.
