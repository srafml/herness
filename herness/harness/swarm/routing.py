"""Role mapping, routing, hooks and tool context of one swarm task (impl 06 §3.10).

`role_prompt_name` maps a task to its spec 05 `RoleSpec.name`, `default_tools` gives the
least-privilege tool list of a role (spec 05 §5.5 allow-lists, TH06-02) and `role_budget` its
`TaskBudget` (§13 O06-01). `route_task` resolves the role spec, model role and client of a task
(TH06-06 off-network detection, hybrid local fallback) and `map_agent_result` maps an
`AgentResult` to `task.result`. `RunEnv`, `build_tool_context` and `build_hooks` live in the
private sibling `_routing_ctx`, `build_task_input` (U06-141) in `_task_input`; both are
re-exported here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from pydantic import ValidationError

from herness.core.errors import ConfigError
from herness.core.types import Depth, Role, Specialty, TaskBudget, TaskSpec
from herness.harness.pipelines.settings import DepthKnobs
from herness.harness.roles.base import get_role
from herness.harness.swarm._routing_ctx import RunEnv, build_hooks, build_tool_context
from herness.harness.swarm._task_input import build_task_input

if TYPE_CHECKING:
    from herness.core.types import AgentResult
    from herness.harness.budget import RunBudget
    from herness.harness.llm.base import LLMClient
    from herness.harness.llm.registry import LLMRegistry
    from herness.harness.llm.settings import ClientConfig
    from herness.harness.roles.base import RoleSpec

__all__ = [
    "BASE_MODEL_ROLE",
    "Route",
    "RunEnv",
    "build_hooks",
    "build_task_input",
    "build_tool_context",
    "default_tools",
    "map_agent_result",
    "role_budget",
    "role_prompt_name",
    "route_task",
]

SPAWN_TOOL: Final = "request_subtask"
_ANALYST: Final = frozenset(
    {
        "describe_table", "get_cluster", "get_metric", "get_record", "get_scores",
        "list_findings", "list_tables", "post_finding", "propose_memory", "recall_memory",
        SPAWN_TOOL, "run_sql", "semantic_search",
    }
)  # fmt: skip
_CROSSCHECK: Final = frozenset(
    {"describe_table", "get_metric", "get_scores", "list_tables", "post_finding", "run_sql"}
)
# Roles whose tool list does not depend on the specialty or the spawn depth.
_FIXED: Final[MappingProxyType[str, frozenset[str]]] = MappingProxyType(
    {
        "skeptic": frozenset(
            {"describe_table", "get_metric", "get_scores", "list_findings", "recall_memory"}
            | {"run_sql", "semantic_search"}
        ),
        "writer": frozenset({"get_metric", "get_scores", "list_findings", "recall_memory"}),
        "planner": frozenset(
            {"describe_table", "get_metric", "get_scores", "list_tables", "recall_memory"}
        ),
        "judge": frozenset(),
        "verifier": frozenset(),
    }
)
_JUDGE_BUDGET: Final = TaskBudget(max_steps=2, max_tokens=20_000, wall_clock_s=300)
# U06-101 names max_tokens=1; the in-tree TaskBudget (U06-04) floors max_tokens at 1,000, and a
# verifier task makes no model call, so its token limit is that floor (spec note in the report).
_VERIFIER_BUDGET: Final = TaskBudget(max_steps=1, max_tokens=1_000, wall_clock_s=900)
_MAX_WALL_CLOCK_S: Final = 86_400


def role_prompt_name(role: Role, specialty: Specialty) -> str:
    """Spec 05 `RoleSpec.name` of a task: `analyst_<specialty>`, other roles 1:1 (U06-97)."""
    return f"analyst_{specialty}" if role == "analyst" else role


def default_tools(
    role: Role, specialty: Specialty, depth_mode: Depth, *, child_depth: int, knobs: DepthKnobs
) -> list[str]:
    """Sorted tool names of one task of `role` (U06-101, TH06-02 least privilege).

    `request_subtask` is dropped when `child_depth >= knobs.max_spawn_depth` (fast mode has
    `max_spawn_depth = 0`, so `depth_mode` acts through `knobs` only). Chat tools depend on the
    chat mode (`CHAT_TOOLS`, design 06 §5.13), so `chat` raises `ConfigError`.
    """
    del depth_mode
    if role == "analyst":
        if specialty == "crosscheck":
            return sorted(_CROSSCHECK)
        drop = {SPAWN_TOOL} if child_depth >= knobs.max_spawn_depth else set()
        return sorted(_ANALYST - drop)
    tools = _FIXED.get(role)
    if tools is None:
        msg = f"role {role} has no default tools"
        raise ConfigError(msg)
    return sorted(tools)


def role_budget(role: Role, knobs: DepthKnobs, *, writer_tokens: int = 0) -> TaskBudget:
    """The `TaskBudget` of one task of `role` (U06-101, §13 O06-01); pure.

    The writer's token limit is the writer ledger (`writer_tokens` ≥ 1, else `ConfigError`); its
    wall clock is 4 times the analyst's, capped at the `TaskBudget` bound of one day.
    """
    analyst = knobs.analyst_budget
    if role in {"planner", "skeptic", "analyst"}:
        return analyst
    if role == "judge":
        return _JUDGE_BUDGET
    if role == "verifier":
        return _VERIFIER_BUDGET
    if role != "writer":
        msg = f"role {role} has no default budget"
        raise ConfigError(msg)
    if writer_tokens < 1:
        msg = "writer_tokens must be >= 1 for the writer budget"
        raise ConfigError(msg)
    wall = min(4 * analyst.wall_clock_s, _MAX_WALL_CLOCK_S)
    try:
        return TaskBudget(max_steps=analyst.max_steps, max_tokens=writer_tokens, wall_clock_s=wall)
    except ValidationError as exc:
        msg = "writer_tokens is below the TaskBudget token minimum"
        raise ConfigError(msg) from exc


# --- result mapping (U06-96)


def map_agent_result(
    res: AgentResult, *, subtasks: Sequence[str] = (), extra: Mapping[str, object] | None = None
) -> dict[str, object]:
    """The `task.result` of a spec 05 `AgentResult` (design 06 §4.1), merged with `extra`; pure.

    Input tokens include cache reads and writes; `stop_cause` is any spec 05 stop reason (R-22).
    """
    usage = res.usage
    result: dict[str, object] = {
        "summary": (res.output or {}).get("summary", ""),
        "finding_ids": list(res.finding_ids),
        "partial": res.status == "partial",
        "stop_cause": res.stop_reason,
        "tokens": {
            "input": usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
            "output": usage.output_tokens,
        },
        "cost_usd": str(res.cost_usd),
        "subtasks": list(subtasks),
    }
    return result | dict(extra or {})


# --- model routing (U06-98)

BASE_MODEL_ROLE: Final[MappingProxyType[str, str]] = MappingProxyType(
    {"skeptic_final": "skeptic", "chat_off_hours": "chat", "judge": "planner"}
)
_LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True, slots=True)
class Route:
    """Role spec, model role and client of one task (U06-98)."""

    role_spec: RoleSpec
    model_role: str
    client_key: str
    client: LLMClient
    config: ClientConfig
    off_network: bool
    fallback_local: bool


def _off_network(config: ClientConfig) -> bool:
    """TH06-06: anthropic, flagged off-network, or any host other than loopback."""
    if config.kind == "anthropic" or config.off_network:
        return True
    return urlsplit(config.base_url or "").hostname not in _LOOPBACK


def _route_key(llms: LLMRegistry, model_role: str, depth: Depth) -> tuple[str, str]:
    """`(model_role, key)`; a missing routing key of a derived role routes its base role."""
    try:
        return model_role, llms.model_for(model_role, depth)
    except ConfigError:
        base = BASE_MODEL_ROLE.get(model_role)
        if base is None:
            raise
        return base, llms.model_for(base, depth)


def _local_fallback(llms: LLMRegistry, model_role: str, depth: Depth) -> str:
    for key in llms.chain_for(model_role, depth):
        if not _off_network(llms.config(key)):
            return key
    msg = f"no local fallback for {model_role}"
    raise ConfigError(msg)


def route_task(
    spec: TaskSpec, *, llms: LLMRegistry, depth: Depth, profile: str, ledger: RunBudget
) -> Route:
    """Resolve role spec, model role and client of `spec` (design 06 §5.11, §5.12).

    In the `hybrid` profile an off-network route switches to the first local key of the model
    role's chain once the ledger's cost cap is reached (`fallback_local`, TH06-12).
    """
    role_spec = get_role(role_prompt_name(spec.role, spec.specialty))
    model_role, key = _route_key(llms, spec.model_role, depth)
    config = llms.config(key)
    off_network, fallback_local = _off_network(config), False
    if profile == "hybrid" and off_network and ledger.cost_cap_reached:
        key = _local_fallback(llms, model_role, depth)
        config, off_network, fallback_local = llms.config(key), False, True
    return Route(role_spec, model_role, key, llms.client(key), config, off_network, fallback_local)
