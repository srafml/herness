# 00 — Foundation: core primitives and repository scaffolding (implementation spec)

Status: Draft v2 · 2026-09-24 (consistency pass: rulings of [`DECISIONS.md`](DECISIONS.md) applied)
Design spec: [`docs/specs/00-overview-and-contracts.md`](../specs/00-overview-and-contracts.md) (design 00)
Standards: [`docs/impl/ENG-STANDARDS.md`](ENG-STANDARDS.md) (ENG)
Rulings: [`docs/impl/DECISIONS.md`](DECISIONS.md) (cited as `R-nn`; they win over design 00 where they differ, until the design edits of DECISIONS §9 land)
Phase: 1
Depends on implementation specs: none (this is the bottom of the stack). Consumed by every other implementation spec. Soft references to impl 09, 10 and 11 are listed in §14.

## 1. Scope and traceability

This spec builds the L0 foundation modules that design 00 assigns to itself: `herness/core/errors.py` (error taxonomy, design 00 §7, with `NotFound`, `hint` and `details` added by R-19), `herness/core/time.py` (UTC clock and timestamp text, design 00 §8), `herness/core/ids.py` (identifiers, and the single implementations of `canonical_json`, `normalize_sql` and `query_id`, design 00 §5 and R-14), `herness/core/numbers.py` (marker parsing, the numeral scanner of design 00 §12.1 and `NumberRef` display formatting, R-16), `herness/core/logging.py` with its private helper `herness/core/_log_pipeline.py` (structured JSON logging, design 00 §8), and the package skeleton, re-export, import rules and ownership check of the shared-types package `herness/core/types/` (design 00 §6, R-01, R-02). The fields of the shared types are owned by specs 03, 05, 06, 07, 08 and 09 and are not specified here. The ownership checker also enforces the settings-module import rule of R-03. It also builds the repository scaffolding that ENG requires: `pyproject.toml` (dependencies, ruff, mypy, import-linter, pytest, coverage), `.pre-commit-config.yaml`, `.secrets.baseline`, the CI-side check scripts in `tools/`, the hosted CI workflow and the release workflow (SBOM and signed provenance for SLSA Build L2), `.gitignore`, `.gitattributes`, `.env.example` and the README. Out of scope: `registry.py`, `config.py`, `secrets.py`, `redact.py`, `egress.py` including `loopback_http_client` (R-06), `audit.py` (impl 10); `resilience.py`, `jobs.py` (impl 08); `result_hash`, `rows_equivalent`, `iter_batch_rows` (impl 04, `herness.metrics.evidence`, R-15); tracing (impl 05); the config key `reports.allowed_numeral_patterns` (impl 09; this spec only compiles and applies the list its callers pass in); the `herness.admin` package (impl 10, R-07) beyond its place in the layer contract.

### 1.1 Traceability matrix

| Design § | Requirement (short) | Impl § | Units | Tasks | Tests |
|----------|---------------------|--------|-------|-------|-------|
| 00 preamble | Spec 00 wins over 01–11 | §13 (deltas raised, never applied locally) | — | — | — |
| 00 §1 | Spec index; `herness/core/` owned by 00, 08, 10 | §2 | U00-49 | T00-01 | UT00-56 |
| 00 §2.1 | Numbers come from SQL | §3.3 (`query_id` is the single ID function used by 04 and 05, R-14); §3.10 (uncited-numeral scanner shared by 05 and 09, R-16) | U00-32, U00-68 | T00-05, T00-16 | UT00-32, PT00-05, UT00-76 |
| 00 §2.2 | Every number traceable by `query_id` | §3.3 | U00-29, U00-31, U00-32 | T00-05 | UT00-30, UT00-31, UT00-32, PT00-04, PT00-05 |
| 00 §2.3 | Deterministic core, pluggable edges | §3.6 (layer contracts); registry is impl 10 | U00-52 | T00-09 | UT00-58, ST00-10 |
| 00 §2.4 | Idempotent and resumable jobs | §4.3 (foundation writes are append-only logs); owners name their keys | U00-32 | T00-05 | PT00-05 |
| 00 §2.5 | Local by default | §3.6 (ruff bans on building `httpx` clients and transports anywhere except `herness.core.egress`, R-06); egress and the socket guard are impl 10 | U00-50 | T00-01 | ST00-09 |
| 00 §2.6 | Small plain Python, pinned dependencies | §3.6, §14 | U00-49 | T00-01 | UT00-56 |
| 00 §3 | Python 3.12, `uv`, single package, layout | §2, §3.6 | U00-48, U00-49, U00-61, U00-62 | T00-01, T00-02 | UT00-55, UT00-56, ST00-11 |
| R-58 | Editable install in development; the target box installs the release wheel through `herness deploy install` after attestation and SBOM checks | §3.6 (U00-49), §3.8 (U00-60), §3.9 (U00-63) | U00-49, U00-60, U00-63 | T00-01, T00-02, T00-15 | UT00-56, ST00-08 |
| 00 §4 | Storage layout (`data/` gitignored; app log path) | §4.2, §3.4, §3.6 | U00-36, U00-40, U00-61 | T00-02, T00-06, T00-07 | UT00-36, ST00-11 |
| 00 §5 | Identifier formats, `new_ulid()`, `query_id`, `build_id`; single `canonical_json`, `normalize_sql`, `query_id` (R-14) | §3.3 | U00-19 … U00-34 | T00-05 | UT00-19 … UT00-34, PT00-03 … PT00-05, FT00-02, ST00-05, ST00-17 |
| 00 §5.1 | `result_hash` implemented once in `herness.metrics.evidence` (R-15) | §3.3 (U00-29 notes: row hashing uses its own encoding) | — (T04-01 (herness.metrics.evidence.result_hash)) | — | — |
| 00 §6 | Shared protocols and types; one owner each; types package with one submodule per owner (R-01); behavioral classes in owner packages (R-02) | §3.5 | U00-44 … U00-47 | T00-08 | UT00-48 … UT00-54, UT00-72, UT00-80, UT00-81, ST00-15 |
| 00 §7 | Error taxonomy, plus `NotFound`, `hint` and `details` (R-19) | §3.1, §6 | U00-01 … U00-08 | T00-03 | UT00-01 … UT00-08, UT00-71, PT00-01, ST00-13 |
| 00 §8 logging | structlog JSON lines to stderr and `data/logs/herness-<date>.jsonl`; required keys; no sensitive data above DEBUG; scrubber | §3.4, §8 | U00-35 … U00-43 | T00-06, T00-07 | UT00-35 … UT00-47, FT00-01, ST00-01 … ST00-04 |
| 00 §8 tracing | Spec 05 `Tracer` is the only trace writer | Not applicable here (impl 05) | — | — | — |
| 00 §8 time | Aware UTC; SQLite fixed-width text; business timezone via `tzdata` | §3.2 | U00-09 … U00-18 | T00-04 | UT00-09 … UT00-18, PT00-02, ST00-14 |
| 00 §8 money | `Decimal` in Python, string in JSON | §3.3 (`canonical_json` encodes `Decimal` as string) | U00-29 | T00-05 | UT00-30 |
| 00 §9 | Dependencies with minimum versions | §3.6, §14 | U00-49 | T00-01 | UT00-56 |
| 00 §10 | Open decisions D1–D7 | §13.2 | — | — | — |
| 00 §11 | Configuration files and owners | Not applicable (impl 10); `.env.example` only | U00-62 | T00-02 | ST00-11 |
| 00 §12.1 | Numbers in model text as markers; allowed numerals; `NumberRef` formats | §3.10 (`herness.core.numbers`, R-16); the Verifier (05) and the renderer (09) import it | U00-64 … U00-70 | T00-16 | UT00-74 … UT00-79, PT00-06, PT00-07, ST00-18, BT00-05 |
| 00 §12.2 | Report hand-off via `ReportDraft` | §3.5 (ownership of `ReportDraft` and helpers registered to 06) | U00-45 | T00-08 | UT00-48 |
| 00 §12.3 | Interfaces between harness specs | §3.5 (behavioral names declared in owner packages, DD-01) | U00-46 | T00-08 | UT00-54 |
| 00 §12.4 | Compaction is append-only | Not applicable (impl 07) | — | — | — |
| 00 Performance targets | Design 00 sets none | §10 sets foundation budgets | U00-20, U00-32, U00-36, U00-40, U00-56, U00-68 | T00-05, T00-07, T00-11, T00-16 | BT00-01 … BT00-05 |
| 00 Open questions | §10 decisions | §13.2 | — | — | — |
| ENG §2.1 | Layer contracts in `pyproject.toml`, including `herness.admin` (R-07), no `herness.core` → `herness.store` imports (ports, R-04), no `herness.enrich` → `herness.harness` imports (R-05), and the settings exception (R-03) | §3.6, §3.5 | U00-47, U00-52 | T00-08, T00-09 | UT00-58, UT00-73, ST00-10 |
| ENG §2.4 | Module line limit checked in CI | §3.7 | U00-55 | T00-10 | UT00-59, UT00-60, IT00-02 |
| ENG §3.1 | Tooling baseline (ruff, mypy strict, pre-commit) | §3.6 | U00-50, U00-51, U00-54 | T00-01, T00-12 | UT00-57, IT00-01 |
| ENG §5.6, §7, E2 | Hosted CI, audit, SBOM, signed provenance | §3.8 | U00-58 … U00-60 | T00-13, T00-14, T00-15 | ST00-06, ST00-07, ST00-08, ST00-16, UT00-69, UT00-70 |
| ENG §6, §7 | Traceability check by script | §3.7 | U00-56 | T00-11 | UT00-61 … UT00-68, BT00-04 |
| ENG §14 E1 | mypy strict on all of `herness/` | §3.6 | U00-51 | T00-01 | UT00-57 |
| ENG §14 E3 | Dev deps add import-linter, pip-audit, cyclonedx-bom, detect-secrets; osv-scanner binary | §3.6, §3.8 | U00-49, U00-59 | T00-01, T00-14 | UT00-56 |
| ENG §14 E6 | `herness/core/types/` is a package; behavioral classes live with their owners (R-01, R-02) | §3.5 | U00-44 … U00-47 | T00-08 | UT00-48 … UT00-54, UT00-72, UT00-80, UT00-81, ST00-15 |
| ENG §14 E7 | Settings import exception (R-03); persistence ports (R-04); egress only through `herness.core.egress` (R-06) | §3.5, §3.6 | U00-47, U00-50, U00-52 | T00-01, T00-08, T00-09 | UT00-58, UT00-73, ST00-09, ST00-10 |

## 2. Module map

| Path | Purpose | Public symbols | Layer | Extra imports | Line budget |
|------|---------|----------------|-------|---------------|-------------|
| `herness/__init__.py` | Package root; exposes the installed version | `__version__` | L0 | `importlib.metadata` | 30 |
| `herness/py.typed` | PEP 561 marker (empty file) | — | — | — | 1 |
| `herness/core/__init__.py` | Foundation package marker; docstring only, no imports, no re-exports | — | L0 | none | 10 |
| `herness/core/errors.py` | Error taxonomy of design 00 §7 plus `NotFound` (R-19) and two helpers | `HernessError`, `RetryableError`, `RecoverableError`, `FatalError`, the 18 leaf classes, `error_kind`, `to_log_fields` | L0 | none (standard library only) | 340 |
| `herness/core/time.py` | UTC clock, sleeps, timestamp text formats, time zones | `now`, `monotonic`, `sleep`, `asleep`, `ensure_utc`, `format_utc`, `parse_utc`, `parse_iso`, `utc_day`, `zone`, `DB_TS_LEN`, `MAX_SLEEP_S` | L0 | `herness.core.errors`, `tzdata` (data only) | 200 |
| `herness/core/ids.py` | ULIDs, prefixed IDs, `build_id`, `record_id`, canonical JSON, SHA-256, `query_id`, tokens | `CROCKFORD_ALPHABET`, `ULID_LEN`, `IdKind`, `ID_PREFIXES`, `new_ulid`, `new_id`, `new_build_id`, `is_valid_ulid`, `is_valid_id`, `is_valid_build_id`, `make_record_id`, `split_record_id`, `canonical_json`, `sha256_hex`, `normalize_sql`, `query_id`, `new_token`, `RECORD_KEY_MAX_LEN` | L0 | `herness.core.errors`, `herness.core.time` | 330 |
| `herness/core/numbers.py` | Marker parsing, uncited-numeral scanner and `NumberRef` display formatting shared by the Verifier (05) and the renderer (09) (R-16) | `MARKER_RE`, `ANY_MARKER_RE`, `MARKER_ID_RE`, `NUMERAL_RE`, `MAX_SCAN_CHARS`, `HIT_TEXT_MAX`, `MAX_ALLOWED_PATTERNS`, `MAX_PATTERN_CHARS`, `NUMBER_FORMATS`, `DEFAULT_FORMAT_BY_UNIT`, `Marker`, `MalformedMarker`, `MarkerScan`, `NumeralHit`, `FormattableNumber`, `parse_markers`, `compile_allowed_patterns`, `find_uncited`, `format_value`, `format_number` | L0 | `herness.core.errors` only | 320 |
| `herness/core/logging.py` | Public logging API: configure, get logger, bind context IDs, reset | `LogLevel`, `configure_logging`, `get_logger`, `bind_ids`, `reset_logging`, `REQUIRED_KEYS`, `CONTEXT_ID_KEYS`, `SECRET_KEYS`, `TEXT_KEYS`, `MAX_FIELD_CHARS`, `MAX_LINE_BYTES`, `EVENT_NAME_RE`, `COMPONENT_RE` | L0 | `structlog`, `herness.core.errors`, `herness.core.ids`, `herness.core.time`, `herness.core._log_pipeline` | 260 |
| `herness/core/_log_pipeline.py` | Private: structlog processors and the daily JSONL file handler | none public (private units U00-39 … U00-43) | L0 | `structlog`, `herness.core.errors`, `herness.core.time` | 330 |
| `herness/core/types/__init__.py` | Re-exports every shared type from its owner submodule; defines nothing | `__all__` plus every name in `TYPE_OWNERS` whose owner submodule exists | L0 | owner submodules of this package only | 150 |
| `herness/core/types/_ownership.py` | Ownership tables read by the ownership checker | `TYPE_OWNERS`, `OWNER_MODULES`, `OWNER_IMPORTS`, `DECLARED_ELSEWHERE` | L0 | none | 120 |
| `herness/core/types/{decisions,harness,swarm,memory,jobs,reports}.py`, or the package `herness/core/types/<submodule>/` | Owner submodules (R-01); created by impl 03, 05, 06, 07, 08, 09 respectively. An owner whose types exceed the 400-line module limit uses the package form (§3.5) | per owner | L0 | per §3.5 import rules | set by owner spec (≤ 400 per file) |
| `tools/__init__.py` | Makes `tools` importable (`tools.synth_data` of impl 11, the check scripts) | — | Tooling | — | 5 |
| `tools/check_module_size.py` | CI check of module line budgets | `main` | Tooling | `tomllib` | 200 |
| `tools/check_traceability.py` | CI check that every task, unit, flow, threat and test ID resolves | `main` | Tooling | none | 390 |
| `tools/check_type_ownership.py` | CI check of shared-type ownership, `core.types` import rules and the settings-module import rule (R-03) | `main` | Tooling | `ast`, `herness.core.types._ownership` | 390 |
| `tools/check_audit.py` | Dependency-audit gate over pip-audit and osv-scanner JSON | `main` | Tooling | `tomllib` | 300 |
| `tools/check_licences.py` | Licence gate over the CycloneDX SBOM | `main` | Tooling | `tomllib` | 250 |
| `tools/audit_ignore.toml` | Time-boxed audit exceptions (starts empty: `ignore = []`) | — | Tooling | — | 50 |
| `pyproject.toml` | Project metadata, dependencies, tool configuration | — | — | — | 400 |
| `uv.lock` | Generated lock with hashes (`uv lock`) | — | — | — | generated |
| `.pre-commit-config.yaml` | Commit and push hooks | — | — | — | 120 |
| `.secrets.baseline` | detect-secrets baseline (generated) | — | — | — | generated |
| `.github/workflows/ci.yml` | Hosted CI: lint, types, test, audit (and reusable by release) | — | — | — | 250 |
| `.github/workflows/release.yml` | Release: build, SBOM, licence gate, provenance and SBOM attestations, GitHub release | — | — | — | 150 |
| `.gitignore`, `.gitattributes`, `.env.example`, `README.md` | Repository hygiene and onboarding | — | — | — | 80, 20, 40, 200 |

Stdlib-name shadowing: `herness/core/logging.py`, `herness/core/time.py`, `herness/core/numbers.py` and `herness/core/types/__init__.py` shadow standard-library module names inside the package. All imports in the repository are absolute, so `import logging` inside `herness/core/logging.py` resolves to the standard library. Ruff rule `A005` is suppressed for exactly these four files (listed in §3.6 U00-50 and §13.3).

Import convention for `herness.core.time`: callers write `from herness.core import time as clock` and call `clock.now()`, `clock.sleep()`. `from herness.core.time import ...` is banned by ruff `ICN003` (U00-50), because impl 11's `FakeClock` patches the module attributes and a `from` import would bind the unpatched function.

## 3. Unit specs

Conventions for this section:

- "Scalar" means `str`, `int`, `float`, `bool` or `None`.
- `SchemaViolation` is the error class for a value that breaks a foundation format contract (bad timestamp text, bad ID, non-canonicalisable JSON value). Callers at a trust boundary catch it and re-raise their own boundary class where their spec says so.
- Every unit's docstring is taken from its Purpose row plus its Errors row (ENG §3.3).

### 3.1 `herness.core.errors`

The module defines exactly the classes of design 00 §7 with exactly those parents, plus `NotFound(RecoverableError)` and the optional `hint` and `details` attributes of `HernessError` (R-19). No other spec adds classes to this file; a subclass needed by a component is declared in that component's module and listed in its implementation spec (ENG §3.4). Under R-19 this covers `JobStateError` (impl 08) and the added attributes of `EgressBlocked` and `ConfigError` (impl 10): impl 10 declares them in its own modules (as subclasses); the classes in this file carry no attributes beyond those specified here. Module constants: `MAX_MESSAGE_CHARS = 1000`, `MAX_CONTEXT_STR_CHARS = 200`, `MAX_HINT_CHARS = 500`, `MAX_DETAILS = 50`, `MAX_DETAIL_KEY_CHARS = 64`, `MAX_DETAIL_VALUE_CHARS = 2000`; private `_DETAIL_KEY_RE = ^[A-Za-z0-9_.\-]{1,64}$`.

#### U00-01 herness.core.errors.HernessError

Signature (`__init__`):

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `message` | `str` | — | positional-only | Names the operation and identifiers; never a secret value, ticket text or personal data (ENG §3.4) |
| `hint` | `str \| None` | `None` | keyword-only | Operator-facing fix, for example the command to run (R-19); never a secret value, ticket text or personal data |
| `details` | `Mapping[str, str] \| None` | `None` | keyword-only | Structured identifiers for the caller (CLI output, report contract, job record) (R-19); same content rule as `hint` |
| `**context` | `str \| int \| float \| bool \| None` | — | keyword (variadic) | Identifier fields such as `record_id`, `job_id`, `query_id`; the names `hint` and `details` are taken by the two parameters above |

Returns: `None` (constructor).

| Field | Content |
|-------|---------|
| Kind | class (base of the taxonomy; subclass of `Exception`) |
| Purpose | Root of every error Herness raises, carrying a bounded message, a scalar-only identifier context, an optional operator hint and optional string details. |
| Preconditions | None enforced at run time beyond the steps below; mypy enforces the types. |
| Postconditions | `self.message` holds the bounded message; `self.context` is a read-only mapping of scalars; `self.hint` is `None` or a bounded string; `self.details` is a read-only `Mapping[str, str]` (empty when not given); `str(self) == self.message`; `self.args == (self.message,)`. |
| Invariants | `len(self.message) <= MAX_MESSAGE_CHARS + 1`; every `context` value is a scalar; every string `context` value has at most `MAX_CONTEXT_STR_CHARS + 1` characters; `self.hint` is `None` or has at most `MAX_HINT_CHARS + 1` characters; `self.details` has at most `MAX_DETAILS` entries, every key matches `_DETAIL_KEY_RE` and every value is a `str` of at most `MAX_DETAIL_VALUE_CHARS + 1` characters. |
| Algorithm | 1. If `len(message) > MAX_MESSAGE_CHARS`, keep the first `MAX_MESSAGE_CHARS` characters and append `…` (U+2026).<br>2. Build `ctx: dict[str, scalar]`: for each `(key, value)` in `context` in call order: if `value` is `bool`, `int`, `float` or `None`, keep it; if `value` is `str`, apply the step-1 rule with limit `MAX_CONTEXT_STR_CHARS`; otherwise store the string `"<" + type(value).__name__ + ">"` (the value itself is never stringified, so objects holding secrets cannot leak through `repr`).<br>3. `hint`: `None` stays `None`; a `str` is bounded by the step-1 rule with limit `MAX_HINT_CHARS`; any other type is stored as `"<" + type(hint).__name__ + ">"`.<br>4. `details`: build `det: dict[str, str]` from the first `MAX_DETAILS` entries of `details` in iteration order (later entries are dropped; `None` gives `{}`). For the entry at position `i`: the key is kept when it is a `str` matching `_DETAIL_KEY_RE`, else it becomes `"key_" + str(i)`; a `str` value is bounded by the step-1 rule with limit `MAX_DETAIL_VALUE_CHARS`; any other value becomes `"<" + type(value).__name__ + ">"`. When a replacement key already exists, the later entry overwrites the earlier one.<br>5. Call `Exception.__init__(self, bounded_message)`.<br>6. Store `self.message = bounded_message`, `self._context = ctx`, `self.hint = bounded_hint`, `self._details = det`.<br>7. Properties `context` and `details` return `types.MappingProxyType(self._context)` and `types.MappingProxyType(self._details)`.<br>8. `__reduce__` returns `(_rebuild, (type(self), self.message, dict(self._context), self.hint, dict(self._details), {name: getattr(self, name) for name in type(self)._extra_attrs}))`. Module-private function `_rebuild(cls, message, context, hint, details, extra)` creates the instance with `cls.__new__(cls)`, calls `Exception.__init__(obj, message)`, sets `message`, `_context`, `hint`, `_details` and each extra attribute, and returns it. This keeps exceptions picklable by `multiprocessing` although subclasses take keyword-only arguments.<br>9. Class attribute `_extra_attrs: ClassVar[tuple[str, ...]] = ()`; subclasses with extra attributes override it (U00-04 … U00-06). Subclass constructors pass `hint` and `details` through to this constructor unchanged. |
| Side effects | None. |
| Errors | None raised by the constructor. |
| Concurrency | Immutable after construction; safe to share across threads. |
| Complexity and limits | O(len(message) + number of context and details entries); message capped at 1,000 characters, each string context value at 200, hint at 500, details at 50 entries of at most 2,000 characters each. |
| Security notes | TH00-06: non-scalar context and details values are replaced by their type name; long values are truncated; `hint` and `details` follow the ENG §3.4 content rule (no secrets, ticket text or personal data). |
| Tests | UT00-02, UT00-08, UT00-71, PT00-01 |

#### U00-02 herness.core.errors.RetryableError, RecoverableError, FatalError

| Field | Content |
|-------|---------|
| Kind | class (three classes, each a direct subclass of `HernessError`) |
| Purpose | The three categories that the resilience layer (impl 08) dispatches on with `isinstance`, never by string matching. |
| Signature | Inherited from U00-01 unchanged. |
| Preconditions | As U00-01. |
| Postconditions | As U00-01. |
| Invariants | `RetryableError`: retried with backoff by impl 08. `RecoverableError`: the caller repairs and retries differently. `FatalError`: no retry; job fails, task goes to dead letter. These meanings are the class docstrings (copied from design 00 §7 comments). |
| Algorithm | No behavior beyond U00-01. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | As U00-01. |
| Security notes | None beyond U00-01. |
| Tests | UT00-01, UT00-06 |

#### U00-03 herness.core.errors leaf classes without extra attributes

| Class | Parent | Docstring meaning (design 00 §7) |
|-------|--------|----------------------------------|
| `SourceUnavailable` | `RetryableError` | A source system cannot be reached or returned a server error |
| `ModelUnavailable` | `RetryableError` | A model endpoint cannot be reached or is overloaded |
| `StoreBusy` | `RetryableError` | SQLite or DuckDB lock contention outlasted its busy timeout |
| `OutputValidationError` | `RecoverableError` | Model output failed schema validation; repair prompt (max 2), then fallback model |
| `ToolInputError` | `RecoverableError` | Tool arguments are invalid; returned as an error tool result |
| `QueryError` | `RecoverableError` | SQL failed or was rejected; returned as an error tool result with a hint |
| `PolicyViolation` | `RecoverableError` | Memory write policy or injection scan refused a write; pending or rejected |
| `ReportContractError` | `RecoverableError` | A draft fails the rendering contract |
| `NotFound` | `RecoverableError` | A requested object (run, job, task, record, memory item, report, file) does not exist; the caller reports it or chooses another object (R-19) |
| `ConfigError` | `FatalError` | Configuration is invalid or incomplete |
| `AuthError` | `FatalError` | Authentication with a source or model endpoint failed |
| `SchemaViolation` | `FatalError` | Data does not match its declared contract |
| `BudgetExceeded` | `FatalError` | A token, cost, tool-call or wall-clock budget is exhausted |
| `PermissionDenied` | `FatalError` | The caller's role is not allowed to perform the action |
| `EgressBlocked` | `FatalError` | The egress guard refused an off-network call |

| Field | Content |
|-------|---------|
| Kind | class (15 classes listed above) |
| Purpose | Leaf error types of design 00 §7 and R-19 that need no attributes beyond those of U00-01. |
| Signature | Inherited from U00-01 unchanged. |
| Preconditions | As U00-01. |
| Postconditions | As U00-01. |
| Invariants | Each class's direct parent is exactly the one in the table. Names are fixed by design 00 §7 and R-19 (hence the `N818` suppression, §13.3). |
| Algorithm | No behavior beyond U00-01. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | As U00-01. |
| Security notes | None beyond U00-01. |
| Tests | UT00-01, UT00-08 |

#### U00-04 herness.core.errors.RateLimited

Signature (`__init__`):

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `message` | `str` | — | positional-only | as U00-01 |
| `retry_after` | `float \| None` | `None` | keyword-only | Seconds to wait as given by the server; normalised below |
| `**context` | scalar | — | keyword (variadic) | as U00-01 |

| Field | Content |
|-------|---------|
| Kind | class (subclass of `RetryableError`) |
| Purpose | A rate limit was hit; carries the server-requested wait so impl 08 can honour it. |
| Preconditions | None beyond types. |
| Postconditions | `self.retry_after` is `None` or a finite float `>= 0.0`. |
| Invariants | As postconditions. `_extra_attrs = ("retry_after",)`. |
| Algorithm | 1. Run U00-01 with `message` and `context`.<br>2. If `retry_after` is `None`, or not finite (`math.isfinite` is false), set `self.retry_after = None`.<br>3. Else if `retry_after < 0`, set `0.0`.<br>4. Else set `float(retry_after)`. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | O(1) beyond U00-01. |
| Security notes | Negative or non-finite server values cannot produce a negative or infinite sleep downstream. |
| Tests | UT00-03, UT00-08 |

#### U00-05 herness.core.errors.CircuitOpen

Signature (`__init__`):

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `message` | `str` | — | positional-only | as U00-01 |
| `key` | `str` | — | keyword-only | Breaker key (impl 08) |
| `retry_at` | `datetime.datetime` | — | keyword-only | When the breaker half-opens |
| `**context` | scalar | — | keyword (variadic) | as U00-01 |

| Field | Content |
|-------|---------|
| Kind | class (subclass of `RetryableError`) |
| Purpose | A circuit breaker is open; the caller must not call until `retry_at`. |
| Preconditions | None beyond types. |
| Postconditions | `self.key == key`; `self.retry_at` is timezone-aware UTC. |
| Invariants | `_extra_attrs = ("key", "retry_at")`. |
| Algorithm | 1. Run U00-01.<br>2. `self.key = key`.<br>3. If `retry_at.tzinfo is None` or `retry_at.utcoffset() is None`, set `self.retry_at = retry_at.replace(tzinfo=datetime.UTC)` (a naive value is read as UTC; this module cannot import `herness.core.time`, see C3 in §3.6). Else set `self.retry_at = retry_at.astimezone(datetime.UTC)`. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | None. |
| Tests | UT00-04, UT00-08 |

#### U00-06 herness.core.errors.ModelRefused

Signature (`__init__`):

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `message` | `str` | — | positional-only | as U00-01 |
| `category` | `str \| None` | `None` | keyword-only | Provider refusal category (spec 05: Anthropic `stop_details.category`) |
| `**context` | scalar | — | keyword (variadic) | as U00-01 |

| Field | Content |
|-------|---------|
| Kind | class (subclass of `RecoverableError`) |
| Purpose | The model refused to answer; impl 08 moves to the next fallback-chain entry. |
| Preconditions | None beyond types. |
| Postconditions | `self.category` is `None` or a string of at most 64 characters. |
| Invariants | `_extra_attrs = ("category",)`. |
| Algorithm | 1. Run U00-01.<br>2. If `category` is `None`, store `None`; else store its first 64 characters. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | Category is provider metadata, never model text. |
| Tests | UT00-05, UT00-08 |

#### U00-07 herness.core.errors.error_kind

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `exc` | `BaseException` | — | positional | any exception |

Returns: `Literal["retryable", "recoverable", "fatal", "unknown"]`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Classify any exception into its taxonomy category for logs and job records. |
| Preconditions | None. |
| Postconditions | Returns exactly one of the four literals. |
| Algorithm | 1. If `isinstance(exc, RetryableError)`, return `"retryable"`.<br>2. If `isinstance(exc, RecoverableError)`, return `"recoverable"`.<br>3. If `isinstance(exc, FatalError)`, return `"fatal"`.<br>4. Return `"unknown"` (includes a bare `HernessError` and every non-Herness exception). |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | O(depth of MRO). |
| Security notes | None. |
| Tests | UT00-06 |

#### U00-08 herness.core.errors.to_log_fields

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `exc` | `BaseException` | — | positional | any exception |

Returns: `dict[str, str | int | float | bool | None]`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Turn an exception into safe, flat, JSON-serialisable log or job-record fields. |
| Preconditions | None. |
| Postconditions | Every value is a scalar; the dict never contains the message of a non-Herness exception or of any `__cause__`/`__context__`. |
| Algorithm | 1. `out = {"error_type": type(exc).__name__, "error_kind": error_kind(exc)}`.<br>2. If `exc` is a `HernessError`: set `out["error_message"] = exc.message` and `out["error_hint"] = exc.hint`; for each `(k, v)` in `exc.details`, set `out["detail_" + k] = v`. For each extra attribute name in `type(exc)._extra_attrs`: value `v = getattr(exc, name)`; if `v` is a `datetime`, store `v.isoformat()`; else store `v`. For each `(k, v)` in `exc.context`: if `k` already in `out`, store under `"ctx_" + k`; else under `k`.<br>3. If `exc` is not a `HernessError`, set `out["error_message"] = ""` (third-party messages can carry URLs with tokens; the traceback in the log line goes through the scrubber instead).<br>4. `cause = exc.__cause__ or exc.__context__`; set `out["cause_type"] = type(cause).__name__` when `cause` is not `None`, else `None`.<br>5. Return `out`. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | O(context size). |
| Security notes | TH00-06: third-party and cause messages never reach job records (impl 08 `last_error`) through this function. `hint` and `details` are copied because they are bounded Herness-authored values (U00-01). |
| Tests | UT00-07, UT00-71, PT00-01, ST00-13 |

### 3.2 `herness.core.time`

Module constants: `DB_TS_LEN: Final = 27`; `MAX_SLEEP_S: Final = 3600.0`; private `_DB_TS_RE = ^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{6})Z$`; private `_DATE_RE = ^\d{4}-\d{2}-\d{2}$`; private `_ZONE_RE = ^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*$`. All functions are module attributes looked up at call time by callers (§2 import convention) so impl 11's `FakeClock` can patch `now`, `sleep` and `asleep`.

#### U00-09 herness.core.time.now

Parameters: none. Returns: `datetime.datetime`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Current wall-clock time as a timezone-aware UTC datetime; the only clock read in Herness code. |
| Preconditions | None. |
| Postconditions | Result has `tzinfo is datetime.UTC`. |
| Algorithm | 1. Return `datetime.datetime.now(datetime.UTC)`. |
| Side effects | Reads the system clock. |
| Errors | None. |
| Concurrency | Thread- and async-safe. |
| Complexity and limits | O(1). |
| Security notes | None. |
| Tests | UT00-09 |

#### U00-10 herness.core.time.sleep

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `seconds` | `float` | — | positional | finite, `0 <= seconds <= MAX_SLEEP_S` |

Returns: `None`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Block the current thread for a bounded time; the only blocking sleep in Herness code. |
| Preconditions | See constraints; violation raises `SchemaViolation`. |
| Postconditions | At least `seconds` elapsed (OS scheduling permitting). |
| Algorithm | 1. If `seconds` is not finite, or `< 0`, or `> MAX_SLEEP_S`, raise `SchemaViolation("invalid sleep duration", seconds=seconds)` (a non-finite value is passed as its `repr` string).<br>2. Call `time.sleep(seconds)`. |
| Side effects | Blocks the thread. |
| Errors | invalid duration → `SchemaViolation` (`seconds`). |
| Concurrency | Blocking; never called from the event loop (ENG §2.5). |
| Complexity and limits | Capped at 3,600 s: ENG §2.5 "no unbounded waits". |
| Security notes | None. |
| Tests | UT00-10 |

#### U00-11 herness.core.time.asleep

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `seconds` | `float` | — | positional | as U00-10 |

Returns: `Awaitable[None]` (async function).

| Field | Content |
|-------|---------|
| Kind | async function |
| Purpose | Non-blocking bounded sleep for async code (LLM clients, swarm, chat). |
| Preconditions | As U00-10. |
| Postconditions | As U00-10. |
| Algorithm | 1. Validate exactly as U00-10 step 1.<br>2. `await asyncio.sleep(seconds)`. |
| Side effects | Suspends the task. |
| Errors | invalid duration → `SchemaViolation` (`seconds`). |
| Concurrency | Async-safe; cancellation propagates `CancelledError` unchanged. |
| Complexity and limits | As U00-10. |
| Security notes | None. |
| Tests | UT00-11 |

#### U00-12 herness.core.time.monotonic

Parameters: none. Returns: `float` (seconds).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Monotonic seconds for measuring durations (latency fields, retry intervals of the log file sink). |
| Preconditions | None. |
| Postconditions | Never less than a previous result in the same process. |
| Algorithm | 1. Return `time.monotonic()`. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Thread-safe. |
| Complexity and limits | O(1). |
| Security notes | None. |
| Tests | UT00-12 |

#### U00-13 herness.core.time.ensure_utc

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `dt` | `datetime.datetime` | — | positional | timezone-aware |

Returns: `datetime.datetime` in UTC.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Reject naive datetimes and normalise aware ones to UTC (ENG §3.2). |
| Preconditions | `dt` is a `datetime` (not a bare `date`), aware. |
| Postconditions | Same instant, `tzinfo is datetime.UTC`. |
| Algorithm | 1. If `type(dt)` is not a `datetime` subclass, raise `SchemaViolation("expected datetime", got=type(dt).__name__)`.<br>2. If `dt.tzinfo is None` or `dt.utcoffset() is None`, raise `SchemaViolation("naive datetime rejected")`.<br>3. Return `dt.astimezone(datetime.UTC)`. |
| Side effects | None. |
| Errors | not a datetime → `SchemaViolation` (`got`); naive → `SchemaViolation`. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | TH00-07. |
| Tests | UT00-13 |

#### U00-14 herness.core.time.format_utc

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `dt` | `datetime.datetime` | — | positional | aware |

Returns: `str` of exactly `DB_TS_LEN` (27) characters, `YYYY-MM-DDTHH:MM:SS.ffffffZ`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | The fixed-width UTC text of design 00 §8 used for SQLite columns, log `ts` and canonical JSON datetimes. |
| Preconditions | As U00-13. |
| Postconditions | Text order equals time order for all results. |
| Algorithm | 1. `u = ensure_utc(dt)`.<br>2. Build the string by zero-padded integer formatting (not `strftime`, whose `%Y` is not zero-padded on every platform): year 4 digits, month 2, day 2, `T`, hour 2, minute 2, second 2, `.`, microsecond 6, `Z`. |
| Side effects | None. |
| Errors | as U00-13. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | TH00-07. |
| Tests | UT00-14, PT00-02 |

#### U00-15 herness.core.time.parse_utc

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `text` | `str` | — | positional | exactly the U00-14 format |

Returns: `datetime.datetime` in UTC.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Strict inverse of `format_utc` for values read back from SQLite and logs. |
| Preconditions | `text` matches `_DB_TS_RE` and is a valid calendar instant. |
| Postconditions | `format_utc(parse_utc(t)) == t`. |
| Algorithm | 1. If `text` is not a `str` or `len(text) != DB_TS_LEN`, raise `SchemaViolation("bad timestamp text", length=len(text) if str else -1)`.<br>2. Match `_DB_TS_RE` with `fullmatch`; no match → `SchemaViolation("bad timestamp text")`.<br>3. Build `datetime(year, month, day, hour, minute, second, microsecond, tzinfo=datetime.UTC)`; a `ValueError` (for example 2026-02-30) is re-raised as `SchemaViolation("bad timestamp text")` with `from exc`.<br>4. Return it. The input text is never copied into the error (it may come from untrusted rows). |
| Side effects | None. |
| Errors | wrong type or length, pattern mismatch, invalid date → `SchemaViolation` (`length` only). |
| Concurrency | Pure. |
| Complexity and limits | O(1); input longer than 27 characters rejected before regex. |
| Security notes | TH00-07. |
| Tests | UT00-15, PT00-02, ST00-14 |

#### U00-16 herness.core.time.parse_iso

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `text` | `str` | — | positional | ISO-8601 date or datetime with offset; 1–64 characters after stripping |

Returns: `datetime.datetime` in UTC.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Lenient parser for operator and config input (CLI `--since`, YAML dates) that still rejects naive datetimes. |
| Preconditions | See constraints. |
| Postconditions | Aware UTC result. |
| Algorithm | 1. `s = text.strip()`; if `len(s) == 0` or `len(s) > 64`, raise `SchemaViolation("bad ISO timestamp", length=len(s))`.<br>2. If `_DATE_RE.fullmatch(s)`: parse with `datetime.date.fromisoformat(s)` and return midnight of that date in UTC.<br>3. If `s` ends with `Z` or `z`, replace that last character with `+00:00`.<br>4. `dt = datetime.datetime.fromisoformat(s)`; `ValueError` → `SchemaViolation("bad ISO timestamp", length=len(s))` from the exception.<br>5. If `dt` is naive, raise `SchemaViolation("naive datetime rejected")`.<br>6. Return `dt.astimezone(datetime.UTC)`. |
| Side effects | None. |
| Errors | empty, too long, unparseable, naive → `SchemaViolation` (`length`). |
| Concurrency | Pure. |
| Complexity and limits | Input capped at 64 characters. |
| Security notes | TH00-07. |
| Tests | UT00-16, ST00-14 |

#### U00-17 herness.core.time.utc_day

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `dt` | `datetime.datetime` | — | positional | aware |

Returns: `str` `YYYY-MM-DD` (UTC date).

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | UTC date text for daily file names (`herness-<date>.jsonl`) and `dt=` partitions. |
| Preconditions | As U00-13. |
| Postconditions | Equals the first 10 characters of `format_utc(dt)`. |
| Algorithm | 1. `u = ensure_utc(dt)`.<br>2. Return zero-padded `YYYY-MM-DD` of `u`. |
| Side effects | None. |
| Errors | as U00-13. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | None. |
| Tests | UT00-17 |

#### U00-18 herness.core.time.zone

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `name` | `str` | — | positional | IANA zone name, 1–64 characters, matches `_ZONE_RE` |

Returns: `zoneinfo.ZoneInfo`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Resolve a business time zone (`weights.yaml: business_timezone`) from the bundled `tzdata`. |
| Preconditions | See constraints. |
| Postconditions | A valid `ZoneInfo`. |
| Algorithm | 1. If `len(name)` is 0 or above 64, or `_ZONE_RE.fullmatch(name)` is `None`, or `".."` in `name`, raise `ConfigError("unknown time zone", zone=name)`.<br>2. Return `zoneinfo.ZoneInfo(name)`; `zoneinfo.ZoneInfoNotFoundError` or `ValueError` → `ConfigError("unknown time zone", zone=name)` from the exception. |
| Side effects | Reads zone data from the `tzdata` package (cached by `zoneinfo`). |
| Errors | invalid or unknown name → `ConfigError` (`zone`). |
| Concurrency | Thread-safe (`zoneinfo` cache is thread-safe). |
| Complexity and limits | Name capped at 64 characters. |
| Security notes | The pattern and the `..` check stop path-like names reaching the file lookup. |
| Tests | UT00-18 |

### 3.3 `herness.core.ids`

#### U00-19 herness.core.ids.CROCKFORD_ALPHABET, ULID_LEN

| Field | Content |
|-------|---------|
| Kind | constant |
| Purpose | ULID encoding constants. |
| Signature | `CROCKFORD_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"`; `ULID_LEN: Final[int] = 26`. Private: `_ULID_RE = ^[0-7][0-9A-HJKMNP-TV-Z]{25}$`; `_RAND_BITS = 80`; `_TS_BITS = 48`. |
| Preconditions | — |
| Postconditions | — |
| Algorithm | Not applicable. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | None. |
| Tests | UT00-19, UT00-25 |

#### U00-20 herness.core.ids.new_ulid

Parameters: none. Returns: `str` (26 characters, uppercase Crockford base32).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Generate a ULID that is unique and strictly increasing within the process (spec 07 relies on `rec_id` order). |
| Preconditions | None. |
| Postconditions | Matches `_ULID_RE`; greater (as a string) than every ULID previously returned in this process. |
| Invariants | Module-private state `_last_ms: int = -1`, `_last_rand: int = 0`, guarded by `_lock: threading.Lock`. |
| Algorithm | 1. `ms = time.time_ns() // 1_000_000` (standard-library `time`; ULIDs do not follow `FakeClock`).<br>2. Acquire `_lock`.<br>3. If `ms > _last_ms`: `rand = secrets.randbits(80)`.<br>4. Else (same millisecond or clock moved backwards): `ms = _last_ms`; `rand = _last_rand + 1`; if `rand >= 2**80`: `ms = _last_ms + 1` and `rand = secrets.randbits(80)`.<br>5. Set `_last_ms = ms`, `_last_rand = rand`; release `_lock`.<br>6. If `ms >= 2**48`, raise `SchemaViolation("ULID timestamp overflow")`.<br>7. `value = (ms << 80) \| rand`; for `i` in 0..25 the character is `CROCKFORD_ALPHABET[(value >> (5 * (25 - i))) & 31]`.<br>8. Return the 26-character string. |
| Side effects | Mutates the generator state. |
| Errors | timestamp beyond year 10889 → `SchemaViolation`. |
| Concurrency | Lock-protected (`_lock`), held only for steps 3–5. Uniqueness across processes relies on 80 random bits per new millisecond; ordering holds within one process only. |
| Complexity and limits | O(1); throughput target BT00-01. |
| Security notes | ULIDs are identifiers, not secrets: within one millisecond the next value is the previous plus one. Anything used as a bearer secret uses `new_token` (U00-34). TH00-05. |
| Tests | UT00-19, UT00-20, UT00-21, UT00-22, FT00-02, BT00-01 |

#### U00-21 herness.core.ids.IdKind, ID_PREFIXES

| Field | Content |
|-------|---------|
| Kind | constant (`enum.StrEnum` plus mapping) |
| Purpose | The closed set of prefixed ULID identifiers of design 00 §5. |
| Signature | `class IdKind(StrEnum)` with members and values: `RUN="run"`, `TASK="task"`, `JOB="job"`, `FINDING="finding"`, `REC="rec"`, `OUTCOME="outcome"`, `MEMORY="memory"`, `CLUSTER="cluster"`, `ITEM="item"`, `REQUEST="request"`, `EGRESS="egress"`, `AUDIT="audit"`. `ID_PREFIXES: Final[Mapping[IdKind, str]]` (a `MappingProxyType`) = `RUN→"run"`, `TASK→"task"`, `JOB→"job"`, `FINDING→"fnd"`, `REC→"rec"`, `OUTCOME→"out"`, `MEMORY→"mem"`, `CLUSTER→"cl"`, `ITEM→"rev"`, `REQUEST→"del"`, `EGRESS→"egr"`, `AUDIT→"aud"`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Every `IdKind` member has exactly one prefix; prefixes are distinct. |
| Algorithm | Not applicable. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | None. |
| Tests | UT00-23 |

#### U00-22 herness.core.ids.new_id

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `kind` | `IdKind` | — | positional | member of `IdKind` |

Returns: `str` `<prefix>_<ulid>`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Create a new prefixed identifier such as `run_01J8…`. |
| Preconditions | `kind` is an `IdKind`. |
| Postconditions | `is_valid_id(kind, result)` is true. |
| Algorithm | 1. Return `ID_PREFIXES[kind] + "_" + new_ulid()`. |
| Side effects | As U00-20. |
| Errors | as U00-20. |
| Concurrency | As U00-20. |
| Complexity and limits | O(1). |
| Security notes | As U00-20. |
| Tests | UT00-23 |

#### U00-23 herness.core.ids.new_build_id

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `now` | `datetime.datetime` | — | positional | aware; caller passes `clock.now()` (ENG §2.3) |

Returns: `str` `YYYYMMDD-HHMMSS-<ulid6>`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Create the warehouse `build_id` of design 00 §5. |
| Preconditions | `now` aware. |
| Postconditions | `is_valid_build_id(result)` is true; date-time part is `now` in UTC truncated to seconds. |
| Algorithm | 1. `u = herness.core.time.ensure_utc(now)`.<br>2. `stamp` = zero-padded `YYYYMMDD` + `-` + `HHMMSS` of `u`.<br>3. `ulid6 = new_ulid()[-6:]` (the last 6 characters, which are random bits, see DD-04).<br>4. Return `stamp + "-" + ulid6`. |
| Side effects | As U00-20. |
| Errors | naive `now` → `SchemaViolation`. |
| Concurrency | As U00-20. |
| Complexity and limits | O(1). 30 random bits per build; builds are minutes apart. |
| Security notes | None. |
| Tests | UT00-24 |

#### U00-24 herness.core.ids.is_valid_ulid

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `value` | `object` | — | positional | any |

Returns: `bool`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Check canonical ULID text (uppercase only; first character `0`–`7`). |
| Preconditions | None. |
| Postconditions | True iff `value` is a `str` of length 26 that fully matches `_ULID_RE`. |
| Algorithm | 1. Return `isinstance(value, str) and len(value) == ULID_LEN and _ULID_RE.fullmatch(value) is not None`. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | Length checked before the regex. |
| Security notes | Used by U00-37 to stop log-context injection. |
| Tests | UT00-25 |

#### U00-25 herness.core.ids.is_valid_id

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `kind` | `IdKind` | — | positional | member |
| `value` | `object` | — | positional | any |

Returns: `bool`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Check a prefixed identifier of the given kind. |
| Preconditions | None. |
| Postconditions | True iff `value` is `ID_PREFIXES[kind] + "_" + <valid ULID>`. |
| Algorithm | 1. If `value` is not a `str`, return `False`.<br>2. `p = ID_PREFIXES[kind] + "_"`; if not `value.startswith(p)`, return `False`.<br>3. Return `is_valid_ulid(value[len(p):])`. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | O(length). |
| Security notes | Callers at trust boundaries (CLI `--resume RUN_ID`, dashboard query parameters) use it before any lookup. |
| Tests | UT00-26 |

#### U00-26 herness.core.ids.is_valid_build_id

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `value` | `object` | — | positional | any |

Returns: `bool`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Check `build_id` text, including that the date-time part is a real calendar instant. |
| Preconditions | None. |
| Postconditions | True iff `value` is a 22-character `str` matching `^\d{8}-\d{6}-[0-9A-HJKMNP-TV-Z]{6}$` whose first 15 characters parse with `datetime.strptime(..., "%Y%m%d-%H%M%S")`. |
| Algorithm | 1. Type and length check (22).<br>2. Regex `fullmatch`; mismatch → `False`.<br>3. `strptime` of the first 15 characters; `ValueError` → `False`.<br>4. Return `True`. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | Build IDs name files (`wh-<build_id>.duckdb`); a valid ID contains no path separators. |
| Tests | UT00-27 |

#### U00-27 herness.core.ids.make_record_id

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `source` | `str` | — | positional | `^[a-z][a-z0-9_]{0,31}$` |
| `entity` | `str` | — | positional | `^[a-z][a-z0-9_]{0,63}$` |
| `source_key` | `str` | — | positional | 1–512 characters; no character with code point below 32 or equal to 127; no leading or trailing whitespace; may contain `:` |

Returns: `str` `<source>:<entity>:<source_key>`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Scalar builder of the design 00 §5 `record_id` (connectors build the same string vectorised in Arrow per spec 01 §4; both must agree). |
| Preconditions | See constraints. |
| Postconditions | `split_record_id(result) == (source, entity, source_key)`. |
| Algorithm | 1. Validate `source` and `entity` with their patterns; failure → `SchemaViolation("bad record_id part", part="source" or "entity")`.<br>2. Validate `source_key` per constraints; failure → `SchemaViolation("bad record_id part", part="source_key", length=len(source_key))`.<br>3. Return `f"{source}:{entity}:{source_key}"`. |
| Side effects | None. |
| Errors | invalid part → `SchemaViolation` (`part`, `length`). |
| Concurrency | Pure. |
| Complexity and limits | `source_key` capped at 512 characters (`RECORD_KEY_MAX_LEN`, U00-34). |
| Security notes | Control characters cannot enter IDs that reach logs and file names. |
| Tests | UT00-28, PT00-03 |

#### U00-28 herness.core.ids.split_record_id

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `record_id` | `str` | — | positional | output format of U00-27 |

Returns: `tuple[str, str, str]` `(source, entity, source_key)`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Parse a `record_id`; the source key keeps any further colons (monitoring keys are `<source_tool>:<event_key>`). |
| Preconditions | See constraints. |
| Postconditions | The three parts satisfy U00-27's constraints. |
| Algorithm | 1. `parts = record_id.split(":", 2)`; if `len(parts) != 3`, raise `SchemaViolation("bad record_id")`.<br>2. Validate the three parts with the U00-27 rules (same errors).<br>3. Return the tuple. |
| Side effects | None. |
| Errors | fewer than three parts or invalid part → `SchemaViolation`. |
| Concurrency | Pure. |
| Complexity and limits | O(length). |
| Security notes | Used on operator input (`herness privacy delete --record-id`). |
| Tests | UT00-29, PT00-03 |

#### U00-29 herness.core.ids.canonical_json

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `value` | `object` | — | positional | nesting depth ≤ 64; types below |

Returns: `str`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | The one canonical JSON encoding for identifiers derived by hashing (`query_id`, and `config_hash` in impl 10). Single implementation per R-14: impl 04 and impl 05 call it and define no copy. |
| Preconditions | Every node is one of the supported types below. |
| Postconditions | Equal inputs (including dicts with different insertion order) give byte-identical output. |
| Algorithm | 1. Convert recursively with depth counter starting at 0; depth above 64 → `SchemaViolation("canonical JSON too deep")`. Conversion by type, checked in this order: `bool` → itself; `int` → itself; `float` → itself if finite, else `SchemaViolation("non-finite float")`; `str` → itself; `None` → itself; `decimal.Decimal` → `str(d)` if finite, else `SchemaViolation("non-finite decimal")`; `datetime.datetime` → `herness.core.time.format_utc(dt)` (naive raises there); `datetime.date` → `d.isoformat()`; `pathlib.PurePath` → `p.as_posix()`; `enum.Enum` → convert `e.value`; `Mapping` → every key must be `str` (else `SchemaViolation("non-string key")`), values converted; `list` or `tuple` → list of converted items; anything else (including `set`, `bytes`, pydantic models) → `SchemaViolation("unsupported type", type=type(x).__name__)`. Callers pass `model.model_dump(mode="json")` for models.<br>2. Return `json.dumps(converted, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`. |
| Side effects | None. |
| Errors | as listed → `SchemaViolation` (`type`). |
| Concurrency | Pure. |
| Complexity and limits | O(size of value); depth ≤ 64. |
| Security notes | Not used for `result_hash` row encoding: design 00 §5.1 keeps keys in column order and formats floats with `.9g`, implemented in T04-01 (herness.metrics.evidence.result_hash). |
| Tests | UT00-30, PT00-04 |

#### U00-30 herness.core.ids.sha256_hex

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `data` | `str \| bytes` | — | positional | `str` is encoded as UTF-8 |

Returns: `str` (64 lowercase hex characters).

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Shared SHA-256 hex helper (`hashlib` only, ENG §5.7). |
| Preconditions | None. |
| Postconditions | `hashlib.sha256(data_bytes).hexdigest()`. |
| Algorithm | 1. If `str`, encode UTF-8.<br>2. Return the hex digest. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | O(len(data)). |
| Security notes | Not a MAC; keyed hashing is `hmac` in impl 10. |
| Tests | UT00-33 |

#### U00-31 herness.core.ids.normalize_sql

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `sql` | `str` | — | positional | any text |

Returns: `str`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | `normalized_sql` of design 00 §5: whitespace collapsed, trailing semicolons removed. Single implementation per R-14 (impl 04 and impl 05 call it). |
| Preconditions | None. |
| Postconditions | No leading or trailing whitespace; no run of two whitespace characters; does not end with `;`. |
| Algorithm | 1. Replace every maximal run of characters matching `\s` (Unicode) with one ASCII space.<br>2. Strip leading and trailing spaces.<br>3. While the result ends with `;`: remove that character, then strip trailing spaces.<br>4. Return the result (possibly empty). Whitespace inside string literals is collapsed too; this is intended by design 00 §5 (the ID is not used to execute SQL). |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | O(len(sql)). |
| Security notes | None. |
| Tests | UT00-31, PT00-05 |

#### U00-32 herness.core.ids.query_id

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `sql` | `str` | — | positional | non-empty after normalisation |
| `params` | `Mapping[str, object]` | — | positional | canonicalisable by U00-29; pass `{}` when there are none (spec 05 `run_sql`) |
| `build_id` | `str` | — | positional | `is_valid_build_id` |

Returns: `str` `q_` + 16 lowercase hex.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | The single implementation of design 00 §5 `query_id`, used by impl 04 (metrics evidence) and impl 05 (tools, Verifier) so both produce identical IDs (DD-03, R-14). |
| Preconditions | See constraints. |
| Postconditions | Same normalised SQL, params and build give the same ID. |
| Algorithm | 1. `norm = normalize_sql(sql)`; if empty, raise `SchemaViolation("empty SQL for query_id")`.<br>2. If not `is_valid_build_id(build_id)`, raise `SchemaViolation("bad build_id for query_id")`.<br>3. `payload = canonical_json({"sql": norm, "params": params, "build_id": build_id})`.<br>4. Return `"q_" + sha256_hex(payload)[:16]`. |
| Side effects | None. |
| Errors | empty SQL, bad build ID, non-canonicalisable params → `SchemaViolation`. |
| Concurrency | Pure. |
| Complexity and limits | O(len(sql) + size of params); BT00-02. |
| Security notes | TH00-14 (accepted residual RR-03: a `Decimal` and a `str` with the same text encode identically). |
| Tests | UT00-32, PT00-05, BT00-02 |

#### U00-33 herness.core.ids.new_token

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `nbytes` | `int` | `32` | positional | `16 <= nbytes <= 64` |

Returns: `str` (URL-safe base64 without padding; 43 characters for 32 bytes).

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Unguessable token for anything that acts as a bearer secret (for example chat session IDs, ENG §5.2 V7). |
| Preconditions | See constraints; violation → `SchemaViolation("bad token size", nbytes=nbytes)`. |
| Postconditions | Result from `secrets.token_urlsafe(nbytes)`. |
| Algorithm | 1. Validate `nbytes`.<br>2. Return `secrets.token_urlsafe(nbytes)`. |
| Side effects | Reads the OS CSPRNG. |
| Errors | size out of range → `SchemaViolation` (`nbytes`). |
| Concurrency | Thread-safe. |
| Complexity and limits | O(nbytes). |
| Security notes | TH00-05. Tokens are confidential: never logged (the log guard drops `token` keys, U00-41). |
| Tests | UT00-34, ST00-05 |

#### U00-34 herness.core.ids.RECORD_KEY_MAX_LEN and private patterns

| Field | Content |
|-------|---------|
| Kind | constant |
| Purpose | Limits and patterns used by U00-24 … U00-28. |
| Signature | `RECORD_KEY_MAX_LEN: Final = 512` (public, used by connectors to reject oversized keys before building IDs); private `_SOURCE_RE`, `_ENTITY_RE`, `_BUILD_ID_RE` with the patterns given in U00-26 and U00-27. |
| Preconditions | — |
| Postconditions | — |
| Algorithm | Not applicable. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | None. |
| Tests | UT00-28 |

### 3.4 `herness.core.logging` and `herness.core._log_pipeline`

Design: structlog is configured to hand every event to the standard-library `logging` module, so structlog events and third-party `logging` records (httpx, duckdb, snowflake) pass through one formatter chain and one set of handlers. The chain order is fixed:

| # | Step | Unit | Runs for |
|---|------|------|----------|
| 1 | `structlog.stdlib.filter_by_level` | library | structlog events |
| 2 | `structlog.contextvars.merge_contextvars` | library | all (native chain and `foreign_pre_chain`) |
| 3 | `check_event_name` | U00-43 | structlog events, in the caller's thread, so strict mode raises to the caller |
| 4 | `structlog.stdlib.ProcessorFormatter.wrap_for_formatter` | library | structlog events |
| 5 | `ProcessorFormatter.remove_processors_meta` | library | all (inside the formatter) |
| 6 | `add_component` | U00-42 | all |
| 7 | `structlog.stdlib.add_log_level` (lowercase `level`) | library | all |
| 8 | `add_timestamp` (`ts`, `pid`) | U00-42 | all |
| 9 | `structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False, max_frames=20))` → key `exception` | library | all |
| 10 | `normalize_values` | U00-42 | all |
| 11 | `guard_sensitive` | U00-41 | all |
| 12 | scrubber (T10-07 (herness.core.secrets.scrub_secrets), passed in) | impl 10 | all, when configured |
| 13 | `limit_sizes` | U00-43 | all |
| 14 | `render_json` | U00-43 | all |

Scrubbing (12) runs after all values are strings (10) and before truncation (13), so a secret that straddles the truncation point is still found whole (ST00-01).

#### U00-35 herness.core.logging constants

| Field | Content |
|-------|---------|
| Kind | constant |
| Purpose | Fixed keys, limits and patterns of the logging contract. |
| Signature | `LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]`.<br>`REQUIRED_KEYS: Final = ("ts", "level", "event", "component")`.<br>`CONTEXT_ID_KEYS: Final = ("run_id", "task_id", "job_id", "build_id")`.<br>`SECRET_KEYS: Final[frozenset[str]] = {"password", "passwd", "secret", "token", "api_key", "apikey", "authorization", "cookie", "set_cookie", "private_key", "client_secret", "access_token", "refresh_token"}`; also any key ending in `_password`, `_secret`, `_token` or `_api_key` (private helper `_is_secret_key`).<br>`TEXT_KEYS: Final[frozenset[str]] = {"prompt", "prompts", "completion", "messages", "text", "ticket_text", "description", "short_description", "close_notes", "comments", "summary", "body", "content", "payload", "raw"}`.<br>`MAX_FIELD_CHARS: Final = 2000`; `MAX_LINE_BYTES: Final = 16384`; `MAX_DEPTH: Final = 4`.<br>`EVENT_NAME_RE: Final = ^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,}$` (ENG §3.6 `component.object.action`).<br>`COMPONENT_RE: Final = ^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`.<br>`NOISY_LOGGERS: Final = ("httpx", "httpcore", "urllib3", "asyncio", "filelock", "huggingface_hub", "snowflake.connector", "pymongo", "openai", "anthropic")`.<br>`FILE_RETRY_S: Final = 60.0`; `LOG_FILE_PREFIX: Final = "herness-"`; `OMITTED: Final = "[omitted]"`. |
| Preconditions | — |
| Postconditions | — |
| Algorithm | Key matching in U00-41 is case-insensitive on the lower-cased key. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH00-01, TH00-02, TH00-04. |
| Tests | UT00-41, ST00-02 |

#### U00-36 herness.core.logging.configure_logging

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `level` | `str` | `"INFO"` | positional | case-insensitive member of `LogLevel` (from `logging.level`, impl 10) |
| `log_dir` | `pathlib.Path \| None` | `None` | keyword-only | directory for `herness-<date>.jsonl` (from `paths.logs`, impl 10); `None` = no file |
| `scrubber` | `structlog.typing.Processor \| None` | `None` | keyword-only | required when `log_dir` is not `None` |
| `stderr` | `bool` | `True` | keyword-only | also write lines to `sys.stderr` |
| `strict_event_names` | `bool` | `False` | keyword-only | tests set `True` |

Returns: `None`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Configure process-wide JSON-lines logging for structlog and standard-library loggers. Called by the composition roots (T09-20 (herness.cli.main), the Streamlit page wrapper, the job worker) once at start with stderr only, and again after config load with `log_dir` and `scrubber`. |
| Preconditions | Level valid; if `log_dir` given then `scrubber` given; `log_dir` creatable. |
| Postconditions | Root `logging` logger has exactly the handlers requested (stderr `StreamHandler` and/or `DailyJsonlHandler`), all using one `ProcessorFormatter` with chain steps 5–14; structlog configured with steps 1–4; `NOISY_LOGGERS` set to the higher of `level` and `WARNING`; event `core.logging.configured` emitted. |
| Invariants | At most one Herness-installed handler of each type on the root logger; the module-private `_state` records them. |
| Algorithm | 1. `lvl = level.upper()`; if not a `LogLevel` value, raise `ConfigError("invalid log level", level=level[:20])`.<br>2. If `log_dir is not None and scrubber is None`, raise `ConfigError("file logging requires a scrubber")`.<br>3. If `log_dir` given: `log_dir.mkdir(parents=True, exist_ok=True)`; `OSError` → `ConfigError("cannot create log directory", path=str(log_dir))` from the exception.<br>4. Acquire module lock `_config_lock` (`threading.Lock`).<br>5. Remove from the root logger and close the handlers previously installed by this module (`_state.handlers`); leave handlers installed by others untouched.<br>6. Build one formatter `ProcessorFormatter(foreign_pre_chain=[merge_contextvars], processors=[remove_processors_meta, add_component, add_log_level, add_timestamp, ExceptionRenderer(ExceptionDictTransformer(show_locals=False, max_frames=20)), normalize_values, guard_sensitive, scrubber (only when given), limit_sizes, render_json])`.<br>7. If `stderr`: add `logging.StreamHandler(sys.stderr)` with the formatter. If `log_dir`: add `DailyJsonlHandler(log_dir)` with the formatter.<br>8. Set the root level to `lvl`; for each name in `NOISY_LOGGERS` set its level to the numerically higher of `lvl` and `WARNING`.<br>9. `structlog.configure(processors=[filter_by_level, merge_contextvars, check_event_name(strict=strict_event_names), ProcessorFormatter.wrap_for_formatter], logger_factory=structlog.stdlib.LoggerFactory(), wrapper_class=structlog.stdlib.BoundLogger, cache_logger_on_first_use=False)`.<br>10. Store handlers and settings in `_state`; release the lock.<br>11. Emit `get_logger("core.logging").info("core.logging.configured", level=lvl, log_dir=<str or None>, scrubber=<bool>, strict_event_names=<bool>)`. |
| Side effects | Global `logging` and structlog configuration; creates `log_dir`; the day file opens lazily on the first line. |
| Errors | bad level → `ConfigError` (`level`); file without scrubber → `ConfigError`; directory creation failure → `ConfigError` (`path`). |
| Concurrency | Lock-protected by `_config_lock`; intended for process start. Handlers are thread-safe (standard `logging.Handler` lock). |
| Complexity and limits | O(number of handlers). |
| Security notes | TH00-01 (no file sink without scrubber), TH00-02, TH00-04. Module-level state is an ENG §2.3 exception listed in §13.3; reset by U00-39. |
| Tests | UT00-35, UT00-36, UT00-37, UT00-43, UT00-46, ST00-01 |

#### U00-37 herness.core.logging.bind_ids

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `**ids` | `str` | — | keyword (variadic) | keys ⊆ `CONTEXT_ID_KEYS`; `run_id`, `task_id`, `job_id` valid for `IdKind.RUN`, `TASK`, `JOB`; `build_id` valid per `is_valid_build_id` |

Returns: `contextlib.AbstractContextManager[None]`.

| Field | Content |
|-------|---------|
| Kind | function (context manager via `contextlib.contextmanager`) |
| Purpose | Attach the IDs in scope to every log line emitted inside the block, in the current thread or asyncio task only. |
| Preconditions | See constraints. |
| Postconditions | Inside the block every line carries the bound IDs; after exit the previous values are restored (nesting works). |
| Algorithm | 1. For each key: if not in `CONTEXT_ID_KEYS`, raise `SchemaViolation("unknown log context key", key=key[:40])`.<br>2. Validate each value with the matching validator (U00-25 or U00-26); failure → `SchemaViolation("invalid id for log context", key=key)` (the value is not echoed).<br>3. `tokens = structlog.contextvars.bind_contextvars(**ids)`.<br>4. `yield`.<br>5. In a `finally` block, `structlog.contextvars.reset_contextvars(**tokens)`. |
| Side effects | Mutates context variables of the current context. |
| Errors | unknown key or invalid value → `SchemaViolation` (`key`). |
| Concurrency | `contextvars`: isolated per thread and per asyncio task. |
| Complexity and limits | At most 4 keys. |
| Security notes | TH00-03: only validated ID text enters every line. |
| Tests | UT00-38, UT00-39 |

#### U00-38 herness.core.logging.get_logger

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `component` | `str` | — | positional | matches `COMPONENT_RE`, ≤ 64 characters (for example `connectors`, `harness.tool`) |

Returns: `structlog.stdlib.BoundLogger`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Logger bound to its `component`, a required key of every line. |
| Preconditions | See constraints. |
| Postconditions | Every event from the logger carries `component`. |
| Algorithm | 1. If `len(component) > 64` or `COMPONENT_RE.fullmatch(component)` is `None`, raise `SchemaViolation("invalid log component", component=component[:64])`.<br>2. Return `structlog.stdlib.get_logger("herness." + component).bind(component=component)`. |
| Side effects | None (structlog creates loggers lazily). |
| Errors | invalid component → `SchemaViolation` (`component`). |
| Concurrency | Thread-safe. Loggers may be created at import time; they pick up configuration at first use because `cache_logger_on_first_use=False`. |
| Complexity and limits | O(1). |
| Security notes | None. |
| Tests | UT00-40 |

#### U00-39 herness.core.logging.reset_logging

Parameters: none. Returns: `None`.

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Undo `configure_logging` (test fixtures; process shutdown). |
| Preconditions | None. |
| Postconditions | Herness handlers removed and closed; structlog defaults restored; context variables cleared; `_state` empty. |
| Algorithm | 1. Acquire `_config_lock`.<br>2. Remove and close every handler in `_state.handlers` from the root logger.<br>3. `structlog.reset_defaults()`; `structlog.contextvars.clear_contextvars()`.<br>4. Clear `_state`; release the lock. |
| Side effects | Closes the day file. |
| Errors | None. |
| Concurrency | Lock-protected. |
| Complexity and limits | O(handlers). |
| Security notes | None. |
| Tests | UT00-45 |

#### U00-40 herness.core._log_pipeline.DailyJsonlHandler

Signature (`__init__`):

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `log_dir` | `pathlib.Path` | — | positional | existing directory |

| Field | Content |
|-------|---------|
| Kind | class (subclass of `logging.Handler`); private module |
| Purpose | Append formatted lines to `log_dir/herness-<UTC date>.jsonl`, switch file at UTC midnight, and survive disk errors without failing the caller. |
| Preconditions | Directory exists (U00-36 step 3). |
| Postconditions | Each accepted record is one line terminated by `\n`, flushed before `emit` returns. |
| Invariants | `_day: str \| None`, `_stream: TextIO \| None`, `_failed_at: float \| None` (monotonic seconds), `_dropped: int`. `_stream` is open only for `_day`. |
| Algorithm | `emit(record)` (called with the handler lock held by `logging.Handler.handle`):<br>1. `line = self.format(record)`.<br>2. If `_failed_at` is not `None` and `clock.monotonic() - _failed_at < FILE_RETRY_S`: `_dropped += 1`; return.<br>3. `day = clock.utc_day(clock.now())`. If `day != _day` or `_stream is None`: close `_stream` if open; open `log_dir / (LOG_FILE_PREFIX + day + ".jsonl")` with mode `"a"`, `encoding="utf-8"`, `newline="\n"`; set `_day = day`.<br>4. If `_failed_at` is not `None` (recovering): write the recovery line (below) to the file and to `sys.stderr`; set `_failed_at = None`, `_dropped = 0`.<br>5. Write `line + "\n"` in one `write` call; `flush()`.<br>6. On `OSError` in steps 3–5: close `_stream` (a second `OSError` on close is ignored), set `_stream = None`, `_failed_at = clock.monotonic()`, `_dropped += 1`, and write the failure line to `sys.stderr`.<br>Failure line: one JSON object with `ts` (`format_utc(now)`), `level: "error"`, `event: "core.logging.sink_failed"`, `component: "core.logging"`, `path` (the file path as POSIX text), `error_type` (exception class name), `retry_in_s: 60.0`. Recovery line: `level: "info"`, `event: "core.logging.sink_recovered"`, `component: "core.logging"`, `path`, `dropped_lines`. Both are built directly with `json.dumps` (not through `logging`, which would recurse).<br>`close()`: close `_stream` under the handler lock, then `super().close()`. |
| Side effects | Appends to the day file; writes to stderr on failure and recovery. |
| Errors | None escape `emit`; `OSError` handled by step 6. |
| Concurrency | Thread-safe through the `logging.Handler` lock. Several processes append to the same day file; each line is one `write` call (residual RR-01). |
| Complexity and limits | One file open per process per day; lines already capped at `MAX_LINE_BYTES` by U00-43. |
| Security notes | TH00-04 (a full disk does not stop the process). File ACLs are applied by impl 10. |
| Tests | UT00-36, FT00-01, BT00-03 |

#### U00-41 herness.core._log_pipeline.guard_sensitive

Signature: structlog processor `(logger: object, method_name: str, event_dict: MutableMapping[str, object]) -> MutableMapping[str, object]`.

| Field | Content |
|-------|---------|
| Kind | function (structlog processor); private module |
| Purpose | Drop secret-named fields at every level, and free-text fields above DEBUG (ENG §3.6), before the scrubber runs. |
| Preconditions | `event_dict["level"]` is set (chain step 7). |
| Postconditions | No key matching the secret rule holds its value; above DEBUG no `TEXT_KEYS` key holds its value. |
| Algorithm | 1. `is_debug = event_dict.get("level") == "debug"`.<br>2. Walk the top-level keys and, recursively, the keys of nested dicts down to `MAX_DEPTH`.<br>3. For each key `k` with lower-cased form `kl`: if `_is_secret_key(kl)`, replace the value with `OMITTED`; else if `kl in TEXT_KEYS` and not `is_debug`, replace the value with `OMITTED`.<br>4. The top-level keys `event`, `component`, `level`, `ts` are never replaced.<br>5. Return `event_dict`. |
| Side effects | Mutates `event_dict`. |
| Errors | None. |
| Concurrency | Pure over its argument. |
| Complexity and limits | O(number of keys down to depth 4). |
| Security notes | TH00-01, TH00-02. First line of defence; the impl 10 scrubber is the last (ENG §3.6). |
| Tests | ST00-02 |

#### U00-42 herness.core._log_pipeline.add_component, add_timestamp, normalize_values

All three have the structlog processor signature of U00-41.

| Field | Content |
|-------|---------|
| Kind | function (three structlog processors); private module |
| Purpose | Fill required keys for every record and make every value JSON-native before guarding, scrubbing and truncation. |
| Preconditions | None. |
| Postconditions | `component`, `ts` and `pid` present; every value is `str`, `int`, `float`, `bool`, `None`, or a list or dict of these. |
| Algorithm | `add_component`:<br>1. If `component` is already present, return unchanged.<br>2. Else read the standard-library logger name from `event_dict["_record"].name` when present: if it starts with `herness.`, use the remainder; otherwise use `"ext." + <first dot-separated segment>` (for example `ext.httpx`, `ext.snowflake`). With no record, use `"unknown"`.<br>`add_timestamp`:<br>1. `event_dict["ts"] = clock.format_utc(clock.now())` (follows `FakeClock` in tests).<br>2. `event_dict["pid"] = os.getpid()`.<br>`normalize_values` (recursive; depth counted from the top level):<br>1. `str`, `int`, `bool`, `None` unchanged; `float` unchanged when finite, else its `repr` string.<br>2. `datetime.datetime`: aware → `clock.format_utc`; naive → `isoformat()` followed by the suffix `" naive"`.<br>3. `decimal.Decimal` → `str`; `pathlib.PurePath` → `as_posix()`; `enum.Enum` → its `value`, normalised; `pydantic.SecretStr` and `pydantic.SecretBytes` → `"**********"`; `BaseException` → `type(e).__name__ + ": " + str(e)`.<br>4. `Mapping` → dict with `str(key)` keys and normalised values; `list`, `tuple` → list; `set`, `frozenset` → list sorted by `str`.<br>5. Deeper than `MAX_DEPTH` → `str(value)`; any other type → `str(value)`.<br>6. The keys `_record` and `_from_structlog` are skipped (removed earlier by `remove_processors_meta` for the formatter chain). |
| Side effects | Mutate `event_dict`. |
| Errors | None escape. If `str(value)` raises `ValueError`, `TypeError` or `RecursionError`, the value becomes `"<unprintable " + type name + ">"` (a named-exception handler, not `except Exception`). |
| Concurrency | Pure over their argument. |
| Complexity and limits | O(size) with depth ≤ 4. |
| Security notes | Converting to strings before the scrubber lets the scrubber see every value (TH00-01). |
| Tests | UT00-35, UT00-41, UT00-43, UT00-47 |

#### U00-43 herness.core._log_pipeline.check_event_name, limit_sizes, render_json

`check_event_name(strict: bool) -> structlog.typing.Processor` is a factory; `limit_sizes` is a processor; `render_json` is a processor returning `str`.

| Field | Content |
|-------|---------|
| Kind | function (processor factory and two processors); private module |
| Purpose | Enforce event naming, bound line size, and render one JSON object per line with required keys first. |
| Preconditions | Values normalised (U00-42). |
| Postconditions | Output is one line (no raw newline), valid UTF-8 JSON, at most `MAX_LINE_BYTES` bytes, keys in the order `ts`, `level`, `event`, `component`, `pid`, then the present `CONTEXT_ID_KEYS` in that order, then the remaining keys in insertion order. |
| Algorithm | `check_event_name(strict)` returns a processor that:<br>1. Reads `event`; if it is a `str` matching `EVENT_NAME_RE`, returns the dict unchanged.<br>2. Else if `strict`, raises `SchemaViolation("invalid log event name", event=str(event)[:120])`; this runs in the caller's thread (chain step 3), so the log call raises.<br>3. Else sets `event_name_invalid = True` and returns.<br>`limit_sizes`:<br>1. Every `str` value (recursively) longer than `MAX_FIELD_CHARS` characters is cut to `MAX_FIELD_CHARS` characters followed by `"…[truncated]"`.<br>`render_json`:<br>1. Build an ordered dict per the postcondition.<br>2. `line = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), allow_nan=False)`; `json.dumps` escapes `\n`, `\r` and other control characters, so a value can never start a new line.<br>3. While `len(line.encode("utf-8")) > MAX_LINE_BYTES`: remove the key, other than `REQUIRED_KEYS`, `pid` and `CONTEXT_ID_KEYS`, whose serialised value is largest (ties: the later-inserted key), add its name to `dropped_fields` (a sorted list), and re-render. If only required keys, `pid` and IDs remain and the line still does not fit, cut `event` to 200 characters and re-render once.<br>4. Return `line`. |
| Side effects | None. |
| Errors | strict mode with a bad name → `SchemaViolation` (`event`). |
| Concurrency | Pure. |
| Complexity and limits | O(size of event); line ≤ 16,384 bytes; field ≤ 2,000 characters plus the marker. |
| Security notes | TH00-03 (no line forging), TH00-04 (bounded lines). |
| Tests | UT00-41, UT00-42, ST00-03, ST00-04 |

### 3.5 `herness.core.types` (structure and ownership only)

Shape: a package (R-01, ENG §2.1, ENG §14 E6). Each owning spec writes its shared types in its own submodule; `__init__.py` re-exports them, so the import path of design 00 §6 (`from herness.core.types import NumberRef`) is unchanged. The package holds data types only (pydantic models, enums, `TypedDict`s, and the pure protocols of rule 5). The fields are owned by the specs in `TYPE_OWNERS`. Behavioral classes live in their owner packages (R-02, `DECLARED_ELSEWHERE`). DD-01 and DD-02 (§13.1) record the structural changes to design 00 §3 and §6, accepted by R-01 and R-02.

A submodule is either a module `herness/core/types/<submodule>.py` or, when the owner's types exceed the 400-line module limit (ENG §2.4), a package `herness/core/types/<submodule>/` whose `__init__.py` holds only imports from the package's own modules and an `__all__` tuple (the owner splits its types over those modules as its spec's module map states). Both forms are imported as `herness.core.types.<submodule>`; exactly one form exists per owner.

| Submodule | Owner | May import these sibling submodules |
|-----------|-------|-------------------------------------|
| `herness.core.types.decisions` | 03 | none |
| `herness.core.types.harness` | 05 | none |
| `herness.core.types.swarm` | 06 | `harness`, `jobs` |
| `herness.core.types.memory` | 07 | `harness`, `swarm` |
| `herness.core.types.jobs` | 08 | `harness` |
| `herness.core.types.reports` | 09 | none |

Import rules for every file in `herness/core/types/`:

1. Standard-library modules (per `sys.stdlib_module_names`).
2. Third-party: `pydantic`, `pydantic_core`, `typing_extensions`, `annotated_types` only.
3. Herness: `herness.core.errors`, `herness.core.ids`, the sibling submodules allowed above, and (inside a package-form submodule) the modules of the same submodule package. Nothing else (ENG §2.1).
4. Code outside `herness/core/types/` imports shared types only from `herness.core.types`, never from a submodule or from `_ownership`.
5. A shared model that must refer to a behavioral object listed in `DECLARED_ELSEWHERE` (for example spec 05 `ToolContext.tracer`) refers to a `typing.Protocol` that the same owner declares in its submodule and registers in `TYPE_OWNERS`.

#### U00-44 herness.core.types (package `__init__`)

| Field | Content |
|-------|---------|
| Kind | module (package initialiser) |
| Purpose | Single import point for shared types; holds no definitions. |
| Signature | Module contents, in order: a docstring naming design 00 §6 and this section; one `from herness.core.types.<submodule> import <Name>, ...` statement per existing owner submodule, in owner order 03, 05, 06, 07, 08, 09, names sorted; `__all__: tuple[str, ...]` equal to the sorted tuple of every re-exported name. At T00-08 no owner submodule exists, so the file holds the docstring and `__all__: tuple[str, ...] = ()`. |
| Preconditions | — |
| Postconditions | For every owner submodule that exists, every name that `TYPE_OWNERS` assigns to that owner is importable from `herness.core.types`. |
| Invariants | No `class`, `def`, assignment other than `__all__`, or conditional import in this file. It does not import `_ownership`. |
| Algorithm | Not applicable (static re-exports). Each owner card (T03-01 (herness.core.types.decisions), T05-01 (herness.core.types.harness), T06-01 (herness.core.types.swarm), T07-01 (herness.core.types.memory), T08-01 (herness.core.types.jobs), T09-01 (herness.core.types.reports)) adds its import line and its names to `__all__` in the same card that creates its submodule. |
| Side effects | Importing the package imports the existing owner submodules (pydantic only). |
| Errors | None. |
| Concurrency | Immutable after import. |
| Complexity and limits | 150 lines. |
| Security notes | TH00-09. |
| Tests | UT00-48 |

#### U00-45 herness.core.types._ownership.TYPE_OWNERS, OWNER_MODULES, OWNER_IMPORTS

| Field | Content |
|-------|---------|
| Kind | constant (three read-only mappings) |
| Purpose | Machine-readable ownership tables for the checker (U00-47). |
| Signature | `OWNER_MODULES: Final[Mapping[str, str]] = {"03": "decisions", "05": "harness", "06": "swarm", "07": "memory", "08": "jobs", "09": "reports"}`.<br>`OWNER_IMPORTS: Final[Mapping[str, frozenset[str]]]` = the sibling column of the table above, keyed by owner (`"06": {"harness", "jobs"}`, `"07": {"harness", "swarm"}`, `"08": {"harness"}`, others empty; the graph is acyclic).<br>`TYPE_OWNERS: Final[Mapping[str, str]]` (name → owner). Content = the design 00 §6 names minus `DECLARED_ELSEWHERE`, plus every helper type the owner implementation specs declare in their submodule (collected from their `herness.core.types.*` unit blocks in the consistency pass; RQ-03 of impl 03):<br>• `"03"` (U03-01 … U03-08): `QuestionType`, `Entity`, `Question`, `QuestionSet`, `DecisionInput`, `Answer`, `DecisionOutput`.<br>• `"05"` (U05-01 … U05-16): `TextPart`, `ToolCall`, `ToolCallPart`, `ToolResultPart`, `ReasoningPart`, `Message`, `SystemBlock`, `ToolSpec`, `RequestMeta`, `LLMRequest`, `Usage`, `LLMResponse`, `Tool`, `AsyncTool`, `ToolErrorInfo`, `ToolResult`, `SqlLimits`, `Budgets`, `BudgetLedger`, `TraceEmitter`, `WarehouseHandle`, `OpsHandle`, `VectorHandle`, `VectorHit`, `ToolContext`, `NumberRef`, `Evidence`, `NumberCheck`, `UncitedSpan`, `ItemResult`, `VerificationResult`, `VerifiableItem`, `LoopSignal`, `LoopLimits`, `LoopState`, `LoopCheckpoint`, `AgentResult`.<br>• `"06"` (U06-01 … U06-21, U06-140): `RunKind`, `Depth`, `Role`, `Specialty`, `ScopeEntityType`, `SkepticCheck`, `SKEPTIC_CHECKS`, `RejectReason`, `FindingStatus`, `Banner`, `SectionId`, `EntityScope`, `TaskInputs`, `TaskBudget`, `TaskSpec`, `PlannedTask` (R-28), `Finding`, `CheckResult`, `Challenge`, `CrossCheck`, `VerificationRecord`, `SwarmTaskState` (R-21), `Paragraph`, `Section`, `RecommendationItem`, `RankedEntity`, `Coverage`, `ReportDraft`, `ChatAnswer`, `ChatEvent`. The function `impact_usd` (U06-08) is not registered: the package holds data types only and no functions (R-01, R-75, ENG §2.1; it lives in `herness.harness`, impl 06; C-09 resolved).<br>• `"07"` (U07-01 … U07-10): `Layer`, `Kind`, `Status`, `KIND_LAYER`, `Provenance`, `MemoryItem`, `MemoryProposal`, `RecallHit`, `MemoryRunContext`, `RecommendationDraft`, `PriorRecommendation`, `PriorContext`, `ConfidenceAdjustment`, `SimilarOutcome`.<br>• `"08"` (U08-01 … U08-03, U08-101): `GpuClass`, `JobKind`, `ServiceName`, `ChatMode`, `BreakerState`, `PolicyName`, `JobSpec`, `JobOutcome`, `MetricSample`.<br>• `"09"` (U09-01): `ReportManifest`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Names are unique across owners. Owner cards append any further helper type they place in their submodule, in the same card, and never remove a design 00 §6 name without a design delta or a ruling. `JobContext` is not in this table: it is behavioral and lives in `herness.core.jobs` (R-02, U00-46). `PlannerOutput` (05) is not a shared type: it lives with the 05 role definitions and refers to `PlannedTask` (R-28). |
| Algorithm | Not applicable. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable (`types.MappingProxyType`). |
| Complexity and limits | — |
| Security notes | TH00-09. |
| Tests | UT00-48, UT00-49, UT00-50, UT00-80 |

#### U00-46 herness.core.types._ownership.DECLARED_ELSEWHERE

| Field | Content |
|-------|---------|
| Kind | constant |
| Purpose | Names listed in design 00 §6 that are behavioral (stateful classes, functions, or protocols that reference non-type objects) and are declared in the owner's package (DD-01, accepted by R-02). |
| Signature | `DECLARED_ELSEWHERE: Final[Mapping[str, tuple[str, str]]]` name → (owner, module), module paths per R-02 and the owner implementation specs: `LoopHooks` → (`"05"`, `herness.harness.loop`); `HarnessHooks` → (`"05"`, `herness.harness.hooks`); `GatedClient` → (`"05"`, `herness.harness.hooks`); `Tracer` → (`"05"`, `herness.harness.tracing`); `RunBudget` → (`"06"`, `herness.harness.budget`); `ModelChain` → (`"08"`, `herness.core.resilience`); `loop_signal_policy` → (`"08"`, `herness.core.resilience`); `JobContext` → (`"08"`, `herness.core.jobs`). |
| Preconditions | — |
| Postconditions | — |
| Invariants | No name appears in both `TYPE_OWNERS` and `DECLARED_ELSEWHERE`. The module of an entry may be a package (impl 08 makes `herness.core.jobs` and `herness.core.resilience` packages, placing `JobContext` in `herness.core.jobs.ports`); a definition in the named module or in any module inside the named package satisfies the entry. |
| Algorithm | Not applicable. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | None. |
| Tests | UT00-54, UT00-81 |

#### U00-47 tools.check_type_ownership.main

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `argv` | `Sequence[str] \| None` | `None` | positional | `None` reads `sys.argv[1:]`; option `--root PATH` (default: current directory) |

Returns: `int` exit code (0 pass, 1 violations, 2 usage error). Run as `python -m tools.check_type_ownership`.

| Field | Content |
|-------|---------|
| Kind | function (CLI entry) |
| Purpose | Enforce one owner per shared type, the §3.5 import rules, and the settings-module import rule of R-03 (ENG §2.1 settings exception), by static analysis (files are parsed, not imported). |
| Preconditions | `<root>/herness/core/types/_ownership.py` exists. |
| Postconditions | Every violation printed as `<path>:<line>: <CODE> <message>`; exit 1 if any. |
| Algorithm | 1. Load the four tables from `<root>/herness/core/types/_ownership.py` by executing it with `runpy.run_path` (the file has no imports beyond `types` and `typing`). Table checks: a name in both `TYPE_OWNERS` and `DECLARED_ELSEWHERE` → `OWN001`; an owner used in `TYPE_OWNERS` but missing from `OWNER_MODULES` → `OWN002`.<br>2. For each owner, the module form is `<root>/herness/core/types/<submodule>.py` and the package form is the directory `<root>/herness/core/types/<submodule>/` with an `__init__.py`. Both present → `OWN003 two forms`. Neither present → write `INFO pending owner <owner>` and skip step 3 for it. The owner's files are the module, or every `*.py` file of the package (recursively).<br>3. Parse each owner file with `ast`. Collect top-level names defined by `class`, `def`, `Assign`, `AnnAssign` and `TypeAlias` statements in all owner files, excluding a package's `__init__.py` and excluding the name `__all__`. (a) Each name that `TYPE_OWNERS` gives this owner must be defined → else `OWN010 missing`. (b) Each defined public name (no leading `_`) must be in `TYPE_OWNERS` with this owner → else `OWN011 unregistered` or `OWN012 wrong owner`. (c) Each `Import` and `ImportFrom` must satisfy §3.5 import rules 1–3 (relative imports resolved against the file's own package) → else `OWN020 forbidden import <module>`. (d) A name of `DECLARED_ELSEWHERE` defined here → `OWN043`. (e) Package form only: the package's `__init__.py` may contain only a docstring, imports from the package's own modules and an assignment to `__all__`; anything else → `OWN033`; the names it imports must equal the owner's `TYPE_OWNERS` names → else `OWN031`.<br>4. Parse `__init__.py`: any `class`, `def`, or assignment other than `__all__` → `OWN030`; for each existing owner submodule, the set of names imported from it must equal that owner's `TYPE_OWNERS` names → else `OWN031`; `__all__` must be a literal tuple equal to the sorted union of imported names → else `OWN032`.<br>5. Walk every `*.py` under `<root>/herness` and `<root>/app`, excluding `herness/core/types/`. For each file: (a) a `class` or `def` at any nesting level, or an assignment target at module or class level, whose name is in `TYPE_OWNERS` → `OWN040 redefined outside core.types`; (b) an import of `herness.core.types.<submodule>` or `herness.core.types._ownership` → `OWN041 import via submodule`; (c) a name of `DECLARED_ELSEWHERE` defined (class, def or assignment target) in a module that is neither its declared module nor inside its declared package → `OWN042`.<br>6. Settings modules (R-03): for every file named `settings.py` under `<root>/herness`, each `Import` and `ImportFrom` (relative imports resolved) must name a standard-library module (per `sys.stdlib_module_names`), `pydantic`, `pydantic_core`, `typing_extensions` or `annotated_types` (the modules pydantic's own constraint types come from), or exactly `herness.core.types` or `herness.core.errors` → else `OWN050 forbidden import in settings module <module>`. Import-linter contract C6 (U00-52) enforces the Herness part of the same rule; this step adds the third-party part, which import-linter does not check.<br>7. Sort violations by path and line; write them; return 1 if any, else 0. Output uses `sys.stdout.write` (no `print`, ruff `T20`). |
| Side effects | Reads files; writes to stdout. |
| Errors | Bad arguments → exit 2 with usage on stderr. A file that fails to parse → violation `OWN090 syntax error` (not an exception). |
| Concurrency | Single-threaded CLI. |
| Complexity and limits | O(total size of scanned files); < 3 s on the full repository. |
| Security notes | TH00-09; TH00-08 (settings modules cannot pull in higher layers or I/O libraries). |
| Tests | UT00-49, UT00-50, UT00-51, UT00-52, UT00-53, UT00-54, UT00-72, UT00-73, UT00-81, ST00-15, IT00-02 |

### 3.6 Package root and project configuration

#### U00-48 herness.__version__

| Field | Content |
|-------|---------|
| Kind | constant |
| Purpose | Installed package version for `herness --version`, run metadata and eval `summary.json`. |
| Signature | `__version__: Final[str]` |
| Preconditions | — |
| Postconditions | Equals `importlib.metadata.version("herness")`, or `"0.0.0+unknown"` when the distribution is not installed. |
| Algorithm | 1. At import, call `importlib.metadata.version("herness")`; on `importlib.metadata.PackageNotFoundError` use `"0.0.0+unknown"`. |
| Side effects | Reads installed metadata once. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | None. |
| Tests | UT00-55 |

Static configuration units U00-49 … U00-54 and U00-59 … U00-63 have no run-time preconditions, postconditions, side effects, errors or concurrency; those rows are omitted and their content is the "Algorithm" (the required contents).

#### U00-49 `pyproject.toml`: project metadata, dependencies, uv and build settings

| Field | Content |
|-------|---------|
| Kind | configuration file |
| Purpose | Declare the package, its dependencies with the design 00 §9 minimum versions, dev tooling (ENG §14 E3), the lock and index policy, and the build backend. |
| Algorithm | **`[build-system]`**: `requires = ["hatchling>=1.25,<2"]`; `build-backend = "hatchling.build"`.<br>**`[project]`**: `name = "herness"`; `version = "0.1.0"`; `description = "Local-first IT operations analytics and agent harness"`; `readme = "README.md"`; `requires-python = ">=3.12,<3.13"`; `license = "LicenseRef-Proprietary"` (open item O-04); `classifiers` = `"Private :: Do Not Upload"`, `"Programming Language :: Python :: 3.12"`, `"Operating System :: Microsoft :: Windows"`, `"Operating System :: POSIX :: Linux"`; `dependencies` = the rows of table 14.1 with scope `runtime`, each with exactly the lower bound shown.<br>**`[project.optional-dependencies]`**: `ner = ["presidio-analyzer", "spacy"]`; `pdf = ["weasyprint"]`.<br>**`[project.scripts]`**: `herness = "herness.cli:main"` (T09-20 (herness.cli.main)).<br>**`[dependency-groups]`**: `dev` = the rows of table 14.1 with scope `dev`.<br>**`[tool.uv]`**: `required-version = "==<the uv version that generated uv.lock in T00-01>"`; `default-groups = ["dev"]`.<br>**`[[tool.uv.index]]`**: `name = "pytorch-cu128"`; `url = "https://download.pytorch.org/whl/cu128"`; `explicit = true` (open item O-05).<br>**`[tool.uv.sources]`**: `torch = [{ index = "pytorch-cu128", marker = "sys_platform == 'win32' or sys_platform == 'linux'" }]`.<br>**`[tool.hatch.build.targets.wheel]`**: `packages = ["herness"]` (package data such as `herness/model/sql/*.sql` and `herness/reports/templates/**` is included because hatchling ships every file under the package).<br>**`[tool.hatch.build.targets.sdist]`**: `include = ["herness", "README.md", "pyproject.toml", "uv.lock"]`.<br>**Install modes (R-58)**: Development: `uv sync --frozen` installs `herness` into `.venv` as an editable install (uv's default for a project with a `[build-system]`); no development command in this spec or the README passes `--no-editable`. Target box: never installed from a checkout; the release wheel built by U00-60 is installed by `herness deploy install` (T10-26 (herness deploy install)) after it verifies the wheel's provenance attestation and SBOM. |
| Complexity and limits | Whole `pyproject.toml` ≤ 400 lines, including U00-50 … U00-53 and the `[tool.herness.*]` tables of U00-55 and U00-58. |
| Security notes | TH00-11: installs come only from `uv.lock` (`uv sync --frozen`, `UV_FROZEN=1` in CI), which records SHA-256 hashes; the torch index is `explicit`, so no other package resolves from it. TH00-13: the target box runs only attested release wheels (R-58). |
| Tests | UT00-56 |

#### U00-50 `pyproject.toml`: ruff configuration

| Field | Content |
|-------|---------|
| Kind | configuration file section |
| Purpose | ENG §3.1 lint and format rules, plus bans that enforce ENG §3.5, the egress rule of ENG §2.1 and R-06, and the `herness.core.time` import convention. |
| Algorithm | **`[tool.ruff]`**: `line-length = 100`; `target-version = "py312"`; `src = ["herness", "app", "tools", "tests"]`; `extend-exclude = ["data", "docs"]`.<br>**`[tool.ruff.lint]`**: `select` = `E`, `W`, `F`, `I`, `N`, `UP`, `B`, `A`, `C4`, `C90`, `SIM`, `PT`, `PL`, `RUF`, `S`, `DTZ`, `TRY`, `ASYNC`, `PERF`, `ANN`, `BLE`, `EM`, `G`, `LOG`, `T20`, `ERA` (ENG §3.1) plus `TID251` and `ICN003` (needed for the bans below; stricter than ENG, §13.3). `ignore = ["TRY003"]` (taxonomy errors carry operation-specific messages by design, ENG §3.4; §13.3).<br>**`[tool.ruff.lint.per-file-ignores]`**: `"tests/**" = ["S101", "PLR2004", "S311", "ANN"]`; `"herness/core/errors.py" = ["N818"]` (class names fixed by design 00 §7); `"herness/core/logging.py" = ["A005"]`; `"herness/core/time.py" = ["A005"]`; `"herness/core/numbers.py" = ["A005"]`; `"herness/core/types/__init__.py" = ["A005"]`; `"herness/core/egress.py" = ["TID251"]` (the only module allowed to build `httpx` clients and transports, R-06; pickle there is still caught by `S301`). Connectors get no exception: they obtain `httpx` clients from `herness.core.egress`, and vendor SDKs (Snowflake, `pymongo`, `msal`) build their own clients only for hosts in `sources.<name>.hosts`, which the impl 10 socket guard enforces at run time (R-06).<br>**`[tool.ruff.lint.mccabe]`**: `max-complexity = 10`.<br>**`[tool.ruff.lint.pylint]`**: `max-args = 6` (open item O-06).<br>**`[tool.ruff.lint.isort]`**: `known-first-party = ["herness", "app", "tools"]`.<br>**`[tool.ruff.lint.flake8-tidy-imports.banned-api]`** (target → message): `pickle`, `marshal`, `shelve` → "ENG §3.5: no pickle, marshal or shelve"; `yaml.load`, `yaml.unsafe_load`, `yaml.full_load` → "use yaml.safe_load (ENG §3.5)"; `requests`, `urllib.request`, `httpx.Client`, `httpx.AsyncClient`, `httpx.HTTPTransport`, `httpx.AsyncHTTPTransport`, and the client-creating shortcuts `httpx.request`, `httpx.stream`, `httpx.get`, `httpx.post`, `httpx.put`, `httpx.patch`, `httpx.delete`, `httpx.head`, `httpx.options` → "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"; `datetime.datetime.utcnow`, `datetime.datetime.utcfromtimestamp` → "naive datetime; use herness.core.time".<br>**`[tool.ruff.lint.flake8-import-conventions]`**: `banned-from = ["herness.core.time"]`.<br>**`[tool.ruff.format]`**: `quote-style = "double"`; `line-ending = "lf"`. |
| Security notes | TH00-08. |
| Tests | UT00-57, ST00-09 |

#### U00-51 `pyproject.toml`: mypy configuration

| Field | Content |
|-------|---------|
| Kind | configuration file section |
| Purpose | `mypy --strict` over all of `herness/` (ENG §3.1, E1) and over `app/` and `tools/` minus `disallow_untyped_decorators`. |
| Algorithm | **`[tool.mypy]`**: `python_version = "3.12"`; `strict = true`; `files = ["herness", "app", "tools"]`; `plugins = ["pydantic.mypy"]`; `warn_unreachable = true`; `enable_error_code = ["ignore-without-code", "redundant-expr", "truthy-bool"]`.<br>**`[[tool.mypy.overrides]]`**: `module = ["app.*", "tools.*"]`; `disallow_untyped_decorators = false`.<br>**`[[tool.mypy.overrides]]`**: `ignore_missing_imports = true` for exactly the third-party modules for which mypy reports `import-untyped` or `import-not-found`. The list starts empty; the card that first imports such a module adds it.<br>**`[tool.pydantic-mypy]`**: `init_forbid_extra = true`; `init_typed = true`; `warn_required_dynamic_aliases = true`.<br>`mypy` runs without path arguments (it reads `files`) in pre-commit and CI. |
| Tests | UT00-57 |

#### U00-52 `pyproject.toml`: import-linter contracts

`[tool.importlinter]`: `root_packages = ["herness", "app", "tools"]`; `include_external_packages = false`. Rule for all contracts: a module or package is listed in a contract if and only if it exists in the repository; the card that creates a listed module adds it to every contract below that names it (UT00-58 fails when an existing package or core module is missing from a contract). Target state:

| ID | Name | Type | Definition |
|----|------|------|------------|
| C1 | `herness layers` | `layers` | Layers high to low: `herness.cli`; `herness.eval` \| `herness.admin` (pipe = independent siblings; `herness.admin` is the L5 package of R-07); `herness.reports`; `herness.harness`; `herness.enrich`; `herness.metrics`; `herness.connectors` \| `herness.model`; `herness.store`; `herness.core`. `ignore_imports` holds exactly one entry, the named **settings exception** (ENG §2.1, R-03), written with the TOML comment `# settings exception (ENG §2.1, R-03)` on the line above it: `herness.core.config -> herness.**.settings`. Result: ENG §2.1 upward imports forbidden; `herness.core` never imports `herness.store`, so L0 reaches the ops store only through ports bound by the composition root (R-04); `herness.enrich` never imports `herness.harness` (R-05); `metrics` cannot import `enrich`; `reports` cannot import `eval` or `admin`; `eval` and `admin` are independent; `connectors` and `model` are independent. |
| C2 | `herness never imports app or tools` | `forbidden` | `source_modules = ["herness"]`; `forbidden_modules = ["app", "tools"]`. |
| C3 | `core base order` | `layers` | Layers high to low: `herness.core.logging`; `herness.core._log_pipeline` \| `herness.core.types`; `herness.core.ids`; `herness.core.time` \| `herness.core.numbers`; `herness.core.errors`. Result: `herness.core.numbers` (R-16) imports only `herness.core.errors` from the core base; it reads `NumberRef` values structurally through its own `FormattableNumber` protocol (U00-65), not by importing `herness.core.types`. |
| C4 | `core base is closed` | `forbidden` | `source_modules` = the seven modules of C3; `forbidden_modules` = every other existing module of `herness.core` (`config`, `registry`, `secrets`, `redact`, `egress`, `audit`, `resilience`, `jobs`, and any `settings`) and every existing top-level package of C1 other than `herness.core`. Indirect imports are checked (default). |
| C5 | `types import only errors and ids` | `forbidden` | `source_modules = ["herness.core.types"]`; `forbidden_modules = ["herness.core.time", "herness.core.logging", "herness.core._log_pipeline"]`; `allow_indirect_imports = true` (types reach `time` only indirectly through `ids`; ENG §2.1 restricts direct imports). |
| C6 | `settings modules are leaves` | `forbidden` | `source_modules` = every existing `herness/**/settings.py` module; `forbidden_modules` = every existing herness package or module except `herness.core.errors`, `herness.core.types` and the settings module itself (R-03); `allow_indirect_imports = true` (a settings module reaches `herness.core.ids` and `herness.core.time` only through `herness.core.types`; R-03 restricts direct imports). The third-party part of R-03 (standard library and pydantic only) is checked by U00-47 step 6. Added by the first card (in any spec) that creates a settings module; each later settings card appends its module. |

| Field | Content |
|-------|---------|
| Kind | configuration file section |
| Purpose | Enforce ENG §2.1 layering (including R-03, R-04, R-05 and R-07) and the core-base rules mechanically. |
| Security notes | TH00-08. |
| Tests | UT00-58, ST00-10 |

#### U00-53 `pyproject.toml`: pytest and coverage configuration

| Field | Content |
|-------|---------|
| Kind | configuration file section |
| Purpose | Markers and defaults of spec 11 §4.1, and coverage measurement for spec 11 §4.3. |
| Algorithm | **`[tool.pytest.ini_options]`**: `minversion = "8.0"`; `testpaths = ["tests"]`; `addopts = "--strict-markers --strict-config -ra"`; `markers` = `unit: no I/O beyond tmp files, no subprocess, no network`, `integration: real DuckDB, SQLite and LanceDB on tiny or small builds, fakes and stubs`, `fault: HERNESS_ENV=test, fault plans, subprocess kills`, `eval: golden or classifier evaluation`, `gpu: needs vLLM, OpenJev or Laya on CUDA`, `slow: over 30 s per test, or small or full scale`; `asyncio_mode = "strict"`; `timeout = 300`; `filterwarnings = ["error:::herness"]` (warnings raised from Herness modules fail tests).<br>**`[tool.coverage.run]`**: `branch = true`; `source = ["herness"]`.<br>**`[tool.coverage.report]`**: `exclude_also = ["if TYPE_CHECKING:", "\\.\\.\\.$", "# pragma: gpu"]`; `show_missing = true`.<br>**`[tool.coverage.json]`**: `output = "coverage.json"`. **`[tool.coverage.xml]`**: `output = "coverage.xml"`.<br>The one-marker-per-file collection rule and the Hypothesis profiles `commit` and `nightly` live in `tests/conftest.py` (T11-01 (tests/conftest.py)). Foundation test files set `pytestmark` at module level. |
| Tests | UT00-57 |

#### U00-54 `.pre-commit-config.yaml` and `.secrets.baseline`

Top level: `minimum_pre_commit_version: "3.2.0"`; `default_install_hook_types: [pre-commit, pre-push]`; `default_stages: [pre-commit]`; `fail_fast: false`. Local hooks use `language: system` and run through `uv run --frozen` so tool versions come from `uv.lock`.

| # | Hook id | Source | Command | Files | pass_filenames | Stage |
|---|---------|--------|---------|-------|----------------|-------|
| 1 | `check-merge-conflict`, `check-toml`, `check-yaml`, `end-of-file-fixer`, `trailing-whitespace` (`--markdown-linebreak-ext=md`), `mixed-line-ending` (`--fix=lf`), `detect-private-key`, `check-added-large-files` (`--maxkb=1024`, exclude `^tests/fixtures/`) | `https://github.com/pre-commit/pre-commit-hooks`, `rev` = full 40-character commit SHA of its latest release tag at T00-12, with the tag in a trailing comment | — | all | default | pre-commit |
| 2 | `ruff-check` | local | `uv run --frozen ruff check --fix --exit-non-zero-on-fix` | `\.py$` | true | pre-commit |
| 3 | `ruff-format` | local | `uv run --frozen ruff format` | `\.py$` | true | pre-commit |
| 4 | `mypy` | local | `uv run --frozen mypy` | `\.py$` | false | pre-commit |
| 5 | `import-linter` | local | `uv run --frozen lint-imports` | `(\.py\|pyproject\.toml)$` | false | pre-commit |
| 6 | `detect-secrets` | local | `uv run --frozen detect-secrets-hook --baseline .secrets.baseline` | all; exclude `^(uv\.lock\|\.secrets\.baseline\|tests/fixtures/pii_corpus\.jsonl)$` | true | pre-commit |
| 7 | `fixtures-pii-scan` | local | `uv run --frozen python -m herness.core.redact --scan tests/fixtures` (T10-11 (herness.core.redact_scan.main)) | `^tests/fixtures/` | false | pre-commit |
| 8 | `module-size` | local | `uv run --frozen python -m tools.check_module_size` | `\.py$` | false | pre-commit |
| 9 | `type-ownership` | local | `uv run --frozen python -m tools.check_type_ownership` | `^(herness\|app)/.*\.py$` | false | pre-commit |
| 10 | `pytest-unit` | local | `uv run --frozen pytest -m unit -x -q` | — (`always_run: true`) | false | pre-commit |
| 11 | `pytest-cpu` | local | `uv run --frozen pytest -m "(unit or integration or fault) and not gpu and not slow" --cov` | — (`always_run: true`) | false | pre-push |

`.secrets.baseline` is generated with `uv run detect-secrets scan --exclude-files '^(uv\.lock|tests/fixtures/pii_corpus\.jsonl)$'`, written to `.secrets.baseline` and committed; every entry is audited with `detect-secrets audit` and marked a false positive before commit. `uv.lock` is excluded because its SHA-256 hashes look like high-entropy strings; the PII corpus is synthetic and is scanned by hook 7 instead.

| Field | Content |
|-------|---------|
| Kind | configuration file |
| Purpose | ENG §3.1 pre-commit list and ENG §7 commit and push gates, within spec 11 §4.2's pre-commit budget (< 90 s with a warm mypy cache). |
| Security notes | TH00-10 (secret scan), TH00-11 (remote hook pinned by SHA). Hook 7 only runs when files under `tests/fixtures/` change, which happens after impl 10 has delivered the scanner. |
| Tests | IT00-01, ST00-12 |

### 3.7 CI check scripts

All scripts: run as `python -m tools.<name>` from the repository root; `main(argv: Sequence[str] | None = None) -> int`; the module ends with `raise SystemExit(main())` under `if __name__ == "__main__":`. Output goes through `sys.stdout.write` and `sys.stderr.write` (no `print`). Exit codes (R-73; the `herness` CLI codes of R-46 do not apply to `tools/`): 0 pass, 1 violations (findings), 2 usage or input error. Violation lines have the form `<path>:<line>: <CODE> <message>` (line `0` when not applicable), sorted by path, line, code. Arguments are parsed with `argparse`; `--root PATH` (default `.`) is accepted by every script.

#### U00-55 tools.check_module_size.main

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `argv` | `Sequence[str] \| None` | `None` | positional | options: `--root PATH` |

Returns: `int`.

| Field | Content |
|-------|---------|
| Kind | function (CLI entry) |
| Purpose | Enforce ENG §2.4 module length limits and the per-file line budgets that implementation specs declare in their module maps. |
| Preconditions | `<root>/pyproject.toml` has `[tool.herness.module_budgets]`. |
| Postconditions | Every Python file under `herness/`, `app/`, `tools/` is within its budget, or a violation is reported. |
| Algorithm | 1. Read `[tool.herness.module_budgets]` with `tomllib`: `default` (int, required) and `overrides` (table: repository-relative POSIX path → inline table `{limit: int, reason: str}`). Missing `default` or a non-int value → exit 2. An override with `limit > default` and an empty `reason` → `MS003`.<br>2. Doc budgets: for each `<root>/docs/impl/*.impl.md`, find Markdown tables whose header row has first cell `Path` and last cell `Line budget`. For each body row: if the first cell contains exactly one backticked token ending in `.py` without `{`, `*` or spaces, and the last cell is a plain integer, record `(path, budget, doc)`. The same path with two different integers → `MS002 conflicting budgets`.<br>3. Files: every `*.py` under `<root>/herness`, `<root>/app`, `<root>/tools`, sorted.<br>4. Budget of a file = `overrides[path].limit` if present, else `default`; then the minimum of that and the doc budget if one exists.<br>5. Read the file as UTF-8 (`UnicodeDecodeError` → `MS004`); `lines = len(text.splitlines())`.<br>6. If `lines > budget` → `MS001 <lines> lines > budget <budget> (<source: default, override or doc name>)`.<br>7. Exit 1 if any violation, else 0. |
| Side effects | Reads files; writes to stdout. |
| Errors | Invalid config → exit 2 with a message on stderr. |
| Concurrency | Single-threaded CLI. |
| Complexity and limits | O(total lines); < 2 s on the full repository. |
| Security notes | None. |
| Tests | UT00-59, UT00-60, IT00-02 |

Initial `[tool.herness.module_budgets]` (in `pyproject.toml`): `default = 400`; `overrides = { "herness/harness/loop.py" = { limit = 220, reason = "design 05 §5.2" } }`.

#### U00-56 tools.check_traceability.main

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `argv` | `Sequence[str] \| None` | `None` | positional | options: `--root PATH`; `--require-implemented NN[,NN...]` (two-digit spec numbers) |

Returns: `int`.

| Field | Content |
|-------|---------|
| Kind | function (CLI entry) |
| Purpose | ENG §7 traceability gate: every task, unit, flow, threat and test ID in the implementation specs resolves, and test IDs in code match the specs. |
| Preconditions | `<root>/docs/impl/` exists. |
| Postconditions | All violations reported. |
| Algorithm | Definitions:<br>• ID pattern `(?<![A-Za-z0-9_])(TH\|UT\|PT\|IT\|FT\|ST\|BT\|ET\|T\|U\|F)(\d{2})-(\d{2,3})(?![0-9])`; test kinds are `UT PT IT FT ST BT ET`.<br>• Docs are `<root>/docs/impl/*.impl.md` (so `ENG-STANDARDS.md` is not scanned). A doc's spec number is the first two characters of its file name. Lines inside fenced code blocks are skipped.<br>• An ID is **defined** where it is the first token of a heading's text (a line starting with 1–6 `#` and a space) or where it is the whole first cell of a table row (after trimming spaces and backticks; separator rows skipped). Every other occurrence is a **reference**.<br>• A task-card section runs from the heading that defines a `T` ID to the next heading of the same or higher level. Its `Tests` row is the table row whose first cell is `Tests`.<br>Steps:<br>1. Scan all docs; collect definitions (ID → list of `(doc, line)`) and references (ID → list of `(doc, line)`).<br>2. `TR001 undefined` for each reference whose ID has no definition in any doc.<br>3. `TR002 duplicate` for each ID defined more than once.<br>4. `TR003 wrong spec` for each definition whose spec digits differ from its doc's spec number.<br>5. `TR004 threat without security test` for each table row that defines a `TH` ID and contains no `ST` ID.<br>6. `TR005 test not on a card` for each defined test ID that appears in no task card's `Tests` row.<br>7. `TR008 unresolved cross-spec reference` for each occurrence of `X:\d{2}/\S+` in a doc (the consistency pass replaces these before implementation starts).<br>8. Code scan: parse every `<root>/tests/**/*.py` with `ast`. For each `FunctionDef` or `AsyncFunctionDef` whose name starts with `test_`, collect IDs from the name using `(?<![a-z0-9])(ut\|pt\|it\|ft\|st\|bt\|et)(\d{2})_(\d{2,3})(?![0-9])` (upper-cased, `_` → `-`) and from its docstring using the ID pattern restricted to test kinds.<br>9. `TR006 unknown test id` for a code ID not defined in any doc; `TR007 test id used twice` for an ID found on two functions; `TR010 several ids on one test` for a function with more than one distinct ID.<br>10. With `--require-implemented`: `TR009 not implemented` for each defined test ID of the listed specs that no test function carries.<br>11. Write violations; also write one summary line `defined=<n> referenced=<n> implemented=<n>`; exit 1 if any violation, else 0. |
| Side effects | Reads files; writes to stdout. |
| Errors | Missing `docs/impl` → exit 2. A test file that fails to parse → `TR090 syntax error`. |
| Concurrency | Single-threaded CLI. |
| Complexity and limits | O(total size of docs and tests); BT00-04 < 5 s. |
| Security notes | ENG §5.1 step 3 (every threat has a proving test) is enforced by TR004 and TR005. |
| Tests | UT00-61, UT00-62, UT00-63, UT00-64, UT00-65, UT00-66, UT00-67, UT00-68, IT00-02, BT00-04 |

Convention for this spec's tests: the first line of each test function's docstring starts with its ID, for example `UT00-14: format_utc pads year and microseconds`.

#### U00-57 tools.check_audit.main

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `argv` | `Sequence[str] \| None` | `None` | positional | options: `--pip-audit PATH` (required), `--osv PATH` (required), `--ignore PATH` (default `tools/audit_ignore.toml`), `--summary PATH` (optional), `--today YYYY-MM-DD` (optional; default `clock.utc_day(clock.now())`) |

Returns: `int`.

| Field | Content |
|-------|---------|
| Kind | function (CLI entry) |
| Purpose | ENG §7 audit gate: fail on a High or Critical vulnerability that has a fix available, merging pip-audit (fix versions) and osv-scanner (severity) results. |
| Preconditions | Both JSON files exist. |
| Postconditions | Decision per finding reported; exit 1 on any blocking finding. |
| Algorithm | 1. Parse pip-audit JSON: `dependencies[]` with `name`, `version`, `vulns[]` (`id`, `fix_versions[]`, `aliases[]`); entries with `skip_reason` are reported as `AU010 skipped <name>` warnings.<br>2. Parse osv-scanner JSON: `results[].packages[]` with `package.name`, `package.version`, `vulnerabilities[]` (`id`, `aliases[]`, `affected[].ranges[].events[]`) and `groups[]` (`ids[]`, `max_severity`).<br>3. Normalise package names per PEP 503 (lower-case; runs of `-`, `_`, `.` → `-`).<br>4. Merge: within the same package, a pip-audit vulnerability and an osv vulnerability are one finding when their ID sets (`id` ∪ `aliases`) intersect. Finding fields: `ids` (union), `package`, `version`, `fix_available` (pip-audit `fix_versions` non-empty, or any osv range event has a `fixed` key), `severity` (maximum of `float(max_severity)` over osv groups sharing an ID with the finding; `None` when absent or not a number).<br>5. Load the ignore file with `tomllib`: `ignore = [{id, package, reason, expires}]`, all four required, `expires` a `YYYY-MM-DD` date; any other shape → exit 2. An entry matches a finding when its `id` is in the finding's `ids` and its normalised `package` equals the finding's.<br>6. Decide per finding: matched by an entry with `expires >= today` → `ignored`; matched only by expired entries → `AU003 expired ignore` (blocking); `severity is None` → treat as 7.0; `severity >= 7.0` and `fix_available` → `AU001 blocking` ; otherwise `AU002 warning`.<br>7. Ignore entries that matched nothing → `AU011 unused ignore` warning.<br>8. If `--summary`: write a Markdown table `package \| version \| ids \| severity \| fix \| decision`.<br>9. Exit 1 if any blocking decision, else 0. |
| Side effects | Reads inputs; writes stdout and the summary file. |
| Errors | Unreadable or malformed JSON or TOML → exit 2. |
| Concurrency | Single-threaded CLI. |
| Complexity and limits | O(findings). |
| Security notes | TH00-11. Unknown severity with a fix available blocks (fail closed). |
| Tests | UT00-69, ST00-07 |

`tools/audit_ignore.toml` initial content: `ignore = []`.

#### U00-58 tools.check_licences.main

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `argv` | `Sequence[str] \| None` | `None` | positional | options: `--sbom PATH` (required), `--pyproject PATH` (default `pyproject.toml`), `--mode enforce\|report` (default `enforce`) |

Returns: `int`.

| Field | Content |
|-------|---------|
| Kind | function (CLI entry) |
| Purpose | ENG §5.6 licence gate over the CycloneDX SBOM of the runtime environment. |
| Preconditions | SBOM is CycloneDX JSON 1.4 or later. |
| Postconditions | Each component classified `allowed`, `approved`, `report-only` or `denied`. |
| Algorithm | 1. Read `[tool.herness.licences]` from the pyproject: `allowed` (list of SPDX IDs), `aliases` (table: free-text licence name → SPDX ID, matched case-insensitively), `approved` (array of tables `{package, licence, approved_by, approved_on, reason}`, `package` is an `fnmatch` glob on the normalised name), `report_only` (list of globs).<br>2. Read `components[]` from the SBOM; skip the component equal to `metadata.component` (Herness itself).<br>3. For each component, collect licence texts from `licenses[]`: `license.id`, `license.name` mapped through `aliases`, or `expression`.<br>4. Evaluate one text: strip outer parentheses; split on top-level ` OR ` into alternatives; an alternative is allowed when every part split on ` AND ` is in `allowed` after removing any ` WITH <exception>` suffix. The text is allowed when any alternative is allowed.<br>5. Multiple `licenses[]` entries are alternatives (package metadata lists dual licences as separate entries): the component is `allowed` when any entry is allowed.<br>6. Otherwise: an `approved` entry whose glob matches the name and whose `licence` equals one of the component's licence texts → `approved`; else a `report_only` glob match → `report-only` (warning `LC002`); else `denied` (`LC001`). No licence data at all counts as the text `UNKNOWN`.<br>7. In `report` mode `LC001` is written as a warning and does not change the exit code.<br>8. Exit 1 if any `LC001` in `enforce` mode, else 0. |
| Side effects | Reads files; writes stdout. |
| Errors | Malformed SBOM or pyproject table → exit 2. |
| Concurrency | Single-threaded CLI. |
| Complexity and limits | O(components). |
| Security notes | ENG §5.6 licence allowlist; supply-chain hygiene under TH00-11. |
| Tests | UT00-70, ST00-16 |

Initial `[tool.herness.licences]` (in `pyproject.toml`): `allowed = ["MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "PSF-2.0", "Python-2.0", "ISC", "MPL-2.0"]`; `aliases` = `"MIT License"` → `MIT`, `"BSD License"` → `BSD-3-Clause`, `"BSD"` → `BSD-3-Clause`, `"Apache Software License"` → `Apache-2.0`, `"Apache 2.0"` → `Apache-2.0`, `"Apache License 2.0"` → `Apache-2.0`, `"Apache License, Version 2.0"` → `Apache-2.0`, `"Python Software Foundation License"` → `PSF-2.0`, `"ISC License (ISCL)"` → `ISC`, `"Mozilla Public License 2.0 (MPL 2.0)"` → `MPL-2.0`; `approved = []`; `report_only = ["nvidia-*"]` (open item O-03).

### 3.8 Hosted CI and release workflows

Rules for both workflow files:

| Rule | Value |
|------|-------|
| Action pinning | Every `uses:` of a third-party action is pinned to a full 40-character commit SHA of that action's latest release tag at the time the card is done, with the tag in a trailing comment. Actions used: `actions/checkout`, `astral-sh/setup-uv`, `actions/upload-artifact`, `actions/download-artifact`, `actions/attest-build-provenance`, `actions/attest-sbom`. |
| Default permissions | Top-level `permissions: contents: read`; jobs widen only as listed. |
| Checkout | `persist-credentials: false`. |
| Untrusted input | No `pull_request_target` trigger. No `${{ github.event.* }}`, `${{ github.head_ref }}` or `${{ inputs.* }}` inside a `run:` script; such values are passed through `env:` and referenced as shell variables. |
| Runners | GitHub-hosted only (`ubuntu-latest`, `windows-latest`); never `self-hosted` (SLSA Build L2 needs a hosted build platform). |
| Python and uv | `astral-sh/setup-uv` with `enable-cache: true`, `cache-dependency-glob: uv.lock`, `python-version: "3.12"`, `version` = the `required-version` of U00-49. |
| Environment | `UV_FROZEN: "1"`, `PYTHONUTF8: "1"`, `HERNESS_ENV: test`. No repository secrets are used; the only credential is the automatic `GITHUB_TOKEN` (and the OIDC token for attestations). |

#### U00-59 `.github/workflows/ci.yml`

Triggers: `push` to `main`; `pull_request` to `main`; `schedule` cron `"17 3 * * *"` (nightly audit); `workflow_dispatch`; `workflow_call` (used by the release workflow). `concurrency`: group `ci-${{ github.ref }}`, `cancel-in-progress: true` except for `schedule`.

| Job | Runs when | Runner, timeout | Steps (in order) | Blocks merge |
|-----|-----------|-----------------|------------------|--------------|
| `lint` | not `schedule` | `ubuntu-latest`, 15 min | checkout; setup-uv; `uv sync --all-extras`; `uv run ruff check --output-format=github .`; `uv run ruff format --check .`; `uv run lint-imports`; `git ls-files -z` piped to `uv run detect-secrets-hook --baseline .secrets.baseline` (excluding the U00-54 hook 6 patterns); `uv run python -m tools.check_module_size`; `uv run python -m tools.check_type_ownership`; `uv run python -m tools.check_traceability` | yes |
| `types` | not `schedule` | `ubuntu-latest`, 20 min | checkout; setup-uv; `uv sync --all-extras`; `uv run mypy` | yes |
| `test` | not `schedule` | matrix: `ubuntu-latest` (selection `(unit or integration or fault) and not gpu and not slow`, with coverage) and `windows-latest` (selection `unit and not gpu`); 30 min | checkout; setup-uv; `uv sync --all-extras`; `uv run pytest -m "<selection>" -p no:cacheprovider` plus, on Ubuntu, `--cov --cov-report=xml --cov-report=json`; on Ubuntu when `hashFiles('tests/eval/mock_scripts/**') != ''`: `uv run herness eval --mock-llm tests/eval/mock_scripts` (T11-30 (herness.eval.runner.run_golden, behind `herness eval --mock-llm`)); upload `coverage.xml`, `coverage.json` and `data/reports/eval/**/report.md` as artifact `ci-reports` (retention 14 days, `if: always()`) | yes |
| `audit` | always (including `schedule`) | `ubuntu-latest`, 20 min | checkout; setup-uv; `uv sync --all-extras`; `uv export --all-extras --no-emit-project --format requirements-txt -o build/requirements.lock.txt`; `uv run pip-audit -r build/requirements.lock.txt --disable-pip --require-hashes --format json -o build/pip-audit.json` (exit codes 0 and 1 accepted; the gate decides); download the osv-scanner release binary of version `OSV_SCANNER_VERSION` and verify its SHA-256 against `OSV_SCANNER_SHA256` (both workflow `env` values recorded at T00-14 from the release's published checksums; mismatch fails the job); run osv-scanner on `build/requirements.lock.txt` as a requirements lockfile with JSON output to `build/osv.json` (exit codes 0 and 1 accepted; open item O-02); `uv run python -m tools.check_audit --pip-audit build/pip-audit.json --osv build/osv.json --summary build/audit-summary.md`; append the summary to `$GITHUB_STEP_SUMMARY`; `UV_PROJECT_ENVIRONMENT=build/runtime-venv uv sync --no-dev --all-extras --no-install-project`; `uv run cyclonedx-py environment build/runtime-venv --output-reproducible --of JSON -o build/sbom.cdx.json`; `uv run python -m tools.check_licences --sbom build/sbom.cdx.json`; upload `build/*.json` and `build/audit-summary.md` as artifact `audit` (retention 30 days, `if: always()`) | yes |

| Field | Content |
|-------|---------|
| Kind | configuration file (GitHub Actions workflow) |
| Purpose | ENG §7 merge gates on a hosted runner (ENG §14 E2) plus the nightly dependency audit. |
| Security notes | TH00-11, TH00-12. Required status checks on `main` (branch protection, set in repository settings per README): `lint`, `types`, `test (ubuntu-latest)`, `test (windows-latest)`, `audit`. |
| Tests | ST00-06 |

#### U00-60 `.github/workflows/release.yml`

Trigger: `push` of tags matching `v*.*.*`. Jobs:

| Job | Needs | Permissions | Steps |
|-----|-------|-------------|-------|
| `ci` | — | `contents: read` | `uses: ./.github/workflows/ci.yml` (reusable; all gates must pass on the tagged commit) |
| `build` | `ci` | `contents: read`, `id-token: write`, `attestations: write` | checkout (`fetch-depth: 0`); setup-uv; verify the tag: with `env: TAG: ${{ github.ref_name }}` and `GH_TOKEN: ${{ github.token }}`, call `gh api repos/$GITHUB_REPOSITORY/git/ref/tags/$TAG`; the object type must be `tag` (annotated), and `gh api repos/$GITHUB_REPOSITORY/git/tags/<object sha>` must report `verification.verified == true`, else fail; check `$TAG` equals `v` + `project.version` read from `pyproject.toml` with `tomllib`, else fail; `uv build` (writes `dist/herness-<version>-py3-none-any.whl` and `dist/herness-<version>.tar.gz`); create the runtime environment and SBOM exactly as the `audit` job, writing `dist/herness-<version>.cdx.json`; `uv run python -m tools.check_licences --sbom dist/herness-<version>.cdx.json` (enforce mode); download the DuckDB `excel` extension for the DuckDB version pinned in `uv.lock` and platform `linux_amd64` from the official extension repository into `dist/duckdb/excel.duckdb_extension` and write its SHA-256 to `dist/duckdb/SHA256SUMS` (impl 10 D10-28); `actions/attest-build-provenance` with `subject-path` = the wheel, the sdist and `dist/duckdb/excel.duckdb_extension`; `actions/attest-sbom` with `subject-path` = the wheel and `sbom-path` = the SBOM; upload `dist/*` as artifact `dist` (retention 90 days) |
| `publish` | `build` | `contents: write` | download artifact `dist`; with `env: TAG`, `gh release create "$TAG" dist/* --verify-tag --title "$TAG" --notes-file <generated from the tag annotation>` |

| Field | Content |
|-------|---------|
| Kind | configuration file (GitHub Actions workflow) |
| Purpose | SLSA Build L2 release (ENG §5.6): build only on a hosted runner, signed provenance and an SBOM attestation per artifact, licence gate, signed-tag check. The release assets (wheel, `herness-<version>.cdx.json` SBOM, the DuckDB `excel` extension with its `SHA256SUMS`, and the attestations) are the inputs of T10-26 (herness deploy install), which verifies the attestation and SBOM before installing the wheel on the target box (ENG §14 E4, R-58). |
| Security notes | TH00-12, TH00-13. Herness builds no container image of its own; third-party images are pinned by digest by impl 10. |
| Tests | ST00-08 |

### 3.9 Repository hygiene files

#### U00-61 `.gitignore` and `.gitattributes`

| Field | Content |
|-------|---------|
| Kind | configuration file |
| Purpose | Keep data, secrets and build output out of git (design 00 §4 `data/` is gitignored), and normalise line endings across Windows and Linux. |
| Algorithm | `.gitignore` entries, one per line: `data/`, `.env`, `!.env.example`, `.venv/`, `build/`, `dist/`, `*.egg-info/`, `__pycache__/`, `*.py[cod]`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `.hypothesis/`, `.benchmarks/`, `.import_linter_cache/`, `.coverage`, `.coverage.*`, `coverage.xml`, `coverage.json`, `htmlcov/`, `*.duckdb`, `*.duckdb.wal`, `*.sqlite`, `*.sqlite-wal`, `*.sqlite-shm`, `docker.env`, `.idea/`, `.vscode/`, `.DS_Store`, `Thumbs.db`. Parquet files are not ignored globally because `tests/fixtures/lake_small/` is committed (spec 11 §4.1).<br>`.gitattributes`: `* text=auto eol=lf`; `*.ps1 text eol=crlf`; `*.bat text eol=crlf`; `*.cmd text eol=crlf`; binary: `*.parquet`, `*.duckdb`, `*.sqlite`, `*.png`, `*.jpg`, `*.pdf`, `*.xlsx`, `*.safetensors`, `*.whl`. |
| Security notes | TH00-10. |
| Tests | ST00-11 |

#### U00-62 `.env.example`

| Field | Content |
|-------|---------|
| Kind | configuration file |
| Purpose | Document the development-only `.env` layout of spec 10 §3.3 and §4.1 without any value. |
| Algorithm | Exact content, in order: comment lines stating (1) copy to `.env` for development only, never commit `.env`; (2) `.env` is read only when the process environment has `HERNESS_ENV=dev` (spec 10 §4.1), so `HERNESS_ENV` must be set in the shell, not in this file; (3) `security.*` keys cannot be set here (spec 10 §4.1); (4) the `dotenv` secrets backend is allowed only with `HERNESS_ENV=dev` or profile `synth`. Then the keys with empty values: `HERNESS_PROFILE=`, `HERNESS_LOGGING__LEVEL=`, `HERNESS_SECRET__VLLM_API_KEY=`, `HERNESS_SECRET__OPENJEV_API_KEY=`, `HERNESS_SECRET__REDACT_HMAC_KEY=`, `HERNESS_SECRET__UI_USER_REF_KEY=`, `HERNESS_SYNTH_CONFIG=`. Each key has a one-line comment above it (`local` default profile; log level; secret `vllm.api_key`; secret `OPENJEV_API_KEY`; secret `redact.hmac_key`, 64 hex characters; secret `ui_user_ref_key`; optional generator fragment path, spec 10 §4.3). |
| Security notes | TH00-10: every value is empty; no `HERNESS_SECURITY__*` key appears. |
| Tests | ST00-11 |

#### U00-63 `README.md`

| Field | Content |
|-------|---------|
| Kind | documentation file |
| Purpose | Onboarding for developers and operators; links to the design and implementation specs rather than restating them. |
| Algorithm | Sections in order: (1) **Herness** — one paragraph of purpose and the six principles as links to design 00 §2. (2) **Status** — current phase and link to `docs/specs/open-questions.md`. (3) **Requirements** — Windows 11 Pro with WSL2 (Linux supported), Python 3.12 via `uv` (pinned version from `pyproject.toml`), NVIDIA GPU with 24 GB or more for Phases 3–4 (design 10). (4) **Quick start (development)** — `uv sync --frozen` (installs `herness` in editable mode, R-58); `uv run pre-commit install`; set `HERNESS_ENV=dev` in the shell; optionally copy `.env.example` to `.env`; `uv run pytest -m unit`; generate a tiny synthetic dataset (`uv run python tools/synth_data.py --seed 7 --scale tiny`, design 11 §3.1); `uv run herness config validate --profile synth`. (5) **Repository layout** — link to design 00 §3 and the impl module maps. (6) **Configuration and secrets** — link to design 10; secrets live in Windows Credential Manager (`herness secrets set`); never put secrets in files. (7) **Development workflow** — task cards, ENG §8 definition of done, the quality-gate table of ENG §7, how to run each check locally (commands of U00-54 and U00-59). (8) **Testing** — markers and selections (design 11 §4.1–4.2). (9) **CI and releases** — required checks, branch protection of `main` (required checks of U00-59, signed annotated tags `vX.Y.Z`), what the release produces (wheel, sdist, SBOM, provenance), how to verify an attestation with `gh attestation verify <wheel> --repo <owner>/<repo>`, and that the target box installs only the release wheel through `herness deploy install`, which runs that verification and the SBOM check first (R-58; design 10). (10) **Security** — how to report a vulnerability internally; ENG §5 summary link. (11) **Documentation index** — links to `docs/architecture.html`, `docs/specs/`, `docs/impl/`. (12) **Licence** — per O-04. |
| Tests | None (documentation; reviewed in T00-02). |

### 3.10 `herness.core.numbers` (R-16)

One implementation of design 00 §12.1 that the Verifier (impl 05), the renderer and report contract (impl 09) and every other spec that checks model-written numbers (06, 07, 11) import instead of keeping their own copies. The module has three parts: marker parsing (U00-66), the uncited-numeral scanner with its allowed-pattern compiler (U00-67, U00-68), and `NumberRef` display formatting for every `format` value (U00-69, U00-70). It is pure: no I/O, no clock, no logging, no configuration access. The allowed-numeral patterns come from `config/app.yaml: reports.allowed_numeral_patterns` (key owned by impl 09); the caller reads the key and passes the list to U00-67. The module imports only the standard library (`re`, `decimal`, `unicodedata`, `dataclasses`, `typing`, `types`) and `herness.core.errors` (contract C3). It does not import `herness.core.types`: it reads `NumberRef` through the structural protocol `FormattableNumber` (U00-65), which the impl 05 `NumberRef` model satisfies.

#### U00-64 herness.core.numbers constants

| Field | Content |
|-------|---------|
| Kind | constant |
| Purpose | The marker, numeral and format definitions of design 00 §12.1, design 05 §5.6 step 3 and design 09 §4.1 rule 5, defined once. |
| Signature | `MARKER_RE: Final[re.Pattern[str]]` = `\[\[(n[0-9]{1,39})\]\]` (a valid marker; group 1 is the id; the 39-digit bound keeps every valid marker inside `ANY_MARKER_RE`).<br>`ANY_MARKER_RE: Final[re.Pattern[str]]` = `\[\[([^\[\]]{0,40})\]\]` (any double-bracket token; group 1 is the inner text).<br>`MARKER_ID_RE: Final[re.Pattern[str]]` = `^n[0-9]+$`.<br>`NUMERAL_RE: Final[re.Pattern[str]]` = `(?<![\w.])[-+]?\$?\d[\d,]*(\.\d+)?\s*(%\|k\|K\|M\|bn\|x)?(?!\w)`, identical to design 05 §5.6 step 3 (each `\|` in this table cell stands for a plain alternation bar in the pattern); compiled without flags, so `\d` and `\w` are Unicode-aware.<br>`MAX_SCAN_CHARS: Final = 100_000`; `HIT_TEXT_MAX: Final = 80`; `MAX_ALLOWED_PATTERNS: Final = 50`; `MAX_PATTERN_CHARS: Final = 200`; `TOO_LONG_TEXT: Final = "<text too long>"`; `NOT_AVAILABLE: Final = "n/a"`.<br>`NUMBER_FORMATS: Final[frozenset[str]]` = `usd`, `usd_compact`, `int`, `pct1`, `ratio2`, `hours1`, `minutes0`, `prob2` (the `NumberRef.format` values of design 00 §12.1) plus `plain` (the fallback of design 09 §4.1 rule 5, never a `NumberRef.format` value).<br>`DEFAULT_FORMAT_BY_UNIT: Final[Mapping[str, str]]` (a `MappingProxyType`) = `usd` → `usd_compact`, `pct` → `pct1`, `count` → `int`, `hours` → `hours1`, `minutes` → `minutes0`, `ratio` → `ratio2`; every other unit uses `plain`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Every string matched by `MARKER_RE` is also matched by `ANY_MARKER_RE`, and its group 1 matches `MARKER_ID_RE`. |
| Algorithm | Not applicable. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable (compiled patterns are thread-safe). |
| Complexity and limits | Scan cap 100,000 characters; hit text 80 characters; 1–50 allowed patterns of 1–200 characters each. |
| Security notes | TH00-15. |
| Tests | UT00-74, UT00-76, UT00-79 |

#### U00-65 herness.core.numbers result types and `FormattableNumber`

| Field | Content |
|-------|---------|
| Kind | class (four frozen dataclasses and one protocol) |
| Purpose | Plain result values of the parser and scanner, and the read-only view of a `NumberRef` the formatter needs. |
| Signature | `Marker` (`@dataclass(frozen=True, slots=True)`): `id: str`, `start: int`, `end: int`.<br>`MalformedMarker` (same decorator): `text: str` (the inner text, at most 40 characters), `start: int`, `end: int`.<br>`MarkerScan` (same decorator): `markers: tuple[Marker, ...]`, `malformed: tuple[MalformedMarker, ...]`; property `ids -> tuple[str, ...]` = the `id` of every marker in text order, duplicates kept.<br>`NumeralHit` (same decorator): `text: str` (at most `HIT_TEXT_MAX` characters), `start: int`, `end: int`.<br>`FormattableNumber` (`typing.Protocol`, not runtime-checkable) with read-only properties `value -> float \| int \| str`, `unit -> str`, `format -> str \| None`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `0 <= start < end`; offsets index the text passed to the producing function. Callers convert these values to their own models at their boundary (impl 05 `UncitedSpan`, impl 09 `UncitedHit`). |
| Algorithm | Not applicable. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | None. |
| Tests | UT00-74, UT00-76, UT00-78 |

#### U00-66 herness.core.numbers.parse_markers

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `text` | `str` | — | positional | any; only the first `MAX_SCAN_CHARS` characters are parsed |

Returns: `MarkerScan`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Find every number marker `[[nK]]` of design 00 §12.1 in model-written text and every malformed double-bracket token. |
| Preconditions | None. |
| Postconditions | `markers` and `malformed` are each sorted by `start` and do not overlap; every marker id matches `MARKER_ID_RE`; offsets refer to `text`. |
| Algorithm | 1. `scan = text[:MAX_SCAN_CHARS]`.<br>2. For each `ANY_MARKER_RE` match on `scan`, in order: when group 1 fully matches `MARKER_ID_RE`, append `Marker(id=group 1, start, end)`; otherwise append `MalformedMarker(text=group 1, start, end)`.<br>3. Return `MarkerScan(tuple(markers), tuple(malformed))`. Checking that each id has a `NumberRef`, that ids are unique and that every `NumberRef` is used stays with the caller (impl 05 `unknown_markers`, impl 09 contract rules), which compares `MarkerScan.ids` with its `numbers` list. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | O(len(text)), capped at 100,000 characters. |
| Security notes | TH00-15: a token such as `[[1,250]]` is malformed, never a marker, so it cannot hide a numeral. |
| Tests | UT00-74 |

#### U00-67 herness.core.numbers.compile_allowed_patterns

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `patterns` | `Sequence[str]` | — | positional | the list of `reports.allowed_numeral_patterns`; 1–50 entries; each a `str` of 1–200 characters that compiles |

Returns: `tuple[re.Pattern[str], ...]` in input order.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Validate and compile the allowed-numeral pattern list once, so the Verifier, the renderer and every other caller apply the same compiled list (design 00 §12.1). |
| Preconditions | See constraints; violations raise `ConfigError`. |
| Postconditions | One compiled pattern per input string, compiled without flags. |
| Algorithm | 1. If `patterns` is a `str` (a single string is also a `Sequence`), raise `ConfigError("allowed numeral patterns must be a list")`.<br>2. If `len(patterns)` is 0 or above `MAX_ALLOWED_PATTERNS`, raise `ConfigError("allowed numeral pattern count out of range", count=len(patterns))`.<br>3. For each `(i, p)`: if `p` is not a `str`, or `len(p)` is 0 or above `MAX_PATTERN_CHARS`, raise `ConfigError("invalid allowed numeral pattern", index=i)`; `re.compile(p)`, where `re.error` → `ConfigError("allowed numeral pattern does not compile", index=i)` from the exception. The pattern text is not copied into the error.<br>4. Return the tuple. |
| Side effects | None. |
| Errors | not a list, count out of range, bad entry, compile failure → `ConfigError` (`count` or `index`). |
| Concurrency | Pure. |
| Complexity and limits | At most 50 patterns of at most 200 characters. |
| Security notes | TB10: the patterns are operator configuration; bounding their number and length limits the cost of a pathological pattern. |
| Tests | UT00-75 |

#### U00-68 herness.core.numbers.find_uncited

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `text` | `str` | — | positional | model-written text; only the first `MAX_SCAN_CHARS` characters are scanned |
| `allowed` | `Sequence[re.Pattern[str]]` | — | positional | the result of U00-67 |

Returns: `tuple[NumeralHit, ...]`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | The numeral scanner of design 00 §12.1: find every numeral outside a valid marker that no allowed pattern covers. |
| Preconditions | `allowed` compiled by U00-67. |
| Postconditions | Hits sorted by `(start, end)`; offsets refer to `text`; an empty result means the text carries numbers only through markers and allowed numerals. |
| Algorithm | 1. `scan = text[:MAX_SCAN_CHARS]`; `markers = parse_markers(scan).markers` (U00-66).<br>2. `blanked` = `scan` with the span of every valid marker replaced by the same number of spaces (offsets are preserved). Malformed markers are not blanked, so numerals inside them are scanned.<br>3. `allowed_spans` = every non-empty match of every pattern in `allowed`, found with `finditer` on `blanked`.<br>4. For each `NUMERAL_RE` match on `blanked` with span `(s, e)`: record `(s, e)` as a numeral span; then, while `e > s` and `blanked[e - 1]` is whitespace, decrease `e`. The token is exempt when some allowed span `(a, b)` has `a <= s` and `e <= b`. Otherwise add `NumeralHit(text=scan[s:e][:HIT_TEXT_MAX], start=s, end=e)`.<br>5. For each index `i` of `blanked` whose character `ch` is not an ASCII digit, is not whitespace and has `unicodedata.numeric(ch, None) is not None` (superscripts, fractions, Roman numeral and other numeric characters that `\d` does not match), and `i` lies inside no numeral span of step 4 and no allowed span: add `NumeralHit(text=ch, start=i, end=i + 1)`.<br>6. If `len(text) > MAX_SCAN_CHARS`, add `NumeralHit(text=TOO_LONG_TEXT, start=MAX_SCAN_CHARS, end=len(text))`, so an overlong text never passes.<br>7. Return the hits sorted by `(start, end)`. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | O(len(text) × len(allowed)), text capped at 100,000 characters; BT00-05. |
| Security notes | TH00-15 (LLM09): Unicode digits (`\d` is Unicode-aware), other numeric characters (step 5), numerals inside malformed markers (step 2) and text past the scan cap (step 6) are all reported. |
| Tests | UT00-76, UT00-77, PT00-07, ST00-18, BT00-05 |

#### U00-69 herness.core.numbers.format_value

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `value` | `object` | — | positional | a `NumberRef.value`: `int`, `float`, decimal `str` or `decimal.Decimal` |
| `unit` | `str` | — | positional | a `NumberRef.unit` value |
| `fmt` | `str \| None` | — | positional | a `NumberRef.format` value or `None` |

Returns: `str`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Display a number deterministically for every `NumberRef.format` value (design 00 §12.1, design 09 §4.1 rule 5), on every OS and locale. |
| Preconditions | None (invalid input gives `NOT_AVAILABLE`). |
| Postconditions | The result is `NOT_AVAILABLE` or a fixed-point string (never exponent notation) built only from the characters `0123456789,.$%-`, space, `K`, `M`, `B`, `h`, `m`, `i` and `n`. The same arguments always give the same result. |
| Algorithm | 1. `f = fmt` when `fmt is not None`, else `DEFAULT_FORMAT_BY_UNIT.get(unit, "plain")`. If `f not in NUMBER_FORMATS`, return `NOT_AVAILABLE`.<br>2. Convert: a `bool` → return `NOT_AVAILABLE`; an `int`, `float`, `str` or `Decimal` → `d = Decimal(str(value))`, where `decimal.InvalidOperation` → return `NOT_AVAILABLE`; any other type → return `NOT_AVAILABLE`. If `d` is not finite, return `NOT_AVAILABLE`.<br>3. Rounding is `quantize` with `ROUND_HALF_EVEN` in a local `decimal.Context` of precision 60 (the global context is neither used nor changed). "Grouped" means the integer digits in groups of three from the right joined by `,`, built by string slicing (no `locale`). A result that rounds to zero is written without a minus sign. A negative result puts `-` first (before `$`).<br>4. Rules by `f`: `usd` → `$`, grouped integer part, 2 decimals (`-$1,250,000.00`). `usd_compact` → with `a = abs(d)`, pick the tier: `a >= 1e9` → divide by `1e9`, 2 decimals, suffix `B`; `a >= 1e6` → divide by `1e6`, 2 decimals, suffix `M`; `a >= 1e3` → divide by `1e3`, 1 decimal, suffix `K`; else 0 decimals and no suffix; when the rounded mantissa is 1000 or more and a higher tier exists, use the next higher tier instead (`999960` → `$1.00M`, `999.6` → `$1.0K`); the `B` tier mantissa is grouped; examples `$1.84M`, `$12.5K`, `$950`. `int` → 0 decimals, grouped (`1,204`). `pct1` → 1 decimal and `%` (unit `pct` stores 0–100; `42.0%`). `ratio2` and `prob2` → 2 decimals (`0.37`). `hours1` → 1 decimal and ` h` (`3.5 h`). `minutes0` → 0 decimals and ` min` (`45 min`). `plain` → when `d` is integral, the `int` rule; else quantize to 4 decimals, drop trailing zeros of the fraction, and use the `int` rule when no fraction digit is left (`7`, `0.1234`).<br>5. Return the string, written in fixed-point form (`format(x, "f")` on the quantized `Decimal`). |
| Side effects | None. |
| Errors | None: unconvertible or non-finite values and unknown formats give `NOT_AVAILABLE`, which the renderer counts as unlinked (impl 09). |
| Concurrency | Pure (local decimal context). |
| Complexity and limits | O(number of digits). |
| Security notes | TB5: the output alphabet contains no markup characters. |
| Tests | UT00-78, UT00-79, PT00-06 |

#### U00-70 herness.core.numbers.format_number

| Name | Type | Default | Kind | Constraints |
|------|------|---------|------|-------------|
| `ref` | `FormattableNumber` | — | positional | an impl 05 `NumberRef` or any object with the three read-only attributes |

Returns: `str`.

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Display a `NumberRef` value; the single formatter the renderer, dashboard, chat and stored-text renderings use (R-16). |
| Preconditions | None. |
| Postconditions | Equals `format_value(ref.value, ref.unit, ref.format)`. |
| Algorithm | 1. Return `format_value(ref.value, ref.unit, ref.format)` (U00-69). |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | As U00-69. |
| Security notes | As U00-69. |
| Tests | UT00-78 |

## 4. State and data

### 4.1 Tables and migrations

Not applicable: the foundation owns no ops-store or warehouse table and no migration, so it has no range under R-11. `metric_sample` (ENG §4) is created by impl 02 migration 006 and written by `herness.store.ops.metrics.record_metric_samples` (impl 08) (R-12).

### 4.2 Files

| File | Written by | Format | Idempotency | Retention |
|------|-----------|--------|-------------|-----------|
| `data/logs/herness-<YYYY-MM-DD>.jsonl` (under `paths.logs`) | U00-40, in every process that called `configure_logging` with `log_dir` | One JSON object per line, UTF-8, `\n` line ends; schema in §4.4 | Append-only; a rerun appends new lines (no key; duplicates are acceptable for logs) | `retention.app_log_days` (30 days), purged by impl 10 maintenance |
| stderr stream | U00-36 stderr handler, U00-40 failure and recovery lines | Same line format | — | Captured by the service wrapper (impl 10) |
| `build/*` (CI only) | U00-59, U00-60 | requirements export, pip-audit JSON, osv JSON, SBOM JSON, audit summary | Regenerated per run | CI artifact retention: 30 days (`audit`), 90 days (`dist`), 14 days (`ci-reports`) |
| `.secrets.baseline` | developer via `detect-secrets scan` | detect-secrets JSON | Regenerated when needed | In git |
| `uv.lock` | `uv lock` | uv lock TOML with hashes | Deterministic for the same inputs | In git |

### 4.3 In-memory state

| State | Module | Concurrency model | Reset |
|-------|--------|-------------------|-------|
| ULID generator `_last_ms`, `_last_rand` | `herness.core.ids` | lock-protected (`_lock`, `threading.Lock`) | Never needs reset; tests monkeypatch `time.time_ns` and the two variables |
| Logging `_state` (installed handlers, settings) and the global `logging`/structlog configuration | `herness.core.logging` | lock-protected (`_config_lock`) for configuration; handlers use the `logging.Handler` lock | `reset_logging()` (U00-39), called by the test fixture `configured_logging` in `tests/unit/core/conftest.py` |
| `DailyJsonlHandler` stream, day, failure clock | `herness.core._log_pipeline` | handler lock | `close()` |
| Log context IDs | structlog `contextvars` | per thread and per asyncio task | `bind_ids` exit; `reset_logging()` |
| Compiled marker, numeral and format constants | `herness.core.numbers` | immutable | not needed |

These are exceptions to ENG §2.3 (module-level mutable state) and are listed in §13.3.

### 4.4 Log line schema (`herness-<date>.jsonl` and stderr)

| Key | Type | Nullability | Constraint | Meaning |
|-----|------|-------------|------------|---------|
| `ts` | string | required | 27 characters, `YYYY-MM-DDTHH:MM:SS.ffffffZ` | Emission time (UTC) from `herness.core.time.now()` |
| `level` | string | required | `debug`, `info`, `warning`, `error`, `critical` | Log level |
| `event` | string | required | matches `EVENT_NAME_RE` for Herness events; free text for third-party records | Event name `component.object.action` |
| `component` | string | required | `COMPONENT_RE`, or `ext.<library>` for third-party records | Emitting component |
| `pid` | integer | required | > 0 | Process ID (several processes share one file) |
| `run_id`, `task_id`, `job_id`, `build_id` | string | optional | valid per U00-25 / U00-26 | Context IDs from `bind_ids` |
| `exception` | array of objects | optional | structlog dict traceback, no locals, ≤ 20 frames | Exception chain |
| `event_name_invalid` | boolean | optional | `true` only | Event name failed `EVENT_NAME_RE` in non-strict mode |
| `dropped_fields` | array of strings | optional | sorted | Fields removed to fit `MAX_LINE_BYTES` |
| other keys | scalar, list or object | optional | strings ≤ 2,000 characters plus marker; depth ≤ 4 | Event fields listed by each implementation spec |

## 5. Control flows

#### F00-01 Generate a prefixed ID

1. Caller calls `new_id(IdKind.RUN)` (U00-22). No state changes yet.
2. `new_ulid()` (U00-20) reads `time.time_ns()`, takes `_lock`, updates `_last_ms` and `_last_rand`, releases the lock. Failure: timestamp overflow → `SchemaViolation`; the caller's job fails as `FatalError` (cannot happen before year 10889).
3. The 128-bit value is encoded to 26 Crockford characters and prefixed. No failure path.
4. Caller stores the ID (the write and its idempotency key belong to the caller's spec).

#### F00-02 Logging bootstrap in a composition root (T09-20 (herness.cli.main), job worker, Streamlit page wrapper)

1. Process start: call `configure_logging("INFO")` (U00-36) with stderr only and no scrubber. State: root handlers installed. Failure: none possible with these arguments.
2. Load configuration (T10-03 (herness.core.config.load_config)). Failure: `ConfigError` is logged to stderr through the bootstrap configuration and the process exits with code 3 (`ConfigError`, R-46).
3. Call `configure_logging(cfg.logging.level, log_dir=cfg.paths.logs, scrubber=T10-07 (herness.core.secrets.scrub_secrets))`. State: handlers replaced; `core.logging.configured` emitted. Failure: `ConfigError` (bad level, directory not creatable) → logged to stderr, process exits with code 3 (`ConfigError`, R-46).
4. Around each job, run or task, the owner wraps work in `bind_ids(job_id=..., run_id=...)` (U00-37). Failure: `SchemaViolation` for an invalid ID → the caller's error path (a programming error; the job fails `FatalError`).
5. On shutdown, `reset_logging()` (U00-39) closes the file. Failure: none.

#### F00-03 Emit one log line

1. Component code calls `log.info("connectors.sync.completed", source=..., rows=...)` on a logger from `get_logger` (U00-38).
2. structlog chain steps 1–4 (§3.4) run in the caller's thread: level filter; context merge; `check_event_name` (strict mode raises `SchemaViolation` to the caller, else flags); hand-off to `logging`.
3. For each handler, the formatter runs steps 5–14: component, level, `ts` and `pid`, exception rendering, value normalisation, sensitive-key guard (U00-41), scrubber, size limits and JSON rendering (U00-42, U00-43). Failure: a scrubber exception is caught by `logging.Handler.handleError`, which writes a Python traceback to stderr and drops the line (the record is not written unscrubbed).
4. stderr handler writes the line. `DailyJsonlHandler` (U00-40) writes it to the day file. Failure: `OSError` → stderr `core.logging.sink_failed`, file writes paused for 60 s, then retried; on success `core.logging.sink_recovered` with `dropped_lines`.

#### F00-04 Raise and classify an error (consumer view)

1. An adapter maps a third-party failure to a taxonomy class (`raise SourceUnavailable("servicenow page fetch failed", source="servicenow", page=3) from exc`). State: none.
2. The resilience layer (T08-07 (herness.core.resilience.retry_call)) dispatches on the class (`isinstance` against U00-02), never on the message.
3. When the error ends a job, impl 08 stores `to_log_fields(exc)` (U00-08) in the job record and logs it; the cause's message is not copied (TH00-06).
4. If the error crosses a process boundary (multiprocessing pool), it is pickled through `HernessError.__reduce__` (U00-01) and rebuilt with the same class, message, context and extra attributes.

#### F00-05 Compute a `query_id` (used by impl 04 and impl 05)

1. Caller renders SQL and parameters, and holds `build_id` of the pinned warehouse.
2. `query_id(sql, params, build_id)` (U00-32): normalise SQL (U00-31); validate build ID (U00-26); canonical JSON (U00-29); SHA-256 (U00-30). Failure: `SchemaViolation` for empty SQL, bad build ID or a non-canonicalisable parameter → the caller maps it (impl 05 returns a `ToolInputError` tool result; impl 04 fails the metric computation).
3. Caller records evidence keyed by the returned ID (owner spec).

#### F00-06 Commit and push gates

1. `git commit` runs the U00-54 hooks 1–10 in order. Failure at any hook: the commit is refused with the hook's output; ruff and format hooks may have modified files, which the developer re-stages.
2. `git push` runs hook 11 (CPU test selection with coverage). Failure: push refused.

#### F00-07 Pull-request CI

1. Pull request to `main` triggers `ci.yml` (U00-59) jobs `lint`, `types`, `test` (two OS), `audit` in parallel.
2. Each job installs from `uv.lock` (`UV_FROZEN=1`). Failure: lock out of date → `uv sync` fails → job fails.
3. Branch protection requires all five checks. Failure of any check blocks the merge; artifacts `ci-reports` and `audit` are uploaded even on failure.

#### F00-08 Nightly dependency audit

1. `schedule` trigger at 03:17 UTC runs only `audit`.
2. `check_audit` (U00-57) blocks on High or Critical with a fix, or an expired ignore. Failure: the scheduled run fails and GitHub notifies the repository admins; triage follows ENG §5.5 (7 days High, 30 days Medium) with a fix or a time-boxed entry in `tools/audit_ignore.toml`.

#### F00-09 Release (SLSA Build L2)

1. Maintainer pushes a signed annotated tag `vX.Y.Z` on a commit of `main`.
2. `release.yml` (U00-60) job `ci` re-runs every gate on the tag. Failure: release stops.
3. Job `build` verifies the tag object is annotated and signature-verified, and that the tag matches `project.version`. Failure: release stops before building.
4. `uv build` on the hosted runner; runtime SBOM; licence gate (enforce). Failure: release stops.
5. Provenance attestation for wheel and sdist; SBOM attestation for the wheel (signed with the workflow's OIDC identity). Failure: release stops.
6. Job `publish` creates the GitHub release with the artifacts. Failure: artifacts stay available as the `dist` workflow artifact; the maintainer re-runs `publish`.
7. On the target box, T10-26 (herness deploy install) verifies the attestation and SBOM before installing (ENG §14 E4).

#### F00-10 An owner spec adds its shared types

1. The owner card creates `herness/core/types/<submodule>.py`, or the package `herness/core/types/<submodule>/` when its types exceed 400 lines (U00-45 `OWNER_MODULES`, R-01), with only the imports allowed by §3.5.
2. It adds any helper model names to `TYPE_OWNERS` in `_ownership.py`.
3. It adds one import line and the names to `__all__` in `herness/core/types/__init__.py` (U00-44).
4. `python -m tools.check_type_ownership` (U00-47) and `lint-imports` (C3–C5) pass. Failure: pre-commit and CI `lint` block the change with `OWN0xx` codes.

#### F00-11 Check the numbers in model-written text (Verifier 05, renderer and report contract 09, and 06, 07, 11)

1. At construction, the caller reads `reports.allowed_numeral_patterns` from configuration and calls `compile_allowed_patterns` (U00-67) once. State: the caller holds the compiled tuple. Failure: `ConfigError` → the caller's constructor fails and the process start fails (the impl 09 config validator reports the same list earlier through `herness config validate`).
2. Per text field, the caller calls `parse_markers(text)` (U00-66) and compares `MarkerScan.ids` with the field's `numbers` list. Failure: none raised; malformed markers, ids without a `NumberRef`, duplicate ids and unused `NumberRef`s are recorded by the caller (impl 05 `unknown_markers` and `bad_refs`; impl 09 contract violations).
3. The caller calls `find_uncited(text, allowed)` (U00-68). Failure: none raised; every `NumeralHit` is an uncited numeral that the caller turns into its own record (impl 05 `UncitedSpan`, which fails the item; impl 09 `UncitedHit`, which blocks or flags the render per `reports.strict_numbers`).
4. The renderer, dashboard and chat display each marker with `format_number(ref)` (U00-70). Failure: none raised; `n/a` means the value could not be displayed and the renderer counts the number as unlinked (impl 09).

## 6. Error handling

| Failure condition | Class raised | Where caught | Retry or fallback | User-visible effect | Log event |
|-------------------|-------------|--------------|-------------------|--------------------|-----------|
| Naive datetime passed to a time helper, `new_build_id` or `canonical_json` | `SchemaViolation` | Caller; at a trust boundary it is re-raised as the boundary's class | none | Operation fails with the caller's message | caller's event |
| Timestamp text not in the fixed-width format | `SchemaViolation` | Caller (impl 02 ops readers, impl 01 watermark) | none | Row or value rejected | caller's event |
| Operator ISO text unparseable or naive | `SchemaViolation` | CLI (impl 09) maps to a usage error | none | CLI prints the error, exit 2 (usage error, R-46) | `cli.command.failed` (impl 09) |
| Unknown time zone | `ConfigError` | Config validation (impl 10) | none | `herness config validate` lists the issue | `config.validate.failed` (impl 10) |
| Sleep duration negative, non-finite or above 3,600 s | `SchemaViolation` | Not caught (programming error) | none | Job fails | impl 08 job failure event |
| Bad `record_id` part or text | `SchemaViolation` | Caller (connectors reject the record; CLI maps to usage error) | none | Record skipped and counted by impl 01; CLI usage error | caller's event |
| Non-canonicalisable value, empty SQL or bad build ID for `query_id` | `SchemaViolation` | impl 05 tool wrapper → `ToolInputError` result; impl 04 → metric run fails | none | Agent gets an error tool result; metric error | impl 05 / 04 events |
| `new_token` size out of range | `SchemaViolation` | Not caught | none | Programming error | — |
| ULID timestamp overflow | `SchemaViolation` | Not caught | none | Not reachable before year 10889 | — |
| Allowed-numeral pattern list not a list, empty, longer than 50, an entry empty or over 200 characters, or not compiling | `ConfigError` (`count` or `index`) | Caller's constructor (impl 05 Verifier, impl 09 renderer); the impl 09 config validator reports it at load | none | `herness config validate` lists the issue; a process that reaches the constructor exits with code 3 (`ConfigError`, R-46) | `config.validate.failed` (impl 10) |
| Model-written text holds malformed markers or uncited numerals | none raised | Caller (F00-11) | none | Verifier fails the item; renderer marks or blocks per impl 09 | caller's event (impl 05, impl 09) |
| Number value cannot be displayed (non-numeric, non-finite, `bool`, unknown format) | none raised (`n/a`) | Renderer (impl 09) | none | `n/a` shown, number counted as unlinked | impl 09 event |
| Invalid log level | `ConfigError` | Composition root | none | Process exits with configuration error | stderr bootstrap line |
| File logging requested without scrubber | `ConfigError` | Composition root | none | Process exits | stderr bootstrap line |
| Log directory cannot be created | `ConfigError` | Composition root | none | Process exits | stderr bootstrap line |
| Invalid log component or context ID, or strict-mode bad event name | `SchemaViolation` | Not caught (programming error; tests use strict mode) | none | Test failure | — |
| Log file write fails (`OSError`) | none raised | `DailyJsonlHandler.emit` | Retry after 60 s; lines kept on stderr meanwhile | None beyond missing file lines | `core.logging.sink_failed`, then `core.logging.sink_recovered` |
| Scrubber raises inside the formatter | none raised to caller | `logging.Handler.handleError` | Line dropped | None | Python traceback on stderr |
| Check script finds violations | none (exit 1) | pre-commit, CI | none | Commit, push, merge or release blocked | script output |
| Check script bad input | none (exit 2) | pre-commit, CI | none | Gate fails | script stderr |

## 7. Security

### 7.1 (a) Trust boundaries touched

| Boundary | How the foundation touches it |
|----------|-------------------------------|
| TB9 Package index, container registry, model hub → build and deploy | `uv.lock` with hashes, explicit torch index, pinned actions and hooks, audit and licence gates, SBOM and provenance |
| TB10 Operator → CLI and config files | `parse_iso`, `split_record_id`, `is_valid_id`, `zone` validate operator input; `.env.example` and `.gitignore` keep secrets out of git |
| TB3 Ticket and model text → logs (internal flow from any component into the log sink) | `guard_sensitive`, scrubber slot, value normalisation, size limits, JSON escaping |
| TB6 Host → off-network endpoints (indirect) | ruff bans on `httpx` client and transport construction outside `herness.core.egress` (R-06) |
| TB4 Model output → Verifier; TB5 Model output → rendered reports, dashboard and chat (through callers) | `herness.core.numbers` parses markers, finds uncited numerals and formats cited values for the Verifier and the renderer (R-16) |

### 7.2 (b) STRIDE threats

| ID | Boundary | STRIDE | Threat | Likelihood | Impact | Control | Reference | Test |
|----|----------|--------|--------|-----------|--------|---------|-----------|------|
| TH00-01 | TB3 / logs | I | A secret value (API key, token) reaches a log file or stderr, directly or inside an exception message | Medium | High | Secret-named keys always omitted (U00-41); file sink refuses to start without the impl 10 scrubber (U00-36); values stringified before scrubbing and scrubbed before truncation (§3.4 order) | ASVS v5.0.0-V16.2; ASVS v5.0.0-V13.3 | ST00-01 |
| TH00-02 | TB3 / logs | I | Ticket text, prompts or completions logged above DEBUG | Medium | Medium | `TEXT_KEYS` omitted above DEBUG (U00-41) | ASVS v5.0.0-V16.2; LLM02 | ST00-02 |
| TH00-03 | TB3 / logs | T, R | Log forging: a value with newlines or JSON fragments creates fake lines or events | Medium | Medium | One `json.dumps` per line escapes control characters (U00-43); context IDs validated (U00-37) | ASVS v5.0.0-V16.4 | ST00-03 |
| TH00-04 | TB3 / logs | D | Oversized or flooding values exhaust disk or make lines unreadable; disk-full stops the process | Low | Medium | 2,000-character field cap, 16 KiB line cap (U00-43); file sink failure is non-fatal with retry (U00-40); retention purge (impl 10) | ASVS v5.0.0-V16.4 | ST00-04 |
| TH00-05 | TB10 | S | A sequential or guessable identifier is used as a bearer secret (session or approval link) | Low | High | ULIDs documented as non-secret; `new_token` (CSPRNG, ≥ 128 bits) for bearer values (U00-33) | ASVS v5.0.0-V11; ASVS v5.0.0-V7 | ST00-05 |
| TH00-06 | TB6 / logs | I | A third-party exception message (URL with query token) copied into job records or log fields | Medium | Medium | `to_log_fields` never copies non-Herness or cause messages (U00-08); `HernessError` context is scalar-only and bounded, and `hint` and `details` (R-19) are bounded strings with non-string values replaced by their type name (U00-01) | ASVS v5.0.0-V16.5 | ST00-13 |
| TH00-07 | TB10 | T | Malformed timestamp text breaks SQLite text ordering or smuggles content through a timestamp field | Low | Medium | Strict fixed-width parser with length check first (U00-15); naive datetimes rejected (U00-13) | ASVS v5.0.0-V2 | ST00-14 |
| TH00-08 | TB6, TB9 | E | Layering bypass: lower layer imports a higher one, a settings module pulls in a higher layer or an I/O library, a module other than `herness.core.egress` builds its own `httpx` client or transport, or code uses pickle or `yaml.load` | Medium | High | import-linter contracts C1–C6 (U00-52); settings rule OWN050 (U00-47, R-03); ruff banned APIs with the only `TID251` exception on `herness/core/egress.py` (U00-50, R-06); all in pre-commit and CI; the impl 10 socket guard is the run-time backstop | ASVS v5.0.0-V15.2; ASVS v5.0.0-V15.3 | ST00-09, ST00-10 |
| TH00-09 | internal | T | A shared type is redefined in another package and drifts from its owner's contract | Medium | Medium | Ownership checker (U00-47) in pre-commit and CI; import rules C3–C5 | ASVS v5.0.0-V15.2 | ST00-15 |
| TH00-10 | TB10 | I | A secret is committed (a real `.env`, a key in code or fixtures) | Medium | High | `.gitignore` of `.env` and `data/`; empty `.env.example`; detect-secrets and `detect-private-key` in pre-commit and CI | ASVS v5.0.0-V13.3 | ST00-11, ST00-12 |
| TH00-11 | TB9 | T | A tampered or vulnerable dependency is installed | Medium | High | `uv.lock` hashes with frozen installs; pip-audit plus osv-scanner gate (U00-57), fail closed on unknown severity; licence gate (U00-58); nightly audit | ASVS v5.0.0-V15.2; LLM03 | ST00-07, ST00-16 |
| TH00-12 | TB9 | T, E | CI compromise: unpinned third-party action, `pull_request_target` with a write token, or script injection from pull-request text | Low | High | Actions pinned by SHA; read-only default permissions; no `pull_request_target`; untrusted values only through `env` (§3.8 rules) | ASVS v5.0.0-V15.2; LLM03 | ST00-06 |
| TH00-13 | TB9 | S, R | A release artifact built on a developer machine, or altered after build, is installed | Low | High | Release only from the hosted workflow on a verified signed tag; provenance and SBOM attestations (U00-60); verification at install (T10-26 (herness deploy install)) | ASVS v5.0.0-V15.2; LLM03 | ST00-08 |
| TH00-14 | internal | T | Two different parameter sets produce the same `query_id` because of encoding ambiguity | Low | Low | Sorted keys, no whitespace, type-specific encodings, non-finite numbers rejected (U00-29); residual RR-03 | ASVS v5.0.0-V11 | ST00-17 |
| TH00-15 | TB4, TB5 | T | A model-written numeral escapes the uncited-numeral check (Unicode or full-width digits, superscripts or fractions, a numeral inside a malformed marker, text past the scan cap), so an unverified number reaches a finding, report or chat answer; or the Verifier and the renderer disagree because each keeps its own scanner | Medium | High | One shared implementation (R-16) used by 05 and 09; Unicode-aware `NUMERAL_RE`; numeric-character pass; malformed markers not blanked; overlong text always reported (U00-66, U00-68) | ASVS v5.0.0-V2; LLM09; LLM05 | ST00-18 |

### 7.3 (c) ASVS 5.0 mapping

| ASVS reference | Requirement area | How this spec meets it | Tests |
|----------------|-----------------|------------------------|-------|
| ASVS v5.0.0-V2 | Input validation | Strict parsers and validators for timestamps, IDs, record IDs, zone names; marker parsing and the uncited-numeral scan of model output; bounded allowed-pattern list | UT00-15, UT00-16, UT00-25 … UT00-29, ST00-14, UT00-74 … UT00-77, ST00-18 |
| ASVS v5.0.0-V11 | Cryptography and random values | `secrets` CSPRNG for ULID randomness and tokens; `hashlib` SHA-256 only | UT00-33, UT00-34, ST00-05 |
| ASVS v5.0.0-V13.3 | Secret management | No secrets in repo, `.env` ignored, empty example, secret scanning | ST00-11, ST00-12 |
| ASVS v5.0.0-V15.2 | Security architecture and dependencies | Layer contracts, lock with hashes, audit, SBOM, provenance, licence gate | ST00-06 … ST00-10, ST00-16 |
| ASVS v5.0.0-V15.3 | Defensive coding | Banned unsafe APIs (pickle, `yaml.load`), mypy strict | ST00-09, UT00-57 |
| ASVS v5.0.0-V16.2 | General logging | Structured JSON lines, UTC timestamps, component and context IDs, no secrets or sensitive text | UT00-35, UT00-41, ST00-01, ST00-02 |
| ASVS v5.0.0-V16.4 | Log protection | Injection-safe encoding, bounded sizes; file ACLs by impl 10 | ST00-03, ST00-04 |
| ASVS v5.0.0-V16.5 | Error handling | Taxonomy with bounded messages; safe log fields | UT00-02, UT00-07, ST00-13 |

### 7.4 (d) LLM Top 10 and AI RMF

| Item | Relevance | Control here |
|------|-----------|--------------|
| LLM02 Sensitive information disclosure | Prompts and completions must not reach logs above DEBUG | `TEXT_KEYS` guard (U00-41), scrubber slot |
| LLM03 Supply chain | Python packages and CI actions | Lock hashes, pins, audit, SBOM, provenance (U00-49, U00-57 … U00-60) |
| LLM05 Improper output handling | Numbers in model text must appear only as `NumberRef` markers | Marker parsing (U00-66); formatter output alphabet has no markup (U00-69) |
| LLM09 Misinformation | Every number in model-written text is cited and re-run | One uncited-numeral scanner shared by the Verifier and the renderer (U00-68, TH00-15, R-16) |
| Other items (LLM01, LLM04, LLM06–LLM08, LLM10) | Not applicable: the foundation builds no prompt, calls no model and stores no model output | — |
| NIST AI RMF | Not applicable: ENG §5.4 names specs 03, 05, 06, 07, 11 | — |

NIST SSDF practices realised here: PO (ENG adoption through pre-commit and CI), PS (lock with hashes, signed tags, protected `main`), PW (ruff `S`, mypy strict, security tests per threat), RV (pip-audit and osv-scanner in CI and nightly, time-boxed ignores with expiry).

### 7.5 (e) Secrets

The foundation modules resolve no secret. The scrubber they call is provided by impl 10 and reads `known_values()`. CI uses no repository secret: only the automatic `GITHUB_TOKEN` (read-only by default; `contents: write` in `publish`) and the workflow OIDC token for attestations (`id-token: write` in `build` only).

### 7.6 (f) Data classification

| Field or artifact | Classification | Notes |
|-------------------|----------------|-------|
| Log lines at INFO and above | internal | IDs, counts, event names; secret keys omitted; text keys omitted |
| Log lines at DEBUG | confidential | May hold ticket text or prompts (redacted upstream by impl 10 where required) |
| ULIDs, prefixed IDs, `build_id`, `query_id` | internal | Not secrets |
| `record_id` | internal | Source keys; never personal data by construction of spec 01 keys |
| Tokens from `new_token` | confidential | Never logged |
| `HernessError.message`, `context`, `hint` and `details` | internal | Must not hold personal data or secrets (ENG §3.4, R-19) |
| `MarkerScan`, `NumeralHit`, formatted numbers | internal | Derived from model-written text that is redacted upstream; hit text capped at 80 characters |
| SBOM, audit reports, coverage | internal | CI artifacts |
| `.env` | confidential | Never committed |
| `.env.example`, README, workflows | public within the organisation | No values |

### 7.7 (g) Accepted residual risks

Residual-risk IDs use the prefix `RR-` so they cannot be confused with the ruling IDs `R-nn` of `DECISIONS.md`.

| ID | Risk | Reason accepted | Owner |
|----|------|-----------------|-------|
| RR-01 | Lines from several processes appending to the same day file can interleave on Windows under heavy concurrent logging | Each line is one `write` call under 16 KiB; interleaving needs simultaneous large writes; logs are diagnostic, not the audit trail (impl 10 owns the chained audit log) | impl 00 |
| RR-02 | ULIDs minted in the same millisecond are sequential (+1) and predictable | ULIDs are identifiers only; bearer values use `new_token` | impl 00 |
| RR-03 | `canonical_json` encodes `Decimal("1")` and `"1"` identically, so `query_id` can collide for params differing only in that type | Parameters come from typed templates (impl 04) or are `{}` (impl 05); the collision would map two identical-text queries to one ID on one build | impl 00 |
| RR-04 | The build backend (`hatchling`) is resolved by range, not by hash, during `uv build` | Build runs only on the hosted runner; the backend is a well-maintained PyPA-adjacent project; provenance records the build | impl 00 |
| RR-05 | Vulnerability databases lag disclosures | Nightly audit re-checks; ENG §5.5 triage times | impl 00 |

## 8. Observability

### 8.1 Log events

| Event | Level | Fields | When |
|-------|-------|--------|------|
| `core.logging.configured` | INFO | `level`, `log_dir`, `scrubber`, `strict_event_names` | End of every `configure_logging` call |
| `core.logging.sink_failed` | ERROR | `path`, `error_type`, `retry_in_s` | File write or open failed (stderr only) |
| `core.logging.sink_recovered` | INFO | `path`, `dropped_lines` | First successful file write after a failure (file and stderr) |

`errors`, `time`, `ids` and `numbers` emit no log events (pure helpers; callers log).

### 8.2 Metrics

None. The foundation's modules are pure helpers or the log sink; none records a metric, so none needs the metric port of R-04 (`metric_sample` is created by impl 02 migration 006 and written by `herness.store.ops.metrics.record_metric_samples`, impl 08, R-12). Log-sink health is visible through the events above.

### 8.3 Trace events

None. Spec 05's `Tracer` is the only trace writer (design 00 §8).

### 8.4 Health

Not applicable: the foundation runs no long-lived component. `herness doctor` (impl 09 and 10) checks log-directory writability as part of its own checks.

## 9. Configuration

| Key path | Type | Default | Validation | Restart needed | Sensitivity | Read by |
|----------|------|---------|------------|----------------|-------------|---------|
| `logging.level` (`config/herness.yaml`, owned by impl 10) | string | `INFO` | one of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, case-insensitive (U00-36) | yes (read at process start) | internal | composition root, passed to U00-36 |
| `paths.logs` (`config/herness.yaml`, owned by impl 10) | path | `data/logs` | directory creatable (U00-36) | yes | internal | composition root, passed to U00-36 |
| `weights.business_timezone` (owned by impl 04) | string | set by impl 04 | resolvable by `zone()` (U00-18) | yes | internal | impl 04 and impl 08 via U00-18 |
| `reports.allowed_numeral_patterns` (`config/app.yaml`, owned by impl 09) | list of regex strings | the five patterns of design 09 §7 | U00-67: a list of 1–50 strings of 1–200 characters that each compile | as impl 09 states (read when the caller is constructed) | internal | impl 05, 06, 07, 09 and 11 callers, passed to U00-67 and U00-68 (R-16) |
| `[tool.herness.module_budgets]` (`pyproject.toml`) | table | `default = 400`, override for `herness/harness/loop.py` | U00-55 step 1 | not applicable (CI) | public | U00-55 |
| `[tool.herness.licences]` (`pyproject.toml`) | table | §3.7 initial content | U00-58 step 1 | not applicable | public | U00-58 |
| `tools/audit_ignore.toml` | TOML | `ignore = []` | U00-57 step 5 | not applicable | public | U00-57 |

Environment variables read by foundation modules: none. `HERNESS_ENV`, `HERNESS_PROFILE` and `HERNESS_*` overrides are read by impl 10.

## 10. Performance and capacity

Design 00 sets no performance targets. The foundation sets these budgets so that it never dominates callers. Reference PC per design 02 §9 (16 cores, 64 GB, NVMe); marker `integration` plus `slow`, files in `tests/bench/`.

| Measure | ID | Dataset and method | Pass threshold |
|---------|----|--------------------|----------------|
| `new_ulid` throughput, one thread | BT00-01 | 1,000,000 calls, `pytest-benchmark` | ≥ 200,000 IDs/s |
| `query_id` latency | BT00-02 | 2 KB SQL with 10 parameters, 10,000 calls | p95 < 50 µs |
| Logging throughput to file | BT00-03 | 10,000 INFO lines with 10 fields each, pass-through scrubber, file handler only | total < 1.0 s |
| `check_traceability` runtime | BT00-04 | all `docs/impl/*.impl.md` plus `tests/` of the repository at the time of the run | < 5 s |
| `find_uncited` latency | BT00-05 | 100,000-character text with 1,000 valid markers, 500 allowed numerals and 50 uncited numerals; the five default patterns; 100 calls | median < 100 ms per call |

Limits enforced by code:

| Limit | Value | Where |
|-------|-------|-------|
| Error message length | 1,000 characters | U00-01 |
| Error context string length | 200 characters | U00-01 |
| Error hint length | 500 characters | U00-01 |
| Error details | 50 entries; key 64 characters; value 2,000 characters | U00-01 |
| Scanned model text | 100,000 characters (longer text always fails the scan) | U00-66, U00-68 |
| Uncited hit text | 80 characters | U00-68 |
| Allowed-numeral patterns | 1–50 patterns of 1–200 characters | U00-67 |
| Sleep duration | 3,600 s | U00-10, U00-11 |
| ISO input length | 64 characters | U00-16 |
| Record source key length | 512 characters | U00-27 |
| Canonical JSON depth | 64 | U00-29 |
| Token size | 16–64 bytes | U00-33 |
| Log field length | 2,000 characters | U00-43 |
| Log line size | 16,384 bytes | U00-43 |
| Log value nesting | 4 | U00-41, U00-42 |
| Log file retry interval | 60 s | U00-40 |
| Module length | 400 lines default | U00-55 |

## 11. Test specification

Locations: `tests/unit/core/` (`test_errors.py`, `test_time.py`, `test_ids.py`, `test_numbers.py`, `test_log_pipeline.py`, `test_logging.py`), `tests/unit/core/conftest.py` (fixture `configured_logging(tmp_path)`: calls `configure_logging("DEBUG", log_dir=tmp_path, scrubber=<pass-through processor that replaces the sentinel "SENTINEL-SECRET-9f3a" with "***">, strict_event_names=True)`, yields, then `reset_logging()`), `tests/unit/tools/` (one file per check script, inputs built under `tmp_path`), `tests/unit/repo/` (static checks of `pyproject.toml`, workflows and hygiene files), `tests/integration/repo/` (subprocess runs of the real tools), `tests/bench/test_core_bench.py`. Every test's docstring first line starts with its ID. Time is frozen with `freezegun` or by monkeypatching `herness.core.time.now`; randomness is seeded or monkeypatched.

### 11.1 Unit tests (marker `unit`)

| ID | Under test | Setup | Action | Expected |
|----|-----------|-------|--------|----------|
| UT00-01 | U00-02, U00-03 | none | Inspect `__bases__` of every taxonomy class | Each direct parent equals design 00 §7, and `NotFound`'s parent is `RecoverableError` (R-19); exactly 3 category and 18 leaf classes; `RetryableError`, `RecoverableError`, `FatalError` subclass `HernessError` |
| UT00-02 | U00-01 | none | Construct with a 1,500-character message, context `{"job_id": "job_x", "obj": object(), "long": "a"*300}` | `message` is 1,000 characters plus `…`; `context["obj"] == "<object>"`; `context["long"]` 200 characters plus `…`; `str(e) == e.message`; `context` is read-only |
| UT00-03 | U00-04 | none | `RateLimited(..., retry_after=x)` for x in `7.0`, `-3`, `nan`, `inf`, `None` | `7.0`, `0.0`, `None`, `None`, `None` |
| UT00-04 | U00-05 | none | Construct with aware non-UTC `retry_at` and with naive `retry_at` | Stored as UTC; naive read as UTC; `key` kept |
| UT00-05 | U00-06 | none | `category` of 100 characters and `None` | First 64 characters; `None` |
| UT00-06 | U00-07 | none | Call on one instance of each class and on `ValueError()` | `retryable`/`recoverable`/`fatal` per category; `unknown` for `HernessError` and `ValueError` |
| UT00-07 | U00-08 | `CircuitOpen` with context `{"error_type": "x", "source": "jira"}` | `to_log_fields` | Contains `error_type="CircuitOpen"`, `error_kind="retryable"`, `error_message`, `key`, `retry_at` ISO text, `ctx_error_type="x"`, `source`, `cause_type=None` |
| UT00-08 | U00-01 | one instance of every class with context and extra attributes | `pickle.loads(pickle.dumps(e))` (`# noqa: S301` in the test, reason: proves multiprocessing transport) | Same class, message, context and extra attributes |
| UT00-09 | U00-09 | none | `now()` | `tzinfo is datetime.UTC` |
| UT00-10 | U00-10 | monkeypatch `time.sleep` to record | `sleep(0.5)`; `sleep(-1)`, `sleep(nan)`, `sleep(3601)` | Recorded 0.5; three `SchemaViolation` |
| UT00-11 | U00-11 | monkeypatch `asyncio.sleep` | `await asleep(0.1)`; `await asleep(-1)` | Awaited 0.1; `SchemaViolation` |
| UT00-12 | U00-12 | none | 1,000 successive calls | Non-decreasing |
| UT00-13 | U00-13 | none | `+02:00` datetime; naive datetime; a `date` | UTC equivalent; `SchemaViolation`; `SchemaViolation` |
| UT00-14 | U00-14 | none | Format `datetime(5, 1, 2, 3, 4, 5, 6, UTC)` and a `+05:30` value | `"0005-01-02T03:04:05.000006Z"`; converted to UTC; length 27 |
| UT00-15 | U00-15 | none | Parse valid text; 26- and 28-character text; missing `Z`; `2026-02-30T00:00:00.000000Z` | Round trip; `SchemaViolation` for each invalid input; error context holds no input text |
| UT00-16 | U00-16 | none | `"2026-09-24"`, `"2026-09-24T10:00:00Z"`, `"2026-09-24T10:00:00+02:00"`, `"2026-09-24T10:00:00"`, 65-character text, `""` | Midnight UTC; 10:00 UTC; 08:00 UTC; `SchemaViolation` × 3 |
| UT00-17 | U00-17 | none | `utc_day` of 23:30 at `-02:00` | Next day's date |
| UT00-18 | U00-18 | none | `"Europe/London"`, `"Mars/Base"`, `"../etc/passwd"`, `""` | `ZoneInfo`; `ConfigError` × 3 with `zone` in context |
| UT00-19 | U00-20 | monkeypatch `time.time_ns` to a fixed value | `new_ulid()` | 26 characters matching `_ULID_RE`; decoding the first 10 characters gives the fixed milliseconds |
| UT00-20 | U00-20 | fixed `time_ns`; seeded `secrets.randbits` via monkeypatch | Two calls | Second equals first with random part + 1; string order increasing |
| UT00-21 | U00-20 | fixed `time_ns`; `randbits` returns `2**80 - 1` | Two calls | Second has timestamp + 1 ms |
| UT00-22 | U00-20 | 8 threads | Each generates 10,000 ULIDs | 80,000 unique values; each thread's list strictly increasing |
| UT00-23 | U00-21, U00-22 | none | `new_id(k)` for every `IdKind` | Prefix per design 00 §5 table; `is_valid_id(k, id)` true |
| UT00-24 | U00-23 | none | `new_build_id(datetime(2026, 9, 24, 21, 14, 3, tzinfo=UTC))`; naive input | Starts `20260924-211403-`, 22 characters, suffix Crockford; `SchemaViolation` |
| UT00-25 | U00-24 | none | Table: valid ULID; lowercase; 25 and 27 characters; first char `8`; contains `I`, `L`, `O`, `U`; non-string | Only the first is true |
| UT00-26 | U00-25 | none | `run_<ulid>` checked as RUN and as TASK; `run_` + bad ULID; `12` | true, false, false, false |
| UT00-27 | U00-26 | none | Valid ID; `20261340-000000-ABCDEF`; wrong separators; lowercase suffix | true; false × 3 |
| UT00-28 | U00-27, U00-34 | none | Valid parts; `Source`; entity with `-`; key with `\n`; key of 513 characters; key with leading space | Joined string; `SchemaViolation` × 5 with `part` |
| UT00-29 | U00-28 | none | `"monitoring:event:prometheus:abc:1"`; `"a:b"` | `("monitoring", "event", "prometheus:abc:1")`; `SchemaViolation` |
| UT00-30 | U00-29 | none | Value with nested dict (unsorted keys), `Decimal("12.50")`, aware datetime, `date`, `PurePosixPath`, `StrEnum`, tuple; then `nan`, `{1: 2}`, `{1, 2}`, `b"x"`, naive datetime, depth 65 | Exact expected string with sorted keys and no spaces; `SchemaViolation` for each invalid input |
| UT00-31 | U00-31 | none | `"  SELECT\n\t1 ;; "`, `"SELECT 1"`, `";"` | `"SELECT 1"`, `"SELECT 1"`, `""` |
| UT00-32 | U00-32 | none | Known vector: compute expected in the test with `hashlib` and `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`; then empty SQL; bad build ID | Equal to `q_` + 16 hex; `SchemaViolation` × 2 |
| UT00-33 | U00-30 | none | `sha256_hex("é")` and `sha256_hex("é".encode())` | Equal; 64 lowercase hex |
| UT00-34 | U00-33 | none | `new_token()`, `new_token(16)`, `new_token(15)`, `new_token(65)` | 43 characters URL-safe; 22 characters; `SchemaViolation` × 2 |
| UT00-35 | U00-36, U00-42 | `configure_logging("INFO")`, capture stderr with `capsys` | `get_logger("core.test").info("core.test.done", n=1)` | One JSON line with `ts`, `level="info"`, `event`, `component="core.test"`, `pid`, `n` |
| UT00-36 | U00-36, U00-40 | fixture `configured_logging`; `now` patched to 23:59:59.9 then 00:00:00.1 next day | Log one line at each time | Two files `herness-<day>.jsonl` and `herness-<day+1>.jsonl`, one line each |
| UT00-37 | U00-36 | `tmp_path` | `configure_logging("VERBOSE")`; `configure_logging(log_dir=tmp_path)` without scrubber; `log_dir` under a regular file | `ConfigError` × 3 |
| UT00-38 | U00-37 | fixture | Nested `bind_ids(run_id=r1)` then `bind_ids(task_id=t1)`; two asyncio tasks binding different `run_id`s | Inner lines carry both IDs; after exit only `run_id`; each task's lines carry its own `run_id` |
| UT00-39 | U00-37 | fixture | `bind_ids(user="x")`; `bind_ids(run_id="run_bad\n{")` | `SchemaViolation` × 2; value not in the error context |
| UT00-40 | U00-38 | none | `get_logger("Harness")`, `get_logger("a" * 65)`, `get_logger("harness.tool")` | `SchemaViolation` × 2; logger bound with `component` |
| UT00-41 | U00-43, U00-42 | fixture | Log with extra fields `zeta`, `alpha` and bound `job_id` | Key order `ts, level, event, component, pid, job_id, zeta, alpha` |
| UT00-42 | U00-43 | fixture (strict) and a non-strict configuration | Log `"Bad Event"` | Strict: `SchemaViolation` raised to caller; non-strict: line with `event_name_invalid: true` |
| UT00-43 | U00-36, U00-42 | `configure_logging("INFO")` | `logging.getLogger("httpx").info(...)`, `.warning(...)`; `logging.getLogger("thirdparty.sub").warning(...)` | httpx INFO dropped; WARNING line with `component="ext.httpx"`; `component="ext.thirdparty"` |
| UT00-44 | U00-36 (exception step) | fixture | `log.exception` inside a function holding a local `password_local="SENTINEL-SECRET-9f3a"` | `exception` present, no `locals` key, sentinel absent |
| UT00-45 | U00-39 | fixture configured | `reset_logging()` | Root has no Herness handler; file handle closed (file can be deleted on Windows); context empty |
| UT00-46 | U00-36 | fixture | Read first line of the file | `event == "core.logging.configured"` with `scrubber: true` |
| UT00-47 | U00-42 | none | `normalize_values` on `Decimal`, `Path`, `SecretStr("x")`, naive datetime, set, nesting depth 6, object whose `__str__` raises `ValueError` | `"1.5"`, POSIX text, `"**********"`, suffix `" naive"`, sorted list, depth-5 value as `str`, `"<unprintable ...>"` |
| UT00-48 | U00-44, U00-45 | repository as is | Import `herness.core.types`; compare `__all__` with `TYPE_OWNERS` names of existing owner submodules | Equal; `__init__` AST contains no class, def or other assignment |
| UT00-49 | U00-47 | tmp tree with `_ownership.py` and `harness.py` missing `NumberRef` | Run `main(["--root", tmp])` | Exit 1; `OWN010` naming `NumberRef` |
| UT00-50 | U00-47 | tmp `harness.py` defining public `Extra` and `Finding` | Run | `OWN011` for `Extra`, `OWN012` for `Finding` |
| UT00-51 | U00-47 | tmp `swarm.py` importing `duckdb`, `herness.core.config`, and `herness.core.types.memory` | Run | Three `OWN020` |
| UT00-52 | U00-47 | tmp tree with no owner submodules | Run | Exit 0; six `INFO pending owner` lines |
| UT00-53 | U00-47 | tmp `herness/harness/x.py` with `from herness.core.types.harness import NumberRef` | Run | `OWN041` |
| UT00-54 | U00-46, U00-47 | tmp `harness.py` defining `Tracer`; tmp `herness/harness/other.py` defining `class ModelChain` | Run | `OWN043` and `OWN042` |
| UT00-55 | U00-48 | installed package | Import `herness` | `__version__ == importlib.metadata.version("herness")` |
| UT00-56 | U00-49 | parse `pyproject.toml` with `tomllib` | Compare dependency lists with table 14.1 | Every runtime and dev row present with the stated bound; extras `ner`, `pdf`; script `herness`; `Private :: Do Not Upload` classifier |
| UT00-57 | U00-50, U00-51, U00-53 | parse `pyproject.toml` | Inspect tool tables | ruff `select` equals the ENG §3.1 list plus `TID251`, `ICN003`; `max-complexity = 10`; `max-args = 6`; banned APIs present; mypy `strict = true`, `files` covers `herness`, `app`, `tools`; six pytest markers; `--strict-markers`; coverage `branch = true` |
| UT00-58 | U00-52 | parse `pyproject.toml`; list top-level packages under `herness/` and modules of `herness/core/` | Compare with contracts C1–C6 | Every existing package of the ENG §2.1 table (including `herness.admin`, R-07) is in C1; every existing core module named in C3/C4 (including `herness.core.numbers`) appears; no contract lists a missing module; C1 `ignore_imports` holds exactly the settings exception; C6 does not exempt `herness.core.ids` (R-03) |
| UT00-59 | U00-55 | tmp tree: `default = 400`, override 220 for `a.py`, doc module map with 150 for `b.py`; files of 221, 151, 400 lines | Run | `MS001` for `a.py` and `b.py`; the 400-line file passes; exit 1 |
| UT00-60 | U00-55 | tmp: override `limit = 500` with empty reason; two docs giving `c.py` 100 and 120 | Run | `MS003` and `MS002` |
| UT00-61 | U00-56 | tmp doc referencing a spec-00 unit ID that no doc defines (the test builds the ID string at run time) | Run | `TR001` |
| UT00-62 | U00-56 | tmp doc defining `T00-01` twice | Run | `TR002` |
| UT00-63 | U00-56 | tmp `00-x.impl.md` defining `U05-01` | Run | `TR003` |
| UT00-64 | U00-56 | tmp threat row without an `ST` ID | Run | `TR004` |
| UT00-65 | U00-56 | tmp test row `UT00-07` not on any card | Run | `TR005` |
| UT00-66 | U00-56 | tmp tests: function `test_ut00_99_x`; two functions carrying `UT00-01`; one function with `UT00-01` and `UT00-02` | Run | `TR006`, `TR007`, `TR010` |
| UT00-67 | U00-56 | tmp doc containing a cross-spec placeholder (the test builds the text `X:` + two digits + `/` + a symbol at run time) | Run | `TR008` |
| UT00-68 | U00-56 | tmp doc defining `UT00-01`, `UT00-02`; code carries `UT00-01` | Run with `--require-implemented 00` | `TR009` for `UT00-02` only |
| UT00-69 | U00-57 | fixture JSON: pip-audit finding `PYSEC-1` alias `GHSA-a`, osv finding `GHSA-a` severity `8.1` | Run | One merged finding with both IDs, severity 8.1, fix available |
| UT00-70 | U00-58 | none | Evaluate `"MIT OR GPL-3.0-only"`, `"(Apache-2.0 AND BSD-3-Clause)"`, `"Apache-2.0 WITH LLVM-exception"`, `"GPL-3.0-only"`, `"MIT License"` (alias) | allowed, allowed, allowed, denied, allowed |
| UT00-71 | U00-01, U00-03, U00-08 | none | `NotFound("run not found", hint="h" * 600, details={"a b": "x", "run_id": "run_x", "n": 5} plus 60 further entries, run_id="run_x")`; then `to_log_fields`; then `pickle` round trip (`# noqa: S301`, as UT00-08); then `ConfigError("x")` without hint or details | `hint` is 500 characters plus `…`; `details` has 50 entries; key `"a b"` stored as `key_0`; value `5` stored as `"<int>"`; `error_kind` is `recoverable`; `to_log_fields` holds `error_hint` and `detail_run_id`; the rebuilt object has equal `hint` and `details`; the plain `ConfigError` has `hint is None` and empty `details` |
| UT00-72 | U00-47 | tmp tree: package form `herness/core/types/harness/` with `__init__.py` importing every 05 `TYPE_OWNERS` name from `llm.py` and `evidence.py`, which define them; second tmp tree with both `harness.py` and `harness/`; third with a `class Extra` in the package `__init__.py` | Run `main(["--root", tmp])` on each | First: exit 0 (no `OWN010`); second: `OWN003`; third: `OWN033` |
| UT00-73 | U00-47 | tmp `herness/connectors/settings.py` importing `pydantic`, `typing`, `herness.core.errors`, `herness.core.types`, `httpx`, `herness.core.config` and `herness.core.types.harness` | Run | Exactly three `OWN050`, naming `httpx`, `herness.core.config` and `herness.core.types.harness` |
| UT00-74 | U00-64, U00-65, U00-66 | none | `parse_markers("a [[n1]] b [[n12]] [[x1]] [[]] [[n 1]] [[n1]]")` | Markers `n1`, `n12`, `n1` with their offsets; `ids == ("n1", "n12", "n1")`; malformed `x1`, empty text and `n 1` with offsets |
| UT00-75 | U00-67 | the five default patterns of design 09 §7 | Compile them; then `[]`, a list of 51 patterns, `["("]`, a 201-character pattern, `[5]`, and the bare string `"\d{4}"` | Five compiled patterns in order; `ConfigError` for each invalid input, with `count` or `index` in the context and no pattern text |
| UT00-76 | U00-68 | default patterns compiled | `find_uncited("In Q3 2026 cost rose to [[n1]] from 1,200 on 2026-09-24 for INC0012345 and PAY-123 (up 12 %) in 2025.", allowed)` | Exactly two hits, `1,200` and `12 %`, with offsets into the original text; the year, quarter, date and record IDs are exempt |
| UT00-77 | U00-68 | default patterns compiled | Scan `"up ²"`, `"about ½"`, `"٣ tickets"` (Arabic-Indic digit), `"[[12]] items"`, and a 100,001-character text of letters | One hit each: `²`, `½`, `٣`, `12`; the long text gives one hit `<text too long>` with `start == 100000` and `end == 100001` |
| UT00-78 | U00-69, U00-70 | none | `format_value` for: `usd` `"-1250000"`; `usd_compact` `1840000`, `12500`, `950`; `int` `1204`; `pct1` `42`; `ratio2` `0.3749`; `hours1` `3.46`; `minutes0` `45.4`; `prob2` `0.805`; `plain` `7.0` and `0.12344`; and `format_number` of a frozen test object with `value="1250000.00"`, `unit="usd"`, `format=None` | `-$1,250,000.00`; `$1.84M`, `$12.5K`, `$950`; `1,204`; `42.0%`; `0.37`; `3.5 h`; `45 min`; `0.80`; `7`, `0.1234`; `$1.25M` |
| UT00-80 | U00-45 | import `herness.core.types._ownership` | Inspect the tables | `TYPE_OWNERS` maps `QuestionType` and `Entity` to `"03"` (RQ-03) and holds every name listed in U00-45 under its owner; no name appears twice or also in `DECLARED_ELSEWHERE`; `impact_usd` and `JobContext` are absent; `OWNER_IMPORTS["06"] == {"harness", "jobs"}`; the `OWNER_IMPORTS` graph is acyclic |
| UT00-81 | U00-46, U00-47 | tmp tree with `herness/core/jobs/ports.py` defining `class JobContext(Protocol)` and `herness/core/resilience/chain.py` defining `class ModelChain`; second tree with `herness/harness/other.py` defining `class JobContext` | Run `main(["--root", tmp])` on each | First: no `OWN042`; second: `OWN042` for `JobContext` |
| UT00-79 | U00-64, U00-69 | none | `format_value` with unit `score` and no format; `usd_compact` of `999960` and `999.6`; `True`; `"abc"`; `float("nan")`; `None`; format `"pct3"`; `int` of `-0.001`; `plain` of `1e22` | `plain` rule applied; `$1.00M`, `$1.0K`; `n/a` × 5; `0`; `10,000,000,000,000,000,000,000` (no exponent) |

### 11.2 Property tests (marker `unit`, Hypothesis)

| ID | Under test | Strategy | Property |
|----|-----------|----------|----------|
| PT00-01 | U00-01, U00-08 | text messages, dicts of identifier keys to scalars or arbitrary objects | `to_log_fields` output is `json.dumps`-able; `len(error_message) <= 1001` |
| PT00-02 | U00-14, U00-15 | aware datetimes from 0001 to 9999 with random offsets | `parse_utc(format_utc(d)) == d.astimezone(UTC)`; for pairs, text order equals time order |
| PT00-03 | U00-27, U00-28 | valid sources, entities, keys (printable, may include `:`) | `split_record_id(make_record_id(s, e, k)) == (s, e, k)` |
| PT00-04 | U00-29 | JSON-like nested dicts | Shuffling key insertion order gives identical output; `json.loads(output)` equals the input's canonical conversion |
| PT00-05 | U00-31, U00-32 | SQL-like text with inserted whitespace runs and trailing `;` | `query_id` unchanged by whitespace and trailing semicolons; differs when `build_id` differs |
| PT00-06 | U00-69 | finite `Decimal`, `int` and `float` values from −1e15 to 1e15, every member of `NUMBER_FORMATS`, units from design 00 §12.1 | Output is `n/a` or uses only the U00-69 alphabet; contains no `E` or `e`; two calls give identical output |
| PT00-07 | U00-66, U00-68 | texts built from ASCII words, valid markers `[[nK]]`, years 1900–2099 and ISO dates, joined by single spaces; then one integer 0–99999 inserted as a separate word at a random word boundary | Before insertion `find_uncited` returns no hit; after insertion it returns exactly one hit whose `text` is the inserted integer and whose `start` is its offset in the new text |

### 11.3 Fault tests (marker `unit`; OS-level faults via `unittest.mock`, allowed by ENG §6)

| ID | Under test | Setup | Action | Expected |
|----|-----------|-------|--------|----------|
| FT00-01 | U00-40 | `DailyJsonlHandler` with `open` patched to raise `OSError` for the first call; `monotonic` patched | Emit 3 lines at t=0, t=10 s, t=61 s | stderr has one `core.logging.sink_failed`; the file (opened at t=61) begins with `core.logging.sink_recovered` with `dropped_lines = 2`, then the third line |
| FT00-02 | U00-20 | `time.time_ns` returns t, then t − 5 ms | Two `new_ulid` calls | Second value greater than the first (timestamp held at t) |

### 11.4 Security tests

| ID | Threat | Marker | Setup | Action | Expected |
|----|--------|--------|-------|--------|----------|
| ST00-01 | TH00-01 | unit | fixture `configured_logging` (sentinel scrubber) | Log the sentinel inside a field value, inside an exception message, as a field placed so it straddles character 2,000 of a long string | Sentinel appears in neither the file nor stderr; `***` present |
| ST00-02 | TH00-02 | unit | `configure_logging("DEBUG")` then `("INFO")`, stderr captured | Log fields `prompt`, `description`, `api_key`, `nested={"access_token": "t"}` | At INFO all four `[omitted]`; at DEBUG `prompt` and `description` kept, `api_key` and `access_token` `[omitted]` |
| ST00-03 | TH00-03 | unit | fixture | Log a value `'x"}\n{"level":"critical","event":"core.fake.injected"'` | Exactly one line in the file; `json.loads` of it succeeds; no line has `event == "core.fake.injected"` |
| ST00-04 | TH00-04 | unit | fixture | Log a 100,000-character field and 20 fields of 1,500 characters | Field cut to 2,000 characters plus marker; line ≤ 16,384 bytes with `dropped_fields` listing removed keys; required keys present |
| ST00-05 | TH00-05 | unit | none | 100,000 `new_token()` calls | All unique; no two consecutive tokens decode to integers differing by less than 2**64 |
| ST00-06 | TH00-12 | unit | parse both workflow files with `yaml.safe_load` | Inspect | Every third-party `uses:` pinned to 40 hex; no `pull_request_target`; top-level `permissions` is `contents: read`; no `run:` contains `${{ github.event.`, `${{ github.head_ref` or `${{ inputs.`; no `self-hosted` runner; every checkout has `persist-credentials: false` |
| ST00-07 | TH00-11 | unit | fixture JSON inputs and ignore TOML | Cases: High with fix; High without fix; unknown severity with fix; ignored unexpired; ignored expired (`--today` after `expires`) | exit 1 with `AU001`; exit 0 with `AU002`; exit 1 `AU001`; exit 0 `ignored`; exit 1 `AU003` |
| ST00-08 | TH00-13 | unit | parse `release.yml` | Inspect | Trigger only tag push `v*.*.*`; `build` needs `ci`; `build` has `id-token: write` and `attestations: write` and uses `attest-build-provenance` on wheel and sdist and `attest-sbom`; tag-verification and version-match steps precede `uv build`; no other job has `id-token: write` |
| ST00-09 | TH00-08 | integration | tmp copy of `pyproject.toml`; tmp file `herness/x.py` with `import pickle`, `yaml.load(s)`, `httpx.Client()`, `from herness.core.time import now`; tmp file `herness/connectors/y.py` with `httpx.AsyncHTTPTransport()` and `httpx.get(url)` | `ruff check --config <tmp pyproject> <files>` via subprocess | `herness/x.py`: `TID251` × 3 and `ICN003`; `herness/connectors/y.py`: `TID251` × 2 (connectors have no exception, R-06); exit non-zero |
| ST00-10 | TH00-08 | integration | Copy `herness/` and `pyproject.toml` to `tmp_path`; add `import herness.harness` (created as an empty package in the copy, and appended to C1) to the copied `herness/core/errors.py` | `lint-imports` via subprocess in `tmp_path` | Non-zero exit naming contracts C1 and C4 |
| ST00-11 | TH00-10 | unit | read `.env.example` and `.gitignore` | Inspect | Every `KEY=value` line has an empty value; no key starts with `HERNESS_SECURITY__`; `.gitignore` contains `.env`, `!.env.example`, `data/` |
| ST00-12 | TH00-10 | integration | tmp file containing a fake private-key header and an AWS-style key literal built at runtime | `detect-secrets-hook --baseline <copy of .secrets.baseline> <file>` via subprocess | Non-zero exit; finding reported |
| ST00-13 | TH00-06 | unit | `SourceUnavailable("fetch failed")` raised `from` a `ValueError("https://x/?token=abc")`; and a bare `RuntimeError("password=abc")` | `to_log_fields` of each | Neither output contains `abc`; `cause_type == "ValueError"`; `error_message == ""` for the `RuntimeError` |
| ST00-14 | TH00-07 | unit | none | `parse_utc` of `"2026-01-01T00:00:00.000000Z' OR 1=1"`, 10,000-character string, `"２０２６-..."` (full-width digits); `parse_iso` of the same | `SchemaViolation` for all; no input text in the error context |
| ST00-15 | TH00-09 | unit | tmp tree with owner `harness.py` defining `NumberRef` and `herness/metrics/x.py` defining `class NumberRef(BaseModel)` | Run `check_type_ownership` | Exit 1 with `OWN040` at `herness/metrics/x.py` |
| ST00-16 | TH00-11 | unit | tmp SBOM with components: `MIT`; `GPL-3.0-only`; no licence; `nvidia-cublas-cu12` proprietary; `pillow` approved in tmp pyproject | Run `check_licences` in enforce and report modes | Enforce: exit 1 with `LC001` for GPL and unknown, `LC002` for nvidia, pillow `approved`; report: exit 0 |
| ST00-17 | TH00-14 | unit | none | `query_id` with params `{"a": 1}` vs `{"a": "1"}` vs `{"a": 1.0}`; params `{"a": [1, 2]}` vs `{"a": [2, 1]}` | All four IDs distinct from each other where the JSON differs (`1`, `"1"`, `1.0`, list order) |
| ST00-18 | TH00-15 | unit | default patterns compiled | `find_uncited` on each evasion text: `"cost [[1,250]] USD"`; full-width `"１２ tickets"`; `"³ outages"`; `"Ⅻ teams"`; `"[[n1]]12 more"`; `"1​200 users"` (zero-width space); `"INC 42"`; a 100,050-character text whose only numeral is at offset 100,010 | Every text gives at least one hit; no hit lies inside a valid marker; the long text's hit is `<text too long>` |

### 11.5 Integration tests (marker `integration`)

| ID | Under test | Setup | Action | Expected |
|----|-----------|-------|--------|----------|
| IT00-01 | U00-54, F00-06 | clean checkout with dev dependencies | `uv run pre-commit run --all-files` via subprocess (timeout 600 s) | Exit 0 |
| IT00-02 | U00-47, U00-55, U00-56 | repository as is | Run the three scripts' `main` with `--root .` | Each returns 0 (traceability run after the consistency pass has removed `X:` references) |

### 11.6 Benchmarks (markers `integration` and `slow`, `tests/bench/`)

| ID | Under test | Setup | Action | Expected |
|----|-----------|-------|--------|----------|
| BT00-01 | U00-20 | reference PC | 1,000,000 `new_ulid` calls | ≥ 200,000 per second |
| BT00-02 | U00-32 | 2 KB SQL, 10 params | 10,000 `query_id` calls with `pytest-benchmark` | p95 < 50 µs |
| BT00-03 | U00-36, U00-40 | file handler only, pass-through scrubber | 10,000 INFO lines, 10 fields each | < 1.0 s total |
| BT00-04 | U00-56 | repository | `check_traceability.main` | < 5 s |
| BT00-05 | U00-68 | 100,000-character synthetic text of §10, five default patterns, reference PC | 100 `find_uncited` calls with `pytest-benchmark` | median < 100 ms |

## 12. Task cards

All cards are Phase 1. Test files are not counted as production files. `uv.lock` and `.secrets.baseline` are generated artifacts and not counted.

#### T00-01 Project metadata and package roots

| Field | Content |
|-------|---------|
| Goal | `pyproject.toml` with metadata, dependencies, uv, build, ruff (including the R-06 egress bans), mypy, pytest and coverage configuration exists, `uv.lock` is generated, and `herness` is an importable typed package installed in editable mode for development (R-58). |
| Depends on | none |
| Units | U00-48, U00-49, U00-50, U00-51, U00-53 |
| Files | `pyproject.toml`, `herness/__init__.py`, `herness/core/__init__.py`, `herness/py.typed` (plus generated `uv.lock`) |
| Tests | UT00-55, UT00-56, UT00-57, ST00-09 |
| Threats | TH00-08 (ruff bans), TH00-11 (lock) |
| Acceptance checks | `uv lock` succeeds and `uv.lock` is committed; `uv sync --frozen --all-extras` succeeds on Windows and Linux; `uv run ruff check .` and `uv run ruff format --check .` report 0 issues; `uv run mypy` reports 0 errors; `uv run pytest -k "UT00_55 or UT00_56 or UT00_57 or ST00_09"` passes |
| Blocked by | O-05 (default applies: cu128 index) |
| Size | M |

#### T00-02 Repository hygiene files

| Field | Content |
|-------|---------|
| Goal | `.gitignore`, `.gitattributes`, `.env.example` and `README.md` exist with the U00-61 … U00-63 contents. |
| Depends on | T00-01 |
| Units | U00-61, U00-62, U00-63 |
| Files | `.gitignore`, `.gitattributes`, `.env.example`, `README.md` |
| Tests | ST00-11 |
| Threats | TH00-10 |
| Acceptance checks | `uv run pytest -k ST00_11` passes; `git check-ignore data/x .env` prints both paths; `git check-ignore .env.example` prints nothing; README contains the 12 sections of U00-63 in order |
| Blocked by | O-04 (default licence text applies) |
| Size | S |

#### T00-03 Error taxonomy

| Field | Content |
|-------|---------|
| Goal | `herness.core.errors` implements design 00 §7 plus `NotFound`, `hint` and `details` (R-19), `error_kind` and `to_log_fields`. |
| Depends on | T00-01 |
| Units | U00-01, U00-02, U00-03, U00-04, U00-05, U00-06, U00-07, U00-08 |
| Files | `herness/core/errors.py` |
| Tests | UT00-01, UT00-02, UT00-03, UT00-04, UT00-05, UT00-06, UT00-07, UT00-08, UT00-71, PT00-01, ST00-13 |
| Threats | TH00-06 |
| Acceptance checks | `uv run pytest tests/unit/core/test_errors.py` passes; `uv run mypy` 0 errors; `herness/core/errors.py` ≤ 340 lines; line and branch coverage of the file ≥ 90 % / 85 % |
| Blocked by | none |
| Size | M |

#### T00-04 Time helpers

| Field | Content |
|-------|---------|
| Goal | `herness.core.time` provides the clock, bounded sleeps, fixed-width UTC text and zone lookup. |
| Depends on | T00-03 |
| Units | U00-09, U00-10, U00-11, U00-12, U00-13, U00-14, U00-15, U00-16, U00-17, U00-18 |
| Files | `herness/core/time.py` |
| Tests | UT00-09, UT00-10, UT00-11, UT00-12, UT00-13, UT00-14, UT00-15, UT00-16, UT00-17, UT00-18, PT00-02, ST00-14 |
| Threats | TH00-07 |
| Acceptance checks | `uv run pytest tests/unit/core/test_time.py` passes; mypy 0 errors; file ≤ 200 lines; coverage ≥ 90 % line / 85 % branch |
| Blocked by | none |
| Size | M |

#### T00-05 Identifiers, canonical JSON and `query_id`

| Field | Content |
|-------|---------|
| Goal | `herness.core.ids` provides ULIDs, prefixed IDs, `build_id`, `record_id`, canonical JSON, SHA-256, `query_id` and tokens. |
| Depends on | T00-04 |
| Units | U00-19, U00-20, U00-21, U00-22, U00-23, U00-24, U00-25, U00-26, U00-27, U00-28, U00-29, U00-30, U00-31, U00-32, U00-33, U00-34 |
| Files | `herness/core/ids.py` |
| Tests | UT00-19, UT00-20, UT00-21, UT00-22, UT00-23, UT00-24, UT00-25, UT00-26, UT00-27, UT00-28, UT00-29, UT00-30, UT00-31, UT00-32, UT00-33, UT00-34, PT00-03, PT00-04, PT00-05, FT00-02, ST00-05, ST00-17, BT00-01, BT00-02 |
| Threats | TH00-05, TH00-14 |
| Acceptance checks | `uv run pytest tests/unit/core/test_ids.py` passes; `uv run pytest tests/bench/test_core_bench.py -k "BT00_01 or BT00_02"` meets §10 on the reference PC; mypy 0 errors; file ≤ 330 lines |
| Blocked by | none |
| Size | M |

#### T00-06 Log pipeline processors and file handler

| Field | Content |
|-------|---------|
| Goal | `herness.core._log_pipeline` provides the processors and the daily JSONL handler of §3.4. |
| Depends on | T00-05 |
| Units | U00-40, U00-41, U00-42, U00-43 |
| Files | `herness/core/_log_pipeline.py` |
| Tests | UT00-47, FT00-01, ST00-02, ST00-03, ST00-04 (processor-level variants run against the processors directly; the configured variants run in T00-07) |
| Threats | TH00-02, TH00-03, TH00-04 |
| Acceptance checks | `uv run pytest tests/unit/core/test_log_pipeline.py` passes; mypy 0 errors; file ≤ 330 lines |
| Blocked by | none |
| Size | M |

#### T00-07 Public logging API

| Field | Content |
|-------|---------|
| Goal | `herness.core.logging` configures structlog and standard logging, binds context IDs and emits `core.logging.configured`. |
| Depends on | T00-06 |
| Units | U00-35, U00-36, U00-37, U00-38, U00-39 |
| Files | `herness/core/logging.py` |
| Tests | UT00-35, UT00-36, UT00-37, UT00-38, UT00-39, UT00-40, UT00-41, UT00-42, UT00-43, UT00-44, UT00-45, UT00-46, ST00-01, BT00-03 |
| Threats | TH00-01, TH00-02, TH00-03, TH00-04 |
| Acceptance checks | `uv run pytest tests/unit/core/test_logging.py` passes; the three §8.1 events are asserted by UT00-46 and FT00-01; mypy 0 errors; file ≤ 260 lines; `herness/core` coverage ≥ 90 % line / 85 % branch |
| Blocked by | none (the real scrubber, T10-07 (herness.core.secrets.scrub_secrets), is not needed: tests use the sentinel scrubber) |
| Size | M |

#### T00-08 Shared-types package and ownership checker

| Field | Content |
|-------|---------|
| Goal | `herness/core/types/` exists with the empty re-export module, the ownership tables (R-01, R-02, RQ-03) and the static checker, which accepts module-form and package-form owner submodules and enforces the settings-module import rule (R-03). |
| Depends on | T00-01, T00-03 |
| Units | U00-44, U00-45, U00-46, U00-47 |
| Files | `herness/core/types/__init__.py`, `herness/core/types/_ownership.py`, `tools/__init__.py`, `tools/check_type_ownership.py` |
| Tests | UT00-48, UT00-49, UT00-50, UT00-51, UT00-52, UT00-53, UT00-54, UT00-72, UT00-73, UT00-80, UT00-81, ST00-15 |
| Threats | TH00-09, TH00-08 (settings rule) |
| Acceptance checks | `uv run python -m tools.check_type_ownership` exits 0 with six `INFO pending owner` lines; `uv run pytest tests/unit/core/test_types_ownership.py tests/unit/tools/test_check_type_ownership.py` passes; mypy 0 errors |
| Blocked by | none (DD-01 and DD-02 accepted by R-01 and R-02) |
| Size | M |

#### T00-09 Import-linter contracts

| Field | Content |
|-------|---------|
| Goal | `pyproject.toml` carries contracts C1–C6 for the modules that exist, including `herness.admin` (R-07), the named settings exception (R-03) and `herness.core.numbers` in C3/C4 once it exists. |
| Depends on | T00-07, T00-08 |
| Units | U00-52 |
| Files | `pyproject.toml` |
| Tests | UT00-58, ST00-10 |
| Threats | TH00-08 |
| Acceptance checks | `uv run lint-imports` exits 0; `uv run pytest -k "UT00_58 or ST00_10"` passes (ST00-10 shows a planted upward import is rejected) |
| Blocked by | O-01 (verify the pipe sibling syntax and `**` wildcard on the pinned import-linter; fallback in O-01) |
| Size | S |

#### T00-10 Module size check

| Field | Content |
|-------|---------|
| Goal | `tools/check_module_size.py` enforces default, override and module-map budgets. |
| Depends on | T00-08 (for `tools/__init__.py`) |
| Units | U00-55 |
| Files | `tools/check_module_size.py`, `pyproject.toml` (`[tool.herness.module_budgets]`) |
| Tests | UT00-59, UT00-60 |
| Threats | none |
| Acceptance checks | `uv run python -m tools.check_module_size` exits 0 on the repository; `uv run pytest tests/unit/tools/test_check_module_size.py` passes |
| Blocked by | none |
| Size | S |

#### T00-11 Traceability check

| Field | Content |
|-------|---------|
| Goal | `tools/check_traceability.py` implements checks TR001–TR010. |
| Depends on | T00-08 |
| Units | U00-56 |
| Files | `tools/check_traceability.py` |
| Tests | UT00-61, UT00-62, UT00-63, UT00-64, UT00-65, UT00-66, UT00-67, UT00-68, IT00-02, BT00-04 |
| Threats | none (supports ENG §5.1 step 3) |
| Acceptance checks | `uv run pytest tests/unit/tools/test_check_traceability.py` passes; `uv run python -m tools.check_traceability --require-implemented 00` exits 0 once T00-01 … T00-16 tests exist; IT00-02 passes |
| Blocked by | Consistency pass removing `X:` references from all implementation specs (TR008) |
| Size | M |

#### T00-12 Pre-commit hooks and secrets baseline

| Field | Content |
|-------|---------|
| Goal | `.pre-commit-config.yaml` runs the U00-54 hooks and `.secrets.baseline` is audited. |
| Depends on | T00-09, T00-10, T00-11 |
| Units | U00-54 |
| Files | `.pre-commit-config.yaml` (plus generated `.secrets.baseline`) |
| Tests | IT00-01, ST00-12 |
| Threats | TH00-10, TH00-11 |
| Acceptance checks | `uv run pre-commit install` installs pre-commit and pre-push hooks; `uv run pre-commit run --all-files` exits 0 in < 90 s with a warm mypy cache; committing a file with a fake private key is refused |
| Blocked by | none (hook 7 is inert until fixtures exist; T10-11 (herness.core.redact_scan.main)) |
| Size | S |

#### T00-13 Audit and licence gates

| Field | Content |
|-------|---------|
| Goal | `tools/check_audit.py` and `tools/check_licences.py` implement U00-57 and U00-58 with their configuration. |
| Depends on | T00-08 |
| Units | U00-57, U00-58 |
| Files | `tools/check_audit.py`, `tools/check_licences.py`, `tools/audit_ignore.toml`, `pyproject.toml` (`[tool.herness.licences]`) |
| Tests | UT00-69, UT00-70, ST00-07, ST00-16 |
| Threats | TH00-11 |
| Acceptance checks | `uv run pytest tests/unit/tools/test_check_audit.py tests/unit/tools/test_check_licences.py` passes; mypy 0 errors |
| Blocked by | none |
| Size | M |

#### T00-14 Hosted CI workflow

| Field | Content |
|-------|---------|
| Goal | `.github/workflows/ci.yml` runs `lint`, `types`, `test` (Ubuntu and Windows) and `audit` on the hosted runner, and the audit nightly. |
| Depends on | T00-12, T00-13 |
| Units | U00-59 |
| Files | `.github/workflows/ci.yml` |
| Tests | ST00-06 |
| Threats | TH00-11, TH00-12 |
| Acceptance checks | A pull request shows the five checks green; `uv run pytest -k ST00_06` passes; the `audit` artifact contains `pip-audit.json`, `osv.json`, `sbom.cdx.json` and `audit-summary.md`; branch protection on `main` requires the five checks (documented in README) |
| Blocked by | O-02 (osv-scanner flags verified on the pinned version), O-03 (report-only default applies) |
| Size | M |

#### T00-15 Release workflow

| Field | Content |
|-------|---------|
| Goal | `.github/workflows/release.yml` builds, gates, attests and publishes a release from a signed tag. |
| Depends on | T00-14 |
| Units | U00-60 |
| Files | `.github/workflows/release.yml` |
| Tests | ST00-08 |
| Threats | TH00-13 |
| Acceptance checks | `uv run pytest -k ST00_08` passes; a signed tag `v0.1.0` produces a GitHub release with wheel, sdist, SBOM and `duckdb/excel.duckdb_extension` with its `SHA256SUMS`; `gh attestation verify dist/herness-0.1.0-py3-none-any.whl --repo <owner>/<repo>` succeeds; an unsigned tag fails the `build` job before `uv build` |
| Blocked by | O-03 (licence approvals needed before the first release passes the enforce-mode gate) |
| Size | S |

#### T00-16 Shared numbers module

| Field | Content |
|-------|---------|
| Goal | `herness.core.numbers` provides marker parsing, the uncited-numeral scanner with its allowed-pattern compiler, and `NumberRef` display formatting, as the single implementation that impl 05, 06, 07, 09 and 11 import (R-16). |
| Depends on | T00-03, T00-09 |
| Units | U00-64, U00-65, U00-66, U00-67, U00-68, U00-69, U00-70 |
| Files | `herness/core/numbers.py`, `pyproject.toml` (per-file `A005` ignore; `herness.core.numbers` added to contracts C3 and C4) |
| Tests | UT00-74, UT00-75, UT00-76, UT00-77, UT00-78, UT00-79, PT00-06, PT00-07, ST00-18, BT00-05 |
| Threats | TH00-15 |
| Acceptance checks | `uv run pytest tests/unit/core/test_numbers.py` passes; `uv run pytest tests/bench/test_core_bench.py -k BT00_05` meets §10 on the reference PC; `uv run lint-imports` exits 0 with the module in C3 and C4; `uv run mypy` 0 errors; file ≤ 320 lines; line and branch coverage of the file ≥ 90 % / 85 % |
| Blocked by | none (the config key `reports.allowed_numeral_patterns` of impl 09 is not needed: tests pass the five design 09 §7 patterns directly) |
| Size | M |

## 13. Design deltas and open items

### 13.1 Design deltas and contradictions

Status of every delta and contradiction after the consistency pass. The rulings are in [`DECISIONS.md`](DECISIONS.md) (R-01 … R-76); design edits they require are pending per DECISIONS §9, and until then the ruling wins for implementation.

| # | Design spec | Change requested | Reason | Default until resolved | Status |
|---|-------------|------------------|--------|------------------------|--------|
| DD-01 | 00 §6 | Split the §6 ownership table: data models and pure protocols live in `herness.core.types`; the behavioral names `LoopHooks`, `HarnessHooks`, `GatedClient`, `Tracer` (05), `RunBudget` (06), `ModelChain`, `loop_signal_policy`, `JobContext` (08) are declared in their owner packages. | They hold state, do I/O or reference `LLMClient`/`ModelChain`, which ENG §2.1 forbids in `core.types`. | `DECLARED_ELSEWHERE` (U00-46) | Accepted (R-02) |
| DD-02 | 00 §3, §6 | `herness/core/types.py` becomes the package `herness/core/types/` with one submodule per owner; the import path `herness.core.types.<Name>` is unchanged. | Around 60 models cannot fit the 400-line module limit (ENG §2.4). | §3.5 structure | Accepted (R-01; ENG §14 E6) |
| DD-03 | 00 §5 | Name `herness.core.ids.query_id()` as the single implementation of `query_id` (as §5.1 does for `result_hash`), and define canonical JSON as in U00-29 (sorted keys, no whitespace, UTF-8, `Decimal` and dates as strings, non-finite numbers rejected). | Specs 04 §5.3 and 05 §5.4.5 both compute `query_id`; identical IDs require one implementation. Spec 10 §4.4 `config_hash` should use the same `canonical_json`. | U00-29, U00-31, U00-32 | Accepted (R-14) |
| DD-04 | 00 §5 | Define `<ulid6>` in `build_id` as the last 6 characters of a fresh ULID. | The first 6 characters are timestamp bits and would collide within ~4 minutes. | U00-23 | Still open (no ruling; design 00 §5 edit) |
| DD-05 | 11 §3.3, §5.2 | `FakeClock` also patches `herness.core.time.asleep`; callers use `from herness.core import time as clock` (ruff `ICN003` bans `from herness.core.time import ...`). | Async retry and swarm code sleeps through `asleep`; `from` imports defeat patching. | U00-11, U00-50 | Still open (no ruling; impl 11 and design 11) |
| DD-06 | 10 §5.2 | Name the module and signature of `scrub_secrets` (impl 10 names it `herness.core.secrets.scrub_secrets`, a structlog processor). | Design 10 names the processor but not its location; logging receives it by parameter. | U00-36 `scrubber` parameter | Resolved by impl 10: `herness.core.secrets.scrub_secrets`, a structlog processor (U10-32, T10-07); design 10 §5.2 edit pending per DECISIONS §9 |
| DD-07 | 07 §3.1 | Shared memory types move from `herness/harness/memory/types.py` to `herness/core/types/memory.py`; the memory package keeps only module-local types. | Contradiction C-01: design 00 §6 places them in `core.types`. | Checker rule OWN040 enforces design 00 | Resolved by R-01 (impl 07 now declares them in `herness.core.types.memory`) |
| DD-08 | 00 §9 | Add dev dependencies `types-PyYAML`, `types-jsonschema`, `types-psutil` (mypy stubs) and build backend `hatchling`, in addition to ENG §14 E3. | Needed for `mypy --strict` and `uv build`. | Table 14.1 | Still open (no ruling; design 00 §9 edit) |
| DD-09 | 11 §4.1, §8 | Benchmark files carry the `integration` marker plus `slow`. | Spec 11 requires exactly one of `unit`, `integration`, `fault`, `eval` per file, while §8 names only `slow` for benchmarks. | §11.6 | Still open (no ruling; impl 11) |
| DD-10 | 00 §7 | Add `NotFound(RecoverableError)` and the optional `HernessError` attributes `hint` and `details`. | Needed by several specs for missing objects and operator fixes. | U00-01, U00-03, U00-08 | Accepted (R-19; design 00 §7 edit pending per DECISIONS §9) |
| DD-11 | 00 §12.1 | Name `herness.core.numbers` as the single implementation of marker parsing, the numeral scanner and `NumberRef` formatting. | Specs 05, 06, 07, 09 and 11 each kept their own copy of the patterns; the Verifier and the renderer must decide identically. | §3.10 | Accepted (R-16) |
| C-01 | 00 §6 vs 07 §3.1 | Memory shared types declared in two places. | — | Resolved by DD-07 | Resolved by R-01 |
| C-02 | 10 §3.1 vs ENG §2.1 | `herness.core.config` (L0) imports section models from `herness/<package>/settings.py` in L2–L5, an upward import. | — | Contract C1 ignores exactly `herness.core.config -> herness.**.settings`; C6 and OWN050 keep settings modules leaf-only. | Resolved by R-03 (ENG §2.1 settings exception) |
| C-03 | 00 §6 vs 05 §3 / 06 §6.3 / 08 §3 | 00 §6 lists behavioral classes as `core.types` members; the owner specs place them in their packages. | — | Resolved by DD-01 | Resolved by R-02 |
| C-04 | 11 §4.2, §10.5 vs ENG §3.1 | mypy scope four packages vs all of `herness/`; hosted CI optional vs required. | — | ENG applies (E1, E2) | Still open (ENG §14 E1, E2 apply; design 11 edit pending per DECISIONS §9) |
| C-05 | R-01 vs impl 05 and impl 06 module maps | R-01 names one submodule per owner, but the 05 and 06 types exceed the 400-line limit; impl 05 first used four sibling files (`agent`, `evidence`, `llm`, `tooling`) directly under `herness/core/types/`. | — | Package-form submodules `herness/core/types/harness/` and `herness/core/types/swarm/` accepted by §3.5 and U00-47 | Resolved by R-01 (package form; impl 05 and 06 now use it) |
| C-06 | R-19 vs impl 09 U09-08 | Impl 09 raises `ReportContractError` with `details = {"where": [...], "rules": [...]}` (lists), but R-19 types `details` as `dict[str, str]`; U00-01 stores a non-string value as its type name. | — | U00-01 as specified | Resolved by R-74 (`details` values are strings; impl 09 joins lists with `, ` or passes a JSON string) |
| C-07 | R-02 vs impl 08 draft (`JobContext` in `herness.core.types`) | An earlier impl 08 draft declared `JobContext` and `ServiceControl` in `herness.core.types`. | — | `DECLARED_ELSEWHERE` names `herness.core.jobs`; package prefixes accepted (U00-46) | Resolved by R-02 (impl 08 now places them in `herness.core.jobs.ports`) |
| C-08 | R-16 vs impl 05 U05-65, impl 07 scanner, impl 09 U09-03/U09-05/U09-11, impl 06 U06-125 | Each spec defines its own `MARKER_RE`, `NUMERAL_RE`, scanner or `format_number`. | — | §3.10 is the single implementation | Resolved by R-16 (owners replace their copies with imports of `herness.core.numbers`) |
| C-09 | R-01 and ENG §2.1 vs impl 06 U06-08 | Impl 06 declares the function `impact_usd` in `herness.core.types.swarm`, but the types package holds data types only. | — | Not registered in `TYPE_OWNERS`; the checker reports `OWN011` for it until it moves (for example to `herness.harness.swarm`) or a ruling permits pure helper functions in the types package | Resolved by R-75 (`herness.core.types` holds no functions; `impact_usd` lives in `herness.harness`, impl 06) |
| C-10 | R-46 vs this spec's `tools/` scripts | R-46 sets exit codes for `herness` CLI commands. The CI check scripts of §3.7 are not CLI commands and use 0 pass, 1 violations, 2 usage or input error, which pre-commit and CI treat as failure for any non-zero code. | — | §3.7 codes | Resolved by R-73 (developer tools under `tools/` exit 0 pass, 1 findings, 2 usage; §3.7 aligned) |

### 13.2 Open questions inherited from design 00 §10

D1–D7 (mapping source, dollar weights, cadence, chat hours, hybrid approval, incident attribution, OpenJev on the target GPU) do not affect the foundation. Defaults as in `docs/specs/open-questions.md`; no card here is blocked by them.

Verification items from `docs/specs/open-questions.md` (b): item 1 is resolved; items 2–27 concern specs 01–11 and block no card here.

### 13.3 Open items of this spec

No ruling in `DECISIONS.md` covers O-01 … O-07; all seven are Still open with the defaults below.

| # | Item | Default until resolved | Blocks |
|---|------|------------------------|--------|
| O-01 | Confirm on the pinned import-linter (≥ 2.1) the pipe syntax for independent sibling layers and the `**` wildcard in `ignore_imports`. | Use them; if unsupported, express each of C1's and C3's sibling rules as a separate `independence` contract (`herness.eval` and `herness.admin`; `herness.connectors` and `herness.model`; `herness.core._log_pipeline` and `herness.core.types`; `herness.core.time` and `herness.core.numbers`), and list each existing settings module explicitly in `ignore_imports`. | T00-09 |
| O-02 | Confirm osv-scanner CLI flags for scanning a requirements lockfile and its JSON field `groups[].max_severity` on the pinned version. | Scan `build/requirements.lock.txt` as `requirements.txt`; missing `max_severity` counts as unknown (High) in U00-57. | T00-14 |
| O-03 | Record licence approvals for runtime dependencies outside the ENG §5.6 allowlist. Expected candidates, to be confirmed by the first SBOM: NVIDIA CUDA runtime wheels `nvidia-*` (proprietary), `filelock` (Unlicense), `pillow` (MIT-CMU), `pyphen` (GPL/LGPL/MPL-1.1 tri-licence, via `weasyprint`). | `nvidia-*` report-only; every other non-allowlisted licence fails the `audit` job until an `approved` entry with approver and date is added. | T00-14, T00-15 |
| O-04 | Herness's own licence. | `LicenseRef-Proprietary` with `Private :: Do Not Upload`; README states "Internal use only". | none |
| O-05 | CUDA version of the torch wheel index for the target GPU. | `cu128`. | none |
| O-06 | ENG §2.4 says more than 6 parameters must be keyword-only or a parameter object, but ruff `PLR0913` counts keyword-only parameters too. | `max-args = 6` as ENG's table states; a function needing more uses a parameter object or a listed `# noqa: PLR0913`. | none |
| O-07 | Exact `uv`, `hatchling` and GitHub action versions. | Latest releases at the time of T00-01, T00-12 and T00-14; recorded as exact pins or SHAs. | none |

Exceptions to ENG rules taken by this spec (ENG §2.4, §2.3, §8 item 3):

| Rule | Exception | Where |
|------|-----------|-------|
| ruff `A005` | Module names `logging`, `time`, `numbers`, `types` shadow the standard library | `herness/core/logging.py`, `time.py`, `numbers.py`, `types/__init__.py` (per-file ignore) |
| ruff `N818` | Error class names fixed by design 00 §7 | `herness/core/errors.py` |
| ruff `TRY003` | Global ignore; taxonomy errors take operation-specific messages | `pyproject.toml` |
| ruff `S301` | `pickle` used in UT00-08 and UT00-71 to prove multiprocessing transport | `tests/unit/core/test_errors.py` (`# noqa: S301`) |
| ruff `TID251` | `httpx` client and transport construction allowed (R-06) | `herness/core/egress.py` only |
| ENG §2.3 module state | ULID generator state; logging configuration state | `herness/core/ids.py`, `herness/core/logging.py`, `herness/core/_log_pipeline.py` |
| Stricter than ENG | Added ruff rules `TID251`, `ICN003` | `pyproject.toml` |

## 14. Dependencies

### 14.1 Third-party packages

Minimum versions from design 00 §9 unless noted; "—" means no lower bound (exact pin in `uv.lock`). Licences are those the packages publish; the licence gate (U00-58) is the check of record.

| Package | Scope | Minimum | Licence | Use |
|---------|-------|---------|---------|-----|
| `duckdb` | runtime | 1.3 | MIT | Warehouse (02) |
| `pyarrow` | runtime | 17 | Apache-2.0 | Lake, batches (01, 02) |
| `polars` | runtime | 1.10 | MIT | Dataframes (02, 04) |
| `numpy` | runtime | — | BSD-3-Clause | Numerics |
| `scipy` | runtime | — | BSD-3-Clause | Statistics (04, 07, 11) |
| `pydantic` | runtime | 2.9 | MIT | Models (all; `SecretStr` in U00-42) |
| `pydantic-settings` | runtime | 2.5 | MIT | Config (10) |
| `pyyaml` | runtime | — | MIT | YAML (`safe_load` only) |
| `jsonschema` | runtime | — | MIT | Schema checks (05) |
| `typer` | runtime | 0.12 | MIT | CLI (09) |
| `structlog` | runtime | 24 | MIT or Apache-2.0 | Logging (this spec) |
| `rich` | runtime | — | MIT | CLI output (09) |
| `httpx` | runtime | 0.27 | BSD-3-Clause | HTTP via egress and connectors (01, 10) |
| `tenacity` | runtime | 9 | Apache-2.0 | Retry primitives (08) |
| `tzdata` | runtime | — | Apache-2.0 | Time zones (U00-18) |
| `psutil` | runtime | — | BSD-3-Clause | Process checks (08, 10) |
| `keyring` | runtime | — | MIT | Secrets (10) |
| `pyahocorasick` | runtime | — | BSD-3-Clause | Redaction (10) |
| `pymongo` | runtime | 4.8 | Apache-2.0 | MongoDB source (01) |
| `snowflake-connector-python[pandas]` | runtime | 3.12 | Apache-2.0 | Snowflake source (01) |
| `msal` | runtime | 1.31 | MIT | Dataverse auth (01) |
| `torch` | runtime | — | BSD-3-Clause | ML (03), from the `pytorch-cu128` index |
| `sentence-transformers` | runtime | 3 | Apache-2.0 | Embeddings (03, 07) |
| `scikit-learn` | runtime | 1.5 | BSD-3-Clause | Clustering, calibration (03) |
| `lancedb` | runtime | 0.13 | Apache-2.0 | Vectors (02, 07) |
| `rapidfuzz` | runtime | 3 | MIT | Fuzzy matching (03) |
| `openai` | runtime | 1.50 | Apache-2.0 | OpenAI-compatible clients (05) |
| `anthropic` | runtime | 1 (and `<2`) | MIT | Anthropic client (05) |
| `sqlglot` | runtime | — | MIT | SQL guard (05) |
| `ortools` | runtime | 9.10 | Apache-2.0 | Portfolio optimiser (04) |
| `jinja2` | runtime | 3.1 | BSD-3-Clause | Reports (09) |
| `streamlit` | runtime | 1.39 | Apache-2.0 | Dashboard (09) |
| `presidio-analyzer` | extra `ner` | — | MIT | NER redaction (10) |
| `spacy` | extra `ner` | — | MIT | NER redaction (10) |
| `weasyprint` | extra `pdf` | — | BSD-3-Clause | PDF reports (09) |
| `pytest` | dev | 8.0 (this spec) | MIT | Tests |
| `pytest-asyncio` | dev | — | Apache-2.0 | Async tests |
| `pytest-cov` | dev | — | MIT | Coverage |
| `pytest-benchmark` | dev | — | BSD-2-Clause | Benchmarks |
| `pytest-timeout` | dev | — | MIT | Test timeouts |
| `hypothesis` | dev | — | MPL-2.0 | Property tests |
| `respx` | dev | — | BSD-3-Clause | HTTP mocking (01, 05) |
| `freezegun` | dev | — | Apache-2.0 | Time freezing |
| `mongomock` | dev | — | ISC | MongoDB tests (01) |
| `ruff` | dev | — | MIT | Lint and format |
| `mypy` | dev | — | MIT | Types |
| `pre-commit` | dev | — | MIT | Hooks |
| `import-linter` | dev | 2.1 (this spec, O-01) | BSD-2-Clause | Layer contracts (ENG E3) |
| `pip-audit` | dev | — | Apache-2.0 | Vulnerability audit (ENG E3) |
| `cyclonedx-bom` | dev | — | Apache-2.0 | SBOM `cyclonedx-py` (ENG E3) |
| `detect-secrets` | dev | — | Apache-2.0 | Secret scan (ENG E3) |
| `types-PyYAML`, `types-jsonschema`, `types-psutil` | dev | — | Apache-2.0 | mypy stubs (DD-08) |
| `hatchling` | build | 1.25 (`<2`) | MIT | Build backend (DD-08) |
| `osv-scanner` | CI binary | pinned version with SHA-256 (O-02, O-07) | Apache-2.0 | Vulnerability audit (ENG E3) |

Maintainer health: every package above is an actively maintained, widely used project (PyPA, Astral, pytest-dev, Google, Microsoft, Anthropic, OpenAI or a long-standing independent maintainer); new additions follow ENG §5.6.

### 14.2 Internal dependencies

| Direction | Spec | Units or artifacts |
|-----------|------|--------------------|
| Used by this spec | impl 10 | T10-07 (herness.core.secrets.scrub_secrets) (scrubber argument, F00-02); T10-11 (herness.core.redact_scan.main) (`--scan` pre-commit hook); T10-03 (herness.core.config.load_config) (F00-02); T10-26 (herness deploy install) (attestation check, F00-09) |
| Used by this spec | impl 09 | T09-20 (herness.cli.main) (entry point in `[project.scripts]`, composition root in F00-02) |
| Used by this spec | impl 11 | T11-01 (tests/conftest.py) (marker rule, Hypothesis profiles); T11-30 (herness.eval.runner.run_golden, behind `herness eval --mock-llm`) (CI step); T11-03 (tests/support/fake_clock.FakeClock) (patches U00-09, U00-10, U00-11 per DD-05) |
| Used by this spec | impl 04 | T04-01 (herness.metrics.evidence.result_hash) (referenced, not implemented here; R-15) |
| Used by this spec | impl 09 (config) | T09-01 (config/app.yaml `reports.allowed_numeral_patterns`, U09-02) (the list callers pass to U00-67, F00-11) |
| Extends this spec | impl 03, 05, 06, 07, 08, 09 | T03-01 (herness.core.types.decisions), T05-01 (herness.core.types.harness), T06-01 (herness.core.types.swarm), T07-01 (herness.core.types.memory), T08-01 (herness.core.types.jobs), T09-01 (herness.core.types.reports) (F00-10) |
| Consumes this spec | all | `herness.core.errors` (including `NotFound`, `hint`, `details`, R-19), `time`, `ids`, `logging`, `types`; `canonical_json`, `normalize_sql` and `query_id` by impl 04 and 05 (R-14); `herness.core.numbers` by impl 05 (Verifier), 06, 07, 09 (renderer, report contract, dashboard, chat) and 11 (R-16); `format_utc` by impl 02 (ops store timestamps) and impl 01 (watermarks); T08-07 (herness.core.resilience.retry_call) dispatches on U00-02 |
