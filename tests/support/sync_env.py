"""Synthetic `sources.yaml` configs and a tiny files inbox for the sync job tests (T01-11).

Stand-in for the `lake_small` fixture of T11-16 (tests/fixtures/lake_small/), which does not
exist yet: `write_sync_config` writes a full, loadable config tree whose `sources.yaml` and
`mappings.yaml` are replaced, and `drop_inbox` writes the smallest files inbox the IT01
tests need (one delta entity, three rows) with an mtime past the settle time.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from tests.support.config_tree import write_full_config

from herness.core import config as c

FILES_SOURCES_YAML = """\
version: 1
sources:
  files:
    enabled: true
    inbox: data/inbox
    entities:
      teams: {pattern: "*.csv", key_field: [team_code]}
"""

TEAMS_CSV = "team_code,name\nT1,Platform\nT2,Payments\nT3,Search\n"
SETTLED_S = 3600  # older than any settle_seconds (max 3600) so the file is eligible


def write_sync_config(root: Path, sources_yaml: str, mappings_yaml: str | None = None) -> Path:
    """Write a full config tree under `root` with the given `sources.yaml`; return its dir."""
    cfg = write_full_config(root)
    (cfg / "sources.yaml").write_text(sources_yaml, encoding="utf-8")
    if mappings_yaml is not None:
        (cfg / "mappings.yaml").write_text(mappings_yaml, encoding="utf-8")
    return cfg


def init_sync_config(root: Path, sources_yaml: str = FILES_SOURCES_YAML) -> c.HernessConfig:
    """Write the tree and cache it for `get_config` (paths.data = `root/data`)."""
    c.reset_config()
    return c.init_config("local", config_dir=write_sync_config(root, sources_yaml), env={})


def drop_inbox(data_root: Path, rel: str = "teams/teams.csv", text: str = TEAMS_CSV) -> Path:
    """Write one inbox file under `data_root/inbox`, back-dated past the settle time."""
    path = data_root / "inbox" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    past = time.time() - SETTLED_S - 60
    os.utime(path, (past, past))
    return path
