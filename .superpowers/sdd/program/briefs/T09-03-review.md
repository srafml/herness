# T09-03 review: chat tables and chat ops functions (impl 09)

Reviewed: worktree agent-aea5820621eef1094, base 8a073b4, head 5c2f386 (one commit). There was no build report, so this review is based on the diff, the tree and gates I ran myself. The worktree is clean after the review.

## Verdict: Needs fixes

One Important defect: `update_chat_message` breaks when `run_write` retries it (I reproduced this). Everything else meets the spec, and all gates are green.

## Spec compliance

| Item | Result | Note |
|------|--------|------|
| U09-43 `090_chat.sql` | ✅ | It has two statements: `CREATE UNIQUE INDEX IF NOT EXISTS chat_message_reply ... WHERE role = 'assistant'` and `ALTER TABLE chat_session ADD COLUMN summary_through_message_id TEXT`. It has no `CREATE TABLE`, and it is 11 lines (budget 20). |
| U09-44 `create_chat_session` | ✅ | It checks `^[0-9a-f]{32}$` and raises SchemaViolation on a mismatch. The id is `ses_`+ULID. It inserts one row and leaves title and summary NULL. A naive `now` raises SchemaViolation through `clock.format_utc`. |
| U09-45 `append_chat_message` | ✅ ⚠️ | `assistant` raises SchemaViolation with the exact message. Any other role outside user/system is refused too. The 20,000-character cap is enforced. The select, insert and session update run in one `run_write`. A missing session returns None. The title uses `coalesce` for user rows only. ⚠️ The title rule takes the first non-blank line (see Minor 1). |
| U09-46 `update_chat_message` | ❌ | The signature, `UNSET` defaults, "at least one field", enum/cap/type checks, fixed-order allowlisted SET, meta merge inside the transaction and `rowcount == 1` all match the spec. It fails on a `run_write` retry (Important 1). |
| U09-47 `upsert_assistant_placeholder` | ✅ | It checks that `reply_to` is a user row of the session, then selects or inserts in one `BEGIN IMMEDIATE`. A new row has `content=''`, `status='streaming'` and `meta={"reply_to":...}`. `now=None` becomes `clock.now()`. The unique index backs it up. |
| U09-48 `latest_user_message` | ✅ | `ORDER BY created_at DESC, message_id DESC LIMIT 1`. |
| U09-49 read functions and the two TypedDicts | ✅ | Primary-key lookups work. Sessions are ordered by `last_active_at DESC` (plus a `session_id` tie-break), with the limit clipped to 1..200. Messages are the newest N returned oldest first, clipped to 1..500. Bad JSON is read as `[]`/`{}` and logs WARNING `store.chat.bad_json` with `message_id` (and `field`). `ChatSessionRow` includes `summary_through_message_id`. |
| U09-50 `purge_chat` | ✅ | One transaction deletes the messages, then the sessions, and returns `(sessions, messages)`. It logs INFO `store.chat.purged` with `sessions`, `messages` and `before`. A naive `before` raises SchemaViolation. |
| U09-107 `find_assistant_message` | ✅ | One `read_one` with session, role and `json_extract` predicates that match the partial index. |
| U09-108 `count_user_turns` | ✅ | `count(*)`; an unknown session gives 0. |
| U09-109 `set_chat_summary` | ✅ | The cap check runs before the transaction and raises "chat summary too long". A missing session logs WARNING `summary_skipped` with `reason=no_session`. A message from another session raises SchemaViolation with the specified text. A repeat is a silent no-op. A stale call logs DEBUG with `reason=stale`. Otherwise both columns are updated, and `last_active_at` is left alone. The summary is never logged. |
| `__init__.py` re-exports | ✅ | One `# 09 chat` block after `# 02 migrate` (behind `# isort: split`), plus a matching `__all__` block. It contains re-export lines only. |
| UT09-36 | ✅ | `ses_` id, NULL title/summary/through, both times equal to now. Also bad user_ref, naive now, and a package re-export test. |
| UT09-37 | ✅ | Assistant refused; 20,001 refused and 20,000 accepted; title is the first line cut to 60; system and blank rows do not set a title; a missing session returns None. |
| UT09-38 | ✅ | Feedback-only update changes exactly `{feedback, feedback_note}`; meta merge keeps `reply_to`; missing row returns False; no field is refused; 12 invalid values are refused before any write; bad stored meta is handled. |
| UT09-39 | ✅ | Two calls return one row; default now; `reply_to` must be a user row of the session; the unique index blocks a duplicate. |
| UT09-40 | ✅ | With the same timestamp, the greater `message_id` wins (assistant rows ignored). |
| UT09-41 | ✅ | 300 messages give the newest 200 in ascending order; clipping works; sessions are ordered by activity and only the user's are listed; lookups; bad JSON reads as empty with a WARNING and no content in the log. |
| UT09-42 | ✅ | Only the inactive session and its 3 messages are deleted; counts are right; the exact INFO event is logged; a second purge returns (0,0); a naive `before` is refused. |
| UT09-104 | ✅ | Finds `msg_A`; `msg_B` gives None; the other session's row is not returned. |
| UT09-105 | ✅ | 3 user, 2 assistant and 1 system rows count 3; an unknown session counts 0. |
| UT09-106 | ✅ | Stored; repeat unchanged; stale ignored with the DEBUG event; another session's message raises SchemaViolation; 6,001 raises SchemaViolation (and 6,000 is accepted); no_session logs a WARNING. |
| IT09-23 | ✅ | A DB at 006 upgraded to 090 equals a fresh DB in normalised `sqlite_schema`. The upgrade applies only `("090_chat",)` and `schema_version()==90`. PRAGMA `index_list` shows `chat_message_reply` as unique and partial. `table_info` shows the column as TEXT, nullable, no default. The file has no CREATE TABLE and is ≤ 20 lines. |
| Naming, docstrings, pytestmark | ✅ | Every function is named `test_<id>_...` and its docstring starts with the ID. `pytestmark` is `unit` in the unit file and `integration` in the integration file. |
| Acceptance checks | ✅ | PRAGMA index_list and table_info, no CREATE TABLE, upgraded == fresh: all covered by IT09-23 and passing. |

### ⚠️ Cannot fully verify

- The title rule: see Minor 1. The spec is ambiguous when content starts with blank lines.
- Nothing asserts that `find_assistant_message` and `upsert_assistant_placeholder` actually use `chat_message_reply` (no EXPLAIN QUERY PLAN check). The predicates match the partial index, so the planner should use it.

## C8 controller ruling checks

- **No table re-creation:** `090_chat.sql` has no `CREATE TABLE`. It adds only the index and the column that 005 lacks. The tables and the indexes `chat_session_user`, `chat_session_active` and `chat_message_session` are not touched.
- **Naming:** `090_chat.sql` matches `_FILE_RE` `([0-9]{3})_([a-z0-9_]+)\.sql`.
- **Owner range:** version 90 falls in `(90, 99, "09")` of `MIGRATION_RANGES`, and the runner logs `owner=09 version=90`.
- **Order and gaps:** `_discover` sorts by version, so 090 runs after 006. The runner has no contiguity check, so the gap 007–089 is fine. `schema_version()` returns 90 after the run (checked in IT09-23).
- **Stale C8 text in impl 02:** impl 02 line ~4088 (the C8 row) still says 090 creates `chat_session`/`chat_message`. That is no longer true for owner 09 and can be closed for 09. The owner-07 part (070) stays open.

## C8 differences (impl 09 §4.1 and other impl 09 statements vs migration 005 as built, plus 090)

`chat_session`

1. `session_id`: the spec says PK, `ses_<ulid>`. 005 has PK only and no format CHECK. The prefix is enforced only by code (U09-44).
2. `user_ref`: both the spec and 005 have `length = 32`. U09-44 additionally requires lower-case hex. 005 does not check the character set, so a raw insert of 32 non-hex characters would pass.
3. `title`: the spec says ≤ 60 chars. 005 has no CHECK. The limit is enforced only by the code's truncation in U09-45.
4. `summary`: the spec says ≤ 6,000 chars. 005 has no CHECK. The limit is enforced only in code (U09-109).
5. `created_at` / `last_active_at`: the spec says fixed-width UTC. 005 adds the 27-char GLOB CHECK with a redundant `IS NULL OR` under NOT NULL. The two are consistent.
6. `summary_through_message_id`: 005 lacks it; 090 adds it as nullable TEXT with no default, as the spec says. It has no FK to `chat_message` and no format CHECK, and the spec asks for neither.
7. STRICT: the spec says nothing about it; 005 creates the table STRICT. This does not conflict with the spec.

`chat_message`

8. `message_id`: the spec says PK, `msg_<ulid>`. 005 has PK only and no format CHECK.
9. `session_id`: FK `ON DELETE CASCADE` in both. They match, and the ops connection sets `PRAGMA foreign_keys = ON`.
10. `role`: the CHECK is identical.
11. `content`: the spec says NOT NULL and ≤ 20,000 (enforced in code). 005 has NOT NULL and **`DEFAULT ''`**, which the spec does not mention. There is no length CHECK in the DB.
12. `status`: the CHECK is identical. Neither has a default.
13. `verified` / `feedback`: the spec writes `CHECK (x IN (...))` and 005 writes `CHECK (x IS NULL OR x IN (...))`. These are semantically equivalent.
14. `feedback_note`: the spec says ≤ 1000 chars. 005 has no CHECK. The limit is enforced only in code (U09-46).
15. `run_id`: no constraint in either. They match.
16. `query_ids`: the spec says JSON **list**, default `'[]'`. 005 has default `'[]'` and `CHECK json_valid` only. It has no `json_type = 'array'` check and no ≤ 500 entries check (code only). This is why U09-49 has the bad-JSON fallback.
17. `meta`: the spec says JSON **object**, default `'{}'`. 005 has default `'{}'` and `CHECK json_valid` only, with no `json_type = 'object'` check. By contrast, `review_item.payload` in the same file does check the type.
18. `created_at`: the 27-char CHECK, as in item 5.
19. STRICT: as in item 7.

Indexes

20. `chat_session_user (user_ref, last_active_at)`, `chat_session_active (last_active_at)` and `chat_message_session (session_id, created_at)` are identical in 005 and the spec.
21. `chat_message_reply`, `UNIQUE (session_id, json_extract(meta,'$.reply_to')) WHERE role='assistant'`, is not in 005 and is added by 090 as the spec says.

Spec-internal inconsistency

22. The opening paragraph of impl 09 §4.1 (line ~2846) says 090 "adds only the index `chat_message_reply`". The column table in §4.1, the U09-43 unit and the card say it also adds `summary_through_message_id`. The build follows U09-43 and the card, which is correct; the §4.1 opening paragraph should be corrected in the consistency pass.

None of these differences stops the functions from working: tests pass on the 005 schema, including STRICT typing, the 27-char timestamps, the FK cascade and the `user_ref` length. The caps the DB does not enforce (items 3, 4, 11, 14, 16, 17) are enforced in code, and the reads tolerate bad JSON as the spec requires.

## Edits to impl 02 tests (check 4)

These edits are legitimate and needed. They do not weaken impl 02's tests in any way that matters.

- **`test_store_ops_migrate.py` `mig_dir` fixture:** it now copies only files `<= 006`, and the count assertion changed to `*.sql == 6`. The runner tests need a fixed set of impl 02 files; otherwise every owner migration would change their expected `applied` tuples and versions. The new glob `*.sql` is stricter than the old `00[1-6]_*.sql`.
- **UT02-32 in `test_store_ops_migrations.py`:**
  - These assertions are relaxed: the `applied` prefix `[:6]`, the `schema_migration` rows `[:6]`, and version = last applied. This is necessary because the test migrates the real package.
  - The table-set check is still exact: `_TABLES_43 | {schema_migration}`. So a later owner migration that creates a table still fails UT02-32, which is exactly the R-11 guard.
  - The index set is still exact, via the added `_LATER_INDEXES`.
  - The added `all(int(name[:3]) >= 10 ...)` guards against an unexpected impl-02-range file.
  - Downside: each later owner that adds an index must edit `_LATER_INDEXES` in an impl 02 test. That is acceptable coupling and makes new indexes visible.

## Findings

### Critical

None.

### Important

1. **`update_chat_message` fails when `run_write` retries: the callback mutates captured state.** In `herness/store/ops/chat.py`, `update_chat_message.write()` (diff line 329), `values["meta"] = core.dump_json({**old, **new}, ...)` overwrites the captured `values` dict. `core.run_write` (`herness/store/ops/core.py:146-183`) rolls back and re-runs `fn` through `retry_call` when a `sqlite3.Error` is mapped to StoreBusy after `fn` has already run, for example a busy COMMIT. On the second attempt, `values["meta"]` is a `str`, so `{**old, **new}` raises `TypeError: 'str' object is not a mapping`. I reproduced this with a probe that makes the first attempt raise `OperationalError("database is locked")` after `fn` ran: the result was `ERROR TypeError 'str' object is not a mapping`. The effect is that a transient busy on a meta update becomes an unexpected TypeError instead of a successful retry or StoreBusy. ChatService calls this on the streaming hot path.
   - **Fix:** use a local variable, for example `params = dict(values)` inside `write()`, or compute `merged` without assigning back into `values`.
   - **Test:** add one to UT09-38 that monkeypatches `core.run_write` to run `fn` twice.

### Minor

1. **Title rule.** In `herness/store/ops/chat.py`, `append_chat_message` (diff lines 275-276), `content.strip().splitlines()` takes the first non-blank line, not the first physical line. The spec says "first line of `content`, stripped". This choice is defensible and tested, but it should be recorded as an interpretation or confirmed.
2. **`query_ids` coercion.** In `herness/store/ops/chat.py`, `_message` (diff line 234), `[str(q) for q in query_ids]` silently turns a stored non-string element such as `[1]` into `["1"]`. There is no `bad_json` WARNING, unlike the other malformed shapes.
3. **Weak default-clock assertion.** `tests/unit/store/ops/test_store_ops_chat.py`, `test_ut09_39_default_now_is_the_clock` (diff lines 882-889), compares against the wall clock truncated to the second. It does not use FakeClock or patch `clock.now`. It is weak but not flaky.
4. **Misplaced `type: ignore`.** `tests/unit/store/ops/test_store_ops_chat.py` (diff lines 1032-1037) puts `# type: ignore[index]` on the closing parenthesis line of a multi-line expression, not on the line mypy would report. Tests are not in the mypy scope, so this does nothing; it is tidiness only.
5. **Re-export test ID.** `test_ut09_36_package_reexports_chat_names` (diff line 1159) puts the package re-export check under UT09-36, which is about `create_chat_session`. That is acceptable, since the card has no dedicated ID for it.

## Layering, file edits and budgets

- **Imports:** `chat.py` imports only `from . import core` from the ops package; everything else comes from `herness.core.*` and the stdlib. `lint-imports` reports 13 kept, 0 broken, including `ops-areas-acyclic`.
- **`__init__.py`:** the `# 09 chat` block comes after `# 02 migrate`. `shared` and the other areas do not exist at base 8a073b4. On merge with the integration branch, which now has `# 01 ingest` (T01-04), 09 chat must come after 01 ingest, following the impl 02 area-table row order. Expect a trivial conflict.
- **`core.py`:** untouched; the diff stat lists only the 7 card and test files.
- **Budgets:** `chat.py` is 360/360, exactly at budget (hard limit 400). `090_chat.sql` is 11/20.

## Gate results (run by the reviewer at 5c2f386)

| Gate | Result |
|------|--------|
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 298 files already formatted |
| `uv run mypy` | Success, no issues in 128 source files |
| `uv run lint-imports` | 13 kept, 0 broken |
| `uv run python -m tools.check_type_ownership` | exit 0 |
| `uv run python -m tools.check_module_size` | exit 0 |
| Chat tests with coverage of `herness.store.ops.chat` (`--cov-branch`) | 52 passed; 100 % line (196/196), 100 % branch (38/38) |
| `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` | 3449 passed, 5 skipped (symlink privilege / no coverage.json), 15 deselected, 1 xfailed (IT00-02, known), 0 failed |


## Re-review 1 (fix commit e7c6a29)

This round covers Important 1, Minor 3 and Minor 4. Minors 1, 2 and 5 and the EXPLAIN item are parked by ruling.

### Verdict: Approved

| Item | Result | Evidence |
|------|--------|----------|
| Important 1: `update_chat_message` retry safety | ✅ Fixed | `write()` now builds `row_values = dict(values)` on every attempt and assigns the merged meta only to that local copy. The captured `values` dict is never changed inside the callback, so a second run of the callback merges from the original `meta` dict again. |
| New test `test_ut09_38_meta_merge_survives_retry` forces a second callback run | ✅ Confirmed | See "How the new test forces a retry" below. |
| New test would fail on 5c2f386's `chat.py` | ✅ Confirmed empirically | See "Proof against 5c2f386" below. |
| Minor 3: UT09-39 default now | ✅ Fixed | The test patches `clock.now` to a fixed microsecond stamp and asserts `created_at == stamp`. `chat.py` calls `clock.now()` through the module attribute, so the patch takes effect. |
| Minor 4: UT09-42 misplaced type-ignore | ✅ Fixed | The count check is now split into `left = core.read_one(...)`, `assert left is not None` and `assert left["n"] == 0`, with no ignore. |

### How the new test forces a retry

1. It patches the module global `core.connection`. `run_write.attempt()` looks that name up on every call, so each attempt gets the wrapper.
2. The wrapper passes everything through to the real connection, including `in_transaction`, except that the first `COMMIT` raises `sqlite3.OperationalError("database is locked")`.
3. `_rollback` rolls back through the wrapper. `_map_error` turns the error into `StoreBusy` because the text contains "locked", and `retry_call` then runs `attempt()` again, which calls `fn` a second time.
4. The test asserts `commits == ["COMMIT"]`, which proves the busy path ran. It then checks that the stored meta is `{"reply_to": "msg_u", "mode": "live"}` and that `status` is `done`.

### Proof against 5c2f386

I ran a scratchpad-only harness (`r1_probe.py`, which loads `git show 5c2f386:herness/store/ops/chat.py` as a separate module) through the same busy-first-COMMIT wrapper:

- Old `chat.py`: `ERROR TypeError 'str' object is not a mapping`. The UPDATE ran once; the second attempt failed at the merge. The new test would therefore fail on 5c2f386.
- New `chat.py`: returns `True` and stores meta `{'mode': 'live', 'reply_to': 'msg_u'}`. The UPDATE ran twice and there was one busy COMMIT.

### New findings

None. The fix commit also drops one blank line, before `_Unset`, which keeps `chat.py` at 360 lines. That is cosmetic.

### Gates (at e7c6a29)

| Gate | Result |
|------|--------|
| `chat.py` length | 360 lines, within the 360 budget (hard limit 400) |
| `tools.check_module_size` | exit 0 |
| `ruff check` | clean |
| `ruff format --check` | clean |
| `mypy` | 0 errors |
| Chat tests plus impl 02 migrate/migrations tests | 116 passed |
| Coverage of `chat.py` | 100 % line (197/197), 100 % branch (38/38) |
| Worktree | clean after the review |
