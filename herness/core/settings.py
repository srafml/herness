"""Pydantic models of the ``herness.yaml`` sections (impl 10 U10-02 … U10-07).

Settings module (R-03): stdlib, pydantic and ``herness.core.errors`` only. Models are
``extra="forbid"``, ``strict=True`` and ``frozen=True``; ``Path`` fields are lax (YAML has
no path type, U10-02) and YAML lists become tuples before strict item validation.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic.functional_validators import AfterValidator, BeforeValidator

from herness.core.errors import ConfigError

# U10-04 step 2: a group holding an unescaped * or + that is itself repeated.
_NESTED_QUANTIFIER: Final = re.compile(r"\((?:[^()\\]|\\.)*[*+](?:[^()\\]|\\.)*\)(?:[*+]|\{\d+,\})")
# C04 hostname rule (U10-20), reused for redaction.denylist_domains.
_HOSTNAME: Final = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_ISO_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
_POSIX_PATH: Final = re.compile(r"/[A-Za-z0-9._/-]{1,200}")
# U10-07 load-time rule: argv-safe characters, or a whole-value <placeholder>.
_ARGV_SAFE: Final = r"^(?:[A-Za-z0-9._:/@+-]{1,256}|<[^<>\s]{1,64}>)$"
_REPO: Final = r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$"
_WORKFLOW: Final = (
    r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}/\.github/workflows/[A-Za-z0-9._-]{1,100}\.ya?ml$"
)
# Secret-reference pattern of U10-27, written out locally (R-03, R-72).
_REF_PATTERN: Final = r"^secret:[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$"
_PARSER_PIN: Final = re.compile(r"[a-z0-9_]{1,40}")
_PIN_RULES: Final[dict[str, re.Pattern[str]]] = {
    "image": re.compile(r"[a-z0-9][a-z0-9._/-]{0,200}@sha256:[0-9a-f]{64}"),
    "model": re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}"),
    "revision": re.compile(r"[0-9a-f]{40}"),
    "gguf": re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.gguf"),
    "sha256": re.compile(r"[0-9a-f]{64}"),
    "served_name": re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"),
    "tool_call_parser": _PARSER_PIN,
    "reasoning_parser": _PARSER_PIN,
}
type _IdKind = Literal["EMPLOYEE_ID", "USER_ID"]
_DEFAULT_IDS: Final[dict[_IdKind, tuple[str, ...]]] = {
    "EMPLOYEE_ID": (r"\bE\d{6}\b",),
    "USER_ID": (),
}
type PinClass = Literal["reasoning", "openjev", "large"]
PIN_CLASSES: Final[tuple[PinClass, ...]] = ("reasoning", "openjev", "large")


def _as_tuple(value: object) -> object:
    if isinstance(value, dict):
        return {key: _as_tuple(item) for key, item in value.items()}
    return tuple(_as_tuple(item) for item in value) if isinstance(value, list) else value


def _check_path(value: object) -> object:
    text = str(value) if isinstance(value, Path) else value
    if isinstance(text, str) and (not text or "\x00" in text):
        msg = "path must be non-empty and must not contain NUL"
        raise ValueError(msg)
    return value


def _iso_date(value: object) -> object:
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        return date.fromisoformat(value)
    return value


def _check_pattern(pattern: str) -> str:
    if len(pattern) > 500:  # noqa: PLR2004 - U10-04 step 1 limit
        msg = "pattern longer than 500 characters"
        raise ValueError(msg)
    if _NESTED_QUANTIFIER.search(pattern):
        msg = "pattern repeats a group that holds * or + (nested quantifier)"
        raise ValueError(msg)
    try:
        re.compile(pattern)
    except re.error as exc:
        msg = f"pattern does not compile: {exc.msg}"
        raise ValueError(msg) from None
    return pattern


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _is_posix_path(value: str) -> bool:
    parts = [part for part in value.split("/") if part]
    return bool(_POSIX_PATH.fullmatch(value)) and ".." not in parts and parts[:1] != ["mnt"]


def _must(test: Callable[[str], bool], msg: str) -> AfterValidator:
    def check(value: str) -> str:
        if not test(value):
            raise ValueError(msg)
        return value

    return AfterValidator(check)


def _re(pattern: str) -> StringConstraints:
    return StringConstraints(pattern=pattern)


_LaxPath = Annotated[Path, BeforeValidator(_check_path), Field(strict=False)]
_Pattern = Annotated[str, AfterValidator(_check_pattern)]
_Patterns = Annotated[tuple[_Pattern, ...], Field(max_length=64)]
_Hosts = Annotated[
    tuple[str, ...], Field(max_length=32), AfterValidator(lambda v: tuple(h.lower() for h in v))
]
_Username = Annotated[str, StringConstraints(min_length=1, max_length=128)]
_Users = Annotated[
    tuple[_Username, ...],
    Field(max_length=500),
    AfterValidator(lambda v: tuple(dict.fromkeys(u.lower() for u in v))),
]
_Hostname = Annotated[
    str, _must(lambda v: _HOSTNAME.fullmatch(v) is not None and not _is_ip(v), "not a hostname")
]
_Ip = Annotated[str, _must(_is_ip, "not an IP literal")]
_Port = Annotated[int, Field(ge=1024, le=65535)]
_Days = Annotated[int, Field(ge=1, le=7300)]
_Safe = Annotated[str, _re(_ARGV_SAFE)]
_PosixPath = Annotated[str, _must(_is_posix_path, "not an absolute POSIX path outside /mnt/")]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _lists_to_tuples(cls, data: object) -> object:
        return _as_tuple(data)


class PathsConfig(_Section):
    """Root paths (U10-02); relative values are resolved by ``load_config``."""

    data: _LaxPath = Path("data")
    logs: _LaxPath = Path("data/logs")
    backup_target: _LaxPath = Path("E:/herness-backup")


class DataPolicyConfig(_Section):
    """Recorded approvals for gated profiles and chat ``cloud`` mode (U10-03, R-38)."""

    hybrid_approved: bool = False
    premium_approved: bool = False
    chat_approved: bool = False
    approved_by: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    approved_on: Annotated[date, BeforeValidator(_iso_date)] | None = None


class SecretsConfig(_Section):
    """Secret store backend (U10-03)."""

    backend: Literal["keyring", "dotenv"] = "keyring"


class EgressConfig(_Section):
    """Off-network egress switch, destinations, purposes and caps (U10-03)."""

    enabled: bool = False
    destinations: _Hosts = ()
    purposes: tuple[Literal["reasoning_final", "reasoning", "bulk_classification"], ...] = ()
    max_request_bytes: Annotated[int, Field(ge=1, le=50_000_000)] = 2_000_000
    max_tokens_per_request: Annotated[int, Field(ge=1, le=2_000_000)] = 200_000
    max_tokens_per_day: Annotated[int, Field(ge=1, le=100_000_000)] = 3_000_000


class NetworkConfig(_Section):
    """Operator socket-guard additions and outbound proxy (U10-03, R-06)."""

    extra_allowed_hosts: _Hosts = ()
    http_proxy: Annotated[str, _re(r"^https?://[A-Za-z0-9.-]+:\d{1,5}$")] | None = None


class RedactionConfig(_Section):
    """Redaction detectors and HMAC key reference (U10-04)."""

    mask_ip: bool = True
    directory_file: _LaxPath | None = Path("D:/herness/private/directory.csv")
    extra_names: tuple[Annotated[str, StringConstraints(min_length=2, max_length=128)], ...] = ()
    id_patterns: dict[_IdKind, _Patterns] = Field(default_factory=lambda: dict(_DEFAULT_IDS))
    national_id_patterns: _Patterns = (r"\b\d{3}-\d{2}-\d{4}\b",)
    custom_patterns: Annotated[
        dict[Annotated[str, _re(r"^[a-z][a-z0-9_]{0,31}$")], _Pattern],
        Field(max_length=64),
    ] = Field(default_factory=dict)
    ner: Literal["none", "presidio"] = "none"
    key: Annotated[str, _re(_REF_PATTERN)] = "secret:redact.hmac_key"
    denylist_domains: tuple[_Hostname, ...] = ()


class ExposeConfig(_Section):
    """Reverse-proxy exposure; cross-field rules are C05 and C21 (U10-05, R-50)."""

    enabled: bool = False
    trusted_proxy: _Ip | None = None
    identity_header: Annotated[str, _re(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")] = "X-Forwarded-User"


class RolesConfig(_Section):
    """Dashboard roles; usernames lower-cased and de-duplicated in order (U10-05)."""

    admins: _Users = ()
    reviewers: _Users = ()
    default_role: Literal["viewer", "denied"] = "viewer"


class UiConfig(_Section):
    """Dashboard bind address, port, exposure and roles (U10-05)."""

    bind: _Ip = "127.0.0.1"
    port: _Port = 8501
    expose: ExposeConfig = ExposeConfig()
    roles: RolesConfig = RolesConfig()


class SecurityConfig(_Section):
    """The ``security`` section (U10-03); file-only (U10-18)."""

    data_policy: DataPolicyConfig = DataPolicyConfig()
    secrets: SecretsConfig = SecretsConfig()
    redaction: RedactionConfig = RedactionConfig()
    egress: EgressConfig = EgressConfig()
    network: NetworkConfig = NetworkConfig()
    ui: UiConfig = UiConfig()


class LoggingConfig(_Section):
    """The ``logging`` section (U10-06)."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class RetentionConfig(_Section):
    """The ``retention`` section (U10-06)."""

    raw_lake_months: Annotated[int, Field(ge=1, le=240)] = 36
    traces_days: _Days = 90
    egress_log_days: _Days = 365
    audit_log_days: _Days = 730
    app_log_days: _Days = 30
    reports_days: _Days = 365
    chat_days: _Days = 180


class BackupConfig(_Section):
    """The ``backup`` section (U10-06)."""

    nightly_at: Annotated[str, _re(r"^([01]\d|2[0-3]):[0-5]\d$")] = "01:30"
    keep_daily: Annotated[int, Field(ge=1, le=365)] = 14
    keep_weekly: Annotated[int, Field(ge=0, le=520)] = 8
    include_lake: bool = False
    include_cache: bool = False


class ReasoningDeploy(_Section):
    """vLLM reasoning server pins (U10-07); host port ``127.0.0.1:8000`` (R-51)."""

    image: _Safe
    model: _Safe
    revision: _Safe
    served_name: _Safe = "local-30b"
    gpu_memory_utilization: Annotated[float, Field(ge=0.10, le=0.98)] = 0.90
    max_model_len: Annotated[int, Field(ge=1024, le=1_048_576)] = 32768
    tool_call_parser: _Safe
    reasoning_parser: _Safe = "qwen3"
    port: _Port = 8000


class OpenJevDeploy(_Section):
    """OpenJev decider server pins (U10-07); host ``127.0.0.1:8100`` → container 8080."""

    image: _Safe
    model: _Safe = "nvidia/diffusiongemma-26B-A4B-it-NVFP4"
    revision: _Safe
    gpu_util: float = 0.9
    max_num_seqs: Annotated[int, Field(ge=1, le=1024)] = 64
    canvas: Annotated[int, Field(ge=1, le=1024)] = 64
    port: _Port = 8100


class LargeDeploy(_Section):
    """llama.cpp large-model server pins (U10-07); host port ``127.0.0.1:8200`` (R-51)."""

    image: _Safe
    gguf: _Safe
    sha256: _Safe
    ctx: int = 32768
    gpu_layers: Annotated[int, Field(ge=0, le=999)] = 20
    port: _Port = 8200


class ServiceDeploy(_Section):
    """Windows service wrapper (U10-07)."""

    manager: Literal["nssm", "task_scheduler"] = "nssm"
    account: _Safe = "svc-herness"


class ReleaseDeploy(_Section):
    """Release-verification block for ``herness deploy install`` (U10-07, ENG E4, R-58)."""

    repo: Annotated[str, _re(_REPO)] | None = None
    signer_workflow: Annotated[str, _re(_WORKFLOW)] | None = None
    licence_exceptions: tuple[_Safe, ...] = ()
    duckdb_extensions: dict[Literal["excel"], Annotated[str, _re(r"^[0-9a-f]{64}$")]] = Field(
        default_factory=dict
    )


class DeployConfig(_Section):
    """The ``deploy`` section (U10-07): argv-safe at load, strict pins at deploy time."""

    wsl_distro: Annotated[str, _re(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")] = "herness"
    model_root: _PosixPath = "/opt/herness/hf"
    env_file: _PosixPath = "/opt/herness/docker.env"
    reasoning: ReasoningDeploy
    openjev: OpenJevDeploy
    large: LargeDeploy
    service: ServiceDeploy = ServiceDeploy()
    release: ReleaseDeploy = ReleaseDeploy()

    @model_validator(mode="after")
    def _ports_distinct(self) -> Self:
        ports = (self.reasoning.port, self.openjev.port, self.large.port)
        if len(set(ports)) != len(ports):
            msg = "deploy ports of reasoning, openjev and large must be pairwise distinct"
            raise ValueError(msg)
        return self

    def unpinned_keys(self, classes: Iterable[PinClass] = PIN_CLASSES) -> tuple[str, ...]:
        """Return ``<class>.<field>`` for every value that fails the deploy-time pin rules."""
        keys: list[str] = []
        for name in classes:
            section: BaseModel = getattr(self, name)
            fields = type(section).model_fields
            for field, rule in _PIN_RULES.items():
                if field in fields and not rule.fullmatch(str(getattr(section, field))):
                    keys.append(f"{name}.{field}")
        return tuple(keys)

    def require_pinned(self, classes: Iterable[PinClass] = PIN_CLASSES) -> None:
        """Raise ``ConfigError("deploy.<key> is not pinned")`` for the first unpinned value."""
        keys = self.unpinned_keys(classes)
        if keys:
            key = f"deploy.{keys[0]}"
            msg = f"{key} is not pinned"
            raise ConfigError(msg, hint="pin the value in herness.yaml deploy", key=key)
