"""Build SQL file discovery, sandboxed Jinja rendering and SQL filters (impl 02 U02-82 … U02-86).

Templates are loaded only from the SQL directory by a ``SandboxedEnvironment`` with
``StrictUndefined`` (TH02-11). A value becomes SQL text only through the filters ``ident``,
``sqlstr``, ``num``, ``sqldate`` and the global ``raw`` (ENG §3.5, TH02-10). Error messages
name files, filter rules and exception classes, never the offending value.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import datetime
import functools
import math
import os
import re
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Final, Literal

import jinja2
from jinja2.environment import TemplateModule
from jinja2.runtime import LoopContext, Macro
from jinja2.sandbox import SandboxedEnvironment
from jinja2.utils import Namespace
from pydantic import BaseModel

from herness.core.errors import ConfigError
from herness.model.lakeinfo import EntityInventory, LakeInventory
from herness.model.render_context import RenderContext

type Stage = Literal["setup", "staging", "core", "attach", "facts", "dq"]

_SQL_DIR: Final = Path(__file__).parent / "sql"
_NAME_RE: Final = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
_IDENT_RE: Final = re.compile(r"^[a-z_][a-z0-9_]{0,127}$")
_SQLSTR_MAX: Final = 1024
_FIRST_PRINTABLE: Final = 0x20
_SQL_TYPES: Final = frozenset({"VARCHAR", "BOOLEAN", "DOUBLE", "BIGINT", "TIMESTAMPTZ"})
_STAGES: Final[tuple[tuple[int, int, Stage], ...]] = (
    (0, 99, "setup"),
    (100, 199, "staging"),
    (200, 299, "core"),
    (300, 399, "attach"),
    (400, 499, "facts"),
    (900, 999, "dq"),
)

# Attribute allowlist of the sandbox (TH02-11): everything else is unsafe, including the
# ``pathlib.Path`` behind ``lake.root`` (templates get ``raw_root`` as a string instead).
_ALLOWED_ATTRS: Final[tuple[tuple[type, frozenset[str]], ...]] = (
    (LakeInventory, frozenset({"get", "entities", "from_synth"})),
    (EntityInventory, frozenset(f.name for f in dataclasses.fields(EntityInventory))),
)
_MAPPING_READS: Final = frozenset({"get", "keys", "values", "items"})
_JINJA_RUNTIME: Final = (LoopContext, Macro, TemplateModule, Namespace)
# Runtime failures inside a render that become ConfigError (U02-85 Errors row).
_RENDER_ERRORS: Final = (
    jinja2.TemplateError,
    ConfigError,
    TypeError,
    ValueError,
    ArithmeticError,
    LookupError,
)

# The lake the ``raw`` global reads during one render (per thread or task).
_current_lake: contextvars.ContextVar[LakeInventory] = contextvars.ContextVar("herness_sql_lake")


def _stage_of(number: int) -> Stage | None:
    return next((stage for low, high, stage in _STAGES if low <= number <= high), None)


@dataclasses.dataclass(frozen=True, slots=True)
class SqlFile:
    """One numbered build SQL file; ``stage`` follows from ``number``."""

    number: int
    name: str
    path: Path
    stage: Stage = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        stage = _stage_of(self.number)
        if stage is None:
            msg = "build SQL number is outside every stage range"
            raise ValueError(msg)
        object.__setattr__(self, "stage", stage)


def discover_sql_files(*, sql_dir: Path | None = None) -> list[SqlFile]:
    """List the build SQL files of ``sql_dir`` (default ``herness/model/sql``) in run order.

    Names starting with ``_`` are ignored. Raises ConfigError for a name outside
    ``NNN_name.sql``, a number in 500-899, or a duplicate number.
    """
    directory = _SQL_DIR if sql_dir is None else sql_dir
    if not directory.is_dir():
        return []
    files: dict[int, SqlFile] = {}
    for path in sorted(directory.glob("*.sql")):
        if path.name.startswith("_"):
            continue
        match = _NAME_RE.fullmatch(path.name)
        if match is None or _stage_of(int(match.group(1))) is None:
            msg = f"unexpected build SQL file {path.name}"
            raise ConfigError(msg)
        number = int(match.group(1))
        if number in files:
            msg = f"duplicate build SQL number {number}"
            raise ConfigError(msg)
        files[number] = SqlFile(number=number, name=path.name, path=path)
    return sorted(files.values(), key=lambda f: f.name)


# --- filters (U02-86) --------------------------------------------------------------------


def _ident(value: object) -> str:
    if not isinstance(value, str) or _IDENT_RE.fullmatch(value) is None:
        msg = "invalid identifier"
        raise ConfigError(msg)
    return f'"{value}"'


def _sqlstr(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > _SQLSTR_MAX
        or any(ord(ch) < _FIRST_PRINTABLE for ch in value)
    ):
        msg = "invalid SQL string literal"
        raise ConfigError(msg)
    return "'" + value.replace("'", "''") + "'"


def _num(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        text = str(int(value))
    elif isinstance(value, float) and math.isfinite(value):
        text = repr(float(value))
    elif isinstance(value, Decimal) and value.is_finite():
        text = str(value)
    else:
        msg = "invalid number"
        raise ConfigError(msg)
    # a bare negative after a minus would start a ``--`` comment: ``5 -{{ -3|num }}``
    return f"({text})" if text.startswith("-") else text


def _sqldate(value: object) -> str:
    if not isinstance(value, datetime.date) or isinstance(value, datetime.datetime):
        msg = "invalid SQL date"
        raise ConfigError(msg)
    return f"DATE '{value.isoformat()}'"


def _raw(inventory: LakeInventory, source: str, entity: str, column: str, sqltype: str) -> str:
    quoted = _ident(column)
    if sqltype not in _SQL_TYPES:
        msg = "invalid SQL type for raw column"
        raise ConfigError(msg)
    if column in inventory.get(source, entity).columns:
        return quoted
    return f"CAST(NULL AS {sqltype})"


def _raw_global(source: str, entity: str, column: str, sqltype: str) -> str:
    return _raw(_current_lake.get(), source, entity, column, sqltype)


# --- rendering (U02-85) ------------------------------------------------------------------


class _SqlSandbox(SandboxedEnvironment):
    """``SandboxedEnvironment`` that also denies every attribute not on the allowlist."""

    def is_safe_attribute(self, obj: object, attr: str, value: object) -> bool:
        if not super().is_safe_attribute(obj, attr, value):
            return False
        if isinstance(obj, _JINJA_RUNTIME):
            return True
        if isinstance(obj, BaseModel):
            return attr in type(obj).model_fields
        for kind, names in _ALLOWED_ATTRS:
            if isinstance(obj, kind):
                return attr in names
        return isinstance(obj, Mapping) and attr in _MAPPING_READS


@functools.cache
def _environment(sql_dir: Path) -> SandboxedEnvironment:
    env = _SqlSandbox(
        loader=jinja2.FileSystemLoader(sql_dir),
        undefined=jinja2.StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters.update(ident=_ident, sqlstr=_sqlstr, num=_num, sqldate=_sqldate)
    env.globals["raw"] = _raw_global
    return env


@contextlib.contextmanager
def _bound_lake(lake: LakeInventory) -> Iterator[None]:
    token = _current_lake.set(lake)
    try:
        yield
    finally:
        _current_lake.reset(token)


def _template_line(tb: TracebackType | None, sql_dir: Path) -> int:
    """Line of the innermost traceback frame that belongs to a template of ``sql_dir``."""
    root = os.path.normcase(os.path.abspath(sql_dir))
    line = 0
    while tb is not None:
        filename = os.path.normcase(os.path.abspath(tb.tb_frame.f_code.co_filename))
        if os.path.dirname(filename) == root:
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def render_sql(file: SqlFile, context: RenderContext) -> str:
    """Render ``file`` with ``context``; any template or filter failure is a ConfigError."""
    sql_dir = file.path.parent
    env = _environment(sql_dir)
    try:
        with _bound_lake(context.lake):
            return env.get_template(file.name).render(context.template_vars())
    except _RENDER_ERRORS as exc:
        if isinstance(exc, jinja2.TemplateSyntaxError):
            line = exc.lineno
        else:
            line = _template_line(exc.__traceback__, sql_dir)
        msg = f"render failed for {file.name}: {type(exc).__name__} at line {line}"
        raise ConfigError(msg) from exc
