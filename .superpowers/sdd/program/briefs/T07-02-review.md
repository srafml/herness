# T07-02 review: Memory configuration

Reviewed: b72d379..e480259 (30b263c feat, e480259 budget ruling) in worktree agent-a65d277c4bb1cf9a9.

### Spec Compliance
- ✅ U07-19 `config/memory.yaml`: every key and value matches design 07 §7 verbatim (docs/specs/07-memory.md:596-652), with the single sanctioned change `outcome.window_weeks: 10` (R-34). 57 lines (budget 80).
- ✅ U07-19 `config/injection_patterns.txt`: the 14 patterns verbatim, in order, markdown `\|` unescaped; pattern 6 `(act|behave) as (an?|the) ` keeps its trailing space (checked with od; .gitattributes only normalises EOL, no trim hook). 17 lines (budget 40).
- ✅ U07-18 Kind: pydantic, `extra="forbid"`, `strict=True`, `frozen=True` (+ `allow_inf_nan=False`); all 8 named section models present, plus small nested sub-models.
- ✅ U07-18 model rules: target<soft<hard<1; weights >=0 summing to 1 ± 1e-9 (fsum); conf/rec floors (0,1]; conflict<merge<=1; delta lo<0<hi; 0<conf lo<hi<1; half_life keys ∈ Kind, all 11 present, >0; expiry keys ∈ Kind, >0; per_metric keys limited to the four week keys; demote < promote.min_pass_lb <= lora.min_pass_lb.
- ✅ §9 per-key ranges: all checked against settings.py (keep_last 1-20, summary_max_tokens 100-4000, ledger 0-500, min_score [0,1), candidates 1-200, mmr [0,1], write limits, rate limits >=1, episodic 0-10 / 1-3650, outcome >=1 (settle >=0) and second>measure_after incl. per_metric effective overrides, min_coverage (0,1], min_rel (0,1), t_crit>0, feedback, procedural, chat).
- ✅ Injection patterns: re.IGNORECASE compile, empty-after-strip rejected, >500 chars rejected, >500 patterns rejected; parser names the 1-based file line via ConfigError(key, line).
- ✅ R-03 imports: stdlib, pydantic, herness.core.types, herness.core.errors only; module added to the settings-leaf import-linter contract (pyproject.toml).
- ✅ Tests: UT07-04 (6 functions) and UT07-05 (7 functions, parametrised) present with IDs in names and docstrings, `pytestmark = unit`; they assert real values/locations.
- ⚠️ Cannot verify here (deferred by ruling to T10-03): `herness config validate --offline`, ValidationError -> ConfigError conversion naming the key path, HernessConfig wiring.

Evidence (run by reviewer): card tests 47 passed; settings.py coverage 100% line / 100% branch; ruff check pass; ruff format --check pass (120 files); mypy 0 issues (54 files); lint-imports 10 kept 0 broken; check_type_ownership exit 0; check_module_size exit 0 (315 <= 320); import-contract/check-scripts tests 6 passed, IT00-02 xfail (pre-existing traceability marker, unrelated).

### Strengths
- Defaults equal the shipped yaml and a test pins both (defaults == shipped == design dict), so an absent file cannot drift.
- per_metric second>first rule evaluates the effective override per metric, a case the spec implies but does not spell out.
- Trailing-space-significant pattern is documented in the file header and pinned by a test (including a negative match).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/harness/memory/settings.py:98 — the parser skips lines whose first non-blank char is `#` and whitespace-only lines; U07-19 says lines *starting with* `#` and blank lines. An indented pattern beginning with `#` would be silently dropped rather than loaded; whitespace-only lines are skipped rather than rejected as "empty after stripping". Both readings are defensible; worth a one-line note in the spec or T10-03.
2. tests/unit/harness/memory/test_memory_settings.py:184-217 — several UT07-05 cases assert only a section prefix (`"compaction"`, `"outcome"`, `"procedural"`), e.g. `("compaction.hard_ratio", 1.0, "compaction")` is caught by the per-field `(0,1)` bound, so the cross-field ordering message is not what is proven there. The ordering validators are still exercised by other cases (target 0.75, soft 0.9, demote 0.7), so this is tightening only (e.g. also assert the rule message).
3. herness/harness/memory/settings.py:198-199, 281 — some bounds are stricter than §9: `conflict_cosine` > 0 (spec only `conflict < merge <= 1`), `demote_pass_lb` in (0,1) (spec only `< min_pass_lb`), per_metric metric name 1-128 chars. Harmless and sensible; noted as Extra.
4. Build report says 46 cases; the file has 47 (report accuracy only).

### Assessment
**Task quality:** Approved
**Reasoning:** Shipped files match design 07 §7 / U07-19 exactly (with R-34), every U07-18 and §9 rule is enforced and tested, and all gates pass; remaining items are Minor and the ConfigError/CLI parts are ruled into T10-03.

Verdict: Approved
