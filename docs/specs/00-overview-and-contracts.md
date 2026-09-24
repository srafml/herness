# 00 — Overview and Shared Contracts

Status: Draft v2 · 2026-09-24 · Source: approved plan (`C:\Users\santh\.claude\plans\i-want-create-build-purring-alpaca.md`) and architecture page (`docs/architecture.html`).

Every component spec (01–11) builds on the contracts in this file. When a component spec disagrees with this file, this file wins, and the conflict gets fixed here first.

v2 folds in the contract changes requested by specs 01–11 and the cross-spec decisions in §12.

## 1. Spec index

| # | Spec | Package / path | Phase |
|---|------|----------------|-------|
| 00 | Overview and shared contracts (this file) | `herness/core/` | 1 |
| 01 | Connectors (ingestion) | `herness/connectors/` | 1, 6 |
| 02 | Data model: lake, warehouse, ops store | `herness/model/`, `herness/store/` | 1 |
| 03 | Enrichment: embeddings, clustering, deciders | `herness/enrich/` | 4 |
| 04 | Metrics, scoring, portfolio optimizer | `herness/metrics/` | 2 |
| 05 | Harness core: LLM clients, agent loop, tools, roles, Verifier | `herness/harness/` | 3 |
| 06 | Swarm orchestration, blackboard, review and chat pipelines | `herness/harness/swarm.py`, `blackboard.py`, `pipelines/` | 3 |
| 07 | Memory | `herness/harness/memory/` | 3 |
| 08 | Resilience: retries, breakers, fallbacks, job queue, worker, GPU arbitration | `herness/core/resilience.py`, `herness/core/jobs.py` | 3 |
| 09 | Outputs: reports, dashboard, chat UI, CLI | `herness/reports/`, `app/`, `herness/cli.py` | 5 |
| 10 | Configuration, secrets, redaction, egress guard, deployment | `config/`, `docker/`, `herness/core/{config,registry,secrets,redact,egress,audit}.py` | 1 |
| 11 | Testing, evaluation, synthetic data | `tests/`, `tools/synth_data.py`, `herness/eval/` | 1–7 |
| — | [Open questions](open-questions.md): decisions needed from the product owner and items to verify per phase | — | — |

Each component spec uses the same sections: Purpose and scope · Responsibilities · Interfaces · Data contracts · Behavior · Errors and resilience · Configuration · Performance targets · Security · Tests and acceptance criteria · Open questions · Dependencies · (Requested contract changes, now resolved here).

## 2. Principles (binding on every component)

1. **Numbers come from SQL.** No model output is ever used as a number in a metric, score or report. A model may classify text (a label with a probability), choose which query to run, and write prose around numbers that came from a tool result.
2. **Every number is traceable.** Any figure in a finding, report or chat answer is a `NumberRef` carrying a `query_id` (§5, §12.1). The Verifier re-runs it.
3. **Deterministic core, pluggable edges.** Connectors, models, deciders and output channels sit behind protocols (§6), and config selects the implementation. Option A, hybrid and Option B are config profiles, not forks.
4. **Idempotent and resumable.** Every job can be killed and rerun without duplicating data or redoing finished work.
5. **Local by default.** Nothing leaves the machine unless a config profile explicitly enables an off-network backend, and then only through the egress guard after redaction (spec 10).
6. **Small, plain Python.** No agent framework (no LangGraph). Standard library plus the pinned dependencies in §9.

## 3. Runtime and repository layout

- Python 3.12, managed by `uv`. Target OS: Windows 11 with WSL2 for GPU containers. Code must also run on Linux.
- Single package `herness`, installed in editable mode. Entry point: `herness` (Typer CLI, spec 09).
- Each package that owns a config section has a `settings.py` with its pydantic section model (spec 10).

```text
d:\herness\
  pyproject.toml  uv.lock  README.md  .env.example
  config/            (file list in §11)
  docker/            compose.yaml
  docs/              architecture.html  specs/
  herness/
    core/            config.py registry.py secrets.py redact.py egress.py audit.py
                     ids.py errors.py types.py logging.py time.py resilience.py jobs.py
    store/           lake.py warehouse.py ops.py vectors.py migrations/
    connectors/      base.py runner.py servicenow.py jira.py monitoring/ mongodb.py snowflake.py dataverse.py files.py
    model/           build.py sql/  (numbered .sql files)
    enrich/          embed.py cluster.py decide.py deciders/ distill.py link_changes.py mapping_suggest.py
    metrics/         catalog.py compute.py facts.py scoring.py levers.py portfolio.py evidence.py
    harness/         llm/ loop.py tools.py roles/ verifier.py tracing.py
                     swarm.py blackboard.py pipelines/ memory/
    reports/         contract.py render.py charts.py templates/
    eval/            golden.py runner.py grading.py report.py
    cli.py
  app/               Home.py pages/ common/
  tools/             synth_data.py
  tests/             unit/ integration/ fault/ eval/ bench/ support/ fixtures/
  data/              (gitignored, layout in §4)
```

## 4. Storage layout

DuckDB allows one writing process per file, and many readers only while nobody writes. The Streamlit app, chat and CLI jobs run as separate processes, so continuously changing state cannot live in the warehouse file. Storage is therefore split:

| Store | Technology | Path | Writers | Readers | Holds |
|-------|-----------|------|---------|---------|-------|
| Inbox | files | `data/inbox/<entity>/` | people, other systems | files connector | CSV/Excel/Parquet drops |
| Raw lake | Parquet (zstd) | `data/raw/<source>/<entity>/dt=YYYY-MM-DD/part-<ulid>.parquet` | connectors | model build | Source records as fetched, append-only |
| Decision cache | Parquet | `data/cache/decisions/<question_set_version>/` | enrichment | build, enrichment | Raw decider outputs keyed by content hash (schema in spec 03) |
| Labels | Parquet | `data/labels/<question_set_version>/{teacher,human,gold}/` | enrichment, review UI | distillation, eval | Teacher labels, human spot-checks, frozen gold set |
| Warehouse | DuckDB | `data/warehouse/wh-<build_id>.duckdb`, pointer `data/warehouse/CURRENT` | build pipeline job only | everything, read-only | Canonical model, labels, metrics, scores, `meta.*` |
| Ops store | SQLite, WAL | `data/ops.sqlite` | all processes | all processes | Jobs, runs, tasks, findings, evidence, memory, reviews, chat, watermarks, health |
| Vectors | LanceDB | `data/vectors/` | enrichment, memory | tools, memory | Ticket and memory embeddings |
| Models | files | `data/models/<name>/<version>/` + `CURRENT` | distillation, enrichment | deciders, clustering | Laya checkpoints, calibration, cluster snapshots, LoRA adapters, `eval.json` gate results |
| Traces | JSONL | `data/traces/<run_id>.jsonl` | harness | eval, dashboard | Model calls, tool calls, retries, fallbacks, verdicts |
| Logs | JSONL | `data/logs/herness-<date>.jsonl`, `egress-<date>.jsonl`, `audit-<date>.jsonl` | all | admins | App log, egress audit, approvals/config audit |
| Config snapshots | YAML | `data/config_snapshots/<config_hash>.yaml` | config loader | audit | Effective config per hash |
| Reports | files | `data/reports/<run_id>/` (`draft.json`, `report.html`, `report.md`, `manifest.json`, optional PDF) | pipelines (06), renderer (09) | users | Review outputs |
| Eval | files | `data/reports/eval/<run_id>/`, `data/cache/judge/`, `data/bench/` | eval harness | developers | Eval reports, judge cache, benchmarks |
| Synthetic | files | `data/synth/<seed>-<scale>/` | generator | tests | Generated lakes and truth files |

**Blue/green warehouse.** `herness build` writes a new file `wh-<build_id>.duckdb`. Enrichment and scoring write into that same file. When the pipeline finishes and passes its checks, it atomically replaces the text in `CURRENT` (write temp file, then `os.replace`). Readers open `CURRENT` read-only and re-check it every 60 s. The last 3 builds are kept. Deleting an old build retries later if a reader still holds it open (Windows file locks). A failed build never touches `CURRENT`.

**Ops store access.** SQLite in WAL mode, `busy_timeout=10000`, one connection per thread. All writes go through `herness.store.ops` functions. DuckDB may `ATTACH` the ops store read-only for joins.

## 5. Identifiers

All IDs are strings. ULIDs come from `herness.core.ids.new_ulid()`.

| ID | Format | Notes |
|----|--------|-------|
| `record_id` | `<source>:<entity>:<source_key>` | `source_key` is the source's immutable key: `sys_id` for ServiceNow, the numeric issue **`id`** for Jira (keys change when issues move projects; `key` is kept as a column). |
| `build_id` | `YYYYMMDD-HHMMSS-<ulid6>` | One per warehouse file. |
| `run_id` | `run_<ulid>` | Reviews, chat turns batches, evals. |
| `task_id` | `task_<ulid>` | Swarm tasks. |
| `job_id` | `job_<ulid>` | Queue jobs. |
| `finding_id` | `fnd_<ulid>` | Blackboard entries. |
| `rec_id` | `rec_<ulid>` | Recommendations. |
| `outcome_id` | `out_<ulid>` | Outcome measurements. |
| `memory_id` | `mem_<ulid>` | Memory items. |
| `cluster_id` | `cl_<ulid>` | Stable across nightly runs (spec 03). |
| `item_id` | `rev_<ulid>` | Review queue items. |
| `request_id` | `del_<ulid>` | Deletion requests (spec 10). |
| `egress_id`, `audit_id` | `egr_<ulid>`, `aud_<ulid>` | Audit log lines. |
| `config_hash` | `cfg_` + 16 hex of SHA-256 over the canonical effective config | Recorded on builds and runs. |
| `query_id` | `q_` + 16 hex of SHA-256 over canonical JSON `{"sql": normalized_sql, "params": params, "build_id": build_id}` | Same query on the same build always gets the same ID. `normalized_sql` = whitespace collapsed, trailing semicolon removed. |
| `content_hash` | 32 hex of SHA-256 over the redacted classifier input text | Decision cache key with question fingerprint and decider version. Rotating the redaction key changes it (spec 10). |

### 5.1 Result hash (shared by specs 04 and 05)

`result_hash` = SHA-256 hex over `header + "\n" + "\n".join(sorted(row_hashes))`. `header` is canonical JSON of `[[column_name, duckdb_type], ...]`. Each `row_hash` is SHA-256 of canonical JSON of the row: keys in column order, floats as `format(x, ".9g")`, DECIMAL as string, timestamps ISO-8601 UTC with `Z`, NULL as `null`. Sorting makes the hash independent of row order. Implemented once in `herness.metrics.evidence.result_hash()` and imported by the harness.

## 6. Shared protocols and types

Protocols are `typing.Protocol` classes; implementations are registered with `herness.core.registry.register(kind, name)` and resolved with `get(kind, name)` (spec 10).

```python
# herness/connectors/base.py  (spec 01)
class Connector(Protocol):
    name: str
    entities: tuple[str, ...]
    def check(self) -> None: ...
    def sync(self, entity: str, since: datetime | None, until: datetime | None = None) -> Iterator[pa.RecordBatch]: ...
    def watermark_field(self, entity: str) -> str: ...

class SupportsKeyListing(Protocol):          # optional, for weekly reconciliation
    def list_keys(self, entity: str) -> Iterator[pa.RecordBatch]: ...

# herness/enrich/decide.py  (spec 03)
class Decider(Protocol):
    name: str; version: str
    def decide(self, items: Sequence[DecisionInput], questions: QuestionSet) -> list[DecisionOutput]: ...
    def health(self) -> None: ...

# herness/harness/llm/base.py  (spec 05)
class LLMClient(Protocol):
    name: str
    def complete(self, req: LLMRequest) -> LLMResponse: ...
    async def acomplete(self, req: LLMRequest) -> LLMResponse: ...

# herness/harness/tools.py  (spec 05)
class Tool(Protocol):
    name: str; description: str; input_schema: dict
    def __call__(self, ctx: ToolContext, **kwargs) -> ToolResult: ...
```

Shared pydantic types live in `herness/core/types.py`. Each has exactly one owning spec; other specs import, never redefine.

| Owner | Types |
|-------|-------|
| 03 | `DecisionInput`, `DecisionOutput`, `QuestionSet`, `Question`, `Answer` |
| 05 | `Message`, `ToolCall`, `LLMRequest`, `LLMResponse`, `Usage`, `Budgets`, `ToolContext`, `ToolResult`, `NumberRef`, `Evidence`, `AgentResult`, `LoopState`, `VerificationResult`, `LoopHooks`, `HarnessHooks`, `GatedClient`, `Tracer`, `BudgetLedger` |
| 06 | `RunBudget`, `TaskSpec`, `EntityScope`, `TaskInputs`, `TaskBudget`, `Finding`, `Challenge`, `CheckResult`, `CrossCheck`, `VerificationRecord`, `ReportDraft` (+ `Section`, `Paragraph`, `RecommendationItem`, `RankedEntity`), `ChatAnswer`, `ChatEvent`, `Coverage` |
| 07 | `MemoryItem`, `MemoryProposal`, `RecallHit`, `RecommendationDraft`, `MemoryRunContext`, `PriorRecommendation`, `PriorContext`, `ConfidenceAdjustment` |
| 08 | `JobSpec`, `JobContext`, `JobOutcome`, `ChatMode`, `ModelChain`, `loop_signal_policy` |
| 09 | `ReportManifest` |

## 7. Error taxonomy

`herness/core/errors.py`. Every raised error is one of these, so the resilience layer (spec 08) decides without string matching.

```text
HernessError
├── RetryableError                 # retried with backoff
│   ├── SourceUnavailable
│   ├── RateLimited                # .retry_after: float | None
│   ├── ModelUnavailable
│   ├── StoreBusy
│   └── CircuitOpen                # .key, .retry_at
├── RecoverableError               # caller repairs and retries differently
│   ├── OutputValidationError      # → repair prompt (max 2), then fallback model
│   ├── ToolInputError             # → error tool result
│   ├── QueryError                 # → error tool result with hint
│   ├── ModelRefused               # .category; Claude stop_reason "refusal" → fallback chain
│   ├── PolicyViolation            # memory write policy / injection scan → pending or rejected
│   └── ReportContractError        # draft fails the rendering contract
└── FatalError                     # no retry; job fails, task goes to dead letter
    ├── ConfigError
    ├── AuthError
    ├── SchemaViolation
    ├── BudgetExceeded
    ├── PermissionDenied           # role not allowed (dashboard/CLI)
    └── EgressBlocked              # egress guard refused an off-network call
```

## 8. Logging, tracing, time, money

- **Logging**: `structlog` JSON lines to stderr and `data/logs/herness-<date>.jsonl`. Required keys: `ts`, `level`, `event`, `component`, plus `run_id` / `task_id` / `job_id` / `build_id` when known. No ticket text, personal data or secret values above DEBUG; a scrubber removes known secret values (spec 10).
- **Tracing**: one JSONL event per model call, tool call, retry, repair, fallback, guard stop, compaction and verifier verdict in `data/traces/<run_id>.jsonl` (schema in spec 05 §5.7; spec 08 adds `repair` and `guard_stop`). Spec 05's `Tracer` is the only trace writer; spec 08 functions take an optional `tracer` argument.
- **Time**: timezone-aware UTC. DuckDB uses `TIMESTAMPTZ`. SQLite stores fixed-width text `YYYY-MM-DDTHH:MM:SS.ffffffZ` so text order equals time order. Business-hour logic uses `weights.yaml: business_timezone` with `tzdata`.
- **Money**: USD, `DECIMAL(18,2)` in DuckDB, `Decimal` in Python, string in JSON. Derived dollar values name their rates in evidence.

## 9. Dependencies (minimum versions; exact pins in `uv.lock`)

| Area | Packages |
|------|----------|
| Data | `duckdb>=1.3`, `pyarrow>=17`, `polars>=1.10`, `numpy`, `scipy` |
| Config / types | `pydantic>=2.9`, `pydantic-settings>=2.5`, `pyyaml`, `jsonschema` |
| CLI / logging | `typer>=0.12`, `structlog>=24`, `rich` |
| HTTP / retry / OS | `httpx>=0.27`, `tenacity>=9`, `tzdata`, `psutil` |
| Security | `keyring`, `pyahocorasick`; optional extra `ner`: `presidio-analyzer`, `spacy` |
| Sources | `pymongo>=4.8`, `snowflake-connector-python[pandas]>=3.12`, `msal>=1.31` |
| ML / text | `torch` (CUDA), `sentence-transformers>=3`, `scikit-learn>=1.5`, `lancedb>=0.13`, `rapidfuzz>=3`, `laya` (pinned at Phase 4) |
| LLM / SQL | `openai>=1.50`, `anthropic` (1.x), `sqlglot` |
| Optimization | `ortools>=9.10` |
| Outputs | `jinja2>=3.1`, `streamlit>=1.39`, `weasyprint` (optional) |
| Dev / tests | `pytest`, `pytest-asyncio`, `pytest-cov`, `pytest-benchmark`, `pytest-timeout`, `hypothesis`, `respx`, `freezegun`, `mongomock`, `ruff`, `mypy`, `pre-commit` |

Containers (spec 10): vLLM OpenAI-compatible server for the reasoning model; `razorback16/openjev:0.4.0` pinned by digest.

## 10. Open decisions carried from Step 0

| # | Question | Default until answered | Affects |
|---|----------|------------------------|---------|
| D1 | Source of truth for service ↔ group ↔ Jira project ↔ org mapping | CMDB plus `mappings.yaml` overrides | 01, 02, 04 |
| D2 | Dollar weights | Placeholders flagged `unconfirmed: true`; reports show a banner | 04, 09 |
| D3 | Review cadence | Nightly standard reviews, weekly deep mode (Sunday 21:00) | 06, 08 |
| D4 | Chat hours | 08:00–19:00 business timezone; outside: smaller model | 08, 10 |
| D5 | Hybrid allowed by data policy | Disabled; `hybrid`/`premium` fail validation without a recorded approval | 05, 10 |
| D6 | Incidents count against resolving team or service owner (spec 04) | Resolving team for org scores; service owner for funding attribution | 04 |
| D7 | OpenJev weights on a non-Blackwell 24 GB GPU (spec 10) | Verify at Phase 4 before relying on OpenJev; fallback teacher = local reasoning LLM decider | 03, 10 |

## 11. Configuration files

Spec 10 owns loading, precedence, profiles and validation. Section contents are owned as listed.

| File | Contents | Owner |
|------|----------|-------|
| `config/herness.yaml` | paths, security (data policy, egress allowlist), logging, retention, backup, deploy, users/roles allowlist | 10 |
| `config/sources.yaml` | connectors, entities, schedules, DQ thresholds, build settings | 01, 02 |
| `config/mappings.yaml` | enum maps, service overrides, custom fields | 02 |
| `config/decisions.yaml` | typed question sets | 03 |
| `config/metrics.yaml` | metric catalog | 04 |
| `config/weights.yaml` | dollar rates, strategic weights, business timezone | 04 |
| `config/models.yaml` | model profiles (endpoint, context_window, max_output_tokens, tokenizer, max_concurrency, prices), role→model mapping, fallback chains | 05 |
| `config/pipelines.yaml` | swarm limits, pipeline definitions, depth modes | 06 |
| `config/memory.yaml`, `config/injection_patterns.txt` | memory policy, retrieval, compaction, outcomes | 07 |
| `config/resilience.yaml` | retry policies, breakers, job queue, GPU windows, schedules, chat-hours policy | 08 |
| `config/app.yaml` | dashboard, chat UI, reports, CLI | 09 |
| `config/eval.yaml` | golden suite, thresholds, judge | 11 |
| `config/profiles/{local,hybrid,premium,synth}.yaml` | overlays | 10 |

## 12. Cross-spec decisions (resolved conflicts)

### 12.1 Numbers in model-written text

Model-written text (finding claims, report paragraphs, recommendation summaries, chat answers) contains numbers only as markers `[[<id>]]`, where `<id>` matches `n[0-9]+` and is unique within the object that carries the text. The object has a `numbers: list[NumberRef]` field, and every marker resolves to the `NumberRef` with that `id`.

`NumberRef` (owner 05): `id`, `value` (JSON number; USD as decimal string), `unit` (`count`, `usd`, `pct`, `ratio`, `hours`, `minutes`, `seconds`, `days`, `score`, `rank`, `other`), `query_id`, `column`, `row_key` (object column→value selecting one row, or null for single-row results), optional `format` (`usd`, `usd_compact`, `int`, `pct1`, `ratio2`, `hours1`, `minutes0`, `prob2`). References between fields use the `id` string (e.g. `expected_usd_ref: "n2"`), never list positions.

Numerals outside markers are allowed only for years, ISO dates, quarters (`Q[1-4] YYYY`) and record identifiers (`INC\d+`, `CHG\d+`, `PRB\d+`, `[A-Z][A-Z0-9]+-\d+`). The Verifier (05) and the renderer (09) apply the same pattern list from `config/app.yaml: reports.allowed_numeral_patterns`.

### 12.2 Report hand-off

The Writer produces `ReportDraft` (owner 06), stored at `data/reports/<run_id>/draft.json` after Verifier gate 2. The renderer (09) reads only `draft.json` plus warehouse and ops tables. `ReportDraft` includes `schema_version`, `ranked_entities` (for eval grading, spec 11) and `rec_id` on each recommendation once spec 07 has written it. Spec 09 §4.1 lists the fields the renderer requires; spec 06 guarantees them.

### 12.3 Interfaces between harness specs

| Caller | Callee | Interface |
|--------|--------|-----------|
| 06 | 05 | `run_agent(role, task_input, ctx, client, profile, hooks, resume_from=None) -> AgentResult`, with `hooks` = spec 05 `HarnessHooks` built by 06 per task (the only `LoopHooks` implementation); per-backend concurrency gates (owned by 06) are applied inside `HarnessHooks.call` through `GatedClient` |
| 05 | 08 | `HarnessHooks` composes `ModelChain.acomplete(req, *, schema=None, client_for, tracer=None) -> tuple[LLMResponse, BaseModel \| None]`, `complete_validated`, `loop_signal_policy(state, signal)` and `save_checkpoint` |
| 05 | 06 | `ToolContext.ledger: BudgetLedger` is implemented by 06 `RunBudget` (raises `BudgetExceeded`) |
| 06 | 05 | `verify_findings(findings, build_id)`, `verify_draft(draft, build_id)`, `verify_answer(answer, build_id)`: thin wrappers over `verify_numbers` |
| 05 | 06 | Tool registry accepts swarm-provided tools `post_finding`, `list_findings`, `request_subtask`, `escalate` |
| 05 | 07 | `LoopHooks.on_context_pressure(state: LoopState) -> list[Message]`, implemented by `MemoryStore.compactor(profile, *, ctx)` |
| 06 | 07 | `MemoryStore.prior_context(run_ctx)`, `MemoryStore.write_recommendations(run_id, recs)` (idempotent per `run_id`), `MemoryStore.promote_procedural(run_id)`, `MemoryStore.session_load(session_id)` / `session_save_turn(session_id, run_id)` (chat) |
| 06 | 08 | `recover_run_tasks(run_id, ...)`, `save_checkpoint`, `complete_task` |
| 09 | 06 | `ChatService.answer(session_id, text, user_ref, mode) -> Iterator[ChatEvent]` in `herness/harness/pipelines/chat.py` |
| 09 | 08 | `jobs.chat_policy(now) -> ChatMode`, `jobs.chat_next_live_at(now)` (ETA for `defer`); the `defer` job itself is enqueued by 06 `ChatService` (`idem_key = chat:<session_id>:<message_id>`) |
| 06 | 08 | `jobs.chat_model_profile(mode)` (chat client per `ChatMode`), `JobContext.gpu_scope("large")` (deep large stage) |

### 12.4 Context compaction is append-only

Compaction never edits earlier turns in place. It returns a new message list: system prompt, a compaction summary message (ledger of every `query_id` and cited number plus notes), and the last K tool-call groups verbatim. For Claude profiles this starts a fresh conversation, because models such as Claude Opus 5.5 reject histories whose earlier thinking blocks were edited.
