# T11-07 report: Text templates and PII injection

Status: DONE_WITH_CONCERNS
Commit: bf8e7f4 feat(synth): add text templates and reserved-range PII injection (T11-07)
Worktree: D:\herness\.claude\worktrees\agent-a4223eb3b67c31e20 (branch worktree-agent-a4223eb3b67c31e20)

## Files
- tools/synth/text.py (291 lines, budget 380): U11-05. TemplateBank (frozen dataclass, families in a MappingProxyType),
  Family, RenderedText, render_incident_text, render_change_text, render_jira_text, ROOT_CAUSE_OPTIONS,
  CHANGE_MARKERS, REPEAT_MARKERS, SLOT_KEYS.
- tools/synth/text_vocab.py (183 lines, NEW SIBLING, not in module map): built-in vocabularies (60 symptoms,
  25 root causes each mapped to one option, 40 actions, 30 families with EN/ES topics, EN/ES pattern skeletons,
  impact/repeat phrases, Jira summaries). Split because text.py was 448 lines with the data inline.
- tools/synth/pii.py (223 lines, budget 250): U11-06. PiiSpan, PiiType, inject_pii, build_name_list, luhn_valid.
- tests/unit/tools/synth/test_synth_text.py: UT11-06 (8 functions), UT11-07 (2 functions).
- tests/unit/tools/synth/test_synth_pii.py: UT11-08 (4 functions), PT11-02 (hypothesis, 200 examples).

## Design notes
- Families: 30, each with 3-6 patterns (EN skeletons rotated by family index, topic substituted), 2 change
  patterns ("After the release to ...", "Following change to ..."), 3 ES patterns, 2 ES change patterns.
  tpl_conn_pool -> capacity, tpl_cert_expiry -> access_identity.
- Slots: symptom, action, host, count. Given slots are kept, each re-drawn with probability slot_variation (0.20);
  missing keys are drawn fresh.
- Noise: per-word adjacent-letter swap at typo_rate (0.01) on description and close_notes; words of the change,
  repeat and impact phrases are protected so truth labels stay detectable. Casing noise (upper/lower of
  short_description) at 0.05. Spanish at spanish_share (0.05). Caps 160 / 3,000 by truncation.
- PII: types chosen uniformly with replacement; phrase from ("contact {v}", "reported by {v}", "from host {v}");
  insertion at a uniformly chosen sentence boundary (0, end, after .!? + whitespace, never inside an existing span);
  later spans shifted. With p 0.5 an INC/CHG + 7-digit number is inserted right after a random span (not a span).
  Names: 10x10 invented syllable compounds per pool (100 first, 100 last); 500 pairs sampled without replacement
  from stream_rng(seed, "names") (STREAM_NAMES constant defined in pii.py, rng.py untouched).
  All values synthetic/reserved (TH11-01).

## RED / GREEN
- RED: `PYTHONUTF8=1 uv run pytest tests/unit/tools/synth/test_synth_text.py tests/unit/tools/synth/test_synth_pii.py`
  -> `ModuleNotFoundError: No module named 'tools.synth.pii'` (2 collection errors).
- GREEN: same command -> 15 passed. Coverage: text.py 100 %, text_vocab.py 100 %, pii.py 100 % line/branch.

## Gates (all at commit)
- uv run ruff check . -> All checks passed; ruff format --check . -> 111 files already formatted
- uv run mypy -> Success, no issues (46 files); uv run lint-imports -> 8 kept, 0 broken
- tools.check_module_size -> exit 0; tools.check_type_ownership -> exit 0
- PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging -> 686 passed, 1 skipped,
  5 deselected, 1 xfailed (pre-existing IT00-02 xfail)

## Deviations / concerns
1. Sibling split: tools/synth/text_vocab.py (183 lines) is not in the §2 module map; text.py alone would be 448
   lines (> 380 budget, > 400 hard limit). Controller to rule on adding a module-map row.
2. Signature extension: render_incident_text takes an extra keyword-only `text: TextParams = TextParams()` so the
   caller can pass params.text (spanish_share, typo_rate, casing_noise_rate, slot_variation); the U11-05 algorithm
   references params.text.spanish_share but the fixed signature has no params. Defaults equal the spec values.
3. Interpretations left open by the spec: `theme` in render_jira_text is a family name (validated; unknown ->
   SynthUsageError), used by the T2 plant epic ("Reduce connection pool exhaustion in ..."); unknown issue_type ->
   SynthUsageError. render_change_text: emergency changes draw a family and carry its root cause; planned changes
   use family "chg_planned" and root_cause "unknown". Spanish, typos and casing noise apply to incidents only.
   impact_level outside 0..3 -> SynthUsageError (precondition enforced). inject_pii with empty `names` ->
   SynthUsageError.
4. Carry-over: the pii-corpus unit (spec line 537) calls inject_pii with n_spans 0..3, but U11-06 requires 1..3 and
   raises otherwise; the corpus writer must skip injection for n_spans = 0.
5. PII type literal is local (`PiiType`); spec 10 `EntityType` does not exist in the tree yet.
