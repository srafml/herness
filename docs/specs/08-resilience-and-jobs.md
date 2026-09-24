# 08 — Resilience and Jobs

Status: Draft v2 · 2026-09-24 · Depends on: 00, 02, 05, 06, 10. Phase 3.

## 1. Purpose and scope

This spec defines how Herness survives failure and how it shares one 24 GB GPU. It covers two modules:

- `herness/core/resilience.py`: retry policies and timeouts, circuit breakers, execution of model and decider fallback chains, structured-output repair, the resilience side of the agent loop hooks, and fault-injection hooks.
- `herness/core/jobs.py`: the SQLite job queue, leases, scheduler, GPU arbitration and service control, chat-hours policy, the `herness worker` process, and the generic lease/resume helpers that spec 06 uses for the `task` table.

Out of scope: what each job does (handlers in §5.1), loop-signal detection and budget ledgers (05, 06), trace file format (05 §5.7), compose file and service install (10), CLI rendering (09).

## 2. Responsibilities

1. Classify every failure by the spec 00 §7 taxonomy and apply one policy per call type. No string matching on messages.
2. Keep sources, model endpoints and deciders from being hammered while down (breakers in `source_health`).
3. Execute the per-role fallback chains configured in `config/models.yaml` (spec 05), with the structured-output repair protocol.
4. Run all background work as leased jobs that survive process kills, reboots and GPU swaps without duplicate side effects.
5. Keep at most one GPU-heavy class loaded, following the schedule windows (D3, D4), and give jobs a safe in-job switch API.
6. Decide the chat mode at any moment (`jobs.chat_policy`, D4).
7. Make retries, fallbacks, breaker transitions, lease expiries and swaps visible in logs, traces, `resilience_event` and `herness status`.
8. Provide the fault hooks the spec 11 fault suite uses.

## 3. Interfaces

### 3.1 Retry, timeouts, breakers (`herness.core.resilience`)

```python
PolicyName = Literal["source_http_page", "llm_local", "llm_large", "llm_cloud", "decider_local",
                     "decider_cloud", "embed_batch", "tool_store", "warehouse_read", "sqlite_write", "gpu_health"]

@dataclass(frozen=True)
class RetryPolicy:
    name: str
    attempts: int                 # total tries, including the first
    base_s: float; cap_s: float   # full-jitter backoff
    max_elapsed_s: float
    timeout_s: float | None       # None = caller supplies it (LLM: model profile timeout_s, spec 05)
    connect_timeout_s: float | None
    retry_after_cap_s: float

def policy(name: PolicyName) -> RetryPolicy
def retrying(name: PolicyName, *, breaker_key: str | None = None) -> Callable[[F], F]   # sync or async
def retry_call(name: PolicyName, fn: Callable[..., T], *a, breaker_key: str | None = None, **kw) -> T
async def aretry_call(name: PolicyName, fn: Callable[..., Awaitable[T]], *a, breaker_key: str | None = None, **kw) -> T
def retry_page(fn: Callable[[], T], *, source: str) -> T      # spec 01: policy source_http_page, breaker <source>
def guard(key: str) -> None                                   # raises CircuitOpen if the breaker disallows (spec 01)
def call_with_timeout(fn: Callable[[], T], timeout_s: float, *, on_timeout: Callable[[], None] | None = None) -> T
def classify(exc: BaseException, *, family: Literal["source", "model", "decider", "store"]) -> HernessError

BreakerState = Literal["closed", "open", "half_open"]
class CircuitBreaker:
    key: str
    def allow(self) -> bool                  # False while open; True for exactly one probe caller when due
    def record_success(self) -> None
    def record_failure(self, err: HernessError) -> None
    def force_open(self, err: HernessError) -> None      # AuthError on a source (spec 01 §6)
    def state(self) -> BreakerState
def breaker(key: str) -> CircuitBreaker      # process-wide registry, persisted in source_health
```

`breaker_key` follows the `source_health.source` convention (spec 02 §5.1): `<connector>`, `model:<profile>`, `decider:<name>`.

### 3.2 Fallback execution and repair

```python
class ModelChain:                          # executes LLMRegistry.chain_for(model_role, depth) (spec 05)
    def __init__(self, model_role: str, *, registry: LLMRegistry, depth: str, gpu: GpuStateReader) -> None: ...
    def candidates(self) -> list[str]      # client keys after filtering (§5.4)
    async def acomplete(self, req: LLMRequest, *, schema: type[BaseModel] | None = None,
                        client_for: Callable[[str], LLMClient], tracer: Tracer | None = None
                        ) -> tuple[LLMResponse, BaseModel | None]
        # retry + repair + fallback; client_for(key) lets spec 05 wrap each client in GatedClient (spec 06 gate)

async def complete_validated(client: LLMClient, req: LLMRequest, *, max_repairs: int = 2,
                             tracer: Tracer | None = None) -> LLMResponse
    # validates against req.response_schema; raises OutputValidationError after max_repairs

class DeciderChain:                        # order from resilience.fallback.decider_chain[<active profile>]
    def __init__(self, order: Sequence[str], *, gpu: GpuStateReader) -> None: ...
    def decide(self, items: Sequence[DecisionInput], questions: QuestionSet
               ) -> tuple[list[DecisionOutput], list[DecisionInput]]      # (decided, deferred)

def loop_signal_policy(state: LoopState, signal: LoopSignal, *, tracer: Tracer | None = None
                       ) -> Literal["nudge", "stop"]
    # pure policy: first signal of a task → "nudge"; second → "stop" (emits guard_stop, writes resilience_event)
```

These are building blocks. The only hooks class is spec 05 `HarnessHooks`: its `call` runs `ModelChain.acomplete`, its `on_loop_signal` returns `loop_signal_policy(...)`, and its `after_step` calls `save_checkpoint` (§3.7). This spec defines no `LoopHooks` implementation.

### 3.3 Fault hook

```python
def fault_point(name: str, **labels: str) -> None
    # no-op unless env HERNESS_FAULTS names a fault plan file; labels (model, source, role, kind) filter rules
```

### 3.4 Jobs (`herness.core.jobs`)

```python
GpuClass = Literal["none", "reasoning", "decider", "large"]
JobKind  = Literal["sync", "reconcile", "build_pipeline", "distill", "review", "chat",
                   "outcome_measure", "memory_maintenance", "maintenance", "eval"]     # spec 02 §5.2

class JobSpec(BaseModel):                  # owner 08 (spec 00 §6)
    kind: JobKind; payload: dict; gpu_class: GpuClass
    priority: int = 50; max_attempts: int | None = None      # None = per-kind default
    scheduled_for: datetime | None = None; idem_key: str | None = None

class JobOutcome(BaseModel):               # owner 08
    status: Literal["done", "yield"]       # yield = stopped at a safe point; requeue without attempt charge
    result: dict = {}                      # e.g. {"partial": true, "skipped_open_circuit": ["jira"]}

def enqueue(kind: JobKind, payload: dict, gpu_class: GpuClass, priority: int = 50,
            scheduled_for: datetime | None = None, *, max_attempts: int | None = None,
            idem_key: str | None = None) -> str                     # job_id (existing one if deduped)
def submit(spec: JobSpec) -> tuple[str, bool]                        # (job_id, created)
def claim(*, owner: str, allowed_classes: Sequence[GpuClass], job_id: str | None = None) -> JobRow | None
def run_inline(job_id: str) -> JobOutcome   # CLI path when no worker is alive (spec 09 §5.6)
def cancel(job_id: str) -> Literal["canceled", "cancel_requested", "not_active"]
def retry(job_id: str) -> None              # dead letter (failed) → queued, attempts = 0
def get(job_id: str) -> JobRow
def list_jobs(*, status: str | None = None, kind: str | None = None, limit: int = 50) -> list[JobRow]
def worker_alive() -> bool                  # a worker row with heartbeat_at newer than 3 × heartbeat_s
def status_snapshot() -> dict               # data for `herness status` / dashboard (spec 09)
def register_handler(kind: JobKind, handler: Callable[[JobContext], JobOutcome]) -> None

ServiceName = Literal["vllm-reasoning", "openjev", "llamacpp-large"]   # compose services (spec 10 §5.6.2)

class ServiceControl:                      # ctx.services; only in the job holding the GPU slot
    def start(self, name: ServiceName, *, timeout_s: float | None = None) -> None   # start + health + warm-up
    def stop(self, name: ServiceName) -> None                                       # stop + VRAM check
    def healthy(self, name: ServiceName) -> bool

class JobContext:                          # owner 08
    job_id: str; kind: JobKind; attempt: int; payload: dict
    services: ServiceControl
    def should_yield(self) -> bool          # cancel, preempt or shutdown requested
    @property
    def stop_reason(self) -> Literal["cancel", "preempt", "shutdown"] | None
    def heartbeat(self, note: str | None = None) -> None    # progress signal; resets the stall timer
    def require_gpu_class(self, cls: GpuClass, *, timeout_s: float | None = None) -> None
        # blocking drain/stop/VRAM check/start/health/warm-up (§5.8); no-op if loaded; ModelUnavailable on failure
    @contextmanager
    def gpu_scope(self, cls: GpuClass) -> Iterator[None]    # require_gpu_class(cls); on exit restore the previous class
    def save_state(self, state: dict) -> None               # job-level checkpoint (specs 03, 04, 10 rekey)
    def load_state(self) -> dict                            # {} on first attempt
```

### 3.5 GPU state and control (requested by specs 03 and 06)

```python
class GpuStateReader(Protocol):
    def loaded_class(self) -> GpuClass | Literal["swapping"]
    def service_healthy(self, name: ServiceName) -> bool

def gpu_state() -> GpuStateReader          # read-only view for any process (reads the worker row)
```

The job's `gpu_class` is the class loaded before the handler starts. Inside the job, `ctx.require_gpu_class` and `ctx.gpu_scope` may switch to any class, and `ctx.services.start/stop` control single services within the current class. The supervisor performs every swap (§5.8) and keeps `worker.gpu_class_loaded` current. Spec 03 uses `ctx.services.start("openjev")` in stage 4 and `ctx.require_gpu_class("reasoning")` in stage 6 of the `build_pipeline` job; spec 06 uses `with ctx.gpu_scope("large"):` around the deep-mode `skeptic_final` and `writer` stages. Calling these from a job without the GPU slot raises `ConfigError`.

### 3.6 Chat policy (spec 00 §12.3, used by 06 and 09)

```python
ChatMode = Literal["live", "small_model", "defer", "cloud"]    # owner 08

def chat_policy(now: datetime) -> ChatMode
def chat_model_profile(mode: ChatMode, depth: str = "fast") -> str | None
    # live: first local key of chain_for("chat", depth); small_model: first of chain_for("chat_off_hours", depth)
    # (local-small-cpu); cloud: first off-network key of chain_for("chat", depth) (e.g. claude-opus); defer: None
def chat_next_live_at(now: datetime) -> datetime | None # UTC; when `live` is next expected (UI eta for defer)
```

`live`: reasoning model loaded and healthy. `small_model`: answer now with the CPU model. `defer`: enqueue a `chat` job answered when reasoning is loaded. `cloud`: answer now with an off-network profile; returned only when the active profile allows egress (D5), else the policy falls back to `small_model`.

### 3.7 Task lease/resume helpers (spec 00 §12.3, used by 06)

```python
def recover_run_tasks(run_id: str, *, max_task_attempts: int, retry_dead: bool = False) -> RecoverySummary
def claim_task(task_id: str) -> bool                        # pending → running, attempts += 1
def save_checkpoint(task_id: str, checkpoint: dict, *, writes: Callable[[sqlite3.Connection], None] | None = None) -> None
def complete_task(task_id: str, result: dict, *, writes: Callable[[sqlite3.Connection], None] | None = None) -> None
def fail_task(task_id: str, err: HernessError, *, max_task_attempts: int) -> Literal["pending", "dead"]
def release_task(task_id: str) -> None                      # running → pending, attempts -= 1 (cancel/preempt)
```

All helpers write through `herness.store.ops` (spec 00 §4). No other module writes `job`, `worker`, `source_health`, `resilience_event` or `task.status`/`attempts`/`checkpoint`.

### 3.8 CLI behavior (commands wired by spec 09)

`herness worker [--gpu-class none,reasoning,decider,large] [--concurrency N] [--once]`, `herness resume RUN_ID`, `herness jobs list|cancel|retry`, and the resilience part of `herness status` (from `status_snapshot()`).

## 4. Data contracts

### 4.1 `job` (spec 02 §5.2)

| Column | Rule |
|---|---|
| `job_id` | `job_<ulid>` |
| `priority` | 0–100, higher first. Defaults: manual CLI 80, chat 75, sync 60, build_pipeline 60, review 40, reconcile 40, outcome_measure 30, memory_maintenance 30, maintenance 30, eval 20 |
| `payload` | JSON, ≤ 64 KB, no secrets or record text. Reviews: spec 06 `RunRequest` incl. `run_id`; scheduled jobs add `schedule`, `fire_at` |
| `idem_key` | default `kind:` + 16 hex of SHA-256 over canonical payload; scheduled `sched:<name>:<fire_at>[:<step>]`; resume `resume:<run_id>`; sync `sync:<source>`; rekey `rekey:<key_id>` |
| `result` | while running: `{"state": {...}}` from `ctx.save_state`; when done: `JobOutcome.result` (the `state` key is dropped) |
| `attempts` | +1 at claim, −1 on `yield` |
| `last_error` | `{"class", "message" (redacted, ≤ 2 KB), "at", "attempt"}` |
| `lease_owner` | `<host>:<pid>:<slot>` |
| timestamps | fixed-width format of spec 00 §8 |

Dead letter = `status = 'failed'`. The dashboard lists failed jobs with `last_error` and a retry action (`jobs.retry`).

### 4.2 `worker`, `source_health`, `resilience_event` (spec 02 §5.1–5.2)

- `worker`: one row per supervisor. `gpu_class_loaded = 'swapping'` during a swap. `requested_class` is set by `ctx.require_gpu_class` requests and by the arbiter. `current_jobs` = `[{"job_id", "slot", "kind", "started_at"}]`.
- `source_health.failures` = consecutive failed attempts; `trips` = consecutive opens without a successful probe.
- `resilience_event.kind` ∈ `retry`, `fallback`, `repair`, `guard_stop`, `breaker_open`, `breaker_half_open`, `breaker_close`, `job_done`, `job_failed`, `job_yield`, `lease_expired`, `gpu_swap`, `gpu_swap_failed`, `service_restart`, `service_start`, `service_stop`, `schedule_fired`, `schedule_missed`, `chain_broken`, `chain_skipped`, `rekey_planned`. `detail` holds the fields of §4.4, never text or payloads.

### 4.3 Task checkpoint envelope (`task.checkpoint`, spec 02 §5.3)

```jsonc
{
  "v": 1,
  "phase": "analyze",                 // spec 06 step name
  "loop": { ... },                    // spec 05 LoopCheckpoint: messages, step, ledger_snapshot, seen_signatures
  "pending_findings": [ ... ],        // spec 06
  "scratchpad": "...",                // reserved for spec 07 (compaction); 08 never reads or writes it
  "state": { ... },                   // spec 06 role-specific (e.g. pseudonym map in hybrid)
  "saved_at": "2026-09-24T22:14:03.120000Z"
}
```

`save_checkpoint` replaces the whole object and preserves `scratchpad` if the caller omits it. Size cap 4 MB; above it the `loop` key is dropped with a warning, so a resume restarts that task from its spec. Spec 05 serializes `loop`; spec 10 rules on text apply (messages hold only redacted text and tool results).

### 4.4 Trace events (format spec 05 §5.7; emitted through spec 05's `Tracer`)

Spec 05's `Tracer` is the only trace writer. Functions here that emit events take an optional `tracer: Tracer | None` argument; they always write `resilience_event` rows, and write trace events only when a tracer is passed.

| `event` | Fields supplied by this spec |
|---|---|
| `retry` | `target` (`llm` \| `tool` \| `output_repair`), `attempt`, `error_type`, `wait_s`, plus `policy`, `breaker_key`, `retry_after_s` |
| `fallback` | `from_profile`, `to_profile`, `reason` (`unavailable` \| `circuit_open` \| `validation` \| `refusal` \| `egress_blocked` \| `auth`) |
| `repair` | `model_profile`, `repair_no`, `error_paths` (JSON pointer paths only) |
| `guard_stop` | `cause` (spec 05 loop signal cause, or `cancel` \| `preempt` \| `shutdown`), `step` |

Outside a run (connectors, enrichment, worker) the same data goes to the log and `resilience_event` only.

## 5. Behavior

### 5.1 Job kinds, handlers and GPU classes

| Kind | Handler owner | `gpu_class` at start | In-job switches | Enqueued by |
|---|---|---|---|---|
| `sync` | 01 (`connectors.runner`) | none | — | per-source cron `sources.yaml: <source>.schedule`; CLI |
| `reconcile` | 01 | none | — | per-source cron `sources.yaml: <source>.reconcile.schedule` (default Sun 03:00) |
| `build_pipeline` | 02 (`model.build`), stages by 03, 04 | decider | 03: `ctx.services.start("openjev")`, `ctx.require_gpu_class("reasoning")` | `nightly` schedule; CLI `build/enrich/score/pipeline` (payload `stages`) |
| `distill` | 03 | decider | `ctx.services.start("openjev")` | CLI; spec 03 weekly/active-learning schedule |
| `review` | 06 | reasoning | deep: `ctx.gpu_scope("large")` | `nightly` chain, `weekly_deep`, CLI, chat escalation (06 §5.13), `herness resume` |
| `chat` | 06 (`ChatService`, deferred turn) | reasoning | — | spec 06 `ChatService` when the mode is `defer` (spec 09 never enqueues it); payload `{session_id, message_id}`, `idem_key = chat:<session_id>:<message_id>` |
| `outcome_measure` | 07 | none | — | spec 07 on accepted decisions (`scheduled_for` = measure date); weekly `outcomes` sweep (payload `{"sweep": true}`) |
| `memory_maintenance` | 07 | none | — | `memory_maintenance` schedule, daily 05:00 |
| `maintenance` `{"action": "backup" \| "purge"}` | 10 | none | — | `maintenance` schedule at `backup.nightly_at` (spec 10, default 01:30) |
| `maintenance` `{"action": "rekey"}` | 10 (key rotation), GPU work via 03 functions | decider | `ctx.services.start("openjev")` | spec 10 key rotation only, through `jobs.schedule_rekey()` (§5.11); never ad hoc |
| `eval` | 11 | reasoning (golden) or decider (classifier), per payload | — | `nightly` chain (optional), CLI |

A handler raises a `HernessError` on failure; any other exception is wrapped as `FatalError` naming the type. Kinds in `exclusive_kinds` (`build_pipeline`, `distill`, `maintenance`) never run concurrently with each other, because they all write the warehouse, decision cache or vectors.

### 5.2 Retry policies

Full jitter: `sleep = uniform(0, min(cap_s, base_s * 2 ** (attempt - 1)))`. With `RateLimited.retry_after`: `sleep = min(max(retry_after, jitter), retry_after_cap_s)`; if `retry_after > retry_after_cap_s` the call re-raises and the job layer reschedules at `now + retry_after`. `classify()` parses `Retry-After` as seconds or HTTP date, else `X-RateLimit-Reset`.

| Policy (call type) | attempts | base / cap (s) | max elapsed (s) | timeout per attempt | Retry-After cap (s) |
|---|---|---|---|---|---|
| `source_http_page` (one page, spec 01) | 6 | 1 / 60 | 600 | source `timeout_s` (60), connect 10 | 300 |
| `llm_local` (vLLM) | 4 | 2 / 30 | 900 | profile `timeout_s` (spec 05) | 60 |
| `llm_large` (CPU-offload server) | 2 | 30 / 120 | 7200 | profile `timeout_s` | 60 |
| `llm_cloud` (Anthropic) | 5 | 2 / 60 | 900 | profile `timeout_s` | 120 |
| `decider_local` (OpenJev request; Laya batch) | 3 | 2 / 20 | 300 | 30 (spec 03) | 30 |
| `decider_cloud` (hosted Jev) | 3 | 2 / 30 | 300 | 30 | 120 |
| `embed_batch` | 3 | 2 / 20 | 300 | 120 | — |
| `tool_store` (`StoreBusy` inside a tool, spec 05 `run_one`) | 3 | 0.5 / 4 | 30 | tool timeout (spec 05) | — |
| `warehouse_read` (metrics/score SQL, `StoreBusy` only) | 3 | 1 / 10 | 60 | 600 | — |
| `sqlite_write` (`StoreBusy` only) | 6 | 0.2 / 5 | 30 | `busy_timeout` 10 | — |
| `gpu_health` (health polls during a swap) | until swap timeout | 2 / 10 | class `start_timeout_s` | 5 | — |

Error class rules:

| Class (spec 00 §7) | In call | Job | Task (spec 06) |
|---|---|---|---|
| `SourceUnavailable`, `ModelUnavailable`, `StoreBusy` | retry per policy; breaker records failure | reschedule `now + uniform(0, min(3600, 60·2^(attempts−1)))` while `attempts < max_attempts`, else `failed` | `fail_task` → `pending` until `max_task_attempts`, else `dead` |
| `RateLimited` | honor `retry_after`; breaker not charged | reschedule at `now + retry_after` | as above |
| `CircuitOpen` | no retry; LLM → next chain entry; source → skipped | `sync`/`reconcile`: `done` with `result.skipped_open_circuit`; others reschedule at `retry_at` | `pending` |
| `OutputValidationError` | repair (§5.3), then fallback | — | chain exhausted → `dead` |
| `ModelRefused` | no retry on the same model; fallback, reason `refusal` | — | chain exhausted → `dead` |
| `EgressBlocked` | no retry; fallback to the next local entry, reason `egress_blocked` | — | chain exhausted → `dead` |
| `ToolInputError`, `QueryError` | not handled here; error tool result (spec 05, 3 attempts per query) | — | — |
| `PolicyViolation`, `ReportContractError` | not handled here (07, 09) | — | — |
| `AuthError` | no retry; source breaker `force_open`; model entry dropped for the run, fallback reason `auth` | `failed` | `dead` |
| `ConfigError`, `SchemaViolation`, `PermissionDenied` | no retry | `failed` | `dead` |
| `BudgetExceeded` | no retry, no fallback | handler returns `done` with `result.partial` (spec 06) | spec 06 §6.3 |

`classify()` mapping: `httpx.ConnectError`/`ReadTimeout`/`PoolTimeout`, HTTP 500/502/503/504/529 → `SourceUnavailable` or `ModelUnavailable` by `family`; 429 → `RateLimited`; 401/403 → `AuthError`; `openai.APIConnectionError`/`APITimeoutError`, `anthropic.APIConnectionError`/`OverloadedError` → `ModelUnavailable`; `sqlite3.OperationalError` "locked"/"busy" and DuckDB lock errors → `StoreBusy`; DuckDB `InterruptException` after timeout → `QueryError(timeout=True)`.

Timeouts: HTTP uses `httpx.Timeout(timeout_s, connect=connect_timeout_s)`; DuckDB uses a `threading.Timer` calling `con.interrupt()`; SQLite uses `busy_timeout`; in-process models (Laya, embeddings) use `call_with_timeout` and on timeout raise `ModelUnavailable`; the stuck thread dies with the job's child process (§5.9).

Tenacity wiring, one factory for `retrying`, `retry_call`, `aretry_call`, `retry_page`:

```python
def _build(p: RetryPolicy, key: str | None) -> tenacity.BaseRetrying:
    return tenacity.Retrying(                         # AsyncRetrying for coroutines
        retry=tenacity.retry_if_exception(lambda e: isinstance(e, RetryableError) and not isinstance(e, CircuitOpen)),
        stop=tenacity.stop_after_attempt(p.attempts) | tenacity.stop_after_delay(p.max_elapsed_s),
        wait=FullJitterRetryAfter(p),                 # custom tenacity.wait.wait_base
        before=lambda rs: guard(key) if key else None,
        after=lambda rs: _breaker_record(key, rs),
        before_sleep=lambda rs: _emit_retry(p, key, rs),   # trace + log + resilience_event
        reraise=True)
```

Wrapped functions convert foreign exceptions with `classify()`, so tenacity only sees `HernessError`.

### 5.3 Circuit breakers

| From | Event | To | Side effects |
|---|---|---|---|
| closed | failure, `failures + 1 < threshold` | closed | `failures += 1` |
| closed | failure reaching threshold, or `force_open` | open | `opened_at = now`, `trips += 1`, event `breaker_open` |
| closed | success | closed | `failures = 0` |
| open | `now >= opened_at + cooldown` and caller wins the probe claim | half_open | one probe runs |
| half_open | probe success | closed | `failures = 0`, `trips = 0`, event `breaker_close` |
| half_open | probe failure | open | `opened_at = now`, `trips += 1` |

`cooldown = min(cooldown_s · 2^(trips−1), cooldown_max_s)`. Only `SourceUnavailable` and `ModelUnavailable` count as failures. Probe claim across processes:

```sql
UPDATE source_health SET state = 'half_open', updated_at = :now
WHERE source = :key AND state = 'open' AND :now >= :probe_due
RETURNING source;
```

Probes: sources call `Connector.check()`; `model:*` calls vLLM `GET /health` (cloud: skipped unless the profile allows egress, then a 1-token call through the egress guard); `decider:*` calls `Decider.health()`. Breaker state is cached per process for 5 s; transitions write through. An open `model:<profile>` breaker for a GPU service that the worker has loaded triggers one service restart (stop + start of that service, max `restart_max_per_hour`), event `service_restart`.

Effect on sync: spec 01 calls `guard(source)` before each entity; an open circuit skips the source, its watermark stays, and the job ends `done` with `result = {"partial": true, "skipped_open_circuit": [...]}`. The build pipeline runs on the data it has.

### 5.4 Model fallback chains and repair

Chains are defined only in `config/models.yaml` (spec 05) and read with `LLMRegistry.chain_for(model_role, depth)`, an ordered list of client keys (`local-30b`, `local-lora-14b`, `local-large-offload`, `local-small-cpu`, `claude-opus`, `claude-sonnet`, `claude-haiku`). This spec executes them. `candidates()` removes: off-network clients when the active profile does not allow egress (D5), clients whose `model:<key>` breaker is open, clients dropped for the run after `AuthError`, and clients whose GPU class is not loaded (`local-large-offload` needs `large`; `local-30b`, `local-lora-14b` need `reasoning`; `local-small-cpu` needs none).

`ModelChain.acomplete` runs, per candidate: `aretry_call("llm_*", ...)` with `breaker_key = model:<client key>` on `client_for(key)` (spec 05 wraps it in `GatedClient` with the spec 06 call gate); if `schema` is set, `complete_validated` (repair) and the parsed model is returned with the response; on a fallback trigger it moves to the next candidate and emits `fallback`. Triggers: `ModelUnavailable` after retries, `CircuitOpen`, `OutputValidationError` after 2 repairs, `ModelRefused`, `EgressBlocked`, `AuthError`. Never: `BudgetExceeded`, `QueryError`, `ToolInputError`. Chain exhausted → the last error propagates.

Repair protocol (`complete_validated`):

1. First call as given (spec 05 sets guided JSON / tool schema where supported).
2. Validate with the role's pydantic model. On failure add the assistant output (truncated to 4 000 chars) and a user message: `Your previous reply was not valid. Errors:\n<≤ 20 lines "path: message">\nReply with only a JSON object that matches this JSON Schema:\n<schema>`.
3. Repairs use temperature 0 and the same model; at most 2; each emits `repair`.
4. After the second failed repair raise `OutputValidationError`; the chain restarts at step 1 with the original request on the next model.

A fallback from a local model to another local model that needs a different GPU class is never done by swapping mid-call; spec 06 switches classes explicitly with `ctx.gpu_scope(...)` at stage boundaries.

### 5.5 Decider fallback (with spec 03)

The order is `resilience.fallback.decider_chain` in `config/resilience.yaml` (default `local: [laya, openjev, llm]`, `hybrid: [laya, openjev, jev, llm]`); spec 03 decides which items enter the chain. `DeciderChain.decide` moves a batch down the order on `ModelUnavailable`, `CircuitOpen` or an in-process crash. Entries whose service is not up are skipped and their items returned as `deferred`: spec 03 sends OpenJev-deferred items to the LLM decider in its reasoning stage (after `ctx.require_gpu_class("reasoning")`), and anything still undecided stays a cache miss for the next night. Low-confidence escalation is spec 03 logic, not fallback.

### 5.6 Agent loop guards: division of labor

| Concern | Detected by | This spec |
|---|---|---|
| Max steps, per-agent tokens/cost | spec 05 loop (`ctx.budgets`) | nothing |
| Run-level tokens/cost | spec 06 `RunBudget` (06 §6.3) | nothing |
| Repeated call, no progress, error streak | spec 05 loop, `LoopState.loop_signal()` | `loop_signal_policy`, called by spec 05 `HarnessHooks.on_loop_signal`: first signal of a task → `nudge`; second → `stop`; emits `guard_stop` |
| Cancel, preempt, shutdown | `JobContext.should_yield()` | spec 06 driver polls it between steps and tasks; running task coroutines get `CancelledError` after their current step; `release_task` returns them to `pending`; `guard_stop` emitted |
| Checkpoint every step | spec 05 `HarnessHooks.after_step` | `save_checkpoint(task_id, {... "loop": checkpoint})`, called at most every `checkpoint_min_interval_s` (5 s) and always on stop |

### 5.7 Job queue mechanics

Enqueue, one transaction:

```sql
INSERT INTO job (job_id, kind, gpu_class, status, priority, payload, idem_key, attempts, max_attempts,
                 scheduled_for, created_at)
VALUES (:job_id, :kind, :gpu_class, 'queued', :priority, :payload, :idem_key, 0, :max_attempts,
        :scheduled_for, :now)
ON CONFLICT (idem_key) WHERE status IN ('queued', 'running') DO NOTHING
RETURNING job_id;
-- no row → SELECT job_id FROM job WHERE idem_key = :idem_key AND status IN ('queued','running')
```

The scheduler also checks, in the same `BEGIN IMMEDIATE` transaction, that no job with a `sched:` key exists in any status, so a finished scheduled job never fires twice.

Atomic claim, under `BEGIN IMMEDIATE`:

```sql
UPDATE job
SET status = 'running', lease_owner = :owner, lease_expires_at = :lease_until,
    attempts = attempts + 1, started_at = COALESCE(started_at, :now)
WHERE job_id = (
    SELECT j.job_id FROM job j
    WHERE j.status = 'queued'
      AND j.scheduled_for <= :now
      AND (:job_id IS NULL OR j.job_id = :job_id)
      AND j.gpu_class IN (SELECT value FROM json_each(:allowed_classes))
      AND NOT (j.kind IN (SELECT value FROM json_each(:exclusive_kinds))
               AND EXISTS (SELECT 1 FROM job r WHERE r.status = 'running'
                           AND r.kind IN (SELECT value FROM json_each(:exclusive_kinds))))
    ORDER BY j.priority DESC, j.scheduled_for ASC, j.created_at ASC
    LIMIT 1)
  AND status = 'queued'
RETURNING *;
```

`allowed_classes`: GPU slot = the class the arbiter chose (§5.10); CPU slots = `["none"]`.

Heartbeat every `heartbeat_s` from the supervisor:

```sql
UPDATE job SET lease_expires_at = :lease_until
WHERE job_id = :job_id AND lease_owner = :owner AND status = 'running';
```

Zero rows → canceled or lease lost: the supervisor sends `stop("cancel")`, waits `cancel_grace_s`, then terminates the child.

Reaper, every 30 s in every worker:

```sql
UPDATE job SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL, scheduled_for = :now,
       last_error = json_object('class', 'LeaseExpired', 'at', :now, 'attempt', attempts)
WHERE status = 'running' AND lease_expires_at < :now AND attempts < max_attempts;
UPDATE job SET status = 'failed', finished_at = :now, lease_owner = NULL,
       last_error = json_object('class', 'LeaseExpired', 'at', :now, 'attempt', attempts)
WHERE status = 'running' AND lease_expires_at < :now AND attempts >= max_attempts;
```

Completion statements all carry `WHERE job_id = :id AND lease_owner = :owner`:
- `done` → `status='done', result=:result, finished_at=:now, lease_owner=NULL`.
- `yield` → `status='queued', attempts=attempts-1, scheduled_for=:resume_at, lease_owner=NULL` (`:resume_at` = next window allowing the class, or now for shutdown).
- retryable failure → `queued` with job backoff (§5.2), or `failed` at `max_attempts`.
- fatal → `failed`.

Cancel: queued → `canceled` immediately; running → `status='canceled'`, stopped cooperatively via the heartbeat path.

`run_inline(job_id)` (CLI without a worker): claims that job with owner `<host>:<pid>:cli`, takes the GPU lock file if `gpu_class != 'none'`, performs any needed swap itself, heartbeats from a thread, and runs the handler in-process. Ctrl+C → `stop("shutdown")` → `yield`.

### 5.8 GPU arbitration and service control

Classes and compose services (spec 10 §5.6.2; configured in `resilience.gpu.classes`):

| Class | Must be running | Must be stopped | In-process GPU users allowed |
|---|---|---|---|
| none | — | — (untouched) | no |
| reasoning | `vllm-reasoning` (127.0.0.1:8000, `GET /health`) | `openjev`, `llamacpp-large` | no |
| decider | nothing at entry; `openjev` (127.0.0.1:8100, `GET /v1/models` with bearer key) only via `ctx.services.start` | `vllm-reasoning`, `llamacpp-large` | bge-m3, Laya (spec 03), only while `openjev` is stopped |
| large | `llamacpp-large` (127.0.0.1:8200, `GET /health`) | `vllm-reasoning`, `openjev` | no |

Warm-up: vLLM one 8-token chat completion; OpenJev one 1-item `POST /v1/systemone`; llama.cpp one 8-token completion.

Commands: `gpu.compose_cmd` (default `[wsl.exe, -d, herness, --, docker, compose, --env-file, /opt/herness/docker.env]`) + `-f <gpu.compose_file>` (default `/mnt/d/herness/docker/compose.yaml`, a WSL path) + `--profile <class> up -d <service>` or `stop -t 60 <service>`. Argument lists only, never a shell. VRAM checks use Windows `nvidia-smi`.

Swap to class B (from `ctx.require_gpu_class`, or from the arbiter between jobs):

```text
swap(A -> B):
  1. worker.gpu_class_loaded = 'swapping', requested_class = B
  2. stop every service that must be stopped for B            timeout stop_timeout_s (120)
       still running -> `docker compose kill <svc>`
  3. VRAM check: nvidia-smi memory.used < vram_free_threshold_mb, poll 2 s, timeout 60 s
       fail -> ModelUnavailable("vram_not_freed"); class = none
  4. start the services B requires; poll health with policy gpu_health until start_timeout_s
  5. warm-up request, timeout warmup_timeout_s (120)
  6. worker.gpu_class_loaded = B; reset model:/decider: breakers for B's endpoints; event gpu_swap {from, to, duration_s}
  failure in 4–5: stop B's services, class = none, breaker records failure, event gpu_swap_failed;
     arbiter: B jobs rescheduled +10 min without attempt charge; in-job require_gpu_class(): raises ModelUnavailable
```

`require_gpu_class` drains first: the handler must have released in-process GPU models (spec 03 unloads bge-m3/Laya and calls `torch.cuda.empty_cache()`) before calling it. `ctx.services.start("openjev")` inside class `decider` has the same precondition and runs steps 3–5 for that service only; `ctx.services.stop` runs steps 2–3. `ctx.services.healthy` is one health GET (5 s timeout). Starting a service that belongs to another class raises `ConfigError` (use `require_gpu_class`). In-job requests go from the child to the supervisor over the job pipe; the supervisor performs the work, keeps heartbeating, and replies `ok` or the error.

On worker start the loaded class is detected with `docker compose ps --format json` plus health checks. If services of two classes run, all are stopped and the class is `none`. `herness deploy up|down` (spec 10 §3.7) goes through the arbiter when a worker is alive (sets `worker.requested_class`), else holds `data/locks/gpu.lock` (§11 Q2).

### 5.9 Worker process model

`herness worker` runs one supervisor with one GPU slot (if `--gpu-class` includes any class other than `none`) and `--concurrency N` CPU slots (default 2). `--once` claims one job, runs it and exits. Each job runs in a fresh child process (`multiprocessing` spawn) so a hang can be killed and GPU memory is freed on exit. Pipe messages: parent → child `stop(reason)`, `gpu_reply`; child → parent `heartbeat`, `save_state`, `gpu_request`, `outcome`.

Supervisor tick (`tick_s` 2 s): reaper and scheduler every 30 s; arbiter decision (§5.10); claim into free slots; lease heartbeats; kill children with no `ctx.heartbeat()` for `stall_timeout_s` (1800 s; 7200 s while class `large` is loaded) and record `ModelUnavailable("stalled")`; update the `worker` row.

Shutdown: SIGINT / Windows `CTRL_C_EVENT`, SIGBREAK (`CTRL_BREAK_EVENT`), SIGTERM on Linux. First signal: stop claiming, `stop("shutdown")` to children, wait `shutdown_grace_s` (120); children checkpoint and return `yield`. Then terminate the rest; their jobs are requeued at the next start. Second signal: terminate at once. GPU services are left as they are.

Crash recovery at start: `running` jobs whose `lease_owner` starts with this host and whose pid is not alive (`psutil.pid_exists`) are requeued at once with the reaper statements.

Single GPU owner per host: the supervisor holds an OS file lock on `data/locks/gpu.lock`; a second GPU-capable worker exits with `ConfigError`. Service install (Task Scheduler `herness-worker`, or NSSM with Ctrl+C stop) is spec 10 §5.6.4.

### 5.10 Schedule windows, arbitration and preemption

Windows use `weights.yaml: business_timezone`. Each lists allowed GPU classes in preference order; `none` jobs run in every window.

| Window | Default | Allowed classes | Preload | At end |
|---|---|---|---|---|
| `morning_prep` | 06:00–08:00 daily | reasoning | reasoning | — |
| `chat` | 08:00–19:00 daily (D4) | reasoning | reasoning | yield jobs of other classes (hard start at 08:00) |
| `enrichment` | 19:00–21:00 daily | decider (in-job switches allowed) | — | may overrun `overrun_max_min` (120) |
| `reviews` | 21:00–06:00 Mon–Sat nights | decider, reasoning | — | overrun 60 min, then yield |
| `deep` | Sun 21:00–Mon 06:00 (D3) | decider, reasoning, large | — | overrun 60 min, then yield |

Arbiter (GPU slot free): keep the loaded class if allowed and it has claimable jobs; else the first allowed class with claimable jobs (swap); else load the window's `preload` class. A job whose start class is not allowed stays `queued` until a window allows it. In-job switches (`require_gpu_class`, `gpu_scope`) are allowed in any window, but at a `hard` boundary the job is preempted like any other.

Preemption applies only when the next window does not allow the running job's current class. Then, at `window_end + overrun` (or `window_end` for hard boundaries), the supervisor sends `stop("preempt")`. Handlers check `ctx.should_yield()` at batch and task boundaries (spec 03 after each batch, spec 06 between steps), checkpoint and return `yield`. After `preempt_grace_min` (15) the child is terminated and the job requeued without an attempt charge.

Batch reasoning jobs (reviews, eval) may start in the `chat` window only with priority ≥ `batch_in_chat_min_priority` (70); `chat` jobs always may. Chat capacity on vLLM is reserved by the client field `chat_reserved_slots` in `models.yaml` (spec 05), enforced by the spec 06 call gate.

`chat_policy(now)`:

```text
chat_key = first local key of chain_for("chat", "fast")                    # e.g. local-30b
if gpu_state().loaded_class == "reasoning" and service_healthy("vllm-reasoning")
   and breaker("model:" + chat_key).state != "open":
    return "live"                               # includes nights when reviews hold reasoning
mode = schedule.chat.in_hours_unavailable if in chat window else schedule.chat.off_hours   # both default small_model (D4)
if mode == "cloud" and not active profile allows egress (D5):  mode = "small_model"
if mode == "small_model" and breaker("model:local-small-cpu") is open:
    mode = "defer"
return mode
```

`defer` enqueues a `chat` job (`gpu_class = reasoning`, priority 75); `chat_next_live_at(now)` gives the UI its ETA (next window whose preload or allowed classes include reasoning, or the end of a running swap). Spec 06 refuses chat escalation to a review unless the mode is `live`, quoting `chat_next_live_at`.

### 5.11 Scheduler

Schedules are 5-field cron expressions (minute hour day-of-month month day-of-week; `*`, `*/n`, `a-b`, lists, names `MON`–`SUN`) evaluated in `business_timezone` every 30 s. The parser is in `jobs.py` (no extra dependency). DST gap → next valid minute; ambiguous time → `fold=0` only.

Sources of schedules: per-source `schedule` and `reconcile.schedule` in `sources.yaml` (spec 01); `maintenance` (backup, purge) at `backup.nightly_at` (spec 10); the rest in `config/resilience.yaml: schedule.jobs`.

Catch-up: for each schedule take the latest fire time `F ≤ now`. If no job with `idem_key = sched:<name>:<F>` exists and `now − F ≤ catch_up_max`, enqueue it; otherwise emit `schedule_missed`. Only the latest missed fire is enqueued. `sync` jobs additionally dedupe on `sync:<source>` while one is queued or running.

Chains: when a scheduled job ends `done` (including partial), each `then` entry is enqueued with `idem_key = sched:<name>:<F>:<step>`, honoring `skip_on`. A `failed` job stops its chain (`chain_broken`).

Defaults:

| Schedule | Cron | Job | Then |
|---|---|---|---|
| per source (spec 01) | `*/30 * * * *` | `sync` | — |
| per source reconcile (spec 01) | `0 3 * * SUN` | `reconcile` | — |
| `nightly` | `0 19 * * *` | `build_pipeline` (decider) | `review` funding standard → `review` org standard (`skip_on: [SUN]`) → `eval` (disabled by default; on for the dev box, spec 11) |
| `weekly_deep` | `0 21 * * SUN` | `review` funding + org, depth deep | — |
| `memory_maintenance` | `0 5 * * *` | `memory_maintenance` (none): expiry sweep, dedupe, FTS/embedding sync (spec 07) | — |
| `outcomes` | `0 6 * * MON` | `outcome_measure` `{"sweep": true}` (none): measures any due recommendation not yet measured | — |
| `maintenance` | from `backup.nightly_at` (01:30) | `maintenance` `{"action": "backup"}` then `{"action": "purge"}` | — |

Spec 07 also enqueues one-off `outcome_measure` jobs with `scheduled_for`; the weekly sweep catches any it missed (idempotent per `rec_id` and measurement number in spec 07).

**Rekey (planned only).** A redaction-key rotation (spec 10 `herness secrets rekey`) changes every `content_hash` and costs about 6 h of GPU re-embedding and re-classification in the following build. It is never run ad hoc. `jobs.schedule_rekey() -> job_id` enqueues `maintenance` `{"action": "rekey"}` with `gpu_class = decider`, priority 70, `idem_key = rekey:<new key_id>`, and `scheduled_for` = the next fire of `schedule.rekey.cron` (default `0 19 * * SAT`, business timezone) at least `schedule.rekey.min_notice_h` (12) ahead. On that night:

- the rekey job claims before the 19:00 `build_pipeline` (higher priority; `exclusive_kinds` keeps them apart);
- the `build_pipeline` then runs the full re-embed/re-classify through the Saturday `reviews` window (decider is allowed there, so no preemption at 21:00; payload `rekey_night: true`);
- the `nightly` chain's standard reviews for that date are skipped (`chain_skipped` event); Sunday's deep run uses the new build.
`herness status` shows the planned rekey night. Canceling it (`jobs cancel`) leaves the old key active (spec 10 §5.3).

### 5.12 Task resume and `herness resume`

A run's tasks are executed only by the process holding the lease on the run's `review` (or `chat`) job, so the job lease is the task lease.

`recover_run_tasks(run_id)` is called by spec 06 `resume()` before driving the run:

```sql
UPDATE task SET status = 'pending', updated_at = :now WHERE run_id = :run_id AND status = 'running';
UPDATE task SET status = 'pending', updated_at = :now
WHERE run_id = :run_id AND status = 'failed' AND attempts < :max_task_attempts;
-- retry_dead: UPDATE task SET status = 'pending', attempts = 0 WHERE run_id = :run_id AND status = 'dead'
```

`attempts` is incremented once, in `claim_task`; recovery does not add to it (§11 Q1). `done` tasks are never rerun. A pending task with `checkpoint.loop` resumes through `run_agent(resume_from=...)` (spec 05).

Idempotency rule (binding on 05/06/07): a task's durable side effects happen only inside `save_checkpoint(..., writes=...)` or `complete_task(..., writes=...)`, which run the callback and the checkpoint/status update in one `BEGIN IMMEDIATE` transaction. Evidence rows use `INSERT OR IGNORE` on `query_id`. Task rows are inserted idempotently on `(run_id, spec.dedup_key)` (spec 02 §5.3).

`herness resume RUN_ID`: checks the run exists and is not terminal (else prints its result); enqueues `review` with payload `{"run_id", "resume": true}`, `idem_key = resume:<run_id>`, priority 80; with `--wait` follows it (runs it inline if no worker is alive). Spec 06 `resume()` raises `ConfigError` when the build is retired or the config hash changed without `--force`.

### 5.13 Fault injection

`fault_point(name, **labels)` is a no-op unless env `HERNESS_FAULTS` holds the path of a fault plan (YAML or JSON). When enabled the worker sets `worker.faults_enabled = 1`, logs a WARNING and `herness status` shows `FAULTS ENABLED`. The plan is loaded once per process.

```yaml
- {point: llm.call, action: timeout, nth: 3}
- {point: llm.output, action: malformed_json, count: 2, model: local-30b}
- {point: sql.query, action: timeout, p: 0.2, seed: 7}
- {point: http.page, action: http_429, retry_after: 7, count: 3, source: jira}
- {point: swarm.after_finding_write, action: kill, nth: 5}
```

Actions: `timeout`, `error:<ErrorClass>`, `http_429` (`retry_after`), `http_503`, `malformed_json`, `delay:<s>`, `kill` (`os.kill(os.getpid(), SIGKILL)`; `TerminateProcess` on Windows), `kill_service:<service>`. Selectors: `nth`, `count`, `p` + `seed`; label filters `model`, `source`, `role`, `kind`. Fake LLM/decider servers (spec 11) have their own per-turn faults; `fault_point` covers in-process points.

Named points (call site owner in brackets): `http.page` [01], `connector.before_watermark` [01], `llm.call`, `llm.output` [08, in `ModelChain.acomplete`], `sql.query` [05], `decider.batch`, `embed.batch`, `enrich.after_batch_write` [03], `build.mid_sql` [02], `pipeline.before_promote` [02], `swarm.after_task_claim`, `swarm.after_finding_write` [06], `verifier.mid_batch` [05], `sqlite.write` [`store.ops`], `job.after_claim`, `job.before_complete`, `gpu.after_stop`, `gpu.after_start` [08].

### 5.14 Observability

Log events (component `resilience` or `jobs`) use the `resilience_event.kind` names (§4.2) plus `job_enqueued`, `job_claimed`. Derived metrics for the dashboard and spec 11 (`retries_per_run`, `fallbacks_per_run`): retries and fallbacks per policy/profile per day, breaker opens per key, queue wait (`started_at − scheduled_for`) and run time p50/p95 per kind, swap duration, dead-letter count.

`status_snapshot()` returns: workers (heartbeat age, slots, loaded class, service health), current window and next window, `chat_policy(now)`, running jobs (kind, attempt, lease left, last `heartbeat` note), planned rekey night, queued count and next job, failed jobs in 24 h, dead letters, breakers not closed (key, since, trips, probe due), 24 h counters, schedules with next fire time, faults flag. Spec 09 renders it in `herness status` and the dashboard.

## 6. Errors and resilience (of this component)

- Ops store unavailable in the supervisor (`StoreBusy` past `sqlite_write`): skip the tick, keep children; after 10 failed ticks, graceful shutdown.
- Clock jumps forward expire leases early; the `lease_owner` guard rejects the old owner's completion and its child is stopped at the next heartbeat.
- WSL or Docker down: swaps fail with `ModelUnavailable`; GPU jobs reschedule; CPU slots continue; `chat_policy` returns `schedule.chat.in_hours_unavailable` (default `small_model`); status shows `gpu: unavailable`.
- Child exits without an outcome: `ModelUnavailable("child_crash")` → job retry policy.
- A broken schedule entry is logged (`schedule_error`) and never blocks the others; invalid cron fails config validation.

## 7. Configuration

`config/resilience.yaml`, two top-level keys, `resilience` and `schedule`; spec 10 §4.1 loads the file as section `cfg.resilience` with both keys inside. Model fallback chains and `chat_reserved_slots` are in `models.yaml` (05); images, ports on the container side and secrets in `herness.yaml: deploy` (10); sync and reconcile crons in `sources.yaml` (01).

```yaml
resilience:
  retry:
    policies:
      source_http_page: {attempts: 6, base_s: 1,   cap_s: 60,  max_elapsed_s: 600,  connect_timeout_s: 10, retry_after_cap_s: 300}
      llm_local:        {attempts: 4, base_s: 2,   cap_s: 30,  max_elapsed_s: 900,  retry_after_cap_s: 60}
      llm_large:        {attempts: 2, base_s: 30,  cap_s: 120, max_elapsed_s: 7200, retry_after_cap_s: 60}
      llm_cloud:        {attempts: 5, base_s: 2,   cap_s: 60,  max_elapsed_s: 900,  retry_after_cap_s: 120}
      decider_local:    {attempts: 3, base_s: 2,   cap_s: 20,  max_elapsed_s: 300,  timeout_s: 30, retry_after_cap_s: 30}
      decider_cloud:    {attempts: 3, base_s: 2,   cap_s: 30,  max_elapsed_s: 300,  timeout_s: 30, retry_after_cap_s: 120}
      embed_batch:      {attempts: 3, base_s: 2,   cap_s: 20,  max_elapsed_s: 300,  timeout_s: 120}
      tool_store:       {attempts: 3, base_s: 0.5, cap_s: 4,   max_elapsed_s: 30}
      warehouse_read:   {attempts: 3, base_s: 1,   cap_s: 10,  max_elapsed_s: 60,   timeout_s: 600}
      sqlite_write:     {attempts: 6, base_s: 0.2, cap_s: 5,   max_elapsed_s: 30}
    job_backoff: {base_s: 60, cap_s: 3600}
  breakers:
    source:  {failure_threshold: 8, cooldown_s: 300, cooldown_max_s: 3600}
    model:   {failure_threshold: 5, cooldown_s: 60,  cooldown_max_s: 900}
    decider: {failure_threshold: 5, cooldown_s: 60,  cooldown_max_s: 900}
    restart_max_per_hour: 1
  fallback:
    max_repairs: 2
    decider_chain:                        # per active profile; model chains live in models.yaml
      local:   [laya, openjev, llm]
      hybrid:  [laya, openjev, jev, llm]
      premium: [jev, llm]
  loop: {checkpoint_min_interval_s: 5, stop_on_signal_no: 2}
  tasks: {max_task_attempts: 3}
  jobs:
    lease_s: 300
    heartbeat_s: 30
    reaper_interval_s: 30
    tick_s: 2
    cancel_grace_s: 60
    shutdown_grace_s: 120
    stall_timeout_s: 1800
    stall_timeout_large_s: 7200
    cpu_slots: 2
    exclusive_kinds: [build_pipeline, distill, maintenance]
    max_attempts: {sync: 3, reconcile: 2, build_pipeline: 2, distill: 2, review: 3, chat: 2,
                   outcome_measure: 3, memory_maintenance: 2, maintenance: 2, eval: 2}
  gpu:
    compose_cmd: [wsl.exe, -d, herness, --, docker, compose, --env-file, /opt/herness/docker.env]
    compose_file: /mnt/d/herness/docker/compose.yaml     # WSL path (spec 10 §5.6.4)
    vram_check_cmd: [nvidia-smi, --query-gpu=memory.used, --format=csv,noheader,nounits]   # Windows nvidia-smi
    vram_free_threshold_mb: 2000
    stop_timeout_s: 120
    warmup_timeout_s: 120
    classes:                              # compose profile = class name (spec 10 §5.6.2)
      reasoning:
        services:
          vllm-reasoning: {url: "http://127.0.0.1:8000", health: {path: /health}, start_timeout_s: 900}
      decider:
        services:                         # not started at class entry; ctx.services.start("openjev")
          openjev: {url: "http://127.0.0.1:8100", health: {path: /v1/models, bearer_secret: openjev.api_key},
                    start_timeout_s: 900, start_on_entry: false}
      large:
        services:
          llamacpp-large: {url: "http://127.0.0.1:8200", health: {path: /health}, start_timeout_s: 1200}
schedule:
  windows:
    - {name: morning_prep, start: "06:00", end: "08:00", classes: [reasoning], preload: reasoning}
    - {name: chat,         start: "08:00", end: "19:00", classes: [reasoning], preload: reasoning, hard_start: true}
    - {name: enrichment,   start: "19:00", end: "21:00", classes: [decider], overrun_max_min: 120}
    - {name: reviews,      start: "21:00", end: "06:00", days: [MON, TUE, WED, THU, FRI, SAT], classes: [decider, reasoning], overrun_max_min: 60}
    - {name: deep,         start: "21:00", end: "06:00", days: [SUN], classes: [decider, reasoning, large], overrun_max_min: 60}
  preempt_grace_min: 15
  batch_in_chat_min_priority: 70
  chat:
    off_hours: small_model                # small_model | defer | cloud   (D4 default small_model)
    in_hours_unavailable: small_model     # small_model | defer | cloud
  rekey: {cron: "0 19 * * SAT", min_notice_h: 12}
  jobs:
    - {name: nightly, cron: "0 19 * * *", catch_up_max: 6h,
       job: {kind: build_pipeline, gpu_class: decider, payload: {stages: [build, enrich, score, dq, promote]}},
       then: [{kind: review, gpu_class: reasoning, payload: {pipeline: funding_review, depth: standard}, skip_on: [SUN]},
              {kind: review, gpu_class: reasoning, payload: {pipeline: org_review, depth: standard}, skip_on: [SUN]},
              {kind: eval, gpu_class: reasoning, payload: {suite: golden}, enabled: false}]}
    - {name: weekly_deep, cron: "0 21 * * SUN", catch_up_max: 12h,
       job: {kind: review, gpu_class: reasoning, payload: {pipelines: [funding_review, org_review], depth: deep}}}
    - {name: memory_maintenance, cron: "0 5 * * *", catch_up_max: 24h, job: {kind: memory_maintenance, gpu_class: none}}
    - {name: outcomes, cron: "0 6 * * MON", catch_up_max: 48h, job: {kind: outcome_measure, gpu_class: none, payload: {sweep: true}}}
  maintenance: {catch_up_max: 12h}        # time comes from backup.nightly_at (spec 10)
```

Validation (spec 10 loader): windows cover each minute of the week exactly once; every window class is a key of `gpu.classes`; cron strings parse; `then` kinds are valid `JobKind`s; `compose_file` reachable through `compose_cmd` (spec 10). Fault plans are not config; they come only from `HERNESS_FAULTS`.

## 8. Performance targets

| Operation | Target |
|---|---|
| Retry wrapper overhead per call (no retry, breaker cached) | < 50 µs |
| Enqueue | p95 < 10 ms |
| Claim with 10 000 queued jobs | p95 < 20 ms |
| Requeue after owner death | ≤ `lease_s` + 30 s; same-host restart ≤ 10 s |
| Breaker open seen by all processes | ≤ 5 s |
| Swap reasoning → decider (stop, VRAM check) | < 2 min |
| `ctx.services.start("openjev")` incl. warm-up | < 5 min |
| Swap decider → reasoning (30B-class load, warm-up) | < 6 min |
| `chat_policy(now)` | < 5 ms (reads cached worker row, ≤ 5 s stale) |
| `herness resume` → first task running (worker idle, class loaded) | < 30 s |
| Supervisor idle CPU | < 1 % of one core |

## 9. Security

- Compose, `wsl.exe` and `nvidia-smi` run as argument lists (`shell=False`); service names must be service keys under `resilience.gpu.classes`. No user input reaches them. The OpenJev bearer key is resolved through spec 10 secrets and never logged.
- `last_error`, `result`, `resilience_event.detail`, traces and logs pass through `herness.core.redact` (spec 10); HTTP error bodies are truncated to 500 chars before redaction; no ticket text, prompts or secrets.
- Payloads carry references (`run_id`, `session_id`, `message_id`, source names), never secrets or record text.
- Fallback never crosses the network boundary unless the active profile allows egress (D5); every off-network call still passes the egress guard (spec 10), and `EgressBlocked` falls back to a local profile.
- `Retry-After` is capped, so a broken server cannot park a call indefinitely.
- Fault hooks are inert without `HERNESS_FAULTS`; when set, status and logs say so.
- `data/locks/` and the ops store are restricted to the service account (spec 10).

## 10. Tests and acceptance criteria

Unit (`tests/unit/core/`):
- Full-jitter bounds over 10 000 samples; `RateLimited(retry_after=7)` sleeps ≥ 7 s (fake clock); `retry_after` above the cap re-raises.
- `classify()` maps each foreign exception in §5.2 to the listed class.
- Breaker: every row of the §5.3 table; `force_open` on `AuthError`; two processes racing for the probe → exactly one wins.
- Repair: 2 malformed then valid → success with 2 `repair` events; 3 malformed → `OutputValidationError` → next candidate; `ModelRefused` and `EgressBlocked` → next candidate without retry.
- `candidates()` over `chain_for(role, depth)`: off-network clients removed under `local`; `local-large-offload` removed unless `large` is loaded.
- Claim SQL: 8 threads × 1 000 claims over 5 000 jobs → no double claim, priority order kept, `exclusive_kinds` never concurrent.
- `schedule_rekey()`: lands on the next `schedule.rekey.cron` fire ≥ 12 h ahead; claims before the same night's `build_pipeline`; that night's chained standard reviews are skipped.
- Idempotent enqueue; a finished `sched:` key is not re-fired; `sync:<source>` dedupe.
- Cron parser table tests; DST gap and fold; catch-up within and beyond `catch_up_max`.
- `chat_policy` truth table: each window × loaded class × breaker state × profile egress → expected `ChatMode` (`cloud` never returned under `local`); `chat_next_live_at` matches the window table.
- `recover_run_tasks`: running → pending without attempt change; `done` untouched; `failed` below the cap → pending; dead only with `retry_dead`.
- Checkpoint: `save_checkpoint` keeps an existing `scratchpad`; over 4 MB drops `loop`.

Fault suite (`tests/fault/`, spec 11 §5.5 scenarios; this spec adds acceptance for its own parts):

| # | Injection | Acceptance |
|---|---|---|
| F1 | fake LLM `die` mid-review | retries, `model:*` breaker opens, fallback or tasks pend; run `done`; no `done` task has `attempts > 1` |
| F2 | `llm.output malformed_json count=2` / `count=3` | 2 repairs and success / 1 fallback |
| F3 | `sql.query timeout p=0.2` | `QueryError` results with hints; run completes |
| F4 | `http.page http_429 retry_after=7 count=3` | 3 `retry` events with `retry_after_s=7`; watermark advanced once |
| F5 | 10 consecutive 503s on one source | breaker `open`; sync `done` with `skipped_open_circuit`; `half_open` after cooldown; closed after a good probe |
| F6 | `swarm.after_finding_write kill`, then `herness resume` | run finishes; finding count equals a clean run; no duplicate findings or evidence |
| F7 | `job.after_claim kill`, worker restart | requeued ≤ 10 s and completed; without restart, requeued ≤ `lease_s` + 30 s |
| F8 | `pipeline.before_promote kill` | `CURRENT` unchanged; rerun promotes |
| F9 | `gpu.after_stop kill`, worker restart | detection leaves exactly one class or none; no two classes running |
| F10 | 08:00 boundary during a decider job | job yields within `preempt_grace_min`, attempts unchanged, finished batches not redone at 19:00 |
| F11 | `sqlite.write error:StoreBusy count=4` | writes succeed on retry; no lost job state |
| F12 | `build_pipeline` with in-job `ctx.services.start("openjev")` then `ctx.require_gpu_class("reasoning")` (fake services) | `worker.gpu_class_loaded` passes decider → reasoning; never two services up; `gpu_swap` events recorded |

Acceptance for Phase 3: F1–F7, F11, F12 (fake services) pass in CI; F8–F10 pass on the target PC with real containers; `status_snapshot()` counts match the suite's `resilience_event` rows.

## 11. Open questions

1. Resolved (spec 06 v2 §5.1): spec 06 calls `recover_run_tasks`; attempts are counted once, in `claim_task`.
2. Resolved (spec 10 v2 §3.7): `herness deploy up|down` sets `worker.requested_class` while a worker is alive, else holds `data/locks/gpu.lock`.
3. Resolved (spec 10 v2 §4.2): `resilience` and `schedule` live in `config/resilience.yaml` (spec 00 §11), not `herness.yaml`.
4. Resolved (spec 10 v2 §5.3): rekey uses `{"action": "rekey"}` and the planned Saturday 19:00 slot (§5.11), skipping that night's chained reviews.
5. Resolved (spec 09 v2 §5.5): the `defer` banner uses `chat_next_live_at(now)`.
6. Resolved (spec 05 v2 §5.7): the trace table lists `repair` and `guard_stop` with the §4.4 fields.
7. OpenJev warm-up via `POST /v1/systemone` and health via `GET /v1/models` must be verified against the pinned image at Phase 4 (spec 10 §5.6.2).
8. Resolved (coordinator decision, 2026-09-24): one hooks class, spec 05 `HarnessHooks`, with no `LoopGuard`, `SwarmHooks` or `ResilienceHooks`. Loop detection runs in the spec 05 loop. This spec owns `ModelChain` (§3.2 signature), `complete_validated`, `loop_signal_policy` and `save_checkpoint`. Spec 06 owns `RunBudget`. Spec 05's `Tracer` is the only trace writer, and there is no `set_trace_sink`.

## 12. Dependencies

- Spec 00: taxonomy §7 (incl. `CircuitOpen`, `ModelRefused`, `EgressBlocked`), IDs §5, ops access §4, logging and time §8, config files §11, interfaces §12.3, D3/D4/D5.
- Spec 02: `job`, `worker`, `resilience_event` (§5.2), `source_health` (§5.1), `task`, `run` (§5.3), build pipeline stages (§4.1).
- Spec 01: `retry_page`, `guard`, `Connector.check()`, per-source crons, sync and reconcile handlers.
- Spec 03: `ctx.require_gpu_class`, `ctx.services.start/stop/healthy`, `ctx.heartbeat`, `ctx.should_yield`, `ctx.save_state` in `build_pipeline` and `distill`; `DeciderChain`; `Decider.health()`; rekey GPU functions.
- Spec 04: scoring steps checkpoint via `ctx.save_state`.
- Spec 05: `LoopHooks` / `HarnessHooks` (calls `ModelChain.acomplete`, `loop_signal_policy`, `save_checkpoint`), `LoopState.loop_signal()`, `LoopCheckpoint`, `Tracer`, `GatedClient`, `LLMRegistry.chain_for`, client keys and `chat_reserved_slots`.
- Spec 06: `recover_run_tasks`, `save_checkpoint`, `complete_task`, `release_task`, call gates, `RunBudget`, `ctx.gpu_scope("large")`, `chat` job handler, `chat_policy` for escalation.
- Spec 07: `outcome_measure` (one-off and weekly sweep), `memory_maintenance` handlers; owns `checkpoint.scratchpad`.
- Spec 09: CLI wiring, dashboard dead-letter and status pages, `chat_policy` / `chat_next_live_at` banners.
- Spec 10: config loader, compose services and ports, `gpu.compose_cmd` default, secrets (OpenJev bearer), redaction, egress guard, service install, `maintenance` handler incl. rekey.
- Spec 11: fault plans, fake servers, suite in §5.5.
- Libraries: `tenacity>=9`, `httpx`, `structlog`, `psutil`, `tzdata`, stdlib `sqlite3`, `zoneinfo`, `multiprocessing`, `signal`.

## 13. Contract changes (resolved)

1. `job.kind` additions (`outcome_measure`, `reconcile`, `chat`, plus `memory_maintenance`, `maintenance`) → spec 02 §5.2; handlers and GPU classes in §5.1 here.
2. `job.idem_key`, `job.result`, partial unique index and claim index → spec 02 §5.2.
3. `source_health.trips`, `updated_at`, `source` PK and key convention → spec 02 §5.1.
4. `worker` table → spec 02 §5.2.
5. `resilience_event` table → spec 02 §5.2.
6. `CircuitOpen(RetryableError)` with `.key`, `.retry_at` → spec 00 §7.
7. Index `task(run_id, status)` → spec 02 §5.3.
8. Fixed-width SQLite timestamps → spec 00 §8.
9. `config/resilience.yaml` → spec 00 §11 and spec 10 §4.2.
10. `tzdata`, `psutil` dependencies → spec 00 §9.
11. Trace events `repair`, `guard_stop` → spec 00 §8 and spec 05 §5.7.
12. Types `JobSpec`, `JobContext`, `JobOutcome`, `ChatMode` owned by 08 → spec 00 §6; `jobs.chat_policy` and task helpers → spec 00 §12.3.
