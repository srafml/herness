# T02-18 review: Build handler and build stage

Reviewer: verify agent. Worktree agent-a2c7ca69f69fbb045, head b1d3ce2, base de08abc. Read-only; tree left clean.

## Runs
- `PYTHONUTF8=1 uv run pytest tests/unit/model tests/integration/model tests/security/test_st02_build.py -q -p no:logging` (with branch coverage): 280 passed, 1 skipped (symlinks).
- Coverage (branch): build.py 99 %, _build_support.py 99 %, meta.py 100 % (>= 90/85).
- Gates: ruff check clean; ruff format --check clean (634 files); mypy clean (249 files); lint-imports 13 kept, 0 broken; check_module_size ok; check_type_ownership ok.
- Mutation probes: blocked by the session permission classifier (in-place edits refused), so none were run. I checked by reading the tests instead. Each of these mutations would fail an assertion: deleted_ids=[] fails `test_it02_21_deletions_are_honoured`; a fixed config_hash or empty watermarks fails `test_it02_21_lake_small_build`; `from exc` fails `error.__cause__ is None`; dropping status="failed" or swallowing the error fails IT02-22; removing the in-range yield fails IT02-27. Removing `fault_point("build.mid_sql")` would not fail any test (FT02-03 belongs to a later card).
- memory_limit probe (read-only python): "99999999999GB" and a 400-digit "1…GB" pass `resolve_memory_limit`; DuckDB then rejects them at connect ("Memory value out of range").

## Spec conformance
| Row | Status | Notes |
|---|---|---|
| U02-88 dataset_kind | ✅ | |
| U02-89 git_sha | ✅ | env regex, argv list, 5 s timeout, TimeoutExpired/OSError -> unknown + WARNING event |
| U02-90 insert_build_row | ✅ | parameterised, sort_keys JSON, existing row -> SchemaViolation |
| U02-91 update_build_row | ✅ | fixed SET fragments, ConfigError on conflict / no change, SchemaViolation on missing row / duckdb.Error |
| U02-92 collect_row_counts | ✅ | parameterised catalog query (current_database()), regex-checked identifiers, views excluded, sorted |
| U02-95 STAGE_ORDER / Stage | ✅ | defined in _build_support, re-exported by build |
| U02-96 BuildPipelinePayload | ✅ | extra=forbid, strict=False, frozen; invariants a-e in model_validator |
| U02-97 run_sql_range | ✅ | 0<=lo<=hi<=999 and 400-499 overlap -> ConfigError; should_yield before each file; extract_statements; BuildSqlError(build_id, file, index, sanitised) `from None`; sql_file_done fields; heartbeat; fault_point("build.mid_sql") |
| U02-98 run_build_pipeline | ✅ | steps 1-8 and the failure path follow the spec. cleanup_builds is replaced by the `# T02-21:` stand-in (ruling); llm_factory is typed `object` with a `# T02-19:` marker |
| U02-99 _stage_build | ✅ | order: open(create) -> scan_lake -> 000-099 -> insert_build_row(config_hash(cfg), list_watermarks) -> deleted ids + refdata -> 100-299 -> row_counts -> CHECKPOINT |
| UT02-65 | ✅ | all spec cases plus extras; memory-limit tests filed under the same ID |
| UT02-67 | ✅ | env / git present / PATH empty, plus the failure modes |
| UT02-69 | ✅ | |
| IT02-21 | ✅ | runs through register_handler -> resolve_handler -> run_handler on a real tmp warehouse; golden counts plus a hashed, sorted core.* snapshot (12 tables); meta.build fields, state, heartbeats and metrics checked |
| IT02-22 | ✅ | BuildSqlError names 230_incident.sql, index 2, status failed, CURRENT unchanged, no literal in error, context, logs or cause |
| IT02-27 | ✅ | yield after 3 files -> no `build` in state; the second run makes a new ID and deletes the orphan (logged); resume rules covered |
| ST02-12 | ✅ | loopback-only socket guard (proven to block example.com), recording audit hook sees 0 socket events, every duckdb.connect (writer, inspector, reader) has autoinstall/autoload false; guard reset by ops_store teardown |
| ST02-14 | ✅ | sentinels in text and a failing cast, then a SQL literal failure; sentinels absent from logs, meta.* (build/dq_result/evidence) and BuildSqlError; db_error shows '?' |
| Test IDs / pytestmark | ✅ | unit / integration markers correct; names follow test_<id>_… |
| Budgets / §2 | ✅ | meta 182/240, build 368/400, _build_support 162/200 with a §2 module-map row added |

## Specific checks
1. register_handler / resolve_handler / run_handler used in IT02-21/22/27: ✅
2. meta.* config_hash = config_hash(cfg), watermarks from ops.list_watermarks(); nothing comes from the payload: ✅ (build.py:234-245)
3. Deleted IDs registered after 000-099 and before 100-299 (build.py:247-254). Facts are not in this card. Test shows the deleted record is absent from core.incident: ✅
4. No SQL is built from payload strings. Identifiers come from fixed lists or regex-checked catalog names. Sentinels are absent: ✅
5. memory_limit rejects nonsense, 0 %, 101 %, and values under 256 MiB, and percent is bounded to 1-100. There is no upper bound on absolute sizes: ⚠️ (see Minor 1)
6. Failure path: status failed + finished_at, mark_failed errors logged and suppressed, metrics(status=failed), re-raise, CURRENT untouched, no BEGIN anywhere (autocommit): ✅. Only HernessError is handled (see Minor 2)
7. Real tmp warehouse, strict guard + audit hook, meaningful goldens: ✅
8. Unit specs verbatim, IDs, marks, budgets, coverage: ✅
9. Fast gates: ✅

## ⚠️ Items (no action needed for this card)
- Mutation probes could not be run (permission classifier). The table above rests on reading the assertions.
- The acceptance check "`herness build` on lake_small" cannot run: the T09-23 CLI does not exist yet. IT02-21 asserts the same result.
- The orphan-deletion stand-in only removes `building` builds with no finished_at. A partial file with no meta.build row (yield or crash inside 000-099, status `unreadable`) is left behind until T02-21's cleanup_builds adds the unreadable rule. The `# T02-21:` marker covers this.
- `fault_point("build.mid_sql")` and the fields of `model.build.sql_file_done` / `model.build.stage_done` are in the code but no test asserts them (FT02-03 is a later card).
- build.py is at 368/400. The later cards T02-19..21 need a sibling module, as the builder notes.

## Findings
Critical: none.

Important: none.

Minor:
1. herness/model/_build_support.py:62-66: absolute sizes have no upper bound. "99999999999GB" and a 400-digit value (float -> inf) pass `resolve_memory_limit`. DuckDB then fails at connect, and `warehouse._open_error` reports it as `SchemaViolation("cannot open warehouse …")` instead of a `ConfigError` naming build.memory_limit. A large size DuckDB does accept (e.g. "100000GB") is passed through above physical RAM. Suggest capping at total RAM, or a sane maximum, with ConfigError.
2. herness/model/build.py:258 and 321-325: `con.execute("CHECKPOINT")` is not wrapped, and the stage error handler catches only HernessError. A raw duckdb.Error there, or any non-Herness exception such as OSError from scan_lake, skips the failed marking, `model.build.failed` and the failed metrics, and carries raw DuckDB text to the job layer. Suggest wrapping CHECKPOINT in SchemaViolation or BuildSqlError.
3. herness/model/build.py:272-273: the payload ConfigError message lists pydantic `loc` entries, which include payload-supplied key names for `extra` fields (capped at 200 chars). These are not row values, so this is outside TH02-14, but it echoes caller text into the job error. You could replace unknown keys with a fixed "extra field" label.
4. herness/model/meta.py:175-176: `collect_row_counts` silently skips tables whose names fail the identifier regex. That is safe, but the later drop check could miss such a table. A DEBUG/WARNING log would make it visible.

## Verdict
Approved (the Minor items are optional follow-ups; Minor 1 and 2 are cheap to fix now if the sub-controller prefers).

## Re-review 1 (fix round 1, head c435761)

Runs: card tests 286 passed, 1 skipped (symlinks). ruff check and ruff format --check are clean on herness/model and the touched tests. mypy is clean (249 files). check_module_size ok: build.py 381/400, _build_support.py 166/200, meta.py 187/240. Tree clean.

| Minor | Status | Evidence |
|---|---|---|
| 1 memory_limit upper bound | ✅ closed | _build_support.py:60-70 now requires a finite size from 256 MiB to total physical RAM (NaN when unmatched). UT02-65 rejects "65GiB" (64 GiB box), "99999999999GB" and "9"*400+"GB" (inf). The percent path is unchanged |
| 2 non-Herness errors skip failure path | ✅ closed | build.py:333-338 turns duckdb.Error/OSError into `SchemaViolation("build stage <name> failed", error_type)` `from None`, runs `_fail` (marks failed, logs `model.build.failed`, writes metrics) and re-raises. IT02-22 is parametrised on duckdb.IOException and OSError: no literal in the error or logs, `__cause__` None, status failed, failed metric written. CHECKPOINT is covered by the same handler |
| 3 payload keys echoed | ✅ closed | build.py:267-271 `_error_label`: extra_forbidden becomes "extra field"; other locations show only a declared model field name, else "payload". New IT02-21 test confirms the key is not echoed |
| 4 silent skip in collect_row_counts | ✅ closed | meta.py:173-186 logs one `model.build.row_count_skipped` WARNING with the schemas and a count, never the names. The test asserts count 1 and that "Bad Name" does not appear |

New findings: none. Note that a machine with less RAM than an explicit configured size now fails with ConfigError. That is intended and the message names build.memory_limit.

Re-review verdict: **Approved**.
