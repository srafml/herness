"""Cross-checks C01-C25 and the start-up owner-validator hook (impl 10 U10-20, U10-109).

Rows read the config through ``effective_dict`` key paths, so this module depends on no owner
model (R-04); the pure rows live in the helper ``config_checks``. Issues carry key paths and
rule text only, never config values (TH10-06). ``herness.core.config`` imports this module on
first use (U10-09 step 8, U10-13): it sits last in the spec's core import order.
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import yaml
from pydantic import ValidationError

from herness.core import config_checks as ck
from herness.core import config_sources as cs
from herness.core import config_view as view
from herness.core import registry, secrets
from herness.core.config import HernessConfig, effective_dict
from herness.core.config_checks import CheckContext, Hit, Severity, get, items
from herness.core.config_view import ConfigIssue
from herness.core.errors import ConfigError
from herness.core.logging import get_logger
from herness.core.redact_patterns import build_detectors
from herness.core.registry import RegistryKind
from herness.core.settings import DeployConfig, RedactionConfig

__all__ = ["CROSS_CHECKS", "CrossCheckRow", "OwnerValidator", "enforce_offline_checks"]
__all__ += ["register_owner_validator", "reset_owner_validators", "run_cross_checks"]
__all__ += ["run_owner_validators", "run_startup_validators", "sort_issues"]

_log = get_logger("core.config")
_OWNER_NAME: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_OWNER_ISSUES, _MAX_MESSAGE, _MAX_LISTED, _COMPOSE_TIMEOUT_S = 500, 300, 20, 20
_GPU: Final = "resilience.resilience.gpu"
_DEPLOY_OF: Final = {"vllm-reasoning": "reasoning", "openjev": "openjev", "llamacpp-large": "large"}
_CREDENTIAL: Final = tuple(d for d in build_detectors(RedactionConfig()) if d.type == "CREDENTIAL")


def _registry_refs(x: CheckContext) -> Iterator[tuple[RegistryKind, str, str]]:
    for name, _ in ck.enabled_sources(x):
        yield "connector", name, f"sources.sources.{name}"
    for name, adapter in items(x.tree, "sources.sources.monitoring.adapters"):
        if get(adapter, "enabled") is True:
            yield "monitoring_adapter", name, f"sources.sources.monitoring.adapters.{name}"
    clients = reversed(items(x.tree, ck.CLIENTS))  # first client of each kind names the path
    for kind, path in {get(c, "kind"): f"{ck.CLIENTS}.{n}.kind" for n, c in clients}.items():
        yield "llm_client", str(kind), path
    for name, decider in items(x.tree, "models.deciders"):  # a decider without the flag is on
        if isinstance(decider, Mapping) and decider.get("enabled", True) is True:
            yield "decider", name, f"models.deciders.{name}"


def _c03(x: CheckContext) -> Iterator[Hit]:
    for kind, name, path in _registry_refs(x):
        try:
            registry.get(kind, name)
        except ConfigError:
            yield path, f"no {kind} implementation is registered under this name"


def _c06(x: CheckContext) -> Iterator[Hit]:
    for name in secrets.referenced_secret_names(x.cfg):
        try:
            found = secrets.exists(name)
        except ConfigError:
            found = False
        if not found:
            yield f"secret:{name}", "referenced secret is missing or the store cannot be read"


def _compose_services(path: Path) -> Mapping[str, Any] | str:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))  # aliases allowed: repo file
    except FileNotFoundError:
        return "not checked: compose file missing"
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return "compose file cannot be read or parsed"
    services = get(doc, "services")
    return services if isinstance(services, Mapping) else {}


def _c08a(x: CheckContext) -> Iterator[Hit]:
    services = _compose_services(x.compose_path)
    if isinstance(services, str):  # a missing file is a warn until T10-23 ships docker/
        where = f"{_GPU}.classes"
        yield (where, services, "warn") if "missing" in services else (where, services)
        return
    for gpu_class, spec in items(x.tree, f"{_GPU}.classes"):
        for name, service in items(spec, "services"):
            path = f"{_GPU}.classes.{gpu_class}.services.{name}"
            if get(services, name) is None:
                yield path, "service is missing from docker/compose.yaml"
            elif services[name].get("profiles") != [gpu_class]:
                yield path, f"compose profiles must be exactly [{gpu_class}]"
            deploy = _DEPLOY_OF.get(name)
            if deploy and ck.port(get(service, "url")) != get(x.tree, f"deploy.{deploy}.port"):
                yield f"{path}.url", f"port must equal deploy.{deploy}.port"


def _wsl_available() -> bool:
    return os.name == "nt" and shutil.which("wsl.exe") is not None


def _c08b(x: CheckContext) -> Iterator[Hit]:
    path = f"{_GPU}.compose_cmd"
    if not _wsl_available():
        yield path, "not checked on this host", "warn"
        return
    argv = [*(get(x.tree, path) or ()), "-f", str(get(x.tree, f"{_GPU}.compose_file"))]
    try:  # argv list from the validated resilience config, no shell (U10-20 C08b)
        done = subprocess.run(  # noqa: S603
            [*argv, "config", "--quiet"],
            capture_output=True,
            timeout=_COMPOSE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        done = None
    if done is None or done.returncode != 0:
        yield path, f"compose config --quiet failed or exceeded {_COMPOSE_TIMEOUT_S} s"


def _c13(x: CheckContext) -> Iterator[Hit]:
    try:
        keys = DeployConfig.model_validate(x.tree.get("deploy")).unpinned_keys()
    except ValidationError:  # structure is U10-09's job; nothing to pin-check
        keys = ()
    for key in keys:
        yield f"deploy.{key}", "is not pinned (U10-07 deploy-time rules)"


def _strings(node: object, path: str, keyed: bool) -> Iterator[tuple[str, str, bool]]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            low = str(key).lower()
            inner = keyed or low in view.SECRET_KEYS or low.endswith("_secret")
            yield from _strings(value, f"{path}.{key}" if path else str(key), inner)
    elif isinstance(node, list | tuple):
        for index, value in enumerate(node):
            yield from _strings(value, f"{path}[{index}]", keyed)
    elif isinstance(node, str):
        yield path, node, keyed


def _credential(text: str) -> bool:
    return any(d.prefilter(text) and next(d.find(text), None) is not None for d in _CREDENTIAL)


def _c16(x: CheckContext) -> Iterator[Hit]:
    for path, text, keyed in _strings(x.tree, "", keyed=False):
        name = text.removeprefix("secret:")
        if name != text and secrets.SECRET_NAME.fullmatch(name):
            continue
        if keyed:
            hint = "write secret:<name>" if secrets.SECRET_NAME.fullmatch(text) else "plain text"
            yield path, f"must be a secret: reference (R-72); {hint}"
        elif _credential(text):
            yield path, "holds a credential-like value; use a secret: reference"


def _c23(x: CheckContext) -> Iterator[Hit]:
    value = get(x.tree, "security.redaction.directory_file")
    if value is None:
        yield "security.redaction.directory_file", "is not set"
    elif not (Path(value).is_file() and os.access(value, os.R_OK)):
        yield "security.redaction.directory_file", "does not exist or is not readable"


@dataclass(frozen=True)
class CrossCheckRow:
    """One row of the U10-20 table; ``mode`` is ``offline``, ``online`` or ``registry``."""

    id: str
    severity: Severity
    mode: Literal["offline", "online", "registry"]
    check: Callable[[CheckContext], Iterable[Hit]]


CROSS_CHECKS: Final[tuple[CrossCheckRow, ...]] = tuple(
    CrossCheckRow(row_id, severity, mode, check)  # type: ignore[arg-type]
    for row_id, severity, mode, check in (
        ("C01", "error", "offline", ck.row_c01),
        ("C02", "error", "offline", ck.row_c02),
        ("C03", "error", "registry", _c03),
        ("C04", "error", "offline", ck.row_c04),
        ("C05", "error", "offline", ck.row_c05),
        ("C06", "error", "online", _c06),
        ("C07", "error", "offline", ck.row_c07),
        ("C08a", "error", "offline", _c08a),
        ("C08b", "error", "online", _c08b),
        ("C09", "error", "offline", ck.row_c09),
        ("C10", "error", "offline", ck.row_c10),
        ("C11", "error", "offline", ck.row_c11),
        ("C12", "error", "offline", ck.row_c12),
        ("C13", "warn", "offline", _c13),
        ("C14", "error", "offline", ck.row_c14),
        ("C16", "error", "offline", _c16),
        ("C17", "error", "offline", ck.row_c17),
        ("C20", "error", "offline", ck.row_c20),
        ("C21", "warn", "offline", ck.row_c21),
        ("C23", "warn", "online", _c23),
        ("C24", "error", "offline", ck.row_c24),
        ("C25", "error", "offline", ck.row_c25),
    )
)


def _tree(cfg: HernessConfig) -> dict[str, Any]:
    return effective_dict(cfg, redact_secrets=False)  # C16 needs the unmasked values


def _file_of(path: str) -> str | None:
    return view._file_of(path.split(".", maxsplit=1)[0], cs.FILE_STEMS)


def run_cross_checks(
    cfg: HernessConfig, *, offline: bool, include_registry: bool, compose_path: Path | None = None
) -> list[ConfigIssue]:
    """Evaluate every applicable ``CROSS_CHECKS`` row; one issue per failed rule instance."""
    load = cs.current_load_context()  # env and config dir of the running load_config, if any
    root = (load.config_dir if load else Path("config")).resolve().parent
    compose = compose_path or root / "docker" / "compose.yaml"
    x = CheckContext(cfg, _tree(cfg), load.env if load else dict(os.environ), compose)
    issues: list[ConfigIssue] = []
    for row in CROSS_CHECKS:
        if (row.mode == "online" and offline) or (row.mode == "registry" and not include_registry):
            continue
        for hit in row.check(x):
            severity: Severity = "warn" if len(hit) == 3 else row.severity  # noqa: PLR2004
            issues.append(ConfigIssue(severity, hit[0], f"{row.id} {hit[1]}", _file_of(hit[0])))
    return issues


def sort_issues(issues: Iterable[ConfigIssue]) -> list[ConfigIssue]:
    """Sort as U10-13: ``error`` first, then ``path``."""
    return sorted(issues, key=lambda issue: (issue.severity != "error", issue.path))


def _enforce(issues: Sequence[ConfigIssue], message: str) -> list[ConfigIssue]:
    for issue in issues:
        _log.warning(
            "config.validate.issue",
            severity=issue.severity,
            path=issue.path,
            rule=issue.message,
            file=issue.file,
        )
    if any(issue.severity == "error" for issue in issues):
        raise ConfigError(message, issues=issues, hint="herness config validate")
    return list(issues)


def enforce_offline_checks(cfg: HernessConfig) -> list[ConfigIssue]:
    """U10-09 step 8: offline rows without registry; raise on an error, log each issue."""
    issues = sort_issues(run_cross_checks(cfg, offline=True, include_registry=False))
    shown = "; ".join(str(issue) for issue in issues[:_MAX_LISTED])
    return _enforce(issues, f"invalid config ({len(issues)} issues, first {_MAX_LISTED}): {shown}")


# --- U10-109 owner validators --------------------------------------------------------------


class OwnerValidator(Protocol):
    """An owner's start-up validator; returns issues with key paths, never values (R-71)."""

    def __call__(
        self, cfg: HernessConfig, *, offline: bool
    ) -> Iterable[ConfigIssue | Mapping[str, str]]: ...


_VALIDATORS: dict[str, OwnerValidator] = {}  # ENG §2.3 module registry, reset by tests
_VAL_LOCK: Final = threading.Lock()


def register_owner_validator(name: str, fn: OwnerValidator) -> None:
    """Register ``fn`` under ``name`` (composition root only, before ``init_config``)."""
    if not _OWNER_NAME.fullmatch(name):
        msg = "invalid owner validator name"
        raise ConfigError(msg)
    with _VAL_LOCK:
        known = _VALIDATORS.setdefault(name, fn)
    if known is not fn:
        msg = f"duplicate owner validator {name}"
        raise ConfigError(msg)


def reset_owner_validators() -> None:
    """Drop every registration (tests only; ``reset_config`` keeps them)."""
    with _VAL_LOCK:
        _VALIDATORS.clear()


def _convert(name: str, item: object) -> ConfigIssue:
    if isinstance(item, ConfigIssue):
        return item
    fields = item if isinstance(item, Mapping) else {}
    severity, path, message, file = (fields.get(k) for k in ("severity", "path", "message", "file"))
    texts = (path, message) if isinstance(path, str) and isinstance(message, str) else None
    if severity in ("error", "warn") and texts and (file is None or isinstance(file, str)):
        return ConfigIssue(severity, texts[0], texts[1][:_MAX_MESSAGE], file)
    return ConfigIssue("error", name, f"validator {name} returned an invalid issue", None)


def _run_one(name: str, fn: OwnerValidator, cfg: HernessConfig, offline: bool) -> list[ConfigIssue]:
    try:
        results = list(itertools.islice(fn(cfg, offline=offline), _MAX_OWNER_ISSUES))
    except Exception as exc:  # noqa: BLE001 - any failure becomes one issue, message dropped
        return [ConfigIssue("error", name, f"validator {name} failed: {type(exc).__name__}", None)]
    return [_convert(name, item) for item in results]


def run_owner_validators(cfg: HernessConfig, *, offline: bool) -> list[ConfigIssue]:
    """Run the registered validators in name order; never raises for a validator problem."""
    with _VAL_LOCK:
        registered = sorted(_VALIDATORS.items())
    issues = [issue for n, fn in registered for issue in _run_one(n, fn, cfg, offline)]
    return sort_issues(issues)


def run_startup_validators(cfg: HernessConfig) -> list[ConfigIssue]:
    """F10-01 step 3a: run owners offline, log each issue, raise ``ConfigError`` on an error."""
    return _enforce(run_owner_validators(cfg, offline=True), "owner validation failed")
