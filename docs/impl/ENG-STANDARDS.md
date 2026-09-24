# Herness — Engineering Standards for Implementation Specs

Status: Draft v1 · 2026-09-24 · Applies to: every file in `docs/impl/`.

This document holds the rules every implementation spec (`docs/impl/NN-*.impl.md`) applies, so each spec states them once by reference instead of repeating them. It also defines the template those specs follow.

## 0. Precedence and language

1. **What** the system does (contracts, types, behavior, owners) is set by the design specs in `docs/specs/`. Spec 00 wins over specs 01–11.
2. **How** it is built (structure, coding rules, security controls, tests, tasks) is set by this document, then by the component's implementation spec.
3. An implementation spec never changes a design contract. If it needs one changed, it lists the change in its "Design deltas" section (§13 of the template) and the design spec is updated first.
4. Where this document is stricter than a design spec (for example type checking scope in §3.2), this document applies and the delta is listed in §14.

Keywords **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT** and **MAY** follow RFC 2119. Anything not marked is descriptive.

Implementation specs contain **no code**: no function bodies, no code blocks of Python, SQL or shell. Signatures are given as tables. Literal identifiers that the code must match exactly (module paths, function and class names, config keys, table and column names, log event names, error class names) are written in backticks.

## 1. Audience and granularity

Implementation specs are written for AI coding agents working one task card at a time. An agent reads the task card, the unit specs it references, this document and the linked design-spec sections. It MUST NOT need to make a design decision. If a behavior is not specified, the gap is a spec defect: the agent stops and reports it rather than guessing.

Consequences:

- Every public class, function, method and constant has a unit spec (§12.3).
- Private helpers are named when they carry logic worth testing (parsing, hashing, retry decisions). Trivial helpers are left to the implementer.
- Every branch that the design spec describes has a named test.
- Every task card is small enough to finish and test in one session: at most about 400 changed lines of production code, touching at most 4 production files.

## 2. Architecture rules

### 2.1 Layers and import direction

Packages form layers. A package MAY import from its own layer and any layer below. Upward imports are forbidden and enforced by `import-linter` contracts in `pyproject.toml`.

| Layer | Packages | May import |
|-------|----------|------------|
| L0 Foundation | `herness.core` (config, registry, secrets, redact, egress, audit, ids, errors, types, logging, time, resilience, jobs) | standard library, pinned third-party only |
| L1 Storage | `herness.store` | L0 |
| L2 Ingestion and model | `herness.connectors`, `herness.model` | L0, L1 |
| L3 Derivation | `herness.enrich`, `herness.metrics` | L0–L2 (`metrics` MUST NOT import `enrich`; it reads enrichment output only through warehouse tables) |
| L4 Agents | `herness.harness` (llm, loop, tools, roles, verifier, tracing, swarm, blackboard, pipelines, memory) | L0–L3 |
| L5 Delivery | `herness.reports`, `herness.eval`, `herness.cli`, `app/` | L0–L4 |
| Tooling | `tools/`, `tests/` | anything |

Additional contracts:

- `herness.core.types` is a package with one submodule per owning spec (`decisions` 03, `harness` 05, `swarm` 06, `memory` 07, `jobs` 08, `reports` 09), re-exported from `herness.core.types`. It holds data types only (pydantic models, enums, `TypedDict`s). It imports nothing from `herness` except `herness.core.errors` and `herness.core.ids`. Behavioral classes named in spec 00 §6 (`HarnessHooks`, `GatedClient`, `Tracer`, `RunBudget`, `ModelChain`, `loop_signal_policy`, `JobContext`) live in their owner's package.
- **Settings exception.** `herness.core.config` MAY import the `settings.py` module of any package to assemble the root config model. A `settings.py` module MUST import only the standard library, pydantic, `herness.core.types` and `herness.core.errors`. `import-linter` encodes this as a named exception.
- **Ports for persistence below L1.** Code in `herness.core` (L0) that needs the ops store (`jobs`, breaker state, metric recording) declares a `typing.Protocol` port in `herness.core`. The implementation lives in `herness.store.ops.<area>` (L1). The composition root binds the port at start-up. `herness.core` never imports `herness.store`.
- **Passed-in clients.** L3 code that needs a model client (enrichment deciders) receives an `LLMClient`-shaped callable as a parameter from the composition root. L3 never imports `herness.harness`.
- **Network egress.** Only `herness.core.egress` constructs `httpx` clients and transports, for both non-loopback hosts and loopback model servers (`egress.loopback_http_client`). Vendor SDKs that cannot take an `httpx` transport (Snowflake, `pymongo`, `msal`) MAY construct their own clients, but only for hosts listed explicitly in the source's `hosts` allowlist in `sources.yaml`. The process-wide socket guard (spec 10) enforces the same allowlist.
- `herness.store.ops` is a package with one submodule per owning spec. Connections, transactions and migrations belong to spec 02. It is re-exported so callers import `herness.store.ops.<function>`. Only `herness.store.ops` writes to `data/ops.sqlite`. Only the build pipeline job writes to a warehouse file.
- `app/` MUST NOT write to any store except through `herness.store.ops` functions and service functions in L4/L5.
- `herness.admin` (spec 10: privacy, backup, retention, deploy) is an L5 package.

Circular imports inside a layer are forbidden. `import-linter` runs an "independence" contract on the sibling packages of each layer, with exceptions listed in `pyproject.toml` and in the implementation spec that needs them.

### 2.2 Ports and adapters

- Every pluggable edge (connector, decider, LLM client, tool, output channel) is a `typing.Protocol` in the owning package and is resolved through `herness.core.registry` (spec 00 §6). Business logic depends on the protocol, never on a concrete adapter.
- Adapters translate external shapes into shared types at the boundary. No third-party response object crosses a module boundary.
- The composition root is `herness.cli` (for jobs and commands) and `app/common/` (for the dashboard). Only these construct concrete adapters and wire them together. Other modules receive dependencies as parameters.

### 2.3 State and side effects

- Calculation code (metrics, scoring, hashing, redaction matching, verification comparisons) is pure: inputs in, value out, no I/O, no clock, no randomness. Time and randomness are passed in (`now: datetime`, `rng: random.Random` or a seed).
- I/O lives in thin adapter functions at module edges.
- There is no module-level mutable state except the registry and cached config, both of which are reset by a test fixture.
- Every job is idempotent and resumable (spec 00 §2.4). Each implementation spec names the idempotency key of every write.

### 2.4 Size and complexity limits

| Measure | Limit | Enforced by |
|---------|-------|-------------|
| Module length | 400 lines (spec 05 §5.2 sets 220 for `loop.py`) | CI check script |
| Function length | 60 lines | ruff `PLR0915` plus review |
| Cyclomatic complexity | 10 per function | ruff `C901` (`max-complexity = 10`) |
| Parameters | 6 positional-or-keyword; more MUST be keyword-only or a parameter object | ruff `PLR0913` (`max-args = 6`) |
| Nesting depth | 4 | review |

An exception needs a `# noqa: <rule>` comment with a reason, and the implementation spec lists it.

### 2.5 Concurrency

- Blocking code is synchronous. Async code is used only where the design spec says so (LLM clients, swarm scheduling, chat streaming).
- Async code never calls blocking I/O on the event loop. It uses `asyncio.to_thread` for DuckDB, SQLite and file work.
- Every shared resource names its concurrency model in the unit spec: immutable, per-thread, lock-protected (which lock), or single-writer process.
- SQLite: one connection per thread, WAL, `busy_timeout = 10000` (spec 00 §4). DuckDB: one writer process per file; readers open read-only.
- Every network call and subprocess has a timeout. There are no unbounded waits.

## 3. Python coding baseline

### 3.1 Tooling

| Tool | Setting |
|------|---------|
| Python | 3.12, managed by `uv`; `uv.lock` committed; installs use `uv sync --frozen` |
| Formatter | `ruff format`, line length 100 |
| Linter | `ruff check` with rule sets `E`, `W`, `F`, `I`, `N`, `UP`, `B`, `A`, `C4`, `C90`, `SIM`, `PT`, `PL`, `RUF`, `S` (bandit), `DTZ`, `TRY`, `ASYNC`, `PERF`, `ANN`, `BLE`, `EM`, `G`, `LOG`, `T20` (no `print`), `ERA` (no commented-out code) |
| Types | `mypy --strict` on all of `herness/`; `app/` and `tools/` with `--strict` minus `disallow_untyped_decorators` |
| Imports | `import-linter` contracts per §2.1 |
| Pre-commit | ruff, ruff format, mypy, import-linter, `detect-secrets`, fixtures PII scan (spec 11 §9), `pytest -m unit` |

### 3.2 Typing

- All public functions and methods are fully annotated. `Any` is allowed only at a third-party boundary, and the value is validated into a typed model on the next line of logic.
- Data crossing a module boundary is a pydantic v2 model, a frozen `dataclass`, a `TypedDict` for JSON-shaped rows, or a `pyarrow.RecordBatch` with a declared schema.
- Pydantic models at trust boundaries (config, files, HTTP responses, model output, UI input) use `extra="forbid"` and `strict=True` unless the implementation spec says otherwise and gives the reason. Internal value objects are `frozen=True`.
- Enumerations are `enum.StrEnum`. String literals for closed sets use `typing.Literal`.
- Money is `decimal.Decimal`; never `float` (spec 00 §8). Datetimes are timezone-aware UTC; naive datetimes are rejected at boundaries.
- Paths are `pathlib.Path`.

### 3.3 Naming and documentation

- PEP 8 names. Modules and functions `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE`. Booleans read as predicates (`is_`, `has_`, `can_`).
- Public modules, classes and functions have a Google-style docstring: one-line summary, `Args`, `Returns`, `Raises`. The unit spec is the source for this text.
- Comments explain why, never what.

### 3.4 Error handling

- Every raised error is a class from the spec 00 §7 taxonomy or a subclass declared in the owning implementation spec. Built-in exceptions (`ValueError`, `KeyError`) never escape a module boundary. They are caught and re-raised as a taxonomy error with `raise ... from exc`.
- There are no bare `except:` and no `except Exception:` except at three places: the job worker top level, the CLI entry point and the Streamlit page wrapper. Each logs and converts the error; none swallows it silently.
- Error messages name the operation and the identifiers involved (`record_id`, `job_id`, `query_id`). They never contain secret values, ticket text or personal data.
- Resources (connections, files, locks, HTTP clients) are opened with context managers.
- Retries happen only in the resilience layer (spec 08). Other code raises a `RetryableError` and lets the caller's policy decide.

### 3.5 Data access

- SQL is parameterised. String formatting of values into SQL is forbidden. Identifiers that must be dynamic (schema, table) are checked against an allowlist and quoted with the driver's identifier quoting.
- Warehouse SQL lives in numbered files under `herness/model/sql/` (spec 02). Ad hoc agent SQL passes the spec 05 guard.
- Ops store schema changes are forward-only numbered migrations in `herness/store/migrations/`, applied in a transaction, recorded in a `schema_migration` table, and tested by upgrading a fixture database from the previous version.
- File writes that must be atomic write a temporary file in the same directory, `fsync` it, then `os.replace` it (the pattern of spec 00 §4).
- Parsing: YAML with `yaml.safe_load` only. JSON with the standard library or pydantic. No `pickle`, `marshal` or `shelve` for any data that crosses a process or is read from disk. Model weights load with `safetensors`, or `torch.load(weights_only=True)` where safetensors is unavailable.
- Subprocesses take an argument list, never `shell=True`, and have a timeout.

### 3.6 Logging

- `structlog` JSON lines (spec 00 §8). Required keys: `ts`, `level`, `event`, `component`, plus the IDs in scope.
- Event names are `component.object.action` in `snake_case` with a past-tense or outcome action, for example `connectors.sync.completed`, `harness.tool.rejected`. Each implementation spec lists its events in its observability section, with level and fields.
- Levels: `DEBUG` developer detail; `INFO` state changes and job milestones; `WARNING` degraded but continuing (fallback used, retry scheduled); `ERROR` an operation failed; `CRITICAL` the process cannot continue.
- Never logged above `DEBUG`: ticket or Jira text, model prompts and completions, personal data. Never logged at any level: secret values. The spec 10 scrubber is the last line of defence, not the first.

## 4. Observability

Each implementation spec lists three things:

1. **Log events**: name, level, fields, when emitted.
2. **Metrics**: counters and histograms written to the ops store `metric_sample` table (spec 08 owns the table), named `herness_<component>_<measure>_<unit>` (for example `herness_connectors_records_total`, `herness_harness_tool_latency_seconds`). Labels are low-cardinality: never a `record_id` or free text.
3. **Trace events**: which spec 05 `Tracer` event types the component emits, and the fields it fills.

Health: every long-running component exposes a `health()` result that `herness doctor` (spec 10) can call, returning status `ok | degraded | down` and a reason.

## 5. Security baseline

### 5.1 Threat modelling procedure (STRIDE)

Each implementation spec contains a threat model for its component:

1. List the trust boundaries it touches, using the global IDs below.
2. For each boundary, list threats under the six STRIDE categories that apply: **S**poofing, **T**ampering, **R**epudiation, **I**nformation disclosure, **D**enial of service, **E**levation of privilege.
3. Give each threat an ID `TH<spec>-<nn>`, a likelihood and impact (Low/Medium/High), the control that mitigates it, the ASVS or LLM Top 10 reference, and the test ID that proves the control.
4. Residual risks that are accepted are stated with the reason and owner.

Global trust boundaries:

| ID | Boundary | Untrusted side |
|----|----------|----------------|
| TB1 | Source systems (ServiceNow, Jira, monitoring, MongoDB, Snowflake, Dataverse) → connectors | Source API responses and record content |
| TB2 | Inbox file drops → files connector | Files placed by people or other systems |
| TB3 | Ticket, Jira and monitoring text → enrichment and model prompts | Free text (prompt injection, personal data) |
| TB4 | Model output → tools, SQL guard, memory, blackboard | Every model completion |
| TB5 | Model output → rendered reports, dashboard and chat UI | Markdown and text that reaches a browser |
| TB6 | Host → off-network model or API endpoints | The data leaving; the endpoint's responses |
| TB7 | Browser → reverse proxy → dashboard | Users, the identity header, form input |
| TB8 | Host ↔ WSL2 containers (vLLM, OpenJev, llama.cpp) | Container images, model weights, local ports |
| TB9 | Package index, container registry, model hub → build and deploy | Every third-party artifact |
| TB10 | Operator → CLI and config files | Config values, CLI arguments, `.env` |

### 5.2 OWASP ASVS 5.0, Level 2

Target: ASVS 5.0.0 Level 2 for every chapter that applies. Implementation specs cite requirements as `ASVS v5.0.0-<chapter>.<section>.<requirement>`, taking the number from the official ASVS 5.0.0 text. A spec that cannot confirm an exact requirement number cites the section (`ASVS v5.0.0-V13.3`) instead. It never invents a number.

| ASVS 5.0 chapter | Applies to | Notes |
|------------------|------------|-------|
| V1 Encoding and Sanitization | 02, 05, 09 | SQL parameterisation, Jinja autoescape, markdown sanitising |
| V2 Validation and Business Logic | all | pydantic at every boundary; business limits (budgets, row caps) |
| V3 Web Frontend Security | 09 | CSP, no external resources, XSRF protection |
| V4 API and Web Service | 01, 05 | Outbound API clients: timeouts, schema checks, size limits |
| V5 File Handling | 01, 02, 09, 10 | Inbox files: type and size checks, path containment, no execution |
| V6 Authentication | 09, 10 | Delegated to the OIDC proxy; app trusts only the configured proxy |
| V7 Session Management | 09 | Streamlit sessions behind the proxy; chat session IDs unguessable |
| V8 Authorization | 06, 07, 09, 10 | viewer / reviewer / admin; approvals; tool least privilege |
| V9 Self-contained Tokens | 01 | OAuth tokens from sources: validated expiry, never logged |
| V10 OAuth and OIDC | 01, 10 | Client-credentials flows to sources; OIDC at the proxy |
| V11 Cryptography | 07, 10 | HMAC-SHA256 pseudonyms, SHA-256 hashes; no custom crypto |
| V12 Secure Communication | 01, 05, 10 | TLS 1.2+ with verification for every non-loopback call |
| V13 Configuration | 10, all | Secure defaults, secrets out of config, hardened containers |
| V14 Data Protection | 02, 03, 07, 10 | Redaction, retention, deletion, BitLocker, ACLs |
| V15 Secure Coding and Architecture | all | Layering, dependency hygiene, safe deserialisation |
| V16 Security Logging and Error Handling | 08, 10, all | Audit and egress logs, no sensitive data in logs |
| V17 WebRTC | none | Not applicable |

### 5.3 OWASP Top 10 for LLM Applications (2025)

Components that call models, build prompts, store model output or embed text (03, 05, 06, 07, 09, 11) map each item below to controls and tests.

| ID | Risk | Herness baseline control |
|----|------|--------------------------|
| LLM01 | Prompt injection | Untrusted text in `<untrusted_data>` blocks (spec 10 §9.1); read-only tools; injection pattern scan on memory writes (spec 07); Skeptic and Verifier bound the impact |
| LLM02 | Sensitive information disclosure | Redaction before any model sees text (spec 10); egress guard and re-scan; no raw text in traces |
| LLM03 | Supply chain | Weights and images pinned by digest; hashes verified on pull (§5.6) |
| LLM04 | Data and model poisoning | Gold set frozen and versioned; distillation gate on held-out eval; memory writes that change scoring need approval |
| LLM05 | Improper output handling | Model output parsed into pydantic schemas; SQL guard; renderer strips links and images; numbers only via `NumberRef` |
| LLM06 | Excessive agency | Tools read-only except findings and memory proposals; no URL, email or shell tools; budgets per task and run |
| LLM07 | System prompt leakage | Prompts hold no secrets or credentials; leakage has no security impact by design |
| LLM08 | Vector and embedding weaknesses | Embeddings built from redacted text; vector reads scoped by the caller's tool context; deletion purges vectors |
| LLM09 | Misinformation | Numbers come from SQL (spec 00 §2.1); Verifier re-runs every `query_id`; eval gates |
| LLM10 | Unbounded consumption | Token, tool-call and wall-clock budgets (specs 05, 06); concurrency gates; breakers (spec 08) |

### 5.4 NIST AI RMF 1.0

Implementation specs for 03, 05, 06, 07 and 11 state which AI RMF functions their controls serve:

| Function | Herness practice |
|----------|------------------|
| Govern | Data policy approvals (D5), model and profile registry, audit log of approvals |
| Map | Documented purpose and limits per role and decider; known failure modes listed in each spec |
| Measure | Golden and classifier eval, calibration, per-org quality, drift checks on nightly runs |
| Manage | Gates block promotion of models and warehouse builds; fallbacks and human review queues |

### 5.5 NIST SSDF (SP 800-218)

| Practice group | Required practice in this repository |
|----------------|--------------------------------------|
| PO Prepare the organization | This document; threat model per implementation spec; security roles named in spec 10 §9.3 |
| PS Protect the software | Git history is the source of truth; `main` protected; signed tags for releases; `uv.lock` with hashes |
| PW Produce well-secured software | Design review against these standards; ruff `S` rules; mypy; security tests per threat; reuse vetted libraries only |
| RV Respond to vulnerabilities | `pip-audit` and `osv-scanner` in CI and nightly; triage within 7 days for High, 30 days for Medium; fixes include a regression test |

### 5.6 Supply chain: SLSA Build Level 2

- Every release wheel and container build runs on a hosted CI runner (GitHub Actions), never on a developer machine. This is required for SLSA Build L2.
- CI generates signed provenance for each artifact (GitHub artifact attestations) and a CycloneDX SBOM (`cyclonedx-py`).
- `herness deploy install` verifies the attestation and SBOM before installing a wheel on the target box.
- Dependencies are installed from `uv.lock` with hashes (`uv sync --frozen`). New dependencies need a line in the owning implementation spec's dependency table with licence, maintainer health and reason.
- Container images and model weights are pinned by digest or SHA-256 in `config/herness.yaml: deploy`, and `herness deploy pull` verifies them (spec 10).
- Allowed licences: MIT, BSD, Apache-2.0, PSF, ISC, MPL-2.0. Anything else needs a recorded approval.

### 5.7 Secure coding rules (all components)

| Area | Rule |
|------|------|
| Secrets | Only `secret:` references in config, resolved by `herness.core.secrets` (spec 10). Never in code, prompts, traces, logs, test fixtures or exceptions |
| Crypto | Standard library `hashlib`, `hmac`, `secrets` only. No custom algorithms, no MD5 or SHA-1 for security purposes, constant-time comparison for MACs |
| Randomness | `secrets` for IDs and tokens (ULIDs via `herness.core.ids`); `random` only for seeded simulation and tests |
| Input validation | Allowlist, type, length and range at every trust boundary |
| Output encoding | Jinja `autoescape=True`; markdown sanitised with an allowlist before rendering |
| Files | Resolve every path and check it stays under its configured root; reject symlinks out of the root; enforce size limits before reading |
| Network | Every non-loopback call goes through the egress guard with TLS verification on; timeouts on connect and read; response size limits |
| Least privilege | Service account `svc-herness`; DuckDB agent connections read-only with external access off; containers without `--privileged` |
| Defaults | Secure by default: `local` profile, loopback binding, `default_role` per D8 |

## 6. Testing standard

Spec 11 owns the test layout, markers, coverage targets and CI selection. Implementation specs add:

- **Test IDs.** `UT<spec>-<nn>` unit, `PT<spec>-<nn>` property (hypothesis), `IT<spec>-<nn>` integration, `FT<spec>-<nn>` fault, `ST<spec>-<nn>` security, `BT<spec>-<nn>` benchmark, `ET<spec>-<nn>` eval. Each test function carries its ID in its name or docstring so the traceability matrix can be checked by script.
- **One behavior per test**, named `test_<unit>_<condition>_<expected>`.
- **Security tests.** Every `TH` threat has at least one `ST` test that tries the attack and asserts the control holds (injection strings, path traversal, oversized input, spoofed identity header, blocked egress).
- **Property tests** for every pure function with non-trivial input space (hashing, parsing, redaction, scoring, marker parsing).
- **Determinism.** Tests freeze time (`freezegun`) and seed randomness. No test depends on network, wall-clock or execution order.
- **Fakes over mocks.** Use the spec 11 fakes (fake LLM, stub decider, stub servers). `unittest.mock` is allowed only for OS-level faults.
- **Coverage.** Spec 11 §4.3 targets are the floor. New code in a task card MUST NOT lower package coverage.

## 7. CI and quality gates

| Gate | Runs on | Blocks |
|------|---------|--------|
| Lint and format (`ruff`) | pre-commit, CI | commit, merge |
| Types (`mypy --strict`) | pre-commit, CI | commit, merge |
| Layering (`import-linter`) | pre-commit, CI | commit, merge |
| Secret scan (`detect-secrets`) | pre-commit, CI | commit, merge |
| Unit tests | pre-commit, CI | commit, merge |
| Integration and CPU fault tests with coverage | pre-push, CI | merge |
| Traceability check (every task, test and threat ID resolves) | CI | merge |
| Dependency audit (`pip-audit`, `osv-scanner`) | CI, nightly | merge on High or Critical with a fix available |
| SBOM and provenance | release CI | release |
| Nightly, GPU and eval selections (spec 11 §4.2) | dev box | phase gates |

## 8. Definition of done (per task card)

A task card is done when all of these hold:

1. The code matches the unit specs it lists, and every acceptance check on the card passes.
2. All tests named on the card exist, carry their IDs and pass. Coverage for the touched packages is at or above target.
3. ruff, mypy, import-linter and detect-secrets pass with no new suppressions, or each new suppression is listed in the implementation spec.
4. Log events, metrics and trace events on the card are emitted and asserted by at least one test.
5. The threats the card lists have their `ST` tests passing.
6. Docstrings exist for every new public symbol.
7. No design contract was changed. If one needed changing, the card is blocked and the delta is raised instead.

## 9. Document conventions

- File name: `docs/impl/NN-<slug>.impl.md`, where `NN` matches the design spec.
- Header lines: title, status and date, design spec link, phase, and the implementation specs it depends on.
- IDs:

| Kind | Format | Example |
|------|--------|---------|
| Task card | `T<spec>-<nn>` | `T05-07` |
| Unit spec | `U<spec>-<nn>` | `U05-12` |
| Threat | `TH<spec>-<nn>` | `TH05-03` |
| Test | per §6 | `ST05-03` |
| Flow | `F<spec>-<nn>` | `F06-02` |

- Cross-references: `design 05 §5.2` for design specs, `impl 05 §4` for implementation specs, `ENG §5.3` for this document.
- Tables over prose wherever a list of facts has the same attributes.

## 10. Implementation spec template

Every implementation spec has these sections in this order. Sections that do not apply say "Not applicable" and why.

1. **Scope and traceability.** One paragraph of scope. Then a matrix mapping every design-spec section to the implementation sections, units, task cards and tests that realise it. Every design section appears, including "Open questions" and "Performance targets".
2. **Module map.** One row per file: path, single purpose, public symbols, layer, allowed imports beyond the layer defaults, line budget.
3. **Unit specs.** One block per unit (§12.3 format).
4. **State and data.** Tables, files and in-memory state the component owns: schema by column (name, type, nullability, constraint, meaning), indexes, migration number, idempotency key of every write, transaction boundaries, retention.
5. **Control flows.** Each significant flow as numbered steps with IDs (`F<spec>-<nn>`). Every step names the unit it calls, the state it changes, and what happens on each failure at that step.
6. **Error handling.** Table: failure condition, taxonomy class raised, where it is caught, retry or fallback behavior, user-visible effect, log event.
7. **Security.** (a) trust boundaries touched; (b) STRIDE threat table per ENG §5.1; (c) ASVS mapping; (d) LLM Top 10 mapping and AI RMF function where applicable; (e) secrets used and how they are resolved; (f) data classification of every field the component stores or emits (`public`, `internal`, `confidential`, `personal`); (g) accepted residual risks.
8. **Observability.** Log events, metrics, trace events and health checks per ENG §4.
9. **Configuration.** Every config key the component reads: key path, type, default, validation rule, whether a change needs a restart, sensitivity.
10. **Performance and capacity.** Design targets restated as measurable benchmarks (`BT` IDs) with dataset scale, hardware and pass threshold, plus the resource limits the code enforces (memory, batch size, concurrency).
11. **Test specification.** Every test ID with: unit or flow under test, setup and fixtures, action, expected result, marker. Grouped by type.
12. **Task cards.** In dependency order (§12.4 format).
13. **Design deltas and open items.** Contract changes this spec needs (or "none"), open questions inherited from the design spec with their current defaults, and the verification items from `docs/specs/open-questions.md` that block specific task cards.
14. **Dependencies.** Third-party packages with minimum version, licence and use; internal implementation specs and the units used from them.

## 11. Writing rules

- Plain, specific language. One requirement per sentence. No "etc.", "as needed", "appropriate" or "handle gracefully": say exactly what happens.
- Every number has a unit. Every limit has a config key or is stated as a constant with its value.
- A value described in the design spec is referenced, not restated, unless restating it removes ambiguity. When restated, it matches the design spec exactly.
- No placeholders ("TBD", "TODO"). An unknown is an open item in §13 with a default.

## 12. Formats

### 12.1 Traceability matrix row

| Design § | Requirement (short) | Impl § | Units | Tasks | Tests |
|----------|---------------------|--------|-------|-------|-------|

### 12.2 Module map row

| Path | Purpose | Public symbols | Layer | Extra imports | Line budget |
|------|---------|----------------|-------|---------------|-------------|

### 12.3 Unit spec block

Heading: `U<spec>-<nn> <qualified name>` (for example `U05-12 herness.harness.loop.run_agent`).

| Field | Content |
|-------|---------|
| Kind | function, async function, class, method, protocol, constant, SQL file, prompt file |
| Purpose | One sentence |
| Signature | Table of parameters: name, type, default, kind (positional, keyword-only), constraints. Then return type |
| Preconditions | What must be true on entry; how violations are reported (error class) |
| Postconditions | What is true on normal return |
| Invariants | For classes: what always holds between method calls |
| Algorithm | Numbered steps in prose. Enough detail that two implementers produce the same observable behavior |
| Side effects | Stores written, files touched, network calls, events emitted |
| Errors | Table: condition, error class, message identifiers |
| Concurrency | Thread-safety, async-safety, locks held |
| Complexity and limits | Time and memory bounds; caps enforced |
| Security notes | Threat IDs this unit mitigates; validation it performs |
| Tests | Test IDs |

### 12.4 Task card

Heading: `T<spec>-<nn> <short title>`.

| Field | Content |
|-------|---------|
| Goal | One sentence: what exists when the card is done |
| Depends on | Task IDs, including ones in other implementation specs |
| Units | Unit IDs implemented or changed |
| Files | Paths created or modified (at most 4 production files) |
| Tests | Test IDs written in this card |
| Threats | Threat IDs whose controls land in this card |
| Acceptance checks | Commands and observable results (for example: "`pytest -k UT05-0` passes; `mypy --strict herness/harness` reports 0 errors") |
| Blocked by | Open items or verification items, or "none" |
| Size | S (< 150 lines), M (150–400 lines) |

## 13. Phase alignment

Task cards carry the phase of their design spec (spec 00 §1). A card MUST NOT depend on a card from a later phase. Where the design spec splits a component across phases (for example connectors: files in Phase 1, live sources in Phase 6), the task list is ordered by phase first.

## 14. Deltas this document requires in design specs

These are stricter than the design specs today. The consistency sweep updates the design specs once the standards are approved.

| # | Design spec | Change |
|---|-------------|--------|
| E1 | 11 §4.2, §10.5 | `mypy --strict` scope becomes all of `herness/`, not four packages |
| E2 | 11 §10.5 | Hosted CI changes from optional to required for release builds (SLSA Build L2), and gains SBOM, provenance and dependency audit steps |
| E3 | 00 §9 | Dev dependencies add `import-linter`, `pip-audit`, `cyclonedx-bom`, `detect-secrets`; `osv-scanner` as a CI binary |
| E4 | 10 §3.7 | `herness deploy install` verifies artifact attestation and SBOM before install |
| E5 | 02, 08 | `metric_sample` table for component metrics (ENG §4): table in impl 02 (ops migration), writer in impl 08 |
| E6 | 00 §6, §3 | `herness/core/types.py` and `herness/store/ops.py` become packages; behavioral classes move to their owners (ENG §2.1) |
| E7 | 10 §3.1, §3.5 | Settings import exception, persistence ports, explicit source `hosts` allowlist, loopback client factory (ENG §2.1) |

All cross-spec rulings made during the consistency pass are recorded in `docs/impl/DECISIONS.md`.
