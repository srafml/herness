# T07-02 report: Memory configuration

Status: DONE_WITH_CONCERNS
Commit: 30b263c feat(harness): add memory configuration (T07-02)
Worktree: D:\herness\.claude\worktrees\agent-a65d277c4bb1cf9a9 (branch worktree-agent-a65d277c4bb1cf9a9)

## What was built
- `herness/harness/memory/settings.py` (315 lines): `MemoryConfig` with the 8 spec-named section models
  (`CompactionConfig`, `RecallConfig`, `WriteConfig`, `EpisodicConfig`, `OutcomeConfig`, `FeedbackConfig`,
  `ProceduralConfig`, `ChatMemoryConfig`) plus small nested models for the yaml sub-maps
  (`ToolGroupKeep`, `RecallWeights`, `RecallCandidates`, `RateLimits`, `DedupeConfig`, `PromoteConfig`,
  `LoraConfig`). All frozen, `extra="forbid"`, `strict=True`, `allow_inf_nan=False`; YAML lists become tuples
  in a before-validator (same pattern as `herness.core.settings`). Defaults equal the shipped yaml.
  Every §9 range and every U07-18 model rule is enforced: target<soft<hard<1; weights >=0 summing to 1 +-1e-9
  (math.fsum); conf/rec floors (0,1]; conflict<merge<=1; delta lo<0<hi; 0<conf lo<hi<1; half_life keys are
  Kind and all 11 present (error names missing kinds); expiry keys are Kind; all values >0; per_metric keys
  limited to the four week keys (settle >=0, others >=1); second_measure_weeks > measure_after_weeks checked
  for the base and for each per_metric effective override; demote < promote.min_pass_lb <= lora.min_pass_lb.
- `injection_patterns: tuple[str, ...]` (default `()`, max 500 entries); each entry is checked non-empty after
  strip, <=500 chars and compiles with `re.IGNORECASE` (errors located at `injection_patterns.<index>`).
- `parse_injection_patterns(text) -> tuple[str, ...]`: pure parser for the pattern file (skips blank and `#`
  lines, strips only the line ending, raises `ConfigError(key="injection_patterns", line=n)` naming the
  1-based file line; >500 patterns rejected). It is the hook for T10-03's loader, which is where "naming
  its line number" can only be done (the model sees the filtered tuple). No file IO in the module.
- `config/memory.yaml`: exactly design 07 §7 with `outcome.window_weeks: 10` (R-34).
- `config/injection_patterns.txt`: the 14 U07-19 patterns verbatim (the markdown `\|` unescaped to `|`),
  3 header comment lines. Pattern 6 `(act|behave) as (an?|the) ` keeps its trailing space (verified in the
  committed blob; a test pins it).
- `pyproject.toml`: `herness.harness.memory.settings` added to "settings modules are leaves"
  source_modules (UT00-58 derives the expected set from the filesystem, so no test edit was needed).
- `Kind` imported as `from herness.core.types import Kind` (the submodule import tripped OWN041/OWN050).

## Tests (tests/unit/harness/memory/test_memory_settings.py, 46 cases)
- UT07-04: shipped yaml dump == design 07 §7 dict (window 10); defaults == shipped; 14 patterns verbatim and
  behaving case-insensitively; half-life covers all kinds; frozen and closed; AST import check (R-03).
- UT07-05: 31 parametrised bad values each located at their key path; weight tolerance 1e-9; missing kind;
  strict rejects "10"/True; bad pattern file lines -> ConfigError naming line 4; comments/CRLF handling;
  >500 patterns (parser and model); model rejects bad pattern at its index.

RED: `PYTHONUTF8=1 uv run pytest tests/unit/harness/memory/test_memory_settings.py -q -p no:logging`
-> `ImportError: cannot import name 'settings' from 'herness.harness.memory'` (1 error during collection).
GREEN: same command plus test_import_contracts -> 48 passed; coverage of settings.py 100% line, 100% branch.

## Gates
- ruff check: All checks passed; ruff format --check: 120 files already formatted
- mypy: Success, no issues in 54 source files
- lint-imports: 10 kept, 0 broken
- check_type_ownership: exit 0
- check_module_size: FAIL MS001 `herness/harness/memory/settings.py` 315 lines > budget 220
- `pytest -m "(unit or integration) and not slow"`: 1078 passed, 1 failed — the failure is
  IT00-02 (test_check_scripts), solely because of the MS001 above.

## Concerns
1. Budget: settings.py is 315 lines vs the §2 budget of 220 (under the 400 hard limit). The spec's config
   surface is 15 section models with ~60 range-checked fields plus 5 cross-field validators; I already removed
   `__all__`, dropped docstrings on trivial sub-models and folded raises into `_require`/`_rule` helpers.
   Getting to 220 would need a sibling module not in the module map or unreadable packing. Request a
   budget raise to 320 in docs/impl/07-memory.impl.md §2 (as for T07-01); that also clears IT00-02.
2. `parse_injection_patterns` is a public helper not named in U07-18; added because the line-number rule
   cannot be met by a model that only receives the filtered tuple. T10-03 should call it.
3. `herness config validate --offline` deferred to T10-03 per program ruling; shipped files are validated
   directly in UT07-04. `ConfigError` conversion of model ValidationErrors is also T10-03's job; UT07-05
   asserts the ValidationError loc names the key path.
4. Mapping fields (`half_life_days`, `expiry_days`, `per_metric`) are plain dicts inside a frozen model
   (same as other settings modules); MappingProxyType triggered pydantic serializer warnings.
