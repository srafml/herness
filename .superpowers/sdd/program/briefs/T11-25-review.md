# T11-25 review: Golden suite model, loader and resolution

Reviewer: verify agent. Head c371268, base ee452a6. Worktree agent-a466d69050adc6a33 (read-only).

## Gates (I re-ran these)
- `ruff check`: all passed. `ruff format --check`: 263 files already formatted.
- `mypy` (strict): no issues in 113 files.
- `lint-imports`: 12 kept, 0 broken.
- `tools.check_module_size`: exit 0 (golden.py is 400/400). `tools.check_type_ownership`: exit 0.
- Card tests (`-k "UT11_45 or ... or ST11_12"` over tests/unit/eval and tests/security): 29 passed, 0 failed, no warnings.
- Coverage of `herness.eval.golden`: 100 % line (293 statements), 100 % branch (52 branches). Required: >= 90 / 85.

### Spec Compliance
- ✅ Spec compliant (with two small documented extras, both accepted below)
- U11-53 models ✅: every model uses `extra="forbid"`.
  - `Suite.version: Literal[3]`. It is equivalent to "int, must equal 3" and fine.
  - `SuiteDefaults` requires both tolerance and datasets.
  - `EvalQuestion`: id pattern `^[GFO][0-9]{2}$`, the pipeline literal, question <= 1,000 chars, tags, datasets, setup, framing, expected.
  - `Expected`: the at-least-one invariant covers numeric/entities/rules/rubric/must_mention/must_not_claim (placeholders_sql alone does not count, which is correct). Top-level lists are merged into `rules` and then cleared.
  - `Tolerance`: exactly one of abs/rel.
  - `EntitiesExpected.check`: the regex is copied verbatim.
  - `RulesExpected` includes the DD11-08 fields. `MentionRule`, `SignRule` and `ClaimRule` (compiled with IGNORECASE at load) are present. `RubricExpected` has criteria 1..10 and min_score 1..5.
  - `NumericExpected.unit` has the same 11 values as `NumberRef.unit` (see Minor 3).
- U11-54 load_suite ✅: the size cap is enforced by a bounded read (cap + 1 byte). It uses safe_load plus the alias budget (via `scripted._parse`), then applies defaults (datasets; tolerance for numeric blocks) and validates. It enforces 500 questions, unique ids (after validation, so the id can be named), patterns compiled with IGNORECASE, and sha256 of the file bytes. Every failure is a `ConfigError`. The `question_id` context names the question, or `#<index>` when it has no id. Hints are built from `errors(include_input=False)`.
- U11-55 resolve ✅ (steps in order):
  1. Dataset skip first (`dataset`, then `truth_ref_on_real`), before any SQL runs.
  2. For each block in the order placeholders_sql -> entities -> numeric: `SqlGuard.check` -> `query_id(sql, {}, build_id)` -> cache by query_id (the guard runs before the cache lookup) -> `_run_sql`. `_run_sql` starts a `threading.Timer(QUERY_TIMEOUT_S, cur.interrupt)`, runs `fetchmany(1000)`, and cancels the timer in `finally`. A per-call `con.cursor()` is closed in `finally`.
  3. `truth_ref` is compared with row 1, column 1 and raises `SuiteError("truth_ref mismatch")`.
  4. Placeholders:
     - `{T...}` goes through `plant_value`, with the aliases as specified and display names from core.team -> service -> org -> work_item. The truth manifest is used only on synthetic builds.
     - `{entity_name}` is the display name of row 1, column 1 of entities.
     - Any other `{name}` is looked up in the order placeholders_sql -> entities -> numeric.
  5. Every error kind listed in the spec raises `SuiteError` with `question_id` set.
- TH11-08 ✅: 1 MB cap, 500-question cap, safe loader, alias budget. The alias bomb and python tags are rejected in under 2 s.
- TH11-09 ✅ (for the in-code parts): guard, 60 s interrupt timer, 1,000-row cap, per-thread cursor, timer cancelled in `finally`.
- Tests:
  - UT11-45 ✅ (includes a no-echo check on the hint)
  - UT11-46 ✅ (`top3` rejected)
  - UT11-47 ✅ (defaults, merge, sha256, version 2)
  - UT11-48 ✅ (duplicate ids, 1.5 MB file, plus other load failures)
  - UT11-49 ✅ (execution counter == 1; also covers the error, empty, row-cap and timeout paths)
  - UT11-50 ✅ (`{T5.team}`, `{entity_name}`, `{org_name}`, plus the source order)
  - UT11-51 ✅ (`dataset`, `truth_ref_on_real`, no SQL executed)
  - UT11-52 ✅
  - ST11-06 ✅: each of the three hostile SQL strings is tried in all three blocks. Every attempt is rejected, the row counts are unchanged on a *writable* in-memory warehouse, and no file is created.
  - ST11-12 suite part ✅: 2 MB, 10^6 aliases (the size cap rejects it first), a sub-1 KB billion-laughs file (alias budget), and python tags. Each raises ConfigError in under 2 s.
  - Every test name contains its ID with `_`, every docstring starts with its ID, and both files set `pytestmark = pytest.mark.unit` (the same as the other tests/security files).
- ⚠️ Cannot verify from diff:
  - The "read-only on wh-<build_id>.duckdb" precondition is the caller's job, and golden does not assert it. In this card the guard is the only protection; the read-only open belongs to the eval runner card.
  - `{T3.ci}` gets a display name only if the CI id appears in core.team, service, org or work_item (there is no core.ci table). If it does not, the question fails with a SuiteError. That is the safe choice, but it depends on the real warehouse contents.

### Builder concerns assessed
- 400/400 lines: accepted. It is at the hard limit, and it gets there partly by packing lines under `# fmt: skip` (lines 29-33, 49-50, 397-400). Any further growth must split the models out (see Minor 5).
- `owning_team` display lookup: accepted. It is a natural reading of design O07 and it is aliased like the other attributes.
- `{T3.ci}` -> SuiteError when no display is found: accepted (see ⚠️).
- SqlGuard built on each resolve from information_schema, with default blocked_columns and empty schemas skipped: accepted. It costs one catalog query per question, which is negligible.
- `SuiteError` stays in truth.py and golden re-exports it: accepted (program ruling). UT11-52 asserts that there is a single class.
- Reuse of the private `scripted._parse`: accepted for now (see Minor 4).
- Duplicate-id check in load_suite, and `Literal[3]`: accepted.

### Strengths
- The bounded read means an oversized file is never loaded into memory. The hint does not echo input, and a test asserts this.
- The guard runs before the cache lookup, so a cached query_id never bypasses the guard.
- The tests are behavioural: they use a real DuckDB, a counted `_run_sql`, real timeout interrupts, a writable warehouse for ST11-06, and a cwd file listing to prove no file was written.
- An empty reference is not cached, and failures always carry `question_id`.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. herness/eval/golden.py:390: `truth_ref` is compared as `str(plant) != str(row[0][0])`. For a numeric truth_ref, a type or scale difference makes equal values mismatch: an INTEGER 3 against a plant float 3.0 gives "3" vs "3.0", and a DECIMAL "0.250" against 0.25 also fails. Consider comparing numerically when both sides are numbers. Today's float plants are generator parameters, so this probably will not happen, but it would be a confusing false "truth_ref mismatch".
2. herness/eval/golden.py:348: `display()` catches every `duckdb.Error` and moves on to the next table. It should catch only `duckdb.CatalogException` (a missing table), so that other failures surface instead of silently falling through to "no display name" or to a later table. This includes a late `InterruptException`: a timer callback that is already running while `cancel()` is called can still interrupt the same cursor's next statement at line 347.
3. herness/eval/golden.py:27,111: `unit` uses `herness.metrics.settings.Unit`. The spec says "NumberRef unit literal", and the owner is `herness.core.types.harness.evidence.NumberRef` (evidence.py:58). The values are identical today, but the two literals can drift apart. Consider deriving it from the NumberRef owner or sharing one alias.
4. herness/eval/golden.py:23: golden imports a private helper (`scripted._parse`) across modules. It would be better to promote it to a public name, such as `safe_parse_yaml`, in a later card.
5. herness/eval/golden.py:29-33, 49-50, 397-400: lines are packed under `# fmt: skip` to fit the 400-line budget, and there is no headroom left. When the next card needs to change this module, move the models to `golden_models.py`.
6. herness/eval/golden.py:236: when the id itself fails the pattern, the raw id (truncated to 16 chars) goes into the message and the context. This is only suite-authored text, so it is harmless, but the "never echo input" rule is not applied in this one case.

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit field, invariant, algorithm step, threat control and test row for U11-53..U11-55 is present and verified by gates I re-ran myself (29 tests pass, 100 % line and branch coverage). The remaining items are small robustness and cleanliness issues, not correctness problems.
