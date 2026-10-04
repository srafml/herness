# T08-12 review — Queue API and handlers (head 3962a48, base 88a7f75)

**Verdict: Approved** (0 Critical, 0 Important, 4 Minor)

### Spec Compliance
- ✅ Spec compliant. Units U08-43..U08-48 and U08-51..U08-56 are implemented as written. The Tests-row IDs are covered as follows:
  - ✅ UT08-51 test_ut08_51_idem_key_ignores_key_order
  - ✅ PT08-06 two hypothesis properties (permutations; distinct iff canonical differs)
  - ✅ UT08-55 12 rejection cases (70 KB, known secret, `Authorization: Bearer synthetic_token_x`, depth 9, key `a b`, non-JSON types, NaN); every case asserts the value is absent from the message
  - ✅ ST08-03 secret / `password=synthetic_pw_123` / 65 537 bytes → SchemaViolation, `COUNT(*) FROM job == 0`, `jobs.job.rejected` WARNING without the value
  - ✅ UT08-52 dedupe until finished (plus counter == 2)
  - ✅ UT08-56 order 90/50/10, future job untouched, lease = claim time + 300 s, other class ignored, invalid owner/classes/job_id → ConfigError
  - ✅ UT08-57 `distill` blocked while `build_pipeline` runs, `sync` claimed
  - ✅ UT08-107 CPU owner takes only `sync`, GPU owner takes review(70) then build(60) and never `sync`, CLI owner claims any job by id
  - ✅ UT08-60 canceled / cancel_requested / not_active ×2; lease kept on the running job
  - ✅ UT08-61 failed → queued with attempts 0, last_error kept; not_failed, conflict and missing → JobStateError
  - ✅ UT08-63 get("job_x") → JobStateError; limit 0/1001/True, bad status and bad kind → ConfigError; rows are JobRow, newest first
  - ✅ UT08-64 duplicate → ConfigError; same object is a no-op; ValueError → FatalError "handler raised ValueError" plus crash log without the message; None → FatalError; SourceUnavailable returned; ValidationError → SchemaViolation; BaseException propagates
  - ✅ UT08-106 every kind with priority=None gets DEFAULT_PRIORITY; explicit 10 kept; invalid field is named
  - ✅ ST08-11 cancel_requested and retry_requested logged with job_id and result
  - ✅ BT08-02 re-run p95 about 0.2 ms (< 10 ms)
  - ✅ BT08-03 re-run p95 4.53 ms (< 20 ms), 1000/1000 claimed
- Carry-overs from T08-11 are all closed:
  - slot and gpu_slot_kinds passed as keywords (queue.py _claim)
  - retry results mapped to the spec's JobStateError messages
  - list_jobs filters and limit validated
  - UT08-53 core half (DEBUG on dedupe, no counter; sched_check blocks a second fire)
  - UT08-65 core half (89 s alive, 91 s not; stopped workers ignored)
- ⚠️ Cannot verify from diff / out of scope: claim atomicity across processes rests on the backend's BEGIN IMMEDIATE (IT08-02, T08-11, strengthened in the T08-11 fix round). Between validation and `claim_job`, the core `_claim` holds no state that a race could exploit. The only shared state it adds is the handler registry, which uses `state.lock` with `setdefault`. A core-level race test is not warranted. `__init__` exports stay a carry-over for U08-98.

### Focus checks
1. Handler lookup: `resolve_handler(kind)` reads only `process_state().handlers`. No payload field reaches the lookup (handlers.py). ✅
2. `submit` calls validate_payload first (queue.py submit), before any id, idem key or backend call.
   - Checks: known_values (len ≥ 8), then redactor.scan for CREDENTIAL/URL_TOKEN.
   - Errors: `_reject` raises `from None`, and the reason text never contains the value.
   - Nothing is stored on rejection (ST08-03 asserts the row count).
   - ✅
3. Log names and fields match §8.1 rows 2261-2269: enqueued, rejected, claimed, cancel_requested, retry_requested and handler.crashed. The crash log carries only job_id, kind and error_type. Nothing is written before validation. ✅
4. The new code has no SQL in core. Every call goes through `require_jobs_backend()`. Metrics are recorded after the backend call returns. The only SQL outside store/ops is the bench seed, a test-only `run_write` insert, which is acceptable. ✅
5. Cancel and retry transitions and messages match U08-51/U08-52 verbatim; the state changes themselves are in the T08-11 backend. ✅
6. Class exclusivity and the slot rule are passed from config and GPU_SLOT_KINDS. Mutation probes (all reverted; `git status` clean afterwards) were each caught:
   - `exclusive_kinds=[]` → UT08-57 red
   - `gpu_slot_kinds=[]` → UT08-107 red
   - known-values check disabled → UT08-55 + ST08-03 red
   - ✅
7. Every test name and docstring carries its ID, and every file sets `pytestmark`. The bench file is `[integration, slow]`, which is the global-constraints benchmark convention; the spec's "benchmark" marker is not registered in pyproject. Thresholds are asserted. ✅
8. Open-detail choices, all accepted:
   - Walk before canonical_json: required, because canonical_json silently converts datetime/Decimal while the spec requires those to be errors.
   - `jobs.job.rejected` log: this is the §8.1 row for U08-45.
   - Invalid claim job_id → ConfigError: consistent with the constraint column.
   - Retry logs before raising: better TH08-11 attribution, and the spec does not forbid it.
   - 64-char id cut in logs and errors: harmless, since valid ids are 30 chars.
   - `jobs_db` fixture with cast: a documented temporary measure until U08-98 and the tasks card.

### Evidence (this review)
- `PYTHONUTF8=1 uv run pytest tests/unit/core/jobs tests/unit/store/ops -q -p no:logging` → 748 passed in 37.7 s.
- `pytest tests/bench/test_jobs_queue_bench.py -m "integration and slow"` → 2 passed (BT08-03 p95 4.53 ms).

### Strengths
- The validation order is deliberate and documented, and errors carry no exception context (`__suppress_context__` is asserted).
- Every refusal path is tested for value leakage.
- Probes show the claim tests really exercise the backend guards.
- Coverage is 100 % line and branch on both modules; the files are well under budget (queue 359/390, handlers 78/120).

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
- M1 herness/core/jobs/queue.py:110 — Payload key names are regex-checked but never scanned for known secret values. An alphanumeric/underscore token of 8 to 64 chars used as a key would pass. The spec says "every string value", so this is compliant. Scanning keys too would be a cheap hardening step (TH08-03).
- M2 tests/unit/core/jobs/test_jobs_queue.py:102 — No URL_TOKEN case (e.g. `https://h/x?token=...`). Only the CREDENTIAL span type is exercised, so a mismatch in the span-type name for URL tokens would go unnoticed. The name matches redact.py:52 today.
- M3 tests/bench/test_jobs_queue_bench.py:86 — BT08-03 claims with a `cli` owner, so the R-43 slot filter (cpu/gpu owner) is not on the benchmarked path. Consider a cpuN or gpu owner, or a mix, for a representative p95.
- M4 herness/core/jobs/queue.py:354 — `worker_alive` filters every worker row in Python rather than in the backend query. This is fine for the tiny `worker` table; noted only.

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit and test ID on the card is implemented and verified. Security and redaction guards are enforced before any write or log, and mutation probes confirm the tests catch regressions in the key guards. The remaining items are optional hardening and test-breadth polish.
