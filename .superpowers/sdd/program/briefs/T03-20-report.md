# T03-20 report — Resolve stage (U03-81 … U03-83)

Status: DONE_WITH_CONCERNS (one workaround for a U03-148 defect, see Deviations 2)
Worktree: D:\herness\.claude\worktrees\agent-a3c6bc561c79e5bcc (base 750ec27)
Commits: 567deca wip(T03-20): spot-checks, decision_wide_sql, run_resolve; fc919b7 feat(enrich): resolve stage, decision_wide view and spot-checks (T03-20).

## Implemented
- `herness/enrich/resolve.py` (348/380): `decision_wide_sql` (U03-82; ids re-checked with
  `re.fullmatch("[a-z][a-z0-9_]{1,40}")`, double-quoted identifiers, single-quoted literals,
  non-pair questions in set order; ConfigError without echoing the id), `run_resolve` (U03-83:
  DDL built first so a bad id writes nothing -> `sync_label_checks` -> `resolve_frame` -> one
  `INSERT … SELECT … WHERE status='final'` into `enrich.decision` -> `decision_wide` DDL ->
  per-entity stats -> spot-checks via `open_label_counts` + `select_spot_checks` +
  `create_if_absent` (now=`clock.now()`), log `enrich.spot_check.created{question,count,purpose}`
  per question -> report `decided`/`escalated` (+=), gauges `herness_enrich_coverage_ratio{entity}`
  (final / (final+queue), entities with in-scope pairs only) and
  `herness_enrich_escalation_share_ratio` (escalated/decided, only when decided > 0), log
  `enrich.resolve.completed{decided, escalated, coverage}`; returns None). `report` is typed with a
  module-private Protocol `_Report` (decided, escalated) per the sub-controller ruling.
  `select_spot_checks` is re-exported from `_spot_checks` (public name stays `resolve.select_spot_checks`).
- `herness/enrich/_spot_checks.py` (137/150, NEW private sibling): U03-81. One DuckDB query over
  `enrich_resolved` + a registered `spot_meta` (question, threshold, n_cap): N_new = final rows,
  decider <> 'human', decided_at >= since; n = least(n_cap, floor(rate x N_new)) with n_cap =
  min(nightly_max_per_question, max(0, open_cap_per_question - open_counts[q])); candidates ordered
  by DuckDB `sha256(build_id|content_hash|question)`; SQL returns only the first n rows by hash and
  the first n band rows; Python `_pick` takes n//2 uniform, then n - n//2 band rows not already
  chosen, then fills from the uniform pool. Payload per design 03 §4.6 (purpose spot_check,
  text_ref enrich.text_redacted, no text). SchemaViolation on DuckDB errors.
- `docs/impl/03-enrichment.impl.md` §2: new row for `_spot_checks.py` (T03-20 spec note, budget 150).
- `.secrets.baseline`: line-number shift of the existing audited docs/impl/03 entry (hook regen; no entries dropped).

## Tests
- tests/unit/enrich/test_resolve_stage.py: UT03-77 (7 functions: 10,000 rows/open 290 -> 10 picks,
  5 uniform + 5 band, reference picks recomputed with hashlib, deterministic across runs and
  connections, other build_id differs; payload shape; band shortfall fill; caps 50/open-cap/odd n;
  duplicate content hash; per-question set order; missing frame SchemaViolation), UT03-78 (exact DDL,
  runs twice on DuckDB, wide row values, pair question excluded).
- tests/unit/enrich/security/test_resolve_security.py: ST03-19 (6 parametrized injected ids incl.
  `a"; DROP` -> ConfigError, id not echoed; valid id quoting).
- tests/integration/enrich/test_resolve_stage_flow.py: IT03-06 (3 functions, real ops store + cache +
  label store: laya above threshold, below with openjev escalation, queued, pending label_check,
  approved correction folded in by sync -> escalated/review_status/decider/probability/decided_at per
  §4.1; decision_wide; report 5/2; gauges incident 0.8, change 1.0, share 0.4; 4 spot-check items;
  logs/payloads free of text; rerun creates 0 duplicates; bad id writes nothing).
- RED: `pytest tests/unit/enrich/test_resolve_stage.py tests/unit/enrich/security/test_resolve_security.py`
  -> 15 failed (AttributeError: no select_spot_checks / decision_wide_sql) before implementation.
- GREEN: `-k "UT03_77 or UT03_78 or ST03_19 or IT03_06" --require-test-ids` 18 passed;
  `pytest tests/unit/enrich tests/integration/enrich` 557 passed, 2 skipped (platform/laya).
- Coverage: _spot_checks.py 100 %, resolve.py 98 % line/branch (missing: run_resolve DuckDB error
  path, decided == 0 branch).
- Gates: ruff format/check clean on touched files, mypy (223 files) clean, lint-imports 13 kept,
  check_type_ownership 0, check_module_size 0.
- Commit hook: pytest-unit fails only on KNOWN-RED ST10-25 (openai_compat); committed with
  `SKIP=pytest-unit,ruff-check` after running ruff check on my own files myself.

## Deviations / spec notes
1. Private sibling `_spot_checks.py` (resolve.py would have been 438 lines): §2 row added in the same commit.
2. match_keys: U03-83 says `match_keys=("purpose","question_set_version","question","content_hash")`
   with `scope={"question_set_version": qsv}`. `review_items.create_if_absent` (U03-148, not mine to
   edit) passes `match_keys + tuple(scope)` to impl 02 without dedup, and impl 02
   `create_review_item_if_absent` rejects duplicate keys (ConfigError "match_keys must be 1-8
   distinct keys"). The spec'd call therefore always fails. Workaround: `match_keys=("purpose",
   "question","content_hash")` + the same scope -> identical combined store match keys and scope
   validation; in-call dedupe equivalent since all payloads share the version. Carry-over: fix
   U03-148 to dedupe `match_keys + scope` (then restore the literal match_keys), owner of review_items.py.
3. Spot-check candidates are deduplicated per (question, content_hash) (lowest record_id), because the
   review-item match key is content_hash: two records with identical content would otherwise be one
   suppressed pick. N_new still counts rows.
4. `since` for spot-checks = `run_started_at` (rows decided during this run are "newly decided").
5. `enrich.decision` is written with one INSERT (spec); no DELETE first — a stage re-run on the same
   build file would duplicate rows (build file is new each run per impl 03 §4.1). Flag if resume on
   the same build file is expected.
6. U03-83 Tests row also names IT03-04 (pipeline, later card) — not in this card's Tests row.

## Carry-overs
- U03-148 create_if_absent: dedupe scope keys against match_keys (see 2).
- T03-xx pipeline: retype `_Report` to StageReport (U03-142); EnrichReport.coverage /
  escalation_share can read the same stats (currently only gauges + log).

## Fix round 1 (review T03-20-review.md, Approved with Minors)
- M2: `_INSERT_SQL` now names the 12 target columns of `enrich.decision` explicitly.
- M3: two new IT03-06 functions — DuckDB error while writing (dropped `enrich.decision`) raises
  `SchemaViolation("run_resolve: …")` and creates no spot-check item; a build with no final pair
  leaves counters 0, sets coverage 0.0 and no escalation-share gauge. resolve.py coverage now 99 %
  (only the T03-19 branch 169->165 in `_pending_items_table` remains partial); _spot_checks.py 100 %.
- M5: spec notes added in docs/impl/03-enrichment.impl.md after U03-81 (dedupe per (question,
  content_hash), lowest record_id) and after U03-83 (match_keys without question_set_version because
  U03-148 appends scope keys without dedupe, restore once fixed; since = run_started_at; explicit
  INSERT column list).
- M1, M4 parked per review.
- resolve.py 349/380; tests/unit/enrich + tests/integration/enrich: 559 passed, 2 skipped;
  ruff/format/mypy clean; check_module_size 0.
- Fix commit: 1898a15 fix(enrich): resolve stage review fixes (T03-20) (SKIP=pytest-unit,ruff-check; hook failed only on known-red ST10-25; .secrets.baseline line shift 3608->3612, LF kept).
