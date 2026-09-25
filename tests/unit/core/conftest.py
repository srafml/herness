"""Fixtures for foundation unit tests (impl 00 §11)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from structlog.typing import EventDict

from herness.core.logging import configure_logging, reset_logging

SENTINEL = "SENTINEL-SECRET-9f3a"


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
def configured_logging(tmp_path: Path) -> Iterator[Path]:
    configure_logging(
        "DEBUG", log_dir=tmp_path, scrubber=sentinel_scrubber, strict_event_names=True
    )
    yield tmp_path
    reset_logging()
