# T01-11 report: Job handlers, factory and CLI payloads

Status: DONE_WITH_CONCERNS (concerns are carry-overs/spec notes, no failing gate)
Worktree: D:\herness\.claude\worktrees\agent-adb0e02aa26892e5e (base de08abc)
Commits: d231dc4 wip (handlers+unit tests), 0f67202 wip (factory UT01-94 + IT01), 1a8cce6 final (empty marker commit: all code landed in the wip commits)
(`feat(connectors): add sync/reconcile job handlers, factory and CLI payloads (T01-11)`).

## Implemented
- herness/connectors/jobs.py (320/320): SyncPayload / ReconcilePayload (strict, extra forbid,
  frozen, hide_input_in_errors; names ^[a-z][a-z0-9_]{0,63}$, <=64 entities, ISO-text dates),
  handle_sync (U01-51), handle_reconcile (U01-52), register_job_handlers (U01-53),
  build_sync_payload (U01-54). Payload validated before any connector is built; invalid →
  ConfigError("invalid sync|reconcile payload") with no cause/input. Per-source runner gets
  `connector_factory=partial(build_connector, name, cfg)` (no late-binding lambda bug),
  `progress` = lock-guarded wrapper of ctx.heartbeat (thread-safe for backfill slice threads),
  `should_stop=ctx.should_yield`. CircuitOpen → key recorded, `connectors.sync.skipped_open_circuit`
  (source, stream, retry_at), rest of the source skipped; other HernessError → recorded,
  `connectors.sync.failed` (source, entity, stream, mode, error_class); runner.skipped_open keys
  merged; runner.stopped / should_yield → JobOutcome(yield, partial result). Result dict per
  spec step 6 (`failed` entries carry error_class only). Step 7 raises first FatalError else
  first failure. Reconcile skips delta-mode files entities (`connectors.reconcile.skipped`,
  reason delta_mode) and refuses `monitoring` up front (ConfigError). No job_id added to logs.
- herness/connectors/factory.py (59/90): build_connector (U01-55) — disabled → ConfigError,
  class only via registry.get("connector", name); files inbox_root (absolute or
  paths.data.parent / inbox, resolved), jira custom_field_ids (non-empty, fixed order),
  monitoring adapters via registry.get("monitoring_adapter", tool)(settings, clock=clock).
- tests/support/sync_env.py (55): lake_small stand-in (config tree with files source, 3-row
  teams CSV inbox back-dated past settle).

## Tests (all green)
- tests/unit/connectors/test_sync_jobs.py: UT01-54 (open breaker done/skipped/second ran, rest of
  source skipped, tool keys merged, clean run, runner wiring, thread-safe progress from 8
  threads, backfill ranges, --full D-6, invalid payload matrix, disabled/unknown entity,
  register idempotent + after process-state reset), UT01-55 (fatal first, retryable first,
  reconcile valve, delta skip, reconcile payload, monitoring refused), UT01-56 (yield before
  entity 2, runner.stopped, reconcile yield).
- tests/unit/connectors/test_sync_payload.py: UT01-57 option matrix + conflict matrix.
- tests/unit/connectors/test_connector_factory.py: UT01-94 (all registered connectors from a
  loaded synth config, Connector/SupportsKeyListing, respx no calls, kwargs branches).
- tests/integration/connectors/test_sync_jobs_flow.py: IT01-01, IT01-08, IT01-09 (real ops
  store, config, registry, runner, files connector, LakeWriter).
- `pytest tests/unit/connectors tests/integration/connectors -q`: 541 passed, 4 skipped (host
  skips: symlink privilege, DuckDB excel).
- Acceptance: `-k "UT01_54 or UT01_55 or UT01_56 or UT01_57 or UT01_94"` 65 passed;
  `-m integration -k "IT01_01 or IT01_08 or IT01_09"` 3 passed; --require-test-ids clean.
- Coverage: jobs.py 100% line / 100% branch; factory.py 100% / 100%.
- Gates: ruff format/check clean, mypy strict 0, lint-imports 13 kept, check_module_size 0,
  check_type_ownership 0; commit hooks (incl. pytest-unit) pass.
- RED evidence: first run of test_jobs/test_jobs_payload → ModuleNotFoundError
  herness.connectors.jobs (collection errors), then green after implementation.

## Deviations / decisions
1. register_job_handlers flag is keyed to the ProcessState object (module-level holder
   `_Registered.state`) instead of a bare bool: the handler table lives in ProcessState, and the
   opt-in reset_process_state fixture replaces that object, so a new state automatically counts
   as "not registered" — no conftest edit needed and no stale-flag state is possible.
2. build_sync_payload also rejects `--reconcile` with `--from/--to` and `--full` with
   `--from/--to` (spec's step order would silently ignore them), and validates the final payload
   against the handler model (bad source/entity names → ConfigError naming the option; TH01-07).
   Empty `entities` is serialised as null; `--full` idem key follows the spec text literally:
   `sync:<source|all>:backfill:None:None`.
3. Payload models are named SyncPayload / ReconcilePayload (not exported in __all__).
4. Test modules renamed test_sync_jobs.py / test_sync_payload.py (pytest basename clash with
   tests/unit/core/types/test_jobs.py).

## Spec notes / carry-overs
- _BUILTINS row ("connector","files") → "herness.connectors.files:FilesConnector" is missing
  from herness/core/registry.py (spec 01 line 99; T10-04 U10-26 says owner cards add rows).
  In production build_connector only resolves `files` if something imported
  herness.connectors.files. Not added here (outside card Files). Also: tests/unit/core/
  test_registry.py's autouse fixture clears `_BUILTINS` entirely after each test (session-wide
  pollution), so tests must register connectors explicitly — mine do.
- `herness sync` CLI acceptance ("herness sync files on the synth profile") → T09-23.
- IT01-08 uses a test-local stand-in for T08-22 run_inline (enqueue → claim → resolve_handler →
  run_handler → finish_done); swap in run_inline when T08-22 lands.
- IT01-01 uses tests/support/sync_env.py instead of T11-16 lake_small.
- UT01-94: only files is real; jira/monitoring(+prometheus adapter)/servicenow are fake
  registered classes covering the factory kwargs branches; re-run with the real classes in
  T01-17 / T01-19.
- Spec gap: `sync` with mode backfill and no source (e.g. `herness sync --full`) includes the
  files source, whose runner refuses backfill (ConfigError) → the job fails after the other
  sources ran. Spec does not say to skip files; left as specified, flag for a ruling.
- JobOutcome result is capped at 1 MiB; a sync over very many entities/files could exceed it
  (run_handler turns that into SchemaViolation). Not observed; noted.

## Fix round 1 (review Approved, 0 Critical / 0 Important; minors fixed)
1. Ruling on concern 1 — DEVIATION OUTSIDE THE CARD FILES LIST: herness/core/registry.py
   `_BUILTINS` gains `("connector", "files"): "herness.connectors.files:FilesConnector"`
   (143/160 lines). Tests (test_connector_factory.py, UT01-94):
   `test_ut01_94_files_resolves_through_the_builtin_table` (reads the shipped table from a
   fresh load of registry.py because test_registry's autouse fixture clears the live
   `_BUILTINS`; reset registry, no explicit register → get/build_connector give
   FilesConnector) and `test_ut01_94_build_connector_files_without_prior_import` (fresh
   interpreter: asserts herness.connectors.files is not in sys.modules, loads a synth config,
   build_connector("files") → FilesConnector). No existing test enumerates `_BUILTINS`;
   test_registry, test_config_validate (C03), ST10 registry, config sections/templates green.
2. m2: the U01-53 registration tests moved out of the unit file into
   tests/integration/connectors/test_sync_jobs_flow.py as IT01-08 (U01-53 lists IT01-08):
   `test_it01_08_register_job_handlers_is_idempotent` now spies on `register_handler` and
   asserts the second call does not reach it (deleting the flag fails the test);
   `test_it01_08_registration_follows_a_reset_process_state`.
3. m3: factory.py keeps the mandated `from herness.core import time as clock` (ICN003 bans
   from-imports of herness.core.time; global constraints fix the alias) and the spec's `clock`
   parameter; the module is now referenced only at module level (`_DEFAULT_CLOCK = clock.now`,
   used as the default), so inside both functions `clock` always means the injected callable;
   `_kwargs` renamed its `now` parameter to `clock` for consistency (factory.py 62/90).
4. m5: FakeConfig docstring corrected (only `sources` is read).
5. m1: left as is (jobs.py unchanged, 320/320).
Parked per instruction: m4, m6.
Round-1 tests: `pytest tests/unit/connectors tests/integration/connectors
tests/unit/core/test_registry.py -q` 551 passed, 4 skipped; acceptance selections 65 unit + 5
integration passed; coverage jobs.py/factory.py 100% line and branch; ruff, mypy, lint-imports
(13 kept), check_module_size, check_type_ownership clean.

Round-1 commit: 908ebb3 fix(connectors): T01-11 review round 1 (registry files builtin, U01-53 test IDs)
