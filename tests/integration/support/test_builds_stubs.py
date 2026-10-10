"""Build fixtures on the loopback stubs (impl 11 IT11-11, U11-48, T11-34).

`ensure_build` runs the real enrichment and cluster naming against `StubDeciderServer`
(oracle) and `FakeLLMServer` for the one build it makes: `tiny_build` holds `enrich.decision`
rows answered by the oracle stub, its clusters carry the stub LLM's label, every stub is shut
down after the build (also a failed one), and a reuse starts none.
"""

from __future__ import annotations

import contextlib
import dataclasses
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support import builds
from tests.support._build_stubs import STUBS, StubUrls
from tests.support.builds import REPO, BuildFixtureError, BuildHandle, ensure_build

from herness.core.errors import HernessError
from herness.store import warehouse
from herness.store.layout import DataLayout

pytestmark = pytest.mark.integration

_LABEL = "Synthetic incident cluster"  # tests/fixtures/llm_scripts/build/cluster_naming.yaml


def _query(handle: BuildHandle, sql: str, *params: object) -> list[tuple[object, ...]]:
    layout = DataLayout.from_root(handle.data_root)
    with warehouse.open_readonly(handle.build_id, layout=layout) as con:
        return con.execute(sql, list(params)).fetchall()


def test_it11_11_tiny_build_has_enrich_decision_rows_from_the_oracle_stub(
    tiny_build: BuildHandle,
) -> None:
    """IT11-11 (acceptance) `tiny_build` holds `enrich.decision` rows answered by the oracle
    stub: every row of `openjev` equals the planted truth label of its (content hash,
    question) (`owning_team` carries the entity prefix of the id)."""
    labels = (tiny_build.root / "truth" / "truth_labels.parquet").as_posix()
    rows = _query(tiny_build, "SELECT count(*) FROM enrich.decision WHERE decider = 'openjev'")
    assert rows[0][0] > 1000, rows
    joined = _query(
        tiny_build,
        "SELECT d.question, d.answer, l.answer FROM enrich.decision AS d JOIN read_parquet(?) AS l"
        " ON l.content_hash = d.content_hash AND l.question = d.question"
        " WHERE d.decider = 'openjev'",
        labels,
    )
    assert len(joined) > 1000
    for question, answer, truth in joined:
        expected = f"servicenow:sys_user_group:{truth}" if question == "owning_team" else truth
        assert answer == expected, (question, answer, truth)
    questions = {q for q, _, _ in joined}
    assert {"root_cause", "change_caused", "repeat_issue", "business_impact"} <= questions


def test_it11_11_tiny_build_clusters_are_named_by_the_stub_llm(tiny_build: BuildHandle) -> None:
    """IT11-11 cluster naming reached the stub LLM: clusters carry its scripted label."""
    rows = _query(tiny_build, "SELECT label, root_cause_category FROM enrich.cluster")
    assert rows
    assert {(_LABEL, "unknown")} <= set(rows), rows


def test_it11_11_build_handle_describes_the_stubs(tiny_build: BuildHandle) -> None:
    """IT11-11 `BuildHandle.stubs` is a frozen, stable description (no live server objects)."""
    stubs = tiny_build.stubs
    assert stubs == STUBS
    assert stubs.decider == "oracle"
    assert (tiny_build.root / stubs.truth_labels).is_file()  # type: ignore[operator]
    assert (REPO / stubs.llm_scripts).is_dir()  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        stubs.decider = "hash"  # type: ignore[misc]


def _accepts(url: str) -> bool:
    host, port = url.removeprefix("http://").split(":")
    try:
        with socket.create_connection((host, int(port)), timeout=1.0):
            return True
    except OSError:
        return False


def _serving() -> int:
    return sum(1 for t in threading.enumerate() if t.is_alive() and "serve_forever" in t.name)


@pytest.fixture
def seen_stubs(monkeypatch: pytest.MonkeyPatch) -> list[StubUrls]:
    """Record the URLs of every stub set `ensure_build` starts (and prove they were up)."""
    seen: list[StubUrls] = []
    real = builds.running_stubs

    @contextlib.contextmanager
    def spy(root: Path) -> Iterator[StubUrls]:
        with real(root) as urls:
            assert _accepts(urls.decider)
            assert _accepts(urls.llm)
            seen.append(urls)
            yield urls

    monkeypatch.setattr(builds, "running_stubs", spy)
    return seen


def test_it11_11_stubs_start_with_the_build_and_stop_after_it(
    tmp_path: Path, seen_stubs: list[StubUrls], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT11-11 the stubs serve during the one build and no listener or thread stays after it;
    the second call reuses the build and starts no stub."""
    before = _serving()
    first = ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path)
    assert len(seen_stubs) == 1
    urls = seen_stubs[0]
    assert not any(_accepts(u) for u in (urls.decider, urls.llm, urls.llm_large))
    assert _serving() == before

    def refuse(root: Path) -> Iterator[StubUrls]:
        msg = f"a reused build must not start stubs ({root})"
        raise AssertionError(msg)

    monkeypatch.setattr(builds, "running_stubs", refuse)
    assert ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path) == first


def test_it11_11_stubs_stop_when_the_build_fails(
    tmp_path: Path, seen_stubs: list[StubUrls], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT11-11 a failing build still stops every stub and leaves no serving thread."""

    def boom(job_id: str) -> object:
        msg = f"scripted failure of {job_id}"
        raise HernessError(msg)

    monkeypatch.setattr(builds, "run_inline", boom)
    before = _serving()
    with pytest.raises(BuildFixtureError, match="build failed"):
        ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path)
    assert len(seen_stubs) == 1
    urls = seen_stubs[0]
    assert not any(_accepts(u) for u in (urls.decider, urls.llm, urls.llm_large))
    assert _serving() == before
    assert not list(tmp_path.glob("*.lock"))
