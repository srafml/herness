"""Egress guard test harness (impl 10 §11: ``tmp_config`` plus a guard over its logs dir).

Builds a real config with ``write_full_config`` (so ``audit`` writes into the same logs dir as
the guard) and a guard whose redactor has a fixed key and a one-name directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.support.config_tree import write_full_config

from herness.core import config as c
from herness.core.egress import EgressGuard
from herness.core.egress_log import EgressLog
from herness.core.redact import Redactor
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig

API = "https://api.anthropic.com/v1/messages"
DIRECTORY_NAME = "José García"
_KEY = bytes(range(32))


def fixed_redactor() -> Redactor:
    """A redactor with a fixed key whose name directory holds ``DIRECTORY_NAME``."""
    names = NameDirectory.from_files(None, (DIRECTORY_NAME,), None)
    return Redactor(RedactionConfig(directory_file=None), _KEY, names)


def load(
    tmp_path: Path,
    profile: str,
    *,
    chat_approved: bool = False,
    purposes: tuple[str, ...] = ("reasoning_final",),
) -> c.HernessConfig:
    """``init_config`` over a full tree; ``purposes`` replaces the hybrid profile's list."""
    cfg_dir = write_full_config(tmp_path)
    if chat_approved:
        path = cfg_dir / "herness.yaml"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("hybrid_approved: true,", "hybrid_approved: true, chat_approved: true,"),
            "utf-8",
        )
    hybrid = cfg_dir / "profiles" / "hybrid.yaml"
    listed = ", ".join(purposes)
    text = hybrid.read_text(encoding="utf-8").replace(
        "purposes: [reasoning_final]", f"purposes: [{listed}]"
    )
    hybrid.write_text(text, "utf-8")
    return c.init_config(profile, config_dir=cfg_dir, env={})  # type: ignore[arg-type]


def with_egress(cfg: c.HernessConfig, **updates: Any) -> c.HernessConfig:
    """``cfg`` with ``security.egress`` fields replaced (bypasses the load-time checks)."""
    egress = cfg.security.egress.model_copy(update=updates)
    security = cfg.security.model_copy(update={"egress": egress})
    return cfg.model_copy(update={"security": security})


def make_guard(cfg: c.HernessConfig, redactor: Redactor | None = None) -> EgressGuard:
    """A guard over ``cfg.paths.logs`` with the test redactor."""
    red = redactor or fixed_redactor()
    return EgressGuard(cfg, lambda: red, EgressLog(cfg.paths.logs, "cfg_test", cfg.profile))


def egress_lines(logs: Path) -> list[dict[str, Any]]:
    """Every egress line under ``logs``, in file order."""
    return [
        json.loads(raw)
        for path in sorted(logs.glob("egress-*.jsonl"))
        for raw in path.read_text(encoding="utf-8").splitlines()
    ]


def audit_fields(logs: Path, event: str = "egress") -> list[dict[str, Any]]:
    """The ``fields`` of every audit line of ``event`` under ``logs``."""
    records = [
        json.loads(raw)
        for path in sorted(logs.glob("audit-*.jsonl"))
        for raw in path.read_text(encoding="utf-8").splitlines()
    ]
    return [r["fields"] for r in records if r["event"] == event]
