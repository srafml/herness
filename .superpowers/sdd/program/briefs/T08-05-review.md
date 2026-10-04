# T08-05 review — Resilience store backend, events and metrics

Reviewed commit 628e719 on worktree-agent-a6d12d170b7cfb4da (base cdadffe). Read-only review.

### Spec Compliance
- ✅ Spec compliant (controller rulings applied: exports deferred, U08-98 not in card, unbound flush returns 0 and keeps buffer, ops-areas-acyclic ignore, _metric_buffer.py private sibling, scrub_secrets before redact_text, audit/redact markers wired)

Per unit:
- U08-18 record_event, EVENT_KINDS, DETAIL_FIELDS ✅ — 21 kinds exactly (events.py:51-89); allowlists, levels, §8.1 event names and the 4 trace types match the table and §8.1; unknown kind/component → ConfigError; unbound → ConfigError via require_ops_backend; datetime → ts text; nested dicts / non-scalars / non-finite floats / lists holding non-scalars dropped with a DEBUG count only; lists cut to 20; strings scrubbed, redacted, cut to 200 (fail closed); evt_ + ULID, ts = now; HernessError from insert_event → WARNING resilience.event.dropped(kind, error_type), never raised; log line at table level with kind, target, IDs, filtered detail; tracer fan-out only for trace kinds.
- U08-19 record_counter ✅ — name regex, unit set, component == 2nd segment, ≤ 6 labels, key/value regexes, finite ≥ 0, ConfigError naming the metric; aggregate per (name, sorted labels, component) under ProcessState.lock; 10 s flush rule. (lru_cache instead of a set: equivalent, errors not cached.)
- U08-20 record_histogram ✅ — cap 10 000 with dropped, flush trigger at 1 000, same validation.
- U08-21 timed ✅ — perf_counter elapsed recorded in finally (normal and exception); seconds unit enforced on entry.
- U08-22 flush_metrics ✅ — swap under lock; counter/gauge/histogram MetricSamples; counters/histograms ts = now, gauges own ts; HernessError (and pydantic ValidationError) → WARNING resilience.metrics.flush_failed(rows, error_type), rows discarded; resilience.metrics.dropped(count) logged at flush; returns rows written.
- U08-103 record_gauge ✅ — units exclude total, negative allowed, NaN refused, last write wins with its own time, 1 000-key cap (existing keys still update), new key beyond cap counted in dropped.
- U08-94 SqliteResilienceBackend, purge_events ✅ — reads via read_one/read_all, writes via run_write; health_apply read-modify-write in one BEGIN IMMEDIATE with ON CONFLICT(source) DO UPDATE; claim-probe SQL verbatim (resilience.py:37-41); health_reset SQL as specified with RETURNING; IN(...) lists built from length only; insert_event plain insert; count/event_counts/latest_event on (kind, ts); insert_metric_samples delegates to U08-100; purge_events op resilience_event_purge, re-exported from herness.store.ops.
- U08-100 record_metric_samples, purge_metric_samples ✅ — more than 100 000 → ConfigError("too many metric samples"); empty → 0; labels canonical JSON over 1 024 bytes → ConfigError naming the metric; conn path is a single executemany in the caller transaction; else 500-row chunks each in run_write(op="metric_samples"); purge op metric_purge; re-exported.
- TH08-02 ✅ (allowlist, scalar-only, scrub + redact + 200 cut, label regex, dropped key names never logged). TH08-10 ✅ (10 000 histogram cap, 1 000 gauge keys, 100 000 rows per call).
- Tests ✅ — UT08-19 (store part), UT08-29, UT08-30, UT08-31, UT08-32, UT08-110, UT08-111, ST08-02 (event/log part), ST08-10 (metric part) present; names carry IDs, docstrings start with the ID, pytestmark = pytest.mark.unit in every file; --require-test-ids collection clean (51 tests).

⚠️ Cannot verify / spec gaps (not findings):
- U08-19 counters have no key cap in the spec; counter keys are unbounded in memory (bounded in practice by the label regex and low cardinality). Spec-level gap for TH08-10.
- ProcessState.lock is a non-reentrant threading.Lock; future code that holds it and then calls audit(), Redactor.redact_batch or a record_* would deadlock. No such caller exists today (checked herness/core/resilience and herness/core/jobs).
- The spec-mandated inline auto-flush runs on the calling thread inside audit() / redact_batch(); under contention the sqlite_write retry policy can add latency to those paths (best effort, never raises).

### Verification run (this review)
- Targeted pytest (5 new test files): 51 passed. Coverage (line+branch): _metric_buffer 100 %, events 98 % (miss :102), core metrics 99 % (miss :164), store metrics 100 %, store resilience 100 %; all at or above 90 / 85.
- pytest tests/unit/core -k "audit or redact or state or UT02_28": 243 passed.
- ruff check clean; ruff format --check clean (348 files); mypy clean (150 files); lint-imports 13 kept, 0 broken; check_type_ownership exit 0; check_module_size exit 0.
- Fresh-interpreter import of herness.core.redact, .audit, .secrets, .resilience, .resilience.events, .resilience.metrics, .resilience._state, herness.store.ops, .ops.resilience, .ops.metrics: all succeed, so no import cycle from the audit/redact wiring.
- A metric failure cannot break audit/redact: flush catches HernessError and ValidationError; run_write maps sqlite errors and nesting to HernessError subclasses; connection() is documented as ConfigError/StoreBusy/SchemaViolation; audit event labels all match the label regex (event validated by _validate before record_counter).

### Strengths
- Tight, readable modules well under budget (events 184/200, core metrics 217/260, store resilience 228/320, store metrics 82/120).
- Store tests assert exact §4.1.3/§4.1.4/§4.1.6 row shapes against the real migrated DDL, 3-transaction chunking via a run_write spy, and commit/rollback of the conn path.
- Fail-closed string cleaning and count-only DEBUG logging of dropped detail keys (no key names or values leak).
- ST08-02 checks logs captured before the log scrubber, so it proves the detail was cleaned at source.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/core/resilience/metrics.py:92 with herness/core/resilience/_metric_buffer.py:211: last_flush defaults to time.monotonic() but _interval_due and the flush swap use clock.monotonic(). Identical in production; under a patched clock with a different base the 10 s rule misfires (never or always due) until the first explicit flush. Tests work around it with the mono fixture resetting last_flush.
2. herness/core/resilience/metrics.py:213-217 with herness/store/ops/metrics.py:290-297: when a later 500-row chunk fails, earlier chunks are already committed but flush_metrics logs flush_failed with the full row count and returns 0; the count is inaccurate for buffers over 500 rows.
3. herness/core/resilience/metrics.py:113-114, 138-139, 163-164: an auto-flush triggered from inside a run_write callback on the same thread (for example redact_batch or audit called within a transaction) hits the nested-run_write ConfigError and discards the whole process buffer (logged as flush_failed). Best effort by spec, but consider skipping the auto-flush while the thread connection is in a transaction.
4. tests/security/test_st08_events_metrics.py:192: the TICKET[:400] check is satisfied trivially by the 200-char cut (the 200-char ticket prefix is stored by design). Assert the cut explicitly (stored to_profile length at most 200) instead of a tautological check.
5. tests/unit/core/resilience/test_resilience_metrics.py: no multi-thread test of concurrent record_* / flush_metrics (swap under lock); thread safety is verified by inspection only.
6. herness/core/resilience/events.py:109-110: for text longer than the 4 000-char redaction window, dropping the last 200 redacted chars is always followed by the 200-char head cut, so it is effectively a no-op; harmless but the docstring overstates it.

### Assessment
**Task quality:** Approved
**Reasoning:** All units (U08-18..22, U08-94, U08-100, U08-103) match the spec, including the exact claim-probe/reset SQL, the 21 event kinds, caps, flush semantics and never-raise behaviour; tests cover every card ID with real assertions, coverage is at least 98 % on changed modules and all gates pass. Remaining items are minor robustness and test-strength polish.

## Re-review round 1 (commit 7174f7e; scope m1, m2, m4, m6; m3 and m5 parked by controller)

- m1 ✅ closed. MetricBuffer.last_flush is now float | None = None (no clock read at construction); _interval_due and the flush swap both use clock.monotonic; a fresh buffer starts its interval at the first recording. _interval_due mutates the buffer, but every caller holds ProcessState.lock (record_counter/histogram/gauge), so thread safety is unchanged. With the port unbound, last_flush is set once and flush_metrics returns early, same behaviour as before. Test covers the fresh-buffer path and the post-flush interval.
- m2 ✅ closed. flush_metrics hands the port 500-row chunks (FLUSH_CHUNK_ROWS) and returns the rows actually written; on HernessError/ValidationError it logs flush_failed with rows (discarded), written and error_type and discards the rest. U08-100 (herness/store/ops/metrics.py) is untouched, so its contract is unchanged; U08-22 semantics (swap under lock, gauge ts, best effort, never raises, returns rows written) hold. The added written field extends the §8.1 fields (rows kept); acceptable. New test (700 rows, 2nd chunk fails → returns 500, logs rows=200 written=500, 500 rows committed) asserts the behaviour.
- m4 ✅ closed. ST08-02 now plants a distinctive marker past char 200 and asserts it is absent, that the stored to_profile equals the 200-char prefix, and that every stored string is at most 200 chars.
- m6 ✅ closed as asked, but see new finding N1: the original review finding was partly wrong.

Checks run: targeted pytest (store resilience/metrics, tests/unit/core/resilience, ST08) 260 passed; coverage events 98 % (miss :101), core metrics 99 % (miss :169), _metric_buffer 100 %; ruff check clean; ruff format --check clean; mypy clean (150 files); check_module_size exit 0.

New findings:
- N1 (Minor) herness/core/resilience/events.py:96-111 (_clean_str). The removed tail trim was not a pure no-op, contrary to review item m6: when redact_text shrinks the 4 000-char window by more than 3 800 chars (for example a multi-KB token or blob replaced by a short placeholder), text from near the window boundary moves into the first 200 kept chars, and a secret split at char 4 000 (unmatched by the pattern because it is cut) could then be stored. The old trim (drop the last 200 redacted chars for over-window input) guarded exactly that edge. Rare, but a TH08-02 regression introduced on reviewer advice; suggested fix: restore the trim, or, for input longer than the window, keep clean[:200] only when len(clean) > 200 + 200 (else drop the tail as before). Not blocking.

Verdict round 1: **Approved** (N1 Minor; controller may choose to restore the trim in a follow-up).

## Re-review round 2 (commit affda12; scope N1 only)

- N1 ✅ closed. events._clean_str again drops the last 200 redacted chars for input longer than the 4 000-char window before the 200-char cut; this is line-for-line the classify.py approach (classify.py:84-90: redact the window, trim for over-window input, then cut). Docstring now explains the reason correctly.
- Regression test test_ut08_29_value_split_at_redaction_window_does_not_leak: independently confirmed it is a real RED/GREEN test. With the test redactor, redact_text on the 4 000-char window gives 33 chars ("api_key=[SECRET] x ops.person@exa"); without the trim the stored value would contain the e-mail fragment, with the trim it is empty.
- Stubbing scrub_secrets as a pass-through is acceptable: it is a separate, earlier layer, and the test targets the redact_text window edge specifically (a token only redact_text detects reaches the same state). The docstring states the stub and why. The end-to-end path with the real scrub stays covered by ST08-02.
- No regressions: targeted pytest (store resilience/metrics, tests/unit/core/resilience, ST08) 261 passed; events coverage 98 % (miss :105); ruff check clean; ruff format --check clean; mypy clean (150 files); check_module_size exit 0.

Verdict round 2: **Approved**. No new findings.
