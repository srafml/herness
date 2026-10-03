"""Tests for tools.synth.inbox (U11-21): UT11-25."""

import csv
import re
from datetime import date
from pathlib import Path

import pytest

from tools.synth.catalog import Catalog, build_catalog
from tools.synth.inbox import write_service_costs
from tools.synth.params import load_params

pytestmark = pytest.mark.unit

_HEADER = "service_name,cost_center,annual_run_cost_usd,downtime_cost_per_hour_usd"
_MONEY = re.compile(r"^\d+\.\d{2}$")
_TINY_SERVICES = 20


@pytest.fixture(scope="module")
def cat() -> Catalog:
    params = load_params(
        "tiny",
        start=date(2026, 6, 1),
        end=date(2026, 8, 31),
        sources=("servicenow", "files"),
        dirty="none",
        fetch_mode="initial",
        params_file=None,
    )
    return build_catalog(7, params)


def test_ut11_25_service_costs_csv(tmp_path: Path, cat: Catalog) -> None:
    """UT11-25 header as designed; 20 rows in catalog order; 2-decimal money."""
    path = write_service_costs(tmp_path, cat)
    assert path == (tmp_path / "data/inbox/service_costs/service_costs.csv").resolve()
    lines = path.read_text("utf-8").splitlines()
    assert lines[0] == _HEADER
    rows = list(csv.DictReader(lines))
    assert len(rows) == _TINY_SERVICES == len(cat.services)
    assert [r["service_name"] for r in rows] == [s.name for s in cat.services]
    for row, service in zip(rows, cat.services, strict=True):
        assert row["cost_center"] == service.cost_center
        for field in ("annual_run_cost_usd", "downtime_cost_per_hour_usd"):
            assert _MONEY.fullmatch(row[field])
        assert float(row["annual_run_cost_usd"]) == pytest.approx(service.annual_run_cost_usd)
        down = float(row["downtime_cost_per_hour_usd"])
        assert down == pytest.approx(service.downtime_cost_per_hour_usd)
    assert [p.name for p in path.parent.iterdir()] == ["service_costs.csv"]  # no temp left


def test_ut11_25_service_costs_rewrite_replaces(tmp_path: Path, cat: Catalog) -> None:
    """UT11-25 a second write replaces the file in place (tmp then `os.replace`)."""
    first = write_service_costs(tmp_path, cat).read_bytes()
    assert write_service_costs(tmp_path, cat).read_bytes() == first
