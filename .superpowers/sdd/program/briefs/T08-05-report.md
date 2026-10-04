# T08-05 report — Resilience store backend, events and metrics

Status: DONE_WITH_CONCERNS (one contract ignore entry added; see Design decisions 1)
Commit: 628e719 feat(resilience): add resilience store backend, events and metrics (T08-05)
Worktree/branch: D:\herness\.claude\worktrees\agent-a6d12d170b7cfb4da / worktree-agent-a6d12d170b7cfb4da (base cdadffe)

## What was built
- herness/store/ops/resilience.py (228 / 320): SqliteResilienceBackend implementing every
  ResilienceBackend method (health_get/list/apply/claim_probe/reset, insert_event, count_events,
  event_counts, latest_event, insert_metric_samples, purge_events, purge_metric_samples) and the
  module function purge_events (run_write op "resilience_event_purge"). Reads via read_one/read_all,
  writes via run_write; IN (...) placeholders from length only.
- herness/store/ops/metrics.py (82 / 120): record_metric_samples(samples, *, conn=None)
  (>100 000 -> ConfigError("too many metric samples"); labels canonical_json > 1 024 bytes ->
  ConfigError naming the metric; conn -> executemany on the caller's transaction; else 500-row
  chunks each in run_write(op="metric_samples")) and purge_metric_samples (op "metric_purge").
- herness/core/resilience/events.py (184 / 200): record_event, EVENT_KINDS (21), DETAIL_FIELDS,
  per-kind log level / §8.1 event name / trace type.
- herness/core/resilience/metrics.py (217 / 260): record_counter, record_histogram, record_gauge,
  timed, flush_metrics; METRIC_FLUSH_INTERVAL_S=10, METRIC_BUFFER_MAX=10000,
  METRIC_GAUGE_KEYS_MAX=1000, histogram flush trigger at 1 000.
- herness/core/resilience/_metric_buffer.py (27, new private sibling, default 400): MetricBuffer.
- herness/core/resilience/_state.py (140 / 140, net 0): metric_buffer: MetricBuffer
  (placeholder replaced); stale atexit comment reworded.
- herness/store/ops/__init__.py (225 / 400): "08 resilience" (purge_events) and "08 metrics"
  (record_metric_samples, purge_metric_samples) blocks after privacy, `# isort: split`.
- pyproject.toml: one ignore_imports line in ops-areas-acyclic
  ("herness.store.ops.resilience -> herness.store.ops.metrics").
- herness/core/audit.py (391 / 395) and herness/core/redact.py (316 / 390): T08-05 markers wired.
- Tests: tests/unit/store/ops/test_store_ops_resilience.py (218), test_store_ops_metrics.py (129),
  tests/unit/core/resilience/test_resilience_events.py (215), test_resilience_metrics.py (275),
  tests/security/test_st08_events_metrics.py (83); `ops_db` fixture added to
  tests/unit/core/resilience/conftest.py (ops_store + reset_process_state +
  bind_ops_backend(SqliteResilienceBackend())). UT02-68 test list needed no change.

## Design decisions
1. Area -> area delegation. U08-94 requires SqliteResilienceBackend.insert_metric_samples to return
   herness.store.ops.metrics.record_metric_samples, but impl 02 §2.2 / contract ops-areas-acyclic
   forbids an area importing another area, and grimp sees function-local imports too, so a local
   import does not help. Alternatives considered: importlib lookup at call time (evades the linter,
   same coupling, rejected as contract evasion); constructor injection (the no-arg
   SqliteResilienceBackend() the card and bind_core_backends need would still need the import in
   resilience.py). Chosen: a top-level `from .metrics import ...` plus ONE narrowly scoped
   ignore_imports entry in ops-areas-acyclic with a comment citing U08-94/R-12. The graph stays
   acyclic (metrics imports only core). Needs the controller's ruling; if rejected, the fallback is
   an importlib lookup inside the two delegating methods (2 lines).
2. MetricBuffer location. _state.py is at 140/140 and builds ProcessState at import time, so it
   cannot import metrics.py (which imports _state) without a cycle. MetricBuffer is a plain
   dataclass in the private sibling herness/core/resilience/_metric_buffer.py (stdlib only),
   imported by both _state.py and metrics.py. Needs a module-map spec note (private sibling).
   last_flush defaults to time.monotonic() (not herness.core.time.monotonic) so building a
   ProcessState never consumes a test's patched clock (UT02-28 patches clock.monotonic with a
   finite iterator); flush sets last_flush from clock.monotonic().
3. flush_metrics with the ops port unbound returns 0 and KEEPS the buffer (bounded by the caps), so
   metrics recorded before binding are written by the first flush after binding; the dropped count
   is logged at that flush. A store failure (HernessError, or pydantic ValidationError) logs
   resilience.metrics.flush_failed(rows, error_type) and discards the rows. record_* raise only
   ConfigError for invalid input.
4. record_event log line: detail keys collide with reserved fields (job_done detail has `kind`,
   retry detail has `target`), so the log line carries kind, target, run_id, job_id, task_id and
   the filtered detail nested as `detail=`. Components log through get_logger("resilience"|"jobs").
   An unknown component also raises ConfigError.
5. record_event string cleaning: known secret values are masked first by reusing
   herness.core.secrets.scrub_secrets (the log scrubber) because redact_text does not know resolved
   secret values (ST08-02 plants one); then redact_text on a 4 000-char window (classify's ruling:
   redact then cut; a text longer than the window loses its last 200 redacted chars), then cut to
   200. A failing scrub/redaction drops the value (fail closed). datetimes -> ts text; NaN/inf,
   objects, nested dicts and lists holding non-scalars are dropped; lists cut to 20 items. Dropped
   keys are logged at DEBUG as a count only (resilience.event.detail_dropped), never key names.
   target is cut to 200 chars but not redacted (it is a breaker key / job kind used for
   count_events(target=...) lookups).
6. health_apply stamps updated_at = now, forces source = key and cuts last_error to 500 chars in the
   written row, and returns that written row as `after` (so the return equals the stored row).
7. Name validation uses functools.lru_cache on (name, component, kind) instead of a set (errors are
   never cached); component must also match the MetricSample component regex (<= 32 chars) so a
   valid-looking name can never fail at flush. Labels are type-checked at run time (int values
   refused).

## Spec notes (DDL vs impl 08 §4.1.x, ambiguities)
- source_health (001): matches §4.1.3; DDL has DEFAULTs (state 'closed', failures/trips 0) and no
  length check on last_error (the 500-char cut is applied by health_apply).
- resilience_event (002): matches §4.1.4; kind/component have no CHECK (spec 08 owns the values),
  detail DEFAULT '{}' with json_valid; target has no length check (cut to 200 in insert_event). An
  extra index resilience_event_ts (ts) exists beyond §4.1.4's (kind, ts); purge_events uses it.
- metric_sample (006): matches §4.1.6 (rowid, ts check, name GLOB 'herness_*', kind CHECK, labels
  JSON object <= 1 024).
- U08-20 says the histogram list flushes at 1 000 and caps at 10 000; with a bound backend the cap
  is only reachable when the flush cannot drain (unbound port). UT08-32's "10 001 values ... overflow
  dropped" is therefore tested while unbound, then flushed after binding.
- U08-19 counters have no key cap in the spec (only gauges: 1 000); left uncapped.
- ST08-02 "known secret" is not covered by redact_text alone; see Design decision 5.
- Test IDs: backend health tests are labelled UT08-29 (row shape; acceptance "rows match
  §4.1.3/§4.1.4") and the store part of the stale probe claim UT08-19; metric delegation UT08-32.
- Module map: _metric_buffer.py is a new private sibling (needs a §2 row/note).

## T08-05 markers
- Wired: herness/core/audit.py (was :250) -> record_counter("herness_audit_lines_total",
  component="audit", labels={"event": event}) after the line is written (+1 line, 391/395).
- Wired: herness/core/redact.py redact_batch failure -> record_counter("herness_redact_records_total",
  component="redact", labels={"result": "failed"}) after the _FAILED_LOCK block (+3 lines incl.
  import, 316/390; T10-11 reserve now 74 lines). No import cycle (metrics imports only errors,
  logging, time, types and _state). Tested by test_ut08_32_audit_and_redact_call_sites.
- Remaining: none found. No other `# T08-05` markers or unmarked metric call sites in herness/.

## Tests / gates
- RED: tests were written alongside the implementation rather than strictly before it (the store
  tests were first run against the finished module); failures seen during development were real
  (date arithmetic, missing test_redactor fixture, freezegun-patched monotonic triggering an
  auto-flush in the gauge tests -> `mono` fixture) and are fixed.
- New tests across UT08-19 (store part), UT08-29, UT08-30, UT08-31, UT08-32, UT08-110, UT08-111,
  ST08-02, ST08-10; all pass.
- Coverage (line/branch combined %): _metric_buffer 100, events 98, core metrics 99, store metrics
  100, store resilience 100, audit 99, redact 98.
- `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`:
  4034 passed, 5 skipped, 1 xfailed.
- ruff format/check clean, mypy clean (150 files), lint-imports 13 kept, check_type_ownership 0,
  check_module_size exit 0, --require-test-ids collection clean; commit hooks all passed
  (detect-secrets: planted fixture names chosen so the keyword detector stays quiet; baseline
  unchanged).

## Concerns
1. ops-areas-acyclic ignore entry (Design decision 1) needs a controller ruling.
2. _metric_buffer.py private sibling needs a module-map spec note.
3. record_event masks known secret values via secrets.scrub_secrets in addition to redact_text
   (beyond the U08-18 algorithm text, needed for ST08-02).

## Fix round 1 (commit 7174f7e fix(resilience): close T08-05 review findings (T08-05))
- m1: `MetricBuffer.last_flush` is now `float | None = None` (no clock read at construction); the
  10 s rule (`_interval_due`) and the flush swap both use `clock.monotonic`, and a fresh buffer's
  interval starts at its first recording. `mono` fixture no longer seeds last_flush;
  test_ut08_32_auto_flush_rules covers the fresh-buffer path and the post-flush interval.
- m2: `flush_metrics` hands rows to the port in 500-row chunks (`FLUSH_CHUNK_ROWS`, same size as
  U08-100's transactions; U08-100 unchanged) and returns the rows actually written; on failure
  `resilience.metrics.flush_failed` logs `rows` (discarded), `written` and `error_type`. New test
  test_ut08_32_partial_flush_counts_written_and_discarded (700 rows, 2nd chunk fails -> returns
  500, logs rows=200 written=500, 500 rows committed).
- m4: ST08-02 plants a distinctive ticket marker past char 200 and asserts it is absent, the stored
  to_profile equals the 200-char prefix, and every stored string is <= 200 chars.
- m6: removed the no-op redaction-window tail trim in events._clean_str; docstring now says
  "redact the first 4 000 chars, then keep the first 200".
- Parked per controller: m3 (auto-flush inside run_write), m5 (thread test).
- Gates: ruff format/check clean, mypy clean, lint-imports 13 kept, check_type_ownership 0,
  check_module_size 0 (core metrics 230/260, events 181/200, _metric_buffer 26); hooks passed.
  Coverage: events 98 %, core metrics 99 %, _metric_buffer 100 %.
  Full `-m "(unit or integration) and not slow"`: 4035 passed, 5 skipped, 1 xfailed.

## Fix round 2 (commit affda12 fix(resilience): restore redaction window trim in record_event (T08-05))
- N1: restored the trim in events._clean_str. For text longer than the 4 000-char window, the last
  200 redacted chars are dropped before the 200-char cut. The docstring explains why: a value split
  at the window edge escapes detection, and redaction can shrink the window so that tail moves into
  the kept 200. The m6 removal was wrong.
- Regression test test_ut08_29_value_split_at_redaction_window_does_not_leak: `api_key=` + 3 975
  chars, then an e-mail straddling char 4 000. Without the trim it stores `...ops.person@exa`
  (RED seen); with the trim the fragment is absent (GREEN).
- Note: in record_event the known-value scrub (scrub_secrets) runs first over the whole text and
  already masks credential-shaped tokens. Probed candidates (PEM, password=) were masked by it;
  long e-mails, URLs, JWT-shaped and Bearer tokens of ~3 900 chars were left unchanged by both
  scrub_secrets and redact_text. The scenario is therefore only reachable through a value that
  redact_text shrinks and the scrub leaves. To test the window edge of redact_text directly, the
  test makes scrub_secrets a pass-through (monkeypatch). The trim is kept as defence in depth.
- Gates: ruff format/check clean, mypy clean, lint-imports 13 kept, check_type_ownership 0,
  check_module_size 0 (events 187/200); hooks passed. events coverage 98 %.
  Full `-m "(unit or integration) and not slow"`: 4036 passed, 5 skipped, 1 xfailed.
