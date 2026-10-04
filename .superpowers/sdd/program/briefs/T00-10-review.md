# Review: T00-10 Module size check

### Spec Compliance
- ✅ Spec compliant

Requirement/test rows (brief + U00-55 algorithm, docs/impl/00-foundation.impl.md §2/§11):

| Row | Status |
|---|---|
| Config load: `default` (int, required), `overrides` table; missing/non-int `default` → exit 2 | ✅ `tools/check_module_size.py:206-217` (`_load_config`) |
| Override `limit > default` with empty `reason` → MS003 | ✅ `check:319-322`; verified against `pyproject.toml`'s own seeded override (`limit=220 > default=400`? No — `220 < 400`, so no MS003 fires on the real repo, correctly) |
| Doc budgets: tables with header `Path`…`Line budget`, single backticked `.py` token w/o `{`,`*`,space, plain-int last cell | ✅ `_parse_doc`/`_module_row`/`_split_row`/`_is_separator` (lines 220-265); spot-checked against the real table at `docs/impl/00-foundation.impl.md:58-84` — the `{decisions,harness,...}.py` row (line 71) is correctly excluded via the `{` check, and non-`.py`/non-integer rows (`py.typed`, `uv.lock`, "generated", "—", "set by owner spec…") are all correctly skipped |
| Same path, two different doc integers → MS002 | ✅ `_doc_budgets:268-280` |
| Files: every `*.py` under `<root>/herness`, `<root>/app`, `<root>/tools`, sorted | ✅ `_files:283-289`, uses `_CODE_ROOTS` and `sorted(...)`; Windows-safe via `.relative_to(root).as_posix()` at `_check_file:304` |
| Effective budget = override-or-default, then min with doc budget if present | ✅ `_effective_budget:292-298`; verified against both UT00-59 cases (override-only, doc-only, default-only) |
| UTF-8 read; `UnicodeDecodeError` → MS004 | ✅ `_check_file:306-310` |
| `lines > budget` → MS001 with exact template `<lines> lines > budget <budget> (<source>)` | ✅ `_check_file:311-313`, matches brief's verbatim template |
| Exit 1 on any violation else 0; exit 2 on config error | ✅ `main:329-346` |
| `pyproject.toml` seed values (`default=400`, `herness/harness/loop.py` override `limit=220`, reason `"design 05 §5.2"`) verbatim | ✅ diff lines 15-18, exact string match to the brief |
| UT00-59 (override+doc+passing default file, exit 1) | ✅ `test_ut00_59_default_override_and_doc_budgets` reproduces the brief's exact scenario and assertions |
| UT00-60 (empty-reason override + conflicting docs, MS003+MS002) | ✅ `test_ut00_60_empty_reason_and_conflicting_doc_budgets` reproduces the brief's exact scenario |
| Card acceptance check: `uv run python -m tools.check_module_size` exits 0 on the real repo | ✅ per report's Gate outputs section (unverified by me beyond the report per reviewer rules — no contradicting signal found) |
| Layer contract update requirement (global constraints) | ✅ verified directly: `pyproject.toml`'s `[tool.importlinter]` already lists `tools` as a root package with a generic "herness never imports app or tools" contract (not a per-file layer list), so no contract edit was needed — report's claim confirmed |

- ⚠️ Cannot verify from diff: line/branch coverage percentage for `tools/check_module_size.py` (no `--cov` output in the report). Note the global constraint's 90%/85% coverage rule is scoped to "each new or changed `herness` module"; `tools/` is arguably outside that literal scope, so this may not even apply — flagged for awareness only, not scored as a gap.
- ⚠️ Cannot verify from diff: real-repo `uv run python -m tools.check_module_size` exit-0 run and the full gate suite, beyond trusting the report's pasted output (per reviewer rules, not re-run).

### Strengths
- Algorithm transcribed faithfully from the brief step-by-step, including the exact MS001 message template and exact seed TOML values.
- Doc-table parser correctly rejects every non-matching row style present in the *real* `docs/impl/00-foundation.impl.md` module map (verified by reading that table directly, not just the tests): multi-file glob entries with `{...}`, non-`.py` paths, and non-integer/`"generated"`/`"—"` budget cells.
- MS002/MS003 are correctly treated as doc/config-level findings independent of whether the named file exists on disk, matching UT00-60's `c.py` (never created) scenario.
- Cross-platform path handling (`as_posix()`) avoids a latent Windows bug that would otherwise break override/doc-path matching on this machine.
- Honest, itemized "Deviations from the brief" section in the report — correctly flags the two under-specified areas (MS002/MS003 message wording, violation path/line for config-level findings) and grounds each choice in a concrete test-compatible rationale.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- `tools/check_module_size.py:202-204` — `_load_config`'s TOML read only catches `(OSError, tomllib.TOMLDecodeError)`; a `pyproject.toml` that fails to *decode* as UTF-8 (`UnicodeDecodeError`, a `ValueError` subclass, not `OSError`) would propagate uncaught instead of yielding a clean exit-2 usage error. Edge case, not exercised by any brief test row.
- `tools/check_module_size.py:211` — `table.get("overrides", {}).items()` assumes `overrides` is a mapping; a malformed config where `overrides` is some other TOML type (e.g. a string or array) raises an uncaught `AttributeError` rather than a clean `_ConfigError`/exit 2. Same class of gap as above — not in the brief's test rows, but `default`'s malformed case is handled and this sibling case isn't.
- `tools/check_module_size.py:204` — `raise _ConfigError(str(exc)) from exc` builds the message inline rather than assigning to `msg` first, unlike every other raise site in the file. Not a ruff EM101/EM102 violation (the argument isn't a string/f-string literal), just a minor internal-consistency nit.
- `tools/check_module_size.py` is 200 lines against its own doc-declared 200-line budget — i.e. it currently passes at the exact boundary (`lines > budget` is false when equal, correctly). The report already flags this itself as a forward-looking observation, not a current defect; repeating it here only so it's visible in the review record for whoever next touches this file.

### Assessment
**Task quality:** Approved
**Reasoning:** The implementation matches the brief's algorithm and exact literal values line-for-line, reproduces both UT00-59 and UT00-60 exactly as specified, and was spot-checked against the real module-map table in `docs/impl/00-foundation.impl.md` to confirm the doc-parsing edge cases (glob-brace rows, non-`.py`/non-integer rows) are handled correctly. The import-linter contract claim was independently verified rather than taken on faith. Remaining findings are all sub-brief-scope robustness edges (malformed-config crash paths) that no test in the brief exercises and do not affect the graded behavior.
