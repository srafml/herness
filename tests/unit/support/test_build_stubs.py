"""Stub lifecycle and wiring of the build fixture (impl 11 U11-48, T11-34).

UT11-48 (cv): `running_stubs` serves on loopback inside the block and has closed every
listener after it, also when the block raises; `stub_overrides` points OpenJev and every
`openai_compat` client at the stubs, with deploy and service ports in step (C08a, C09).
"""

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from tests.support import _build_stubs
from tests.support._build_cache import BuildFixtureError, fingerprint
from tests.support._build_stubs import (
    BUILD_SCRIPTS,
    STUBS,
    LlmFactory,
    StubUrls,
    running_stubs,
    stub_overrides,
)

from herness.eval.scripted import load_scripts

pytestmark = pytest.mark.unit

SHIPPED = Path(__file__).resolve().parents[3] / "config"


def _accepts(url: str) -> bool:
    host, port = url.removeprefix("http://").split(":")
    try:
        with socket.create_connection((host, int(port)), timeout=1.0):
            return True
    except OSError:
        return False


def _urls(root: Path) -> list[str]:
    with running_stubs(root) as urls:
        assert all(url.startswith("http://127.0.0.1:") for url in (urls.decider, urls.llm))
        seen = [urls.decider, urls.llm, urls.llm_large]
        assert all(_accepts(url) for url in seen)
    return seen


def _labels(root: Path) -> Path:
    (root / "truth").mkdir(parents=True)
    path = root / "truth" / "truth_labels.parquet"
    import pyarrow as pa  # noqa: PLC0415 - only this test writes parquet
    import pyarrow.parquet as pq  # noqa: PLC0415

    cols = {"content_hash": ["h"], "question": ["q"], "answer": ["a"]}
    pq.write_table(pa.table(cols), path)
    return path


def test_ut11_48_stubs_close_after_the_block(tmp_path: Path) -> None:
    """UT11-48 every stub listener is closed once `running_stubs` returns."""
    _labels(tmp_path)
    assert not any(_accepts(url) for url in _urls(tmp_path))


def _failing_block(root: Path, seen: list[str]) -> None:
    with running_stubs(root) as urls:
        seen += [urls.decider, urls.llm, urls.llm_large]
        msg = "boom"
        raise RuntimeError(msg)


def test_ut11_48_stubs_close_when_the_block_raises(tmp_path: Path) -> None:
    """UT11-48 a failing build body still stops every stub."""
    _labels(tmp_path)
    seen: list[str] = []
    with pytest.raises(RuntimeError, match="boom"):
        _failing_block(tmp_path, seen)
    assert len(seen) == 3
    assert not any(_accepts(url) for url in seen)


def test_ut11_48_overrides_put_every_client_on_a_stub(tmp_path: Path) -> None:
    """UT11-48 OpenJev and every `openai_compat` client get a stub URL; anthropic none."""
    shipped = yaml.safe_load((SHIPPED / "models.yaml").read_text(encoding="utf-8"))
    clients = shipped["models"]["clients"]
    (tmp_path / "models.yaml").write_text(yaml.safe_dump(shipped), encoding="utf-8")
    urls = StubUrls("http://127.0.0.1:1", "http://127.0.0.1:2", "http://127.0.0.1:3")
    out = stub_overrides(tmp_path, urls)
    assert "models.deciders.openjev.base_url=http://127.0.0.1:1" in out
    local = {k for k, c in clients.items() if c["kind"] == "openai_compat"}
    assert local
    wired = {o.split(".base_url=")[0].split(".")[-1] for o in out if o.startswith("models.models")}
    assert wired == local
    assert not any("claude" in o for o in out)
    large = [k for k in local if clients[k]["gpu_class"] == "large"]
    assert large
    assert all(f"models.models.clients.{k}.base_url=http://127.0.0.1:3/v1" in out for k in large)
    assert "deploy.reasoning.port=2" in out
    assert "deploy.large.port=3" in out


def test_ut11_48_build_script_book_loads_and_stubs_describe_it() -> None:
    """UT11-48 the build script book parses; `STUBS` names it relative to the repo."""
    assert load_scripts(BUILD_SCRIPTS)
    assert (BUILD_SCRIPTS.parents[3] / STUBS.llm_scripts).resolve() == BUILD_SCRIPTS.resolve()
    assert STUBS.decider == "oracle"


class _Cfg:
    def __init__(self, base_url: str | None, kind: str = "openai_compat") -> None:
        self.base_url, self.kind, self.model, self.max_concurrency = base_url, kind, "m", 2


class _Client:
    closed = 0

    def close(self) -> None:
        type(self).closed += 1


class _Registry:
    cfg: _Cfg

    def __init__(self, *_a: object, **_k: object) -> None:
        pass

    def model_for(self, role: str, depth: str) -> str:
        return "k"

    def config(self, key: str) -> _Cfg:
        return self.cfg

    def client(self, key: str) -> _Client:
        return _Client()


def _root_config() -> object:
    return SimpleNamespace(models=None)


def _factory(monkeypatch: pytest.MonkeyPatch, cfg: _Cfg) -> LlmFactory:
    _Registry.cfg = cfg
    monkeypatch.setattr(_build_stubs, "LLMRegistry", _Registry)
    monkeypatch.setattr(_build_stubs, "get_config", _root_config)
    return LlmFactory()


@pytest.mark.parametrize(
    "cfg",
    [
        _Cfg("https://api.example.com/v1"),
        _Cfg("http://10.0.0.5:8000/v1"),
        _Cfg(None, kind="anthropic"),
        _Cfg("http://127.0.0.1:1/v1", kind="anthropic"),
    ],
)
def test_ut11_48_llm_factory_refuses_a_client_off_loopback(
    monkeypatch: pytest.MonkeyPatch, cfg: _Cfg
) -> None:
    """UT11-48 the waived registry guard is replaced by a loopback openai_compat check."""
    with pytest.raises(BuildFixtureError, match="loopback"):
        _factory(monkeypatch, cfg)("cluster_namer")


def test_ut11_48_llm_factory_serves_loopback_and_closes_its_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT11-48 a loopback client is handed out, `close` closes it once and is idempotent."""
    factory = _factory(monkeypatch, _Cfg("http://127.0.0.1:9/v1"))
    client, model, conc = factory("cluster_namer")
    assert (model, conc) == ("m", 2)
    _Client.closed = 0
    factory.close()
    factory.close()
    assert _Client.closed == 1
    assert isinstance(client, _Client)


def test_ut11_48_script_book_is_part_of_the_fingerprint(tmp_path: Path) -> None:
    """UT11-48 changing the stub LLM's script book changes the build fingerprint."""
    book = tmp_path / "tests" / "fixtures" / "llm_scripts" / "build"
    book.mkdir(parents=True)
    (tmp_path / "config" / "profiles").mkdir(parents=True)
    (tmp_path / "config" / "profiles" / "synth.yaml").write_text("version: 1", encoding="utf-8")
    (book / "a.yaml").write_text("one", encoding="utf-8")
    first = fingerprint(tmp_path, "sha256:p")
    (book / "a.yaml").write_text("two", encoding="utf-8")
    assert fingerprint(tmp_path, "sha256:p") != first
