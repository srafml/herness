# T11-05 review: Generator parameters and RNG

Reviewed commit a79cc0a (base 79f73d6) in worktree agent-a4223eb3b67c31e20. I checked the unit specs against impl 11 (U11-01..03, the rows for UT11-01..04, PT11-01 and PT11-04, and TH11-08) and design §5.1.1/§5.1.3/§5.1.4/§5.1.6/§5.1.7. I also re-checked the defaults taken from U11-05/07/08/16.
Focused runs: the 3 card test files gave 36 passed in 1.84 s. `mypy --strict tools/synth` succeeded on 4 files. An edge-case probe script called `load_params` with hostile or odd params files; its results are below.

### Spec Compliance
- ✅ U11-01 SCALE_PRESETS. The frozen slotted dataclass has all 12 fields, and the tiny, small and full values match the invariants exactly. `5m` is not a key. small and full share catalog_class "standard".
- ✅ U11-02 signature. `load_params` takes keyword-only args exactly as specified. `params_hash(p) -> str`.
- ✅ U11-02 algorithm order. Steps: normalize scale (5m→full), reject daily with full (tools/synth/params.py:126), check sources, apply the tiny span (start = end − 89 d), check start < end, then size cap, `yaml.safe_load`, mapping check, deep merge (maps merge, lists replace) and validate.
- ✅ U11-02 model. It uses pydantic `extra="forbid"` and `frozen=True`. All 18 groups plus `business_timezone` are present, with default "UTC" validated as an IANA zone.
- ✅ U11-02 defaults vs design §5.1.3/§5.1.4/§5.1.6 and DD11-05. Every value matches: org criticality, Poisson 12.5 in [6,20], Pareto 1.2 with 5 %/40 % and x1.5, priority mix, weekend 0.35/0.30, annual +-0.1, 10 holidays, ack 85 %, MTTR medians and sigma 0.9/0.2, reassign 0.6/+0.8 above 1.3, reopen 4 %/6 %, SLA 4/12/72/168/720 h, impact 70 % U(0.3,1.0), change and close-code mixes with x3, problems 1/80 and 40 %, event severities, near-incident 0.50/0.90 (DD11-05), availability 99.5-99.99, the Jira tree 1/5/14/80, points p, cycle median 6 d, 12 %/15 %/3 %, text 1 %/5 %/20 %, PII 3 %/1 %, 1-3 spans, 500 names, and all 9 dirty rates.
- ✅ Defaults from later unit specs match the spec text (ruling 2 check):
  - ack medians {5,15,45,90,180} sigma 0.9 match U11-07.
  - The hour curve (0.3 for 0-6 and 20-23, 2.2 for 10-15, else 1.0; 24 values) matches U11-07.
  - The holiday list (all 10 dates) matches U11-07.
  - Jira .48/.20/.12 equals .80 x .60/.25/.15, and sigma 0.8 matches U11-08.
  - casing 0.05 matches U11-05.
  - heavy x5, capped at 1.0, matches U11-16.
  - No disagreement found.
- ✅ U11-02 invariants. Each distribution must sum to 1 +- 1e-9 (uses fsum). Rates must lie in [0,1]. Priority keys must be 1..5, and min/max pairs must be ordered.
- ✅ U11-02 errors. A bad value, unknown key, oversized file, non-mapping, malformed YAML or missing file raises `SynthUsageError(ConfigError)`, as declared in impl 11 §3 for tools/synth/params.py, with `context["key"]` set to the dotted path. One exception (recursive alias) is covered under Important below.
- ✅ U11-02 params_hash. It returns "sha256:" + SHA-256 of canonical JSON of `model_dump(mode="json")` with nothing excluded: sorted keys, no whitespace, ISO dates and `.12g` floats.
- ✅ U11-03. `shard_key_hash` takes the first 8 bytes of SHA-256, big-endian unsigned, over `"\x1f".join(key)`. `shard_rng` and `stream_rng` use SeedSequence(seed, spawn_key=(hash,)) with PCG64. A negative seed raises SynthUsageError. All 5 STREAM_* constants are present, and Python's `hash()` is never used.
- ✅ `GENERATOR_VERSION = "2.0.0"`, which matches the design spec line 164.
- ✅ Tests and ID markers:
  - UT11-01: `test_ut11_01_*`.
  - UT11-02: 5m alias, tiny span, defaults, hash, merge, sources.
  - UT11-03: daily+full, unknown key with its path, 300 KB file, sum 0.9, and a bad-values table.
  - UT11-04: recorded vector 0x55dfed315c1801ab, recorded draws, streams differ.
  - PT11-01: in-process Hypothesis test plus a fresh-subprocess run with PYTHONHASHSEED=random.
  - PT11-04: Hypothesis over key order, flow/block/indent YAML and JSON form.
  - Every test carries its ID in both its name and its docstring.
- ✅ TH11-08. The file is read capped at 256 KB + 1 byte, parsed with `yaml.safe_load`, and alias expansion is capped at a 100k node count. The recursive-alias hole is listed under Important below.
- ✅ Acceptance. The tests pass and `mypy --strict tools/synth` is clean. The report says the full gates are green; I did not re-run them.
- ⚠️ Cannot verify from diff: the full fast suite, ruff, lint-imports and check_module_size results. These are the report's claims, and I only re-ran the card tests and mypy.
- ⚠️ tools/synth/param_groups.py is outside the Files list and module map. It is accepted by sub-controller ruling 1, and a module-map row should follow at integration.

### Strengths
- The public API matches the unit signatures exactly. Error keys are precise dotted paths, and argument fields are trimmed to the argument name.
- RNG determinism is proven three ways: a recorded hash vector, recorded draws against an explicit SeedSequence construction, and a fresh-interpreter comparison with a random hash seed.
- It goes beyond the spec on TH11-08 with an alias-bomb node cap, which is tested.
- Every default is traceable to a spec sentence. The comments name the unit spec each non-design value comes from.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. **A recursive YAML alias escapes as a raw `RecursionError` instead of `SynthUsageError`.** Location: tools/synth/params.py:150 and :156-164.
   - What happens: `yaml.safe_load` builds a self-referencing object from `a: &a [*a]` or `a: &a {b: *a}` without error. `_normalize` then recurses until the depth limit and raises `RecursionError`, before it reaches the 100k node cap. The `except (..., RecursionError)` at :146 only covers `safe_load`.
   - Probe result: both inputs give `RecursionError`, not `SynthUsageError`.
   - Why it matters: this breaks the U11-02 contract that every hostile or bad YAML becomes a `SynthUsageError` naming the key (exit 3, R-46). This is the TH11-08 threat surface. The CLI would crash with a traceback and a wrong exit code instead of exiting 3.
   - Fix: catch `RecursionError` around the `_normalize` call, or track a depth or id-set in `_normalize` and `_fail("params_file", ...)`. Add a UT11-03 case for both inputs.

#### Minor (Nice to Have)
1. **Distribution maps do not pin their key set.** Location: tools/synth/param_groups.py:46-47, used at :78, :134-140, :150, :169, :177. `Dist`/`IntDist` accept any extra key as long as the sum stays 1. For example, `event: severities: {critical: 0.0, critcal: 0.05}` is accepted (probed), so a typo becomes a silent new category. Only priorities are pinned (`_check_priorities`). The fixed sets are change types, close codes, severities, Jira issue types and org criticality 1..4. Consider pinning them to their fixed sets, in line with the "unknown key -> SynthUsageError" rule.
2. **Lax coercions and non-finite values are accepted.** Location: tools/synth/param_groups.py:44-45.
   - `text: {typo_rate: true}` is accepted as 1.0 (pydantic lax bool->float).
   - `org: {teams_per_org_mean: .inf}` is accepted, because `Positive` has no finiteness bound.
   - Consider `strict`/`allow_inf_nan=False` on `Rate`/`Positive`.
3. **Colliding keys merge silently after `str(k)`.** Location: tools/synth/params.py:162. `str(k)` merges colliding keys (`{1: 2, '1': 5}`), and the last one wins without error.
4. **One test is filed under the wrong test ID.** Location: tests/unit/tools/synth/test_synth_params.py:86. `test_ut11_01_generator_version` is labelled UT11-01, but UT11-01 covers only SCALE_PRESETS. The check is fine; the ID attribution is loose.
5. **The alias-bomb test sits in the wrong section.** Location: tests/unit/tools/synth/test_synth_params.py:338. The UT11-03 alias-bomb test is placed after the PT11-04 section. This is layout only.
6. **Two `# type: ignore` comments could be avoided.** Location: tools/synth/params.py:150 and the `_merge` ignore at about :172. A `cast` or typed helper would avoid them.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The spec is followed closely: every value, algorithm step and test ID matches, and determinism is well proven. However, a recursive alias in a params file crashes with `RecursionError` instead of the required `SynthUsageError`. That is a gap in the TH11-08 and error contract, and it needs a small fix and a test.

## Re-review round 1 (commit 8c11ea7)

Scope: only the earlier findings (Important 1, Minors 1-6).
Evidence:
- The worktree HEAD is 8c11ea7.
- The synth tests (unit + integration) gave 43 passed in 1.60 s.
- `mypy --strict tools/synth` is clean on 4 files.
- I re-ran the round-0 edge-case probe script against the new code.

- ✅ Important 1 (recursive alias) is resolved.
  - `_normalize` now tracks the key path and caps depth at 32 (`_MAX_DEPTH`).
  - A `RecursionError` around `_normalize_map` is mapped to `params_file`.
  - Probe result: `a: &a [*a]` and `a: &a {b: *a}` now raise SynthUsageError(key=params_file).
  - The new parametrized UT11-03 test covers both inputs.
- ✅ Minor 1 (map key sets) is resolved. `_keys(...)` pins the key sets of org.criticality, the priority maps, change.types, close_codes, severities, jira.issue_types and story_points. Probe result: a misspelled severity key now raises with key `event.severities`.
- ✅ Minor 2 (bool, inf and nan values) is resolved, partly by the accepted ruling.
  - bool, inf and nan are now rejected. Probe results: typo_rate `true` gives key `text.typo_rate`, and `.inf` gives key `org.teams_per_org_mean`.
  - Counts are strict ints.
  - Numeric strings are still accepted for floats, per the accepted ruling (PyYAML reads JSON exponents as strings). Not re-flagged.
- ✅ Minor 3 (colliding keys) is resolved. Keys that collide after `str()` now raise with the path. Probe result: `mttr.median_hours.1`.
- ✅ Minor 4 (test ID label) is resolved. The version assertion moved into the UT11-02 defaults test.
- ✅ Minor 5 (test placement) is resolved. The alias-bomb test now sits in the UT11-03 section.
- ✅ Minor 6 (`# type: ignore`) is resolved. No `# type: ignore` remains in tools/synth.
- Regression check: the params_hash of a default load is the same as in round 0, so the defaults did not change.

New findings: none.

**Task quality:** Approved
**Reasoning:** Every round-0 finding is fixed and each fix has a test. The card's tests and the strict type check pass.
