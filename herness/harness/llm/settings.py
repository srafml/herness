"""Section models of ``config/models.yaml`` (impl 05 U05-19, design §7).

Settings module (R-03): stdlib, pydantic, ``herness.core.types`` and ``herness.core.errors``
only. ``ModelsConfig`` holds the ``models`` and ``harness`` sections; the sibling ``deciders``
section is impl 03's, composed by the impl 10 root config (R-76, D05-28). Violations raise
``ConfigError("<key path>: <rule>")`` naming keys, never values, with no cause (TH05-15).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Annotated, ClassVar, Final, Literal, NoReturn, Self
from urllib.parse import SplitResult, urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    StringConstraints,
    ValidationError,
    model_validator,
)

from herness.core.errors import ConfigError

_Depth = Literal["fast", "standard", "deep"]
_TraceKind = Literal["eval", "chat", "review"]
_MODEL_CONFIG: Final = ConfigDict(extra="forbid", strict=True, frozen=True)
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_OLLAMA_PORT: Final = 11434
# Secret-reference shape of U10-27, restated locally (R-03, R-72); plain-text keys fail.
_REF_PATTERN: Final = r"^secret:[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$"
_BLOCKED_COLUMN: Final = r"^(core|enrich|metrics|score|meta)\.[a-z_]+\.[a-z_]+$"
_DEFAULT_BLOCKED_COLUMNS: Final = (
    "core.incident.short_description",
    "core.incident.description",
    "core.incident.close_notes",
    "core.change.short_description",
    "core.change.description",
    "core.problem.root_cause_text",
    "core.work_item.description",
)


def _fail(path: str, rule: str) -> NoReturn:
    msg = f"{path}: {rule}"
    raise ConfigError(msg, key=path)


def _key_path(prefix: str, loc: tuple[int | str, ...]) -> str:
    path = prefix + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in loc)
    return path.removeprefix(".")


def _convert[T](prefix: str, data: object, handler: Callable[[object], T]) -> T:
    """Run ``handler``; turn a pydantic error into ``ConfigError`` without its cause."""
    try:
        return handler(data)
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False, include_context=False)[0]
    _fail(_key_path(prefix, first["loc"]), first["msg"].removeprefix("Value error, "))


def _no_float(value: object) -> object:
    if isinstance(value, float | bool):
        msg = "must be a decimal string"
        raise ValueError(msg)  # noqa: TRY004 - pydantic reports only ValueError with a key path
    return value


def _split(url: str) -> SplitResult | None:
    """Split ``url``; ``None`` when its port is not a valid port number."""
    parts = urlsplit(url)
    try:
        _ = parts.port
    except ValueError:
        return None
    return parts


def _infer_server(data: Mapping[str, object]) -> str:
    """Effective server of an ``openai_compat`` client without ``server`` (D05-08)."""
    if data.get("tokenizer") == "vllm_endpoint":
        return "vllm"
    url = data.get("base_url")
    parts = _split(url) if isinstance(url, str) else None
    return "ollama" if parts is not None and parts.port == _OLLAMA_PORT else "llamacpp"


class _Section(BaseModel):
    """Frozen, strict, closed section model; ``_PREFIX`` set → errors become ``ConfigError``."""

    model_config = _MODEL_CONFIG
    _PREFIX: ClassVar[str | None] = None

    @model_validator(mode="wrap")
    @classmethod
    def _as_config_error(cls, data: object, handler: ModelWrapValidatorHandler[Self]) -> Self:
        return handler(data) if cls._PREFIX is None else _convert(cls._PREFIX, data, handler)


_Price = Annotated[
    Decimal, BeforeValidator(_no_float), Field(ge=0, strict=False, allow_inf_nan=False)
]
_SecretRef = Annotated[str, StringConstraints(pattern=_REF_PATTERN)]
_Positive = Annotated[int, Field(gt=0)]
_Column = Annotated[str, StringConstraints(pattern=_BLOCKED_COLUMN)]


class PricePerMTok(_Section):
    """US dollars per million tokens, parsed from decimal strings."""

    input: _Price
    output: _Price
    cache_read: _Price
    cache_write: _Price


class ClientSupports(_Section):
    """Capabilities of one model client."""

    tools: bool = True
    json_schema: bool = True
    thinking_toggle: bool = False
    seed: bool = False
    effort: bool = False
    sampling_params: bool = True
    batch: bool = False


class ClientConfig(_Section):
    """One ``models.clients.<key>`` entry; ``name`` is filled from the map key."""

    name: str = Field(min_length=1)
    kind: Literal["openai_compat", "anthropic"]
    base_url: str | None = None
    api_key: _SecretRef | None = None
    model: str = Field(min_length=1)
    context_window: _Positive
    max_effective_context: _Positive | None = None
    max_output_tokens: _Positive
    tokenizer: Literal["vllm_endpoint", "estimate", "anthropic"]
    max_concurrency: int = Field(ge=1, le=200)
    chat_reserved_slots: int = Field(default=0, ge=0)
    reasoning_parser: str | None = None
    supports: ClientSupports = ClientSupports()
    timeout_s: float = Field(default=300, gt=0, allow_inf_nan=False)
    off_network: bool = False
    gpu_class: Literal["reasoning", "large", "decider"] | None = None
    price_per_mtok: PricePerMTok
    thinking_mode: Literal["adaptive_always", "adaptive_optional", "budget"] | None = None
    server: Literal["vllm", "ollama", "llamacpp", "openai"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _fill_server(cls, raw: object) -> object:
        if isinstance(raw, dict) and raw.get("kind") == "openai_compat" and not raw.get("server"):
            return {**raw, "server": _infer_server(raw)}
        return raw

    def _path(self, key: str) -> str:
        return f"models.clients.{self.name}.{key}"

    @model_validator(mode="after")
    def _check_rules(self) -> Self:
        if self.kind == "anthropic":
            self._check_anthropic()
        else:
            self._check_openai_compat()
        effective = min(self.context_window, self.max_effective_context or self.context_window)
        if effective <= 2 * self.max_output_tokens:
            _fail(self._path("max_output_tokens"), "effective context must exceed 2 x max output")
        if self.chat_reserved_slots >= self.max_concurrency:
            _fail(self._path("chat_reserved_slots"), "must be below max_concurrency")
        return self

    def _check_anthropic(self) -> None:
        if self.base_url is not None:
            _fail(self._path("base_url"), "must be absent for anthropic clients")
        if self.thinking_mode is None:
            _fail(self._path("thinking_mode"), "required for anthropic clients")
        if self.tokenizer != "anthropic":
            _fail(self._path("tokenizer"), "anthropic clients use the anthropic tokenizer")
        if self.price_per_mtok.input <= 0 or self.price_per_mtok.output <= 0:
            _fail(self._path("price_per_mtok"), "anthropic clients have input and output prices")
        if self.server is not None:
            _fail(self._path("server"), "must be null for anthropic clients")

    def _check_openai_compat(self) -> None:
        if self.tokenizer == "anthropic":
            _fail(self._path("tokenizer"), "anthropic tokenizer only on anthropic clients")
        path = self._path("base_url")
        if self.base_url is None:
            _fail(path, "required for openai_compat clients")
        parts = _split(self.base_url)
        if parts is None or parts.scheme not in {"http", "https"} or not parts.hostname:
            _fail(path, "must be an http or https URL with a host and a valid port")
        if self.off_network:
            if parts.scheme != "https":
                _fail(path, "off-network clients must use https")
        elif parts.hostname not in _LOOPBACK_HOSTS:
            _fail(path, "host must be loopback unless off_network is true")


class RoleParams(_Section):
    """Sampling defaults of one model role (design §5.5)."""

    temperature: Annotated[float, Field(ge=0, le=2)] | None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None
    thinking: Literal["off", "on", "auto"]


class DepthOverride(_Section):
    """``models.depth_overrides.<depth>``: role-to-client overrides for one depth."""

    roles: dict[str, str] = {}


class AnthropicSettings(_Section):
    """Anthropic-wide settings; server-side fallback stays off (spec 08 owns fallback)."""

    server_side_fallback: bool = False
    cache_ttl: Literal["5m"] = "5m"


class DepthDefault(_Section):
    """``models.depth``: the default run depth."""

    default: _Depth = "standard"


class ModelsSection(_Section):
    """The ``models`` section: clients, routing and role parameters (no ``deciders``, R-76)."""

    _PREFIX = "models"
    clients: dict[str, ClientConfig]
    roles: dict[str, str]
    fallback: dict[str, Annotated[list[str], Field(min_length=1)]] = {}
    depth_overrides: dict[_Depth, DepthOverride] = {}
    role_params: dict[str, RoleParams] = {}
    anthropic: AnthropicSettings = AnthropicSettings()
    depth: DepthDefault = DepthDefault()

    @model_validator(mode="before")
    @classmethod
    def _fill_names(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("clients"), dict):
            clients = {
                key: {"name": key, **value} if isinstance(value, dict) else value
                for key, value in data["clients"].items()
            }
            return {**data, "clients": clients}
        return data

    def _known(self, path: str, client: str) -> None:
        if client not in self.clients:
            _fail(path, "names no client in models.clients")

    @model_validator(mode="after")
    def _check_routing(self) -> Self:
        for key, client in self.clients.items():
            if client.name != key:
                _fail(f"models.clients.{key}.name", "must equal the client key")
        for role, target in self.roles.items():
            self._known(f"models.roles.{role}", target)
        for role, chain in self.fallback.items():
            for index, target in enumerate(chain):
                self._known(f"models.fallback.{role}[{index}]", target)
            if role in self.roles and chain[0] != self.roles[role]:
                _fail(f"models.fallback.{role}[0]", "must equal models.roles.<role>")
        for depth, override in self.depth_overrides.items():
            for role, target in override.roles.items():
                self._known(f"models.depth_overrides.{depth}.roles.{role}", target)
        if self.anthropic.server_side_fallback:
            _fail("models.anthropic.server_side_fallback", "must be false")
        return self


class ToolsSettings(_Section):
    """``harness.tools``: tool dispatch limits."""

    max_parallel: int = Field(default=4, ge=1, le=16)


class SqlSettings(_Section):
    """``harness.sql``: SQL tool limits and columns the guard blocks."""

    return_rows: int = Field(default=200, ge=1, le=1_000)
    scan_rows: int = Field(default=1_000_000, ge=1_000, le=10_000_000)
    # three distinct Literal keys: min_length=3 means every depth is set
    timeout_s: dict[_Depth, Annotated[float, Field(ge=1, le=600)]] = Field(
        default={"fast": 15.0, "standard": 30.0, "deep": 120.0}, min_length=3
    )
    threads: int = Field(default=4, ge=1, le=32)
    memory_limit: Annotated[str, StringConstraints(pattern=r"^[0-9]+(MB|GB)$")] = "8GB"
    blocked_columns: list[_Column] = list(_DEFAULT_BLOCKED_COLUMNS)


class LoopSettings(_Section):
    """``harness.loop``: agent-loop guards."""

    wrap_up_ratio: float = Field(default=0.9, ge=0.5, le=0.99)
    no_progress_steps: int = Field(default=4, ge=2, le=20)
    error_streak: int = Field(default=3, ge=2, le=20)


class VerifierSettings(_Section):
    """``harness.verifier`` (``claim_check`` and ``claim_checker`` removed by R-37)."""

    float_rel_tol: float = Field(default=0.005, gt=0, le=0.05)
    rerun_timeout_s: float = Field(default=60, ge=1, le=600)


class TraceSettings(_Section):
    """``harness.trace``: payload sampling per run kind."""

    payload_sample_rate: dict[_TraceKind, Annotated[float, Field(ge=0, le=1)]] = Field(
        default={"eval": 1.0, "chat": 0.0, "review": 0.1}, min_length=3
    )
    max_payload_chars: int = Field(default=20_000, ge=1_000, le=200_000)


class HarnessSettings(_Section):
    """The ``harness`` section."""

    _PREFIX = "harness"
    tools: ToolsSettings = ToolsSettings()
    sql: SqlSettings = SqlSettings()
    loop: LoopSettings = LoopSettings()
    verifier: VerifierSettings = VerifierSettings()
    trace: TraceSettings = TraceSettings()


class ModelsConfig(_Section):
    """The impl 05 part of ``config/models.yaml``: the ``models`` and ``harness`` sections."""

    _PREFIX = ""
    models: ModelsSection
    harness: HarnessSettings
