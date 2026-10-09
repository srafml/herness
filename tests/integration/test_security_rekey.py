"""Integration test for the rekey flow F10-10 (impl 10 IT10-07; design 10 §5.3 steps 2-4).

The ``maintenance`` job ``{"action": "rekey"}`` runs on a planted ``CURRENT`` warehouse and
human, gold (and gold review) and teacher label parts; every label ends on its new hash.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pyarrow.parquet as pq
import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.support.rekey_env import (
    NEW_ID,
    NEW_KEY,
    QSV,
    FakeCtx,
    RekeyEnv,
    open_rekey_env,
    write_labels,
)

from herness.admin.maintenance import handle_maintenance
from herness.core.jobs.ports import JobContext
from herness.enrich.labels import LabelStore
from herness.enrich.layout import EnrichPaths

pytestmark = pytest.mark.integration

KINDS = ("human", "gold", "gold/_reviews", "teacher")
READ_KINDS = {
    "human": "human",
    "gold": "gold",
    "gold/_reviews": "gold_reviews",
    "teacher": "teacher",
}


@pytest.fixture
def rekey_env(tmp_path: Path, fake_keyring: MemoryKeyring) -> Iterator[RekeyEnv]:
    """The planted rekey environment of ``tests.support.rekey_env``."""
    with open_rekey_env(tmp_path, fake_keyring) as env:
        yield env


def test_it10_07_every_label_mapped_to_its_new_hash(rekey_env: RekeyEnv) -> None:
    """IT10-07 human, gold and teacher labels: every label mapped to its new hash; no loss."""
    env = rekey_env
    ids = ["sn:incident:INC1", "sn:change:CHG1", "sn:problem:PRB1"]
    for n, kind in enumerate(KINDS):
        write_labels(env, kind, f"{n}a", [(rid, env.old[rid]) for rid in ids])
        other = ids[n % len(ids)]
        write_labels(env, kind, f"{n}b", [(other, env.old[other])])
    before = {kind: len(env.label_rows(kind)) for kind in KINDS}
    outcome = handle_maintenance(cast("JobContext", FakeCtx()), memory_purge=lambda _r: 0)
    assert outcome.status == "done"
    total = sum(before.values())
    assert outcome.result["counts"] == {"rekeyed": total, "kept_old": 0, "ambiguous": 0}
    store = LabelStore(EnrichPaths.from_config(env.cfg), QSV)
    for kind in KINDS:
        rows = env.label_rows(kind)
        assert len(rows) == before[kind]  # no loss
        assert all(r["content_hash"] == env.new[str(r["record_id"])] for r in rows)
        read = store.read(READ_KINDS[kind], columns=["content_hash"])  # type: ignore[arg-type]
        assert sorted(read["content_hash"].to_pylist()) == sorted(r["content_hash"] for r in rows)
    assert pq.read_table(env.map_path).num_rows == len(env.old)
    assert env.key("redact.hmac_key") == NEW_KEY
    assert env.key("redact.hmac_key.next") is None
    assert NEW_ID in env.marker.read_text("utf-8")
