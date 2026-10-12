"""A deterministic synthetic ticket corpus for the model rows (T03-36; BT03-02 ... BT03-05).

The model rows measure a service's throughput on ticket-sized texts; they need neither the lake
nor a database. `ticket_texts(n)` returns `n` distinct texts of about 300-700 characters (a
short description, a blank line and a body of two to five sentences) built from fixed word
lists with `random.Random(seed)`, so every run and every host sees the same corpus. The texts
contain no personal data.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from herness.core.types import DecisionInput
from herness.enrich.text import content_hash

__all__ = ["decision_items", "ticket_texts"]

_SYSTEMS = (
    "payroll",
    "email gateway",
    "vpn",
    "billing api",
    "warehouse database",
    "badge reader",
    "ledger service",
    "intranet portal",
    "backup job",
    "print server",
    "sso login",
)
_SYMPTOMS = (
    "fails with a timeout",
    "returns error 500",
    "is very slow since the morning",
    "does not start after the update",
    "loses the session every few minutes",
    "shows stale data",
    "rejects valid credentials",
    "runs out of disk space",
    "crashes when the nightly batch starts",
    "drops connections under load",
)
_DETAILS = (
    "Users in three offices report the same behaviour.",
    "It started after the last change window.",
    "No alert fired, the first call came from the service desk.",
    "A restart helped for about an hour.",
    "The error log shows repeated connection resets.",
    "Two teams were paged and no owner answered.",
    "Customers outside the region are not affected.",
    "The workaround is to retry later in the day.",
    "Capacity was last reviewed two quarters ago.",
    "The same ticket was closed twice last month.",
)


def ticket_texts(n: int, *, seed: int = 42) -> list[str]:
    """`n` distinct deterministic ticket texts."""
    rng = random.Random(seed)
    out: list[str] = []
    for i in range(n):
        system, symptom = rng.choice(_SYSTEMS), rng.choice(_SYMPTOMS)
        body = " ".join(rng.sample(_DETAILS, rng.randint(2, 5)))
        out.append(f"Ticket {i}: {system} {symptom}\n\nThe {system} {symptom}. {body}")
    return out


def decision_items(texts: Sequence[str]) -> list[DecisionInput]:
    """One `DecisionInput` per text (incident entity, content hash of the text)."""
    return [
        DecisionInput(
            record_id=f"INC{i:07d}", entity="incident", content_hash=content_hash(text), text=text
        )
        for i, text in enumerate(texts)
    ]
