# T08-08 Fault hook: review (verify agent)

Reviewed: worktree agent-a6e58c138a42034d8, cdadffe..4400ba9 (1 commit). Read-only.

### Spec Compliance
- ✅ Spec compliant (units). Per row:
  - U08-33 `NAMED_POINTS` (18 points, exact design 08 §5.13 list) ✅; `FaultRule` pydantic extra=forbid, strict (+frozen) ✅; validation table: point in set, closed action set (timeout/http_429/http_503/malformed_json/kill, error:<18 spec 00 §7 leaves>, delay:<0<s<=600>, kill_service:<ServiceName>) ✅, nth/count >= 1 and mutually exclusive ✅, 0<p<=1 requires seed ✅, retry_after >= 0 only with http_429 ✅, label filters ✅.
  - `load_fault_plan`: is_symlink refusal, resolve(strict), regular file, `.json` only, reads at most 65 537 bytes (no stat) and refuses > 65 536, json.loads (NaN/Infinity rejected), list of <= 100 mappings, `FaultPlan(rules, counters=[0]*n, rngs)` ✅. Errors `ConfigError("invalid fault plan: <name>: <reason>")`, file name only, pydantic error without input ✅. No yaml import ✅ (AST-checked by ST08-06).
  - U08-34 `fault_point`: inert path reads state attribute only; empty var = unset; non-test env stores None, never touches the file, one `resilience.faults.ignored` WARNING (`env` or `unset`) ✅; test env loads, sets `faults_enabled`, WARNING `resilience.faults.enabled` (plan = name, rules = count) ✅; point/label keys checked only with a plan ✅; counters + RNG draws under `ProcessState.lock`, first firing rule wins, later rules not counted ✅; family mapping http./sqlite./else ✅; timeout on sql.* -> `QueryError("fault: timeout", timeout=True)` ✅; error:<Class> ✅ (CircuitOpen built with key/retry_at); http_429 with retry_after ✅; http_503 family ✅; malformed_json -> OutputValidationError ✅; delay via `process_state().sleep` (outside lock) ✅; kill: CRITICAL log then SIGKILL else SIGTERM ✅; kill_service: stub map (loopback check -> ConfigError) -> `loopback_http_client(url, timeout_s=5)` POST `/__control/kill`, else hook, else WARNING `no_service_hook` ✅ (egress-missing fallthrough per controller ruling).
  - Shim replacements: core.py untouched (core.py:155 still calls `_shims.fault_point("sqlite.write", kind=op)`), `_shims.fault_point` is the real hook (UT02-28 asserts identity; FT02-02 proves the call reaches it via plan counters), retry_call + `# T08-07` untouched, enrich cache flush calls the real hook (cache.py:260); no `# T08-08` no-op left anywhere in herness/ ✅.
  - Tests: UT08-46 ✅, UT08-47 ✅ (real symlink skipped here; patched branch covered), UT08-48 ✅, UT08-49 ✅, UT08-50 ✅, UT08-105 ✅ (see Important #1 on spy strength), ST08-06 ✅ for the card's half; FT02-02 ✅ (real JSON plan via fault_env; count=4 -> 5 hook calls, success; count=7 -> StoreBusy after 6 attempts, counters==[6]); UT03-35 ✅ (real hook, never-firing rule, counter reads 1/2/0).
- ⚠️ Cannot verify from diff / carried over:
  - ST08-06 `FAULTS ENABLED` status + degraded `health()` -> U08-92 (ruled carry-over).
  - Stub-service POST only exercised with a fake `herness.core.egress` (impl 10 T10-18 absent). Impl 10 signature `loopback_http_client(base_url, *, timeout_s)` matches the call; context-manager + `post().raise_for_status()` contract to be rechecked when T10-18 lands.
  - Real-symlink tests skip on this machine (WinError 1314); should run on CI/Linux.

### Strengths
- Tight, readable implementation; file never touched outside test env (no stat, no open); bounded read instead of stat+read; closed sets validated at load time; error messages carry file name only.
- Double-checked lazy load under the lock; actions (sleep, hook, kill) run outside the lock.
- Re-points use the real hook through a genuine HERNESS_FAULTS JSON plan (fault_env fixture), not a fake.
- 100 % line/branch on faults.py per report; good negative coverage (23 invalid-plan cases, 6 bad stub configs, import-error propagation).

### Issues
#### Critical (Must Fix)
- none

#### Important (Should Fix)
1. UT08-105 file-access spy is weaker than UT08-46's and misses real access. tests/unit/core/resilience/test_resilience_faults.py:588-590 patch only `Path.open` and `builtins.open`, not `io.open`/`os.open` (the `open_spy` fixture at :37-49 does all four). Mutation check (scratch pytest plugin wrapping `_ensure_loaded`, env unset/prod): a mutant that does `io.open(src, "rb").close()` in the ignored branch -> UT08-105 still PASSES; a mutant that `Path(src).stat()` + `is_symlink()` -> PASSES (neither spy covers stat, UT08-46 included); a mutant that fully calls `load_fault_plan` -> FAILS (good). Since "UT08-105 proves ... no file access" is a card acceptance check, reuse `open_spy` in UT08-105 and add `os.stat`/`Path.stat`/`Path.is_symlink` to the spy (fix is a few lines; the implementation itself is correct).

#### Minor (Nice to Have)
2. tests/fault/store/test_store_ops_fault.py:38 dropped the old assertion that the hook is called with `kind=<op>` label. Add `"kind": "insert_item"` to the rule so the label pass-through from run_write is proven again (the create_item write would then not match).
3. herness/core/resilience/faults.py:219-223 selector short-circuit: if a rule sets `p` together with `nth` or `count` (not forbidden by the spec), the RNG is not drawn on calls where nth/count fire, so the p stream shifts. Deterministic either way; either reject p with nth/count at load time or draw unconditionally.
4. herness/core/resilience/faults.py:151 `.suffix.lower()` accepts `.JSON`; spec says suffix `.json`. Harmless, note only.
5. herness/core/resilience/faults.py:145-153 is_symlink then resolve/open is a TOCTOU window (test-only hook, acceptable).
6. `# fmt: off` block faults.py:34-58 to hit 340/340: acceptable — same pattern is used in 8 other modules (types/__init__, _ownership, memory, ui_reads, ...), `ruff format --check` clean, content is pure constant tuples. No action.

### Gates (run by reviewer)
ruff check: clean; ruff format --check: 342 files already formatted; mypy: no issues (146 files); lint-imports: 13 kept, 0 broken; check_type_ownership: exit 0; check_module_size: exit 0 (faults.py 340/340); pytest (faults unit, ST08-06, tests/fault/store, enrich cache, store ops incl. shims): 419 passed, 1 skipped (symlink privilege).

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Implementation matches U08-33/U08-34 and the shim/cache replacements are correct; only the UT08-105 acceptance test's file-access spy is demonstrably unable to catch io.open/os.open/stat access and should be tightened (Important #1), plus small test-evidence polish.

## Re-review round 1 (head 38e001e; scope I1 + m2 only, m3-m6 parked by ruling)

- I1 ✅ Fixed. `_fs_spy()` (tests/unit/core/resilience/test_resilience_faults.py:38-72) records and refuses builtins.open, io.open, os.open, os.stat, Path.open/stat/is_symlink/resolve/exists; UT08-46 (both tests) and UT08-105 use it.
  - Scoping: the spy wraps the whole call loop, including the first `fault_point` call. With `reset_process_state` the plan is NOT_LOADED, so that first call is the one that loads or ignores it. The plan file is written before the spy is installed. Coverage of the loading call is intact.
  - My round-0 mutation plugin re-run (wraps `_ensure_loaded`, env unset/prod): baseline 8 passed; `load_fault_plan` mutant FAILS both UT08-105 cases; `Path.stat()+is_symlink()` mutant now FAILS both; `io.open(...).close()` mutant now FAILS both (all three were checked; stat and io.open passed in round 0).
  - New test_ut08_46_spy_catches_stat_only_access proves every entry point is intercepted and restored.
  - Nit (no action): `os.lstat` / `Path.lstat` are not in the set; `is_symlink` is patched directly, so the realistic paths are covered.
- m2 ✅ Fixed. tests/fault/store/test_store_ops_fault.py:38-47: the rule now filters `"kind": "insert_item"`. The counters `[5]` / `[6]` can only be reached if run_write passes `kind=<op>`, and the `create_item` write is not counted. Spec rows unchanged: count=4 succeeds; count=7 gives StoreBusy after 6 attempts.
- Gates (reviewer run): ruff check clean; ruff format --check 342 files formatted; mypy no issues (146 files); card tests + ST08-06 + tests/fault/store + enrich cache + store ops: 420 passed, 1 skipped (symlink privilege).

**Task quality:** Approved
**Reasoning:** Both scoped findings are fixed. The file-access spies now kill the stat-only and io.open mutants that survived in round 0, and FT02-02 proves the kind label again. Gates are green.
