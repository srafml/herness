# T08-26 review: Configuration section models (base 0ee14b5, head 2efb43f)

### Spec Compliance
- ✅ U08-06 (`ResilienceSection` and sub-models): every model, field, type and default checked against the unit table and design 08 §7. `extra="forbid"`, `strict=True`, `frozen=True` via `_Model` (settings.py:109-112). Policy keys are exactly `PolicyName` minus `gpu_health`: unknown keys fail on the Literal, and a missing key or an extra `gpu_health` fails in `_policy_keys` (settings.py:98-101). `retry_after_max_s = 86400` and > 0 (settings.py:152). `JobsSettings` defaults, `cpu_slots` 0-16, every `JobKind` present with values 1-20, and `heartbeat_s*3 < lease_s` (settings.py:197-217). The `bearer_secret` regex is at settings.py:31/224. URLs must be loopback http with an explicit port (settings.py:85-95). Service names come from the `ServiceName` literal, and all three classes are required with each service in only one class (settings.py:236-261).
- ✅ U08-07 (`ScheduleSection`, `ResilienceConfig`): `WindowSpec`, `ChatSchedule`, `RekeySchedule` (`0 19 * * SAT`, 12, 0-168), `ChainStep`, `ScheduledJob` (name, reserved names, cron shape, `catch_up_max` <= 7 d, `then` <= 10, unique names) and `MaintenanceSchedule` (`12h`) all match. The root validator checks window classes, `preload` and chain-step `gpu_class` other than `none` against `gpu.classes`, and its message names the path (settings.py:370-382). It does not check week coverage, which is correct.
- ✅ Rulings: in the fixture, the nightly `job.gpu_class` is `none` (R-43, fixtures/resilience.yaml:82). Ports 8000/8100/8200 are on 127.0.0.1 (R-51). OpenJev uses `secret:OPENJEV_API_KEY` (R-53). The UT08-03 test re-extracts the §7 block from the design doc and asserts equality, so the fixture cannot drift from the design.
- ✅ UT08-03: every expected row is covered (design equality, the three rulings, 86400, a missing policy key, a non-loopback URL, and `openjev.api_key` rejected along with its path). TH08-01 (service-name allowlist, `GpuClass` literal, NUL-free argv) and TH08-12 (loopback URL, bearer reference) are covered at the config layer.
- ✅ R-03: the imports are stdlib, pydantic and `herness.core.types` only. `lint-imports` reports `resilience-settings-light` as KEPT.
- ✅ Budget: settings.py is 382 of 390 lines. `__init__.py` is untouched (the settings names are not part of §3.3-§3.8).
- ✅ The builder's extra validation (positive durations, `vram_free_threshold_mb` >= 0, `preempt_grace_min` >= 0, `batch_in_chat_min_priority` 0-100, no inf/nan, non-empty and NUL-free `vram_check_cmd`, no userinfo in URLs, decider_chain profile-key regex) does not contradict any design §7 default. The design YAML validates with the two ruling amendments.
- ⚠️ Cannot verify from diff: mounting at `cfg.resilience` and the `Europe/London` part of `cfg_default` (T10-03, by program ruling). The acceptance command written literally as `pytest -k "UT08-03"` selects 0 tests (the hyphen). The program convention `-k UT08_03` selects 25 and all pass (global-constraints line 8), so this is not a finding.

### Gates (re-run by the reviewer in the worktree)
ruff check: pass. ruff format --check: 53 files formatted. mypy: no issues. lint-imports: 10 kept, 0 broken. check_type_ownership: exit 0. `pytest -m "(unit or integration) and not slow" -q -p no:logging` (PYTHONUTF8=1): 270 passed, 4 deselected. `-k UT08_03`: 25 passed.

### Strengths
- The design-equality test parses the actual §7 YAML block, so defaults cannot silently diverge from the design.
- The root cross-check messages carry the full path (`schedule.jobs[i].then[n].gpu_class`).
- The module is compact and readable, and every model shares one strict, frozen base.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. settings.py:70: `_catch_up` lets a non-`ValueError` escape model validation. The input `catch_up_max: "99999999999999999999d"` raises a raw `OverflowError` ("Python int too large to convert to C int") from `timedelta(...)`, not a `ValidationError`. The reviewer reproduced this with `MaintenanceSchedule(catch_up_max="99999999999999999999d")`. This breaks the U08-06/U08-07 Errors row ("Validation failure → pydantic ValidationError; spec 10 loader reports it as ConfigError with the key path"): an operator typo would crash the loader with a traceback and no key path. Fix: compare `int(match[1])` against a bound before building the `timedelta`, or catch `OverflowError` and re-raise it as `ValueError`. Add a test case.

#### Minor (Nice to Have)
1. settings.py:33/66: `_CATCH_UP_RE.match` uses `$`, which in Python `re` also matches before a trailing newline, so `"6h\n"` is accepted and stored verbatim. `\d` also matches non-ASCII digits. Use `fullmatch` with `[0-9]`. The same applies to `_CRON_FIELD_RE` (settings.py:32), although `split()` already hides the newline case there.
2. settings.py:93: port `0` is accepted as an "explicit port" (`http://127.0.0.1:0`). Consider requiring 1-65535.
3. settings.py:325: the `sync.`/`reconcile.` prefix check can never fire, because `_NAME_RE` (settings.py:333) already forbids `.`. It is harmless, but it is dead code for spec parity. A comment would help.
4. settings.py:112: `frozen=True` stops attribute assignment, but the nested `dict` and `list` fields (`policies`, `classes`, `windows`, `jobs`, `payload`) can still be mutated in place, so "Concurrency: Immutable" holds only shallowly. Acceptable for now; note it for consumers.
5. tests/unit/core/conftest.py:10: the shared foundation conftest now imports `herness.core.resilience.settings` at module import, so every `tests/unit/core` test fails to collect if the resilience settings break. Consider a local import inside `cfg_default`, or moving the fixture to `tests/unit/core/resilience/conftest.py`.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Everything in the models, defaults, rulings and threats matches the spec, and all gates pass. One input path (an oversized `catch_up_max`) escapes as `OverflowError` instead of `ValidationError`, which violates the units' stated error contract. It is a one-line fix plus a test.

## Re-review round 1 (commit 377ddfd; scope: Important 1, Minor 1, 2, 5)

- ✅ Important 1 is resolved. `_catch_up` now uses `fullmatch(r"([0-9]{1,9})(m|h|d)")` and compares plain int seconds against 7 d, with no `timedelta`. I reproduced it: `"99999999999999999999d"` now raises `ValidationError`, not `OverflowError`. Tests cover the overflow case, and the 7 d boundary is exercised on both sides (10080m/168h/7d accepted, 10081m/169h rejected).
- ✅ Minor 1 is resolved. The cron-field and catch-up checks use `fullmatch`, and `\d` became `[0-9]` (also in `_HHMM_RE`). `"6h\n"` and non-ASCII digits are rejected, and tests cover this.
- ✅ Minor 2 is resolved. `port > 0` is now required (settings.py `_loopback_url`), and a test covers port 0.
- ✅ Minor 5 is resolved. `tests/unit/core/conftest.py` imports `ResilienceConfig` only under TYPE_CHECKING and lazily inside `cfg_default`.
- Minor 3 and 4 remain parked, as intended.
- No regressions. Gates re-run: ruff check pass; format 53 files formatted; mypy no issues; lint-imports 10 kept, 0 broken; check_type_ownership exit 0; `pytest -m "(unit or integration) and not slow" -q -p no:logging` (PYTHONUTF8=1) 282 passed, 4 deselected. settings.py is 381 of 390 lines.

**Task quality:** Approved
**Reasoning:** The one Important finding and the three minors that were in scope are fixed correctly, with tests. All gates pass and nothing regressed.
