# T10-09 report: Name directory

Status: DONE

## What was built

- `herness/core/redact_directory.py` (204 lines, budget 220 per docs/impl/10-config-security-deployment.impl.md module map).
  - `NameDirectory.from_files(directory_file, extra_names, display_names_file) -> NameDirectory` (classmethod): builds a pyahocorasick Automaton over
    - CSV `display_name` (>=2 tokens) variants: "first last" (lower, as written), "last, first", "last,first", canonical = normalize_value("PERSON", display_name);
    - `alt_names` (semicolon-separated) mapped to the same canonical, skipped when the row's display_name had fewer than 2 tokens (no canonical computed);
    - `extra_names` (any token count, including single tokens) as their own canonical;
    - lines of `display_names_file` processed exactly like a CSV display_name (same step reused).
  - `find(text) -> list[tuple[int, int, str]]`: lower-cases text; falls back to a per-character lower-case plus index map when len(low) != len(text) (e.g. Turkish dotted capital I expanding to 2 characters); iterates automaton.iter(low); keeps a match only when the character before/after is not alphanumeric; resolves overlaps longest-first-then-leftmost.
  - Attributes `size` (distinct canonical names) and `variant_count` (len(automaton), i.e. distinct keys added), both computed after make_automaton().
  - `update_display_names(names, *, data_dir) -> int`: reads the existing <data_dir>/cache/redact/display_names.txt (absent -> empty set), adds stripped names with 2-8 tokens and 3-128 characters, writes the sorted union atomically (tempfile.mkstemp + os.replace, temp file cleaned up on any failure) only if the set grew, returns the count added.
  - Errors: a missing directory_file logs redact.directory.missing at WARNING (via herness.core.logging.get_logger) and yields no rows / an empty directory; any other OSError (permission, or a directory path) or an oversized file (> MAX_DIRECTORY_BYTES = 200 MiB) raises ConfigError("cannot read directory_file"), verbatim per spec. A missing display_names_file is silently empty (not logged; only the primary directory_file "missing" case is a WARNING per spec). Any OSError in update_display_names (reading the existing cache file, mkdir, or the atomic write) raises StoreBusy("display_names write failed").
  - Found no existing atomic-write helper in herness/core (checked); wrote a small private `_write_atomic` using tempfile.mkstemp(dir=path.parent) + os.replace.

- `pyproject.toml`:
  - Added "herness.core.redact_directory" to the "core base is closed" import-linter contract's forbidden_modules (alongside redact / redact_patterns), per UT00-58 (mirrors what T10-08 had to do).
  - Added a [[tool.mypy.overrides]] for module = ["ahocorasick"] with ignore_missing_imports = true: pyahocorasick ships a compiled .pyd extension with no .pyi stubs and no stub package was present in the venv or repo.

- `tests/unit/core/test_redact_directory.py` (17 test functions, all IDed test_ut10_46_..., pytestmark = pytest.mark.unit; one test additionally marked @pytest.mark.slow):
  - CSV + alt_names + word-bounded variants, including a text with an embedded (non-word-bounded) rejection case.
  - Single first name matches only when listed in extra_names (with and without).
  - alt_names of a single-token display_name are skipped (no canonical yet).
  - Blank alt_names segment and blank extra_names entries are skipped (covers the "if alt:" / "if extra:" guards).
  - Overlap resolution: longer span wins even when it starts later; equal-length overlap resolved leftmost.
  - Non-length-preserving lower(): Turkish capital I-with-dot (U+0130) expands text.lower() by one character; verified the fallback index map still returns the correct original-text span.
  - Missing directory_file -> warning event redact.directory.missing at WARNING + empty directory (size == 0, variant_count == 0, find() == []).
  - Unreadable directory_file (a directory path, triggering PermissionError on Windows, confirmed interactively) -> ConfigError containing "cannot read directory_file".
  - Oversized directory_file (via monkeypatch.setattr(rd, "MAX_DIRECTORY_BYTES", 10)) -> same ConfigError.
  - Unreadable / missing display_names_file -> ConfigError / silently empty respectively.
  - update_display_names: valid names filtered by token/char count, cache file sorted, count returned; a repeat call with an already-present name returns 0; a new name grows and re-sorts the file; NameDirectory.from_files picks up names persisted this way.
  - OSError from os.replace (monkeypatched) during update_display_names -> StoreBusy containing "display_names write failed".
  - @pytest.mark.slow acceptance test: builds a 100,000-name CSV directory and asserts elapsed < 5.0; measured about 0.90 s on the dev box (well under budget), size == 100_000.

## Gates run (all green)

- uv run was avoided per the fallback instruction; used .venv/Scripts/python.exe -m ... directly. PYTHONUTF8=1 set for all pytest/mypy/ruff runs.
- ruff check . -- All checks passed.
- ruff format --check . -- 75 files already formatted (after running ruff format . once to normalize 2 lines in the new test file).
- mypy (repo-wide, files = ["herness", "tools"]) -- Success: no issues found in 32 source files.
- lint-imports (via .venv/Scripts/lint-imports.exe, since python -m importlinter has no __main__) -- Contracts: 8 kept, 0 broken.
- python -m tools.check_type_ownership -- exit 0 (only pre-existing "pending owner" INFO lines for specs 05/06/07/09, unrelated to this card).
- python -m tools.check_module_size -- exit 0 (redact_directory.py at 204/220 lines).
- pytest tests/unit/core/test_redact_directory.py -q -p no:logging -- 17 passed.
- pytest -m "(unit or integration) and not slow" -q -p no:logging (repo-wide) -- 644 passed, 6 deselected, 1 pre-existing xfail (unrelated check_traceability doc-defects marker), no regressions.
- Coverage of the new module: --cov=herness.core.redact_directory --cov-branch -- 100% line and 100% branch (148 statements, 44 branches; above the 90%/85% requirement).

## Line counts vs budgets

- herness/core/redact_directory.py: 204 lines (module-map budget 220, ENG hard limit 400).
- tests/unit/core/test_redact_directory.py: 243 lines (test files carry no module-size budget in this repo's tooling; check_module_size only scans herness/, app/, tools/).

## Build timing for 100k names

About 0.90 s measured, acceptance threshold under 5 s. Well within budget; the spec's own complexity note ("build about 2 s and about 150 MB for 100k names") is conservative relative to what pyahocorasick actually delivers here.

## Deviations from the brief / spec, and rationale

1. size/variant_count semantics were underspecified (brief/spec give only the field names and types, no formula). Chose: size = number of distinct canonical names contributed to the automaton (len of the set of canonicals from automaton.values()), variant_count = number of distinct keys added (len(automaton), pyahocorasick's own word count). This is the natural reading of "size of the directory" vs "count of variants" and is internally consistent (tested explicitly). No other module in the tree references these attributes yet (Redactor/scan, U10-40/U10-41, is a later card), so there is no consumer to cross-check against; flagging this as the one interpretive call in an otherwise literal implementation.
2. Empty automaton guard: pyahocorasick's Automaton.make_automaton() is a no-op when zero words were added, and .iter() then raises AttributeError ("Not an Aho-Corasick automaton yet"). The spec's algorithm doesn't mention this pyahocorasick quirk. Added a variant_count == 0 short-circuit in find() returning [] before touching the automaton, so NameDirectory.from_files(None, (), None) and the missing-file case behave as the spec's "empty directory" describes. This is a compatibility fix for a third-party library quirk, not a spec deviation.
3. Everything else is exact-string-preserving: error messages ("cannot read directory_file", "display_names write failed"), the log event name (redact.directory.missing) and level (WARNING), and all algorithm steps implemented verbatim per U10-38/U10-39.

## Concerns

- None blocking. The one interpretive call (item 1 above) should be revisited by whichever card first consumes size/variant_count (likely the Redactor construction area, U10-40, which logs redact.directory.loaded per the spec) to confirm the fields it wants match this semantics.
- csv.DictReader on a row with more or fewer columns than the header is handled permissively (missing keys treated as empty); not explicitly tested since the spec doesn't call out malformed-CSV-shape as a case, noting the permissive behavior in case a later reviewer expects a hard failure instead.
