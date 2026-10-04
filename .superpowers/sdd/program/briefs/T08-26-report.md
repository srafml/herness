# T08-26 report: Configuration section models

Status: DONE_WITH_CONCERNS (minor, see Concerns)
Commit: 2efb43f feat(resilience): add config/resilience.yaml section models (T08-26)
Worktree: D:\herness\.claude\worktrees\agent-aa1aad6752aca051b

## Implemented
- `herness/core/resilience/settings.py` (382 lines; budget 390): U08-06 models (`PolicySettings`, `JobBackoffSettings`,
  `RetrySettings`, `BreakerSettings`, `BreakersSection`, `FallbackSettings`, `LoopSettings`, `TaskSettings`,
  `JobsSettings`, `HealthCheck`, `ServiceSettings`, `GpuClassSettings`, `GpuSettings`, `ResilienceSection`) and
  U08-07 models (`WindowSpec`, `ChatSchedule`, `RekeySchedule`, `ChainStep`, `ScheduledJob`, `MaintenanceSchedule`,
  `ScheduleSection`, `ResilienceConfig`). All `extra="forbid"`, `strict=True`, `frozen=True` through a private
  `_Model` base. Imports: stdlib, pydantic, `herness.core.types` only (R-03).
  - Policy keys must be exactly `PolicyName` minus `gpu_health` (error loc `retry.policies`).
  - Loopback URL check: scheme `http`, host 127.0.0.1/localhost/::1, explicit valid port, no userinfo (TH08-12).
  - `bearer_secret` regex `^secret:[A-Z][A-Z0-9_]{0,63}$` (R-53). Service names from the `ServiceName` literal (TH08-01).
  - Cron shape only (5 fields, `^[0-9A-Za-z*/,\-]+$`); `catch_up_max` `^\d+(m|h|d)$` and <= 7 d.
  - Root validator: window classes/preload and non-`none` ChainStep gpu_class must be `gpu.classes` keys; the
    message names the path (e.g. `schedule.jobs[0].then[0].gpu_class`).
- `__init__.py` unchanged (settings names are not part of §3.3-§3.8; lazy map untouched, 62/70 lines).
- No `HernessConfig.resilience` field and nothing in herness.core.config (T10-03's job, per program ruling).

## cfg_default fixture
`tests/unit/core/conftest.py::cfg_default` returns a `ResilienceConfig` validated from
`tests/unit/core/fixtures/resilience.yaml` via `yaml.safe_load` (no spec 10 loader). That file is the design 08 §7
YAML block verbatim with two amendments: nightly `job.gpu_class: none` (R-43) and OpenJev
`bearer_secret: "secret:OPENJEV_API_KEY"` (R-53). (`tests/.../data/` was not usable: `.gitignore` ignores `data/`.)
UT08-03 also re-extracts the YAML block from `docs/specs/08-resilience-and-jobs.md`, applies the same two amendments
and asserts equality with `cfg_default` and a round-trip `model_dump(exclude_unset=True)`, so the fixture cannot
drift from the design. The business timezone `Europe/London` part of `cfg_default` in impl §11 is not in this file
(it is herness.yaml / spec 10); later cards can extend the fixture once T10-03 exists.

## Tests (tests/unit/core/resilience/test_resilience_settings.py, 45 cases)
- UT08-03: design equality, R-43/R-51/R-53 values, per-field defaults, frozen/extra-forbid, missing policy key and
  extra `gpu_health` rejected, non-loopback URLs (7 cases) rejected, `openjev.api_key` and other bad references
  rejected (with path), GPU class/service rules, policy/breaker/fallback/jobs bounds.
- UT08-05 (model part only; U08-99 cron parse and week coverage stay with T08-02): 4-field cron rejected at
  `("schedule","jobs",0,"cron")`, `60 * * * *` passes the shape check, `then` kind `foo` rejected at its path,
  preload not in classes, window class not a gpu class, root cross-check paths, window/scheduled-job/chain-step rules.

RED: `pytest tests/unit/core/resilience/test_resilience_settings.py` ->
`ModuleNotFoundError: No module named 'herness.core.resilience.settings'` (conftest import).
GREEN: same file -> `45 passed`; coverage of settings.py 100 % line, 100 % branch.
`pytest -k "UT08_03 or UT08_05"` -> 45 passed.

## Gates (all run with .venv python directly)
- ruff check . : All checks passed; ruff format --check . : 53 files already formatted
- mypy: Success, no issues
- lint-imports: 10 kept, 0 broken (incl. `resilience-settings-light` now matching the new module; no contract change)
- tools.check_type_ownership: exit 0
- `pytest -m "(unit or integration) and not slow" -q -p no:logging` (PYTHONUTF8=1): 270 passed, 4 deselected

## Deviations / choices
- Bounds not stated in the unit table but added as sanity: positive durations in `JobsSettings`/`GpuSettings`/
  `ServiceSettings.start_timeout_s`, `vram_free_threshold_mb >= 0`, `preempt_grace_min >= 0`,
  `batch_in_chat_min_priority` 0-100, floats reject inf/nan, `vram_check_cmd` non-empty and NUL-free like
  `compose_cmd`, service URL rejects userinfo, decider_chain profile keys use `^[a-z][a-z0-9_]{0,31}$`.
- Fully-defaulted sub-sections (`loop`, `tasks`, `chat`, `rekey`, `maintenance`) get `default_factory`; other
  sections without a stated default are required.
- `ChainStep.priority` None means `DEFAULT_PRIORITY[kind]`; `DEFAULT_PRIORITY` does not exist in
  `herness.core.types` yet and is not needed by this card (documented in the docstring only).
- The `S105` noqa comments (pattern / reference strings, not secrets) carry reasons.

## Concerns
- Card lists T10-03 as a dependency; per program ruling nothing in herness.core.config was touched, so
  `cfg.resilience` mounting remains for T10-03.
- `cfg_default` currently yields a `ResilienceConfig`, not a full `cfg`; when T10-03 lands, the fixture should
  become the full config (with `Europe/London`) and tests switch to `cfg.resilience`.
- The raw design §7 YAML (with `openjev.api_key`) does not validate by design (UT08-03 requires rejection); the shipped
  `config/resilience.yaml` (spec 10) must use `secret:OPENJEV_API_KEY`.

## Fix round 1 (commit 377ddfd fix(resilience): harden settings validators (T08-26 review round 1))
- Important 1: `catch_up_max` is parsed with `fullmatch(r"([0-9]{1,9})(m|h|d)")` and compared as plain int seconds
  against 7 d (no `timedelta`), so `99999999999999999999d` is a ValidationError, never OverflowError.
- Minor 1: cron-field and catch-up regexes use `fullmatch` (no `$`), and `\d` became `[0-9]` (also in `_HHMM_RE`),
  so `6h\n` and non-ASCII digits are rejected.
- Minor 2: port 0 is rejected in service URLs.
- Minor 5: `tests/unit/core/conftest.py` imports `ResilienceConfig` only under TYPE_CHECKING and lazily inside
  `cfg_default`.
- New tests: catch_up_max rejects (overflow, trailing newline, Arabic-Indic digit, `1w`, 10081m, 169h), accepts
  boundaries (10080m, 168h, 7d, 0m); ASCII-only window time and cron; port 0 rejected.
- Gates: ruff/format clean, mypy clean, lint-imports 10 kept, check_type_ownership 0; settings tests 57 passed
  (100 % line/branch); `(unit or integration) and not slow` 282 passed. settings.py 381 lines (budget 390).
