### Spec Compliance
- ✅ Spec compliant
- ⚠️ Cannot verify from diff: the card's Acceptance check ("`herness config validate` passes with the new file") is not verifiable from this diff. `herness.core.config.HernessConfig` (T10-03, this card's `Depends on`) does not yet exist anywhere in the tree (no `herness/core/config.py`, no `HernessConfig` symbol outside `docs/`), so no root-config wiring of `cfg.eval` to `EvalConfig` exists yet to run that check against. This is outside T11-20's own `Files` list (`herness/eval/settings.py`, `config/eval.yaml`) and is T10-03's responsibility per the Algorithm row ("T10-03 ... types its optional `eval` field with this model"), so it is not a fault of this implementation — flagging only because the acceptance check as literally stated cannot be exercised yet.

Detail on what was checked:
- `config/eval.yaml` (`herness/eval/settings.py` sibling, U11-74) is byte-for-byte identical to the design §7 snippet given in the dispatch, including the trailing inline comment on the `judge` line — `D:\herness\.claude\worktrees\agent-abc483a46b9847ecd\config\eval.yaml`.
- `EvalConfig` and its nested models (`herness/eval/settings.py:71-113`) match the U11-52 signature field-for-field: `suite: str`, `judge: JudgeSettings(profile, temperature, cache_dir)`, `repeat: dict[Literal["fast","standard","deep"], int]`, `thresholds: Thresholds(...)` with all seven fields, `classifier: ClassifierSettings(gate_recompute_tolerance, synthetic_sample, bootstrap)`, `baseline: str`.
- All U11-52 invariants are enforced: repeat values 1..10 (`_RepeatCount`, line 50), ratio fields `latency_p95_ratio_max`/`cost_ratio_max` > 0 (`_PosFloat`, lines 48, 92-93), floors/`tool_success_min` in [0,1] (`_Fraction`, lines 46, 90-91), `correctness_floor` keys restricted to the exact five allowed values (`_floor_keys`, lines 65-68, 90), `synthetic_sample` 100..100_000 and `bootstrap` 100..10_000 (lines 101-102). `repeat` additionally requires all three phase keys present (`_repeat_keys`, lines 58-62), matching the design's fixed three-phase shape.
- Per the subcontroller ruling in `w02-s11b.md`: the module defines `load_eval_config(raw: object) -> EvalConfig` (`herness/eval/settings.py:116-122`), which calls `EvalConfig.model_validate(raw)` and, on pydantic `ValidationError`, raises `herness.core.errors.ConfigError(msg, hint=str(exc)) from exc`. This is a real `ConfigError`-raising path driven by invalid raw config data, not merely pydantic models that raise `ValidationError` on their own — matching the ruling and satisfying UT11-108's expectation of `ConfigError`.
- `pyproject.toml:326` adds `"herness.eval.settings"` to `source_modules` of the `"settings modules are leaves"` import-linter contract in the same commit, satisfying UT00-58. The package-level `herness.eval` entries in the main layering contracts (lines 247, 297) already existed, so no further contract edit was needed there.
- Module docstring states imports are "the standard library, pydantic and `herness.core.errors`" (no `herness.core.types`), and the actual imports (`herness/eval/settings.py:36-40`) match that. The brief's Algorithm line lists `herness.core.types` as one of the *allowed* imports (an upper bound, shared with the import-linter's allow-list), not a mandatory one; nothing in `EvalConfig` needs a shared domain type, and the T08-26 precedent (`herness/core/resilience/settings.py`) similarly imports `herness.core.types` only because it actually uses domain types (`GpuClass`, `JobKind`, etc.). Omitting an unused import here is correct, not a gap.
- Module size: 96 lines vs. the 150-line module-map budget (diff `@@ -0,0 +1,96 @@`), well under the 400-line hard limit.
- Tests: `tests/unit/eval/test_eval_settings.py` covers UT11-107 (config matches design §7 field-for-field; `repeat.deep=11` rejected) and UT11-108 (bad `correctness_floor` key rejected; non-positive ratio rejected) with real, non-vacuous assertions (equality against explicit model instances, `pytest.raises(ConfigError)`). Test names and docstrings carry their IDs (`test_ut11_107_...`, `test_ut11_108_...`), `pytestmark = pytest.mark.unit` is set at module level, both required IDs have functions.

### Strengths
- `load_eval_config` correctly implements the harder-to-get-right part of this card (the ConfigError-wrapping ruling): it wraps with `from exc`, preserving the traceback chain, and follows the repo's EM101 convention (message assigned to `msg` before `raise`).
- Nested-model design (`_Model` base with `extra="forbid", strict=True, frozen=True`, `Annotated[...]` field aliases `_Fraction`/`_NonNegFloat`/`_PosFloat`/`_NonEmptyStr`/`_RepeatCount`) is DRY and mirrors the established house style from `herness/core/resilience/settings.py`, making the module easy to audit against the invariant table.
- Asymmetric key-validation is correctly modeled: `repeat` requires exactly its three phase keys (closed set via `Literal` + `AfterValidator`), while `correctness_floor` only restricts to a subset of five allowed keys and `tool_success_min` is left unrestricted — matching the brief's differing invariants precisely rather than applying a one-size-fits-all check.
- `config/eval.yaml` was committed verbatim against the design snippet, including the trailing inline YAML comment, rather than a paraphrased or reformatted version.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- `tests/unit/eval/test_eval_settings.py` exercises only two of the module's several bounded fields (`repeat` upper bound, one floor key, one ratio lower bound). The brief's Tests row only requires UT11-107/UT11-108 and both are satisfied, but additional boundary tests (e.g. `synthetic_sample`/`bootstrap` bounds, `correctness_drop_max_pp` negative rejection) would strengthen confidence beyond the minimum, given 100%/100% coverage was reached without them (i.e. those branches are pydantic-generated, not hand-written, so coverage doesn't surface the gap).

### Assessment
**Task quality:** Approved
**Reasoning:** The implementation matches U11-52's model shape and invariants exactly, correctly implements the subcontroller's ConfigError-wrapping ruling (the one previously flagged risk), commits `config/eval.yaml` verbatim per U11-74, and adds the required import-linter contract entry for UT00-58. The only open item — the card's literal acceptance check — is blocked on T10-03 (`HernessConfig`) not yet existing in the tree, which is outside this card's scope and not a defect in this diff.
