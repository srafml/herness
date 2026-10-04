# T00-11 report: Traceability check

Status: DONE_WITH_CONCERNS

## Implemented
`tools/check_traceability.py` (U00-56): a CLI that uses only the standard library, `main(argv) -> int`, `--root PATH`, `--require-implemented NN[,NN...]`. It runs checks TR001–TR010 and TR090 as the spec algorithm describes. Violations print as `<path>:<line>: <CODE> <message>`, sorted, followed by the summary line `defined=<n> referenced=<n> implemented=<n>`. Exit codes: 0 = clean, 1 = violations, 2 = missing `docs/impl`, a bad `--require-implemented` value, an unknown option, or an unreadable doc. The tool follows the structure of `check_module_size.py`: a `Violation`/`Report` dataclass, argparse, and `SystemExit` mapped to exit code 2.

## Files
- `tools/check_traceability.py` (new, 320 lines; budget 390)
- `tests/unit/tools/test_check_traceability.py` (new): UT00-61 … UT00-68, one function per ID. Every tmp ID is built at run time.
- `tests/integration/repo/test_check_scripts.py` (new): IT00-02, marked `xfail(strict=True)` (see concerns)
- `tests/bench/test_core_bench.py`: added BT00-04 (0.3 s on the repository; limit 5 s)

## Deviation
- **Range expansion in card `Tests` rows.** The specs write ranges more than 1,100 times (`UT01-06–UT01-13` with an en dash, `…`, or `...`). If only literal IDs counted as "appears in a Tests row", ranges alone would cause about 475 false TR005 violations. The tool therefore expands `A<sep>B` inside a card's `Tests` row when both ends have the same kind and spec. This applies only to TR005. It affects neither the definition/reference sets nor TR001. UT00-65 covers it.
- TR008 honours the fence-skip rule like every other doc check, so placeholders inside fenced code are not reported.

## RED / GREEN
- RED: with the tool moved aside, `uv run pytest tests/unit/tools/test_check_traceability.py` failed with `ModuleNotFoundError: No module named 'tools.check_traceability'` (1 error during collection). I wrote the tool before the test file, so this RED only shows that the module was missing.
- GREEN: `uv run pytest tests/unit/tools/test_check_traceability.py tests/bench/test_core_bench.py -k "ut00_6 or bt00_04"` gives 9 passed.
- Full run: `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` gives 116 passed and 1 xfailed (IT00-02).

## Gates
ruff format: all files already formatted. ruff check: all checks passed. mypy: no issues in 14 files. lint-imports: 4 contracts kept. check_module_size: exit 0. check_type_ownership: exit 0.

## Acceptance checks
- The card's unit tests pass.
- `python -m tools.check_traceability --require-implemented 00` exits 1. For spec 00 it reports only:
  - TR009 for tests of cards not built yet: UT00-69, UT00-70, ST00-06/07/08/12/16, IT00-01
  - TR007 in `tests/unit/tools/test_check_module_size.py` (see below)
- IT00-02 does not pass yet (xfail).

## Concerns
1. On the repository, `check_traceability` exits 1. Spec 00 docs are clean. The failures come from:
   - defects in specs 01–11: 134 TR001, 5 TR002, 6 TR004 and 66 TR005 (after range expansion)
   - 3 TR007 in the T00-10 test file: `test_ut00_59_*` ×2 and `test_ut00_60_*` ×3 reuse IDs
   
   IT00-02 therefore carries `xfail(strict=True)` so the non-slow suite stays green. The xfail flips to a failure once those issues are cleaned, which is the signal to remove the marker. The card is "Blocked by consistency pass", and the pass has not fully cleaned the specs. The T00-10 duplicates need merging into single test functions, which belongs to that card's owner; I did not change that file.
2. The range expansion described under Deviation is my interpretation. The spec says only "appears in a Tests row".
