# T03-24 review: Stable IDs and descriptors

Reviewed: worktree agent-a193c5a4fafe248f1, a46205f..44f4cab (ed616bf, 9427594, 44f4cab). Worktree left clean.

### Spec Compliance
- ❌ Issues found: one design-conformance defect in U03-97 (sub-threshold pairs take part in the Hungarian optimum; see Important 1). Everything else conforms.

| Unit / test | Status | Notes |
|---|---|---|
| U03-96 compute_centroids | ✅ | `np.add.at` per batch, noise (`idx<0`) skipped, eps-normalised; empty cluster -> zero centroid (no NaN); no batches -> (c,1024) zeros. |
| U03-97 match_cluster_ids | ⚠️ | Signature, IdMatch, inherit/revive/new/retired flow, `revive_days` window (`>=` cutoff), ids only from injected `new_id`, no own hashing (R-14), c=0 and no-prev paths all correct; prev/retired rows sorted by id -> exact order invariance. Hungarian-then-threshold can hand a split's id to the *less* similar side (Important 1). |
| U03-98 describe_clusters | ✅ | One CTE query; `n*100 >= size*5`, `row_number` by n desc then id, `<= 5`, empty list not NULL; `members_view` checked against `^[a-z_]{1,32}$` before formatting; duckdb errors -> SchemaViolation (catalog/binder first line = identifiers only; others by class). |
| U03-99 top_terms_ctfidf | ✅ | Placeholder regex removed before `TfidfVectorizer` with the spec's exact parameters; min_df 1 below 2 docs; top 10 weight desc, ties by term; empty vocabulary -> empty lists. |
| U03-100 needs_naming | ✅ | As postconditions; `named_size <= 0` -> True (safe extra). |
| U03-101 representative_texts | ✅ | Stable argsort desc, hash dedup before the `n` cap, `[:max_chars]`. |
| U03-102 name_clusters | ✅ | Sort size desc, id; cap; schema (with/without enum) exact; role `cluster_namer`, temp 0.2, prompt `## System` section; examples via the reused `wrap_untrusted` (R-20); `aretry_call("llm_local", complete_validated, ..., max_repairs=2, breaker_key="decider:llm")`; fallback event WARNING with `cluster_id`, `error_class` only; CircuitOpen stops further calls; labels Cc->space, Cf removed, <= 60 even for a lax client; auto label same cleaning. |
| llm.py `wrap_untrusted` | ✅ | Behaviour of `_wrap` unchanged (same body/rid escaping, `source` default `_SOURCE`, now also attribute-escaped). `__all__ += [...]` passes ruff (RUF022 not triggered) and mypy; no repo test inspects this module's `__all__`. File 349 lines (<= 350). |
| UT03-90 | ✅ | normalised means per cluster over 7-row batches, noise skipped, empty cluster. |
| UT03-91 | ✅ | split, merge, revival at 30 d vs 120 d, boundary day 90, revive_cos 0.89/0.91, below match_cos, first run with `"cl_"+new_ulid()`, empty new. |
| UT03-92 | ✅ | 60/35/5 % order, 4 % excluded, tie by id, cap 5, NULL services, identifier validation, missing table, class-only error. |
| UT03-93 | ✅ | `[PERSON_ab12]`, `[SECRET]`, `[HOST_0f3]`, `[EMAIL_1a]` never terms; <= 10; tie order; empty inputs. |
| UT03-94 | ✅ | cos 0.96/0.94 -> false/true; ratios 0.4, 2.1 -> true; 0.5/2 inclusive. |
| UT03-95 | ✅ | dedup, <= 600, order, n. |
| UT03-96 | ✅ | 600 candidates / cap 500 -> 500 llm + 100 `auto: a / b / c`, exactly 500 requests; request shape; label cleaning; lax client. |
| UT03-97 | ✅ | invalid JSON after 2 repairs (3 calls) -> auto + event; CircuitOpen on call 3 -> it and all later auto, no more calls; no text in any log event. |
| PT03-12 | ✅ | ids unique, disjoint from retired; inherited cos >= 0.85, revived cos >= 0.90 within 90 d; counts consistent; invariant to prev row order; deterministic. (The property does not cover the split-side rule of Important 1.) |

- ⚠️ Cannot verify from diff: none beyond Important 1 (verified by running code).

### Evidence (re-run by reviewer)
- Card tests: 33 passed. `PYTHONUTF8=1 uv run pytest tests/unit/enrich -q -p no:logging`: 716 passed, 1 skipped (symlink privilege).
- Branch coverage: cluster_ids.py 100 % (62 stmts, 12 branches), cluster_describe.py 100 % (174 stmts, 32 branches).
- ruff check / ruff format --check clean; project `mypy` (herness, tools) 0 errors in 263 files; lint-imports 13 kept; tools/check_module_size.py exit 0.
- Mutants (all killed, files restored byte-for-byte, `git status` clean): revive cutoff `>=`->`>`; drop id sort; threshold -0.2; keep calling after CircuitOpen; drop placeholder removal; service tie order; drop hash dedup; max_repairs 1; term tie order; drop R-20 escaping in `wrap_untrusted`; unsorted retired list.

### Strengths
- Clean pure numerics; ids only from the injected factory; deterministic, row-order-invariant output.
- Validated LLM path reuses T03-15's wrapper instead of re-implementing escaping; label bound and control/bidi stripping enforced after validation, so a schema-ignoring client cannot exceed 60 chars.
- Logs/errors carry only cluster_id, error_class, identifiers; tested with a sensitive example string.
- Strong tests: every mutant tried was caught.

### Issues
#### Critical (Must Fix)
- none

#### Important (Should Fix)
1. **Hungarian optimum includes sub-threshold pairs, so a split can hand the id to the less similar side** (plan-mandated literal algorithm, but contradicts design 03 §5.3 step 6 "A split keeps the ID on the side with the higher centroid similarity" and the module docstring's own claim). `herness/enrich/cluster_ids.py:64-69` (`_pairs`). Reproduced: new A has cos 0.90 to P1 and 0.80 to P2; new B has cos 0.86 to P1 and 0 to P2; match_cos 0.85. `linear_sum_assignment(1-C)` picks A-P2 (0.80, rejected) + B-P1 (0.86, accepted) because 0.20+0.14 < 0.10+1.00, giving `ids=('cl_new0','cl_p1')`: the closer side A gets a new id and P2 retires. Fix: mask ineligible pairs before solving, e.g. `cost = np.where(sim >= threshold, 1.0 - sim, 1e6)` (a large finite constant; keep the `sim[r, c] >= threshold` filter), so the solver maximises the number of eligible pairs and then similarity; results are identical whenever all optimal pairs were already eligible. Applies to both the inherit and revive passes (same helper). Add a UT03-91 case with the numbers above asserting `ids == ("cl_p1", <new>)`, and optionally a PT03-12 clause: no unmatched new cluster has an eligible unmatched previous id (maximality). Note the deviation from the literal unit text in the report for the spec owner.

#### Minor (Nice to Have)
1. `tests/unit/enrich/test_enrich_cluster_describe.py:316-318` and `:404-408`: 3 mypy --strict errors (`Collection[str]` / `object` returned where `Reply` is expected). Tests are outside the project mypy scope so the gate is green, but annotate `replies: Iterator[Reply] = iter([...])` and type the `.get(...)` dict as `dict[str, Reply]`.
2. `herness/enrich/cluster_describe.py:268-270`: top terms (derived from ticket text) go into the prompt unwrapped (builder concern 2). Spec wraps only examples and terms are `\w` n-grams, so accepted; consider noting in the prompt that terms are also data.
3. `herness/enrich/cluster_describe.py:365-367`: `ConfigError` for a missing/malformed prompt propagates although the unit says "Errors: none propagate". Acceptable as a deployment fault (message names only the file); it is documented in the docstring; keep it in the report.
4. `herness/enrich/cluster_describe.py:158`: spec-literal `min_df=2` across cluster documents drops terms unique to one cluster (builder concern); raise with the spec owner, no change here.

### Builder concerns
1 accept (0.2 s). 2 accept (Minor 2). 3 accept: stopping calls on AuthError/EgressBlocked etc. is a sound reading of "none propagate"; each still logs the fallback event. Report decisions (request metadata, `_NO_RUN`, prompt parsing, label cleaning, id sorting, inline `core.incident`) accepted.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** All units, tests, gates and coverage are sound, but `match_cluster_ids` lets sub-threshold pairs steer the one-to-one assignment, so a split can give the id to the less similar side, against design 03 §5.3 step 6; the fix is a masked cost matrix plus one regression test.
