"""Full, valid ``config/`` tree for ``load_config`` tests (impl 10 §11 ``tmp_config``).

Copies the repository's shipped owner files (``decisions``, ``eval``, ``metrics``, ``models``,
``weights``) and adds minimal sections for the files not shipped yet. Two adaptations are
applied to the copies, both reported as open items of T10-03:

* ``decisions.yaml``, ``eval.yaml`` and ``models.yaml`` ship without the ``version: 1`` line
  U10-16 requires; it is prepended. T10-13 confirmed this is permanent, not transitional:
  ``DecisionsConfig``, ``load_eval_config`` and the ``set(_raw()) <= {...}`` assertion of
  ``test_ut05_125_repository_config_loads`` each reject an extra ``version`` key, so the real
  files cannot carry it (Ruling R2); ``_versioned`` only ever touches these test copies.
* ``metrics.yaml`` ships ``metrics: []`` until T04-08; one catalog entry is inserted.

``write_repo_config`` (T10-13) is the templates-era counterpart: it copies the real
``config/herness.yaml`` and ``config/profiles/*.yaml`` (shipped by T10-13) plus the owner files
above, verbatim except for the same two adaptations, so it starts exercising each remaining
owner file (``sources``, ``mappings``, ``pipelines``, ``resilience``) the moment it ships.
``app.yaml`` and ``memory.yaml`` are already shipped with real content, but ``HernessConfig``
still mounts them as closed no-field stand-ins (``_AppStub``, ``_MemoryStub``) pending the T09
and T07 cards that wire in ``AppConfig`` and ``MemoryConfig``; loading either file's real
content raises today regardless of this module, so ``write_repo_config`` stands in the minimal
``version: 1`` content for both, same as ``write_full_config`` (T10-13 report carries this over).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from herness.core import registry

REPO = Path(__file__).resolve().parents[2]
SHIPPED = REPO / "config"
FIXTURES = REPO / "tests" / "unit" / "core" / "fixtures"
RESILIENCE_FIXTURE = FIXTURES / "resilience.yaml"
COMPOSE_FIXTURE = FIXTURES / "compose.yaml"  # U10-80 test copy until T10-23 ships docker/

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

# C24 (U10-20): premium needs at least one destination and one purpose.
PREMIUM_YAML = """\
version: 1
security:
  egress: {enabled: true, destinations: [api.anthropic.com], purposes: [reasoning]}
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
    for name in ("local", "synth"):
        (cfg / "profiles" / f"{name}.yaml").write_text("version: 1\n", encoding="utf-8")
    (cfg / "profiles" / "hybrid.yaml").write_text(HYBRID_YAML, encoding="utf-8")
    (cfg / "profiles" / "premium.yaml").write_text(PREMIUM_YAML, encoding="utf-8")
    return cfg


def _pins() -> dict[str, str]:
    # Built at run time so no digest-like literal is committed (detect-secrets).
    return {
        "<reasoning-image>": "vllm/vllm-openai@sha256:" + "a" * 64,
        "<openjev-image>": "razorback16/openjev@sha256:" + "b" * 64,
        "<large-image>": "ghcr.io/ggml-org/llama.cpp@sha256:" + "c" * 64,
        "<rev>": ("0123456789abcdef" * 3)[:40],
        "<gguf>": "qwen3-large.gguf",
        "<sha256>": "d" * 64,
    }


def write_checked_config(root: Path) -> Path:
    """``write_full_config`` with pinned deploy values plus ``<root>/docker/compose.yaml``.

    Every offline cross-check (U10-20) passes on this tree: C13 finds no placeholder and
    C08a finds the compose file (the U10-80 test copy).
    """
    cfg = write_full_config(root)
    text = (cfg / "herness.yaml").read_text(encoding="utf-8")
    for placeholder, value in _pins().items():
        text = text.replace(f'"{placeholder}"', value)
    (cfg / "herness.yaml").write_text(text, encoding="utf-8")
    (root / "docker").mkdir()
    shutil.copyfile(COMPOSE_FIXTURE, root / "docker" / "compose.yaml")
    return cfg


# Stems with no repo file yet (T06-03, T10-23 wiring aside, none of these has shipped: UT10-76
# asserts it stays that way so this stand-in drops out the moment the file lands).
_MISSING_STEMS: tuple[str, ...] = ("sources", "mappings", "pipelines", "resilience")
# Stems shipped with real content that ``HernessConfig`` still mounts as a closed stand-in.
_STUBBED_STEMS: tuple[str, ...] = ("app", "memory")
_PROFILES: tuple[str, ...] = ("local", "hybrid", "premium", "synth")


def write_repo_config(root: Path) -> Path:
    """Copy the repository's shipped ``config/`` templates verbatim; stand in the rest.

    Copies ``herness.yaml`` and ``profiles/*.yaml`` (T10-13) plus the owner files already
    loadable through ``HernessConfig`` (``decisions``, ``eval``, ``metrics``, ``models``,
    ``weights``, ``injection_patterns.txt``), byte for byte apart from the two adaptations the
    module docstring explains. ``_MISSING_STEMS`` and ``_STUBBED_STEMS`` get the same minimal
    stand-in ``write_full_config`` uses; see the docstring for why each is still needed.
    """
    cfg = root / "config"
    (cfg / "profiles").mkdir(parents=True)
    shutil.copyfile(SHIPPED / "herness.yaml", cfg / "herness.yaml")
    for name in ("decisions", "eval", "models"):
        text = (SHIPPED / f"{name}.yaml").read_text(encoding="utf-8")
        (cfg / f"{name}.yaml").write_text(_versioned(text), encoding="utf-8")
    shutil.copyfile(SHIPPED / "weights.yaml", cfg / "weights.yaml")
    shutil.copyfile(SHIPPED / "injection_patterns.txt", cfg / "injection_patterns.txt")
    metrics = (SHIPPED / "metrics.yaml").read_text(encoding="utf-8")
    assert "\nmetrics: []\n" in metrics
    (cfg / "metrics.yaml").write_text(metrics.replace("metrics: []\n", METRIC_ENTRY), "utf-8")
    for stem in _MISSING_STEMS:
        assert not (SHIPPED / f"{stem}.yaml").exists(), f"{stem}.yaml shipped: drop this stand-in"
        if stem == "resilience":
            shutil.copyfile(RESILIENCE_FIXTURE, cfg / "resilience.yaml")
            text = (cfg / "resilience.yaml").read_text(encoding="utf-8")
            (cfg / "resilience.yaml").write_text(_versioned(text), encoding="utf-8")
        else:
            (cfg / f"{stem}.yaml").write_text("version: 1\n", encoding="utf-8")
    for stem in _STUBBED_STEMS:
        (cfg / f"{stem}.yaml").write_text("version: 1\n", encoding="utf-8")
    for name in _PROFILES:
        shutil.copyfile(SHIPPED / "profiles" / f"{name}.yaml", cfg / "profiles" / f"{name}.yaml")
    return cfg


# Every registry name the checked tree references (C03): client kinds and enabled deciders.
CHECKED_NAMES: tuple[tuple[registry.RegistryKind, str], ...] = (
    ("llm_client", "openai_compat"),
    ("llm_client", "anthropic"),
    ("decider", "laya"),
    ("decider", "openjev"),
    ("decider", "llm"),
)


def register_checked_names() -> None:
    """Register stand-ins for ``CHECKED_NAMES`` so C03 passes; tests call ``reset_registry``."""
    for kind, name in CHECKED_NAMES:
        registry.register(kind, name)(object())
