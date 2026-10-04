# T01-11 review (verify agent) — head 1a8cce6, base de08abc

### Spec Compliance
- ✅ U01-51 handle_sync: payload validated first (jobs.py:311-313 `_parse` before `get_config`/`build_connector`); ConfigError("invalid sync payload") `from None`, `hide_input_in_errors` (jobs.py:47,112-117); names = source or `enabled_sources()` (jobs.py:237); per-source runner with `partial(build_connector, name, cfg)`, locked progress, `should_stop=ctx.should_yield` (jobs.py:172-185); yield check before each entity + `runner.stopped` (jobs.py:144-165); CircuitOpen → key + `connectors.sync.skipped_open_circuit` + return (rest of source skipped) (jobs.py:148-152); other HernessError → record + `connectors.sync.failed` (error_class only) + continue (jobs.py:153-157); result dict shape per step 6 (jobs.py:93-102); fatal-first raise then first failure (jobs.py:104-109).
- ✅ U01-52 handle_reconcile: required `source`, extra forbid; files `delta` entities skipped with `connectors.reconcile.skipped` INFO reason `delta_mode` (jobs.py:218-227); monitoring refused up front (jobs.py:245-247).
- ✅ U01-53 register_job_handlers: only `register_handler("sync"/"reconcile", …)` with static kinds; flag keyed to the ProcessState object, so reset_process_state automatically re-arms it (jobs.py:251-265). Consistent with process-state reset (tested).
- ✅ U01-54 build_sync_payload: full option matrix, idem keys `sync:<src|all>`, `sync:…:backfill:<from>:<to>`, `reconcile:<src>`, ISO-string dates, postcondition validated against SyncPayload/ReconcilePayload (jobs.py:268-320). Extra rejections (`--reconcile`/`--full` with `--from/--to`) are sensible tightenings.
- ✅ U01-55 build_connector: disabled → ConfigError; class only via `registry.get("connector", name)` (factory.py:58); files/jira/monitoring kwargs per spec (factory.py:27-43); no importlib; the only getattr is over the constant `_JIRA_FIELDS` tuple (factory.py:37), never payload strings.
- ✅ Sub-controller rulings implemented: IT01-08 test-local run_inline stand-in (enqueue/claim/resolve_handler/run_handler, test_sync_jobs_flow.py:99-137); IT01-01 via tests/support/sync_env.py; core/jobs/__init__.py untouched (jobs.py imports `herness.core.jobs.handlers`/`.ports`).
- ⚠️ Cannot verify from diff / carried: `("connector","files")` _BUILTINS row (ruled, fix round); `--full` with no source including files (parked spec note); `herness sync` CLI acceptance (T09-23); lint-imports/mypy gates taken from the report (imports observed are L0/L1 only: herness.core.*, plus same-package connectors modules).

### Evidence run (worktree, PYTHONUTF8=1)
- `pytest -k "UT01_54 or UT01_55 or UT01_56 or UT01_57 or UT01_94" -q -p no:logging` → 65 passed.
- `pytest -m integration -k "IT01_01 or IT01_08 or IT01_09" -q -p no:logging` → 3 passed.
- Coverage (the four new test modules, --cov-branch): jobs.py 100% (197 stmts, 52 branches), factory.py 100% (34 stmts, 8 branches) — ≥90/85.
- Budgets: jobs.py 320/320, factory.py 59/90.
- Focus 4 (secrets): logs/outcome carry `error_class` only; ConfigError messages contain only pattern-validated names; test asserts raw exception text absent from `connectors.sync.failed` lines (test_sync_jobs.py UT01-55 fatal test).
- Focus 5 (lock): `_locked` (jobs.py:120-129); test drives 8 threads x 20 beats through a deliberately non-atomic heartbeat with a sleep and asserts zero overlaps + 160 beats — removing the lock would be caught (mutation-probe by reading).
- Mutation probe fatal-first: swapping to `errors[0]` fails `test_ut01_55_fatal_error_raised_first_after_all_entities` (A=SourceUnavailable recorded first, asserts `info.value is violation`); dropping the `continue` path/early-return on CircuitOpen fails the call-order asserts in UT01-54/55.
- Test IDs: every card ID (UT01-54/55/56/57/94, IT01-01/08/09) has functions in underscore form, docstrings start with the ID, module `pytestmark` set. FT01-06 is listed on U01-51 but not in this card's Tests row (not required here).

### Strengths
- Clean separation: `_Step` abstraction shares steps 4-8 between sync and reconcile; `_Tally` isolates result/raise logic.
- Late-binding lambda bug avoided with `partial`, and tested.
- Strict payload models (extra forbid, strict, frozen, name pattern, ≤64 entities) validated before any config/connector use; invalid-payload matrix asserts no runner built.
- register flag tied to ProcessState identity is robust against test resets.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. herness/connectors/jobs.py (320 lines) sits exactly at its 320 budget; the fix round (or any follow-up) has zero headroom. Consider trimming (e.g. `get_config()` is called twice, jobs.py:237 and jobs.py:170 — pass cfg through).
2. tests/unit/connectors/test_sync_jobs.py:666-685 — the U01-53 idempotence tests are labelled UT01-54 (card maps U01-53 to IT01-08), and `register_handler` already treats re-registering the same object as a no-op (herness/core/jobs/handlers.py:40-45), so deleting the `_Registered` flag would not fail `test_ut01_54_register_job_handlers_is_idempotent`. The test cannot detect loss of the flag; an assertion that `register_handler` is not called on the second call (monkeypatch spy) would.
3. herness/connectors/factory.py:17,48 — module imported as `clock` and shadowed by the `clock` parameter inside `build_connector`; works (default bound at def time) but is confusing. `_kwargs` names it `now` — use one naming.
4. herness/connectors/jobs.py:43-46 — `_Date` uses `date.fromisoformat`, which on 3.11+ also accepts basic/week forms (`20260101`, `2026-W01-1`); payload dates are meant to be ISO `YYYY-MM-DD`. Low risk (builder always emits extended form).
5. tests/unit/connectors/_jobs_data.py:21-25 — `FakeConfig` docstring mentions `paths.data` but the class has only `sources`.
6. herness/connectors/jobs.py:168-186 — in all-sources mode a `build_connector` ConfigError for a later source aborts the job after earlier sources already wrote (results discarded from the outcome). Spec-conformant (ConfigError from config), noted only for the files/`--full` spec note already parked.

### Assessment
**Task quality:** Approved
**Reasoning:** All five units match the spec algorithms with explicit evidence (payload-first validation, registry-only resolution, fatal-first raise, circuit skip, delta skip, locked progress); acceptance tests pass, coverage is 100% line/branch on both modules, and remaining items are minor/labelling or already-ruled carry-overs.

---

## Re-review r1 (head 908ebb3, base 1a8cce6; scope: registry row, U01-53 tests, factory clock, FakeConfig)

### Evidence (worktree, PYTHONUTF8=1)
- `pytest tests/unit/connectors/test_connector_factory.py tests/unit/connectors/test_sync_jobs.py tests/integration/connectors/test_sync_jobs_flow.py tests/unit/core/test_registry.py -q -p no:logging` → 51 passed.
- `pytest -m integration -k IT01_08 -q -p no:logging` → 3 passed (queue flow + the two moved U01-53 tests).
- Lazy import: `import herness.core.registry` in a fresh interpreter → `duckdb`, `pyarrow`, `herness.connectors.files` all absent from sys.modules. The row is an import string resolved in `registry.get` via `importlib.import_module` only on lookup (registry.py:110-114).
- Mutation of the builtin row (fresh interpreter, `_BUILTINS.pop(("connector","files"))` injected into the test's `_PROBE` before `build_connector`): exit 1 with `ConfigError: unknown connector 'files'`, and `herness.connectors.files` confirmed not imported by `load_config` (files.py:171 self-registers on import, so this matters). The subprocess test therefore fails without the row. Proven.
- `test_ut01_94_files_resolves_through_the_builtin_table`: removing the row fails its first assertion (KeyError on the freshly loaded table); the resolution half then checks the live `get` path with the shipped string. Adequate.
- U01-53 spy (test_sync_jobs_flow.py:139-157): the spy wraps the real `register_handler`; without the `_Registered` flag the second call appends again (`seen` has 4 entries) and the assert at :153 fails. Kills the mutation that the old test could not detect.
- factory.py: the module alias `clock` is now referenced only at module level (`_DEFAULT_CLOCK`, factory.py:26); inside `_kwargs`/`build_connector` `clock` is always the injected callable. Correct; 62/90 lines. registry.py 143/160.
- FakeConfig docstring (_jobs_data.py:96-97) now accurate.

### Findings
#### Critical / Important
- None.

#### Minor
1. tests/unit/connectors/test_connector_factory.py:271 — `done.stdout.endswith("\nherness.connectors.files:FilesConnector")` relies on the config loader's `config.load.completed` log line (and validate warnings) going to stdout just before the probe's output; I confirmed that is what supplies the leading `\n` today. If logging moves to stderr or is silenced, the test fails even though behaviour is correct. Not flaky as things stand (deterministic, synchronous logging), but coupled. More robust: `done.stdout.splitlines()[-1] == "herness.connectors.files:FilesConnector"`, or a unique marker prefix.
2. test_connector_factory.py:252 (`_PROBE`) — the `sys.modules` assertion runs before `load_config`, not immediately before `build_connector`; I verified `load_config` does not import the files module today, but moving the assert to just before `build_connector` would make the "never imported" premise hold by construction.
- Carried from round 0 (parked per instruction): m1 (jobs.py 320/320), m4, m6.

### Verdict r1
**Task quality:** Approved
**Reasoning:** All three scoped fixes are correct and verified by mutation: the builtin row is lazy and the tests fail without it, and the spy test fails if the flag is removed. The two remaining notes are test-robustness polish.
