"""Decider backends: registration and the factory (impl 03 U03-68, U03-69, L3).

Backend classes are imported inside the functions (no import of every backend, no cycle via
`herness.core.config`). Secrets are resolved only in `build_decider` (TH03-14, R-72)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from herness.core import registry, secrets
from herness.core.errors import AuthError, ConfigError

if TYPE_CHECKING:
    from herness.core.config import HernessConfig
    from herness.enrich.decide import Decider
    from herness.enrich.deciders.llm import CompletionClient
    from herness.enrich.layout import EnrichPaths

__all__ = ["build_decider", "register_deciders"]


def register_deciders() -> None:
    """Register the five decider classes (U03-68); idempotent, a clash raises ConfigError."""
    from herness.enrich.deciders.ensemble import EnsembleDecider  # noqa: PLC0415 - lazy
    from herness.enrich.deciders.jev_hosted import JevHostedDecider  # noqa: PLC0415 - lazy
    from herness.enrich.deciders.laya import LayaDecider  # noqa: PLC0415 - lazy
    from herness.enrich.deciders.llm import LlmDecider  # noqa: PLC0415 - lazy
    from herness.enrich.deciders.openjev import OpenJevDecider  # noqa: PLC0415 - lazy

    registry.register("decider", "laya")(LayaDecider)
    registry.register("decider", "openjev")(OpenJevDecider)
    registry.register("decider", "jev")(JevHostedDecider)
    registry.register("decider", "llm")(LlmDecider)
    registry.register("decider", "ensemble")(EnsembleDecider)


def _image_tag(image: str) -> str:
    """The text between `:` and `@` of the pinned image; the digest's first 12 hex without a tag."""
    name, _, digest = image.partition("@")
    tail = name.rsplit("/", 1)[-1]
    tag = tail.rpartition(":")[2] if ":" in tail else digest.rpartition(":")[2][:12]
    if not tag or "<" in tag:
        msg = "deploy.openjev image has no tag or digest"
        raise ConfigError(msg)
    return tag


def build_decider(  # noqa: PLR0913 - U03-69's keyword-only signature is binding
    name: Literal["laya", "openjev", "jev", "llm"],
    *,
    cfg: HernessConfig,
    depth: Literal["fast", "standard", "deep"],
    paths: EnrichPaths,
    llm: tuple[CompletionClient, str, int] | None,
    samples_override: int | None = None,
    embed_fn: Callable[..., object] | None = None,
) -> Decider:
    """Construct a configured decider from its registry class (U03-69). Raises ConfigError
    (disabled backend, missing `llm` tuple, unknown name) or AuthError (no hosted Jev key)."""
    settings = cfg.models.deciders
    cls = registry.get("decider", name)
    if name == "laya":
        return cls(settings.laya, paths=paths, embed_fn=embed_fn)  # type: ignore[no-any-return]
    if name == "llm":
        if llm is None:
            msg = "decider llm needs an llm client"
            raise ConfigError(msg)
        client, version, max_concurrency = llm
        votes, temperature = getattr(settings.llm.votes, depth), settings.llm.temperature
        return cls(client, version=version, votes=votes, temperature=temperature,  # type: ignore[no-any-return]
                   max_concurrency=max_concurrency)  # fmt: skip
    if name not in ("openjev", "jev"):
        msg = f"decider {name} is not built by build_decider"
        raise ConfigError(msg)
    backend = settings.openjev if name == "openjev" else settings.jev
    if not backend.enabled:
        msg = f"decider {name} disabled"
        raise ConfigError(msg)
    samples = samples_override or getattr(settings.openjev.samples, depth)
    ref = backend.api_key
    if name == "openjev":
        key = secrets.resolve(ref) if secrets.exists(ref) else None
        tag = _image_tag(cfg.deploy.openjev.image)
        return cls(backend, api_key=key, image_tag=tag, samples=samples)  # type: ignore[no-any-return]
    if not secrets.exists(ref):
        msg = "decider jev has no api key"
        raise AuthError(msg)
    return cls(backend, api_key=secrets.resolve(ref), samples=samples)  # type: ignore[no-any-return]
