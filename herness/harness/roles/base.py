"""Role definitions and the role table (impl 05 U05-49, U05-51; design 05 §5.5).

`RoleSpec` bundles a role's prompt files, tool allow-list, output model and sampling defaults.
System prompts come only from the package prompt files and code constants, never from task
text (TH05-21); allow-lists are frozenset constants in code, not config (TH05-07). Roles never
call a model client: they only build the system blocks and the first user message.
`get_role` imports the role modules lazily, so they may import this module.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import importlib
import importlib.util
import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Final, Literal, cast

from pydantic import BaseModel, JsonValue

from herness.core.errors import ConfigError
from herness.core.ids import canonical_json
from herness.core.types import LoopCheckpoint, Message, SystemBlock, TextPart, ToolContext
from herness.harness.llm.settings import RoleParams
from herness.harness.tools import wrap_untrusted
from herness.metrics.catalog import load_catalog

__all__ = ["ROLE_NAMES", "RoleSpec", "get_role"]

ROLE_NAMES: Final = (
    "planner",
    "judge",
    "analyst_ops",
    "analyst_change",
    "analyst_delivery",
    "analyst_org",
    "analyst_crosscheck",
    "analyst_retrospective",
    "analyst_general",
    "skeptic",
    "writer",
    "chat",
)
_MAX_BLOCK_ONE_CHARS: Final = 60_000
_RESUMED = "This task restarted after an interruption; continue from the state below."
# role name -> (module under herness.harness.roles, constant); analysts go through analyst_role
_LOCATIONS: Final[dict[str, tuple[str, str]]] = {
    "planner": ("planner", "PLANNER"),
    "judge": ("judge", "JUDGE"),
    "skeptic": ("skeptic", "SKEPTIC"),
    "writer": ("writer", "WRITER"),
    "chat": ("chat", "CHAT"),
}
_MODEL_ROLES: Final[Mapping[str, frozenset[str]]] = {
    "skeptic": frozenset({"skeptic", "skeptic_final"}),
    "chat": frozenset({"chat", "chat_off_hours"}),
}


def _prompts_root() -> Traversable:
    """The one prompt resolver: `prompts/` in the `herness.harness.roles` package data."""
    return resources.files("herness.harness.roles") / "prompts"


def _catalog_describe() -> list[dict[str, object]]:
    """The metric catalog summaries (T04-03)."""
    return load_catalog().describe()


def _catalog_lines() -> list[str]:
    """One `name — unit, better, grains; description` line per enabled metric, by name."""
    rows = sorted((m for m in _catalog_describe() if m["enabled"]), key=lambda m: str(m["name"]))
    return [
        f"{m['name']} — {m['unit']}, {m['better']}, "
        f"{', '.join(map(str, cast('list[object]', m['grains'])))}; {m['description']}"
        for m in rows
    ]


@dataclass(frozen=True)
class RoleSpec:
    """One role (design §5.5); immutable, prompt text read once per instance."""

    name: str
    specialty: str | None
    prompt_files: tuple[str, ...]
    allowed_tools: frozenset[str]
    output_model: type[BaseModel] | None
    temperature: float | None
    effort: str | None
    thinking: Literal["off", "on", "auto"]
    model_role: str

    @functools.cached_property
    def _prompts(self) -> tuple[tuple[str, str], ...]:
        root = _prompts_root()
        contents: list[tuple[str, str]] = []
        for file in self.prompt_files:
            path = root / file
            if not path.is_file():
                msg = f"prompt file {file} missing"
                raise ConfigError(msg)
            contents.append((file, path.read_text(encoding="utf-8")))
        return tuple(contents)

    def prompt_text(self) -> str:
        """The prompt files joined with a blank line (`_common.md` first)."""
        return "\n\n".join(content for _file, content in self._prompts)

    @property
    def prompt_hash(self) -> str:
        """16 hex of SHA-256 over each file name and content (design §5.5 versioning)."""
        joined = "\n".join(f"{file}\n{content}" for file, content in self._prompts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    def fallback_params(self) -> RoleParams:
        """The role's sampling defaults when `models.yaml` has no `role_params` entry."""
        values = {"temperature": self.temperature, "effort": self.effort, "thinking": self.thinking}
        return RoleParams.model_validate(values)

    def system_blocks(self, ctx: ToolContext) -> list[SystemBlock]:
        """Block 1 (cached, no per-run value) and block 2 (per task, not cached)."""
        first = self.prompt_text()
        if self.output_model is not None:
            schema = canonical_json(self.output_model.model_json_schema())
            first += f"\n\n## Output schema\n{schema}"
        if "get_metric" in self.allowed_tools:
            first += "\n\n## Metric catalog\n" + "\n".join(_catalog_lines())
        if len(first) > _MAX_BLOCK_ONE_CHARS:
            msg = f"system block 1 of role {self.name} exceeds 60,000 chars"
            raise ConfigError(msg)
        second = "\n".join(
            (
                f"role: {self.name}",
                f"specialty: {ctx.specialty}",
                f"depth: {ctx.depth}",
                f"build: {ctx.build_id}",
                f"step budget: {ctx.budgets.max_steps}",
                f"token budget: {ctx.budgets.max_tokens}",
                f"tools: {', '.join(sorted(ctx.tool_names))}",
            )
        )
        return [SystemBlock(text=first, cache=True), SystemBlock(text=second, cache=False)]

    def render_task(
        self, task_input: Mapping[str, JsonValue], resume_from: LoopCheckpoint | None
    ) -> Message:
        """The first user message: the task as JSON, plus the resume state when resuming."""
        body = json.dumps(json.loads(canonical_json(task_input)), indent=2, sort_keys=True)
        task = TextPart(text=f"## Task\n{body}")
        if resume_from is None:
            return Message(role="user", parts=[task])
        return Message(role="user", parts=[task, TextPart(text=_resume_text(resume_from))])


def _resume_text(cp: LoopCheckpoint) -> str:
    """The resume part: ids already gathered or committed, and the escaped scratchpad (R-20)."""
    lines = [
        "## Resumed task",
        _RESUMED,
        f"Queries already gathered: {', '.join(cp.query_ids) or 'none'}",
        "Findings already committed (do not post them again): "
        + (", ".join(cp.finding_ids) or "none"),
    ]
    if cp.scratchpad is not None:
        lines += ["Scratchpad:", wrap_untrusted(cp.scratchpad, source="scratchpad")]
    return "\n".join(lines)


def _check_preconditions(name: str, variant: str, model_role: str | None) -> None:
    if name not in ROLE_NAMES:
        msg = f"unknown role {name[:64]}"
        raise ConfigError(msg)
    if variant != "default" and (variant, name) != ("retrospective", "writer"):
        msg = f"variant {variant[:32]} not allowed for role {name}"
        raise ConfigError(msg)
    if model_role is None:
        return
    default = "analyst" if name.startswith("analyst_") else name
    if model_role not in _MODEL_ROLES.get(name, frozenset({default})):
        msg = f"model role {model_role[:64]} not allowed for role {name}"
        raise ConfigError(msg)


def _lookup(name: str, variant: str) -> RoleSpec:
    analyst = name.startswith("analyst_")
    module, attr = ("analyst", "analyst_role") if analyst else _LOCATIONS[name]
    if variant == "retrospective":
        attr = "WRITER_RETROSPECTIVE"
    full = f"herness.harness.roles.{module}"
    found: object = None
    if importlib.util.find_spec(full) is not None:  # skeptic/writer/chat arrive with T05-20
        found = getattr(importlib.import_module(full), attr, None)
    if analyst and callable(found):
        found = found(name.removeprefix("analyst_"))
    if not isinstance(found, RoleSpec):
        msg = f"role {name} not available"
        raise ConfigError(msg)
    return found


def get_role(
    name: str,
    *,
    variant: Literal["default", "retrospective"] = "default",
    model_role: str | None = None,
) -> RoleSpec:
    """The `RoleSpec` for a spec 06 role name; `ConfigError` for a bad name or combination."""
    _check_preconditions(name, variant, model_role)
    role = _lookup(name, variant)
    if model_role is not None and model_role != role.model_role:
        return dataclasses.replace(role, model_role=model_role)
    return role
