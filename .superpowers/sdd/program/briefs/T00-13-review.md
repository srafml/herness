# T00-13 review: Audit and licence gates

Verdict: **Approved.** There are no Critical or Important findings. The Minor items can be fixed now or folded into T00-14.

### Spec Compliance
- ✅ Spec compliant. Each requirement and test row was checked against the brief and against `docs/impl/00-foundation.impl.md` (U00-57 and U00-58 at lines 1341 and 1365, and card T00-13 at line 2235).

| Row | Status | Evidence |
|---|---|---|
| U00-57 options: `--pip-audit` and `--osv` required, `--ignore` default `tools/audit_ignore.toml`, `--summary`, `--today` default `clock.utc_day(clock.now())` | ✅ | check_audit.py:255-262, 267 |
| U00-57 step 1: pip-audit parsing, `skip_reason` gives `AU010 skipped <name>` | ✅ | check_audit.py:86-101 |
| U00-57 step 2: osv parsing of `results[].packages[]`, vulnerabilities, affected ranges and events, and `groups[].ids` / `max_severity` | ✅ | check_audit.py:104-149 |
| U00-57 step 3: PEP 503 normalisation | ✅ | check_audit.py:27, 61-63 |
| U00-57 step 4: merge on intersecting `id ∪ aliases` within a package; `fix_available` is the OR; severity is the max of `float(max_severity)` and None when absent, non-numeric or non-finite | ✅ | check_audit.py:152-170, 113-120. The merge is correctly transitive, because merged entries stay pairwise disjoint. |
| U00-57 step 5: strict ignore file (all four keys, `YYYY-MM-DD`, anything else exits 2) | ✅ | check_audit.py:173-208 |
| U00-57 step 6: decisions `ignored` / `AU003 expired ignore` (blocking) / unknown severity treated as 7.0 / `AU001 blocking` / `AU002 warning` | ✅ | check_audit.py:211-223. `expires == today` is still `ignored` (>=). |
| U00-57 step 7: `AU011 unused ignore` | ✅ | check_audit.py:276 |
| U00-57 step 8: summary table `package \| version \| ids \| severity \| fix \| decision` | ✅ | check_audit.py:241-252. Pipes are escaped. |
| U00-57 step 9 and errors: exit 1 on AU001 or AU003, 0 otherwise, 2 on unreadable or malformed JSON or TOML (R-73) | ✅ | check_audit.py:281, 284-294 |
| `tools/audit_ignore.toml` contains `ignore = []` | ✅ | audit_ignore.toml:3 |
| U00-58 options: `--sbom` required, `--pyproject` default, `--mode enforce\|report` default `enforce` | ✅ | check_licences.py:236-239 |
| U00-58 step 1: policy table (allowed, case-insensitive aliases, approved with 5 keys, report_only) | ✅ | check_licences.py:66-95. See Minor M2 and M3. |
| U00-58 step 2: skip `metadata.component` | ✅ | check_licences.py:190-209. Matched by bom-ref, falling back to name and version. |
| U00-58 step 3: texts from `license.id`, from `license.name` via aliases, or from `expression` | ✅ with a deviation | check_licences.py:145-169. The trove-classifier last-segment lookup is ruled on under concern 1 below. |
| U00-58 step 4: outer parentheses stripped, top-level OR, AND, WITH removed | ✅ | check_licences.py:99-142. Handles nesting recursively, a superset of the spec. Malformed text evaluates to False, so the gate fails closed. I probed `"MIT) OR (GPL"`, `"(MIT"`, `"MIT AND"`, `"mit"`, `"MIT or GPL-3.0"` and `""`: all return False. |
| U00-58 steps 5-6: entries are alternatives; approved, then report-only LC002, then denied LC001; no data gives `UNKNOWN` | ✅ | check_licences.py:161-183 |
| U00-58 steps 7-8: report mode writes LC001 as a warning and exits 0; enforce exits 1; malformed input exits 2 | ✅ | check_licences.py:216-229, 240-246. See Minor M1. |
| Initial `[tool.herness.licences]`: 8 allowed, the 10 aliases verbatim, `approved = []`, `report_only = ["nvidia-*"]` | ✅ | pyproject.toml:98-113 (diff lines 15-30). Matches the spec at line 1373 exactly. |
| UT00-69: one merged finding with both IDs, severity 8.1 and a fix | ✅ | test_check_audit.py:75-93 |
| UT00-70: the five expressions give allowed, allowed, allowed, denied, allowed (the last through an alias) | ✅ | test_check_licences.py:51-60 |
| ST00-07: five cases with codes and exits 1/AU001, 0/AU002, 1/AU001, 0/ignored, 1/AU003 | ✅ | test_check_audit.py:105-134. The expired case uses `--today` the day after `expires`. |
| ST00-16: enforce exits 1 with LC001 for GPL and unknown, LC002 for nvidia and pillow `approved`; report exits 0 | ✅ | test_check_licences.py:63-97 |
| Card acceptance: both test files pass and mypy reports 0 errors | ✅ (claimed) | Report: 22 passed; mypy clean. Not re-run, per the reviewer rules. |
| Security: no subprocess, no `shell=True`, no eval or exec; inputs parsed only with `json` and `tomllib` | ✅ | Confirmed across both tools. |
| Line budgets: 300, 250 and 50 | ✅ | 297/300, 250/250 and 3/50. See Minor M6. |

- ⚠️ Cannot verify from the diff:
  - Real pip-audit and osv-scanner JSON (concern 3). The parsers follow the shapes in the spec. Checking them against real output belongs to O-02 and T00-14. Missing or empty `max_severity` becomes None, which counts as High, so an osv shape the parser does not recognise fails closed for any finding that has a fix.
  - Real cyclonedx-py output for the CI runtime venv. It will first be seen in the T00-14 `audit` job.
  - Test and gate results. These are taken from the report, not re-run.

### Rulings on the builder's concerns
1. **Trove-classifier alias lookup (last `::` segment): accepted as faithful to the fail-closed intent.**
   - The lookup applies only when the name starts with `License ::`.
   - It can only resolve through an alias the operator configured, and the resulting SPDX ID must still be in `allowed`. Anything unmapped keeps the full classifier text and is denied. Examples: `Other/Proprietary License`, a bare `OSI Approved`, `MIT No Attribution License`.
   - It does not widen policy. For example, `BSD License` → `BSD-3-Clause` was already the spec's own alias.
   - Condition: record it as an implementation deviation in the spec's deviation or open-items list, so that T00-14 and the spec stay in sync. See also Minor M4.
2. **Enforce mode now vs. report mode until T00-14: keep enforce as the default. This card is correct as built.**
   - The spec's O-03 default (spec line 2335) says: "`nvidia-*` report-only; every other non-allowlisted licence fails the `audit` job until an `approved` entry with approver and date is added".
   - Both CI call sites (spec lines 1398 and 1414) invoke `check_licences` without `--mode`, which means enforce.
   - The 11 denials came from the **dev** venv. It includes dev dependencies and herness installed editable. CI builds the SBOM from `build/runtime-venv` with `--no-install-project`, so herness is absent there.
   - Approval entries are policy work for T00-14 and T00-15 (O-03), not a defect here. Flag to T00-14: numpy (`… AND 0BSD AND Zlib AND CC0-1.0`), pillow (MIT-CMU), regex (CNRI-Python) and the no-metadata packages are probably runtime dependencies and will need approvals or allowlist additions.
3. **Scanners not run: acceptable for this card.** The card's acceptance is the unit tests. O-02 is owned by T00-14.
4. **Line budgets: compliant.** 250/250 is at the limit, not over it. See Minor M6.

### Strengths
- Strict and consistent input validation. The typed `_as` / `_strs` helpers mean every shape error becomes an `_InputError`, which exits 2.
- The ignore file needs exactly its four keys, and a TOML date and a `YYYY-MM-DD` string are both accepted.
- The fail-closed behaviour is right in the places that matter:
  - Unknown severity counts as High.
  - A non-finite severity becomes None.
  - Malformed licence expressions are denied.
  - Allowed-ID comparison is case-sensitive.
  - `UNKNOWN` is used when no licence text is found.
- The merge is transitive, and the severity comes from groups within the same package.
- Tests assert exact output lines and exit codes. They also cover default `--ignore` and `--today` with a frozen clock, malformed-input cases and the repository's own licence table.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- **M1**, tools/check_licences.py:88: `data.get("tool", {}).get("herness", {})` assumes both are tables. I probed a pyproject with `tool = 1`: it raises an uncaught `AttributeError`, prints a traceback and exits 1 instead of 2. It still fails closed, but it breaks the "malformed pyproject table → exit 2" contract. Wrap each level in `_as(dict, …)`.
  - A similar case, tools/check_licences.py:133-142: `evaluate` recurses once per nesting level. I probed `"MIT AND (" * 1200`: it raises `RecursionError` and exits 1, not 2. SBOM input is not adversarial here, so it is cosmetic. Catching `RecursionError` in `run` would restore exit 2.
- **M2**, tools/check_licences.py:77: `approved.package` is lowercased but not PEP 503-normalised. A glob such as `Foo_Bar*` will never match the normalised name `foo-bar`. Normalise the literal parts, or document that globs must already be normalised.
- **M3**, tools/check_licences.py:72-75: `approved_on` accepts any string, for example `"soon"`. check_audit validates its dates strictly, so the two tools are inconsistent. Reusing a `YYYY-MM-DD` check would make an approval's date auditable.
- **M4**, tools/check_licences.py:156-157: the trove handling looks up only the last segment. An operator who adds an alias keyed on the full classifier string gets it silently ignored. Try the full name first, then the last segment. This ties in with the deviation to record under concern 1.
- **M5**, tools/check_audit.py:229-238 and tools/check_licences.py:211-213: package names, versions and IDs from scanner JSON or the SBOM are written to stdout and to the Markdown summary, which is appended to `$GITHUB_STEP_SUMMARY`, without stripping newlines or control characters. The input is low-trust but not adversarial, so this is low risk. Consider replacing `\r`, `\n` and control characters in `_cell` / `_render`.
- **M6**: the helpers `_NAME_RUN_RE`, `normalise`, `_as`, `_strs` and `_InputError` are duplicated in check_audit.py:26-73 and check_licences.py:23-63. check_licences.py sits at exactly its 250-line budget, so any T00-14 tweak will break the budget. A small shared `tools/_inputs.py` would recover about 25 lines in each file. It is outside this card's Files list, so defer it.
- **M7**, tests/unit/tools/test_check_audit.py:137, 166, 180, 201 and tests/unit/tools/test_check_licences.py:100, 135, 152: seven supplementary tests have no test ID. This breaks the letter of global constraint 8 ("Every test function name contains its ID"). There is precedent: `test_rf_*` and `test_cv_*` in tests/unit/core. It is acceptable, but the program should decide the convention once.
- **M8**, tools/check_audit.py:145: a missing `results` key in osv JSON (for example `{}`) is accepted as "no osv data" instead of exit 2. The pip-audit side requires `dependencies`. It still fails closed for fixable findings, because all severities become unknown, which counts as High. For symmetry with the "malformed JSON → exit 2" contract, consider making `results` required.

### Assessment
**Task quality:** Approved
**Reasoning:** Both gates match U00-57 and U00-58 row for row. They use the exact codes and exit semantics, fail closed on unknown severity and on malformed licence data, and the four card tests cover the spec's expected values. The remaining items are robustness polish (exit 1 vs 2 on contrived malformed input, glob and date strictness) and a spec-deviation note for the trove-classifier lookup.
