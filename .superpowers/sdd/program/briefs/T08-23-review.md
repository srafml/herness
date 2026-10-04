# T08-23 verify review: bind_core_backends, 08 export maps, import contracts

Worktree agent-af6ca4b3d433f45d9, HEAD ab2039c, base 5f2b7c0. Verified on 2026-10-03.

## Verdict: Needs fixes (1 Important, 4 Minor)

The functionality is correct and every acceptance check passes. One Important gap remains: the R-03 settings contract (UT08-103) is incomplete. It is a cheap fix.

## Spec acceptance checks
- ✅ **Every public name importable from the packages.** I cross-checked the impl §2 public-symbol columns.
  - Resilience exports 52 names: ports 9, _state 5, policies 8, classify 2, events 2, metrics 5, breaker 7, retry 5, faults 4, chain 3, deciders 1, loop_policy 1. The map equals the spec list exactly.
  - Jobs exports 80 names from the §2 rows, plus `SchedCheck` (U08-41) and `SchedulerReport` (U08-72), 82 in total. Those two are types in public signatures and are recorded in the §2 `_exports.py` row, so they are acceptable extras. No private helper is exported, and nothing is missing.
  - Every module-level def or class in design 08 §3 is covered. `JobSpec` and `JobOutcome` live in `herness.core.types`, which is correct.
  - `__all__` equals the sorted map keys, and every name resolves to its owning object (test, plus my own probe).
- ✅ **`import herness.core.resilience.settings` stays light.** It loads only `herness.core.resilience`, `_exports` and `settings`. No `herness.core.jobs` module is loaded at all (probe).
- ✅ **`herness.store.ops` re-exports.** `record_metric_samples`, `purge_metric_samples`, `purge_events` and `bind_core_backends` are all present in `__all__` and as attributes. The block order holds: the UT02-68 tests in tests/unit/store/ops pass.
- ✅ **U08-98 `bind_core_backends`** (herness/store/ops/resilience.py:241-248):
  - each call constructs one `SqliteResilienceBackend` and one `SqliteJobsBackend` and binds both ports;
  - a repeat call replaces both without error;
  - the atexit flush is registered once (test_store_ops_bind).
- ✅ **IT08-04 runs bound through `bind_core_backends`.** It goes via tests/support/worker_bootstrap.py:125 and passes.
- ⚠️ **UT08-103 contracts.**
  - core → no store/harness: unchanged and kept.
  - types.jobs: kept, through `types-jobs-light` plus "core base is closed".
  - httpx: the AST test holds.
  - settings: the R-03 contract is incomplete (I-1).

## Findings

### Important
**I-1. pyproject.toml:536-575 (`resilience-settings-light`): the R-03 settings contract is not complete.**
- UT08-103 says `herness.core.resilience.settings` imports only stdlib, pydantic, `herness.core.types` and `herness.core.errors`, and the new test's docstring claims the same (tests/unit/repo/test_import_contracts_08.py, `test_ut08_103_settings_contract_allows_only_types_and_errors`). The forbidden list still leaves these Herness modules open:
  - herness.core: `config_checks`, `config_sources`, `config_validate`, `config_view`, `_config_sections`, `redact_patterns`, `redact_directory`, `redact_scan`, `_redact_pool`, `egress_log`, `egress_socket`, `egress_clients`, `_egress_scan`, `_egress_transport`, `_egress_streams`, `_egress_source`;
  - herness.core.resilience: `deciders`, `loop_policy`, `_classify_httpx`, `_metric_buffer`;
  - top-level packages: `herness.eval` and `herness.reports`.
- Import-linter matches by package, not by string prefix, so `herness.core.config` does not cover `herness.core.config_checks`.
- Mutations M6 and M6b confirm the gap: adding `import herness.core.redact_patterns`, or `import herness.core.resilience.deciders`, to settings.py leaves lint-imports at 14 kept, 0 broken.
- **Fix, either of:**
  - (a) Add the missing modules, mirroring the list in "core base is closed" (pyproject.toml:329-375) plus the four resilience submodules, and extend `_SETTINGS_FORBIDDEN` to match.
  - (b) Preferable: add an AST allowlist test in UT08-103. It would check that every `herness.*` import in herness/core/resilience/settings.py starts with `herness.core.types` or `herness.core.errors`. This cannot drift as new modules are added.

### Minor
- **M-1. herness/core/jobs/chat_policy.py:160-161.** No test ever calls `_CallableModule.__call__` (coverage misses line 161).
  - `test_cv_t08_23_same_name_exports_are_callable_modules` only asserts `callable(...)`.
  - My probe confirmed that `jobs.chat_policy(x)` delegates to the function.
  - Fix: add a call-through assertion, e.g. monkeypatch the module's `chat_policy` and call the package attribute.
- **M-2. tests/unit/store/ops/test_store_ops_bind.py:46-52.** `test_cv_t08_23_package_reexports` checks object identity only for `bind_core_backends` and `purge_events`. For `record_metric_samples` and `purge_metric_samples` it checks only `__all__` membership. Add the `is herness.store.ops.metrics.<name>` checks.
- **M-3. tests/unit/store/ops/test_store_ops_bind.py:19, 32, 47.** These unit tests cite "IT08-04 (cv)" in their docstrings. "U08-98 (cv)" or "UT08-29/U08-10 (cv)" would be more accurate, since IT08-04 is the integration row.
- **M-4. docs/impl/08-resilience-and-jobs.impl.md:73 and :86.** The two package `__init__` rows still say "§3.3–§3.8" and "§3.9–§3.15". The impl numbering is resilience §3.3–§3.9 and jobs §3.2 (validate), §3.3 (jobs ports) and §3.10–§3.17. The new `_exports.py` rows state the real scope, so this is only cosmetic, but rewording the two rows would remove the contradiction.

## Rulings on builder concerns
- **(a) chat_policy.py `_CallableModule` (file outside the card's list): accepted.**
  - It is identical in form to the precedents in classify.py:219-226 and breaker.py:360-367. It is needed because the U08-75 function shares its name with the submodule, and importing the submodule overwrites the package attribute.
  - Behaviour is unchanged: all chat_policy tests pass (tests/unit/core/jobs green), `jobs.chat_policy is herness.core.jobs.chat_policy` holds, and calling the module delegates to the function.
  - The file is 164 lines against a budget of 170.
  - The UT08-63 edit (test_jobs_ports.py:187-189) is a pure adaptation. It changes the expected value only for `name == submodule`, which only the newly added `chat_policy` entry hits. Every other name keeps the strict `is getattr(owner, name)` check.
- **(b) `ops-areas-acyclic` ignore `herness.store.ops.resilience -> herness.store.ops.jobs`: accepted as the minimal edge.**
  - U08-98 fixes the location (store/ops/resilience.py) and requires `SqliteJobsBackend`.
  - Import-linter also sees function-local imports, so a lazy import would not avoid the ignore.
  - I grepped store/ops/jobs.py, worker.py, tasks.py and _job_sql.py: none imports `.resilience` or the ops package, so no cycle arises.
  - No other contract was weakened. The other pyproject changes only add forbidden entries and one new contract.
- **(c) Export maps split by owning package: accepted.** The impl §3 numbering does not match the §2 row ranges. Splitting by owning submodule is the only consistent reading, and the `_exports.py` §2 rows document it. See M-4 for the cosmetic follow-up.
- **(d) Budget: accepted.**
  - At base, the spec §2 row for resilience/__init__ is already 120, so the brief's 70 is stale.
  - The file is now 30 lines, and jobs/__init__ is 32 against 90.
  - The new `_exports.py` rows (160 and 230) were added to docs/impl/08 §2 in the same change; the files are 146 and 218 lines.
  - `tools.check_module_size` exits 0.

## Test hygiene
- **IDs and markers:** test names carry `cv_t08_23` or `ut08_103`, docstrings cite IDs, and `pytestmark = pytest.mark.unit` is present.
- **RED-then-GREEN is credible:** the new tests assert names, attributes and contracts that did not exist at base (bind_core_backends, the 34 extra resilience and 64 extra jobs entries, the types-jobs-light contract).
- **Fixtures:** `_queue_env.jobs_db` and the resilience `conftest.ops_db` now call `bind_core_backends()`, which matches §11 `ops_db` ("bound with U08-98"). The removed casts and type-ignores are correct, and the suites stay green.

## Runs
- **Card tests plus tests/integration/jobs** (pandas imported first): 52 passed.
- **tests/unit/core/resilience, tests/unit/core/jobs, tests/unit/store/ops, tests/unit/repo, test_exports_08.py and tests/integration/jobs, with coverage:** 1757 passed, 1 skipped (symlink privilege).
- **Coverage:** herness.core.jobs, herness.core.resilience and herness.store.ops total 99% line+branch. chat_policy.py is at 99% (line 161, see M-1). The changed `__init__`, `_exports` and store/ops/resilience modules are at 100%.
- **Known crash, not counted:** tests/integration/jobs run without pandas pre-imported hits a stack overflow in test_resilience_retry_storm.py::test_st08_05. This is the known freezegun/pandas issue, and I reproduced it on an archive of base 5f2b7c0.
- **Gates:**
  - ruff check: clean.
  - ruff format --check: 974 files already formatted.
  - mypy: no issues in 356 files.
  - lint-imports: 14 kept, 0 broken.
  - check_type_ownership: exit 0.
  - check_module_size: exit 0.

## Mutations (all reverted; at the end the worktree status is clean and the diff is empty)

| # | Mutation | Result |
|---|---|---|
| M1 | Eager `import herness.core.jobs.supervisor` at the top of resilience/__init__ | RED: conftest ImportError (circular import) |
| M1b | Eager `import herness.core.resilience.retry` at the top of resilience/__init__ | RED: same circular import |
| M1c | Eager `import herness.core.jobs.supervisor` at the top of jobs/__init__ | RED: `package_import_loads_no_submodule` |
| M1d | Eager `import herness.core.resilience.ports` at the top of resilience/__init__ | RED: `package_import_loads_no_submodule` and `settings_import_stays_light` |
| M2 | Drop `record_event` from the resilience `EXPORTS` | RED: `export_map_is_the_spec_list[resilience]` and `every_name_resolves_to_its_owner[resilience]` |
| M3 | Drop `bind_jobs_backend(...)` from bind_core_backends | RED: 2 bind tests |
| M4 | Drop `bind_ops_backend(...)` from bind_core_backends | RED: 2 bind tests, plus IT08-04/06/09/11 in test_jobs_worker.py |
| M5 | settings.py imports herness.core.time | RED: lint-imports, 2 broken (`resilience-settings-light`, `settings modules are leaves`); UT08-103 `lint_imports_passes` fails |
| M6 | settings.py imports herness.core.redact_patterns | **GREEN, a gap (I-1)** |
| M6b | settings.py imports herness.core.resilience.deciders | **GREEN in lint-imports, a gap (I-1)**. The runtime subprocess test would still catch it. |
| M7 | types/jobs.py imports herness.core.time | RED: 2 broken; UT08-103 `lint_imports_passes` and `types_jobs_imports_allowed` fail |
| M8 | types/jobs.py imports herness.core.types.memory | RED: `types-jobs-light` broken |
| M9 | `httpx.Client()` constructed in herness/core/jobs/inline.py | RED: `test_ut08_103_jobs_never_construct_httpx_clients` |

## Re-verify fix round 1
Scope: I-1 and M-1 to M-4 only. Worktree agent-af6ca4b3d433f45d9, HEAD 47bd5a4, fix range ab2039c..47bd5a4.

### Per finding
- **I-1 ✅**
  - **Contract.** `resilience-settings-light` (pyproject.toml:535-598) now forbids:
    - all 9 top-level packages except `herness.core` (connectors, enrich, eval, harness, metrics, model, reports, store);
    - every herness.core module and package except types and errors;
    - every herness.core.resilience module except settings.
  - **Tree check.** The list matches `ls herness`, `ls herness/core` and `ls herness/core/resilience`.
  - **Contract scope.** The pyproject diff against ab2039c is a single hunk inside this contract, so no other contract changed.
  - **Tests.**
    - `_settings_forbidden()` derives the expectation from the tree, at test_import_contracts_08.py:54-74 and 124.
    - The new AST allowlist tests are `test_ut08_103_settings_imports_allowlist`, `_rejects` (6 cases) and `_accepts`.
  - **Mutation 1** (`import herness.core.redact_patterns` in settings.py): **RED**.
    - lint-imports reports 13 kept, 1 broken (`resilience-settings-light`, l.27).
    - pytest reports 2 failed: `settings_imports_allowlist` and `lint_imports_passes`.
  - **Mutation 2** (`import herness.core.resilience.deciders`): **RED**.
    - lint-imports reports 13 kept, 1 broken.
    - The normal pytest run aborts at collection: conftest → config → settings → deciders → `from herness.core.config import get_config` raises a circular ImportError, and the exit code is non-zero.
    - With `--noconftest`: 2 failed, the same two tests.
  - **Ruling on the abort.** A red-by-abort is acceptable. The gate fails loudly either way, lint-imports names the exact edge, and the `--noconftest` run shows the allowlist test also catches it. The cycle is a runtime consequence of the forbidden edge, not a hole in the tests.
  - **AST `from X import Y` probe.** Every case resolves correctly:

    | Import | Result |
    |---|---|
    | `from herness.core import config` | herness.core.config, rejected |
    | `from herness import core` | herness.core, rejected |
    | `import herness` / `import herness.core` | rejected |
    | `from herness.core import errors, config` | errors allowed, config rejected |
    | `from .. import errors` | allowed |
    | `from ..types import jobs` | allowed (base herness.core.types) |
    | `from herness.core.typesx import A` | rejected (no prefix bleed) |
    | `import pydantic.fields` | allowed |
    | `import httpx` | rejected |

  - **Probe 3** (empty `herness/core/zz_probe.py`): **RED**. `test_ut08_103_settings_contract_allows_only_types_and_errors` fails, naming `herness.core.zz_probe`. The file was deleted afterwards.
- **M-1 ✅** `test_cv_t08_23_chat_policy_module_call_delegates` (tests/unit/core/test_exports_08.py:140-152) calls `jobs.chat_policy(now)` through `_CallableModule.__call__` with the function monkeypatched.
  - Mutation: `__call__` changed to `return "live"` without delegating. Result: **RED**, that test fails. Reverted.
- **M-2 ✅** test_store_ops_bind.py:54-55 adds `ops.record_metric_samples is metrics_area.record_metric_samples` and the matching `purge_metric_samples` identity check.
- **M-3 ✅** The three docstrings (lines 20, 33, 48) now cite "U08-98 (cv)", and no "IT08-04" remains in the file.
- **M-4 ✅** Rows 71 and 87 of docs/impl/08 now say:
  - resilience: §3.3–§3.9;
  - jobs: §3.2 (`validate`), §3.3 (`ports`) and §3.10–§3.17.

  These match the headings: 3.2 Configuration models (U08-67 validate_windows at line 311), 3.3 Ports (U08-41 jobs ports at line 428), 3.9 Repair/chains/deciders/loop policy, and 3.10–3.17 for the jobs units.

### Tests and gates
- **Tests:** tests/unit/repo, tests/unit/store/ops, tests/unit/core/test_exports_08.py and tests/unit/core/jobs (which includes test_jobs_chat_policy.py): **1179 passed**, with pandas pre-imported.
  - Without the pre-import, the tests/unit/core/jobs directory run hits the known freezegun/pandas Windows stack overflow (dateutil tz → pandas import under a frozen clock, via store/vectors → lancedb).
  - That is the same known environment issue noted in the original review. The fix range touches no file under tests/unit/core/jobs or herness/.
  - Run separately without the pre-import, everything passes: repo + store/ops + exports_08 gave 557 passed, and test_jobs_chat_policy.py gave 16 passed.
- **Gates:**
  - ruff check: clean.
  - ruff format --check: 974 files already formatted.
  - mypy: no issues in 356 files.
  - lint-imports: 14 kept, 0 broken.
  - check_type_ownership: exit 0.
  - check_module_size: exit 0.
- **Revert check:** all probes were reverted. At the end `git status` is clean, `git diff` is empty, and HEAD is still 47bd5a4.

### New findings
None (Critical 0, Important 0, Minor 0).

### Verdict
**Approved**
