# T03-35 review (verify, opus): Security and cross-cutting tests

Worktree agent-a7b49f6bb2be7c6b5, base e8634a0, head 0181960. Verdict at the bottom.

### Spec Compliance
- OK 1. No production change: git diff --stat e8634a0..HEAD -- herness/ is empty. 4 files added (A), no existing test function or assertion edited.
- OK 2. Sinks are scanned with the real herness.core.redact (text stage) and the real secrets.scrub_secrets known-value path (set_secret+resolve, masked control). Every listed sink went red when I planted a leak through production code: logs (stderr and file), events (resilience_event), error message, job last_error, lake files, review payloads, mapping payload, job result. Evidence: enrichment writes no evidence rows (only evidence_counts in the mapping payload). The full ops .dump covers the table anyway. Exception: partial or cut ticket text survives. See I-2.
- OK 3. 26 own probes, table below. 24 red, 1 survivor on a guard (G14 LLM_CHUNK, see M-1), 1 survivor on a sink (S1c, see I-2). S8 could not be planted: the type refuses it.
- FAIL 4. Strict xfail. The defect reproduces on the base code: with --runxfail the pydantic Answer.distribution "at most 255 items, not 1000" error is raised at herness/enrich/deciders/llm.py:184, and assert 1000 <= 64 fails. It fails for the stated reason, not because of a test bug. strict=True is set, and no other ST03-11 assertion is weakened around it. But the reason string names "T03-21", not T03-21b as the controller ruled (I-1).
- OK 5. IDs, docstrings and pytestmark follow the global constraints. ST03-02/04 are integration and ST03-11 is unit, per section 11. Two -k ST03 runs gave the same result, 108 passed + 1 xfailed (45 s and 29 s). The enrich security and integration dirs gave 152 passed, 1 skipped (laya CUDA) and 1 xfailed. The slowest ST03-11 test takes 1.5 s, and the 1M queue is lazy (DuckDB range / a lazy Sequence). No network and no real LLM.
- OK 6. mypy --strict --explicit-package-bases on the 4 files reports 0 issues. ruff check and ruff format --check are clean on them.
- WARN Not verifiable here: whether tiny_build (spec 11) would plant in other fields. The stand-in is a ledger ruling and stays a carry-over.

### Strengths
- Each ST03-04 sink has a positive control, so the scan is not vacuous. The known secret is checked masked in both log sinks.
- ST03-02 spies all four model families (encoder, Laya, OpenJev, LLM) and the distillation teacher and candidate. It checks set inclusion against text_redacted and pair texts, not just an email regex.
- ST03-11 uses real functions with lazy 1M inputs and an independent ranking check for the shortlist.

### Issues
#### Critical (Must Fix)
- none

#### Important (Should Fix)
- I-1 tests/unit/enrich/security/test_enrich_limits_security.py:184: the xfail reason says "T03-21". Per the controller ruling the follow-up card is T03-21b, so the reason must name T03-21b, for example "T03-21b (spec gap: U03-19 shortlist_options has no caller) ...".
- I-2 tests/integration/enrich/security/test_enrich_log_payload_security.py:108-111 (_leaks) and tests/integration/enrich/security/_planted.py:117-124 (scan): ticket text is detected only by whole-value containment of text_redacted values (>= 12 chars), the sentinel or an email. A cut fragment of ticket text survives, which is the most realistic leak shape: event target is cut to 200 chars (events.py STRING_MAX_CHARS), last_error to 2,048, log values get truncated. Probe S1c logged left(text, 60) of a redacted ticket at INFO, and the test stayed green. The sentinel only catches fragments that include the end of the description of an extra incident. Fix (tests only), either:
  - match on shingles, for example any 32-char window of a text_redacted value, or a set of per-ticket distinctive tokens;
  - plant a sentinel at the start of every planted text too (short description), and assert the fragment case with a positive control.

#### Minor (Nice to Have)
- M-1 test_enrich_limits_security.py:311: max(llm.chunks) <= LLM_CHUNK compares against the imported constant. Probe G14 (LLM_CHUNK = 501) survived. Assert against the spec literal 500 (TH03-09 batch caps).
- M-2 test_enrich_limits_security.py:182-211: the xfail has no raises=, and the finally: assertions replace the original ValidationError. Any failure, including a future test bug, would satisfy the xfail. Add raises=(AssertionError, ValidationError) or drop the finally.
- M-3 test_enrich_log_payload_security.py:303-311 (distill test): the planted log line is checked only in stderr, not in the log files. There is no positive control for the distill lake files (labels/candidate) either. The pipeline test has both.
- M-4 _planted.py:113-114: PLANTED_RAW / PLANTED_EMAILS are computed at import from pe._lake(). That is fine today, but it couples import time to the stand-in, and it must move with the tiny_build re-point (carry-over already recorded).

### Probe table (guard removed in herness/, test run, file restored)
| # | Mutation | Test | Result |
|---|---|---|---|
| G1 | redact _EMAIL detector matches nothing | ST03-02 every_model_input | RED |
| G2 | embed stage encoder reads raw core short+description | ST03-02 | RED (encoder) |
| G3 | decide stage _ASKS_CTE text from raw core (Laya input) | ST03-02 | RED (laya) |
| G4 | escalation_queue text from raw core (OpenJev/LLM input) | ST03-02 | RED (openjev) |
| G5 | link_changes pair text from raw incident description | ST03-02 | RED (openjev pair) |
| G9 | normalize_text cut to 3x4,000 | ST03-11 | RED (2 tests) |
| G10 | DecisionInput.text max 12,000 to 20M | ST03-11 | RED |
| G11 | shortlist_options ascending similarity | ST03-11 | RED |
| G12 | escalation_queue ORDER BY scoring ASC | ST03-11 1M queue | RED |
| G13 | run_llm_escalation cap + 1 | ST03-11 20k | RED |
| G14 | LLM_CHUNK 500 to 501 | ST03-11 20k | SURVIVED (M-1) |
| G15 | decider_max_pairs default 30,001 | ST03-11 caps | RED |
| G16 | escalation_queue LIMIT max_records + 1 | ST03-11 1M queue | RED |
| S8 | stage report.note = ticket text | ST03-04 pipeline | RED (StageReport refuses non-code notes: the job fails). The note-code guard is enforced by the type. |

### Sink-plant table (leak planted through production code, test run, file restored)
| # | Sink | Plant | Test | Result |
|---|---|---|---|---|
| S1 | logs stderr + file | enrich.text.redacted log carries the longest redacted ticket | ST03-04 pipeline | RED (stderr, log files) |
| S1b | logs stderr + file | same log carries a raw core.incident.description with an email | ST03-04 pipeline | RED (both) |
| S1d | logs | redacted ticket without sentinel or email (whole value) | ST03-04 pipeline | RED (both) |
| S1c | logs | 60-char prefix of a redacted ticket (no sentinel or email) | ST03-04 pipeline | SURVIVED (I-2) |
| S2a | events (resilience_event) | record_event gpu_swap with target = short ticket | ST03-04 pipeline | RED (ops.sqlite + logs) |
| S2b | events | target = long ticket cut to 200 | ST03-04 pipeline | RED (only because the prefix held the sentinel or another whole text, see I-2) |
| S3 | error message | _hook_errors echoes the exception | ST03-04 failed job | RED (error) |
| S4 | job last_error | extra email field in _last_error | ST03-04 failed job | RED (ops.sqlite) |
| S5 | lake files | text stage writes data/leak.parquet of text rows | ST03-04 pipeline | RED (leak.parquet) |
| S6 | review payloads | create_if_absent adds an email note | ST03-04 pipeline + distill | RED (review items, ops.sqlite) |
| S6b | review payload (mapping) | raw team name in the mapping_suggestion payload | ST03-04 pipeline | RED |
| S7 | job result | run.result leak key = ticket text | ST03-04 pipeline | RED (job result) |

### Gates run
- PYTHONUTF8=1, TMP=TEMP=the w29-s03 temp dir, uv run pytest -m "unit or integration" -k ST03 -q -p no:logging, twice: 108 passed, 1 xfailed both times.
- uv run pytest tests/unit/enrich/security tests/integration/enrich -q -p no:logging: 152 passed, 1 skipped, 1 xfailed.
- --runxfail -k llm_decider: fails with the documented ValidationError and 1000 <= 64.
- mypy --strict (explicit-package-bases), ruff check and ruff format --check on the 4 files: clean.
- After all probes, git status is clean and git diff is empty.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The tests are solid, and every listed sink and guard goes red with a real plant. Two things must be fixed: the xfail reason must name T03-21b (controller ruling), and the leak scan misses cut fragments of ticket text (I-2), which is the most likely real leak shape. Both fixes are test-only and small.

## Re-review round 1 (fix commit d456350 on 0181960)

Scope: I-1, I-2, M-1, M-2, M-3. M-4 is parked.

- OK herness/ is unchanged: git diff --stat e8634a0..HEAD -- herness/ is empty. Only 3 test files changed. mypy --strict (explicit-package-bases), ruff check and ruff format --check are clean.
- OK I-1: the xfail reason now starts "T03-21b:" (test_enrich_limits_security.py:186).
- OK I-2: fragment detection via 32-char shingles (_planted.py shingles/fragments; log_payload _hits). Re-probes:
  - S1c (60-char prefix of a redacted ticket, logged at INFO): now RED in stderr and log files.
  - S1e (40-char middle cut): RED.
  - S1f (50 chars around an EMAIL placeholder): RED. The windows that keep 16 or more real chars still match, so skipping placeholder-only windows opens no hole.
  - S2d (event target cut to 200 chars, logging of the target disabled so only ops.sqlite holds it): RED in ops.sqlite.
  - A genuine cut of a text longer than 200 chars could not be built: the stand-in texts are short. The fragment windows still match independent of length.
  - Accepted limit: a fragment shorter than 32 chars, or one with fewer than 16 real chars per window, is not detected. Probe S1g (31-char prefix) survives by design.
  - No false positives: -k ST03 run twice, 108 passed and 1 xfailed both times.
  - The new fragment positive control is asserted in stderr and in the log files.
- OK M-1: the check now uses the literal 500. Probe G14 (LLM_CHUNK = 501) is RED (assert 501 <= 500).
- OK M-2: the xfail has raises=ValidationError, and the finally block is gone, so the assertions run after decide. --runxfail fails with the pydantic Answer.distribution ValidationError (1000 > 255). The normal run gives XFAIL with the T03-21b reason.
- OK M-3: the distill test asserts the planted line in the log files. It also has a lake-file control (a labels-dir leak-control.parquet holding a 40-char fragment), which is the only file hit.
- After all probes, git status is clean and git diff is empty.

**Re-review verdict:** Approved
