# T03-21b build report

Status: DONE (commit SHA in the reply).

## Design
- New private sibling `herness/enrich/deciders/_shortlist.py` (68 lines, section 2 row added, budget 100): `asked_for` (the shared questions-asked rule, moved out of openjev.py `_asked`; ensemble.py now imports it) and `shortlist_asked(items, questions, embed_fn)` returning per-item asked tuples; every choice question with > 255 options is replaced per record by `shortlist_options(q, text_vec, option_vecs, k=64)`. Option vectors are embedded once per question per decide call from `"<label>: <description>"` (sorted labels); text_vec = `embed_fn([item.text])[0]`. <= 255 options: embed_fn never called. A wide question with embed_fn None raises ConfigError ("... more than 255 options ..."); embed errors propagate. All raise before any client/HTTP call (it runs first in `_adecide`).
- LlmDecider and `_JevHttpBackend` (hence OpenJevDecider and JevHostedDecider) accept `embed_fn`. OpenJev needed no regrouping: `_one` already sends one request per item with its own asked tuple, so each wire carries its own shortlist.
- `build_decider` passes `embed_fn` to llm/openjev/jev; `_pipeline_stages._teachers` passes `_embed_fn(run)` to openjev/jev and to the llm build.

## Line counts vs budgets
llm.py 350/350, openjev.py 316/320, deciders/__init__.py 89/90, ensemble.py 200/200, jev_hosted.py 162/200, _pipeline_stages.py 390/390 (hard limit 400), _shortlist.py 68/100. check_module_size exit 0.

## Tests
- ST03-11 `test_st03_11_llm_decider_asks_at_most_64_of_1000_dynamic_options`: xfail marker removed; setup only (`embed_fn=_fake_embed`, helper `_fake_embed`, `import zlib`); also deleted the now-stale "expected failure" comment above the decide call. Docstring, item, reply and assertions verbatim.
- New tests/unit/enrich/test_decider_shortlist.py (7 tests, ST03-11 IDs): LLM schema 64 + embed once + per record; LLM 255 unchanged with no embed call; LLM wide without embed_fn -> ConfigError, 0 requests; LLM embed raising -> no request; OpenJev wire 64 criteria per record (2 records, different texts); OpenJev 255 unchanged + wide without embed_fn ConfigError and no HTTP; narrow questions never touch embed_fn.
- tests/unit/enrich/test_decider_registration.py: `test_ut03_67_embed_fn_reaches_llm_openjev_and_jev` (build_decider wiring).

## Mutation evidence
Replaced the `shortlist_asked(...)` call in llm.py with the plain asked list: ST03-11 LLM test FAILED (1 failed); reverted (diff shows only intended changes).

## Results
`tests/unit/enrich tests/fault/enrich`: 1103 passed, 1 skipped (symlink privilege). `-k ST03_11`: 8 passed, 0 xfailed. ruff check/format clean (repo), mypy clean (herness/enrich + touched tests), lint-imports 15 kept / 0 broken, check_module_size 0, check_type_ownership 0.

## Deviations / concerns
- `_asked` moved from openjev.py into `_shortlist.py` as `asked_for` (openjev budget); ensemble.py import updated.
- `_embed_fn(run)` loads the CPU encoder inside each call (existing behaviour); with a wide question each decide chunk re-embeds the options once. Could be cached by the caller later.
- `_pipeline_stages.py` 390/390 and `llm.py` 350/350: no headroom.
- Option description is used as resolved (already redacted by resolve_dynamic_options); not re-normalized as mapping_suggest does.
