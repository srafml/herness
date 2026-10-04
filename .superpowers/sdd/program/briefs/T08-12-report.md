# T08-12 report — Queue API and handlers

Status: DONE — final commit 3962a48 feat(jobs): add queue API and handler registry (T08-12)
Worktree: D:\herness\.claude\worktrees\agent-a4ccececa0f9d384c (branch worktree-agent-a4ccececa0f9d384c, base 88a7f75)
Checkpoints: 6f476c9 wip(T08-12): queue API, handler registry and unit tests

## Implemented
- herness/core/jobs/queue.py (359/390): DEFAULT_PRIORITY (read-only mapping, verbatim values), MANUAL_PRIORITY=80,
  GPU_SLOT_KINDS=frozenset({"build_pipeline"}), default_idem_key, validate_payload, submit, enqueue, claim (+ private
  _claim(min_priority, priority_exempt_kinds) for the supervisor), cancel, retry, get, list_jobs, worker_alive.
- herness/core/jobs/handlers.py (78/120): register_handler, resolve_handler, run_handler (type alias Handler).
- No SQL in core; everything through require_jobs_backend(). Metrics recorded after the backend call returns (never
  inside run_write). claim passes slot (from owner suffix gpu|cpuN|cli) and gpu_slot_kinds=sorted(GPU_SLOT_KINDS), and
  exclusive_kinds / lease_s from cfg.resilience.resilience.jobs.

## Tests (ID -> functions)
- UT08-51: test_ut08_51_idem_key_ignores_key_order
- PT08-06: test_pt08_06_permutations_give_one_key, test_pt08_06_distinct_payloads_give_distinct_keys (hypothesis)
- UT08-55: test_ut08_55_valid_payload_returns_canonical_bytes, test_ut08_55_rejected_payloads (12 cases: 70 KB, known
  secret, `Authorization: Bearer synthetic_token_x`, depth 9, key `a b`, list depth, non-str key, datetime, Decimal, set,
  bytes, NaN; the message never holds the value), test_ut08_55_short_known_values_are_ignored_and_non_object_refused,
  test_ut08_55_unscannable_or_unencodable_payload
- ST08-03: test_st08_03_secret_or_oversize_payload_stores_no_row (secret value, password=synthetic_pw_123, 65 537 bytes;
  no row; jobs.job.rejected WARNING without the value)
- UT08-52: test_ut08_52_enqueue_dedupes_until_finished
- UT08-53 (core half, carry-over from T08-11): test_ut08_53_dedupe_logs_debug_and_counts_nothing,
  test_ut08_53_sched_check_blocks_a_second_fire
- UT08-106: test_ut08_106_default_priorities_and_explicit_value, test_ut08_106_enqueue_keeps_explicit_fields,
  test_ut08_106_invalid_spec_names_the_field
- UT08-56: test_ut08_56_claims_in_priority_order_and_skips_future, test_ut08_56_claim_logs_claimed,
  test_ut08_56_invalid_claim_arguments
- UT08-57: test_ut08_57_exclusive_kind_waits_for_running_build
- UT08-107: test_ut08_107_slot_rule_for_none_class_jobs, test_ut08_107_cli_owner_claims_any_kind_by_id
- UT08-60: test_ut08_60_cancel_results
- UT08-61: test_ut08_61_retry_failed_and_refusals
- ST08-11: test_st08_11_cancel_and_retry_are_logged_with_job_id
- UT08-63: test_ut08_63_get_and_list_jobs, test_ut08_63_invalid_list_filters
- UT08-65 (core half, carry-over): test_ut08_65_worker_alive_uses_three_heartbeats (89 s alive / 91 s not; stopped
  ignored), test_ut08_65_unbound_backend_is_config_error
- UT08-64: test_ut08_64_register_resolve_and_duplicate, test_ut08_64_register_refuses_unknown_kind_and_non_callable,
  test_ut08_64_run_handler_wraps_errors, test_ut08_64_validation_error_is_schema_violation,
  test_ut08_64_base_exception_propagates
- BT08-02: test_bt08_02_enqueue_p95_under_10_ms (measured p95 0.209 ms)
- BT08-03: test_bt08_03_claim_p95_under_20_ms (measured p95 4.089 ms, 1000/1000 claimed, 10 000 seeded)

Files: tests/unit/core/jobs/test_jobs_queue.py (280), test_jobs_queue_claim.py (292), test_jobs_handlers.py (110),
_queue_env.py (48, shared `jobs_db` fixture), tests/bench/test_jobs_queue_bench.py (92, [integration, slow]).

## Evidence
- GREEN: `PYTHONUTF8=1 pytest tests/unit/store/ops tests/unit/core/jobs tests/integration/jobs -q -p no:logging` -> 753 passed.
  `pytest tests/unit/core/jobs tests/bench/test_jobs_queue_bench.py --require-test-ids -m "unit or integration"` -> 236 passed.
- Coverage (card tests): queue.py 100 % line / 100 % branch; handlers.py 100 % / 100 %.
- Gates: ruff format/check clean; mypy herness/core/jobs 0 issues; lint-imports 13 kept 0 broken; check_module_size 0;
  check_type_ownership 0; detect-secrets hook clean on the new files (no baseline change needed); pre-commit hooks
  passed on the wip commit (no SKIP).
- RED: implementation and tests were written in the same step (critical-path speed), so there is no RED log for the
  absent-module state. The first run surfaced one mismatch (UT08-107 CPU-owner allowed classes), a test-reading error
  fixed in the test (the row's "(allowed [reasoning, none])" belongs to the GPU owner).

## Decisions / deviations (no spec change needed)
- validate_payload walks the value first (JSON types, key regex, depth <= 8 with the payload object at depth 1), then
  canonical_json, because herness.core.ids.canonical_json silently converts datetime/Decimal/Path/Enum while the spec
  requires those to be errors. Reasons: "not JSON: <type>", "invalid key name", "nested deeper than 8",
  "non-finite number", "larger than 65536 bytes", "contains a known secret value", "contains a credential or URL token",
  "string could not be scanned" (RedactionFailed), "not canonical JSON", "payload is not an object". Raised `from None`.
- submit logs `jobs.job.rejected` (WARNING, kind + reason; §8.1 row U08-45) on a validation failure and re-raises.
- enqueue: pydantic ValidationError -> SchemaViolation("invalid job spec field: <top-level field>") (loc[0] only, so no
  payload key or value leaks).
- claim: an invalid job_id (not job_<ulid>) -> ConfigError (the spec constrains it; its Errors row names owner/classes
  only). allowed_classes given as a bare str is refused. wait_s clamped at >= 0.
- cancel/retry/get cut caller-supplied job ids to 64 chars in logs and in JobStateError.job_id.
- retry logs retry_requested with the backend result BEFORE raising JobStateError (attribution of refused retries, TH08-11).
- list_jobs: limit must be a real int (bool refused), 1..1000; status/kind validated against the JobStatus/JobKind literals.
- worker_alive: strict `heartbeat_at > now - 3*heartbeat_s`, statuses starting/running/draining.
- register_handler also refuses unknown kinds and non-callables (ConfigError); resolve_handler cuts kind to 40 chars.
- run_handler catches ValidationError before the generic Exception branch; the crash log carries only job_id, kind,
  error_type.
- Tests bind SqliteJobsBackend via cast("JobsBackend", ...) (task methods land with the tasks card); config via
  write_full_config + init_config; fixed-key test redactor (pattern of tests/unit/core/resilience/conftest.py).
- The fixture is shared through tests/unit/core/jobs/_queue_env.py (imported with `# noqa: F401`; tests use
  @pytest.mark.usefixtures) instead of a new tests/unit/core/jobs/conftest.py, to avoid an add/add conflict with the
  parallel group w15-s08b, which also works in herness/core/jobs.
- No concurrency test in core: claim atomicity is the backend's BEGIN IMMEDIATE (IT08-02 covers it); tests/support has
  no claim race helper (only breaker_race and gpu_lock_race).

## Rulings needed
- None new. Followed: no edit of herness/core/jobs/__init__.py or ports.py.

## Carry-overs
- U08-98 export-map card: add to herness/core/jobs/__init__.py `_EXPORTS` + TYPE_CHECKING block:
  queue: DEFAULT_PRIORITY, MANUAL_PRIORITY, GPU_SLOT_KINDS, default_idem_key, validate_payload, submit, enqueue, claim,
  cancel, retry, get, list_jobs, worker_alive; handlers: register_handler, resolve_handler, run_handler.
- Supervisor card: use queue._claim(owner=..., allowed_classes=..., job_id=None, min_priority=...,
  priority_exempt_kinds=...) for the chat-window rule.
- ST08-04 requeue half stays with U08-50 (not this card).
- Once U08-98 bind_core_backends exists, _queue_env.jobs_db can switch to it (drop the cast).

## Spec notes
- None required (all behaviour is within the U08-43..U08-56 text; the jobs.job.rejected log comes from §8.1).
