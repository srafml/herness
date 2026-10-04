# T10-11 report: Table redaction and fixture scanner

Status: DONE_WITH_CONCERNS
Commit: 949496c feat(core): add redact_table process pool and fixture PII scanner (T10-11)
Worktree: D:\herness\.claude\worktrees\agent-ac49eabcdb02adbd2 (base 6b70189)

## Continuation
A second builder picked up the first builder's uncommitted WIP after the first one hit a usage limit. I reviewed it against the brief and the w07-s10 rulings and kept it. Changes I made:
- renamed the type alias `Finding` to `_Finding` in redact_scan.py. The public-looking name failed type-ownership (OWN040), which in turn failed IT00-02 and IT00-01.
- the detect-secrets hook rewrote `.secrets.baseline`. The only changes are line numbers in docs/impl/10 (+1, from the new §2 row) and `generated_at`. No audited entries were dropped, and the file keeps LF endings.

## What was built
- `redact_table` (U10-47), public in herness/core/redact.py.
  - `workers` defaults to `sources.build.threads`. When that is None it falls back to `os.cpu_count() or 1`, and it is never below 1.
  - Rows are split into 20,000-row chunks. The work runs in-process for `workers == 1` or a single chunk. Otherwise it uses a spawn `ProcessPoolExecutor` with `_init_worker(profile, overrides, config_dir)`, which calls `init_config` and then `get_redactor`. At most 2*workers chunks are in flight.
  - Each text column is redacted on its own and the non-null results are joined with "\n\n". A row that raises `RedactionFailed` gets NULL text and is counted as failed (fail closed).
  - Logs: `redact.record.failed` (record_id and error_type only), `redact.table.completed` (rows, failed_rows, workers, duration_ms), and `redact.table.failed` on a crash.
  - `BrokenProcessPool` becomes `StoreBusy("redaction worker crashed")` and no partial table is returned.
  - A missing or non-string column (duplicate names count as missing) raises `SchemaViolation("redact_table: missing column <c>")`.
  - `get_redactor()` is called in the parent before any worker starts, so a missing key raises ConfigError instead of crashing a worker.
  - Metrics: `# T08-05:` marker only.
- The private sibling herness/core/_redact_pool.py holds chunking, the pool, `_init_worker`/`_redact_chunk` (module level, so spawn can pickle them), `worker_count`, `string_column` and `spawn_args`. Its impl 10 §2 module-map row is in the same commit, with a budget of 130.
- herness/core/redact_scan.py `main` (U10-49), behind the `__main__` guard at the end of redact.py (`sys.exit(redact_scan.main())`, imported inside the guard).
  - Uses argparse with a repeatable `--scan`.
  - Builds a Redactor with a zero key, default RedactionConfig and no directory.
  - Reads the denylist from `config/herness.yaml` merged with `config/profiles/synth.yaml`, relative to cwd, via `load_yaml_file` and `deep_merge`. A missing file contributes nothing; invalid YAML or a non-list denylist exits 2.
  - Scans the listed suffixes, and Parquet string columns (line = row, col = column index). A file over 50 MiB or an unreadable file is a finding at `0:0`.
  - Applies the spec 11 §5.1.4 allow rules. A denylisted domain matches as a label-bounded host suffix (case-insensitive) and reports as DENYLISTED_DOMAIN.
  - Output is `path:line:col TYPE` only, never the value. Exit codes: 0 no finding, 1 findings, 2 usage or input error.

## pyproject.toml
One minimal change: `herness.core.redact_scan` and `herness.core._redact_pool` are added to the forbidden_modules list of the import-linter contract "core base is closed". Every other herness.core module above the base (redact, redact_patterns, redact_directory, ...) is already listed there. The addition stops the base modules (logging, errors, types, ...) from importing the new modules. No other contract or mypy list needed a change.

## Files and line counts (budget)
- herness/core/redact.py: 370 (390)
- herness/core/_redact_pool.py: 118 (130, new §2 row)
- herness/core/redact_scan.py: 174 (200)
- docs/impl/10-config-security-deployment.impl.md: +1 §2 row
- pyproject.toml: +2
- .secrets.baseline: line-number shift only
- tests/unit/core/test_redact_table.py (231), tests/unit/core/test_redact_scan.py (223), tests/security/test_st10_redact_scan.py (59), tests/fault/test_security_faults.py (+FT10-07)

## Tests
- UT10-43: joined text, poisoned row NULL, failed_rows=1, log fields, no raw text in logs; SchemaViolation cases; large_string input; missing key raises ConfigError; worker-count defaults; spawn_args; _init_worker; single chunk runs in-process.
- UT10-44: 50k rows, workers=1 vs workers=4 through the real spawn pool, identical table.
- UT10-47: allowed-only tree over every suffix plus Parquet exits 0; findings print location and type with no values; Parquet row/column reporting; unreadable and too-large files; usage errors exit 2; invalid config exits 2; subprocess `python -m herness.core.redact --scan tests/fixtures` (the hook command) exits 0 with empty stdout; subprocess exit code equals the scan result.
- FT10-07: a killed worker (`os._exit` in a worker) raises StoreBusy, which is a RetryableError; no table is returned and there is no completed log.
- ST10-30: real-format SSN plus an email at a denylisted domain exits 1; a denylisted URL host is a finding.

## Gates
- ruff format and ruff check: clean. mypy: clean (145 files). lint-imports: 13 kept. check_module_size: exit 0. check_type_ownership: exit 0.
- `pytest tests/unit/core tests/fault tests/security -m "(unit or integration) and not slow"`: 1409 passed, 1 skipped (symlink privilege).
- Full not-slow suite with --require-test-ids: 3871 passed. The 2 failures (OWN040) are fixed; IT00-01 and IT00-02 re-run give 2 passed, 1 xfailed.
- All pre-commit hooks passed on commit.
- Coverage (card tests): _redact_pool 98%, redact.py 96%, redact_scan 99% (line+branch combined; branch misses 1/3/0).

## Timings
- UT10-44 (50k rows, workers=1 then workers=4 spawn): 16.8 s. This is under ~20 s, so it stays in the normal run and has no slow variant.
- Scanner on tests/fixtures (one file, result_hash_vectors.json): about 0.56 s wall time via `uv run`.

## Deviations / concerns (need a ruling)
1. The scanner has two allow rules that are not in the spec (marked "pending ruling" in redact_scan.py). Without them the hook exits 1 on the existing impl 00 fixture tests/fixtures/result_hash_vectors.json, which has 10 false positives:
   - PHONE on decimal fractions and fractional seconds (`0.333333333`, `1.10000002`, `00.123456789`, `59.999999-05`). Allowed when the value starts with `\d{1,2}\.\d{6}`.
   - CARD on UUID prefixes `00000000-0000-0000`. Allowed when the value starts with `0`, since no PAN has MII 0.
   The better fix is tighter PHONE/CARD detectors in redact_patterns.py, but that file is at 395/395. The fixture is hash vectors and should not be edited. Option: accept the rules as spec 11 §5.1.4 additions, or give redact_patterns.py room for a fix.
2. `spawn_args` passes `overrides=()` and `config_dir=<cached root>/config`, because config.py (315/320) stores neither. Workers therefore ignore `--set` overrides. Redaction settings are under file-only `security.*`, so the redactor is unaffected. A later config card could store the actual config_dir and overrides.
3. Workers resolve the HMAC key through their own secrets backend. keyring and dotenv work across spawn. A test-only in-memory keyring does not, so the pool tests use a dotenv config.

## Fix round 1 (review T10-11-review.md)
Commit: 73ff103 fix(core): narrow scanner allow rules and harden redact_table rows (T10-11), on top of a7e4b47. No egress files were touched.

Fixes:
- I1: the PHONE decimal allow rule is now a full match on `[0-9]{1,2}\.[0-9]{6,}` or `[0-9]{2}\.[0-9]{6,9}[+-][0-9]{2}`, ASCII digits only.
  - Negative tests (still findings): `30.123456 7890`, `12.345678-9012`, `41.555012 34`, `01.234567 89`, `12.345678+9012`, `415.555.0142`, `+44 20 7946 0958`, `(415) 555-0142`.
  - It still clears all fixture false positives: `0.333333333`, `1.10000002`, `00.123456789`, `59.999999-05`.
- I2: the CARD allow rule is now `_card_allowed`. It strips non-digits, then allows a number starting with `4111` or one with fewer than 12 digits left after the leading zeros are removed.
  - Negative tests: `04532015112830366`, `0 4532 0151 1283 0366`, `0-4532-0151-1283-0366`, `0378282246310005`, `0004532015112830366`, `5500 0000 0000 0004`.
  - The UUID prefix `00000000-0000-0000` is still allowed.
  - Extracting the helper also resolves ruff PLR0911 in `_allowed`.
- I3: when `Redactor.scan` raises RedactionFailed (text over MAX_TEXT_CHARS) on a line or a Parquet cell, the scanner reports the finding `<path>:<line>:<col> text too long to scan` with no value, and the exit code is 1.
  - Tests: a patched scan covering both a text line and a Parquet cell, and a real line of 4,000,001 characters.
- m1: `redact_rows` catches any `Exception` for a row (`# noqa: BLE001`, U10-47 step 4), so the row becomes NULL and counts as failed. Pool-level BrokenProcessPool still raises StoreBusy.
  - Test: a stand-in redactor that raises ValueError.
  - Note: the `redact.record.failed` log still reports `error_type="RedactionFailed"` for every failed row, whatever the actual exception class.
- m3: new test `test_ut10_43_pool_keeps_at_most_two_chunks_per_worker_in_flight`. It sets CHUNK_ROWS=2, uses 11 rows (6 chunks) and workers=2, and checks the result equals the workers=1 result. `_redact_pool.py` coverage is now 100%.
- m4: the single-chunk test now replaces `ProcessPoolExecutor` with a function that raises, which proves no pool is started.
- m6: `spawn_args` returns config_dir as `str` (ENG X-2). `_init_worker` takes `str` and passes `Path(config_dir)` to init_config.

Parked as instructed: m2 (private `config._Cache.root`), m5 (volume of per-row warnings).

Deviation status: the extra scanner allow rules are an accepted deviation only in this narrowed form (whole-decimal or fractional-seconds PHONE; CARD with fewer than 12 significant digits). The comment in redact_scan.py says so.

Line counts: redact_scan.py 188/200, _redact_pool.py 118/130, redact.py 370/390 (unchanged).

Gates:
- ruff format and check: clean. mypy: clean (148 files). lint-imports: 13 kept. check_module_size: 0. check_type_ownership: 0.
- `python -m herness.core.redact --scan tests/fixtures` exits 0.
- `pytest tests/unit/core tests/fault tests/security -m "(unit or integration) and not slow"`: 1484 passed, 1 skipped.
- Coverage: _redact_pool 100%, redact_scan 99%, redact 96%.
- All pre-commit hooks passed.
- UT10-44 took 8.9 s in this run.
