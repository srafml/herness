"""The build fixture's stand-in encoder (impl 11 U11-48, T11-34, T11-41).

UT11-121 (determinism) .. UT11-125 (pickle-free): `TopicEncoder` is a pure function of the text:
two instances, any batch split and any text order give the same unit-norm float32 rows, and the
cosine geometry follows the generator's own labels (family, component, host).
"""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest
from tests.support import _build_stubs
from tests.support._build_stubs import TopicEncoder

from herness.store.vectors import EMBEDDING_DIM
from tools.synth.text_vocab import FAMILY_ROWS

pytestmark = pytest.mark.unit

_EN = (
    "{symptom} in {comp}\n\nUsers report {symptom} in {comp}. "
    "Monitoring shows {topic} on {host}. {tail}"
)
_ES = (
    "Incidencia en {comp}: {topic}\n\nIncidencia en {comp} por {topic} "
    "en el servidor {host}. {tail}"
)
_TAIL = "Minor impact on a few users."


def _en(topic: str, comp: str, host: str, symptom: str = "slow checkout") -> str:
    return _EN.format(symptom=symptom, comp=comp, topic=topic, host=host, tail=_TAIL)


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b)


def _encode(*texts: str) -> np.ndarray:
    return TopicEncoder().encode(list(texts), batch_size=2)


def test_ut11_121_deterministic_across_instances_batches_and_order() -> None:
    """UT11-121: same text -> same vector for two fresh instances, any batch size, any order."""
    texts = [_en(FAMILY_ROWS[i][1], f"Comp {i}", f"web{i:02d}") for i in range(7)]
    first = TopicEncoder().encode(texts, batch_size=1)
    second = TopicEncoder().encode(texts, batch_size=64)
    assert np.array_equal(first, second)
    shuffled = TopicEncoder().encode(texts[::-1], batch_size=3)
    assert np.array_equal(shuffled[::-1], first)


def test_ut11_122_shape_dtype_unit_norm_and_empty() -> None:
    """UT11-122: (n, 1024) float32 unit-norm rows in input order; no texts -> (0, 1024)."""
    out = _encode(_en("packet loss", "Rapid Bridge", "web43"), "plain text without labels")
    assert out.shape == (2, EMBEDDING_DIM)
    assert out.dtype == np.float32
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)
    empty = TopicEncoder().encode([], batch_size=8)
    assert empty.shape == (0, EMBEDDING_DIM)
    assert empty.dtype == np.float32


def test_ut11_123_topic_separation_follows_family_component_host() -> None:
    """UT11-123: one family + component + host is closest, then one family, then none."""
    topic, other = FAMILY_ROWS[19][1], FAMILY_ROWS[3][1]
    base = _en(topic, "Rapid Bridge", "web43", "failed health checks")
    same = _en(topic, "Rapid Bridge", "web43", "slow checkout")
    other_comp = _en(topic, "Iron Comet", "db07", "slow checkout")
    other_family = _en(other, "Iron Comet", "db07", "slow checkout")
    v = _encode(base, same, other_comp, other_family)
    near, mid, far = _cos(v[0], v[1]), _cos(v[0], v[2]), _cos(v[0], v[3])
    assert near > 0.95
    assert 0.85 < mid < near - 0.02
    assert far < mid - 0.4
    assert _cos(v[0], _encode(_en(topic, "Rapid Bridge", "db07"))[0]) > mid  # a shared component


def test_ut11_124_english_and_spanish_topics_share_a_family() -> None:
    """UT11-124: the EN and ES phrasing of one family are closer than another family's."""
    _, en_topic, es_topic, _ = FAMILY_ROWS[19]
    other_es = FAMILY_ROWS[3][2]
    v = _encode(
        _EN.format(
            symptom="slow checkout", comp="Rapid Bridge", topic=en_topic, host="web43", tail=_TAIL
        ),
        _ES.format(
            comp="Rapid Bridge",
            topic=es_topic,
            host="web43",
            tail="Impacto menor en pocos usuarios.",
        ),
        _ES.format(
            comp="Rapid Bridge",
            topic=other_es,
            host="web43",
            tail="Impacto menor en pocos usuarios.",
        ),
    )
    assert _cos(v[0], v[1]) > _cos(v[0], v[2]) + 0.2


def test_ut11_125_distinct_texts_never_coincide() -> None:
    """UT11-125: texts equal in every label still differ (the vector index needs diverse rows)."""
    a, b = _encode(
        _en("packet loss", "Rapid Bridge", "web43") + " 1",
        _en("packet loss", "Rapid Bridge", "web43") + " 2",
    )
    assert not np.array_equal(a, b)
    assert _cos(a, b) < 1.0 - 1e-3


def test_ut11_126_no_pickle_and_no_file_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT11-126: `_build_stubs` imports no pickle-family module; encoding opens no file."""
    tree = ast.parse(Path(_build_stubs.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"pickle", "joblib", "cloudpickle", "dill"}

    def _no_open(*args: object, **kwargs: object) -> None:
        msg = "the encoder must not open files"
        raise AssertionError(msg)

    encoder = TopicEncoder()
    monkeypatch.setattr("builtins.open", _no_open)
    assert encoder.encode([_en("packet loss", "Rapid Bridge", "web43")], batch_size=1).shape == (
        1,
        EMBEDDING_DIM,
    )
    assert encoder.model_id.startswith("test/topic@")


def test_ut11_127_repeat_flavored_incidents_form_their_own_region() -> None:
    """UT11-127: a repeat marker moves a text further than a component change does, so a
    planted repeat-flavored blob separates from its family; its variants stay closer to each
    other than to the unmarked texts."""
    topic = FAMILY_ROWS[19][1]
    plain = _en(topic, "Rapid Bridge", "web43")
    again = plain + " This is happening again."
    recurring = plain + " Recurring issue."
    other_comp = _en(topic, "Iron Comet", "web43")
    v = _encode(plain, again, recurring, other_comp)
    assert _cos(v[1], v[2]) > _cos(v[1], v[0]) + 0.05
    assert _cos(v[0], v[1]) < _cos(v[0], v[3])
