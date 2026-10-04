# T03-33 review: Promotion commands (herness/enrich/laya_admin.py)

**Verdict: Approved**

## Spec compliance (per unit / test row)

- ✅ U03-138 `accept_model` — signature `(version, questions=None, *, actor="system") -> None`
  matches (actor added per binding ruling w13-s03 §3, carry-over for CLI T09). Algorithm order
  matches verbatim: validate/`verify_model_dir(require_status={"candidate","accepted"})` →
  read `eval.json` → staleness (`question_set_version` vs manifest vs active config;
  `gold_sha256` vs `gold_digest(LabelStore(paths, qsv).read("gold"))`, i.e. the whole current
  gold) → requested ⊆ proposed (`ConfigError` listing refused ids) → atomic manifest write
  (`replace_atomic`, unhashed by `verify_model_dir`'s `_UNHASHED`) → `write_current` → audit →
  log. `laya_admin.py:155-189`.
- ✅ U03-139 `rollback_model` — `verify_model_dir(require_status={"accepted"})`, `write_current`,
  audit `laya_rollback` with `detail=f"from={previous or 'none'}"`, log
  `enrich.laya.rolled_back`. `laya_admin.py:192-210`.
- ✅ U03-140 `laya_status` — read-only, lists `laya_root()`, skips non-matching names, invalid
  (missing/oversized/schema-bad/link/OSError) entries reduced to `{"version","status":"invalid"}`,
  no weight hashing, newest-first by `(date, n)`. `laya_admin.py:228-268`.
- ✅ UT03-130 — `test_ut03_130_accept_subset_then_refused` matches the row exactly (q1
  proposed/q2 not; accept [q1] then [q2] → CURRENT written, manifest updated, audit called,
  `ConfigError`); plus default-subset, empty-set, missing/oversized/malformed eval edge cases.
- ✅ UT03-131 — `test_ut03_131_rollback_and_status` matches the row (two accepted versions;
  rollback then status lists both, newest first); plus rollback-status-gate, `from=none`,
  skip/invalid, `accepted_proposed`/`macro_metric`, and one real-OSError-on-one-entry test.
- ✅ ST03-06 (TH03-04) — unproposed-question and both staleness axes (gold digest,
  question_set_version) refused with `ConfigError`.
- ✅ ST03-10 (TH03-08) — `accept_model("../../etc")` refused via `EnrichPaths.laya_dir`'s version
  pattern check (`ConfigError`); embedding-path half re-pins the existing
  `EnrichPaths.embedding_model_dir()` containment guard (laya_admin.py never touches the
  embedding path — correct per the ruling, see Minor below).
- ✅ ST03-13 (TH03-11) — real, end-to-end `herness.core.audit.audit` call verified: chained
  JSONL line with `event="admin_action"`, `fields.action="laya_accept"`, `fields.target=version`,
  `fields.detail=["root_cause"]`, `actor="system"`; manifest `accepted_by` starts with `"os:"`.
- ✅ Rulings (w13-s03.md) all honored: `_ACTIONS` extended with 0 line growth (`audit.py` stays
  391/391 lines, confirmed by `wc -l`); impl-10 `admin_action.action` enum mirrored at line 1190;
  audit shape mapped to spec-10 schema (`action`/`target`/`detail`, not spec-03's informal
  `version=`/`questions=`); `actor` kw-only default `"system"`; config via `get_config()` →
  `EnrichPaths.from_config`; lock `data/locks/laya.lock` via `log_lock` held across verify through
  `write_current`; empty resolved question set → `ConfigError`; `laya_status` never hashes
  weights, skips non-version names, shows invalid entries as `"invalid"`.
- ⚠️ Cannot verify from diff/spec: the 2 MB `eval.json` byte cap and its "too large"/"invalid"
  wording are the builder's own choice (spec/brief give no exact limit here, only the 256 KB
  manifest cap elsewhere) — reasonable and explicitly flagged in the report; not a defect.

## Verified independently (not just taking the report's word)

- `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_laya_admin.py tests/unit/enrich/security/test_laya_admin_security.py -q -p no:logging --cov=herness.enrich.laya_admin --cov-branch --cov-report=term-missing` → **19 passed**, `herness/enrich/laya_admin.py` **139/139 stmts, 16/16 branches, 100%/100%** (gate ≥90 %/≥85 %).
- `uv run mypy herness/enrich/laya_admin.py` → **Success, no issues**.
- `uv run ruff check herness/enrich tests/unit/enrich` → **All checks passed**.
- `uv run lint-imports` → **13 contracts kept, 0 broken** (layering intact: laya_admin.py imports
  only `herness.core.*` and sibling `herness.enrich.*` modules — no L4+ import).
- `wc -l herness/enrich/laya_admin.py herness/core/audit.py` → **215** (budget 220, hard limit
  400) and **391** (unchanged line count, confirming the "0 added lines" claim for `_ACTIONS`).
- Cross-read `herness/enrich/laya_models.py`, `herness/enrich/layout.py`,
  `herness/enrich/labels.py`, `herness/enrich/cache.py`, `herness/core/audit.py` to confirm every
  called API's contract (signatures, error messages the tests `match=` against, `LayaManifest`'s
  `accepted`⇒`accepted_by`/`accepted_at` validator, `gold_digest`'s whole-table semantics,
  `_UNHASHED` covering `manifest.json`) — all consistent with the implementation and the rulings.

## Findings

### Critical
None.

### Important
None.

### Minor
1. `laya_admin.py:230-233` (`_status_entry`) — the read-only `laya_status` link check uses
   `Path.is_dir()`/`Path.is_symlink()` rather than `laya_models._is_reparse` (which also catches
   Windows junctions via `FILE_ATTRIBUTE_REPARSE_POINT`, not just `S_ISLNK`). A Windows junction
   could in principle display as a normal (even "accepted") entry in `laya status` even though
   `verify_model_dir` — the actual promotion/load gate — would still refuse it. Purely
   informational-view risk, already called out by the builder in the report; not a promotion-time
   hole. No fix required, but worth a one-line note if `laya status` output is ever trusted for
   more than operator display.
2. `tests/unit/enrich/security/test_laya_admin_security.py:436-444`
   (`test_st03_10_embedding_path_traversal_rejected`) exercises `EnrichPaths.embedding_model_dir()`
   directly, not any `laya_admin.py` code path — `laya_admin.py` never touches the embedding
   path. This is the correct call per the card (`laya_admin` doesn't own that guard) and the
   builder documents it as a thin regression pin, but it means half of ST03-10 as scoped to this
   file is a "still true elsewhere" check rather than new coverage.

## Assessment
**Task quality: Approved**
**Reasoning:** Every U03-138/139/140 postcondition, error path and concurrency rule is
implemented exactly as specced and as ruled by the sub-controller (audit schema mapping, lock
scope, empty-set rejection, status invalid/skip semantics); all required test rows are present
and assert real behavior (manifest contents, CURRENT, real chained audit line); coverage, mypy,
ruff, layering, and the 220-line module budget all pass under independent re-run, not just the
report's claim.
