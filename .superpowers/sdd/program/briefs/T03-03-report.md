# T03-03 report: Layout and question set

Status: DONE_WITH_CONCERNS (minor; see Concerns)
Commit: a49edce feat(enrich): add data layout and question set loader (T03-03)
Worktree/branch: D:\herness\.claude\worktrees\agent-af9c6453f5b7d7b1b / worktree-agent-af9c6453f5b7d7b1b (base 2b2e44d)

## Files (lines vs budget)
- herness/enrich/layout.py: 149 / 150 (resolve_data_path, EnrichPaths, LabelKind)
- herness/enrich/questions.py: 230 / 300 (PAIR_QUESTIONS, question_fingerprint, load_question_set, check_fingerprint_registry, resolve_dynamic_options, shortlist_options, acceptance_for)
- tests/unit/enrich/test_layout.py: 172
- tests/unit/enrich/test_questions.py: 559
- tests/unit/enrich/security/test_layout_security.py: ST03-10
- settings.py NOT touched; pyproject.toml unchanged (existing contracts cover herness.enrich).

## Units -> tests
- U03-12 resolve_data_path: UT03-10 (data/models/x OK, absolute-inside OK; ../x, data/../../x, /etc/x, absolute elsewhere, symlink out, Windows junction out -> ConfigError), ST03-10
- U03-13 EnrichPaths: UT03-11 (all 12 path methods, from_config via fake config; laya-../x, laya-2026-1, decider evil, bad qsv/kind/decider_version/slugs, relative data_root -> ConfigError naming the argument), ST03-10 (`../../etc` version, embedding.path ../../x, CURRENT traversal, `.`/`..` decider_version and snapshot_id)
- U03-14 PAIR_QUESTIONS: UT03-12 (declaration + load pair-shape checks)
- U03-15 question_fingerprint: UT03-13 (each of 9 fields changes it; dynamic options do not; fingerprint field ignored; QuestionConfig == Question; threshold at 6 dp), PT03-02 (hypothesis: key-order invariance for fields and options; mutation of id/instructions/options/threshold/scoring_use changes it)
- U03-16 load_question_set: UT03-02, UT03-03, UT03-14 (+ fingerprints set, order preserved, duplicate id, bad version)
- U03-17 check_fingerprint_registry: UT03-15 (drift -> ConfigError + `enrich.config.fingerprint_drift` event captured with structlog capture_logs, file untouched; new ids appended, old kept, rerun no-op; oversized/bad json/non-object/non-string/bad utf8 -> ConfigError; failing os.replace leaves no temp file)
- U03-18 resolve_dynamic_options: UT03-16 (in-memory duckdb, CREATE SCHEMA core + tables in test: 3 active teams incl. 1 bad id and 1 NULL name + 1 inactive -> 2 options, skip logged WARNING question/count, NULL name -> id, stub redactor applied only to used names, fingerprint unchanged, other questions untouched; core.service: empty name -> id, 500-char truncation after redaction; 1 team -> ConfigError "dynamic options < 2 for owning_team"; redactor returning "" -> id)
- U03-19 shortlist_options: UT03-17 (300 options, 1024-d synthetic vectors, 3 exact ties -> label order, 64 kept, descending similarity, k=5, missing vector -> ConfigError naming the label)
- U03-20 acceptance_for: UT03-18 (min_accuracy override + choice defaults; bool/score defaults; unknown id -> ConfigError)

## Rulings applied
1. EnrichPaths.from_config is typed against the local Protocol `_EnrichConfig` (paths.data, decisions.embedding.path, models.deciders.laya.current_file), marked `# T10-03:`. To stay within the 150-line budget, the leaf sections are typed with the real settings models (`EmbeddingSettings`, `DecidersSettings`) instead of more nested Protocols. The tests use a tiny dataclass fake that carries real settings leaves.
2. resolve_dynamic_options has a required keyword-only `redact: Callable[[str], str]` (the docstring notes T10-10). The tests pass a stub and assert it was applied.
3. UT03-12: build_inputs (U03-84) is absent, so the test covers the PAIR_QUESTIONS declaration and the load_question_set pair-shape checks. CARRY-OVER: "pair question never asked" by build_inputs belongs to the U03-84 card.
4. UT03-02 / UT03-03: DecisionsConfig (QuestionConfig._rules) rejects all these shapes first with ValidationError, and those tests are kept. Loader-level tests were added via QuestionConfig.model_construct + DecisionsConfig.model_copy: load_question_set raises ConfigError naming the question (`bad_question`), with the ValidationError as __cause__. UT03-14 (pair scoring_use true) reaches the loader directly from YAML, because DecisionsConfig does not check pair shape. The older T03-01 tests in test_decisions.py (adapted UT03-02/03) were left unchanged; their docstring still says load_question_set is absent. That is cosmetic and was not edited because the file is outside this card.
5. Test layout, markers and IDs follow the instructions; `pytest --require-test-ids` passes. The symlink sub-case is skipped on this machine (no privilege). An extra Windows junction test (_winapi.CreateJunction, importorskip on other platforms) exercises the same escape and passes.

## Deviations / notes
- Identifier hardening beyond the spec patterns: `.` and `..` are rejected for every identifier. The decider_version pattern and the slug pattern `^[A-Za-z0-9._-]{1,96}$` both admit `..`, and `quote("..", safe="")` stays `..`, which would escape one directory in `calibration/<decider>/<v>/` and `clusters/<av>/<sid>` (TH03-08).
- Identifier rules reuse the `QuestionSet.version` and `DecisionOutput.decider_version` patterns (read from pydantic Field metadata) and the `DecisionOutput.decider` Literal set. The Laya version regex and the slug regex are local because nothing else owns them yet.
- The option-key rule (U03-02 rule e) is re-declared in questions.py because the decisions.py constants are private. A comment points to the owner.
- No atomic-write helper exists in herness/core or herness/store, so questions.py has a private `_write_atomic` (mkstemp in the same dir + os.replace, temp removed on failure).
- Drift is logged at ERROR level (the spec gives no level), with fields question and question_set_version.
- resolve_dynamic_options returns a freshly built QuestionSet rather than a model_copy of the set, so the private id index stays consistent. Each question uses model_copy(update={"options": ...}) as specified. Rows are queried once per source.
- The fingerprint treats thresholds that are equal at 6 decimals as equal (spec: format(threshold, ".6f")). PT03-02 therefore changes the threshold by at least 1e-3.
- A write failure (OSError) in check_fingerprint_registry propagates unchanged; the spec lists only drift and an unreadable file as ConfigError.

## RED / GREEN
- RED: `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_layout.py tests/unit/enrich/test_questions.py -q -p no:logging` -> "2 errors during collection" (modules missing).
- GREEN: the same command plus the security file -> 65 passed, 1 skipped (symlink privilege).

## Coverage (card tests only, --cov-branch)
- herness/enrich/layout.py: 100% line, 100% branch (79 stmts, 8 branches)
- herness/enrich/questions.py: 100% line, 1 partial branch (188->192: a second dynamic question sharing a source), 99% total

## Gates
- ruff format --check: 151 files already formatted; ruff check: All checks passed
- mypy (strict): Success, 68 files
- lint-imports: 11 kept, 0 broken
- tools.check_type_ownership: exit 0 (INFO pending owner 09)
- tools.check_module_size: exit 0
- `pytest -m "(unit or integration) and not slow"`: 1458 passed, 1 skipped, 5 deselected, 1 xfailed (pre-existing)

## Concerns
- layout.py is at 149/150. Fitting it needed the settings-typed leaf protocol (above) and compact identifier rules, so any addition will need a budget raise.
- UT03-12 carries over to the build_inputs (U03-84) card.


## Fix round 1 (review T03-03-review.md)

Commit: fce01a4 fix(enrich): address T03-03 review round 1 (T03-03)

Correction to the first report: test_layout.py was 163 lines at a49edce, not 172. It is now 186.

| Finding | Status | Change |
|---|---|---|
| I-1 | fixed | layout.py `_UNSAFE_RE` `(?i)^(?:.*\.|(?:CON|PRN|AUX|NUL|COM\d|LPT\d)(?:\..*)?)$` replaces the `.`/`..` set. It rejects every identifier that ends in `.` (all-dots included) and every Windows device name, with or without an extension, case-insensitive. The check applies to every identifier (qsv, version, decider, decider_version, algorithm_version, snapshot_id, kind). decider_version is checked raw; quoting only adds `%XX` and cannot create a device name or a trailing dot. Tests: UT03-11 parametrised cases (`...`, `a.`, `NUL`, `com1.txt`, `Lpt9`, decider_version `...`, `a..`); ST03-10 `test_st03_10_windows_aliasing_identifiers_rejected` (`...`, `a.`, `a..`, `NUL`, `nul.txt`, `CON`, `aux`, `COM1` against snapshot_id, algorithm_version and decider_version); `NULL` stays valid. |
| I-2 | fixed | `_write_atomic` calls flush() and os.fsync() before the file is closed and replaced. |
| M-1 | fixed | The registry is read once with a bounded `read(64 KB + 1)`. A missing file gives an empty registry; any other OSError, oversize, bad UTF-8/JSON, a non-object, or a value that is not `^[0-9a-f]{16}$` raises ConfigError (`questions.json is unreadable` / `exceeds 64 KB`). Tests add upper-hex, short and path-is-a-directory cases. |
| M-2 | fixed | `EnrichPaths.__post_init__` resolves data_root once (object.__setattr__) after the absolute check. `resolve_data_path("")` and `"."` raise ConfigError (`path outside data root (or empty)`). Test: `test_ut03_11_data_root_resolved_once`. |
| M-3 | left (blocked) | Importing `_OPTION_KEY_RE` / `_BOOL_WORDS` from `herness.core.types.decisions` fails `tools.check_type_ownership` with OWN041 (import via submodule), and `herness.core.types` does not re-export private names. The local copy stays; `test_ut03_16_option_key_rule_matches_owner` asserts it equals the owner's pattern and set, so drift fails a test. |
| M-4 | fixed | PT03-02 builds `QuestionConfig` and `Question` from a dict whose top-level keys and option keys are shuffled with `st.permutations`, asserts equal fingerprints, and asserts `canonical_json(shuffled) == canonical_json(fields)`. The mutation property samples all 9 fingerprinted fields through `_mutate`. |
| M-5 | fixed | shortlist_options raises ConfigError for a non-choice question or empty options, and for a text or option vector whose shape is not (1024,). Test: `test_ut03_17_preconditions_checked`; the missing-vector test now uses 1024-d vectors. The "> 255 options" precondition is not enforced; shortlisting a smaller set is harmless. |
| M-6 | fixed | The drift test asserts exactly one event, `log_level == "error"` and `question_set_version`. The failed-write test moved into the U03-17 section. |

Line counts: layout.py 150/150, questions.py 253/300, test_layout.py 186, test_questions.py 609, security/test_layout_security.py 60.

Gates: ruff format/check clean; mypy strict passes (68 files); lint-imports 11 kept; check_type_ownership exit 0; check_module_size exit 0; `pytest tests/unit/enrich --require-test-ids` 211 passed, 1 skipped (symlink privilege); full `(unit or integration) and not slow` 1479 passed, 1 skipped, 1 xfailed (the xfail was already there).
Coverage: layout.py 100% line and branch; questions.py 100% line, 1 partial branch (200->204: a second dynamic question sharing a source), 99%.

## Fix round 2

96c25e4 fix(enrich): restore exact U03-12 containment message (T03-03). This fixes N-1:
- Containment failures raise ConfigError with the exact spec text "path outside data root: <path>".
- An empty path ("" or ".") gets its own ConfigError, "empty data path".
- UT03-10 and ST03-10 now assert both messages as full-match regexes.

layout.py stays at 150/150 lines; the message choice is a single conditional line.

Gates: ruff format/check, mypy and check_module_size are clean. `pytest tests/unit/enrich` gave 211 passed, 1 skipped. Coverage is unchanged: layout.py 100%, questions.py 99%.
