# T10-07 Log scrubber - build report

Commit: aca2a68 on branch worktree-agent-a8ac72aa070bb1537
feat(core): add log scrubber scrub_secrets (T10-07)

## Starting point

A prior build agent died leaving a 6-line uncommitted WIP in herness/core/secrets.py:
the MutableMapping import, the redact_patterns.build_detectors / settings.RedactionConfig
imports and the __all__ += ["scrub_secrets", ...] entry. The diff was sound (matches the
imports scrub_secrets actually needs) and was continued from directly - no rework.

## Implementation

herness/core/secrets.py (U10-32):

- _KNOWN_VERSION (module int): bumped under _KNOWN_LOCK in _remember and _reset,
  but only when _KNOWN actually changes size - so a resolve of an already-known value, or
  a reset of an empty set, does not force a needless rebuild.
- _scrub_pattern / _scrub_version (module cache): scrub_secrets rebuilds the compiled
  alternation of re.escape(v) for all known values (longest first, so a value that is a
  prefix of another never shadows it) only when _scrub_version != _KNOWN_VERSION, under
  _KNOWN_LOCK; the pattern is swapped by a single attribute assignment (atomic in CPython).
- _SCRUB_DETECTORS: the CREDENTIAL and URL_TOKEN detectors from
  redact_patterns.build_detectors(RedactionConfig()) - built once at import time. Only these
  two entity types don't depend on the site's redact.yaml (id_patterns, custom_patterns,
  national_id_patterns do), and scrub_secrets's signature (fixed structlog processor shape)
  has no way to receive a live RedactionConfig, so a default instance is the correct and only
  usable source.
- _scrub_value(value, pattern, depth): the recursive walker.
  - Non-container, non-string values pass through unchanged.
  - Strings: truncated to 64 KiB (_SCRUB_MAX_CHARS = 64 * 1024), then pattern.sub("***", ...)
    for known values, then each detector's spans are masked to "[SECRET]" (spans sorted and
    non-overlapping matches skipped, so a later overlapping match can't corrupt the rebuilt
    string).
  - dict / list / tuple: recursed with depth + 1; beyond _SCRUB_MAX_DEPTH = 6 the
    subtree is returned unchanged rather than walked further (matches the spec's "depth <= 6").
- scrub_secrets(logger, method_name, event_dict): the structlog processor. Builds/reuses the
  known-value pattern, walks every value, and wraps the whole thing in
  except Exception: return {"event": "log.scrub.failed"} - nothing unscrubbed can escape a
  bug in the scrubber itself (TH10-07).

Idempotency (the binding ruling): herness/core/logging.py's pipeline calls the scrubber
twice per structlog-originated line - once inside clean_early(scrubber) (the structlog-side
processor chain) and again inside _formatter's ProcessorFormatter processors list (which
runs on every record, including ones already processed by structlog, per
structlog.stdlib.ProcessorFormatter semantics). scrub_secrets is naturally idempotent:
"***" and "[SECRET]" never match a known-value pattern (values are real secrets, not those
literal strings) or the CREDENTIAL/URL_TOKEN detectors ("[SECRET]" contains no ':', '=',
'-----', '://', etc., so _credential_prefilter/URL prefilters reject it outright). Verified by
test_ut10_35_scrub_secrets_is_idempotent (run twice, same output) and indirectly by
test_st10_15_... (real configure_logging pipeline, which really does call it twice).

scrub_secrets and known_values are not new APIs on top of stock structlog wiring - no
changes were made to logging.py / _log_pipeline.py / errors.py, per the ruling.

## Files changed

- herness/core/secrets.py - _KNOWN_VERSION, scrub constants, _scrub_value, scrub_secrets;
  _remember/_reset updated to bump the version counter. 360 lines (see Budget below).
- tests/unit/core/test_secrets.py - test_ut10_34_nested_known_value_masked,
  test_ut10_35_credential_and_url_token_masked, test_ut10_35_scrub_secrets_is_idempotent,
  test_st10_15_exception_with_secret_is_scrubbed; added the
  herness.core.logging import used by the new ST10-15 test.
- .secrets.baseline - regenerated (detect-secrets scan); only line-number shifts in the
  existing tests/unit/core/test_secrets.py entries (from the new import line) and the
  timestamp changed. The regen dropped the docs\impl\00-foundation.impl.md and
  docs\impl\10-config-security-deployment.impl.md entries as it always does; both were
  restored verbatim from the pre-regen baseline (verified via a JSON diff of results keys:
  dropped = exactly those two, added = none). File kept LF line endings.

## RED then GREEN evidence

RED: temporarily replaced the working-tree herness/core/secrets.py with the committed
(pre-T10-07) HEAD version and ran the new tests:

  4 failed - AttributeError: module 'herness.core.secrets' has no attribute 'scrub_secrets'
    (test_ut10_34_nested_known_value_masked, test_ut10_35_credential_and_url_token_masked,
     test_ut10_35_scrub_secrets_is_idempotent, test_st10_15_exception_with_secret_is_scrubbed)

GREEN: restored the implementation and re-ran the same selection:

  4 passed, 20 deselected in 0.10s

Full test_secrets.py + test_logging.py: 43 passed.

## Gate outputs

- uv run python -m tools.check_module_size -> exit 0.
- uv run ruff check herness/core/secrets.py tests/unit/core/test_secrets.py -> All checks
  passed (after adding # noqa: PLW0603 on the three global statements, matching the
  existing precedent in herness/core/registry.py, and # noqa: ANN401 on the structlog
  processor's logger: Any parameter).
- uv run ruff format --check ... -> both files already formatted.
- uv run mypy herness/core/secrets.py -> Success: no issues found.
- uv run lint-imports -> 13 kept, 0 broken (no new layering violations from the
  redact_patterns / settings imports).
- uv run --frozen python -m tools.check_type_ownership -> exit 0.
- PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging ->
  3446 passed, 5 skipped, 16 deselected, 1 xfailed (the pre-existing TR001/TR002/TR004/TR005
  doc-traceability xfail, unrelated to this card). One transient failure,
  test_it00_01_pre_commit_run_all_files, was .secrets.baseline being unstaged mid-run
  (detect-secrets refuses an unstaged baseline); after git add, pre-commit run --all-files
  passed all 17 hooks (ruff-check, ruff-format, mypy, import-linter, detect-secrets,
  fixtures-pii-scan, module-size, type-ownership, pytest-unit, etc.).
- The commit itself ran the full local pre-commit suite via the git hook and passed cleanly
  (no --no-verify, no PRE_COMMIT_ALLOW_NO_CONFIG).

## Line count vs budget

herness/core/secrets.py: 360 / 360 lines (module map budget). ENG hard limit 400, so
there is no violation, but zero headroom remains in this file.

## Deviations from the ruling

- Skipped the parked T10-06 minors (r1-m1, r1-m2, r1-m3). Implementing scrub_secrets
  alone (constants, the recursive walker, the processor, and the _KNOWN_VERSION/_remember/
  _reset changes) already consumes the file up to exactly 360 lines even after several
  rounds of compaction (merging the span-masking helper into _scrub_value, inlining the
  pattern-rebuild into scrub_secrets instead of a separate _known_pattern helper, collapsing
  several constant declarations onto shared lines, trimming docstrings to the minimum that
  still documents behavior and cites the unit/threat IDs). There was no remaining room for
  any of the three minors without exceeding the 360 budget, and the ruling explicitly says
  to skip any that don't fit. None of _dotenv_root, _backend, or SecretRef.parse were
  touched; r1-m1/m2/m3 are still open for a follow-up card.

## Concerns

- The file is now at its exact line budget with no slack. Any future small addition to
  herness/core/secrets.py (including r1-m1/m2/m3) will need either a compaction pass first
  or a budget increase in the impl-10 doc's module map.
- _SCRUB_DETECTORS is built once at import time from a default RedactionConfig(). This
  is correct for CREDENTIAL/URL_TOKEN specifically (per U10-32's own wording - it names
  those two detectors, not "the site's configured detectors"), since those two detector
  functions in redact_patterns.build_detectors don't read cfg.id_patterns /
  cfg.custom_patterns / cfg.national_id_patterns at all. If a future change to
  redact_patterns.py ever makes CREDENTIAL or URL_TOKEN config-dependent, this module-load
  snapshot would go stale; flagging it here since scrub_secrets's fixed structlog-processor
  signature has no path to receive a live config.

## Fix round 1 (post-review)

Commit: c138c0d "test(core): cover scrubber fallback, depth and truncation (T10-07)" on
worktree-agent-a8ac72aa070bb1537 (on top of 03a42d2, T10-12's commit). herness/core/secrets.py
was left untouched (still 360/360 lines, byte-identical to aca2a68); only
tests/unit/core/test_secrets.py and .secrets.baseline changed.

Addressed review findings (D:\herness\.superpowers\sdd\program\briefs\T10-07-review.md):

- I1 (Important): `test_ut10_34_scrub_secrets_failure_returns_fixed_event` monkeypatches
  `s._scrub_value` to raise `RuntimeError`, then asserts
  `s.scrub_secrets(...) == {"event": "log.scrub.failed"}` exactly - covers the previously-untested
  TH10-07 fallback branch (secrets.py:154-155).
- I1 (Important): `test_ut10_34_depth_beyond_six_left_unscrubbed` documents and asserts the exact
  depth-6 boundary the review flagged as a caveat. Empirically verified before writing the
  assertions (via a scratch `_scrub_value` walk): a string is masked regardless of the depth
  counter reaching it - only a *container* first encountered at depth > 6 is returned unwalked.
  So a known value wrapped in 6 nested dicts (`_nest(6, VALUE)`) is still masked (the leaf is
  reached as a raw string at depth 7, and the string branch has no depth gate), but wrapped in 7
  dicts (`_nest(7, VALUE)`) the innermost dict is itself handed back unwalked at depth 7 and the
  string inside survives unmasked. Both cases are asserted; the exact mechanism is stated in the
  test's docstring since `herness/core/secrets.py` could not be touched (already at 360/360).
- M1 (Minor): `test_ut10_34_known_value_scrub_is_idempotent` exercises the `***` known-value path
  directly (resolve -> scrub -> scrub again -> same output), closing the gap left by the existing
  idempotency test only covering the CREDENTIAL-detector path.
- M2 (Minor): `test_ut10_35_long_string_truncated_before_scanning` builds a 70,015-character
  string with a distinctive tail marker past the 64 KiB cut, and asserts the scrubbed field is
  exactly `64 * 1024` characters long and the tail marker is gone - confirms truncation happens
  before scanning, not just that the output looks shorter.
- Dict keys: left unscrubbed per the instruction (spec says walk values only) - no change made.

All four new tests reuse UT10-34/UT10-35 per the instruction (test IDs UT10-34 for the fallback,
depth-boundary and known-value-idempotency tests; UT10-35 for the truncation test), each with an
ID-first docstring.

### Gate outputs
- `PYTHONUTF8=1 uv run pytest tests/unit/core/test_secrets.py -q -p no:logging` -> 28 passed.
- `PYTHONUTF8=1 uv run pytest tests/unit/core/test_secrets.py --cov=herness.core.secrets --cov-branch -q -p no:logging`
  -> 98% line/branch (248 stmts/4 miss, 66 branches/1 partial), up from 97%/97%. Remaining misses:
  `172-175` (pre-existing, unrelated `_keyring_call` backend-failure path) and `122->121`
  (the overlapping-span skip branch) - neither was flagged as a review finding requiring a fix.
- `uv run ruff format --check tests/unit/core/test_secrets.py` -> already formatted.
- `uv run ruff check tests/unit/core/test_secrets.py` -> All checks passed.
- `uv run --frozen mypy` (the actual configured gate; `[tool.mypy].files = ["herness", "tools"]`
  does not include `tests/`) -> Success: no issues found in 131 source files. (A direct
  `mypy tests/unit/core/test_secrets.py` single-file invocation flags an unrelated pre-existing
  line via a strict-mode single-file quirk; not part of the real gate and not something this
  round touched.)
- `uv run python -m tools.check_module_size` -> exit 0.
- `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` ->
  3513 passed, 5 skipped, 16 deselected, 1 xfailed (the same pre-existing, unrelated
  TR001/TR002/TR004/TR005 doc-traceability xfail as before).
- `.secrets.baseline` regenerated again; same recurring pattern (drops the two docs/impl
  entries, which were restored verbatim from the pre-regen baseline) - this round's real diff
  is a one-line `generated_at` timestamp change only (no new entries for the new test strings).
- `uv run pre-commit run --all-files` -> all 17 hooks passed. The commit itself ran the real
  hooks (no `--no-verify`, no `PRE_COMMIT_ALLOW_NO_CONFIG`).

### Deviations / notes
- herness/core/secrets.py was not modified, as instructed; it remains at exactly 360/360 lines.
- The `122->121` overlapping-span-skip branch and the pre-existing `_keyring_call` backend-failure
  lines remain uncovered; both are outside this round's scope (not flagged as findings) and are
  left for a future round if ever prioritized.
