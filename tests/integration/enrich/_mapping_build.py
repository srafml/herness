"""Shared setup for the mapping-suggestion flow tests (IT03-14, ST03-12; T03-27).

A real core build (files 000-280 through `build_harness`) over a lake with four ServiceNow
groups and three services: team t1 owns s1 through the CMDB, teams t2 ("Payments") and t3
("Search") have no mapping. The encoder stand-in maps every text to one unit vector, so the
semantic score is 1 for every pair and fuzzy names decide the ranking.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

import numpy as np
from tests.integration.model._core_build import SVC, TEAM, build_core
from tests.integration.model._stg_lake import Row, at, commit
from tests.support.build_harness import BuildHarness

from herness.core.types import QuestionSet
from herness.enrich.embed import Encoder
from herness.enrich.mapping_suggest import prepare_mapping_vectors, run_suggest_stage
from herness.enrich.settings import DecisionsConfig
from herness.store.ops import approved_mapping_suggestions

SN = "servicenow"
QSV = "qs-2026-09-01"
CFG = DecisionsConfig.model_validate(
    {"question_set_version": QSV, "questions": (), "change_link": {"use_decider": False}}
)
MAP_SQL = (
    "SELECT service_id, team_id, jira_project, jira_component, role, link_source "
    "FROM core.service_map ORDER BY ALL"
)


@dataclass
class Report:
    status: Literal["done", "skipped", "degraded", "failed"] = "done"
    rows: int = 0


class ConstantEncoder:
    """Stands in for `Encoder`: every text becomes the same unit vector."""

    def encode(self, texts: Sequence[str], *, batch_size: int) -> np.ndarray:
        rows = np.zeros((len(texts), 1024), dtype=np.float32)
        rows[:, 0] = 1.0
        return rows


def write_lake(harness: BuildHarness) -> None:
    groups = [("root", None, "Root"), ("t1", "root", "Checkout Team"),
              ("t2", "root", "Payments"), ("t3", "root", "Search")]  # fmt: skip
    commit(
        harness.layout.raw, SN, "sys_user_group",
        [Row(g, at(0), {"sys_id": g, "name": n, "parent": p}) for g, p, n in groups],
    )  # fmt: skip
    services = [("s1", "Checkout", "t1"), ("s2", "Payments Gateway", None),
                ("s3", "Search Index", None)]  # fmt: skip
    commit(
        harness.layout.raw, SN, "cmdb_ci_service",
        [Row(k, at(0), {"name": name, "owned_by": own}) for k, name, own in services],
    )  # fmt: skip


def rebuild(harness: BuildHarness) -> list[tuple[object, ...]]:
    """Build 000-280 with the currently approved suggestions; return `core.service_map`."""
    build_core(harness, approved=approved_mapping_suggestions(), hi=280)
    return harness.query(MAP_SQL)


def suggest(harness: BuildHarness) -> Report:
    """Stage `embed`'s vector preparation, then stage `suggest`, on the build file."""
    vectors = prepare_mapping_vectors(
        harness.con,
        encoder=cast("Encoder", ConstantEncoder()),
        qs=QuestionSet(version=QSV, questions=()),
    )
    report = Report()
    run_suggest_stage(harness.con, vectors=vectors, cfg=CFG, report=report)
    return report


__all__ = ["CFG", "MAP_SQL", "SVC", "TEAM", "Report", "rebuild", "suggest", "write_lake"]
