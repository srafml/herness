# T10-05 report: Audit log

Status: DONE_WITH_CONCERNS (audit.py is 391 lines; the budget is 360 and the ENG hard limit is 400)
Commit: 6e17b87 feat(core): add hash-chained audit log (T10-05)

## What was built
- `herness/core/audit.py` (L0): `AuditEvent`, `audit`, `log_lock`, `append_jsonl_locked`, `verify_chain`, `ChainReport`, `record_config_change`, `last_secret_set_times`, plus the private `_audit_locked`, `_known_values`, `_changed_paths` and `_last_line`.
- `pyproject.toml`: added `herness.core.audit` to the "core base is closed" `forbidden_modules` list (UT00-58 stays green).
- Tests:
  - `tests/unit/core/test_audit.py` (unit)
  - `tests/security/test_st10_audit.py` (unit)
  - `tests/fault/test_security_faults.py` (fault)
  - `tests/integration/core/test_audit_concurrency.py` (integration, about 3.6 s, not marked slow)

## Units -> tests
- U10-60 audit
  - test_ut10_57_chain_across_two_dates
  - test_ut10_58_invalid_input_writes_nothing
  - test_ut10_58_known_secret_and_credential_refused
  - test_ut10_58_known_values_import_rules
  - test_ut10_58_store_busy_becomes_fatal
  - test_ut10_58_config_hash_null_on_config_error
  - test_st10_18_known_secret_in_detail_refused
  - test_ft10_01_lock_held_elsewhere_blocks_the_audited_action
- U10-61 log_lock / append_jsonl_locked
  - test_ut10_57_append_without_chain_and_lock_held
  - test_ut10_57_previous_line_lookup
  - test_ut10_57_write_error_and_timeout_are_store_busy
  - test_ut10_57_thread_lock_timeout
  - test_ut10_57_two_processes_keep_one_valid_chain (integration acceptance check: 2 processes x 1,000 audit() calls, then verify_chain ok with 2,000 lines)
  - FT10-01
- U10-62 verify_chain
  - test_ut10_59_valid_and_tampered_chains
  - test_ut10_59_malformed_lines_and_unreadable_file
  - test_pt10_08_single_byte_mutation_detected
  - test_st10_17_tampering_breaks_chain_naming_the_line
  - test_st10_17_truncated_earlier_day_breaks_next_day
- U10-63 record_config_change
  - test_ut10_21_record_config_change
  - test_ut10_21_changed_paths_are_capped
- U10-64 last_secret_set_times
  - test_ut10_72_last_secret_set_times (the U10-64 half of UT10-72)

## Sub-controller rulings applied
1. `_known_values()` imports `herness.core.secrets` at call time. It returns frozenset() only on ModuleNotFoundError with name == "herness.core.secrets" and re-raises anything else. The fallback carries a `# T10-06:` comment, the CREDENTIAL check always runs, and the tests monkeypatch `_known_values`.
2. Metric: the call site carries the marker `# T08-05: herness_audit_lines_total{event} += 1`. There is no recorder import.
3. The U10-64 tests carry the ID UT10-72.
4. config_hash is memoised per config object in `_HASH_MEMO` (identity compare) and is null on ConfigError. There is no new config API.
5. D10-08: the OS lock is polled every 50 ms through the module-level `_monotonic`/`_sleep`. FT10-01 advances fake time by at least 10 s while a subprocess holds the lock through `log_lock`. The secret-set-like action did not run and no audit file was written; after the lock is released the action proceeds.
6. The two-process acceptance check is an integration test.
7. `admin_action` requires `action` and `target`, with `counts` and `detail` optional. Every other event must have exactly its listed fields. Validation messages are generic ("audit actor invalid", "audit fields invalid", "audit field would contain a secret") and echo no values or keys.
8. Every test name contains its ID, every docstring starts with the ID, and every file has a module-level pytestmark. ST10-17 tests `verify_chain`'s first_break; wiring it into the doctor `audit_chain` check is deferred to the herness.admin card (noted in the file docstring).
9. Coverage of audit.py is 99 % (253 statements, 0 missed; 3 of 60 branches partial). The platform lock branches (msvcrt / fcntl) carry `# pragma: no cover - platform branch`. mypy --strict is clean on Windows (sys.platform check).
10. Contract updated in the same commit.

## Deviations / interpretations
- **Log field name:** the log field `event` of `audit.write.failed` is emitted as `audit_event`, because structlog's positional `event` argument clashes with a keyword named `event` (TypeError). The `error_type` field is unchanged.
- **changed_paths cap:** "at most 200, then `+<n> more`" would give 201 entries, but a list field allows at most 200. I keep 199 paths plus `+<n> more` when there are more than 200 changes.
- **Old snapshot missing:** when the old snapshot file is absent, including on the first start (old_hash null), `changed_paths` is `[]`.
- **LAST missing:** when LAST is missing and the fallback audit scan finds the same hash, the function returns without rewriting LAST (spec step 3: equal -> leave).
- **First break on an edit:** an edited line is reported at the next line, whose prev_hash no longer matches. A deleted line or a swapped pair is reported at the first misplaced line. An unterminated tail older than 2 s is reported at its own line number; a younger one is ignored as a write in progress.
- **PT10-08 limit:** a hash chain cannot detect a change to the newest line, because no successor hashes it. The property covers every line that has a successor; the newest line relies on ACLs and backups (TH10-09 mitigations). The first line of the oldest file is covered through line 2.
- **Validation order:** the actor is validated before the event.
- **Timestamps:** `ts` is `clock.format_utc(clock.now())` and is taken inside the lock, so it is non-decreasing across processes. The file date is `clock.utc_day(now)`.
- **Directory creation:** `audit()` and `record_config_change` create `<paths.logs>` (and the snapshots dir) when it is missing.
- **CREDENTIAL detector:** built once at import from `build_detectors(RedactionConfig())`; the credential patterns do not depend on config.
- **Two lock waits:** the per-process thread lock also waits up to timeout_s (`acquire(timeout=)`), so the worst case is two waits in sequence.
- **Lock-file open errors:** an OSError while opening the lock file becomes StoreBusy("log write failed: <name>").

## Line counts
- herness/core/audit.py: **391** (budget 360, ENG limit 400). This is over budget after several compaction passes; the first draft was 494 once formatted.

## Gates
- ruff format --check: clean
- ruff check: all checks passed
- mypy: no issues (98 files)
- lint-imports: 12 kept, 0 broken
- check_type_ownership: exit 0
- check_module_size: **MS001 audit.py 391 > 360** (exit 1)
- Card tests: 21 passed (unit, security, fault and integration), and they also pass with --require-test-ids
- `pytest -m "(unit or integration) and not slow"`: 2499 passed, 4 skipped (symlink privilege), **1 failed**: `tests/integration/repo/test_check_scripts.py::test_it00_02_check_scripts_pass_on_repo`, which fails only because of the MS001 budget finding above
- tests/fault: 2 passed

## RED / GREEN
- RED: `pytest tests/unit/core/test_audit.py` -> `ImportError: cannot import name 'audit' from 'herness.core'`.
- GREEN: see Gates.

## Concerns
1. The budget for audit.py needs a ruling: raise it to 400 in impl 10 §2, or allow a helper module (spec §1 permits helper modules forced by size) such as moving U10-61 into `herness/core/_log_append.py`. IT00-02 stays red until then.
2. The `audit_event` log key deviates from the spec's `event` field name (a structlog constraint).
3. The PT10-08 limit on the newest line (see above).

## Fix round 1

Commit: c63cc17 fix(core): address T10-05 review round 1 (T10-05). This is a new commit on top of 6e17b87, not an amend.

### Review items -> fix -> regression test
- **Important 1 (fork at midnight):** `_audit_locked` now takes the lock itself, unless `lock_held` is set. Inside the lock it reads `clock.now()` once and uses that one reading for both the day file and `ts`. It then calls `append_jsonl_locked(..., lock_held=True)`.
  - Test: `test_ut10_57_rf_midnight_writer_appends_to_the_day_of_its_ts`. Another writer takes the lock just after midnight and writes first; the chain stays valid and every line sits in the file named for the date of its `ts`.
- **Minor 2 (list `action`):** the action check is now `str(fields["action"]) in _ACTIONS`, so a list gives SchemaViolation instead of TypeError.
  - Test: `test_ut10_58_rf_non_str_actor_and_list_action_rejected`.
- **Minor 3 (non-str actor):** the actor check starts with `isinstance(actor, str)`. `_validate` now types `actor` as `object`, so mypy does not flag the check as redundant.
  - Test: same as Minor 2 (a None actor and a list actor).
- **Minor 4 (malformed lines in `last_secret_set_times`):** lines whose `target` is not a str are skipped. A non-str `ts` already raises SchemaViolation inside `parse_utc`, which is suppressed.
  - Test: `test_ut10_72_rf_malformed_target_and_ts_are_skipped`.
- **Minor 5 (sequential waits):** one deadline is computed before the thread-lock acquire (`timeout=max(0, deadline - now)`), and the OS-lock poll loop uses the same deadline.
  - Test: `test_ut10_57_rf_thread_and_os_waits_share_one_deadline`. The thread lock uses 0.6 s of a 1.0 s budget, and the total fake wait stays below 1.0 s plus one poll interval.
- **Minor 7 (memo not thread-safe):** `_cached_hash` reads a snapshot of the memo, returns its local value and replaces the memo slot in one assignment.
  - Test: none new; the existing memo test still passes.
- **Minor 8 (raw OSError in the LAST fallback):** a new `_fatal_on_io(event)` context manager maps StoreBusy/OSError to a logged `audit.write.failed` and `FatalError("audit write failed: <event>")`. It is used by `_audit_locked` and wraps the `record_config_change` lock region, which covers the LAST fallback scan, the snapshot/LAST writes and a lock timeout.
  - Test: `test_ut10_21_rf_unreadable_audit_file_is_fatal`.
- **Minor 9 (duplicate monkeypatch line):** removed from `_chain` in `test_audit.py`.
- **Minor 6:** not fixed (parked by ruling).

### Budget change
The budget ruling is applied: the `herness/core/audit.py` row in impl 10 §2 goes from 360 to 395, and only that one cell was edited in the worktree copy.

### Line count
`audit.py` is **394** lines against the new budget of 395.

### Gates
- ruff format --check: clean
- ruff check: all checks passed
- mypy: no issues (98 files)
- lint-imports: 12 kept, 0 broken
- check_type_ownership: exit 0
- check_module_size: exit 0
- Card tests: 26 passed; audit.py coverage is 99 % (264 statements, 0 missed; 3 of 60 branches partial)
- `pytest -m "(unit or integration) and not slow"`: 2504 passed, 4 skipped, 1 xfailed. IT00-02 is now XFAIL for its pre-existing traceability marker; the MS001 failure is gone.
- tests/fault: 2 passed
