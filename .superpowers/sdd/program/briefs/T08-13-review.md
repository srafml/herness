# T08-13 review: Windows and arbiter (impl 08)

Reviewed: worktree agent-a7d1e1b1d2ee59eed, head edb4ace (code in f149e06), base ae18ed5.
Focused checks run: `pytest tests/unit/core/jobs` with branch coverage (98 passed; windows.py 100 %, arbiter.py 100 %, __init__.py 100 % line+branch); lazy-export resolution of every `_EXPORTS` name against its owning submodule (18/18 ok); WindowSpec `_HHMM_RE` (zero-padded, so the string `end <= start` test is sound); consumers of `ArbiterDecision.reason` in impl 08 (only `controller.swap(d.target, reason=d.reason)`, a free `str`, line 1775/1634).

### Spec Compliance
- ✅ Spec compliant.
  - U08-66 `window_at` ✅: `local.date()` then `−1 day` candidates; `days` filter on the start date; `d' = d + 1` when `end <= start`; both bounds through `resolve_local` (gap → next valid minute, fold=0); `start_at <= now < end_at`; no match → match at `now + 1 h`; several → `max(start_at)`. `ActiveWindow` frozen, `spec/start_at/end_at`, UTC.
  - U08-68 `next_window_allowing` ✅: `none` → now; allowed now → now; walks `window_at(w.end_at)` bounded by `end_at <= now + 8 d` (end_at strictly grows → terminates); fallback `now + 1 day` with WARNING `jobs.window.class_never_allowed` (field `gpu_class`).
  - U08-69 `preempt_deadline` ✅: steps 1–3 exact incl. `class_since >= w.start_at`; `hard_start` checked before looking up `prev` (equivalent result, one fewer lookup); `prev = window_at(w.start_at − 1 min)`, `+ prev.spec.overrun_max_min`.
  - U08-70 `arbiter_decide` ✅: step order 1, 2, 2a (`claimable.get("none", 0) > 0` → keep loaded, R-43), 3 (preference order, keep/swap), 4 (preload ≠ loaded), 5 (idle, target loaded). `requested` keyword-only.
  - UT08-75 ✅: all 168 hours against an independently written §5.10 table, Sun 23:59 → `deep` with bounds, UTC tz asserted, DST spring (8 h) / autumn (10 h) nights, +1 h fallback, latest-start tie-break, naive rejected, real-config wiring.
  - UT08-76 ✅ verbatim: Tue 05:00/07:01 → 07:00; Sun 22:00/Mon 06:30 → Mon 07:00; reasoning 08:00 → None (plus hard_start, enrichment 120 overrun, in-window switch, `none`).
  - UT08-77 ✅ verbatim: Tue 10:00 decider → 19:00 same day; plus large → Sun 21:00, never-allowed warning captured.
  - UT08-78 ✅: 19-row table covering every step, requested `large` and `none`, and a sweep asserting `claimable["none"] > 0` → keep without swap for every window × loaded class.
  - PT08-05 rerun ✅: hypothesis over valid daily partitions against the real `window_at`, plus every minute of the default week.
  - Budgets ✅: windows.py 134/290, arbiter.py 78/150, jobs/__init__.py 75/90. Layering ✅: no store import; windows imports config/logging/time/errors/jobs.cron only; arbiter imports types under TYPE_CHECKING only. Conventions ✅: IDs in every docstring, `pytestmark = pytest.mark.unit`.
- Builder interpretations judged: ServiceClass (settings) vs GpuClass (API) with `in` membership — correct (`none` never in classes, matching the explicit `none` branches). Missing claimable keys = 0 — correct (`claimable_counts` is a GROUP BY and omits zero classes, impl line 1880). Invented reasons `loaded_claimable` / `slot_kind_claimable` / `claimable` / `no_work` — acceptable (spec leaves them open; reason is a free `str` passed to `controller.swap`/`gpu_swap` event). Step-1 rejection target = loaded — sensible. ConfigError when the +1 h retry also misses — acceptable (only reachable with a config U08-67 rejects; beats returning a wrong window or looping).
- ⚠️ Cannot verify from diff: ruff/mypy/lint-imports/pre-commit gates (report claims clean; not re-run). FT08-10 and supervisor logging of `jobs.gpu.request_rejected` belong to later cards (carry-over noted).

### Strengths
- Tight, readable implementation; shared `_occurrences`/`_match` keeps U08-66 steps literal.
- UT08-75 expected names come from an independent encoding of the design table, not from config, so the test is not tautological.
- DST cases assert concrete durations and bounds; PT08-05 now exercises the real function.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/core/jobs/__init__.py:22 — `"arbiter_decide"` is inserted between `SchedCheck` and `ServiceControl`; the dict is otherwise ASCII-ordered (uppercase then lowercase), so it belongs after `WorkerRow` before `bind_jobs_backend`. `__all__` is sorted anyway; cosmetic.
2. herness/core/jobs/windows.py:41 — `ActiveWindow` docstring promises `start_at <= t < end_at`, but the `now + 1 h` fallback (windows.py:90) returns a window whose `start_at > now`. Say "for the matched instant" or note the DST exception.
3. tests/unit/core/jobs/test_jobs_arbiter.py:59-61 — the row `("reasoning", "chat", {"reasoning": 0}, ...)` → `idle/no_work` is under the `# step 4: preload` comment but exercises step 5 (preload == loaded). Move it below the step-5 comment.
4. tests/unit/core/jobs/test_jobs_validate.py:303 — invalid draws `return` silently; `hypothesis.assume(validate_windows(windows) == [])` would make filtered examples visible to Hypothesis health checks (same pattern pre-exists at :260).

### Assessment
**Task quality:** Approved
**Reasoning:** All four units follow the U08-66/68/69/70 algorithms step for step, UT08-75..78 and the PT08-05 rerun hit the verbatim expectations with 100 % line/branch coverage, and budgets/layering/exports hold; only cosmetic minors remain.
