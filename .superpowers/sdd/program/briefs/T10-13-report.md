# Report for T10-13: Config templates

Status: DONE. Commit b99ea386dfec404001733ff82cace9bfa5a8a341 on branch
worktree-agent-af50e19538e054fc0 (base cd44be5).

## Files changed

| File | Lines | Kind |
|---|---|---|
| config/herness.yaml | 112 | new |
| config/profiles/local.yaml | 3 | new |
| config/profiles/hybrid.yaml | 18 | new |
| config/profiles/premium.yaml | 40 | new |
| config/profiles/synth.yaml | 18 | new |
| .streamlit/config.toml | 9 | new |
| .env.example | +4/-0 (6 -> 10 key lines) | modified |
| tests/support/config_tree.py | +58 | modified (new write_repo_config, docstring update) |
| tests/unit/core/test_config_templates.py | 146 | new (UT10-76, 10 tests) |
| tests/integration/core/test_config_templates_it.py | 35 | new (IT10-01, 1 test) |

## Design defaults taken, and from where

- config/herness.yaml paths, security.data_policy, security.secrets, security.redaction,
  security.egress, security.network, security.ui, logging, retention, backup: every value
  copied verbatim from design 10 section 4.2, 7.2, 7.3 (all equal the herness/core/settings.py
  pydantic defaults; confirmed against the model file, not just the design prose).
- deploy.*: structure from design 10 section 7.4. Values with model defaults that already
  satisfy the design (served_name local-30b, gpu_memory_utilization 0.90, max_model_len 32768,
  reasoning_parser qwen3, ports, gpu_util, max_num_seqs, canvas, ctx, gpu_layers, wsl_distro,
  model_root, env_file, service.*) are written out explicitly for documentation. openjev.model
  uses the real HF repo id (design default, not a placeholder). reasoning.model uses the real
  design value Qwen/Qwen3-30B-A3B-Instruct (an HF repo id, not a secret/digest, so it is not a
  placeholder). The seven fields that need externally-supplied pins
  (reasoning.image/revision/tool_call_parser, openjev.image/revision,
  large.image/gguf/sha256) get whole-value <placeholder> tokens, because DeployConfig's _Safe
  pattern (herness/core/settings.py _ARGV_SAFE) only accepts a fully argv-safe string OR a
  whole <...> token, never a mixed literal like the design prose's
  "vllm/vllm-openai@sha256:<digest>" (verified that string does not parse and is illustrative
  only; the existing test fixture tests/support/config_tree.py HERNESS_YAML already uses the
  same whole-token convention, e.g. "<reasoning-image>"). release: {repo: null,
  signer_workflow: null, licence_exceptions: []} per the brief, matching ReleaseDeploy's
  defaults.
- config/profiles/hybrid.yaml, premium.yaml: role/fallback overlays from design 05 section 7
  profile overlay bullets, restricted to the role keys that actually exist in the shipped
  config/models.yaml (planner, judge, analyst, skeptic, skeptic_final, writer, chat, triage,
  enrich_decider, cluster_namer). Design 05 section 7's premium bullet also lists
  verifier_claim; the shipped models.yaml has no such role yet (T05-04 has not added it), so it
  is omitted rather than inventing a role with no client backing -- see Deviations.
- .streamlit/config.toml: the four keys and values verbatim from design 10 section 9.2.
- .env.example: the two secret names from design 10 section 3.3's table, converted to
  HERNESS_SECRET__<NAME> per the dotenv-backend naming rule (section 3.3): anthropic.api_key ->
  ANTHROPIC_API_KEY, TYPESAFE_API_KEY (already upper snake case, no dot).

## Verification (empirical, not just design reading)

Confirmed by direct load_config/validate calls against a copy of the real config/ tree
(write_repo_config) before writing the test suite:
- local and synth load cleanly; the only cross-check issues are C13 (unpinned deploy
  placeholders, 7 keys) and C08a warn ("compose file missing", docker/ not shipped yet,
  T10-23). Zero errors, matching U10-93's invariant.
- hybrid raises ConfigError: profile hybrid requires recorded approval in herness.yaml
  security.data_policy (the gate), matching the brief's acceptance check.
- premium, with a temporary local approval patch (not committed), loads cleanly too and
  models.roles.planner == "claude-opus" -- confirms the overlay itself is well-formed even
  though herness.yaml's default premium_approved: false gates it like hybrid.
- validate(cfg_dir, "synth", offline=True) (IT10-01's exact call) returns zero error-severity
  issues.

## Ruling R2 (owner-file version: 1): none of the four candidate files could take it

Checked all four candidates by constructing each owner model directly with a version: 1 key
added to the real file's parsed YAML:

- config/decisions.yaml -> DecisionsConfig.model_validate raises (extra="forbid", no version
  field).
- config/eval.yaml -> herness.eval.settings.load_eval_config raises ConfigError for the same
  reason.
- config/models.yaml -> not even attempted against the model; tests/unit/harness/
  test_models_yaml.py::test_ut05_125_repository_config_loads asserts
  set(_raw()) <= {"models", "harness", "deciders"} directly on the raw YAML keys, which a
  version key would break outright.
- config/memory.yaml -> MemoryConfig.model_validate raises (extra="forbid", no version field).

config/metrics.yaml already has version: 1 (T04-08); config/weights.yaml already has it too
(found while checking, not called out in the brief). config/app.yaml also already has
version: 1 (see the deviation below on stub-typed files).

No changes were made to any of these four files. Reported as a carry-over, one line per owner:
- config/decisions.yaml -> owner impl 03 (herness.enrich.settings.DecisionsConfig)
- config/eval.yaml -> owner impl 11 (herness.eval.settings.EvalConfig)
- config/models.yaml -> owner impl 05 (herness.harness.llm.settings.ModelsConfig)
- config/memory.yaml -> owner impl 07 (herness.harness.memory.settings.MemoryConfig)

Updated tests/support/config_tree.py's module docstring to record that this is now a
confirmed-permanent gap (not "not yet fixed"), per the ruling's instruction.

## Deviations

1. config/profiles/premium.yaml omits verifier_claim. Design 05 section 7's premium overlay
   bullet lists a verifier_claim: claude-haiku role mapping, but the shipped
   config/models.yaml (T05-04) has no verifier_claim role yet. Adding it via the overlay would
   technically validate (roles/fallback are open dict[str, str]), but it would create a role
   key nothing else references. Left it out and flagged it here; T05-04 should add both the
   models.yaml role and this overlay line together once verifier_claim exists.
2. write_repo_config does not copy config/app.yaml and config/memory.yaml verbatim, even
   though both are real, already-shipped files (a stricter reading than R3's literal text).
   Verified empirically that HernessConfig still mounts app and memory as closed no-field
   stand-ins (_AppStub, _MemoryStub in herness/core/config.py) pending the T09 (AppConfig) and
   T07 (MemoryConfig) wiring cards -- validating either file's real content against its stub
   raises ValidationError today, independent of this card (reproduced directly:
   _AppStub.model_validate on app.yaml minus version, and the memory equivalent, both fail with
   extra_forbidden on every top-level key). Since U10-93 requires local/synth to actually load,
   write_repo_config stands in version: 1 for app and memory, exactly like the existing
   write_full_config already does. Carry-over: whichever card wires AppConfig/MemoryConfig
   into HernessConfig should also update write_repo_config to copy these two files verbatim
   (drop them from _STUBBED_STEMS).
3. Everything else in R3 was followed literally: sources.yaml, mappings.yaml, pipelines.yaml,
   resilience.yaml were not created; write_repo_config asserts each is still missing from the
   repo (tests/unit/core/test_config_templates.py::
   test_ut10_76_stand_in_stems_still_have_no_repo_file and the assertion baked into
   write_repo_config itself), so it starts using the real file automatically once any of them
   ships. metrics.yaml's metrics: [] gets the same one-entry stand-in write_full_config already
   uses. write_full_config itself is untouched (still used by ~20 other test files).

## Carry-overs

- T05-04: add verifier_claim role/client + the premium overlay line once it exists
  (deviation 1 above).
- Wiring card for T07/T09 (whichever card sets HernessConfig.app/memory to the real
  AppConfig/MemoryConfig): update tests/support/config_tree.py write_repo_config to copy
  app.yaml/memory.yaml verbatim instead of standing them in (deviation 2 above).
- T06-03 (herness.core.pipelines.settings.PipelinesConfig): config/profiles/premium.yaml needs
  a 50-200 parallel swarm-cap overlay per design 10 section 4.3; documented as a YAML comment
  in the file (Ruling R4) since HernessConfig.pipelines is still _PipelinesStub (no fields) and
  cannot accept it.
- T11-16 (synth profile agreement check, this card's own forward dependency):
  config/profiles/synth.yaml needs the mappings.* custom-field IDs/enum maps, the
  sources.sources.files entry for service_costs, and the sources.sources.servicenow field
  rename (acknowledged_at <- u_acknowledged_at) per design 11 section 5.1.2.
  config/sources.yaml (impl 01/02) and synth_mappings.yaml (impl 11) haven't shipped, so this
  content would have to be invented rather than read from an owner file; documented as a YAML
  comment in the file.
- O-8 (denylist domains): security.redaction.denylist_domains: [] in both herness.yaml and
  profiles/synth.yaml, empty until Phase 1 supplies the corporate domain list, per the brief.

## detect-secrets handling

One false positive: config/herness.yaml's security.redaction.key: "secret:redact.hmac_key"
line was flagged as a "Secret Keyword" hit (the field name key matches the detector's keyword
list; the value is a secret: reference, never a real value). Resolved with an inline
"# pragma: allowlist secret - a secret: reference, not a value" comment on that line, per the
brief's instruction to prefer this over adding a baseline entry. No other placeholder
(<reasoning-image-digest>, <tool-call-parser>, etc.) triggered the scanner -- they contain no
high-entropy or hex-like content.

## Gate results

All green on this worktree (base cd44be5):

- uv run ruff format . -- 352 files already formatted, no changes needed.
- uv run ruff check --fix . / uv run ruff check . -- all checks passed.
- uv run mypy -- Success: no issues found in 150 source files.
- uv run lint-imports -- 13 contracts kept, 0 broken.
- uv run python -m tools.check_module_size -- exit 0, no violations.
- PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging --
  4161 passed, 5 skipped (pre-existing, platform symlink privilege / no coverage.json), 17
  deselected (slow), 1 xfailed (pre-existing test_it00_02_check_scripts_pass_on_repo, unrelated
  doc-consistency debt). Also ran tests/unit (3723 passed) and tests/integration (29 passed,
  1 xfailed) separately as a sanity check before the combined run.
- uv run pre-commit run --all-files -- all 17 hooks passed (after the detect-secrets pragma fix
  above), including pytest-unit, module-size, type-ownership, fixtures-pii-scan.

No test outside tests/unit/core/test_config_templates.py and
tests/integration/core/test_config_templates_it.py needed changes; in particular every owner
test named in the brief (test_enrich_settings.py, test_eval_settings.py,
test_memory_settings.py, test_models_yaml.py) still passes unchanged, and all existing
tests/support/config_tree.py callers (write_full_config, write_checked_config,
register_checked_names, CHECKED_NAMES) are untouched.
