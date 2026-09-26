"""ST03-06, ST03-10, ST03-13 (TH03-04, TH03-08, TH03-11): laya_admin threat tests (T03-33)."""

from __future__ import annotations

import getpass
import json
from pathlib import Path

import pytest
from tests.support.fake_laya import write_laya_version
from tests.unit.enrich import _laya_admin_fixtures as fx

from herness.core.config import HernessConfig
from herness.core.errors import ConfigError
from herness.enrich import laya_admin
from herness.enrich.labels import gold_digest
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import EmbeddingSettings

pytestmark = pytest.mark.unit


@pytest.fixture
def cfg(tmp_path: Path) -> HernessConfig:
    return fx.build_cfg(tmp_path)


@pytest.fixture
def paths(cfg: HernessConfig) -> EnrichPaths:
    return EnrichPaths.from_config(cfg)


# --- ST03-06 (TH03-04) -------------------------------------------------------------------------


def test_st03_06_unproposed_question_refused(paths: EnrichPaths) -> None:
    """ST03-06 accept_model for a question with accepted_proposed = false is refused."""
    store = fx.gold_store(paths)
    digest = gold_digest(store.read("gold"))
    write_laya_version(paths.laya_root(), fx.V1, status="candidate")
    fx.write_eval(
        paths, fx.V1, gold_sha256=digest, questions={"root_cause": {"accepted_proposed": False}}
    )

    with pytest.raises(ConfigError, match="not accepted_proposed") as info:
        laya_admin.accept_model(fx.V1, ["root_cause"])
    assert info.value.details["refused"] == "root_cause"


def test_st03_06_stale_gold_sha256_refused(paths: EnrichPaths) -> None:
    """ST03-06 an eval.json whose gold_sha256 no longer matches frozen gold is refused."""
    fx.gold_store(paths)  # gold on disk; eval.json below carries a digest that does not match
    write_laya_version(paths.laya_root(), fx.V1, status="candidate")
    fx.write_eval(
        paths, fx.V1, gold_sha256="f" * 64,
        questions={"root_cause": {"accepted_proposed": True}},
    )  # fmt: skip

    with pytest.raises(ConfigError, match="stale eval"):
        laya_admin.accept_model(fx.V1, ["root_cause"])


def test_st03_06_stale_question_set_version_refused(paths: EnrichPaths) -> None:
    """ST03-06 an eval.json question_set_version older than the manifest's/active one is stale."""
    store = fx.gold_store(paths)
    digest = gold_digest(store.read("gold"))
    write_laya_version(paths.laya_root(), fx.V1, status="candidate")
    fx.write_eval(
        paths, fx.V1, gold_sha256=digest, question_set_version="qs-2025-01-01.1",
        questions={"root_cause": {"accepted_proposed": True}},
    )  # fmt: skip

    with pytest.raises(ConfigError, match="stale eval"):
        laya_admin.accept_model(fx.V1, ["root_cause"])


# --- ST03-10 (TH03-08) -------------------------------------------------------------------------


def test_st03_10_accept_version_traversal_rejected(cfg: HernessConfig) -> None:
    """ST03-10 `laya accept ../../etc`: the traversing version never reaches the filesystem."""
    with pytest.raises(ConfigError):
        laya_admin.accept_model("../../etc")


def test_st03_10_embedding_path_traversal_rejected(tmp_path: Path) -> None:
    """ST03-10 `embedding.path: ../../x`: the existing EnrichPaths containment check refuses it
    (laya_admin never resolves the embedding path itself; this pins the shared guard)."""
    embedding = EmbeddingSettings(path="../../x").path
    paths = EnrichPaths(
        data_root=tmp_path.resolve(), embedding_path=embedding, laya_current_file="c"
    )
    with pytest.raises(ConfigError, match="path outside data root"):
        paths.embedding_model_dir()


# --- ST03-13 (TH03-11) -------------------------------------------------------------------------


def test_st03_13_accept_writes_manifest_and_real_audit_line(
    paths: EnrichPaths, cfg: HernessConfig
) -> None:
    """ST03-13 accept a model: manifest.accepted_by and a real chained admin_action line."""
    store = fx.gold_store(paths)
    digest = gold_digest(store.read("gold"))
    write_laya_version(paths.laya_root(), fx.V1, status="candidate")
    fx.write_eval(
        paths, fx.V1, gold_sha256=digest, questions={"root_cause": {"accepted_proposed": True}}
    )

    laya_admin.accept_model(fx.V1, actor="system")

    manifest = json.loads((paths.laya_dir(fx.V1) / "manifest.json").read_text("utf-8"))
    assert manifest["accepted_by"] == f"os:{getpass.getuser()}"

    logs_dir = Path(cfg.paths.logs)
    records = [
        json.loads(line)
        for log_file in sorted(logs_dir.glob("audit-*.jsonl"))
        for line in log_file.read_text("utf-8").splitlines()
    ]
    matches = [
        r
        for r in records
        if r["event"] == "admin_action" and r["fields"]["action"] == "laya_accept"
    ]
    assert len(matches) == 1
    assert matches[0]["fields"]["target"] == fx.V1
    assert matches[0]["fields"]["detail"] == ["root_cause"]
    assert matches[0]["actor"] == "system"
