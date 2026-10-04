# T04-03 review: Catalog and validator (build 1dafaf8, base cdadffe)

## Initial review (1dafaf8) — verdict: Needs fixes

### Spec Compliance
- U04-23 ✅, U04-24 ✅ (⚠️ < 2 s not met for the 28-metric catalog, ~2.3 s), U04-25 ✅, U04-26 ✅ steps 1-10 / ❌ perf bound, U04-27 ✅ (27 entries), U04-28 ✅, U04-83 ✅ algorithm / ❌ OwnerValidator protocol.
- Tests row IDs all present with ID names/docstrings; pytestmark unit.
- Gates: ruff, format, mypy (147 files), lint-imports 13 kept, type ownership, module size all green; 441 passed 1 xfailed; catalog.py and _catalog_checks.py 100 % branch.

### Carry-overs
- (a) ✅ No CatalogView left; TYPE_CHECKING imports in _binds/render (avoid runtime cycle), runtime import in context; behaviour unchanged.
- (b) ✅ Meaningful: FILTER read from executed _macros module; wrapper threshold regex-parsed from template text.

### Issues
Critical: none.
Important:
1. catalog.py:341 `metrics_owner_validator(cfg, /, *, offline)` incompatible with OwnerValidator (config_validate.py:294-299); mypy arg-type error at registration. Drop `/`, add static guard.
2. U04-26/U04-24 < 2 s unmet and untested; spec conflict with U04-38 (< 20 ms/render) and impl 04 line 1823 (fresh env per render). Needs ruling.
3. _catalog_checks.py lacks an impl 04 §2 module-map note (controller action).
Minor:
4. catalog.py:183 production `assert`.
5. catalog.py:319 LAST content used unvalidated as filename.
6. test_metrics_catalog.py:265 `module.FILTER` fails mypy.
7. test_metrics_catalog.py:513 ST04-04 deferral lacks `# T04-05:` marker; `.replace(";", "")` hides real `;`.
8. Spec gap: text-column word list bypassable via `f.*`, COLUMNS(), to_json(f); entity_id cast to VARCHAR.
9. Step 5 column types unchecked; wrapper does not CAST optional columns.

### Builder concerns
(i) Perf: ~25 ms/render, ~73 % Jinja compile. Scratch prototype caching compiled code objects (fresh env per render kept): 200x4 = 4.5 s, still > 2 s. Recommend a spec ruling relaxing the bound (e.g. shipped catalog < 2 s; 200x5 < 10 s) with a slow bench, plus optional private compiled-code cache helper (render.py has 1 line left) and a one-line amendment to line 1823.
(ii) UT04-13 strict xfail naming T04-08: acceptable.
(iii) LEVER_PLACEHOLDERS with `# T04-18:` marker: acceptable; values equal U04-71; T04-18 must re-export.
(iv) Contracts: covered (pyproject leaves list); still needs §2 note.
(v) Step 5 types: acceptable as recorded deviation.
(vi) ST04-04 scope: acceptable interim; schema scan carries to T04-05 / score steps.

Owner validator not registered in production; `# T09-20:` marker at catalog.py:358-359. No import cycle.

## Re-review r1 (9ac2ace) — verdict: Approved

Scope per controller rulings: I-1 and Minors 4-7 (I-2 parked as spec note, I-3 recorded as module-map note, Minors 8-9 parked).

- I-1 ✅ `metrics_owner_validator(cfg: HernessConfig, *, offline: bool)` (no `/`); static guard `_OWNER_VALIDATOR: Final[OwnerValidator] = metrics_owner_validator` in catalog.py. Importing herness.core.config_validate from metrics is layer-legal (lint-imports 13 kept). mypy on the test file (previously 2 errors incl. the arg-type at registration) now clean.
- Minor 4 ✅ unit_for restructured without `assert`; same behaviour (metric row + metric → catalog unit; metric row without metric or unknown column → "no unit for <column>"); UT04-117 unchanged and green.
- Minor 5 ✅ LAST must fullmatch `cfg_[0-9a-f]{16}` (matches config_hash format, U10-11, and T10-05 writes the hash to LAST, audit.py:371); anything else → the unreadable-snapshot `warn`. New parametrized test covers `../../secrets`, `cfg_ABC`, `cfg_….yaml`, empty. Note: an empty LAST now warns instead of first-load; acceptable.
- Minor 6 ✅ `getattr(module, "FILTER", None)` + isinstance assert.
- Minor 7 ✅ `# T04-05:` marker added; scan no longer strips `;`, it now asserts only the text-column rule (explicit, commented rationale), so nothing is hidden.
- Gates re-run: ruff check pass; ruff format 341 formatted; mypy 0 issues / 147 files; lint-imports 13 kept; check_module_size exit 0 (catalog.py 367/380). `PYTHONUTF8=1 uv run pytest tests/unit/metrics`: 445 passed, 1 xfailed; catalog.py 100 % (188 stmts, 40 branches), _catalog_checks.py 100 %.
- No regressions found. No new findings.
