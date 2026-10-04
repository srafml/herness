# T03-35 report: Security and cross-cutting tests (impl 03, tests only)

Worktree D:\herness\.claude\worktrees\agent-a7b49f6bb2be7c6b5, branch worktree-agent-a7b49f6bb2be7c6b5, base e8634a0.
Status: DONE_WITH_CONCERNS. One strict xfail pins a production defect (see below).
Commits: 538229c wip(T03-35): ST03-02, ST03-04, ST03-11 security tests green (all files). The final commit (card subject) is empty: no file changed after the checkpoint (final commit 0181960 test(enrich): T03-35 security and cross-cutting tests).

## Files added (all new; no herness/ change, no existing test touched)
| File | Lines | Marker |
|---|---|---|
| tests/integration/enrich/security/_planted.py | 154 | helper (no tests) |
| tests/integration/enrich/security/test_enrich_model_inputs_security.py | 207 | integration (ST03-02) |
| tests/integration/enrich/security/test_enrich_log_payload_security.py | 320 | integration (ST03-04) |
| tests/unit/enrich/security/test_enrich_limits_security.py | 324 | unit (ST03-11) |

There is no diff against base e8634a0 under herness/.

## Stand-in for tiny_build (carry-over)
Spec 11 `tiny_build` does not exist. Per the ledger ruling the stand-in is the T03-28 pipeline env (`tests/integration/enrich/_pipeline_env.py`). `_planted.py` patches its `_lake` and `_KEEP` before the `pipeline_env` fixture runs (`planted_env` fixture):
- Emails planted in every enrichment text field read from `core.*`: the 20 extra incidents (short description + description, also the sentinel `ZQXSENTINELTXT<id>`), change short descriptions, problem cause notes, team names (`sys_user_group`) and Jira summaries. One description also carries a `password=` credential (`CRED`, a `ZQXSENTINELCRED...` value built at run time, so detect-secrets stays quiet).
- Change `ch2` gets work times 12 h before the extra incidents on their CI. Their window pairs land in the decider band, so pair texts reach the teacher.
- `owning_team` (dynamic, core.team) and `change_caused_pair` are kept, and `change_link.use_decider` is turned on (`reconfigure`). This puts redacted team names into OpenJev and LLM requests.

Carry-over: re-point to spec 11 `tiny_build` once it exists.

## Per test ID
### ST03-02 (TH03-02), integration
- `test_st03_02_every_model_input_is_redacted_text_or_a_pair_text`: a deep `build_pipeline` job (impl 02's real handler, real `run_enrichment` stages) on the planted build. It spies every model input:
  - SpyEncoder: inputs recorded.
  - Fake Laya: `predict_batch` and `predict_shortlist` wrapped.
  - Loopback OpenJev stub: `respond` wrapped, every request body recorded.
  - Scripted LLM: every request recorded.

  Assertions:
  - The planted emails are present in `core.incident` (at least 20), so the scan is not vacuous.
  - No `text_redacted` value holds a raw value or an email, and the sentinel survives redaction.
  - Each input contains no email pattern, no planted raw value, no planted email, no planted credential and no planted domain.
  - Laya states are a subset of the `text_redacted` values.
  - OpenJev `state` values are a subset of the `text_redacted` values plus the pair texts (`pair_text` of every incident/change text, each side cut at 5,990), and at least one pair text was sent.
  - LLM `<untrusted_data>` bodies are a subset of the ticket texts plus the pair texts.
  - Encoder inputs are either a `text_redacted` value or a U03-113 mapping text. A mapping text is `<redacted name | option id>: <pieces>`, and every piece is a redacted name/summary or a prefix of a normalized `text_redacted` value.
- `test_st03_02_distill_teacher_and_candidate_inputs_are_text_redacted`: an initial distillation round in the T03-32 env. Every teacher `decide` input and every candidate Laya `predict_batch` state is a `text_redacted` value, with no email.

### ST03-04 (TH03-03), integration
- `test_st03_04_pipeline_logs_review_items_and_files_carry_no_ticket_text`: a deep run on the planted build.
  - `mapping_suggest.min_score` is set to 0 so `mapping_suggestion` items exist too.
  - The LLM echoes the untrusted block as invalid text on every third request, which exercises the repair and error paths.
  - Logging is the real pipeline: `configure_logging("INFO", log_dir=data/logs, scrubber=scrub_secrets)`, called after capsys capture starts.
  - A keyring secret (`set_secret` + `resolve`, so a known value) is planted.

  Sinks scanned:
  - stderr JSON lines and the daily log files.
  - Every review item over kinds x statuses via `list_review_items` (the test asserts ensemble_disagreement `label_check` items and `mapping_suggestion` items exist).
  - The full `ops.sqlite` `.dump` (review_item, evidence, events, jobs).
  - Every parquet/JSON file under the data dir except raw/, warehouse/ and logs/: the cache/decisions partitions, cache/pairs index, questions.json, Laya model json, and the lance version hints.
  - The job result.

  Leak terms: the email regex, planted raw values, emails and credential, the sentinel, and every `text_redacted` value of 12+ characters.

  Positive controls:
  - Logs: a planted `enrich.leak.control` line is found in stderr and in the file. An `enrich.leak.secret_control` line carrying the known secret is present but masked in both sinks.
  - Ops store: a planted `label_check` payload is found in the ops dump and in `list_review_items`.
  - Files: a planted parquet under cache/ is the only file hit.
- `test_st03_04_failed_enrichment_job_last_error_carries_no_ticket_text`: a `build_pipeline` job through the real queue (`enqueue`, `claim` with a gpu-slot owner, `run_handler`, `finish_job`). Its encoder raises an error that echoes ticket text (sentinel + email).
  - The job is `failed` with `last_error.class == FatalError`.
  - The returned error, stderr, the log files and the ops dump hold no ticket text.
  - Positive control: a job failed with a HernessError carrying the sentinel keeps it in `last_error.message` and the ops dump.
- `test_st03_04_distill_round_logs_and_review_items_carry_no_ticket_text`: a distillation round under the same logging. stderr, log files, review items (spot_check, gold), the ops dump, labels parquet/candidate files and the report hold no ticket text. Positive controls: a planted log line (stderr) and a planted review item (ops dump).

### ST03-11 (TH03-09), unit
- `test_st03_11_ten_million_char_field_truncated_to_4000`: a 10M-character field is cut to 4,000 or fewer by `normalize_text` (which is idempotent). `compose_text` returns at most 8,002 characters. `DecisionInput` refuses the 10M text (12,000 max).
- `test_st03_11_text_stage_stores_truncated_text`: the real text stage (real redaction) on two 10M fields stores a text of 4,000 < len <= 8,002.
- `test_st03_11_thousand_option_dynamic_question_shortlisted_to_64`: 1,000 active teams give `resolve_dynamic_options` 1,000 options. `to_wire_questions` refuses it (> 255). `shortlist_options` keeps the 64 most similar (checked against an independent ranking, fingerprint unchanged), and the wire accepts 64.
- `test_st03_11_static_question_above_255_options_refused`: 256 static options raise a ValidationError; 255 are accepted and sent on the wire.
- `test_st03_11_million_record_queue_capped_at_150000`: 1M queued rows (DuckDB `range`) give `escalation_queue` at the config cap exactly 150,000 items, newest scoring record first.
- `test_st03_11_million_deferred_items_capped_at_20000_llm_records`: a lazy 1M-item `Sequence` goes to `run_llm_escalation` with cap 20,000. A counting stub LLM sees exactly 20,000 items (it fails fast past the cap) in chunks of at most 500, and the cache holds 20,000 keys.
- `test_st03_11_nightly_caps_match_the_spec`: defaults are 150k / 20k / 300k / 30k / 500 naming calls / 500 disagreement reviews.
- STRICT XFAIL `test_st03_11_llm_decider_asks_at_most_64_of_1000_dynamic_options` (see defect below).

## Production defect pinned (strict xfail, not fixed)
- Owner: T03-21 (Decide stages). This is a spec gap: U03-19 `shortlist_options` has no caller in herness/ or in the spec flows. Design 03 section 5.5 and spec section 12 row 4 ("owning_team with > 255 teams -> shortlist to 64") are not wired.
- What happens: a dynamic `owning_team` question with more than 255 teams reaches the deciders unshortlisted.
  - LLM decider: all 1,000 options go into the response schema and the prompt. The vote's 1,000-entry distribution then fails `Answer` validation (<= 255) with an uncaught pydantic ValidationError, which aborts the whole `decide` call.
  - OpenJev / hosted Jev: `to_wire_questions` raises ConfigError for the request.
- Impact: the shipped config/decisions.yaml has `owning_team` (core.team), so a customer with more than 255 active teams would hit this. With `--runxfail` the test fails on `1000 <= 64` and the ValidationError.

## Mutation probes (guard removed in herness/, test red, restored; final tree clean)
| # | Mutation | Test | Result |
|---|---|---|---|
| P1 | text stage: `redact_table` bypassed (`out = tbl`) | ST03-02 every_model_input | RED (ST03-04 pipeline stays green, as expected: it scans sinks, not redaction) |
| P2 | mapping_suggest `_redact` returns input unredacted | ST03-02 every_model_input | RED |
| P3 | `_pipeline_stages.prepare`: dynamic option names unredacted | ST03-02 every_model_input | RED |
| P4 | `_distill_steps._inputs` text not the `text_redacted` value | ST03-02 distill | RED |
| P5 | `_build_stages._hook_errors` message echoes the exception | ST03-04 failed job | RED |
| P5b | `jobs.handlers.run_handler` message echoes the exception | ST03-04 failed job | survived: impl 02's `_hook_errors` wraps first (defence in depth; P5 pins the first guard) |
| P6 | `scrub_secrets` known-value masking off | ST03-04 pipeline (secret control) | RED |
| P7 | ensemble disagreement payload carries the ticket text | ST03-04 pipeline | RED |
| P8 | `normalize_text` without the 4,000 cut | ST03-11 x2 | RED |
| P9 | jev_wire > 255 guard removed | ST03-11 shortlist | RED |
| P10 | `shortlist_options` without the `k` cut | ST03-11 shortlist | RED |
| P11 | static option bound 255 -> 1000 | ST03-11 static | RED |
| P12 | escalation_queue `LIMIT $max_records * 10` | ST03-11 1M queue | RED |
| P13 | `run_llm_escalation` cap x10 | ST03-11 1M deferred | RED |

## Gates / results (PYTHONUTF8=1, TMP=TEMP=C:\Users\santh\AppData\Local\Temp\w29-s03)
- `uv run pytest -m "unit or integration" -k ST03 -q -p no:logging`: 108 passed, 1 xfailed (35 s).
- `uv run pytest tests/unit/enrich/security tests/integration/enrich -q -p no:logging`: 152 passed, 1 skipped (laya parity, needs CUDA), 1 xfailed.
- `ruff check .`: pass. `ruff format --check .`: pass (1041 files).
- `uv run mypy --explicit-package-bases <4 new files>`: no issues. Plain `mypy <files>` stops at "source file found twice" because the test dirs have no `__init__.py`, the same as existing tests.
- detect-secrets hook on the new files: pass, no baseline change.
- Hooks on the wip checkpoint (incl. the pytest-unit hook): pass.

## Concerns / carry-overs
1. Strict xfail (owner T03-21, spec gap U03-19 has no caller): see above.
2. tiny_build re-point (spec 11) for ST03-02 / ST03-04.
3. The existing `test_enrich_text_security.py` docstring still names the spy decider / encoder half as a carry-over. It was left untouched per rules, and that half is now covered by `test_enrich_model_inputs_security.py`.
4. Lance vector data files (`vectors/*.lance` data) are binary and not text-scanned. Only their JSON version hints are scanned. The ticket_embedding table holds vectors and hashes.
5. ST03-04 pipeline assertions on review-item kinds depend on the deep run creating ensemble disagreements and on `min_score=0` creating suggestions. They are deterministic on the stand-in today.
6. The final card-subject checkpoint is empty (all content landed in 538229c).

## Fix round 1 (base 0181960; tests only, same 4 files)
- I-1: the strict xfail reason now starts with "T03-21b:". It states the defect: shortlist_options (U03-19) has no caller, a > 255-option dynamic question reaches the deciders unshortlisted, and the LLM decider's Answer validation fails at herness/enrich/deciders/llm.py:184.
- I-2: ST03-04 now also matches cut fragments.
  - `_planted.shingles` / `fragments` match every 32-character window of each line of the stored ticket texts (`WINDOW = 32`).
  - Windows with fewer than 16 characters outside redaction placeholders `[TYPE_hex]` are skipped, so placeholder-only strings cannot false-positive.
  - `_leaks` combines the old whole-value scan with fragment matching.
  - New positive control: a 60-character fragment of a stored ticket text (no sentinel, email or placeholder) is logged at INFO. Whole-value matching alone misses it (asserted). It is caught in stderr and in the log files.
  - No false positive in any real sink: all ST03-04 tests are green.
- M-1: the chunk bound is asserted against the literal 500 (LLM_CHUNK import removed).
- M-2: the xfail declares `raises=ValidationError`. The try/finally is gone and all assertions run after `decide`, so an AssertionError fails the xfail instead of satisfying it. With `--runxfail` it fails with the pydantic ValidationError for Answer.
- M-3: the distill test now:
  - checks the planted log line in the log files too;
  - checks stderr and log files via the same control-free filter;
  - adds a lake-file positive control: a parquet holding a 40-character head of the longest stored text, next to the labels parquet, is the only file hit.
- M-4 parked (moves with the tiny_build re-point).

Re-probes (herness/ mutated, test run, restored; tree clean):
| # | Mutation | Test | Result |
|---|---|---|---|
| F1 | text stage logs the first 60 chars of a redacted ticket at INFO | ST03-04 pipeline | RED (was green before the fix) |
| F2 | ensemble disagreement payload carries a 60-char text fragment | ST03-04 pipeline | RED |
| F3 | distill `_inputs` logs a 40-char teacher-input head at INFO | ST03-04 distill | RED |
| M1 | LLM_CHUNK 500 -> 501 | ST03-11 1M deferred | RED (assert 501 <= 500) |

Results:
- `-k ST03`: 108 passed, 1 xfailed.
- `tests/unit/enrich/security` + `tests/integration/enrich`: 152 passed, 1 skipped, 1 xfailed.
- `mypy --strict --explicit-package-bases` on the 4 files: clean.
- ruff format and ruff check: clean.

Fix round 1 commit: d456350 test(enrich): T03-35 fix round 1 (hooks passed, NO SKIP).
