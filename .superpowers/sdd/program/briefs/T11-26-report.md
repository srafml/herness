# T11-26 Grading: build report

Status: DONE_WITH_CONCERNS (minor, see Concerns)
Commit: badf70d feat(eval): grading methods and unsupported-number count (T11-26)
Worktree/branch: D:\herness\.claude\worktrees\agent-abbb912a8bb61713e / worktree-agent-abbb912a8bb61713e (base 6a38cbf)

## Files
- herness/eval/grading.py (new): 374 physical lines (budget 400, hard limit 400)
- tests/unit/eval/test_grading.py (new): U11-56, U11-57, U11-59 tests: UT11-78..85, UT11-91, UT11-92, PT11-06
- tests/unit/eval/test_grading_rules.py (new): U11-58, U11-60 tests: UT11-86..90, UT11-93..95, PT11-07
- golden.py was not touched (still 400 lines). No pyproject change: herness.eval is already covered by the contracts and mypy files.

## Public API (grading.py)
GradeResult(check, status, detail), NumberCarrier(text, numbers), UnsupportedReport(total, unsupported,
stray_numerals, orphan_markers, stale_refs, rate), grade_numeric, grade_entities, kendall_tau_check,
chat_entity_ids, split_sentences, grade_rules, grade_rubric, count_unsupported, plus these helpers and types:
carriers_from(items), RubricScorer (Protocol), EvidenceLookup, RerunFn, GradeStatus.

## Design choices, deviations and assumptions
- Judge typing (ruling 1): local structural `RubricScorer` Protocol with
  `score(question_id, criteria, min_score, final_text) -> _Scored`, where `_Scored` is a Protocol with a read-only
  `scores: Mapping[str, int]` property. It is named RubricScorer, not RubricJudge, so it does not clash with the
  T11-27 class. No judge.py was created.
- `EvidenceLookup = Callable[[str, str], bool]`: `(query_id, build_id)` returns True when ops `evidence`, else warehouse
  `meta.evidence`, holds the query for that build (DD11-09 default). The runner (later card) composes the two lookups.
- `RerunFn = Callable[[str], Sequence[Mapping[str, object]]]`: returns the full re-run result as rows of column -> cell.
  Raising any exception means the rerun failed, and count_unsupported catches it (`except Exception`,
  `# noqa: BLE001` with a reason). One rerun per query_id per call (cached). Evidence missing means no rerun.
- Comparison source: `herness.harness.verifier.compare_value` and `row_matches` (spec 05 §5.6 steps 6-7). They are
  reused as is and not reimplemented. RerunFn gives no column types, so the DuckDB type family comes from the
  Python cell: Decimal -> "DECIMAL", int -> "BIGINT", anything else -> "DOUBLE". DuckDB's fetch types make this exact
  for the branches compare_value distinguishes. Step 6 selection: rows matching row_key (None means all rows) must
  number exactly 1, and the column must be present. Otherwise the ref is stale.
- Markers and numerals: `herness.core.numbers.parse_markers` and `find_uncited` (R-16). Malformed `[[...]]` tokens are
  ignored, as the spec defines markers as `[[n\d+]]`.
- Counting: `total` = marker occurrences (duplicates count) + stray numerals. `unsupported` = stray + stale refs (one
  per distinct NumberRef id, first one wins on duplicate ids) + orphan marker occurrences. `rate` = unsupported/total,
  0 when total = 0 (spec). Refs with no marker in the text are still checked, following the spec literally (step 3,
  "each NumberRef").
- grade_numeric: every comparison uses Decimal, not only USD. With float, UT11-78 fails: 8.08-8.0 = 0.08000000000000007
  > 0.08. Floats convert through repr and tolerances through repr. `tolerance=None` counts as exact (abs 0). The loader
  normally fills it from the defaults. The reference cell must be finite and numeric (not bool or None). Otherwise, or
  when there is not exactly one row, or when there are >1 columns and none is `value`, it raises SuiteError. The
  detail holds reference (str), query_id and candidates [{id, value}] for refs with a marker and the matching unit.
- kendall_tau_check: tau-b via scipy.stats.kendalltau(variant="b"). The pass test is `tau >= threshold - 1e-9` because
  scipy returns 0.9999999999999999 for identical order (found by PT11-06) and 0.7999... for one swap in 5 (UT11-84).
  NaN fails, and n < 2 raises SuiteError. Positions use the first occurrence in ranked. A missing item gets len(ranked).
- grade_entities: the reference is `str(row[0])` of all rows. An empty reference raises SuiteError. The check name is
  `entities:<check>`. The detail holds query_id, the first 20 of reference and ranked, and tau (None when NaN).
- chat_entity_ids: for each name, longest first, the regex `(?<!\w)name(?!\w)` runs with IGNORECASE on the original
  text. Each match is masked with same-length NUL chars, which are non-word, so no new word boundaries appear. The
  result is unique ids ordered by first occurrence. Empty names are skipped.
- grade_rules: order is must_mention items, must_not_claim items, max_number_refs (when set), number_signs. MentionRule
  and ClaimRule match case-insensitively (casefold). ClaimRule uses `ClaimRule.regex` (IGNORECASE). The detail
  `where` names the source ("final_text" / "claim[i]") and never the matched sentence (no ticket text in details).
  number_signs compares values as Decimal, so a USD string works too.
- grade_rubric: judge None -> skipped {"reason": "no_judge"}. ModelUnavailable -> skipped
  {"reason": "judge_unavailable"}. Otherwise it passes when the mean of the returned scores >= min_score. Empty scores
  fail.
- carriers_from([ChatAnswer | Paragraph | RecommendationItem]): text is `.text`, or `headline + "\n" + summary`.
- Imports use `herness.core.types` re-exports (the type-ownership check flags submodule imports). The scipy import
  carries `# type: ignore[import-untyped]`, like herness/enrich/calibrate.py.

## Tests
- RED: `uv run pytest tests/unit/eval/test_grading.py tests/unit/eval/test_grading_rules.py` failed at collection:
  ImportError: cannot import name 'grading' from 'herness.eval'.
- GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/eval -q -p no:logging --require-test-ids` gave 78 passed.
- Coverage of herness/eval/grading.py: 100 % line, 100 % branch (214 stmts, 54 branches).

## Gates
- ruff format / ruff check --fix: clean. mypy: Success, 127 files. lint-imports: 13 kept, 0 broken.
- check_type_ownership: exit 0. check_module_size: exit 0.
- Full `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`: 3352 passed, 5 skipped,
  14 deselected, 1 xfailed (pre-existing).
- Pre-commit hooks all passed on commit (detect-secrets clean, no baseline change).

## Concerns
1. Tau epsilon 1e-9 is an addition to the spec's `tau >= X`, needed because of float error. Without it, PT11-06 fails.
2. RerunFn has no column-type channel. The type family comes from the Python cell type (see above). If the runner
   wants DuckDB-declared types, RerunFn would have to return (rows, types).
3. Stale refs count even when no marker cites them. That follows the spec text, but it can make unsupported > total
   (rate > 1), or hide the count when total = 0. The controller may want only cited refs counted.
4. grade_numeric compares with Decimal for all units. The spec names Decimal only for USD. The UT11-78 values need
   exact decimal arithmetic.
