"""ST10-25: only the egress component builds HTTP clients and transports (impl 10, R-06).

An AST scan of ``herness/``, ``app/`` and ``tools/`` flags, outside
``herness/core/egress.py`` and ``herness/core/egress_clients.py`` (no connector allowance):

- any reference that resolves to an ``httpx`` or ``httpx2`` client or pool transport class
  (``Client``, ``AsyncClient``, ``HTTPTransport``, ``AsyncHTTPTransport``) or module-level
  request function (``request``, ``stream``, ``get``, ...): a call, an assignment
  (``C = httpx.Client``), a base class, a ``partial`` argument or a from-import;
- any use of the private modules ``httpx._*`` / ``httpx2._*``;
- any use of ``requests`` or ``urllib.request``;
- ``anthropic.Anthropic(`` / ``AsyncAnthropic(`` and ``openai.OpenAI(`` / ``AsyncOpenAI(``
  without ``http_client=``;
- ``from httpx import *`` / ``from httpx2 import *``.

Import aliases and module rebinding (``x = httpx2``) are resolved. Type annotations (argument,
return and variable annotations, string annotations included), except calls and walrus
expressions inside them, and ``if TYPE_CHECKING:`` blocks, where the flag resolves to
``typing.TYPE_CHECKING``, are not flagged: they build nothing (T10-17 review rulings I5, m1,
m2); imports there still bind names.
``getattr``/``importlib`` tricks are out of scope (the socket
guard, U10-58, holds those). Vendor SDK constructors (Snowflake, ``pymongo``, ``msal``) are
not flagged; ST10-55 holds their hosts.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("herness", "app", "tools")
ALLOWED = frozenset({"herness/core/egress.py", "herness/core/egress_clients.py"})
HTTP_PACKAGES = ("httpx", "httpx2")
_BUILDER_NAMES = ("Client", "AsyncClient", "HTTPTransport", "AsyncHTTPTransport")
_FUNCTIONS = ("request", "stream", "get", "post", "put", "patch", "delete", "head", "options")
BUILDERS = frozenset(
    f"{pkg}.{name}" for pkg in HTTP_PACKAGES for name in (*_BUILDER_NAMES, *_FUNCTIONS, "query")
)
SDK_CTORS = frozenset(
    {"anthropic.Anthropic", "anthropic.AsyncAnthropic", "openai.OpenAI", "openai.AsyncOpenAI"}
)
BANNED_MODULES = ("requests", "urllib.request")


def _forbidden(name: str) -> bool:
    """A builder, a private ``httpx``/``httpx2`` module or a banned module (or inside one)."""
    if name in BUILDERS or any(name.startswith(f"{pkg}._") for pkg in HTTP_PACKAGES):
        return True
    return any(name == mod or name.startswith(mod + ".") for mod in BANNED_MODULES)


class _Scanner(ast.NodeVisitor):
    """Collect ``(line, what)`` findings of one module."""

    def __init__(self, tree: ast.AST) -> None:
        self.aliases: dict[str, str] = {}
        self.findings: list[tuple[int, str]] = []
        self._quiet = False  # inside ``if TYPE_CHECKING:``: imports bind, nothing is flagged
        self._skip: set[int] = set()  # annotation nodes
        for node in ast.walk(tree):
            if isinstance(node, ast.arg | ast.AnnAssign) and node.annotation is not None:
                self._skip.add(id(node.annotation))
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns:
                self._skip.add(id(node.returns))

    def visit(self, node: ast.AST) -> None:
        if id(node) not in self._skip:
            super().visit(node)
            return
        stack = [node]  # an annotation: only a call or a walrus inside it can build anything
        while stack:
            sub = stack.pop()
            if isinstance(sub, ast.Call | ast.NamedExpr):
                super().visit(sub)
            else:
                stack.extend(ast.iter_child_nodes(sub))

    def _flag(self, line: int, what: str) -> None:
        if not self._quiet:
            self.findings.append((line, what))

    def visit_If(self, node: ast.If) -> None:
        if self._dotted(node.test) != "typing.TYPE_CHECKING":  # only the real flag
            self.generic_visit(node)
            return
        self._quiet = True
        for stmt in node.body:
            if isinstance(stmt, ast.Import | ast.ImportFrom):
                self.visit(stmt)
        self._quiet = False
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        bound = self._dotted(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if bound is None:
                    self.aliases.pop(target.id, None)
                else:  # ``x = httpx2`` or ``C = httpx.Client``: later uses resolve through it
                    self.aliases[target.id] = bound

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _forbidden(alias.name):
                self._flag(node.lineno, f"import {alias.name}")
            if alias.asname:
                self.aliases[alias.asname] = alias.name
            else:  # ``import a.b`` binds ``a``
                top = alias.name.split(".")[0]
                self.aliases[top] = top

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            full = f"{module}.{alias.name}" if module else alias.name
            star = alias.name == "*" and module.split(".")[0] in HTTP_PACKAGES
            if node.level == 0 and (star or _forbidden(module) or _forbidden(full)):
                self._flag(node.lineno, f"from {module} import {alias.name}")
            self.aliases[alias.asname or alias.name] = full

    def _dotted(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self._dotted(node.value)
            return None if base is None else f"{base}.{node.attr}"
        return None

    def visit_Name(self, node: ast.Name) -> None:
        name = self._dotted(node)
        if name is not None and _forbidden(name):
            self._flag(node.lineno, name)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = self._dotted(node)
        if name is not None and _forbidden(name):
            self._flag(node.lineno, name)
            return  # one finding per chain
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = self._dotted(node.func)
        if name in SDK_CTORS and not any(kw.arg == "http_client" for kw in node.keywords):
            self._flag(node.lineno, f"{name}( without http_client=")
        self.generic_visit(node)


def scan_source(source: str) -> list[tuple[int, str]]:
    """Findings of one module's source text."""
    tree = ast.parse(source)
    scanner = _Scanner(tree)
    scanner.visit(tree)
    return sorted(scanner.findings)


def _modules(root: Path) -> Iterator[tuple[str, Path]]:
    for top in ROOTS:
        base = root / top
        if base.is_dir():
            for path in sorted(base.rglob("*.py")):
                yield path.relative_to(root).as_posix(), path


def scan_tree(root: Path) -> list[str]:
    """Every finding under ``root`` outside the egress component, as ``path:line: what``."""
    return [
        f"{rel}:{line}: {what}"
        for rel, path in _modules(root)
        if rel not in ALLOWED
        for line, what in scan_source(path.read_text(encoding="utf-8"))
    ]


def test_st10_25_repository_passes() -> None:
    """ST10-25 no module outside the egress component builds an HTTP client or transport."""
    assert scan_tree(REPO) == []


def test_st10_25_egress_component_is_the_builder() -> None:
    """ST10-25 the scan does see the egress component's own clients (the rule is live)."""
    for rel in sorted(ALLOWED):
        found = {what for _line, what in scan_source((REPO / rel).read_text(encoding="utf-8"))}
        assert found & BUILDERS, rel


PLANTED = {
    "import httpx\nhttpx.Client()\n": "httpx.Client",
    "import httpx as hx\nhx.AsyncClient(timeout=1)\n": "httpx.AsyncClient",
    "import httpx as h\nC = h.Client\n": "httpx.Client",
    "from httpx import HTTPTransport\n": "from httpx import HTTPTransport",
    "from httpx import AsyncHTTPTransport as T\nT(retries=0)\n": "httpx.AsyncHTTPTransport",
    "import httpx\nC = httpx.Client\nC()\n": "httpx.Client",
    "import httpx\nclass X(httpx.Client):\n    pass\n": "httpx.Client",
    "import functools\nimport httpx\nf = functools.partial(httpx.Client)\n": "httpx.Client",
    "import httpx\nhttpx._client.Client()\n": "httpx._client.Client",
    "from httpx._client import Client\n": "from httpx._client import Client",
    "import httpx._transports.default\n": "import httpx._transports.default",
    "import httpx\nhttpx.get('https://x')\n": "httpx.get",
    "import httpx2\nhttpx2.AsyncClient()\n": "httpx2.AsyncClient",
    "import httpx2 as h2\nclass Y(h2.HTTPTransport):\n    pass\n": "httpx2.HTTPTransport",
    "from httpx2 import Client as C2\nC2()\n": "from httpx2 import Client",
    "import httpx2\nhttpx2.stream('GET', 'https://x')\n": "httpx2.stream",
    "import httpx2\nhttpx2._config.create_ssl_context()\n": "httpx2._config.create_ssl_context",
    "import requests\n": "import requests",
    "from requests import Session\n": "from requests import Session",
    "import urllib.request\n": "import urllib.request",
    "import urllib\nurllib.request.urlopen('http://x')\n": "urllib.request.urlopen",
    "from urllib import request\n": "from urllib import request",
    "from urllib.request import urlopen\n": "from urllib.request import urlopen",
    "import anthropic\nanthropic.Anthropic()\n": "anthropic.Anthropic( without http_client=",
    "import httpx2\ndef f(c: httpx2.Client()) -> None:\n    pass\n": "httpx2.Client",
    "import httpx2\nx: httpx2.Client() = 1\n": "httpx2.Client",
    "import httpx2\ndef f() -> (c := httpx2.AsyncClient()):\n    pass\n": "httpx2.AsyncClient",
    "TYPE_CHECKING = True\nif TYPE_CHECKING:\n    import httpx2\n    httpx2.Client()\n": (
        "httpx2.Client"
    ),
    "class K:\n    TYPE_CHECKING = True\nif K.TYPE_CHECKING:\n    import requests\n": (
        "import requests"
    ),
    "import openai\nopenai.AsyncOpenAI(api_key=k)\n": "openai.AsyncOpenAI( without http_client=",
    "from openai import OpenAI\nOpenAI()\n": "openai.OpenAI( without http_client=",
    "from httpx import *\n": "from httpx import *",
    "from httpx2 import *\n": "from httpx2 import *",
    "import httpx2\nx = httpx2\nx.Client(timeout=1)\n": "httpx2.Client",
    "import httpx2\nx = httpx2\ny = x\nclass Z(y.AsyncClient):\n    pass\n": "httpx2.AsyncClient",
    (
        "from typing import TYPE_CHECKING\nimport httpx2\nif TYPE_CHECKING:\n    pass\n"
        "def f() -> httpx2.AsyncClient:\n    return httpx2.AsyncClient()\n"
    ): "httpx2.AsyncClient",
    (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import httpx2\n"
        "else:\n    import httpx2\nhttpx2.Client()\n"
    ): "httpx2.Client",
    "from anthropic import AsyncAnthropic\nAsyncAnthropic(api_key=k)\n": (
        "anthropic.AsyncAnthropic( without http_client="
    ),
}


@pytest.mark.parametrize(("source", "expected"), list(PLANTED.items()))
def test_st10_25_planted_violation_fails(tmp_path: Path, source: str, expected: str) -> None:
    """ST10-25 a planted violation in a temp module is reported."""
    (tmp_path / "herness" / "connectors").mkdir(parents=True)
    (tmp_path / "herness" / "connectors" / "planted.py").write_text(source, encoding="utf-8")
    findings = scan_tree(tmp_path)
    assert findings, source
    assert all(f.startswith("herness/connectors/planted.py:") for f in findings)
    assert any(f.endswith(": " + expected) for f in findings), findings


@pytest.mark.parametrize(
    "source",
    [
        "import anthropic\nanthropic.AsyncAnthropic(http_client=c)\n",
        "import snowflake.connector\nsnowflake.connector.connect(account='a')\n",
        "import pymongo\npymongo.MongoClient('mongodb://h')\n",
        "import msal\nmsal.ConfidentialClientApplication('id')\n",
        "import httpx\nhttpx.URL('https://x')\nhttpx.Timeout(1.0)\n",
        "import httpx2\nclass T(httpx2.BaseTransport):\n    pass\nhttpx2.MockTransport\n",
        "import httpx\ntry:\n    pass\nexcept httpx.ConnectError:\n    pass\n",
        "import httpx2\ndef f(c: httpx2.AsyncClient) -> httpx2.Client:\n    x: httpx2.Client\n",
        "def f(c: 'httpx2.AsyncClient') -> 'httpx2.Client':\n    pass\n",
        (
            "import typing as t\nif t.TYPE_CHECKING:\n    import httpx2\n"
            "    from httpx2 import AsyncClient\n    C = httpx2.Client\n"
            "def f() -> AsyncClient:\n    pass\n"
        ),
        "import openai\nopenai.AsyncOpenAI(api_key=k, http_client=c)\n",
        "import httpx2\nx = httpx2\nx = 3\nx.Client()\n",
        "from herness.core.egress import loopback_http_client as lc\nlc('x', timeout_s=1)\n",
        "Client()\n",
    ],
)
def test_st10_25_allowed_constructs_pass(source: str) -> None:
    """ST10-25 vendor SDKs, anthropic with http_client= and httpx value types are not flagged."""
    assert scan_source(source) == []


def test_st10_25_egress_files_are_the_only_exemption(tmp_path: Path) -> None:
    """ST10-25 the same code inside herness/core/egress.py is exempt, elsewhere it is not."""
    body = "import httpx\nhttpx.Client()\n"
    for rel in ("herness/core/egress.py", "herness/core/egress_clients.py", "app/ui.py"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    assert scan_tree(tmp_path) == ["app/ui.py:2: httpx.Client"]


T05_08_SHAPE = """
from typing import TYPE_CHECKING, Protocol, cast

import anthropic

from herness.core import egress as _egress

if TYPE_CHECKING:
    import httpx2


class _GuardedClientFactory(Protocol):
    def async_http_client(
        self, purpose: str, payload_class: str, *, run_id: str | None, task_id: str | None,
        timeout: float,
    ) -> httpx2.AsyncClient: ...


class AnthropicClient:
    def _http_client(self, req: object) -> httpx2.AsyncClient:
        guard = _egress.get_guard()
        return cast("_GuardedClientFactory", guard).async_http_client(
            "reasoning", "aggregated_evidence", run_id=None, task_id=None, timeout=60.0
        )

    def _sdk(self, req: object) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key="k", max_retries=0, timeout=60.0, http_client=self._http_client(req)
        )
"""


def test_st10_25_t05_08_adapter_shape_passes() -> None:
    """ST10-25 T05-08's adapter shape (TYPE_CHECKING httpx2, annotations, http_client=) passes."""
    assert scan_source(T05_08_SHAPE) == []
    built = T05_08_SHAPE.replace("http_client=self._http_client(req)", "max_retries=1")
    assert [what for _l, what in scan_source(built)] == [
        "anthropic.AsyncAnthropic( without http_client="
    ]
