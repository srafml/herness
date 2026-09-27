"""Tests for herness.enrich.deciders registration and factory (U03-68, U03-69; T03-16).

`build_decider` reads only `cfg.models.deciders` and `cfg.deploy.openjev.image`, so the
tests hand it a namespace with those two paths instead of a fully loaded `HernessConfig`.
Secrets come from the in-memory `fake_keyring`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.enrich._fake_llm import FakeLLMClient, votes_by_seed

from herness.core import registry
from herness.core.errors import AuthError, ConfigError
from herness.enrich.deciders import build_decider, register_deciders
from herness.enrich.deciders.ensemble import EnsembleDecider
from herness.enrich.deciders.jev_hosted import JevHostedDecider
from herness.enrich.deciders.laya import LayaDecider
from herness.enrich.deciders.llm import LlmDecider
from herness.enrich.deciders.openjev import OpenJevDecider
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import (
    DecidersSettings,
    JevSettings,
    LayaSettings,
    OpenJevSettings,
)

if TYPE_CHECKING:
    from herness.core.config import HernessConfig

pytestmark = pytest.mark.unit


_IMAGE = "razorback16/openjev:0.4.0@sha256:" + "0" * 64
_KEY_VALUE = "unit-" + "decider-token"  # synthetic
_CLASSES = {
    "laya": LayaDecider,
    "openjev": OpenJevDecider,
    "jev": JevHostedDecider,
    "llm": LlmDecider,
    "ensemble": EnsembleDecider,
}


def _cfg(deciders: DecidersSettings | None = None, image: str = _IMAGE) -> HernessConfig:
    ns = SimpleNamespace(
        models=SimpleNamespace(deciders=deciders or DecidersSettings()),
        deploy=SimpleNamespace(openjev=SimpleNamespace(image=image)),
    )
    return cast("HernessConfig", ns)


def _paths(tmp_path: Path) -> EnrichPaths:
    return EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")


def _build(name: str, tmp_path: Path, **kw: Any) -> Any:
    register_deciders()
    kw.setdefault("cfg", _cfg())
    kw.setdefault("depth", "deep")
    kw.setdefault("llm", None)
    return build_decider(name, paths=_paths(tmp_path), **kw)  # type: ignore[arg-type]


# --- U03-68 register_deciders -----------------------------------------------------------


def test_ut03_66_register_twice_resolves_every_class() -> None:
    """UT03-66 on a clean registry, registering twice resolves the five classes, no error."""
    register_deciders()
    register_deciders()
    for name, cls in _CLASSES.items():
        assert registry.get("decider", name) is cls


def test_ut03_66_different_class_under_a_name_is_config_error() -> None:
    """UT03-66 a different class already registered under a name raises the registry error."""
    registry.register("decider", "laya")(object)
    with pytest.raises(ConfigError, match="duplicate"):
        register_deciders()


# --- U03-69 build_decider ---------------------------------------------------------------


def test_ut03_67_openjev_disabled_is_config_error(tmp_path: Path) -> None:
    """UT03-67 openjev disabled -> ConfigError("decider openjev disabled")."""
    cfg = _cfg(DecidersSettings(openjev=OpenJevSettings(enabled=False)))
    with pytest.raises(ConfigError, match="decider openjev disabled"):
        _build("openjev", tmp_path, cfg=cfg)


def test_ut03_67_jev_disabled_is_config_error(tmp_path: Path) -> None:
    """UT03-67 jev disabled (the default) -> ConfigError("decider jev disabled")."""
    with pytest.raises(ConfigError, match="decider jev disabled"):
        _build("jev", tmp_path)


def test_ut03_67_jev_without_key_is_auth_error(tmp_path: Path, fake_keyring: MemoryKeyring) -> None:
    """UT03-67 jev enabled without its secret -> AuthError naming no secret value."""
    del fake_keyring
    cfg = _cfg(DecidersSettings(jev=JevSettings(enabled=True)))
    with pytest.raises(AuthError) as info:
        _build("jev", tmp_path, cfg=cfg)
    assert "TYPESAFE_API_KEY" not in str(info.value)


def test_ut03_67_jev_with_key(tmp_path: Path, fake_keyring: MemoryKeyring) -> None:
    """UT03-67 jev enabled with its secret builds a JevHostedDecider with the depth samples."""
    fake_keyring.set_password("herness", "typesafe_api_key", _KEY_VALUE)
    cfg = _cfg(DecidersSettings(jev=JevSettings(enabled=True)))
    decider = _build("jev", tmp_path, cfg=cfg)
    assert isinstance(decider, JevHostedDecider)
    assert decider._api_key is not None
    assert decider._api_key.get_secret_value() == _KEY_VALUE
    assert decider._samples == 5  # settings.openjev.samples.deep


def test_ut03_67_openjev_samples_key_and_version(
    tmp_path: Path, fake_keyring: MemoryKeyring
) -> None:
    """UT03-67 openjev: depth samples, override, optional key and the pinned image tag."""
    fake_keyring.set_password("herness", "openjev_api_key", _KEY_VALUE)
    deep = _build("openjev", tmp_path)
    assert isinstance(deep, OpenJevDecider)
    assert deep.version == "openjev-0.4.0/openjev-latest"
    assert deep._samples == 5
    assert deep._api_key is not None
    assert deep._api_key.get_secret_value() == _KEY_VALUE
    fast = _build("openjev", tmp_path, depth="fast")
    assert fast._samples == 1
    assert _build("openjev", tmp_path, depth="standard")._samples is None
    assert _build("openjev", tmp_path, depth="fast", samples_override=3)._samples == 3


def test_ut03_67_openjev_without_key_sends_none(
    tmp_path: Path, fake_keyring: MemoryKeyring
) -> None:
    """UT03-67 openjev without its secret builds with api_key None (no header)."""
    del fake_keyring
    assert _build("openjev", tmp_path)._api_key is None


@pytest.mark.parametrize(
    ("image", "version"),
    [
        ("registry.local:5000/team/openjev:0.5.1@sha256:" + "a" * 64, "openjev-0.5.1/m"),
        ("razorback16/openjev@sha256:" + "b" * 64, "openjev-" + "b" * 12 + "/m"),
    ],
)
def test_ut03_67_openjev_image_tag_forms(
    tmp_path: Path, fake_keyring: MemoryKeyring, image: str, version: str
) -> None:
    """UT03-67 the tag is the text between the last `:` of the name and `@`; digest prefix else."""
    del fake_keyring
    cfg = _cfg(DecidersSettings(openjev=OpenJevSettings(model="m")), image=image)
    assert _build("openjev", tmp_path, cfg=cfg).version == version


@pytest.mark.parametrize("image", ["<openjev-image>", "openjev"])
def test_ut03_67_openjev_unpinned_image_is_config_error(
    tmp_path: Path, fake_keyring: MemoryKeyring, image: str
) -> None:
    """UT03-67 an image with neither tag nor digest cannot give a version."""
    del fake_keyring
    with pytest.raises(ConfigError, match="openjev image"):
        _build("openjev", tmp_path, cfg=_cfg(image=image))


def test_ut03_67_llm_votes_temperature_and_client(tmp_path: Path) -> None:
    """UT03-67 llm: votes by depth, settings temperature, the injected client tuple."""
    client = FakeLLMClient(votes_by_seed({}))
    decider = _build("llm", tmp_path, depth="standard", llm=(client, "local/qwen", 2))
    assert isinstance(decider, LlmDecider)
    assert decider.version == "local/qwen"
    assert decider.samples == 3
    assert decider._temperature == 0.7
    assert decider._max_concurrency == 2
    assert decider._client is client


def test_ut03_67_llm_without_client_is_config_error(tmp_path: Path) -> None:
    """UT03-67 llm without the `llm` tuple is a ConfigError."""
    with pytest.raises(ConfigError, match="llm"):
        _build("llm", tmp_path)


def test_ut03_67_laya_uses_settings_paths_and_embed_fn(tmp_path: Path) -> None:
    """UT03-67 laya reads `CURRENT` through `paths` and keeps `embed_fn`; nothing is loaded."""
    current = tmp_path / "c"  # `data/c` under the data root
    current.write_text("laya-20261004-1\n", encoding="utf-8")
    cfg = _cfg(DecidersSettings(laya=LayaSettings(device="cpu")))

    def embed(texts: Any) -> Any:
        return texts

    decider = _build("laya", tmp_path, cfg=cfg, embed_fn=embed)
    assert isinstance(decider, LayaDecider)
    assert decider.version == "laya-20261004-1"
    assert decider._settings.device == "cpu"
    assert decider._embed_fn is embed
    assert decider._agent is None


def test_ut03_67_unknown_or_unregistered_name_is_config_error(tmp_path: Path) -> None:
    """UT03-67 `ensemble` is not built by the factory; an unregistered name fails in registry."""
    with pytest.raises(ConfigError, match="ensemble"):
        _build("ensemble", tmp_path)
    registry.reset_registry()
    with pytest.raises(ConfigError, match="unknown decider"):
        build_decider("laya", cfg=_cfg(), depth="deep", paths=_paths(tmp_path), llm=None)
