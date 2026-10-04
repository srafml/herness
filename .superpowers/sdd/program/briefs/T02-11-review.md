# T02-11 review: Lake inventory and SQL rendering (commit ff435d7)

**Verdict: Needs fixes** (one Important sandbox finding. Everything else is compliant or Minor.)

### Spec Compliance

- ❌ Issues found: see Important I-1. U02-85 and TH02-11 require template code to stay confined to the sandbox, and it does not. Through `lake.root` (a `pathlib.Path`), a template can read and write any file.

| Req / test | Status | Note |
|---|---|---|
| U02-78 `EXPECTED_ENTITIES` | ✅ | 12 pairs, verbatim, `Final` tuple |
| U02-79 `EntityInventory` | ✅ | frozen and slots; `present ⇔ files > 0` enforced (ValueError) |
| U02-80 `LakeInventory` | ✅ | `entities` wrapped in a MappingProxy; `get()` returns an absent entry for an unknown key. The `from_synth=False` default is an extra (Minor m-6) |
| U02-81 `scan_lake` | ✅ | glob `**/[!.]*.parquet`, 100,000 limit, SchemaViolation messages verbatim, synth pair or marker, INFO `model.build.lake_scanned` |
| U02-82 `SqlFile` | ✅ | stage derived from number; out of range → ValueError |
| U02-83 `discover_sql_files` | ✅ | pattern, 500–899 rejected, duplicates rejected, `_` ignored, sorted; error texts verbatim |
| U02-84 `RenderContext` / `build_render_context` | ✅ (with ⚠️) | exact keys plus `raw_root`. `cfg` is typed as a structural Protocol (see ⚠️) |
| U02-85 `render_sql` | ❌ (I-1), Minor m-1 | environment arguments exactly as specified and cached; filters and `raw` registered; mapping to the message format verbatim. The sandbox exposes filesystem methods. Non-Jinja runtime errors are not mapped |
| U02-86 filters | ✅ | `ident` uses fullmatch; `sqlstr` doubles `'`, enforces ≤1,024 and no char < U+0020; `num` excludes bool and non-finite values; `sqldate`; `raw` checks ident then the sqltype allowlist |
| UT02-55 | ✅ | union, temp/dot ignored, absent entity, synth root, synth path without marker (symlink variant skips on Windows, but the same branch is covered by another test) |
| UT02-56 | ✅ | order, `_macros.jinja`/`_x.sql` ignored, `510_x.sql`, duplicate |
| UT02-57 | ✅ | undefined var → `ConfigError` naming file and line 2 (anchored regex) |
| UT02-58 | ✅ | table-driven valid and invalid inputs for every filter |
| PT02-02 | ✅ | hypothesis, DuckDB round-trip of `sqlstr` and single-statement check; `ident` has no inner quote and round-trips as a column alias |
| ST02-10 | ✅ (partial by ruling, ⚠️) | config rejects the field (ValidationError on both mappings); ident/raw reject it; a forged `model_construct` config fails at render; `core.incident` survives. The enum part is simulated with Arrow registration because U02-87 does not exist yet |
| ST02-11 | ✅ as written | `__mro__`, `__subclasses__`, `__class__`, `__globals__` → SecurityError; `../` include → TemplateNotFound. The row passes, but it does not cover non-dunder escapes (I-1) |
| TH02-10 | ✅ | allowlisted and quoted identifiers, `num`, no free-text values in the context |
| TH02-11 | ❌ | see I-1 |
| Budgets | ✅ | lakeinfo 171/200, sqlfiles 204/260, render_context 103/200 |

- ⚠️ Cannot verify from diff:
  - Gate results (313 passed, ruff, mypy, lint-imports, 100% coverage) are taken from the report and were not re-run.
  - **Concern 1 (cfg as a structural Protocol): acceptable ruling.** `HernessConfig` (T10-03) does not exist. There is a precedent for this (`herness/store/layout.py`), and nothing was invented under a spec name. One focused check: the attribute names the code reads (`sources.sources.{files,mongodb,snowflake,dataverse}.entities`) match `SourcesSection` and `*Settings.entities` in `herness/connectors/settings.py` on `feat/impl00-foundation-runtime`. Strictly, the global constraints say NEEDS_CONTEXT for missing cross-spec symbols, so the controller should record this as a tracked follow-up: swap in `HernessConfig` at T10-03 and tighten `_SourcesFile.sources: object` (render_context.py:37). The `getattr(..., None)` at render_context.py:84 would silently hide a renamed field until then.
  - **Concern 2 (ContextVar for `raw`): acceptable.** It keeps the cached environment immutable per render, it is safe across threads and tasks, and it works inside imported macros (UT02-57 macros test). It meets "bound to `context.lake`".
  - **Concern 3 (sqldate rejects datetime): acceptable.** Otherwise `isoformat()` would put a time part inside `DATE '…'`.
  - **Concern 4 (absent entity `from_synth` = marker): acceptable**, but it is inconsistent with `LakeInventory.get()` for unknown keys (m-5).
  - **Concern 6 (ST02-10 enum part deferred to U02-87): acceptable** only if U02-87's card re-covers ST02-10's enum clause against the real refdata path. The controller should note this on the U02-87 brief.

### Strengths
- The filters are tight and exact to the spec. `fullmatch` avoids the `$`-before-newline trap. `num` excludes `bool`. `raw` validates the column before the type, and the type before the inventory lookup.
- Error messages never echo the offending value. The original exception is chained, not interpolated.
- PT02-02 checks the real parser (DuckDB), not just string shape. It includes a "tricky" alphabet and a single-statement check.
- ST02-10 goes further than required: it has a real DuckDB file, a forged config via `model_construct`, and a check that `core` still has 1 table.
- Tests assert anchored messages, including line numbers.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
- **I-1 Sandbox allows arbitrary filesystem read and write through `lake.root`** (herness/model/sqlfiles.py:156-167, herness/model/render_context.py:75; `LakeInventory.root: Path` at herness/model/lakeinfo.py:68). `SandboxedEnvironment` blocks only `_`-prefixed attributes and a few known-unsafe methods. Public methods of objects in the context are callable. I confirmed this with a probe render in the worktree using the real `render_sql` and `RenderContext`:
  - `{{ lake.root.joinpath('pwned.txt').write_text('owned') }}` renders `5` and creates the file.
  - `{{ lake.root.parent.joinpath('x.py').read_text() }}` renders the file contents.

  `unlink`, `rmdir`, `rename` and similar methods are reachable the same way. This defeats TH02-11 ("template code escapes the Jinja sandbox": ASVS V15). ST02-11 only exercises dunder attributes, so it cannot catch this.

  This is partly plan-mandated: the spec puts `lake: LakeInventory` (with `root: Path`) into `template_vars()`. The fix is still inside this card. Options:
  - (a) Subclass `SandboxedEnvironment` and override `is_safe_attribute` / `is_safe_callable` to deny `pathlib.PurePath` objects, or better, allowlist only `LakeInventory.get`, `EntityInventory` fields, pydantic fields and mapping reads. This is still a SandboxedEnvironment, as the spec requires.
  - (b) Pass templates a read-only lake view without `root`. `raw_root` already carries the string.

  Add an ST02-11 case for `lake.root.read_text()` / `write_text()`. The pydantic models (`dq`, `custom_fields`) expose `model_copy`/`model_dump`, which are harmless. The allowlist approach closes that surface too.

#### Minor (Nice to Have)
- **m-1 Non-Jinja runtime errors escape unmapped** (herness/model/sqlfiles.py:198). The probe confirmed that `{{ raw('a') }}` raises TypeError (wrong filter/global arity, arguably a "filter error"), `{{ 1/0 }}` raises ZeroDivisionError, and `range(10**9)` raises the sandbox's OverflowError. All three propagate as raw exceptions, and U02-85's Errors row says `ConfigError`. Consider catching `Exception` inside the render (and still re-raising `ConfigError` with the file and line).
- **m-2 `num` renders negatives bare, so `x -{{ n | num }}` becomes `x --3` and comments out the rest of the line** (herness/model/sqlfiles.py:122; confirmed by the probe: `SELECT 5 --3 AS x`). This output is plan-mandated (`str(int)`). Suggested fix: a spec note, or parenthesising negatives (`(-3)`) with a spec amendment. Config values today are mostly non-negative thresholds, so the risk is low.
- **m-3 Line and file attribution for errors in imported `_macros.jinja`** (herness/model/sqlfiles.py:179-190, 199-201). A `TemplateSyntaxError.lineno` or innermost frame in the macro file is reported under the importing file's name. The message would then point to the wrong file.
- **m-4 `render_sql` loads by `file.name` from `file.path.parent`** (herness/model/sqlfiles.py:193-197). `SqlFile` does not check `name == path.name` or the name pattern (sqlfiles.py:62), so a hand-built `SqlFile` can render a template other than its path. The loader directory is also whatever the caller's path says, not only the package directory. That is fine for tests, but TH02-11 says "templates only from the package directory". Consider validating `name == path.name` in `SqlFile.__post_init__`.
- **m-5 `LakeInventory.get()` for an unknown key hard-codes `from_synth=False`** (herness/model/lakeinfo.py:81). Scanned absent entities use `layout.synth_marker` (lakeinfo.py:132), so the two paths disagree. Using `self.from_synth` would make them consistent.
- **m-6 `LakeInventory.from_synth` has a default of `False`** (herness/model/lakeinfo.py:70). The spec defines it as derived. A default invites constructing inventories that under-report synth data (as in `dataset_kind`).
- **m-7 `path.stat()` is outside the SchemaViolation mapping** (herness/model/lakeinfo.py:138). A file removed between glob and stat (compaction or deletion) raises a raw FileNotFoundError.
- **m-8 `test_ut02_56_default_package_dir` is vacuous when the directory is empty or absent** (tests/unit/model/test_model_sqlfiles.py:93-96). `all()` over `[]` passes. It should assert that the default resolves to the package path, for example with a monkeypatched `_SQL_DIR` or a non-empty check once SQL files land.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The spec implementation is faithful, and the TH02-10 filters and tests are strong. But the mandated sandbox exposes a `Path` object whose methods give templates arbitrary file read and write (confirmed by a probe), which defeats TH02-11. Fix it with a restricted SandboxedEnvironment subclass or a lake view, and add an ST02-11 case. The builder's concerns 1–6 are acceptable rulings; concerns 1 and 6 need follow-ups recorded on T10-03 and U02-87.


---

## Re-review round 1 (fix commit 947b9cd)

**Verdict: Needs fixes.** All six scoped findings are fixed. One new Important finding (N-1) is a problem in `render_sql` that already existed; the allowlist did not cause it. m-3, m-4 and m-6 are parked, as the controller directed.

Method: I read `T02-11-fix1.diff` and the "Fix round 1" notes. I re-ran the round-0 probe (`probe\check.py`) against HEAD 947b9cd after deleting the file the old code had written. I also ran two new probes against the real `render_sql`: `probe\check2.py` (7 legitimate spec-shaped renders and 35 bypass attempts) and `probe\check3.py` (the macro-import context). I did not re-run the gates; the builder reports 135 and 327 passed, clean lint/type/import checks, and 100% coverage.

### Per-finding

| Finding | Status | Evidence |
|---|---|---|
| I-1 Path I/O through `lake.root` | ✅ | `_SqlSandbox` (sqlfiles.py:180-195) keeps the base checks and denies everything else by default. The probe now gives SecurityError for `lake.root.joinpath(..).write_text(..)` and `.read_text()`, and no file is created. New tests cover Path I/O, `model_dump`, str methods and frozenset methods. |
| m-1 runtime errors unmapped | ✅ | `_RENDER_ERRORS` (sqlfiles.py:62). The probe maps TypeError, ZeroDivisionError and OverflowError to `ConfigError("render failed for … : <Class> at line n")`. New UT02-57 test. |
| m-2 negative `num` becomes `--` | ✅ (needs a spec note) | sqlfiles.py:153 parenthesises negatives. The probe renders `SELECT 5 -(-3) AS x`. This refines U02-86's `str(int)`/`repr(float)`/`str(Decimal)` wording, so the controller should record the refinement in impl 02. |
| m-5 `get()` unknown key `from_synth` | ✅ | lakeinfo.py:81 uses `self.from_synth`. Test added. |
| m-7 `stat()` outside the mapping | ✅ | `_read_file` calls `stat()` inside the same try. The vanished-file test gives SchemaViolation. |
| m-8 vacuous default-dir test | ✅ | The test asserts `_SQL_DIR`, lists a monkeypatched non-empty dir, and checks that an absent dir gives `[]`. |

### Bypass attempts (all against HEAD)
- **Blocked (SecurityError → ConfigError):**
  - `lake|attr('root')`, `lake['root']`, `lake['root'].read_text()`, `lake['__class__']`
  - `([lake]|map(attribute='root')|first).read_text()`, `sum/selectattr/unique(attribute='root')`
  - `'{0.root}'.format(lake)`, `glob.replace(..)`, `cycler(lake).next()`, `cycler(1).items`, `joiner(',').__call__`
  - `namespace(p=lake)` then `ns.p.root`, `dict(a=lake).a.root`, `loop.__class__`, `macro._func`
  - `dq.model_fields`, `dq.model_copy`, `lake.entities.copy()`, generator `gi_frame`
- **Other errors:** `'%(root)s' % lake` and `lake|tojson` raise TypeError, which maps to ConfigError.
- **Harmless:**
  - `'%s'|format(lake)`, `|string`, `|pprint`, `sort(attribute='root')` and `groupby('root')` render the dataclass `repr`, which includes the root path. That path is already exposed as `raw_root`, and nothing callable is reachable from it (the `.read_text()` follow-up is blocked).
  - `map(attribute='root')` yields `Undefined`.
  - `self`, `macro.name`/`.arguments`, and a module's macro `.name` are readable Jinja runtime data with no escape route.
- No file I/O, class-hierarchy access or globals access was reachable.

### Legitimate usage
These spec-shaped patterns render correctly under the allowlist:
- The U02-106 macros `typed`, `cast_stats` and `rid`: concatenation with `~`, then `ident`/`sqlstr`.
- `dq.*|num`, `custom_fields.*|sqlstr/ident`, `raw_root|sqlstr`, `build_id|sqlstr`.
- `extra_entities.items()` loops with `lake.get(s, e).present`, `'col' in ent.columns`, `|length`, `|join`, `extra_entities.files[0]`.
- `loop.first`/`loop.index`, `namespace`, and `{% call %}`/`caller()`.

⚠️ The allowlist denies all `str`, `list`, `tuple` and `frozenset` methods (for example `.split`, `.lower()`, `.startswith`). No macro in U02-106's table needs them. Later SQL cards (T02-12 onwards) must use filters (`lower`, `replace`, `join`, and so on) instead. This is worth one line in their briefs.

### New findings

#### Important
- **N-1 Macros imported the spec way cannot see `lake`** (herness/model/sqlfiles.py:238, the environment globals at sqlfiles.py:207).
  - What happens: `render_sql` passes `template_vars()` as render arguments, not as globals. A macro module imported without context therefore sees only environment globals: the `raw` global (via the ContextVar) and Jinja builtins.
  - Why it matters: U02-106 says files use `{% import "_macros.jinja" as m %}` (plain, without context), and the `latest` macro reads `lake.get(source, entity).present` / `.glob`.
  - Probe result (`check3.py`): a macro `{{ lake.get(s, e).present }}` imported plainly raises `ConfigError("… UndefinedError at line 1")` (cause: `'lake' is undefined`). The same macro imported `with context` renders `False`. My spec-shaped `latest` macro fails in both its present and absent branches.
  - Scope: this existed before the fix and is not caused by the allowlist. Its first consumer is U02-106, but `render_sql` (U02-85) owns it, and the UT02-57 macros test only passes because it uses `raw`, never `lake`.
  - Fix options:
    - (a) Expose `lake` (and possibly the other context values) to imports through the same ContextVar mechanism as `raw`. For example, make `lake` an environment global proxy that reads `_current_lake`. Avoid `get_template(globals=…)`, because Jinja updates the cached template's globals in place, which is not thread-safe.
    - (b) Amend U02-106 to `{% import "_macros.jinja" as m with context %}`.
  - Add a UT02-57 case for a macro that reads `lake` and is imported without context.
  - The controller could instead hand this to the U02-106 card with the chosen fix recorded, but as written, `render_sql` cannot serve its documented consumer.

#### Minor
- None beyond the parked m-3, m-4 and m-6. The sqlfiles.py budget is 245/260; the headroom is small but within limits.

### Assessment (round 1)
**Task quality:** Needs fixes
**Reasoning:** I-1 is properly closed. The deny-by-default allowlist withstood every bypass I tried (attr and subscript access, attribute-taking filters, `str.format`/`%`, cycler/joiner/namespace/loop/macro objects) and still renders the spec's macro shapes. m-1, m-2, m-5, m-7 and m-8 are fixed and tested. N-1 remains: spec-style plain `import` of `_macros.jinja` cannot read `lake`, so U02-106's `latest` would fail. Fix it here or hand it off explicitly.

## Re-review round 2 (fix commit 6592a39, scoped to N-1)

**Verdict: Approved.** N-1 is closed. Nothing regressed, and the proxy opens no sandbox bypass.

Method: I read `T02-11-review.diff` (6592a39 over 947b9cd) and `herness/model/sqlfiles.py` at HEAD. I then ran a throwaway probe against the real `render_sql` and the `_context`/`_sql_file` test helpers. The probe lived in the session scratchpad, not the worktree, and I deleted it afterwards. I re-ran the gates in the worktree with `.venv\Scripts\python.exe` and `PYTHONUTF8=1`.

### Spec
| Item | Status | Evidence |
|---|---|---|
| N-1: a macro in a plain `{% import "_macros.jinja" as m %}` reads `lake` (U02-106 / U02-85) | ✅ | See the correctness and ContextVar tables below. |
| TH02-11 deny-by-default sandbox still holds | ✅ | The proxy is on the same allowlist as `LakeInventory`: `get`, `entities` and `from_synth` only (sqlfiles.py:76-77). See Bypass attempts below. |
| UT02-57 test for a lake-reading macro imported without context | ✅ | `test_ut02_57_macro_reads_lake_without_context` (test_model_sqlfiles.py:173) covers both branches, `entities|length`, `from_synth`, and `lake.root` failing as ConfigError. |
| Budgets | ✅ (⚠️) | sqlfiles 257/260, lakeinfo 173/200, render_context 103/200. |

**Correctness.** All of these ran with a plain `import` of `_macros.jinja` (sqlfiles.py:219 exposes the `lake` global):
- A spec-shaped `latest` renders `FROM read_parquet('<glob>')` when the entity is present. It renders the zero-row branch when the entity is absent or unknown.
- `lake.get(s,e).columns|sort|join`, `lake.entities.items()` loops and `from_synth` render correctly.
- `{% from ... import latest %}` also works.
- One cached template rendered in the sequence present → absent → present → absent. Every output was correct, so the cached import module does not keep a stale lake.
- Keyword calls work: `lake.get(source=..., entity=...)`.

**ContextVar lifecycle.** `_bound_lake` uses set and reset in a `finally` (sqlfiles.py:224-229).
- `_current_lake.get(None)` is `None` after a successful render.
- It is also `None` after a ZeroDivisionError at top level and after a TypeError raised inside the macro. Both errors were mapped to ConfigError.
- An outer binding set before `render_sql` survives the call.
- Calling the proxy outside any render raises LookupError. No template can reach that path.

**Concurrency.**
- 8 threads (4 with the lake present, 4 with it absent) each ran 60 renders. Each render made 200 macro calls. No output mixed the two branches.
- An asyncio `gather` mixing `to_thread` renders and inline renders also gave consistent results.
- This is expected: `render()` is synchronous and runs entirely inside the `with` block. A new thread starts with an empty context, so it sees nothing until its own render sets the var.

**Bypass attempts (from a macro imported without context).** Every attempt was blocked or harmless, and all failures surfaced as ConfigError.
- SecurityError:
  - `lake.__class__`, `lake.__slots__`, `lake['__class__']`, `lake|attr('__init_subclass__')`
  - `lake.get.__self__`, `lake.get.__func__`, `lake.get.__globals__`
  - `'{0.__class__}'.format(lake)`, `'{0.get.__globals__}'.format(lake)`
  - `lake.entities.copy()`, `lake.entities.__class__`
- UndefinedError (the proxy has no such attribute):
  - `lake.root`, `lake.root.read_text()`, `lake|attr('root')`, `lake['root']`
  - `lake._current_lake`, `[lake]|sum(attribute='root')`
  - `namespace(p=lake)` then `ns.p.root`, `dict(a=lake).a.root`
  - `lake.get(..).root`
- Other errors: `{% set lake.x = 1 %}` raises TemplateRuntimeError. `lake|tojson` raises TypeError.
- Harmless: `lake|string` renders the proxy's default repr (`<..._LakeProxy object at 0x…>`) and `map(attribute='root')` gives `[Undefined]`. Neither reaches anything callable.
- The proxy holds no state (`__slots__ = ()`), so nothing is reachable through it beyond the three allowlisted names. `lake.entities` returns the same read-only `mappingproxy` of `EntityInventory` that `LakeInventory` exposes.

### Gates
- pytest `-k "UT02_5 or UT02_6 or ST02"` (case-insensitive variants included): 143 passed, 1 skipped (symlinks unavailable on this host).
- `tests/unit/model` plus `tests/integration/model`: 136 passed, 1 skipped. Branch coverage is 100% for sqlfiles, lakeinfo and render_context.
- `ruff check` and `ruff format --check` are clean for `herness/model` and the model tests.
- mypy is clean on `sqlfiles.py` and `test_model_sqlfiles.py`.
- `lint-imports`: 8 contracts kept, 0 broken. `tools.check_type_ownership` exits 0.
- `tools.check_module_size` does not exist in this worktree. `tools/` holds only `check_type_ownership.py`. I checked the budgets by hand (see the Spec table).

### ⚠️
- sqlfiles.py is at 257/260 lines. Any further change to this module needs a budget ruling first.
- A plain import exposes only the `raw` and `lake` globals (plus Jinja builtins) to macros. `dq`, `custom_fields`, `build_id`, `extra_entities` and `raw_root` are still invisible to a macro imported without context. U02-106's macros (`latest`, `typed`, `cast_stats`, `rid`) need only `lake` and `raw`, so this is fine as specced. Still, the T02-12 brief should say that any macro needing other context values must take them as arguments or be imported `with context`.
- Inside a macro, `lake` is a `_LakeProxy`. At the top level of a file, the render argument shadows the global, so `lake` is the real `LakeInventory`. Both carry the same three allowlisted names, so they behave the same; only their `|string` output differs.

### Findings
- Critical: none.
- Important: none.
- Minor: none. `_RUNTIME_ERRORS` (sqlfiles.py:83) is a harmless refactor with the same set of errors as before.

### Assessment (round 2)
**Task quality:** Approved
**Reasoning:** The environment-global `_LakeProxy`, backed by the ContextVar that `raw` already uses, closes N-1. It is safe across threads, resets on both success and exception, and sits under the deny-by-default allowlist, so the sandbox gains no reach. The new UT02-57 case covers a lake-reading macro imported without context in both branches. All gates are green and every module is within budget.
