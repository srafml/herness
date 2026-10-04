# T05-04 review (Model settings, pricing, models.yaml) — base 311ed52, head 46d2cc4

**Verdict: Needs fixes** (1 Important, 0 Critical)

### Spec Compliance
- ✅ U05-19 section models: every signature class present, all `extra="forbid"`, `frozen=True` (+ `strict=True`, required by ENG-STANDARDS §108 for config trust boundaries). `ModelsSection` has no `deciders` field (R-76); imports are stdlib, pydantic, `herness.core.errors` only (R-03).
- ✅ Rule (1) `secret:` api_key: uses the exact U10-27 settings pattern (impl 10 line 612 tells owner settings modules to restate it), so the "stricter than starts-with" deviation is the mandated one.
- ✅ Rule (2) loopback / off_network https: `urlsplit().hostname` beats userinfo tricks (`http://127.0.0.1@evil`); invalid port and non-http schemes rejected.
- ✅ Rules (3), (4), (5), (6), (7) including D05-08 server inference; (8), (9), (10) on ModelsSection; (11) blocked_columns pattern.
- ✅ Failures raise `ConfigError("<key path>: <rule>")` with key paths like `models.fallback.writer[0]`, and the error carries no cause, context or input value (TH05-15).
- ✅ R-37: `claim_check` and `claim_checker` fail `extra="forbid"` with a ConfigError naming the key.
- ✅ U05-22 `cost_usd`: signature (positional/`*`/batch), formula, batch 0.5 applied before a single ROUND_HALF_EVEN quantize to 6 places, reasoning tokens not added. I hand-checked the UT05-21 values (Opus 0.305160/0.152580, Sonnet 0.162580/0.081290, Haiku 0.081290/0.040645).
- ✅ U05-72 models.yaml: R-51 ports (8000, 8200, local-large-offload on 8200), R-52 32768, verifier_claim/claim_* omitted, placeholders, 7 blocked columns, timeout_s {15, 30, 120}, zero price maps written out, Anthropic supports blocks as mandated, 125/180 lines.
- ✅ UT05-17: all 11 rules plus ranges, R-37, `deciders` on ModelsSection directly, no value echo. UT05-21 ✅. UT05-125 ✅. PT05-03 ✅ (non-negative, linear per field, batch halves).
- ✅ TH05-13 (loopback-only validator), TH05-15 (secret refs only, no echo).
- ⚠️ Acceptance check `herness config validate --offline`: deferred per program ruling (T10-03 absent). UT05-125 stands in.
- ⚠️ Cannot verify from diff: the reported gate and test results (60 passed; 609 passed full suite; 100 % coverage; lint-imports 8 kept). Accepted as the builder's evidence.
- ⚠️ U05-72 says "contents equal design §7 exactly". claude-sonnet and claude-haiku gain `api_key: "secret:anthropic.api_key"` and `timeout_s: 600`, which design §7 does not have. These are justified by the impl §9 defaults table (Claude timeout 600, api_key default). Judged acceptable, but they are recorded as a deviation.

### Judgement of declared deviations
1. Extra validation (temperature 0–2, all three depth/trace keys required, non-empty fallback lists, http(s)+host+valid port, U10-27 secret pattern, positive ints, ModelsSection/Sql/Trace defaults from impl §9): accepted. Each value is stricter or a documented default, and none contradicts U05-19.
2. Edit of impl 00 ST00-10 (tests/integration/repo/test_import_contracts_enforced.py:22-49): accepted and necessary. `mkdir()` would raise `FileExistsError` now that `herness/harness` is real, and the tomllib guard prevents a duplicate layer. The check's intent (upward import from core fails naming both contracts) is unchanged. Merge-conflict risk is noted for the integrator.
3. settings.py at exactly 330/330 lines with `# fmt: off` and no docstrings on five public classes: **not accepted**, see Important I-1.
4. store-no-upward entry `herness.harness` (pyproject.toml:587): accepted. The contract's own comment says "later cards append packages", and L4 must not be imported by the store.

### Strengths
- A clean single conversion point (`_Section` wrap validator plus `_PREFIX`) turns pydantic errors into key-path ConfigErrors. It is raised outside the `except` block, so there is no cause or context, and it uses `include_input=False`. Tests assert that no value is echoed and that `__cause__`/`__context__` are None.
- Prices reject float/bool/NaN/Inf, and a 60-digit local Decimal context avoids precision loss.
- Tests are behavioural and parametrized with exact expected key paths. The property tests use whole-dollar prices where exact linearity holds.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
- **I-1 Public classes without docstrings, traded away to meet the line budget.** Affected: herness/harness/llm/settings.py:214 `DepthOverride`, :225 `DepthDefault`, :276 `ToolsSettings`, :294 `LoopSettings`, :307 `TraceSettings`. This violates ENG-STANDARDS §116 and Definition of Done §8 item 6 ("Docstrings exist for every new public symbol"). The `# fmt: off` block at :37-43 exists only to save lines. A binding standard may not be silently traded for a budget. Fix: add the five docstrings, drop the `# fmt: off` block, and request a sub-controller budget raise for settings.py (precedent: the core/types/memory.py raise, commit 94dee19). The file would be about 345 lines, well under the ENG 400 hard limit.

#### Minor (Nice to Have)
- M-1 herness/harness/llm/settings.py:68 `# noqa: TRY004` is a new suppression (DoD §8 item 3). It is justified, because pydantic converts only ValueError, but it should be listed in impl 05 or the controller's deviations log. The same applies to `# fmt: off` if I-1 keeps it.
- M-2 herness/harness/llm/settings.py:149 / :178-188 `_check_anthropic`: an anthropic client with `off_network: false` (the default when omitted) validates. U05-31's egress cross-check (impl 05 line 687) keys on `off_network`, so it would skip that client. The Anthropic adapter's own egress precondition (impl 05 line 614) still guards construction, so this is not an exploitable gap today. Suggest a rule "anthropic ⇒ off_network true", or a note for the U05-31 card.
- M-3 herness/harness/llm/settings.py:234-237, :291: `frozen=True` does not freeze the dict and list contents (`roles`, `fallback`, `blocked_columns`, …). "Validated immutable config" holds only shallowly. Consider tuples/`Mapping` or a note.
- M-4 herness/harness/llm/settings.py:62 `_convert` reports only the first pydantic error. That is acceptable, but a config with several errors needs several fix/validate rounds.
- M-5 tests/unit/harness/test_models_yaml.py:80-97 checks the §5.1.3 table columns, but not `timeout_s` (300 / 3600 / 600), `gpu_class`, `reasoning_parser` or `thinking_mode` for opus/sonnet. Part of "contents equal design §7" is untested.
- M-6 config/models.yaml:56-67: the sonnet/haiku `api_key`/`timeout_s` additions (see ⚠️) should be recorded in the deviations log so the "exactly design §7" postcondition is formally reconciled.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation, the rules (1)-(11), pricing, models.yaml and the tests are correct and well-built. The one Important issue is that five public classes have no docstrings, contrary to Definition of Done §8.6. That is a binding standard traded for the line budget, and the fix is to add the docstrings with a budget raise rather than keep the omission.

---

## Re-review round 1 (fix commit 780a4c5, diff 46d2cc4..780a4c5)

**Verdict: Approved**

Scope: I-1, M-1, M-5 and M-6. M-2, M-3 and M-4 are parked by sub-controller ruling. The settings.py budget raise to 360 is accepted per ruling.

- ✅ I-1 resolved. `DepthOverride`, `DepthDefault`, `ToolsSettings`, `LoopSettings` and `TraceSettings` now have docstrings (herness/harness/llm/settings.py, around lines 217, 232, 286, 306, 321). The `# fmt: off`/`# fmt: on` block is removed and the blocked-column default is a normally formatted tuple. The file is 342 lines, within the 360 budget. The budget cell in impl 05 §2 is a bare integer, as check_module_size requires.
- ✅ M-1 resolved. `# noqa: TRY004` is listed in the impl 05 §12 "listed suppressions" sentence and recorded as D05-32 in §13.1 with its rationale.
- ✅ M-5 resolved. `test_ut05_125_client_runtime_fields_equal_design` (tests/unit/harness/test_models_yaml.py, around lines 95-115) checks timeout_s, gpu_class, reasoning_parser and thinking_mode for all 8 clients. The expected values match design §7 plus the impl §9 Claude timeout of 600. I spot-checked local-30b (300, reasoning, qwen3), local-large-offload (3600, large) and haiku (budget).
- ✅ M-6 resolved. The sonnet/haiku `api_key` and `timeout_s: 600` completion is recorded as D05-33, which reconciles U05-72's "equal design §7 exactly".
- No new issues were introduced. The diff touches only docstrings, formatting, one test and the spec text.
- ⚠️ The gate results (610 passed; 100 % line/branch; ruff, mypy, lint-imports and module-size clean) are the builder's claims and were not re-run.

Open findings: none in scope. M-2, M-3 and M-4 remain parked by ruling.
