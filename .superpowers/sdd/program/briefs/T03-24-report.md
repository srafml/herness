# T03-24 report: Stable IDs and descriptors

Worktree: D:\herness\.claude\worktrees\agent-a193c5a4fafe248f1 (branch worktree-agent-a193c5a4fafe248f1)

## Implemented
- `herness/enrich/cluster_ids.py` (123 lines, budget 220): `compute_centroids` (U03-96, `np.add.at`
  per batch, noise `idx < 0` skipped, eps-normalized; empty cluster -> zero centroid; no batches ->
  1024-wide zeros), `match_cluster_ids` (U03-97, Hungarian on `1 - cos` via scipy, inherit
  `>= match_cos`, then revive on the sub-matrix of retired ids with `retired_at >= now - revive_days`
  and `cos >= revive_cos`, rest `new_id()` in cluster order; `retired` = unmatched previous
  active ids sorted), `IdMatch` frozen dataclass.
- `herness/enrich/cluster_describe.py` (367 lines, budget 380): `describe_clusters` (U03-98, one
  CTE query joining the members view to `core.incident`; share threshold `n*100 >= size*5`,
  `row_number` by share desc then id, `<= 5`, empty list when none; view name checked against
  `^[a-z_]{1,32}$` -> SchemaViolation; duckdb catalog/binder errors -> SchemaViolation with the
  first message line, others by class only; returns `pa.Table` via `to_arrow_table`),
  `top_terms_ctfidf` (U03-99, placeholder regex removed before `TfidfVectorizer` with the spec's
  exact parameters, min_df 1 when < 2 docs, top 10 by weight desc then term, empty vocabulary ->
  empty lists), `needs_naming` (U03-100), `representative_texts` (U03-101, stable argsort),
  `name_clusters` (U03-102) + `NamingCandidate`, `NameResult`.
- `herness/enrich/deciders/llm.py` (349 lines, <= 350): extracted public `wrap_untrusted(text, *,
  record_id="", source="enrich.text_redacted")` (R-20 escaping; `source` also attribute-escaped);
  `_wrap` delegates; added to `__all__` via `__all__ += [...]` to stay within 350 lines.
- Tests: `tests/unit/enrich/test_enrich_cluster_ids.py` (UT03-90, UT03-91, PT03-12),
  `tests/unit/enrich/test_enrich_cluster_describe.py` (UT03-92 ... UT03-97).

## Decisions / spec notes
- Naming: `_system_prompt` takes the `## System` section (up to any next `## ` heading) stripped;
  missing/malformed -> ConfigError naming only the file name. The prompt is read only when a
  client is given and `max_calls > 0`.
- Request: role/model_role `cluster_namer`, temperature 0.2, `thinking="off"`, timeout 120 s,
  `max_output_tokens=256`, schema name `cluster_name`, `request_key = "<null run id>:<i>:cluster_namer"`
  (RequestMeta format), `_NO_RUN` redefined locally (no private cross-module import). User message:
  `Top terms: ...`, `Services: ...` (plain), then each example via `wrap_untrusted(text)`
  (record_id "").
- Fallback: OutputValidationError / ModelUnavailable -> auto for that cluster and continue;
  CircuitOpen -> auto and every later candidate auto without calls. Spec says "none propagate", so
  any other HernessError (AuthError, EgressBlocked, ...) also gives an auto label and stops
  further calls (no retry fixes them). Non-Herness exceptions (bugs) still propagate. Each
  fallback logs `enrich.cluster.naming_fallback` WARNING with `cluster_id`, `error_class` only.
- Labels: control chars (Cc) -> space, format chars (Cf: bidi overrides, zero-width) removed,
  whitespace collapsed, cut to 60 chars; enforced after `complete_validated` too (a lax client
  cannot exceed 60). A label empty after cleaning, a non-JSON reply or an off-list category ->
  OutputValidationError -> auto. Auto labels go through the same cleaning/60-char bound.
- match_cluster_ids sorts previous active and retired rows by id before the assignment, so the
  result is exactly invariant to the input row order (PT03-12 checks this and determinism).
- UT03-92: no shared fixture has `core.incident` with `opened_at` + `service_id`
  (`tests/support/text_warehouse.py` lacks both); the test builds a minimal in-memory
  `core.incident` inline as `test_link_changes.py` does.
- c-TF-IDF note (spec as written): `min_df=2` across cluster documents drops terms unique to one
  cluster; implemented verbatim.
- No pyproject change: import-linter contracts already cover `herness.enrich`; scipy/sklearn
  imports carry local `# type: ignore[import-untyped]`.

## Evidence
- RED: new test modules failed at import (`herness.enrich.cluster_ids` / `cluster_describe` absent)
  before the implementations were written.
- GREEN: card tests `pytest tests/unit/enrich/test_enrich_cluster_ids.py
  tests/unit/enrich/test_enrich_cluster_describe.py` -> 33 passed (UT03-90 ... UT03-97, PT03-12;
  60 hypothesis examples). LLM decider tests after the helper extraction: 43 passed.
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich -q -p no:logging` -> 716 passed, 1 skipped (symlink).
- Coverage (branch): cluster_describe.py 100 % (172 stmts, 32 branches), cluster_ids.py 100 %
  (62 stmts, 12 branches).
- Gates: ruff check / ruff format --check clean; mypy strict 0 errors (263 files); lint-imports
  13 kept 0 broken; check_module_size clean; check_type_ownership clean. Both WIP commits ran the
  real pre-commit hooks (including the repo pytest-unit hook) and passed.

## Commits
- ed616bf wip(T03-24): cluster_ids and shared wrap_untrusted helper
- 9427594 wip(T03-24): cluster_describe descriptors, terms and naming
- 44f4cab feat(enrich): stable cluster ids and descriptors (T03-24) (empty marker commit: all
  content landed in the WIP checkpoints, the tree was clean)

## Concerns
- UT03-96's 600-candidate run takes ~0.2 s with the real sqlite breaker backend; fine.
- Service names and top terms go into the user message unwrapped (spec wraps only examples);
  top terms are \w tokens, service names are CMDB data.
- The stop-on-other-HernessError rule (AuthError/EgressBlocked) is my reading of "none propagate".

## Fix round 1
- I1: `cluster_ids._pairs` now masks ineligible pairs (`sim < threshold`) before
  `linear_sum_assignment`: cost `1 - sim` for eligible pairs, `2 * min(c, p) + 1` for masked ones
  (more than any set of eligible pairs, so the solver maximizes the number of eligible pairs
  first, then total similarity). The `>=` filter after solving is kept. Used by both the inherit
  and the revive pass. cluster_ids.py now 130 lines (budget 220).
  Spec note to U03-97: ineligible pairs are masked before the assignment, following design 03
  §5.3 step 6 (a split keeps the id on the side with the higher similarity).
- New UT03-91 regression `test_ut03_91_ineligible_pair_does_not_displace_the_closer_split_side`
  (A cos 0.90/0.80 to P1/P2, B 0.86/0 to P1/P2, match_cos 0.85 -> A inherits P1, B new, P2
  retired). PT03-12 gains a maximality clause: no retired previous id and unmatched new cluster
  are left with cos >= match_cos.
- m1: annotated the scripted replies in test_enrich_cluster_describe.py (`list[Reply]` /
  `Iterator[Reply]`, `dict[str, Reply]`); mypy --strict on both test files: 0 errors.
- Gates: card tests 34 passed (coverage 100 % line/branch on both modules); tests/unit/enrich
  717 passed, 1 skipped; ruff, ruff format --check, mypy (263 files), check_module_size clean.
- Parked per controller: m2, m3, m4.
- Commit: 6323890 fix(enrich): mask ineligible cluster-id pairs before assignment (T03-24)
