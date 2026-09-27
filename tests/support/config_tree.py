"""Full, valid ``config/`` trees for ``load_config`` tests (impl 10 §11 ``tmp_config``).

``write_full_config`` copies the repository's shipped owner files ``decisions``, ``eval``,
``models``, ``weights``, ``resilience`` and ``metrics`` verbatim and adds minimal ``version: 1``
sections for the rest plus its own ``herness.yaml`` and profiles. ``write_repo_config`` (T10-13,
T10-03b) copies the whole shipped ``config/`` tree verbatim. Every shipped owner file carries
``version: 1`` (T10-03b fix round 1), so no copy needs the line added.
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


def write_full_config(root: Path) -> Path:
    """Write a loadable ``config/`` tree (and profiles) under ``root``; return the config dir."""
    cfg = root / "config"
    (cfg / "profiles").mkdir(parents=True)
    for name in ("decisions", "eval", "models", "weights", "resilience", "metrics"):
        shutil.copyfile(SHIPPED / f"{name}.yaml", cfg / f"{name}.yaml")
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


# Owner files copied byte for byte; ``metrics.yaml`` is copied alongside them.
OWNER_STEMS: tuple[str, ...] = (
    "sources",
    "mappings",
    "decisions",
    "weights",
    "models",
    "pipelines",
    "memory",
    "resilience",
    "app",
    "eval",
)
_PROFILES: tuple[str, ...] = ("local", "hybrid", "premium", "synth")


def write_repo_config(root: Path) -> Path:
    """Copy the repository's shipped ``config/`` tree byte for byte.

    ``herness.yaml``, ``profiles/*.yaml``, ``injection_patterns.txt``, ``metrics.yaml`` and the
    ``OWNER_STEMS`` files are copied unchanged.
    """
    cfg = root / "config"
    (cfg / "profiles").mkdir(parents=True)
    shutil.copyfile(SHIPPED / "herness.yaml", cfg / "herness.yaml")
    for name in OWNER_STEMS:
        shutil.copyfile(SHIPPED / f"{name}.yaml", cfg / f"{name}.yaml")
    shutil.copyfile(SHIPPED / "injection_patterns.txt", cfg / "injection_patterns.txt")
    shutil.copyfile(SHIPPED / "metrics.yaml", cfg / "metrics.yaml")
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
