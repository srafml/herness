"""LLM script format, loader and matcher for the scripted fake drivers (U11-37, U11-38).

One script format serves the in-process client, the loopback HTTP stub and the
`--mock-llm` registry (design 11 §5.2). Loading is hardened against hostile YAML
(TH11-08): a 256 KB size cap per file, a 500-script cap, `yaml.safe_load` semantics
and an expanded-node budget that rejects alias bombs and cyclic anchors before any
Python object is built.
"""

from __future__ import annotations

import fnmatch
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from herness.core.errors import ConfigError, FatalError

MAX_SCRIPT_BYTES: Final = 256 * 1024
MAX_SCRIPTS: Final = 500
MAX_TURNS: Final = 200
MAX_EXPANDED_NODES: Final = 100_000
_SUFFIXES: Final = frozenset({".yaml", ".yml"})

type FaultKind = Literal["http_500", "http_429", "malformed_json", "hang", "disconnect"]
type ActionKind = Literal["turn", "fault"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ScriptMatch(_Model):
    """Which calls a script answers: exact role/model role or `*`, dedup key glob."""

    role: str = "*"
    model_role: str = "*"
    dedup_key: str = "*"


class ToolCallSpec(_Model):
    """One scripted tool call: tool name and its arguments."""

    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


def _check_final(final: Mapping[str, Any]) -> None:
    if len(final) != 1:
        msg = "final must have exactly one key: output, text or a tool name"
        raise ValueError(msg)
    ((key, value),) = final.items()
    if key == "text":
        if not isinstance(value, str):
            msg = "final.text must be a string"
            raise ValueError(msg)
    elif not isinstance(value, Mapping):
        msg = f"final.{key} must be a mapping"
        raise ValueError(msg)


class ScriptTurn(_Model):
    """One model reply: exactly one of `tool_calls` or `final`."""

    tool_calls: Annotated[list[ToolCallSpec], Field(min_length=1)] | None = None
    final: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ScriptTurn:
        if (self.tool_calls is None) == (self.final is None):
            msg = "a turn needs exactly one of tool_calls or final"
            raise ValueError(msg)
        if self.final is not None:
            _check_final(self.final)
        return self


class ScriptFault(_Model):
    """A fault served instead of a turn for calls `at <= call_index < at + count`."""

    at: int = Field(ge=0)
    kind: FaultKind
    count: int = Field(default=1, ge=1, le=100)


class LLMScript(_Model):
    """A scripted conversation; `source` is `<file>#<index>` set by the loader."""

    match: ScriptMatch = Field(default_factory=ScriptMatch)
    turns: list[ScriptTurn] = Field(min_length=1, max_length=MAX_TURNS)
    faults: list[ScriptFault] = Field(default_factory=list)
    source: str = Field(min_length=1)


class ScriptMismatch(FatalError):  # noqa: N818 - name fixed by spec 11 U11-38
    """No script matches a call, or the matched script ran out of turns."""

    _extra_attrs: ClassVar[tuple[str, ...]] = (
        "role",
        "model_role",
        "dedup_key",
        "call_index",
        "prompt_hash",
    )
    role: str
    model_role: str
    dedup_key: str
    call_index: int
    prompt_hash: str

    def __init__(
        self,
        message: str,
        /,
        *,
        role: str,
        model_role: str,
        dedup_key: str,
        call_index: int,
        prompt_hash: str = "",
    ) -> None:
        super().__init__(message)
        self.role = role
        self.model_role = model_role
        self.dedup_key = dedup_key
        self.call_index = call_index
        self.prompt_hash = prompt_hash


@dataclass(frozen=True, slots=True)
class ScriptAction:
    """What to serve for one call: a turn of `script` or one of its faults."""

    kind: ActionKind
    script: LLMScript
    turn_index: int
    call_index: int
    fault: ScriptFault | None


class ScriptBook:
    """Ordered scripts plus per-`(role, dedup_key)` call and turn counters (U11-38)."""

    def __init__(self, scripts: Sequence[LLMScript]) -> None:
        self._scripts = tuple(scripts)
        self._lock = threading.Lock()
        self._calls: dict[tuple[str, str], int] = {}
        self._served: dict[tuple[str, str], int] = {}

    @property
    def scripts(self) -> tuple[LLMScript, ...]:
        """The scripts in match order."""
        return self._scripts

    def _find(self, role: str, model_role: str, dedup_key: str) -> LLMScript | None:
        for script in self._scripts:
            m = script.match
            if (
                m.role in {"*", role}
                and m.model_role in {"*", model_role}
                and fnmatch.fnmatchcase(dedup_key, m.dedup_key)
            ):
                return script
        return None

    def next_action(
        self, role: str, model_role: str, dedup_key: str, *, prompt_hash: str = ""
    ) -> ScriptAction:
        """Match the call to a script and return its next turn or active fault."""
        key = (role, dedup_key)
        ident = {"role": role, "model_role": model_role, "dedup_key": dedup_key}
        with self._lock:
            call_index = self._calls.get(key, 0)
            script = self._find(role, model_role, dedup_key)
            if script is None:
                msg = "no script matches the call"
                raise ScriptMismatch(msg, call_index=call_index, prompt_hash=prompt_hash, **ident)
            self._calls[key] = call_index + 1
            turn_index = self._served.get(key, 0)
            for fault in script.faults:
                if fault.at <= call_index < fault.at + fault.count:
                    return ScriptAction("fault", script, turn_index, call_index, fault)
            if turn_index >= len(script.turns):
                msg = "script exhausted"
                raise ScriptMismatch(msg, call_index=call_index, prompt_hash=prompt_hash, **ident)
            self._served[key] = turn_index + 1
            return ScriptAction("turn", script, turn_index, call_index, None)

    def calls(self, role: str, dedup_key: str) -> int:
        """Number of calls made so far for `(role, dedup_key)` (for tests)."""
        with self._lock:
            return self._calls.get((role, dedup_key), 0)


def _children(node: yaml.Node) -> list[yaml.Node]:
    if isinstance(node, yaml.SequenceNode):
        return list(node.value)
    if isinstance(node, yaml.MappingNode):
        return [part for pair in node.value for part in pair]
    return []


def _check_expansion(root: yaml.Node) -> None:
    """Reject cyclic anchors and alias expansions beyond `MAX_EXPANDED_NODES`."""
    sizes: dict[int, int] = {}
    open_nodes: set[int] = set()
    stack: list[tuple[yaml.Node, bool]] = [(root, False)]
    while stack:
        node, closing = stack.pop()
        ident = id(node)
        if closing:
            sizes[ident] = 1 + sum(sizes[id(child)] for child in _children(node))
            if sizes[ident] > MAX_EXPANDED_NODES:
                msg = f"document expands beyond {MAX_EXPANDED_NODES} nodes"
                raise ValueError(msg)
            continue
        if ident in sizes:
            continue
        if ident in open_nodes:
            msg = "document has a cyclic alias"
            raise ValueError(msg)
        open_nodes.add(ident)
        stack.append((node, True))
        stack.extend((child, False) for child in _children(node) if id(child) not in sizes)


def _parse(text: str) -> object:
    """`yaml.safe_load` with the expanded-node budget checked before construction."""
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        _check_expansion(node)
        return loader.construct_document(node)
    finally:
        loader.dispose()


def _read(file: Path) -> object:
    name = file.name
    size = file.stat().st_size
    if size > MAX_SCRIPT_BYTES:
        msg = f"{name}: script file is {size} bytes, over the {MAX_SCRIPT_BYTES} byte cap"
        raise ConfigError(msg, file=name)
    try:
        return _parse(file.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError, ValueError, RecursionError) as exc:
        msg = f"{name}: script file is not valid YAML"
        raise ConfigError(msg, hint=type(exc).__name__, file=name) from exc


def _validate(raw: object, name: str, index: int) -> LLMScript:
    source = f"{name}#{index}"
    if not isinstance(raw, dict):
        msg = f"{source}: a script must be a mapping"
        raise ConfigError(msg, file=name, index=index)
    try:
        return LLMScript.model_validate({**raw, "source": source})
    except ValidationError as exc:
        msg = f"{source}: script failed validation"
        raise ConfigError(msg, hint=str(exc), file=name, index=index) from exc


def _files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        found = [p for p in path.iterdir() if p.is_file() and p.suffix in _SUFFIXES]
        return sorted(found, key=lambda p: p.name)
    msg = "script path is not a file or directory"
    raise ConfigError(msg, path=str(path))


def load_scripts(path: Path) -> ScriptBook:
    """Load every script under `path` (a file or a directory of `.yaml`/`.yml` files)."""
    scripts: list[LLMScript] = []
    for file in _files(path):
        doc = _read(file)
        items = doc if isinstance(doc, list) else [doc]
        if len(scripts) + len(items) > MAX_SCRIPTS:
            msg = f"{file.name}: more than {MAX_SCRIPTS} scripts in total"
            raise ConfigError(msg, file=file.name)
        scripts.extend(_validate(raw, file.name, i) for i, raw in enumerate(items))
    return ScriptBook(scripts)
