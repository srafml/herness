"""Full, valid ``config/`` tree for ``load_config`` tests (impl 10 §11 ``tmp_config``).

Copies the repository's shipped owner files (``decisions``, ``eval``, ``metrics``, ``models``,
``weights``) and adds minimal sections for the files not shipped yet. Two adaptations are
applied to the copies, both reported as open items of T10-03:

* ``decisions.yaml``, ``eval.yaml`` and ``models.yaml`` ship without the ``version: 1`` line
  U10-16 requires; it is prepended.
* ``metrics.yaml`` ships ``metrics: []`` until T04-08; one catalog entry is inserted.
"""

from __future__ import annotations

import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHIPPED = REPO / "config"
RESILIENCE_FIXTURE = REPO / "tests" / "unit" / "core" / "fixtures" / "resilience.yaml"

HERNESS_YAML = """\
version: 1
paths: {data: data, logs: data/logs, backup_target: backup}
security:
  data_policy: {hybrid_approved: true, premium_approved: true, approved_by: ops-lead,
                approved_on: 2026-09-01}
  redaction: {directory_file: null}
logging: {level: INFO}
deploy:
  reasoning: {image: "<reasoning-image>", model: Qwen/Qwen3-30B-A3B, revision: "<rev>",
              tool_call_parser: hermes}
  openjev: {image: "<openjev-image>", revision: "<rev>"}
  large: {image: "<large-image>", gguf: "<gguf>", sha256: "<sha256>"}
"""

HYBRID_YAML = """\
version: 1
models:
  models:
    roles: {skeptic_final: claude-opus, writer: claude-opus}
    fallback: {skeptic_final: [claude-opus, local-30b], writer: [claude-opus, local-30b]}
security:
  egress: {enabled: true, destinations: [api.anthropic.com], purposes: [reasoning_final]}
"""

METRIC_ENTRY = """\
metrics:
  - name: mttr_hours
    description: Mean wall-clock hours from opened_at to resolved_at.
    domain: ops
    grains: [service, team]
    unit: hours
    better: lower
    aggregation: mean
    min_sample_size: 10
    owner: sre-analytics
    estimate: false
    uses_weights: []
    usd_model: mttr
    filters: [priority, service_id]
    enabled: true
    requires_columns: []
    sql: SELECT 1
"""


def _versioned(text: str) -> str:
    return text if "\nversion: 1\n" in f"\n{text}" else "version: 1\n" + text


def write_full_config(root: Path) -> Path:
    """Write a loadable ``config/`` tree (and profiles) under ``root``; return the config dir."""
    cfg = root / "config"
    (cfg / "profiles").mkdir(parents=True)
    for name in ("decisions", "eval", "models", "weights"):
        text = (SHIPPED / f"{name}.yaml").read_text(encoding="utf-8")
        (cfg / f"{name}.yaml").write_text(_versioned(text), encoding="utf-8")
    metrics = (SHIPPED / "metrics.yaml").read_text(encoding="utf-8")
    assert "\nmetrics: []\n" in metrics
    (cfg / "metrics.yaml").write_text(metrics.replace("metrics: []\n", METRIC_ENTRY), "utf-8")
    shutil.copyfile(RESILIENCE_FIXTURE, cfg / "resilience.yaml")
    resilience = (cfg / "resilience.yaml").read_text(encoding="utf-8")
    (cfg / "resilience.yaml").write_text(_versioned(resilience), encoding="utf-8")
    for stem in ("sources", "mappings", "pipelines", "memory", "app"):
        (cfg / f"{stem}.yaml").write_text("version: 1\n", encoding="utf-8")
    (cfg / "herness.yaml").write_text(HERNESS_YAML, encoding="utf-8")
    (cfg / "injection_patterns.txt").write_text("# patterns\nignore previous\n", "utf-8")
    for name in ("local", "premium", "synth"):
        (cfg / "profiles" / f"{name}.yaml").write_text("version: 1\n", encoding="utf-8")
    (cfg / "profiles" / "hybrid.yaml").write_text(HYBRID_YAML, encoding="utf-8")
    return cfg
