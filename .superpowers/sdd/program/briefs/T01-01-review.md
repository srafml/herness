# T01-01 review: Common settings models (commit 39c65e8)

### Spec Compliance
- ✅ U01-01 AuthSettings: fields, `SECRET_REF_PATTERN` validator with the exact message, `none`/required rules with the exact messages, tenant_id GUID rule (paths `auth.tenant_id`). Frozen, extra=forbid.
- ✅ U01-02 ReconcileSettings: default `"0 3 * * SUN"`, five-field rule, `0 < max_delete_pct <= 100`, messages name `reconcile.*`.
- ✅ U01-03 BackfillSettings: `start >= 1970-01-01`, `1 <= slice_days <= 366`, `resolve_start` (1096 d, UTC midnight, `ConfigError("naive datetime")`, `ConfigError("backfill.start is in the future")` on `>= now`).
- ✅ U01-04 EntitySettings: three optional fields, overlap bound, `None` defaults.
- ✅ U01-05 SourceSettings: every field/default/bound in the table; `checkpoint_rows >= batch_rows`; auth-method check incl. the exact `oauth_3lo` message; `verify` bool/missing-path rejection with the exact TLS message; `hosts` rule (lower-case, regex, no IP, no dup, <= 50, index-only messages); base_url rule; all six methods with the specified behaviour.
- ✅ U01-06 constants: all six values match the brief verbatim; `MappingProxyType` / `frozenset`.
- ✅ R-03 import rule: fresh-interpreter check (`import herness.connectors.settings_base`) loads only `herness`, `herness.core`, `herness.core.errors`, `herness.connectors*`, pydantic and its own deps (`pydantic_core`, `typing_extensions`, `annotated_types`, `typing_inspection`).
- ✅ Tests UT01-01, UT01-03, UT01-04, UT01-05, ST01-01 (config part) exist, IDs in names/docstrings, `pytestmark = unit`.
- ✅ Contracts: `herness.connectors` added to C1 above `herness.core` in the same commit; C4 "core base is closed" matches impl 00 U00-52 (C4 = the seven C3 modules, forbidding every other existing C1 package).
- ⚠️ Cannot verify from diff: gates (ruff, mypy, lint-imports, 100 % coverage, 169 passed) are taken from the report; ST01-01 connection part (SourceUnavailable on a self-signed TLS server) is deferred to the HTTP/egress cards; T01-02 must add C6 listing `settings` and `settings_base` and widen the UT00-58 glob / OWN050 to cover `settings_base.py`.

### Declared deviations: rulings
1. `hide_input_in_errors=True` - **acceptable.** Needed for UT01-01 (pydantic echoes `input_value` otherwise); strengthens TH01-03. T10-03 must still not copy `errors()[i]["input"]` into `ConfigError`.
2. Strict floats instead of `Field(strict=False)` - **acceptable (spec-text divergence).** Impl 01 §2 line 107 says float fields use `Field(strict=False)` *so YAML ints are accepted*; pydantic strict float already accepts ints (verified: `timeout_s: 5` -> `5.0`), and strict additionally rejects `True`/`"2.0"`. The stated reason is met with a tighter result. Record it as a spec amendment so T01-02 follows the same convention.
3. Base `page_size` default 1000, `ge=1` - **acceptable.** Spec says "subclass default / subclass bounds"; a base default is needed for models that do not redeclare it. See Minor M3 on the `<= inf` message.
4. `max_concurrency` default via `mode="before"` validator - **acceptable, with side effects** (Minor M2): JSON schema advertises `default: 0`, and `max_concurrency` is always in `model_fields_set` (`model_dump(exclude_unset=True)` -> `{'max_concurrency': 4}`).
5. 312 lines vs 260 budget - **defect, Minor** (M1). Under the 400 hard limit, but 20 % over the §2 budget.
6. No C1 ignore for `settings_base`, C6 deferred to T01-02 - **acceptable.** UT00-58 requires C1 `ignore_imports` to hold exactly the settings exception and C6 to exist iff a `settings.py` exists; T01-02's Files row carries the C6 change (O-15).
7. C4 added forbidding `herness.connectors` - **acceptable, required** by U00-52 C4 definition.
8. ST00-10 edited - **acceptable.** The edit only makes the pyproject rewrite robust to a multi-layer C1 and an existing C4; the expected result (non-zero exit naming C1 and C4) is unchanged, and it now asserts the rewrite took effect.
9. Schedule separators space/tab only, anchored - acceptable (stricter reading of "whitespace-separated").

### Strengths
- Every validator message names the key and never echoes the value; `hide_input_in_errors` closes the pydantic echo path; tests assert absence of `synthetic-*` values.
- `_range`/`_schedule` factories keep bound checks DRY; NaN/inf are rejected by the comparison form.
- base_url check handles `urlsplit` port `ValueError`, userinfo/query/fragment, non-printables, and loopback-only http.
- Tests cover boundaries both sides (caps, 1440, 100, 366, 1970-01-01, midnight == now).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
- **I1. The third-party half of the R-03 `sys.modules` test asserts nothing.** `tests/unit/connectors/test_settings_base.py:429-444` purges only `herness*` from `sys.modules`; pydantic and any other third-party package already imported by the test session (pydantic at line 12, plus whatever other tests load) are cached and never appear in `loaded`. A `import yaml` (or any lib pulled transitively via a herness module) would pass line 443-444 unnoticed. The allowlist is also incomplete for a fresh interpreter (`typing_inspection` is loaded there), which confirms the check has never exercised a real import of pydantic. This is the card's explicit acceptance check ("asserted by a test with `sys.modules`"). Fix: run the import in a subprocess (`sys.executable -c "import sys; import herness.connectors.settings_base; print(sorted(sys.modules))"`) and compare against stdlib + `{pydantic, pydantic_core, typing_extensions, annotated_types, typing_inspection}` + the allowed herness modules. The AST test (447-462) remains a useful direct-import check but does not cover transitive imports.

#### Minor (Nice to Have)
- **M1.** `herness/connectors/settings_base.py` is 312 lines vs the §2 budget of 260 (limit 400). Either trim (e.g. the `_check_common` elif chain, module docstring, `_range` signature formatting) or record a budget amendment for the controller.
- **M2.** `herness/connectors/settings_base.py:243,252-261`: the placeholder `max_concurrency = 0` leaks into `model_json_schema()` (`default: 0`) and marks the field as explicitly set on every instance. A `__pydantic_init_subclass__` hook that sets the subclass field default from `CONCURRENCY_DEFAULTS[SOURCE]` (plus `model_rebuild`) would avoid both; or document it for T10-03 / any `exclude_unset` consumer.
- **M3.** `herness/connectors/settings_base.py:74,238`: with `high=math.inf` the message reads `page_size must be >= 1 and <= inf`. Omit the upper half when `high` is infinite.
- **M4.** `herness/connectors/settings_base.py:249`: `entities` is a plain mutable `dict` inside a frozen model (`src.entities["b"] = ...` succeeds, verified). The unit invariant is "Frozen / Immutable"; consider wrapping in `MappingProxyType` via an after-validator (as the constants do).
- **M5.** `herness/connectors/settings_base.py:224,297-300`: entity `page_size` has no bound at all (`-5` accepted and returned by `page_size_for`). Spec delegates bounds to the source subclass, so this is in scope for T01-02, but the base could cheaply enforce `>= 1`; at minimum T01-02 must check it.
- **M6.** `tests/unit/connectors/test_settings_base.py:393`: `.replace("input_value", "")` is dead with `hide_input_in_errors=True`; it weakens rather than strengthens the assertion. Drop it.
- **M7.** `tests/unit/connectors/test_settings_base.py`: 462 lines, above the ENG §2.4 400-line module length if the CI script applies to tests; consider splitting (e.g. `test_settings_base_source.py`).
- **M8.** `tests/unit/connectors/test_settings_base.py:427,447`: the R-03 import tests are tagged UT01-01; they are selected by the acceptance `-k`, but an explicit mention of "R-03" in the docstring (already present) is the only link. Fine as is; noting only for traceability.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The models match every value and rule of U01-01..U01-06 and the declared deviations are acceptable (budget overrun Minor), but the card's explicit `sys.modules` acceptance check is vacuous for third-party imports (I1) and must run in a fresh interpreter.

## Round 1 re-review (fix commit 1efbeb5, diff 39c65e8..1efbeb5)

### Prior findings
- **I1 ✅ resolved.** `tests/integration/connectors/test_settings_base_imports.py` runs `import herness.connectors.settings_base` in a fresh `sys.executable` subprocess (timeout 120 s, per ENG "every subprocess has a timeout"), diffs `sys.modules` before/after, asserts `pydantic` is observed (proves third-party imports are not cached away) and every new module is stdlib, the pydantic family (incl. `typing_inspection`) or an allowed herness module. Mutation check: probe with an extra `import yaml` yields disallowed `yaml`, `yaml._yaml`, ... so the test now fails on a transitive leak. Placement under `tests/integration` is correct: `pyproject.toml:188` defines `unit` as "no subprocess", and global constraints require the file-level marker to match. The unit file keeps the AST direct-import check (`test_settings_base_auth.py`, `test_ut01_01_r03_settings_base_source_imports`) with a docstring pointer to the runtime half. Consequence already recorded as a ruling (`groups/w01-s01.md:13`): the brief's acceptance command `pytest -m unit -k ...` no longer runs the runtime R-03 check; the acceptance clause "asserted by a test with `sys.modules`" is met by the integration test. Acceptable.
- **M1 ✅ resolved as a recorded amendment.** `settings_base.py` is 316 lines (budget 260, hard limit 400). The trim work was done (`_rule` factory folds the credential/start/schedule/range checks, `_check_rules` single-raise chain, constants inlined), and the net growth is the M2/M4 fixes (`__pydantic_init_subclass__`, `_ReadOnlyDict`). One cohesive module under the ENG hard limit; splitting would scatter the closed-set constants from the models. Acceptable provided the controller records the budget amendment (260 -> 320) in the impl 01 §2 module map / program log.
- **M2 ✅ resolved.** `max_concurrency` is now a required base field whose default is set per subclass in `__pydantic_init_subclass__` (`settings_base.py:251-258`). Verified: `_ServiceNow().model_dump(exclude_unset=True) == {}`, JSON schema `default: 4`, subclass-of-subclass inherits 4, `jira` gets 2, base `FieldInfo` stays required and is not shared between subclasses (`A.model_fields[...] is B.model_fields[...]` -> False). Covered by `test_ut01_04_concurrency_default_is_a_real_default`.
- **M3 ✅ resolved.** `_range` omits the upper half when `high` is infinite; `test_ut01_05_unbounded_range_message` asserts `page_size must be >= 1` without `<= inf` and keeps the two-sided message for `timeout_s`.
- **M4 ✅ resolved (see new Minor N1).** `entities` becomes a `_ReadOnlyDict` (after-validator at `settings_base.py:280`, default factory `_ReadOnlyDict`); `__setitem__`, `__delitem__`, `__ior__`, `clear`, `pop`, `popitem`, `setdefault`, `update` raise `TypeError`. `model_dump`/`model_dump_json` round-trip verified by test.
- **M5 — parked** (carry to T01-02: bound entity `page_size`).
- **M6 ✅ resolved.** `.replace("input_value", "")` removed (`test_settings_base_models.py`, `test_ut01_05_base_url_rejected`).
- **M7 ✅ resolved.** Tests split into `test_settings_base_auth.py` (221 lines) and `test_settings_base_models.py` (273 lines); every test kept, IDs and `pytestmark = unit` preserved.
- **M8 ✅ resolved.** R-03 tests are named `..._r03_...` and docstrings say "(R-03 acceptance check)".

### Regression check
- `uv run pytest tests/unit/connectors tests/integration/connectors -q -p no:logging`: 69 passed.
- `uv run mypy`: no issues (14 files). `ruff check` / `ruff format --check`: clean. `lint-imports`: 5 kept, 0 broken.
- Unit coverage of `herness.connectors`: 100 % statements and branches (195 stmts, 46 branches).

### New issues
#### Critical
None.
#### Important
None.
#### Minor
- **N1.** `herness/connectors/settings_base.py:139-147`: `_ReadOnlyDict` has no `__reduce__`/`__deepcopy__`, so `copy.deepcopy(src)`, `src.model_copy(deep=True)` and `pickle.dumps(src)` raise `TypeError: settings mappings are read-only` for any source with a non-empty `entities` (dict reconstruction calls the refused `__setitem__`). Shallow `model_copy(update=...)` works. No spec path deep-copies or pickles source settings today (impl 01 line 308 uses `model_copy(update=...)` on entity slices; ENG forbids pickle), so latent only. Cheap fix: `def __reduce__(self): return (_ReadOnlyDict, (dict(self),))`, plus a one-line test; do it now or in T01-02.
- Observation (no finding): `__pydantic_init_subclass__` sets the default unconditionally, so a subclass that redeclares `max_concurrency` with its own default is overridden by `CONCURRENCY_DEFAULTS[SOURCE]`. That matches the spec (default is the table value); T01-02 subclasses must not redeclare it.

### Assessment
**Task quality:** Approved
**Reasoning:** I1 is fixed with a real fresh-interpreter check whose integration placement is required by the `unit` marker and already recorded as a ruling. All Minors are resolved (M1 via a budget amendment the controller should record). No regressions in tests, typing, lint or import contracts. N1 is a new latent Minor and does not block.
