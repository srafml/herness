# T08-08 Fault hook: build report

Status: DONE
Commit: 4400ba9 feat(resilience): add fault plans and fault_point hook (T08-08)
Worktree/branch: D:\herness\.claude\worktrees\agent-a6e58c138a42034d8 / worktree-agent-a6e58c138a42034d8 (base cdadffe)

## What was built
- herness/core/resilience/faults.py (340/340 lines): NAMED_POINTS (the 18 points), FaultRule
  (pydantic, extra=forbid, strict, frozen; point/action validators, selector rules), FaultPlan
  (dataclass: rules, counters, rngs), load_fault_plan, fault_point.
  - load: is_symlink refusal, resolve(strict), must be a regular file, `.json` suffix, reads at most
    65 537 bytes and refuses > 65 536, json.loads with parse_constant rejecting NaN/Infinity,
    list of <= 100 mappings, each FaultRule.model_validate. Errors are
    `ConfigError("invalid fault plan: <file name>: <reason>")`: file name only, and the first pydantic
    error without its input.
  - fault_point: NOT_LOADED -> double-checked load under ProcessState.lock. No/empty HERNESS_FAULTS
    stores None. HERNESS_ENV != test stores None and logs WARNING resilience.faults.ignored once
    (env = value[:32] or "unset"); the file is never opened. Under test it loads the plan, sets
    faults_enabled and logs WARNING resilience.faults.enabled (plan = file name, rules = count).
    A load failure raises ConfigError and leaves the plan NOT_LOADED. With a plan loaded, an unknown
    point or a label key outside model/source/role/kind raises ConfigError. Counters and RNG draws run
    under the lock, and the first firing rule wins. Every action follows the U08-34 table.
  - kill: CRITICAL resilience.faults.kill, then os.kill(getpid, SIGKILL if available else SIGTERM).
  - kill_service: reads HERNESS_STUB_SERVICES (JSON object). If the value is malformed, not a dict,
    has a non-string URL or a non-loopback or non-http(s) URL, it raises ConfigError, checked in
    faults.py itself. If the object names the service, faults.py imports herness.core.egress lazily
    (importlib; no httpx import) and runs loopback_http_client(url, timeout_s=5).post("/__control/kill")
    .raise_for_status(). If herness.core.egress itself is missing (ModuleNotFoundError with
    name == "herness.core.egress"), it falls through to kill_service_hook. Any other
    ModuleNotFoundError propagates. With no hook it logs WARNING resilience.faults.no_service_hook.
- _state.py: `type FaultPlan = Any` placeholder replaced with a TYPE_CHECKING import of the real
  FaultPlan.
- resilience/__init__.py (80 lines): lazy exports + TYPE_CHECKING re-exports of fault_point,
  FaultRule, load_fault_plan, NAMED_POINTS (R1). The impl 08 §2 row budget went 70 -> 120 in the
  same commit.
- store/ops/_shims.py: the no-op fault_point and its `# T08-08` marker are deleted and replaced by
  `from herness.core.resilience import fault_point, process_state` plus
  `__all__ = ("fault_point", "retry_call")`. I used `__all__` because ruff PLC0414 rejects
  `import x as x`, and `__all__` keeps the name explicitly exported for mypy. The docstring is
  updated. retry_call and its `# T08-07` marker are untouched. core.py is not edited. The impl 02
  §2 `_shims.py` row is updated as ruled (budget 80 unchanged).
- enrich/cache.py: the private `_fault_point` is deleted and replaced by
  `from herness.core.resilience import fault_point` and a call at the flush site.

## Decisions
- error:<Class>: the closed set is exactly the 18 spec 00 §7 leaf classes (incl. NotFound and
  EgressBlocked, R-19). Spec-local subclasses (JobStateError) are rejected at load time.
  Validation happens at load time in the FaultRule action validator. Only CircuitOpen has required
  kwargs; it is built with key=f"fault:<point>" and retry_at=clock.now(). RateLimited via error:
  gets retry_after=None (its default). All the others take the message only ("fault: injected").
- QueryError timeout carrier: `QueryError("fault: timeout", timeout=True)`. HernessError's
  **context takes it, so it reads as err.context["timeout"] is True (same as ledger w07-s08).
- delay:<s>: regex `[0-9]{1,3}(\.[0-9]{1,6})?` and 0 < s <= 600. kill_service:<svc>: svc must be in
  the ServiceName literal (from herness.core.types).
- fault_env: tests/support/fault_env.py, registered as a plugin in the root tests/conftest.py
  pytest_plugins (`tests.support.fault_env`), the same pattern as fake_keyring and ops_store. The
  fixture depends on reset_process_state (seeded rng, no-op sleeps). It returns
  `enable(rules) -> Path`, which writes tmp_path/fault_plan.json, sets HERNESS_ENV=test and
  HERNESS_FAULTS, and resets fault_plan to NOT_LOADED and faults_enabled to False. The type alias
  is FaultEnv.
- Selector semantics are implemented literally: fire when (nth and c==nth) or (count and c<=count)
  or (p and rng<p) or none set.

## Tests re-pointed (R4)
- FT02-02 (tests/fault/store/test_store_ops_fault.py): real JSON plan via fault_env, rule
  {"point":"sqlite.write","action":"error:StoreBusy","count":4|7}. Asserts plan.counters == [5] or [6],
  faults_enabled, and the row outcome.
- UT03-35 (tests/unit/enrich/test_cache.py): the `fault_calls` fixture now enables a never-firing
  rule (nth 10**6) on enrich.after_batch_write and returns a counter reader. Assertions changed from
  list equality to counts (1/2/0). Test IDs are unchanged.
- UT02-28 shims test: now `_shims.fault_point is resilience.fault_point`. FT02-01 and the
  test_store_ops_core spy are left alone.

## New tests
- tests/unit/core/resilience/test_resilience_faults.py: UT08-46 (exports; 1 000 calls with open spied
  on builtins/io/os/Path: no access, no log), UT08-47 (23 invalid-plan cases, 70 KB, exact-limit
  accept, .yaml, real symlink (skipped: no privilege on this machine), patched is_symlink, bad
  JSON/NaN/non-UTF-8, missing/dir, valid plan state), UT08-48 (nth, count, p+seed reproducible, label
  filter, first-rule-wins, unknown point/label), UT08-49 (family actions, all 18 error: classes,
  http_429 retry_after, delay on fake sleep, kill with os.kill patched + SIGTERM fallback,
  kill_service hook / no hook / fake egress via sys.modules for 127.0.0.1, localhost and [::1] /
  stub not naming the service / 6 bad stub configs / egress absent / other import error),
  UT08-50 (enabled log, invalid plan stays disabled, lock race branch), UT08-105 (unset/prod ignored
  with open spied and one WARNING; test applied; fixture check).
- tests/security/test_st08_faults.py (ST08-06, marker unit): symlink (real, or the patched
  is_symlink fallback when the platform refuses), 1 MB .json, and .yaml with
  !!python/object/apply all raise ConfigError. os.system is spied and never called and the marker
  file is not created. The same YAML body named .json fails JSON parsing. An AST check confirms no
  yaml import in faults.py. Unset env gives resilience.faults.ignored. A valid plan under test
  gives faults_enabled True.

## RED / GREEN
- RED: with faults.py moved aside, `pytest tests/unit/core/resilience/test_resilience_faults.py` ->
  ImportError: Error importing plugin "tests.support.ops_store": No module named
  'herness.core.resilience.faults'. Collection fails because _shims re-exports it.
- GREEN: card + touched tests: 627 passed, 1 skipped (symlink privilege). Full
  `pytest -m "(unit or integration) and not slow"`: 4082 passed, 6 skipped, 17 deselected, 1 xfailed
  (the xfail was already there, IT00-02 traceability). `pytest tests/fault/store`: 2 passed.
  `--require-test-ids` on the new and changed files: clean.

## Gates
ruff format/check clean; mypy strict: no issues (146 files); lint-imports: 13 kept, 0 broken;
check_type_ownership exit 0; check_module_size exit 0; C901/PLR0913 clean; the pre-commit hooks all
passed (no --no-verify, no PRE_COMMIT_ALLOW_NO_CONFIG).

## Coverage (line / branch, card and touched tests)
faults.py 100 % (241 stmts, 80 branches); resilience/__init__.py 100 %; _state.py 100 %;
store/ops/_shims.py 100 %; enrich/cache.py 100 %.

## Line counts vs budget
faults.py 340/340; resilience/__init__.py 80/120 (budget raised per R1); _shims.py 62/80;
enrich/cache.py 279/360; _state.py 140/140.
Note: faults.py reaches 340 only with a `# fmt: off/on` block that packs the NAMED_POINTS and leaf
class name tuples several per line. ruff format would otherwise put one name per line and push the
file to about 375 lines.

## Carry-overs
- herness.core.egress (impl 10 T10-18) is not in the tree. For kill_service with
  HERNESS_STUB_SERVICES, faults.py falls through to kill_service_hook / no_service_hook while the
  module is missing. The stub POST path is only exercised with a fake module injected via
  sys.modules. Once impl 10 lands, check that loopback_http_client's return value is a context
  manager with post().raise_for_status() (httpx.Client is).
- ST08-06 status `FAULTS ENABLED` and degraded health() belong to U08-92 (later card). This card
  covers only process_state().faults_enabled.
- The real-symlink tests skip on this machine (WinError 1314). The branch is covered through the
  patched Path.is_symlink.

## Fix round 1 (commit 38e001e test(resilience): tighten fault hook file-access spies (T08-08))
- I1: UT08-46 and UT08-105 now share one spy. `_fs_spy()` is a context manager scoped to the
  fault_point calls, built on pytest.MonkeyPatch.context. It records and refuses builtins.open,
  io.open, os.open, os.stat, Path.open, Path.stat, Path.is_symlink, Path.resolve and Path.exists,
  on any path. In UT08-105 the plan file is written before the spy is installed. The spy is scoped
  rather than test-wide because a test-wide patch of Path.resolve made pytest's own failure
  reporting crash. New UT08-46 test_ut08_46_spy_catches_stat_only_access checks that every entry
  point, and a bound Path method, is recorded and then restored.
- Mutants were checked by temporary in-place edits of faults.py, reverted with git checkout and not
  committed:
  - Path(source).is_symlink() before the env gate: UT08-105 fails (both cases).
  - Path(source).stat(): UT08-105 fails.
  - io.open(source).close(): UT08-105 fails.
  - Path(source or ".").exists() with the variable unset or empty: both UT08-46 tests fail.
- m2: the FT02-02 rule now carries "kind": "insert_item" (counts 4 and 7 and the outcomes are
  unchanged). A temporary wrong label (create_item) fails both cases.
- Gates: ruff format/check clean, mypy clean, pre-commit hooks passed. Card tests: 94 passed,
  1 skipped. tests/fault/store + ST08-06: 9 passed. Full `(unit or integration) and not slow`:
  4083 passed, 6 skipped, 1 xfailed (the IT00-02 xfail was already there).
