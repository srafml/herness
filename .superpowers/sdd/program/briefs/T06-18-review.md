# T06-18 review (verify): Hybrid pack and pseudonyms

Worktree agent-a2fdb96db08def1a5, base 02bd94a, head 58517ea. Reviewer ran: card tests (19 passed; hybrid.py 100 % line / 100 % branch), ruff check + format --check clean, mypy --strict clean on hybrid.py and the test file, a mutation run (pseudonymize -> identity: 7 tests fail incl. both PT06-06 and the UT06-83 leakage tests), and two adversarial probes (scratchpad T06-18-verify/probe1.py, probe2.py).

### Spec Compliance
- ✅ U06-124 Pseudonymizer: tokens f"{entity_type}_{n:03d}" per type in input order (duplicates ignored); one longest-first alternation with (?<!\w)/(?!\w) whole-token guards, regex metachars escaped (probe: "A+B (x)", "t.1", "ENG-1" all correct; "ENG-12", "xENG-1", "t.1x" left alone); restore -> name (id when unnamed); restore_obj switches to id mode under target_id/entity_id/row_key, walks dicts/lists/tuples; mapping() fits SwarmTaskState.pseudonyms. token() unknown -> NotFound with entity_type only.
- ✅ U06-123 build_evidence_pack: items exactly per step 1 (objective, finding_id, entity_type, entity_id token, claim, numbers with row_key scrubbed, confidence, challenge verdict + noted checks, portfolio scenario/rows/selected/query_ids, levers, DQ check names, unconfirmed weights); only those inp keys are read; spec.inputs never read, so result_sample / ticket text / text_redacted / prior_context / inputs.notes at their spec locations are absent. Keyword-only cfg per group ruling; count_tokens(cfg, [user TextPart(canonical pack)], [], [])[0] is the only estimator; measured on the final pseudonymized+redacted pack; drops the last finding while > max_tokens - 8000. Every outbound string = redact_text(pseudonymize(s)), fail closed (finding dropped / item dropped / objective raises RedactionFailed without text). No LLM/HTTP client (AST test). mapping never packed or logged (drop log = finding_id + error type).
- ✅ UT06-83 (12 fns): samples/notes absent, ids pseudonymized, size trimmed, count_tokens args, fail-closed cases. Matches row.
- ✅ UT06-84 (4 fns): overlapping names, longest-first, stable tokens, restore rules. Matches row.
- ✅ PT06-06 (2 hypothesis fns): round trip on the domain where U06-124 step 3 allows it + id-field round trip + leakage assertion (no raw id/name survives as a whole token). Verified by mutation that removing pseudonymization fails both.
- ✅ Test naming/docstrings/pytestmark per global constraints; swarm/__init__.py docstring-only (ruling); hybrid.py 259/260; build_evidence_pack 6 args; complexity within C901; layering (L4 imports core + harness.llm only).
- ⚠️ Cannot verify from diff / spec owner:
  - Over-cap with no findings left raises BudgetExceeded (spec silent). Acceptable (never returns an over-cap pack), but the U06-115 / Skeptic routing cards must map it to the local fallback (hybrid_fallback banner), not to the run-budget-exhausted path. Flag for the T06 writer card.
  - Pack omits outline, open_concerns, contested, question (built strictly to U06-123 step 1); the off-network Writer may need outline/open_concerns (U06-115 step 3). Spec gap, not a build defect; hybrid.py has 1 line of budget left.
  - With an anthropic tokenizer the trim loop does one count_tokens (network) call per dropped finding.

### Strengths
- Single-pass regex means tokens are never re-replaced; escaping makes metachar names safe.
- Redaction applied after pseudonymization, and the cut measured after redaction (per ruling), with a spy test proving the redactor never sees raw names.
- Tests use a real Redactor; leakage is asserted, not just round trip.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. Case variants of names/ids pass through raw. herness/harness/swarm/hybrid.py:43,64,79. Probe: entity ("team","core","Core") -> "core Core CORE" pseudonymizes to "team_003 team_003 CORE"; "payments platform" / "PAYMENTS PLATFORM" in a lever string reach the pack verbatim. The spec says "whole-token", not "case-sensitive"; a case variant of a team/service name is still the name, and TH06-06 is "hybrid call leaks ... names". The shared redactor only catches directory persons and secrets, not team or service names. Fix: key _forward by s.casefold() (first wins), compile the forward regex with re.IGNORECASE, look up self._forward[m.group(0).casefold()]; keep restore case-sensitive (tokens are generated). Add a UT06-84 case (lower/upper name -> token) and keep PT06-06 filler from forming a case variant of a name (or assert restore gives the canonical-case text).
2. Mapping keys are never scrubbed. herness/harness/swarm/hybrid.py:139 ({str(k): ...}). Probe: levers row {"by_team": {"team-core": 3, "team-payments": 4}} and NumberRef row_key {"team-core": "x"} leave raw ids in the pack. U06-123 requires "portfolio and lever rows with ids pseudonymized"; this is a fail-open path at the privacy boundary. Fix: {_clean(pseudo, str(k)): _scrub(pseudo, v) ...} (same line count); add a UT06-83 assertion with an id-keyed nested dict.

#### Minor (Nice to Have)
3. Non-string ids pass raw. hybrid.py:134-142. An int 10042 whose catalog id is "10042" (probe: lever entity_id 10042, row_key wid 10042) is not pseudonymized. Catalog ids are TEXT, so low likelihood; fix: in _scrub, map an int whose str() is a known raw id to its token (or stringify ints under _ID_KEYS / *_id keys first).
4. Rows are passed through generically, so never-included keys planted inside a lever/portfolio row (result_sample, text_redacted, notes, prior_context) are packed (redacted). hybrid.py:145-153,194-200. Spec lever rows are fixed SQL columns (entity_id, metric, target_kind, current_value, target_value, delta_usd, query_ids). Fix: drop those key names in _scrub for mappings, or allowlist lever columns.
5. Token-shaped literals: text that already contains e.g. "team_003" (not an entity) is rewritten by restore to an entity name (builder spec note 5; token-shaped labels like team_010 exist in other fixtures). hybrid.py:99-107. Fix option: reject/escape token-shaped raw strings in pseudonymize, or document at the caller.
6. Partial overlap at an earlier offset leaves a fragment: names "Alpha Beta" and "Beta Gamma", text "Alpha Beta Gamma" -> "team_004 Gamma". Inherent to leftmost matching; a comment is enough.
7. Trim loop cost: one count per dropped finding (hybrid.py:254-258); with an exact (network) tokenizer consider estimating the drop count first or bisecting, keeping count_tokens as the final check.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Structure, fail-closed redaction, budget cut and tests are solid and meet the card, but two cheap fixes are needed at the privacy boundary: case-variant names leak and dict keys are never pseudonymized (items 1-2); minors 3-4 are one-liners worth folding in.

## Re-review 1 (head 3e5462d; scope I1, I2, m3, m4, m6; m5/m7 parked)

Reviewer ran: card tests 21 passed, hybrid.py 100 % line / 100 % branch (132 stmts, 34 branches); ruff check + format --check clean; mypy --strict clean (hybrid.py + test file); hybrid.py 260/260; mutation (pseudonymize -> identity) now fails 9 tests including both PT06-06 functions, the new case-variant UT06-84 and the new key/int/never-key UT06-83 test. Probes probe1/probe2 re-run, new probes probe3-probe6 (scratchpad T06-18-verify).

### Spec Compliance
- ✅ I1 fixed: "core Core CORE cOrE" -> team_001 x4; "payments platform" / "PAYMENTS PLATFORM" -> team_002; lever string in probe2 now "team_001 and team_001 and team_002". Restore stays case-sensitive ("TEAM_001" untouched, tokens restore to the canonical name). First entity wins across case variants (tested).
- ✅ I2 fixed: keys go through _clean; probe2 by_team {"team-core", "team-payments"} -> {"team_002", "team_001"}, row_key {"team-core": ...} -> {"team_002": ...}.
- ✅ m3 fixed: int values under *_id / row_key subtrees that equal a known raw id become the token (entity_id 10042 -> work_item_001, row_key wid 10042 -> work_item_001); other ints (score, week) and bools kept.
- ✅ m4 fixed: result_sample / text_redacted / notes / prior_context / ticket_text dropped at any depth in levers, portfolio rows and selected (probe2 and new test).
- ✅ m6: comment on leftmost-match overlap added (hybrid.py:43).
- ✅ PT06-06 still asserts leakage (now case-insensitive whole-token search) plus pseudonymize(upper).lower() == pseudonymize(lower); generator keeps names unique ignoring case and filler letters disjoint from name letters, so the round-trip property stays valid.
- ✅ No regression in restore / restore_obj (UT06-84 restore-rules test unchanged and passing; probe1 restore_obj output identical).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
R1-1. Performance regression from the named-group alternation. hybrid.py:43-50 (_replacer: `(?P<g{i}>...)` per key). One named group per key disables the regex engine's alternation prefix optimisation. Timing for a 2.2 KB string (probe5/probe6): 300 entities 0.001 s (base) -> 0.017 s (now); 5,000 entities 0.008 s -> 2.26 s (~280x). The isolated cause is the named groups, not IGNORECASE: plain 0.008 s, plain+IGNORECASE 0.022 s, named 4.2 s, named+IGNORECASE 2.5 s (10,000 keys). The same replacer is used for _to_ids/_to_names, so restore of the Writer output pays it too. Every pack string goes through pseudonymize, so a funding_review pack with ~1-2k catalog entities and ~90 KB of text takes tens of seconds, and the cost grows with catalog size × text (LLM10). Fix, within budget: keep a plain alternation (no groups). Forward: compile with re.IGNORECASE and look up `lowered[m.group(0).lower()]`, failing closed on a miss from a Unicode case-fold mismatch (for example `lowered.get(g.lower(), "[ENTITY]")`, never the raw text). Restore (flags=0): look up `table[m.group(0)]` exactly. Add one UT06-84 timing-free guard if wanted (for example 2,000 entities pseudonymize a 10 KB string within a generous bound, or just assert the compiled pattern has no groups: `pattern.groups == 0`).

#### Minor (Nice to Have)
R1-2. Key collision after pseudonymization silently drops a value: {"core": 1, "Core": 2} -> {"team_001": 2} (hybrid.py:135-136). Nothing leaks, but data is lost without a signal. Low likelihood (row keys are column names). Option: keep the first value, or suffix duplicates.
R1-3. The never-packed key check is case-sensitive: "Result_Sample" is kept (hybrid.py:135, `k not in _NEVER`). Option: `str(k).lower() not in _NEVER`. Low likelihood (SQL column names are lower case).
R1-4. A bare "id" key is not id-valued for m3 (`_IDK = ("_id", "row_key")`): {"id": 10042} keeps the int. Low likelihood; option: add "id" to the tuple as an exact match.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The fixes for I1, I2, m3, m4 and m6 are correct and tested, and the leak probes are clean. But the named-group replacer makes pseudonymize and restore up to ~280x slower as the entity count grows (R1-1). The fix is small and within budget: a plain alternation with a dictionary lookup that fails closed.

## Re-review 2 (head ed359f0; scope R1-1, R1-3, R1-4; R1-2 parked)

Reviewer ran:
- Card tests: 23 passed. hybrid.py has 100 % line and branch coverage (130 stmts, 36 branches).
- ruff check and format --check: clean. mypy --strict (hybrid.py and the test file): clean. hybrid.py is at 260/260.
- Mutation (pseudonymize returns its input): 10 tests fail, including both PT06-06 functions.
- Probes: probe2 and probe4 re-run; new probe7 covers non-ASCII case folding and the 5k-entity timing (scratchpad T06-18-verify).

### Spec Compliance
- ✅ R1-1 fixed. The alternation has no groups. With 5,000 entities on a 2.2 KB string, pseudonymize takes 0.0076 s (round 1: 2.26 s) and restore takes under 1 ms. The guard test checks `.groups == 0`.
- ✅ Restore is exact and case-sensitive: "team_006 TEAM_006 team_0061" restores to "Core TEAM_006 team_0061".
- ✅ A miss after a case-fold match never sends raw text. "Kaſse" (long s) and the Kelvin sign both match: the Kelvin form gets its token, the long-s form gets "[ENTITY]".
- ✅ Case variants pseudonymize and restore to the canonical form for:
  - Greek (Ωmega/ωmega/ΩMEGA; ΣΟΦΙΑ/σοφια)
  - titlecase digraphs (ǅ/ǆ/Ǆ)
  - capital sharp s (STRAẞE)
- ✅ R1-3 fixed: "Result_Sample" is dropped.
- ✅ R1-4 fixed: {"id": 5} becomes the token when "5" is a known id.
- ✅ probe2 is still leak-free: keys, ints, never-packed keys at depth and case variants.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
R2-1. Regression: a name or id whose `str.lower()` changes length is no longer pseudonymized even in its exact canonical spelling (hybrid.py:69-70 together with 45-50).
- The forward table and the regex now hold only `raw.lower()`. For U+0130 (Turkish dotted capital I), `"İstanbul Ops".lower()` is `"i̇stanbul ops"`: nine characters, the i followed by a combining dot above.
- The pattern matches text one character at a time, so the original "İstanbul Ops" does not match and is sent raw. So are "ISTANBUL OPS" and "istanbul ops".
- Probe7: `'İstanbul Ops and istanbul ops and ISTANBUL OPS and i̇stanbul ops' -> 'İstanbul Ops and istanbul ops and ISTANBUL OPS and team_001'`.
- Round 1 put the raw key in the pattern, so the exact form was caught; this fix lost that.
- Turkish team, service or site names in a catalog are plausible.
- Fix, a few changed lines: build the pattern from the raw strings plus their lowered forms, while the lookup table stays keyed by `raw.lower()`. With `table.get(m.group(0).lower(), "[ENTITY]")` the raw "İstanbul Ops" then matches its own alternative and lowers to a key in the table. For example, pass `_replacer` a separate key list `{raw, raw.lower()}`, or add `forward.setdefault(raw, token)` under a fold-aware lookup.
- Add a UT06-84 case with "İ" (exact spelling pseudonymized, restore gives the canonical name).

#### Minor (Nice to Have)
R2-2. Case variants that change length under full case folding still pass raw:
- "STRASSE" for a name spelled "Straße"
- "ISTANBUL OPS" / "istanbul ops" for "İstanbul Ops", even after R2-1 is fixed
Python `re` IGNORECASE matches one character at a time, so matching these would need casefold-normalised matching over the whole text. Low likelihood. Document it as a known limit next to the leftmost-match comment, or leave it to the spec 10 egress guard.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** R1-1, R1-3 and R1-4 are fixed and the slowdown is gone. But lower-casing the pattern keys lost the exact-spelling match for names whose lower case changes length (U+0130), so such a name now reaches the hosted side in its original spelling (R2-1). The fix is small: put the raw keys back in the alternation next to the lowered ones.

## Re-review 3 (head 5f58156; scope R2-1; R2-2 parked as a documented limit)

Reviewer ran:
- Card tests: 24 passed. hybrid.py has 100 % line and branch coverage (129 stmts, 36 branches).
- ruff check and format --check: clean. mypy --strict: clean. hybrid.py is at 259/260.
- Mutation (pseudonymize returns its input): 11 tests fail, including both PT06-06 functions and the new length-changing UT06-84 test.
- probe2, the pack leak probe, was re-run. None of these raw strings is in the pack: team-, Payments/payments, svc-, Ledger, Core, Jane, 10042, the planted *-NESTED strings.
- New probes probe8 and probe9 (scratchpad T06-18-verify).

### Spec Compliance
- ✅ R2-1 fixed. The exact "İstanbul Ops" and its lower() "i̇stanbul ops" both become team_002 and restore to the canonical name. "ISTANBUL OPS" matches the exact key under IGNORECASE; its lower() is not a key, so it sends "[ENTITY]", never raw text. That is fail closed and better than round 2.
- ✅ The builder's condition (exact key added only when lower() changes length) is sound. I checked this two ways:
  - Brute force over every code point (0..0x10FFFF, surrogates skipped) where `len(c.lower()) == 1` and `c.lower() != c`: `re.fullmatch(re.escape(c.lower()), c, re.I)` holds in every case (0 misses).
  - Every alphabetic BMP character put inside a name ("Team<c>x"), pseudonymized in its exact spelling: 0 leaks.
- ✅ Context-dependent lower() (Greek final sigma) is safe:
  - "ΟΔΟΣ" / "οδος" / "ΟΔΟς" all become the token.
  - "οδοσ" (non-final sigma) matches under IGNORECASE but misses the lookup, so it sends "[ENTITY]".
  - "ΣΟΦΙΑΣ ΤΕΑΜ" becomes the token in every case form.
- ✅ Restore is exact: "team_003 TEAM_003" restores to "Core TEAM_003".
- ✅ Speed with 5,000 entities on a 2.2 KB string: pseudonymize 0.006 s, restore 0.0015 s.
- ✅ R2-2 is documented in the comment at hybrid.py:72.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
R3-1. Contrived gap: two entities whose names differ only by a length-changing fold.
- Example: entity A is named "i̇stanbul ops" (already lower case) and entity B is named "İstanbul Ops".
- B's lower() is already a key, so the `not in forward` guard at hybrid.py:70 also skips adding B's exact spelling. "İstanbul Ops" is then sent raw (probe9: `'İstanbul Ops and team_001'`).
- Fix, no extra line: add the exact key whenever `len(low) != len(raw)`, even when `low` is already present, for example `forward.setdefault(raw, forward.setdefault(low, token))` in place of the guarded update. The lookup by `.lower()` then returns the first entity's token.
- Not blocking: the case needs two catalog entities whose names differ only in that way.

### Assessment
**Task quality:** Approved
**Reasoning:** R2-1 is fixed and the builder's same-length assumption holds for every code point (brute force). Pack leak probes, restore exactness, speed and the mutation check all pass. The one gap left (R3-1) needs two entities whose names differ only by a length-changing fold, and can be folded into a later change.

## Re-review 4 (head dc50e5b; scope R3-1 only)

Reviewer ran:
- Card tests: 25 passed. hybrid.py has 100 % line and branch coverage (130 stmts, 36 branches).
- ruff check and format --check: clean. mypy --strict: clean. hybrid.py is at 260/260.
- Probes probe2, probe7 and probe8 re-run; new probe10 covers the A/B pair in both orders and fold-colliding ids (scratchpad T06-18-verify).

### Mutation check: pseudonymize returns its input
12 of 25 tests fail. The 13 that pass do not depend on pseudonymize (restore rules, tokens and mapping, count_tokens/size, fail-closed objective, missing inputs, no-client AST, no-groups guard).

The 12 that fail:
- UT06-83 (5): pack_items_pseudonymized_and_redacted, pack_never_holds_samples_notes_ticket_text_or_raw_entities, redaction_failure_fails_closed, redactor_sees_pseudonymized_text, row_keys_int_ids_and_never_keys.
- UT06-84 (5): case_variants_pseudonymized_restore_canonical, case_fold_miss_sends_placeholder, length_changing_lower_keeps_exact_spelling, fold_colliding_names_keep_exact_keys (new), longest_first_whole_token_replacement.
- PT06-06 (2): restore_inverts_pseudonymize_and_hides_raw, id_fields_round_trip.

### Spec Compliance
- ✅ R3-1 fixed. For A = "i̇stanbul ops" and B = "İstanbul Ops", in both entity orders:
  - Both exact spellings become the first entity's token, and so does the upper-case "İSTANBUL OPS".
  - No raw spelling reaches the output as a whole token.
  - Fold-colliding ids ("İD-1" / "i̇d-1") work the same way.
- ✅ The new test test_ut06_84_fold_colliding_names_keep_exact_keys asserts the full output string and that no raw spelling (İ/i̇ pair, Straße, STRASSE, strasse) is in it. It fails under the mutation.
- ✅ Restore when names collide: both spellings restore to the first owner's name. B's own token (from token() and entity_id fields) still restores to B's id and name through restore_obj (probe10: `{"entity_id": "team_002"} -> "b"`). I accept this. It is the same first-entity-wins rule already used for any case-variant collision since round 1. Two names equal after lower() cannot be told apart in prose anyway. Ids stay distinct, and nothing leaks: the error is a name mix-up on the local side, never raw text on the hosted side.
- ✅ No regression:
  - Brute force over all code points: 0 same-length misses. Exact-spelling sweep over alphabetic BMP: 0 leaks.
  - Final sigma, Kelvin sign and long s still behave as before (token, or "[ENTITY]", never raw).
  - probe2 pack: no raw ids, names or planted strings.
  - Restore is still exact.
- ✅ Speed with 5,000 entities: pseudonymize 0.015 s, restore 0.002 s. Same order as round 3 allowing for run-to-run noise, and far from the round-1 2.26 s.
- Note: in round 3, "istanbul ops" / "ISTANBUL OPS" were sent raw for the name "İstanbul Ops". They now send "[ENTITY]" (probe7), because the exact key matches them under IGNORECASE and the lookup misses. That is fail closed, an improvement.
- Note, as the spec intends: substrings inside longer tokens ("xİstanbul Ops", "İstanbul Opss") are not replaced. This is whole-token matching, the same as "Coregate" in UT06-84.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
None new. Still parked: R1-2, m5, m7, R2-2.

### Assessment
**Task quality:** Approved
**Reasoning:** R3-1 is closed in both entity orders with a test that asserts no raw spelling, and first-owner restore on fold collisions is consistent and never leaks. Removing pseudonymization fails 12 tests, including both PT06-06 properties.
