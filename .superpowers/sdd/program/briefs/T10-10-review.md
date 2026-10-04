# T10-10 Redactor: verify review (head 7f58b80, base 3e0ee9a)

### Spec Compliance
- ✅ U10-40 Redactor constructor: 32-byte check with the exact message; key_id = sha256[:8]; detectors built in DETECTION_ORDER with PERSON from the directory; `repr` shows only key_id; lazy Presidio import, `ConfigError("install the ner extra")`.
- ✅ U10-41 scan: 4,000,000-char limit, TOKEN_PATTERN protection, overlap claim, detector exception wrapped as `RedactionFailed(<type>)` (replacement/pseudonym also inside the try), IP always reported, NER on masked text, sorted output. NUL-masked detector view (ruling b) implemented as ruled; overlap check kept.
- ✅ U10-42 redact: None -> None, "" -> `RedactionResult("", {})`, IP dropped only at replacement when `mask_ip` false, counts per type, no raw text on error (exception propagates).
- ✅ U10-43 redact_batch: per-item fail-closed, lock-protected in-process counter (ruling). UT10-43 belongs to T10-11.
- ✅ U10-44 pseudonym: stdlib HMAC-SHA256, `[TYPE_<10 hex>]`.
- ✅ U10-45 get_redactor/reset_redactor/_load_key: `_RED_LOCK` lazy init, 64-hex fullmatch then `bytes.fromhex`, directory with `cfg.paths.data / "cache/redact/display_names.txt"`, `redact.directory.loaded` (names/variants/duration_ms, no key); reset hook appended to `config._RESET_HOOKS`.
- ✅ U10-46 redact_text: None -> None; `RedactionFailed` -> WARNING `redact.record.failed` with `error_type` only, returns None; only `ConfigError` escapes.
- ✅ U10-48 RedactionFailed(FatalError); messages are the entity type or `text too long`, never text (checked every raise site: redact.py scan, _detect, _ner_spans).
- ✅ `_KEY_ID_PROVIDER` registered; missing secret -> resolve's `secret not found:` ConfigError -> `config_hash` falls back to `"unresolved"` (config.py:284-291); malformed key still raises (U10-11 behaviour, not this card).
- ✅ Ruling (a) CARD: spec matches first, then Luhn-valid cuts at separators; Luhn parity prefix sums verified correct (digit k doubled iff k%2 != last%2); inner loop bounded to 7 lengths -> linear.
- ✅ Budgets: redact.py 312/390, redact_patterns.py 395/395 (spec §2 updated in the same commit). PT10-01 passes `key_id="unresolved"` (ruling).
- ✅ Tests: every brief ID present (UT10-37, 39, 40, 41, 42, 45, PT10-02, 03, 05); names carry IDs, docstrings start with ID/RF, `pytestmark = pytest.mark.unit`. Focused run: 52 passed (redactor + config_hash); coverage redact.py 98 % line / branch partials 2, redact_patterns.py 100 %.
- ⚠️ Cannot verify from diff / cross-card:
  - Nothing in the tree imports `herness.core.redact` before config load (grep: only `herness/enrich/questions.py` mentions it in a docstring). Until an entry point imports it, `config_hash` silently uses `"unresolved"`, and the hash of the same config differs by import order. Integration (CLI bootstrap / T09) must import the module; worth a controller note.
  - T08-04 downstream: `redact_text` removes the e-mail and `api_key=` value on a 500-char cut (UT10-42 contract test + probe with the pair at the cut edge: `...from [EMAIL_55ce64bace]: api_key`). But impl 08 cuts before redacting (spec line 578), so an e-mail split by the cut (`ops@examp`) is no longer an EMAIL match and survives; and impl 08 step 6 `redact_text(str(err))[:500]` raises TypeError when redact_text fails closed (None). Both belong to T08-04, not this card.
  - BT10-05 / BT10-03 not in this card (see Important 1).

### Strengths
- Fail-closed paths are tight: pseudonym/normalize failures inside `_detect` become `RedactionFailed(type)`; tests assert the message is exactly the type and no text leaks into logs.
- `_Taken` bisect claim is correct for half-open ranges; masked view preserves positions, so every detector's offsets stay valid.
- Adjacent-entity RF tests (card+IPv4/IPv6, phone+SSN, both mask_ip settings) check scan, redaction, idempotence and no residual blocking span; PT10-02/03 keep the bare-space separator that found the original bug.
- Key never logged; repr/key_id tests; malformed-key parametrisation includes whitespace-padded and 63-char values.

### Issues

#### Critical (Must Fix)
- none

#### Important (Should Fix)
1. herness/core/redact_patterns.py:255-257 — `_card_cuts` does Python-level work (digit list, cut set, two prefix-sum lists) for every digit run, even runs that cannot hold 13 digits. On ordinary log text (timestamps, IPs, ports, pids) this costs ~0.28 s/MB (probe: 1.14 MB syslog-like text, 0.316 s in `_card_cuts` alone) and ~0.7 s/MB on e-mail+IP text — 6-14x the whole-scan BT10-05 budget (50 ms/MB) on normal, not pathological, input, and it will also sink BT10-03 (5,000 records/s at 1.5 KB). The controller ruling accepted the cost only for the pathological `1 1 1 ...` input. Fix: skip a run early when `run.end() - run.start() < 13` (or its digit count < 13); probe shows the filter cuts 160,001 runs to 20,000 and the pass to ~30 ms/MB, with identical output (a cut needs >= 13 digits).

#### Minor (Nice to Have)
1. herness/core/redact_directory.py:100 (T10-09, not in this diff) — `_resolve_overlaps` is O(k^2) in candidates; scan of 1.7 MB text with 80k name hits took 103 s (40k hits: 25 s). This is the only quadratic cost found under `scan` (T10-10's `_Taken.insert` is ~0.3 s for 160k interleaved spans). Route to a T10-09 follow-up before BT10-05; not a T10-10 defect.
2. herness/core/redact.py:306 — `redact.record.failed` logs only `error_type` (always `RedactionFailed`); the message (entity type or `text too long`) is safe per U10-48 and would make the event actionable.
3. herness/core/redact_patterns.py:73 with herness/core/redact.py:82-88 — a CREDENTIAL/URL value that touches an existing token (`password=[SECRET]abc`, `token=abc[EMAIL_…]def`) is dropped whole, leaving `abc`/`def` in clear. Same as pre-mask behaviour and spec-conformant (never cover a token), and only reachable with literal token text in input; noting because the NUL view would allow stopping the value class at `\x00`.
4. herness/core/redact.py:205-206 — `_ner_spans` does not bound Presidio offsets to `len(text)`; a malformed hit beyond the end would be claimed. Presidio is trusted, so low risk.
5. herness/core/redact.py:127 — `key_id` is a plain mutable slot while the unit states "immutable"; not enforced (a property would enforce it). Cosmetic.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Spec compliance, fail-closed behaviour, key handling, hook registration, NUL masking and idempotence are all correct and well tested; one Important performance defect remains — the CARD run-cut pass costs 0.3-0.7 s/MB on ordinary log text, far beyond BT10-05/BT10-03, fixable with a one-line length guard.

## Re-review r1 (head 98b8bbb, fix commit only)

Scope: Important 1, m2, m4, and regressions in the touched lines. m1, m3, m5 are parked by the controller; whole-scan BT10-05 cost in the T10-08 detectors is a spec note, not this card.

- ✅ Important 1 (CARD cut cost), herness/core/redact_patterns.py:108: `_CARD_RUN` is now `(?<!\w)[0-9](?:[ -]?[0-9]){12,}`, so runs shorter than 13 digits are rejected inside the regex engine. The runs are the same: the greedy repeat still reaches the end of each maximal run, and a later start inside a short run is either blocked by the lookbehind or shorter still, so the run-end word check and the cut logic see identical runs.
  - Equivalence, compared against the 7f58b80 `_card_cuts` loaded from `git show`: 100k random short texts (0 differences, 259 with cuts) and 100k digit-heavy texts (0 differences, 7,983 with cuts).
  - Cost on the same syslog-like probe: cuts 275 -> ~93 ms/MB, CARD detector 320 -> ~140 ms/MB. The remaining cut cost is real 13+ digit candidates: in that probe every line joins `pid N` with the next timestamp into a 13+ digit run. On text without such runs it is the regex cost only (~17 ms/MB). This is no longer the short-run waste the finding was about. Resolved; the remaining full-scan cost goes to the BT10-05 note.
- ✅ m2, herness/core/redact.py:307-308: `redact.record.failed` now adds `reason=exc.message`. Every `RedactionFailed` raise site passes the entity type or `text too long` (the U10-48 contract), never text. The UT10-42 test asserts `reason == "PERSON"` and still asserts no `jane` appears on stderr.
- ✅ m4, herness/core/redact.py:210: hits are kept only when `0 <= start < end <= len(text)`. The NER test adds (-4, 3) and (33, 40) and still expects exactly the three in-range spans.
- ✅ Regressions: the new test `test_rf_card_cuts_skip_runs_shorter_than_13_digits` follows the ID and docstring rules. The redactor and patterns tests pass (164 passed). Line budgets hold (redact.py 313/390, redact_patterns.py 395/395).
- No new findings.

**Task quality:** Approved
**Reasoning:** All three fixes are correct and tested. The CARD change gives the same output as before in randomized comparison and removes the per-short-run Python cost.
