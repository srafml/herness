# T03-15 review — LLM decider and prompts (verify)

Reviewed: worktree agent-a1ec505ecf7351f42, 55f2c48..615b7d2 (wip 06a1c35 + empty card-subject commit). Read-only; mutants run in a scratch copy (D:\tmp-herness-verify-T03-15, deleted afterwards). Worktree left clean at 615b7d2.

### Spec Compliance
- ✅ Spec compliant (with accepted deviations below)

| Row | Status | Evidence |
|-----|--------|----------|
| U03-60 CompletionClient | ✅ | llm.py:70-78 runtime-checkable Protocol `name`, `complete`, `acomplete`; types from herness.core.types; no herness.harness import |
| U03-61 vote_schema | ✅ | llm.py:89-106 exact postcondition shape; labels choice/bool/score per spec |
| U03-62 vote_distribution | ✅ | llm.py:109-121 `(n+0.5)/(k+0.5K)`, label order, out-of-labels → OutputValidationError("vote outside labels") |
| U03-63 LlmDecider | ✅ | asyncio.run + Semaphore(max_concurrency) (llm.py:236-261); request fields per step 2 (llm.py:270-286); aretry_call("llm_local", complete_validated, client, req, max_repairs=2, breaker_key="decider:llm") (llm.py:295-302); only OutputValidationError drops a vote (llm.py:303-311); argmax ties → first label (llm.py:178); 0 valid → item error (llm.py:263-266); backend_confidence None |
| U03-64 health | ✅ | llm.py:326-344 1 token, temperature 0, call_with_timeout(…, 30.0), any Exception → ModelUnavailable("llm health") |
| Prompt files | ✅ | enrich_decider.md: `## System` header + 5 paraphrases, standing untrusted-data instruction; cluster_namer.md prompt only (call site is U03-102) |
| UT03-58 | ✅ | test_llm_decider.py:61 |
| UT03-59 | ✅ | test_llm_decider.py:70 (mixed bool/choice/score, enums, additionalProperties false at both levels) |
| UT03-60 | ✅ | test_llm_decider.py:96, :105 |
| UT03-61 | ✅ | test_llm_decider.py:132 (3 votes, 1 invalid after 1+2 repairs, distribution from 2 votes, 5 requests), :153 request fields, :173 paraphrase/questions/block, :190 text only in block, :210 prompt secret scan, :292 record_id escaping, :305 AuthError propagates |
| UT03-62 | ✅ | test_llm_decider.py:315 all invalid → item error; :334/:342 health failure/timeout → ModelUnavailable |
| PT03-06 | ✅ | test_llm_decider.py:111-126 hypothesis over K 2..12 unique labels, k 1..25 votes; asserts sum≈1, every label > 0 and the label set (no-smoothing mutant killed) |
| ST03-01 | ✅ | security/test_llm_decider_security.py:59 injected out-of-enum answer rejected by the real complete_validated (9 calls = 3×(1+2)), votes dropped, item error, text only inside the block and never in system; :87 `</untrusted_data>` → `&lt;/untrusted_data>` (any case), exactly one closing delimiter |
| Budgets | ✅ | llm.py 344/350; enrich_decider.md 61/80; cluster_namer.md 32/50 |
| Coverage | ✅ | re-run: llm.py 167 stmts / 26 branches, 100 % / 100 % (43 passed: card tests + UT03-45 table) |
| Test hygiene | ✅ | IDs in every test name, pytestmark unit, docstrings; security test under tests/unit/enrich/security/ with marker unit |

Focus checks verified:
- Prompts are files: all instructions live in the .md files; code carries only scaffolding ("Questions:", "Ticket:", bullet format, health ping "Reply with OK."). Accepted (deviation 6).
- Schema path is real: complete_validated validates `parsed`/decoded text with jsonschema Draft 2020-12 against req.response_schema (chain.py:77-97, 155-188); an out-of-enum answer fails `enum` and after 2 repairs raises OutputValidationError, a RecoverableError: not retried (`_should_retry` retries RetryableError only) and not counted by the breaker (breaker.py:224-228 counts only SourceUnavailable/ModelUnavailable), so invalid votes cannot open `decider:llm`. ModelUnavailable/CircuitOpen/AuthError/EgressBlocked propagate (llm.py:306-307), matching the §3.9 shared rule for DeciderChain.
- No ticket text in logs/errors: vote_dropped logs decider/record_id/vote index; item_failed logs decider/error_class; complete_validated's messages and repair records carry JSON pointers + constraint names only; ConfigError names only the prompt file name.
- R-20 wrapping: text escaped before wrapping (llm.py:147), record_id html-escaped with quote=True so it cannot break the attribute (llm.py:148); both tested and mutant-checked.
- Concurrency: ≤ max_concurrency items in flight; votes of one item run sequentially inside the slot, so ≤ max_concurrency model calls in flight. Correct by inspection; untested (m1).
- Health semantics match U03-64 (sync `complete` on call_with_timeout's daemon thread; timeout → ModelUnavailable, rewrapped as "llm health").

Deviations judged:
1. run_id sentinel `"run_"+"0"*26`: accepted — RequestMeta.run_id is `str`; same sentinel as herness/eval/judge.py:39. Needs a spec note to U03-63.
2. max_output_tokens 2048 / timeout_s 120 constants: accepted (LLMRequest requires them; spec silent). Later source from the client profile via the composition root.
3. vote_schema → dict[str, JsonValue]: accepted (narrowing of `dict[str, object]` required by LLMRequest.response_schema).
4. `samples` property: accepted — DecisionOutput has no samples field; "outputs record samples = votes" is met by the caller passing decider.samples.
5. ModelRefused propagates: accepted. A plain spec 05 LLMClient returns a refusal as stop_reason (U05-25), which then fails validation and drops the vote; ModelRefused is raised only by the harness GatedClient, which enrichment does not use.
6. Scaffolding strings in code: accepted (not instructions).
7. Public load_prompt: accepted (small Extra export; reuse by U03-102).
8. Dynamic option names (options_source team/service) rendered outside the untrusted block: spec-literal (U03-63 step 2 puts labels with descriptions in the question list, and they are also the enum). Residual TH03-01 risk from source-system names — carry-over, not a finding for this card.

- ⚠️ Cannot verify from diff / not re-run: import-linter and full pre-commit (builder report: lint-imports 13 kept 0 broken, hooks green, no SKIP; no herness.harness import by inspection); behaviour against a real spec 05 client (FakeLLMClient is test-local, U11-42 carry-over per ruling).

### Strengths
- Votes run through the real aretry_call → complete_validated → repair loop in tests (jev_env), so "invalid after repairs" and the 1+2 call counts are real, not simulated.
- The error split is exactly what DeciderChain needs: only the per-vote validation failure is local; backend-level errors raise.
- Hardening beyond spec is sound and tested: case-insensitive delimiter escaping, attribute-escaped record_id, prompt-file loader that fails fast on shape.
- Mutants killed by the card tests (scratch copy): delimiter escape removed, case-sensitive escape, record_id unescaped, formula 1.0·K, no smoothing, reversed tie-break, max_repairs=0, direct client.acomplete instead of aretry_call/complete_validated, policy name changed, health max_output_tokens=2, bool votes accepted, item error raised instead of returned.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- m1 Concurrency bound untested: removing `async with gate` (llm.py:260) or using `Semaphore(1000)` (llm.py:242) survives all card tests. Add a test with a fake whose acomplete awaits and records the peak in-flight count (max_concurrency=2, 5 items → peak ≤ 2).
- m2 Only AuthError propagation is tested (test_llm_decider.py:305). Mutant `isinstance(err, OutputValidationError | ModelUnavailable)` at llm.py:306 survives — a regression turning model outages into dropped votes/item errors (DeciderChain would never fall back) would go unnoticed. Parametrize over ModelUnavailable (after llm_local retries), CircuitOpen and EgressBlocked.
- m3 No test that ticket text never reaches logs: adding `text=item.text` to the vote_dropped log (llm.py:308-310) survives. A log capture in UT03-61/ST03-01 asserting the injected text is absent would pin it.
- m4 breaker_key untested: `breaker_key=None` (llm.py:301) survives. Cheap to pin with a pre-opened `decider:llm` breaker → CircuitOpen, or accept as reviewed by inspection.
- m5 cluster_namer.md:14 shows the delimiter as `record_id=""`, while R-20 allows the example's id and U03-102 may pass real ids; reword to `record_id="..."` (as enrich_decider.md:17) when U03-102 lands.
- m6 llm.py:41 imports the private `_asked` from openjev.py (jev_hosted precedent); consider a shared public helper later. llm.py is at 344/350, little headroom.

### Assessment
**Task quality:** Approved
**Reasoning:** All units, prompt files and card tests (UT03-58..62, PT03-06, ST03-01) meet the spec; votes are genuinely validated through complete_validated's schema path with the correct error split, R-20 wrapping is correct and hardened, coverage 100/100 and budgets met. Remaining items are test-strength gaps (concurrency bound, non-Auth propagation, log hygiene, breaker key) that are correct by inspection; m1+m2 are cheap and worth a fix round if the controller wants one.
