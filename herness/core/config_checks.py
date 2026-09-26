"""Pure offline cross-check rows of U10-20 (helper of ``herness.core.config_validate``).

Split out of ``config_validate.py`` for the ENG §2.4 size limit (impl 10 §1). Each row reads
the effective dict through key paths only and yields ``(path, rule)`` pairs: key paths and
rule text, never config values (TH10-06). Rows with side effects (registry, keyring,
subprocess, files) stay in ``config_validate``.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import urlsplit

from herness.core.config_sources import SDK_SOURCE_KINDS

if TYPE_CHECKING:
    from herness.core.config import HernessConfig

__all__ = ["CLIENTS", "CheckContext", "Hit", "Severity", "enabled_sources", "get", "items"]
__all__ += ["loopback", "port", "row_c01", "row_c02", "row_c04", "row_c05", "row_c07"]
__all__ += ["row_c09", "row_c10", "row_c11", "row_c12", "row_c14", "row_c17", "row_c20"]
__all__ += ["row_c21", "row_c24", "row_c25"]

type Severity = Literal["error", "warn"]
type Hit = tuple[str, str] | tuple[str, str, Literal["warn"]]  # (path, rule[, severity override])

CLIENTS: Final = "models.models.clients"
_HOSTNAME: Final = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_WINDOW_LIMIT: Final = {"reasoning": "deploy.reasoning.max_model_len", "large": "deploy.large.ctx"}
_PURPOSES: Final = {
    "hybrid": frozenset({"reasoning_final"}),
    "premium": frozenset({"reasoning", "reasoning_final", "bulk_classification"}),
}
_SDK_HOST: Final = {"dataverse": "login.microsoftonline.com"}
_NOT_CLIENT: Final = "names a client missing from models.models.clients"
_LOOPBACK: Final = "must be a loopback address (R-50)"


@dataclass(frozen=True)
class CheckContext:
    """Inputs of one ``run_cross_checks`` call; ``tree`` is the unmasked effective dict."""

    cfg: HernessConfig
    tree: Mapping[str, Any]
    env: Mapping[str, str]
    compose_path: Path

    @property
    def profile(self) -> str:
        """The profile the config was loaded with."""
        return str(self.tree.get("profile"))


def get(node: object, path: str) -> Any:  # noqa: ANN401 - JSON-shaped tree
    """The value at dotted ``path``, or ``None`` when any step is missing."""
    for part in path.split("."):
        node = node.get(part) if isinstance(node, Mapping) else None
    return node


def items(node: object, path: str) -> list[tuple[str, Any]]:
    """The ``(key, value)`` pairs of the mapping at ``path``; empty when it is no mapping."""
    found = get(node, path)
    return list(found.items()) if isinstance(found, Mapping) else []


def enabled_sources(x: CheckContext) -> list[tuple[str, Mapping[str, Any]]]:
    """``sources.sources`` sections with ``enabled: true``."""
    return [
        (name, src)
        for name, src in items(x.tree, "sources.sources")
        if isinstance(src, Mapping) and src.get("enabled") is True
    ]


def _ip(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(str(value))
    except ValueError:
        return None


def loopback(host: object) -> bool:
    """``localhost`` or a loopback IP literal."""
    address = _ip(host)
    return host == "localhost" or (address is not None and address.is_loopback)


def port(url: object) -> int | None:
    """The explicit port of ``url``, or ``None``."""
    try:
        return urlsplit(str(url)).port
    except ValueError:
        return None


def row_c01(x: CheckContext) -> Iterator[Hit]:
    """C01: roles and fallback entries name configured clients."""
    clients = get(x.tree, CLIENTS) or {}
    for role, name in items(x.tree, "models.models.roles"):
        if isinstance(name, str) and name not in clients:
            yield f"models.models.roles.{role}", _NOT_CLIENT
    for role, chain in items(x.tree, "models.models.fallback"):
        for index, name in enumerate(chain or ()):
            if name not in clients:
                yield f"models.models.fallback.{role}[{index}]", _NOT_CLIENT


def row_c02(x: CheckContext) -> Iterator[Hit]:
    """C02: a role on an off-network client needs egress and the profile gate."""
    clients = get(x.tree, CLIENTS) or {}
    gate = get(x.tree, f"security.data_policy.{x.profile}_approved") is True
    allowed = gate and get(x.tree, "security.egress.enabled") is True
    for role, name in items(x.tree, "models.models.roles"):
        client = clients.get(name) if isinstance(name, str) else None
        if isinstance(client, Mapping) and client.get("off_network") is True and not allowed:
            rule = "names an off-network client: needs security.egress.enabled and profile approval"
            yield f"models.models.roles.{role}", rule


def row_c04(x: CheckContext) -> Iterator[Hit]:
    """C04: egress, extra and source host lists hold host names, not IP literals (R-06)."""
    paths = ("security.egress.destinations", "security.network.extra_allowed_hosts")
    lists = [(path, get(x.tree, path)) for path in paths]
    for name, source in items(x.tree, "sources.sources"):
        lists.append((f"sources.sources.{name}.hosts", get(source, "hosts")))
    for path, hosts in lists:
        for index, host in enumerate(hosts or ()):
            if _ip(host) is not None or not _HOSTNAME.fullmatch(str(host)):
                yield f"{path}[{index}]", "is not a lower-case host name (IP literals not allowed)"


def row_c05(x: CheckContext) -> Iterator[Hit]:
    """C05: loopback bind; exposure requires a loopback ``trusted_proxy`` (R-50)."""
    ui = get(x.tree, "security.ui") or {}
    proxy = get(ui, "expose.trusted_proxy")
    if not loopback(ui.get("bind")):
        yield "security.ui.bind", _LOOPBACK
    if get(ui, "expose.enabled") is True and proxy is None:
        yield "security.ui.expose.trusted_proxy", "is required when security.ui.expose.enabled"
    elif proxy is not None and not loopback(proxy):
        yield "security.ui.expose.trusted_proxy", _LOOPBACK


def row_c07(x: CheckContext) -> Iterator[Hit]:
    """C07: the vLLM served name is the ``local-30b`` client's model."""
    if get(x.tree, "deploy.reasoning.served_name") != get(x.tree, f"{CLIENTS}.local-30b.model"):
        yield "deploy.reasoning.served_name", f"must equal {CLIENTS}.local-30b.model"


def _local_clients(x: CheckContext) -> Iterator[tuple[str, Mapping[str, Any]]]:
    for name, client in items(x.tree, CLIENTS):
        if isinstance(client, Mapping) and client.get("off_network") is False:
            yield f"{CLIENTS}.{name}", client


def row_c09(x: CheckContext) -> Iterator[Hit]:
    """C09: local GPU clients point at the deploy port of their server (R-51)."""
    for path, client in _local_clients(x):
        gpu = client.get("gpu_class")
        served = client.get("kind") == "openai_compat" and gpu in _WINDOW_LIMIT
        if served and port(client.get("base_url")) != get(x.tree, f"deploy.{gpu}.port"):
            yield f"{path}.base_url", f"port must equal deploy.{gpu}.port (R-51)"


def row_c10(x: CheckContext) -> Iterator[Hit]:
    """C10: ``context_window`` fits the server's ``max_model_len`` (R-52)."""
    for path, client in _local_clients(x):
        limit = _WINDOW_LIMIT.get(str(client.get("gpu_class")))
        top, window = limit and get(x.tree, limit), client.get("context_window")
        if isinstance(top, int) and isinstance(window, int) and window > top:
            yield f"{path}.context_window", f"must be <= {limit} (R-52)"


def row_c11(x: CheckContext) -> Iterator[Hit]:
    """C11: egress purposes allowed per profile; ``reasoning`` in hybrid needs chat approval."""
    allowed = _PURPOSES.get(x.profile)
    if allowed is None:
        return
    if x.profile == "hybrid" and get(x.tree, "security.data_policy.chat_approved") is True:
        allowed = allowed | {"reasoning"}
    for index, purpose in enumerate(get(x.tree, "security.egress.purposes") or ()):
        if purpose not in allowed:
            yield f"security.egress.purposes[{index}]", f"purpose not allowed in {x.profile} (R-38)"


def row_c12(x: CheckContext) -> Iterator[Hit]:
    """C12: the ``dotenv`` secrets backend only in dev or ``synth``."""
    dev = x.env.get("HERNESS_ENV") == "dev" or x.profile == "synth"
    if get(x.tree, "security.secrets.backend") == "dotenv" and not dev:
        yield "security.secrets.backend", "dotenv only with HERNESS_ENV=dev or profile synth"


def row_c14(x: CheckContext) -> Iterator[Hit]:
    """C14: custom redaction pattern names are unique case-insensitively."""
    seen: set[str] = set()
    for name, _ in items(x.tree, "security.redaction.custom_patterns"):
        if name.lower() in seen:
            yield f"security.redaction.custom_patterns.{name}", "name repeats (case-insensitive)"
        seen.add(name.lower())


def _base_urls(node: object, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, Mapping) and node.get("enabled") is not False:
        for key, value in node.items():
            if key == "base_url" and isinstance(value, str):
                yield f"{path}.{key}", value
            yield from _base_urls(value, f"{path}.{key}")


def row_c17(x: CheckContext) -> Iterator[Hit]:
    """C17: every enabled source ``base_url`` uses https unless its host is loopback."""
    for name, source in enabled_sources(x):
        for path, url in _base_urls(source, f"sources.sources.{name}"):
            parts = urlsplit(url)
            if parts.scheme != "https" and not loopback(parts.hostname):
                yield path, "must use https unless the host is loopback"


def row_c20(x: CheckContext) -> Iterator[Hit]:
    """C20: SDK sources and MSAL dataverse list their hosts; nothing from ``base_url`` (R-06)."""
    for name, source in enabled_sources(x):
        msal = name == "dataverse" and get(source, "auth.method") == "msal_client_credentials"
        if name not in SDK_SOURCE_KINDS and not msal:
            continue
        hosts = source.get("hosts") or ()
        account = f"{str(source.get('account')).lower()}.snowflakecomputing.com"
        need = account if name == "snowflake" else _SDK_HOST.get(name)
        path = f"sources.sources.{name}.hosts"
        if not hosts:
            yield path, "must list the hosts the vendor SDK connects to (R-06)"
        elif need is not None and need not in hosts:
            what = "<account>.snowflakecomputing.com" if name == "snowflake" else need
            yield path, f"must contain {what} (R-06)"


def row_c21(x: CheckContext) -> Iterator[Hit]:
    """C21: an exposed dashboard with ``default_role: viewer``."""
    ui = get(x.tree, "security.ui") or {}
    if get(ui, "expose.enabled") is True and get(ui, "roles.default_role") == "viewer":
        yield "security.ui.roles.default_role", "is viewer while exposed; D8 recommends denied"


def row_c24(x: CheckContext) -> Iterator[Hit]:
    """C24: ``hybrid`` and ``premium`` have a destination and a purpose."""
    if x.profile in _PURPOSES:
        for key in ("destinations", "purposes"):
            if not get(x.tree, f"security.egress.{key}"):
                yield f"security.egress.{key}", f"needs at least one entry in {x.profile}"


def row_c25(x: CheckContext) -> Iterator[Hit]:
    """C25: ``chat_approved`` only with ``hybrid_approved`` or ``premium_approved`` (R-38)."""
    policy = get(x.tree, "security.data_policy") or {}
    if policy.get("chat_approved") is True and not (
        policy.get("hybrid_approved") is True or policy.get("premium_approved") is True
    ):
        yield "security.data_policy.chat_approved", "needs hybrid_approved or premium_approved"
