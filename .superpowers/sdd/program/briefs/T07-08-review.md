# T07-08 review (verify): Propose pipeline, U07-50

Reviewed: worktree agent-a5a7e90506ce9216f, base 984de12, head bdf18c7 (f4e2909 wip + bdf18c7 feat). Tree left clean at bdf18c7 (`git status` empty). Scratch only under D:\tmp-w21s07a\verify (mut.py, mut.log, probe\test_probe_t0708.py).

## Evidence run
- Card tests + ST07-07 re-point: 79 passed. `tests/unit/harness/memory tests/security tests/fault/harness`: 1020 passed, 1 skipped (ST05-17 Windows symlink privilege, unrelated).
- Coverage (card tests): write.py 100 % line, 67/68 branches (338->exit); _write_steps.py 100 % / 100 %.
- ruff check, ruff format --check, mypy (herness/harness/memory), lint-imports (13 kept), tools/check_module_size.py (exit 0), `--require-test-ids` (77 passed): all green. pytestmark present (unit / integration / fault). write.py 345/380, _write_steps.py 222/260. No HTTP client in the touched files.
- 35 mutation probes (table below) and 7 behaviour probes against the real writer.

## Spec compliance (U07-50 steps)
| Step | Result | Note |
|---|---|---|
| 1 schema.layer_kind | ✅ | write.py:132 |
| 2 check_limits on raw input | ✅ | write.py:134; ST07-11 kills its removal |
| 3 redact before everything else | ✅ order / ❌ scope | ordering is right: numerals, scan, hash, embed and every write use the redacted text (probes 0-4 killed). The exemptions are wider than the spec (Important I1) |
| 4 numerals: model / system / human, procedural exempt | ✅ | _write_steps.py:145-162 |
| 5 injection scan (configured patterns, ZW/full-width) -> instruction_like, log shows pattern indices only | ✅ | write.py:137-141; scanner injected from MemoryConfig |
| 6 provenance (a)-(d) | ✅ | (a) runs only when run_ctx is given, which is what the spec says (Minor M3); (d) uses get_chat_message/get_chat_session, message in session, role user, user_ref == author_ref |
| 7 decide_policy before any write | ✅ | write.py:145 |
| 8 hash + idempotency (task_hash; system keyed hash, any status) | ✅ | repeated inside the tx (write.py:306) |
| 9 rate limits (tool/chat/dashboard/cli) | ✅ | repeated inside the tx (write.py:308); reading (4) defensible |
| 10 confidence | ✅ | _write_steps.py:180-187 |
| 11 dedupe (a) exact, (b) embed / embedding_pending, (c) near-dup (cos >= merge, entity overlap or both empty, best wins), (d) conflict (active, [conflict, merge), shared entity, max 10, step 7 re-run) | ✅ | skipped for instruction_like and procedural; merge in one run_write, provenance_history last 20, confidence only when the new status is active (checked with UT07-24 + mutation 24) |
| 12 insert, one run_write, review item in the same tx, review_item_id in data, §4.1 payload fields, expires_at | ✅ | write.py:300-317, _write_steps.py:190-222 |
| 13 vector after commit; ModelUnavailable -> embedding_pending + memory.embedding.failed | ✅ | store errors map to ModelUnavailable in VectorIndex._guard, so reading (5) holds |
| 14 memory.proposal.stored + herness_memory_proposals_total | ✅ | asserted in test_ut07_31_stored_log_and_metrics_carry_no_text |
| Errors: memory.proposal.rejected{rule} + herness_memory_policy_violations_total{rule}; no text in message/details | ✅ | write.py:115-122, _write_steps.py:57-60 |
| insert_system_item(conn=...) writes inside the caller's tx, embedding_pending, embed_after_commit | ✅ | UT07-27 caller-tx + rollback tests |

Tests: UT07-24 ✅, UT07-25 ✅, UT07-26 ✅, UT07-27 ✅, UT07-28 ✅, UT07-29 ✅, UT07-30 ✅ (gap M2), UT07-31 ✅, ST07-01 ✅, ST07-02 ✅, ST07-03 ✅, ST07-05 ✅ (gap M1; scope gap I1 is not tested), ST07-10 ✅, ST07-11 ✅ (k 1000 belongs to recall/T07-10, agreed), ST07-22 ✅, ST07-23 ✅, FT07-02 ✅ (gap M4), ST07-07 re-point ✅ (killed by mutation 11).

Spec note and §2 row: the `_write_steps.py` row (L4, budget 260, private, imported only by write.py) is consistent. I checked readings (1)-(6) and all are defensible. (2) skipping dedupe for keyed system items is needed, because a near-dup merge would fold separate run summaries into one row and the keyed lookup would then fail on resume. (5) holds because VectorIndex maps LanceDB errors to ModelUnavailable.

### ⚠️ Items
- Merge path, chat/tool into active (probe): a chat proposal whose text exactly matches, or is a near duplicate of, an active item returns `status="active"`, `merged_into=<old>` and no review item. The chat text itself is never stored. Only its provenance is added to provenance_history, and confidence stays the same because the new policy status is pending. This matches U07-50 ("status=old.status") and does not break TH07-23. Callers and ST07-23 must not read result.status as "this content became active".
- Pending or instruction_like content cannot change an active row's confidence: instruction_like skips dedupe, and the confidence bump happens only when the new decision is active (mutation 24 killed). A clean active-policy proposal can merge into a pending instruction_like item (near-dup) and raise its confidence. That item stays pending for review, so this is acceptable.
- Merges do not add rows, so they do not count toward step 9 rate limits. Repeated merges only rewrite one row (provenance_history is capped at 20) and create no review items, so TH07-10 is not affected.
- Outside this card: InjectionScanner._data_strings (policy.py:133) stops after 2,000 strings and never scans dict keys. Data can hold more than 2,000 short strings within 16 KB, so an instruction placed after 2,000 padding strings is not flagged. Owner: U07-40.
- `anthropic_client.py` imports httpx2. That file predates this card and is out of scope.

## Findings
### Critical
none

### Important
**I1 Redaction exemptions are wider than U07-50 step 3, so personal data reaches memory_item.data and the review payload** (TH07-05). File: herness/harness/memory/_write_steps.py:129-138.
- `walk(v, entities=k == "entities")` gives the entity exemption to any key named `entities` at any depth. The spec exempts only `data.entities[*].type/.id`.
- `obj(value, ID_KEYS)` skips the whole value under an ID key at every depth, whatever its type. For example `{"query_ids": {"n": ["<email>"]}}` is never redacted.
- Dict keys are never redacted.

Probe (probe\test_probe_t0708.py, real Redactor): each of these payloads leaves the planted email in the stored row:
- `{"x": {"entities": [{"type": "t", "id": EMAIL}]}}`
- `{"query_ids": {"n": [EMAIL]}}`
- `{EMAIL: "x"}`

The spec-sanctioned cases, a top-level `rule_id: EMAIL` and a top-level `entities[*].id`, also persist, as the spec intends.

Fix:
- Apply `_ENTITY_KEEP` only to the list at top-level `data["entities"]`.
- Skip redaction under an ID key only when the value is a str or a list of str; walk anything else normally.
- Redact dict keys too, or reject the proposal when redacting a key would change it (PolicyViolation("data.keys")).
- Add those three payloads as ST07-05 cases.

### Minor
- **M1** ST07-05 does not prove the ID_KEYS / entity type-id exemptions (mutations 5 and 6 survived). The kept values (`br_owner`, `team`, `team_x`) are not changed by the redactor, so dropping `rule_id` from ID_KEYS or redacting entity ids still passes. tests/security/test_st07_write.py:258-260. Fix: use a spy Redactor that records its inputs and assert it never receives the ID-key or entity type/id values.
- **M2** UT07-30 does not pin step 11(b) (mutation 29 survived). With `draft.flags.append("embedding_pending")` removed from write.py:256, step 13 re-embeds, fails again and sets the flag afterwards, so the test still passes. This breaks "at most one embedding per call", and the insert tx and review payload lose the flag. tests/unit/harness/memory/test_memory_write.py:500. Fix: assert `len(env.embed.calls) == 1` and that the review payload flags include `embedding_pending`.
- **M3** An agent proposal with `run_ctx=None` skips the run/task match (write.py:193-195). This is spec-conformant ("when run_ctx is given"), and the probe showed an arbitrary run_id/task_id accepted as pending. A caller that forgets the ctx can charge another run's rate budget or hit another task's idempotency key. Fix: add defence in depth by rejecting `author_type == "agent"` without run_ctx (`provenance.mismatch`), or add a spec-note line saying the tool wrapper (U07-?? ProposeMemoryTool) must always pass run_ctx, with a test there.
- **M4** FT07-02 injects at `sqlite.write`, which fires before `BEGIN IMMEDIATE` (herness/store/ops/core.py:155). "Nothing half-written" is therefore trivially true, and a busy error in the middle of the transaction is never exercised. tests/fault/harness/test_memory_write_fault.py:25. Fix: add a case where `create_review_item` (or `insert_memory_item`) raises `sqlite3.OperationalError("database is locked")` once inside the tx. Assert the retry commits exactly one item with one review item.

## Mutation probe table (35 probes: 32 killed, 3 survived)
| # | Guard neutralised | Tests | Result |
|---|---|---|---|
| 0 | redactor output ignored | ST07-05 | killed |
| 1 | data not redacted | ST07-05 | killed |
| 2 | embed raw content in 11(b) | ST07-05 | killed |
| 3 | store raw content | ST07-05 | killed |
| 4 | entity `label` exempted | ST07-05 | killed |
| 5 | `rule_id` dropped from ID_KEYS | ST07-05 + unit | **survived** (M1) |
| 6 | entity type/id no longer exempt | ST07-05 + unit | **survived** (M1) |
| 7 | instruction_like not added | ST07-01 | killed |
| 8 | data strings not scanned | ST07-01 data | killed |
| 9 | flagged log carries content | ST07-01 | killed |
| 10 | dedupe not skipped for instruction_like | ST07-01 data | killed |
| 11 | instruction_like not added | ST07-07 re-point | killed |
| 12 | provenance.mismatch removed | ST07-02 | killed |
| 13 | numerals.uncited removed | ST07-03 | killed |
| 14 | human unverified_numbers removed | ST07-03 | killed |
| 15 | human marker rule removed | unit | killed |
| 16 | per_run limit off | ST07-10 | killed |
| 17 | check_limits removed | ST07-11 | killed |
| 18 | message-in-session check removed | ST07-22 | killed |
| 19 | session user_ref check removed | ST07-22 | killed |
| 20 | role == user check removed | ST07-22 | killed |
| 21 | session check chat-only (dashboard skipped) | ST07-22 | killed |
| 22 | row stored active | ST07-23 | killed |
| 23 | review item not created | ST07-23 | killed |
| 24 | confidence bumped on pending merge | unit | killed |
| 25 | provenance_history unbounded | unit | killed |
| 26 | no step-7 re-run on conflict | unit | killed |
| 27 | idempotency not repeated in tx | unit | killed |
| 28 | rate not repeated in tx | unit | killed |
| 29 | 11(b) embedding_pending not set | unit | **survived** (M2) |
| 30 | provenance.query_ids removed | unit | killed |
| 31 | default expires_at dropped | unit + security | killed |
| 32 | rejected log carries content | unit | killed |
| 33 | stored log carries content | unit | killed |
| 34 | conflict without shared entity | unit | killed |

All mutations were applied in place and restored from the original bytes right after each run. `git status` is clean at bdf18c7.

## Verdict
**Needs fixes**: 0 Critical, 1 Important (I1), 4 Minor (M1-M4). The pipeline follows U07-50 step by step and is well tested (32/35 mutations killed). I1 is a real TH07-05 gap beyond the spec's stated exemptions and should be fixed with ST07-05 cases before approval. M1 and M2 can be done in the same pass.

## Re-verify round 1 (e0e44e0 `fix(memory): T07-08 review round 1`, bdf18c7..e0e44e0)

Scope: I1, M1, M2, M3, M4. Environment and rules as in the first pass. The tree was left clean at e0e44e0.

Gates at e0e44e0:
- Card tests + ST07-07: 87 passed.
- Coverage: write.py 100 % line / 67 of 68 branches; _write_steps.py 100 % / 100 % (245 of 260 lines).
- ruff check, ruff format --check, mypy, lint-imports (13 kept) and check_module_size (exit 0) all pass.
- The diff adds no new log statements, so no new memory text goes into logs.

| Item | Status | Evidence |
|---|---|---|
| I1 redaction exemptions | ✅ fixed | See the I1 detail below. |
| M1 exemptions proven | ✅ fixed | `test_st07_05_only_spec_exemptions_skip_the_redactor` spies on `Redactor.redact`. Mutants #5 (rule_id dropped from ID_KEYS) and #6 (entity type/id no longer exempt) are now killed. |
| M2 11(b) embedding_pending | ✅ fixed | `test_ut07_30_dedupe_embedding_failure_is_final` asserts one embed call, the flag on the row and the flag in the review payload. Mutant #29 is now killed. |
| M3 agent without run_ctx | ✅ accepted as a spec note (item 8) | The wording is adequate. It matches U07-50 6(a) as written and names the wrapper that must pass run_ctx (U07-64 / T07-11). U07-64 steps 3-4 already build run_ctx and provenance only from ToolContext. When T07-11 is reviewed, check that its test covers this. |
| M4 in-transaction fault | ✅ fixed | `test_ft07_02_busy_inside_the_transaction_is_retried` raises "database is locked" once inside the transaction, from create_review_item or insert_memory_item. It asserts two calls, exactly one item and one review item, and the two linked. Mutant N4 (insert without run_write) is killed. |

I1 detail:
- The only exemptions left are a str or list-of-str value under an ID_KEYS key, and the str type/id of the entries of the top-level `data["entities"]`.
- Nested `entities`, other value types under ID keys, and dict keys are now redacted (`_write_steps.py:117-165`).
- Probe results:
  - My three I1 probes (nested entities, ID-key subtree, email as a dict key) now come back CLEAN.
  - Top-level `rule_id: EMAIL` and a top-level `entities[*].id` still persist, which is what the spec allows.
  - The new parametrised ST07-05 case also covers a `template_id` dict and checks SQLite, FTS and the embedder input.
- The spy test confirms that ID-key strings and top-level entity type/id still reach storage without passing through the redactor.

Round-1 mutation probes (8): 7 killed, 1 survived.

| # | Mutant | Result |
|---|---|---|
| #5 | rule_id dropped from ID_KEYS | killed |
| #6 | `_ENTITY_KEEP` emptied | killed |
| #29 | 11(b) embedding_pending not set | killed |
| N1 | dict keys not redacted | killed |
| N2 | any value under an ID key exempt | killed |
| N3 | nested entities exempt again | killed |
| N4 | insert without run_write | killed (FT07-02 in-transaction test) |
| N5 | entity type/id kept even when not a str | survived, but equivalent: step 2 `_entities_ok` (policy.py:209) already rejects entities whose type/id is not a short str, so this path cannot be reached |

⚠️ Redacting dict keys (spec note 7) rewrites a key to its pseudonym token. Two different keys that redact to the same token would collapse into one key. The HMAC pseudonyms make this practically impossible, so no action is needed. The spec note reads the step 3 rule "every string in data" as including keys, which is defensible.

Open items: none in this card's scope. Carry-overs (not T07-08): the U07-40 scan limit of 2,000 strings and its skipping of dict keys; the T07-11 test that the tool always passes run_ctx.

### Round 1 verdict
**Approved.** I1 and M1-M4 are resolved; there are no new findings.
