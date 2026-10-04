# T03-33 report: Promotion commands

## Status: DONE

## What was built

New module `herness/enrich/laya_admin.py` (215/220 lines, budget 220, hard limit 400):

- `accept_model(version, questions=None, *, actor="system") -> None` (U03-138): validates the
  version and directory via `verify_model_dir(require_status={"candidate","accepted"})`, reads
  `eval.json` (2 MB cap), checks it is not stale (`question_set_version` must equal both the
  manifest and the active config; `gold_sha256` must equal `gold_digest` of the current
  gold), resolves the requested question subset (default: every `accepted_proposed` question;
  an id absent from `eval.json` or not `accepted_proposed` is refused with `ConfigError` whose
  `details["refused"]` lists the refused ids; an empty resolved set is also a `ConfigError`),
  writes the manifest atomically (`status="accepted"`, `accepted_questions`, `accepted_by =
  "os:" + getpass.getuser()`, `accepted_at = clock.now()`) and `write_current`, all under
  `data/locks/laya.lock` (`log_lock`, 10 s timeout); audits `admin_action` (`action="laya_accept"`,
  `target=version`, `detail=<sorted accepted ids>`) and logs `enrich.laya.accepted` after
  releasing the lock.
- `rollback_model(to_version, *, actor="system") -> None` (U03-139): `verify_model_dir` with
  `require_status={"accepted"}`, reads the previous `CURRENT` (or `None` if missing/invalid),
  `write_current`, all under the same lock; audits `action="laya_rollback"`,
  `detail=f"from={previous or 'none'}"`; logs `enrich.laya.rolled_back`.
- `laya_status() -> dict[str, object]` (U03-140): lists `laya_root()`, keeps only entries
  matching `^laya-\d{8}-\d+$`, newest first by (date, n) numerically; a manifest that is
  missing/oversized (>256 KB)/schema-invalid, or an entry that is not a plain directory (link
  or OSError on the kind check), yields `{"version", "status": "invalid"}` only; otherwise
  `{version, status, created_at, teacher, accepted_questions, accepted_proposed (sorted
  accepted_proposed qids from eval.json, [] if no eval), macro_metric (None if no eval)}`;
  `eval.json` read is bounded (2 MB) and never hashes weights. Never raises for a single bad
  entry. `current` = `read_current` or `None`.

### Rulings applied (binding, from groups/w13-s03.md)

- `herness/core/audit.py`: added `"laya_accept", "laya_rollback"` to the existing short third
  line of `_ACTIONS` (0 added lines; file stays 391/395 lines). Grepped tests/ for anything
  pinning the exact `_ACTIONS` set: none found, only individual `action="..."` calls.
- `docs/impl/10-config-security-deployment.impl.md` line 1190: appended the two values to the
  `admin_action.action` enumeration, same commit.
- Audit call shape mapped onto the spec-10 schema exactly as specified (`action`, `target`,
  `detail`), not the informal `version=`/`questions=` from spec 03 prose.
- `actor: str = "system"` keyword-only on both public functions (CLI wiring is spec 09,
  per the card).
- Config resolved via `get_config()` (module-level import, so tests can monkeypatch
  `laya_admin.get_config` if needed) -> `EnrichPaths.from_config(cfg)`; active qsv via
  `load_question_set(cfg.decisions).version`; gold via `LabelStore(paths, qsv).read("gold")`.
- Lock `data/locks/laya.lock` via `herness.core.audit.log_lock` (10 s), held through the
  manifest write and `write_current`; audit and the log event happen after the lock is
  released.
- Empty resolved question set -> ConfigError; ids absent from eval.json count as refused.
- `laya_status` bounds: manifest <= 256 KB, eval.json <= 2 MB (spec silent on the exact eval
  cap; chosen generously above the largest fixture eval.json in the repo), no weight
  hashing, newest-first sort, "invalid" for links/unreadable/schema-bad matching entries,
  non-matching directory names skipped entirely.

## Files changed

- `herness/enrich/laya_admin.py` (new, 215 lines)
- `herness/core/audit.py` (+2 tokens on one existing line; still 391 lines)
- `docs/impl/10-config-security-deployment.impl.md` (1 line, admin_action.action enumeration)
- `tests/unit/enrich/_laya_admin_fixtures.py` (new, shared test helpers: real config via
  tests.support.config_tree.write_full_config + init_config, so laya_admin own get_config()
  and herness.core.audit internal one see the same cached config, needed for ST03-13 real
  audit line; a small gold-rows helper; an eval.json writer)
- `tests/unit/enrich/test_laya_admin.py` (new, UT03-130 and UT03-131 plus edge-case tests for
  full branch coverage)
- `tests/unit/enrich/security/test_laya_admin_security.py` (new, ST03-06, ST03-10, ST03-13)

## RED evidence

Ran the new test files against the module before accept_model/rollback_model/laya_status
existed (module absent): ModuleNotFoundError: No module named herness.enrich.laya_admin on
collection. Standard TDD loop was then: write one test group, watch it fail for the right
reason (missing function / wrong error), implement, go green, next group.

## GREEN evidence

PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_laya_admin.py tests/unit/enrich/security/test_laya_admin_security.py -q -p no:logging
  -> 19 passed in 1.5s

PYTHONUTF8=1 uv run pytest tests/unit/enrich tests/unit/core -q -p no:logging
  -> 2117 passed, 2 skipped in 67s

Full suite (uv run pytest -m "(unit or integration) and not slow" -q -p no:logging):
3 failed, 6063 passed, 13 skipped, 35 deselected, 2 xfailed. The 3 failures are exactly the
pre-existing known-red items named in the dispatch (test_it00_01_pre_commit_run_all_files,
test_st10_25_repository_passes, test_st05_13_ast_lint_harness_builds_no_unguarded_clients in
test_llm_anthropic.py), unrelated to this card and present on the base commit.

## Coverage

PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_laya_admin.py tests/unit/enrich/security/test_laya_admin_security.py -q -p no:logging --cov=herness.enrich.laya_admin --cov-branch --cov-report=term-missing

Name                           Stmts   Miss Branch BrPart  Cover   Missing
--------------------------------------------------------------------------
herness\enrich\laya_admin.py     139      0     16      0   100%

100% line and 100% branch coverage on laya_admin.py (gate: >= 90% line / >= 85% branch).

## Gate outputs

- uv run ruff format . : clean (3 files reformatted during development, 0 on final run).
- uv run ruff check herness/enrich herness/core/audit.py tests/unit/enrich tests/unit/core :
  all checks passed. uv run ruff check . (whole repo) shows only the 7 pre-existing TID251
  hits in herness/harness/llm/openai_compat.py and its tests, not in this card scope.
- uv run mypy (project-wide, --strict) : Success, no issues found in 218 source files.
- uv run lint-imports : 13 contracts kept, 0 broken.
- uv run python -m tools.check_type_ownership : clean (no output).
- uv run python -m tools.check_module_size : clean (no output); laya_admin.py is 215/220.

## Deviations / spec notes

- The eval.json size cap (2 MB) and the "eval.json invalid" wording are an implementation
  choice; the brief and spec 03 do not give an exact byte limit for this file (only the 256 KB
  manifest cap is spec-ed, in laya_models.py). Chosen generously since a real eval.json can
  have one entry per question with several float fields.
- laya_status per-entry link/OSError detection uses Path.is_dir() / Path.is_symlink() rather
  than importing laya_models._is_reparse (private, and Windows-junction-aware); this matches
  the read-only, best-effort nature of U03-140 (verify_model_dir remains the strict gate) but
  means a Windows junction masquerading as a directory could in principle pass the
  is_symlink() check on some Python/OS combinations the way laya_models._is_reparse guards
  against for the promotion path. Not a promotion-time hole (verify_model_dir still gates
  accept/rollback); only the informational status view.
- ST03-10 embedding.path half is a thin regression test pinning the existing
  EnrichPaths.embedding_model_dir() containment check (owned by T03-03 layout.py, already
  covered by tests/unit/enrich/security/test_layout_security.py); laya_admin.py itself never
  touches the embedding path, per the ruling.
- UT03-130 audit assertion monkeypatches laya_admin.audit (records the call) per the dispatch
  explicit allowance; ST03-13 uses the real herness.core.audit.audit end to end (real config
  via write_full_config/init_config, real chained JSONL line read back from the configured
  logs directory).

## Concerns

None blocking. One minor judgment call: the laya_status OSError-on-is_dir/is_symlink branch
is exercised in tests via monkeypatching pathlib.Path.is_dir, since provoking a real OSError
from a stat call is not practical on Windows in a unit test; this is a common pattern
elsewhere in the suite for negative-path coverage.

## Commit

5bcadd0 feat(enrich): laya promotion commands (T03-33)
Committed with SKIP=pytest-unit,ruff-check (pre-commit pytest-unit hook otherwise fails on the
pre-existing known-red ST10-25 test_st10_25_repository_passes, unrelated to this card; ruff-check
hook was already passing standalone, skipped defensively per the dispatch allowance).
