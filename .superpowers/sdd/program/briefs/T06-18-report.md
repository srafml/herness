# T06-18 report: Hybrid pack and pseudonyms (build)

Status: DONE_WITH_CONCERNS (small, listed below). Worktree agent-a2fdb96db08def1a5, base 02bd94a.
Commits: 0d4e4f8 wip(T06-18): hybrid pack, pseudonymizer and card tests green; 58517ea feat(harness): hybrid evidence pack and pseudonyms (T06-18).

## Files
- herness/harness/swarm/__init__.py: 1 line (budget 20), docstring only, no re-exports (ledger ruling).
- herness/harness/swarm/hybrid.py: 259 lines (budget 260). `PACK_HEADROOM_TOKENS = 8000`, `Pseudonymizer`, `build_evidence_pack`.
- tests/unit/harness/swarm/test_swarm_hybrid.py: 19 tests (UT06-83 x12, UT06-84 x4, PT06-06 x2 hypothesis).

## Implementation
- `Pseudonymizer(entities)`: tokens f"{entity_type}_{n:03d}" per type in input order; duplicate (type, id) ignored; raw string -> token first-wins. `pseudonymize`: one regex alternation sorted longest first, whole-token lookarounds `(?<!\w)...(?!\w)`, single pass (never re-replaces inside a token). `restore` (prose): token -> name, or id when name is None/empty. `restore_obj`: walks Mapping/list/tuple (tuples -> lists); keys `target_id`, `entity_id`, `row_key` switch the subtree to id mode; keys are not rewritten. `token()` unknown -> `NotFound` (context entity_type only). `mapping()` -> {token: {entity_type, entity_id[, name]}} (fits `SwarmTaskState.pseudonyms`, tested).
- `build_evidence_pack(spec, *, findings, inp, pseudo, max_tokens, cfg)` (6 args; `cfg: ClientConfig` per ledger ruling). Pack keys: objective, findings[{finding_id, entity_type, entity_id (token), claim, numbers (NumberRef dump, row_key scrubbed), confidence, challenges[{round, verdict, checks[{check, result, note}] for checks with a note}]}], portfolio{scenario, rows, selected, query_ids} (custom not packed), levers, dq_checks (check_name only), unconfirmed_weights. Only those inp keys are read; spec.inputs is never read; findings come from the `findings` arg (inp["findings"] ignored).
- Every outbound string = `redact_text(pseudo.pseudonymize(s))` (shared herness.core.redact path, no new detection code). Fail closed: finding with unredactable claim/note/row_key string or unknown entity -> dropped (WARNING `harness.hybrid.finding_dropped`, finding_id + error type only); unredactable lever/portfolio/flag item -> that item dropped; unredactable objective -> `RedactionFailed("objective redaction failed", task_id=...)`.
- Size: `count_tokens(cfg, [Message(user, [TextPart(canonical_json(pack))])], [], [])[0]` on the final pseudonymized+redacted pack; while > max_tokens - 8000 drop the last finding. Pack JSON > 200,000 chars (TextPart max_length) counts as over without a count call.
- No LLM/HTTP client constructed (test parses the module AST: no httpx/anthropic/openai/*client import, no *Client(...) call).

## Evidence
- RED: `PYTHONUTF8=1 uv run pytest tests/unit/harness/swarm -q -p no:logging` -> ImportError: cannot import name 'hybrid' from 'herness.harness.swarm' (collection error).
- GREEN: same command -> 19 passed. Coverage hybrid.py 100 % line / 100 % branch (136 stmts, 40 branches).
- `PYTHONUTF8=1 uv run pytest tests/unit/harness -q -p no:logging` -> 1122 passed, 1 skipped (symlink privilege).
- ruff format/check clean; mypy (touched files) clean; lint-imports 13 kept 0 broken; check_module_size 0; check_type_ownership 0; pre-commit hooks passed on commit (no --no-verify).
- Tests use a real `Redactor` (directory name "Jane Doe") installed as the process-wide instance, so real PERSON/secret detection runs; fail-closed cases patch `hybrid.redact_text`.

## Spec notes
1. U06-123 signature: + keyword-only `cfg: ClientConfig` (ledger ruling; real T05-07 API). "Pure" -> no side effect except count_tokens and a WARNING log per dropped finding.
2. U06-123 step 2 does not say what happens when no finding is left and the pack is still over the cap: implemented as `BudgetExceeded("evidence pack exceeds max_input_tokens_per_call", task_id, max_tokens)` (never return an over-cap pack). The caller (U06-115 / Skeptic routing) must decide whether that is a local fallback; spec owner to confirm the error class (BudgetExceeded is excluded from the U06-82 step 8 "failed" path).
3. U06-123 item list omits `kind`, `outline`, `contested`, `open_concerns`, `dead_tasks`, `crosscheck_incomplete`, `question`, portfolio `custom`; built exactly to the list. The off-network Writer may need `outline`/`open_concerns` (U06-115 step 3 adds them to inp) - spec owner to decide; adding a key is a few lines, but hybrid.py is at 259/260.
4. PT06-06 vs U06-124 step 3: `restore` in prose maps a token to the NAME, so restore(pseudonymize(x)) == x cannot hold when x contains the id of a named entity. PT06-06 is tested on the domain where it holds (named entities by name, unnamed by id, filler words that cannot form an id) plus a second property: id-valued fields (entity_id, row_key) round-trip to ids for every entity. Leakage property: no raw id or name remains as a whole token in pseudonymize output.
5. `pseudonymize` is case-sensitive and whole-token (spec wording): "payments platform" in lower case or a name split across punctuation is not replaced (the shared redactor still catches directory persons and secrets). Text already containing a literal token string (e.g. "team_001") would be rewritten by restore.
6. Strings in rows are scrubbed; non-string values (ints, floats, bools) and mapping keys (column names) are passed as-is; an integer-valued entity id in a row would not be pseudonymized (catalog ids are strings).

## Concerns
- hybrid.py is at 259/260 lines: no room for spec-note 3 additions without a budget ruling.
- Environment: C: had ~750 MB free; the pre-commit pytest-unit hook failed with ENOSPC (test_ut01_50 sparse file) until TEMP/TMP were pointed to D:/t0618tmp for the commit (NOT removed: the safety check refused rm -rf of D:/t0618tmp; delete it manually, it holds only pytest temp dirs). Not a code issue, but other agents' commits may hit the same.
- The worktree had no .venv at dispatch (ledger said present); ran `uv sync --frozen`.

## Fix round 1 (review T06-18-review.md: I1, I2, m3, m4, m6 comment)
- I1: outbound matching ignores case. Forward table keyed first-wins by `raw.lower()`, regex compiled with re.IGNORECASE; each alternative is a named group (`g<i>`), so the token is looked up by `m.lastgroup`, not by the matched text, and Unicode case-folding differences cannot miss a lookup. Restore stays case-sensitive: `_to_ids` and `_to_names` are two exact replacers; a case variant restores to the canonical name (spec note: round trip holds up to case). New UT06-84 test covers this. PT06-06 generator: names unique ignoring case, filler letters disjoint from name letters, leakage check case-insensitive, plus `pseudonymize(upper).lower() == pseudonymize(lower)`.
- I2: mapping keys go through `_clean` (pseudonymize + shared redactor), same as values. UT06-83 asserts id- and name-keyed nested dicts in levers and row_key.
- m3: int values under id-valued keys (`*_id`, `row_key` subtree, which covers entity_id and target_id) are matched by str() and replaced with the token when they are a known raw id. Other ints are unchanged (e.g. `score`).
- m4: keys result_sample, text_redacted, notes, prior_context, ticket_text are dropped at any nesting in scrubbed rows. Tested in levers (top level and nested) and in portfolio rows and selected.
- m6: one comment line on the leftmost-match overlap in `_replacer`. m5 and m7 are parked as instructed.
- Size: hybrid.py is 260/260. To fit, `mapping()` became a comprehension and the `_restore` closure became two replacers.
- Commit 3e5462d. Evidence: card tests 21 passed; hybrid.py 100 % line and branch (132 stmts, 34 branches). PT06-06 passes with seeds 1-4. tests/unit/harness: 1125 passed, 1 skipped. ruff, mypy, lint-imports and check_module_size are clean. TEMP/TMP pointed at D:/t0618tmp for the hooks.

## Fix round 2 (Re-review 1: R1-1, R1-3, R1-4; R1-2 parked)
- R1-1: `_replacer` is now a plain alternation with no groups (keys escaped, longest first, same whole-token lookarounds).
  - Forward table: keyed by `raw.lower()`, first entity wins. It is compiled with re.IGNORECASE and looked up with `table.get(m.group(0).lower(), "[ENTITY]")`. A miss sends the placeholder, never raw text; for example, the long s (U+017F) matches "s" under IGNORECASE but does not lower to "s".
  - Restore: exact `table[m.group(0)]`.
  - Guard test: all three compiled patterns have `.groups == 0`.
  - Timing, 5,000 entities, 4.5k-char text: pseudonymize 0.0052 s, restore 0.0009 s (named groups: 2.26 s).
- R1-3: never-packed keys are compared with `str(k).lower()`. `"Result_Sample"` is dropped (tested).
- R1-4: the id-key test is `f"_{k}".endswith(("_id", "row_key"))`, so a bare `id` counts too. An int under `id` is pseudonymized (tested).
- Commit ed359f0. hybrid.py: 260/260. Tests: card 23 passed; hybrid.py 100 % line and branch; tests/unit/harness 1127 passed, 1 skipped. ruff, mypy, lint-imports and check_module_size are clean.

## Fix round 3 (Re-review 2: R2-1; R2-2 comment)
- R2-1: the forward table stays keyed by `raw.lower()` (first entity wins) and is looked up with `.get(m.group(0).lower(), "[ENTITY]")`.
  - When `lower()` changes the length (e.g. "İ" -> "i" + combining dot), the exact spelling is also a key, so it is in the alternation too.
  - I did not add the exact spelling for every key: that made pseudonymize about 10x slower (0.05 s vs 0.005 s at 5,000 entities).
  - New UT06-84 test: exact "İstanbul Ops" and its lower() both become team_001 and restore to the canonical name.
- R2-2: one comment line says length-changing full case folds ("STRASSE" for "Straße") are not matched by re; the redactor and egress guard are the backstop.
- To fit, `_over` now builds the Message inline.
- Commit 5f58156. Evidence: hybrid.py 259/260. Card tests 24 passed; hybrid.py 100 % line and branch. tests/unit/harness 1128 passed, 1 skipped. ruff, mypy, lint-imports and check_module_size are clean. Timing at 5,000 entities: pseudonymize 0.0050 s, restore 0.0009 s.

## Fix round 4 (Re-review 3: R3-1 only)
- R3-1: `first = forward.setdefault(low := raw.lower(), token)`, then the exact spelling is added whenever `len(low) != len(raw)`, even when another entity already owns the lowered key.
  - The exact key maps to the lowered key's owner. Fold-mode lookup goes through `.lower()`, so both spellings pseudonymize to the first entity's token and restore to its name (first-entity-wins, as for any case variant).
- New UT06-84 test covers two pairs of separate entities:
  - "i̇stanbul ops" and "İstanbul Ops": both spellings become team_001 and restore to the first name.
  - "Straße" and "STRASSE": their lower() forms differ, so each keeps its own token and restores to its own name; "strasse" goes to STRASSE.
  - No raw spelling appears in the output.
- Public surface unchanged (Pseudonymizer and build_evidence_pack signatures and behaviour otherwise the same). No sibling module.
- Commit dc50e5b. Evidence: hybrid.py 260/260; card tests 25 passed; hybrid.py 100 % line and branch; tests/unit/harness 1129 passed, 1 skipped. ruff, mypy, lint-imports and check_module_size are clean. pseudonymize at 5,000 entities: 0.0045 s.
