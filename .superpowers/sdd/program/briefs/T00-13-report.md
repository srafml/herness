# T00-13 report: Audit and licence gates

Status: DONE_WITH_CONCERNS
Commit: 098937b feat(tools): add audit and licence gates (T00-13)

## Implemented
- `tools/check_audit.py` (U00-57):
  - Parses pip-audit JSON and reports AU010 for entries with `skip_reason`. Parses osv-scanner JSON. Normalises package names per PEP 503.
  - Merges findings within a package when their `id ∪ aliases` sets intersect (transitively).
  - `fix_available` is true when pip-audit `fix_versions` is non-empty or any osv range event has `fixed`.
  - Severity is the max `float(max_severity)` over osv groups that share an ID. A non-numeric or non-finite value gives None.
  - The ignore file is read with tomllib and its shape is strict. The only allowed key is `ignore`. Each entry must have exactly `id`, `package`, `reason` and `expires`. The first three must be non-empty strings, and `expires` is a TOML date or a `YYYY-MM-DD` string. Anything else exits 2.
  - Decisions, in order:
    - ignored
    - AU003 expired ignore (blocking)
    - AU001 blocking (a None severity counts as 7.0)
    - AU002 warning
  - AU011 is reported for unused ignore entries.
  - `--summary` (optional) writes a Markdown table `package | version | ids | severity | fix | decision`.
  - `--today` defaults to `clock.utc_day(clock.now())`.
  - Exit codes are 0, 1 and 2 (R-73).
- `tools/check_licences.py` (U00-58):
  - Loads `[tool.herness.licences]`. `allowed` is required. `aliases`, `approved` and `report_only` default to empty. Each approved entry needs exactly the five keys.
  - Reads SBOM `components[]` and skips `metadata.component` (matched by bom-ref, else by name and version).
  - Collects licence texts from `license.id`, from `license.name` via case-insensitive aliases, or from `expression`. A component with none gets `UNKNOWN`.
  - The evaluator strips outer parentheses, splits on top-level ` OR ` and then ` AND ` recursively, and drops ` WITH <exception>`. Multiple licence entries are alternatives.
  - Components that are not allowed are checked in this order:
    - approved: `fnmatchcase` glob on the normalised name plus exact licence text
    - report_only: LC002
    - denied: LC001
  - `--mode report` writes `LC001 warning denied …` and exits 0.
  - Allowed components print nothing.
- `tools/audit_ignore.toml`: `ignore = []`, plus two comment lines.
- `pyproject.toml`: `[tool.herness.licences]` with the brief's allowed list, 10 aliases, `approved = []` and `report_only = ["nvidia-*"]`.
- Tests:
  - `tests/unit/tools/test_check_audit.py`:
    - UT00-69
    - ST00-07, parametrised over the 5 cases
    - extra tests: unused ignore entry with an osv-only finding, malformed ignore file, malformed JSON and usage errors, default `--ignore` and `--today`
  - `tests/unit/tools/test_check_licences.py`:
    - UT00-70
    - ST00-16
    - extra tests: nested expressions and trove names, malformed inputs, the repository's own table loads

## RED / GREEN
- RED: `uv run pytest tests/unit/tools/test_check_audit.py` gave `ModuleNotFoundError: No module named 'tools.check_audit'` (collection error). `tools.check_licences` failed the same way.
- GREEN: `uv run pytest tests/unit/tools/test_check_audit.py tests/unit/tools/test_check_licences.py` gave 22 passed.

## Gates
- `ruff check`: All checks passed. `ruff format --check`: 43 files already formatted.
- `mypy`: Success, no issues in 16 source files.
- `lint-imports`: 4 contracts kept, 0 broken.
- `check_type_ownership`: exit 0.
- `check_module_size`: exit 0.
- `check_traceability`: no new findings. The only change in its output is the summary line, `implemented=107` → `111`. It reports 0 findings under tests/ or in spec 00.
- `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`: 135 passed, 5 deselected, 1 xfailed. The xfail is IT00-02, a strict xfail that already existed for the spec 01-11 doc debt.

## Line counts vs budgets
- tools/check_audit.py: 297 / 300
- tools/check_licences.py: 250 / 250 (exactly at budget)
- tools/audit_ignore.toml: 3 / 50

## Deviations and interpretation
1. Trove-classifier licence names. A local run of `cyclonedx-py environment .venv` showed that cyclonedx-py writes `license.name` as the full classifier (`License :: OSI Approved :: BSD License`). The brief's aliases are the short names, so a literal lookup would deny most packages. When a name starts with `License ::`, `check_licences` looks up the alias of the last `::` segment and keeps the full name for reporting. The gate still fails closed, because the alias must map to an allowed ID. A test covers this.
2. Allowed IDs are compared exactly (case-sensitive), which fails closed. Aliases are case-insensitive, as the spec says.
3. Nested parenthesised expressions are evaluated recursively. This is a superset of the brief's flat algorithm and gives the same result on every brief case.
4. The extra tests without an ID follow existing precedent (`test_rf_*`, `test_cv_*`).

## Concerns (for T00-14 / O-03)
- I ran report mode over an SBOM of the dev .venv. It includes dev dependencies, so it is not the CI runtime environment. It still reports LC001 for:
  - cffi (MIT-0)
  - chardet (LGPLv2+)
  - numpy (`... AND 0BSD AND Zlib AND CC0-1.0`)
  - pillow (MIT-CMU)
  - regex (`Apache-2.0 AND CNRI-Python`)
  - protobuf, pyahocorasick, pydeck, sentinels and transformers (UNKNOWN: no licence metadata)
  - herness itself (`metadata.component` is null without `--pyproject`; CI uses `--no-install-project`)

  Enforce mode in T00-14 will need `approved` entries or additions to the allowlist. That is a policy decision and I did not make it here.
- pip-audit and osv-scanner were not run, because they need network access. The audit gate is tested on fixture JSON only. The parsing follows the JSON shapes that the spec describes; I did not check it against real scanner output. O-02 stays open.
- No dependency changes: the dependency lists in pyproject.toml and uv.lock are untouched.
