"""Off-network predicate and egress-aware fallback chains of ``LLMRegistry`` (T07-25, DD R-81).

Private sibling of ``registry.py``. With egress disabled an off-network fallback TAIL (a chain
entry after the head) is unreachable by design (``_usable`` in ``core/resilience/chain.py``
skips it at run time), so it is dropped here with a WARNING instead of failing construction.
Heads (role, depth-override, chain head) are never dropped: they fail closed in the registry.
"""

from __future__ import annotations

from herness.core.logging import get_logger
from herness.harness.llm.settings import ClientConfig, ModelsSection

__all__ = ["is_off_network", "reachable_fallback", "used_client_names"]

_log = get_logger("harness.llm.registry")


def is_off_network(cfg: ClientConfig) -> bool:
    """T05-04 M-2: off-network is ``cfg.off_network`` widened to every anthropic client."""
    return cfg.off_network or cfg.kind == "anthropic"


def reachable_fallback(
    models: ModelsSection, *, egress_enabled: bool, profile: str
) -> dict[str, list[str]]:
    """``models.fallback`` minus off-network tails when egress is off (heads are kept)."""
    kept: dict[str, list[str]] = {}
    for role, chain in models.fallback.items():
        kept[role] = chain[:1]
        for name in chain[1:]:
            cfg = models.clients.get(name)
            if egress_enabled or cfg is None or not is_off_network(cfg):
                kept[role].append(name)
            else:
                _log.warning(
                    "harness.llm.fallback_tail_dropped", role=role, client=name, profile=profile
                )
    return kept


def used_client_names(models: ModelsSection, fallback: dict[str, list[str]]) -> set[str]:
    """Every client key named by ``roles``, the (reachable) ``fallback`` or ``depth_overrides``."""
    names = set(models.roles.values())
    for chain in fallback.values():
        names.update(chain)
    for override in models.depth_overrides.values():
        names.update(override.roles.values())
    return names
