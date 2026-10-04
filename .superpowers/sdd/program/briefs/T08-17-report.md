# T08-17 report: GPU services and lock

Worktree: D:\herness\.claude\worktrees\agent-aae2a8f4e5c8fed0a (branch worktree-agent-aae2a8f4e5c8fed0a, base 750ec27).

## Built

- `herness/core/jobs/gpu_services.py` (287 lines, budget 360)
  - `ComposeRunner(gpu: GpuSettings | None = None)`: `up(cls, service)`, `stop(service)`, `kill(service)`, `ps()`.
    argv = `R.gpu.compose_cmd + ["-f", R.gpu.compose_file] + <tail>` exactly as U08-78; timeouts 120 (`COMPOSE_UP_TIMEOUT_S`),
    `R.gpu.stop_timeout_s`, 60, 30. `subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=t, check=False)`.
    Validation (ConfigError, no process started): service must be a key under `R.gpu.classes[*].services` AND match
    `^[a-z][a-z0-9-]{0,63}$`; `cls` must be a key of `R.gpu.classes`. FileNotFoundError -> `ModelUnavailable("compose unavailable")`;
    TimeoutExpired -> `"compose <verb> <service> timed out"`; rc != 0 -> `"compose <verb> <service> failed rc=<n>"` plus WARNING
    `jobs.gpu.compose_failed` (verb, service, rc, stderr redacted-then-cut to 500 by classify's `_redacted`, reused, not edited).
    `ps` parse: `[`-prefixed text = one JSON array, else one JSON object per non-empty line; needs str `Service` and `State`;
    anything else -> `ModelUnavailable("compose ps unreadable")`. Without pinned settings R.gpu is read at each call.
  - `LoopbackHttp.healthy(svc, *, timeout_s=5)` / `warm_up(name, svc, *, timeout_s)`: per-request loopback check (scheme http,
    host 127.0.0.1 / localhost / ::1, else ConfigError before any client is built); client from
    `herness.core.egress.loopback_http_client(svc.url, timeout_s=..., bearer=...)`, looked up on the module at call time (R-06);
    no httpx client is constructed. Bearer from `herness.core.secrets.resolve` (SecretStr handed to the factory, never logged).
    Redirects not followed (factory has follow_redirects=False), so 3xx is unhealthy. Body streamed, read up to 64 KB, discarded.
    Warm-up bodies verbatim from the U08-79 table; vLLM model = first `models.yaml` client with gpu_class reasoning
    (`get_config().models.models.clients`), bearer = that client's `api_key` when set; OpenJev bearer = `health.bearer_secret`.
  - `vram_used_mb()`: `R.gpu.vram_check_cmd`, 10 s timeout, first non-empty stdout line as int; any failure or rc != 0 -> None.
  - `wait_vram_free(*, threshold_mb, poll_s=2, timeout_s=60)`: `clock.monotonic` deadline, `clock.sleep(poll_s)`;
    a None reading is not free; timeout -> `ModelUnavailable("vram_not_freed")`.
- `herness/core/jobs/gpu_lock.py` (80 lines, budget 90): `GpuLock(path)` context manager; mkdir parents, open `a+b`,
  seek 0, `msvcrt.locking(fd, LK_NBLCK, 1)` on Windows / `fcntl.flock(LOCK_EX | LOCK_NB)` elsewhere (`# pragma: no cover`);
  OSError -> close and `ConfigError("another GPU owner holds data/locks/gpu.lock")`; exit unlocks and closes; a second exit is a no-op.
- `tests/support/fake_gpu.py` (229 lines): the `fake_gpu` fixture, registered with one line in `tests/conftest.py` `pytest_plugins`.
  `FakeGpu` replaces `gpu_services.subprocess` (a namespace with a fake `run`) and `gpu_services._gpu` (R.gpu) with the shipped
  `resilience.gpu` whose service URLs point at per-service stub loopback HTTP servers on ephemeral ports. It records every
  compose argv and kwargs, keeps service states (up -> running, stop/kill -> exited unless the service is in `sticky`), serves
  `ps` as JSON lines or an array (`ps_format`; `ps_stdout` overrides), injects failures (`fail[verb] = (rc, stderr)`,
  `raise_on[verb] = exc`), answers VRAM from the `vram_readings` queue (then 20 000 MB while any service runs, else 0), and
  keeps a `running_classes` history with `assert_single_class()` for the U08-81 tests. Stub servers record
  `(method, path, Authorization, JSON body)`; `answers[path]` sets the status, `payloads[path]` the body, and a 3xx redirects
  to `http://attacker.example:8000/`. `runner()` gives a real `ComposeRunner`; `http` is a `LoopbackHttp`.
- `tests/support/gpu_lock_race.py` (24 lines): spawn child target for UT08-95.

## Tests (80 card tests, all green; 100 % line and 100 % branch coverage of both modules)

- `tests/unit/core/jobs/test_jobs_gpu_services.py` (479): UT08-87 (exact argv lists and kwargs, default runner reads R.gpu,
  bad service names incl. `"x; rm -rf"` and bad classes -> ConfigError with no argv, missing compose, timeouts, rc failure
  logged redacted and <= 500 chars), UT08-88 (array, lines, shapes, empty, 9 garbage cases), UT08-89 (spy on
  `herness.core.egress.loopback_http_client`; bearer sent and absent from logs; non-2xx incl. 302 unhealthy; connection
  refused; missing secret; non-loopback incl. `http://10.0.0.1:8000` -> ConfigError with no client built; warm-up bodies of
  all three services; warm-up non-2xx and exceptions; no reasoning client; 1 MB body), UT08-90 (20 000 then 1 500 returns on
  the second poll; timeout after 60 fake seconds / 31 polls; None not free; vram_used_mb parse and failure cases).
- `tests/unit/core/jobs/test_jobs_gpu_lock.py` (78): UT08-95 with two real processes (multiprocessing spawn): the second is
  refused and succeeds after the first exits; the lock is released when the owner is killed; same-process second lock refused.
- `tests/security/test_st08_gpu.py` (108): ST08-01 (config rejects `openjev;calc`, `$(id)`, `../x` through the ServiceName
  allowlist; runner up/stop/kill refuse them and start no subprocess; argv is a list with shell=False), ST08-12 (config rejects
  `http://attacker.example:8000` and other non-loopback URLs; per-request refusal; a 302 to the attacker is not followed:
  one request and one connect, to 127.0.0.1 only).

RED evidence: `pytest tests/unit/core/jobs/test_jobs_gpu_lock.py` -> `ModuleNotFoundError: No module named
'herness.core.jobs.gpu_lock'` before gpu_lock.py existed. gpu_services.py was written before its test file (TDD order not
kept for that module); the first test run against it had 9 failures, all caused by the vram argv config issue below.

GREEN: `pytest tests/security/test_st08_gpu.py tests/unit/core/jobs tests/unit/repo/test_import_contracts_08.py` -> 182 passed
(UT08-103 incl. test_ut08_103_jobs_never_construct_httpx_clients green).

Gates: ruff format and check clean on the touched files (the repo check shows only the 7 known TID251 hits in openai_compat);
`mypy` (strict, whole tree): no issues in 224 files; `lint-imports`: 13 kept, 0 broken; `tools.check_module_size`: exit 0;
`tools.check_type_ownership`: pass. The pytest-unit commit hook fails only on the known-red ST10-25 (its hits are all in
openai_compat, checked with scan_tree), so commits used `SKIP=pytest-unit`. Because `fake_gpu` is now a repo-wide plugin, the whole
`pytest -m unit` set was run once (known-red ST10-25 deselected): 6081 passed, 11 skipped, 1 xfailed, 1 failed, the failure
being the known-red ST05-13(a) `test_llm_anthropic.py::test_st05_13_ast_lint_harness_builds_no_unguarded_clients`.

## Deviations and interpretations

1. `ComposeRunner` takes an optional pinned `GpuSettings` (the spec gives no constructor); by default it reads R.gpu per call.
2. `LoopbackHttp.healthy`: a bearer secret that cannot be resolved counts as an exception -> False (spec: "any other status
   or exception -> False"). The loopback URL check runs first and raises ConfigError.
3. The `llamacpp-large` warm-up sends `health.bearer_secret` when one is configured (none in the shipped config); the spec names none.
4. "Read up to 64 KB and discarded": the body is streamed and reading stops at 64 KB (a larger body does not fail the call);
   the factory's default 50 MiB ceiling still applies.
5. ConfigError messages do not echo the rejected name (attacker-controlled input).
6. stderr redaction reuses `herness.core.resilience.classify._redacted` (a private import, per the "reuse, do not edit" ruling).

## Carry-overs and concerns

- CONFIG BUG (not fixed; outside the card files): `config/resilience.yaml` line 50 (also design 08 §7 line 651 and the test
  fixture tests/unit/core/fixtures/resilience.yaml line 46) writes `vram_check_cmd: [nvidia-smi, --query-gpu=memory.used,
  --format=csv,noheader,nounits]` as a YAML flow list, so the commas split the last item: the loaded argv is
  `[..., "--format=csv", "noheader", "nounits"]`, which nvidia-smi rejects. `vram_used_mb()` would then return None forever
  and every swap would fail with `vram_not_freed`. Fix: quote the item (`"--format=csv,noheader,nounits"`). The tests take the
  argv from the loaded config, so they need no change when the owner fixes it.
- ST08-01 "in gpu_request": the JobContext `gpu_request` entry point (U08-86 / supervisor cards) does not exist on the base;
  it is covered here at the ComposeRunner level (ConfigError, no subprocess). The gpu_request case belongs to the supervisor card.
- ST08-09 (listed under U08-82 Tests) is not in this card's Tests row and was not written here.
- The OpenJev warm-up body is the O08-01 / (b)15 placeholder; the fake server accepts it until Phase 4.

## Fix round 1 (commit 245d880)

- C1: quoted "--format=csv,noheader,nounits" in config/resilience.yaml:50 and tests/unit/core/fixtures/resilience.yaml:46.
  New test test_ut08_90_vram_check_cmd_loads_unsplit pins the loaded argv of both files to exactly
  [nvidia-smi, --query-gpu=memory.used, --format=csv,noheader,nounits]; VRAM_ARGV is now that literal and the comment is updated.
  docs/specs/08 left alone. No impl 08 doc quotes the flow list (impl 10 line 1694 is a different, Python-quoted command).
  Side effect: UT08-03 test_ut08_03_cfg_default_equals_design compares the fixture with the design 08 §7 YAML block, which
  still has the unquoted item. Its reader _design_yaml (tests/unit/core/resilience/test_resilience_settings.py) now applies
  a "T08-17 C1" amendment next to its R-43 and R-53 ones. .secrets.baseline: one line-number update for that file
  (175 -> 178), no entry dropped, LF kept.
- M1: test_ut08_87_exact_argv_lists pins stop_timeout_s to 90 and expects timeouts (120, 90, 60, 30).
- M2: test_ut08_87_name_regex_decides adds keys "bad;name", "Openjev", "-x" and 65 chars through model_copy (no validation), so
  the allowlist accepts them and the regex is what rejects them: ConfigError, no argv.
- M3: test_ut08_89_large_body_read_up_to_64_kb serves 4 MB and counts the bytes httpx2 Response.iter_bytes hands out:
  64 KB <= total < 1 MB, and the last chunk is the one that crosses the cap. Without the cap the total would be 4 MB.
- M4: test_ut08_95_file_closed_when_lock_fails (the handle is closed when _lock raises), test_ut08_95_exit_unlocks_before_close
  (spy: _unlock gets the locked handle while it is still open, then the handle is closed), test_ut08_95_unlock_releases_while_handle_open
  (after _unlock alone, a new GpuLock succeeds while the first handle is still open).
- M5: parked, no change.

Tests: tests/unit/core + tests/security/test_st08_gpu.py + tests/unit/repo/test_import_contracts_08.py: 1706 passed, 1 skipped
(symlink privilege). Both modules still at 100 % line and 100 % branch. ruff and mypy (224 files) clean, check_module_size exit 0.
The commit used SKIP=pytest-unit because that hook failed only on the known-red ST10-25; every other hook passed.
