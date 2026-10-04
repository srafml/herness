# T03-03 review (Layout and question set): verify agent

Worktree: D:\herness\.claude\worktrees\agent-af9c6453f5b7d7b1b, base 2b2e44d, head a49edce (5 files, +1149).

### Spec Compliance
- ❌ Issues found (2 Important, details below). All other unit and test requirements are met.

| Item | Status | Note |
|------|--------|------|
| U03-12 resolve_data_path | ✅ | An absolute path makes joinpath replace the root; `data` is dropped only from relative paths; resolve() follows links; the check is `is_relative_to(root.resolve())`; the message is `path outside data root: <path>`. Probed on Windows: `\\server\share\x`, `//?/C:/x`, `\etc\x`, `data\..\x`, `D:/` and other drives are rejected; an upper-cased root, `C:foo` (same drive) and `data/./x` stay contained. |
| U03-13 EnrichPaths | ✅ with ⚠️ | Paths match §4.2–§4.7. Patterns: qsv and decider_version are read from the `QuestionSet.version` / `DecisionOutput.decider_version` Field metadata (identical to the spec regexes); Laya version `^laya-\d{8}-\d+$`; slug `^[A-Za-z0-9._-]{1,96}$`; decider from the Literal set; `quote(v, safe="")`. Errors name the argument, not the value. Ruling: `from_config` is typed on the local Protocol `_EnrichConfig` with a `# T10-03:` marker, as stated. Windows gap: see I-1. |
| U03-14 PAIR_QUESTIONS | ✅ | `frozenset({"change_caused_pair"})`. |
| U03-15 question_fingerprint | ✅ | Field list exact (9); dynamic source → options None; levels/applies_to tuples → lists; `format(threshold, ".6f")`; `sha256_hex(canonical_json)[:16]`. |
| U03-16 load_question_set | ✅ | One Question with fingerprint per QuestionConfig; order kept; pair shape checked (bool, scoring_use False, applies_to == ("incident",)); ValidationError → ConfigError naming id + loc, `from exc`; duplicate id → ConfigError. |
| U03-17 check_fingerprint_registry | ❌ | 64 KB limit, `enrich.config.fingerprint_drift` (ERROR, fields question and question_set_version, matching the §8 log table), exact drift message and merge-and-write are correct. The atomic write lacks the fsync that ENG-STANDARDS line 132 requires (I-2). |
| U03-18 resolve_dynamic_options | ✅ | SQL verbatim; redact, then `[:500]`; NULL, empty and empty-after-redaction names → id; rule-e skip including bool words; `enrich.questions.option_skipped` WARNING with question and count; `dynamic options < 2 for <qid>`; model_copy per question, fingerprint kept. Ruling: required keyword-only `redact: Callable[[str], str]` with a T10-10 docstring note, as stated. |
| U03-19 shortlist_options | ✅ | Label-sorted stack, matrix·vec, sort by (−sim, label), top k, ConfigError naming a missing label. |
| U03-20 acceptance_for | ✅ | Per-type default overlaid by `model_dump(exclude_none=True)`; unknown id → ConfigError. |
| UT03-02 / UT03-03 | ✅ | Both paths covered: DecisionsConfig ValidationError from YAML (safe_load + model_validate), and loader ConfigError naming `bad_question` via model_construct, with `__cause__` asserted for UT03-03. |
| UT03-10 | ✅ | `data/models/x` OK; `../x`, `/etc/x`, `data/../../x` and an absolute path elsewhere are rejected. The symlink case skips without privilege; the junction case runs and passes. |
| UT03-11 | ✅ | `laya-20261004-1` is valid; `laya-../x`, decider `evil` and other bad values are rejected naming the argument; from_config is exercised via a fake config. |
| UT03-12 | ✅ (ruling) | PAIR_QUESTIONS and the load-time pair checks only; the build_inputs half is carried over to the U03-84 card (noted in the module docstring). |
| UT03-13 | ✅ | Each of the 9 fields; dynamic options ignored; QuestionConfig == Question. |
| UT03-14 | ✅ | Pair `scoring_use: true` → ConfigError. |
| UT03-15 | ✅ | Drift: event captured, file untouched. Append, keep and no-op rerun. Oversized, bad JSON, non-object, bad value and bad UTF-8 files. A failed replace leaves no temp file. |
| UT03-16 | ✅ | 3 active teams (1 bad id, 1 NULL name) + 1 inactive → 2 options, skip logged, id as description; 1 team → ConfigError; service names truncated after redaction. |
| UT03-17 | ✅ | 300 options, 1024-d, 3 exact ties ordered by label, 64 kept, descending. |
| UT03-18 | ✅ | min_accuracy override + choice defaults. |
| PT03-02 | ✅ (weak, see M-4) | |
| ST03-10 | ✅ | `laya accept ../../etc`, `embedding.path: ../../x`, CURRENT traversal, `.` and `..`. |
| Test IDs / docstrings / pytestmark | ✅ | `--require-test-ids` passes; every file sets `pytestmark = pytest.mark.unit`. |
| Budgets | ✅ | layout.py 149/150, questions.py 230/300. settings.py untouched (5 files in the diff). |

⚠️ Cannot verify from the diff or this environment:
- The UT03-10 symlink sub-case is skipped on this machine (no symlink privilege). The junction test exercises the same `resolve()` path and passes. The symlink case should run on POSIX CI.

Gates run in the worktree with uv run:
- `pytest tests/unit/enrich -q -p no:logging`: 190 passed, 1 skipped.
- Coverage: layout.py 100 % line and branch; questions.py 100 % line, 1 partial branch (188->192), 99 % total.
- `--require-test-ids`: pass.
- mypy strict: no issues (68 files).
- ruff check: pass. ruff format --check: 151 files formatted.
- tools.check_module_size: exit 0.
- lint-imports: 11 contracts kept, 0 broken.

### Strengths
- Containment is correct on Windows for drive paths, UNC paths, `\\?\` paths, rooted paths without a drive, and case-folded roots (probed).
- The added `.`/`..` rejection closes a real escape the spec patterns allow: `..` passes the decider_version and slug patterns, and `quote` keeps it.
- Identifier rules come from the owning pydantic fields, so they cannot drift from `QuestionSet` / `DecisionOutput`.
- Error messages carry only the argument name, question id or file path. Skipped team ids and names are never logged, and redaction runs before truncation.
- Tests assert real behaviour: captured log events, file contents, mtime no-op, temp-file cleanup, `__cause__`.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **I-1 Windows trailing-dot and all-dots identifiers alias other directories (TH03-08).** `herness/enrich/layout.py:28,38,55-61`. `_DOT_NAMES` blocks only `.` and `..`, but Win32 path normalization strips trailing dots. Probed on this machine:
   - `cluster_snapshot("v1", "...")` returns `...\clusters\v1\...`, which the OS treats as `...\clusters\v1`: `os.path.abspath` gives `clusters\v1\`, and `mkdir` fails with WinError 183 because v1 exists.
   - `cluster_root("...")` aliases `clusters\`.
   - `calibration_file("llm", "...", qsv)` writes to `calibration\llm\<qsv>.json`.
   - `a..` aliases `a`.

   These paths stay inside the data root, but an identifier can now target its parent directory. A later rmtree or overwrite of a "snapshot" would then hit every snapshot of that algorithm version, and two distinct identifiers can collide on one directory. The slug and decider_version patterns admit these values, and so does the encoded decider_version (`quote` keeps dots). Fix: reject any identifier that is all dots or ends in `.`. Ideally also reject Windows reserved device names `CON|PRN|AUX|NUL|COM\d|LPT\d`, optionally followed by an extension; probed `cluster_root("NUL")` gives `abspath` `\\.\NUL`. Add UT03-11/ST03-10 cases for `...`, `a.` and `NUL`.
2. **I-2 The atomic write omits fsync, violating ENG-STANDARDS line 132** ("write a temporary file in the same directory, `fsync` it, then `os.replace` it"). `herness/enrich/questions.py:118-129`. A crash after `os.replace` but before the data is flushed can leave an empty or partial `questions.json`. The next build then fails with "questions.json is unreadable" until someone removes the file by hand, and the drift guard is lost in the meantime. Fix: `handle.flush(); os.fsync(handle.fileno())` before closing.

#### Minor (Nice to Have)
1. **M-1** `herness/enrich/questions.py:98-106`: TOCTOU race between `stat().st_size` and `read_text()`; the file could grow in between. Reading the bytes once and checking `len(raw) > 64 KB` removes the race and saves one syscall. The registry also accepts any string value; checking `^[0-9a-f]{16}$` would give an earlier and clearer error.
2. **M-2** `herness/enrich/layout.py:94-97`: `__post_init__` checks only `is_absolute()`. A directly constructed `EnrichPaths` with an absolute but unresolved root (a symlink or `..` inside it) returns lexical paths from most methods but resolved paths from `laya_current()` and `embedding_model_dir()`. Resolving `data_root` in `__post_init__` (via `object.__setattr__`) would keep them consistent. Also, `resolve_data_path("")` returns the data root itself, so an empty `embedding.path` is accepted silently.
3. **M-3** `herness/enrich/questions.py:41-43`: `_OPTION_KEY_RE` and `_BOOL_WORDS` duplicate `herness/core/types/decisions.py:23,28`. A comment points to the owner, but the two copies can drift. A public helper in the types module, or a `_field_pattern`-style read, would keep one source.
4. **M-4** `tests/unit/enrich/test_questions.py:305-336` (PT03-02): reordering the top-level input dict before `model_validate` proves nothing, because model field order is fixed; only the options reorder tests key-order invariance. The mutation property samples 5 of the 9 fields; type, options_source, levels and applies_to are covered only by UT03-13.
5. **M-5** `herness/enrich/questions.py:203-218`: shortlist preconditions (`type == "choice"`, > 255 options, 1024-d `text_vec`) are not checked. A shape mismatch surfaces as a numpy `ValueError`, and empty options make `np.stack` raise. The spec states these as preconditions, so a check is optional.
6. **M-6** `tests/unit/enrich/test_questions.py:350-356`: the drift test does not assert the `question_set_version` log field or the ERROR level. `test_ut03_15_failed_write_leaves_no_temp_file` sits after the U03-20 section. The report gives test_layout.py as 172 lines; it has 163.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation matches every unit and test row, and all gates pass. Two fixes remain: the identifier hardening leaves a Windows trailing-dot aliasing hole (I-1), and the atomic registry write skips the fsync that ENG-STANDARDS requires (I-2). Both are small and local.

---

## Re-review round 1 (fce01a4, diff a49edce..fce01a4)

Scope: the round-0 findings and any regressions the fixes introduced.

| Finding | Status | Evidence |
|---------|--------|----------|
| I-1 Windows trailing-dot and device-name aliasing | ✅ | `herness/enrich/layout.py:37` `_UNSAFE_RE` (case-insensitive fullmatch). It rejects any identifier ending in `.` (which covers `...` and `a..`) and `CON/PRN/AUX/NUL/COMn/LPTn` with or without an extension. It applies to every identifier in `_valid` (layout.py:56-62). decider_version is checked before quoting, which is correct because quoting cannot add a trailing dot or a device name. `NULL` stays valid. Tests: UT03-11 parametrised cases and ST03-10 `test_st03_10_windows_aliasing_identifiers_rejected`. |
| I-2 fsync | ✅ | `herness/enrich/questions.py` `_write_atomic`: `flush()` and `os.fsync(fileno())` run before close and `os.replace`. This matches ENG-STANDARDS line 132. |
| M-1 registry TOCTOU / value check | ✅ | Single bounded `read(64 KB + 1)`. FileNotFoundError → empty registry; any other OSError → ConfigError. Size is checked on the bytes read, and values must match `^[0-9a-f]{16}$`. New cases: upper-hex, short value, path is a directory. |
| M-2 unresolved root / empty path | ✅ with a regression, see N-1 | `__post_init__` resolves `data_root` via `object.__setattr__` after the absolute check. `""` and `"."` are rejected. Test: `test_ut03_11_data_root_resolved_once`. |
| M-3 duplicated rule-e constants | ✅ (accepted as left) | Importing from `herness.core.types.decisions` is blocked by OWN041. A local copy stays, and `test_ut03_16_option_key_rule_matches_owner` pins it to the owner's pattern and set, so any drift fails a test. |
| M-4 PT03-02 | ✅ | Top-level and option keys are shuffled with `st.permutations`. Both `QuestionConfig` and `Question` are fingerprinted from the shuffled dict. The mutation property samples all 9 fields through `_mutate`, and each mutation really differs (applies_to falls back correctly when all three entities are present). |
| M-5 shortlist preconditions | ✅ | A non-choice question or empty options → ConfigError. `text_vec` and each option vector must have shape (1024,), else ConfigError. "> 255 options" is not enforced, which is acceptable because it is harmless. Test: `test_ut03_17_preconditions_checked`. |
| M-6 drift test / placement | ✅ | The test asserts exactly one event, `log_level == "error"` and `question_set_version`. The failed-write test now sits in the U03-17 section. |

### New findings

#### Critical
None.

#### Important
None.

#### Minor
1. **N-1** `herness/enrich/layout.py:47-48`: the M-2 fix changed the error text for every rejection to `path outside data root (or empty): <path>`. U03-12 specifies `ConfigError("path outside data root: <path>")`. The existing tests still pass only because they match the prefix. Suggested fix: keep the spec text for containment failures and raise a separate message (for example `empty data path`) for `""` and `"."`. The budget is tight (layout.py is at 150/150), so if the message has to stay combined, record it as a deviation.

### Gates (re-run in the worktree)
- layout.py 150/150 lines (at the budget, not over it); questions.py 253/300.
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich -q -p no:logging --require-test-ids`: 211 passed, 1 skipped (symlink privilege).
- Coverage: layout.py 100 % line and branch; questions.py 100 % line, 1 partial branch (200->204), 99 % total.
- mypy strict: no issues (68 files).
- ruff check: pass. ruff format --check: 151 files formatted.
- tools.check_module_size: exit 0. tools.check_type_ownership: exit 0.

### Assessment (round 1)
**Task quality:** Approved
**Reasoning:** Both Important findings and all addressed Minor findings are fixed with tests, and every gate passes. The only open item is Minor N-1: the containment error text drifted from the spec wording. It can be fixed in a later pass or recorded as a deviation.

### Re-review round 1, addendum: regenerated brief check
- I diffed the unit specs in the regenerated `T03-03.md` against `docs/impl/03-enrichment.impl.md` lines 382-545. They are identical apart from whitespace and the dropped `### 3.3` heading. The test rows UT03-10 to UT03-18, PT03-02 and ST03-10 are verbatim copies of the §11 rows.
- My round-0 review already checked the code against those same spec rows. I re-checked U03-12 to U03-20 and the nine test rows against fce01a4 and found no new differences between the brief and the implementation. The only open item is still N-1: the U03-12 error text now reads `path outside data root (or empty): <path>`, where the spec says `path outside data root: <path>`.
- The regenerated brief does not list UT03-02 and UT03-03; they appear only in the U03-16 Tests field. Both are still covered: see the round-0 table.
- The verdict stays **Approved**, with Minor N-1 open.
