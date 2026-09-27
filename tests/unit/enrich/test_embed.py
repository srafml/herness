"""Tests for herness.enrich.embed (U03-29 ... U03-32; T03-06).

The encoder tests load the tiny sentence-transformers model in
`tests/fixtures/models/tiny-st/` (built by `tests/support/make_tiny_st.py`) on the CPU.
Device selection monkeypatches `torch.cuda.is_available` and `embed.gpu_state`, so nothing
here needs a real GPU.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import sentence_transformers
import torch
from tests.support.config_tree import write_full_config
from tests.support.make_tiny_st import TINY_ST

import herness.enrich
from herness.core.config import init_config
from herness.core.errors import ConfigError, ModelUnavailable, StoreBusy, ToolInputError
from herness.enrich import embed
from herness.enrich.embed import Encoder, embed_query, embed_texts, get_encoder
from herness.store.vectors import TICKET_EMBEDDING_SCHEMA, VectorStore

pytestmark = pytest.mark.unit

_NAME = "test/tiny"
_MODEL_ID = f"{_NAME}@tiny-st"


@pytest.fixture(autouse=True)
def _reset_embed_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh `get_encoder` cache and model-check result; no real CUDA release."""
    get_encoder.cache_clear()
    monkeypatch.setattr(embed._QueryState, "model_check", None)
    monkeypatch.setattr(embed, "release_cuda", lambda: None)
    yield
    get_encoder.cache_clear()


@pytest.fixture(scope="module")
def tiny() -> Iterator[Encoder]:
    """One tiny-st encoder loaded on the CPU, shared by the module's read-only tests."""
    encoder = Encoder(TINY_ST, model_name=_NAME)
    encoder.load("cpu")
    yield encoder
    encoder.unload()


class _Gpu:
    """A fake `GpuStateReader`."""

    def __init__(self, loaded: str, *, openjev_healthy: bool = False) -> None:
        self.loaded = loaded
        self.openjev_healthy = openjev_healthy
        self.asked: list[str] = []

    def loaded_class(self) -> str:
        return self.loaded

    def service_healthy(self, name: str) -> bool:
        self.asked.append(name)
        return self.openjev_healthy


class _RecordingEncoder:
    """Stands in for `Encoder` in device and ordering tests; vectors encode text length."""

    def __init__(self, model_id: str = _MODEL_ID) -> None:
        self.model_id = model_id
        self.devices: list[str] = []
        self.batches: list[list[str]] = []

    def load(self, device: str) -> None:
        self.devices.append(device)

    def encode(self, texts: Sequence[str], *, batch_size: int) -> np.ndarray:
        assert batch_size == len(texts)
        self.batches.append(list(texts))
        rows = np.zeros((len(texts), 1024), dtype=np.float32)
        rows[:, 0] = [len(t) for t in texts]
        return rows


def _copy_tiny(tmp_path: Path) -> Path:
    target = tmp_path / "tiny-st"
    shutil.copytree(TINY_ST, target)
    return target


def _vectors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, models: Sequence[str]) -> Path:
    """A LanceDB store under `tmp_path` whose `ticket_embedding` holds one row per model."""
    path = tmp_path / "vectors"
    store = VectorStore(path)
    store.ensure_tables()
    if models:
        rows = [
            {
                "record_id": f"INC-{i}",
                "entity": "incident",
                "service_id": None,
                "opened_at": None,
                "content_hash": f"{i:032x}",
                "model": model,
                "vector": [0.0] * 1024,
            }
            for i, model in enumerate(models)
        ]
        import pyarrow as pa  # noqa: PLC0415 - only this helper needs it

        store.table("ticket_embedding").add(pa.Table.from_pylist(rows, TICKET_EMBEDDING_SCHEMA))
    monkeypatch.setattr(embed, "VectorStore", lambda: VectorStore(path))
    return path


def _query_setup(
    monkeypatch: pytest.MonkeyPatch, encoder: Any, gpu: _Gpu, *, cuda: bool = True
) -> None:
    monkeypatch.setattr(embed, "get_encoder", lambda: encoder)
    monkeypatch.setattr(embed, "gpu_state", lambda: gpu)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: cuda)


# --- UT03-26: Encoder and get_encoder --------------------------------------------------


def test_ut03_26_load_cpu_encode_unit_norm_float32(tiny: Encoder) -> None:
    """UT03-26 tiny-st loaded on cpu: encode gives unit-norm float32 rows of shape (n, 1024)."""
    rows = tiny.encode(["disk failure", "vpn login timeout", "printer"], batch_size=2)
    assert rows.shape == (3, 1024)
    assert rows.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(rows, axis=1), 1.0, rtol=1e-5)
    assert tiny.device == "cpu"
    assert tiny.model_id == _MODEL_ID


def test_ut03_26_rows_follow_input_order(tiny: Encoder) -> None:
    """UT03-26 encode keeps input order: each row equals that text encoded alone."""
    texts = ["server outage", "a", "password reset failed for user"]
    rows = tiny.encode(texts, batch_size=3)
    for text, row in zip(texts, rows, strict=True):
        np.testing.assert_allclose(tiny.encode([text], batch_size=1)[0], row, atol=1e-5)


def test_ut03_26_load_same_device_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-26 a second load on the same device keeps the model; HF_HUB_OFFLINE=1 is set."""
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    encoder = Encoder(TINY_ST, model_name=_NAME, max_seq_length=256)
    encoder.load("cpu")
    model = encoder._model
    encoder.load("cpu")
    assert encoder._model is model
    assert model.max_seq_length == 256
    assert next(model.parameters()).dtype == torch.float32
    import os  # noqa: PLC0415 - read after load

    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_ut03_26_unload_drops_model_and_releases_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-26 unload drops the reference, calls release_cuda; encode then is ModelUnavailable."""
    released: list[int] = []
    monkeypatch.setattr(embed, "release_cuda", lambda: released.append(1))
    encoder = Encoder(TINY_ST, model_name=_NAME)
    encoder.load("cpu")
    encoder.unload()
    assert encoder.device is None
    assert released == [1]
    with pytest.raises(ModelUnavailable):
        encoder.encode(["disk"], batch_size=1)


@pytest.mark.parametrize("name", ["pytorch_model.bin", "2_Dense/pytorch_model.bin", "w.pkl"])
def test_ut03_26_non_safetensors_weights_are_config_error(tmp_path: Path, name: str) -> None:
    """UT03-26 a directory holding a `.bin` or `.pkl` weight file -> ConfigError, not loaded."""
    model_dir = _copy_tiny(tmp_path)
    (model_dir / name).write_bytes(b"\x80\x04not a real pickle")
    encoder = Encoder(model_dir, model_name=_NAME)
    with pytest.raises(ConfigError, match="safetensors"):
        encoder.load("cpu")
    assert encoder.device is None


def test_ut03_26_missing_directory_is_config_error(tmp_path: Path) -> None:
    """UT03-26 a missing model directory -> ConfigError."""
    with pytest.raises(ConfigError, match="directory"):
        Encoder(tmp_path / "absent", model_name=_NAME).load("cpu")


def test_ut03_26_missing_safetensors_is_config_error(tmp_path: Path) -> None:
    """UT03-26 a directory without `model.safetensors` -> ConfigError."""
    model_dir = _copy_tiny(tmp_path)
    (model_dir / "model.safetensors").unlink()
    with pytest.raises(ConfigError, match=r"model\.safetensors"):
        Encoder(model_dir, model_name=_NAME).load("cpu")


def test_ut03_26_load_failure_is_model_unavailable(tmp_path: Path) -> None:
    """UT03-26 a model that fails to load -> ModelUnavailable("bge-m3 load failed")."""
    model_dir = _copy_tiny(tmp_path)
    (model_dir / "config.json").write_text("{", encoding="utf-8")
    with pytest.raises(ModelUnavailable, match="bge-m3 load failed"):
        Encoder(model_dir, model_name=_NAME).load("cpu")


class _FakeST:
    """A fake `SentenceTransformer` recording its arguments and dtype calls."""

    instances: list[_FakeST] = []  # noqa: RUF012 - test-local registry

    def __init__(self, path: str, *, device: str, local_files_only: bool) -> None:
        self.path, self.device_arg, self.local_files_only = path, device, local_files_only
        self.calls: list[str] = []
        self.max_seq_length = 0
        _FakeST.instances.append(self)

    def half(self) -> _FakeST:
        self.calls.append("half")
        return self

    def float(self) -> _FakeST:
        self.calls.append("float")
        return self


def test_ut03_26_cuda_load_is_fp16_and_device_change_reloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT03-26 load(cuda) calls half(), local files only; a device change reloads in fp32."""
    _FakeST.instances.clear()
    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeST)
    released: list[int] = []
    monkeypatch.setattr(embed, "release_cuda", lambda: released.append(1))
    encoder = Encoder(TINY_ST, model_name=_NAME)
    encoder.load("cuda")
    assert encoder.device == "cuda"
    encoder.load("cpu")
    assert encoder.device == "cpu"
    first, second = _FakeST.instances
    assert (first.calls, second.calls) == (["half"], ["float"])
    assert first.local_files_only
    assert first.device_arg == "cuda"
    assert first.max_seq_length == 512
    assert released == [1]  # the cuda model was released before the cpu load


def test_ut03_26_cuda_oom_on_load_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-26 torch.cuda.OutOfMemoryError during load propagates unchanged."""

    def boom(*_args: object, **_kwargs: object) -> None:
        msg = "simulated oom"
        raise torch.cuda.OutOfMemoryError(msg)

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", boom)
    with pytest.raises(torch.cuda.OutOfMemoryError):
        Encoder(TINY_ST, model_name=_NAME).load("cuda")


def test_ut03_26_get_encoder_builds_from_config_once(tmp_path: Path) -> None:
    """UT03-26 get_encoder: one cached Encoder from config; cache_clear builds a new one."""
    cfg = init_config(config_dir=write_full_config(tmp_path), env={})
    encoder = get_encoder()
    assert get_encoder() is encoder
    embedding = cfg.decisions.embedding
    assert encoder.model_id.startswith(f"{embedding.model}@")
    assert encoder.model_id == f"{embedding.model}@{encoder.model_dir.name}"
    assert encoder.max_seq_length == embedding.max_seq_length
    assert encoder.device is None  # loading is lazy
    get_encoder.cache_clear()
    assert get_encoder() is not encoder


# --- UT03-27: model mismatch against ticket_embedding ------------------------------------


def test_ut03_27_model_mismatch_is_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tiny: Encoder
) -> None:
    """UT03-27 ticket_embedding rows of model `other@x` -> ConfigError (model mismatch)."""
    _vectors(tmp_path, monkeypatch, ["other@x"])
    _query_setup(monkeypatch, tiny, _Gpu("reasoning"))
    with pytest.raises(ConfigError, match="embedding model mismatch"):
        embed_query("disk failure")
    monkeypatch.setattr(embed, "VectorStore", _no_store)
    with pytest.raises(ConfigError, match="embedding model mismatch"):
        embed_query("disk failure")  # the cached result, the store is not read again


def _no_store() -> VectorStore:
    msg = "vector store read twice"
    raise AssertionError(msg)


def test_ut03_27_matching_model_embeds_and_check_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tiny: Encoder
) -> None:
    """UT03-27 rows of the encoder's own model: a unit-norm (1024,) float32; checked once."""
    _vectors(tmp_path, monkeypatch, [_MODEL_ID, _MODEL_ID])
    _query_setup(monkeypatch, tiny, _Gpu("reasoning"))
    vector = embed_query("disk failure")
    assert vector.shape == (1024,)
    assert vector.dtype == np.float32
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-5
    monkeypatch.setattr(embed, "VectorStore", _no_store)
    np.testing.assert_allclose(embed_query("disk  failure "), vector, atol=1e-6)


@pytest.mark.parametrize("state", ["empty", "missing"])
def test_ut03_27_no_rows_or_missing_table_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """UT03-27 an empty or missing ticket_embedding table counts as no rows: no mismatch."""
    if state == "empty":
        _vectors(tmp_path, monkeypatch, [])
    else:
        path = tmp_path / "vectors"
        monkeypatch.setattr(embed, "VectorStore", lambda: VectorStore(path))
    encoder = _RecordingEncoder(model_id="BAAI/bge-m3@abc")
    _query_setup(monkeypatch, encoder, _Gpu("reasoning"))
    assert embed_query("vpn")[0] == 3.0
    assert embed._QueryState.model_check is True


def test_ut03_27_store_error_propagates_and_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-27 a store error other than NotFoundError propagates; the check stays pending."""

    def busy() -> VectorStore:
        msg = "busy"
        raise StoreBusy(msg)

    monkeypatch.setattr(embed, "VectorStore", busy)
    _query_setup(monkeypatch, _RecordingEncoder(), _Gpu("reasoning"))
    with pytest.raises(StoreBusy):
        embed_query("vpn")
    assert embed._QueryState.model_check is None


# --- UT03-28: device choice and input bounds ---------------------------------------------


@pytest.fixture
def no_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _vectors(tmp_path, monkeypatch, [])


@pytest.mark.usefixtures("no_rows")
def test_ut03_28_reasoning_loaded_gives_cpu(monkeypatch: pytest.MonkeyPatch, tiny: Encoder) -> None:
    """UT03-28 fake gpu_state reporting `reasoning` (CUDA available) -> device cpu."""
    _query_setup(monkeypatch, tiny, _Gpu("reasoning"))
    vector = embed_query("server outage")
    assert tiny.device == "cpu"
    assert vector.shape == (1024,)


@pytest.mark.usefixtures("no_rows")
@pytest.mark.parametrize("text", ["", "   \t\n ", "　"])
def test_ut03_28_empty_text_is_tool_input_error(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """UT03-28 empty text after normalize_text -> ToolInputError("empty query"), nothing loaded."""
    encoder = _RecordingEncoder()
    _query_setup(monkeypatch, encoder, _Gpu("reasoning"))
    with pytest.raises(ToolInputError, match="empty query"):
        embed_query(text)
    assert encoder.devices == []


@pytest.mark.usefixtures("no_rows")
@pytest.mark.parametrize(
    ("cuda", "loaded", "openjev", "device"),
    [
        (True, "none", False, "cuda"),
        (True, "decider", False, "cuda"),
        (True, "decider", True, "cpu"),
        (True, "reasoning", False, "cpu"),
        (True, "swapping", False, "cpu"),
        (False, "none", False, "cpu"),
    ],
)
def test_ut03_28_device_rule(
    monkeypatch: pytest.MonkeyPatch, *, cuda: bool, loaded: str, openjev: bool, device: str
) -> None:
    """UT03-28 cuda only when available, class none/decider and OpenJev not healthy."""
    encoder = _RecordingEncoder()
    gpu = _Gpu(loaded, openjev_healthy=openjev)
    _query_setup(monkeypatch, encoder, gpu, cuda=cuda)
    embed_query("disk")
    assert encoder.devices == [device]
    if cuda and loaded in {"none", "decider"}:
        assert gpu.asked == ["openjev"]


@pytest.mark.usefixtures("no_rows")
def test_ut03_28_unreadable_gpu_state_gives_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-28 gpu_state failing with a HernessError -> cpu (fail closed, never contend)."""

    class _Broken(_Gpu):
        def loaded_class(self) -> str:
            msg = "no jobs backend"
            raise ModelUnavailable(msg)

    encoder = _RecordingEncoder()
    _query_setup(monkeypatch, encoder, _Broken("none"))
    embed_query("disk")
    assert encoder.devices == ["cpu"]


@pytest.mark.usefixtures("no_rows")
def test_ut03_28_text_over_4000_chars_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-28 text over 4,000 chars is truncated by normalize_text (TH03-09), not rejected."""
    encoder = _RecordingEncoder()
    _query_setup(monkeypatch, encoder, _Gpu("reasoning"))
    vector = embed_query("word " * 2000)
    assert encoder.batches == [[("word " * 800).strip()]]
    assert vector[0] == 3999.0


# --- UT03-29: embed_texts -----------------------------------------------------------------


def test_ut03_29_output_order_equals_input_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-29 texts of varied length: rows align with the input; batches run shortest first."""
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    encoder = _RecordingEncoder()
    texts = ["x" * n for n in (7, 1, 12, 3, 3, 30, 2)]
    seen: list[tuple[int, list[float]]] = []
    rows = embed_texts(
        encoder,  # type: ignore[arg-type]
        texts,
        batch_size=3,
        on_batch=lambda i, block: seen.append((i, block[:, 0].tolist())),
    )
    assert rows.shape == (7, 1024)
    assert rows.dtype == np.float32
    assert rows[:, 0].tolist() == [7.0, 1.0, 12.0, 3.0, 3.0, 30.0, 2.0]
    assert [len(b) for b in encoder.batches] == [3, 3, 1]
    assert seen == [(0, [1.0, 2.0, 3.0]), (1, [3.0, 7.0, 12.0]), (2, [30.0])]


def test_ut03_29_real_encoder_rows_match_single_encodes(tiny: Encoder) -> None:
    """UT03-29 with tiny-st: each output row equals its text encoded alone."""
    texts = ["backup job failed overnight", "vpn", "disk", "email service slow for users"]
    rows = embed_texts(tiny, texts, batch_size=2)
    for text, row in zip(texts, rows, strict=True):
        np.testing.assert_allclose(tiny.encode([text], batch_size=1)[0], row, atol=1e-5)


def test_ut03_29_no_texts_gives_empty_matrix() -> None:
    """UT03-29 no texts -> a (0, 1024) float32 matrix, no encode call."""
    encoder = _RecordingEncoder()
    rows = embed_texts(encoder, [], batch_size=8)  # type: ignore[arg-type]
    assert rows.shape == (0, 1024)
    assert rows.dtype == np.float32
    assert encoder.batches == []


def test_ut03_28_package_facade_exports_embed_query() -> None:
    """UT03-28 `herness.enrich` exports `embed_query` (U03-31 public entry point)."""
    assert herness.enrich.embed_query is embed_query
    assert "embed_query" in herness.enrich.__all__
