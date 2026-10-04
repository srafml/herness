# T05-26 report: Health

Status: DONE
Commit: c4ca22f feat(harness): harness health (T05-26), branch worktree-agent-ade13e717ba891ac3

## What was built
- `herness/harness/health.py` (94 lines, budget 100), exporting `harness_health` (U05-73):
  - `harness_health(registry: LLMRegistry, *, traces_dir: Path, warehouse_dir: Path) -> dict[str, JsonValue]`.
    Runs three independent checks (clients, traces, warehouse), each wrapped in its own
    `except Exception` (never raises), takes the worst status (`ok` < `degraded` < `down`) and
    joins the failing reasons with `"; "`.
  - Clients check: `clients = registry.health()`; a `down` client is one whose value
    `.startswith("down")`. Route clients = union of `registry.chain_for(r, d)` for
    `r in ("analyst", "chat")`, `d in ("fast", "standard", "deep")`, skipping a role/depth pair
    that raises `ConfigError`. All route clients down (and at least one route client exists) ->
    `"down"`, reason `"<sorted client keys joined by '/'>" + " clients down"` (e.g.
    `"analyst/chat clients down"` when the routed client keys are literally `analyst`/`chat`).
    Any other down client (in the route set once the all-down clients are removed, or outside
    it) -> `"degraded"`, reason `"client <key> down"` per key. `registry.health()` raising ->
    `"degraded"`, reason `"client health unavailable"`, `clients` returned as `{}`.
  - Traces check: creates and deletes a `tempfile.NamedTemporaryFile(dir=traces_dir)`; any
    failure (missing dir, `traces_dir` is a file, permissions) -> `"down"`, reason
    `"traces dir not writable"`.
  - Warehouse check: reads `warehouse_dir/CURRENT` bounded to 65 bytes (64 + 1, via a plain
    `open(...).read(65)`, never the whole file), decodes/strips it, validates it against
    `herness.harness.warehouse.BUILD_ID_RE` (the same public validator `verifier.py` already
    reuses), then `open_warehouse(build_id, warehouse_dir=warehouse_dir, sql=get_config().models.harness.sql)`
    (the same `sql` source `RerunCache`/warehouse tools use) and closes it. Any failure ->
    `"degraded"`, reason `"current warehouse unavailable"`.
  - No HTTP client of its own (`ruff`/import-linter confirm no `httpx`/`egress` import); every
    network check goes through `LLMRegistry.health()` per the controller ruling.
  - A failing check logs one WARNING `harness.health.check_failed` (`check`, `error_type` =
    exception type name only) via `herness.core.logging.get_logger`, itself wrapped in
    `contextlib.suppress(Exception)` so a broken logging pipeline can never make
    `harness_health` raise.
  - No pyproject.toml changes needed: `herness/harness/health.py` already has a module-map row
    (budget 100) in `docs/impl/05-harness-core.impl.md:104`, which `tools/check_module_size`
    reads directly; no import-linter contract or `check_type_ownership` entry was needed (the
    module only imports from `herness.core` and other `herness.harness` submodules, both
    already permitted by the existing layered contracts).
- `tests/unit/harness/test_health.py` (9 tests, all `UT05-123`, `pytestmark = pytest.mark.unit`):
  - `test_ut05_123_ok_path` - every client ok, traces writable, `CURRENT` valid -> `ok`, `""`.
  - `test_ut05_123_non_route_client_down_is_degraded` - a down client outside the analyst/chat
    routes -> `degraded`, `"client extra down"`.
  - `test_ut05_123_all_analyst_chat_route_clients_down_is_down` - both route clients down ->
    `down`, `"analyst/chat clients down"` (the config names the two route clients `analyst` and
    `chat` so the reason matches the controller ruling's example literally).
  - `test_ut05_123_r52_vllm_entry_below_context_window` - a vLLM client (`tokenizer:
    vllm_endpoint`) with a stubbed `/v1/models` body whose `max_model_len` is below
    `context_window`, through the same `reg.egress.loopback_http_client` /
    `reg.secrets.resolve` monkeypatch pattern `tests/unit/harness/test_llm_registry.py` uses for
    its own R-52 test -> that client's value starts with `"down"` and carries
    `"context_window 32768 above server max_model_len 4096"`; overall status `degraded` since
    the other route client is still ok.
  - `test_ut05_123_traces_dir_unwritable_is_down` - `traces_dir` pointed at an existing file
    (portable on Windows, avoids symlink/chmod tricks) -> `down`,
    `"traces dir not writable"`.
  - `test_ut05_123_missing_current_is_degraded` / `test_ut05_123_garbage_current_is_degraded` -
    no `CURRENT` file, and a `CURRENT` with non-build-id content -> `degraded`,
    `"current warehouse unavailable"`.
  - `test_ut05_123_registry_health_raising_is_degraded` - `registry.health()` raising with a
    message containing a secret-looking token and a path -> `degraded`,
    `"client health unavailable"`, `clients == {}`, and asserts none of the secret/path
    substrings appear anywhere in `str(result)`.
  - `test_ut05_123_warehouse_failure_never_leaks_secrets_or_paths` - `open_warehouse` monkeypatched
    to raise with an embedded `api_key=secret:...` and the warehouse path -> `degraded`,
    `"current warehouse unavailable"`, and asserts neither the secret ref nor the path string
    appear in `str(result)`.
  - Real `LLMRegistry` instances are used throughout (not fakes), built from a `ModelsConfig`
    with three `openai_compat` clients named `analyst`, `chat`, `extra` routed via
    `roles`/no `fallback`, so `chain_for` returns single-element chains and the down-client
    reason strings are literal and deterministic. A real warehouse build comes from
    `tests/support/tools_standin.make_build` / `BUILD_ID`, written under a `CURRENT` file.

## Deviations
- None from the brief/controller rulings. One judgment call: when the "all route clients down"
  case fires, any *other* down client outside the route set still gets its own
  `"client <key> down"` degraded reason appended (status stays `down`, since `down` outranks
  `degraded`); this wasn't explicit in the ruling but keeps every down client visible in the
  reason string without conflicting with the worst-of-checks rule.

## Commands and results
- `uv run pytest tests/unit/harness/test_health.py -q`: 9 passed.
- `uv run pytest tests/unit/harness -q`: 1090 passed, 1 skipped (pre-existing Windows symlink
  skip in `test_warehouse.py`, unrelated to this card).
- `uv run ruff format --check herness/harness/health.py tests/unit/harness/test_health.py`: clean.
- `uv run ruff check .`: all checks passed.
- `uv run mypy` (project scope: `herness`, `tools`; tests are outside mypy's configured `files`,
  same as the rest of the suite, e.g. `tests/unit/harness/test_llm_registry.py` also fails
  mypy in isolation for the same reason): no issues found in 244 source files.
- `uv run lint-imports`: 13 contracts kept, 0 broken.
- `uv run python -m tools.check_module_size`: no violations (94 lines <= budget 100).
- `uv run python -m tools.check_type_ownership`: no violations.

## Fix round 1

Review: D:\herness\.superpowers\sdd\program\briefs\T05-26-review.md (base b4d1054, head c4ca22f).
Assessment: "Needs fixes" - one Important gap (I1), two Minor notes addressed (M1, M3).

### I1 (Important) - clients check was only partly wrapped
`_client_checks` previously wrapped only `registry.health()` in `try/except Exception`; the
down-set computation, the `chain_for` route union and the down/degraded comparison ran outside
that `try`, so a `chain_for` call raising anything other than `ConfigError`, or a `health()`
result with a non-`str` value (`AttributeError` from `.startswith`), escaped `harness_health`
entirely - violating U05-73 "Errors: none escape". Fixed by moving the whole body (the
`down`/`route`/`checks` computation) inside the same `try` block that already wraps
`registry.health()`, so a single `except Exception  # noqa: BLE001` (citing U05-73) now covers
every step of the clients check; on failure it returns `{}`, `[("degraded", "client health
unavailable")]`, same as before. Added two tests:
- `test_ut05_123_chain_for_raising_non_config_error_is_degraded` - `chain_for` monkeypatched to
  raise `RuntimeError("boom sk-secret")` -> degraded, `clients == {}`, and `"sk-secret"` is
  never in `str(result)`.
- `test_ut05_123_health_with_non_str_value_is_degraded` - `health()` returns `{"a": None}` ->
  degraded, `clients == {}`, no exception escapes.

### M1 (Minor) - CURRENT could be a POSIX FIFO
`_warehouse_check` now calls `current_path.is_file()` before opening `CURRENT`; a FIFO (or any
non-regular file) fails that check and takes the same `degraded` / "current warehouse
unavailable" path instead of blocking on `open("rb")` waiting for a writer. No new test was
practical for the FIFO case itself (POSIX-only, `os.mkfifo` unavailable on the Windows CI
runner this card was built on); the existing `test_ut05_123_missing_current_is_degraded` and
`test_ut05_123_garbage_current_is_degraded` exercise the same `is_file()` guard's `False`
branch (missing path also fails `is_file()`).

### M3 (Minor) - route union across depths/fallback chains untested
Added `_config_with_fallback()` (an `analyst` fallback chain to `analyst_fallback`, and a
`deep`-depth override routing `chat` to `chat_deep`) plus three tests:
- `test_ut05_123_route_union_spans_fallback_and_depth_overrides` - `analyst`/`chat` (the
  fast/standard heads) down, but `analyst_fallback`/`chat_deep` (only reachable via the fallback
  chain / depth override) still up -> `degraded`, not `down`, proving the union is not just the
  three depth heads.
- `test_ut05_123_route_union_all_members_down_is_down` - all four route-union members down ->
  `down`, reason `"analyst/analyst_fallback/chat/chat_deep clients down"`.
- `test_ut05_123_all_route_down_still_lists_other_down_clients` - locks in the builder's
  accepted deviation: all route clients down plus one non-route client also down -> `down`,
  reason `"analyst/chat clients down; client extra down"`.

M2 (informational, no fix needed per the review) and the out-of-scope `open_warehouse` leak note
were left as-is, per the review's own guidance.

### Commands and results (fix round 1)
- `uv run pytest tests/unit/harness/test_health.py -q`: 14 passed (5 new).
- `uv run pytest tests/unit/harness -q`: 1095 passed, 1 skipped (pre-existing Windows symlink
  skip in `test_warehouse.py`, unrelated).
- `uv run ruff format --check` / `uv run ruff check .`: clean.
- `uv run mypy`: no issues found in 244 source files.
- `uv run lint-imports`: 13 contracts kept, 0 broken.
- `uv run python -m tools.check_module_size`: no violations (`health.py` 97 lines <= budget 100).
- `uv run python -m tools.check_type_ownership`: no violations.
