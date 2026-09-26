"""Model client registry and role routing (impl 05 U05-31, U05-32; design §3.2, §7).

``LLMRegistry`` builds one client instance per client key and resolves model roles to client
keys, fallback chains and sampling parameters; ``client_for`` is the single-call convenience the
composition root uses outside the agent loop, because L3 never imports ``herness.harness``
(R-05). Off-network predicate (T05-04 M-2): a client counts as off-network when
``cfg.off_network`` or ``cfg.kind == "anthropic"``. Construction fails closed when an
off-network client is in use and egress is disabled; ``health`` makes a bounded loopback call
per local client and never calls an off-network one when egress is disabled (TH05-13).
"""

from __future__ import annotations

import threading
from typing import Final, cast
from urllib.parse import urlsplit

from herness.core import egress, secrets
from herness.core import registry as _registry
from herness.core.config import get_config
from herness.core.errors import ConfigError
from herness.core.logging import get_logger
from herness.harness.llm.base import BASE_ROLE, LLMClient
from herness.harness.llm.settings import ClientConfig, ModelsConfig, ModelsSection, RoleParams

__all__ = ["LLMRegistry", "client_for"]

_HEALTH_TIMEOUT_S: Final = 2.0
_HEALTH_MAX_RESPONSE_BYTES: Final = 1_048_576
_HTTP_OK: Final = 200

_log = get_logger("harness.llm.registry")


def _is_off_network(cfg: ClientConfig) -> bool:
    """T05-04 M-2: off-network is ``cfg.off_network`` widened to every anthropic client."""
    return cfg.off_network or cfg.kind == "anthropic"


def _used_client_names(models: ModelsSection) -> set[str]:
    """Every client key named by ``roles``, ``fallback`` or ``depth_overrides``."""
    names = set(models.roles.values())
    for chain in models.fallback.values():
        names.update(chain)
    for override in models.depth_overrides.values():
        names.update(override.roles.values())
    return names


def _loopback_root(base_url: str) -> str:
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def _context_window_reason(cfg: ClientConfig, payload: object) -> str | None:
    """R-52: the reason ``cfg.context_window`` exceeds the server's ``max_model_len``, else None."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    entry = next((e for e in data if isinstance(e, dict) and e.get("id") == cfg.model), None)
    if entry is None:
        return None
    max_len = entry.get("max_model_len")
    if isinstance(max_len, bool) or not isinstance(max_len, int) or cfg.context_window <= max_len:
        return None
    return f"context_window {cfg.context_window} above server max_model_len {max_len}"


class LLMRegistry:
    """One client instance per client key; role and depth routing (U05-31)."""

    def __init__(
        self, cfg: ModelsConfig, *, profile: str, egress_enabled: bool | None = None
    ) -> None:
        self._cfg = cfg
        self._profile = profile
        self._egress_enabled = (
            get_config().security.egress.enabled if egress_enabled is None else egress_enabled
        )
        self._lock = threading.Lock()
        self._clients: dict[str, LLMClient] = {}
        for name in _used_client_names(cfg.models):
            client_cfg = cfg.models.clients[name]
            if _is_off_network(client_cfg) and not self._egress_enabled:
                _log.error("harness.llm.config_invalid", client=name, profile=profile)
                msg = f"client {name} is off-network but egress is disabled in profile {profile}"
                raise ConfigError(msg, client=name, profile=profile)

    def config(self, name: str) -> ClientConfig:
        """The validated config of client ``name``. Raises ``ConfigError`` when unknown."""
        cfg = self._cfg.models.clients.get(name)
        if cfg is None:
            msg = f"unknown model client {name}"
            raise ConfigError(msg, client=name)
        return cfg

    def client(self, name: str) -> LLMClient:
        """The cached client for ``name``, constructed under lock on first use (algorithm 1)."""
        with self._lock:
            cached = self._clients.get(name)
            if cached is not None:
                return cached
            cfg = self.config(name)
            client_cls = _registry.get("llm_client", cfg.kind)
            instance = cast("LLMClient", client_cls(cfg))
            self._clients[name] = instance
            return instance

    def model_for(self, model_role: str, depth: str) -> str:
        """The client key ``model_role`` routes to at ``depth`` (algorithm 3)."""
        roles = dict(self._cfg.models.roles)
        override = self._cfg.models.depth_overrides.get(depth)  # type: ignore[call-overload]
        if override is not None:
            roles.update(override.roles)
        if model_role in roles:
            return roles[model_role]
        base = BASE_ROLE.get(model_role)
        if base is not None and base in roles:
            return roles[base]
        msg = f"no model for role {model_role}"
        raise ConfigError(msg, model_role=model_role)

    def chain_for(self, model_role: str, depth: str) -> list[str]:
        """``model_for``'s head, followed by its fallback chain, deduplicated (algorithm 4)."""
        head = self.model_for(model_role, depth)
        fallback = self._cfg.models.fallback
        rest = fallback.get(model_role)
        if rest is None:
            base = BASE_ROLE.get(model_role)
            rest = fallback.get(base, []) if base is not None else []
        chain = [head]
        seen = {head}
        for name in rest:
            if name not in seen:
                seen.add(name)
                chain.append(name)
        return chain

    def role_params(self, model_role: str) -> RoleParams | None:
        """``role_params[model_role]``, else the base role's, else ``None`` (algorithm 5)."""
        params = self._cfg.models.role_params
        if model_role in params:
            return params[model_role]
        base = BASE_ROLE.get(model_role)
        return params.get(base) if base is not None else None

    def health(self) -> dict[str, str]:
        """``"ok"``, ``"down"`` or ``"down: <reason>"`` per client key in use (algorithm 6)."""
        names = _used_client_names(self._cfg.models)
        return {name: self._health_one(self.config(name)) for name in names}

    def _health_one(self, cfg: ClientConfig) -> str:
        if _is_off_network(cfg):
            return "ok" if self._egress_enabled else "down: egress disabled"
        try:
            return self._health_local(cfg)
        except Exception as exc:  # noqa: BLE001 - never leak transport/secret/JSON details (R-19)
            return f"down: {type(exc).__name__}"

    def _health_local(self, cfg: ClientConfig) -> str:
        root = _loopback_root(cast("str", cfg.base_url))
        bearer = secrets.resolve(cfg.api_key) if cfg.api_key is not None else None
        with egress.loopback_http_client(
            root,
            timeout_s=_HEALTH_TIMEOUT_S,
            bearer=bearer,
            max_response_bytes=_HEALTH_MAX_RESPONSE_BYTES,
        ) as http:
            response = http.get("/v1/models")
        if response.status_code != _HTTP_OK:
            return "down"
        if cfg.server != "vllm":
            return "ok"
        reason = _context_window_reason(cfg, response.json())
        return "ok" if reason is None else f"down: {reason}"


def client_for(model_role: str, *, profile: str, depth: str) -> tuple[LLMClient, ClientConfig]:
    """The head client and its config for ``model_role`` at ``depth`` (U05-32)."""
    root_cfg = get_config()
    if profile != root_cfg.profile:
        msg = f"client_for profile {profile} does not match loaded profile {root_cfg.profile}"
        raise ConfigError(msg, profile=profile)
    reg = LLMRegistry(root_cfg.models, profile=profile)
    key = reg.model_for(model_role, depth)
    return reg.client(key), reg.config(key)
