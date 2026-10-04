# T05-26 review (Health) — base b4d1054, head c4ca22f

### Spec Compliance
- ❌ Issues found: U05-73 "Errors: None escape" / Postcondition "Never raises for a failing check" and the controller ruling "every check wrapped so nothing escapes" are only partly met. The clients check wraps `registry.health()` but not the rest of the check: `chain_for` raising anything other than `ConfigError`, or a non-`str` health value, escapes `harness_health` (probed; see Important 1).
- Otherwise compliant with U05-73 steps 1-5 and the rulings (w15-s05 T05-26; w13-s05b health values):
  - (1) clients come only from `registry.health()`. health.py has no HTTP client and no egress import. Off-network clients make no call (registry side).
  - (2) traces: `NamedTemporaryFile(dir=traces_dir)` is created and deleted. Missing dir, file-as-dir and similar failures give `down` / "traces dir not writable". No temp file is left behind (probed).
  - (3) warehouse: CURRENT read is bounded to 65 bytes. A 50 MB CURRENT had a peak allocation of about 10 KB (probed). The id is validated with `BUILD_ID_RE`, then `open_warehouse` opens it (duckdb `read_only=True`) and it is closed. `close()` was confirmed called on success. Every failure (binary garbage, oversized, CURRENT is a directory, dir missing, valid id with no file, `get_config` raising, `close` raising) gives `degraded` / "current warehouse unavailable".
  - (4) route set = union of `chain_for(r, d)` over analyst/chat and fast/standard/deep, with `ConfigError` skipped. If all route clients are down the result is `down`. If some are down it is `degraded`. A client shared between analyst and chat with the other chain member up gives `degraded` (correct).
  - (5) status is the worst of the checks and reasons are joined with "; " (probed: `"a/c clients down; client x down; traces dir not writable; current warehouse unavailable"`).
  - BaseException is not swallowed: KeyboardInterrupt from health(), NamedTemporaryFile and open_warehouse all propagate (probed).
  - No secrets: exception messages never reach the dict. The only variable content is registry-sanitised values and config client keys. Logging uses type names only and is itself suppressed, so a broken logger does not raise (probed).
- ⚠️ Cannot verify from diff: behaviour on POSIX special files (FIFO as CURRENT). Only Windows was probed.
- Builder's deviation: when every route client is down, other down clients outside the route set are still listed ("client x down"). Status stays `down`. Accepted: it matches the worst-of rule and keeps every down client visible.

### Strengths
- Small, readable module (94/100). Three independent checks and a clean worst-of fold.
- Bounded CURRENT read with validation before any filesystem path is built. Reuses `BUILD_ID_RE` and `open_warehouse` instead of duplicating them.
- Tests use a real `LLMRegistry` and a real warehouse build. The R-52 case goes through the registry's actual vLLM path. Secret and path non-leak is asserted.
- Gates re-run here: 9 passed. Coverage of health.py is 100 % line and 100 % branch (68 stmts, 10 branches). ruff check/format are clean, mypy is clean, and module size is 94 ≤ 100.
- Test names and docstrings carry UT05-123, and `pytestmark = pytest.mark.unit` is set.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. `herness/harness/health.py:58-62`: part of the clients check is unwrapped, so errors escape `harness_health`. Probes:
   - `chain_for` raising `RuntimeError("boom sk-secret")` escaped as `RuntimeError`.
   - `health()` returning `{"a": None}` escaped as `AttributeError`.

   With today's `LLMRegistry` both are unlikely, because `chain_for` only raises `ConfigError` and `health()` returns `dict[str, str]`. The contract is still "none escape", and the ruling requires every check to be wrapped. An escaping message can also carry unsanitised text into the `herness doctor` traceback.

   Fix: put the whole of `_client_checks` after `health()` (the down set, the route union and the comparison) inside `try/except Exception`, with `# noqa: BLE001` citing U05-73. On failure, log the type name and add a fixed reason. Keeping `clients` as returned is fine if `health()` succeeded; otherwise `{}`. Add one test with `chain_for` monkeypatched to raise `RuntimeError`.

#### Minor (Nice to Have)
1. `herness/harness/health.py:84`: `open("rb")` on a POSIX FIFO named CURRENT blocks until a writer appears, so the probe could hang. Anyone who can plant a FIFO there already has write access to `warehouse_dir`, which is why this is only Minor. A cheap guard is `path.is_file()` before opening, or `os.open(..., O_RDONLY | O_NONBLOCK)`.
2. `herness/harness/health.py:65`: if neither analyst nor chat is routable (empty route set), all down clients only degrade and nothing says the chat/analyst route is unusable. This is defensible because config validation should require these roles. Note only.
3. `tests/unit/harness/test_health.py:38-53`: the "union over depths" and fallback parts of the ruling are not exercised. Every test has single-client routes, no `fallback` and no `depth_overrides`. The builder's deviation (all route clients down plus another client down → `"… clients down; client x down"`) is also not tested. Suggest one test with `depth_overrides.deep.roles.analyst = extra` and `fallback`, asserting that "all down" needs every member of the union.
4. Out of scope (pre-existing, `herness/harness/warehouse.py:174-182`): `open_warehouse` does not close `con` if the `SET`/self-check steps raise after `duckdb.connect`. health.py cannot close a handle it never receives. Flag it to the warehouse card owner rather than fixing it here.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The behaviour, bounds, secrecy and coverage are all good. One contractual gap remains: the clients check's route/down computation (health.py:58-62) is unwrapped, so non-`ConfigError` failures escape, which U05-73 and the ruling forbid. The fix is one `try` block plus one test.

---

## Re-review round 1 — head eea2516 (previous c4ca22f); scope I1, M1, M3 (M2, M4 parked)

### Spec Compliance
- ✅ Spec compliant: U05-73 "Errors: None escape" is now met for all three checks, as the ruling requires.
- ⚠️ Cannot verify here: the FIFO case for M1 on POSIX. The environment is Windows only. By reading the code, `Path.is_file()` returns False for a FIFO, so the open is never reached.

### Resolution
- **I1 resolved.** `herness/harness/health.py:52-67` now wraps the whole clients check (`health()`, the down set, the route union and the comparison) in one `try/except Exception` (noqa BLE001, cites U05-73). On failure it gives `{}` / `("degraded", "client health unavailable")` and logs the type name only.
  - Probe: `chain_for` raising `RuntimeError("boom sk-secret")` gives degraded, `clients` `{}`, and no secret text in the output.
  - Probe: `health()` returning `{"a": None}` gives degraded.
  - New tests: `test_ut05_123_chain_for_raising_non_config_error_is_degraded` and `test_ut05_123_health_with_non_str_value_is_degraded`.
- **M1 resolved.** `health.py:83-86` checks `current_path.is_file()` before opening, so a FIFO, directory or other special file fails fast with no blocking open. The CURRENT-is-directory probe still gives degraded.
- **M3 resolved.** Three tests were added:
  - A union over a fallback chain plus a `deep` depth override, where the heads are down but other members are up, gives degraded with "client analyst down; client chat down".
  - Every union member down gives down with "analyst/analyst_fallback/chat/chat_deep clients down".
  - The builder's deviation, all route clients down plus a non-route client down, gives down with "analyst/chat clients down; client extra down".

### Regression check (full probe.py re-run at eea2516)
Every earlier probe gives the same result as at c4ca22f:
- **CURRENT read is bounded:** a 50 MB CURRENT peaked at about 10 KB allocated.
- **Degraded as before:** binary garbage, CRLF (ok), CURRENT as a directory, missing warehouse dir, a missing build file, `get_config` raising and `close` raising.
- **Traces check:** a missing dir or a file in place of the dir gives down, and no temp file is left behind.
- **Worst-of and "; " joining** are unchanged. A broken logger still does not raise.
- **KeyboardInterrupt** from `health()`, `NamedTemporaryFile` and `open_warehouse` still propagates. It is not swallowed.

### Gates (re-run)
- 14 passed.
- Coverage of health.py: 100 % line and 100 % branch (72 stmts, 12 branches).
- ruff check and format are clean, and mypy is clean.
- Module size: `check_module_size` found no violations (97 ≤ 100).
- The worktree is clean.

### Issues
#### Critical
- None.
#### Important
- None.
#### Minor
- None new. There is a theoretical race at `health.py:84-87` between the `is_file()` check and the open. It is negligible, because it needs someone with write access to swap CURRENT in between. Note only, no action.

### Assessment
**Task quality:** Approved
**Reasoning:** I1, M1 and M3 are fixed and pinned by tests. The full probe re-run shows no regression, and all gates are green.
