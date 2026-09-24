# 10 — Configuration, Security and Deployment

Status: Draft v2 · 2026-09-24 · Depends on: 00, 02. Phase 1 (config, redaction), Phase 3–4 (egress, containers).

## 1. Purpose and scope

This spec defines how Herness is configured, how it protects data and secrets, and how it is installed and run on one Windows 11 Pro PC with one 24 GB+ NVIDIA GPU.

In scope:
- Config loading, profiles, precedence, validation, `config_hash`, and the implementation registry (`herness/core/config.py`, `herness/core/registry.py`, `config/*.yaml`, `config/profiles/*.yaml`).
- Secrets resolution (`herness/core/secrets.py`).
- PII and secret redaction (`herness/core/redact.py`), which produces `enrich.text_redacted`.
- The egress guard, the single choke point for off-network calls (`herness/core/egress.py`).
- Data protection, retention, deletion, audit logging (`herness/core/audit.py`).
- Application security controls (dashboard exposure, agent SQL, prompt injection).
- Deployment: prerequisites, `docker/compose.yaml`, install runbook, `herness doctor`, Windows services, model upgrade and rollback.

Out of scope: the content of each section's settings (owned by the specs named in §4.2), GPU swap timing (08), CLI command framework (09).

## 2. Responsibilities

1. Load one immutable `HernessConfig` per process from files, profile, environment and CLI, in a fixed precedence.
2. Reject invalid config before any job starts (`ConfigError`), and compute a stable `config_hash` recorded in `meta.build.config_hash` and `run.config_hash`.
3. Resolve secrets by name at use time. Never write secret values to config dumps, logs, traces, prompts or the ops store.
4. Mask PII and secrets in free text with stable pseudonyms, at ≥ 5k records/s/core.
5. Route every off-network call through the egress guard. In profile `local`, block before any socket opens.
6. Write an append-only audit trail for approvals, config changes, egress and admin actions.
7. Provide a reproducible, pinned deployment of the GPU model servers and a `herness doctor` check that proves the host is correctly set up.

## 3. Interfaces

### 3.1 Config (`herness/core/config.py`)

```python
ProfileName = Literal["local", "hybrid", "premium", "synth"]

class HernessConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HERNESS_", env_nested_delimiter="__",
                                      extra="forbid", frozen=True)
    profile: ProfileName = "local"            # D5 default; resolved per §4.1
    # config/herness.yaml: root sections owned by this spec
    paths: PathsConfig
    security: SecurityConfig
    logging: LoggingConfig
    retention: RetentionConfig
    backup: BackupConfig
    deploy: DeployConfig
    # one section per other file = the whole file (00 §11); model class in the owner's settings.py
    sources: SourcesConfig                    # sources.yaml     (01; dq, build: 02)
    mappings: MappingsConfig                  # mappings.yaml    (02)
    decisions: DecisionsConfig                # decisions.yaml   (03)
    metrics: MetricsCatalogConfig             # metrics.yaml     (04)
    weights: WeightsConfig                    # weights.yaml     (04; includes business_timezone)
    models: ModelsConfig                      # models.yaml      (05)
    pipelines: PipelinesConfig                # pipelines.yaml   (06)
    memory: MemoryConfig                      # memory.yaml + injection_patterns.txt (07)
    resilience: ResilienceConfig              # resilience.yaml  (08: top-level keys `resilience` and `schedule`)
    app: AppConfig                            # app.yaml         (09: dashboard, chat UI, reports, CLI)
    eval: EvalConfig | None                   # eval.yaml        (11)

def load_config(profile: ProfileName | None = None, overrides: Sequence[str] = (),
                config_dir: Path = Path("config"), env: Mapping[str, str] | None = None) -> HernessConfig: ...
def get_config() -> HernessConfig: ...            # process-wide cached instance; loaded once at CLI entry
def config_hash(cfg: HernessConfig) -> str: ...   # "cfg_" + 16 hex
def effective_dict(cfg: HernessConfig, *, redact_secrets: bool = True) -> dict: ...
def validate(cfg_dir: Path, profile: ProfileName, *, offline: bool = False) -> list[ConfigIssue]: ...

@dataclass(frozen=True)
class ConfigIssue:
    severity: Literal["error", "warn"]; path: str; message: str; file: str | None
```

Section models live next to their owners as `herness/<package>/settings.py` (pydantic and stdlib imports only, so loading config never imports torch or duckdb). `herness/core/config.py` owns only the root model and the `herness.yaml` sections. When a spec writes "`config/X.yaml: key`" it means `cfg.X.key`. For example, `weights.yaml: business_timezone` is `cfg.weights.business_timezone` and `resilience.yaml: resilience.gpu` is `cfg.resilience.resilience.gpu`.

### 3.2 Registry (`herness/core/registry.py`)

```python
Kind = Literal["connector", "monitoring_adapter", "decider", "llm_client", "tool",
               "embedder", "renderer"]

def register(kind: Kind, name: str) -> Callable[[T], T]: ...     # decorator
def get(kind: Kind, name: str) -> Any: ...                       # returns the class / factory
def available(kind: Kind) -> list[str]: ...
```

Built-ins are declared in a static table `_BUILTINS: dict[tuple[Kind, str], str]` of `"module:attr"` strings and imported lazily on first `get`. Third-party plugins may register through the entry-point group `herness.plugins`. An unknown name raises `ConfigError` listing `available(kind)`. `validate()` calls `get` for every name referenced in config (without instantiating).

### 3.3 Secrets (`herness/core/secrets.py`)

```python
SECRET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")   # case-insensitive; stored lower-case

class SecretRef(str): ...                    # "secret:<name>" value, or the bare name in a *_secret field
def resolve(ref: SecretRef | str) -> SecretStr: ...        # raises ConfigError("secret not found: <name>")
def resolve_json(ref: SecretRef | str) -> dict[str, SecretStr]: ...   # compound credentials stored as JSON
def exists(name: str) -> bool: ...
def set_secret(name: str, value: str) -> None: ...    # CLI only; audited (name only)
def known_values() -> frozenset[str]: ...             # resolved values (and JSON members), used by the log scrubber
```

A config value can reference a secret in two ways, and both resolve through this module:
- A string `"secret:<name>"`, as in spec 01 `auth.credentials`.
- A field whose name ends in `_secret` and holds the bare name, as in spec 03 `api_key_secret: OPENJEV_API_KEY`.

A plain-text credential in a field that expects a secret is a `ConfigError`.

Backends, chosen by `security.secrets.backend`:
- `keyring` (default): Windows Credential Manager through `keyring`. Service name `herness`, username = lower-cased secret name.
- `dotenv` (dev and `synth` only): reads `HERNESS_SECRET__<NAME>` from `.env` (dots become `_`, upper case). Refused unless `HERNESS_ENV=dev` or the profile is `synth`.

Secret names in use (confirmed with the owning specs):

| Name | Holds | Used by |
|---|---|---|
| `OPENJEV_API_KEY` | bearer key for local OpenJev (optional; set it anyway, defense in depth) | 03 decider; compose env (§5.6.2) |
| `TYPESAFE_API_KEY` | hosted Jev API key (premium only) | 03 `jev` decider |
| `vllm.api_key` | bearer key for the local vLLM server | 05 `local-*` models; compose env |
| `anthropic.api_key` | Anthropic API key (hybrid/premium only) | 05 Anthropic adapter |
| `redact.hmac_key` | 32 random bytes, hex | this spec, §5.3 |
| `ui_user_ref_key` | HMAC key that hashes usernames into `user_ref` (spec 09 §9.2) | 09; audit `actor` |
| `servicenow_oauth`, `jira_api_token`, `snowflake_svc`, `dataverse_app`, `datadog_keys`, `mimir_read`, … | per-source credentials, JSON when compound | 01 |

### 3.4 Redaction (`herness/core/redact.py`)

```python
EntityType = Literal["EMAIL", "PHONE", "IP", "PERSON", "EMPLOYEE_ID", "USER_ID",
                     "CREDENTIAL", "URL_TOKEN", "CARD", "NATIONAL_ID", "CUSTOM"]

@dataclass(frozen=True)
class Span: start: int; end: int; type: EntityType; replacement: str

@dataclass(frozen=True)
class RedactionResult: text: str; counts: dict[EntityType, int]

class Redactor:
    def __init__(self, cfg: RedactionConfig, hmac_key: bytes,
                 directory: NameDirectory | None = None): ...
    def scan(self, text: str) -> list[Span]: ...                 # detect only
    def redact(self, text: str | None) -> RedactionResult | None: ...
    def redact_batch(self, texts: Sequence[str | None]) -> list[str | None]: ...
    def pseudonym(self, type: EntityType, value: str) -> str: ...
    key_id: str                                                   # first 8 hex of SHA-256(hmac_key)

def get_redactor() -> Redactor: ...                              # process-wide, built from config
def redact_text(text: str | None) -> str | None: ...              # get_redactor().redact(text).text; used by 05, 08, 09
def redact_table(tbl: pa.Table, text_cols: Sequence[str], id_col: str = "record_id",
                 workers: int | None = None) -> pa.Table: ...     # -> (record_id, text)
```

Callers:
- Spec 03 builds the classifier text, calls `redact_table` to fill `enrich.text_redacted`, and derives `content_hash` from the result (00 §5).
- Spec 05 passes sampled trace payloads through `redact_text`.
- Spec 07 redacts before any memory write.
- Spec 08 redacts `last_error`.
- Spec 09 redacts chat input before storage and Jira titles at display.

`python -m herness.core.redact --scan PATH` is the fixture scanner that spec 11's pre-commit hook runs.

### 3.5 Egress guard (`herness/core/egress.py`)

```python
Purpose = Literal["reasoning_final", "reasoning", "bulk_classification", "model_download"]
PayloadClass = Literal["aggregated_evidence", "redacted_text", "none"]

class EgressGuard:
    def http_client(self, purpose: Purpose, payload_class: PayloadClass, *,
                    run_id: str | None = None, task_id: str | None = None,
                    timeout: float = 120.0) -> httpx.Client: ...
    def async_http_client(self, purpose: Purpose, payload_class: PayloadClass, **kw) -> httpx.AsyncClient: ...
    def check(self, url: str, body: bytes, purpose: Purpose, payload_class: PayloadClass,
              token_estimate: int | None = None) -> None: ...   # raises EgressBlocked

def get_guard() -> EgressGuard: ...
def install_socket_guard(cfg: HernessConfig) -> None: ...   # called first thing in cli.main()
```

Rules:
- Any code that talks to a non-local endpoint must obtain its HTTP client from `get_guard().http_client(...)`. The Anthropic adapter passes it as `anthropic.Anthropic(http_client=...)`; the hosted Jev decider and any OpenAI-compatible cloud endpoint do the same. A lint test (§10) fails the build if `httpx.Client(`, `requests.`, `urllib.request` or `anthropic.Anthropic(` without `http_client=` appears outside `herness/core/egress.py` and `herness/connectors/`.
- Source connectors are not egress (they read from configured corporate sources). They build clients through `herness.connectors.base.http_client(source)`, which only allows hosts from that source's `base_url` in `sources.yaml`.
- Local model endpoints (`127.0.0.1`, `localhost`) need no guard.

### 3.6 Audit (`herness/core/audit.py`)

```python
AuditEvent = Literal["review_decision", "recommendation_decision", "config_change",
                     "admin_action", "auth", "egress"]
def audit(event: AuditEvent, actor: str, **fields: Any) -> None: ...
```

### 3.7 CLI surface (rows in spec 09's command table; behavior defined here)

| Command | Options | Does | Role |
|---|---|---|---|
| `config validate` | `--profile NAME`, `--offline` (skip secret lookups), `--strict` (warnings fail) | Runs the §5.1 checks; prints the resolved profile and `config_hash` | any |
| `config show` | `--profile NAME` | Prints the effective config; secrets show as `secret:<name>` | admin |
| `config hash` | `--profile NAME` | Prints `config_hash` | any |
| `secrets init` | — | Creates `redact.hmac_key` and `ui_user_ref_key` if absent; asks for escrow confirmation | admin (OS) |
| `secrets set` | `NAME` (value from a hidden prompt, never an argument) | Stores the value in the backend; audits `secret_set` | admin (OS) |
| `secrets status` | — | Lists each referenced name as present/missing, with last-set time from the audit log | admin |
| `secrets rekey` | — | Stages a new redaction key and enqueues the `maintenance` rekey job (§5.3) | admin (OS) |
| `deploy render` | — | Writes `/opt/herness/docker.env` from `deploy.*` (§7.4) | admin |
| `deploy pull` | `--allow-download` (required) | Pulls images by digest and weights by revision in a firewall window | admin |
| `deploy up` / `down` | `reasoning\|decider\|large` | Manually requests or releases one GPU class; the worker (08) normally does this. While a worker is alive (`jobs.worker_alive()`), it only sets `worker.requested_class` and waits for the arbiter (spec 08 §5.8); with no worker it holds `data/locks/gpu.lock` while it starts or stops containers. It never loads a class next to a running worker | admin |
| `deploy rollback` | `reasoning\|decider\|large` | Restarts the class with the previous pinned values after a config revert | admin |
| `deploy prune` | — | Removes image digests and weight revisions older than the last two promoted | admin |
| `privacy delete` | `--record-id ID` (repeatable), `--reason-ref REF` | Enqueues a `maintenance` job (§5.5) | admin |
| `maintenance backup` / `purge` | `--dry-run` | Enqueues a `maintenance` job | admin |

`doctor` remains spec 09's command. §5.6.3 lists the checks this spec adds to it.

## 4. Data contracts

### 4.1 Files and precedence

```text
config/                      (00 §11)
  herness.yaml               # paths, security (incl. ui: bind, port, identity header, users/roles), logging, retention, backup, deploy
  sources.yaml mappings.yaml decisions.yaml metrics.yaml weights.yaml models.yaml
  pipelines.yaml memory.yaml injection_patterns.txt resilience.yaml app.yaml eval.yaml
  profiles/local.yaml profiles/hybrid.yaml profiles/premium.yaml profiles/synth.yaml
```

Precedence, lowest to highest: pydantic field defaults < `config/*.yaml` < `config/profiles/<profile>.yaml` < `.env` (only when `HERNESS_ENV=dev`) < environment `HERNESS_*` < CLI flags (`--profile`, `--depth`, repeatable `--set a.b.c=<yaml value>`).

- The top-level keys of `herness.yaml` are root sections. Every other file becomes one section named after its file stem, with that file's top-level keys inside it. Each file has its own `version: 1`. A missing file is a `ConfigError`, except `eval.yaml`, which is only required for eval runs.
- A profile file is a partial overlay addressed by section, e.g. `{security: {egress: {...}}, pipelines: {...}}`. The profile layer and all higher layers deep-merge mappings; lists and scalars replace.
- The profile is chosen by `--profile`, else `HERNESS_PROFILE`, else `local`. The profile overlay `config/profiles/<profile>.yaml` supplies spec 05's `models.roles` and `models.fallback` overrides (spec 05 §7), and the loader passes `cfg.profile` to `LLMRegistry(profile=...)`, so the role map always matches this spec's profile. `synth` uses the `local` role map.
- Keys under `security.*` and `profile` gating fields are file-only: env or `--set` overrides of `security.*` raise `ConfigError`. A stray variable can never enable egress.
- Implemented with `settings_customise_sources` returning, in priority order: init kwargs (CLI), env, dotenv, `ProfileYamlSource`, `FilesYamlSource`.

### 4.2 Top-level skeletons and owners

```yaml
# config/herness.yaml — spec 10 only
version: 1
paths: {data: data, logs: data/logs, backup_target: "E:/herness-backup"}
security:                                   # §7.1–7.2
  data_policy: {hybrid_approved: false, premium_approved: false, approved_by: null, approved_on: null}
  secrets: {backend: keyring}
  redaction: {...}
  egress: {enabled: false, destinations: [], purposes: [], max_request_bytes: 2000000,
           max_tokens_per_request: 200000, max_tokens_per_day: 3000000}
  network: {extra_allowed_hosts: [], http_proxy: null}
  ui: {...}                                 # §7.3: bind, port, identity header, users/roles; spec 09 reads it
logging: {level: INFO}
retention: {raw_lake_months: 36, traces_days: 90, egress_log_days: 365, audit_log_days: 730,
            app_log_days: 30, reports_days: 365, chat_days: 180}
backup: {nightly_at: "01:30", keep_daily: 14, keep_weekly: 8, include_lake: false, include_cache: false}
deploy: {...}                               # §7.4
```

For the other files, the listed spec owns shape and content. This spec only loads and cross-validates them.

| File → section | Top-level keys (per owner) | Owner |
|---|---|---|
| `sources.yaml` → `sources` | `sources` (per source: `base_url`, `auth` with `secret:` refs, entities, schedules), `dq`, `build` | 01, 02 |
| `mappings.yaml` → `mappings` | `enums`, `service_overrides`, `custom_fields` | 02 |
| `decisions.yaml` → `decisions` | typed question sets | 03 |
| `metrics.yaml` → `metrics` | metric catalog | 04 |
| `weights.yaml` → `weights` | dollar rates, strategic weights (D2 `unconfirmed`), `business_timezone` | 04 |
| `models.yaml` → `models` | `models` (`clients`, `roles`, `fallback`, `depth_overrides`, `role_params`, `anthropic`, `deciders` (03), `depth`), `harness`; per-profile role maps are overlays in `config/profiles/*.yaml` (spec 05 §7) | 05 |
| `pipelines.yaml` → `pipelines` | swarm limits, pipeline definitions, depth modes | 06 |
| `memory.yaml` → `memory` (+ `injection_patterns.txt`) | memory policy, retrieval, compaction, outcomes | 07 |
| `resilience.yaml` → `resilience` | `resilience` (`retry`, `breakers`, `fallback`, `loop`, `tasks`, `jobs`, `gpu`: compose file and command, GPU classes), `schedule` (`windows`, `chat` (D4), `rekey`, `jobs` (D3), `maintenance`) | 08 |
| `app.yaml` → `app` | dashboard, chat UI, reports (`allowed_numeral_patterns`), CLI | 09 |
| `eval.yaml` → `eval` | golden suite, thresholds, judge | 11 |

Any secret-bearing value in any file uses `secret:<name>`. Examples: `sources.servicenow.auth.password: "secret:servicenow.password"`, `models.clients.claude-opus.api_key: "secret:anthropic.api_key"`, `models.clients.local-30b.api_key: "secret:vllm.api_key"`.

### 4.3 Profiles

| Setting | `local` (default, D5) | `hybrid` | `premium` | `synth` (spec 11) |
|---|---|---|---|---|
| Gate in `herness.yaml` | none | `security.data_policy.hybrid_approved: true` + `approved_by` + `approved_on` | `premium_approved: true` + same | none |
| Role → model | `models.yaml: models.roles` as written (all local) | `profiles/hybrid.yaml` overlay: `skeptic_final` and `writer` → `claude-opus`; other roles local (spec 05 §7) | `profiles/premium.yaml` overlay: `claude-opus` / `claude-sonnet` / `claude-haiku` per role (spec 05 §7) | as `local` |
| Deciders (spec 03) | Laya student; OpenJev teacher and escalation; LLM decider when OpenJev is unavailable (D7) | as local | hosted Jev for bulk; Batch API LLM for hard cases | as local |
| `security.egress.enabled` | `false` | `true` | `true` | `false` (cannot be overridden) |
| `egress.destinations` | `[]` | `[api.anthropic.com]` | `[api.anthropic.com, api.typesafe.ai]` (Jev host from spec 03; verify at Phase 4) | `[]` |
| `egress.purposes` | `[]` | `[reasoning_final]` | `[reasoning, reasoning_final, bulk_classification]` | `[]` |
| Allowed payload class | none | `aggregated_evidence` only | `aggregated_evidence`, `redacted_text` (single redacted tickets, spec 03) | none |
| `pipelines` swarm caps | 4–8 parallel | as local | 50–200 (spec 06) | as local |
| Sources | as configured | as local | as local | only the `files` connector over `data/synth/<seed>-<scale>/`; builds record `meta.build.dataset_kind = 'synthetic'` |
| Secrets backend | `keyring` | `keyring` | `keyring` | `dotenv` allowed (fixed test HMAC key) |
| Socket guard allows | loopback + source hosts | + destinations | + destinations | loopback only |

Loading a gated profile without its gate is a `ConfigError`. The profile name is written to `run.profile` on every run. When `HERNESS_SYNTH_CONFIG=<path>` is set, the generator's fragment (`custom_fields`, `enums`, spec 11) is merged after `profiles/synth.yaml`, and only `mappings.*` keys are accepted from it. Profile files may set `security.redaction.denylist_domains`, which spec 11's fixture scanner reads.

### 4.4 `config_hash`

`"cfg_" + sha256(canonical_json(effective_dict(cfg)))[:16]`, where canonical JSON has sorted keys, no whitespace, `Decimal` and `Path` as strings (paths POSIX-style), secret refs left as `secret:<name>`. Excluded keys: `logging.*`, `paths.*`, `backup.*`, `security.ui.*`, `deploy.service.*`. Included extra inputs: `redact.key_id` and the SHA-256 of the name directory file (not its content), because both change `content_hash`. Recorded in `meta.build.config_hash` and `run.config_hash`. A snapshot of `effective_dict` is written once per hash to `data/config_snapshots/<config_hash>.yaml`.

### 4.5 Egress log line (`data/logs/egress-<YYYY-MM-DD>.jsonl`)

```json
{"ts":"2026-09-24T21:14:03.120Z","egress_id":"egr_01J8...","decision":"allowed","reason":null,
 "profile":"hybrid","purpose":"reasoning_final","payload_class":"aggregated_evidence",
 "destination":"api.anthropic.com","method":"POST","path":"/v1/messages","run_id":"run_01J8...",
 "task_id":"task_01J8...","bytes_out":84211,"bytes_in":9120,"tokens_in":21034,"tokens_out":2211,
 "payload_sha256":"9c1e...","scan_hits":{},"status_code":200,"latency_ms":18230,"config_hash":"cfg_..."}
```

The payload is never logged. `tokens_in` is the estimate before sending, replaced by the provider's usage figure when the response arrives (second line with the same `egress_id`, `decision: "completed"`).

### 4.6 Audit log line (`data/logs/audit-<YYYY-MM-DD>.jsonl`)

Fields: `ts`, `audit_id`, `event`, `actor` (the `user_ref` from spec 09 §9.2, i.e. an HMAC of the username keyed with `secret:ui_user_ref_key`; `system` for the worker), `fields` (event-specific, never secret values or ticket text), `config_hash`, `prev_hash` (SHA-256 of the previous line; the first line of a day chains to the last line of the previous day). The chain makes silent edits detectable (`herness doctor` verifies it).

| Event | Written by | Fields |
|---|---|---|
| `review_decision` | `herness.store.ops` when `review_item.status` changes | `item_id`, `kind`, `status`, `decided_by`, `note_len` |
| `recommendation_decision` | on `decision_log` insert | `rec_id`, `decision`, `decided_by` |
| `config_change` | process start when `config_hash` differs from the last audited one | `old_hash`, `new_hash`, `changed_paths` (key paths only), `profile` |
| `admin_action` | CLI / dashboard admin | `action` (`secret_set`, `secret_rotate`, `privacy_delete`, `deploy_up`, `deploy_rollback`, `profile_switch`, `purge`, `backup`), `target` (secret name, record_id, image digest…) |
| `auth` | dashboard | `user_ref`, `role`, `result` (`allowed` / `denied`) |
| `egress` | egress guard, summary line per blocked call | `egress_id`, `reason` |

## 5. Behavior

### 5.1 Config load

1. `cli.main()` calls `install_socket_guard` with a minimal bootstrap config (profile + `security` only), then `load_config`.
2. Read YAML with `yaml.safe_load`; reject duplicate keys (custom loader) and unknown root keys.
3. Merge layers per §4.1. Validate with pydantic (`extra="forbid"`).
4. Cross-checks (all run by `validate`): every entry of `models.roles` and `models.fallback` (after the profile overlay) names a client in `models.clients` (spec 05 §7); a profile that maps any role to an off-network model must have egress enabled and its gate set; every `get(kind, name)` resolves; egress destinations are bare hostnames with HTTPS implied; `security.ui.expose.enabled` requires `security.ui.expose.trusted_proxy` and a non-loopback `bind` is refused otherwise; every `secret:` ref exists (skipped with `--offline`); `deploy.reasoning.served_name` equals `models.clients.local-30b.model` (the vLLM served name, spec 05 §7); every service in `resilience.gpu.classes.*.services` exists in `docker/compose.yaml`, and its health URL uses the matching `deploy.*.port`; metric SQL references only known schemas (spec 04 validator); question names unique (spec 03 validator); D2 unconfirmed weights → `warn`.
5. Compute `config_hash`; if it differs from the last `config_change` audit line, write one and the snapshot file.

`herness config validate` exits 0 when no errors, 1 on any error, 2 on warnings with `--strict`. Output: one line per issue, `severity path file: message`.

### 5.2 Secrets

- Values are resolved at the point of use (client construction), held as `SecretStr`, and never stored on config objects.
- The structlog pipeline has a `scrub_secrets` processor that replaces any substring equal to a value in `known_values()` with `***` and runs the redactor's `CREDENTIAL` and `URL_TOKEN` detectors on every string field. The trace writer (spec 05) uses the same processor.
- Prompts never contain secrets: LLM adapters put API keys only in HTTP headers, and headers are not traced.
- Rotation: `herness secrets set <name>` replaces the value; clients pick it up on next construction (worker restarts clients at job start). Rotate source tokens at least every 90 days or per corporate policy; `herness secrets status` shows last-set time from the audit log. The redaction HMAC key is never rotated routinely (§5.3).

Least-privilege source accounts:

| Source | Account | Grants |
|---|---|---|
| ServiceNow | integration user `svc_herness_ro`, web-service access only | read ACLs on `incident`, `problem`, `change_request`, `cmdb_ci*`, `cmdb_rel_ci`, `sys_user_group` (e.g. `itil` read-only or a custom read role); no write roles |
| Jira Cloud | dedicated user with API token | Browse Projects on in-scope projects only; no admin, no edit |
| Dataverse | Entra app registration (client credentials, certificate preferred over secret) | application user with a custom security role: Read at org level on required tables only |
| Snowflake | user `HERNESS_RO`, key-pair auth | role with `USAGE` on warehouse/db/schema and `SELECT` on required views; small warehouse with auto-suspend and resource monitor |
| MongoDB | user with built-in `read` role on required databases | |
| Monitoring | read-only API keys (Datadog application key with read scopes, Splunk role with search on named indexes, Prometheus behind read-only proxy, Dynatrace token with read scopes) | |

### 5.3 Redaction

Detection order (earlier types win on overlap): `CREDENTIAL` → `URL_TOKEN` → `EMAIL` → `CARD` → `NATIONAL_ID` → `EMPLOYEE_ID` → `USER_ID` → `PHONE` → `IP` → `PERSON` → `CUSTOM`.

| Type | Detector |
|---|---|
| `CREDENTIAL` | PEM private key blocks; `Authorization: (Bearer\|Basic) …`; `(password\|passwd\|pwd\|secret\|api[_-]?key\|token)\s*[:=]\s*\S+`; known key formats (AWS `AKIA[0-9A-Z]{16}`, GitHub `gh[pousr]_…`, Slack `xox[abpors]-…`, JWT `eyJ…\.…\.…`, Azure `AccountKey=…`); user:pass in URLs |
| `URL_TOKEN` | URLs whose query has `token\|access_token\|key\|sig\|signature\|code\|password\|sv\|se` params: the value is masked, the rest of the URL kept |
| `EMAIL` | RFC-5322-lite regex |
| `CARD` | 13–19 digits with separators, Luhn-valid |
| `NATIONAL_ID` | configurable list, default US SSN `\b\d{3}-\d{2}-\d{4}\b` |
| `EMPLOYEE_ID`, `USER_ID` | regexes from `security.redaction.id_patterns` (e.g. `\bE\d{6}\b`) |
| `PHONE` | international and NANP forms, 9–15 digits after normalization, not inside a longer digit run, not matching a ticket number pattern (`INC\d+`, `CHG\d+`…) |
| `IP` | IPv4/IPv6; `mask_ip: true` by default (hosts can identify people's machines); set false if IP analysis matters more |
| `PERSON` | Aho-Corasick (`pyahocorasick`) over a name directory: full names, `Last, First`, `First Last`, word-bounded, case-insensitive. Directory built from `security.redaction.directory_file` (CSV, ACL-protected) plus user display names seen in `*_display` lake fields (spec 01). Single first names are not masked unless listed in `extra_names` |
| `CUSTOM` | `security.redaction.custom_patterns` |

Replacement format: `[<TYPE>_<10 hex>]`, e.g. `[PERSON_3f9a0c1d2e]`, where the hex is `HMAC-SHA256(key, TYPE + ":" + normalize(value))[:10]`. Normalization: emails lowercased; phones as E.164 digits; person names casefolded, whitespace collapsed, `Last, First` → `first last`; IDs upper-cased. The same person or address maps to the same token across all records, and the token reveals nothing without the key. `CREDENTIAL` and `URL_TOKEN` values become a fixed `[SECRET]` (no linkage needed). 40 bits keeps expected collisions below 0.01 for 100k distinct values.

The HMAC key is 32 random bytes created by `herness secrets init` as `secret:redact.hmac_key` and exported once to the corporate password vault (escrow). Changing it changes every pseudonym and every `content_hash` (00 §5). That forces a full re-embed and re-classification, about 6 h of GPU time (spec 03). Rotate only on suspected key compromise, and only as a planned job:

1. `herness secrets rekey` stores the new key under `redact.hmac_key.next` and calls spec 08 `jobs.schedule_rekey()`, which enqueues a `maintenance` job with payload `{"action": "rekey"}` and `gpu_class: decider` in the planned slot: the next `schedule.rekey.cron` fire (default Saturday 19:00, business timezone) at least `schedule.rekey.min_notice_h` (12 h) ahead. That night the rekey job runs before the `build_pipeline`, the full re-embed and re-classification runs through the Saturday reviews window, and that night's chained standard reviews are skipped (spec 08 §5.11). `herness status` shows the planned night; canceling the job leaves the old key active.
2. The job recomputes redacted text with the new key for every record and writes the map `data/cache/rekey/<new key_id>.parquet` (`record_id`, `old_content_hash`, `new_content_hash`).
3. It re-keys the human and gold labels in `data/labels/<question_set_version>/{human,gold}/` through the map. Each file is written as a new file and then swapped in with `os.replace`. A label whose record no longer exists is kept under its old hash and reported. Teacher labels are re-keyed too, so distillation (spec 03) does not need to re-label.
4. It atomically moves `redact.hmac_key.next` to `redact.hmac_key` and audits `admin_action: redact_rekey` with both key IDs. The next build pipeline re-embeds and re-classifies under the new hashes. The old decision cache is deleted once that build is promoted.

If the job fails before step 4, the old key stays active and nothing downstream changes.

Optional NER (`security.redaction.ner: none | presidio`, default `none`). Presidio + spaCy finds names not in the directory, but runs about 100–300 records/s/core (well under the 5k target), needs a ~500 MB model download and flags service and product names ("Jenkins", "Rose", "Mercury") as people, which damages clustering and classification. When enabled it runs only on the output of the regex/directory pass, for `redacted_text` payloads that are about to leave the machine (premium bulk classification), not for the local bulk pass.

Failure mode is closed: if redaction of a record raises, `redact_table` writes `text = NULL` for that record, counts it, and spec 03's DQ reports it. Raw text is never substituted.

Throughput design: prefilter by cheap character checks (`@` for email, digit counts for phone/card/ID, `=`/`:` for credentials) before running each regex; precompiled patterns; `redact_table` chunks 20k rows per worker in a `ProcessPoolExecutor` sized to `sources.yaml: build.threads`.

### 5.4 Egress guard

`GuardedTransport(httpx.HTTPTransport)` (and its async twin) runs `check()` inside `handle_request`, before the connection pool opens a socket. `check()` steps, stopping at the first failure:

1. Profile gate: `security.egress.enabled` must be true, else `EgressBlocked("profile local forbids egress")`.
2. Purpose is in `security.egress.purposes`, and `payload_class` is allowed for the profile (§4.3).
3. URL scheme is `https`, port 443, host exactly in `security.egress.destinations` (no wildcard, no IP literals).
4. Body is bytes (streaming request bodies are rejected), `len(body) ≤ max_request_bytes`.
5. Token estimate (`len(body) / 3.5` unless given) `≤ max_tokens_per_request`, and today's total from the egress log plus this estimate `≤ max_tokens_per_day`.
6. Re-scan: decode the body as UTF-8 and run `Redactor.scan`. Pseudonym tokens `[TYPE_hex]` and `[SECRET]` are ignored. Any hit of `EMAIL`, `PHONE`, `CARD`, `NATIONAL_ID`, `CREDENTIAL`, `URL_TOKEN`, `EMPLOYEE_ID`, `USER_ID`, or `PERSON` blocks the call; `IP` hits block when `mask_ip` is true. Only hit counts per type go to the log.
7. Write the audit line (`decision: allowed`), then send. Blocked calls write `decision: blocked` with `reason` and raise `EgressBlocked`.

`model_download` is used only by `herness deploy pull` in an explicit maintenance window (`--allow-download`), with `payload_class: none` and destinations `huggingface.co`, `cdn-lfs.huggingface.co` (verify exact CDN hosts at Phase 4), and never by jobs.

Socket guard (defense in depth for the Python process): `install_socket_guard` registers `sys.addaudithook` handling `socket.getaddrinfo` and `socket.connect`. It allows loopback, hosts from every `base_url` in `sources.yaml` and `security.network.extra_allowed_hosts`, and, when egress is enabled, `security.egress.destinations`; resolved addresses of allowed hosts are cached for the `connect` check. Any other target raises `EgressBlocked` before the connection opens. The guard also sets `HF_HUB_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`, `DO_NOT_TRACK=1` in the process environment, so libraries do not try to download or report. When a corporate proxy is configured, the proxy host is allowed at socket level and step 3 still checks the target URL.

Containers are outside the Python guard; see §9.2 for host firewall rules.

### 5.5 Data protection

- Folder ACLs (applied by the runbook, checked by doctor): `data\`, `config\profiles\`, the directory file and `D:\herness\.env` grant Full to `Administrators` and Modify to `svc-herness`, inheritance removed, no `Users`:
  `icacls D:\herness\data /inheritance:r /grant:r "Administrators:(OI)(CI)F" "svc-herness:(OI)(CI)M"`.
- BitLocker on the data drive and the system drive (the WSL2 VHDX with model weights lives under `%LOCALAPPDATA%`). Doctor checks `manage-bde -status`.
- Backup (`herness maintenance backup`, nightly at `backup.nightly_at`, job kind `maintenance`):
  - `ops.sqlite` via `sqlite3.Connection.backup()` (online, consistent under WAL) to `<backup_target>/ops/ops-<date>.sqlite`, then `PRAGMA integrity_check` on the copy.
  - `config/` is in git; the backup also copies `data/config_snapshots/`.
  - `data/models/` (fine-tuned Laya, LoRA) copied after each successful distill.
  - `data/reports/`, `data/logs/audit-*`, `data/logs/egress-*` copied incrementally.
  - Not backed up: warehouse (rebuildable), vectors (rebuildable), decision cache (rebuildable, but costs GPU hours; include if `backup.include_cache: true`), raw lake (re-fetchable while the source retains history; set `include_lake: true` if sources purge old tickets).
  - Keep 14 daily and 8 weekly copies. Secrets are not backed up; they are re-entered from the vault. The HMAC key escrow is mandatory.
- Retention (`herness maintenance purge`, nightly): lake partitions older than `raw_lake_months` by `dt`; traces after `traces_days`; egress logs after `egress_log_days`; audit logs after `audit_log_days`; app logs after `app_log_days`; reports after `reports_days`; `chat_session`/`chat_message` after `chat_days` of inactivity. Every purge writes an `admin_action` audit line with counts.
- Deletion requests (`herness privacy delete --record-id`) run as a `maintenance` job that follows the steps below in order. Each step records its result in `deletion_request.steps`, and a failed step leaves the request `running` so the job retries from that step.
  1. Insert a `deletion_request` row (`request_id = del_<ulid>`, status `pending`), then set it to `running`. From this point connectors (01 §5.6, reloaded at each checkpoint) and staging (02 §4.2) drop the record.
  2. Lake purge, first pass: rewrite each lake file that contains the `_record_id` without it (write a new file, then `os.replace`).
  3. `herness.enrich.purge_record(record_id)` (spec 03) removes `ticket_embedding` rows, and removes decision-cache and label rows by `content_hash` unless another live record shares that hash.
  4. Remove the record from `evidence.result_sample` and from the `finding.numbers` row keys by rewriting the JSON. Delete traces that reference the record.
  5. Enqueue a `build_pipeline` job. After it is promoted, delete retired warehouse files older than the new build.
  6. Set the request to `done` and write the audit line `admin_action: privacy_delete`.
  7. Lake purge, second pass: repeat step 2. This catches rows a running sync committed before its next checkpoint reloaded the deletion set (01 Q11). If rows were found, run step 3 again.

### 5.6 Deployment

#### 5.6.1 Prerequisites

Windows 11 Pro (23H2 or later), BitLocker on; NVIDIA driver with WSL CUDA support (current Game Ready/Studio or RTX Enterprise; do not install a Linux driver inside WSL); WSL2 with a distro named `herness` (Ubuntu 24.04), systemd enabled; Docker Engine inside the distro (preferred over Docker Desktop because it runs without a logged-in desktop session) plus NVIDIA Container Toolkit; `uv` and Python 3.12 on Windows; ≥ 64 GB RAM, ≥ 200 GB free NVMe for weights, lake and warehouse.

#### 5.6.2 `docker/compose.yaml`

Service names, ports and compose profiles match `resilience.yaml: resilience.gpu.classes` (spec 08). Only one class runs at a time; the worker (spec 08) swaps them.

| Compose service | Compose profile / GPU class | Host port (loopback) | Health |
|---|---|---|---|
| `vllm-reasoning` | `reasoning` | 8000 | `GET /health` |
| `openjev` | `decider` | 8100 → container 8080 | `GET /v1/models` with bearer (spec 03; no `/health` documented) |
| `llamacpp-large` | `large` | 8200 → container 8080 | `GET /health` |

```yaml
name: herness
x-gpu: &gpu
  deploy: {resources: {reservations: {devices: [{driver: nvidia, count: all, capabilities: [gpu]}]}}}
  ipc: host
  restart: "no"                     # the worker (08) starts and stops services
x-offline-env: &offline
  HF_HUB_OFFLINE: "1"
  HF_HUB_DISABLE_TELEMETRY: "1"
  DO_NOT_TRACK: "1"
services:
  vllm-reasoning:
    <<: *gpu
    profiles: ["reasoning"]
    image: ${REASONING_IMAGE}                      # vllm/vllm-openai@sha256:...
    ports: ["127.0.0.1:${REASONING_PORT}:8000"]
    volumes: ["${MODEL_ROOT}:/root/.cache/huggingface"]
    environment:
      <<: *offline
      VLLM_NO_USAGE_STATS: "1"
      VLLM_API_KEY: ${VLLM_API_KEY}
    command:                                       # entrypoint form: verify at Phase 4
      - --model=${REASONING_MODEL}
      - --revision=${REASONING_REVISION}
      - --served-model-name=${REASONING_SERVED_NAME}
      - --gpu-memory-utilization=${REASONING_GPU_UTIL}
      - --max-model-len=${REASONING_MAX_MODEL_LEN}
      - --enable-auto-tool-choice
      - --tool-call-parser=${REASONING_TOOL_PARSER}
      - --reasoning-parser=${REASONING_PARSER}
      - --host=0.0.0.0
      - --port=8000
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 900s
  openjev:
    <<: *gpu
    profiles: ["decider"]
    image: ${OPENJEV_IMAGE}                        # razorback16/openjev@sha256:... (tag 0.4.0)
    ports: ["127.0.0.1:${OPENJEV_PORT}:8080"]
    volumes: ["${MODEL_ROOT}:/root/.cache/huggingface"]
    environment:
      <<: *offline                                 # OpenJev honoring HF_HUB_OFFLINE: verify at Phase 4
      OPENJEV_BACKEND: vllm
      OPENJEV_MODEL: ${OPENJEV_MODEL}
      OPENJEV_GPU_UTIL: ${OPENJEV_GPU_UTIL}
      OPENJEV_MAX_NUM_SEQS: ${OPENJEV_MAX_NUM_SEQS}
      OPENJEV_CANVAS: ${OPENJEV_CANVAS}
      OPENJEV_API_KEY: ${OPENJEV_API_KEY}
    healthcheck:
      test: ["CMD", "python3", "-c", "import os,urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8080/v1/models', headers={'Authorization':'Bearer '+os.environ['OPENJEV_API_KEY']}), timeout=5)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 900s
  llamacpp-large:                                  # deep-mode lever 7: large model with CPU-RAM offload
    <<: *gpu
    profiles: ["large"]
    image: ${LARGE_IMAGE}                          # ghcr.io/ggml-org/llama.cpp server-cuda@sha256:...
    ports: ["127.0.0.1:${LARGE_PORT}:8080"]
    volumes: ["${MODEL_ROOT}/gguf:/models:ro"]
    command: ["-m", "/models/${LARGE_GGUF}", "--host", "0.0.0.0", "--port", "8080",
              "-c", "${LARGE_CTX}", "-ngl", "${LARGE_GPU_LAYERS}", "--api-key", "${VLLM_API_KEY}"]
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/health"]   # tool present in image: verify at Phase 4
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 1200s
```

OpenJev facts from its README (github.com/razorback16/openjev, read 2026-09-24):
- Image `razorback16/openjev:0.4.0`, 26 commits, Apache-2.0.
- Container port 8080; the README publishes it as `127.0.0.1:8080:8080`, while Herness uses host port 8100 to match spec 08. Run flags `--gpus all --ipc=host`; volume `~/.cache/huggingface:/root/.cache/huggingface`.
- Env: `OPENJEV_BACKEND` (default `vllm`), `OPENJEV_UPSTREAM`, `OPENJEV_MODEL` (default `nvidia/diffusiongemma-26B-A4B-it-NVFP4`), `OPENJEV_API_KEY`, `OPENJEV_GPU_UTIL` (0.9), `OPENJEV_MAX_NUM_SEQS` (64), `OPENJEV_CANVAS` (64).
- Endpoints `POST /v1/systemone`, `POST /v1/chat/completions`, `GET /v1/models`.
- About 18 GB of weights; needs 24 GB+ VRAM; tested on an RTX PRO 6000 Blackwell (sm_120).

Not documented, verify at Phase 4: a health endpoint, `python3` in the image, and offline behavior. Also open is whether NVFP4 weights run on pre-Blackwell GPUs (D7, 00 §10). If they do not, spec 03's fallback teacher, the local reasoning-LLM decider, takes OpenJev's role and the `decider` class carries only in-process Laya and embeddings.

#### 5.6.3 Install runbook

1. As admin: create `svc-herness`; enable BitLocker; `wsl --install -d Ubuntu-24.04 --name herness` while logged in as `svc-herness`; write `.wslconfig` (`networkingMode=mirrored`, `memory=` ≤ 50 % of RAM, `vmIdleTimeout=-1`); enable systemd in `/etc/wsl.conf`.
2. In WSL: install Docker Engine and NVIDIA Container Toolkit (`nvidia-ctk runtime configure --runtime=docker`); add the user to `docker`; `mkdir -p /opt/herness/hf`. Check: `docker run --rm --gpus all nvidia/cuda:<tag>-base nvidia-smi` shows the GPU.
3. On Windows: install `uv`; `git clone` to `D:\herness`; `uv sync`; apply ACLs (§5.5).
4. `herness secrets init` (creates `redact.hmac_key` and `ui_user_ref_key` and asks for escrow confirmation); `herness secrets set <name>` for each source and for `vllm.api_key` and `OPENJEV_API_KEY`; `herness config validate`.
5. `herness deploy render`; `herness deploy pull --allow-download` (opens the firewall window, pulls images by digest and weights by revision, closes it).
6. Before the worker service is registered: `herness deploy up reasoning` → healthy; `herness deploy down`; `herness deploy up decider` → healthy (if D7 fails, record it and switch spec 03's teacher to the LLM decider); `down`; the same check for `large` if deep mode will use it.
7. Register services (§5.6.4). `herness doctor` must pass all checks.

Checks this spec adds to spec 09's `herness doctor` (same PASS/WARN/FAIL table with a fix hint per row): Python 3.12 and `uv`; config validate; all secret refs exist; ACLs on `data\`; BitLocker on; audit hash chain intact; WSL distro present, systemd and dockerd running; `nvidia-smi` in WSL; GPU VRAM ≥ 24 GB; images present with pinned digests; weights present at pinned revisions; health of the running model server; ports 8000, 8100, 8200 and `security.ui.port` listening on loopback only; socket-guard self-test (a connect to `1.1.1.1:443` must raise `EgressBlocked` in `local`); firewall rules present; free disk ≥ 50 GB; clock skew < 5 s.

#### 5.6.4 Services

- **Worker.** Recommended: NSSM service `herness-worker` running `D:\herness\.venv\Scripts\herness.exe worker --gpu-class none,reasoning,decider,large` as `.\svc-herness`. Settings: `AppStopMethodConsole` with a 150 s wait so spec 08's graceful shutdown runs; stdout and stderr to `data\logs`; restart on exit after 60 s. Alternative: Task Scheduler "at startup, run whether user is logged on or not, restart every 1 min". Its stop is a hard kill, and spec 08's leases then recover the work.
- **WSL boot.** A Task Scheduler task `herness-wsl` at startup, as `svc-herness`, runs `wsl.exe -d herness -- systemctl is-active docker` to boot the distro. `.wslconfig` has `vmIdleTimeout=-1`. Whether `wsl.exe` works from a non-interactive session must be verified at Phase 4. If it does not, the fallback is an auto-logon session for `svc-herness` with both tasks triggered at logon.
- **GPU control from Windows.** Docker Engine runs inside WSL, so the Windows `docker` CLI cannot reach it. On this host, `resilience.yaml` sets:
  `resilience.gpu.compose_cmd: [wsl.exe, -d, herness, --, docker, compose, --env-file, /opt/herness/docker.env]` and `resilience.gpu.compose_file: /mnt/d/herness/docker/compose.yaml`, which is a WSL path.
  `resilience.gpu.vram_check_cmd` keeps Windows `nvidia-smi`. `config validate` checks that the compose file path is reachable through `compose_cmd`.
- **Dashboard.** NSSM service `herness-ui` running `herness ui`, which binds `security.ui.bind:security.ui.port`.
- **Caddy** (only for LAN exposure): a Windows service installed with its own installer or with NSSM.

#### 5.6.5 Upgrade and rollback of pinned models

Upgrade: edit `deploy.reasoning.{image,model,revision}` (and the matching `models.yaml` entry) on a branch → `herness deploy pull --allow-download` → `herness eval` against the candidate (spec 11) → merge only if it is not worse on the golden set → `herness deploy render && herness deploy up reasoning`. The `config_change` audit line records old and new hashes. Rollback: revert the config commit, `herness deploy render && herness deploy up reasoning`. Old image digests and old weight revisions stay on disk until two newer versions have been promoted (`herness deploy prune`), so rollback needs no download. The same flow applies to OpenJev, the large GGUF model and Laya checkpoints under `data/models/<name>/<version>/`.

## 6. Errors and resilience

| Condition | Error | Handling |
|---|---|---|
| Invalid YAML, unknown key, failed cross-check, gated profile without approval | `ConfigError` | process exits before any job; `herness doctor` shows the issue |
| Secret missing | `ConfigError("secret not found: <name>")` | name only; never the value |
| Keyring backend unavailable | `ConfigError` | doctor hint: run as the account that owns the credential |
| Egress refused by any rule | `EgressBlocked` (`FatalError`, 00 §7) | not retried; the fallback chain (spec 08) may choose a local model instead; the task records the reason |
| Redaction exception on one record | none raised | `text = NULL`, counter increments, DQ warns |
| Model server unhealthy | `ModelUnavailable` | spec 08 retries and re-runs its swap procedure |
| Rekey job fails before the key swap | `FatalError` in the job | old key stays active; the `.next` key is kept for the retry |
| Deletion step fails | step error in `deletion_request.steps` | request stays `running`; the job retries from that step |
| Audit write fails | `StoreBusy` retried 3×, then `FatalError` | an action that cannot be audited does not happen (approvals, egress, deletes) |

## 7. Configuration

### 7.1 Security block (`herness.yaml: security`)

See §4.2. Schema: `SecurityConfig` in `herness/core/config.py`.

### 7.2 Redaction

```yaml
security:
  redaction:
    mask_ip: true
    directory_file: "D:/herness/private/directory.csv"   # columns: display_name, alt_names
    extra_names: []
    id_patterns: {EMPLOYEE_ID: ['\bE\d{6}\b'], USER_ID: []}
    national_id_patterns: ['\b\d{3}-\d{2}-\d{4}\b']
    custom_patterns: {}
    ner: none              # none | presidio
    key: "secret:redact.hmac_key"
```

### 7.3 UI and access (`herness.yaml: security.ui`)

Spec 09 reads this block for binding, identity and roles. `app.yaml` defines no host, port or role list.

```yaml
security:
  ui:
    bind: 127.0.0.1
    port: 8501
    expose:
      enabled: false                    # LAN access only behind the reverse proxy (§9.2)
      trusted_proxy: null               # e.g. "127.0.0.1" when Caddy on this host fronts the app
      identity_header: X-Forwarded-User
    roles:
      admins: []                        # usernames, case-insensitive
      reviewers: []
      default_role: viewer              # viewer | denied; use denied when expose.enabled
```

### 7.4 Deploy

```yaml
deploy:
  wsl_distro: herness
  model_root: /opt/herness/hf           # ext4 inside WSL2, not /mnt/d
  env_file: /opt/herness/docker.env
  reasoning: {image: "vllm/vllm-openai@sha256:<digest>", model: "Qwen/Qwen3-30B-A3B-Instruct",   # pinned weights
              revision: "<commit-sha>", served_name: "local-30b",       # = models.yaml clients.local-30b.model (spec 05)
              gpu_memory_utilization: 0.90, max_model_len: 32768,       # ≥ models.yaml context_window
              tool_call_parser: "<per model>", reasoning_parser: qwen3, port: 8000}
  openjev: {image: "razorback16/openjev@sha256:<digest of 0.4.0>",
            model: "nvidia/diffusiongemma-26B-A4B-it-NVFP4", revision: "<commit-sha>",
            gpu_util: 0.9, max_num_seqs: 64, canvas: 64, port: 8100}
  large: {image: "ghcr.io/ggml-org/llama.cpp@sha256:<server-cuda digest>", gguf: "<file>.gguf",
          sha256: "<file hash>", ctx: 32768, gpu_layers: 20, port: 8200}
  service: {manager: nssm, account: svc-herness}   # nssm | task_scheduler
```

`herness deploy render` writes `deploy.env_file` (mode 600, owner root) from `deploy.*` plus the resolved `vllm.api_key` and `OPENJEV_API_KEY`. That file is the only place secrets reach containers.

## 8. Performance targets

| Operation | Target |
|---|---|
| `load_config` + validation (no secret checks) | < 1 s |
| `config_hash` | deterministic across runs, OS and key order; < 50 ms |
| Redaction, directory of 100k names, avg 1.5 KB text | ≥ 5,000 records/s per core; 6M records < 5 min on 16 cores |
| `Redactor.scan` in egress check | < 50 ms per MB of payload |
| Socket audit hook overhead | < 20 µs per connect |
| `herness doctor` (models running) | < 60 s |
| Nightly ops backup (1 GB ops store) | < 2 min |

## 9. Security

### 9.1 Threat stance

- Ticket text, Jira text and monitoring messages are untrusted data. Roles (spec 05) place them inside delimited blocks (`<untrusted_data source="..." record_id="...">…</untrusted_data>`) with a standing instruction that content inside is data, never instructions.
- Tools cannot write anything except `finding` rows and memory proposals (`memory_item.status = 'pending_approval'` or `review_item`). No tool sends email, calls URLs or runs shell commands.
- Agent SQL (spec 05) runs on a DuckDB connection opened `read_only=True`, then `SET enable_external_access = false; SET lock_configuration = true;`, which blocks `read_csv` on arbitrary paths, `httpfs`, `INSTALL`/`LOAD`, `ATTACH` and `COPY`. Row cap and timeout per spec 05. Agents see `enrich.text_redacted`, never `core.*.description` (spec 02 §10).
- Rendered model output (chat, reports) strips external images and non-relative links, so injected markdown cannot exfiltrate data through the browser. Static report HTML under `data/reports/` carries spec 09's CSP `default-src 'none'; style-src 'unsafe-inline'; img-src data:`. The reverse proxy sets `default-src 'self'; img-src 'self' data:` on the dashboard.
- Anything sent off-network in hybrid is aggregated evidence (numbers, cluster labels, redacted excerpts), never raw tickets; the egress re-scan enforces it. Premium requires a data-processing agreement and zero-retention settings with each provider, recorded in `security.data_policy`.

### 9.2 Network

- Dashboard binds `security.ui.bind:security.ui.port` (default `127.0.0.1:8501`). `.streamlit/config.toml`: `server.address = "127.0.0.1"`, `server.headless = true`, `server.enableXsrfProtection = true`, `browser.gatherUsageStats = false`.
- LAN exposure only through a reverse proxy on the same host (Caddy recommended) terminating TLS with an internal certificate, authenticating with OIDC (Entra ID via `oauth2-proxy` forward-auth) or Windows Integrated Auth, and passing `X-Forwarded-User`. The app trusts `security.ui.expose.identity_header` only when `expose.enabled` and `trusted_proxy` are set and the request comes from that address. It maps the user to `viewer | reviewer | admin` from `security.ui.roles`. Reviewer is required to approve `review_item` rows; admin for config, deploy and privacy actions.
- Container ports are published on `127.0.0.1` only. The Docker API is never exposed on TCP.
- Windows Firewall, defense in depth: use WSL mirrored networking (`.wslconfig`: `networkingMode=mirrored`) so Hyper-V firewall rules apply to WSL traffic, then set default outbound block for the WSL VM (`Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultOutboundAction Block`) and allow outbound only during `herness deploy pull`. Also add an inbound block for 8000, 8100, 8200 and the UI port from non-loopback. The VM creator ID and cmdlets must be verified at Phase 4.
- Containers run with `HF_HUB_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`, `VLLM_NO_USAGE_STATS=1`, `DO_NOT_TRACK=1` after weights are pulled.

### 9.3 Accounts

A dedicated local, non-admin Windows account `svc-herness` runs the worker, the dashboard and owns the WSL distro (WSL distros are per-user). Human admins use their own accounts; admin actions go through the CLI with audit.

## 10. Tests and acceptance criteria

- **Precedence**: table-driven tests with a temp `config/` for each layer pair (file < profile < env < CLI); lists replace, maps merge; a missing required file and an unknown key → `ConfigError`; `--profile hybrid` sets `cfg.profile = "hybrid"` and applies the hybrid `models.roles` overlay; `HERNESS_SECURITY__EGRESS__ENABLED=true` → `ConfigError`.
- **Profile gates**: `hybrid` without `hybrid_approved` fails validation; `local` and `synth` have empty egress; `synth` rejects an overlay that enables egress.
- **config_hash**: same config with reordered keys and different `logging.level` → same hash; changed weight → different hash; hash contains no secret value.
- **Egress blocked in local**: a fake socket (monkeypatched `socket.socket.connect` recording calls) plus `respx`: every `http_client()` request in `local` raises `EgressBlocked` and the fake socket records zero connects. Also via the audit hook: raw `socket.create_connection(("1.1.1.1", 443))` raises.
- **Egress checks**: in `hybrid`, a payload containing a raw email or a `password=...` is blocked and logged with hit counts; a payload with only pseudonym tokens passes; unknown host, `http://`, oversized body and daily token cap each block. The log line never contains payload text (assert the payload's unique marker string is absent from all log files).
- **Lint**: AST scan fails on direct HTTP client construction outside allowed modules (§3.5).
- **Redaction corpus** (spec 11 `tests/unit/test_redaction_corpus.py`, ≥ 2,000 synthetic sentences with labeled spans and several phone and name formats). Recall must be ≥ 0.99 on every patterned type (`EMAIL`, `PHONE`, `IP`, `CARD`, `NATIONAL_ID`, `EMPLOYEE_ID`, `CREDENTIAL`, `URL_TOKEN`), which is stricter than spec 11's floor for IDs and credentials. Directory names need recall ≥ 0.95, and overall precision must be ≥ 0.90. Ticket numbers (`INC0012345`) are never masked. Pseudonym stability: same person in 3 formats → one token; different key → different tokens.
- **Throughput**: benchmark ≥ 5,000 records/s on one core on the corpus replayed to 100k records.
- **Secrets never leak**: run an end-to-end fake pipeline (sync with fixture connector, chat turn with a mocked LLM) with sentinel secret values; grep `data/logs/`, `data/traces/`, `data/config_snapshots/`, `ops.sqlite` dump and rendered reports for each sentinel → zero hits.
- **Audit**: approving a `review_item` writes one `review_decision` line; tampering one line breaks the chain and doctor fails.
- **Deletion**: after `privacy delete`, the record is absent from the lake, decision cache, labels, vectors, evidence samples and the next build, and the next fixture sync does not re-ingest it. A sync that commits the record between step 1 and its next checkpoint is cleaned by the second lake pass (step 7).
- **Rekey**: on a fixture with human and gold labels, a rekey job maps every label to its new `content_hash` with no loss. Killing the job before the key swap leaves the old key active and all hashes unchanged.
- **Backup**: backup during concurrent ops writes yields a copy that passes `integrity_check`.
- **Deployment (Phase 4, manual + scripted)**: `herness doctor` all pass on the target PC; each compose profile becomes healthy; only one GPU service runs at a time; `netstat` shows loopback-only binds.

## 11. Open questions

1. D5 (00 §10): is hybrid allowed by data policy? Until it is answered, `hybrid` and `premium` fail validation.
2. D7 (00 §10): OpenJev's default NVFP4 weights were tested only on Blackwell. On an Ada or Ampere 24 GB card they may not run. Verify at Phase 4. The fallback is spec 03's LLM-decider teacher (§5.6.2).
3. Hosted Jev base URL (`api.typesafe.ai` in spec 03, `api.codiv.ai` in the OpenJev README), auth scheme and data-retention terms (premium). Verify at Phase 4.
4. Service-session `wsl.exe` behavior and the WSL Hyper-V firewall cmdlets on the target Windows build. Verify at Phase 4.
5. Source of the name directory: a corporate HR export or a `sys_user` sync (the latter would add an entity to spec 01).
6. Corporate retention rules may override the defaults in `retention.*` (legal hold, 7-year audit).
7. SSO provider for LAN exposure (Entra ID assumed).
8. Cross-spec items:
   - Resolved (spec 08 v2 §7): `resilience.gpu.compose_cmd` goes through `wsl.exe`, `compose_file` is a WSL path, and OpenJev health is `GET /v1/models` with the bearer key.
   - Resolved (spec 09 v2 §5.6, §7, §9): UI binding, identity and roles live only in `herness.yaml: security.ui`; `user_ref` uses the keyring secret `ui_user_ref_key`; `herness init` points to `secrets init`; the `doctor` and `config validate` rows list §5.6.3 and `--offline`/`--strict`.
   - Resolved (spec 08 v2 §5.8, §5.11): `deploy up|down` goes through the worker or the GPU lock file (§3.7); rekey runs in spec 08's planned Saturday slot (§5.3).
   - **Spec 05 Q1:** `core.work_item.summary` (Jira titles) can contain names. This spec's position: do not block the column, but tools and the UI pass it through `redact_text()` before it reaches a model or a screen.

## 12. Dependencies

Specs:
- 00: contracts, errors, IDs, D5, D7.
- 01: source hosts, secret names, deletion-set reload.
- 02: tables written or purged here; `deletion_request`.
- 03: redaction consumer, `purge_record`, decision cache and labels re-keyed by rekey, fallback teacher.
- 05: LLM adapters use the guard; `models.yaml` profiles; trace redaction.
- 08: `resilience.yaml: gpu` drives compose; `maintenance` jobs; service stop semantics.
- 09: CLI rows, `doctor`, dashboard auth, report CSP.
- 11: PII corpus, fixture scanner, `synth` profile, eval before upgrades.

Packages: `pydantic-settings`, `pyyaml`, `keyring`, `httpx`, `structlog`, `pyahocorasick`; optional extra `ner` (`presidio-analyzer`, `spacy`).
Tools: Docker Engine, NVIDIA Container Toolkit, WSL2, NSSM or Task Scheduler, optionally Caddy and oauth2-proxy.

## 13. Contract changes (resolved)

1. Core modules `registry.py`, `secrets.py`, `egress.py`, `audit.py` and the per-package `settings.py` convention → 00 §3.
2. Config file list, including `herness.yaml` scope and `profiles/synth.yaml` → 00 §11.
3. Log files `egress-<date>.jsonl`, `audit-<date>.jsonl` and `data/config_snapshots/` → 00 §4.
4. IDs `config_hash` (`cfg_`), `egress_id` (`egr_`), `audit_id` (`aud_`), `request_id` (`del_`) → 00 §5.
5. `EgressBlocked(FatalError)` → 00 §7.
6. Dependencies `keyring`, `pyahocorasick`, optional `ner` extra → 00 §9.
7. `job.kind = 'maintenance'` (backup, purge, privacy delete, rekey) → 02 §5.2.
8. `deletion_request` table and staging filter → 02 §5.5 and §4.2; connector filter → 01 §5.6.
9. OpenJev on non-Blackwell GPUs tracked as D7 → 00 §10.
10. CLI commands in §3.7 → spec 09 command table.
