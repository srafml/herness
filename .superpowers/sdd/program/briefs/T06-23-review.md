# T06-23 review (Chat support), reviewed commit 8218ae1 (base 7654d99)

**Task quality: Needs fixes** (1 Important, 3 Minor)

### Spec Compliance
- U06-126 MODE_MESSAGES / CHAT_TOOLS / EGRESS_NOTICE: ✅ all four banners and the egress notice match the spec character for character. The full tool list is in spec order, `cloud` is the full list minus get_record/get_cluster/semantic_search with order kept, and `defer` is `()`. The mappings are read-only `MappingProxyType`.
- U06-130 ObservedTool: ❌ one partial deviation (Important 1). Otherwise ✅: name/description/input_schema are passed through as properties. Sync tools run through `asyncio.to_thread` and async tools are awaited. Events come out in order: ToolEvent(name, first query id or None, result.ok), then one EvidenceEvent per new query id. A raised error emits ToolEvent(ok=False) and re-raises. The class conforms to `AsyncTool`: a mypy assignment `t: AsyncTool = ObservedTool(...)` passes and the runtime `isinstance` check is True (probe run from the scratchpad).
- U06-131 has_review_intent: ✅ all three intent regexes, the ranking regex, the plural-noun list and the kind regex (`fund|invest|epic|initiative|candidate` or candidate entities) match the spec. The ≥ 3 entities rule counts ids across every type. `detect_entities` is out of scope (already ruled: blocked:EntityDirectory).
- U06-132 trim_failing_claims: ✅ steps 1-5 are all implemented: failing ids = checks that are not `match` plus unknown markers, the split regex is verbatim, marker and uncited-overlap sentences are dropped, orphaned NumberRefs are dropped, query_ids are recomputed, and an empty result falls back to the fixed sentence.
- U06-133 chunk_text: ✅ concatenation equals the input, each chunk is ≤ size, the break falls after the last whitespace in the window and is a hard cut when the window has none. The function is pure.
- UT06-86: ✅ covers funding, org, plain→False, the ranking rules and candidate entities. The entity cases wait on detect_entities (ruled). UT06-87 ✅ · UT06-88 ✅ · UT06-89 ✅ · UT06-90 ✅ (one tool event, two evidence events, order kept; the dump goes through `CHAT_EVENT_ADAPTER.dump_python` without `exclude_defaults`, so `type` survives).
- ⚠️ Cannot verify from diff:
  - The offsets of `v.items[*].uncited` relative to `answer.text`. They are right if T05-25 follows U05-67 (spec 05 ~l.1350: `verify_answer` builds one item with `text = answer.text`). No verifier exists in the tree yet.
  - Spec note for the controller: U06-129 step 5e calls `has_review_intent(question)` with one argument, while U06-131 defines two (`question, entities`). T06-24/25 must pass `detect_entities(...)` output, or `{}` until EntityDirectory is resolved.

### Gates (re-checked)
- 19 passed; chat_support.py 114 stmts / 26 branches at 100% line and 100% branch (≥ 90/85).
- ruff check and ruff format clean; mypy clean.
- lint-imports: 10 kept, 0 broken. tools/check_type_ownership.py exits 0.
- Size: 218 lines against a budget of 300.
- Every test has an ID-prefixed name and a docstring starting with its UT id; `pytestmark = pytest.mark.unit` is set.

### Builder concerns, judged
- Evidence dedup per instance vs "per turn": **not accepted**, see Important 1.
- trim_failing_claims reading `v.items[*].uncited` instead of calling `find_uncited`: **accepted.** U05-67 gives one item whose text is `answer.text`, so the offsets line up. It also avoids re-deriving the allowed patterns, which the signature does not carry.
- query_ids recomputation (drop only the ids referenced solely by dropped numbers; keep ids not tied to any number, in order): **accepted** as a reasonable reading of "recompute". See Minor 3 for one edge.
- Catching `Exception`, not `BaseException`: **accepted.** Cancellation (CancelledError) means the turn is being stopped, so emitting no ToolEvent is correct. Every HernessError is an Exception.
- chunk_text raising ValueError for size < 1: **accepted, and needed.** With size 0 the loop would never advance (the window is empty, so cut = 0 and end = start).

### Strengths
- Small and pure, with verbatim constants.
- `_TEXT_TOOLS` derives the cloud set instead of copying the list, so the two cannot drift apart.
- The tests pin exact strings, event order, the adapter's `type` discriminator, the sync and async paths, the raise path and the trim edge cases (unknown marker, uncited span, everything removed, trailing whitespace).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **Evidence dedup is per ObservedTool instance, but the spec requires it per turn.** herness/harness/pipelines/chat_support.py:81-89 and :115-118.
   - U06-130 says "one EvidenceEvent(q) per query_id not emitted before in this turn".
   - U06-129 step 5c builds one ObservedTool per tool in `CHAT_TOOLS[mode]`, so a turn holds up to 12 instances, each with its own `_seen`. The same query id coming back from two different tools in one turn (for example `run_sql`, then `get_metric` or `list_findings` citing the same recorded query) emits a duplicate EvidenceEvent.
   - The docstring ("One instance serves one chat turn") contradicts that usage, and nothing in T06-24's units tells ChatService to dedup in `emit`, so the guarantee would silently disappear.
   - Fix: add a keyword-only `seen: set[str] | None = None`, defaulting to a private set, that ChatService shares across the turn's wrappers. Fix the docstring and add a UT06-90 case with two wrappers sharing `seen`. This is a compatible extension of the spec signature.

#### Minor (Nice to Have)
1. chat_support.py:187: `" ".join(kept)` flattens the layout of the kept text. The split regex `(?<=[.!?])\s+` eats paragraph breaks and newlines before list items, so a probe of `"Intro [[n1]] here.\n\nBad [[n2]] one.\n\n- bullet [[n1]] again."` returned `'Intro [[n1]] here. - bullet [[n1]] again.'`. A `partial` chat answer loses its markdown structure. Suggest keeping the original separator that followed each kept sentence (the split already has the offsets).
2. chat_support.py:122: sync vs async is detected with `inspect.iscoroutinefunction(inner.__call__)`. This is right for both protocols as defined (`async def __call__`). A callable that returns an awaitable without being `async def` (a decorator-wrapped `__call__`, for example) would be sent to a thread and hand back an un-awaited coroutine. Consider checking `inspect.isawaitable` on the result, or leave a comment on the assumption.
3. chat_support.py:191-196: when every sentence is dropped, the text becomes `NO_VERIFIED_ANSWER` but `query_ids` can still list queries that no number referenced. Harmless as evidence links, but consider emptying `query_ids` when the fallback text is used. Optional.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Constants, intent rules, trim and chunking match the spec, and every gate passes (100% coverage). The one gap is ObservedTool's evidence dedup: the spec requires it per turn, but it only works per wrapper instance. Under ChatService's one-wrapper-per-tool wiring this emits duplicate EvidenceEvents, and a small optional shared `seen` set fixes it.

---

## Re-review round 1 (fix commit 2fd84bb, scope `git diff 8218ae1..2fd84bb` plus a full re-check of the regenerated brief)

**Task quality: Approved**

### Findings from round 0
- **Important-1 (evidence dedup per turn): fixed.**
  - chat_support.py:87-96: `__init__(inner, emit, *, seen: set[str] | None = None)` keeps the caller's set when one is given (no copy) and otherwise creates a private one. The docstring now describes the per-turn contract.
  - New test `test_ut06_90_wrappers_sharing_seen_emit_each_query_id_once_per_turn`: a sync wrapper and an async wrapper share one set, Q2 is emitted as evidence once, and the order is exact.
  - The keyword-only extra argument is a compatible extension of the spec signature. T06-24 must pass one set per turn.
- **Minor-1 (layout flattening): fixed.**
  - `_sentences` now returns the separator that follows each sentence. `_join` keeps the original separators between kept sentences; when sentences between two kept ones are removed, the separator with the most line breaks wins, with the first one kept on a tie.
  - Dropped sentences at the start or end leave no stray separator, and empty sentences from trailing whitespace are still skipped.
  - The new paragraph and bullet test pins the exact output. The split regex is still the spec's.
- **Minor-2 (awaitable detection): fixed.**
  - `async def __call__` is awaited directly. Any other tool runs through `to_thread`, and an awaitable it returns is then awaited on the loop. That is safe for coroutine objects, which bind to no loop until awaited.
  - A return value that is not a `ToolResult` raises TypeError inside the `try`, so it emits ToolEvent(ok=False) and re-raises, consistent with U06-130. Two new tests cover this.
- **Minor-3 (fallback query_ids): fixed.** The fallback text now sets `query_ids = []`, and the test adds an unrelated Q3 that is dropped.

### Full re-check against the regenerated brief (HEAD 2fd84bb)
- ✅ U06-126: all four banners, `EGRESS_NOTICE` and the three tool sets are verbatim (no change in this round).
- ✅ U06-130:
  - name, description and input_schema are passed through; sync tools go through `asyncio.to_thread`.
  - ToolEvent(name, first query id or None, result.ok) comes first, then one EvidenceEvent per query id not seen before in the turn (now enforceable through the shared `seen` set).
  - A raised error emits ok=False and re-raises. The class conforms to `AsyncTool`.
  - The `seen` keyword argument is extra but compatible.
- ✅ U06-131 `has_review_intent`: the regexes, the ≥ 3 entities or plural-noun rule and the kind rule are verbatim.
  - `detect_entities` stays blocked. The regenerated brief still only mentions `EntityDirectory` in the U06-131 signature and algorithm ("loaded once per build (core.team, core.service, score.funding, core.work_item.key)"). It gives no fields, owner or loader, so the ruling is unchanged.
  - The UT06-86 "entities" action is covered only through the `entities` argument of `has_review_intent`.
- ✅ U06-132: steps 1-5 are implemented, with the split regex verbatim, orphaned NumberRefs dropped, query_ids recomputed and the fallback sentence verbatim. The function stays pure.
- ✅ U06-133: the invariants hold (no change in this round).
- ✅ UT06-86: funding, org and plain→False.
- ✅ UT06-87: one failing marker, the sentence is removed and the number dropped.
- ✅ UT06-88: long text, the chunks concatenate to the input and are ≤ 64.
- ✅ UT06-89: `cloud` lacks the text tools and `defer` is empty.
- ✅ UT06-90: one tool event, two evidence events, order kept.
- No mismatches found.

### Regressions and gates (re-run by me at 2fd84bb, tree clean)
- 23 passed; chat_support.py 134 stmts / 40 branches at 100% line and 100% branch.
- ruff check and format clean; mypy clean.
- lint-imports: 10 kept, 0 broken. check_type_ownership exits 0.
- Size: 255 lines against a budget of 300.
- None of the round-0 tests changed behaviour. The fallback test's only change is the added Q3.

### Remaining findings
None: no Critical, Important or Minor findings. Carried forward for the controller (not findings against this card): the U06-129 step 5e call `has_review_intent(question)` uses one argument against the two-argument U06-131 signature, and EntityDirectory is still unspecified.

**Reasoning:** Every round-0 finding is fixed with a targeted test, the unit and test rows of the regenerated brief match HEAD, and every gate passes with no regressions.
