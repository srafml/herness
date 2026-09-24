# Herness — Consistency Pass Decisions

Status: v1 · 2026-09-24 · Scope: rulings on the contradictions and design deltas reported by the twelve implementation specs.

Each ruling is binding on every implementation spec. Where a ruling changes a design contract, the design spec change is listed in §9 and is pending until the design specs are edited. Until then this file wins over the design specs for implementation purposes, and each implementation spec cites the ruling ID (`R-nn`) where it applies it.

## 1. Architecture and layering (ENG §2.1 revised)

| ID | Ruling |
|----|--------|
| R-01 | `herness.core.types` is a package with submodules `decisions` (03), `harness` (05), `swarm` (06), `memory` (07), `jobs` (08), `reports` (09), re-exported from `herness.core.types`. An owner's submodule may itself be a package with internal module names of the owner's choosing (for example `herness.core.types.harness.llm`). It holds data types only. Impl 00 owns the package skeleton, the re-export and the ownership check. Each owner spec owns the fields of its submodule. |
| R-02 | Behavioral classes named in design 00 §6 live in their owner packages: `HarnessHooks`, `GatedClient`, `Tracer` in `herness.harness` (05); `RunBudget` in `herness/harness/budget.py` (06); `ModelChain`, `loop_signal_policy` in `herness.core.resilience` (08); `JobContext` in `herness.core.jobs` (08). `ModelChain` refers only to types in `herness.core.types` and receives clients through its `client_for` callable. |
| R-03 | `herness.core.config` may import every package's `settings.py`. Settings modules import only the standard library, pydantic, `herness.core.types` and `herness.core.errors`. Impl 00 encodes this as a named `import-linter` exception. |
| R-04 | L0 code that needs the ops store uses ports: `herness.core` declares a `Protocol`, `herness.store.ops.<area>` implements it, and the composition root (`herness.cli`, `app/common`) binds it. This is the pattern impl 08 already uses; it applies to jobs, tasks, breaker state and metric recording. |
| R-05 | Enrichment (L3) receives model clients as parameters from the composition root and never imports `herness.harness` (impl 03 DD-02 accepted). |
| R-06 | Only `herness.core.egress` builds `httpx` clients and transports. It adds `loopback_http_client(base_url, *, timeout_s)` (plus an async variant) for local model servers (vLLM, OpenJev, llama.cpp), and `source_http_client(source, base_url, *, timeout_s, auth=None)` for httpx-based source connectors, which checks the source's `hosts` allowlist and the egress allowlist and enforces TLS verification. Both are owned by impl 10. Vendor SDKs (Snowflake, `pymongo`, `msal`) may build their own clients for hosts listed in the new per-source key `sources.<name>.hosts` (a list of host names). The socket guard allowlist is the union of those lists, the egress allowlist and loopback. Nothing is derived from `base_url` for SDK sources. |
| R-07 | New L5 package `herness.admin` (impl 10) holds privacy deletion, backup, retention and deploy. |

## 2. Ops store

| ID | Ruling |
|----|--------|
| R-08 | `herness/store/ops/` is a package. Impl 02 §2 defines the area table. The canonical submodules and owners are:<br>• `core.py`, `migrate.py`, `shared.py` (with all `review_item` functions): 02<br>• `ingest.py`: 01<br>• `evidence.py`: 05<br>• `runs.py`, `findings.py`: 06<br>• `memory.py`, `closed_loop.py`: 07<br>• `jobs.py`, `tasks.py`, `worker.py`, `resilience.py`, `metrics.py`: 08<br>• `chat.py`, `ui_reads.py`: 09<br>• `privacy.py`: 10<br>Top-level files named `herness/store/ops_<area>.py` are renamed to `herness/store/ops/<area>.py`. |
| R-09 | A function in an area is specified only in the owner's implementation spec. A non-owner spec that defined one replaces its unit block with a reference to the owner. The owner spec adds a unit for every function in its area that any other spec references. |
| R-10 | The core API names are impl 02's: `connection()`, `run_write()`, `read_one()`, `read_all()`, `dump_json()`, `load_json()`, `migrate()`, `pending_migrations()`, `schema_version()`. These names replace `write_tx`, `write_transaction`, `transaction`, `connect`, `read_connection`, `open_ops_store`, `OpsStore` and `migration_status` wherever they are used. |
| R-11 | Migrations 001–006 (impl 02) create every table named in the design specs. A table or column that exists only in an implementation spec gets a migration in the owner's range: 01 → 010–019, 03 → 020–029, 05 → 030–039, 06 → 040–049, 08 → 050–059, 07 → 070–079, 10 → 080–089, 09 → 090–099. Each such migration is listed as a unit in the owner spec, and impl 02's runner applies all of them in numeric order. |
| R-12 | `metric_sample` table: impl 02 migration 006. Writer `herness.store.ops.metrics.record_metric_samples`: impl 08. Every other spec uses that name (it replaces `write_metric_samples` and `record_metric_sample`). |
| R-13 | Evidence writer: `herness.store.ops.evidence.record_evidence` (05). It replaces `insert_evidence`. |

## 3. Shared implementations

| ID | Ruling |
|----|--------|
| R-14 | `herness.core.ids` (00) owns the single implementations of `canonical_json`, `normalize_sql` and `query_id`. Specs 04 and 05 call them. |
| R-15 | `herness.metrics.evidence` (04) owns `result_hash` with impl 04's pinned canonical rules, and owns `rows_equivalent` and `iter_batch_rows`. The Verifier (05) calls `rows_equivalent` for the cell tolerance of design 04 §4.4. |
| R-16 | New module `herness.core.numbers` (00) owns three things: the numeral scanner that implements design 00 §12.1 (marker pattern `[[n\d+]]`, and the allowed-numeral patterns loaded from `reports.allowed_numeral_patterns`); `NumberRef` formatting for every `format` value; and marker parsing. The Verifier (05) and the renderer (09) both import it. |
| R-17 | Token estimation: `herness.harness.llm.tokens.count_tokens` (05) is the only estimator. Impl 05 adds `LoopState.est_input_tokens()`. Impl 07 uses both and drops its own bytes/3.0 rule. |
| R-18 | `herness.enrich.embed.embed_query` returns a 1-D `numpy.ndarray` of float32. Tool code in 05 converts it to `list[float]` at the tool boundary. |
| R-19 | Error taxonomy additions (00): `NotFound(RecoverableError)`, and `HernessError` gains the optional attributes `hint: str \| None` and `details: dict[str, str]`. Spec-local subclasses (`JobStateError` in 08, the new attributes on `EgressBlocked` and `ConfigError` in 10) are declared by their owners. |

## 4. Harness, swarm and memory

| ID | Ruling |
|----|--------|
| R-20 | Every block of untrusted text in a prompt uses `<untrusted_data source="<source>" record_id="<id or empty>">…</untrusted_data>` (design 10 §9.1). Memory recall uses the same tag with `source="memory"`. The tags `<ticket_text>` and `<memory_context>` are dropped. Before wrapping, any literal `</untrusted_data` in the content is escaped. |
| R-21 | Checkpoint envelope (owner 08): `task.checkpoint` is a JSON object `{schema_version, loop, state, scratchpad}`. `loop` holds the `LoopCheckpoint` fields (owner 05), `state` holds the swarm phase and pending findings (owner 06), and `scratchpad` holds working memory (owner 07). `save_checkpoint(task_id, key, value)` replaces only the named key, inside one write transaction, and never erases other keys. Only 05, 06 and 07 call it, each for its own key. |
| R-22 | `AgentResult.stop_reason` (05) adds `task_budget`, `error_streak` and `wall_clock`. `Budgets` (05) adds `deadline: datetime \| None`, enforced by the loop. The scheduler (06) also enforces the task wall clock. |
| R-23 | `TaskBudget.to_budgets(now)` (06) is the only conversion. Impl 05 removes `Budgets.from_task_budget`. |
| R-24 | Impl 05 uses the retry policy `tool_store`. There is no `sql_tool` policy. |
| R-25 | The context compactor (07) never raises `BudgetExceeded` itself. It charges tokens to the ledger, and the ledger (`RunBudget`, 06) is the only raiser. When summarising fails, the compactor raises `OutputValidationError` and the loop falls back to truncation per impl 05. |
| R-26 | Every tool input schema is compatible with strict mode: all properties are listed in `required`, optional values are nullable, and `additionalProperties` is false. Impl 07 converts its schemas; the `row_key` fallback follows impl 05. |
| R-27 | Tool ownership: impl 06 owns the `post_finding` schema. A rejected post raises `ToolInputError`. The Writer role does not get `propose_memory` (owner 07); impl 05 removes it from the Writer's tools. Few-shot templates are not fetched from memory; impl 07 removes that statement. |
| R-28 | Role temperatures come from impl 05 (Planner 0.2). The Planner outputs `PlannerOutput{tasks: list[PlannedTask], rationale, unknowns}`, where `PlannedTask` is a new type owned by 06 (impl 06 D06-03 accepted), and the swarm expands each `PlannedTask` into a full `TaskSpec`. This unblocks T06-15. |
| R-29 | `run_agent(..., resume_from: LoopCheckpoint \| None)` is typed (05). |
| R-30 | `MemoryStore.prior_context` returns `PriorContext`. `RecommendationDraft` and the base-confidence rule follow impl 07. The recommendation summary limit is 400 characters. |
| R-31 | Deep-mode judge samples `k_samples = 5`. The second dedup pass merges in memory only; there is no new `verified → merged` transition. |
| R-32 | A chat correction is captured after the answer is sent. The answer never mentions the capture; the UI shows a separate `correction_captured` chat event. |
| R-33 | `review_hooks` is removed. Memory items are decided through `MemoryStore.approve` / `MemoryStore.reject` (07); `MemoryStore.decide` records recommendation decisions. Other review items are decided through `herness.store.ops.shared.decide_review_item` (02). Approving a memory item also decides its `review_item` in the same transaction. The CLI `approve` command takes a `memory_id` (09). The dashboard correction form carries `session_id` and `source_message_id`. |
| R-34 | Outcome measurement: `measure_after_weeks = 12` (D22 default) means a 2-week settle period plus a 10-week window. The second measurement happens at 26 weeks with a 10-week window. |
| R-35 | Under chat mode `small_model`, escalation is enqueued for the next live window (impl 06 applies; impl 08 updates). |
| R-36 | A cancelled task is released with `release_task` (08). |
| R-37 | The OpenJev `claim_supported` question is removed from impl 05. The Verifier checks numbers only, and claims are judged by the Skeptic role. |
| R-38 | Chat `cloud` mode uses model purpose `reasoning` with `payload_class = "aggregated_evidence"`. In the `hybrid` profile it is allowed only when `security.data_policy` records approval for `chat` (D5). Otherwise chat falls back to the local model. Impl 10 adds the approval flag. |

## 5. Resilience, jobs and CLI

| ID | Ruling |
|----|--------|
| R-39 | A sync that meets an open circuit ends `done` with outcome `skipped_open_circuit` (08). The next scheduled run retries. |
| R-40 | Fault points are named only in impl 08's registry. The connector point is `connector.before_watermark`. For impl 11 X1 and X5, use the closest existing points (`job.before_complete`, `connector.before_watermark`). If a needed point is missing, impl 08 adds it and impl 11 references it. Fault plans are JSON only and are honoured only when `HERNESS_ENV=test`. In any other environment a plan file is ignored and a `WARNING` event is logged. |
| R-41 | `enqueue(priority: int \| None = None)`: `None` means the per-kind default from impl 08 §4.1. |
| R-42 | Job handlers take one argument, `ctx: JobContext`, and read their payload from `ctx.job.payload` (08). Impl 11 changes `handle_eval` to match. |
| R-43 | GPU class per job: `build_pipeline` starts with no GPU class, and enrichment stages take the `decider` class through `ctx.gpu_scope("decider")`. The classifier eval uses `decider`. |
| R-44 | Worker liveness is `herness.core.jobs.worker_alive()`: a heartbeat within 3 × `heartbeat_s`. Every spec uses it, and the 60 s check in design 09 is dropped. |
| R-45 | CLI commands that start work enqueue a job by default. When no worker is alive they print a warning with the fix. An admin-only `--inline` flag runs the job in-process through `herness.core.jobs.run_inline` (08, kept). This applies to `herness score` too. |
| R-46 | *(Corrected.)* The `herness` CLI's exit codes are exactly design 09 §5.8: 0 success, 1 general failure (including `doctor` FAIL and invalid config found by `config validate`), 2 usage error (Typer only), 3 `ConfigError`, 4 source auth/availability, 5 build blocked, 6 partial run, 7 not found (`NotFound`), 8 store busy, 9 model/GPU unavailable, 10 budget exceeded, 11 permission denied, 12 report contract, 13 egress blocked, 130 interrupted. One addition: **14 eval gate failed** (impl 11). No spec uses 2 for anything but usage errors. The earlier version of this ruling (codes 3 and 4 for validation and eval) is withdrawn; impls 09, 10 and 11 restore and align with this table. |
| R-47 | Impl 09 owns the full CLI command table. It is the union of the options in design 09 and design 10 §3.7, and adds `secrets rekey`, `deploy install` and the `large` GPU class for `gpu load/unload`. |
| R-48 | `run_enrichment(..., stages: Sequence[str] \| None = None)` (03) supports `herness enrich --stage`. |
| R-49 | Writer-dead run: impl 06 writes `draft.json` with `mode: "findings_only"`, holding verified findings and no Writer paragraphs. Impl 09 renders that mode with a banner. `ReportDraft` gains `mode: Literal["full", "findings_only"]` (owner 06). |
| R-50 | Identity header trust: the app trusts `identity_header` only when three things hold: `expose.enabled` is true, `trusted_proxy` is set, and the request's peer address equals `trusted_proxy`. With exposure on, the app binds to loopback. Impl 09 and impl 10 both state both checks. |

## 6. Security and configuration

| ID | Ruling |
|----|--------|
| R-51 | Local model ports: vLLM `127.0.0.1:8000`; OpenJev host `127.0.0.1:8100` (container port 8080); llama.cpp `127.0.0.1:8200`. `local-large-offload` points to 8200. |
| R-52 | A model profile's `context_window` must be ≤ the `max_model_len` the server is started with. Config validation checks this. The default for `local-30b` is `context_window = 32768`. This is a Phase 4 verification item on the real card. |
| R-53 | The OpenJev secret reference is `secret:OPENJEV_API_KEY` (10). Impl 08 health checks use it. |
| R-54 | Privacy deletion (impl 10) adds a step after design 10 §5.5 step 3: `MemoryStore.purge(record_id)` (07). It removes memory items, memory vectors and FTS rows that cite the record. Impl 07 adds `purge` as a unit. |
| R-55 | Egress calls use `herness.core.egress.get_guard()`. The name `guard_egress` does not exist; impl 07 uses the real name. |
| R-56 | Redaction thresholds and the PII corpus size are owned by impl 10, and impl 11 uses its values. Synthetic phone numbers use the full 10-digit fictional form `+1-202-555-01xx`, which satisfies the 9-digit rule. |
| R-57 | The lake is append-only except for three operations: privacy deletion, the retention purge (10) and compaction (if impl 02 has it). |
| R-58 | Development uses an editable install. The target box installs the release wheel through `herness deploy install` after verifying its attestation and SBOM (00, 10). |

## 7. Data contracts and synthetic data

| ID | Ruling |
|----|--------|
| R-59 | Impl 01 owns the Jira raw column-name contract, which is what the connector writes to the lake. Impl 02 staging reads it, and the impl 11 generator writes it. |
| R-60 | CMDB: `busines_criticality` (the source's spelling) is read from `cmdb_ci_service`. Impl 11's generator writes `cmdb_ci_service`, `cmn_department` and `task_sla`, and impl 02 staging tolerates any of them being missing. |
| R-61 | The peer-group key spelling is `team:crit_hi` (04). |
| R-62 | Monitoring watermarks are kept per tool (01 §6, 02 §5.1). |
| R-63 | `sync --full` means a backfill from `backfill.start` to now (01). |
| R-64 | Truth isolation: nothing under `herness/` may reference truth files except `herness/eval/truth.py`. |
| R-65 | Fakes: `tests/support/fake_llm.py` holds `FakeLLMClient` (in-process) and `FakeLLMServer` (HTTP). Scripts live in `tests/fixtures/llm_scripts/`. Both are owned by impl 11. |
| R-66 | `LoopState` gains a loop-signal counter separate from `nudges` (08 D08-04 accepted). `Tracer` exposes `run_id` and `task_id` (08 D08-05 accepted). |

## 8. Resolving cross-spec references

Each implementation spec replaces every `X:<NN>/<symbol>` with `T<NN>-<nn> (<symbol>)`, where `T<NN>-<nn>` is the task card in impl NN whose Units list contains the unit that defines the symbol. When the symbol is defined in several cards, the reference points to the earliest card. When impl NN defines no such symbol, the reference becomes `UNRESOLVED(<NN>/<symbol>)` and is listed in §13 of the spec. The final check requires zero `UNRESOLVED` entries; the owner spec adds the missing unit.

## 8a. Rulings added during the pass

| ID | Ruling |
|----|--------|
| R-67 | Synthetic credentials and tokens in fixtures and generated data start with `synthetic` so secret scanners and the log scrubber can tell them apart (impl 10 D10-15). |
| R-68 | `herness.store.ops` is one flat re-exported namespace, so function and type names are unique across areas. Reads of `run` and `task` belong to impl 06 (`get_run`, `select_runs`, `select_tasks`). UI-only projections in impl 09's `ui_reads.py` take a `ui_` prefix. `deleted_record_ids` belongs to impl 10's `privacy.py`. |
| R-69 | `sources.yaml` has sibling sections: impl 01's `herness.connectors.settings` owns the connector sections, and impl 02's `herness.model.settings` owns `dq` and `build`. The impl 10 root config composes them, and neither settings module imports the other. |
| R-70 | Impl 08 owns the single `parse_retry_after`. It accepts RFC 9110 delay-seconds and HTTP-date, clamps to a configured maximum, and returns None on invalid input. Impl 01 uses it. |
| R-71 | Owner validators that need layers above L0 (for example impl 04 `validate_catalog`) register with impl 10's start-up validation hook. The composition root (impl 09 CLI and `app/common`) runs them after `load_config`. `config validate` and `doctor` also run them. Their issues become `ConfigIssue` rows. |
| R-73 | R-46 exit codes apply to the `herness` CLI. Developer check scripts under `tools/` use `0` pass, `1` findings, `2` usage error. |
| R-74 | `HernessError.details` values are strings. A caller with a list joins it with `, ` or passes a JSON string. |
| R-75 | `herness.core.types` holds no functions. Computed helpers such as `impact_usd` live in the owner package (impl 06: `herness.harness`). |
| R-76 | The `models.yaml` sections `deciders` (impl 03 `herness.enrich.settings`) and `models`/`roles` (impl 05 `herness.harness.llm.settings`) are sibling sections, composed by the impl 10 root config like R-69. |
| R-72 | Settings models hold secret references as strings that match the `secret:` pattern. They are resolved later by `herness.core.secrets`, which settings modules never import. |

## 9. Design spec changes pending

The rulings above change these design specs. They will be edited in a follow-up pass: 00 (§3, §5.1, §6, §7, §9), 01 (§5.1, §5.3), 03 (port, `embed_query`, settings location), 05 (delimiter, stop reasons, Budgets, Writer tools, ports; R-37 removes `claim_supported` from §4.7, §5.5–§5.7, §7), 06 (`PlannedTask`, `ReportDraft.mode`, k), 07 (tool schemas, purge, measurement window), 08 (checkpoint envelope, fault plans, priority, GPU class), 09 (CLI table, exit codes, worker liveness), 10 (§3.1, §3.5, `hosts`, loopback client, deletion step, chat approval flag), 11 (§4.2 mypy scope, §10.5 CI, fakes, fault points, phones).
