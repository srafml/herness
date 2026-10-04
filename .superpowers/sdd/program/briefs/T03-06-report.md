# T03-06 report: Encoder and `embed_query` (build agent)

Worktree: D:\herness\.claude\worktrees\agent-aee988a7bc9d007fa (branch worktree-agent-aee988a7bc9d007fa, base 8bb8194)
Status: DONE_WITH_CONCERNS (minor; see Concerns)

## Files
| File | Lines | Budget |
|------|-------|--------|
| herness/enrich/embed.py (new) | 229 | 230 |
| herness/enrich/__init__.py | 20 | 20 |
| tests/unit/enrich/test_embed.py (new) | ~420 | - |
| tests/support/make_tiny_st.py (new, generator) | ~105 | - |
| tests/fixtures/models/tiny-st/ (new, 10 files, ~143 KB) | - | < 1 MB |

`uv run python -m tools.check_module_size` exit 0. No pyproject.toml change needed: import-linter
contracts name `herness.enrich` as a layer only (13 kept, 0 broken; UT00-58 file passes).

## Implemented
- U03-29 `Encoder(model_dir, *, model_name, max_seq_length=512)`: `load(device)` (no-op on same
  device; device change unloads then reloads; refuses before loading with ConfigError when the
  directory is missing, holds any `*.bin/*.pkl/*.pt/*.pth/*.ckpt` file anywhere below it, or lacks
  `model.safetensors`; sets HF_HUB_OFFLINE=1; `SentenceTransformer(str(dir), device=..., local_files_only=True)`;
  cuda -> `.half()`, cpu -> `.float()`; `max_seq_length` set; torch.cuda.OutOfMemoryError re-raised
  unchanged, any other exception -> ModelUnavailable("bge-m3 load failed")); `encode` (normalize_embeddings,
  float32; ModelUnavailable("bge-m3 not loaded") if called unloaded); `unload` (drop ref + release_cuda());
  `model_id = f"{model_name}@{model_dir.name}"`; `device` property.
- U03-30 `get_encoder()`: functools.cache; `Encoder(EnrichPaths.from_config(cfg).embedding_model_dir(),
  model_name=cfg.decisions.embedding.model, max_seq_length=cfg.decisions.embedding.max_seq_length)`.
- U03-31 `embed_query(text, /)`: normalize_text -> empty -> ToolInputError("empty query"); under module
  `threading.Lock` (class `_QueryState`): first-call model check against one `model` value of
  `VectorStore().table("ticket_embedding")` (NotFoundError = no rows; read via
  `search().select(["model"]).limit(1).to_list()`), result cached (both pass and mismatch; a store error
  other than NotFoundError propagates and leaves the check pending); device rule per spec; load; encode.
  No GpuLock / lock file (ruling 2).
- U03-32 `embed_texts(encoder, texts, *, batch_size, on_batch=None)`: stable length sort, 
  `run_batches_with_oom_backoff(..., fault_name="embed.batch")` reused, scatter back; empty input ->
  (0, 1024) float32 without calling the encoder.
- Facade: `herness.enrich.embed_query` only (ruling 3).

## Design decisions / deviations
1. **Lazy facade (PEP 562 `__getattr__`) in `herness/enrich/__init__.py`.** An eager
   `from herness.enrich.embed import embed_query` is a circular import: `herness.core.config` ->
   `_config_sections` -> `herness.enrich.settings` runs the package `__init__`, which would import
   embed -> `herness.core.config` (partially initialized). It would also load LanceDB/numpy whenever config
   loads. A `TYPE_CHECKING` import keeps mypy typing of `herness.enrich.embed_query`.
2. **> 4,000 chars (ruling 5):** truncated, not rejected. The spec reads "1–4,000 chars *after*
   normalize_text", and U03-24 normalize_text truncates to 4,000 (TH03-09 "Text truncated to 4,000
   chars"); ticket texts are embedded the same way, so the query is treated identically. Spec 05
   `semantic_search` already caps its input at 500 chars. Tested (UT03-28 truncation test). The
   sub-controller's suggested ToolInputError reading was not taken — controller may overrule.
3. **Pickle suffixes:** spec names `*.bin`/`*.pkl`; I also refuse `.pt/.pth/.ckpt` ("only safetensors
   allowed", TH03-05; laya_models refuses the same set). Operational note: the upstream BAAI/bge-m3 repo
   ships `pytorch_model.bin`, `colbert_linear.pt`, `sparse_linear.pt` — the provisioned model directory must
   not contain them (spec-mandated for .bin anyway).
4. **Unreadable gpu_state -> cpu:** if `gpu_state().loaded_class()/service_healthy()` raises a HernessError
   (e.g. no jobs backend in the calling process), embed_query uses cpu and logs
   `enrich.embed.gpu_state_unreadable` (WARNING, error type only). Fail-closed: never contend for VRAM.
   `torch.cuda.is_available()` is checked first so gpu_state is not consulted on CPU-only hosts.
5. **`on_batch` of embed_texts** receives each batch's rows as an ndarray in *length-sorted* order (the spec
   signature `(int, np.ndarray)` carries no indices). The embed-stage card may need indices; flagged.
6. Encoder attributes `model_dir`, `model_name`, `max_seq_length` are public (read by tests / later cards).

## Fixture `tests/fixtures/models/tiny-st/` (ruling 1)
Generated offline by `uv run python -m tests.support.make_tiny_st` (seed 0; reproducible — re-running
gives byte-identical files): 1-layer BERT (hidden 16, 2 heads, 512 positions, ~130-token local WordPiece
vocab of generic words) + mean Pooling + Dense 16->1024 (Identity activation). Safetensors only; the model
card README is dropped; text files end with one newline, no trailing whitespace. Total ~143 KB.
detect-secrets hook, fixtures-pii-scan (`herness.core.redact --scan tests/fixtures`) pass; large-file hook
already excludes tests/fixtures/. No hook config changes. `config_sentence_transformers.json` records
the generating library versions (informational only).

## Tests (TDD)
RED: `uv run pytest tests/unit/enrich/test_embed.py` -> `ImportError: cannot import name 'embed' from 'herness.enrich'`.
GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_embed.py -q -p no:logging` -> 34 passed.
IDs: UT03-26 (13 fns incl. params), UT03-27 (5), UT03-28 (6 + facade), UT03-29 (3).
Touched packages: `pytest tests/unit/enrich tests/unit/repo/test_import_contracts.py tests/security/test_st10_lint.py`
-> 707 passed, 1 skipped (symlink privilege) in 118 s.
Coverage (test_embed.py alone, branch): embed.py 100 % line / 100 % branch (133 stmts, 24 branches);
__init__.py 100 %.
Gates: ruff format/check clean, mypy strict 0 issues (253 files), lint-imports 13 kept 0 broken,
check_module_size exit 0, check_type_ownership clean, ST10-25 lint passes (no HTTP client in enrich).

## Concerns
- Deviation 2 (truncate vs ToolInputError for > 4,000 chars) needs controller confirmation.
- Deviation 5 (on_batch rows in sorted order) may matter to the later embed-stage card.
- Lazy facade (deviation 1) is required by the existing import graph; later facade exports
  (purge_record, health) should follow the same `__getattr__` pattern (20-line budget is tight: 20/20).

## Spec notes
- U03-29 could name `.pt/.pth/.ckpt` explicitly next to `*.bin`/`*.pkl`.
- U03-31 could state the device choice when `gpu_state` is unreadable, and the > 4,000 behaviour.
- Module-map row for `__init__.py` could note the lazy facade pattern (import cycle with core.config).

## Final commit
bbcb127 feat(enrich): T03-06 encoder and embed_query — all pre-commit hooks passed (incl. pytest-unit,
detect-secrets, fixtures-pii-scan, module-size, large-files, eof/whitespace). Single commit: no wip
checkpoints were taken (hook runs the full unit suite, and a red TDD state cannot be committed).
Line counts final: test_embed.py 463, make_tiny_st.py 100.
