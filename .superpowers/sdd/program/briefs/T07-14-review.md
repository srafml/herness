# T07-14 review (verify agent): ContextCompactor and compaction notes summarizer

Head bf24f0c on base 9c8f34a. Worktree agent-a557a8b5dbd0d1f19. Reviewer: independent opus verify.

**Verdict: Needs fixes** (0 Critical, 2 Important (both are missing tests; the production code looks correct), 5 Minor)

## Gates run by the reviewer
- `pytest tests/unit/harness/memory tests/fault/harness/test_memory_compactor_fault.py tests/security/test_st07_compactor.py tests/unit/harness/test_tools_recording.py -q -p no:logging --cov-branch`: 366 passed (27 s).
- Coverage (line+branch combined): compactor.py 99 % (210->exit), _compactor_ledger.py 98 % (47->52, 50->47), _compactor_llm.py 100 %, compact_build.py 100 %. All ≥ 90/85.
- ruff check and ruff format --check on the 10 changed files: clean. mypy (herness/harness/memory + test_memory_compactor.py, which includes the `CompactorLike` assignment): 0 issues. lint-imports: 13 kept, 0 broken. tools/check_module_size.py: exit 0.
- Sizes: compactor.py 312/330, _compactor_ledger.py 155/200, _compactor_llm.py 146/200 (both have §2 rows), compact_build.py 383/390, working.py 296/300 (untouched), hooks.py 340 (untouched), _compact_text.py untouched, compaction_notes.md 34/60.
- Wheel: `uv build --wheel` (hatchling `packages=["herness"]`) ships `herness/harness/memory/prompts/compaction_notes.md`, so the importlib.resources loader works from an installed wheel.

## Spec compliance per item
1. ✅ Summarizer path. The only `acomplete` is inside `Completer` (_compactor_llm.py:76). Every call goes `complete_validated(max_repairs=1)` → `Completer`, which does `wait_for(profile.timeout_s)`, then `ctx.ledger.charge` for every response (the repair included), then the `llm_call` trace with `prompt_hash`, and raises `ModelRefused` on a refusal. `validate_notes` is applied, and a `None` result becomes deterministic notes. The U07-77 step-6 list (compactor.py:62) gives deterministic notes plus a `notes_fallback` WARNING with reason = the class name. `BudgetExceeded` propagates (tested). The grep finds no raise in herness/harness/memory. Mutants on the repair charge, trace, refusal, timeout, validate_notes and swallowing all errors were all killed.
2. ✅ Citations. The PT07-01 loop (hypothesis, 60 examples, both profiles) feeds each output back as `state.messages`. It asserts every id seen so far and every cited (query_id, value) pair, and that groups are never split (local profile). The Claude profile has a single message, so there are no groups to split there. The id rescue is judged in the next section.
3. ✅ FT07-05. It is parametrized for timeout and for a scripted hang. It checks deterministic notes, that the scratchpad was saved by `ops.save_scratchpad` at the fault point, that a kill during the next summarizer call saves nothing, and that a NEW compactor restores from the checkpoint, reaches compactions == 2, and keeps all ids and cited numbers.
4. ⚠️ ST07-15/16. The Claude transcript is escaped and wrapped once. It is tested against a closing-tag injection, a fake scratchpad, role-marker lines, NUL and zero-width characters, and the no-escape mutant is killed. For invented output, numbers and markers become `[[?]]` and ids are removed, in both profiles. The spec note against U07-75 is present. The one gap is that the escape and wrap of the summarizer INPUT (dropped groups and prior notes) is not tested; see I1.
5. ⚠️ Size cap. `enforce_cap` runs before every render and save (compactor.py:237, inside `_build`, and every mutation path ends in `_build`). The compactor itself does no redaction. Tool results reach `state.messages` already redacted upstream (harness/tools), so the cap applies to redacted content by construction, and there is no in-compactor ordering against redaction to test. The cap's wiring and its order relative to render are untested; see I2.
6. ✅ `CompactorLike`: mypy-checked through `_as_compactor_like`, with a runtime signature and coroutine check. hooks.py is untouched. The T07-13 carry-overs are closed: M0 is `task_message` (killed mutant m0-unchanged), the Claude profile ledgers kept groups (killed mutant claude-no-kept-ledger), and orphan results get the "unknown" call (the mutant survives; see m3).
7. ✅ Prompt file. It is 34 lines, contains the exact U07-99 sentence and no secrets or PII, and is loaded with importlib.resources from package data (verified in a real wheel). A missing file raises `ConfigError` at construction (tested). The 16-hex hash appears in the trace (tested).
8. ✅ Acceptance. `memory.compaction.completed` is asserted with the report fields. The UT07-60 grep is present. `last_report` has all 7 fields. Module budgets hold, there is no HTTP client import in herness/harness/memory, layering is clean, and test IDs, docstrings and pytestmark are correct.
9. ✅ Non-card tests. The UT05-124 allow-list gains one row, `(memory/_compactor_llm.py, compaction_prompt)`, which is a prompt version hash and not a row hash. This matches the existing roles/base.py prompt_hash precedent. The UT07-59 update follows the controller's transcript-wrap ruling (the expected value is now `wrap_untrusted(escape_content(body))`, and the tail assertion now ends with `</untrusted_data>`). Both changes are minimal and justified.

### Judgement on the broadened id rescue (spec note U07-76 (d))
The rescue (compactor.py:227) gives a minimal entry to every id in `state.messages` or `state.query_ids` that is not in the ledger, M0 or the kept groups. As the note says, this makes the step-9 **id** invariant hold by construction. The invariant still guards cited refs, recorded numeral mentions, render and cap regressions, so it is not dead code. It also cannot mask evidence loss: the alternative for such ids (for example an id mentioned only in an assistant's text) was OVE and then spec-05 truncation, which loses more.

Tests on the failure path still exist:
- `test_ut07_62_invariant_failure_retries_then_raises` monkeypatches `invariants_hold` and gets deterministic notes, then an `OutputValidationError` with an `invariant_failed` ERROR.
- `test_ut07_62_invariants_detect_losses` checks the real function against a lost id, a lost cited value and a lost numeral. The invariants-true mutant is killed.

No test makes the real invariant fire through `on_context_pressure`. That is acceptable, but see m4 on what the rescue legitimises.

## Mutation results (19 mutants; scratch script .agent-tmp/T07-14-verify/mut.py, all restored)
| Mutant | Result | Killer |
|---|---|---|
| repair response not charged | KILLED | UT07-61 bad_json_twice |
| Claude transcript not escaped | KILLED | UT07-59 claude_profile |
| summarizer chunk not escaped (_compactor_llm.py:125) | **SURVIVED** | (I1) |
| prior notes not escaped (_compactor_llm.py:111) | **SURVIVED** | (I1) |
| validate_notes skipped (plain model_validate) | KILLED | UT07-61 notes_failing_validation |
| enforce_cap call removed (compactor.py:237) | **SURVIVED** | (I2) |
| enforce_cap moved after render | **SURVIVED** | (I2) |
| id rescue removed | KILLED | UT07-62 orphans_state_ids |
| invariants_hold always True | KILLED | UT07-62 invariants_detect_losses |
| no wait_for timeout | KILLED | UT07-61 timeout |
| `_FALLBACK` catches Exception (swallows BudgetExceeded) | KILLED | UT07-60 ledger_budget_error |
| Claude kept groups not ledgered | KILLED | PT07-01 compactor loop |
| M0 = merged head (nesting) | KILLED | UT07-62 no_nest |
| orphan result skipped instead of "unknown" call | **SURVIVED** | (m3) |
| no save / no restore | KILLED / KILLED | UT07-62 saved_then_restored |
| no llm_call trace | KILLED | UT07-61 charged_and_traced |
| refusal ignored | KILLED | UT07-61 refusal |
| completed event renamed | KILLED | UT07-62 completed_event |

## Issues

### Critical
None.

### Important
- **I1: the TH07-16 input-side mitigation is untested** (herness/harness/memory/_compactor_llm.py:111 and :125; tests/security/test_st07_compactor.py:115). Removing `escape_content` from the summarizer chunk or from the prior notes leaves all 247 tests green. U07-77 Security notes and the controller ruling both require the dropped groups to reach the model escaped and wrapped in `<untrusted_data source="tool_results" record_id="">`.
  - Fix: in ST07-16 (or a new ST07-16 test), run a compaction over `_adversarial_history()` with `cs.notes_llm(...)`.
  - Read the request the model received: the `payload` of the `llm_call` event in `RecordingTracer`, or record `req` in a small FakeLLMClient subclass.
  - Assert the user text contains exactly one `<untrusted_data source="tool_results" record_id="">` and exactly one `</untrusted_data>`.
  - Assert the raw `INJECTION` string is absent, and that `&lt;/blocked-untrusted_data&gt;` and `&lt;blocked-scratchpad` are present.
  - Use a prior-notes case whose text contains `<scratchpad>` to cover :111.
- **I2: the size-cap wiring and its order are untested** (herness/harness/memory/compactor.py:237). `enforce_cap` is unit-tested on its own, but deleting the call, or moving it after `pad.render`, passes every test. The w20-s07 ruling makes the compactor responsible for the cap, and the brief asks for the order to be verified by a test.
  - Fix: add a compactor-level UT07-62 test that forces the cap. For example, monkeypatch `cm.enforce_cap` with `functools.partial(ledger_mod.enforce_cap, limit=<small>)`, or seed many `unmatched` numerals and patch the limit the same way.
  - Assert that `ops.saved[TASK_ID]` has a compact-JSON size ≤ the limit and that `last_report.ledger_compacted` is True.
  - Assert that the rendered `<scratchpad>` in `new[0]` reflects the capped pad, for example that the dropped oldest unmatched value and the `cols=[` samples are absent. This kills the cap-after-render mutant.
  - Optionally, add a one-line comment or docstring noting that redaction happens upstream (tool results arrive redacted), so "after redaction" holds by construction.

### Minor
- **m1: a repair request can carry an unsupported temperature, and some client errors escape `summarize_notes`** (herness/core/resilience/chain.py:130 via herness/harness/memory/_compactor_llm.py:76). spec 08 `build_repair_request` always sets `temperature=0.0`. For a profile with `supports.sampling_params == False`, the anthropic adapter still sends it (anthropic_client.py:227). A resulting HTTP 400 is classified as `ConfigError`, which is not in the U07-77 step-6 list, so it escapes `on_context_pressure`. The same applies to `AuthError`. This follows the step-6 list literally, but it contradicts "never raises for model problems".
  - Fix: in `Completer.acomplete`, when the profile does not support sampling, send `req.model_copy(update={"temperature": None})` (pass the flag in from `profile.supports.sampling_params`).
  - Optionally, raise the ConfigError question in a spec note.
- **m2: the repair path needs a bound resilience ops backend** (herness/core/resilience/chain.py:187 → events.py:173). If none is bound, `record_event` raises `ConfigError`, and that escapes `summarize_notes`. Production binds it, and the tests bind a fake. The report mentions this, but the code does not.
  - Fix: add a one-line comment in `summarize_notes` or a spec note stating the precondition (resilience backend bound, as for every `complete_validated` user).
- **m3: the orphan "unknown" placeholder is only weakly asserted** (herness/harness/memory/_compactor_ledger.py:105; tests/unit/harness/memory/test_memory_compactor.py:383). Skipping orphan results instead of ledgering them survives, because the id rescue then creates the same `tool="unknown"` entry. The orphan's table data (row_count, columns, cited numbers) would be lost unnoticed.
  - Fix: give the orphan a `query_id=... rows=2` table header and assert that `by_id[qid(900)].row_count == 2` and that its step is the group's step, not 0.
- **m4: rescued ids appear in the LEDGER as if they came from a tool result** (herness/harness/memory/compactor.py:227). The LEDGER is headed "verbatim from tool results". Rescued ids (for example an id the agent itself wrote in assistant text) are rendered there as `unknown step 0`, and `validate_notes` then treats them as known ids. The spec already accepts this for summary ids and state ids, and "unknown step 0" is a visible signal.
  - Fix: keep the behaviour. Optionally mention the trade-off in spec note (d).
- **m5: an unrecorded deviation on `max_output_tokens`** (herness/harness/memory/_compactor_llm.py:141). It uses `min(cfg.summary_max_tokens, profile.max_output_tokens)` where the spec says `cfg.summary_max_tokens`. The deviation is sensible but is not in the U07-77 spec note.
  - Fix: add it to the note.

## Strengths
- The error discipline is clean. There is a single `acomplete` site. The charge happens before the refusal check, so refused responses are still billed. BudgetExceeded propagation and the absence of any raise are both tested.
- The invariant, cap and ledger logic is kept pure in a private sibling and unit-tested directly, including negative invariant cases.
- The FT07-05 restart uses a BaseException kill, which realistically models a process that dies mid-call, and it verifies that nothing was saved by the killed call.
- The PT07-01 compactor loop covers both profiles, random K and repeated compactions with the output fed back.

**Task quality: Needs fixes.** The behaviour is correct and all gates pass. Two binding security and size requirements (escaping of the summarizer input, and the cap wiring and order) have no test that fails when they are removed.

## Re-review round 1 (head b2a6cdb, fix diff bf24f0c..b2a6cdb)

**Verdict: Approved.** Every finding (I1, I2, m1 to m5) is closed. One new nit (n1) is non-blocking.

Scope: findings I1, I2 and m1 to m5 only.

Gates:
- Tests: 369 passed. This covers the card tests, `tests/unit/harness/memory` and `test_tools_recording.py`.
- Coverage (line and branch combined): `compactor.py` 100 %, `_compactor_llm.py` 100 %, `_compactor_ledger.py` 98 %.
- `ruff check` and `ruff format --check`: clean.
- mypy (memory package plus both card test files): 0 issues.
- `check_module_size`: exit 0.
- Sizes: `compactor.py` 314/330, `_compactor_llm.py` 150/200.

Mutants re-run (all mutated files restored):

| Mutant | Result | Killer |
|---|---|---|
| summarizer chunk not escaped | KILLED | ST07-16 `summarizer_input_is_escaped_and_wrapped` |
| prior notes not escaped | KILLED | same test |
| `enforce_cap` call removed | KILLED | UT07-62 `size_cap_wired_before_render_and_save` |
| cap moved after render | KILLED | same test |
| orphan results skipped | KILLED | UT07-62 `orphans_state_ids_and_summary_rescue` (`row_count` 3, step 1) |
| temperature strip removed (new, for m1) | KILLED | UT07-61 `no_sampling_params_means_no_temperature` |

Findings:
- **I1: closed.** The request text is read from the `llm_call` payload. Tests assert:
  - there is one `tool_results` wrapper and one closing tag;
  - the raw injection text and raw `<scratchpad` do not appear;
  - both the prior-notes part and the data part are escaped.
- **I2: closed.** The cap is patched to a small limit. Tests assert:
  - the saved checkpoint is at or under the limit;
  - `ledger_compacted` is set;
  - the rendered head equals the render of the capped, saved pad.
- **m1: closed.** `Completer` now takes the profile and sends `temperature=None` on every request, including the repair, when `sampling_params` is false. Tests cover both the unsupported and the supported case.
- **m2: closed.** A code comment at the `complete_validated` call and the U07-77 spec note now state the precondition (the resilience ops backend must be bound).
- **m3: closed.** The orphan test now uses a table result, so a rescue cannot produce the asserted values.
- **m4: closed.** The trade-off is recorded in U07-76 spec note (d).
- **m5: closed.** The `max_output_tokens` deviation is recorded in the U07-77 spec note.

New nit (non-blocking):
- **n1** (`tests/unit/harness/memory/test_memory_compactor.py`, `test_ut07_61_no_sampling_params_means_no_temperature`): the test assigns the private `comp._profile` after construction.
  - Fix: add a `supports` override to `cs.profile()`/`cs.compactor()`. This can be a later cleanup.

## Merge-fix re-review (scratch tree .agent-tmp/T07-14-merge/tree: 38cdf4d integration 1445014 → 63e42da b2a6cdb as merged → 713731d fix)

**Verdict: Approved.** There are 0 Critical and 0 Important findings, and 1 Minor (mf1).

Environment: `TMP`/`TEMP=D:\tmp-w20s07\rv`, the tree's `.venv`, `VIRTUAL_ENV` unset. The tree was left clean after every mutation.

### Checks
1. **The patch is 713731d and the fix is minimal.**
   - `briefs/T07-14-merge-fix.patch` has the same `patch-id --stable` as `713731d` (60fc5907…), and `git apply --check -R` succeeds on the tree.
   - 63e42da matches b2a6cdb in stat (13 files, +1797/−8). Its patch-id differs only because the integration context lines differ.
   - The fix touches 6 files, +22/−13. Each ruling item:
     - **(a)** UT05-94 now asserts `sorted(hits) == ["memory/_compactor_llm.py", "roles/base.py"]`, with a U07-77 comment.
     - **(b)** ST05-21 adds `_PROMPT_DIRS` (roles and memory), sets `_EXPECTED_COUNT = 15`, adds an assert that `compaction_notes.md` is present, and the bite test is unchanged.
     - **(c)** `artifacts` adds `herness/harness/memory/prompts/*.md`.
     - **(d)** Spec notes are added under T05-21 (05-harness-core) and U07-77 (07-memory).
   - `.secrets.baseline` changes only a `line_number` (2658 → 2660, the +2 lines of the inserted 05 spec note) and the `generated_at` timestamp.
2. **Mutations (each restored with `git checkout`):**

| Mutant | Result | Killer |
|---|---|---|
| hostname `db01.corp-internal.net` appended to memory compaction_notes.md | KILLED | ST05-21 `prompts_have_no_credentials_or_urls` |
| URL `https://wiki.example.org/x` appended | KILLED | same |
| AWS access key id (bite-test shape) appended | KILLED | same |
| `api_key = ghp_…` token appended | KILLED | same |
| third module (`memory/compactor.py`) naming `"prompts/x.md"` | KILLED | UT05-94 `only_the_resolver_reads_prompts` |
| memory glob removed from wheel `artifacts` | SURVIVED (see mf1) | none; the wheel still ships the prompt |

One more plant survived: an AWS *secret* key in the documentation-example shape (`wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`). This is a pre-existing detector-scope limit that applies to the role prompts too. It is not introduced by this fix.

3. **Tests and lint.**
   - Tests: 451 passed. This covers `tests/unit/harness/roles`, `tests/security/test_st05_prompts.py`, `tests/unit/harness/memory`, `tests/fault/harness/test_memory_compactor_fault.py` and `tests/security/test_st07_compactor.py`.
   - ruff check, ruff format --check and mypy are clean on the two touched test files.
4. **Wheel.** `uv build --wheel` contains `herness/harness/memory/prompts/compaction_notes.md`.
5. **prompt_hash.** It is `ba238f457c9ee142` in two separate processes. The sha256[:16] of the prompt file inside the wheel matches. `Traversable.read_text` uses text mode, so the hash does not depend on CRLF/LF line endings.

### Findings
- **mf1 (Minor, untested belt-and-braces)** (`pyproject.toml`, `[tool.hatch.build.targets.wheel] artifacts`). Removing the memory glob fails no test, and the wheel still contains the prompt, because `packages = ["herness"]` already includes every non-ignored file under the package. The entry only guards against a future VCS-ignore rule matching `*.md`, which is the same rationale as the existing role-prompt entry.
  - Fix (optional, can be a later card): add a packaging test that checks the `artifacts` list names both prompt directories, or one that builds the wheel and asserts both prompt directories are present. Otherwise accept it as documented intent.
