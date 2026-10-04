# T03-31 review (Laya trainers) - verify agent

Head 555ea14 (base bdb61b7). Read-only review.

Commands run in the worktree:
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_laya_trainer.py -q -p no:logging` with branch coverage: 12 passed in 4.1 s. Coverage: laya_trainer 100 %, _sft_loop 98 % (4 partial branches: 117->119, 120->122, 124->123, 253->exit), _train_ckpt 100 %. All three modules are above the 90/85 bar.
- `uv run mypy` (project config): no issues in 210 files.
- `ruff check` on the 6 touched Python files: clean. C901 at max-complexity 10: clean.
- Module sizes: 232/390, 297/320, 122/140.
- Grep for `torch.save`, `torch.load` and `pickle` in herness/enrich and the new tests: none (only docstrings and the existing laya_models rejection).

### Spec Compliance
- U03-130 TrainingSet / build_training_set ✅
  - A correction replaces the target with a one-hot on the human answer at weight 3, and only when the human fingerprint matches.
  - Gold hashes are excluded before the train/val split, so they are in neither table. This includes a gold hash that also has a correction.
  - The val rule is `int(sha256_hex(hash)[-1],16) < 2`.
  - sha256 is computed over canonical_json of the (hash, question, target) rows, sorted by key.
  - TRAIN_SCHEMA matches the spec columns.
- U03-131 LayaTrainer / TrainHyper / TrainResult ✅
  - TrainHyper defaults are verbatim: 4 epochs, lr 2e-5/1e-4, weight decay 0.01, warmup 6 %, micro batch 8 x accumulation 4, bf16, gradient and head checkpointing, max_len 512, patience 1, seed, 6 h cap.
  - Checkpoints go to `out_dir/checkpoints/epoch-<n>/` as model and optimizer safetensors plus state.json. Writes are atomic (tmp dir, then rename).
  - Resume uses the complete checkpoint with the highest (epochs_done, step).
  - out_dir gets model.safetensors (best epoch), rl_agent_config.json and the tokenizer files.
  - The wall-clock cap is cumulative across resumes, stops after the current epoch, keeps the best checkpoint and logs.
  - OOM down to micro batch 1 raises FatalError through run_batches_with_oom_backoff. I checked that this helper resumes from the failed chunk and does not replay completed chunks.
- U03-132 SoftLabelSftTrainer ✅
  - Loss is weight x (CE - H(target)) = KL(target || softmax), normalised by the micro batch's weight sum and by accumulation.
  - A single noul logit becomes two-way (true, false).
  - AdamW has two groups (head* / other). Warmup is linear, then linear decay.
  - The epoch shuffle is seeded from (seed, epoch), so it is deterministic across processes.
  - Validation NLL is a weighted mean, with early stopping at patience 1.
  - heartbeat("train") and should_yield run every `check_every` (50) optimizer steps. A yield writes a mid-epoch checkpoint and then raises YieldRequested("train").
  - Resume after a yield restarts at the saved micro-batch index. The base lrs are captured before the optimizer state is loaded, so the schedule stays correct. UT03-127 shows the resumed final weights are allclose to an uninterrupted run.
- U03-133 RlcdTrainer ✅: a missing vendored module raises ConfigError, and the trainer delegates and checks the result type. Under the controller ruling, `_vendor/laya_rlcd.py` is absent.
- U03-134 select_trainer ✅: uses find_spec (a missing parent package is handled) and logs `enrich.distill.trainer_selected` with `trainer`, which matches §8.1.
- TH03-16 ✅: weights and optimizer state are safetensors only, and state.json is JSON. TH03-04 ✅: gold is excluded, which UT03-126 asserts.
- UT03-126 ✅ (2 tests). UT03-127 ✅ (10 tests: 2 epochs, yield, kill, resume, early stop, cap, OOM, config errors, selection, RLCD, checkpoint lookup). BT03-12 ✅ (marked integration/slow/gpu, skipped without laya, CUDA and a base dir). Test names carry their IDs and every test has a docstring.
- ⚠️ Cannot verify here:
  - The V-11 carry-overs: the real `laya.load(..., fast=False)`, `question_logits` shape, `head*` parameter naming, the gradient-checkpointing hook, and whether `save_model(module)` output is loadable by `laya.load`.
  - BT03-12 at 6 h on the dev box.
  - `manifest.hyperparams.trainer` / `seed` recording and hashing `TrainResult.files`, which belong to the distill card.

### Strengths
- Resume is well tested: the yield-then-resume run is compared weight for weight with an uninterrupted run, and the kill/resume run reaches the same best NLL. Checkpoint writes are atomic.
- The optimizer state round-trips through safetensors plus JSON param_groups, with no pickle path.
- The module split is recorded in §2 rows, and all three modules are under budget.
- Imports of torch, laya and safetensors are lazy. V-11 call sites are marked.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/enrich/_sft_loop.py:211-221: if a CUDA OOM is raised partway through `loss.backward()`, some leaves may already have gradients. The retry then adds full gradients on top, so those parameters get double-counted for that step (the report admits this in deviation 6). Suggested fix: on OOM inside a training micro step, zero the gradients and restart the whole accumulation step, or accept this and document it next to the call.
2. herness/enrich/_sft_loop.py:212 with :203-209: the last accumulation group of an epoch can have fewer than `accumulation` micro batches, but it is still divided by `accumulation`. That under-weights the final optimizer step of each epoch by up to 4x. The loss is also a mean of per-micro-batch weighted means, not a global weighted mean over (row, question). The effect is small.
3. herness/enrich/_sft_loop.py:180/186-188: resume reseeds torch with the original seed and does not restore the torch or CUDA RNG state. With a real encoder that uses dropout, a resumed run is deterministic but not identical to an uninterrupted one. The checkpoint also records no training-set sha256 or hyper values, so a stale `checkpoints/` from different data would be resumed silently. This is safe today because run_distill uses a new version dir each run. Suggested fix: store `data.sha256` in state.json and reject a mismatch.
4. herness/enrich/_sft_loop.py:61: a bool target with neither "true" nor "false" gives p_true = 1.0 silently. build_training_set only rejects empty distributions. A ConfigError, like the one for choice targets, would be safer.
5. herness/enrich/laya_trainer.py:163 and :210: `except ModuleNotFoundError` also catches a missing transitive dependency of `laya` or of the vendored module, which is then reported as "not installed" or "missing". Suggested fix: check `exc.name` against `"laya"` / VENDOR_MODULE and its parents, and re-raise otherwise.
6. herness/enrich/_sft_loop.py:284: the new event `enrich.distill.wall_clock_cap` has no §8.1 catalog row, although this commit already edits docs/impl/03. The report lists it as a carry-over. It should be added.
7. tests/bench/enrich/test_laya_trainer_bench.py:79-80: the trainer caps itself at 6 h, so the bench can pass on a run that was capped after one epoch. It should also assert that no `enrich.distill.wall_clock_cap` event fired, or that epochs_run was 4 or ended by early stopping.
8. herness/enrich/_train_ckpt.py:77-78: checkpoint files are not fsynced before the rename, so atomicity holds for process kills but not for power loss. This is acceptable for the stated kill/resume requirement.
9. tests/unit/enrich/test_laya_trainer.py:460: a `label_order` assert unrelated to the test sits at the end of the checkpoint-lookup test. No test asserts the TrainHyper defaults verbatim (a 1-line check would pin U03-131).

### Assessment
**Task quality:** Approved
**Reasoning:** All units U03-130 to U03-134 match the spec, and resume, early stopping, the cap, OOM and TH03-16 safetensors-only behaviour are verified by passing tests with 98-100 % coverage, clean mypy and clean lint. The remaining items are Minor robustness and documentation polish (OOM partial gradients, the scale of the last accumulation group, the missing catalog row, the weak bench assertion).

## Round 1 re-review (555ea14..96d9b8e; scope M2, M3, M4, M6, M7, M9)

Commands run in the worktree at 96d9b8e:
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_laya_trainer.py -q -p no:logging` with branch coverage: 15 passed in 4.4 s. Coverage: laya_trainer 100 %, _sft_loop 98 %, _train_ckpt 100 %.
- mypy (project, 210 files): clean. ruff check and ruff format: clean.
- Module sizes: _sft_loop 307/320, _train_ckpt 128/140.

Findings:
- M2 ✅ closed.
  - `_train_epoch` now scales each micro batch by 1 / (total weight of its accumulation group), and the short last group uses its own weight. Each optimizer step is therefore the weighted mean over that group's (row, question) rows.
  - Mid-epoch resume starts on a group boundary, so the scale does not change.
  - The new test compares accumulated micro batches with a short tail against one full-batch step; the weights match within 1e-6.
- M3 ✅ closed.
  - state.json records `data_sha256`. A mismatch raises ConfigError before any state is loaded. I accept this choice: it avoids silently leaving stale checkpoints, and a checkpoint from before the fix, which has no key, also gets a clean ConfigError instead of a KeyError on the missing `rng.cpu`.
  - The torch CPU RNG state is saved as a uint8 tensor in optimizer.safetensors, so it is still safetensors only (TH03-16). It is restored on load. The test checks the RNG stream is identical after a restore and that other data raises ConfigError.
  - Carry-over (accepted): the CUDA RNG is not saved, so dropout on GPU will not replay identically after a resume. The comment at `_train_ckpt.py` (`torch CPU RNG (dropout) for identical resume`) overstates this for the CUDA device. That is Minor, a wording fix only.
- M4 ✅ closed: a bool target with neither true nor false raises ConfigError, and a test covers it.
- M6 ✅ closed: §8.1 now has a row for `enrich.distill.wall_clock_cap` (WARNING, `epochs_run`, `best_epoch`, U03-131). The detect-secrets line number was bumped to match.
- M7 ✅ closed: BT03-12 captures logs and asserts that no `wall_clock_cap` event fired.
- M9 ✅ closed: the stray assert moved into the targets test, and a new test pins the TrainHyper defaults to the U03-131 values.
- Regressions: none found. The pre-existing 12 tests still pass unchanged, and the yield/kill resume-equivalence tests still pass with the new scaling and the RNG restore.
- Parked per controller: M1 (OOM partial gradients), M5 (ModuleNotFoundError breadth) and M8 (fsync).

**Round 1 verdict:** Approved.
