# T03-10 Calibration: review (verify agent)

Worktree agent-ad8bea210b9833b65, HEAD dc00c47 (base 48f28a3). Read-only review. Gates re-run by the reviewer.

### Spec Compliance
- ✅ Spec compliant.
  - U03-42 apply_temperature ✅. The bool path is clip, then sigmoid(logit/t) via log/log1p, returning [p', 1-p']. choice/score is softmax(log(p+1e-9)/t) with max subtraction. t <= 0, NaN and inf raise ConfigError. Probed: rows sum to 1 (bool extreme [1-1e-12, 1e-12] at t=0.05 has sum error 0), a one-hot choice row at t=0.05 has no overflow, exact ties are kept ([0.5,0.5] bool gives argmax 0; equal choice entries give equal outputs). See M1 for the near-0.5 edge case.
  - U03-43 fit_temperature ✅. bounded minimize_scalar over (0.05, 10), xatol 1e-4, maxiter 500 (fits the "<= 500 evaluations" budget). `res.success` false returns 1.0, and the private `_fit` returns None so `cross_fit` marks the question uncalibrated. The clamp at calibrate.py:76 is redundant but harmless.
  - U03-44 ece ✅. argmax takes the lowest index on ties, sort uses kind="stable", then `array_split(order, min(n_bins, n))` and the weighted |acc - conf| sum. The clamp to [0,1] is a no-op guard.
  - U03-45 CalibrationResult ✅. frozen+slots, fields in the specified order. The invariant is enforced in `__post_init__` (builder deviation 5, accepted).
  - U03-46 cross_fit ✅. Step 1 covers n < 100 and either fold empty, returning (1.0, ece_raw, ece_raw, accuracy, n, True). Steps 2-4 match the spec. The fits are all done before the ECEs, which is equivalent. accuracy = mean(argmax == labels). A failed fit gives the uncalibrated result.
  - U03-47 CalibrationStore ✅:
    - Paths: Laya at `laya_dir(v)/calibration.json`, others at `calibration_file(...)`.
    - Validation: file keys exactly per §4.3, private pydantic models with extra="forbid" (plus strict, whose int-to-float acceptance was probed OK).
    - Laya qsv mismatch gives `{}`, so `temperature` returns (1.0, True). The 1 MB cap comes from stat.st_size. Memoized per path and mtime_ns.
    - Save: merges with existing entries, writes atomically (mkstemp in the same dir, fsync, os.replace, temp cleaned on failure) and drops the memo.
    - Errors: malformed JSON or schema raises ConfigError with path.name in the message and path=str(path).
    - as_table has the 5 columns in the specified order with a fixed schema.
  - Tests:
    - UT03-39 ✅ (choice/score/bool, T in {0.5, 1, 2}, identity at T=1, bad t, bad shape).
    - UT03-40 ✅ (choice K=4 and bool, n=6000, seeded, |T-2|/2 < 5 %; failure path via monkeypatch). The bool synthetic is exact because logit of softmax(2z) = 2*(z0-z1).
    - UT03-41 ✅ (30 rows, 15 bins of 2, exact Fraction oracle, shuffled input so the stable sort is exercised; see M3).
    - UT03-42 ✅ (99 rows; 200 rows with fold all 0; invariant).
    - UT03-43 ✅ (1000 rows sharpened x3: T > 1, ece < ece_raw, accuracy exact).
    - UT03-44 ✅ (round-trip plus merge, missing file/entry gives (1.0, True), Laya other qsv treated missing, memo re-read, malformed x7, oversize, atomic-write failure cleanup).
    - PT03-04 ✅ (hypothesis, 150 examples, T in [0.05, 10], bool and choice).
- ⚠️ Cannot verify from diff / by test: nothing in scope. The consumers (EnsembleDecider, gate `calib` view, evaluate_candidate, the health reason `calibration_missing`) come in later cards.

Builder interpretations, judged:
1. A Laya save over another qsv replaces the old entries. Accepted: it follows from "treated as missing" plus "merge with existing", and U03-129 writes one calibration.json per candidate version.
2. A non-qsv identity mismatch (decider/version/qsv field differs from the path key) raises ConfigError naming the file. Accepted: it is a misplaced or corrupt file, and the tests cover it (`_doc(decider="jev")`, `_doc(question_set_version=...)`).
3. The extra shape checks raise ConfigError. Accepted: cheap, and they use the error class the spec already has.
4. CalibrationResult invariant raising ConfigError. Accepted; a file that breaks it is reported as malformed (calibrate.py:211).
5. Local `# type: ignore[import-untyped]` for scipy. Accepted: no scipy override exists in the worktree or in D:\herness\pyproject.toml, and no other herness module imports scipy. If a later card adds a scipy mypy override, strict `warn_unused_ignores` will flag this line.

### Gates (re-run by reviewer)
- `pytest tests/unit/enrich -q -p no:logging --cov=herness.enrich.calibrate --cov-branch`: 293 passed, 1 skipped (pre-existing symlink-privilege skip in test_layout.py:97). calibrate.py coverage is 100 % line and 100 % branch (173 statements, 28 branches).
- `ruff check`: clean. `ruff format --check`: 178 files formatted. `mypy`: 0 issues (81 files). `lint-imports`: 11 kept, 0 broken. `check_module_size`: exit 0 (calibrate.py 273 of 280). `check_type_ownership`: exit 0.
- Noise: uv prints `VIRTUAL_ENV=D:\herness\.venv does not match` warnings. That is environmental, not a finding against this card.

### Strengths
- The numerics are careful: logit via `log - log1p`, max-subtracted softmax, and float64 coercion. The fold fits share one `_fit` with a None failure channel, so `fit_temperature` keeps its spec contract while `cross_fit` still sees failures.
- The store is defensive without being heavy: strict pydantic, bounds on every numeric field, size cap before parse, and atomic write with temp cleanup tested.
- The tests assert real behaviour. UT03-41 shuffles its input, UT03-43 cross-checks T against a full fit, and the malformed matrix covers syntax, schema, extra keys, identity and invariant.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- M1 (plan-mandated algorithm; spec feedback), calibrate.py:53-56. The bool path looks only at column 0, so argmax can flip for near-0.5 rows:
  - Case (a): rows allowed by the ±1e-3 precondition, for example [0.5002, 0.5007]. Input argmax is 1, output argmax is 0.
  - Case (b): at float epsilon, p = nextafter(0.5, 0) collapses to [0.5, 0.5] for t >= 2, so argmax goes from 1 to 0. Probed.
  - The postcondition "argmax unchanged" and the verbatim algorithm conflict only here. Real bool deciders emit normalized rows, so there is no practical impact. Optional fix: use p = probs[:,0] / probs.sum(1) before the clip. Worth a note to the spec owner.
- M2, calibrate.py:238-256. `save` does not validate `results` against the `_Entry` bounds. A CalibrationResult with T outside [0.05, 10] (for example 0.01) is written and then rejected on load as malformed (probed). Because `save` itself loads first, every later save to that key also raises until the file is removed by hand. `cross_fit` cannot produce such a value, so this is latent. Suggest validating each entry through `_Entry` before writing.
- M3, tests/unit/enrich/test_calibrate.py:140-144. UT03-41's expected value is an exact Fraction re-derivation inside the test, not a literal. It is independent of numpy, argsort and array_split, and the strictly increasing confs make the pairing trivial by construction, so it is a valid oracle. Still, pinning the hand value `1693/6000` (≈ 0.28216666…) as a literal as well would match "hand-computed" more literally and guard against oracle drift.
- M4, calibrate.py:196-200. The 1 MB cap is checked on `stat` and then `read_bytes()` reads unbounded. A file swapped in between is read in full. This is local, trusted-dir TOCTOU. Optional fix: `read_bytes` capped by opening and reading `_MAX_FILE_BYTES + 1`.
- M5, test_calibrate.py:331. PT03-04 `assume`s a unique maximum, so tie preservation (brief: "argmax preserved incl. ties") is not property- or unit-tested. Ties were verified correct by reviewer probe. Adding one tie row to UT03-39 (for example [0.4, 0.4, 0.2] and [0.5, 0.5]) would pin it.
- M6, test_calibrate.py:243-252. The memo test only proves a re-read after an mtime change. It does not assert that a same-mtime read is served from the memo. Coverage reaches the branch, but no assertion checks it.
- M7, calibrate.py:202/211. The error message names only `path.name`. For Laya that is always `calibration.json` and does not identify the version. The full path is in the `path` detail, so this is acceptable. Consider including the parent dir name in the message.

### Assessment
**Task quality:** Approved
**Reasoning:** All six units and seven test rows match the brief, the numerics check out under targeted probes, the builder's interpretations are sound, and every gate is green with 100 % line and branch coverage. The remaining items are edge-case hardening and test-precision polish.


## Re-review round 1 (1df3ddd; scoped to M2, M3, M4, M6)

- M2 ✅ calibrate.py:257-261. `save` now validates the merged document with `_File` before `_write_atomic`. Out-of-bounds values (T outside [0.05, 10], ece/ece_raw/accuracy outside [0, 1], n < 0, NaN) raise ConfigError ("out of bounds", path detail) and nothing is written. The existing file is left unchanged. New test `test_ut03_44_store_save_rejects_out_of_bounds` covers both a new key (no file created) and a merge into an existing file (content preserved).
- M3 ✅ test_calibrate.py:148-150. The hand value is pinned as the literal `Fraction(1693, 6000)`, and `ece` is checked against `1693/6000` to 1e-12, in addition to the exact-fraction oracle.
- M4 ✅ calibrate.py:196-200. The read is bounded (`handle.read(_MAX_FILE_BYTES + 1)`, rejected when longer) and parses the same bytes. The stat-then-unbounded-read gap is closed. The existing oversize test still exercises the path.
- M6 ✅ test_calibrate.py:250-256. A second load with an unchanged mtime runs with `Path.open` patched to raise, which proves the memo hit. `_read` only calls `stat` before the memo check, so the patch cannot mask a real read.
- M1 (becomes a spec note), and M5, M7 parked: noted, not re-reviewed.

Gates re-run on 1df3ddd:
- pytest tests/unit/enrich: 294 passed, 1 skipped (the pre-existing symlink skip). calibrate.py is at 100 % line and branch (180 statements, 28 branches).
- ruff check: clean. ruff format --check: 178 files formatted.
- mypy: 0 issues (81 files).
- lint-imports: 11 kept.
- check_module_size: exit 0.

New findings: none blocking.
- Minor N1: calibrate.py is at 280/280 lines, exactly at its §2 budget. The next card that touches it (for example the `calibration_missing` existence probe in the carry-overs) will need to trim or move code.

**Task quality:** Approved
