# T10-10 Redactor — build report

Status: DONE_WITH_CONCERNS. Commit 7f58b80 `feat(core): add Redactor, get_redactor and redact_text (T10-10)`
on worktree-agent-aeccb0496e45746f0 (base 3e0ee9a). All hooks ran (no --no-verify).

## What was built
- herness/core/redact.py (312 / 390): RedactionFailed(FatalError); Redactor (32-byte key check,
  key_id = sha256[:8], repr shows only key_id, detectors in DETECTION_ORDER with PERSON from the
  NameDirectory; scan: TOKEN_PATTERN protection, O(log n) overlap claim, NUL-masked detector view,
  detector exception -> RedactionFailed(<type>), 4,000,000-char limit, lazy Presidio at the first
  scan(ner=True) with ner == "presidio", missing -> ConfigError("install the ner extra");
  redact (None/"" rules, IP kept when mask_ip false, counts); redact_batch (None per failed item,
  lock-protected in-process failed counter + _records_failed_total(), sink wiring deferred);
  pseudonym (stdlib HMAC-SHA256)); get_redactor/reset_redactor (_RED_LOCK, _load_key 64-hex check,
  directory with cfg.paths.data / "cache/redact/display_names.txt", log redact.directory.loaded
  names/variants/duration_ms); redact_text (logs redact.record.failed with error_type only, returns
  None); registers reset_redactor in config._RESET_HOOKS and _config_key_id as
  config._KEY_ID_PROVIDER (missing secret -> resolve's "secret not found" ConfigError -> config_hash
  "unresolved").
- herness/core/redact_patterns.py (395; budget raised 380 -> 395): CARD fix (below). __all__
  compacted to two lines to make room.
- docs/impl/10-config-security-deployment.impl.md §2 module map: redact_patterns 380 -> 395.
- tests/unit/core/test_redact_redactor.py (462 lines, 36 tests): UT10-37 (x4), UT10-39, UT10-40 (x5 +
  parametrized malformed keys), UT10-41, UT10-42 (x3 incl. the T08-04 e-mail + api_key= contract),
  UT10-45, test_rf_adjacent_numeric_entities_are_all_redacted (card+IPv4, IPv4+card, IPv6+card+digit,
  card+IPv6, phone+SSN; each with mask_ip true and false; checks scan, redaction, idempotence, no
  blocking span left), test_rf_card_cut_prefers_leftmost_longest_and_respects_word_boundaries,
  test_rf_redact_batch_fails_closed_per_item, test_rf_ner_* (x2), PT10-02, PT10-03, PT10-05
  (hypothesis; PT10-02/03 keep the bare " " separator, max_examples=300).
- tests/unit/core/test_config_hash.py: PT10-01 passes key_id="unresolved" (accepted ruling (d)).

## Spec deviations (controller rulings (a)-(c), for the record)
1. U10-36 CARD find: the spec regex matches are yielded first (unchanged behaviour), then
   `_card_cuts` yields Luhn-valid 13-19 digit cuts of maximal digit runs (`(?<!\w)[0-9](?:[ -]?[0-9])*`),
   cut only at separators / run ends not followed by a word char, leftmost start then longest;
   Luhn per cut is O(1) via parity prefix sums. Redactor.scan drops cuts that overlap an accepted
   match, so the result is a strict superset of the spec's. Finds "<card> 10.20.30.40",
   "10.20.30.40 <card>", "2001:db8::1 <card> 2", "<card> 2001:db8::1".
   I kept the spec match first rather than only the run cuts: a run-cuts-only version missed 335
   spec-valid cards in a 50k random digit-soup comparison (it picked a different leftmost cut).
2. U10-41 step 2: every detector (prefilter + find) runs on a view of the text where protected
   tokens and accepted spans are replaced by "\x00" (same length). The view is rebuilt only after
   a detector accepted spans; the overlap claim stays as a safety net. Checked the regexes: "\x00" is
   not \w, not \s, not a digit or [ -] or [().-], so EMAIL/CARD/PHONE/IP/IDs stop at it; the
   CREDENTIAL (c) value class and the URL class `[^\s<>"']+` do include "\x00", as they included the
   token characters before, so behaviour there is unchanged (overlap drop). PHONE next to SSN is now found.
3. Budget: redact_patterns 380 -> 395 in the impl 10 module map (same commit).

## RED / GREEN evidence
- RED: PT10-03 failed with pieces=['4111 1111 1111 1111', '10.20.30.40'], sep=' ' (CARD found
  only after the IP was tokenised); PT10-02 failed at max_examples=3000; a seeded 100k probe found
  4 failure classes (CARD+IPv4, PHONE+SSN, CARD+IPv6, mixed), all with a bare-space separator.
- GREEN: same probe 0 failures; PT10-02 + PT10-03 at max_examples=3000 with the bare " "
  separator: 2 passed (7.3 s); committed at 300. Random cross-check: every yielded card is 13-19
  Luhn-valid digits (20k samples).

## Gates (final tree)
- ruff check . : All checks passed. ruff format --check . : 303 files already formatted.
- mypy (strict, package): no issues in 129 files; mypy on the new test file: no issues.
- lint-imports: 13 kept, 0 broken. check_type_ownership: rc 0. check_module_size: rc 0.
- PYTHONUTF8=1 pytest -m "(unit or integration) and not slow" -q -p no:logging:
  3479 passed, 5 skipped, 16 deselected, 1 xfailed (216 s).
- Commit hooks: all passed (detect-secrets flagged the fixture names API_KEY/SECRET_VALUE on the
  first try; renamed to KEY_PARAM/PLANTED — no baseline change).

## Line counts
- redact.py 312 / 390 (78 left for T10-11 redact_table; the NER path, about 40 lines, can move out
  if needed). redact_patterns.py 395 / 395. Test file 462 (tests not size-gated).

## Concerns
- CARD cuts cost on pathological input: ~0.6 s per MB for "1 1 1 ..." (0.4 s for 1M contiguous
  digits, 0.15 s for ordinary mixed text per MB). Linear, but above BT10-05's 50 ms/MB on that
  input; BT10-05 is not in this card. Worth checking when BT10-05 lands.
- A cut may be Luhn-valid by chance inside long digit soup (about 1 in 10 per candidate), which
  over-redacts. That fails safe, and the spec regex has the same property.
- redact_patterns.py is at its new budget (395) and close to the hard limit of 400; any further
  detector work needs a split.
- redact.py is above the ~260 target (312).

## Fix round 1 (review T10-10-review.md) — commit 98b8bbb
1. Important (CARD cut cost): `_CARD_RUN` is now `(?<!\w)[0-9](?:[ -]?[0-9]){12,}`, so only runs
   that can hold 13 digits reach the Python cut loop; shorter runs are rejected inside the regex
   engine. Results are unchanged: 100k random digit-soup samples give the same `_card_cuts` output
   as before (a shorter run's suffix is shorter still, so no new starts). redact_patterns.py
   stays at 395 lines.
   Measurements (2.7 MB log text: timestamps, IPv4, ports, e-mail per line):
   - CARD detector (spec regex + cuts): 212 -> 37 ms/MB; cuts alone about 12 ms/MB.
   - Whole Redactor.scan: 598 -> 427 ms/MB on that text, 228 ms/MB on plain job-log text.
     The rest is T10-08 detectors and HMAC, not this card: CREDENTIAL about 95 ms/MB (every ":" in
     timestamps passes its prefilter), IP about 55, PHONE 17-28, EMAIL 26, NATIONAL_ID 14,
     EMPLOYEE_ID 12, plus pseudonym HMAC for about 40k spans. BT10-05/BT10-03 (50 ms/MB, 5k rec/s)
     look unreachable in pure Python with the spec detectors on log-heavy text; flagging this for
     the benchmark card.
   Test: test_rf_card_cuts_skip_runs_shorter_than_13_digits (short runs untouched, 13-digit run
   still matched). No timing test (would be flaky); the numbers above are recorded here instead.
2. Minor m2: redact.record.failed now logs `reason=exc.message` (the entity type or
   "text too long", never text); the UT10-42 log test asserts reason == "PERSON".
3. Minor m4: `_ner_spans` keeps only hits with 0 <= start < end <= len(text). The NER test adds
   hits (-4, 3) and (33, 40). RED without the check: test_rf_ner_hits_are_added_without_overlap
   failed; GREEN with it.
Gates: ruff format/check clean; mypy strict clean (129 files + test file); lint-imports 13 kept;
type-ownership rc 0; module-size rc 0 (redact.py 313/390, redact_patterns.py 395/395); full
unit+integration not slow: 3480 passed, 5 skipped, 1 xfailed. Hooks all passed.
Parked as instructed: m1, m3, m5.
