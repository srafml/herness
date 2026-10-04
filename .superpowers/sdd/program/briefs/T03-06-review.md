# T03-06 review (verify agent): Encoder and `embed_query`

Worktree D:\herness\.claude\worktrees\agent-aee988a7bc9d007fa, head bbcb127 (base 8bb8194). Read-only; two temporary mutation edits were reverted with `git checkout --`, `git status` clean afterwards.

**Task quality: Approved** (Minor items only)

### Spec Compliance
- ✅ U03-29 Encoder — signature, lazy load, same-device no-op, device change unloads then reloads, `SentenceTransformer(str(dir), device=, local_files_only=True)`, cuda `.half()` / cpu `.float()`, `max_seq_length`, `encode(normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False).astype(float32)`, `unload` = drop ref + `release_cuda()`, `model_id = f"{model_name}@{model_dir.name}"` (embed.py:44-124).
- ✅ U03-30 get_encoder — `functools.cache`, built from `EnrichPaths.from_config(cfg).embedding_model_dir()` and `cfg.decisions.embedding.{model,max_seq_length}` (embed.py:127-140).
- ✅ U03-31 embed_query — normalize → empty `ToolInputError("empty query")`; module `threading.Lock`; model check → device → load → `encode([t],1)[0]`, in spec order (embed.py:185-202).
- ✅ U03-32 embed_texts — stable length sort, `run_batches_with_oom_backoff(..., fault_name="embed.batch")`, scatter `result[order] = ...` (embed.py:205-229).
- ✅ Facade exports only `embed_query` (ruling 3), lazy PEP 562.
- ⚠️ TH03-05 says bge-m3 weights are SHA-256-verified on load; U03-29 has no such step and the card does not list ST03-07, so this card is not at fault — spec gap for the controller (who owns bge-m3 weight hashing?).
- ⚠️ `on_batch(i, rows)` receives rows in length-sorted order with no indices (builder deviation 5). The spec signature allows it, but the embed-stage card may need indices.
- ⚠️ `cfg.decisions.embedding.dtype` ("fp16"/"fp32") is ignored. U03-29 fixes fp16 on cuda and fp32 on cpu, so this matches the spec, but the setting has no effect.

### Verification points
1. ✅ Only local configured model, no network: `local_files_only=True`, `HF_HUB_OFFLINE=1` set before the lazy import (embed.py:91), the `_check_dir` refusal (missing dir / `*.bin|.pkl|.pt|.pth|.ckpt` anywhere via rglob / no `model.safetensors`) runs BEFORE `SentenceTransformer` (embed.py:67-78, 87). Messages carry no paths. Load failure → `ModelUnavailable("bge-m3 load failed")` (embed.py:101-103). `torch.cuda.OutOfMemoryError` re-raised (99-100). Tests cover each.
2. ✅ `normalize_text` bounds to 4,000 chars before encoding (ruling 5: truncate, tested); empty/whitespace/ideographic space → ToolInputError, nothing loaded.
3. ✅ Determinism probe (scratch script, tiny-st, two separately loaded Encoder instances, cpu): same-instance and cross-instance outputs `np.array_equal` True, max diff 0.0. Unit-norm float32 (1024,) tested. Mismatch check reads one row (`search().select(["model"]).limit(1)`), NotFoundError = no rows, result cached (the test swaps in a store that raises when read twice). Other store errors propagate uncached.
4. ✅ gpu_state only read (`loaded_class`, `service_healthy`). No swap/request_gpu_class/_Holder/GpuLock/gpu_lock in herness/enrich/embed.py or `__init__.py` (grep). Device rule is exactly as specified: `torch.cuda.is_available()` is checked first, then class ∈ {none, decider} and not `service_healthy("openjev")`. HernessError → cpu + WARNING with the error type only (ruling). Parametrised test covers all 6 branches.
5. ✅ No vector writes; the read goes through `VectorStore().table("ticket_embedding")`.
6. ✅ No HTTP client in herness/enrich: `ruff --select TID251 herness/enrich` clean. ST10-25 + ST05-13 lint tests: 85 passed.
7. ✅ Verified in code and by mutation (below).
8. ✅ Every test name/docstring carries its UT id, `pytestmark = pytest.mark.unit`, and the real tiny-st model is loaded and encoded (UT03-26 encode/order, UT03-27 matching, UT03-28 reasoning→cpu, UT03-29 real-encoder rows). Mutation probes:
   - A: scatter `result[order]=` → `result[:]=` → 2 UT03-29 tests FAIL (killed).
   - B: pickle-suffix check disabled → all 3 `non_safetensors` params FAIL (killed).
   Both reverted; `git status` clean.
9. ✅ Gates (run by me): `pytest tests/unit/enrich` 650 passed / 1 skipped (symlink privilege); test_embed.py 34 passed; embed.py coverage 100 % line / 100 % branch (129 stmts, 22 branches); ruff check + format --check clean; mypy strict 0 issues (253 files); lint-imports 13 kept 0 broken; check_module_size exit 0 (embed.py 229/230, `__init__` 20/20); check_type_ownership exit 0.

Sub-controller rulings — all implemented soundly:
- tiny-st: offline generator with seed 0 (`tests/support/make_tiny_st.py`), safetensors only, ~143 KB.
- No cross-process GpuLock: module lock only.
- Lazy facade: **claim verified**. In a scratch copy with an eager `from herness.enrich.embed import embed_query` in `herness/enrich/__init__.py`, a fresh `import herness.core.config` fails with `ImportError: cannot import name 'get_config' from partially initialized module 'herness.core.config' (most likely due to a circular import)`.
- Truncation rather than an error for text over 4,000 chars.
- gpu_state HernessError → cpu with a WARNING.
- `.pt/.pth/.ckpt` are also refused.

### Strengths
- Refusal-before-load ordering; generic error messages; OOM propagation preserved.
- The model-check cache semantics are tested, including the "store not read twice" and "store error not cached" cases.
- The order test encodes text length in the fake vectors, so a scatter bug is observable, and a real-model row-equality test backs it up.
- `torch` and sentence_transformers stay lazy at module import.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. embed.py:91 — `os.environ["HF_HUB_OFFLINE"] = "1"` is set at load time. huggingface_hub reads this flag into constants when it is first imported, so if it was imported earlier in the process (the test module imports sentence_transformers at top level) the env write has no effect. `local_files_only=True` is the effective guard. The test (test_embed.py UT03-26 same-device) asserts only the env var, not offline behaviour. It is also a process-wide env mutation, the same pattern as laya.py:159.
2. embed.py:106-117 — `encode` does not check the output width. A model directory with a different dimension would make `embed_query` return a non-1024 vector silently, and make `embed_texts` fail with a raw numpy broadcasting ValueError (embed.py:228). A `ModelUnavailable`/`ConfigError` on `rows.shape[1] != EMBEDDING_DIM` would make the postcondition explicit.
3. test_embed.py (`test_ut03_26_non_safetensors_weights_are_config_error`) — the test proves ConfigError and `device is None`, but it does not pin that the refusal happens before `SentenceTransformer` is constructed. The code order is correct; the assertion is weak. A sentinel `SentenceTransformer` that fails the test if called would pin it.
4. embed.py:103 — `raise ModelUnavailable(...) from exc` keeps the underlying exception (which may name file paths) as `__cause__`. Paths are not personal data and the message is generic; note only in case the error envelope serialises causes.
5. The commit trailer is `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; global-constraints asks for `Claude Opus 5.5 (1M context)`. Cosmetic.
6. Test noise: loading tiny-st prints a transformers "Loading weights" tqdm bar to stderr (seen in my probe; suppressed under pytest capture).

### Assessment
**Task quality: Approved**
**Reasoning:** All four units match the spec and the sub-controller rulings. The rulings' premises hold (circular import reproduced) and the key assertions survive mutation. All gates are green with 100 % line/branch coverage of embed.py. The remaining items are Minor hardening and test-strength notes.
