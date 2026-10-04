# Report: T08-01 Shared types and error class

Status: DONE
Commit: 8ec6cbe feat(core): add jobs shared types and JobStateError (T08-01)

## What was built

- `herness/core/types/jobs.py` (new, 156 lines): the six 08 literal aliases
  (`GpuClass`, `JobKind`, `ServiceName`, `ChatMode`, `BreakerState`, `PolicyName`,
  via PEP 695 `type X = Literal[...]`, matching the `errors.py` style), and three
  pydantic v2 models: `JobSpec` (extra="forbid", strict=True, frozen=True;
  `priority` 0-100 or None, `max_attempts` 1-20 or None, `scheduled_for`
  timezone-aware or None via a custom validator, `idem_key` pattern/length),
  `JobOutcome` (extra="forbid", frozen=True; `result` defaults to `{}`, capped at
  1 MiB of `canonical_json` bytes), `MetricSample` (extra="forbid", strict=True,
  frozen=True; `ts` timezone-aware, `name`/`component` regex, `value` finite and
  non-negative for counter/histogram, `labels` <= 6 keys with key/value regexes and
  a 1024-byte canonical-JSON cap).
- `herness/core/types/__init__.py`: added the 08 import line and its 9 names to
  `__all__` (sorted tuple, per the ownership checker's OWN032).
- `herness/core/errors.py`: added a `# --- 08 (resilience and jobs) ---` section
  with `JobStateError(FatalError)`, signature and `_extra_attrs` exactly as the
  brief (`job_id`, `task_id`, `run_id`, all optional). Also reworded one line of
  the module docstring ("No other spec adds classes to this file" ->
  "Spec-local subclasses are declared by their owners, each in its own section at
  the end of this file (R-19)."), since R-19 explicitly names `JobStateError` in
  08 as being declared in this file by its owner, directly contradicting the old
  sentence; this card is the proof case.
- `herness/core/types/_ownership.py` needed no change: the T00-08 commit already
  carried the full 08 TYPE_OWNERS/DECLARED_ELSEWHERE entries (`GpuClass` through
  `MetricSample` under owner "08"; `ModelChain`, `loop_signal_policy`,
  `JobContext` under DECLARED_ELSEWHERE), and
  `tests/unit/core/test_types_ownership.py` already asserts that exact set.
  Verified with `git log --oneline -- herness/core/types/_ownership.py` (only the
  T00-08 commit touches it) and a diff-free `git status` on that file.

## Tests

- `tests/unit/core/types/test_jobs.py` (new): UT08-01 (JobSpec priority bounds
  and None acceptance, extra-field rejection, naive-scheduled_for rejection,
  frozen instance, the six literal sets matching design 08 exactly, and
  `herness.core.types` exporting no `JobContext`) and UT08-02 (JobOutcome
  default `{}`, oversize-result rejection, frozen/extra-forbidden), plus
  supplementary (non-ID) tests for MetricSample and the JobSpec/JobOutcome
  edges not named by this card's Tests field, added to keep coverage honest.
- `tests/unit/core/test_job_state_error.py` (new, no formal UT ID -- see
  Concerns): smoke/coverage tests for JobStateError (FatalError parent,
  identifier defaults, to_log_fields, pickle round-trip).

## Gate results

- `uv run ruff format --check .` -- 32 files already formatted.
- `uv run ruff check .` -- All checks passed (two DTZ001 findings on
  intentional naive-datetime test inputs were suppressed with `# noqa: DTZ001`,
  matching the existing pattern in `tests/unit/core/test_errors.py`).
- `uv run mypy` -- Success: no issues found in 13 source files.
- `uv run lint-imports` -- 4 contracts kept, 0 broken (no contract changes
  needed; this card only adds a submodule inside the existing
  herness.core.types layer).
- `uv run python -m tools.check_type_ownership` -- exit 0 (5 "INFO pending
  owner" lines for 03/05/06/07/09, which are not yet built; no violations).
- `uv run pytest -k "ut08_01 or ut08_02" -q -p no:logging` -- 9 passed. (The
  brief's literal `-k "UT08-01 or UT08-02"` selects 0 tests: pytest -k
  substring-matches node ids, and the global-constraints doc and every existing
  test in the repo -- test_ut00_49_..., test_ut00_01_..., etc. -- name IDs
  with underscores, e.g. test_ut08_01_..., not hyphens. I followed the
  established repo convention; see Concerns.)
- `uv run pytest tests/unit/core -q -p no:logging` -- 112 passed.
- `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` --
  128 passed, 4 deselected (fault/eval/gpu-marked tests elsewhere in the repo).
- Coverage (--cov=herness.core.types --cov=herness.core.errors --cov-branch):
  herness/core/types/jobs.py 100% (91 stmts, 12 branches, 0 missed either way);
  herness/core/errors.py 99% (one pre-existing partial branch in
  HernessError.__init__, not touched by this card); both well over the 90%
  line / 85% branch bar.

## Line counts vs budget

- `herness/core/types/jobs.py`: 156 lines vs the brief's 150-line module-map
  budget (6 over). The overage is the validators needed for TH08-10 (1 MiB
  result cap) and TH08-02 (label allowlist + 1024-byte cap) plus one
  explanatory comment (see ambiguity 3 below). Well under the ENG 400-line
  hard limit; no automated tool enforces the per-module figure (checked: no
  such check exists in tools/ or tests/unit/tools/).
- `herness/core/errors.py`: 354 lines total (was ~322), vs the brief's "+12"
  estimate for the 08 section. The class itself is ~30 lines including its
  docstring and _extra_attrs/type annotations, consistent with the existing
  CircuitOpen/RateLimited classes in the same file. Well under 400.
- `herness/core/types/__init__.py`: 29 lines vs "+2" estimate. The brief's "+2"
  undercounts a 9-name multi-line import plus __all__ tuple; the ownership
  checker's OWN030/OWN032 require exactly this shape (one ImportFrom per owner
  submodule, sorted-tuple __all__), so the line count is fixed by the checker,
  not a style choice.

## Spec ambiguity and how it was resolved

1. JsonValue: JobSpec.payload and JobOutcome.result are typed
   dict[str, JsonValue] in the brief, but no JsonValue symbol exists anywhere
   in the tree or in any impl/spec doc (grepped docs/impl/*.md,
   docs/specs/*.md, herness/). Resolved: this is pydantic.JsonValue (pydantic
   2.13, third-party; _TYPE_THIRD_PARTY in tools/check_type_ownership.py
   already allowlists "pydantic" for every owner), not a herness-owned type.
   This also explains why JsonValue is absent from TYPE_OWNERS/_NAMES for
   every owner (05, 07, 08, ...) despite being used pervasively across specs
   05/07/08.
2. UT08-01's JobContext import clause: the master test spec's UT08-01 row
   (which spans units U08-01, U08-02, U08-04 -- the last not in this card's
   Units list) says "JobContext is importable from herness.core.jobs and not
   from herness.core.types". herness.core.jobs does not exist yet (built by
   later 08 cards, e.g. T08-12); this card's own Acceptance checks only
   require "herness.core.types exports no JobContext", which I implemented
   and tested. I did not attempt the herness.core.jobs import half, since
   inventing that package would exceed this card's Files list and Depends-on
   (T00-08, T00-03 only).
3. errors.py module docstring vs R-19: see "What was built" above; fixed a
   one-line contradiction between the file's stale docstring and the binding
   R-19 ruling that this very card proves.
4. test_job_state_error.py has no formal UT ID: U08-05's own Tests row names
   UT08-61, UT08-69, but those aren't in this card's Tests field (UT08-01,
   UT08-02) and, per docs/impl/08-resilience-and-jobs.impl.md section 11's
   traceability, they exercise real job/task-state operations (T08-11 /
   T08-16 territory) that don't exist yet. To avoid a name collision with
   whichever later card owns UT08-61/UT08-69, I wrote a small, unnumbered
   smoke-test file purely to keep errors.py's new lines covered, and said so
   in its module docstring.
5. pytest -k ID casing: see Gate results above; used underscore form
   consistent with the rest of the suite and the global-constraints wording.

## Concerns

- None blocking. The two deviations worth a second look are (4) the
  unnumbered JobStateError smoke test and (5) the -k casing -- both are
  conservative, repo-convention-following choices, not scope changes, but
  flagging per "if a test in the brief contradicts the spec... report
  DONE_WITH_CONCERNS or NEEDS_CONTEXT."
