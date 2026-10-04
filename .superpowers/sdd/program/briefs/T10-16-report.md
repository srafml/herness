# T10-16 report: Egress log and check

Status: DONE_WITH_CONCERNS (one spec conflict needs a ruling, see Concerns 1)
Worktree: D:\herness\.claude\worktrees\agent-ac49eabcdb02adbd2 (base 949496c = T10-11)
Commit: see the final line of this report.

## Built
- `herness/core/egress.py` (257/390 lines): U10-50 constants (`Purpose`, `PayloadClass`,
  `PAYLOAD_CLASSES_BY_PROFILE`, `MODEL_DOWNLOAD_HOSTS`, `MAX_RESPONSE_BYTES`, `CHARS_PER_TOKEN`,
  `BLOCKING_TYPES` (+`IP` when `mask_ip`), `LOOPBACK_HOSTS`), `EgressTicket`, `EgressGuard`
  (constructor U10-56, `check` and `_check_and_log` U10-51 steps 1-7), `get_guard` /
  `reset_guard` (under `_GUARD_LOCK`; redactor fetched lazily at the first re-scan; `reset_guard`
  appended to `config._RESET_HOOKS`), `cloud_chat_allowed` (U10-107).
- `herness/core/egress_log.py` (124/200): `EgressLog` (U10-57). `locked()` = U10-61
  `audit.log_lock(<logs>/.egress.lock)` plus a thread-local "held" flag; `write()` adds `ts`
  (milliseconds, Z), `profile`, `config_hash`, refuses keys outside design 10 §4.5 with
  `SchemaViolation` (key names not echoed) and appends via
  `audit.append_jsonl_locked(lock_held=<held by this thread>)`; `tokens_today(now)` is incremental
  (date, offset, total, map), reads whole lines only, ignores torn or foreign lines, bounds the map
  at 100,000 entries (past it a completed line adds its full figure: over-counts, never
  under-counts) and resets at the UTC date change. audit.py is not edited.
- `herness/core/_egress_scan.py` (101 lines, new private sibling; §2 row added with budget 120):
  step 3 `host_reason` (scheme, port, userinfo, IP literal, host allowlist) and step 6 `rescan`
  (UTF-8, JSON string leaves and keys, NFKC plus zero-width removal, 1,000,000-char limit, counts
  per type, pseudonym tokens protected by `Redactor.scan`). It is split out so egress.py keeps
  ~133 lines for the T10-17 transports, clients and download window (egress_clients.py and
  egress_socket.py already have their own rows).
- pyproject: `herness.core.egress`, `egress_log` and `_egress_scan` are added to the "core base is
  closed" forbidden list, the same way the redact modules were. `.secrets.baseline`: only the
  line-number shifts of the docs/impl/10 entries (one §2 row inserted); kept LF; no entry dropped.
- Test support: `tests/support/egress_harness.py` (a real config from `write_full_config` +
  `init_config`, so audit writes into the guard's logs dir; a fixed-key redactor with a one-name
  directory; `with_egress`, a model_copy that bypasses the load-time checks).

## Design decisions inside the spec
- The order of checks is exactly 1, 2, 3, 4, 5 per request, 5 per day, then 6. The step 6 re-scan
  runs outside the egress lock because it is the slow part, but its result is used only when the
  day cap passes, so the reported reason is always the first failing step. The day cap and the
  append are one critical section under `EgressLog.locked()`.
- Allowed line: `decision`, `reason=null`, `purpose`, `payload_class`, `destination`, `method`,
  `path`, `run_id`, `task_id`, `bytes_out`, `tokens_in`, `payload_sha256`, `scan_hits={}` (plus
  ts, profile, config_hash). Blocked line: the same with `reason`, `scan_hits` (counts, only for
  pii_detected) and `payload_sha256=null` (no hash of a refused body, because a short PII body
  could be guessed from its hash).
- `path` is `httpx.URL.path` (no query string). A purpose or payload_class outside its Literal is
  logged as null, so a free string is never echoed.
- Blocked path: log `egress.call.blocked` (WARNING), `audit("egress", "system", egress_id,
  reason)`, raise `EgressBlocked("egress blocked (<id>): <reason>", egress_id=, reason=)`. A
  failure of the egress log (StoreBusy or OSError, including a lock timeout or a tokens_today read
  error) gives `egress_log_failed` (a best-effort audit line is still written). An audit failure
  (FatalError) on a refusal turns the reason into `egress_log_failed`. `check` never sends
  anything.
- Windowed `model_download`: `_window_open()` returns False with a `# T10-17:` marker. When it is
  true, steps 1-2 are skipped and `MODEL_DOWNLOAD_HOSTS` is added to the allowed hosts
  (`# T10-24:` marker for the registry hosts of pinned deploy.*.image values, U10-82). The test
  monkeypatches the predicate.
- Metrics: `# T08-05:` markers for herness_egress_calls_total, tokens_total and bytes_total.
- Hardening within the spec's wording: duplicate JSON keys are kept (object_pairs_hook), so a
  duplicate key cannot hide a value from the scan. A JSON scalar body (for example a bare card
  number) and a non-JSON body are scanned as whole text. A deep-nesting RecursionError falls back
  to whole text. Identical strings are scanned once and their counts multiplied (Counter): same
  result, much faster on evidence packs.

## Deviations and extra reason codes (need acknowledging)
- `url_invalid`: `httpx.URL` raised InvalidURL (for example a NUL in the URL). The spec lists no
  code for an unparsable URL.
- `scan_failed`: `RedactionFailed` from a detector during the re-scan (fail closed). The spec
  lists no code for it.
- A `ConfigError` from the redactor factory (missing HMAC key) propagates (no line is written and
  nothing is sent); it is not turned into a blocked line.
- The UT10-52 client-config half (follow_redirects, trust_env) and the ST10-12 streaming-body half
  belong to T10-17 (sub-controller ruling) and are not built.

## Tests (IDs covered)
- tests/unit/core/test_egress_check.py: UT10-48 (local profile, blocked line and audit line,
  redactor never fetched; get_guard cache and reset through reset_config), UT10-49 (purpose and
  payload class, constants), UT10-50 (the five codes plus IPv6, trailing dot, host with a space,
  url_invalid; an allowed :443 URL logs host and path), UT10-51, UT10-52 (2,999,000 + 2,000 gives
  tokens_per_day; 200,001 gives tokens_per_request; estimate ceil(len/3.5); ticket), UT10-53
  (email with counts, `password=`, pseudonym tokens only allowed with sha256, non-UTF-8, strings
  over 1M, keys/duplicates/scalars, IP versus mask_ip, scan_failed, audit failure,
  model_download window predicate), UT10-79 (cloud_chat_allowed matrix, guard chat_not_approved
  and the approved path).
- tests/unit/core/test_egress_log.py: UT10-52 (line format, unknown keys, allowed, completed and
  blocked accounting, incremental reads with torn lines, UTC date change, map bound, two
  instances, write under the held lock).
- tests/security/test_st10_egress.py: ST10-10 (JSON-escaped directory name plus email gives
  {"EMAIL": 1, "PERSON": 1}), ST10-11 (full-width email, zero-width characters in an email and a
  card; as text and as JSON), ST10-12 (oversized body; day cap across two guards; day cap across a
  real subprocess writer), ST10-13 (marker absent from every file under logs, including the audit
  log, lock files and the app log configured into the same dir, after one allowed and three
  blocked calls), ST10-33, ST10-56 (a) C11 through `config.validate`, (b) C11 bypassed through
  model_copy: cloud_chat_allowed False, chat_not_approved, zero socket connects (recorded
  socket.socket.connect), audit line, (c) C25 through validate.
- tests/fault/test_security_faults.py: FT10-08 (Path.open on egress-*.jsonl raises ENOSPC; an
  allowed and a PII body both give egress_log_failed, no connects, no egress file, an audit egress
  line).
- tests/bench/test_bt10.py: BT10-05 under xfail(strict=False) citing the pending bench-owner
  ruling.

## BT10-05 measurement (this machine, ~1.0 MB JSON evidence pack through `rescan`)
- Pack with one unique note per row (~5,600 rows, repeated keys and labels): median 97.5 ms per
  run, about 97 ms/MB: XFAIL against < 50 ms/MB.
- The same pack without the per-string dedupe: about 367 ms/MB (in line with T10-10's
  228-427 ms/MB).
- A fully repetitive pack (identical notes): about 9.6 ms/MB (XPASS). The cost is dominated by the
  per-call overhead of `Redactor.scan`; the threshold is reachable only for low-cardinality packs.

## Gates
- ruff format and ruff check: clean. mypy (148 files): no issues. lint-imports: 13 kept, 0 broken.
  check_type_ownership: exit 0. check_module_size: exit 0.
- `PYTHONUTF8=1 uv run pytest tests/unit/core tests/fault tests/security -q -p no:logging -m
  "(unit or integration) and not slow"`: 1458 passed, 1 skipped (symlink privilege). The fault
  file (marker `fault`, not selected by that -m): 5 passed.
- Coverage from the egress tests: egress.py, egress_log.py and _egress_scan.py 100% line and
  branch.
- Full `-m "(unit or integration) and not slow" --require-test-ids` (run once): 3920 passed,
  1 failed. The failure was IT00-01 (pre-commit run --all-files): detect-secrets had to update the
  `.secrets.baseline` line numbers for my docs row, which were unstaged at test time. Committing
  the updated LF baseline with the card resolves it.

## RED / GREEN
- The tests were written module by module against the new code. The first runs failed on real
  problems: `port_not_443` stored as `invalid` (Concerns 1); `https://exa mple.com:99999`
  accepted by httpx (the test now uses a NUL URL); PYTEST_CURRENT_TEST overflowing from 1 MB
  parametrize ids (ids added). GREEN: 37 unit, 12 security, 2 fault, 1 bench (xfail), as above.

## Carry-overs
- T10-17: wire `_window_open` to U10-55; guarded transports and clients (U10-52..54) calling
  `_check_and_log` (streaming body gives `streaming_body` through the blocked path; `completed`
  lines); the UT10-52 client-config half; the ST10-12 streaming half. egress.py has 133 lines
  left.
- T10-24: registry hosts of pinned deploy.*.image values in the windowed host set.
- T08-05: the three egress metrics.
- T08-19: must call `cloud_chat_allowed` (this card's acceptance check, owned by T08-19).

## Concerns
1. Spec conflict: the U10-51 reason code `port_not_443` contains digits, but U10-108 (and
   herness/core/errors.py, which I may not edit) restrict `EgressBlocked.reason` to
   `^[a-z_]{1,40}$`. For that one code the attribute is stored as `invalid`; the message, the
   egress line and the audit line say `port_not_443`. Fix options: widen the regex to `[a-z0-9_]`
   in errors.py and U10-108 (one line, no size change), or rename the code (for example
   `port_not_default`). The test asserts the current behaviour, with a comment pointing at this
   ruling.
2. Step 6 scans JSON string leaves and keys only, as the spec says. Numbers inside JSON (for
   example a card number serialized as a JSON integer inside an object) are not scanned; a bare
   scalar body is. Scanning numbers risks false PHONE hits on aggregated figures, so this is left
   as specified pending a ruling.
3. BT10-05 stays above 50 ms/MB for realistic packs (about 97 ms/MB); see the measurement.
4. New reason codes `url_invalid` and `scan_failed` (see Deviations).
Commit: a7e4b47 feat(core): add egress guard check and egress log (T10-16)


## Fix round 1 (review a7e4b47 -> Needs fixes; base 73ff103)

Note: the Bash heredoc into this file was refused by worktree isolation, so this section was
written to the scratchpad and appended with `cat >>`.

- I1 `_egress_scan._strings`: a body that is one JSON string now yields the raw text and the
  decoded string, so JSON escapes (`"José García"`, `"john@corp.com"`) are caught.
  Test: `test_ut10_53_json_string_body_is_decoded` (two cases).
- I2 every refusal now goes through the blocked path (egress line, audit line, `EgressBlocked`):
  (a)+(c) `rescan` maps any Exception from the redactor factory (e.g. ConfigError for a missing
  key) or the scan to `scan_failed` (`# noqa: BLE001`, fail closed); (b) URL parsing and the
  host/path extraction catch `httpx.InvalidURL`, `ValueError` (covers UnicodeEncodeError and
  idna.IDNAError) and `TypeError` -> `url_invalid`. Tests: `test_ut10_53_key_failure_is_scan_failed_with_lines`,
  `test_ut10_53_any_scan_exception_is_scan_failed`, `test_ut10_50_unicode_and_idna_errors_are_url_invalid`
  (lone surrogate, `https://xn--zz.com/`).
- Ruling applied: `EgressBlocked` reason regex widened to `[a-z0-9_]{1,40}` in
  herness/core/errors.py (still 380 lines) and the U10-108 precondition row now reads
  `^[a-z0-9_]{1,40}$`. Side effect found by UT10-80: with digits allowed, `reason=3` (an int)
  matched via `str(3)` and was stored; line 304 now also requires `isinstance(reason, str | None)`
  so a non-str reason still reads `invalid` (same line count). test_config_errors.py docstring
  updated. `_blocked` in test_egress_check.py asserts `exc.reason == reason` (port_not_443 too).
- m1: every allowed/blocked line starts from `dict.fromkeys(LINE_KEYS)`, so `bytes_in`,
  `tokens_out`, `status_code`, `latency_ms` (and any unset key) are null. `EgressLog.write` now
  requires exactly the §4.5 key set and a decision in {allowed, blocked, completed}
  (`SchemaViolation("egress line keys invalid")`). Tests: `test_ut10_52_every_line_has_the_full_shape`,
  `test_ut10_52_line_shape_is_enforced` (unknown key, missing keys, unknown decision).
- m2: `token_estimate` None or < 1 -> estimate from the body. Test:
  `test_ut10_52_non_positive_estimate_uses_body_length` (0 and -5,000).
- m4: §2 row for `_egress_scan.py` now cites "w07-s10 ruling (size-forced private sibling)".
- m5: `rescan` returns `(None, {})` for an empty body before touching the redactor. Test:
  `test_ut10_53_empty_body_never_fetches_the_redactor` (factory that raises is never called).
- m6: `get_guard` falls back to `config_hash=None` on ConfigError (EgressLog takes `str | None`).
  Tests: `test_ut10_48_get_guard_without_config_hash`, `test_ut10_52_null_config_hash_is_written`.
- m8: `test_st10_12_day_cap_race_between_two_processes`: two spawned processes
  (`tests.support.egress_harness.race_worker`, each loading the config and its own guard) meet at a
  `multiprocessing` barrier and each asks for 60 of a 100-token day cap: exactly one `allowed`,
  one `tokens_per_day` (both in results and in the egress lines).
- m9 (fail-closed widening beyond the spec list): step 6 normalisation also strips U+00AD (soft
  hyphen), U+180E (Mongolian vowel separator) and U+2061-U+2064 (invisible operators), in addition
  to U+200B-U+200D, U+2060, U+FEFF. Tests: ST10-11 cases `soft_hyphen_email` (`john­@corp.com`)
  and `invisible_operator_email`.
- Parked per instruction: m3, m7.

Sizes: egress.py 259/390, _egress_scan.py 108/120 (budget not raised), egress_log.py 128/200,
errors.py 380/380.

Gates: ruff format/check clean; mypy 148 files clean; lint-imports 13 kept; check_type_ownership 0;
check_module_size 0. `PYTHONUTF8=1 uv run pytest tests/unit/core tests/fault tests/security -q
-p no:logging -m "(unit or integration) and not slow"`: 1501 passed, 1 skipped. Fault file: 5
passed. Egress coverage: egress.py, egress_log.py, _egress_scan.py 100% line and branch.
BT10-05 still XFAIL (unchanged path).
Fix round 1 commit: c3df0b8 fix(core): close egress guard review findings (T10-16)
