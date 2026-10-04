# Review: T08-01 Shared types and error class

Commit reviewed: 8ec6cbe on top of 079088a, worktree D:\herness\.claude\worktrees\agent-abca177ec95e8a696.

## Spec compliance by unit

- U08-01 (herness.core.types.jobs literal aliases) - Spec: PASS. All six aliases (GpuClass, JobKind, ServiceName, ChatMode, BreakerState, PolicyName) match design 08 value sets exactly (herness/core/types/jobs.py:140-168), defined with PEP 695 type X = Literal[...] matching the file stated style. Re-exported from herness.core.types.__init__ (herness/core/types/__init__.py:83-105). TYPE_OWNERS/DECLARED_ELSEWHERE already carry the correct 08 entries (verified directly in herness/core/types/_ownership.py:20-51: all 9 names under owner "08", and ModelChain/loop_signal_policy/JobContext correctly under DECLARED_ELSEWHERE pointing at herness.core.resilience/herness.core.jobs) - the build report claim that T00-08 already added these and no change was needed here is correct; the diff carries no change to this file.

- U08-02 (JobSpec) - Spec: PASS. kind, payload: dict[str, JsonValue], gpu_class required; priority Field(ge=0, le=100, default=None); max_attempts Field(ge=1, le=20, default=None); scheduled_for optional, naive datetimes rejected by _scheduled_for_aware (herness/core/types/jobs.py:200-203); idem_key Field(max_length=200, pattern=...) matching the brief regex exactly. Model config extra="forbid", strict=True, frozen=True matches the brief. Deferred None-default resolutions (DEFAULT_PRIORITY[kind], R.jobs.max_attempts[kind], "now at submit", U08-44) are correctly left to the future submit unit (out of this card scope, consistent with Preconditions text).

- U08-03 (JobOutcome) - Spec: FAIL (partial). status: Literal["done", "yield"] and result: dict[str, JsonValue] = {} with the 1 MiB canonical-JSON cap (_result_size validator, herness/core/types/jobs.py:214-220) are implemented and correct. Missing: the field own constraint column also states "key state is dropped on done" - no code anywhere in JobOutcome (or elsewhere in the diff) drops a state key from result when status == "done". This is a same-model cross-field concern (status + result, both present at construction, no external config needed) - the same pattern the diff already uses for MetricSample.value/kind cross-validation via ValidationInfo - so it was implementable in this card and was not implemented or tested. See Issues.

- U08-05 (JobStateError) - Spec: PASS. Section header "# --- 08 (resilience and jobs) ---", subclass of FatalError, _extra_attrs = ("job_id", "task_id", "run_id"), signature (message, /, *, job_id=None, task_id=None, run_id=None, hint=None, details=None, **context) (herness/core/errors.py:33-60) - verified this (message, /, *, ...) positional-only shape, and the hint/details/**context pass-through, is the established convention already used by RateLimited, CircuitOpen, and ModelRefused in the same file (herness/core/errors.py:162-242), so it is not a deviation from the brief simplified signature line. Attributes present with correct names/types.

- U08-101 (MetricSample) - Spec: PASS. ts tz-aware (validator), name/component regex, kind Literal, value finite and >=0 for counter/histogram via cross-field ValidationInfo check, labels <=6 keys with key/value regex and a defensive (correctly pragma: no cover marked, since currently unreachable given the regex/key-count bounds) 1024-byte canonical-JSON cap. Matches the brief exactly, including model config.

## Test coverage (UT08-01, UT08-02)

Both rows listed behaviors are covered in tests/unit/core/types/test_jobs.py: priority bounds/None, extra-field rejection, naive scheduled_for rejection, frozen instance, all six literal sets, herness.core.types exporting no JobContext (UT08-01); JobOutcome default {} and oversize-result rejection (UT08-02). Ran "uv run pytest -k ut08_01 or ut08_02 -q -p no:logging" myself: 9 passed, confirming the report. Note UT08-02 as literally worded (JobOutcome(status=done); oversize result) never exercises a non-empty result on status=done, so it would not have caught the missing state-drop behavior above even if it existed - this is a gap in the row itself, not something the build could route around.

## TYPE_OWNERS / acceptance checks

- "from herness.core.types import JobSpec, MetricSample" works (both exported in __init__.py:95-105).
- herness.core.types exports no JobContext - verified, both by direct inspection of __init__.py and by the test test_ut08_01_types_package_exports_no_jobcontext.
- Ownership check - _ownership.py inspected directly (see U08-01 above); matches brief. Report exit-0 claim for tools.check_type_ownership is consistent with what is in the file and not contradicted by anything in the diff.
- Ran the literal brief acceptance command myself: "uv run pytest -k UT08-01 or UT08-02 -q -p no:logging" -> 132 deselected, 0 selected - confirms the build report claim that the brief hyphenated -k expression is unsatisfiable given the project underscore test-naming convention (global-constraints.md line 8: test_ut00_55_... / pytest -k UT00_55). The build choice to use the underscore form is the only one consistent with global-constraints and the rest of the suite; this is a genuine brief/global-constraints inconsistency, correctly flagged and reasonably resolved, not a defect.

## Gates

Ran directly on the touched files: "uv run ruff check" -> all checks passed; "uv run mypy" on jobs.py and errors.py -> no issues found. Consistent with the report full-repo gate claims (ruff format/check, mypy --strict, lint-imports, check_type_ownership all reported clean).

## Builder three stated concerns - assessment

1. JsonValue resolution - reasonable and correct. No JsonValue symbol exists in the spec docs or tree; pydantic.JsonValue is a real symbol in the pinned pydantic==2.13.5 (uv.lock:2149), and pydantic is allowlisted as third-party in tools/check_type_ownership.py:20 (_TYPE_THIRD_PARTY), so it correctly bypasses TYPE_OWNERS. Warning-level item (spec-silent, but the resolution is sound and verified against the actual dependency).
2. UT08-01 JobContext-from-herness.core.jobs half being out of card scope - correct call. This card own Acceptance checks field only requires "herness.core.types exports no JobContext", and herness.core.jobs is built by a later card per Depends-on. Not a defect.
3. -k UT08-01 vs underscore test names - confirmed above by direct execution; not a defect, correctly flagged.

## Docstring edit in errors.py

herness/core/errors.py:9-12: replaced "No other spec adds classes to this file" with wording matching R-19. Checked docs/impl/DECISIONS.md:39 directly: R-19 explicitly names JobStateError in 08 (and new attributes on EgressBlocked/ConfigError in 10) as declared by their owners in this file - the old sentence was in direct contradiction with the binding ruling this very card exercises. The edit is correct and necessary, not scope creep.

## Findings

### Critical
None.

### Important
- Missing requirement (plan-mandated): JobOutcome.result does not drop the state key when status == done. herness/core/types/jobs.py:206-220 (U08-03 field table: result constraint "canonical JSON <= 1 MiB (1 048 576 bytes); key state is dropped on done"). Only the size cap is implemented (_result_size validator); nothing drops a state key. Since status and result are both fields on the same frozen model, this was implementable in this card via a cross-field validator (the diff already demonstrates that pattern for MetricSample.value/kind), and untested by UT08-02 as currently worded (which never passes a non-empty result with status=done). Recommend adding a model_validator(mode=after) (or equivalent) that drops state from result on done, plus a test.

### Minor
- herness/core/types/jobs.py is 156 lines vs the module map 150-line budget (6 over); well under the ENG 400-line hard limit and no tool enforces the module-map figure. Acceptable, flagged for completeness.
- herness/core/types/__init__.py (29 lines vs +2 estimate) and herness/core/errors.py (+32 lines vs +12 estimate) exceed the module-map deltas; both are driven by mechanical requirements (ownership checker sorted-tuple __all__/one-import-per-owner shape; existing class-with-_extra_attrs shape in the same file) rather than scope creep, and stay well under the 400-line hard limit.

### Warning items (not defects, noted per instructions)
- JsonValue resolved as pydantic.JsonValue - spec-silent on the symbol origin, but verified sound (see above).
- UT08-01 JobContext-from-herness.core.jobs half is not implemented here - correctly deferred to a later 08 card that creates herness.core.jobs.
- The brief literal pytest -k UT08-01 or UT08-02 acceptance command selects 0 tests given the project underscore test-naming convention; verified empirically. The build used the underscore form instead, consistent with global-constraints.md.

## Verdict

Needs fixes. One Important, plan-mandated gap: JobOutcome does not implement the documented "key state is dropped on done" behavior for its result field (U08-03). Everything else in this card - the six literal aliases, JobSpec, MetricSample, JobStateError, the __init__.py/_ownership.py wiring, the errors.py docstring fix, gates, and UT08-01/UT08-02 coverage as literally specified - is spec-compliant and well-built.
