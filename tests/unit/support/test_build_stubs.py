"""Stub lifecycle and wiring of the build fixture (impl 11 U11-48, T11-34).

UT11-48 (cv): `running_stubs` serves on loopback inside the block and has closed every
listener after it, also when the block raises; `stub_overrides` points OpenJev and every
`openai_compat` client at the stubs, with deploy and service ports in step (C08a, C09).
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
import yaml
from tests.support._build_stubs import (
    BUILD_SCRIPTS,
    STUBS,
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
