"""Generation shards of the synthetic lake (U11-19, design §5.1.8).

Only the `Shard` value type lives here for now; the U11-19 card adds `plan_shards`,
`run_shard` and `run_all_shards` later.
"""

import dataclasses
from datetime import date


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


__all__ = ["Shard"]
