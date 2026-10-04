# T10-05 review: Audit log (herness/core/audit.py)

Worktree head 6e17b87 (base 7fcdb90). Gates re-run by the reviewer: card tests 21 passed (6.8 s); coverage of audit.py is 99 % (253 statements, 0 missed, 60 branches, 3 partial; the platform lock branches are excluded by pragma); ruff check and format clean; mypy clean (98 files); lint-imports 12 kept, 0 broken. audit.py is 391 lines, within the 395 budget from ruling 5.

### Spec Compliance
- ❌ Issues found: one chain-correctness bug at the day boundary (Important 1). Everything else matches the brief or a sub-controller ruling.

Per unit:
- U10-60 audit / AuditEvent: ✅ with one exception (Important 1). The literal, the exact field sets (admin_action needs action and target, counts and detail optional), value types and limits, the action list, the actor regex, the secret check (list items included, CREDENTIAL always runs, ruling 1), the record keys, canonical JSON plus a newline, fsync, StoreBusy/OSError -> log `audit.write.failed` + FatalError("audit write failed: <event>"), the metric deferred with a `# T08-05:` marker (ruling 2), the config_hash memo that is null on ConfigError (ruling 4), and the `audit_event` log key (ruling 6) are all in place. Validation runs before the lock, so a refusal writes nothing. Error messages are generic and echo no values (ENG §3.4).
- U10-61 log_lock / append_jsonl_locked: ✅ The per-process threading.Lock is keyed by the resolved path. msvcrt LK_NBLCK locks byte 0 after an lseek. The wait polls every 50 ms up to timeout_s, then raises StoreBusy("log lock timeout: <name>"). Unlock, close and release run in LIFO order through an ExitStack. An OSError becomes StoreBusy("log write failed: <name>"). The backwards seek works: a file with a missing trailing newline gives its last terminated line, and an empty or partial-only file falls back to the newest earlier file in name order.
- U10-62 verify_chain / ChainReport: ✅ Files are read in name order. The first line of the oldest file may carry any prev_hash. Each line needs the exact key set, a valid `aud_`+ULID, a parsable ts that does not decrease (fixed-width format, so the string comparison is sound) and a hash link that may cross files. An unterminated tail is a break only when its mtime is at least 2 s old. An unreadable file is reported as `<file>:0`. Lines are numbered from 1.
- U10-63 record_config_change: ✅ The LAST fallback scans the audit lines newest first. The snapshot is written atomically when absent. changed_paths is computed from the diff and capped. The line goes out through `_audit_locked(lock_held=True)`, then LAST is written, all under the audit lock.
- U10-64 last_secret_set_times: ✅ Newest first, secret_set or secret_rotate only, stops early once every name is found (tests named UT10-72, ruling 3).

Per test row:
- UT10-21 ✅ Same hash twice, then a change; one new line; changed_paths == ["retention.traces_days"]; the snapshot exists; LAST is updated; the LAST fallback path is covered.
- UT10-57 ✅ First prev_hash is 64 zeros, the next-day first line chains to the previous day's last line, and the output is canonical JSON. Previous-line lookup, plain mode, lock_held and the StoreBusy mappings are also covered.
- UT10-58 ✅ Unknown key, missing key, bad actor, bad values, a known secret and a credential all raise SchemaViolation, and no file is created.
- UT10-59 ✅ Valid, edited, deleted, swapped and truncated (old and in-progress) chains, plus malformed lines and an unreadable file.
- PT10-08 ✅ Hypothesis, 150 examples, every line except the newest (see ⚠️).
- FT10-01 ✅ A real subprocess holds the lock through log_lock. Fake monotonic time advances at least 10 s with no real sleep. The result is FatalError, the backend was not written and no audit file exists. After release the action proceeds.
- ST10-17 ✅ at the verify_chain level: edit, delete, reorder and truncate each name file:line, and a truncated earlier day breaks the next day. The doctor wiring is deferred to the herness.admin card.
- ST10-18 ✅ `detail=<known secret>` raises SchemaViolation. The secret is absent from the message, context and details, and nothing is written.
- UT10-72 (U10-64 half) ✅
- Acceptance check (2 processes x 1,000 lines -> valid chain) ✅ tests/integration/core/test_audit_concurrency.py, 2,000 lines, verify_chain ok.

- ⚠️ Cannot verify or needs a spec note:
  - An edited line is reported at the next line (N+1), because line N still links correctly. That is inherent to a hash chain and is accepted as the reading of "the right file:line", but spec rows UT10-59 and ST10-17 should say so.
  - PT10-08 and TH10-09: a change to, or truncation of, the newest line (or trailing lines of the newest file at a newline boundary) cannot be detected by the chain. The spec property says "any line" and should record this limit.
  - changed_paths cap: 199 paths + "+<n> more" = 200 entries. The U10-63 text ("at most 200, then a final entry") conflicts with U10-60's list limit of 200. The resolution is sensible, but it is not among the rulings and needs a spec row update.
  - The ST10-17 doctor `audit_chain` FAIL itself is deferred to the herness.admin card.

### Strengths
- Tight, readable implementation: generic validation errors, validation before any I/O, the ts taken inside the lock, ExitStack cleanup.
- Real cross-process tests: FT10-01 uses a subprocess with fake time, and the acceptance test uses two writer processes.
- Good edge coverage: 9,000-byte lines across the 4 KiB seek chunk, empty files, a partial tail, the young versus stale tail, an unreadable file.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **Chain forks at the UTC day boundary under lock contention.** herness/core/audit.py:241 picks the file (`clock.utc_day(clock.now())`) before the lock is taken (lock at :243 -> :148), while `ts` and the previous-line lookup happen inside it (:229, :150). Sequence: A picks `audit-D` at 23:59:59.9 and waits for the lock. B picks `audit-D+1` after midnight, gets the lock first, and chains its line to the last line of `audit-D`. A then appends to `audit-D`, chaining to that same line. Result: file D+1 line 1 no longer links to the true previous line, so verify_chain FAILs on an untampered log (and A's line in file D carries a D+1 ts). Reproduced by the reviewer with a nested audit() that simulates B winning the lock: `ChainReport(ok=False, files=2, lines=2, first_break='audit-2026-09-24.jsonl:1')`. The acceptance test cannot catch this because both writers stay on one day. Fix: choose the path inside the lock from the same `now` that becomes `ts`. For example, let append_jsonl_locked take a path factory (or pick the path under the lock in `_audit_locked`), and have the previous-line lookup also consider files named later than the chosen one, or simply take the newest file. Add a UT10-57 case that simulates the midnight race.

#### Minor (Nice to Have)
2. herness/core/audit.py:193: `fields["action"] in _ACTIONS` raises `TypeError: unhashable type: 'list'` when `action` is a list, which `_valid_value` allows. It should be SchemaViolation("audit fields invalid"). Reproduced. Nothing is written, so this is Minor.
3. herness/core/audit.py:184: a non-str actor (for example None) raises TypeError from `re.fullmatch`, not SchemaViolation("audit actor invalid"). Reproduced.
4. herness/core/audit.py:387: `target in missing` raises TypeError when an admin_action line's `target` is a list, which audit() accepts. last_secret_set_times then crashes, although the spec says Errors: none. Reproduced. Guard with `isinstance(target, str)` (and the same for `action` at :382). `clock.parse_utc(record.get("ts"))` at :389 has the same problem with a non-str ts, since only SchemaViolation is suppressed.
5. herness/core/audit.py:97-113: two sequential waits (thread lock, then OS lock) can reach 2 x timeout_s, but the spec says lock wait <= 10 s (D10-08). Use one deadline, taken before `local.acquire`, for both waits.
6. herness/core/audit.py:131-136 and :150-154: when the current file ends in an unterminated partial line (after a crash), the next append is glued onto that fragment, which leaves a permanent break. Consider writing a leading newline when the file does not end with one, or document it.
7. herness/core/audit.py:206-216: `_HASH_MEMO` is not thread-safe. Two threads with different config objects can interleave between the list assignment and the `_HASH_MEMO[0][1]` read and get the other config's hash. Return the local `value` instead of re-reading the list.
8. herness/core/audit.py:311-330: an OSError from `read_bytes` in the LAST fallback scan (inside record_config_change) escapes as a raw OSError, while the spec lists Errors "as U10-60".
9. tests/unit/core/test_audit.py:285-286: duplicated `monkeypatch.setattr(a.clock, "now", ...)` line.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Everything else is solid and well tested, but the file is chosen outside the lock, so concurrent writers at UTC midnight can fork the chain and make verify_chain fail on an untampered log (Important 1, reproduced). The fix is small and needs one regression test. Findings 2-4 (TypeError instead of SchemaViolation) are cheap to fold into the same round.


## Re-review round 1 (6e17b87..c63cc17)

Scope: I1, minors 2, 3, 4, 5, 7, 8 and 9 (minor 6 is parked by ruling), the §2 budget cell, the line count, regressions, and the FatalError side effect in record_config_change.

Gates re-run by the reviewer on c63cc17: 26 card tests pass. Coverage of audit.py is 99 % (264 statements, 0 missed; 3 of 60 branches partial). ruff check and ruff format are clean, mypy is clean (98 files), check_module_size exits 0 and lint-imports reports 12 kept. audit.py is 394 lines, within the 395 budget.

| Item | Status | Evidence |
|------|--------|----------|
| I1 fork at midnight | ✅ Fixed | `_audit_locked` takes the lock itself, unless `lock_held`, and reads `clock.now()` once under it for both the day file and `ts` (audit.py ~247-253). `append_jsonl_locked` is called with `lock_held=True`, so the lock is not taken twice. The regression test `test_ut10_57_rf_midnight_writer_appends_to_the_day_of_its_ts` injects a second writer inside the lock after midnight, then asserts that the chain is valid over 3 lines and that each line sits in the file named for its ts date. This is the same scenario as my repro. |
| Minor 2 list `action` | ✅ Fixed | `str(fields["action"]) in _ACTIONS`. A list gives SchemaViolation("audit fields invalid"), which the test covers. |
| Minor 3 non-str actor | ✅ Fixed | An `isinstance(actor, str)` guard comes first. The test covers a None actor and a list actor. |
| Minor 4 malformed lines in last_secret_set_times | ⚠️ Partly fixed | A non-str `target` is skipped, and a non-str `ts` is suppressed (parse_utc raises SchemaViolation). Both are tested. The `action` half is **still open**: a line whose `action` is a list raises `TypeError: unhashable type: 'list'` at audit.py `fields.get("action") not in _SECRET_ACTIONS`, which I reproduced. audit() now refuses a list action, so only a hand-edited or foreign line can trigger this. Minor, not blocking. Fix: `isinstance(fields.get("action"), str) and ...`. |
| Minor 5 one deadline | ✅ Fixed | The deadline is computed before `local.acquire(timeout=max(0, deadline - now))`, and the OS poll loop uses the same deadline. The test checks that the total stays under timeout plus one poll. |
| Minor 7 memo thread safety | ✅ Fixed | The function reads a snapshot of the memo, returns its local value and replaces the memo slot in one assignment. The existing memo test passes. |
| Minor 8 raw OSError | ✅ Fixed | The new `_fatal_on_io(event)` wraps `_audit_locked` and the lock region of record_config_change (LAST fallback scan, snapshot/LAST writes, lock timeout). The test uses an unreadable audit file and gets FatalError("audit write failed: config_change"). Residual (Minor, optional): the two `folder.mkdir` calls in record_config_change sit before the wrapped region and can still raise a raw OSError. |
| Minor 9 duplicate line | ✅ Fixed | The duplicate line is removed. |
| §2 budget cell | ✅ | The docs diff is one row, and only the last cell changes (360 -> 395). |

Side effect, FatalError instead of StoreBusy from record_config_change: ✅ consistent. U10-63 lists its errors "as U10-60", and U10-60's errors are SchemaViolation and FatalError (StoreBusy -> FatalError, step 6). A lock timeout or an I/O failure now surfaces as FatalError("audit write failed: config_change") after logging `audit.write.failed` with no values. SchemaViolation from validation still passes through unchanged. The nested `_fatal_on_io` does not double-wrap or double-log: the inner one converts the error, and the outer one only catches StoreBusy and OSError. ConfigError from the `config_hash(cfg)` call before the lock is a pre-existing path the spec does not cover, and it is not a regression.

No regressions found.

**Re-review verdict:** Approved. Open, non-blocking Minors: the list `action` in last_secret_set_times (the rest of minor 4) and the mkdir calls outside `_fatal_on_io` in record_config_change. Both can be fixed in any later touch of audit.py.
