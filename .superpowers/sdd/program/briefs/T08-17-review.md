# T08-17 review: GPU services and lock

Reviewed: worktree agent-aae2a8f4e5c8fed0a, HEAD 3ac2075 (base 750ec27). Read-only; mutation probes were applied temporarily and reverted (working tree clean at the end).

### Spec Compliance
- ✅ Spec compliant. Every unit and test row is met. None of the findings below blocks the card.

| Unit / row | Result | Notes |
|---|---|---|
| U08-78 ComposeRunner | ✅ | argv = `compose_cmd + ["-f", compose_file] + tail`, matching the table exactly (gpu_services.py:84,91,97,101,120). Timeouts are 120 / `stop_timeout_s` / 60 / 30. `shell=False, capture_output=True, text=True, check=False`. The class and service are validated (allowlist, then regex) before any subprocess runs. Error texts match the spec: "compose unavailable", "compose <verb> <service> timed out", "compose <verb> <service> failed rc=<n>" (ps uses "compose ps ..."). On rc≠0 it logs WARNING `jobs.gpu.compose_failed` with stderr that is redacted first, then cut to 500. It reuses `classify._redacted`, so it gets the same straddle handling: redact a window, then drop the last 500 chars when the body is longer than the window. ps accepts an array or JSON lines, and garbage (including deep nesting via RecursionError) gives "compose ps unreadable". |
| U08-79 LoopbackHttp | ✅ | Every request runs the loopback check first (scheme http, host 127.0.0.1/localhost/::1, else ConfigError), in both `healthy` and `warm_up`. The client comes only from `egress.loopback_http_client`, looked up per call. The factory has `follow_redirects=False`, so a 3xx makes `healthy` return False. The body is streamed and the read stops at 64 KB. The bearer is passed as a SecretStr and is never logged. The warm-up bodies match the table verbatim. The vLLM model comes from the first `gpu_class == "reasoning"` client, with that client's `api_key`. |
| U08-80 vram_used_mb / wait_vram_free | ✅ | Runs the argv list with a 10 s timeout. It parses the first non-empty line as an int, and any failure or rc≠0 returns None. A None reading counts as not free; an equal reading is also not free. It uses `clock.monotonic`/`clock.sleep`, so the fake clock applies. Timeout raises "vram_not_freed". |
| U08-82 GpuLock | ✅ | Creates the parent dir, opens with `a+b`, seeks to 0, then locks with `msvcrt.locking(LK_NBLCK,1)` or `fcntl.flock(LOCK_EX\|LOCK_NB)`. On OSError it closes the file and raises `ConfigError("another GPU owner holds data/locks/gpu.lock")` verbatim. Exit unlocks and closes, and a second exit does nothing. |
| UT08-87 | ✅ | Exact argv lists and kwargs, `"x; rm -rf"` plus other bad names, bad class → ConfigError with no argv. |
| UT08-88 | ✅ | Array, lines, shapes, empty, and 9 garbage cases. |
| UT08-89 | ✅ | Spy on the factory, bearer sent and not in the logs, `http://10.0.0.1:8000` → ConfigError with no client built, 302 → unhealthy, all 3 warm-up bodies. |
| UT08-90 | ✅ | 20 000 then 1 500 returns on the 2nd poll. The timeout case takes 60 fake seconds and 31 polls. |
| UT08-95 | ✅ | Two real processes (spawn): the second is refused and succeeds once the first exits. The lock is also released when the owner is killed. |
| ST08-01 | ✅ (config and runner halves) | The "in gpu_request" half is a carry-over per the controller ruling. |
| ST08-12 | ✅ | Config rejects the attacker URL. A 302 is not followed: one request and one connect, both to 127.0.0.1. |
| Acceptance: argv exact; UT08-103 no httpx client in jobs | ✅ | test_import_contracts_08 is green. |

- ⚠️ POSIX branch of GpuLock (`fcntl`, `# pragma: no cover`) was not exercised on this Windows host.
- ⚠️ The real nvidia-smi / compose behaviour was not exercised (by design, the fake is used). See C1.

### Gates run
- `pytest tests/unit/core/jobs tests/security/test_st08_gpu.py tests/unit/repo/test_import_contracts_08.py -q`: 182 passed.
- Card tests with branch coverage: 80 passed. gpu_services.py 100 % (170 stmts / 36 branches), gpu_lock.py 100 %. Target is ≥90/85.
- ruff check: clean. ruff format --check: clean (8 files).
- mypy: no issues in 224 files.
- lint-imports: 13 kept, 0 broken.
- `tools.check_module_size`: rc 0 (287/360 and 80/90).
- Test naming: IDs are in the function names and the first docstring line, and `pytestmark = unit` is set in all 3 files.

### Mutation probes (security- and behaviour-relevant)
KILLED by the tests:
- removing the allowlist check
- removing the class check
- `shell=True`
- removing the loopback check in `healthy`
- removing the loopback check in `warm_up`
- dropping the scheme check
- None counting as free
- `<=` threshold
- no redaction
- no 500 cut
- picking the last reasoning client
- ignoring rc in vram
- dropping RecursionError from the ps except

SURVIVED (all minor, listed below):
- removing the `_SERVICE_RE` regex check
- stop using `COMPOSE_UP_TIMEOUT_S` instead of `stop_timeout_s`
- removing the 64 KB read cap
- GpuLock not closing the file when the lock fails
- GpuLock `__exit__` not unlocking (closing still releases the lock on Windows)

### Strengths
- The loopback check is local and per request, before the secret is resolved and before any client is built. The egress factory's `LoopbackOnlyTransport` adds a second enforcement layer. ST08-12 proves redirects are not followed at the socket level.
- The fake_gpu fixture is realistic. It runs the real `ComposeRunner`, uses real stub HTTP servers on ephemeral loopback ports, records kwargs, and injects failures. It is also ready for the U08-81 swap tests (`running_classes`, `assert_single_class`).
- UT08-95 uses real separate processes, including the case where the owner is killed.
- ConfigError texts do not echo attacker-controlled names.

### Builder deviations: judgement
1. Pinned `GpuSettings` on `ComposeRunner`: accept. The spec gives no constructor, and the default reads R.gpu per call.
2. `healthy` returns False when the bearer cannot be resolved: accept. The spec says "any other status or exception → False", and the loopback ConfigError is still raised first.
3. llamacpp sends `health.bearer_secret` when one is configured: accept. It does nothing in the shipped config and is consistent with TH08-12.
4. Streamed 64 KB cap: accept. This is the right reading of "read up to 64 KB and discarded", and the factory's 50 MiB ceiling stays as the outer limit. The test does not prove the cap (see M3).
5. Private `classify._redacted` import: accept per the controller ruling. One cosmetic side effect: if redaction fails, the log event is named `resilience.classify.redact_failed` even though it came from a jobs call (classify.py:86). A later cleanup could make the helper public (e.g. `redact_detail`). No action needed now.

### Builder concern C1: vram_check_cmd YAML split (confirmed)
`yaml.safe_load('x: [nvidia-smi, --query-gpu=memory.used, --format=csv,noheader,nounits]')` gives `['nvidia-smi', '--query-gpu=memory.used', '--format=csv', 'noheader', 'nounits']`. The flow sequence splits at every comma.

In production, nvidia-smi either rejects the stray positional arguments (rc≠0) or prints a CSV header and "MiB" units. Either way `vram_used_mb()` returns None forever. Every class swap then ends in `ModelUnavailable("vram_not_freed")` after 60 s, so the GPU switch does not work at all.

The same text is in:
- config/resilience.yaml:50
- tests/unit/core/fixtures/resilience.yaml:46
- design docs/specs/08-resilience-and-jobs.md:651

**Recommendation: yes, fix it with this card.** It is small (under 10 minutes):
- Quote the item as `"--format=csv,noheader,nounits"` in config/resilience.yaml:50 and tests/unit/core/fixtures/resilience.yaml:46.
- Add one assertion that pins the loaded argv, e.g. `shipped_gpu_settings().vram_check_cmd == ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]`, so the bug cannot come back.
- Update the comment at tests/unit/core/jobs/test_jobs_gpu_services.py:35-37.

`VRAM_ARGV` is derived from the loaded config, so no other test changes are needed. Risk is negligible: no code reads the list positionally. The design-doc line 651 correction is a spec edit, so route it to the spec owner or DECISIONS log instead of this card.

If the controller would rather keep the card's file list strict, open a follow-up card now and mark it blocking for the first real-GPU run (T08-18 swap / Phase 4).

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None in the card's own files. C1 (config/resilience.yaml:50, tests/unit/core/fixtures/resilience.yaml:46) is an Important product defect outside this card's Files list. It is recommended to be fixed here (see above).

#### Minor (Nice to Have)
- **M1** tests/unit/core/jobs/test_jobs_gpu_services.py:94: the stop timeout is asserted as 120, which equals `COMPOSE_UP_TIMEOUT_S`. A mutation that uses the constant instead of `R.gpu.stop_timeout_s` survives. Pin a `GpuSettings` with, for example, `stop_timeout_s=90` in one case.
- **M2** herness/core/jobs/gpu_services.py:113: the `^[a-z][a-z0-9-]{0,63}$` regex check is never the deciding check. The allowlist rejects every tested bad name first, and the removal mutation survives. Add one case with a `model_construct`-built GpuSettings whose class holds a malformed key (e.g. `"x;y"`), and assert ConfigError with no argv. This keeps the defence-in-depth branch honest.
- **M3** tests/unit/core/jobs/test_jobs_gpu_services.py:399-402: `test_ut08_89_large_body_read_up_to_64_kb` only asserts `True`. Removing the 64 KB cap (gpu_services.py:223-224) survives. To make the test prove the cap, spy on `response.iter_bytes` or count the bytes read, or serve a body that never ends and assert the call still returns.
- **M4** herness/core/jobs/gpu_lock.py:61,78: nothing tests that the file is closed when locking fails, or that `__exit__` actually unlocks. Both mutations survive. Possible fixes: monkeypatch `_lock` to raise OSError and assert the handle is closed, and spy on `_unlock`.
- **M5** herness/core/jobs/gpu_services.py:25 / classify.py:86: the cross-module private import and the misattributed `redact_failed` event name (see deviation 5). Cosmetic.

### Assessment
**Task quality:** Approved

**Reasoning:** All four units match the spec. Argv lists, timeouts and messages are exact, the security checks are covered and their mutations are killed, and coverage is 100 %/100 % with all gates green. The remaining items are minor test-strength gaps. C1 is a real, confirmed shipped-config bug outside the card's files, and fixing it alongside this card is recommended.
