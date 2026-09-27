"""Static lint of `herness/connectors/` for HTTP clients and methods (impl 01 ST01-14; TH01-14,
TH01-15; R-06, ENG §2.1; T01-13).

Every connector gets its HTTP client from `herness.core.egress` (R-06), never builds one,
never disables TLS verification and issues only GET and POST (read-only source identity).
The scanner reads each module's text (the constructor names are banned anywhere, comments
included, as the threat row says) and its AST (aliased imports, `verify=False`, write verbs,
request calls whose method is not a literal GET or POST, `requests` imports). A self-test
proves the scanner flags a planted snippet of each kind and passes the allowed forms.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

import pytest

import herness.connectors

pytestmark = pytest.mark.unit

CONNECTORS: Final = Path(herness.connectors.__file__).resolve().parent
_CLIENTS: Final = ("Client", "AsyncClient", "HTTPTransport", "AsyncHTTPTransport")
_CTOR_RE: Final = re.compile(r"\bhttpx\s*\.\s*(?:Async)?(?:Client|HTTPTransport)\s*\(")
_VERIFY_RE: Final = re.compile(r"\bverify\s*=\s*False\b")
_ALLOWED: Final = frozenset({"GET", "POST"})
_OTHER_VERBS: Final = frozenset({"PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"})
# httpx client verb methods other than get/post (`.connect(` is e.g. duckdb.connect)
_VERB_CALLS: Final = frozenset({"put", "patch", "delete", "head", "options"})
_REQUEST_CALLS: Final = frozenset({"request", "stream", "build_request"})


def _is_false(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _literal_method(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.upper()
    return None


def _import_findings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [f"import {a.name}" for a in node.names if a.name.split(".")[0] == "requests"]
    if isinstance(node, ast.ImportFrom) and node.module is not None:
        root = node.module.split(".")[0]
        if root == "requests":
            return [f"from {node.module} import"]
        if root == "httpx":
            return [f"from httpx import {a.name}" for a in node.names if a.name in _CLIENTS]
    return []


def _call_findings(node: ast.Call) -> list[str]:
    found = [
        f"verify=False at line {node.lineno}"
        for k in node.keywords
        if k.arg == "verify" and _is_false(k.value)
    ]
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if isinstance(func, ast.Attribute) and name in _VERB_CALLS:
        found.append(f".{name}( at line {node.lineno}")
    keyword = next((k.value for k in node.keywords if k.arg == "method"), None)
    if name in _REQUEST_CALLS:
        method = _literal_method(keyword if keyword is not None else next(iter(node.args), None))
        if method not in _ALLOWED:
            found.append(f"{name}() method {method or 'not a literal'} at line {node.lineno}")
    elif keyword is not None and _literal_method(keyword) in _OTHER_VERBS:
        found.append(f"method={_literal_method(keyword)} at line {node.lineno}")
    return found


def scan_source(text: str) -> list[str]:
    """Findings of one module: banned constructor text, then AST findings."""
    found = [f"{m.group(0)!r} in text" for m in _CTOR_RE.finditer(text)]
    found += [f"{m.group(0)!r} in text" for m in _VERIFY_RE.finditer(text)]
    for node in ast.walk(ast.parse(text)):
        found += _import_findings(node)
        if isinstance(node, ast.Call):
            found += _call_findings(node)
    return found


def _modules() -> list[Path]:
    return sorted(CONNECTORS.rglob("*.py"))


def test_st01_14_connectors_open_no_client_and_use_only_get_post() -> None:
    """ST01-14 static scan of `herness/connectors/`: no httpx client or transport
    constructor, no `verify=False`, no HTTP method other than GET/POST, no `requests`."""
    modules = _modules()
    assert len(modules) >= 15, modules  # the whole package is scanned, not a subset
    findings = {
        path.relative_to(CONNECTORS).as_posix(): hits
        for path in modules
        if (hits := scan_source(path.read_text(encoding="utf-8")))
    }
    assert findings == {}


@pytest.mark.parametrize(
    "snippet",
    [
        "import httpx\nc = httpx.Client()\n",
        "import httpx\nc = httpx.AsyncClient(timeout=5)\n",
        "import httpx\nt = httpx.HTTPTransport(retries=1)\n",
        "import httpx\nt = httpx.AsyncHTTPTransport()\n",
        "# never call httpx.Client( here\n",
        "from httpx import Client\n",
        "from httpx import AsyncHTTPTransport as T\n",
        "def f(make):\n    return make(url, verify=False)\n",
        "import requests\n",
        "from requests.adapters import HTTPAdapter\n",
        "import requests.sessions as s\n",
        "def f(client):\n    client.put('/x', json={})\n",
        "def f(client):\n    client.patch('/x')\n",
        "def f(client):\n    client.delete('/x')\n",
        "def f(client):\n    client.head('/x')\n",
        "def f(client):\n    client.options('/x')\n",
        "def f(client):\n    client.request('PUT', '/x')\n",
        "def f(client):\n    client.request(method='delete', url='/x')\n",
        "def f(client, verb):\n    client.request(verb, '/x')\n",
        "def f(client):\n    client.stream('PATCH', '/x')\n",
        "def f(client):\n    client.build_request('DELETE', '/x')\n",
        "def f(send):\n    send(url='/x', method='PUT')\n",
    ],
)
def test_st01_14_scanner_flags_planted_snippet(snippet: str) -> None:
    """ST01-14 self-test: the scanner flags each banned form."""
    assert scan_source(snippet), snippet


def test_st01_14_scanner_passes_allowed_forms() -> None:
    """ST01-14 self-test: GET, POST (also as request/stream methods), `verify` set to a
    bundle path, `httpx.Timeout`, egress-built clients and the word "requests" pass."""
    allowed = """\
import httpx
from herness.core import egress

# deletion requests. are filtered before every write
def f(client: httpx.Client, bundle: str, auth: object) -> None:
    client.get("/a", params={"q": 1})
    client.post("/search", json={})
    client.request("GET", "/a")
    client.request(method="post", url="/b")
    client.stream("GET", "/c")
    client.build_request("POST", "/d")
    make(url="/x", verify=bundle, timeout=httpx.Timeout(5.0))
    choose(method="basic")
    egress.source_http_client("jira")
    if auth.method == "none":
        pass
"""
    assert scan_source(allowed) == []
