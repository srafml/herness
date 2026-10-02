"""Tests for herness.enrich.health (U03-146; T03-34): UT03-135, and the facade (CV T03-34)."""

from __future__ import annotations

import importlib
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config

from herness.core import config
from herness.core.errors import ConfigError
from herness.enrich.calibrate import CalibrationResult, CalibrationStore
from herness.enrich.layout import EnrichPaths

pytestmark = pytest.mark.unit

# The package facade keeps `herness.enrich.health` the function (U03-146), so the module is
# reached through importlib: `from herness.enrich import health` yields the function.
health_module = importlib.import_module("herness.enrich.health")

VERSION = "laya-20261001-1"
QSV = "qs-2026-10-01.1"
RESULT = {"q_a": CalibrationResult(1.2, 0.05, 0.1, 0.9, 120, uncalibrated=False)}
CODES = {"config_invalid", "cache_not_writable", "laya_degraded", "embedding_model_missing",
         "calibration_missing", "ok"}  # fmt: skip


class Verifier:
    """Stands in for `verify_model_dir`: counts calls, fails on demand."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def __call__(self, paths: EnrichPaths, version: str) -> None:
        self.calls += 1
        if self.fail:
            msg = "hash mismatch"
            raise ConfigError(msg)


@pytest.fixture
def paths(tmp_path: Path) -> EnrichPaths:
    config.reset_config()
    overrides = ["decisions.embedding.path=data/models/bge-m3"]
    config.init_config("local", overrides, config_dir=write_full_config(tmp_path), env={})
    return EnrichPaths.from_config(config.get_config())


@pytest.fixture
def verifier(monkeypatch: pytest.MonkeyPatch) -> Verifier:
    fake = Verifier()
    monkeypatch.setattr(health_module, "verify_model_dir", fake)
    health_module._MEMO.clear()
    return fake


def _healthy(paths: EnrichPaths) -> None:
    """CURRENT, the Laya and embedding directories and both calibration files in use."""
    paths.laya_dir(VERSION).mkdir(parents=True)
    (paths.laya_dir(VERSION) / "model.safetensors").write_bytes(b"w")
    paths.laya_current().write_text(VERSION + "\n", encoding="ascii")
    paths.embedding_model_dir().mkdir(parents=True)
    store = CalibrationStore(paths)
    store.save("laya", VERSION, QSV, RESULT)
    store.save("openjev", "openjev-latest", QSV, RESULT)


def test_ut03_135_missing_current_is_degraded(paths: EnrichPaths, verifier: Verifier) -> None:
    """UT03-135 missing CURRENT -> degraded / laya_degraded."""
    _healthy(paths)
    paths.laya_current().unlink()
    assert health_module.health() == ("degraded", "laya_degraded")


def test_ut03_135_unwritable_cache_root_is_down(paths: EnrichPaths, verifier: Verifier) -> None:
    """UT03-135 a cache root that cannot take a file -> down / cache_not_writable."""
    _healthy(paths)
    (paths.data_root / "cache").write_bytes(b"not a directory")
    assert health_module.health() == ("down", "cache_not_writable")


def test_ut03_135_probe_refused_is_down(
    paths: EnrichPaths, verifier: Verifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-135 the probe file cannot be created (permission) -> down / cache_not_writable."""
    _healthy(paths)

    def refuse(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(health_module.tempfile, "mkstemp", refuse)
    assert health_module.health() == ("down", "cache_not_writable")


def test_ut03_135_ok_and_probe_removed(paths: EnrichPaths, verifier: Verifier) -> None:
    """UT03-135 everything present -> ok; the probe file is gone; nothing else is written."""
    _healthy(paths)
    cache_root = paths.data_root / "cache"
    cache_root.mkdir()
    before = sorted(p.relative_to(paths.data_root) for p in paths.data_root.rglob("*"))
    assert health_module.health() == ("ok", "ok")
    after = sorted(p.relative_to(paths.data_root) for p in paths.data_root.rglob("*"))
    assert after == before


def test_ut03_135_missing_cache_root_probes_the_data_root(
    paths: EnrichPaths, verifier: Verifier
) -> None:
    """UT03-135 no cache root yet (fresh install): the data root is probed; not created."""
    _healthy(paths)
    assert health_module.health() == ("ok", "ok")
    assert not (paths.data_root / "cache").exists()


def test_ut03_135_degraded_reasons(paths: EnrichPaths, verifier: Verifier) -> None:
    """UT03-135 verify failure, missing embedding dir, missing calibration -> degraded codes."""
    _healthy(paths)
    verifier.fail = True
    health_module._MEMO.clear()
    assert health_module.health() == ("degraded", "laya_degraded")
    verifier.fail = False
    health_module._MEMO.clear()
    paths.embedding_model_dir().rmdir()
    assert health_module.health() == ("degraded", "embedding_model_missing")
    paths.embedding_model_dir().mkdir()
    target = paths.calibration_file("openjev", "openjev-latest", QSV)
    target.unlink()
    assert health_module.health() == ("degraded", "calibration_missing")


def test_ut03_135_config_invalid_is_down(
    verifier: Verifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-135 an invalid config (DecisionsConfig fails) -> down / config_invalid."""

    def broken() -> None:
        msg = "decisions.yaml invalid"
        raise ConfigError(msg)

    monkeypatch.setattr(health_module, "get_config", broken)
    assert health_module.health() == ("down", "config_invalid")


def test_ut03_135_hash_check_memoized(paths: EnrichPaths, verifier: Verifier) -> None:
    """UT03-135 the Laya hash check runs once for an unchanged version; thread-safe."""
    _healthy(paths)
    results: list[tuple[str, str]] = []
    threads = [
        threading.Thread(target=lambda: results.append(health_module.health())) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [("ok", "ok")] * 8
    assert verifier.calls == 1
    assert {reason for _, reason in results} <= CODES


def test_ut03_135_unexpected_errors_never_raise(
    paths: EnrichPaths, verifier: Verifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-135 an unexpected exception inside a check maps to that check's code."""
    _healthy(paths)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(str(paths.data_root))

    monkeypatch.setattr(health_module.CalibrationStore, "load", explode)
    assert health_module.health() == ("degraded", "calibration_missing")


def test_cv_t03_34_facade_exports_are_lazy() -> None:
    """CV T03-34 importing herness.enrich loads neither purge, health nor LanceDB."""
    code = (
        "import sys, herness.enrich as e; "
        "print(sorted(m for m in ('herness.enrich.purge', 'herness.enrich.health', 'lancedb', "
        "'herness.enrich.embed') if m in sys.modules)); print(sorted(e.__all__))"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    ).stdout.splitlines()
    assert out == ["[]", "['embed_query', 'health', 'purge_record']"]


def test_cv_t03_34_facade_names_the_functions() -> None:
    """CV T03-34 `herness.enrich.health` stays the function after its submodule is imported."""
    from herness import enrich  # noqa: PLC0415 - checks the facade itself
    from herness.enrich.embed import embed_query  # noqa: PLC0415
    from herness.enrich.purge import purge_record  # noqa: PLC0415

    assert enrich.health is health_module.health
    assert enrich.purge_record is purge_record
    assert enrich.embed_query is embed_query
    with pytest.raises(AttributeError):
        _ = enrich.nothing_here  # type: ignore[attr-defined]
