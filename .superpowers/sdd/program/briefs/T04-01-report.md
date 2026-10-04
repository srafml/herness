# T04-01 report: Result hash and encoding

## Fix round 2 (commit `b5871bb fix(metrics): fail closed on path-dependent UNION members and types (T04-01)`)

### I-3: multi-member UNION
- **What changed.** In `_encode_nested._union`, a UNION with several members now raises `SchemaViolation("unsupported column type <t>: fetchall and Arrow values differ")` whenever any member type is one of:
  - HUGEINT, UHUGEINT or INTERVAL;
  - `DECIMAL(...)`;
  - STRUCT, MAP or UNION;
  - a list or array;
  - one of the unsupported types below.
- **Why.** The decision is made from the member types, not from the value's Python type, so fetchall and Arrow now always agree. Both raise.
- **Other cases.**
  - A UNION with one member is still encoded by that member's type.
  - A UNION whose members are all path-independent is still encoded by Python type. Examples of such members: integers other than HUGEINT, VARCHAR, TIMESTAMPTZ.
- **Tests.** The synthetic `test_ut04_01_ambiguous_union_fails_closed` is replaced by two tests that run real DuckDB queries:
  - `test_ut04_01_path_dependent_types_fail_closed_on_both_paths` checks that the fetchall value and the Arrow value both raise for:
    - `UNION(h HUGEINT, s VARCHAR)`, the UHUGEINT, INTERVAL, `DECIMAL(9,2)`, `INTEGER[]` and MAP variants, and a list-wrapped HUGEINT UNION;
    - BIT and BIGNUM, and BIT inside a STRUCT;
    - TIMETZ, and TIMETZ inside a list.
  - `test_ut04_01_stable_multi_member_union_encodes_equally` checks that a `UNION(n INTEGER, s VARCHAR, t TIMESTAMPTZ)` encodes equally from both paths.
- **Red evidence.** I checked out the round-1 `_encode*.py` and ran the new tests: `Failed: DID NOT RAISE SchemaViolation`, 1 failed. With the fix: all pass.

### M-8: BIT and BIGNUM/VARINT fail closed
- **The mismatch.** On DuckDB 1.5.5:
  - fetchall gives BIT as `'101'`, but Arrow gives the internal bytes `b'\x05\xfd'`.
  - fetchall gives BIGNUM (VARINT reports as `BIGNUM`) as decimal text, but Arrow gives an opaque extension type backed by DuckDB-internal bytes.
- **Fix.** `UNSUPPORTED_TYPES` = {BIT, BITSTRING, BIGNUM, VARINT, TIME WITH TIME ZONE, TIMETZ}. These raise at any depth: top level, or as a STRUCT, list, MAP or UNION member.
- **Why not type rules.** The spec has no rules for these types. Decoding DuckDB's internal BIGNUM bytes would couple the hash to a storage format.

### M-9: TIMETZ fails closed
- **Why the offset can't be included.** Arrow exports TIMETZ as `time64[us]` and drops the offset: 12:00+02 comes back as 12:00 with no timezone. The offset therefore cannot be included on the Arrow path.
- **Fix.** TIMETZ fails closed on both paths, as part of `UNSUPPORTED_TYPES`. Metric templates never select TIMETZ.

### M-10
- `tests/support/__init__.py` is added, with a docstring only.
- Repository convention: the test directories have no `__init__.py`. `tests/support` is an importable helper package (`python -m tests.support.result_hash_vectors`), so this does not conflict with pytest collection.

### Updated spec delta against U04-01 step 14 (replaces the round-1 wording)
- STRUCT, MAP and UNION members are encoded by the member types read from the type string.
- A UNION with several members is encoded by Python type only when every member type is path-independent. Otherwise the cell raises `SchemaViolation`.
- The types BIT, BIGNUM/VARINT and TIME WITH TIME ZONE raise at any depth.
- Step 14 by Python type still applies to other types that have no type-driven rule. For those, dict keys are written as `json.dumps(str(key))`.
- The wrapped conversion errors now also include `LookupError`.

### Vectors
- V01 to V20 are byte-identical. I re-ran `python -m tests.support.result_hash_vectors` and the fixture did not change.

### Gates (b5871bb)
- `ruff format --check`: 41 files formatted.
- `ruff check`: clean.
- `mypy`: 18 files, no issues.
- `lint-imports`: 6 kept.
- `check_type_ownership`: exit 0.
- Unit and integration tests (not slow): 319 passed, 4 deselected.
- Acceptance `-k`: 133 passed. It was 134 in round 1: the one test I removed had 4 parametrised cases, and the two new tests replace them.
- Coverage: `_encode.py`, `_encode_nested.py` and `evidence.py` are each at 100% line and branch.
- Line counts: `_encode.py` 277/280, `_encode_nested.py` 127.
- **Environment note.** During this round, `uv run` failed because it also parses the main checkout's `D:\herness\pyproject.toml`, which currently contains merge-conflict markers at line 282. I ran all the gates with the worktree's own `.venv\Scripts\*.exe` tools instead. I did not touch the main checkout.

---

## Fix round 1 (commit `891b6ba fix(metrics): type-driven nested encoding and strict non-finite compare (T04-01)`)

### I-1: nested values now encode the same from fetchall and Arrow
- **What changed.** STRUCT, MAP and UNION values are now encoded by the member types read from the DuckDB type string. This logic lives in a new private module, `herness/metrics/_encode_nested.py` (116 lines).
  - Commas are split only outside parentheses and quotes.
  - Quoted STRUCT field names are handled, including `""` escapes.
  - Members are matched to fields by position, because `normalize_type` upper-cases field names.
- **Budget.** Per the controller ruling, `_encode.py` stays within budget (277/280 lines). The new module has no row in the spec module map. Proposed row: *"`herness/metrics/_encode_nested.py` | type-driven STRUCT/MAP/UNION member encoding | `encode_nested`, `member_types`, `split_top` | L3 | — | 120"*.
- **MAP.** The encoder accepts three input shapes:
  - a `dict` from fetchall;
  - a list of `(key, value)` pairs from Arrow;
  - the fetchall shape `{"key": [...], "value": [...]}`, which DuckDB returns when the key type is compound (a list, array, STRUCT, MAP or UNION, which Python cannot use as dict keys).
  
  Each key is encoded by the key type. If the key's token is not already a JSON string, the token is written as a JSON string. Example: `{"[1]":"a"}`.
- **UNION with one member.** Encoded by that member's type.
- **UNION with several members.** The value does not carry its tag. If the value is a `dict`, `list`, `tuple` or `Decimal`, the encoder fails closed with `SchemaViolation("ambiguous UNION member value <type> for column type <t>")`. Other scalars are still encoded by Python type.
- **Tests added:**
  - The UT04-01 parity test `test_ut04_01_nested_fetchall_and_arrow_encode_equally` covers the reviewer's 4 probes, plus nested HUGEINT MAP in a list, compound MAP keys, a two-member UNION list, a comma inside an ENUM, and an INTERVAL in a STRUCT.
  - New token cases and failure cases.
  - `test_ut04_01_ambiguous_union_fails_closed`.
- **Vectors.** V18 to V20 are added for nested values, giving 20 in total. V01 to V17 are byte-identical to the first commit.
- **Spec delta against U04-01 step 14.** STRUCT, MAP and UNION are now type-driven rather than encoded by Python type, and multi-member UNION values of ambiguous type fail closed. Step 14 by Python type now applies only to types with no type-driven rule, such as unknown types. For those, `dict` keys are written as `json.dumps(str(key))`. `LookupError` is added to the conversion errors that get wrapped.

### I-2: non-finite values in rows_equivalent
- If either value is non-finite, the cells match only when `x == y` or both are NaN.
- UT04-11 now asserts both directions for:
  - inf vs 1.0
  - inf vs -inf
  - NaN vs 1.0
  - 1e308 vs inf
- -inf vs -inf still matches.

### M-1: spec deltas for U04-07, for the Verifier (impl 05)
1. **Tie-break.** Rows are sorted by the spec's coarse key (`.6g` for numeric cells, `encode_cell` text otherwise), then by the exact `encode_cell` texts of all cells. The tie-break only orders rows whose coarse keys are equal.
2. **Non-finite values.**
   - A non-finite value matches only itself: inf==inf and -inf==-inf.
   - NaN matches NaN.
   - A non-finite value never matches a finite one.
   - The tolerance formula applies only when both values are finite.
3. **Unchanged.** Both NULL counts as a match; one NULL does not.

### M-2: test IDs
- `test_ut04_13_constants` is renamed `test_ut04_09_constants`, with docstring "UT04-09 (U04-13 part) …".
- The accumulator test is now `test_ut04_05_accumulator_sample_of_120_rows`. It checks that the accumulator returns 50 of 120 rows in ascending digest order (UT04-05, through U04-03), plus the limit bounds and that the accumulator cannot be reused after `finish`.

### M-3: integral DOUBLE in samples (no change; justification)
- U04-06 says a sampled float is "the value of their `.9g` token". The `.9g` token of 3.0 is `3`, so the sample holds the number 3.
- Samples are stored in `meta.evidence` as JSON text, where `3` and `3.0` are the same JSON number. A consumer that needs float type fidelity must use the column types in the header.
- Emitting `3.0` would change the pinned `result_hash` token rules (DD04-07), so I left it unchanged.

### M-4: PT04-11
- `b` is now a permutation of `a` with the DOUBLE cell of each row scaled by a relative delta drawn from {0, 1e-12, 1e-7, 1e-3}.
- The test asserts symmetry for this near-copy `b` and also for an independently drawn list. It also asserts reflexivity, and that a list is equivalent to its own permutation.

### M-7: generator committed
- The generator is now `tests/support/result_hash_vectors.py`. Run it with `uv run python -m tests.support.result_hash_vectors`. `tests/support/` is the spec §11 helper location.
- UT04-02 asserts that the fixture's `(id, name, sql)` rows equal the generator's `VECTORS`.
- The file carries `# ruff: noqa: E501`, because its SQL texts are frozen byte-for-byte in the fixture.

### Gates (after fix)
- `ruff format --check`: 39 files formatted.
- `ruff check`: clean.
- `mypy`: 17 files, no issues.
- `lint-imports`: 6 kept, 0 broken.
- `check_type_ownership`: exit 0.
- Unit and integration tests (not slow): 318 passed, 4 deselected.
- Acceptance `-k`: 134 passed.
- Coverage: `_encode.py`, `_encode_nested.py` and `evidence.py` are each at 100% line and branch.
- Line counts: `_encode.py` 277/280, `evidence.py` 207/390, `_encode_nested.py` 116 (not in the module map).
- `pyproject.toml` is not touched in this round.

### Process note
- **Not test-first.** The I-1 and I-2 code changes were written together with their tests. I did not record a separate red run.
- **Proof the tests fail without the fix:** each new parity probe asserts output the reviewer showed the old code getting wrong (`{"h":"5"}` from Arrow). The same holds for the new inf cases in UT04-11.

---

Status: DONE_WITH_CONCERNS
Commit: `f2b1819 feat(metrics): add result hash, encoding and golden vectors (T04-01)` (worktree branch `worktree-agent-a30cc2fcfdd6fba0c`)

## What was implemented
- `herness/metrics/__init__.py`: a package marker with a docstring only.
- `herness/metrics/_encode.py` (280 lines, budget 280):
  - `encode_cell` (U04-01), following steps 1 to 14.
  - `row_digest` (U04-02), which builds the row text directly so duplicate column names are kept.
  - `HashAccumulator` (U04-03): a bounded max-heap sample, a sorted digest body and a typed header.
  - `hash_arrow_batch` (U04-04): reads the IPC stream and rows through `evidence.iter_batch_rows`, imported lazily because of the import cycle, so the function is defined only in `evidence` (R-15).
  - Public helpers `normalize_type`, `INTEGER_TYPES` and `FLOAT_TYPES`, which `evidence` reuses.
- `herness/metrics/evidence.py` (206 lines, budget 390):
  - `result_hash` (U04-05), `result_sample` (U04-06), `rows_equivalent` (U04-07), `iter_batch_rows` (U04-08) and `canonical_params` (U04-09). `canonical_params` pre-converts values, then calls `herness.core.ids.canonical_json` (R-14).
  - The U04-13 constants, verbatim.
  - `RecordedQuery`, `IntoSpec` and `run_recorded` are left for T04-05.
- `tests/fixtures/result_hash_vectors.json`: 17 vectors (V01 to V17). Each vector has:
  - the DuckDB SQL;
  - the frozen `str()` column types (VI04-02, DuckDB 1.5.5);
  - the canonical row JSON in digest order;
  - `result_hash`.
- Tests:
  - `tests/unit/metrics/test_metrics_encode.py` covers UT04-01, UT04-02 (row_digest), UT04-03, UT04-05 (accumulator), UT04-12, ST04-12 and PT04-02.
  - `tests/unit/metrics/test_metrics_evidence.py` covers UT04-02 (golden vectors, checked via fetchall and via Arrow, plus an independent recompute from `row_json`), UT04-04, UT04-05, UT04-06, UT04-10 (canonical_params part), UT04-11, UT04-12 (iter_batch_rows), the U04-13 constants, PT04-01 and PT04-11.
  - `tests/unit/metrics/conftest.py` adds each docstring's hyphenated ID (for example `UT04-01`) as a `-k` keyword. Without it, the card's literal acceptance command (`-k "UT04-0 or ..."`, with hyphens) selects nothing.
- `pyproject.toml`:
  - `herness.metrics` is added as the top layer of "herness layers".
  - The C4 "core base is closed" contract is added: sources are the 7 core base modules, forbidden is `herness.metrics`. UT00-58 requires it as soon as a non-core top-level package exists.
  - A mypy override adds `ignore_missing_imports` for `pyarrow`, because pyarrow has no `py.typed` and the lock has no stubs.

## VI04-02 findings (DuckDB 1.5.5, pyarrow 25.0.1)
- Type strings: `DECIMAL(18,2)` (no space), `TIMESTAMP WITH TIME ZONE`, `VARCHAR[]`, `INTEGER[2]`, `STRUCT(a INTEGER, b VARCHAR)`, `MAP(VARCHAR, INTEGER)`, `ENUM('a', 'b')`, `UNION(num INTEGER)`. `REAL` reports as `FLOAT`. All of these are frozen in the vectors.
- `fetchall` and Arrow give different Python values for some types. The encoder handles each case so that both paths produce the same text (the U04-01 postcondition):
  - **INTERVAL:** `timedelta` vs Arrow `MonthDayNano`. The encoder converts `MonthDayNano` using 30 days per month, which matches DuckDB's timedelta.
  - **MAP:** `dict` vs a list of `(k, v)` pairs. The encoder turns the pairs into a dict.
  - **UUID:** `uuid.UUID` vs `str`.
  - **ARRAY:** tuple vs list.
  - **TIMESTAMP_NS:** `datetime` vs `pd.Timestamp`. Both are truncated to microseconds.
  - **HUGEINT:** `int` vs `Decimal`.

## RED / GREEN evidence
- **RED:** I wrote `_encode.py` first, then both test files before `evidence.py` existed. `uv run pytest tests/unit/metrics -q` gave 2 collection errors:
  - `ModuleNotFoundError: No module named 'herness.metrics.evidence'`
  - `ImportError: cannot import name 'evidence' from 'herness.metrics'`
  
  So the RED step was only partly test-first: the `_encode` tests never ran red against a missing `_encode`.
- **GREEN:** `uv run pytest tests/unit/metrics -q -p no:logging` gave 117 passed.
- **Acceptance:** `uv run pytest -m unit -k "UT04-0 or UT04-11 or UT04-12 or PT04-0 or PT04-11 or ST04-12" -q -p no:logging` gave 101 passed, 123 deselected. The underscore form gives the same result.
- **Full run:** `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` gave 220 passed, 4 deselected.

## Gates
- `ruff format --check`: 35 files already formatted.
- `ruff check`: all checks passed.
- `mypy`: no issues in 15 source files.
- `lint-imports`: 5 contracts kept, 0 broken.
- `tools.check_type_ownership`: exit 0.
- Coverage: `_encode.py` and `evidence.py` both at 100% line and 100% branch.

## Deviations and concerns
1. **ST00-10 test changed.** `tests/integration/repo/test_import_contracts_enforced.py` (an impl 00 test) failed once C1 listed `herness.metrics`. It assumed C1's text was exactly `layers = ["herness.core"]` and that C4 did not exist. I changed it to:
   - put `herness.harness` at the top of the first layers list;
   - add `herness.harness` to C4's `forbidden_modules` when C4 exists.
   
   The test still checks that both "herness layers" and "core base is closed" break.
2. **`encode_cell` step 14 goes beyond the spec's list.** To meet the fetchall/Arrow parity postcondition inside STRUCT, MAP and UNION values, step 14 also accepts `datetime.time`, `timedelta`/`MonthDayNano`, `uuid.UUID` and `bytearray`/`memoryview`. The spec says anything else raises.
   - Dict keys that are not strings (DuckDB MAP keys) are written as the JSON string of their encoded token.
   - Remaining gap: a MAP nested inside a STRUCT still encodes differently for fetchall (dict) and Arrow (pairs). Only top-level MAP and MAP-in-list are normalised.
3. **Helpers added beyond the spec text:**
   - `rows_equivalent` sorts by the spec's coarse key (`.6g` / encode text), then by the exact `encode_cell` texts as a tie-breaker. Without it, pairing depends on input order when coarse keys tie.
   - `rows_equivalent` treats NaN==NaN and inf==inf as passing; without that the function is not reflexive (PT04-11).
   - `encode_cell` raises `SchemaViolation("empty column type")` for an empty type string.
   - A naive value under a `TIMESTAMPTZ` column is treated as UTC.
4. **Acceptance selection:** the card's `-k` uses hyphenated IDs, but test names carry underscore IDs. I added the metrics `conftest.py` keyword hook instead of changing the names convention. The same issue may affect other cards.
5. **Vector generator not committed:** the fixture was generated by a one-off script in the scratchpad. The fixture's `description` field records the formula, and UT04-02 re-derives each hash independently from `row_json`.
6. **UT04-10 is partial:** only the `canonical_params` part is written, including the `query_id` equality through `herness.core.ids`. The `run_recorded` part belongs to T04-05.
7. **Report not at the requested path:** the report could not be written to `D:\herness\.superpowers\sdd\program\briefs\T04-01-report.md` because this worktree-isolated agent is refused writes outside the worktree. It is at the scratchpad path instead; copy it across.
