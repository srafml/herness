# T03-34 review — Purge and health (verify)

Worktree agent-aac28d982f57551bb, head 3edee17, base fb11c4a. Read-only review: no source edits, no git writes. Probes are under `.agent-tmp/T03-34-verify/`.

### Spec Compliance
- ❌ Issues found: C-1 (deleted vectors stay readable in older LanceDB versions; TH03-12/TH02-09, impl 02 F02-07 step 3).

| Row | Result | Evidence |
|---|---|---|
| U03-145 `purge_record` | ❌ | Steps 1–9 are implemented in the spec's order. Step 1 runs before any IO. H comes from vectors, CURRENT text_redacted and own labels. P comes from the pair index. Shared hashes are computed from vectors and CURRENT, and the cache is purged with `purge_hashes`. Labels of every kind, including `gold/_reviews`, are rewritten, the pair index is rewritten, and the logs are as the spec says. But the vector delete is soft (C-1). The literal invariant "a second call returns zeros" fails when the hash is shared (m-1). |
| U03-146 `health` | ✅ | Fixed codes only. Never raises. The memoized hash check runs under a lock. The probe is created and removed. No model is loaded. (m-3 covers the probe cleanup on failure.) |
| UT03-133 | ✅ | `test_enrich_purge.py:51,64,76`: vector removed, cache kept, `hashes_shared=1`. Covers sharing through vectors and through the warehouse, and label rows. |
| UT03-134 | ✅ | `test_enrich_purge.py:101` covers all stores, the gold WARNING, the INFO counts, no record_id in logs, and a second call that returns ZEROS. Lines 135–228 cover the change side, no stores, stale tmp files, malformed hashes, and the StoreBusy/SchemaViolation mapping. |
| UT03-135 | ✅ | `test_enrich_health.py:73` gives degraded/laya_degraded. Lines 80 and 87 give down/cache_not_writable. Also covered: ok with the probe removed, config_invalid, the memo with 8 threads, and the other degraded codes. |
| ST03-09 (purge half) | ✅ | `test_embed_stage_security.py:70`: SchemaViolation, no config or VectorStore access, no row deleted in any store, value not echoed. Two mutation probes both go RED: (a) the allowlist is removed from step 1; (b) the config is opened before validation (`.agent-tmp/T03-34-verify/test_probe_mut.py`). |
| ST03-14 | ✅ (the test is sound) | It is an integration test (`pytestmark = integration`) through the public facade. It re-reads with a NEW `VectorStore` connection and fresh `pq.read_table` calls, and covers two cache qsvs, every label kind, two pair parts, the shared hash HX (kept), and a second call that returns zeros. It checks only the latest LanceDB version, so it cannot see C-1. |

- ⚠️ Cannot verify here: the acceptance check "the spec 10 deletion-flow integration test calls `herness.enrich.purge_record`". `herness.admin.privacy` is absent, so this carries over to T10-29.
- ⚠️ The `herness doctor` call path does not exist yet (spec 10). The facade probes show that `herness.enrich.health()` resolves to the function in every import order tried.

### Gates (all run)
- `ruff check .`: All checks passed.
- `ruff format --check .`: 824 files already formatted.
- `mypy`: no issues (309 files).
- `lint-imports`: 13 contracts kept, 0 broken.
- `check_type_ownership`: exit 0.
- `check_module_size`: exit 0.
- `PYTHONUTF8=1 pytest tests/unit/enrich tests/integration/enrich -q -p no:logging`: **980 passed, 2 skipped**.
- Coverage on the card tests: purge.py 100% line and branch (155 statements, 28 branches); health.py 100% (62 statements, 12 branches).
- Sizes: purge.py 238/260, health.py 95/100, `__init__.py` 39/40. The §2 row reads 40 (docs line 75), in the same commit.
- Every test function carries its ID in its name and docstring. Every test file has a `pytestmark`.

### Strengths
- Step 1 validation is the first statement (`purge.py:211`). The SchemaViolation message never echoes the value, and the probes show it.
- It reuses `purge_hashes` and `replace_atomic` and does not reimplement them. Stale `.part-*.parquet.tmp` files are removed from the label and pair directories.
- Malformed label hashes never reach a filter (`_hashes`), so a bad row cannot wedge the purge.
- Logging is clean. `enrich.purge.completed` carries only counts; `gold_modified` carries only `question`. Error messages name no path and no id.
- Memory is untouched (R-54). Only `ticket_embedding` is opened. `ensure_tables()` only creates missing tables.
- The health memo key is (version, mtime_ns, size) under a lock. The 8-thread test proves one verify call.

### Issues

#### Critical (Must Fix)
**C-1. A purged record's vectors stay readable in older LanceDB versions.** `herness/enrich/purge.py:223-225`
- `table.delete` is a soft delete: it writes a new version. Nothing in the tree calls `VectorStore.purge_history` (`vectors.py:209`); a grep finds no caller.
- Probe `.agent-tmp/T03-34-verify/test_probe_history.py`: after `purge_record(INC1)` I opened a fresh `VectorStore`, called `list_versions()`, got [1, 2, 3], and ran `checkout(2)`. The table returned `['servicenow:incident:INC1', 'servicenow:incident:INC2']`, so the purged vector and its record_id are still readable.
- This defeats TH03-12 ("lingers in vectors") and TH02-09. Impl 02 F02-07 step 3 assigns this explicitly: "spec 03 `purge_record` → `VectorStore.delete_ids` + `purge_history`". U03-145 step 4 leaves it out, which is a spec inconsistency.
- **Fix:**
  - After the delete (when `embeddings > 0`, or always, so a retried step also cleans up), call `store.purge_history("ticket_embedding")`. Map its StoreBusy/SchemaViolation as the method already does.
  - Extend ST03-14 to check every version: `t.list_versions()`, then for each version `t.checkout(v)` and assert INC1 is absent. Or assert `len(list_versions()) == 1`.
  - Record a spec note against U03-145 step 4 citing F02-07 step 3. About 2 source lines (budget 238/260).

#### Important (Should Fix)
**I-1. The facade silently discards every assignment to `herness.enrich.health`.** `herness/enrich/__init__.py:28-36`
- The concern 1 approach is correct for production. Probes (`facade_probe.py`, `facade_probe2.py`) show `herness.enrich.health` stays the function after `import herness.enrich.health`, after `from herness.enrich import health`, after `importlib.reload(herness.enrich)` and after reloading the submodule. Pickling the function works. mypy is clean.
- But the setter swallows **every** value, not just the module the import system binds. As a result:
  - `monkeypatch.setattr(herness.enrich, "health", fake)` is a silent no-op: `e.health is fake` gives False.
  - `mock.patch("herness.enrich.health", fake)` is a no-op inside the block and then raises `AttributeError: property 'health' of '_Facade' object has no deleter` on exit.
- Spec 10 doctor tests are the obvious caller that will fake this function, so they will fail in confusing ways.
- The simpler alternatives in the brief do not work:
  - The submodule cannot set the attribute itself. `_find_and_load_unlocked` rebinds the parent attribute *after* the submodule runs.
  - Binding the function in `__getattr__` is order-dependent. If anything imports `herness.enrich.health` first, the attribute is the module and `__getattr__` is never called, so `herness.enrich.health()` raises TypeError.
  - A module subclass is therefore the right tool inside the §2 map. The only cleaner option is renaming the submodule, which is a §2 change.
- **Fix (recommended):** replace the property, setter and missing deleter with a narrow `__setattr__` (5 lines instead of 9, so the file drops to about 35/40):
  ```python
  class _Facade(types.ModuleType):
      """Keeps `health` the function: the import system binds the submodule on the package."""

      def __setattr__(self, name: str, value: object) -> None:
          if not (name == "health" and isinstance(value, types.ModuleType)):
              super().__setattr__(name, value)
  ```
  I simulated this in `.agent-tmp/T03-34-verify/sim/` and every case passes:
  - The function survives a submodule-first import.
  - `mock.patch` takes effect and restores.
  - `monkeypatch.setattr` takes effect and undoes.
  - The function survives a reload.
  - `importlib.import_module("herness.enrich.health")` still returns the module.
- Add a CV test that `monkeypatch.setattr(herness.enrich, "health", fake)` takes effect.
- Known and acceptable: `import herness.enrich.health as m` binds the function, not the module. That is inherent to any order-independent fix, and the test file already uses `importlib`. String-target `monkeypatch.setattr("herness.enrich.health.X", …)` cannot reach the module; use the module object instead. Document both in the module docstring. Fallback if the controller prefers no class trick: rename the submodule (for example `_health.py`) with a §2 spec note.

#### Minor (Nice to Have)
**m-1. Concern 4: a second call reports `hashes_shared = 1` (a literal deviation from "a second call returns zeros").** `purge.py:196,235`
- Probe `test_probe_idem.py` uses the UT03-133 setup and purges twice. The second call returns `{..., 'hashes_shared': 1}` with every deletion count 0. CURRENT `text_redacted` still holds INC1 until the next build (spec 10 step 5), so H still contains H1, and it is still shared.
- Impact is nil: nothing is deleted, and spec 10 only stores the counts. But the invariant row is explicit, and UT03-133 never purges twice.
- **Fix:** report only the shared hashes the record still held this call in vectors or own labels, e.g. `len(shared & (vector_hashes | own_label_hashes))`. UT03-133 cases 1 and 3 are unchanged. Case 2 (shared only through the warehouse) still deletes a vector with H1, so it stays 1. Add a second-call assertion to UT03-133.
- Or record a spec note that `hashes_shared` is informational and excluded from the idempotence invariant. The builder's report already lists this as a spec note; the controller should choose one.

**m-2. Concern 3: mixed OS-error mapping.** `purge.py:230-231`, through `cache.py:67-68`
- `purge_hashes` and `replace_atomic` raise **FatalError** for OS errors other than EACCES/EBUSY (for example ENOSPC or EIO), while purge.py's own `_io` maps every OSError to StoreBusy.
- U03-41's Errors row says "as U03-38", so the reused helper's mapping is spec-sanctioned. Step 6 mandates the reuse, so this is not a defect.
- **Fix:** keep it and record the spec note the builder already wrote (U03-145 Errors: IO → StoreBusy, or FatalError from U03-41/U03-38 for non-busy OS errors). A FatalError is the right outcome for a full disk.

**m-3. The health probe is not removed on a partial failure.** `herness/enrich/health.py:34-36`
- If `os.close` or `os.unlink` raises after `mkstemp` succeeded, the probe file stays. On Windows an antivirus lock can make the unlink fail, and `_attempt` turns the failure into `cache_not_writable`.
- **Fix:** `handle, probe = mkstemp(...)`, then `try: os.close(handle) finally: Path(probe).unlink(missing_ok=True)` (wrapping the unlink in `contextlib.suppress(OSError)` if needed). About 2 lines (95/100).

**m-4. Retry after a partial failure can lose vector-only hashes.** `purge.py:219-227`
- The spec order deletes the vectors (step 4) before `purge_hashes` and the label rewrite (steps 6–7). If those later steps fail and spec 10 retries, H no longer includes the hashes that came only from the deleted vectors.
- Low risk: CURRENT `text_redacted` normally still carries the same hash until the step 5 rebuild.
- **Fix (optional):** delete the vectors last. Compute `shared` with `content_hash IN (...) AND record_id <> '<id>'`; both parts are allowlisted. Or record a spec note.

**m-5. A pair label whose pair is no longer in the index survives.** `purge.py:162-164`
- Label rows with `record_id = "<incident>|<change>"` are dropped only through P, the pair index hashes. If an older pair index part was already overwritten or deleted, the pair label and its cache rows (pair text built from the purged record) remain.
- **Fix:** also treat label rows whose `record_id` starts with `"<id>|"` or ends with `"|<id>"` as the record's own rows, and add their hashes to the targets. Or record a spec gap against U03-145 step 3.

**m-6. A Parquet part missing an expected column gives an unmapped `KeyError`.** `purge.py:134,164,178,187`
- A label or pair part that lacks `record_id`, `incident_id` or `change_id` raises `KeyError` or `ArrowInvalid` outside `_io` (for example `table["incident_id"]`), so an exception outside the Errors row escapes.
- **Fix:** wrap the column access in `_io`, or catch `KeyError` → `SchemaViolation("unreadable <what> part")`.

**m-7. Carry-over: private import `herness.store.vectors._store_error`.** `purge.py:32`
- Same precedent as T03-07 M-3. Keep it as a carry-over to the T02-08 owner (public error mapper).

**m-8. `dir(herness.enrich)` lists none of the exports.** `__init__.py`
- No `__dir__` is defined. Cosmetic.

### Judgements on the builder's concerns
1. **Facade class:** correct for production, and the only order-independent option inside the §2 map. Narrow the setter as I-1 describes.
2. **"Calibration in use":** the builder reads it as "every statically known in-use decider version has a non-empty calibration for the current qsv; llm skipped". I accept this. It is the natural reading of "degraded when *no calibration file* for *any* decider version in use", and the other reading would almost never fire. Keep it as a spec note.
3. **FatalError on non-busy OS errors:** acceptable through U03-41/U03-38 (m-2). Record the spec note.
4. **Second-call `hashes_shared=1`:** a literal deviation with no effect (m-1). Apply the small fix or a spec note.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The code is careful, fully covered and passes every gate. ST03-09 is proven by mutation and ST03-14 re-reads through fresh handles. But a purged record's vectors remain readable through LanceDB version history because `purge_history` is never called, which defeats TH03-12. The facade setter also silently swallows test patches. Both fixes are a few lines.


---

## Re-review round 1 (head 05c4c3a, base 3edee17)

### Verdict: **Approved**

Every finding in scope is closed, and the fix introduces no new defect. Two notes remain; neither blocks.

### Gates (all run on the head)
- `ruff check .`: All checks passed.
- `ruff format --check .`: 824 files already formatted.
- `mypy`: no issues (309 files).
- `lint-imports`: 13 contracts kept, 0 broken.
- `check_type_ownership`: exit 0.
- `check_module_size`: exit 0.
- `PYTHONUTF8=1 pytest tests/unit/enrich tests/integration/enrich -q -p no:logging`: **986 passed, 2 skipped**.
- Card tests: 34 passed. Coverage is 100% line and branch for purge.py (164 statements, 28 branches) and health.py (64 statements, 12 branches).
- Sizes: purge.py 252/260, health.py 98/100, `__init__.py` **40/40** (the budget is now full).

### Findings
| ID | Status | Evidence |
|---|---|---|
| C-1 | ✅ closed | `purge.py:251` calls `store.purge_history(_TABLE)` on every call, after the delete, so a retried step also cleans up. `PurgeEnv.ids_in_every_version()` checks out every version through a fresh handle and is asserted in ST03-14 and UT03-134. Mutation probe (`purge_history` made a no-op, in `.agent-tmp/T03-34-verify/test_probe_fix1.py`): both ST03-14 and UT03-134 go **RED** (`INC1` found in an older version). A spec note is added under U03-145. |
| I-1 | ✅ closed | `__init__.py:34-37` uses a narrow `__setattr__` that drops only a ModuleType bound to `health`. I re-ran the facade probes on the head. The function survives importing the submodule first, `from herness.enrich import health`, and `importlib.reload(herness.enrich)`. `mock.patch("herness.enrich.health", fake)` takes effect and restores. `monkeypatch.setattr` takes effect and undoes. `mock.patch("herness.enrich.health.verify_model_dir")` still reaches the module. Pickling works. The new CV test covers the patch cases. |
| m-1 | ✅ closed | `hashes_shared = len(shared & held)` (`purge.py:248`). Both UT03-133 cases now purge twice and get ZEROS, including the case where `CURRENT` still lists INC1. |
| m-2 | ✅ parked | A spec note under U03-145 covers FatalError from the reused U03-41/U03-38 helpers. |
| m-3 | ✅ closed | `health.py:36-39` removes the probe in a `finally` block. The new UT03-135 test fails `os.close` and asserts the cache root is empty. |
| m-4 | ✅ closed | The vectors are deleted last (`purge.py:249-250`, after the cache, labels and pair index). `_shared` reads `(content_hash IN …) AND NOT (record_id IN …)` (`purge.py:207-210`). Both clauses are built by `lance_filter_in` from allowlisted values (the record_id regex `[A-Za-z0-9._-]` excludes `'` and `|`); no raw value is interpolated. Mutation probe (NOT clause removed): UT03-134 full-record, UT03-134 no-stores and the new retry test go **RED**. The UT03-133 shared case stays green, as expected. |
| m-5 | ✅ closed | `_pair_key` uses `starts_with("<id>|")` and `ends_with("|<id>")`; ids cannot contain `|`, so the match is exact per component. Those label rows are dropped, and their hashes go into the pair targets without the shared filter. The test covers both sides and keeps the near-miss `<id>x|…`. |
| m-6 | ✅ closed | `_io` maps `KeyError`, and the column access moved inside it (`purge.py:58,140-143,155,196-197`). Parametrized test for labels and pairs. |
| m-7 | ✅ parked | Carry-over to the T02-08 owner (a public LanceDB error mapper). |
| m-8 | ✅ closed | `__dir__` returns globals plus `__all__`; asserted in the CV test. |

### New observations (Minor, no action needed for this card)
- **n-1** `purge.py:251`: `purge_history` (`optimize` with `cleanup_older_than=0`) runs on every call, even when nothing was deleted, so it compacts the whole table each time. This is acceptable: the job is an exclusive maintenance job, and it keeps a retry after a failure in step 4 safe. Revisit only if purge volume grows.
- **n-2** Test-only edge case: after `monkeypatch.undo()`, `herness.enrich.__dict__["health"]` holds a fixed function object. A later `importlib.reload(herness.enrich.health)` in the same process therefore leaves the facade pointing at the old function. Production never reloads, so nothing to fix.

**Task quality:** Approved
**Reasoning:** C-1 and I-1 are closed and proven by mutation probes and facade probes. All in-scope Minors are fixed with tests, m-2 and m-7 are parked as agreed, and the gates and enrich suite are green.
