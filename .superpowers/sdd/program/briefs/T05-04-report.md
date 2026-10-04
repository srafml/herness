# T05-04 report: Model settings, pricing, models.yaml

Status: DONE_WITH_CONCERNS
Commit: 46d2cc4 feat(harness): add model settings, pricing and models.yaml (T05-04)
Worktree: D:\herness\.claude\worktrees\agent-a26e796c1c53753e8

## What was built
- `herness/harness/__init__.py`, `herness/harness/llm/__init__.py`: new L4 package (docstring only).
- `herness/harness/llm/settings.py` (U05-19), 330/330 lines: PricePerMTok, ClientSupports, ClientConfig,
  RoleParams, DepthOverride, AnthropicSettings, DepthDefault, ModelsSection, ToolsSettings, SqlSettings,
  LoopSettings, VerifierSettings, TraceSettings, HarnessSettings, ModelsConfig. All extra="forbid",
  strict, frozen. Imports: stdlib, pydantic, herness.core.errors only (R-03).
  - Rules (1)-(11) implemented. Rule failures raise `ConfigError("<key path>: <rule>", key=<path>)`.
  - Pydantic errors (extra keys incl. R-37 `claim_check`/`claim_checker`, `deciders`, ranges, patterns)
    are converted to ConfigError by a wrap validator on the section roots ModelsSection ("models."),
    HarnessSettings ("harness.") and ModelsConfig (""), using the error loc as key path
    (e.g. `models.fallback.writer[0]`, `harness.sql.blocked_columns[0]`). The ConfigError is raised
    outside the except block: no `__cause__`/`__context__`, and the message never contains the input
    value (plain-text api_key is not echoed; TH05-15).
  - `ClientConfig.name` is filled from the map key by ModelsSection (mismatch -> ConfigError
    `models.clients.<key>.name`). `server` is inferred once for openai_compat clients (D05-08) in a
    before-validator: vllm_endpoint -> vllm; port 11434 -> ollama; else llamacpp.
  - Prices parse from decimal strings (and int/Decimal); floats and bools are rejected; NaN/Inf rejected.
- `herness/harness/llm/pricing.py` (U05-22), 34/60 lines: `cost_usd(usage, prices, /, *, batch=False)`,
  Decimal with a 60-digit local context, quantized to 0.000001 ROUND_HALF_EVEN; reasoning tokens not added.
- `config/models.yaml` (U05-72), 125/180 lines: only `models` and `harness` sections (deciders belongs to
  impl 03). Ports 8000 (vLLM) / 8200 (llama.cpp, local-large-offload) / 11434 (Ollama); local-30b
  context_window 32768; no verifier_claim, claim_check, claim_checker; placeholders "small-cpu-model",
  "judge-model"; 7 blocked columns; timeout_s {15, 30, 120}; zero price maps written out.
- pyproject.toml: `herness.harness` as top layer of "herness layers"; added to "core base is closed"
  forbidden_modules and to "store-no-upward" forbidden_modules; `herness.harness.llm.settings` added to
  "settings modules are leaves" source_modules. tools.check_type_ownership already applies its settings
  rule (OWN050) to every settings.py, so no tool change was needed.
- tests/integration/repo/test_import_contracts_enforced.py (ST00-10): it created `herness/harness` and
  inserted it into the contracts unconditionally; now it only does so when the package / contract entry
  is missing (otherwise FileExistsError and a duplicate layer). Behaviour of the check is unchanged.

## Tests
- tests/unit/harness/test_llm_settings.py: UT05-17 (46 cases incl. 38 parametrized violations, each rule
  (1)-(11), ranges, R-37 keys, `deciders` on ModelsSection directly, no value echo, server inference).
- tests/unit/harness/test_llm_pricing.py: UT05-21 (Opus 0.305160 / batch 0.152580, Sonnet 0.162580 /
  0.081290, Haiku 0.081290 / 0.040645; half-even rounding; reasoning not double-charged), PT05-03
  (hypothesis: non-negative, linear per field, batch halves).
- tests/unit/harness/test_models_yaml.py: UT05-125 (loads via yaml.safe_load + ModelsConfig on the
  models/harness sections; values equal design §5.1.3 table; ports; R-52; R-37; secret refs; <= 180 lines).

RED: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/unit/harness -q -p no:logging`
  -> `ModuleNotFoundError: No module named 'herness.harness'`, 3 errors during collection.
GREEN: same command -> 60 passed. `-k "UT05_17 or UT05_21 or UT05_125 or PT05_03"` -> 60 passed.
Coverage (herness.harness): 100 % line, 100 % branch.

## Gates (all via worktree venv)
- ruff check . : All checks passed; ruff format --check . : 83 files already formatted
- mypy: Success, no issues found in 37 source files
- lint-imports: 8 kept, 0 broken
- tools.check_type_ownership: exit 0; tools.check_module_size: exit 0
- pytest -m "(unit or integration) and not slow" -q -p no:logging: 609 passed, 5 deselected, 1 xfailed

## Deviations / decisions
1. Acceptance check `herness config validate --offline` is deferred to T10-03 (herness.core.config does
   not exist; program ruling). UT05-125 validates the file directly.
2. models.yaml additions beyond design §7 text: claude-sonnet and claude-haiku get
   `api_key: "secret:anthropic.api_key"` and `timeout_s: 600` (impl §9 table: api_key default
   secret:anthropic.api_key, timeout "Claude 600"), and explicit `supports` blocks as U05-72 requires.
   `thinking: "off"` is quoted in role_params because YAML 1.1 (PyYAML) reads bare `off` as false.
3. Not stated in U05-19, added: RoleParams.temperature range 0-2; defaults for ModelsSection
   fallback/depth_overrides/role_params/anthropic/depth, SqlSettings.timeout_s and blocked_columns
   (the 7 columns) and TraceSettings.payload_sample_rate (impl §9 defaults); timeout_s and
   payload_sample_rate must name all three keys (dict min_length=3 over Literal keys); fallback lists
   must be non-empty; base_url must be http(s) with a host and a valid port; api_key must match the
   U10-27 secret-reference pattern (`^secret:[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$`), stricter than
   "starts with secret:".
4. RoleParams fields have no defaults (as in the signature), so every role_params entry sets all three.

## Concerns
- settings.py is exactly at its 330-line budget; to fit, five trivial section classes (DepthOverride,
  DepthDefault, ToolsSettings, LoopSettings, TraceSettings) have no docstring and the blocked-column
  default uses a `# fmt: off` block. Any later change needs a budget raise.
- The integration test edit (ST00-10, impl 00 file) may conflict with other branches touching the same
  test (the D:\herness main checkout shows that file modified in an open merge).
- sonnet/haiku cache prices remain the design defaults (VI-6 open).

## Fix round 1
Commit: 780a4c5 fix(harness): docstrings, settings budget 360, deltas D05-32/33 (T05-04 fix 1)
- I-1: docstrings added to DepthOverride, DepthDefault, ToolsSettings, LoopSettings, TraceSettings;
  the `# fmt: off` block is removed (blocked-column default is a normal formatted tuple).
  settings.py now 342 lines. Budget raised 330 -> 360 in the impl 05 §2 module map
  (docs/impl/05-harness-core.impl.md), commit message notes "sub-controller ruling w02-s05". The note is
  not put in the budget cell because tools.check_module_size requires a bare integer there (same as 94dee19).
- M-1: `# noqa: TRY004` in `_no_float` recorded as delta D05-32 in impl 05 §13.1, and added to the
  §12 "listed suppressions" sentence next to run_agent's PLR0913.
- M-5: new UT05-125 test `test_ut05_125_client_runtime_fields_equal_design` checks timeout_s
  (300/300/3600/300/300/600/600/600), gpu_class, reasoning_parser (qwen3 on local-30b) and thinking_mode
  (adaptive_always/adaptive_optional/budget) for all 8 clients.
- M-6: sonnet/haiku `api_key` and `timeout_s: 600` recorded as delta D05-33 in impl 05 §13.1.
- M-2, M-3, M-4 not addressed (parked).
Gates: ruff check/format clean; mypy clean (37 files); lint-imports 8 kept 0 broken; check_type_ownership 0;
check_module_size 0; card tests 61 passed (herness.harness 100 % line/branch);
pytest -m "(unit or integration) and not slow": 610 passed, 5 deselected, 1 xfailed.
