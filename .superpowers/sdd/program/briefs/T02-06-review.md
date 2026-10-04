# T02-06 review (relayed) — Needs fixes: 0 Critical / 1 Important / 3 Minor. DDL conforms column by column.
Important 1: tests/unit/store/ops/test_store_ops_migrations.py:236,246,253,269 — three UT02-37 rejection cases reuse PKs r1/s1 already inserted (267-268); PK conflict also raises SchemaViolation, so run enum/ts CHECKs and chat_session.user_ref length CHECK are untested. Fix: fresh ids and/or match="CHECK constraint failed".
Minor 1: :108 schema_migration SELECT lacks ORDER BY version.
Minor 2: test_store_ops_package.py:117-122 UT02-68 shared-block test vacuous until T02-07/T02-24.
Minor 3: IS NULL OR ts form on NOT NULL cols (spec-mandated, no action).
Spec-level: char vs byte caps (length() counts chars; 80,008-byte payload passes); memory_item implicit rowid + external-content FTS desync on rowid change/VACUUM; evidence.query_id GLOB loose; C8 impl 07 070 / impl 09 090 recreate 004/005 tables.
