# T11-05 report: Generator parameters and RNG

Status: DONE_WITH_CONCERNS
Commit: a79cc0a feat(synth): add generator params, presets, params hash and RNG streams (T11-05)
Worktree/branch: D:\herness\.claude\worktrees\agent-a4223eb3b67c31e20 / worktree-agent-a4223eb3b67c31e20

## Resume handling
The previous agent left untracked files. rng.py, __init__.py and all three test files matched the brief and were kept
(two small lint fixes in tests: an unused `noqa: S311` and one over-long comment line). params.py had been cut off after
~110 lines (presets and helpers only, with no SynthParams, load_params or params_hash), so it was rewritten. The
__pycache__ dirs were deleted. Test dirs have no __init__.py, which matches the repo layout (only tests/support has one).

## Files (lines / budget)
- tools/synth/__init__.py: 7 / 10. `GENERATOR_VERSION = "2.0.0"`.
- tools/synth/params.py: 219 / 300. SynthUsageError(ConfigError), ScalePreset, SCALE_PRESETS, SOURCES,
  PARAMS_FILE_MAX_BYTES, SynthParams, load_params, params_hash.
- tools/synth/param_groups.py: 210 / default 400 (NEW, not in the module map; see deviations). The 17 parameter-group
  models with embedded defaults, validators (distributions sum to 1 ± 1e-9, rates in [0,1], priority keys 1..5,
  ordered min/max pairs) and check_zone (via herness.core.time.zone).
- tools/synth/rng.py: 54 / 80. STREAM_* constants, shard_key_hash, shard_rng, stream_rng.
- tests/unit/tools/synth/test_synth_params.py, tests/unit/tools/synth/test_synth_rng.py,
  tests/integration/tools/synth/test_synth_rng_subprocess.py

## Units
- U11-01: SCALE_PRESETS matches the verbatim values. `5m` is not a key.
- U11-02: load_params does these steps in order: normalize scale (5m→full); reject daily with full (key fetch_mode);
  check sources is a non-empty subset, then de-duplicate into canonical order; for tiny, set start = end − 89 days;
  require start < end (key start). If a params file is given: read at most 256 KB + 1 byte (oversize, missing,
  non-UTF-8, bad YAML or non-mapping → key params_file), `yaml.safe_load`, normalize keys to str while counting nodes
  (cap 100,000 as the alias-bomb guard, TH11-08), reject CLI-argument keys in the file (e.g. key `scale`), deep-merge
  over model_dump(mode="json") of the defaults (maps merge, lists replace), then validate. A pydantic error becomes
  SynthUsageError(key=<dotted loc path>), with the key cut to the argument name for argument fields. params_hash =
  "sha256:" + SHA-256 of canonical JSON (sorted keys, no whitespace, floats format(x, ".12g")) of
  model_dump(mode="json").
- U11-03: follows the brief exactly (SHA-256 first 8 bytes BE over "\x1f".join(key); SeedSequence(seed,
  spawn_key=(hash,)); PCG64). A negative seed raises SynthUsageError.

## Default values
Taken from the brief/design §5.1.3/§5.1.4/§5.1.6, plus values fixed by the impl 11 generator units (U11-07 servicenow,
U11-08 jira, U11-09 monitoring, U11-05 text, U11-16 dirty):
- ack medians {1:5, 2:15, 3:45, 4:90, 5:180} min, σ 0.9
- hour curve: 2.2 for 10–15, 0.3 for 0–6 and 20–23, else 1.0
- 10 holiday dates, holiday_factor 0.5
- Jira split story .48 / bug .20 / task .12 (0.80 × .60/.25/.15), cycle_time_sigma 0.8
- text.casing_noise_rate 0.05
- dirty_rates.heavy_multiplier 5.0
- DD11-05 event shares 0.50 / 0.90

Other constants in those units (window hours, latency medians, cost-per-point range, etc.) were left as unit-local
constants and are not parameters.

## Tests covered
UT11-01, UT11-02, UT11-03, UT11-04, PT11-01 (in-process Hypothesis test plus a fresh-subprocess integration test with
PYTHONHASHSEED=random), PT11-04 (Hypothesis: key order, YAML flow/block/indent, and JSON form give the same hash).
Added a UT11-03 test that a YAML alias bomb is rejected quickly. 36 tests pass (about 1.7 s).

## Gates (all green)
- ruff check . : All checks passed
- ruff format --check . : 101 files already formatted
- mypy : Success (39 files)
- mypy --strict tools/synth : Success (4 files)
- lint-imports : 8 kept, 0 broken
- python -m tools.check_module_size : exit 0
- PYTHONUTF8=1 pytest -m "(unit or integration) and not slow" -q -p no:logging : 609 passed, 1 skipped, 1 xfailed
  (the xfail is the pre-existing traceability marker)

## Deviations / concerns
1. NEW MODULE tools/synth/param_groups.py (not in the card's Files list or the §2 module map). The full parameter model
   with all 18 groups, compactly written and ruff-formatted, came to 396 lines in params.py against the 300 budget.
   That made MS001 fire, which broke IT00-02 (test_check_scripts) in the fast suite. So the group models moved to a
   sibling module. The public API (ScalePreset, SCALE_PRESETS, SynthParams, load_params, params_hash, SynthUsageError)
   stays in tools.synth.params. Impl 11 §9 says the defaults are "embedded in tools/synth/params.py": they are now
   embedded in tools/synth/param_groups.py, which params.py imports. Controller ruling needed: either add a module-map
   row for param_groups.py (Tooling, pydantic, ~220) or raise the params.py budget to ~400 and merge the two back.
2. SynthParams fields added beyond the brief's group names are listed in "Default values" above (all values from impl
   units, none invented).
3. No dependency on T10-03 (HernessConfig) was needed. No carry-over.

## Fix round 1 (review T11-05-review.md)
Commit: 8c11ea7 fix(synth): review fixes for params (T11-05)
- Important 1: a self-referential alias (`a: &a [*a]`, `a: &a {b: *a}`) now fails with SynthUsageError(key
  params_file). Normalization tracks the key path and caps depth at 32 (`_MAX_DEPTH`), and a RecursionError around
  normalization is also mapped to params_file. New parametrized UT11-03 test covers both inputs.
- Minor 1: key sets are pinned with the `_keys(...)` validator for org.criticality {1..4}, priority maps {1..5},
  change.types, change.close_codes, event.severities, jira.issue_types and jira.story_points {1,2,3,5,8,13}.
- Minor 2: Rate/Positive/NonNegative/Percent use allow_inf_nan=False plus a before-validator that rejects bool.
  Counts are strict ints. Numeric strings are still accepted for floats on purpose: PyYAML (YAML 1.1) reads JSON
  exponent floats such as `1e-09` as strings, and rejecting them broke PT11-04 for a JSON-formatted params file.
- Minor 3: keys that collide after str conversion (`1` and `'1'`) raise SynthUsageError naming the path
  (e.g. `mttr.median_hours.1`).
- Minor 4: the GENERATOR_VERSION assertion moved into the UT11-02 defaults test (no row covers it on its own; it
  changes together with the defaults). The separate UT11-01-labelled test is gone.
- Minor 5: the alias-bomb test moved into the UT11-03 section, next to the new UT11-03 tests.
- Minor 6: both `# type: ignore` comments removed (typed `_normalize_map`; isinstance-narrowed `_merge`).
- Lines: params.py 238/300, param_groups.py 245/400 default, rng.py 54/80, __init__.py 7/10.
- Gates: ruff check/format clean; mypy and mypy --strict tools/synth clean; lint-imports 8 kept;
  check_module_size exit 0; synth tests 43 passed; fast suite 616 passed, 1 skipped, 1 xfailed.
