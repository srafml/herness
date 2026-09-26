"""Public ``herness.store.ops`` namespace (impl 02 U02-62, §2.3, R-08).

Re-exports only. Each area adds one block of ``from .<area> import`` lines and one block of
``__all__`` names, headed by a comment with the owning spec number and area name, in the
import order of impl 02 §2.3 rule 5: ``core``, ``migrate``, ``shared``, then the other areas
in the row order of the area table. ``herness.store.ops.<name>`` and
``herness.store.ops.<area>.<name>`` are the same object. Importing this package opens no
connection.
"""

# 02 core
from .core import (
    OPS_JSON_MAX_BYTES,
    connection,
    dump_json,
    load_json,
    read_all,
    read_one,
    reset_connections,
    run_write,
)

# 02 migrate
from .migrate import (
    MIGRATION_RANGES,
    MigrationReport,
    migrate,
    ops_health,
    pending_migrations,
    schema_version,
)

# isort: split
# 09 chat
from .chat import (
    ChatMessageRow,
    ChatSessionRow,
    append_chat_message,
    count_user_turns,
    create_chat_session,
    find_assistant_message,
    get_chat_message,
    get_chat_session,
    latest_user_message,
    list_chat_messages,
    list_chat_sessions,
    purge_chat,
    set_chat_summary,
    update_chat_message,
    upsert_assistant_placeholder,
)

__all__ = [  # noqa: RUF022 - block order of impl 02 U02-62, not sorted
    # 02 core
    "connection",
    "run_write",
    "read_one",
    "read_all",
    "dump_json",
    "load_json",
    "reset_connections",
    "OPS_JSON_MAX_BYTES",
    # 02 migrate
    "MIGRATION_RANGES",
    "migrate",
    "pending_migrations",
    "schema_version",
    "ops_health",
    "MigrationReport",
    # 09 chat
    "create_chat_session",
    "append_chat_message",
    "update_chat_message",
    "upsert_assistant_placeholder",
    "latest_user_message",
    "get_chat_session",
    "list_chat_sessions",
    "list_chat_messages",
    "get_chat_message",
    "find_assistant_message",
    "count_user_turns",
    "set_chat_summary",
    "purge_chat",
    "ChatSessionRow",
    "ChatMessageRow",
]
