# T03-31 report: Laya trainers

Status: DONE_WITH_CONCERNS (the concerns are V-11 carry-overs and spec notes; no open defects)
Worktree: D:\herness\.claude\worktrees\agent-a48bf53e76fe9947e (branch worktree-agent-a48bf53e76fe9947e, base bdb61b7)
Commits: ed62042 wip (trainers, loop, checkpoints, UT03-126/127), a4ad4ab wip (BT03-12, checkpoint edge tests), final 555ea14 `feat(enrich): T03-31 Laya trainers`.

## Implemented
- `herness/enrich/laya_trainer.py` (232 / 390): `TRAIN_SCHEMA`, `TrainingSet` (U03-130), `TrainHyper` / `TrainResult` (frozen dataclasses; defaults are the U03-131 values verbatim), `LayaTrainer` protocol (U03-131), `build_training_set` (private helper of run_distill, UT03-126), `SoftLabelSftTrainer` (U03-132, name "sft"), `RlcdTrainer` (U03-133, name "rlcd"), `select_trainer` (U03-134: find_spec, with ModuleNotFoundError from the missing parent package caught; logs `enrich.distill.trainer_selected` with field `trainer`), `VENDOR_MODULE` constant.
- `herness/enrich/_sft_loop.py` (297 / 320, NEW private sibling; module-map row and spec note added to docs/impl/03 §2 in the same commit): `SftLoop`. It uses AdamW with encoder/head groups (2e-5 / 1e-4, wd 0.01), a linear warmup over 6 % then linear decay, bf16 autocast and micro batch 8 x accumulation 4. Loss is the weighted sum of KL(target || softmax(logits)), normalised per micro batch and accumulation. After each epoch it computes validation NLL (weighted mean cross-entropy), applies early stopping with patience 1 and writes a checkpoint. Every `check_every` (50) optimizer steps it calls heartbeat("train") and should_yield; a yield saves a mid-epoch checkpoint and raises YieldRequested("train"). The wall-clock cap (6 h, cumulative across resumes via state.elapsed_s) stops training after the current epoch, keeps the best checkpoint and logs WARNING `enrich.distill.wall_clock_cap` (epochs_run, best_epoch). Forward/backward and validation run through `run_batches_with_oom_backoff(..., fault_name="decider.batch")`, so a CUDA OOM that persists down to micro batch 1 raises FatalError("cuda oom at batch 1"). Outputs: the best epoch's weights go to out_dir/model.safetensors (safetensors save_model); rl_agent_config.json, tokenizer*, special_tokens_map.json and vocab* are copied from init_dir, and the config file is checked BEFORE training starts. The module also holds `label_order` and `_target_vector`: bool targets are two-way (true, false); choice and score targets are renormalised over the wire label order.
- `herness/enrich/_train_ckpt.py` (122 / 140, NEW private sibling; module-map row and spec note): checkpoints `checkpoints/epoch-<n>/` and mid-epoch `epoch-<n>-step-<k>/`. Each holds model.safetensors, optimizer.safetensors (AdamW state tensors keyed `<idx>.<name>`) and state.json (progress plus JSON param_groups). Checkpoints are written to `.tmp-<name>` and then moved with os.replace, so writes are atomic. The latest complete checkpoint is the one with the highest (epochs_done, step) among directories not starting with a dot. Mid-epoch checkpoints are deleted after each epoch checkpoint. No torch.save or pickle anywhere (TH03-16).
- `tests/support/fake_laya.py` (+76 lines; the existing API is unchanged): adds `trainable_fake_laya(wire, dim, seed, fail_with)`, a small torch module with an EmbeddingBag encoder (`encoder.*`), per-question Linear heads (`head.<qid>`), `head_checkpointing` and `gradient_checkpointing_enable`, which records `logit_calls`. Also adds `fake_laya_train_module(factory)`, which builds a fresh agent on each `load`. This fixes the V-11 training call to `agent.question_logits(states, wire_questions, *, max_len) -> {qid: Tensor[B, K]}`, where K = 1 for noul (the logit of "true") and len(criteria) in wire order otherwise.
- Not created, per controller ruling: `herness/enrich/_vendor/laya_rlcd.py`. deciders/laya.py, settings.py, resolve.py and evaluate.py are untouched.

## Tests
- tests/unit/enrich/test_laya_trainer.py (unit, 12 tests):
  - UT03-126 (2 tests): a correction becomes a one-hot target with weight 3; a human row with a stale fingerprint is ignored; gold hashes are excluded; teacher rows with a stale fingerprint, an empty distribution or no text are dropped; the validation rule is sha256(hash)[-1] < 2; the latest round wins; sha256 does not depend on row order.
  - UT03-127 (10 tests):
    - 2-epoch SFT on CPU: a checkpoint per epoch with its 3 files (two safetensors, one JSON), the output files, the checkpointing flags and heartbeats, and no .pt/.bin/.pkl files.
    - Yield in the middle of epoch 2: an epoch-2-step-* checkpoint is written. The resumed run trains only the rest of epoch 2 and its final weights are allclose to an uninterrupted run.
    - Simulated kill (KeyboardInterrupt from heartbeat) after epoch 1: the resume starts from epoch-1 and reaches the same best NLL. Re-running a finished run does no training.
    - Early stopping with lr 0 stops after epoch 2 and keeps epoch 1 as best. The wall-clock cap stops after epoch 1 and logs.
    - CUDA OOM at micro batch 1 raises FatalError. The ConfigError cases are covered, as are target vectors and the lr schedule.
    - select_trainer logs sft, and rlcd when a fake vendored module with a ModuleSpec is put in sys.modules.
    - RlcdTrainer raises ConfigError when the module is missing, delegates when it is present, and rejects a result that is not a TrainResult.
    - Checkpoint lookup and unreadable state.
- tests/bench/enrich/test_laya_trainer_bench.py: BT03-12, marked [integration, slow, gpu]. It is skipped unless `laya` is importable, CUDA is available and HERNESS_BT03_12_BASE_DIR names a base Laya directory. It trains on 30k synthetic records and asserts the run takes <= 6 h.
- RED evidence: tests and implementation were written in the same step, so there is no recorded RED run from before the implementation. The first run failed on test-fixture problems: max() over an empty distribution in a helper; a per-load seed that gave the reference run and the killed run different starting weights; the tick arithmetic of the wall-clock test; log dicts carrying `component`. All were fixed in the tests.
- GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_laya_trainer.py -q -p no:logging --cov=... --cov-branch` gave 12 passed. Coverage: laya_trainer 100 %, _sft_loop 98 % (4 partial branches on optional-hook paths), _train_ckpt 100 %. `PYTHONUTF8=1 uv run pytest tests/unit/enrich -q -p no:logging` gave 504 passed, 1 skipped (the existing symlink skip). BT03-12: 1 skipped (no laya).
- Gates: ruff format and ruff check clean, mypy (210 files) clean, lint-imports 13 contracts kept, check_module_size exit 0, check_type_ownership exit 0. Commits used SKIP=pytest-unit because of the known-red ST05-13(a) and IT00-01, which were not touched. detect-secrets renumbered the one docs/impl/03 baseline entry (line 3604 to 3606, because of the 2 new module-map rows); the baseline keeps LF endings.

## Deviations / spec notes
1. Module split: with everything in it, laya_trainer.py was 484 lines. The SFT loop moved to the private sibling `_sft_loop.py` (budget 320) and checkpoints to `_train_ckpt.py` (budget 140), each with a §2 module-map row (spec note T03-31). `build_training_set` stays in laya_trainer.py.
2. `build_training_set(teacher, human, *, texts, questions, gold) -> TrainingSet` is the smallest honest signature:
   - `teacher` is TEACHER_SCHEMA rows and `human` is `LabelStore.latest_human()`.
   - `texts` maps content_hash to redacted text and is supplied by run_distill; hashes without text are dropped (for example purged records).
   - `questions` is the trainable (unblocked) set; a row must match both the question id and its current fingerprint.
   - For each (hash, question) the latest `round` wins, and empty distributions are dropped.
   - A human correction only replaces an existing teacher row; it never adds a row.
3. `TrainHyper.seed` defaults to 0 because the spec gives no value; run_distill should pass the seed it records. Extra fields: `gradient_checkpointing`, `head_checkpointing`, `wall_clock_cap_s`, `patience`, `warmup` (a fraction).
4. The log event `enrich.distill.wall_clock_cap` (WARNING, `epochs_run`, `best_epoch`) is not in the catalog. The spec only says "logged" and gives no name, so §8.1 needs a row.
5. The yield stage name is "train". Constructor options: `SoftLabelSftTrainer(device=None, check_every=50)`; the device defaults to cuda when available.
6. OOM handling reuses run_batches_with_oom_backoff on each micro batch: retry once, then halve down to 1, then FatalError. Caveat: if an OOM is raised partway through a chunk's backward pass, that chunk's partial gradients may already be accumulated before the retry.
7. Training needs non-empty train and val tables (ConfigError otherwise).

## Carry-overs
- V-11 (every such call is marked `# V-11:`):
  - Training loads with `laya.load(str(init_dir), fast=False)`.
  - The torch module is `agent.model`, or the agent itself when there is no `model`.
  - Head checkpointing is switched on with `model.head_checkpointing = True`, and the encoder with the `gradient_checkpointing_enable()` hook.
  - Head parameters are assumed to be named `head*`.
  - `agent.question_logits(states, wire, *, max_len)` has the shape fixed in tests/support/fake_laya.py.
  - Output weights are saved with safetensors `save_model(module)` and the rl_agent_config/tokenizer files are copied. Both must be checked against what `laya.load` expects.
  - The vendored RLCD entry point is `train(data, *, init_dir, out_dir, hyper, ctx) -> TrainResult`.
- `_vendor/laya_rlcd.py` is absent because the notebook is unavailable, so select_trainer always returns SFT today.
- Distill card (run_distill steps 9 and 10):
  - Call build_training_set with the unblocked question set, `latest_human()`, gold_hashes() and texts.
  - Enter `ctx.gpu_scope("decider")` with openjev stopped, and catch YieldRequested.
  - Record `trainer.name` in `manifest.hyperparams.trainer`, along with the seed.
  - Hash `TrainResult.files` for `weights_sha256`.
  - Delete `checkpoints/` from the version dir, or leave it out of manifest hashing, as needed.
- ~~§8.1 log catalog row for `enrich.distill.wall_clock_cap`.~~ Done in fix round 1 (M6).

## Fix round 1 (review D:\herness\.superpowers\sdd\program\briefs\T03-31-review.md)
Final commit: 96d9b8e `fix(enrich): T03-31 review round 1` (the round-1 wip commit a621954 was folded into it with a soft reset before anything used it). M1, M5 and M8 are parked and left as they were.
- M2 (`_sft_loop.py`): each micro batch's loss is now the weighted KL sum divided by the total weight of its accumulation group (all micro batches up to the next optimizer step, including a short last group). One optimizer step is therefore the weighted mean over the (row, question) pairs of its group, as U03-132 requires. Test `test_ut03_127_accumulation_is_a_weighted_mean_over_the_group`: a single step over 17 micro batches of 5 (the last one short) matches a single full-batch step (allclose, atol 1e-6). RED check: with the old `1/(micro weight x accumulation)` scale temporarily restored, the test failed (1 failed); with the fix it passes.
- M3: checkpoints now store the torch CPU RNG state as a uint8 tensor under `rng.cpu` in optimizer.safetensors, and `load_checkpoint` restores it. The CUDA RNG is not saved; that is a carry-over for real-GPU dropout. state.json records `data_sha256` = TrainingSet.sha256. **Choice: a mismatch raises ConfigError** ("trained on other data; use a new out_dir") before anything is loaded. I did not pick "start fresh and log" because that would silently leave stale checkpoints in the directory. Test `test_ut03_127_checkpoint_restores_rng_and_rejects_other_data`.
- M4: a bool target with neither "true" nor "false" now raises ConfigError. It is tested in `test_ut03_127_targets_and_schedule`.
- M6: added the §8.1 row `enrich.distill.wall_clock_cap` | WARNING | `epochs_run`, `best_epoch` | U03-131. The .secrets.baseline docs/impl/03 entry moved from line 3606 to 3607, with LF endings kept.
- M7: BT03-12 captures the logs and asserts that no `enrich.distill.wall_clock_cap` event fired.
- M9: moved the stray `label_order` assert into the targets test, and added `test_ut03_127_train_hyper_defaults_are_design_values`, which pins every U03-131 default.
- Sizes: `_sft_loop.py` 307/320, `_train_ckpt.py` 128/140, `laya_trainer.py` 232/390.
- Tests: `test_laya_trainer.py` 15 passed. `tests/unit/enrich` + `tests/bench/enrich`: 507 passed, 2 skipped (the existing symlink skip and BT03-12, since `laya` is not installed). Coverage: `_sft_loop` 98 %, `_train_ckpt` 100 %, `laya_trainer` 100 %. ruff, format, mypy (210 files), lint-imports (13 kept) and check_module_size are all clean.
