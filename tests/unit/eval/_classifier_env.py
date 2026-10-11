"""Fixture model directory of the classifier eval tests (T11-35: IT11-25, IT11-26, ST11-04).

A tmp data root (the `ops_store` one) holding impl 03's hand-built evaluation fixture
(`tests/unit/enrich/_evaluate_fixtures`: frozen gold for `root_cause` and `change_caused`,
Laya and OpenJev cache parts), a fake Laya version directory (`tests/support/fake_laya`) whose
`eval.json` and `calibration.json` are written by impl 03's own `evaluate_candidate`, so the
cross-check is genuine, and `CURRENT` pointing at it. The loaded config is a full config tree
whose `decisions.yaml` is the fixture's question set. The warehouse is the placeholder build
of `_runner_env.make_root` (`real` by default, so the synthetic step is skipped).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_laya import write_laya_version
from tests.support.ops_store import OpsStoreHandle
from tests.unit.enrich import _evaluate_fixtures as fx
from tests.unit.eval._runner_env import FakeCtx, make_deps, make_root, settings

from herness.core import config as herness_config
from herness.enrich.evaluate import evaluate_candidate
from herness.enrich.layout import EnrichPaths
from herness.eval import runner
from herness.eval.classifier import ClassifierReport, run_classifier

__all__ = ["ClassifierEnv", "classifier_env", "set_eval_metric"]

VERSION = fx.VERSION
QSV = fx.QSV


@dataclass
class ClassifierEnv:
    """The fixture data root, its enrichment paths and the placeholder warehouse."""

    data_root: Path
    paths: EnrichPaths
    con: duckdb.DuckDBPyConnection
    fixture: fx.Fixture

    @property
    def laya_dir(self) -> Path:
        return self.paths.laya_dir(VERSION)

    def eval_doc(self) -> dict[str, Any]:
        return json.loads((self.laya_dir / "eval.json").read_text(encoding="utf-8"))

    def write_eval(self, doc: dict[str, Any]) -> None:
        (self.laya_dir / "eval.json").write_text(json.dumps(doc), encoding="utf-8")

    def run(self, **kwargs: Any) -> ClassifierReport:
        """`run_classifier` on the fixture (no warehouse, no truth unless given)."""
        args: dict[str, Any] = {"data_root": self.data_root, "warehouse": None,
                                "truth_dir": None, "settings": settings().classifier}  # fmt: skip
        return run_classifier(QSV, kwargs.pop("deciders", ["laya", "openjev", "human"]),
                              **(args | kwargs))  # fmt: skip

    def handle(self, **payload: Any) -> tuple[FakeCtx, Any]:
        """`handle_eval` in-process for `--suite classifier` (the `--inline` path, R-45)."""
        ctx = FakeCtx({"suite": "classifier", "profile": "local", "repeat": 1} | payload)
        runner.set_deps_factory(lambda c: make_deps(self.data_root, self.con, job=c))
        return ctx, runner.handle_eval(ctx)  # type: ignore[arg-type]

    def out_dir(self, run_id: object) -> Path:
        return self.data_root / "reports" / "eval" / str(run_id)


def set_eval_metric(env: ClassifierEnv, qid: str, metric: str, delta: float) -> float:
    """Change one stored `eval.json` metric by `delta`; returns the new value."""
    doc = env.eval_doc()
    doc["questions"][qid]["laya"][metric] += delta
    env.write_eval(doc)
    return float(doc["questions"][qid]["laya"][metric])


def load_fixture_config(tmp_path: Path) -> None:
    """A full config tree whose question set is the fixture's (fingerprints match)."""
    cfg = write_full_config(tmp_path / "cfg_root")
    (cfg / "decisions.yaml").write_text("version: 1\n" + fx.DECISIONS_YAML, encoding="utf-8")
    herness_config.reset_config()
    herness_config.init_config(None, (), config_dir=cfg)


def build_model_dir(paths: EnrichPaths, f: fx.Fixture, *, accepted: list[str]) -> None:
    """Fake Laya version + `eval.json` and calibration from `evaluate_candidate`, `CURRENT`."""
    directory = write_laya_version(paths.laya_root(), VERSION)
    (directory / "calibration.json").unlink()  # evaluate_candidate writes the real one
    evaluate_candidate(version=VERSION, qs=f.qs, cfg=f.cfg, store=f.store, cache=f.cache,
                       calibration=f.calibration, teacher=fx.TEACHER, paths=paths,
                       now=fx.NOW)  # fmt: skip
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    manifest |= {"teacher_version": fx.TEACHER[1], "accepted_questions": accepted}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (paths.laya_root() / "CURRENT").write_text(VERSION + "\n", encoding="ascii")


@pytest.fixture
def classifier_env(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ClassifierEnv:
    """The IT11-25 fixture model dir under the `ops_store` data root (`tmp_path/data`)."""
    load_fixture_config(tmp_path)
    data_root = ops_store.data_root
    f = fx.build(tmp_path)  # writes under tmp_path/data == data_root
    paths = EnrichPaths(data_root=data_root, embedding_path="models/e",
                        laya_current_file="data/models/laya/CURRENT")  # fmt: skip
    build_model_dir(paths, f, accepted=["root_cause"])
    con = make_root(data_root, kind="real")
    monkeypatch.setattr(runner._Binding, "factory", None)
    return ClassifierEnv(data_root, paths, con, f)
