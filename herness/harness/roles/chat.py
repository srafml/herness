"""The Chat role (impl 05 U05-50, U05-51; design 05 §5.5).

Chat answers one question with `ChatAnswer` (spec 06) as its output model; `get_role` swaps in
the `chat_off_hours` model role when the caller asks for it.
"""

from __future__ import annotations

from typing import Final

from herness.core.types import ChatAnswer
from herness.harness.roles.base import RoleSpec

__all__ = ["CHAT"]

CHAT: Final = RoleSpec(
    name="chat",
    specialty=None,
    prompt_files=("_common.md", "chat.md"),
    allowed_tools=frozenset(
        {
            "list_tables",
            "describe_table",
            "run_sql",
            "get_metric",
            "get_scores",
            "get_cluster",
            "get_record",
            "semantic_search",
            "list_findings",
            "recall_memory",
            "propose_memory",
            "escalate",
        }
    ),
    output_model=ChatAnswer,
    temperature=0.3,
    effort="medium",
    thinking="auto",
    model_role="chat",
)
