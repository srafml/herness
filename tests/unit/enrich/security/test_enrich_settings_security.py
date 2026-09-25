"""Security tests for herness.enrich.settings (ST03-17, TH03-15)."""

import pytest
from pydantic import ValidationError

from herness.enrich.settings import DecidersSettings, OpenJevSettings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8100",
        "http://openjev.example.com:8100",
        "http://127.0.0.1.example.com:8100",
        "http://127.0.0.1@10.0.0.5:8100",
        "ftp://127.0.0.1:8100",
        "http://[::ffff:10.0.0.5]:8100",
    ],
)
def test_st03_17_openjev_base_url_must_be_loopback(url: str) -> None:
    """ST03-17 a non-loopback deciders.openjev.base_url in models.yaml is a ValidationError."""
    with pytest.raises(ValidationError, match=r"openjev\.base_url"):
        DecidersSettings.model_validate({"openjev": {"base_url": url}})


@pytest.mark.parametrize("url", ["http://127.0.0.1:8100", "http://localhost:8100/v1"])
def test_st03_17_loopback_base_url_accepted(url: str) -> None:
    """ST03-17 the loopback hosts 127.0.0.1 and localhost are accepted."""
    assert OpenJevSettings.model_validate({"base_url": url}).base_url == url
