# T10-07 Log scrubber - review

## Spec compliance (per row)

| Unit/Test | Status | Notes |
|---|---|---|
| U10-32 `known_values` | ✅ | Unchanged snapshot semantics, `_KNOWN_LOCK`-protected. |
| U10-32 `scrub_secrets` algorithm step 1 (rebuild only on change) | ✅ | `_KNOWN_VERSION` bumped only when `_KNOWN` size actually changes (`_remember`/`_reset`, secrets.py:104-134); rebuild gated on `_scrub_version != _KNOWN_VERSION` (secrets.py:147). |
| step 2 (longest-first alternation, atomic swap under `_KNOWN_LOCK`) | ✅ | `sorted(_KNOWN, key=len, reverse=True)` then single-assignment swap inside the lock (secrets.py:146-152). |
| step 2 (walk dicts/lists/tuples, depth ≤ 6) | ✅ (with a caveat) | Containers stop recursing past depth 6 (secrets.py:127-133), but a string is masked regardless of the depth counter reaching it (the `isinstance(str)` branch returns before the depth gate, secrets.py:114). Net effect: strings immediately under a depth-7 container are still masked; only a string buried inside a *depth-8+* container goes unmasked (the whole subtree is skipped). This is a defensible reading of "depth ≤ 6" as a container-walk bound, not a value-processing bound, but it is **not exercised by any test** — see Important finding. |
| step 2/step 3 (`***` vs `[SECRET]`) | ✅ | Known-value pattern → `***`; CREDENTIAL/URL_TOKEN detector spans → `[SECRET]`, non-overlapping spans only (secrets.py:118-125). |
| step 4 (tracebacks scrubbed via T00-06 pipeline; inserted last before renderer) | ✅ | Verified against `herness/core/logging.py`: `EXC_RENDERER` runs before `scrub_secrets` in both `clean_early(scrubber)` (structlog-side, logging.py:158) and the `ProcessorFormatter` chain (`_formatter`, logging.py:117-129), and `scrubber` is appended immediately before `limit_sizes`/`render_json`. |
| 64 KiB truncation | ✅ implemented (secrets.py:115) | **Untested** — no test with a >64 KiB string. |
| Failure → `{"event": "log.scrub.failed"}` | ✅ implemented (secrets.py:154-155) | **Untested** — coverage report shows this branch (154-155) is never hit by any test. |
| Concurrency (`_KNOWN_LOCK`, atomic swap) | ✅ | Rebuild and read of `_scrub_pattern` both happen under the lock; the local `pattern` reference used outside the lock is safe against concurrent rebuilds. |
| UT10-34 (nested dict/list masking) | ✅ | `test_ut10_34_nested_known_value_masked`, passes. |
| UT10-35 (password=/SAS URL → `[SECRET]`) | ✅ | `test_ut10_35_credential_and_url_token_masked`, passes. |
| ST10-15 (exception with resolved secret, exc_info) | ✅ | `test_st10_15_exception_with_secret_is_scrubbed`, exercises the real `configure_logging` pipeline end-to-end (both scrub passes), passes. |
| Idempotency ruling | ✅ mostly | `test_ut10_35_scrub_secrets_is_idempotent` only exercises the CREDENTIAL-detector path (`password=abc123xyz`) twice; it does not re-run a *known-value* (`***`)-masked event through `scrub_secrets` a second time to directly confirm that path's idempotency (relies on the ST10-15 real-pipeline test doing this implicitly). Minor. |
| Test IDs/docstrings/pytestmark | ✅ | All four new tests carry their ID in the function name and as the docstring's first line; module `pytestmark = pytest.mark.unit` already present and unchanged. |
| Import-time `_SCRUB_DETECTORS` construction | ✅ | Verified against `herness/core/redact_patterns.py:296-316`: `build_detectors` builds `CREDENTIAL` and `URL_TOKEN` from fixed prefilters/finders, not from `cfg.id_patterns`/`cfg.custom_patterns`/`cfg.national_id_patterns`; a default `RedactionConfig()` is safe and correct for these two entity types specifically. |
| Dict keys | ⚠️ not scrubbed | Only `event_dict.items()` values are walked (secrets.py:130); keys are never passed through `_scrub_value`. This matches the algorithm's literal wording ("Walk event_dict values recursively") and field names aren't expected to carry secrets, so not flagged as a defect, but noting it since it was explicitly asked about. |

## Gates (re-run in worktree, independent of report)
- `uv run python -m tools.check_module_size` → exit 0.
- `uv run ruff check herness/core/secrets.py tests/unit/core/test_secrets.py` → All checks passed.
- `uv run mypy herness/core/secrets.py` → Success, no issues.
- `PYTHONUTF8=1 uv run pytest tests/unit/core/test_secrets.py --cov=herness.core.secrets --cov-branch -q -p no:logging` → 24 passed, **97% line / 97% branch** (248 stmts/7 miss, 66 branches/2 partial) — exceeds the 90%/85% gate. Uncovered: line 128 (depth>6 return), 154-155 (failure fallback), 122->121 (overlapping-span skip branch), 172-175 (pre-existing, unrelated `_keyring_call` backend-failure path).

## Findings

### Critical
None.

### Important
- secrets.py:127-128 and 154-155 — the two most security-relevant branches of `scrub_secrets` (the 64 KiB truncation/depth-6 cutoff boundary, and the TH10-07 "last line of defence" `{"event": "log.scrub.failed"}` fallback) are never exercised by any test — confirmed by the coverage run above (0 hits on 128 and 154-155). A regression that made the fallback itself raise, or that mis-implemented the depth cutoff so a secret-bearing subtree survives, would not be caught. Recommend one test that forces `_scrub_value`/`pattern.sub` to raise (e.g. monkeypatch) and asserts the exact `{"event": "log.scrub.failed"}` output, and one test with a container nested past depth 6 confirming the subtree is left unwalked as designed.

### Minor
- tests/unit/core/test_secrets.py:411-417 (`test_ut10_35_scrub_secrets_is_idempotent`) exercises only the CREDENTIAL-detector idempotency path, not the known-value (`***`) masking path directly (relies on ST10-15's full-pipeline double-pass for that). A direct `scrub_secrets` re-run on a `***`-masked known-value event would close this gap cheaply.
- No test for the 64 KiB per-string truncation (secrets.py:115); low risk given the code is simple slicing, but it's an explicit spec limit with zero direct coverage.
- Report/deviation section: parked T10-06 minors r1-m1/m2/m3 left unfixed at the 360/360 line budget — consistent with the stated ruling, not a new finding, just confirming it's intentional and documented.

## Verdict
**Approved** (with follow-up recommended, not blocking): all U10-32 algorithm steps are correctly implemented and match the binding spec and rulings (idempotent double-pass wiring in `logging.py` verified independently, import-time detector construction verified safe), all required tests (UT10-34, UT10-35, ST10-15) pass, and all gates (module size, ruff, mypy, coverage ≥90%/85%) pass with margin. The one Important item is a test-coverage gap on the failure-fallback and depth-cutoff branches of a security-critical function, not an implementation defect — recommend closing it in this card or a fast follow-up before this scrubber is relied upon as the "last line of defence."
