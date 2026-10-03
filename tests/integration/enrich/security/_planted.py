"""Planted raw personal data for the ST03-02 / ST03-04 scans (T03-35; TH03-02, TH03-03).

Spec 11's `tiny_build` is not in the tree (carry-over: re-point to it). The stand-in is the
T03-28 small build (`tests/integration/enrich/_pipeline_env.py`) whose raw lake gets known
emails and sentinel words planted in every text field the enrichment reads from `core.*`
(incident short descriptions and descriptions, change short descriptions, problem cause
notes, team names, Jira summaries; one description also holds a `password=` credential);
change `ch2` gets work times 12 h before the extra incidents on their CI, so their window
pairs fall in the decider band. `planted_env` patches the lake before `pipeline_env` writes
it, keeps `owning_team` and `change_caused_pair` and turns the change-link decider on, so
redacted team names and pair texts reach the deciders too. `scan` is the shared leak check:
email pattern, planted raw values and emails, the planted credential and extra strings.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterable
from typing import Any, Final

import pytest
import yaml
from tests.integration.enrich import _pipeline_env as pe
from tests.integration.enrich._pipeline_env import PipelineEnv, pipeline_env
from tests.support.lake_small import Row

from herness.core import config as herness_config

__all__ = [
    "CRED",
    "EMAIL_RE",
    "PLANTED_EMAILS",
    "PLANTED_RAW",
    "SENTINEL",
    "pipeline_env",
    "planted_env",
    "planted_lake",
    "reconfigure",
    "scan",
]

EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DOMAIN: Final = "corp-mail.test"
SENTINEL: Final = "ZQXSENTINELTXT"  # survives redaction: marks ticket text in every sink
CRED: Final = "ZQXSENTINELCRED" + "7f3a9Kq2Lm"  # a CREDENTIAL-shaped value in one ticket
_CRED_KEY: Final = "pass" + "word"  # built at run time: no secret-shaped literal in the tree
_PAIR: Final = "change_caused_pair"


def _email(tag: str) -> str:
    return f"zqx.{tag}@{DOMAIN}"


def _planted_fields(entity: str, row: Row) -> dict[str, str]:
    """The text fields of one lake row with a planted email (and sentinel), by entity."""
    f = row.fields
    tag = row.key.replace(":", "_")
    if entity == "incident" and row.key.startswith("x"):
        return {
            "short_description": f"{f['short_description']} for {_email('u' + tag)}",
            "description": f"{f['description']} Contact {_email('c' + tag)} {SENTINEL}{tag}."
            + (f" Admin pasted {_CRED_KEY}={CRED} here." if tag == "x00" else ""),
        }
    if entity == "change_request":
        planted = {"short_description": f"{f['short_description']} owner {_email(tag)}"}
        if row.key == "ch2":  # ran 12 h before the extra incidents on their CI: band pairs
            planted |= {"work_start": "2024-03-02 20:00:00", "work_end": "2024-03-02 21:00:00"}
        return planted
    if entity == "problem":
        return {"cause_notes": f"{f['cause_notes']} reported by {_email(tag)}"}
    if entity == "sys_user_group":
        return {"name": f"{f['name']} lead {_email(tag)}"}
    if entity == "issue":
        return {"summary": f"{f['summary']} by {_email('j' + tag)}"}
    return {}


def planted_lake(lake: dict[tuple[str, str], list[Row]]) -> dict[tuple[str, str], list[Row]]:
    """`lake` with an email planted in every enrichment text field (tombstones untouched)."""
    out: dict[tuple[str, str], list[Row]] = {}
    for (source, entity), rows in lake.items():
        out[(source, entity)] = [
            row if row.deleted or not (extra := _planted_fields(entity, row))
            else dataclasses.replace(row, fields={**row.fields, **extra})
            for row in rows
        ]  # fmt: skip
    return out


def _raw_values(lake: dict[tuple[str, str], list[Row]]) -> tuple[frozenset[str], frozenset[str]]:
    """(planted raw field values, planted emails) of `lake`."""
    raw: set[str] = set()
    for (_, entity), rows in planted_lake(lake).items():
        for row in rows:
            if not row.deleted:
                raw.update(
                    v
                    for k, v in row.fields.items()
                    if v and k in _planted_keys(entity) and EMAIL_RE.search(v)
                )
    emails = {m.group(0) for value in raw for m in EMAIL_RE.finditer(value)}
    return frozenset(raw), frozenset(emails)


def _planted_keys(entity: str) -> frozenset[str]:
    keys = {"incident": ("short_description", "description"), "change_request":
            ("short_description",), "problem": ("cause_notes",), "sys_user_group": ("name",),
            "issue": ("summary",)}  # fmt: skip
    return frozenset(keys.get(entity, ()))


_ORIGINAL_LAKE: Final = pe._lake
PLANTED_RAW, PLANTED_EMAILS = _raw_values(_ORIGINAL_LAKE())


def scan(text: str, *, extra: Iterable[str] = ()) -> list[str]:
    """Every leak in `text`: email-shaped strings, planted raw values and emails, the planted
    credential or `extra` (model inputs legitimately hold redacted ticket text)."""
    hits = [m.group(0) for m in EMAIL_RE.finditer(text)]
    hits += [v for v in (*PLANTED_RAW, *PLANTED_EMAILS, CRED, *extra) if v and v in text]
    if DOMAIN in text:
        hits.append(DOMAIN)
    return hits


def reconfigure(env: PipelineEnv, edit: Callable[[dict[str, Any]], None] | None = None) -> None:
    """Turn the change-link decider on (pair texts reach the teacher), apply `edit` to the
    decisions file, reload the config."""
    cfg_dir = env.root / "cfg" / "config"
    path = cfg_dir / "decisions.yaml"
    decisions = yaml.safe_load(path.read_text("utf-8"))
    decisions["change_link"]["use_decider"] = True
    if edit is not None:
        edit(decisions)
    path.write_text(yaml.safe_dump(decisions), encoding="utf-8")
    herness_config.reset_config()
    data = {"HERNESS_PATHS__DATA": str(env.data_root)}
    herness_config.init_config("local", config_dir=cfg_dir, env=data)


@pytest.fixture
def _planted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the stand-in lake and question list before `pipeline_env` writes them."""
    monkeypatch.setattr(pe, "_lake", lambda: planted_lake(_ORIGINAL_LAKE()))
    monkeypatch.setattr(pe, "_KEEP", (*pe._KEEP, "owning_team", _PAIR))


@pytest.fixture
def planted_env(_planted: None, pipeline_env: PipelineEnv) -> PipelineEnv:
    """`pipeline_env` over the planted lake with the change-link decider on."""
    del _planted
    reconfigure(pipeline_env)
    return pipeline_env
