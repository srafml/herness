"""Fixtures for foundation unit tests (impl 00 §11)."""

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from structlog.typing import EventDict

from herness.core import _log_pipeline as lp
from herness.core.logging import configure_logging, reset_logging

if TYPE_CHECKING:
    from herness.core.resilience.settings import ResilienceConfig

SENTINEL = "SENTINEL-SECRET-9f3a"
RESILIENCE_YAML = Path(__file__).parent / "fixtures" / "resilience.yaml"


def _scrub(value: object) -> object:
    if isinstance(value, str):
        return value.replace(SENTINEL, "***")
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def sentinel_scrubber(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        event_dict[key] = _scrub(event_dict[key])
    return event_dict


@pytest.fixture
def configured_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """DEBUG logging to stderr and tmp_path; fails the test if any emit_failed line is written."""
    failures: list[str] = []
    write_emit_failed = lp._write_emit_failed

    def record(logger_name: str) -> None:
        failures.append(logger_name)
        write_emit_failed(logger_name)

    monkeypatch.setattr(lp, "_write_emit_failed", record)
    configure_logging(
        "DEBUG", log_dir=tmp_path, scrubber=sentinel_scrubber, strict_event_names=True
    )
    yield tmp_path
    reset_logging()
    assert not failures, "core.logging.emit_failed was written to stderr during the test"


@pytest.fixture
def cfg_default() -> "ResilienceConfig":
    """Design 08 §7 `config/resilience.yaml` (R-43, R-51, R-53 applied) as `ResilienceConfig`.

    Stands in for `cfg.resilience` until the spec 10 loader (T10-03) exists.
    """
    from herness.core.resilience.settings import ResilienceConfig  # noqa: PLC0415 - lazy

    return ResilienceConfig.model_validate(yaml.safe_load(RESILIENCE_YAML.read_text("utf-8")))
