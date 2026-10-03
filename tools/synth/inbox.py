"""`service_costs.csv` inbox drop for the files connector (U11-21, spec 01 §5.10).

One row per catalog service, in catalog order, with the run and downtime costs the catalog
drew (`ServiceRow`: run cost log-normal around 250,000; downtime cost per hour by
criticality x U(0.8, 1.2)); money is written with 2 decimals. The file lands under
`<root>/data/inbox/service_costs/`, written tmp-then-`os.replace` (TH11-07: the path is
resolved and must stay under `root`).
"""

import csv
import io
import os
from pathlib import Path
from typing import Final

from tools.synth.catalog_rows import Catalog
from tools.synth.shards import require_under

HEADER: Final = ("service_name", "cost_center", "annual_run_cost_usd", "downtime_cost_per_hour_usd")


def write_service_costs(root: Path, cat: Catalog) -> Path:
    """Write `<root>/data/inbox/service_costs/service_costs.csv` and return its path."""
    path = require_under(root, root / "data" / "inbox" / "service_costs" / "service_costs.csv")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(HEADER)
    for s in cat.services:
        run, down = f"{s.annual_run_cost_usd:.2f}", f"{s.downtime_cost_per_hour_usd:.2f}"
        writer.writerow((s.name, s.cost_center, run, down))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(out.getvalue(), encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


__all__ = ["HEADER", "write_service_costs"]
