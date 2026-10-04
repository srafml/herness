# T11-07 review: Text templates and PII injection (commit bf8e7f4)

### Spec Compliance
- ✅ Spec compliant (with Minor notes below)

U11-05 (tools/synth/text.py + text_vocab.py)
- ✅ Signature: `TemplateBank()` with no args (all fields `init=False`); the three render functions have keyword-only args as specified; `RenderedText` has all 7 fields with `language: Literal["en","es"]`.
- ✅ `ROOT_CAUSE_OPTIONS` is the exact 9-tuple in spec order (text_vocab.py:15; asserted in the UT11-06 test).
- ✅ Invariants: 60 symptoms, 25 causes each mapped to one option (all 9 options covered), 40 actions, 30 families with 3..6 patterns (`3 + index % 4`, text.py:65), plus 2 EN and 2 ES change patterns per family. `tpl_conn_pool` maps to capacity and `tpl_cert_expiry` to access_identity. The bank is immutable (frozen dataclass, MappingProxyType) and the test checks this.
- ✅ Algorithm 1-7: family is given or uniform; slots are reused with a 0.20 re-draw (`TextParams.slot_variation`) and missing slots are drawn fresh; patterns are drawn uniformly, from the change pool when change_flavored; the repeat phrase is appended; impact phrases have 4 levels; Spanish is chosen at `spanish_share`; typos are adjacent swaps; casing noise is upper/lower of short_description at 0.05; caps are 160 and 3,000.
- ✅ Errors: unknown family raises `SynthUsageError` (`TemplateBank.family`). impact_level outside 0..3 raises too (enforces the precondition).
- ✅ Security: every string is built in and invented; no LLM (TH11-01).
- ✅ Truth labels stay detectable. The change, repeat and impact words are protected from typos (text.py:154-164). `CHANGE_MARKERS` and `REPEAT_MARKERS` include the ES forms. The test checks that the marker appears exactly when the flag is set, over 1,000 and 400 renders, including when noise is at 1.0. close_notes carries the cause phrase.

U11-06 (tools/synth/pii.py)
- ✅ Signatures and the frozen `PiiSpan(field, start, end, type)` match the spec. `build_name_list` draws 500 pairs without replacement from a 100x100 pool via `stream_rng(seed, "names")`.
- ✅ Reserved ranges only:
  - emails use example.com/.org;
  - phones use the 10-digit `+1-202-555-01NN` in the 3 required formats (R-56), which match the U10-49 allow rule `+120255501NN`;
  - IPs are 192.0.2.1-254 and 198.51.100.1-254;
  - `E` + 6 digits;
  - cards are 16-digit, Luhn-valid and start with 4111;
  - `password=synthetic` + 8 alnum;
  - `https://portal.example.com/x?token=synthetic` + 16 hex (DD11-18/R-67);
  - PERSON uses the 3 formats.
- ✅ Algorithm: types are chosen uniformly with replacement; one of the 3 phrases is used; insertion is at a uniformly chosen sentence boundary that is never inside a span, and spans are shifted; with p 0.5 an INC/CHG + 7-digit number is placed right after a span, outside every span.
- ✅ Errors: n_spans outside 1..3 raises `SynthUsageError`. Empty names also raises (extra, reasonable).
- ✅ Determinism: everything is driven by the passed Generator. The seeded-equality test covers text; the name list is stable per seed.

Tests
- ✅ UT11-06, UT11-07, UT11-08 and PT11-02 are present. Names carry `_ut11_0N_`/`_pt11_02_`, each docstring starts with its ID, and each module sets `pytestmark = pytest.mark.unit`. I re-ran the 2 files: 15 passed in 0.64 s.

Ruling evaluations
- Provisional ruling (`text: TextParams = TextParams()` kwarg on render_incident_text): JUSTIFIED; recommend recording it as a spec delta.
  - U11-05 step 5 cites `params.text.spanish_share`, but the fixed signature has no params, and `TemplateBank()` is fixed to take no args, so the bank cannot carry params either.
  - The only caller with params is U11-07 `gen_incidents(cat, params: SynthParams, ...)`. It holds `params.text` but has no spec path to hand it over.
  - Without the kwarg, `SynthParams.text` (typo_rate, casing_noise_rate, spanish_share, slot_variation; param_groups.py:219) would be dead config.
  - The kwarg is keyword-only and its default equals the spec values, so every spec-shaped call works unchanged (for example the pii-corpus unit, spec line 537).
  - Follow-up: the T11 card that builds gen_incidents must pass `text=params.text`.
- Builder open choices (report concern 3), checked against the spec text:
  - `theme` as a family name: acceptable. The spec never defines theme. The T2 plant (impl line 310) needs an "epic summary about connection pooling", and `theme="tpl_conn_pool"` yields "Reduce connection pool exhaustion in ...". Unknown theme raising SynthUsageError matches the U11-05 error style.
  - issue_type set {initiative, epic, feature, story, bug, task}: matches the U11-08 type mix (impl line 262). The lowercase-only matching is covered under Minor 3.
  - render_change_text: planned changes get family `chg_planned` with root_cause `unknown`, and emergency changes take a drawn family's cause. The spec has no change truth labels, so this is harmless. Note that `chg_planned` is not a bank family.
  - Noise and Spanish only on incidents: consistent with design §5.1.4 ("5 % Spanish templates" sits in the incident/cluster paragraph). Acceptable.
  - impact_level precondition enforced: acceptable.

- ⚠️ Cannot verify from diff: ST11-01 (the fixture scan over generated output) belongs to a later card. The report's gate claims (ruff, mypy, lint-imports, module size, full unit suite 686 passed) are taken as stated.

### Strengths
- The span arithmetic is careful. Boundaries exclude span interiors, only spans at or after the insertion point shift, and the ticket goes at `anchor.end`, which no other span can start at. A Hypothesis check (200 examples) confirms spans never overlap.
- Typo protection of the marker, repeat and impact words keeps the slot-derived truth labels detectable under noise, and a test at noise 1.0 checks this.
- The vocab is split into text_vocab.py; text.py is 291 lines and pii.py 223, both under budget. The tests cover the reuse rate (0.75-0.88), casing noise, family-to-cause mapping, caps with long components, and error paths.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. tools/synth/text.py:159: the effective typo rate is below the 1 % in design §5.1.4 and U11-05 step 6. Words shorter than 4 characters (`_MIN_TYPO_WORD`, text.py:43) and protected words are skipped. A swap can also hit two equal letters ("pool") or move punctuation. short_description never gets typos. Skipping protected words is justified; the short-word floor is not in the spec. Nothing tests the rate.
2. tools/synth/text.py:200-201, 213-214: the caps work by plain truncation. The repeat and impact phrases sit at the end of description, so a component longer than about 2,900 characters would cut them off and break the `repeat_issue` and `business_impact` labels. Real components are service names, so this is edge-only. Truncating the component, or asserting the phrases survive, would close it.
3. tools/synth/text.py:257: `issue_type` matching is lowercase-only, while the Jira JSON carries `issuetype.name` capitalized ("Epic", impl lines 261/310). A caller passing the display name gets SynthUsageError. Either normalize case or document the lowercase contract for the Jira generator card.
4. tools/synth/pii.py:23: `STREAM_NAMES` is defined in pii.py while every other stream constant lives in tools/synth/rng.py:16-20. The spec's U11-02 constant list omits "names", so this is defensible, but it splits ownership of the streams.
5. tools/synth/pii.py:38-53: a few syllable compounds match real given names or surnames ("Karan", "Selin", "Bramley"). This is harmless for TH11-01, since bare names are not linked to anything, but the spec says "invented names". Optional: swap those syllables.
6. tests/unit/tools/synth/test_synth_pii.py:100: PT11-02 draws ASCII 32..126 only. "Printable" in the spec arguably includes Unicode and whitespace such as \n. The code handles these via `isspace`, but the property is not exercised.
7. tests/unit/tools/synth/test_synth_text.py:178: `test_ut11_07_change_and_jira_texts` files change and Jira coverage under UT11-07, whose row covers only families and Spanish share. This is harmless extra coverage, but a traceability script would count it against UT11-07.

### Assessment
**Task quality:** Approved
**Reasoning:** All U11-05/U11-06 fields are implemented as specified: rates, vocab sizes, reserved PII ranges, errors and determinism. UT11-06..08 and PT11-02 pass with correct IDs and markers, and the truth labels stay detectable under noise. The TextParams kwarg is justified and should be recorded as a spec delta. The remaining items are Minor.
