# T03-34 report — Purge and health (build)

Status: DONE_WITH_CONCERNS (spec notes below; no budget overrun)
Worktree: D:\herness\.claude\worktrees\agent-aac28d982f57551bb (branch worktree-agent-aac28d982f57551bb), base fb11c4a
Commits: 13fa8cb wip(T03-34): purge_record, health, lazy facade and card tests; 3edee17 feat(enrich): purge_record and health (T03-34)

## Files (lines / budget)
- herness/enrich/purge.py 237/260 (new) — U03-145 purge_record
- herness/enrich/health.py 95/100 (new) — U03-146 health
- herness/enrich/__init__.py 39/40 (was 20/20) — lazy PEP 562 facade exporting embed_query, health, purge_record
- docs/impl/03-enrichment.impl.md §2 row `herness/enrich/__init__.py` budget 20 -> 40 (same commit as the facade, per ruling); no line shift (.secrets.baseline untouched)
- tests/unit/enrich/_purge_support.py (PurgeEnv: full config with paths.data=<tmp>/data, vectors, CURRENT warehouse, cache, labels, pair index helpers)
- tests/unit/enrich/test_enrich_purge.py — UT03-133 (3), UT03-134 (9)
- tests/unit/enrich/test_enrich_health.py — UT03-135 (9), CV T03-34 facade (2)
- tests/unit/enrich/security/test_embed_stage_security.py — ST03-09 purge_record half added; docstring carry-over note updated
- tests/integration/enrich/security/test_enrich_purge_security.py — ST03-14 (integration)
(Test files use unique basenames: test_health.py/test_purge.py collided with tests/unit/harness/test_health.py in the pytest-unit hook.)

## Tests
- RED: `PYTHONUTF8=1 uv run pytest <card tests>` -> 3 collection errors, `ImportError: cannot import name 'purge' from 'herness.enrich'` (purge/health absent).
- GREEN: card tests 28 passed; coverage purge.py 100% line/branch, health.py 100%, __init__.py 100%.
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich tests/integration/enrich -q -p no:logging`: 980 passed, 2 skipped (pre-existing symlink / laya CUDA skips).
- Fast gates: ruff check pass, ruff format --check pass (824), mypy 0 issues (309), lint-imports 13 kept 0 broken, check_type_ownership 0, check_module_size 0. Commits ran all hooks (no --no-verify, no PRE_COMMIT_ALLOW_NO_CONFIG).

## Implementation
purge_record(record_id): step 1 `lance_filter_in("record_id",[id])` before any IO (SchemaViolation, value never echoed; ST03-09 proves config/VectorStore never touched and no row in any store deleted). Paths from `EnrichPaths.from_config(get_config())`; vectors via `VectorStore()` (same default layout as embed_stage) + `ensure_tables()`; H = record's ticket_embedding hashes ∪ CURRENT `enrich.text_redacted` hash (open_readonly(None); NotFoundError -> none; bound parameters) ∪ hashes of label rows with record_id; P = pair index hashes where incident_id or change_id = record_id; vector delete `table.delete(lance_filter_in(...))` (count first); shared = H hashes another record has in vectors (lance_filter_in("content_hash", chunks of 1,000)) or CURRENT text_redacted; cache via `cache_maint.purge_hashes(paths, (H−shared)∪P)` (reused); labels of every version and kind incl. gold/_reviews rewritten (atomic via cache.replace_atomic, reused) dropping content_hash ∈ targets or record_id equal; `enrich.purge.gold_modified` WARNING per question of removed gold rows; pair index parts rewritten (emptied part deleted); stale `.part-*.parquet.tmp` in label/pair dirs deleted (TH03-12, as purge_hashes does); `enrich.purge.completed` INFO with the five counts only (no record_id anywhere in logs). ST03-14 re-reads with a new VectorStore connection and fresh Parquet readers; second call returns all zeros.
health(): config_invalid (get_config/EnrichPaths fail) -> down; probe file (tempfile.mkstemp + unlink) in data/cache, or in the data root while the cache root does not exist yet -> down cache_not_writable on failure / cache root not a directory; read_current + verify_model_dir memoized per (version, model.safetensors mtime_ns, size) under a lock -> laya_degraded; embedding_model_dir().is_dir() -> embedding_model_missing; calibration -> calibration_missing; ok. Each check's unexpected exception maps to that check's code; never raises; loads no model.

## Rulings applied / deviations
- __init__ 39/40 with §2 row 40 in the same commit; exports lazy (TYPE_CHECKING + __getattr__ + importlib). CV test runs a subprocess: importing herness.enrich loads none of herness.enrich.purge/health/embed or lancedb.
- Facade name clash (concern): the spec names both the submodule `herness/enrich/health.py` and the re-exported function `health`. The import system binds a submodule on its package after the first import, so `herness.enrich.health` would become the module (and `herness.enrich.health()` from doctor a TypeError) once anything imports `herness.enrich.health`. The facade sets the package's module class to a small ModuleType subclass whose `health` property returns the function and ignores the import system's setattr. Side effect: `from herness.enrich import health` and `import herness.enrich.health as m` yield the function; code needing the module uses `importlib.import_module("herness.enrich.health")` (the tests do). Alternative if rejected: rename the module (spec §2 change).
- Private import `herness.store.vectors._store_error` for LanceDB error mapping (T03-07 precedent, M-3 carry-over to T02-08 owner).
- Errors: own OS errors -> StoreBusy, LanceDB conflict/lock -> StoreBusy (other LanceDB failures SchemaViolation via _store_error), duckdb errors reading CURRENT -> StoreBusy, unreadable label/pair Parquet -> SchemaViolation. Reused helpers keep their own mapping: purge_hashes/replace_atomic raise StoreBusy for EACCES/EBUSY but FatalError for other OS errors (spec says IO -> StoreBusy) — spec note.
- Label hashes that are not 32-hex never reach a filter or purge_hashes (such rows are still removed by record_id) so a malformed label row cannot wedge the purge.
- An emptied label part is rewritten with zero rows (not deleted): LabelStore.read fails on a kind folder with no part (pyarrow dataset without schema). Emptied pair index parts are deleted.

## Spec notes (impl 03)
- U03-146 "no calibration file for any decider version in use": implemented as "every statically known decider version in use (primary_decider, per-question primaries, escalation_chain) has non-empty CalibrationStore.load for the current qsv"; versions: laya = CURRENT, openjev/jev = deciders.<name>.model when enabled; llm skipped (its version comes from the spec 05 role binding at run time; no model loaded). Reading "none of them has a file" would be the weaker alternative.
- U03-146 probe: when data/cache does not exist yet (fresh install) the data root is probed; health never creates the cache root.
- U03-145 idempotency: hashes_shared is recomputed each call; if CURRENT text_redacted still holds the purged record and its hash is shared, a second call reports hashes_shared = 1 (all deletion counts 0). Spec 10 order decides whether the warehouse row is gone first.
- U03-145 errors: reused helper FatalError for non-busy OS errors (see above).
- herness.enrich.health module/function name clash handled in the facade (see above).

## Carry-overs
- Spec 10 deletion-flow integration test calling herness.enrich.purge_record (acceptance check): no stand-in in the tree (herness.admin.privacy / run_privacy_delete absent) -> spec 10 card (T10-29).
- T02-08 owner: public LanceDB error mapper instead of private vectors._store_error.
- Closed: w18-s03a ST03-09 purge_record half (tests/unit/enrich/security/test_embed_stage_security.py).

## Fix round 1 (review briefs/T03-34-review.md; base 3edee17)
Commit: 05c4c3a fix(enrich): purge and health review round 1 (T03-34) (single commit; the work was finished before a checkpoint was needed)
Sizes: purge.py 252/260, health.py 98/100, __init__.py 40/40; check_module_size 0.
RED (`.agent-tmp/T03-34-build/red-fix1.txt`): 8 failed before the source change — ST03-14 + UT03-134 all-versions check (C-1), CV facade patch test (I-1), UT03-133 second-call zeros x2 (m-1), pair label without index row (m-5), pair part missing column (m-6), retry after failed cache purge (m-4). GREEN: card tests 34 passed; purge/health/__init__ 100% line+branch; tests/unit/enrich + tests/integration/enrich 986 passed, 2 skipped; fast gates clean.
- C-1 fixed: `store.purge_history("ticket_embedding")` after the vector delete (every call, so a retry also cleans); `PurgeEnv.ids_in_every_version()` checks out every LanceDB version through a fresh handle — ST03-14 and UT03-134 assert the record is absent from all versions. Spec note under U03-145 cites impl 02 F02-07 step 3.
- I-1 fixed: `_Facade.__setattr__` drops only a ModuleType bound to `health`; CV test: `mock.patch("herness.enrich.health", fake)` takes effect and restores, `monkeypatch.setattr` takes effect and undoes. Module docstring documents `importlib.import_module` for the module.
- m-1 fixed: `hashes_shared = len(shared & held)` (held = hashes found in its vectors or own labels this call); UT03-133 cases now purge twice and get ZEROS.
- m-3 fixed: probe removed in `finally` (`Path.unlink(missing_ok=True)`); UT03-135 test with a failing `os.close`.
- m-4 fixed: vectors deleted last (after cache, labels, pair index); `shared` from vectors read as `content_hash IN (...) AND NOT (record_id IN (...))` (both via lance_filter_in); UT03-134 retry test (purge_hashes fails, retry finishes).
- m-5 fixed: label rows with pair key `<id>|…` / `…|<id>` (pc.starts_with/ends_with with the separator; ids have no `|`, so exact components) are dropped and their hashes purged as pair hashes (never shared-filtered); UT03-134 test incl. a near-miss key `<id>x|…` kept.
- m-6 fixed: `_io` maps KeyError too and column access moved inside it -> SchemaViolation("unreadable <what> part"); parametrized UT03-134 test (labels, pairs).
- m-8 fixed: module `__dir__` lists globals + __all__.
- m-2 parked: spec note added (FatalError for non-busy OS errors from reused U03-41/U03-38 helpers).
- m-7 parked: private `herness.store.vectors._store_error` import -> carry-over to the T02-08 owner (public error mapper).
- Docs: two spec notes (U03-145, U03-146) added in docs/impl/03 (+4 lines); `.secrets.baseline` docs/impl/03 entry line_number 3614 -> 3618 (only change; LF kept; detect-secrets-hook rc 0).
