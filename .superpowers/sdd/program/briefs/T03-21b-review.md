# T03-21b verify review (head 0fb7566, base 10f9733)

Verdict: Approved

## Spec
- 1 ST03-11 test: xfail marker removed; diff vs base is setup only (import zlib, `_fake_embed`, `embed_fn=` kwarg); docstring/item/reply/assertions verbatim. Deleted comment ("expected failure ... ValidationError") was stale once the marker went; deletion is correct. `-k ST03_11`: 8 passed, 0 xfailed. OK
- 2 Mutation (`if not wide:` -> `if True:` in _shortlist.shortlist_asked, wide questions pass through): 5 failed - ST03-11 test_st03_11_llm_decider_asks_at_most_64_of_1000_dynamic_options, test_st03_11_llm_schema_lists_64_shortlisted_options, ..._llm_wide_question_without_embed_fn_fails_closed, ..._llm_embed_failure_fails_closed, ..._openjev_wire_carries_64_criteria_per_record. 121 others passed. Reverted. OK
- 3 Fail-closed: shortlist_asked runs first in `_adecide` of LLM and `_JevHttpBackend` (before client factory/open). Tests cover LLM (None, raising) and OpenJev (None). Hosted Jev not covered by a dedicated test; my scratch probe (client_factory that raises, embed None and embed raising) passed for JevHostedDecider: ConfigError / RuntimeError, client never built. Probe removed. OK (see Minor 1)
- 4 <=255 unchanged, embed not called (LLM and OpenJev tests, narrow-question test); k=64; text_vec = embed_fn([item.text])[0] per record; option vectors embedded once per wide question per decide call (assert one 300-text call across two records); ensemble.py imports asked_for correctly (ensemble tests pass); build_decider passes embed_fn to llm/openjev/jev; _teachers passes _embed_fn(run) to openjev/jev and llm. OK
- 5 Test IDs: all new tests carry ST03-11 / UT03-67 in name; `--collect-only --require-test-ids` over touched files: 126 collected, no UsageError. OK
- 6 check_module_size exit 0; section 2 row for _shortlist.py present, well-formed (path, purpose, symbols, L3, deps, budget 100; file is 68 lines); lint-imports 15 kept / 0 broken; ruff check, ruff format --check, mypy (51 files) clean; check_type_ownership exit 0. OK
- 7 .secrets.baseline: only docs\impl\03 entry line 3633 -> 3634 and generated_at; no dropped entries; 0 CR bytes (LF). OK
- 8 tests/unit/enrich + tests/fault/enrich: 1103 passed, 1 skipped. OK

## Findings
Critical: 0
Important: 0
Minor: 2
1. tests/unit/enrich/test_decider_shortlist.py: no dedicated hosted-Jev (JevHostedDecider) fail-closed test; it shares _JevHttpBackend._adecide with OpenJev (tested) and my probe confirmed behaviour, so risk is low.
2. herness/enrich/deciders/llm.py (~line 57): builder also rewrote the `_NO_RUN` two-line comment into one trailing comment (budget-driven, 350/350); unrelated to the card but harmless. llm.py and _pipeline_stages.py (390/400) have zero headroom.

Warnings: embed_fn encoder is re-loaded per call (builder noted); option vectors re-embedded per decide chunk. Performance only.

Cleanup: probe test file deleted, mutation reverted via git checkout of the one file; git status clean, HEAD 0fb7566.
