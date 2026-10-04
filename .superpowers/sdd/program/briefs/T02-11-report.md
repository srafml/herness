# T02-11 report: Lake inventory and SQL rendering

Status: DONE_WITH_CONCERNS
Commit: ff435d7 feat(model): add lake inventory and sandboxed SQL rendering (T02-11)
Worktree: D:\herness\.claude\worktrees\agent-a1af0ed3d76167e88

## Implemented
- herness/model/lakeinfo.py (171/200 lines): EXPECTED_ENTITIES (verbatim, 12 pairs), EntityInventory
  (frozen, `present == (files > 0)` enforced in __post_init__ -> ValueError), LakeInventory (entities
  frozen to MappingProxyType; get() returns an absent EntityInventory for unknown keys, names validated
  via lake_glob), scan_lake(layout, *, extra_entities=()) — Path.glob("**/" + LAKE_FILE_PATTERN), files
  only, >100,000 -> SchemaViolation("too many lake files for s/e") (constant MAX_FILES_PER_ENTITY),
  pq.read_schema failure -> SchemaViolation("unreadable lake file <rel to raw>"), from_synth = resolved
  file path has (data, synth) or layout.synth_marker, INFO log `model.build.lake_scanned`
  (entities_present, files, bytes). LakeInventory.root = layout.raw.
- herness/model/sqlfiles.py (204/260): SqlFile (stage derived from number, init=False; out-of-range ->
  ValueError), discover_sql_files(*, sql_dir=None) (default herness/model/sql; missing dir -> []),
  render_sql(file, context) with SandboxedEnvironment exactly as specified, cached per SQL dir
  (functools.cache), filters ident/sqlstr/num/sqldate, global `raw` bound to context.lake through a
  ContextVar (thread/task safe, visible inside imported macros without `with context`). Errors
  (jinja2.TemplateError incl. Undefined/Syntax/Security/TemplateNotFound, and filter ConfigError) ->
  ConfigError("render failed for <file>: <Class> at line <n>"); line from TemplateSyntaxError.lineno or
  the innermost traceback frame belonging to a template of the SQL dir.
- herness/model/render_context.py (103/200): RenderContext (extra_entities frozen), template_vars()
  (exact keys + raw_root POSIX), build_render_context(cfg, inventory, build_id) (invalid build id ->
  ConfigError("invalid build id")).

## Tests
- tests/unit/model/test_model_lakeinfo.py (UT02-55), tests/unit/model/test_model_sqlfiles.py (UT02-56,
  UT02-57, UT02-58, ST02-11), tests/unit/model/test_model_sqlfiles_property.py (PT02-02, hypothesis +
  in-memory DuckDB), tests/integration/model/test_model_sql_injection.py (ST02-10, real DuckDB file).
- RED: `.venv\Scripts\python.exe -m pytest <new files>` -> 4 collection errors
  "ImportError: cannot import name 'sqlfiles' from 'herness.model'".
- GREEN: same -> 121 passed, 1 skipped (herness.model tests); coverage lakeinfo/sqlfiles/render_context
  100% line and branch.
- Full gate: pytest -m "(unit or integration) and not slow" -> 313 passed, 1 skipped, 4 deselected.
- ruff check: all passed; ruff format --check: 52 files formatted; mypy: no issues in 23 files;
  lint-imports: 8 kept, 0 broken; tools.check_type_ownership: exit 0. No pyproject change needed
  (herness.model already in the layers contract; jinja2 already a dependency).

## Deviations / concerns
1. build_render_context `cfg: HernessConfig` — T10-03 herness.core.config does not exist. Following the
   precedent of herness/store/layout.py, cfg is typed as a private structural Protocol (`_BuildConfig`:
   cfg.sources.dq, cfg.mappings.custom_fields, cfg.sources.sources.<files|mongodb|snowflake|dataverse>
   .entities per U01-14 / R-69). Configured entity names are taken from every *configured* (non-None)
   section of those four sources, regardless of `enabled`; the spec says "configured entity names".
   Tests use SimpleNamespace fakes. Swap the annotation to HernessConfig when T10-03 lands.
2. `raw` is registered as an environment global that reads a ContextVar set during render_sql, rather
   than mutating env.globals per render (the environment is cached and shared).
3. sqldate rejects datetime (a date subclass) so only a pure date renders as DATE 'YYYY-MM-DD'.
4. For an absent entity, from_synth = layout.synth_marker (spec only defines it per file).
5. UT02-55 symlink variant (resolved path through a link into data/synth) skips on Windows without
   symlink privilege; the same branch is covered by a non-skipped test using a DataLayout whose raw root
   lies under data/synth with synth_marker=False.
6. ST02-10 enum part: enum maps are U02-87 (refdata, later card); the test registers the quoted value as
   Arrow data, matches it by join, and checks sqlstr of the same value matches literally in one statement;
   core.incident survives. The custom-field part is checked at config (ValidationError), filter (ident/raw)
   and render (model_construct-forged config -> ConfigError) levels.
7. tools.check_module_size does not exist in this tree yet; budgets checked by hand (all within).

## Fix round 1 (commit 947b9cd fix(model): address T02-11 review round 1 (T02-11))
- I-1: sqlfiles now uses `_SqlSandbox(SandboxedEnvironment)` (same constructor arguments). Its
  `is_safe_attribute` keeps the base checks and then allows only: Jinja runtime objects (LoopContext,
  Macro, TemplateModule, Namespace); pydantic model fields (`type(obj).model_fields`, so no
  model_dump/model_copy); LakeInventory {get, entities, from_synth} (no `root`, templates use the
  `raw_root` string); every EntityInventory field; and Mapping {get, keys, values, items}. Everything
  else (Path methods, str methods, frozenset methods) raises SecurityError, which becomes ConfigError.
  New ST02-11 cases: `lake.root`, `lake.root.read_text()`, `lake.root.joinpath(..).write_text(..)`,
  `...unlink()`, `dq.model_dump()`, `build_id.upper()`, `columns.union`. Each one raises
  "SecurityError at line 1", and the test checks that no file was created. Another test shows that
  loops, namespace, `.items()`, `.get()`, entity fields, pydantic fields and `is none` still work.
- m-1: render errors in (TemplateError, ConfigError, TypeError, ValueError, ArithmeticError,
  LookupError) all map to "render failed for <file>: <Class> at line <n>". Tests cover TypeError
  (`raw('a')`), ZeroDivisionError and the sandbox's OverflowError (`range(10**9)`).
- m-2: `num` now parenthesises negative results: -3 -> "(-3)", -0.5 -> "(-0.5)", Decimal("-2") -> "(-2)".
  None of the spec's test rows gives an exact negative output, so this is a refinement of U02-86's
  `str(int)`/`repr(float)`/`str(Decimal)` wording. Worth a spec note.
- m-5: `LakeInventory.get()` gives an unknown key `from_synth=self.from_synth` (test added).
- m-7: `stat()` has moved into the same try block as `read_schema`, so a file that vanishes between
  listing and reading becomes SchemaViolation("unreadable lake file <rel>") (test added).
- m-8: the default-dir test asserts `_SQL_DIR == herness/model/sql`. It also monkeypatches `_SQL_DIR`
  to a temp dir with one file and checks that dir is listed, and checks that an absent dir gives [].
- Not changed (parked by the reviewer): m-3, m-4, m-6.
- Gates: ruff check and format clean; mypy: no issues in 23 files; lint-imports: 8 kept; type ownership
  exits 0. herness.model tests: 135 passed, 1 skipped, with 100% line and branch coverage on all three
  modules. Full fast suite: 327 passed, 1 skipped, 4 deselected.
- Line counts: lakeinfo 173/200, sqlfiles 245/260, render_context 103/200.

## Fix round 2 (commit 6592a39 fix(model): expose lake to macros imported without context (T02-11))
- N-1: a `lake` environment global (`_LakeProxy`, no state, `__slots__ = ()`) now returns the current
  render's LakeInventory. It reads the same ContextVar that `raw` uses, so macro files imported the
  U02-106 way (`{% import "_macros.jinja" as m %}`, no context) can see `lake`. I did not use
  get_template(globals=...). The proxy exposes only `get`, `entities` and `from_synth`. It is on the
  sandbox allowlist next to LakeInventory, so `root`/Path cannot be reached through it. In the main
  template the render argument `lake` (the real LakeInventory, also allowlisted) still shadows the
  global. The U02-106 macros need no other template variable: they use `lake`, `raw` and the filters.
- New test test_ut02_57_macro_reads_lake_without_context: a plain-import macro reads
  `lake.get(..).present`, `.glob | sqlstr`, `lake.entities | length` and `lake.from_synth`, rendering
  both the present and the absent branch. `lake.root` from a macro fails with ConfigError.
- The render-error tuple is condensed (same classes) to stay within budget.
- Gates: ruff and format clean; mypy: no issues; lint-imports: 8 kept; type ownership OK. Full fast suite:
  328 passed, 1 skipped, 4 deselected. Coverage stays at 100% line and branch on all three modules.
- Line count: sqlfiles.py 257/260.
