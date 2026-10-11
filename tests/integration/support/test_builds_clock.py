"""Fixture builds are the same on every day (impl 11 U11-48, T11-39).

IT11-33: the enrichment windows (`clustering.window_days`) are clock-relative, so
`ensure_build` pins `herness.core.time.now` to the synth end + 1 day; two builds made while
the wall clock reads two different days are identical.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

import pytest
from tests.support.builds import REPO, BuildHandle, ensure_build

from herness.core import time as clock
from herness.store import warehouse
from herness.store.layout import DataLayout

pytestmark = pytest.mark.integration

_DAY_A = datetime.datetime(2026, 10, 11, 12, 0, tzinfo=datetime.UTC)
_DAY_B = datetime.datetime(2027, 3, 4, 9, 30, tzinfo=datetime.UTC)


def _digest(rows: list[list[object]]) -> tuple[int, str]:
    lines = sorted(json.dumps([str(v) for v in r]) for r in rows)
    return len(lines), hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _digests(handle: BuildHandle) -> dict[str, tuple[int, str]]:
    """Row count and SHA-256 over the sorted rows (every column as text) per table. The only
    column left out is `cluster_id`, a random `new_id()` that legitimately differs between two
    builds; a cluster is identified by the hash of its sorted member record ids instead."""
    layout = DataLayout.from_root(handle.data_root)
    with warehouse.open_readonly(handle.build_id, layout=layout) as con:
        members = con.execute("SELECT * FROM enrich.cluster_member").fetchall()
        clusters = con.execute("SELECT * FROM enrich.cluster").fetchall()
        links = con.execute("SELECT * FROM enrich.incident_change_link").fetchall()
        names = [d[0] for d in con.execute("SELECT * FROM enrich.cluster").description or []]
    by_cluster: dict[str, list[str]] = {}
    for record_id, cluster_id, _prob in members:
        by_cluster.setdefault(str(cluster_id), []).append(str(record_id))
    sig = {
        c: hashlib.sha256("|".join(sorted(r)).encode()).hexdigest() for c, r in by_cluster.items()
    }
    at = names.index("cluster_id")
    return {
        "enrich.cluster": _digest(
            [[sig.get(str(r[at]), "none"), *r[:at], *r[at + 1 :]] for r in clusters]
        ),
        "enrich.cluster_member": _digest([[r, sig[str(c)], p] for r, c, p in members]),
        "enrich.incident_change_link": _digest([list(r) for r in links]),
    }


def test_it11_33_builds_on_two_wall_clock_days_are_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT11-33 the tiny fixture built into two fresh caches while the wall clock reads two
    different days gives identical cluster, member and link rows (counts and digests)."""
    outer: list[datetime.datetime] = []
    built = []
    for day, name in ((_DAY_A, "a"), (_DAY_B, "b")):
        monkeypatch.setattr(clock, "now", lambda day=day: day)
        outer.append(clock.now())
        built.append(ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path / name))
        assert clock.now() == day  # the wall clock (patched here) is back after the build
    assert outer[0] != outer[1]  # the clock really varied
    first, second = (_digests(h) for h in built)
    assert first == second
    assert built[0].build_id[:15] == built[1].build_id[:15]  # the date-time part of the id
    assert first["enrich.cluster"][0] > 0
    assert first["enrich.incident_change_link"][0] > 0
    assert first["enrich.cluster_member"][0] > 0
