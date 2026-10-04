# T10-03b review (verify agent) — head 3c520b2, base 9b794b5

### Spec Compliance
- ✅ Version rule: strip + `==1` (int, not bool/str) + ConfigError naming the file is the frozen config_sources `_check_version` (config_sources.py:209-213, 253); ST10-37 proves it end to end for all 11 owner files x {2,'1',true,missing}. Owner models not edited (diff touches no herness/**/settings.py).
- ✅ Stubs removed: `_Stub/_MemoryStub/_AppStub` gone; `memory: MemoryConfig`, `app: AppConfig`, `pipelines: PipelinesConfig` (config.py:86-90). `AppSettings` named in the brief does not exist; `AppConfig` is the owner root of app.yaml (ruling accepted).
- ✅ parse_injection_patterns called (T07-02 carry-over, w03-s07:9/34) via `_config_sections.memory_with_patterns` (before-validator config.py:106-110). Line numbers = physical 1-based file lines (BOM, comments, indented comment tested: line 5). Trailing space kept (shipped `(act|behave) as (an?|the) `), CRLF `\r` dropped. Higher-layer list (`--set`) kept and still validated by MemoryConfig -> ConfigError.
- ✅ config.py 285/320; _config_sections.py 70/200 with §2 row + settings-exception/import-order bullets in the same commit; config_sources.py (390) and errors.py unchanged.
- ✅ Import-linter: `_config_sections` added beside `herness.core.config` in "core base is closed" forbidden list and in all three settings-exception ignore lists (layers, store-no-upward, core-must-not-import-store/harness). config.py is not in "settings modules are leaves", so the sibling correctly is not either. 13 kept / 0 broken.
- ✅ YAML values trace to spec: sources.yaml dq/build = impl 02 §9 (all 14 values checked); mappings.yaml = impl 02 §9 template (7 domains, every entry checked) + service_overrides/custom_fields nulls; resilience.yaml = design 08 §7 block verbatim except exactly R-53 bearer_secret, R-43 nightly gpu_class none, + impl 08 §9 retry_after_max_s 86400 (mechanical diff). Omitted `sources:` map justified: reviewer confirmed `sources.files: {}` fails `entities: Field required`, so no default-only section is writable. Omission recorded in the file header and report.
- ✅ detect-secrets inline pragma on resilience.yaml:58 (same form as herness.yaml:34 precedent).
- ✅ UT04-13 strict xfail untouched (XFAIL on run; tests/unit/metrics and metrics.yaml not in diff).
- ⚠️ Goal "the committed config/ tree loads end to end": NOT met for the bare tree (Important 1). Acceptance checks pass on the write_repo_config tree.
- ⚠️ Acceptance says "C13 placeholder warnings only"; actual output also has one C08a warn (Important 2).

### Acceptance-check output (run by reviewer, write_repo_config tree)
- as shipped herness.yaml: local OK [warn C08a, warn C13]; synth OK [C08a, C13]; hybrid/premium ConfigError "profile <p> requires recorded approval in herness.yaml security.data_policy" (correct U10-09 step 5: shipped file records no approval).
- with approval recorded in the copy: local/hybrid/premium/synth all OK; issues = 8x C13 "is not pinned" (deploy.large.gguf/image/sha256, openjev.image/revision, reasoning.image/revision/tool_call_parser) + 1x C08a "not checked: compose file missing" (resilience.gpu.classes; cause: docker/compose.yaml absent from the repo, T10-23). No errors.
- `validate(cfg_dir, "synth", offline=True)`: 9 issues, 0 errors ({C08a, C13}).
- bare repo `config/`: `load_config("local", config_dir=config)` -> ConfigError "decisions.yaml: version must be 1". Cause: decisions/eval/models/memory.yaml ship without `version: 1` (owner direct-load tests UT03-08, UT11-107, UT05-125, UT07-04 reject the key); metrics.yaml `metrics: []` would fail next (T04-08).

### Gates (reviewer run)
pytest tests/unit/core + tests/security/test_st10_config.py + tests/unit/repo: 1522 passed, 1 skipped (symlink privilege). memory/reports/pipelines owner tests: 383 passed, 1 skipped. Coverage config.py 100% line / 100% branch, _config_sections.py 100% / 100%. ruff format/check clean, mypy 0 issues (190 files), lint-imports 13/0, check_module_size 0, check_type_ownership 0.

### Strengths
- Precise, spec-traceable YAML; resilience.yaml is a mechanical copy of the design block plus exactly the named amendments.
- Clean handling of the frozen step-4 limitation; the owner ConfigError (with `line` context) propagates unchanged; no pattern echo.
- ST10-37 matrix is thorough; the UT00-58 edit is narrow (second ignore line required only iff the sibling exists; no wildcard) and required by the brief's layering rule.

### Issues
#### Critical
- none
#### Important (plan-level, controller routing; not a builder defect)
1. Card Goal unmet for the committed tree: bare `config/` fails `decisions.yaml: version must be 1` (config/decisions.yaml, eval.yaml, models.yaml, memory.yaml lack `version: 1`; tests/support/config_tree.py:143 `_UNVERSIONED_STEMS` papers over it in tests). Fix is small (add the line to 4 files + pop `version` in 4 owner tests) but outside card Files; the builder flagged it. Needs a ruling: extend this card, or open owner carry-overs (T03-02, T05-04, T07-02, T11-20) plus T04-08 for metrics.
2. tests/unit/core/test_config_templates.py:~158 accepts `{C13, C08a}` while the acceptance check says C13 only. C08a comes from the missing docker/compose.yaml (T10-23), outside this card; controller should accept the tolerance explicitly and have T10-23 tighten it to C13 only.
#### Minor
3. herness/core/_config_sections.py:44-47 `_file_layer` duplicates config_sources step 4 (config_sources.py:254-256) to detect the file-layer value: if step 4 ever changes, the equality silently fails and the stripped list (trailing spaces lost) reaches MemoryConfig with no error. Only the trailing-space test guards it indirectly; name the coupling in a comment or pin it with a test.
4. _config_sections.py:62-63 re-reads the file (TOCTOU vs the source's read; a file growing between reads could surface a raw UnicodeDecodeError rather than ConfigError). Negligible.
5. tests/unit/core/test_config_load.py:308 still named `test_ut10_84_stub_sections_are_closed` though the stubs are gone (docstring updated only).
6. config/profiles/premium.yaml stale `_PipelinesStub` comment remains (report routes it to T06-03).

### Assessment
**Task quality:** Approved (Important 1-2 routed to the controller as plan-level carry-overs)
**Reasoning:** Everything within the card's Files is correct, spec-traceable, fully covered and gate-clean; the one unmet item (bare-tree load) is blocked by owner files/tests outside the card's scope and is transparently reported.


## Re-review r1 (fix round 1, 3c520b2..30817bb)

### Items
- ✅ I1 closed: `version: 1` added to config/decisions.yaml, eval.yaml, models.yaml, memory.yaml. The owner direct-load tests pop the root key test-only with `assert data.pop("version") == 1`: UT03-08 test_enrich_settings.py:195, UT11-107 test_eval_settings.py:_raw, UT05-125 test_models_yaml.py:_raw (so the raw key-set assertion still holds), UT07-04 test_memory_settings.py:_raw. `_UNVERSIONED_STEMS` and `_versioned` are removed. `write_repo_config` and `write_full_config` now copy every owner file verbatim; the only adaptation left is metrics (`_with_metric_entry`, with an assert message telling T04-08 to drop it). A grep found no production code that reads these files directly; everything goes through load_config.
- ✅ Bare repo `config/` (reviewer run): `load_config("local", config_dir=config)` gives ConfigError "invalid config (1 issues ...): metrics.metrics (metrics.yaml): List should have at least 1 item"; issues = [("metrics.metrics", "metrics.yaml")] only. It is pinned by the new `test_ut10_76_bare_repo_tree_fails_only_on_the_empty_metric_catalog` (local, synth).
- ✅ m3 closed: `test_ut10_76_file_layer_matches_config_sources_step_4` runs the real `FilesYamlSource` inside `load_context` and asserts that its stored list equals `_file_layer(text)`. The input covers CRLF, an indented comment, blank lines, leading/trailing spaces, \x0c, U+2028 and tabs, and the test also pins the expected list.
- ✅ m5 closed: renamed to `test_ut10_84_owner_sections_are_closed` (test_config_load.py:308).
- ✅ m6 closed: premium.yaml comment no longer names `_PipelinesStub`; it now points to PipelinesConfig and records the T06-03 swarm-cap carry-over.
- ✅ No owner model changed: `git diff 3c520b2 HEAD -- herness` is empty.
- ✅ .secrets.baseline: 8/8 changed lines. They are 7 `line_number` bumps (+1: four in models.yaml after the inserted top line, three in test_enrich_settings.py after the inserted pop line) plus `generated_at`. There are 78 hashed_secret entries before and after (none dropped) and 0 CR bytes (LF). `detect-secrets-hook --baseline` on the changed files exits 0.
- Not in scope, as instructed: I2 (accepted, carry-over to T10-23) and m4 (parked).

### Acceptance (reviewer re-run, write_repo_config tree)
- As shipped: local OK, synth OK [warn C08a, warn C13]; hybrid/premium fail the recorded-approval gate (correct).
- With approval recorded in the copy: local/hybrid/premium/synth all OK, issues {C13 x8, C08a x1}, no errors.
- `validate(cfg_dir, "synth", offline=True)`: 9 issues, 0 errors.
- UT04-13 strict xfail still XFAIL.

### Gates
pytest tests/unit/{core,repo,enrich,eval,harness,metrics,reports} + tests/security: 3678 passed, 5 skipped (symlink privilege), 1 xfailed (UT04-13). ruff format (448 unchanged) and ruff check clean, mypy 0 issues (190 files), lint-imports 13 kept / 0 broken. Working tree clean.

### New findings
- none.

**Re-review verdict: Approved.**
