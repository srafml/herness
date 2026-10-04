# Report: T00-10 Module size check

Status: DONE

## What was implemented

`tools/check_module_size.py` (U00-55), implementing the algorithm verbatim from the brief:

1. Reads `[tool.herness.module_budgets]` from `<root>/pyproject.toml` with `tomllib`: `default` (int, required) and `overrides` (path -> `{limit, reason}`). Missing/non-int `default`, or a non-int override `limit`, raises `_ConfigError` -> exit 2 with a stderr message. An override with `limit > default` and an empty (or whitespace-only) `reason` -> `MS003`.
2. Scans every `<root>/docs/impl/*.impl.md` for Markdown tables whose header row's first cell is `Path` and last cell is `Line budget`. Body rows qualify when the first cell contains exactly one backtick-delimited token ending in `.py` with no `{`, `*` or space, and the last cell is a plain integer. The same path with two conflicting integers (within or across docs) -> `MS002 conflicting budgets ...`.
3. Walks every `*.py` under `<root>/herness`, `<root>/app`, `<root>/tools`, sorted.
4. Effective budget = `overrides[path].limit` if present else `default`; then the minimum of that and any doc budget (ties keep the doc source label).
5. Reads each file as UTF-8; `UnicodeDecodeError` -> `MS004`.
6. `lines = len(text.splitlines())`; `lines > budget` -> `MS001 <lines> lines > budget <budget> (<source>)` where source is `default`, `override` or the doc's filename.
7. Exit 1 if any violation, else 0. Violations sorted by `(path, line, code, message)` via a frozen, orderable `Violation` dataclass, rendered `<path>:<line>: <CODE> <message>` — matching the `check_type_ownership.py` (T00-08) CLI conventions (argparse `--root`, `sys.stdout.write`, exit codes 0/1/2, `if __name__ == "__main__": raise SystemExit(main())`).

`pyproject.toml`: added the `[tool.herness.module_budgets]` table exactly as specified in the brief:
```toml
[tool.herness.module_budgets]
default = 400
overrides = { "herness/harness/loop.py" = { limit = 220, reason = "design 05 §5.2" } }
```
Placed after `[tool.uv.sources]` and before `[tool.hatch.build.targets.wheel]`.

## Files changed

- `tools/check_module_size.py` (new, 200 lines — exactly at its own doc-declared budget of 200 from `docs/impl/00-foundation.impl.md`'s module map)
- `tests/unit/tools/test_check_module_size.py` (new)
- `pyproject.toml` (added `[tool.herness.module_budgets]`)

No import-linter contract changes were needed: `check_module_size.py` imports only stdlib (`argparse`, `re`, `sys`, `tomllib`, `collections.abc.Sequence`, `dataclasses`, `pathlib`), so it doesn't touch any layer contract.

## RED evidence

```
$ uv run pytest tests/unit/tools/test_check_module_size.py -q
ModuleNotFoundError: No module named 'tools.check_module_size'
ERROR tests/unit/tools/test_check_module_size.py
1 error in 0.74s
```

## GREEN evidence

```
$ uv run pytest tests/unit/tools/test_check_module_size.py -q
.....                                                                    [100%]
5 passed in 0.12s
```

Tests written (covering UT00-59 and UT00-60 exactly as specified, plus 3 supporting cases for full branch coverage of the algorithm — passing default budget, missing-default usage error, and MS004 decode error):
- `test_ut00_59_default_override_and_doc_budgets` — override (220) exceeded by a.py (221 lines), doc budget (150) exceeded by b.py (151 lines), default-budget (400) file c.py (400 lines) passes; exit 1.
- `test_ut00_60_empty_reason_and_conflicting_doc_budgets` — override `limit=500` with empty reason gives MS003; two docs giving the same path different budgets (100 vs 120) gives MS002; exit 1.
- `test_ut00_59_passing_repo_has_no_violations` — no violations, no output, exit 0.
- `test_ut00_60_missing_default_is_usage_error` — missing `default` key exits 2.
- `test_ut00_60_non_utf8_file_gives_ms004` — undecodable file gives MS004.

## Gate outputs (all clean)

```
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
32 files already formatted

$ uv run mypy
Success: no issues found in 13 source files

$ uv run lint-imports
Contracts: 4 kept, 0 broken.

$ uv run python -m tools.check_type_ownership
INFO pending owner 03 / 05 / 06 / 07 / 08 / 09
(exit 0)

$ uv run python -m tools.check_module_size
(no output, exit 0 — passes on the real repository, satisfying the card's acceptance check)

$ uv run pytest -m "(unit or integration) and not slow" -q -p no:logging
........................................................................ [ 66%]
....................................                                     [100%]
108 passed, 4 deselected in 2.69s
```

## Line counts vs budgets (real repo, all doc-sourced budgets from docs/impl/00-foundation.impl.md, since no overrides currently match an existing file)

| File | Lines | Effective budget | Source |
|---|---|---|---|
| tools/check_module_size.py | 200 | 200 | doc |
| tools/check_type_ownership.py | 338 | 390 | doc |
| herness/core/errors.py | 320 | 340 | doc |
| herness/core/ids.py | 291 | 330 | doc |
| herness/core/logging.py | 241 | 260 | doc |
| herness/core/numbers.py | 287 | 320 | doc |
| herness/core/time.py | 136 | 200 | doc |
| herness/core/_log_pipeline.py | 332 | 360 | doc |
| herness/__init__.py | 14 | 30 | doc |
| herness/core/__init__.py | 1 | 10 | doc |
| herness/core/types/__init__.py | 7 | 150 | doc |
| herness/core/types/_ownership.py | 57 | 120 | doc |
| tools/__init__.py | 1 | 5 | doc |

All within budget.

## Deviations from the brief

None in behaviour. Two implementation choices the brief left open, resolved by inference from context (both exercised only by substring assertions in the given test specs, so no test contradicts them):

1. **MS001/MS002/MS003 message wording beyond the given template.** The brief gives the exact MS001 template verbatim; MS002 and MS003 are named only as codes (`MS002 conflicting budgets`, and MS003 with no example text). I used `MS002 conflicting budgets <a> (<doc-a>) vs <b> (<doc-b>)` and `MS003 override limit <limit> > default <default> with empty reason` — both start with the literal code and read naturally; UT00-60 only checks the codes appear in output.
2. **Violation path/line for MS002 and MS003**, which aren't tied to a specific source line the way MS001/MS004 are (they're config- or doc-level findings, not necessarily about an existing file — confirmed by UT00-60 where `c.py` is never created as an actual file, only referenced from two docs). I used the declared module path as `path` and `0` as `line`, mirroring how `check_type_ownership.py` anchors table-level violations (e.g. `OWN001`/`OWN002`) to a synthetic path at line 0.
3. Added a stricter defensive check not explicitly demanded by the algorithm text: an override's `limit` must be an int (same rule as `default`), raising `_ConfigError` (exit 2) if not. This only affects malformed config, which none of the brief's test rows exercise, and keeps `_load_config`'s two failure paths symmetric.

## Concerns

None outstanding. The tool's own file sits exactly at its 200-line doc budget (`lines == budget`, which the algorithm's `lines > budget` check correctly treats as passing) — a future change to this file that adds lines will need either tightening or a doc-budget bump; flagging this only as an observation, not a defect.
