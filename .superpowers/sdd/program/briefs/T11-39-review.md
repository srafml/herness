# Review: T11-39 ServiceNow departments and task SLA generators

Commit reviewed: 7276ef5 (`tools/synth/servicenow_aux.py`, `tests/unit/tools/synth/test_servicenow_aux.py`).

## Spec compliance

- Spec: docs/impl/11-testing-eval-synthetic-data.impl.md, U11-77 (line 578 area), UT11-113 (line 1719).
- ✅ `gen_departments`: one record per `OrgRow`, `sys_id`/`cost_center` from the org, `name` from the org, `parent` empty, `sys_updated_on` = `span_start(params)` (00:00:00Z), all fields `{value, display_value}` pairs via `servicenow_common.pair`/`ts_pair`. Verified against `tools/synth/servicenow_aux.py:15-30` and `tools/synth/servicenow_common.py` (`pair`, `ts_pair`, `span_start`).
- ✅ `gen_task_slas`: one row per incident with a non-empty `resolved_at.value`, tombstones (`__tombstone__` truthy) and open records (no `resolved_at`) skipped (`_is_resolved`, `servicenow_aux.py:33-37`). `sys_id` = `new_sys_id(rng)` = `rng.bytes(16).hex()` (32 lowercase hex, fresh per call — confirmed against `servicenow_common.py:61-62`). `task` = incident `sys_id`. `sla` = `f"P{priority} resolution"`; confirmed the `priority` field's `value` is `str(priority)` (e.g. `"1"`) in the real generator (`tools/synth/servicenow_incidents.py:174`), so the format matches "P1 resolution" etc. `stage` = `"completed"`. `has_breached` = `"true"` iff `made_sla.value == "false"` — confirmed `made_sla` is produced as `pair("false"/"true")` strings by `servicenow_incidents.py:187,357`. `sys_updated_on` copied verbatim from the incident.
- ✅ Timestamp format (U11-07): both functions reuse `servicenow_common.ts_pair`/`span_start`, the same internal `%Y-%m-%d %H:%M:%S` UTC format as the rest of the ServiceNow generators — no ad hoc formatting introduced.
- ✅ Tombstone interpretation: the dispatch pointed at "U11-21 tombstone marker around line 392," but line 392 is actually inside **U11-16** (`tools.synth.dirty.apply_dirty`, heading at line 383); U11-21 (line 465) is `write_service_costs`, unrelated. The tombstone shape the module implements — `{"__tombstone__": True, "key": ..., "deleted_at": ...}` — matches U11-16's algorithm text verbatim ("tombstone record marker `{"__tombstone__": true, key, deleted_at}`"). Correctly interpreted; the misattribution is only in the dispatch note (and is also repeated in the code's own docstring/comments — see Minor finding below).
- ✅ UT11-113 setup/expectations: test fixture builds P1–P5 resolved incidents (alternating `made_sla`), one open record, one tombstone; asserts one department per org, one `task_sla` per resolved incident, `has_breached` agreement both directions, no row for open/tombstone, empty-list edge case. All present and passing.
- ✅ Test ID / pytestmark conventions: module sets `pytestmark = pytest.mark.unit`; all 8 test functions named `test_ut11_113_...` with docstrings starting `"""UT11-113 ...`.
- ✅ Budget: `tools/synth/servicenow_aux.py` is 76 lines against a 150-line module-map budget; `check_module_size` picks it up automatically and passes (confirmed by re-running it).
- ✅ Coverage: re-ran `pytest tests/unit/tools/synth/test_servicenow_aux.py --cov=tools.synth.servicenow_aux --cov-branch` independently — 100% line (26/26), 100% branch (6/6), both above the 90%/85% gate.
- ✅ Re-ran `mypy --strict tools/synth/servicenow_aux.py` (clean), `ruff check` on both files (clean), `lint-imports` (13 kept, 0 broken).

No ⚠️ items — everything needed to verify the postconditions was checkable from the diff plus the referenced shared modules (`servicenow_common.py`, `servicenow_incidents.py`, `catalog_rows.py`).

## Findings

### Critical (Must Fix)
None.

### Important (Should Fix)
None.

### Minor (Nice to Have)
- `tools/synth/servicenow_aux.py:10-13` (module docstring) and the builder report both cite the tombstone algorithm as "U11-21 `apply_dirty`". The actual unit is **U11-16** (`tools.synth.dirty.apply_dirty`, impl spec line 383); U11-21 is `write_service_costs` and is unrelated. The tombstone *shape* implemented is correct (matches U11-16's algorithm text), only the citation number in the comment is wrong. Low risk (comment-only), but worth a one-line fix so future readers aren't sent to the wrong unit.

## Verdict

**Approved.**

All U11-77 postconditions and UT11-113 expectations are met; the tombstone shape is correctly derived from the real spec algorithm (U11-16) despite the traceability-comment misnumbering; timestamp format, test-ID/pytestmark conventions, the 150-line budget, and the 90/85 coverage gates are all satisfied and independently re-verified. The one Minor citation-number fix does not block approval.
