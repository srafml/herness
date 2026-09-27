"""Tests for herness.harness.memory.store (impl 07 U07-48 Embedder, U07-49 VectorIndex)."""

import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from herness import enrich
from herness.core.errors import ConfigError, ModelUnavailable, SchemaViolation, ToolInputError
from herness.harness.memory import store as s
from herness.store.vectors import EMBEDDING_DIM, VectorStore

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
_CROCK = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # pragma: allowlist secret - Crockford base32 alphabet


def _mid(n: int) -> str:
    """A valid memory id whose order follows ``n``."""
    digits = ""
    for _ in range(26):
        n, rem = divmod(n, 32)
        digits = _CROCK[rem] + digits
    return "mem_" + digits


def _unit(i: int) -> np.ndarray:
    vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    vec[i % EMBEDDING_DIM] = 1.0
    return vec


class FakeEmbed:
    """Deterministic hash-seeded embed_fn that records its calls."""

    def __init__(self, result: Callable[[str], Any] | None = None) -> None:
        self.calls: list[str] = []
        self._result = result

    def __call__(self, text: str) -> Any:
        self.calls.append(text)
        if self._result is not None:
            return self._result(text)
        seed = sum(text.encode("utf-8")) + len(text)
        return np.random.default_rng(seed).normal(size=EMBEDDING_DIM) * 3.0


def _raiser(exc: BaseException) -> Callable[[str], Any]:
    def fn(_text: str) -> Any:
        raise exc

    return fn


# ---------------------------------------------------------------- UT07-22 Embedder


def test_ut07_22_repeat_is_cache_hit() -> None:
    """UT07-22 a repeated text is served from the cache without calling embed_fn again."""
    fake = FakeEmbed()
    emb = s.Embedder(fake, model_name="bge-m3")
    first = emb.embed("revenue by region")
    second = emb.embed("revenue by region")
    assert fake.calls == ["revenue by region"]
    assert second is first
    assert emb.model_name == "bge-m3"


def test_ut07_22_output_is_unit_float32_read_only() -> None:
    """UT07-22 returned arrays are float32, shape (1024,), L2-normalized and read-only."""
    vec = s.Embedder(FakeEmbed(), model_name="m").embed("x")
    assert vec.dtype == np.float32
    assert vec.shape == (EMBEDDING_DIM,)
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5
    assert vec.flags.writeable is False
    with pytest.raises(ValueError, match="read-only"):
        vec[0] = 1.0


def test_ut07_22_overflow_evicts_least_recently_used() -> None:
    """UT07-22 inserting past cache_size evicts the least recently used entry."""
    fake = FakeEmbed()
    emb = s.Embedder(fake, model_name="m", cache_size=2)
    emb.embed("a")
    emb.embed("b")
    emb.embed("a")  # hit, a becomes most recent
    emb.embed("c")  # evicts b
    assert len(emb._cache) == 2
    emb.embed("a")
    assert fake.calls == ["a", "b", "c"]
    emb.embed("b")
    assert fake.calls == ["a", "b", "c", "b"]
    assert len(emb._cache) == 2


def test_ut07_22_cache_keyed_by_sha256_not_text() -> None:
    """UT07-22 the cache key is the SHA-256 hex of the text; the text itself is not kept."""
    emb = s.Embedder(FakeEmbed(), model_name="m")
    emb.embed("secret customer note")
    (key,) = emb._cache.keys()
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)
    assert "secret" not in key


@pytest.mark.parametrize(
    "exc", [RuntimeError("boom"), OSError("io"), ValueError("v"), MemoryError()]
)
def test_ut07_22_embed_fn_failure_is_model_unavailable(exc: BaseException) -> None:
    """UT07-22 RuntimeError/OSError/ValueError/MemoryError from embed_fn -> ModelUnavailable."""
    emb = s.Embedder(_raiser(exc), model_name="m")
    with pytest.raises(ModelUnavailable, match="memory embedding failed") as info:
        emb.embed("the text")
    assert "the text" not in str(info.value)
    assert emb._cache == {}


def test_ut07_22_config_and_input_errors_propagate() -> None:
    """UT07-22 ConfigError (model mismatch) and ToolInputError (empty text) propagate as is."""
    with pytest.raises(ConfigError):
        s.Embedder(_raiser(ConfigError("model mismatch")), model_name="m").embed("x")
    with pytest.raises(ToolInputError):
        s.Embedder(_raiser(ToolInputError("empty query")), model_name="m").embed(" ")
    with pytest.raises(ModelUnavailable, match="cannot load"):
        s.Embedder(_raiser(ModelUnavailable("cannot load")), model_name="m").embed("x")


def _nan_vec() -> np.ndarray:
    vec = np.ones(EMBEDDING_DIM)
    vec[5] = np.nan
    return vec


@pytest.mark.parametrize(
    "bad",
    [
        _nan_vec(),
        np.full(EMBEDDING_DIM, np.inf),
        np.zeros(EMBEDDING_DIM),
        np.ones(512),
        np.ones((1, EMBEDDING_DIM)),
        np.ones(EMBEDDING_DIM, dtype=np.int64),
        [1.0] * EMBEDDING_DIM,
        None,
    ],
    ids=["nan", "inf", "zero_norm", "short", "2d", "int", "list", "none"],
)
def test_ut07_22_invalid_output_is_model_unavailable(bad: object) -> None:
    """UT07-22 NaN, inf, zero norm, wrong shape or dtype -> ModelUnavailable, nothing cached."""

    def fn(_text: str) -> Any:
        return bad

    emb = s.Embedder(fn, model_name="m")
    with pytest.raises(ModelUnavailable, match="memory embedding invalid output"):
        emb.embed("x")
    assert emb._cache == {}


def test_ut07_22_huge_finite_values_normalize() -> None:
    """UT07-22 finite values beyond float32 range still normalize (norm computed in float64)."""
    vec = s.Embedder(lambda _t: np.full(EMBEDDING_DIM, 1e300), model_name="m").embed("x")
    assert np.isfinite(vec).all()
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5


def test_ut07_22_embed_item_prefixes_kind() -> None:
    """UT07-22 embed_item embeds ``kind + ": " + content``; an unknown kind is rejected."""
    fake = FakeEmbed()
    emb = s.Embedder(fake, model_name="m")
    emb.embed_item("glossary", "ARR means annual recurring revenue")
    assert fake.calls == ["glossary: ARR means annual recurring revenue"]
    with pytest.raises(ToolInputError, match="unknown memory kind"):
        emb.embed_item("nope", "x")  # type: ignore[arg-type]


def test_ut07_22_cache_size_must_be_positive() -> None:
    """UT07-22 cache_size below 1 is a ConfigError."""
    with pytest.raises(ConfigError, match="cache_size"):
        s.Embedder(FakeEmbed(), model_name="m", cache_size=0)


def test_ut07_22_model_call_runs_outside_lock() -> None:
    """UT07-22 embed_fn runs without the cache lock held; concurrent callers are safe."""
    holder: dict[str, s.Embedder] = {}
    seen: list[bool] = []

    def fn(text: str) -> Any:
        seen.append(holder["emb"]._lock.locked())
        return FakeEmbed()(text)

    emb = s.Embedder(fn, model_name="m", cache_size=8)
    holder["emb"] = emb
    threads = [
        threading.Thread(target=lambda i=i: [emb.embed(f"t{i % 12}") for _ in range(20)])
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen
    assert not any(seen)
    assert len(emb._cache) <= 8


def test_ut07_22_default_embed_fn_resolves_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-22 the default embed_fn is herness.enrich.embed_query, resolved at first use."""
    fake = FakeEmbed()
    monkeypatch.setattr(enrich, "embed_query", fake, raising=False)
    emb = s.Embedder(model_name="m")
    emb.embed("q")
    assert fake.calls == ["q"]


def test_ut07_22_import_does_not_load_embed_module() -> None:
    """UT07-22 importing the store module does not import herness.enrich.embed (circularity)."""
    code = (
        "import sys, herness.harness.memory.store\n"
        "sys.stdout.write(str('herness.enrich.embed' in sys.modules))"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
    )
    assert out.stdout.strip() == "False"


# ---------------------------------------------------------------- UT07-23 VectorIndex


@pytest.fixture
def vector_tmp(tmp_path: Path) -> s.VectorIndex:
    """A VectorIndex on a fresh LanceDB VectorStore in tmp_path."""
    return s.VectorIndex(lambda: VectorStore(tmp_path / "vectors"))


def _row(
    n: int, *, layer: Any = "semantic", kind: Any = "glossary", status: Any = "active"
) -> s.VectorRow:
    return s.VectorRow(
        memory_id=_mid(n),
        layer=layer,
        kind=kind,
        status=status,
        content_hash=f"h{n}",
        model="bge-m3",
        vector=_unit(n),
    )


def test_ut07_23_upsert_search_roundtrip(vector_tmp: s.VectorIndex) -> None:
    """UT07-23 upsert then search returns the nearest ids with cosine distances."""
    vector_tmp.ensure_table()
    vector_tmp.upsert([_row(i) for i in range(5)] + [_row(9, layer="episodic", kind="run_summary")])
    hits = vector_tmp.search(_unit(3), ["semantic"], ["active"], 3)
    assert hits[0][0] == _mid(3)
    assert hits[0][1] == pytest.approx(0.0, abs=1e-6)
    assert len(hits) == 3
    assert _mid(9) not in {h[0] for h in hits}
    episodic = vector_tmp.search(_unit(9), ["episodic"], ["active", "candidate"], 10)
    assert [h[0] for h in episodic] == [_mid(9)]


def test_ut07_23_upsert_merges_on_memory_id(vector_tmp: s.VectorIndex) -> None:
    """UT07-23 a second upsert with the same memory_id updates the row instead of adding one."""
    vector_tmp.upsert([_row(1), _row(2)])
    vector_tmp.upsert([_row(1, status="candidate"), _row(1, status="expired")])
    metas = vector_tmp.list_ids("", 10)
    assert [(m.memory_id, m.status) for m in metas] == [(_mid(1), "expired"), (_mid(2), "active")]
    assert metas[0] == s.VectorMeta(_mid(1), "expired", "h1", "bge-m3")
    vector_tmp.upsert([])


def test_ut07_23_set_status_filters_search(vector_tmp: s.VectorIndex) -> None:
    """UT07-23 set_status mirrors the status so a status-filtered search excludes the item."""
    vector_tmp.upsert([_row(i) for i in range(3)])
    vector_tmp.set_status([_mid(1)], "rejected")
    vector_tmp.set_status([], "rejected")
    ids = {h[0] for h in vector_tmp.search(_unit(1), ["semantic"], ["active"], 10)}
    assert ids == {_mid(0), _mid(2)}
    rejected = vector_tmp.search(_unit(1), ["semantic"], ["rejected"], 10)
    assert [h[0] for h in rejected] == [_mid(1)]


def test_ut07_23_set_status_chunks_of_200(tmp_path: Path) -> None:
    """UT07-23 set_status issues one update per 200 ids."""
    idx = s.VectorIndex(lambda: VectorStore(tmp_path / "v"))
    idx.upsert([_row(i) for i in range(450)])
    calls: list[str] = []
    table = idx._tbl()
    original = table.update

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["where"])
        return original(*args, **kwargs)

    idx._table = type("T", (), {"update": staticmethod(spy)})()
    idx.set_status([_mid(i) for i in range(450)], "expired")
    assert len(calls) == 3
    idx._table = table
    assert {m.status for m in idx.list_ids("", 1000)} == {"expired"}


def test_ut07_23_delete_and_vectors(vector_tmp: s.VectorIndex) -> None:
    """UT07-23 vectors returns read-only arrays by id; delete removes rows; empty is a no-op."""
    vector_tmp.upsert([_row(i) for i in range(4)])
    got = vector_tmp.vectors([_mid(0), _mid(2), _mid(7)])
    assert set(got) == {_mid(0), _mid(2)}
    assert np.array_equal(got[_mid(2)], _unit(2))
    assert got[_mid(2)].dtype == np.float32
    assert got[_mid(2)].flags.writeable is False
    assert vector_tmp.vectors([]) == {}
    vector_tmp.delete([_mid(0), _mid(0), _mid(1)])
    vector_tmp.delete([])
    assert [m.memory_id for m in vector_tmp.list_ids("", 10)] == [_mid(2), _mid(3)]


def test_ut07_23_delete_chunks_of_200(tmp_path: Path) -> None:
    """UT07-23 delete goes through VectorStore.delete_ids in chunks of 200 ids."""
    idx = s.VectorIndex(lambda: VectorStore(tmp_path / "v"))
    idx.upsert([_row(i) for i in range(401)])
    assert idx._store is not None
    sizes: list[int] = []
    original = idx._store.delete_ids

    def spy(name: Any, column: str, ids: Any) -> int:
        sizes.append(len(ids))
        return original(name, column, ids)

    idx._store.delete_ids = spy  # type: ignore[method-assign]
    idx.delete([_mid(i) for i in range(401)])
    assert sizes == [200, 200, 1]
    assert idx.list_ids("", 10) == []


def test_ut07_23_list_ids_pages_in_order(vector_tmp: s.VectorIndex) -> None:
    """UT07-23 list_ids pages by ``memory_id > after`` in memory_id order; "" starts."""
    vector_tmp.upsert([_row(i) for i in (5, 1, 4, 2, 3)])
    page1 = vector_tmp.list_ids("", 2)
    assert [m.memory_id for m in page1] == [_mid(1), _mid(2)]
    page2 = vector_tmp.list_ids(page1[-1].memory_id, 2)
    assert [m.memory_id for m in page2] == [_mid(3), _mid(4)]
    assert [m.memory_id for m in vector_tmp.list_ids(_mid(5), 2)] == []


def test_ut07_23_search_empty_filters_return_nothing(vector_tmp: s.VectorIndex) -> None:
    """UT07-23 an empty layer or status list matches nothing."""
    vector_tmp.upsert([_row(1)])
    assert vector_tmp.search(_unit(1), [], ["active"], 5) == []
    assert vector_tmp.search(_unit(1), ["semantic"], [], 5) == []


@pytest.mark.parametrize("limit", [0, -1, 201, True, 2.0])
def test_ut07_23_search_limit_bounded(vector_tmp: s.VectorIndex, limit: Any) -> None:
    """UT07-23 search limit must be an int in 1..200."""
    with pytest.raises(ToolInputError, match="limit"):
        vector_tmp.search(_unit(1), ["semantic"], ["active"], limit)


@pytest.mark.parametrize("limit", [0, 1001, False])
def test_ut07_23_list_ids_limit_bounded(vector_tmp: s.VectorIndex, limit: Any) -> None:
    """UT07-23 list_ids limit must be an int in 1..1000."""
    with pytest.raises(ToolInputError, match="limit"):
        vector_tmp.list_ids("", limit)


@pytest.mark.parametrize(
    "vec",
    [np.ones(3), _nan_vec(), np.zeros(EMBEDDING_DIM), [0.1] * EMBEDDING_DIM],
    ids=["short", "nan", "zero", "list"],
)
def test_ut07_23_bad_query_vector(vector_tmp: s.VectorIndex, vec: Any) -> None:
    """UT07-23 a malformed query vector is a ToolInputError."""
    with pytest.raises(ToolInputError, match="invalid query vector"):
        vector_tmp.search(vec, ["semantic"], ["active"], 5)


def test_ut07_23_bad_id_is_tool_input_error(vector_tmp: s.VectorIndex) -> None:
    """UT07-23 a malformed id in any id-taking method is a ToolInputError."""
    bad = "mem_short"
    for call in (
        lambda: vector_tmp.set_status([bad], "active"),
        lambda: vector_tmp.delete([_mid(1), bad]),
        lambda: vector_tmp.vectors([bad]),
        lambda: vector_tmp.list_ids(bad, 5),
        lambda: vector_tmp.set_status(_mid(1), "active"),
    ):
        with pytest.raises(ToolInputError, match="invalid id in vector filter"):
            call()


@pytest.mark.parametrize(
    "row",
    [
        _row(1, layer="bogus"),
        _row(1, kind="bogus"),
        _row(1, status="bogus"),
        s.VectorRow(_mid(1), "semantic", "glossary", "active", "", "m", _unit(1)),
        s.VectorRow(_mid(1), "semantic", "glossary", "active", "h", "", _unit(1)),
        s.VectorRow(_mid(1), "semantic", "glossary", "active", "h", "m", np.ones(5)),
        s.VectorRow(_mid(1), "semantic", "glossary", "active", "h", "m", _nan_vec()),
        s.VectorRow(_mid(1), "semantic", "glossary", "active", "h", "m", np.zeros(EMBEDDING_DIM)),
        s.VectorRow(_mid(1), "semantic", "glossary", "active", "h" * 257, "m", _unit(1)),
        s.VectorRow("mem_bad", "semantic", "glossary", "active", "h", "m", _unit(1)),
    ],
    ids=["layer", "kind", "status", "hash", "model", "shape", "nan", "zero", "long", "id"],
)
def test_ut07_23_invalid_row_rejected(vector_tmp: s.VectorIndex, row: s.VectorRow) -> None:
    """UT07-23 upsert rejects a row with a bad field before writing anything."""
    with pytest.raises(ToolInputError, match="invalid vector row"):
        vector_tmp.upsert([_row(2), row])
    assert vector_tmp.list_ids("", 10) == []


def test_ut07_23_open_failure_is_model_unavailable() -> None:
    """UT07-23 a VectorStore that cannot open maps to ModelUnavailable (no leak)."""

    def factory() -> VectorStore:
        msg = "disk gone"
        raise OSError(msg)

    idx = s.VectorIndex(factory)
    with pytest.raises(ModelUnavailable, match="memory vector store unavailable: ensure_table"):
        idx.ensure_table()

    def factory2() -> VectorStore:
        msg = "cannot open vector store"
        raise SchemaViolation(msg)

    with pytest.raises(ModelUnavailable, match="memory vector store unavailable: ensure_table"):
        s.VectorIndex(factory2).list_ids("", 1)


@pytest.mark.parametrize("exc", [RuntimeError("commit conflict"), ValueError("bad"), OSError("io")])
def test_ut07_23_lancedb_failure_is_model_unavailable(
    vector_tmp: s.VectorIndex, exc: BaseException
) -> None:
    """UT07-23 OSError/ValueError/RuntimeError from lancedb -> ModelUnavailable per op."""
    vector_tmp.upsert([_row(1)])

    def boom(*_a: Any, **_k: Any) -> Any:
        raise exc

    broken = type(
        "Broken",
        (),
        {"merge_insert": boom, "update": boom, "search": boom, "checkout_latest": boom},
    )()
    vector_tmp._table = broken
    assert vector_tmp._store is not None
    vector_tmp._store.delete_ids = boom  # type: ignore[method-assign]
    ops: dict[str, Callable[[], object]] = {
        "upsert": lambda: vector_tmp.upsert([_row(2)]),
        "set_status": lambda: vector_tmp.set_status([_mid(1)], "active"),
        "delete": lambda: vector_tmp.delete([_mid(1)]),
        "search": lambda: vector_tmp.search(_unit(1), ["semantic"], ["active"], 5),
        "vectors": lambda: vector_tmp.vectors([_mid(1)]),
        "list_ids": lambda: vector_tmp.list_ids("", 5),
    }
    for op, call in ops.items():
        with pytest.raises(ModelUnavailable, match=f"memory vector store unavailable: {op}$"):
            call()
