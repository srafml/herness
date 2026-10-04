# T03-27 review: Mapping suggestions (herness/enrich/mapping_suggest.py)

Reviewed: worktree agent-aebe6290afe6fac15, head 75e4416 (base 667fcf4). Read-only; working tree left clean.

## Verdict
**Task quality: Approved** (no Critical or Important findings; three Minor items).

## Evidence gathered by the reviewer
- Card tests (unit + IT03-14 + ST03-12) with `--cov=herness.enrich.mapping_suggest --cov-branch`: 16 passed, 100 % line / 100 % branch (182 stmts, 20 branches).
- `tests/unit/enrich`: 731 passed, 1 skipped (pre-existing symlink skip).
- `ruff check` (incl. explicit `C901,PLR0912,PLR0913,PLR0915`) clean; `mypy` clean; module 358 lines (budget 360).
- Mutation probes (15 single-line mutations, file restored after each, tree clean):
  - KILLED: blocking_statuses -> ("pending",); rank ascending; tie-break by service_id removed; `redact_batch` output bypassed; name redaction bypassed; Jira weight not moved to semantic; `/100` removed; semantic clip removed; `top_n` slice removed; algorithm_version changed; `NOT EXISTS core.service_map` team filter dropped; "most recent" order reversed.
  - SURVIVED: `score >= min_score` -> `>` (boundary untested); `"jira_component"` removed from match_keys (untested); `.astype(np.float32)` on score removed (equivalent mutant: numpy 2 weak-scalar promotion already keeps float32).

## Spec compliance
### Units
- ✅ U03-111 `norm_name` - lower-case, Unicode `P*` -> space, whitespace collapsed, whole-token expansion (mapping_suggest.py:132-141). Expansions are also cleaned (report note 5) - stricter than, and consistent with, the postcondition.
- ✅ U03-112 `mapping_scores` - `process.cdist(..., scorer=fuzz.token_set_ratio, workers=-1)/100`, `clip(S @ V.T, 0, 1)`, team `w_f*f + w_s*s + w_c*c`, Jira `w_f*f + (w_s+w_c)*s`, all float32, clipped to [0, 1] (:144-171). Binding 8-kwarg signature carries `# noqa: PLR0913` (plan-mandated, acceptable).
- ✅ U03-113 `prepare_mapping_vectors` - queries per Algorithm (unmapped Jira (project, component) with non-NULL component; active teams with no `core.service_map` row); texts per Postconditions (20 most recent summaries; first 200 chars of 20 most recent `enrich.text_redacted` incident texts for teams/services; `"<label>: <description>"` options); names, headers, summaries and option descriptions through `get_redactor().redact_batch`; `embed_texts(batch_size=128)`; vectors not persisted; limits 2,000 / 5,000 / 1,000 enforced (:185-278).
- ✅ U03-114 `run_suggest_stage` - skipped when `vectors is None`; co-occurrence share by SQL; `mapping_scores`; per-subject top `top_n` with `score >= min_score` ordered score desc then service_id (`np.lexsort`, :312-316); payload fields exactly the §4.6 / payload-table set with `evidence_counts` {work_items, team_incidents, team_incidents_on_service} and `algorithm_version = "map-v1"`; `create_if_absent("mapping_suggestion", ..., match_keys=(subject_type, jira_project, jira_component, team_id, service_id), blocking_statuses=("pending","rejected"), now=clock.now())` (:350-354); log `enrich.suggest.emitted` with subjects/created/suppressed; metric `herness_enrich_mapping_suggestions_total`. No write to `core.service_map` and no approval anywhere in the module (the only writer is `create_if_absent`). All SQL values are bound parameters; the only formatted identifier is the constant `team_id`/`service_id` (:67-75, :205, :232, :240).

### Test rows
- ✅ UT03-106 - `"PMT-Auth Svc"` -> `payment authentication svc`, plus whole-token / Unicode punctuation cases.
- ✅ UT03-107 - 2 x 3 known vectors; expected fuzzy/semantic/cooc/score matrices; Jira row ignores cooc and moves its weight to semantic; float32, [0, 1]; a negative dot product exercises the clip (probe-confirmed).
- ✅ UT03-108 - stub redactor records inputs; the full list of encoder texts is asserted to equal redacted names/headers/summaries or `text_redacted` snippets (20 most recent, 200-char cut); raw summary absent; SQL-meta characters in a service name stay data (`DROP TABLE` name, table intact); caps; empty; schema error without values in the message. Probe-confirmed: bypassing redaction goes red.
- ✅ UT03-109 - rejected (team t1, s1) not re-emitted; others pending; payload shape/evidence counts/metric/log; idempotent re-run; tie order and `top_n`. Uses the real migrated ops store instead of a fake (stronger).
- ✅ UT03-110 - scores below 0.60 -> nothing emitted (metric 0); skipped without vectors; SchemaViolation before any write.
- ✅ PT03-14 - hypothesis, 300 examples, keys drawn from the config key regex; precondition is "no expansion token is a key" (see ⚠️ 1).
- ✅ IT03-14 - real ops store (`ops_store`), real core build 000-280 via `build_harness` with `approved_mapping_suggestions()`, real redactor; after one approval the only `suggested_approved` row is that item; other pairs absent; non-suggested rows unchanged; re-run emits nothing.
- ✅ ST03-12 - high-score (>= 0.8) item is `pending`, undecided; `approved_mapping_suggestions() == []`; `core.service_map` identical before the stage, after it, and after rebuild. Marker `integration`, location `tests/integration/enrich/security/` per §11.
- ✅ Test names and docstrings carry IDs; module-level `pytestmark` in all three files.

## ⚠️ Spec-silent choices / spec issues
1. Spec issue (U03-111 invariant / config row ~3554): the validator rule "no expansion equals a key" does not make the literal algorithm idempotent (e.g. expansion `"x pmt"` with key `pmt`). The implementation cleans expansions and PT03-14 assumes "no expansion *token* is a key". Recommend the spec (and the T03-06 validator) tighten the rule to tokens; not a defect of this card.
2. The encoder in IT03-14/ST03-12 is a constant-vector stand-in, not `tests/fixtures/models/tiny-st/`; the core build and ops store are real. Spec-silent on the encoder for these rows; acceptable since the threat concerns the write path.
3. Extra log events `enrich.suggest.skipped` and `enrich.suggest.capped` and an extra `services` field on `enrich.suggest.emitted` (spec table lists subjects/created/suppressed). Counts only, no text; harmless.
4. Report notes 1-4 and 6-9 judged acceptable: header/service-name/option redaction (conservative); `project IS NOT NULL` (avoids team-keyed rows in 220_service_map); deterministic cap with warning instead of an error; recency by created_at/opened_at with record_id tie-break; co-occurrence denominator includes NULL-service incidents (matches "share of the team's incidents"); rounding after the min_score filter; `normalize_text` 4,000-char bound; redaction failure -> "" fail closed (tested); `report.status="skipped"`. Note 5: see ⚠️ 1.
5. `coalesce(t.active, true)` treats a NULL `active` as active (mapping_suggest.py:59); spec says "active core.team rows" and is silent on NULL.
6. Metric carries `component="enrich"`, required by T08-05 `record_counter`; spec labels "—" means no extra labels. OK.

## Findings
### Critical
none

### Important
none

### Minor
- M1 tests/unit/enrich/test_mapping_suggest.py:500-516 / herness/enrich/mapping_suggest.py:314 - the `score >= min_score` boundary is untested (mutation to `>` survives). Add a case with a score exactly at `min_score` to pin inclusivity. Float32 rounding can also drop a pair whose exact score is 0.60 (compare in float64 or document).
- M2 tests/unit/enrich/test_mapping_suggest.py:413-475 - no test distinguishes two Jira components of the same project against the same service; dropping `"jira_component"` from `_MATCH_KEYS` (herness/enrich/mapping_suggest.py:42) survives. Add a pending item for (PAY, comp-a, s1) and assert (PAY, comp-b, s1) is still emitted.
- M3 herness/enrich/mapping_suggest.py:50-56, 213 - `_SUMMARY_SQL` reads up to 20 summaries for every unmapped (project, component), not only the <= 2,000 kept subjects, so the U03-113 input bound is not applied to this read (builder concern acknowledged). Filter by the kept subject keys when headroom allows; module is at 358/360.

## Assessment
**Task quality:** Approved
**Reasoning:** All four units match §3.18; the no-auto-apply invariant is enforced structurally and proven by UT03-109/IT03-14/ST03-12; redaction and ranking are probe-verified. Remaining gaps are test-boundary and input-bound polish.
