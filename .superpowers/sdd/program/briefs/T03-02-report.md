# T03-02 report: Settings model

Status: DONE_WITH_CONCERNS
Commit: 1875c00 feat(enrich): add decisions and deciders settings models (T03-02)
Worktree: D:\herness\.claude\worktrees\agent-affa5a39a5189981a

## What was implemented

- `herness/enrich/__init__.py`: new L3 package `herness.enrich`.
- `herness/enrich/settings.py` (393 lines):
  - `DecisionsConfig` (U03-09): every §9 key with its default; no `deciders` field (R-76).
    Section models: `AcceptanceDefaults`, `EmbeddingSettings`, `EscalationSettings`, `SpotCheckSettings`,
    `EnsembleSettings`, `DistillSettings` (+ `ActiveLearningSettings`), `ClusteringSettings`
    (+ `NamingSettings`), `ChangeLinkSettings`, `MappingSuggestSettings` (+ `MappingWeights`).
    Cross-field rules: `question_set_version` checked by building a `QuestionSet` (reuses the
    T03-01 pattern, no private import); `escalation_chain` has no duplicates and does not contain
    `primary_decider`; unique question ids; ≤ 64 questions; `change_link.decider_band` ascending
    within [0, 1]; `mapping_suggest.weights` sum to 1 ± 1e-6; `clustering.k_min ≤ k_max`. The other §9
    rules are enforced too: every numeric range, `assign_min_sim < full_sim`,
    `distill.spot_check_min ≤ spot_check_max`, `per_round ≤ candidates ≤ pool`, abbreviation key pattern
    and no expansion equal to a key, and `change_link.use_decider` needs a `change_caused_pair` question.
  - `QuestionConfig` (U03-10): the `Question` fields except `fingerprint`, plus `acceptance` and
    `primary_decider`. The private `_check_question_fields` applies the `Question` rules by validating a
    `Question` built from the shared fields.
  - `AcceptanceCriteria` (U03-11): optional criteria with their ranges; at least one must be set.
    `AcceptanceDefaults.bool_` has the YAML alias `bool`.
  - `DecidersSettings` (U03-150): `LayaSettings`, `OpenJevSettings`, `JevSettings`,
    `LlmDeciderSettings`, `DepthSamples`, `DepthVotes`. The OpenJev `base_url` must be http(s) with host
    `127.0.0.1` or `localhost`. The Jev `base_url` must be https. No URL may carry userinfo. `api_key`
    must match `^secret:[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$`, declared locally (R-72). The defaults are
    `secret:OPENJEV_API_KEY` (R-53) and `secret:TYPESAFE_API_KEY`.
  - `check_decider_refs` (U03-151): `error` for `jev` while `deciders.jev.enabled` is false, `warn` for
    `openjev` while `deciders.openjev.enabled` is false. It checks the positions `escalation_chain`,
    `primary_decider` and `questions[i].primary_decider`. Messages name keys only.
  - Every model uses `extra="forbid"`, `strict=True`, `frozen=True`, `allow_inf_nan=False` and
    `hide_input_in_errors=True`. The last one keeps a mistyped secret value out of `ValidationError`
    text (TH03-14).
  - A `before` validator converts YAML lists to tuples for the strict tuple fields. Without it, strict
    pydantic rejects a list for a tuple field.
  - Cross-field rules share one `_rules()` hook on the base model. It yields `(holds, message)` pairs,
    and one `model_validator` raises the first broken rule. This keeps the module under 400 lines.
  - The `# T10-12:` comment marks where the owner validator `enrich.deciders` is registered, with the
    exact lambda.
- `config/decisions.yaml`: the design 03 §5.5 and §7 YAML (without `deciders`), plus a
  `change_caused_pair` question (see deviations).
- `config/models.yaml`: only the `deciders` section, with the §9 key names and values.
- `pyproject.toml`: the import-linter contracts changed as follows.
  - `herness.enrich` is a new top layer in "herness layers".
  - `herness.enrich` is added to the forbidden modules of "core base is closed".
  - `herness.enrich.settings` is added to "settings modules are leaves".
  - New contract "enrich-settings-light", analogous to model-settings-light. It also forbids
    `herness.model` and `herness.connectors`.
  - `herness.enrich` is appended to "store-no-upward", whose comment says later cards append packages.

  `tools/check_type_ownership` has no settings registry. It checks every `herness/**/settings.py`
  automatically, and the new module passes (OWN050 clean).
- Tests:
  - `tests/unit/enrich/test_enrich_settings.py`: UT03-08 ×5 (incl. shipped files), UT03-09 ×26.
  - `tests/unit/enrich/security/test_enrich_settings_security.py`: ST03-17 ×8.

  All are marker `unit`, with the ID in the function name and the docstring.

## RED evidence

`PYTHONUTF8=1 uv run pytest tests/unit/enrich -q -p no:logging -k "UT03_08 or UT03_09 or ST03_17"`

```
E   ModuleNotFoundError: No module named 'herness.enrich'
ERROR tests/unit/enrich/security/test_enrich_settings_security.py
ERROR tests/unit/enrich/test_enrich_settings.py
18 deselected, 2 errors in 0.69s
```

## GREEN evidence

The same command, with `--cov=herness.enrich --cov-branch`:

```
herness\enrich\settings.py     255      0      6      0   100%
40 passed, 18 deselected in 0.91s
```

`uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`: 322 passed, 4 deselected.
This includes UT00-58 (import contracts match the repository).

## Gates

- `ruff format --check .`: 53 files already formatted.
- `ruff check .`: all checks passed.
- `mypy`: no issues in 24 source files.
- `lint-imports`: 9 kept, 0 broken.
- `python -m tools.check_type_ownership`: exit 0 (only the INFO "pending owner" lines).
- `tools.check_module_size`: not on this branch, so it was not run.

## Line counts vs budgets

- `herness/enrich/settings.py`: 393 lines. The §2 budget is 320, so it is **over budget**, but it is
  under the ENG 400 hard limit. §9 has about 110 keys spread over 20 section models, each with its own
  range and default. The module was compacted: a shared `_rules()` hook, and no docstrings on the
  plain section classes. Going further would hurt readability. Two ways out: raise the budget to about
  400, or split `DecidersSettings` into its own module (the module map puts it in settings.py).

## Deviations and decisions

1. **The UT03-08 fixture is adapted from the design YAML.**
   - Design §7 writes `api_key_secret: OPENJEV_API_KEY` and `base_url: http://127.0.0.1:8080`.
   - The impl §9 is binding, and so is the test's own expected value
     `cfg.models.deciders.openjev.api_key == "secret:OPENJEV_API_KEY"`. So the fixture and the shipped
     models.yaml use `api_key: "secret:…"` and `http://127.0.0.1:8100` (R-51, R-53, R-72).
2. **A `change_caused_pair` question was added** to the shipped decisions.yaml and to the UT03-08
   fixture.
   - The §9 rule "`change_link.use_decider` requires `change_caused_pair` in `questions`" is enforced.
     With the default `use_decider: true`, the design §5.5 question list alone would fail it.
   - Design §5.10 step 3 says the question is defined in decisions.yaml: bool, `applies_to: [incident]`,
     `scoring_use: false`. Its wording in the shipped file is mine; the spec gives none.
   - If the controller prefers this rule checked only at run time, drop that one rule and its test.
3. **Tests assert pydantic `ValidationError` instead of `ConfigError`,** and load YAML with
   `yaml.safe_load` + `model_validate`, per the program ruling (T10-03 loader not present).
4. **Some §9 rules depend on other specs or on run-time state and are not checked here:**
   - `embedding.path` and `laya.current_file` under the data root and existing on disk (this is
     `resolve_data_path` / `EnrichPaths`, T03-03).
   - `jev.enabled` needs a profile that allows egress (spec 10).
   - The `llm.role` / `clustering.naming.role` roles must exist in impl 05.
   - `laya.fast` needs the operator's parity check.

   Roles are only checked against `^[a-z][a-z0-9_]{0,63}$`, and the paths only for being non-empty.
5. **`DepthSamples` and `DepthVotes` are small models** with the fields `fast`, `standard` and `deep`,
   in place of a free map. With `extra="forbid"` this rejects unknown depths.

## Deferred

- The acceptance check `herness config validate --offline` was skipped, per the ruling. It is
  deferred to T10, which brings `load_config` and `config_validate`, and then the owner-validator
  registration at the `# T10-12:` comment.

## Concerns

- Module size is 393 lines against the 320 budget (see above).
- Deviation 2: the `change_caused_pair` rule is enforced, so the plain design §5.5 YAML fails unless
  `use_decider` is false or the question is present.
