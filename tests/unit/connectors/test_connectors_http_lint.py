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
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

import herness.connectors

pytestmark = pytest.mark.unit

CONNECTORS: Final = Path(herness.connectors.__file__).resolve().parent
_HTTP_MODULES: Final = frozenset({"httpx", "httpx2"})  # httpx2: the egress client library
_CLIENTS: Final = frozenset({"Client", "AsyncClient", "HTTPTransport", "AsyncHTTPTransport"})
_CTOR_RE: Final = re.compile(r"\bhttpx2?\s*\.\s*(?:Async)?(?:Client|HTTPTransport)\s*\(")
_VERIFY_RE: Final = re.compile(r"\bverify\s*=\s*False\b")
_ALLOWED: Final = frozenset({"GET", "POST"})
_OTHER_VERBS: Final = frozenset(
    {"PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT", "QUERY"}
)
# client verb methods other than get/post (`.connect(` is e.g. duckdb.connect, not flagged)
_VERB_CALLS: Final = frozenset({"put", "patch", "delete", "head", "options", "query"})
_REQUEST_CALLS: Final = frozenset({"request", "stream", "build_request"})


@dataclass(frozen=True, slots=True)
class _Names:
    """Local names bound to an HTTP module and to its `Request` class (import aliases)."""

    modules: frozenset[str]
    requests: frozenset[str]


def _is_false(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _literal_method(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.upper()
    return None


def _root(module: str) -> str:
    return module.split(".", maxsplit=1)[0]


def _bound_names(tree: ast.AST) -> _Names:
    modules, requests = set(_HTTP_MODULES), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                a.asname for a in node.names if a.asname and _root(a.name) in _HTTP_MODULES
            )
        elif isinstance(node, ast.ImportFrom) and _root(node.module or "") in _HTTP_MODULES:
            requests.update(a.asname or a.name for a in node.names if a.name == "Request")
    return _Names(frozenset(modules), frozenset(requests))


def _import_findings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [f"import {a.name}" for a in node.names if _root(a.name) == "requests"]
    if isinstance(node, ast.ImportFrom) and node.module is not None:
        root = _root(node.module)
        if root == "requests":
            return [f"from {node.module} import"]
        if root in _HTTP_MODULES:
            return [f"from {root} import {a.name}" for a in node.names if a.name in _CLIENTS]
    return []


def _method_of(node: ast.Call) -> str | None:
    """The literal method of a request-style call: `method=` keyword, else the first arg."""
    keyword = next((k.value for k in node.keywords if k.arg == "method"), None)
    return _literal_method(keyword if keyword is not None else next(iter(node.args), None))


def _constructor_findings(node: ast.Call, names: _Names) -> list[str]:
    """`<http module alias>.Client(...)` etc. and `Request(<not GET/POST>)` constructions."""
    func, line = node.func, node.lineno
    owner = (
        func.value.id
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
        else None
    )
    if owner in names.modules and isinstance(func, ast.Attribute) and func.attr in _CLIENTS:
        return [f"{owner}.{func.attr}( at line {line}"]
    request = (
        owner in names.modules and isinstance(func, ast.Attribute) and func.attr == "Request"
    ) or (isinstance(func, ast.Name) and func.id in names.requests)
    if request and _method_of(node) not in _ALLOWED:
        return [f"Request() method {_method_of(node) or 'not a literal'} at line {line}"]
    return []


def _call_findings(node: ast.Call, names: _Names) -> list[str]:
    found = [
        f"verify=False at line {node.lineno}"
        for k in node.keywords
        if k.arg == "verify" and _is_false(k.value)
    ]
    found += _constructor_findings(node, names)
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if isinstance(func, ast.Attribute) and name in _VERB_CALLS:
        found.append(f".{name}( at line {node.lineno}")
    keyword = next((k.value for k in node.keywords if k.arg == "method"), None)
    if name in _REQUEST_CALLS:
        method = _method_of(node)
        if method not in _ALLOWED:
            found.append(f"{name}() method {method or 'not a literal'} at line {node.lineno}")
    elif keyword is not None and _literal_method(keyword) in _OTHER_VERBS:
        found.append(f"method={_literal_method(keyword)} at line {node.lineno}")
    return found


def scan_source(text: str) -> list[str]:
    """Findings of one module: banned constructor text, then AST findings."""
    found = [f"{m.group(0)!r} in text" for m in _CTOR_RE.finditer(text)]
    found += [f"{m.group(0)!r} in text" for m in _VERIFY_RE.finditer(text)]
    tree = ast.parse(text)
    names = _bound_names(tree)
    for node in ast.walk(tree):
        found += _import_findings(node)
        if isinstance(node, ast.Call):
            found += _call_findings(node, names)
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
        "def f(client):\n    client.query('/x', content=b'{}')\n",
        "def f(send):\n    send(url='/x', method='QUERY')\n",
        "import httpx2\nc = httpx2.Client()\n",
        "import httpx2\nt = httpx2.AsyncHTTPTransport()\n",
        "from httpx2 import AsyncClient\n",
        "import httpx as h\nc = h.Client()\n",
        "import httpx2 as x\nt = x.AsyncHTTPTransport()\n",
        "import httpx.foo as h\nc = h.AsyncClient()\n",
        "import httpx\ndef f(c):\n    c.send(httpx.Request('PUT', '/x'))\n",
        "import httpx2\ndef f(c):\n    c.send(httpx2.Request(method='DELETE', url='/x'))\n",
        "import httpx2 as x\ndef f(c, m):\n    c.send(x.Request(m, '/x'))\n",
        "from httpx2 import Request as R\ndef f(c):\n    c.send(R('PATCH', '/x'))\n",
        "from httpx import Request\ndef f(c):\n    c.send(Request('QUERY', '/x'))\n",
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
    client.send(httpx.Request("GET", "/e"))
    client.send(httpx.Request(method="POST", url="/f"))
"""
    assert scan_source(allowed) == []
