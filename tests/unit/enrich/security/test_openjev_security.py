"""ST03-16 (TH03-14): the OpenJev API key never reaches exceptions or logs (T03-12).

The key is the synthetic value of R-67. The scripted server echoes it in every error body,
the worst case for a message that forwarded response text.
"""

from __future__ import annotations

import json
import traceback
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from tests.unit.enrich._openjev_support import JSON, QS, Reply, install, item, jev_env

from herness.core.errors import AuthError, HernessError, ModelUnavailable
from herness.core.resilience import ProcessState
from herness.core.resilience.classify import classify
from herness.enrich.deciders import openjev as oj
from herness.enrich.deciders.openjev import OpenJevDecider
from herness.enrich.settings import OpenJevSettings

pytestmark = pytest.mark.unit

__all__ = ["jev_env"]  # the fixture is used by name

_SYNTHETIC = "synthetic-openjev-key"


def _echo(code: int) -> Reply:
    body = {"detail": f"bad Authorization: Bearer {_SYNTHETIC}", "key": _SYNTHETIC}
    return code, json.dumps(body).encode(), JSON


def _exception_text(exc: BaseException) -> str:
    """Every rendering of `exc` and its chain: str, repr, args, notes, traceback."""
    parts = ["".join(traceback.format_exception(exc))]
    seen: BaseException | None = exc
    while seen is not None:
        parts += [str(seen), repr(seen), repr(seen.args), repr(getattr(seen, "__notes__", []))]
        if isinstance(seen, HernessError):
            parts.append(repr(vars(seen)))
        seen = seen.__cause__ or seen.__context__
    return "\n".join(parts)


@pytest.mark.parametrize(
    "case",
    [
        (401, AuthError, "decide"),
        (403, AuthError, "decide"),
        (500, ModelUnavailable, "decide"),
        (401, ModelUnavailable, "health"),
    ],
)
def test_st03_16_key_absent_from_exceptions_and_logs(
    jev_env: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[int, type[HernessError], str],
) -> None:
    """ST03-16 401 from OpenJev with the synthetic key set: key value absent everywhere."""
    code, error, call = case
    net = install(monkeypatch, lambda _body: _echo(code))
    decider = OpenJevDecider(
        OpenJevSettings(), api_key=SecretStr(_SYNTHETIC), image_tag="0.4.0", samples=None
    )
    run: Any = (lambda: decider.decide([item(1)], QS)) if call == "decide" else decider.health
    with capture_logs() as logs, pytest.raises(error) as info:
        run()
    with pytest.raises(HernessError):  # again with the normal renderers (breaker may be open)
        run()
    # the key was really sent, only in the Authorization header
    assert net.requests[0].headers["authorization"] == f"Bearer {_SYNTHETIC}"
    assert _SYNTHETIC not in _exception_text(info.value)
    assert _SYNTHETIC not in repr(logs)
    captured = capsys.readouterr()
    assert _SYNTHETIC not in captured.out + captured.err
    assert _SYNTHETIC not in repr(decider) + repr(vars(decider))


@pytest.mark.parametrize("code", [429, 500, 529, 404])
def test_st03_16_classify_mirror_has_no_body_or_request_headers(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """ST03-16 the status handed to classify carries no body, no request headers and only
    the Retry-After headers, so an echoed key cannot reach the error text."""
    seen: list[BaseException] = []
    real = classify

    def spy(exc: BaseException, *, family: Any) -> HernessError:
        seen.append(exc)
        return real(exc, family=family)

    monkeypatch.setattr(oj, "classify", spy)
    headers = {**JSON, "retry-after": "120", "x-echo": _SYNTHETIC}
    install(monkeypatch, lambda _body: (code, _echo(code)[1], headers))
    decider = OpenJevDecider(
        OpenJevSettings(), api_key=SecretStr(_SYNTHETIC), image_tag="0.4.0", samples=None
    )
    with capture_logs() as logs, pytest.raises(HernessError) as info:
        decider.decide([item(1)], QS)
    mirrors = [e for e in seen if isinstance(e, httpx.HTTPStatusError)]
    assert mirrors
    for mirror in mirrors:
        assert mirror.response.content == b""
        assert dict(mirror.response.headers) == {"retry-after": "120"}
        assert "authorization" not in mirror.request.headers
    assert _SYNTHETIC not in _exception_text(info.value)
    assert _SYNTHETIC not in repr(logs)
