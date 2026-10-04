# T03-16 report — Ensemble and registration

Status: DONE_WITH_CONCERNS (all gates green; concerns are spec notes, see below)
Worktree: D:\herness\.claude\worktrees\agent-adbb66aff419524b3 (branch worktree-agent-adbb66aff419524b3, base a46205f)
Commits: 78b6cd1 wip(T03-16): ensemble pooling, registration and factory with tests (all code); 924da66 feat(enrich): T03-16 ensemble pooling, decider registration and factory (empty closing commit, message only)

## Files (lines vs budget)
- herness/enrich/deciders/ensemble.py — 196 / 200 (new): `pool_log_linear` (U03-65), `ensemble_version` (U03-66), `EnsembleDecider` (U03-67), `EnsembleMember` type alias.
- herness/enrich/deciders/__init__.py — 89 / 90: `register_deciders` (U03-68), `build_decider` (U03-69); backend classes imported lazily inside the functions (no eager import of every backend; no cycle via herness.core.config — checked in fresh interpreters in both import orders).
- tests/unit/enrich/test_ensemble.py (new): UT03-63, UT03-64, UT03-65, PT03-07 (hypothesis, as the other PT03 tests).
- tests/unit/enrich/test_decider_registration.py (new): UT03-66, UT03-67.
- tests/unit/enrich/test_decider_protocol.py: UT03-45 table extended with `EnsembleDecider` (earlier-card practice).
No registry.py / _BUILTINS / pyproject changes; no openjev.py edits.

## Tests
- RED: `uv run pytest tests/unit/enrich/test_ensemble.py tests/unit/enrich/test_decider_registration.py` -> ImportError: cannot import name 'build_decider' from 'herness.enrich.deciders' (collection errors, 2).
- GREEN: card files + UT03-45: 45 passed. `-k "UT03_63 or UT03_64 or UT03_65 or UT03_66 or UT03_67 or PT03_07"`: 36 passed.
- `uv run pytest tests/unit/enrich -q -p no:logging`: 720 passed, 1 skipped (symlink privilege).
- Coverage (branch): ensemble.py 100 % line / 100 % branch; deciders/__init__.py 100 % / 100 %.
- Gates: ruff format/check clean, mypy 0 issues (262 files), lint-imports 13 kept, check_module_size clean, check_type_ownership clean; commit hooks (incl. pytest-unit) passed with no SKIP.

## Deviations / spec notes
1. `EnsembleMember` (module-map export, no unit row): `type EnsembleMember = tuple[str, str]` = (decider, decider_version).
2. `build_decider` for `jev`: JevSettings has no `samples`; "Jev: same" read as `samples_override or settings.openjev.samples[depth]`.
3. OpenJev `image_tag`: text between the last `:` of the image's final path segment and `@` (so `host:5000/repo:tag@...` works). Without a tag the first 12 hex of the digest are used; neither tag nor digest (e.g. a `<placeholder>`) -> ConfigError("deploy.openjev image has no tag or digest").
4. `build_decider` looks classes up in the registry as specified (it does not call `register_deciders` itself; unregistered -> registry ConfigError). Name `ensemble` -> ConfigError (it is built by run_ensemble_pool, not the factory). `llm` without the tuple -> ConfigError. Disabled -> ConfigError("decider <name> disabled"); jev without key -> AuthError("decider jev has no api key") (no secret name/value in the message). Secrets resolved only here via secrets.exists/resolve.
5. EnsembleDecider details the spec leaves open: members must name distinct deciders (weights are keyed (decider, qid)) else ConfigError; any present member without a weight for the question -> equal weights for that question (F03-08 step 4; the `ensemble_weights_default` warning is left to U03-89); label order bool (true, false) [column 0 = p(true) for apply_temperature], score 0..3, choice = options order then unseen keys sorted; each member vector is renormalized over that label space (all zero -> uniform) before calibration; per (hash, question, decider) the latest `decided_at` row wins, ties broken by sorted distribution content so the pick does not depend on part-file order; a row counts only when its fingerprint equals the current fingerprint of its own question and its version is the member's; questions follow `_asked` (item.question_ids or entity subset without pair questions); items without rows get an output with empty answers (protocol: one output per input). Cache OSError -> io_error (StoreBusy for EACCES/EBUSY, else FatalError). `health()` no-op. Never calls any client.
6. `pool_log_linear` also raises ConfigError for unequal/non-vector lengths and negative or non-finite weights (preconditions made explicit). Weight absent from the mapping counts as 0.
7. `build_decider` needs `# noqa: PLR0913` (binding 7-parameter signature); `embed_fn` typed `Callable[..., object] | None`.
8. UT03-67 tests pass a SimpleNamespace standing in for HernessConfig (only `models.deciders` and `deploy.openjev.image` are read); secrets via the `fake_keyring` fixture.

## Concerns
- Cross-spec: impl 10's strict deploy pin for `image` (`[a-z0-9][a-z0-9._/-]{0,200}@sha256:<64 hex>`) allows no `:tag`, while U03-52 derives `image_tag` from "the text between `:` and `@`" (and UT03-50 uses `razorback16/openjev:0.4.0@sha256:...`). With a strictly pinned image there is no tag; this card falls back to the digest prefix (note 3). Controller may want a ruling.
- Budgets are tight (196/200, 89/90); two `# fmt: skip` lines used to stay within them.

## Fix round 1 (review T03-16-review.md, Minor findings)
- m1 done: `_temperature` maps an OSError from `CalibrationStore.temperature` through `io_error` ("cannot read calibration": StoreBusy for EACCES/EBUSY, else FatalError); test `test_ut03_65_calibration_lock_is_store_busy`. ensemble.py now 200 / 200.
- m4 done: (a) `test_ut03_65_row_of_member_name_with_other_member_version_dropped` (decider `llm` carrying laya's version passes both isin filters and must be dropped by the pairing check); (b) `test_ut03_64_version_is_pinned_for_a_fixed_input` pins `87865117b6d8`; (c) `test_ut03_65_choice_question_uses_member_temperature` (choice question, openjev T = 0.5, exact pooled values).
- m5 done: `decider.health()` called without asserting its return value.
- m7 done: `build_decider` checks `enabled` for openjev/jev before `registry.get`; `test_ut03_67_disabled_reported_before_registry_lookup` (empty registry, both names). __init__.py 89 / 90.
- Commit: cf92066 fix(enrich): T03-16 review round 1 (hooks passed, no SKIP).
- Gates: ruff, mypy (0), lint-imports (13 kept), module size, type ownership clean; card tests 51 passed (100 % line/branch on both modules); tests/unit/enrich 726 passed, 1 skipped.
