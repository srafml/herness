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


@pytest.mark.parametrize("section", ["openjev", "jev"])
@pytest.mark.parametrize(
    ("url", "needle"),
    [
        (" {scheme}://{host}:8100", "whitespace or control"),
        ("{scheme}://{host}:8100 ", "whitespace or control"),
        ("{scheme}://{host}:8100\r\n", "whitespace or control"),
        ("{scheme}://{host}:81\t00", "whitespace or control"),
        ("{scheme}://{host}:8100/\x85x", "whitespace or control"),
        ("{scheme}://{host}:99999", "malformed port"),
        ("{scheme}://{host}:80x", "malformed port"),
    ],
)
def test_st03_17_base_url_rejects_whitespace_and_bad_port(
    section: str, url: str, needle: str
) -> None:
    """ST03-17 base URLs with whitespace, control characters or a bad port are rejected."""
    scheme, host = ("http", "127.0.0.1") if section == "openjev" else ("https", "api.typesafe.ai")
    bad = url.format(scheme=scheme, host=host)
    with pytest.raises(ValidationError, match=rf"{section}\.base_url .*{needle}") as caught:
        DecidersSettings.model_validate({section: {"base_url": bad}})
    assert bad not in str(caught.value)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8100", "http://localhost:8100/v1"])
def test_st03_17_loopback_base_url_accepted(url: str) -> None:
    """ST03-17 the loopback hosts 127.0.0.1 and localhost are accepted."""
    assert OpenJevSettings.model_validate({"base_url": url}).base_url == url
