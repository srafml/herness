# T03-14 report — Laya model files and backend

Status: DONE_WITH_CONCERNS (concerns are carry-overs/interpretations; no failing gate)
Worktree: D:\herness\.claude\worktrees\agent-aa6ce388466d1eb9d (branch worktree-agent-aa6ce388466d1eb9d, base c3eee74)
Checkpoints: 24b6a0b wip(laya_models), 3919976 wip(LayaDecider/ST03-07/IT03-16); final: `feat(enrich): add Laya model files and LayaDecider backend (T03-14)` cbe8866.

## Files (lines / budget)
- herness/enrich/laya_models.py — 258 / 260 (U03-115..U03-119)
- herness/enrich/deciders/laya.py — 286 / 330 (U03-57..U03-59 + private `_call_with_timeout`)
- tests/support/fake_laya.py — 170 (FakeLayaAgent, fake_laya_module, manifest_dict, write_laya_version)
- tests/unit/enrich/test_laya_models.py — 359
- tests/unit/enrich/test_laya_decider.py — 381
- tests/unit/enrich/security/test_laya_security.py — 64
- tests/integration/enrich/test_laya_parity.py — 96
- tests/unit/enrich/test_decider_protocol.py — UT03-45 table extended with LayaDecider (constructed without loading)
No pyproject / uv.lock / import-linter changes (laya not added; existing contracts cover the new modules).

## Units
- U03-115 LayaManifest: pydantic, extra=forbid, strict, frozen; version/parent_version pattern; accepted => accepted_by + accepted_at; hyperparams must include trainer/seed/round/round_kind; weights_sha256 must include model.safetensors and rl_agent_config.json, keys are relative names (no `..`), values 64-hex.
- U03-116 read_current: reads <= 65 bytes; > 64, non-ASCII or pattern miss -> ConfigError("laya CURRENT missing or invalid").
- U03-117 write_current: validates version, mkstemp in the same dir, fsync, os.replace; OSError -> FatalError, temp removed.
- U03-118 verify_model_dir: lstat (symlink + Windows reparse-point/junction attribute) -> "directory is a link"; resolved dir must stay under laya_root; rglob: any linked entry -> "linked entry present"; *.bin/*.pt/*.pkl/*.ckpt (case-insensitive) outside checkpoints/ -> "pickle-format file present"; model.safetensors required; manifest <= 256 KB, parses, version matches; status in require_status; every file (except manifest/eval/calibration and checkpoints/) listed in weights_sha256; every listed file present with matching SHA-256. Hashing in 8 MB blocks, memo per (path, size, mtime_ns) under a lock. ConfigError message `<version>: <check>`, context version + check; never contents.
- U03-119 new_version_id: UTC date of an aware `now`; max n of same-day dirs + 1.
- U03-57 LayaDecider: name "laya"; version = arg or read_current; version validated at construction (no disk). load: verify_model_dir -> HF_HUB_OFFLINE=1 -> importlib.import_module("laya") (V-11) -> laya.load(str(dir), fast=settings.fast) (V-11) -> agent.to(device, bf16|fp32) else agent.model.to(...) (V-11); any exception -> ModelUnavailable("laya load"). Idempotent while loaded; unload() drops refs + release_cuda(); after unload the instance refuses to load ("loaded at most once").
- U03-58 decide: auto-loads (load() idempotent); asked = item.question_ids or for_entity minus PAIR_QUESTIONS; grouped by asked ids; >20-option choice without embed_fn -> ConfigError before any model call; narrow questions via run_batches_with_oom_backoff(start_batch=call_batch, fault_name="decider.batch") with fn = _call_with_timeout(predict_batch(batch, wire, batch_size=settings.batch_size, sort_by_length=True), 30.0); result count mismatch -> ModelUnavailable; wide questions via laya.predict_shortlist(agent, state, [q_wire], embed_fn=..., k=16) (V-11), also through the backoff helper (start_batch=1) and the timeout; parts merged per state, parse_wire_answers; incomplete answers or a choice answer without probabilities -> item error output (error="OutputValidationError"); outputs in input order.
- U03-59 health: read_current + verify_model_dir(require_status={"accepted"}); ConfigError -> ModelUnavailable("laya: <reason>").
- `_call_with_timeout` (module-private, `# T08-07: replace with herness.core.resilience.call_with_timeout`): U08-32 steps 1-5 (daemon "herness-timeout", join, ModelUnavailable("call timed out after <t>s"), re-raise unchanged, return value); timeout_s <= 0 -> ConfigError; no on_timeout hook.

## Tests (ID -> functions)
- UT03-111: test_ut03_111_accepted_without_accepted_by_rejected, _accepted_without_accepted_at_rejected, _valid_manifests_parse, _invalid_manifest_fields_rejected (9 cases)
- UT03-112: test_ut03_112_write_then_read, _bad_content_raises (5), _missing_current_raises, _write_invalid_version_rejected, _write_os_error_is_fatal
- UT03-113: test_ut03_113_good_directory_verifies, _checkpoints_are_ignored, _pickle_format_file_rejected (4, incl. model.bin), _symlinked_dir_rejected (symlink; falls back to a real NTFS junction on Windows, so it runs here), _reparse_point_rejected, _wrong_status_rejected, _failed_checks_named (8; the nested-link case skips without symlink privilege), _missing_directory_rejected, _invalid_version_rejected, _hash_memoized_per_stat, _hash_reads_in_blocks, _concurrent_verification_is_consistent
- UT03-114: test_ut03_114_next_id_after_existing, _first_id_and_utc_date, _naive_datetime_rejected
- UT03-55: test_ut03_55_bad_hash_load_raises_config_error, _load_sets_offline_and_moves_agent, _fp32_and_model_attribute_fallback, _missing_laya_package_is_model_unavailable, _load_failure_is_model_unavailable, _version_from_current_and_validation, _unload_releases_and_blocks_reload
- UT03-56: test_ut03_56_batched_calls_and_shortlist_path (300 items -> predict_batch sizes [256, 44], batch_size 64, 300 shortlist calls with k=16), _missing_embed_fn_raises_config_error, _groups_by_asked_questions_in_input_order, _choice_without_probabilities_is_item_error, _malformed_results_are_item_errors (4), _result_count_mismatch_is_model_unavailable, _oom_is_retried_by_backoff, _timeout_is_model_unavailable, _decide_loads_on_first_use, _call_with_timeout_contract
- UT03-57: test_ut03_57_health
- UT03-45 (extended): test_ut03_45_decider_classes_conform[_laya]
- ST03-07: tests/unit/enrich/security/test_laya_security.py::test_st03_07_flipped_byte_refused_on_load_and_accept
- IT03-16: tests/integration/enrich/test_laya_parity.py::test_it03_16_fast_path_argmax_parity [integration, gpu, slow]; skipped unless laya importable, CUDA available and HERNESS_IT_LAYA_DATA set (skipped here).

RED: tests written first; the laya_models test file run with the module moved away -> "1 error during collection" (ImportError). The decider test file was written before the module, but its first run happened after the module existed (no recorded RED run for that file).
GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/enrich tests/integration/enrich/test_laya_parity.py -q -p no:logging` -> 427 passed, 3 skipped. `--require-test-ids` on the card files -> 72 passed, 2 skipped.
Coverage (card tests): deciders/laya.py 100 % line / 100 % branch; laya_models.py 95 % (190 stmts, 9 missed; 3/52 partial branches — OS-error paths).
Gates: ruff format/check clean; mypy strict (herness + tools) 0 issues; lint-imports 13 kept; check_type_ownership ok; check_module_size exit 0; pre-commit hooks passed on the checkpoint commits.

## Deviations / spec notes
- dtype follows `settings.dtype` (`bf16` -> torch.bfloat16, `fp32` -> torch.float32), where U03-57 step 4 says torch.bfloat16 only; the default setting (`bf16`) gives the spec's behaviour.
- verify_model_dir also fails when a file outside checkpoints/ (other than manifest/eval/calibration) is not listed in weights_sha256 ("file not listed in manifest") and when any entry is a link. Rationale: U03-115 defines weights_sha256 as covering every such file, and the U03-115 invariant (tokenizer files covered) can only be enforced this way; it closes file substitution (TH03-05). The distill writer must list every file it writes.
- LayaManifest also enforces the "hyperparams includes trainer, seed, round, round_kind" wording and the two required weight names; tokenizer coverage is enforced by the directory check above (tokenizer file names vary).
- new_version_id rejects a naive `now` (ConfigError) so the date is unambiguously UTC.
- "Loaded at most once per instance": load() is idempotent; after unload() the instance raises ModelUnavailable("laya decider was unloaded") on load/decide. decide() calls load() itself (auto-load on first use).
- The embed_fn check runs over all items before any model call (not per group).
- A predict_batch result-count mismatch is backend-level (ModelUnavailable), not an item error.
- Shortlist calls also go through run_batches_with_oom_backoff (start_batch=1) and the 30 s timeout — spec step 4 names neither; added for consistency.
- The shortlist result is expected to carry a full-label `probabilities` distribution (parse_wire_answers requires all labels); V-11 decides.
- ST03-07 pins a distinct mtime after the byte flip so a coarse filesystem clock cannot leave the (path, size, mtime_ns) memo key unchanged inside one test process; the memo is per process as spec'd (U03-118).

## Carry-overs
- V-11 call sites (marked `# V-11:` in deciders/laya.py): importlib.import_module("laya") / repo path; laya.load(path, fast=...); device move agent.to(device, dtype) vs agent.model.to(...); predict_batch result shape incl. choice `probabilities`; laya.predict_shortlist(agent, state, [q_wire], embed_fn=..., k=16) argument shape (q_wire = to_wire_questions([q])).
- T08-07: replace `_call_with_timeout` in deciders/laya.py with herness.core.resilience.call_with_timeout (marked).
- T03-16: register LayaDecider as ("decider", "laya").
- IT03-16: run on the dev box (laya installed, CUDA, HERNESS_IT_LAYA_DATA=<data root with an accepted CURRENT>) before enabling deciders.laya.fast.
- The caller of new_version_id (distill) must mkdir(exist_ok=False) and convert FileExistsError -> StoreBusy.

## Concerns
- laya_models.py is at 258/260 — no room for additions.
- The hash memo is a module-global dict that is never evicted (bounded by the distinct weight files seen per process; small).

## Fix round 1 (review T03-14-review.md, 5 minors)
1. Item failures now log the catalogued `enrich.decide.item_failed` (WARNING, `decider`, `error_class`); the uncatalogued `enrich.decider.laya_loaded` was dropped (§8.1 has no load event).
2. HF_HUB_OFFLINE no longer leaks: autouse fixture in test_laya_decider.py (setenv then delenv so monkeypatch restores the original); same in ST03-07.
3. The nested "linked entry present" case now falls back to an NTFS junction on Windows (runs here; no skip left in test_laya_models.py).
4. read_current raises `from None`; the manifest ValidationError is suppressed and ConfigError raised outside the except block (no __context__ holding input fragments). laya_models.py stays 258/260.
5. dtype note added under Deviations.
Commit: 369a29d. Verification: `PYTHONUTF8=1 uv run pytest tests/unit/enrich tests/integration/enrich/test_laya_parity.py -q -p no:logging` -> 428 passed, 2 skipped (IT03-16 gated, one pre-existing layout symlink skip). Coverage: deciders/laya.py 100 % line/branch, laya_models.py 96 %. ruff, mypy strict, check_module_size exit 0.
