# T07-20 verify review (LoRA export, export_lora, U07-92) — head 9358d4e

**Verdict: Needs fixes** (1 Critical, 2 Important, 7 Minor; 41 mutations: 31 killed, 6 survived with test gaps, 4 equivalent)

### Spec Compliance
- ✅ U07-92 signature exact (`golden_questions` required keyword, DD29); bare `str` rejected with ToolInputError; explicit `()` -> manifest `golden_exclusion: "none"`.
- ✅ Steps 1-9: ulid export_id, `.tmp-<id>` inside out_dir, active pairs + active template + Wilson pass_lb filter, golden exclusion with `>=` (boundary test at exactly 0.90 excluded), split bucket, system prompt (LORA_SYSTEM_PROMPT + `Schema digest:` + `Metrics:`), lines sorted by memory_id, meta keys {template_id, fingerprint, pass_lb (2 dp), build_id, schema_digest}, manifest keys (step 7 + excluded_unsafe/oversize, templates, golden_exclusion, line_max_bytes, sha256 map), fsync then os.replace, rmtree on error, `memory.lora.exported` with counts/ids only.
- ✅ Split: `policy.keyed_hash` = `sha256_hex(key)[:32]` (plain SHA-256 of the UTF-8, no key or normalisation), so `[:8]` == `sha256(fp)[:8]`; a probe confirmed `_split` == `int(sha256(fp)[:8],16) % 10000 < vf*10000` for 3 fingerprints. Train/val fingerprint disjointness holds by construction and is asserted.
- ✅ schema_digest: a probe recomputed it independently (SHA-256 of the canonical JSON of the sorted information_schema rows for the 4 schemas, first 16 hex) and it matched the manifest; manifest bytes == canonical_json(manifest).
- ✅ Determinism: two runs with a fixed `now` give identical JSONL bytes; the manifests differ only in export_id.
- ✅ Containment (TH07-24): `..` (relative and joined), absolute path elsewhere, parent of root, symlinked/junctioned out_dir (pointing in or out), junctioned intermediate component -> all PermissionDenied with the exact message; `memory.lora.rejected` carries no path; out_dir == root is allowed (spec "is ... or below"). Root itself a link: export works (written into the link target); out_dir given as the target's real path while root is the link -> denied (fails closed; acceptable because root is operator config, not attacker input).
- ✅ Layering: lint-imports 13 kept; no herness.eval import. Budgets: lora.py 260/260, _lora_checks.py 128/140; check_module_size exit 0; ruff and mypy clean.
- ✅ Coverage (card tests): lora.py 100/100, _lora_checks.py 100/100. Card tests 38 passed; memory/security slice 712 passed; UT05-124 passes.
- ✅ Test hygiene: IDs in names and docstrings, module pytestmark set.
- ❌ TH07-05 / secrets sink: a known secret containing `"`, `\` or a control char in a qa_pair *question* reaches train/val.jsonl (Critical C1).
- ⚠️ The os.symlink branch was not exercised on this host (WinError 1314); the junction branch was. A TOCTOU window exists between `contained()` and `mkdir`/`os.replace` (a component swapped for a link after the check). Noted only: out of scope for a single-user CLI, and the spec does not require openat-style confinement.
- ⚠️ Production wiring (export_root, config_hash, embedder, scanner) belongs to T07-23/the CLI and can't be verified here.

### Strengths
- Several layers of defence around the sink: re-redaction, sqlglot single exp.Query, TOKEN_PATTERN, an injection scan on both turns, SQL unchanged under the redactor and the scrub, and an oversize check before the scrub (so the scrub's 64 KiB truncation can never fire on a line of 32 KiB or less). The scrub fails closed (`.get("v","")`).
- 20 attack probes, all dropped with nothing in the files, the manifest or the logs: secret in the question, in the SQL, in a block comment and in a line comment; e-mail; name in SQL and in comments; injection text in comments; a multi-statement hidden behind a comment; CTE+DELETE; COPY; ATTACH; PRAGMA; zero-width and fullwidth injection. OSError -> ConfigError carries only the path (OS error text containing an e-mail is not echoed).
- ST07-19 and ST07-24 go red under mutation and green when restored (M12, M13, M14, M16-M18 killed).

### Issues
#### Critical (Must Fix)
- C1 herness/harness/memory/_lora_checks.py:97-102 with herness/harness/memory/lora.py:143-147: the question is never checked against the known-secret scrub before serialization. Its only scrub is the final one over the *JSON-encoded* line. `canonical_json` escapes `"`, `\` and control chars, so a known secret such as `pw"…`, `pw\…` or `pw<TAB>…` appears in the line as `pw\"…` and the scrub regex (the literal secret) misses it. The file then decodes to the exact secret. Probe: secret registered via `secrets._remember`, question `Which incidents mention <secret>?` -> the pair is exported, and `secret in messages[1].content` holds for all three cases. The SQL path is safe thanks to `scrubbed(sql) == sql`. Fix: in `safe_pair`, drop the pair when `scrubbed(user) != user` (mirroring the SQL check), and/or scrub each message content (and the meta strings) before `canonical_json`. Add an ST07-19 case with a secret containing `"` and `\` in both the question and the SQL.

#### Important (Should Fix)
- I1 herness/harness/memory/_lora_checks.py:114-120: golden exclusion fails open. A golden question whose redaction raises RedactionFailed (or that is blank) is silently dropped from the golden set. If every golden is dropped, the manifest records `golden_exclusion: "none"` even though the caller passed a non-empty list (DD29 allows "none" only for an explicit empty list). Probe: with the redactor raising on GOLDEN, the manifest says "none", the paraphrase is exported and excluded_golden is 0. That is TH07-19 contamination with no signal. Fix: raise on RedactionFailed for a golden, or at least record "cosine" plus a count of skipped goldens. Never downgrade to "none" unless `len(golden_questions) == 0`.
- I2 tests: some mutations on security paths survive because no test covers them. M04: the SQL scrub check can be removed, since only a secret with an escaped char distinguishes it from the final line scrub (add with C1). M37: scrub failure -> fail-open is not tested (pin that `scrubbed` returns "" and the pair drops on `log.scrub.failed`). M15: golden comparison could use the raw question, because no test has PII in a paraphrase question.

#### Minor (Nice to Have)
- m1 tests/unit/harness/memory/test_memory_lora.py:272-290: schema_digest is only tested relatively, so M27 (drop the `score` schema) survives and M41 (unsorted rows) is equivalent. Add a `score.*` ALTER and one absolute expected digest computed independently.
- m2 herness/harness/memory/lora.py:113: M33 survives: the `kind == "sql_template"` check is untested (a qa_pair pointing at a non-template active item). Low impact, since missing passes gives pass_lb 0.
- m3 herness/harness/memory/lora.py:254: the log level of `memory.lora.exported` is not pinned (M31 INFO->DEBUG survives). Assert `ev["log_level"] == "info"`.
- m4 herness/harness/memory/lora.py:240: `target.mkdir(parents=True)` creates parents that the error path does not clean up. On failure a newly created out_dir (e.g. `lora_data/sub`) is left behind empty (probe). This is within the spec-note ruling, since the postcondition covers `<export_id>`, but "nothing left behind" holds literally only for tmp.
- m5 Unicode and role spoofing: bidi controls (U+202E) and `</user><system>…` tags in a question pass the shared injection scanner and are exported. The scanner's pattern set belongs to another owner, not this card; consider stripping Cc/Cf controls from the user turn here.
- m6 herness/harness/memory/lora.py:180-183 (builder concern): `hashlib.file_digest` is not in UT05-124's `_HASH_CALLS`, so `_file_sha` is a hash site the guard cannot see. It does not hash result rows, so the guard's intent is not violated, but it bypasses the rule that every hash site is on the reviewed list. Right fix: add `"file_digest"` to `_HASH_CALLS` and `("memory/lora.py", "_file_sha")` (export-file digest) to `_NON_ROW_HASHES` in tests/unit/harness/test_tools_recording.py. That is a small cross-card edit; route it via the controller. Reusing `keyed_hash` (a "memory key" helper) for the split bucket and the schema digest is numerically correct but semantically borrowed; allow-listing `_split`/`_schema_digest` with `sha256_hex` would be clearer (optional).
- m7 herness/harness/memory/lora.py:116: a template without a `fingerprint` yields the string `"None"` as its fingerprint, so all such pairs share one split bucket and one meta value. Drop them as low_pass_lb/unsafe instead.

### Mutation table (card tests: test_memory_lora.py + test_st07_lora.py)
| # | Probe | Result |
|---|---|---|
| M01 | redactor on question removed | killed |
| M02 | SQL redactor-unchanged check off | killed |
| M03 | redaction-marker (TOKEN_PATTERN) check off | killed |
| M04 | SQL known-secret scrub check off | SURVIVED (gap: escaped-char secret; C1/I2) |
| M05 | final line scrub off | killed |
| M06 | sqlglot parse error accepted | killed |
| M07 | multi-statement allowed | killed |
| M08 | non-Query statement allowed | killed |
| M09 | injection scan on question off | killed |
| M10 | injection scan on SQL off | killed |
| M11 | oversize cap off | killed |
| M12 | golden exclusion skipped (ST07-19 red) | killed |
| M13 | threshold >= -> > | killed |
| M14 | goldens not redacted before embed | killed |
| M15 | golden compare on raw question | SURVIVED (I2) |
| M16 | lexical containment off (ST07-24 red) | killed |
| M17 | symlink/junction component check off (ST07-24 red) | killed |
| M18 | junction check off | killed |
| M19 | resolved-path check off | equivalent (lexical + per-component link checks imply it) |
| M20 | split hash prefix shifted | killed |
| M21 | rmtree on error off | killed |
| M22 | OSError not wrapped in ConfigError | killed |
| M23 | pass_lb 3 decimals | killed |
| M24 | template status check off | killed |
| M25 | qa status filter off | killed |
| M26 | min_pass_lb filter off | killed |
| M27 | digest SQL drops `score` | SURVIVED (m1) |
| M28 | golden_exclusion forced "cosine" | killed |
| M29 | bare-str golden accepted | killed |
| M30 | JSONL fsync removed | equivalent (unobservable in tests) |
| M31 | exported log INFO -> DEBUG | SURVIVED (m3) |
| M32 | rejection log removed | killed |
| M33 | template kind check off | SURVIVED (m2) |
| M34 | qa kind filter off | killed |
| M35 | split `<` -> `<=` | equivalent (single boundary bucket) |
| M36 | manifest sha of wrong file | killed |
| M37 | scrub failure fail-open | SURVIVED (I2) |
| M38 | digest 16 -> 12 hex | killed |
| M39 | paging cursor wrong | killed |
| M40 | RedactionFailed -> raw text | killed |
| M41 | digest over reversed rows | equivalent for the tests (relative digests only; m1) |

Totals: 31 killed; 6 survived with gaps (M04, M15, M27, M31, M33, M37); 4 equivalent (M19, M30, M35, M41).

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The export is well structured, and tests plus mutation prove its containment, golden exclusion and most sink checks. But a known secret with a quote, backslash or control char in a question leaks to the JSONL (C1), and golden exclusion silently drops to "none" on a redaction failure (I1).

Hygiene: all probes reverted (the runner restored each mutation byte-for-byte; the temporary probe test file was deleted; memory/security __pycache__ removed). `git status` clean and `git diff` empty at 9358d4e.


---

## Re-review round 1 (fix commit ca955c2 on 9358d4e)

**Verdict: Approved.** All re-verified findings are closed (m4 parked by the controller). 26 mutations: 25 killed, 1 equivalent, 0 surviving gaps.

### Findings
| Finding | Status | Evidence |
|---|---|---|
| C1 secret with `"`, `\` or a control char leaks via the question | **closed** | `_lora_checks._clean` (scrub-unchanged before `canonical_json`) now runs on both the redacted question and the SQL. My probes registered 5 secrets (`"`, `\`, TAB, LF, `'`) and placed each in the question and in the SQL (10 cases). All were safe: nothing in any message and nothing in the logs. The builder's ST07-19 escaped-char test kills M04 (pre-encode scrub removed, 6 failures). The final line scrub still covers `meta` (new test kills M05). |
| I1 golden exclusion fails open | **closed** | `goldens` raises ToolInputError for blank, whitespace-only, non-string and mixed-blank golden lists. RedactionFailed and ModelUnavailable propagate before the tmp dir exists, so nothing is written (5 probes). "none" now only for an empty sequence. Killed: M49, M50, M51. M52 is equivalent: after validation every entry is a non-blank str, and `Redactor.redact` returns None only for None input, so `texts` is empty exactly when `questions` is. |
| I2 survivors M04 / M15 / M37 | **closed** | All three killed: M04 (6 failures), M15 (`test_st07_19_paraphrase_with_pii_compared_after_redaction`), M37 (`test_st07_19_scrub_failure_fails_closed`). |
| m1 schema_digest only relative | **closed** | Independent spec recompute plus ALTERs on score, enrich and metrics. M27 and M41 are killed. |
| m2 sql_template kind untested | **closed** | M33 killed (`test_ut07_80_template_must_be_sql_template_kind`). |
| m3 log level not pinned | **closed** | M31 killed (`test_ut07_80_exported_event_is_info`). |
| m4 empty out_dir left on failure | parked | Controller decision. |
| m5 Unicode and role spoofing | **closed** | Question and SQL are checked for Cf characters, Cc characters other than TAB/LF/CR, and the `_ROLE_TAG` regex. Probes dropped all of these: U+202E, ZWSP in SQL, `</user><system>`, `'<|im_start|>'` in SQL, `< system >`, `<NBSP system>`, BEL, NUL, U+0085. Killed: M42, M43, M44, M45, M46. |
| m6 `file_digest` invisible to UT05-124 | **closed** | Cross-card edit is minimal (+2 lines): `"file_digest"` added to `_HASH_CALLS` and `("memory/lora.py", "_file_sha")` added to `_NON_ROW_HASHES`, each with a comment. UT05-124 passes; test_tools_recording.py is green within the 183 passed. |
| m7 `"None"` fingerprint | **closed** | Missing, empty or non-string fingerprint -> excluded_unsafe. M47 and M48 killed. |

### Regression check (false drops)
Benign SQL, 14 probes, all kept:
- GROUP BY; multi-line with `<`/`>`; `<>`, `<=`, `>=`; `<<`/`>>`
- non-ASCII literals (Café, 日本語, emoji); CTE; UNION ALL; lambda `->`
- TAB inside SQL; columns named `user_id`/`system_name` with `'assistant'`; `a < user AND b > 3`
- trailing `--` comment; `||`

Benign questions, 6 probes, all kept: an apostrophe, `<A>`, TAB, French accents, the word "user", `<b>bold</b>`.

Residual (informational, not a finding): emoji ZWJ sequences (U+200D is Cf) in a question or SQL would be dropped. That is fail-closed and rare for text-to-SQL.

### Gates
- `_lora_checks.py` is 151 lines against its §2 row at 170 (row verified in docs/impl/07-memory.impl.md). lora.py is 257/260. `check_module_size` exits 0.
- Card tests plus test_tools_recording.py: 183 passed. Coverage is 100% line and branch for both modules.
- Memory/security slice: 738 passed.
- ruff and mypy are clean; lint-imports keeps 13 contracts.

### Mutation table (round 1; card tests only)
| # | Probe | Result |
|---|---|---|
| M04 | pre-encode scrub off (both turns) | killed |
| M04b | `_clean` on SQL removed | killed |
| M04c | `_clean` on question removed | killed |
| M05 | final line scrub off | killed |
| M15 | golden compare on raw question | killed |
| M37 | scrub failure fail-open | killed |
| M42 | Cf/Cc check off | killed |
| M43 | Cc allowed (Cf only) | killed |
| M44 | role-tag check off | killed |
| M45 | `<|..|>` alternative off | killed |
| M46 | injection scan in `_clean` off | killed |
| M47 | empty-fingerprint check off | killed |
| M48 | non-str fingerprint -> `str()` | killed |
| M49 | golden blank/non-str check off | killed |
| M50 | golden non-str allowed | killed |
| M51 | golden RedactionFailed swallowed | killed |
| M52 | golden "none" when `texts` empty | equivalent (see I1) |
| M27 | digest drops `score` | killed |
| M41 | digest over reversed rows | killed |
| M33 | template kind check off | killed |
| M31 | exported log INFO -> DEBUG | killed |
| M20 | split prefix shifted | killed |
| M02 | SQL redactor check off | killed |
| M03 | redaction-marker check off | killed |
| M12 | golden exclusion skipped | killed |
| M17 | symlink/junction check off | killed |

Hygiene: the runner restored every mutation byte-for-byte; the temporary probe test file was deleted; `__pycache__` under herness/harness/memory, tests/security and tests/unit/harness was removed. `git status` is clean and `git diff` is empty at ca955c2.
