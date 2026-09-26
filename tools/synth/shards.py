"""Generation shards of the synthetic lake (U11-19, design §5.1.8).

Only the `Shard` and `IncidentTimeIndex` value types live here for now; the U11-19 card
adds `plan_shards`, `run_shard`, `run_all_shards` and builds the index after phase A.
"""

import dataclasses
from collections.abc import Mapping
from datetime import date

import numpy as np
import numpy.typing as npt


@dataclasses.dataclass(frozen=True, slots=True)
class Shard:
    """One `(source, entity, month)` unit of work; `month` is the first day of the month.

    `n_records` background records are numbered from `seq_start`; `index` is the shard's
    position in the plan.
    """

    source: str
    entity: str
    month: date
    n_records: int
    seq_start: int
    index: int


@dataclasses.dataclass(frozen=True, slots=True, eq=False)
class IncidentTimeIndex:
    """Incident open times per service (keyed by the incident's `business_service` sys_id).

    For each service `opened_at[s]` is a sorted `int64` array of `opened_at` epoch seconds
    (UTC) for the shard month +/- 1 day; `sys_ids[s]` and `numbers[s]` hold the matching
    incident sys_ids and numbers in the same order. Data only; U11-19 builds it.
    """

    opened_at: Mapping[str, npt.NDArray[np.int64]]
    sys_ids: Mapping[str, tuple[str, ...]]
    numbers: Mapping[str, tuple[str, ...]]


__all__ = ["IncidentTimeIndex", "Shard"]
