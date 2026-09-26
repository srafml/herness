-- Migration 090 (impl 09 U09-43, §4.1; R-11): implementation-only chat objects.
-- The tables chat_session, chat_message and their other indexes come from migration 005.
-- Applied files never change (checksum, TH02-15): a fix is a new migration.

-- One assistant row per user turn (meta.reply_to); upsert_assistant_placeholder relies on it.
CREATE UNIQUE INDEX IF NOT EXISTS chat_message_reply
    ON chat_message (session_id, json_extract(meta, '$.reply_to'))
    WHERE role = 'assistant';

-- Last message the rolling summary covers (set_chat_summary, U09-109); nullable.
ALTER TABLE chat_session ADD COLUMN summary_through_message_id TEXT;
