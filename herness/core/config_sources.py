"""Safe YAML reading, config layer sources, ``--set`` parsing and bootstrap (U10-15 … U10-21).

Sources emit plain YAML types; list-to-tuple conversion for strict models is the models' job
(``herness.core.settings`` section base). ``herness.core.config`` (U10-09) uses them.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from collections.abc import Hashable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, NoReturn

import yaml
from pydantic import ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import PydanticBaseSettingsSource

from herness.core.errors import ConfigError
from herness.core.settings import SecurityConfig

type ProfileName = Literal["local", "hybrid", "premium", "synth"]
PROFILES: Final[tuple[ProfileName, ...]] = ("local", "hybrid", "premium", "synth")
# Sources whose vendor SDK builds its own client: nothing is derived from base_url (R-06).
SDK_SOURCE_KINDS: Final = frozenset({"snowflake", "mongodb"})
ROOT_SECTIONS: Final = ("paths", "security", "logging", "retention", "backup", "deploy")
_STEMS: Final = "sources mappings decisions metrics weights models pipelines memory resilience app"
FILE_STEMS: Final = tuple(_STEMS.split())
SECTION_NAMES: Final = frozenset({*ROOT_SECTIONS, *FILE_STEMS, "eval"})
MAX_YAML_BYTES: Final = 5_242_880
_MAX_PATTERNS_BYTES: Final = 1_048_576
_MAX_DOTENV_BYTES: Final = 65_536
_MAX_OVERRIDES: Final = 100
_MAX_SEGMENTS: Final = 12
_MAX_VALUE_CHARS: Final = 4096
_MAX_ENV_VARS: Final = 500
_SEGMENT: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DOTENV_LINE: Final = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)")
_ENV_SKIP: Final = {"HERNESS_" + n for n in ["PROFILE", "ENV", "SYNTH_CONFIG", "FAULTS", "WORKER"]}
_PROFILE_BY_NAME: Final[dict[str, ProfileName]] = {name: name for name in PROFILES}


def _fail(msg: str) -> NoReturn:
    raise ConfigError(msg)


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects anchors, aliases and duplicate keys (U10-15 step 4)."""

    source_name: str = "<yaml>"

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        event = self.peek_event()  # type: ignore[no-untyped-call]  # untyped in the PyYAML stubs
        if getattr(event, "anchor", None) is not None:
            line = event.start_mark.line + 1
            _fail(f"YAML anchors and aliases are not allowed: {self.source_name}:{line}")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, Hashable):
                continue  # the base class reports an unhashable key as invalid YAML
            if key in seen:
                _fail(f"duplicate key '{key}' at {self.source_name}:{key_node.start_mark.line + 1}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _parse(text: str, name: str) -> Any:  # noqa: ANN401 - any YAML value
    loader = _StrictLoader(text)
    loader.source_name = name
    try:
        return loader.get_single_data()
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        msg = f"{name}:{mark.line + 1 if mark is not None else 0}: invalid YAML"
        raise ConfigError(msg) from None


def _read_text(path: Path, max_bytes: int) -> str:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        msg = f"config file missing: {path.name}"
        raise ConfigError(msg) from None
    if size > max_bytes:
        _fail(f"config file too large: {path.name}")
    try:
        return path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        msg = f"{path.name}: unreadable or not UTF-8"
        raise ConfigError(msg) from None


def load_yaml_file(path: Path, *, max_bytes: int = MAX_YAML_BYTES) -> dict[str, Any]:
    """Read one config YAML file safely (U10-15)."""
    data = _parse(_read_text(path, max_bytes), path.name)
    if data is None:
        return {}
    if not isinstance(data, dict):
        _fail(f"{path.name}: top level must be a mapping")
    return data


def deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """Merge mappings key by key; lists and scalars from ``over`` replace (U10-08)."""
    out = dict(base)
    for key, value in over.items():
        cur = out.get(key)
        merge = isinstance(cur, Mapping) and isinstance(value, Mapping)
        out[key] = deep_merge(cur, value) if merge else value  # type: ignore[arg-type]
    return out


def _parse_value(text: str, label: str) -> Any:  # noqa: ANN401 - any YAML value
    if len(text) > _MAX_VALUE_CHARS:
        _fail(f"{label}: value too long")
    try:
        return _parse(text, label)
    except ConfigError:
        msg = f"{label}: invalid YAML value"
        raise ConfigError(msg) from None


def _assign(tree: dict[str, Any], segments: Sequence[str], text: str, label: str) -> None:
    if len(segments) > _MAX_SEGMENTS or not all(_SEGMENT.fullmatch(s) for s in segments):
        _fail(f"{label}: invalid key path")
    value = _parse_value(text, label)
    node = tree
    for depth, seg in enumerate(segments[:-1]):
        node = node.setdefault(seg, {})
        if not isinstance(node, dict):
            _fail(f"{label}: {'.'.join(segments[: depth + 1])} is already set to a scalar")
    node[segments[-1]] = value


def parse_overrides(overrides: Sequence[str]) -> dict[str, Any]:
    """Parse repeatable ``--set a.b.c=<yaml value>`` flags into a nested dict (U10-19)."""
    if len(overrides) > _MAX_OVERRIDES:
        _fail(f"at most {_MAX_OVERRIDES} --set overrides")
    out: dict[str, Any] = {}
    for item in overrides:
        key, sep, text = item.partition("=")
        if not sep:
            _fail(f"--set needs key=value: {key}")
        _assign(out, key.split("."), text, f"--set {key}")
    return out


@dataclass(frozen=True)
class LoadContext:
    """State of one ``load_config`` call, read by the layer sources (U10-09 step 2)."""

    profile: ProfileName
    config_dir: Path
    env: Mapping[str, str]


_CONTEXT: ContextVar[LoadContext | None] = ContextVar("herness_config_load", default=None)


@contextmanager
def load_context(
    profile: ProfileName, config_dir: Path, env: Mapping[str, str]
) -> Iterator[LoadContext]:
    """Activate a load context for the sources; reset it on exit (U10-09 step 2)."""
    ctx = LoadContext(profile, config_dir, dict(env))
    token = _CONTEXT.set(ctx)
    try:
        yield ctx
    finally:
        _CONTEXT.reset(token)


def current_load_context() -> LoadContext | None:
    """Return the active load context, or ``None`` outside ``load_config``."""
    return _CONTEXT.get()


def resolve_profile(profile: str | None, env: Mapping[str, str]) -> ProfileName:
    """Argument, else ``HERNESS_PROFILE``, else ``local`` (U10-09 step 1)."""
    value = profile if profile is not None else env.get("HERNESS_PROFILE", "local")
    resolved = _PROFILE_BY_NAME.get(value)
    if resolved is None:
        _fail(f"unknown profile: {value}")
    return resolved


def check_profile_egress(profile: ProfileName, security: SecurityConfig) -> None:
    """``local`` and ``synth`` forbid any egress setting (U10-09 step 6)."""
    egress = security.egress
    if profile in {"local", "synth"} and (egress.enabled or egress.destinations or egress.purposes):
        _fail(f"profile {profile} forbids egress")


def _subtree(data: Mapping[str, Any], key: str, label: str) -> dict[str, Any]:
    value = data.get(key)
    if value is not None and not isinstance(value, dict):
        _fail(f"{label}: {key} must be a mapping")
    return value or {}


def _check_version(data: dict[str, Any], name: str, *, keep: bool = False) -> None:
    version = data.get("version")
    if type(version) is not int or version != 1:
        _fail(f"{name}: version must be 1")
    if not keep:
        del data["version"]


def check_overlay_security(overlay: Mapping[str, Any], profile: ProfileName, label: str) -> None:
    """Overlays never set ``security.data_policy``; ``synth`` never enables egress (U10-17)."""
    security = _subtree(overlay, "security", label)
    if "data_policy" in security:
        _fail("profiles may not set security.data_policy")
    egress = security.get("egress")
    keys = ("enabled", "destinations", "purposes")
    if profile == "synth" and isinstance(egress, dict) and any(egress.get(k) for k in keys):
        _fail("profile synth cannot enable egress")


class _ContextSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        ctx = _CONTEXT.get()
        if ctx is None:
            _fail("config sources need the load_config load context")
        return self._load(ctx)

    def _load(self, ctx: LoadContext) -> dict[str, Any]:
        raise NotImplementedError


class FilesYamlSource(_ContextSource):
    """Lowest layer: ``config/*.yaml`` and ``injection_patterns.txt`` (U10-16)."""

    def _load(self, ctx: LoadContext) -> dict[str, Any]:
        cdir = ctx.config_dir
        out = load_yaml_file(cdir / "herness.yaml")
        _check_version(out, "herness.yaml")
        for key in out:
            if key not in ROOT_SECTIONS:
                _fail(f"herness.yaml: unknown root key {key}")
        names = [*FILE_STEMS, "eval"] if (cdir / "eval.yaml").exists() else FILE_STEMS
        for stem in names:
            out[stem] = load_yaml_file(cdir / f"{stem}.yaml")
            _check_version(out[stem], f"{stem}.yaml", keep=stem == "sources")
        text = _read_text(cdir / "injection_patterns.txt", _MAX_PATTERNS_BYTES)
        lines = (line.strip() for line in text.splitlines())
        out["memory"]["injection_patterns"] = [s for s in lines if s and not s.startswith("#")]
        allowed = {"herness.yaml", "eval.yaml", *(f"{stem}.yaml" for stem in FILE_STEMS)}
        extra = sorted(p.name for p in cdir.glob("*.yaml") if p.name not in allowed)
        if extra:
            _fail(f"unexpected config file {extra[0]}")
        return out


class ProfileYamlSource(_ContextSource):
    """Profile overlay plus the ``HERNESS_SYNTH_CONFIG`` fragment (U10-17)."""

    def _load(self, ctx: LoadContext) -> dict[str, Any]:
        path = ctx.config_dir / "profiles" / f"{ctx.profile}.yaml"
        overlay = load_yaml_file(path)
        _check_version(overlay, path.name)
        for key in overlay:
            if key not in SECTION_NAMES:
                _fail(f"profiles/{path.name}: unknown section {key}")
        check_overlay_security(overlay, ctx.profile, f"profiles/{path.name}")
        synth = ctx.env.get("HERNESS_SYNTH_CONFIG")
        if not synth:
            return overlay
        if ctx.profile != "synth":
            _fail("HERNESS_SYNTH_CONFIG requires profile synth")
        fragment = load_yaml_file(Path(synth))
        if any(key != "mappings" for key in fragment):
            _fail("HERNESS_SYNTH_CONFIG: only mappings may be set")
        return deep_merge(overlay, fragment)


class GuardedEnvSource(_ContextSource):
    """``HERNESS_*`` environment layer; ``security.*`` and ``profile`` are file-only (U10-18)."""

    def _load(self, ctx: LoadContext) -> dict[str, Any]:
        return self._layer(ctx.env.items())

    @staticmethod
    def _layer(items: Iterable[tuple[str, str]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        count = 0
        for name, value in items:
            upper = name.upper()
            skip = upper in _ENV_SKIP or upper.startswith("HERNESS_SECRET__")
            if skip or not upper.startswith("HERNESS_"):
                continue
            count += 1
            if count > _MAX_ENV_VARS:
                _fail(f"more than {_MAX_ENV_VARS} HERNESS_ variables")
            segments = upper.removeprefix("HERNESS_").lower().split("__")
            if segments[0] in {"security", "profile"}:
                what = "security.*" if segments[0] == "security" else "profile"
                _fail(f"{what} is file-only; remove {name}")
            _assign(out, segments, value, name)
        return out


class FilteredDotEnvSource(GuardedEnvSource):
    """``<config_dir parent>/.env`` layer, only with ``HERNESS_ENV=dev`` (U10-18)."""

    def _load(self, ctx: LoadContext) -> dict[str, Any]:
        path = ctx.config_dir.resolve().parent / ".env"
        if ctx.env.get("HERNESS_ENV") != "dev" or not path.exists():
            return {}
        items: list[tuple[str, str]] = []
        text = _read_text(path, _MAX_DOTENV_BYTES)
        for number, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = _DOTENV_LINE.fullmatch(line)
            if match is None:
                _fail(f".env line {number}: malformed")
            value = match.group(2).strip()
            quoted = len(value) >= 2 and value[0] == value[-1] == '"'  # noqa: PLR2004 - quotes
            items.append((match.group(1), value[1:-1] if quoted else value))
        return self._layer(items)


@dataclass(frozen=True)
class BootstrapConfig:
    """Minimal config for the socket guard before the full load (U10-21)."""

    profile: ProfileName
    security: SecurityConfig
    source_hosts: tuple[str, ...]


def _base_url_hosts(node: object) -> Iterator[str]:
    if isinstance(node, list):
        for item in node:
            yield from _base_url_hosts(item)
    elif isinstance(node, dict) and node.get("enabled") is not False:
        url = node.get("base_url")
        try:
            host = urllib.parse.urlsplit(url).hostname if isinstance(url, str) else None
        except ValueError:
            host = None
        if host:
            yield host
        for value in node.values():
            yield from _base_url_hosts(value)


def _source_hosts(sources_file: Mapping[str, Any]) -> tuple[str, ...]:
    """U10-58 step 1 over raw ``sources.yaml`` (R-06): listed hosts plus non-SDK base_urls."""
    hosts: set[str] = set()
    for name, src in _subtree(sources_file, "sources", "sources.yaml").items():
        if not isinstance(src, dict) or src.get("enabled") is False:
            continue
        listed = src.get("hosts") or []
        if not isinstance(listed, list) or not all(isinstance(h, str) for h in listed):
            _fail(f"sources.yaml: sources.{name}.hosts must be a list of hostnames")
        hosts.update(h.lower() for h in listed)
        if name not in SDK_SOURCE_KINDS:
            hosts.update(_base_url_hosts(src))
    return tuple(sorted(hosts))


def load_bootstrap(
    profile: ProfileName | None = None,
    config_dir: Path = Path("config"),
    env: Mapping[str, str] | None = None,
) -> BootstrapConfig:
    """Load profile, merged ``security`` and source hosts for the socket guard (U10-21)."""
    name = resolve_profile(profile, os.environ if env is None else env)
    label = f"profiles/{name}.yaml"
    base = load_yaml_file(config_dir / "herness.yaml")
    overlay = load_yaml_file(config_dir / label)
    check_overlay_security(overlay, name, label)
    merged = deep_merge(_subtree(base, "security", "herness.yaml"), overlay.get("security") or {})
    try:
        security = SecurityConfig.model_validate(merged)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)[:20]
        issues = "; ".join(f"security.{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors)
        msg = f"invalid security config: {issues}"
        raise ConfigError(msg) from None
    hosts = _source_hosts(load_yaml_file(config_dir / "sources.yaml"))
    check_profile_egress(name, security)
    return BootstrapConfig(name, security, hosts)
