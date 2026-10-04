# Review: T10-13 Config templates

Worktree: D:\herness\.claude\worktrees\agent-af50e19538e054fc0 (head b99ea38, base cd44be5)

## Spec compliance

### Units

- U10-93 config/herness.yaml -- Spec OK. Every field checked line-by-line against
  herness/core/settings.py model defaults (PathsConfig, DataPolicyConfig, SecretsConfig,
  RedactionConfig incl. _DEFAULT_IDS, EgressConfig, NetworkConfig, UiConfig/ExposeConfig/
  RolesConfig, LoggingConfig, RetentionConfig, BackupConfig, DeployConfig and its
  ReasoningDeploy/OpenJevDeploy/LargeDeploy/ServiceDeploy/ReleaseDeploy) and against design 10
  section 4.2/7.2-7.4: all equal. Secure defaults present: security.egress.enabled false,
  security.ui.bind 127.0.0.1 (loopback), security.ui.roles.default_role viewer,
  security.redaction.mask_ip true, security.redaction.denylist_domains [],
  release {repo null, signer_workflow null, licence_exceptions []}. The seven deploy pin
  fields use whole <placeholder> tokens; confirmed empirically that _ARGV_SAFE
  (herness/core/settings.py:31) cannot accept a mixed literal like
  vllm/vllm-openai@sha256:<digest> (its two alternatives are a pure literal or a pure <...>
  token), so the whole-token choice is correct, not a shortcut. Invariant "loads for local and
  synth, C13-only warnings" verified by running UT10-76/IT10-01 against the real repo tree
  (see Tests below): both profiles produce warnings of severity warn only (codes C13, C08a),
  zero errors.
- U10-94 profile overlays -- Spec OK, with one documented, justified deviation. local.yaml is
  version: 1 plus comments only (3 lines). hybrid.yaml: models.models.roles/fallback overlay
  for skeptic_final/writer -> claude-opus matches design 05 section 7 verbatim
  (docs/specs/05-harness-core.md:748); security.egress = {enabled true, destinations
  [api.anthropic.com], purposes [reasoning_final]} matches design 10 section 4.3 table
  verbatim. premium.yaml: role map matches design 05 section 7
  (docs/specs/05-harness-core.md:749) except verifier_claim, which is omitted; confirmed
  empirically that config/models.yaml (shipped by T05-04) has no verifier_claim role today
  (its roles block lists only the local-profile roles), so adding the overlay line would
  create a role with no client backing -- the omission is correctly reasoned and reported as a
  T05-04 carry-over, not silently dropped. security.egress for premium matches the design
  table verbatim ([api.anthropic.com, api.typesafe.ai] / [reasoning, reasoning_final,
  bulk_classification]). Swarm caps correctly omitted per the group ruling
  (HernessConfig.pipelines is still _PipelinesStub, confirmed at herness/core/config.py:70,122)
  and documented as a YAML comment carry-over to T06-03. synth.yaml: sets
  security.secrets.backend dotenv and security.redaction.denylist_domains []; no
  security.egress key at all -- verified by test and by direct read, satisfying "synth.yaml
  never enables egress" both as a static overlay fact and as a runtime invariant enforced by
  check_profile_egress (herness/core/config_sources.py:195-199). No overlay (local/hybrid/
  premium/synth) sets security.data_policy -- verified directly.
- U10-95 .streamlit/config.toml and .env.example -- Spec OK. .streamlit/config.toml has
  exactly the four design 10 section 9.2 keys (server.address = "127.0.0.1",
  server.headless = true, server.enableXsrfProtection = true, browser.gatherUsageStats =
  false), no extras. .env.example: every value empty (verified by reading the file and by
  test_st00_11_env_example_and_gitignore, which still passes unchanged); all six named
  secrets from design 10 section 3.3 present as HERNESS_SECRET__<NAME>=; HERNESS_ENV correctly
  not added as a key per the binding w08-s10 ruling; .gitignore already has .env/
  !.env.example/data/ (pre-existing, test confirms).

### Tests

- UT10-76 -- Spec OK. tests/support/config_tree.py write_repo_config copies the real
  repository config tree (SHIPPED variable, confirmed at line 33) -- genuinely the repo
  templates, not a hand-written stand-in -- for herness.yaml, all four profiles yaml files,
  and the already-shipped owner files, standing in only for the four stems (sources, mappings,
  pipelines, resilience) that have no repo file yet, each guarded by an existence assertion
  that will fail loudly the day one ships.
  Ran pytest with the filter matching UT10_76, IT10_01 and ST00_11: 12 passed (10 UT10-76
  plus 1 IT10-01 plus 1 ST00-11), confirming: local and synth load with only warn-severity
  issues (codes subset of C13, C08a); hybrid raises ConfigError matching the message
  "profile hybrid requires recorded approval" (exact message from herness/core/config.py
  line 185); .env.example has no values and all six named secrets present; no profile
  overlay sets security.data_policy; synth sets no security.egress key; hybrid and premium
  both declare non-empty destinations and purposes (C24 shape). Test IDs (prefixed
  test_ut10_76), docstrings starting with UT10-76, and module-level pytestmark
  pytest.mark.unit all conform to the global-constraints convention.
- IT10-01 -- Spec OK. tests/integration/core/test_config_templates_it.py calls the
  validate function with offline True on the full repo-templates tree via write_repo_config
  and asserts no error-severity issues; ran it directly, passes. pytestmark
  pytest.mark.integration, ID and docstring conform.
- ST00-11 -- Spec OK, still passes unchanged (ran directly).
- tests/unit/core full directory -- Spec OK, 1123 passed.

### Ruling R2 empirical check (owner version 1)

The report claims none of config/decisions.yaml, eval.yaml, models.yaml, memory.yaml can take
an added version: 1 because the owner model or test rejects it. This was verified
independently in a scratch script under the scratchpad directory, never touching the
worktree, that loads each real shipped file, injects version: 1 into the parsed mapping, and
validates it against the actual owner model. Results:
1. decisions.yaml plus version: 1 raises ValidationError from DecisionsConfig (extra field
   forbidden).
2. eval.yaml plus version: 1 raises ConfigError from load_eval_config ("eval config failed
   validation").
3. models.yaml plus version: 1 breaks the literal key-set assertion in
   test_ut05_125_repository_config_loads, and ModelsConfig.model_validate also rejects the
   extra input.
4. memory.yaml plus version: 1 raises ValidationError from MemoryConfig (extra field
   forbidden).

All four rejections confirmed empirically. The claim holds; not applying the ruling was
correct.

### Threats

- TH10-03 (overlay self-approving data policy): mitigated. No overlay sets
  security.data_policy; the gate reads only herness.yaml (the check_gate function in
  herness/core/config.py around line 177).
- TH10-12 (dashboard exposed without proxy, or identity header trusted from anywhere):
  mitigated. security.ui.bind is 127.0.0.1, expose.enabled is false, trusted_proxy is null;
  .streamlit/config.toml binds loopback. The C05 cross-check (loopback bind, trusted_proxy
  requirements) exists in config_validate.py and is exercised by the passing IT10-01 and
  UT10-76 runs.

## Other checks

- detect-secrets: the security.redaction.key line ("secret:redact.hmac_key") false positive
  is suppressed with an inline pragma-allowlist-secret comment (config/herness.yaml line 34);
  confirmed no .secrets.baseline change in commit b99ea38, and a pre-commit run of the
  detect-secrets hook on the touched files passes clean.
- Gates re-run independently: mypy (full project) reports 0 errors across 150 source files;
  lint-imports reports 13 contracts kept, 0 broken; the module-size checker exits 0; ruff
  check and format on the touched Python files are clean.
- config/herness.yaml is 112 lines versus the section-2 module-map row's documented estimate
  of 110; harmless, since check_module_size scans only Python files, and the same reasoning
  was already accepted for .env.example growth in the group ledger. Flagged as Minor only.

## Warnings

- None beyond what is covered above; every card-scoped invariant was exercised by actually
  running the tests against the real repository tree rather than relying solely on the
  report's narrative.

## Findings

### Critical
None.

### Important
None.

### Minor
1. config/herness.yaml is 2 lines over the section-2 module-map row's documented 110-line
   estimate (112 actual). Not enforced by tooling for non-Python files and not a functional
   issue; noted for completeness only (config/herness.yaml, whole file).

## Verdict

Approved
