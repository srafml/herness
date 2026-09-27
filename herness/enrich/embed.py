"""bge-m3 encoder, bulk embedding and the public `embed_query` (impl 03 U03-29 ... U03-32).

Design 03 §5.2. Weights load from a local directory only (`HF_HUB_OFFLINE=1`,
`local_files_only=True`) and only as safetensors: a directory holding any pickle-format
weight file is refused before loading (TH03-05). Callers pass redacted text only (TH03-13).
`sentence_transformers` and `torch` are imported lazily: importing this module loads neither.
"""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, Literal

import numpy as np

from herness.core.config import get_config
from herness.core.errors import ConfigError, HernessError, ModelUnavailable, ToolInputError
from herness.core.jobs.gpu import gpu_state
from herness.core.logging import get_logger
from herness.enrich.gpu import release_cuda, run_batches_with_oom_backoff
from herness.enrich.layout import EnrichPaths
from herness.enrich.text import normalize_text
from herness.store.errors import NotFoundError
from herness.store.vectors import EMBEDDING_DIM, VectorStore

__all__ = ["Encoder", "embed_query", "embed_texts", "get_encoder"]

type Device = Literal["cuda", "cpu"]

# Only safetensors may be loaded (U03-29 step 2); the spec names *.bin and *.pkl, and the
# other pickle-based suffixes are refused for the same reason (TH03-05, as laya_models does).
_PICKLE_SUFFIXES: Final = frozenset({".bin", ".pkl", ".pt", ".pth", ".ckpt"})
_WEIGHTS: Final = "model.safetensors"
_GPU_OK: Final = frozenset({"none", "decider"})  # classes that leave room for bge-m3

_log = get_logger("enrich.embed")


class Encoder:
    """Lazily loaded sentence-transformers encoder with fixed settings (U03-29).

    At most one loaded model per instance; `load` on another device reloads. Not
    thread-safe: one owner thread (`embed_query` serializes with a module lock).
    """

    def __init__(self, model_dir: Path, *, model_name: str, max_seq_length: int = 512) -> None:
        self.model_dir = model_dir
        self.model_name = model_name
        self.max_seq_length = max_seq_length
        self._model: Any = None
        self._device: Device | None = None

    @property
    def model_id(self) -> str:
        """`<model_name>@<directory name>`: the value written to `ticket_embedding.model`."""
        return f"{self.model_name}@{self.model_dir.name}"

    @property
    def device(self) -> Device | None:
        """The device of the loaded model, or None when nothing is loaded."""
        return self._device

    def _check_dir(self) -> None:
        """ConfigError unless the directory exists and holds safetensors weights only."""
        if not self.model_dir.is_dir():
            msg = "embedding model directory missing"
            raise ConfigError(msg)
        if any(p.suffix.lower() in _PICKLE_SUFFIXES for p in self.model_dir.rglob("*")):
            msg = "embedding model: only safetensors weights are allowed"
            raise ConfigError(msg)
        if not (self.model_dir / _WEIGHTS).is_file():
            msg = f"embedding model: {_WEIGHTS} missing"
            raise ConfigError(msg)

    def load(self, device: Device) -> None:
        """Load on `device` (fp16 on cuda, fp32 on cpu); a no-op when already loaded there.

        Raises ConfigError for a missing directory or non-safetensors weights,
        ModelUnavailable("bge-m3 load failed") on any other load failure; a
        `torch.cuda.OutOfMemoryError` propagates to the caller's OOM wrapper.
        """
        if self._model is not None and self._device == device:
            return
        self._check_dir()
        if self._model is not None:
            self.unload()  # at most one loaded model per instance
        os.environ["HF_HUB_OFFLINE"] = "1"  # spec 10 socket guard: local files only
        import sentence_transformers  # noqa: PLC0415 - lazy, as torch
        import torch  # noqa: PLC0415 - lazy: importing this module must never load torch

        try:
            model = sentence_transformers.SentenceTransformer(
                str(self.model_dir), device=device, local_files_only=True
            )
            model = model.half() if device == "cuda" else model.float()
            model.max_seq_length = self.max_seq_length
        except torch.cuda.OutOfMemoryError:
            raise
        except Exception as exc:
            msg = "bge-m3 load failed"
            raise ModelUnavailable(msg) from exc
        self._model, self._device = model, device
        _log.info("enrich.embed.loaded", model=self.model_id, device=device)

    def encode(self, texts: Sequence[str], *, batch_size: int) -> np.ndarray:
        """Unit-norm float32 rows of shape (n, 1024), in input order."""
        if self._model is None:
            msg = "bge-m3 not loaded"
            raise ModelUnavailable(msg)
        rows = self._model.encode(
            list(texts),
            batch_size=batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(rows).astype(np.float32)

    def unload(self) -> None:
        """Drop the model reference and free CUDA memory."""
        self._model = None
        self._device = None
        release_cuda()


@functools.cache
def get_encoder() -> Encoder:
    """The process-wide `Encoder` built from config (U03-30); loading stays lazy.

    Tests reset it with `get_encoder.cache_clear()`. ConfigError from path resolution.
    """
    cfg = get_config()
    settings = cfg.decisions.embedding
    return Encoder(
        EnrichPaths.from_config(cfg).embedding_model_dir(),
        model_name=settings.model,
        max_seq_length=settings.max_seq_length,
    )


class _QueryState:  # module state of `embed_query` (ENG §2.3); tests reset `model_check`
    lock: Final = threading.Lock()
    model_check: bool | None = None  # None: not checked yet, else "the models match"


def _stored_model() -> str | None:
    """One `model` value of `ticket_embedding`, or None for no rows or no table."""
    try:
        table = VectorStore().table("ticket_embedding")
    except NotFoundError:
        return None
    rows = table.search().select(["model"]).limit(1).to_list()
    return str(rows[0]["model"]) if rows else None


def _check_model(model_id: str) -> None:
    """ConfigError when stored vectors came from another model; the result is cached."""
    if _QueryState.model_check is None:
        stored = _stored_model()
        _QueryState.model_check = stored is None or stored == model_id
    if not _QueryState.model_check:
        msg = "embedding model mismatch"
        raise ConfigError(msg)


def _query_device() -> Device:
    """cuda when available, the loaded class leaves room and OpenJev is not up; else cpu."""
    import torch  # noqa: PLC0415 - lazy: importing this module must never load torch

    if not torch.cuda.is_available():
        return "cpu"
    try:
        state = gpu_state()
        free = state.loaded_class() in _GPU_OK and not state.service_healthy("openjev")
    except HernessError as exc:  # unknown GPU state: never contend for VRAM
        _log.warning("enrich.embed.gpu_state_unreadable", error_type=type(exc).__name__)
        return "cpu"
    return "cuda" if free else "cpu"


def embed_query(text: str, /) -> np.ndarray:
    """Embed one redacted query like the ticket embeddings: a unit-norm (1024,) float32 (U03-31).

    Text is normalized with `normalize_text`, which truncates it to 4,000 characters
    (TH03-09). Raises ToolInputError("empty query") when nothing is left, ConfigError on a
    model mismatch with `ticket_embedding`, ModelUnavailable when the model cannot load.
    """
    normalized = normalize_text(text)
    if not normalized:
        msg = "empty query"
        raise ToolInputError(msg)
    with _QueryState.lock:
        encoder = get_encoder()
        _check_model(encoder.model_id)
        encoder.load(_query_device())
        vector: np.ndarray = encoder.encode([normalized], batch_size=1)[0]
        return vector


def embed_texts(
    encoder: Encoder,
    texts: Sequence[str],
    *,
    batch_size: int,
    on_batch: Callable[[int, np.ndarray], None] | None = None,
) -> np.ndarray:
    """Bulk-encode with CUDA OOM backoff; rows align with `texts` (U03-32).

    Texts run shortest first (stable); `on_batch(index, rows)` sees each batch's rows in
    that length order. Errors come from `run_batches_with_oom_backoff` (U03-22).
    """
    if not texts:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    report = None if on_batch is None else lambda i, rows: on_batch(i, np.stack(rows))
    rows = run_batches_with_oom_backoff(
        [texts[i] for i in order],
        lambda batch: list(encoder.encode(batch, batch_size=len(batch))),
        start_batch=batch_size,
        fault_name="embed.batch",
        on_batch=report,
    )
    result = np.empty((len(texts), EMBEDDING_DIM), dtype=np.float32)
    result[order] = np.stack(rows)
    return result
