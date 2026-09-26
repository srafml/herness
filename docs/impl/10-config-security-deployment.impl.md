# 10 — Configuration, Security and Deployment: Implementation Spec

Status: Draft v2 · 2026-09-24 (consistency pass: applies the rulings of [`DECISIONS.md`](DECISIONS.md), cited as `R-nn`) · Design spec: [`docs/specs/10-config-security-deployment.md`](../specs/10-config-security-deployment.md) (design 10) · Phases: 1 (config, registry, secrets, redaction, audit), 3 (egress guard, socket guard, backup and purge), 4 (containers, deploy, doctor, privacy deletion, rekey) · Standards: [`ENG-STANDARDS.md`](ENG-STANDARDS.md) (ENG) · Depends on implementation specs: 00 (errors, ids, canonical JSON, logging, import-linter settings exception), 01 (source settings including `sources.<name>.hosts`, deletion-set reload), 02 (ops store core, `deletion_request` table, lake purge primitives, build cleanup), 03 (`purge_record`, classifier text), 04 (metric validator), 05 (model settings, Anthropic adapter, trace writer), 07 (`MemoryStore.purge`), 08 (jobs, GPU arbiter, metric sink), 09 (CLI command table, doctor command, chat mode selection), 11 (fixtures, corpus, `synth` profile).

This document contains no code. Signatures are tables. Regular expressions, commands and file paths are literal identifiers in backticks. Commands that run external programs are given as argument lists, element by element, separated by spaces inside one pair of backticks; every element is passed as one `argv` item with no shell.

## 1. Scope and traceability

This spec implements everything design 10 assigns to `herness/core/{config,registry,secrets,redact,egress,audit}.py`, the `config/` templates and profiles, the `docker/compose.yaml` file, data protection (folder ACL checks, BitLocker checks, backup, retention purge, privacy deletion, redaction rekey) and deployment (`herness deploy render|pull|up|down|rollback|prune|install`, the checks added to `herness doctor`). It applies ENG delta E4: `herness deploy install` verifies the SLSA Build L2 provenance attestation and the attested CycloneDX SBOM of a release before it installs anything, and `herness deploy pull` verifies every image digest and weight hash after download. Helper modules that the size limit (ENG §2.4) forces out of the six design modules live next to them in `herness/core/`. Operator commands, job handlers and doctor checks, which must import L1–L4 packages, live in the L5 package `herness.admin` (R-07). The ops-store area `herness/store/ops/privacy.py` (`deletion_request` and the privacy rewrites of `evidence.result_sample` and `finding.numbers`) is owned here (R-08). This spec also owns `herness.core.egress.loopback_http_client` and the socket-guard allowlist built from `sources.<name>.hosts` (R-06), the chat approval flag for `hybrid` (R-38), the `EgressBlocked` and `ConfigError` attributes (R-19), and the redaction corpus thresholds (R-56). The CLI command table itself is owned by impl 09 (R-47); this spec specifies command behavior only.

### 1.1 Traceability matrix

| Design § | Requirement (short) | Impl § | Units | Tasks | Tests |
|----------|---------------------|--------|-------|-------|-------|
| 1 | Purpose and scope | 1, 2 | all | all | all |
| 2.1 | One immutable config per process, fixed precedence | 3.1, 5 F10-01 | U10-08, U10-09, U10-10, U10-15–U10-19 | T10-02, T10-03 | UT10-01–UT10-04, UT10-22, PT10-06 |
| 2.2 | Reject invalid config; stable `config_hash` | 3.1, 5 F10-01 | U10-11, U10-13, U10-20 | T10-03, T10-12 | UT10-15–UT10-17, UT10-19, PT10-01, BT10-02 |
| 2.3 | Secrets resolved at use; never written out | 3.3, 7 | U10-27–U10-34 | T10-06, T10-07 | UT10-28–UT10-35, ST10-14–ST10-16 |
| 2.4 | PII masking with stable pseudonyms at ≥ 5k rec/s/core | 3.4 | U10-35–U10-49 | T10-08–T10-11, T10-15 | UT10-36–UT10-47, PT10-02–PT10-05, IT10-02, BT10-03, BT10-04 |
| 2.5 | Every off-network call through the guard; `local` blocks before a socket | 3.5, 5 F10-03, F10-04 | U10-50–U10-59, U10-107 | T10-16–T10-18 | UT10-48–UT10-56, UT10-79, ST10-07–ST10-13, ST10-54–ST10-56, IT10-03, IT10-04, IT10-15 |
| 2.6 | Append-only audit trail | 3.6, 4.3 | U10-60–U10-64 | T10-05 | UT10-57–UT10-59, ST10-17, ST10-18, PT10-08, FT10-01 |
| 2.7 | Pinned deployment and doctor | 3.7, 5 F10-06–F10-09 | U10-78–U10-95 | T10-22–T10-28, T10-31 | UT10-60–UT10-66, UT10-71, ST10-19–ST10-24, IT10-13, IT10-14 |
| 3.1 | `HernessConfig`, `load_config`, `get_config`, `config_hash`, `effective_dict`, `validate`, `ConfigIssue`; section models next to owners; owner validators run by the composition root | 3.1 | U10-01–U10-21, U10-109 | T10-01–T10-03, T10-12 | UT10-01–UT10-24, UT10-81 |
| 3.2 | Registry: `register`, `get`, `available`, `_BUILTINS`, entry points | 3.2 | U10-23–U10-26 | T10-04 | UT10-25–UT10-27, ST10-46 |
| 3.3 | Secrets API, two reference forms, backends, name table | 3.3 | U10-27–U10-34 | T10-06, T10-07 | UT10-28–UT10-35, ST10-26 |
| 3.4 | Redaction API, callers, fixture scanner | 3.4 | U10-35–U10-49 | T10-08–T10-11 | UT10-36–UT10-47, ST10-30 |
| 3.5 | Egress API; lint rule; loopback client and source client (R-06); source `hosts` allowlist (R-06) | 3.5 | U10-50–U10-59, U10-110 | T10-16–T10-18, T10-33 | UT10-48–UT10-56, UT10-74, UT10-82, ST10-25, ST10-54, ST10-55, ST10-58, ST10-59 |
| 3.6 | `audit(event, actor, **fields)` | 3.6 | U10-60 | T10-05 | UT10-57, UT10-58 |
| 3.7 | CLI surface behavior (command table owned by impl 09, R-47) | 3.7 | U10-65–U10-77 | T10-14, T10-20, T10-24–T10-30 | UT10-20, UT10-72, UT10-73, IT10-11, ST10-36 |
| 4.1 | Files, precedence, file stems, profile overlay, file-only `security.*`, sources order | 3.1, 5 F10-01 | U10-15–U10-19 | T10-02, T10-03 | UT10-01–UT10-14, ST10-01, ST10-02 |
| 4.2 | `herness.yaml` skeleton; owners of other files | 3.1, 9, 12 T10-13 | U10-02–U10-07, U10-109 | T10-01, T10-12, T10-13 | UT10-76, IT10-01 |
| 4.3 | Profiles and gates | 3.1, 3.5 | U10-09, U10-20, U10-50 | T10-03, T10-12, T10-13 | UT10-09, UT10-11–UT10-14, ST10-03 |
| 4.4 | `config_hash` definition and snapshot | 3.1, 4.2 | U10-11, U10-63 | T10-03, T10-05 | UT10-15–UT10-17, UT10-21, PT10-01 |
| 4.5 | Egress log line | 4.2, 3.5 | U10-57, U10-54 | T10-16, T10-17 | UT10-54, ST10-13, ST10-40 |
| 4.6 | Audit log line, hash chain, event table | 4.2, 3.6 | U10-60–U10-64 | T10-05 | UT10-57–UT10-59, ST10-17 |
| 5.1 | Config load flow, cross-checks, exit codes (R-46) | 5 F10-01, 3.1 | U10-09, U10-13, U10-20, U10-65, U10-108 | T10-03, T10-12, T10-14 | UT10-19, UT10-20, UT10-80, BT10-01 |
| 5.2 | Secrets at point of use; scrubber; rotation; least-privilege accounts | 3.3, 7 | U10-28, U10-32, U10-68 | T10-06, T10-07, T10-14 | UT10-34, UT10-35, UT10-72, ST10-15 |
| 5.3 | Detection order, detectors, pseudonyms, key, rekey, NER option, fail closed, throughput | 3.4, 5 F10-10 | U10-36–U10-49, U10-101–U10-103 | T10-08–T10-11, T10-30 | UT10-36–UT10-47, UT10-70, FT10-02, IT10-07 |
| 5.4 | Guard steps 1–7, `model_download`, socket guard, containers outside | 3.5, 5 F10-03, F10-04 | U10-51–U10-58 | T10-16–T10-18 | UT10-48–UT10-56, ST10-07–ST10-12, ST10-29, ST10-35 |
| 5.5 ACLs | Folder ACLs checked by doctor | 3.7 | U10-90 | T10-27 | UT10-71, ST10-38 |
| 5.5 BitLocker | BitLocker check | 3.7 | U10-90 | T10-27 | UT10-71, ST10-49 |
| 5.5 Backup | Online ops backup, integrity check, copies, keep policy | 3.7, 5 F10-11 | U10-96, U10-97 | T10-20 | UT10-67, IT10-08, FT10-04, BT10-08 |
| 5.5 Retention | Nightly purge per key with audit counts (lake exception, R-57) | 3.7, 5 F10-12 | U10-98 | T10-20 | UT10-68, IT10-09 |
| 5.5 Deletion | Seven-step privacy deletion plus memory purge step 3b (R-54); deletion set read by connectors and staging | 3.7, 4.1, 5 F10-13 | U10-99, U10-105, U10-111 | T10-29, T10-32 | UT10-69, UT10-77, UT10-83, IT10-06, FT10-03, ST10-31, ST10-50, ST10-57 |
| 5.6.1 | Prerequisites | 3.7, 8 | U10-90, U10-91 | T10-27, T10-28 | UT10-71, IT10-13 |
| 5.6.2 | `docker/compose.yaml` | 12 T10-23, 4.2 | U10-80 | T10-23 | UT10-75, ST10-43, ST10-44 |
| 5.6.3 | Install runbook and doctor checks | 3.7, 5 F10-09 | U10-89–U10-91 | T10-27, T10-28 | UT10-71, IT10-12, IT10-13, BT10-07 |
| 5.6.4 | Services (NSSM, WSL boot task, compose via `wsl.exe`, UI, Caddy) | 3.7 | U10-91 | T10-28 | UT10-71, IT10-13 |
| 5.6.5 | Upgrade and rollback, prune | 3.7, 5 F10-08 | U10-84, U10-85 | T10-25 | UT10-65, ST10-52 |
| 6 | Errors and resilience table | 6 | all | all | FT10-01–FT10-09 |
| 7.1 | `SecurityConfig` in `config.py`; chat approval flag (R-38) | 3.1, 9 | U10-03, U10-107 | T10-01, T10-16 | UT10-19, UT10-79, ST10-56 |
| 7.2 | Redaction block | 3.1, 9 | U10-04 | T10-01 | UT10-19, ST10-37 |
| 7.3 | UI and access block; identity-header trust conditions (R-50) | 3.1, 9 | U10-05, U10-20 (C05) | T10-01, T10-12 | UT10-19, ST10-27 |
| 7.4 | Deploy block and `deploy render` | 3.1, 3.7, 9 | U10-07, U10-79 | T10-01, T10-23 | UT10-60, UT10-61, ST10-34 |
| 8 | Performance targets | 10 | U10-09, U10-11, U10-42, U10-47, U10-51, U10-58, U10-89, U10-96 | T10-15, T10-18, T10-20, T10-27 | BT10-01–BT10-08 |
| 9.1 | Threat stance (untrusted data blocks, read-only tools, DuckDB lockdown, output stripping, hybrid payload) | 7 | U10-51 (payload re-scan); others realised by impl 05, impl 06, impl 09 | T10-16 | ST10-10, ST10-11; owner specs' tests |
| 9.2 | Network: UI bind, reverse proxy, loopback container ports, Hyper-V firewall, offline env | 3.7, 7 | U10-20 (C05), U10-80, U10-81, U10-90, U10-91, U10-95 | T10-12, T10-23, T10-24, T10-27, T10-28 | ST10-27, ST10-28, ST10-43, ST10-44, ST10-47 |
| 9.3 | Accounts (`svc-herness`) | 3.7 | U10-90, U10-91 | T10-27, T10-28 | UT10-71 |
| 10 Precedence | Layer pairs, list/map merge, missing file, unknown key, `--profile hybrid`, env `security` override | 11 | U10-09, U10-15–U10-19 | T10-02, T10-03 | UT10-01–UT10-10 |
| 10 Profile gates | Gates, empty egress in `local`/`synth` | 11 | U10-09, U10-20 | T10-03, T10-12 | UT10-11, UT10-12 |
| 10 config_hash | Reorder, logging change, weight change, no secret | 11 | U10-11 | T10-03 | UT10-15–UT10-17, PT10-01 |
| 10 Egress blocked in local | Fake socket plus respx; audit hook | 11 | U10-51, U10-58 | T10-17, T10-18 | IT10-04, ST10-07, ST10-08, IT10-15 |
| 10 Egress checks | PII, credential, pseudonym-only, host, scheme, size, daily cap, no payload in logs | 11 | U10-51 | T10-16, T10-17 | UT10-48–UT10-54, ST10-09–ST10-13 |
| 10 Lint | AST scan for direct HTTP clients | 11 | tests only | T10-17 | ST10-25 |
| 10 Redaction corpus | Recall ≥ 0.99 patterned, ≥ 0.95 names, precision ≥ 0.90, ticket numbers, stability | 11 | U10-41, U10-42 | T10-15 | IT10-02, UT10-38, UT10-39 |
| 10 Throughput | ≥ 5,000 records/s/core | 10 | U10-42, U10-47 | T10-15 | BT10-03, BT10-04 |
| 10 Secrets never leak | Sentinel end-to-end grep | 11 | U10-32 | T10-21 | ST10-14 |
| 10 Audit | One `review_decision` line; tamper breaks chain | 11 | U10-60, U10-62 | T10-05, T10-21 | IT10-05, ST10-17 |
| 10 Deletion | All stores clean, memory included (R-54); second pass | 11 | U10-99 | T10-29 | IT10-06, ST10-50, ST10-57 |
| 10 Rekey | Labels mapped without loss; kill before swap | 11 | U10-101–U10-103 | T10-30 | IT10-07, FT10-02 |
| 10 Backup | Concurrent writes, `integrity_check` | 11 | U10-96 | T10-20 | IT10-08 |
| 10 Deployment | Doctor all pass on target; each profile healthy; one GPU service; loopback binds | 11 | U10-89–U10-91 | T10-31 | IT10-13 |
| 11 | Open questions | 13 | — | blocked cards in 12 | — |
| 12 | Dependencies | 14 | — | — | — |
| 13 | Contract changes (resolved) | 13 | — | — | — |
| DECISIONS | Rulings R-01–R-66 that bind this spec | 13.1 status column | U10-03, U10-09, U10-11, U10-20, U10-21, U10-27, U10-58, U10-59, U10-65, U10-99, U10-105–U10-111 | T10-03, T10-12, T10-16–T10-18, T10-29, T10-32, T10-33 | UT10-77–UT10-83, ST10-54–ST10-59 |
| ENG E4 | `deploy install` verifies attestation and SBOM | 3.7, 5 F10-07 | U10-86–U10-88, U10-112 | T10-26 | ST10-22–ST10-24, ST10-48, ST10-53, ST10-60, UT10-85, IT10-14 |
| ENG §5.6 | Image and weight digests verified on pull | 3.7, 5 F10-06 | U10-81–U10-83 | T10-24 | ST10-19–ST10-21, UT10-62 |

## 2. Module map

Line budgets are production lines including docstrings. "Extra imports" lists imports beyond the layer defaults of ENG §2.1.

| Path | Purpose | Public symbols | Layer | Extra imports | Line budget |
|------|---------|----------------|-------|---------------|-------------|
| `herness/core/settings.py` | Pydantic models of the `herness.yaml` sections | `PathsConfig`, `DataPolicyConfig`, `SecretsConfig`, `RedactionConfig`, `EgressConfig`, `NetworkConfig`, `ExposeConfig`, `RolesConfig`, `UiConfig`, `SecurityConfig`, `LoggingConfig`, `RetentionConfig`, `BackupConfig`, `ReasoningDeploy`, `OpenJevDeploy`, `LargeDeploy`, `ServiceDeploy`, `ReleaseDeploy`, `DeployConfig` | L0 | `pydantic` only (settings-module rule, R-03) | 370 |
| `herness/core/config_sources.py` | YAML reading, layer sources, override parsing, bootstrap config | `load_yaml_file`, `FilesYamlSource`, `ProfileYamlSource`, `GuardedEnvSource`, `FilteredDotEnvSource`, `parse_overrides`, `BootstrapConfig`, `load_bootstrap` | L0 | `pydantic_settings`, `yaml` | 390 |
| `herness/core/config.py` | Root model, load, cache, hash, effective dict; re-exports `SecurityConfig` | `ProfileName`, `HernessConfig`, `load_config`, `init_config`, `get_config`, `reset_config`, `config_hash`, `effective_dict`, `validate`, `ConfigIssue` | L0 | the `settings.py` module of any package (named import-linter exception encoded by impl 00, R-03) | 320 |
| `herness/core/config_validate.py` | Cross-checks C01–C25 and the start-up owner-validator hook | `CROSS_CHECKS`, `CrossCheckRow`, `run_cross_checks`, `enforce_offline_checks`, `sort_issues`, `OwnerValidator`, `register_owner_validator`, `run_owner_validators`, `run_startup_validators`, `reset_owner_validators` | L0 | none | 390 |
| `herness/core/config_checks.py` | Pure offline cross-check rows of U10-20 (size-forced helper of `config_validate.py`, §1) | `CheckContext`, `Hit`, `Severity`, `CLIENTS`, `get`, `items`, `enabled_sources`, `loopback`, `port`, `row_c01`, `row_c02`, `row_c04`, `row_c05`, `row_c07`, `row_c09`, `row_c10`, `row_c11`, `row_c12`, `row_c14`, `row_c17`, `row_c20`, `row_c21`, `row_c24`, `row_c25` | L0 | none | 280 |
| `herness/core/registry.py` | Implementation registry | `Kind`, `register`, `get`, `available`, `reset_registry` | L0 | `importlib.metadata` | 160 |
| `herness/core/secrets.py` | Secret references, backends, resolution, scrubber | `SECRET_NAME`, `SecretRef`, `SecretRefStr`, `SecretNameStr`, `resolve`, `resolve_json`, `exists`, `set_secret`, `delete_secret`, `known_values`, `scrub_secrets`, `referenced_secret_names` | L0 | `keyring` | 360 |
| `herness/core/redact_patterns.py` | Compiled detectors, prefilters, Luhn, normalization | `Detector`, `build_detectors`, `luhn_valid`, `normalize_value`, `TOKEN_PATTERN` | L0 | none | 395 |
| `herness/core/redact_directory.py` | Name directory and Aho-Corasick matcher | `NameDirectory`, `update_display_names` | L0 | `ahocorasick` | 220 |
| `herness/core/redact.py` | Redactor, process-wide instance, table redaction | `EntityType`, `Span`, `RedactionResult`, `RedactionFailed`, `Redactor`, `get_redactor`, `reset_redactor`, `redact_text`, `redact_table` | L0 | `pyarrow` | 390 |
| `herness/core/_redact_pool.py` | Chunking, spawn worker pool and worker initializer of `redact_table` (size-forced private sibling of `redact.py`, T10-11 ruling; no public API outside `redact_table`) | `CHUNK_ROWS`, `Chunk`, `RowsOut`, `redact_rows`, `redact_chunks`, `spawn_args`, `string_column`, `worker_count` | L0 | `pyarrow` | 130 |
| `herness/core/redact_scan.py` | Fixture scanner behind `python -m herness.core.redact --scan` | `main` | L0 | `pyarrow.parquet` | 200 |
| `herness/core/egress.py` | Egress guard, guarded transports, clients, chat approval check | `Purpose`, `PayloadClass`, `EgressGuard`, `GuardedTransport`, `AsyncGuardedTransport`, `get_guard`, `reset_guard`, `cloud_chat_allowed`; re-exports `loopback_http_client`, `aloopback_http_client`, `source_http_client`, `install_socket_guard` | L0 | `httpx2` (with `egress_clients.py` the only modules that build HTTP clients and transports, R-06; `httpx2` because the locked `anthropic`/`openai` SDKs accept only `httpx2` clients, T10-17 ruling), `httpx` (`httpx.URL` parsing only) | 390 |
| `herness/core/egress_log.py` | Egress JSONL writer and daily token counter | `EgressLog` | L0 | none | 200 |
| `herness/core/_egress_scan.py` | URL shape checks (U10-51 step 3) and the PII re-scan (step 6) of `EgressGuard.check` (w07-s10 ruling (size-forced private sibling) of `egress.py`, leaving `egress.py` room for the T10-17 transports; no public API outside `herness.core.egress`) | `MAX_SCAN_CHARS`, `host_reason`, `rescan` | L0 | `httpx` (`httpx.URL` parsing only; builds no client or transport) | 120 |
| `herness/core/_egress_transport.py` | Guarded transports of the egress component: per-request check, counted and capped response stream, `completed` line with provider token counts (U10-54) (T10-17 size-forced private sibling of `egress.py`; `GuardedTransport`, `AsyncGuardedTransport` and `MAX_RESPONSE_BYTES` are re-exported from `herness.core.egress`, the public home) | `GuardedTransport`, `AsyncGuardedTransport`, `TransportOpts`, `MAX_RESPONSE_BYTES`, `USAGE_MAX_BYTES` | L0 | `httpx2` (subclasses the base transports; builds no client or transport) | 220 |
| `herness/core/egress_clients.py` | Loopback and source client factories and their host-restricting transports (part of the egress component; public names re-exported from `herness.core.egress`, R-06) | `LoopbackOnlyTransport`, `loopback_http_client`, `aloopback_http_client`, `SourceHostTransport`, `source_http_client` | L0 | `httpx2`, `certifi` | 300 |
| `herness/core/egress_socket.py` | Process socket guard (audit hook) | `SocketPolicy`, `install_socket_guard`, `reset_socket_guard` | L0 | none | 220 |
| `herness/core/audit.py` | Audit JSONL with hash chain, locked append, config-change record | `AuditEvent`, `audit`, `append_jsonl_locked`, `verify_chain`, `ChainReport`, `record_config_change`, `last_secret_set_times` | L0 | none | 395 |
| `herness/admin/__init__.py` | Package marker; registers the `maintenance` job handler | `register_handlers` | L5 | — | 30 |
| `herness/admin/commands_config.py` | `config validate|show|hash` behavior | `cmd_config_validate`, `cmd_config_show`, `cmd_config_hash` | L5 | none | 180 |
| `herness/admin/commands_secrets.py` | `secrets init|set|status|rekey` behavior | `cmd_secrets_init`, `cmd_secrets_set`, `cmd_secrets_status`, `cmd_secrets_rekey` | L5 | none | 260 |
| `herness/admin/commands_deploy.py` | `deploy *` behavior (thin wrappers) | `cmd_deploy_render`, `cmd_deploy_pull`, `cmd_deploy_up`, `cmd_deploy_down`, `cmd_deploy_rollback`, `cmd_deploy_prune`, `cmd_deploy_install` | L5 | none | 220 |
| `herness/admin/commands_data.py` | `privacy delete`, `maintenance backup|purge` enqueue | `cmd_privacy_delete`, `cmd_maintenance` | L5 | none | 140 |
| `herness/admin/wsl.py` | Argument-list runners for `wsl.exe`, `powershell.exe`, Windows tools | `run_cmd`, `run_wsl`, `run_powershell`, `CmdResult` | L5 | none | 180 |
| `herness/admin/deploy.py` | Env render, class up/down/rollback, history, prune | `render_env`, `class_up`, `class_down`, `rollback_class`, `prune`, `DeployHistory` | L5 | none | 390 |
| `herness/admin/deploy_pull.py` | Firewall window, image pull and verification, weight pull and verification | `firewall_window`, `pull_images`, `pull_weights`, `verify_weights`, `run_pull` | L5 | none | 390 |
| `herness/admin/attest.py` | Release bundle verification and install | `ReleaseBundle`, `verify_bundle`, `install_bundle` | L5 | none | 380 |
| `herness/admin/duckdb_ext.py` | Offline install and verification of pinned DuckDB extensions from the release bundle (impl 01 O-3) | `install_duckdb_extensions`, `EXT_INSTALL_SCRIPT` | L5 | none (runs the venv's `duckdb` in a subprocess) | 120 |
| `herness/admin/doctor_host.py` | Doctor checks for host, config, secrets, ACL, BitLocker, audit, disk, clock, ports, socket guard | `CheckResult`, `doctor_checks`, `HOST_CHECKS` | L5 | `psutil` | 390 |
| `herness/admin/doctor_gpu.py` | Doctor checks for WSL, Docker, GPU, images, weights, health, firewall, services | `GPU_CHECKS` | L5 | none | 380 |
| `herness/admin/maintenance.py` | `maintenance` job dispatch, backup, purge | `handle_maintenance`, `run_backup`, `select_backup_keep`, `run_purge` | L5 | none | 390 |
| `herness/admin/privacy.py` | Privacy deletion (seven design steps plus memory purge step 3b, R-54) | `run_privacy_delete` | L5 | none | 330 |
| `herness/store/ops/privacy.py` | Ops-store area `privacy` (R-08): `deletion_request` rows and the deletion-set read (the scrubs of `evidence.result_sample` and `finding.numbers` are impl 05's U05-75 and impl 06's U06-144, R-77) | `DeletionRequest`, `create_deletion_request`, `get_deletion_request`, `open_deletion_request`, `set_deletion_status`, `record_deletion_step`, `deleted_record_ids` | L1 | none | 250 |
| `herness/core/errors.py` (owned by impl 00; this spec declares two attribute sets, R-19) | `EgressBlocked.egress_id`, `EgressBlocked.reason`, `ConfigError.issues` | `EgressBlocked`, `ConfigError` | L0 | none | +30 lines |
| `herness/admin/rekey.py` | Redaction key rotation job | `run_rekey`, `rekey_labels`, `build_rekey_map` | L5 | `pyarrow.parquet` | 360 |
| `config/herness.yaml` | Root config template (§4.2 of design 10) | — | config | — | 110 |
| `config/profiles/local.yaml`, `hybrid.yaml`, `premium.yaml`, `synth.yaml` | Profile overlays | — | config | — | 40 each |
| `docker/compose.yaml` | GPU model servers (design 10 §5.6.2, verbatim) | — | deploy | — | 90 |
| `.env.example` | Dev-only variables (`HERNESS_ENV`, `HERNESS_PROFILE`, `HERNESS_SECRET__*` names with empty values) | — | config | — | 20 |
| `.streamlit/config.toml` | Streamlit server hardening (design 10 §9.2) | — | config | — | 10 |

Layer rules specific to this spec:

- Settings exception (R-03, ENG §2.1): `herness.core.config` MAY import the `settings.py` module of any package to type the root model. Every `settings.py` module, including `herness/core/settings.py`, imports only the standard library, `pydantic`, `herness.core.types` and `herness.core.errors`. Impl 00 encodes this as the named import-linter exception; this spec adds no import-linter contract. Consequence: a `settings.py` module cannot import `herness.core.secrets`, so secret-reference fields are declared with the pattern constraints of U10-27 written out locally, and C16 (U10-20) re-checks every such value at load.
- `herness.core` modules of this spec import each other only in this acyclic order: `settings` → `config_sources` → `config` → {`registry`, `redact_patterns`, `redact_directory`} → `audit` → `secrets` → `redact` → `egress_log` → `egress_socket` → `egress_clients` → `egress` → `config_validate`. A module imports only modules to its left.
- Network egress (R-06, ENG §2.1): only the egress component (`herness/core/egress.py` and its helper `herness/core/egress_clients.py`, reached as `herness.core.egress`) constructs `httpx` clients and transports: off-network (`EgressGuard.http_client`, `async_http_client`), on-network source hosts (`source_http_client`) and loopback model servers (`loopback_http_client`, `aloopback_http_client`). Vendor SDKs (Snowflake, `pymongo`, `msal`) build their own clients only for hosts listed in `sources.<name>.hosts`, and the socket guard (U10-58) enforces that list.
- `herness/store/ops/privacy.py` is L1. It uses only the core API of impl 02 (`connection()`, `run_write()`, `read_one()`, `read_all()`, `dump_json()`, `load_json()`; R-10) and adds its public names to the spec-10 `__all__` block of `herness/store/ops/__init__.py`.
- `herness/admin/*` is L5 (R-07) and is imported only by the composition root `herness.cli` (impl 09), which also registers the `maintenance` job handler when it starts the worker or runs a job inline (R-04: `herness.core.jobs` never imports `herness.admin`).

## 3. Unit specs

Conventions for all unit specs below:

- "Raises `ConfigError`" means `herness.core.errors.ConfigError` (T00-03 (herness.core.errors)). All error messages name the operation and identifiers and never contain secret values, ticket text or personal data (ENG §3.4).
- `now` values are timezone-aware UTC `datetime` objects from T00-04 (herness.core.time); no unit reads the wall clock except where "reads the clock" is stated.
- File writes marked "atomic" follow ENG §3.5: temp file in the same directory, `fsync`, `os.replace`.

### 3.1 Configuration (`herness/core/settings.py`, `config_sources.py`, `config.py`, `config_validate.py`)

#### U10-01 herness.core.config.ProfileName

| Field | Content |
|-------|---------|
| Kind | constant (type alias) |
| Purpose | Closed set of profile names. |
| Signature | `ProfileName = Literal["local", "hybrid", "premium", "synth"]`; companion constant `GATED_PROFILES: frozenset[str] = {"hybrid", "premium"}` |
| Preconditions / Postconditions | none / none |
| Algorithm | Declared once; every other unit imports it. |
| Side effects / Errors / Concurrency | none / none / immutable |
| Complexity and limits | n/a |
| Security notes | TH10-03: gated profiles are enumerated here and checked in U10-09. |
| Tests | UT10-09, UT10-11 |

#### U10-02 herness.core.settings.PathsConfig

| Field | Content |
|-------|---------|
| Kind | class (pydantic model, `extra="forbid"`, `strict=True`, `frozen=True`) |
| Purpose | Root paths. |
| Signature | Fields: `data: Path = Path("data")`; `logs: Path = Path("data/logs")`; `backup_target: Path = Path("E:/herness-backup")` |
| Preconditions | Values are strings in YAML; strict mode is relaxed for `Path` fields only (pydantic lax mode for `Path`), because YAML has no path type. This is the one `strict=True` exception in this spec (ENG §3.2). |
| Postconditions | After load, U10-09 replaces each relative path with `(config_dir.resolve().parent / value).resolve()`. |
| Invariants | All three paths are absolute after U10-09 returns. |
| Algorithm | Field validator rejects empty strings and values containing a NUL character. |
| Side effects | none |
| Errors | invalid value → pydantic `ValidationError`, converted to `ConfigError` by U10-09 |
| Concurrency | immutable |
| Complexity and limits | n/a |
| Security notes | ENG §5.7 Files: every later path join under these roots is checked for containment by its caller. |
| Tests | UT10-01, UT10-76 |

#### U10-03 herness.core.settings.SecurityConfig (with DataPolicyConfig, SecretsConfig, EgressConfig, NetworkConfig)

| Field | Content |
|-------|---------|
| Kind | class (pydantic models, `extra="forbid"`, `strict=True`, `frozen=True`) |
| Purpose | The `security` section (design 10 §4.2, §7.1). Re-exported as `herness.core.config.SecurityConfig`. The R-38 approval flag for chat `cloud` mode is exactly `security.data_policy.chat_approved` (field `DataPolicyConfig.chat_approved`); every other mention in this spec and in impls 05, 06 and 09 uses this name. |
| Signature | `DataPolicyConfig`: `hybrid_approved: bool = False`; `premium_approved: bool = False`; `chat_approved: bool = False` (R-38: approval recorded for chat `cloud` mode, purpose `reasoning`, payload class `aggregated_evidence`, in the `hybrid` profile; D5); `approved_by: str \| None = None` (1–128 chars); `approved_on: date \| None = None`. `SecretsConfig`: `backend: Literal["keyring", "dotenv"] = "keyring"`. `EgressConfig`: `enabled: bool = False`; `destinations: tuple[str, ...] = ()`; `purposes: tuple[Literal["reasoning_final", "reasoning", "bulk_classification"], ...] = ()`; `max_request_bytes: int = 2_000_000` (1–50,000,000); `max_tokens_per_request: int = 200_000` (1–2,000,000); `max_tokens_per_day: int = 3_000_000` (1–100,000,000). `NetworkConfig`: `extra_allowed_hosts: tuple[str, ...] = ()` (operator additions to the socket-guard allowlist for hosts that belong to no source; source hosts belong in `sources.<name>.hosts`, R-06); `http_proxy: str \| None = None` (matches `^https?://[A-Za-z0-9.-]+:\d{1,5}$`). `SecurityConfig`: `data_policy`, `secrets`, `redaction` (U10-04), `egress`, `network`, `ui` (U10-05), each with its model's defaults. |
| Preconditions | none |
| Postconditions | Instances are immutable. |
| Invariants | `model_download` can never appear in `egress.purposes` (the `Literal` excludes it); it is reachable only through U10-55. |
| Algorithm | 1. Validate field types and ranges. 2. Lower-case `destinations` and `extra_allowed_hosts`; C04 (U10-20) checks their form. 3. `approved_on` accepts ISO `YYYY-MM-DD` only. |
| Side effects | none |
| Errors | out-of-range or unknown key → `ValidationError` → `ConfigError` (U10-09) |
| Concurrency | immutable |
| Complexity and limits | at most 32 entries in each host tuple |
| Security notes | TH10-02, TH10-16, TH10-49: the only switches that enable egress and chat `cloud` mode live here, and U10-18 forbids overriding them from env or CLI. |
| Tests | UT10-06, UT10-19, ST10-01, ST10-56 |

#### U10-04 herness.core.settings.RedactionConfig

| Field | Content |
|-------|---------|
| Kind | class (pydantic model, `extra="forbid"`, `strict=True`, `frozen=True`) |
| Purpose | Redaction settings (design 10 §7.2). |
| Signature | `mask_ip: bool = True`; `directory_file: Path \| None = Path("D:/herness/private/directory.csv")`; `extra_names: tuple[str, ...] = ()`; `id_patterns: dict[Literal["EMPLOYEE_ID", "USER_ID"], tuple[str, ...]]` default `{"EMPLOYEE_ID": ("\bE\d{6}\b",), "USER_ID": ()}`; `national_id_patterns: tuple[str, ...] = ("\b\d{3}-\d{2}-\d{4}\b",)`; `custom_patterns: dict[str, str] = {}` (keys match `^[a-z][a-z0-9_]{0,31}$`); `ner: Literal["none", "presidio"] = "none"`; `key: str = "secret:redact.hmac_key"` (constrained with the secret-reference pattern of U10-27, written out locally because a settings module cannot import `herness.core.secrets`, R-03); `denylist_domains: tuple[str, ...] = ()` (design 10 §4.3 prose) |
| Preconditions | none |
| Postconditions | Every pattern compiles and passes the nested-quantifier rule. |
| Algorithm | Field validator for every pattern (id, national, custom): 1. Reject length > 500 characters. 2. Reject a pattern in which a parenthesised group containing an unescaped `*` or `+` is itself followed by `*`, `+` or `{n,}`; detection scans the pattern source with `\((?:[^()\\]\|\\.)*[*+](?:[^()\\]\|\\.)*\)(?:[*+]\|\{\d+,\})`. 3. Compile with `re.compile`; `re.error` is a validation error naming the key. 4. `extra_names` entries are 2–128 characters. At most 64 patterns per list and 64 custom patterns. 5. `denylist_domains` entries pass the C04 hostname rule. |
| Side effects | none |
| Errors | pattern too long, nested quantifier, compile error → `ValidationError` → `ConfigError` with the key path |
| Concurrency | immutable |
| Complexity and limits | pattern ≤ 500 chars; ≤ 64 patterns per list |
| Security notes | TH10-10 (ReDoS from config); TH10-14 (detectors configured here). |
| Tests | UT10-19, ST10-37 |

#### U10-05 herness.core.settings.UiConfig (with ExposeConfig, RolesConfig)

| Field | Content |
|-------|---------|
| Kind | class (pydantic models, `extra="forbid"`, `strict=True`, `frozen=True`) |
| Purpose | Dashboard binding, exposure and roles (design 10 §7.3); read by impl 09. |
| Signature | `ExposeConfig`: `enabled: bool = False`; `trusted_proxy: str \| None = None` (IP literal, checked with `ipaddress.ip_address`); `identity_header: str = "X-Forwarded-User"` (`^[A-Za-z][A-Za-z0-9-]{0,63}$`). `RolesConfig`: `admins: tuple[str, ...] = ()`; `reviewers: tuple[str, ...] = ()`; `default_role: Literal["viewer", "denied"] = "viewer"`; usernames 1–128 chars. `UiConfig`: `bind: str = "127.0.0.1"` (IP literal); `port: int = 8501` (1024–65535); `expose: ExposeConfig`; `roles: RolesConfig`. |
| Preconditions | none |
| Postconditions | Usernames lower-cased and de-duplicated preserving order. |
| Algorithm | Field validators as in the signature. Cross-field rules are C05 and C21 (U10-20), so `config show` can still display a misconfigured block. Identity-header trust has two checks (R-50), and this spec and impl 09 both state them: (1) at config time, C05 requires a loopback `bind`, and requires `trusted_proxy` (a loopback address) whenever `expose.enabled` is true; (2) at request time, T09-13 (app.common.auth.resolve_identity) trusts `identity_header` only when `expose.enabled` is true, `trusted_proxy` is set, and the request's peer address equals `trusted_proxy`; otherwise the header is ignored and the request gets `default_role`. |
| Side effects | none |
| Errors | invalid IP, port or header → `ConfigError` via U10-09 |
| Concurrency | immutable |
| Complexity and limits | ≤ 500 usernames per list |
| Security notes | TH10-12 (R-50). |
| Tests | UT10-19, ST10-27 |

#### U10-06 herness.core.settings.LoggingConfig, RetentionConfig, BackupConfig

| Field | Content |
|-------|---------|
| Kind | class (pydantic models, `extra="forbid"`, `strict=True`, `frozen=True`) |
| Purpose | `logging`, `retention` and `backup` sections (design 10 §4.2). |
| Signature | `LoggingConfig`: `level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"`. `RetentionConfig`: `raw_lake_months: int = 36` (1–240); `traces_days: int = 90`; `egress_log_days: int = 365`; `audit_log_days: int = 730`; `app_log_days: int = 30`; `reports_days: int = 365`; `chat_days: int = 180` (each day value 1–7,300). `BackupConfig`: `nightly_at: str = "01:30"` (`^([01]\d\|2[0-3]):[0-5]\d$`); `keep_daily: int = 14` (1–365); `keep_weekly: int = 8` (0–520); `include_lake: bool = False`; `include_cache: bool = False`. |
| Preconditions / Postconditions | none / none |
| Algorithm | Field validation only. `nightly_at` is interpreted in `weights.business_timezone` by T08-14 (herness.core.jobs.scheduler.run_scheduler). |
| Side effects / Errors / Concurrency | none / validation → `ConfigError` / immutable |
| Complexity and limits | n/a |
| Security notes | ASVS V14.2 retention (§7). |
| Tests | UT10-19, UT10-67, UT10-68 |

#### U10-07 herness.core.settings.DeployConfig (with ReasoningDeploy, OpenJevDeploy, LargeDeploy, ServiceDeploy, ReleaseDeploy)

| Field | Content |
|-------|---------|
| Kind | class (pydantic models, `extra="forbid"`, `strict=True`, `frozen=True`) |
| Purpose | Pinned deployment values (design 10 §7.4) plus the release-verification block that `herness deploy install` needs (ENG E4, R-58). |
| Signature | `DeployConfig`: `wsl_distro: str = "herness"` (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`); `model_root: str = "/opt/herness/hf"`; `env_file: str = "/opt/herness/docker.env"` (absolute POSIX paths matching `^/[A-Za-z0-9._/-]{1,200}$`, no `..` segment, not under `/mnt/`); `reasoning`, `openjev`, `large`, `service`, `release` sub-models. `ReasoningDeploy`: `image: str`; `model: str`; `revision: str`; `served_name: str = "local-30b"`; `gpu_memory_utilization: float = 0.90` (0.10–0.98); `max_model_len: int = 32768` (1,024–1,048,576); `tool_call_parser: str`; `reasoning_parser: str = "qwen3"`; `port: int = 8000` (host `127.0.0.1:8000`, R-51). `OpenJevDeploy`: `image: str`; `model: str = "nvidia/diffusiongemma-26B-A4B-it-NVFP4"`; `revision: str`; `gpu_util: float = 0.9`; `max_num_seqs: int = 64` (1–1024); `canvas: int = 64` (1–1024); `port: int = 8100` (host port `127.0.0.1:8100`; the container listens on 8080, R-51). `LargeDeploy`: `image: str`; `gguf: str`; `sha256: str`; `ctx: int = 32768`; `gpu_layers: int = 20` (0–999); `port: int = 8200` (host `127.0.0.1:8200`, also the port of the `local-large-offload` client, R-51). `ServiceDeploy`: `manager: Literal["nssm", "task_scheduler"] = "nssm"`; `account: str = "svc-herness"`. `ReleaseDeploy`: `repo: str \| None = None` (`^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$`); `signer_workflow: str \| None = None` (`^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}/\.github/workflows/[A-Za-z0-9._-]{1,100}\.ya?ml$`); `licence_exceptions: tuple[str, ...] = ()` (package names); `duckdb_extensions: dict[Literal["excel"], str] = {}` (extension name → SHA-256 of the bundled extension file, 64 lower-case hex characters; pinned per release in `herness.yaml`, used by U10-112). |
| Preconditions | none |
| Postconditions | Every string value is safe to pass as one `argv` item and as an env-file value. |
| Invariants | No value contains whitespace, NUL, `;`, `&`, `\|`, `$`, a backtick, a quote, `<` or `>`, except a whole-value template placeholder `<...>`. |
| Algorithm | Two levels. Load time (this model): each string matches `^[A-Za-z0-9._:/@+-]{1,256}$` or is a placeholder `^<[^<>\s]{1,64}>$`. Deploy time (U10-79, U10-82, U10-83) applies strict pin rules and rejects placeholders: `image` `^[a-z0-9][a-z0-9._/-]{0,200}@sha256:[0-9a-f]{64}$`; `model` `^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`; `revision` `^[0-9a-f]{40}$`; `gguf` `^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.gguf$`; `sha256` `^[0-9a-f]{64}$`; `served_name` `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`; parsers `^[a-z0-9_]{1,40}$`. Ports are 1024–65535 and pairwise distinct (load time). |
| Side effects | none |
| Errors | load-time violation → `ConfigError`; deploy-time violation → `ConfigError("deploy.<key> is not pinned")` raised by the deploy unit |
| Concurrency | immutable |
| Complexity and limits | n/a |
| Security notes | TH10-05 (argument injection into `wsl.exe`), TH10-33–TH10-36 (pinning). |
| Tests | UT10-61, ST10-06 |

#### U10-08 herness.core.config.HernessConfig

| Field | Content |
|-------|---------|
| Kind | class (`pydantic_settings.BaseSettings`) |
| Purpose | The immutable root config (design 10 §3.1). |
| Signature | `model_config = SettingsConfigDict(env_prefix="HERNESS_", env_nested_delimiter="__", extra="forbid", frozen=True)`. Fields exactly as design 10 §3.1: `profile: ProfileName = "local"`; `paths`, `security`, `logging`, `retention`, `backup`, `deploy` (U10-02–U10-07); one field per other file typed with the owner's model: `sources`, `mappings`, `decisions`, `metrics`, `weights`, `models`, `pipelines`, `memory`, `resilience`, `app`; `eval: EvalConfig \| None = None`. Two files hold sibling top-level sections owned by different specs, and because settings modules never import each other (R-03) this module composes them: `sources: SourcesFileConfig`, a subclass of `T01-02 (herness.connectors.settings.SourcesConfig)` (the connector sections under `sources`) that adds `dq: DqSettings = DqSettings()` and `build: BuildSettings = BuildSettings()` from `T02-01 (herness.model.settings.DqSettings)` and `T02-01 (herness.model.settings.BuildSettings)` (R-69; read as `cfg.sources.dq`, `cfg.sources.build`); and `models: ModelsFileConfig`, a subclass of `T05-04 (herness.harness.llm.settings.ModelsConfig)` (the sections `models`, which holds `roles`, and `harness`) that adds `deciders: DecidersSettings` from `T03-02 (herness.enrich.settings.DecidersSettings)` (R-76; read as `cfg.models.deciders`). Both composite classes are declared in `herness/core/config.py`, keep `extra="forbid"`, and add no validation of their own; each section is validated only by its owner's model. `mappings` is `T02-01 (herness.model.settings.MappingsConfig)`. Class method `settings_customise_sources` returns, in priority order: `init_settings` (CLI overrides), `GuardedEnvSource`, `FilteredDotEnvSource`, `ProfileYamlSource`, `FilesYamlSource` (U10-16–U10-18). |
| Preconditions | Constructed only by U10-09 inside its load context. Construction without an active context raises `ConfigError("HernessConfig must be built by load_config")` (checked in a `model_validator(mode="before")`). |
| Postconditions | Frozen; all nested models frozen. |
| Invariants | No field holds a resolved secret value: every secret reference is a string matching the `secret:` pattern (R-72), resolved later by `herness.core.secrets`. |
| Algorithm | Pydantic-settings deep-updates the source dicts: mappings merge key by key; lists and scalars from a higher source replace. |
| Side effects / Errors | none / `ValidationError`, converted by U10-09 |
| Concurrency | immutable; safe to share across threads |
| Complexity and limits | n/a |
| Security notes | TH10-02, TH10-07. |
| Tests | UT10-01–UT10-06 |

#### U10-09 herness.core.config.load_config

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Build and validate a `HernessConfig` from files, profile, dotenv, environment and CLI overrides (design 10 §4.1, §5.1 steps 2–4, offline subset). |
| Signature | `profile: ProfileName \| None = None`; `overrides: Sequence[str] = ()` (each `a.b.c=<yaml>`); `config_dir: Path = Path("config")`; `env: Mapping[str, str] \| None = None` (defaults to `os.environ`). All positional-or-keyword. Returns `HernessConfig`. |
| Preconditions | `config_dir` is an existing directory, else `ConfigError("config dir not found: <path>")`. |
| Postconditions | The config passed pydantic validation, the profile gate, the file-only rule and the offline cross-checks (U10-20 with `offline=True, include_registry=False`). Relative `paths.*` are absolute. Does not cache, audit or write snapshots. |
| Algorithm | 1. Resolve profile: argument, else `env["HERNESS_PROFILE"]`, else `"local"`; a value outside `ProfileName` raises `ConfigError("unknown profile: <p>")`. 2. Set a `contextvars.ContextVar` load context holding `profile`, `config_dir`, `env` and the parsed overrides; reset it in a `finally`. 3. Parse `overrides` with U10-19; a path starting with `security.` or equal to `profile` raises `ConfigError("security.* and profile are file-only: <path>")`. 4. Instantiate `HernessConfig(**override_dict, profile=profile)`; convert `ValidationError` into one `ConfigError` listing up to 20 errors as `<loc>: <msg>`, dropping pydantic's `input` so no value is echoed; the error also carries the structured list as attribute `issues` (U10-108, R-19) and sets `hint="herness config validate"`. 5. Profile gate: `hybrid` requires `security.data_policy.hybrid_approved`, `approved_by` and `approved_on`; `premium` requires `premium_approved` and both; failure raises `ConfigError("profile <p> requires recorded approval in herness.yaml security.data_policy")`. `chat_approved: true` needs `approved_by` and `approved_on` as well (same error with `chat` in place of the profile); in `hybrid` it enables chat `cloud` mode only through U10-107 (R-38). 6. For `local` and `synth`, `security.egress.enabled` must be false and `destinations` and `purposes` empty, else `ConfigError("profile <p> forbids egress")`. 7. Resolve relative `paths.*` against `config_dir.resolve().parent` (rebuild the frozen model with `model_copy(update=...)`). 8. Run U10-20 offline without registry; any `error` issue raises `ConfigError` listing the issues; each `warn` issue is logged as `config.validate.issue`. 9. Log `config.load.completed` with `profile`, `config_hash` (U10-11; `key_id="unresolved"` when the key secret is absent) and `duration_ms`. |
| Side effects | reads files under `config_dir`; reads `.env` when allowed; logs |
| Errors | missing file, YAML error, duplicate key, alias, unknown key, validation, gate, file-only violation → `ConfigError` (messages in U10-15–U10-19) |
| Concurrency | re-entrant; the context variable isolates concurrent loads in different threads |
| Complexity and limits | < 1 s for the shipped config (BT10-01); each file ≤ 5 MiB (U10-15) |
| Security notes | TH10-02, TH10-03, TH10-04, TH10-49. |
| Tests | UT10-01–UT10-14, BT10-01 |

#### U10-10 herness.core.config.init_config, get_config, reset_config

| Field | Content |
|-------|---------|
| Kind | function (three) |
| Purpose | Process-wide cached config (design 10 §3.1 `get_config`). `init_config` and `reset_config` are additive (delta D10-04). |
| Signature | `init_config(profile: ProfileName \| None = None, overrides: Sequence[str] = (), config_dir: Path = Path("config"), env: Mapping[str, str] \| None = None) -> HernessConfig`; `get_config() -> HernessConfig`; `reset_config() -> None` |
| Preconditions | none |
| Postconditions | After `init_config`, `get_config()` returns the same object until `reset_config()`. |
| Algorithm | `init_config`: under the module lock `_CACHE_LOCK`, call U10-09 and store the result; replacing an existing cache logs `config.cache.replaced` at WARNING. `get_config`: return the cache; if empty, call `init_config()` with defaults. `reset_config`: clear the cache and call every callback in the module list `_RESET_HOOKS`, to which U10-45, U10-56 and U10-58 append their reset functions at import time. |
| Side effects | module cache (the cached config permitted by ENG §2.3) |
| Errors | as U10-09 |
| Concurrency | lock-protected (`_CACHE_LOCK`, a `threading.Lock`) |
| Complexity and limits | O(1) after load |
| Security notes | Worker child processes call `init_config` with the parent's profile and overrides (T08-21 (herness.core.jobs.child.child_main)); no mutated object is inherited. |
| Tests | UT10-22 |

#### U10-11 herness.core.config.config_hash

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Stable fingerprint of the effective config (design 10 §4.4). |
| Signature | `cfg: HernessConfig` (positional); `key_id: str \| None = None` (keyword-only). Returns `str` matching `^cfg_[0-9a-f]{16}$`. |
| Preconditions | none |
| Postconditions | Same inputs give the same output on any OS and in any key order. |
| Algorithm | 1. `d = effective_dict(cfg)`. 2. Delete the subtrees `logging`, `paths`, `backup`, `security.ui`, `deploy.service`. 3. Set `d["_inputs"] = {"redact_key_id": K, "directory_sha256": H}`. `K` is the `key_id` argument; when `None`, `K` comes from the module hook `_KEY_ID_PROVIDER` (a `Callable[[HernessConfig], str]` that `herness.core.redact` registers at import; it resolves `security.redaction.key` through U10-28 and returns the U10-40 key id), which keeps `config.py` free of imports from `secrets`/`redact`; when no provider is registered or the secret is missing, `K = "unresolved"`. `H` is the SHA-256 hex of the bytes of `security.redaction.directory_file`, or `null` when the file is absent or `directory_file` is `None`. 4. Canonical JSON: `T00-05 (herness.core.ids.canonical_json)(d)`, the single implementation (R-14; it encodes `Decimal` as a string, `Path` as POSIX text, `date` as ISO-8601, tuples as lists, with sorted keys and no insignificant whitespace). 5. Return `"cfg_" + T00-05 (herness.core.ids.sha256_hex)(<UTF-8 bytes of step 4>)[:16]`. |
| Side effects | reads the directory file (streamed for hashing, content discarded) and the keyring |
| Errors | directory file exists but cannot be read → `ConfigError("cannot read directory_file")` |
| Concurrency | thread-safe |
| Complexity and limits | < 50 ms excluding the directory hash (BT10-02); directory hashed in 1 MiB chunks |
| Security notes | The hash input holds `secret:<name>` references only, never values (UT10-17). |
| Tests | UT10-15, UT10-16, UT10-17, PT10-01, BT10-02 |

#### U10-12 herness.core.config.effective_dict

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Plain-dict view for `config show`, snapshots and hashing. |
| Signature | `cfg: HernessConfig` (positional); `redact_secrets: bool = True` (keyword-only). Returns `dict[str, Any]` (JSON-shaped). |
| Preconditions | none |
| Postconditions | Secret references appear as `secret:<name>` (R-72). |
| Algorithm | 1. `cfg.model_dump(mode="json")`. 2. When `redact_secrets` is true, walk the dict; replace with `"***"` any string value under a key named `password`, `passwd`, `pwd`, `api_key`, `apikey`, `token`, `secret`, `credentials`, `client_secret` or `private_key` that does not start with `secret:` (defense in depth; C16 already rejects such values). 3. Return the dict. |
| Side effects / Errors / Concurrency | none / none / pure |
| Complexity and limits | O(size of config) |
| Security notes | TH10-07. |
| Tests | UT10-18, ST10-16 |

#### U10-13 herness.core.config.validate

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Full validation for `herness config validate` and `herness doctor` (design 10 §5.1). |
| Signature | `cfg_dir: Path` (positional); `profile: ProfileName` (positional); `offline: bool = False` (keyword-only). Returns `list[ConfigIssue]` sorted by severity (`error` first), then `path`. |
| Preconditions | none |
| Postconditions | A config problem never raises; each is an issue. |
| Algorithm | 1. Call U10-09 with `profile` and `config_dir=cfg_dir`; on `ConfigError`, return its `issues` attribute (one `error` issue per problem). 2. Run U10-20 with `offline=offline, include_registry=True`. 3. `run_owner_validators(cfg, offline=offline)` (U10-109) and append its issues; the validators are those the composition root registered before calling this function. 4. Sort and return. |
| Side effects | imports registered implementation modules (C03); keyring reads unless offline; one compose subprocess unless offline (C08b) |
| Errors | none raised for config problems; an exception inside an owner validator becomes `error` issue `validator <name> failed: <ExceptionClass>` |
| Concurrency | call from one thread (registry import side effects) |
| Complexity and limits | offline < 1 s (BT10-01); online adds at most the 20 s compose timeout |
| Security notes | TH10-06 (C16), TH10-12 (C05). |
| Tests | UT10-19, UT10-20, IT10-01 |

#### U10-14 herness.core.config.ConfigIssue

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass) |
| Purpose | One validation finding. |
| Signature | `severity: Literal["error", "warn"]`; `path: str` (dotted key path, or `<file>` for a file-level issue); `message: str` (≤ 300 chars); `file: str \| None` (path relative to `cfg_dir`) |
| Algorithm | `__str__` renders `<severity> <path> <file>: <message>` with `file` shown as `-` when `None` (design 10 §5.1). |
| Side effects / Errors / Concurrency | none / none / immutable |
| Complexity and limits | n/a |
| Security notes | Messages contain key paths, hostnames and names only, never values of keys in the U10-12 secret key list. |
| Tests | UT10-20 |

#### U10-15 herness.core.config_sources.load_yaml_file

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Read one config YAML file safely. |
| Signature | `path: Path` (positional); `max_bytes: int = 5_242_880` (keyword-only). Returns `dict[str, Any]`. |
| Preconditions | none |
| Postconditions | The mapping has no duplicate keys and no alias-derived values. |
| Algorithm | 1. Missing file → `ConfigError("config file missing: <name>")`. 2. `st_size > max_bytes` → `ConfigError("config file too large: <name>")`. 3. Read bytes; decode UTF-8, stripping a BOM. 4. Parse with a subclass of `yaml.SafeLoader` that raises `ConfigError("YAML anchors and aliases are not allowed: <name>:<line>")` on any anchor or alias event and raises `ConfigError("duplicate key '<key>' at <name>:<line>")` from `construct_mapping` on a repeated key. 5. An empty document gives `{}`; a non-mapping top level gives `ConfigError("<name>: top level must be a mapping")`. 6. `yaml.YAMLError` becomes `ConfigError("<name>:<line>: invalid YAML")`. |
| Side effects | reads one file |
| Errors | as listed |
| Concurrency | thread-safe |
| Complexity and limits | ≤ 5 MiB per file |
| Security notes | TH10-04 (alias bombs, duplicate-key shadowing). ENG §3.5. |
| Tests | UT10-07, UT10-08, ST10-05 |

#### U10-16 herness.core.config_sources.FilesYamlSource

| Field | Content |
|-------|---------|
| Kind | class (`pydantic_settings.PydanticBaseSettingsSource`) |
| Purpose | Lowest file layer: `config/*.yaml` (design 10 §4.1). |
| Signature | `__init__(settings_cls: type[BaseSettings])`; `__call__() -> dict[str, Any]` |
| Preconditions | Inside the U10-09 load context. |
| Postconditions | Returns the root sections of `herness.yaml` plus one section per other file stem. |
| Algorithm | 1. Load `herness.yaml` (U10-15); `version` must equal 1 (`ConfigError("<file>: version must be 1")`); the other top-level keys must be a subset of `paths`, `security`, `logging`, `retention`, `backup`, `deploy` (`ConfigError("herness.yaml: unknown root key <k>")`). 2. For each stem in the fixed list `sources`, `mappings`, `decisions`, `metrics`, `weights`, `models`, `pipelines`, `memory`, `resilience`, `app`: load `<stem>.yaml`, check `version`, and store the file's top-level mapping as section `<stem>`; `version` is dropped except for `sources.yaml`, whose model `SourcesConfig` (impl 01) declares it. The sibling sections of `sources.yaml` (`sources`, `dq`, `build`; R-69) and of `models.yaml` (`models`, `harness`, `deciders`; R-76) stay together in that section and are split across the owners' models by the U10-08 composite classes. 3. Load `eval.yaml` the same way only if it exists. 4. Read `injection_patterns.txt` (UTF-8, ≤ 1 MiB; missing → `ConfigError`); keep stripped lines that are non-empty and do not start with `#`; store the list at `memory["injection_patterns"]` (T07-02 (herness.harness.memory.settings.MemoryConfig) declares the field). 5. Any other `*.yaml` file directly under `config/` → `ConfigError("unexpected config file <name>")`. |
| Side effects | reads files |
| Errors | as above plus U10-15 |
| Concurrency | per-call state only |
| Complexity and limits | 12 files |
| Security notes | TH10-04. |
| Tests | UT10-05, UT10-06 |

#### U10-17 herness.core.config_sources.ProfileYamlSource

| Field | Content |
|-------|---------|
| Kind | class (`PydanticBaseSettingsSource`) |
| Purpose | Profile overlay layer plus the synthetic fragment (design 10 §4.1, §4.3). |
| Signature | `__init__(settings_cls)`; `__call__() -> dict[str, Any]` |
| Preconditions | Inside the load context. |
| Postconditions | Returns the overlay addressed by section. |
| Algorithm | 1. Load `config/profiles/<profile>.yaml` (missing → `ConfigError`); require and drop `version: 1`. 2. Top-level keys must be `HernessConfig` section names, else `ConfigError`. 3. Any key under `security.data_policy` → `ConfigError("profiles may not set security.data_policy")`. 4. For profile `synth`, an overlay setting `security.egress.enabled: true` or non-empty `destinations` or `purposes` → `ConfigError("profile synth cannot enable egress")`. 5. If the context env has `HERNESS_SYNTH_CONFIG`: require profile `synth` (`ConfigError("HERNESS_SYNTH_CONFIG requires profile synth")`); load that file (U10-15); its only allowed top-level key is `mappings`; deep-merge it after the overlay. 6. Return the result. |
| Side effects | reads files |
| Errors | as above |
| Concurrency | per-call |
| Complexity and limits | n/a |
| Security notes | TH10-03 (self-approval through a profile file). |
| Tests | UT10-09, UT10-12, UT10-13, UT10-14, ST10-03 |

#### U10-18 herness.core.config_sources.GuardedEnvSource, FilteredDotEnvSource

| Field | Content |
|-------|---------|
| Kind | class (two `PydanticBaseSettingsSource` subclasses) |
| Purpose | Environment and dotenv layers with the file-only rule (design 10 §4.1). |
| Signature | `__init__(settings_cls)`; `__call__() -> dict[str, Any]` |
| Preconditions | Inside the load context. |
| Postconditions | The dict has no `security` and no `profile` key. |
| Algorithm | `GuardedEnvSource`: 1. Take context-env variables whose name starts with `HERNESS_`. 2. Skip `HERNESS_PROFILE`, `HERNESS_ENV`, `HERNESS_SYNTH_CONFIG`, `HERNESS_FAULTS`, `HERNESS_WORKER` and names starting with `HERNESS_SECRET__`. 3. A name starting with `HERNESS_SECURITY__` raises `ConfigError("security.* is file-only; remove <NAME>")` (name only). 4. Split the remainder on `__` into lower-cased segments; parse the value as in U10-19; build the nested dict. `FilteredDotEnvSource`: 1. Active only when the context env has `HERNESS_ENV=dev`; otherwise returns `{}`. 2. Read `<config_dir parent>/.env` (`KEY=VALUE` lines, `#` comments, optional double quotes, ≤ 64 KiB). 3. Apply the same skip and rejection rules. |
| Side effects | reads `.env` in dev |
| Errors | `ConfigError` for `security` overrides and malformed lines (line number only) |
| Concurrency | per-call |
| Complexity and limits | ≤ 500 variables |
| Security notes | TH10-02: a stray variable can never enable egress. |
| Tests | UT10-02, UT10-10, ST10-01 |

#### U10-19 herness.core.config_sources.parse_overrides

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Parse repeatable `--set a.b.c=<yaml value>` flags. |
| Signature | `overrides: Sequence[str]` (positional). Returns `dict[str, Any]` (nested). |
| Preconditions | none |
| Postconditions | A later override of the same path replaces an earlier one. |
| Algorithm | 1. Split each item at the first `=`; no `=` → `ConfigError("--set needs key=value: <key part>")`. 2. Split the key on `.`; each segment matches `^[A-Za-z0-9_-]{1,64}$`; at most 12 segments. 3. Parse the value text (≤ 4,096 chars) with the U10-15 loader rules (safe, no aliases). 4. Build nested dicts; descending into a scalar set by an earlier override → `ConfigError`. |
| Side effects / Errors | none / `ConfigError` naming the key path only |
| Concurrency | pure |
| Complexity and limits | ≤ 100 overrides |
| Security notes | The `security.`/`profile` ban is applied by U10-09 step 3. impl 09 translates its `--depth D` flag into the override `models.models.depth.default=<D>`. |
| Tests | UT10-03, UT10-23, PT10-06 |

#### U10-20 herness.core.config_validate.CROSS_CHECKS, run_cross_checks

| Field | Content |
|-------|---------|
| Kind | constant (table) and function |
| Purpose | Cross-section rules of design 10 §5.1 step 4 plus the security rules this spec adds. |
| Signature | `run_cross_checks(cfg: HernessConfig, *, offline: bool, include_registry: bool, compose_path: Path \| None = None) -> list[ConfigIssue]`; `compose_path` defaults to `<config_dir parent>/docker/compose.yaml`. |
| Preconditions | `cfg` passed U10-09 steps 1–7. |
| Postconditions | One issue per failed rule instance. |
| Algorithm | Evaluate every row of the table below in order and collect issues. Rows marked "online" run only when `offline` is false; C03 runs only when `include_registry` is true. Values are read through `effective_dict(cfg)` key paths, so this module does not depend on owner model attributes. `docker/compose.yaml` is parsed with `yaml.safe_load` (aliases allowed here: the file is repository-controlled and uses the `x-gpu` anchor). |
| Side effects | C03 imports modules; C06 reads the keyring; C08b runs one subprocess; C23 stats one file |
| Errors | none raised |
| Concurrency | one thread |
| Complexity and limits | offline part < 200 ms |
| Security notes | TH10-06, TH10-11, TH10-12, TH10-16, TH10-48, TH10-49. |
| Tests | UT10-19 (one parametrised case per row), UT10-75, ST10-55, ST10-56 |

Cross-check table (`CROSS_CHECKS`):

| ID | Rule | Severity | Mode |
|----|------|----------|------|
| C01 | Every value of `models.models.roles` and every entry of every list in `models.models.fallback` (after the overlay) is a key of `models.models.clients` | error | offline |
| C02 | If a value of `models.models.roles` names a client with `off_network: true`, `security.egress.enabled` is true and the profile's gate is set. Fallback entries are not checked (T08-09 (herness.core.resilience.ModelChain) drops off-network entries at run time when egress is off) | error | offline |
| C03 | `registry.get(kind, name)` resolves for every referenced name: `connector` ← keys of `sources.sources` with `enabled: true`; `monitoring_adapter` ← keys of `sources.sources.monitoring.adapters` with `enabled: true`; `llm_client` ← distinct `kind` values of `models.models.clients.*`; `decider` ← keys of `models.deciders` (the top-level `deciders` section of `models.yaml`, U03-150, R-76) with `enabled: true` | error | registry |
| C04 | Each `security.egress.destinations`, `security.network.extra_allowed_hosts` and `sources.sources.<name>.hosts` entry (R-06) matches `^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$` and is not an IP literal | error | offline |
| C05 | `security.ui.bind` is a loopback address (the app always binds to loopback, R-50); `security.ui.expose.enabled` requires `trusted_proxy`; `trusted_proxy`, when set, is a loopback address (the proxy runs on the same host, so it is the only peer that can equal it) | error | offline |
| C06 | Every secret reference returned by U10-34 exists (U10-30) | error | online |
| C07 | `deploy.reasoning.served_name` equals `models.models.clients.local-30b.model` | error | offline |
| C08a | Every service under `resilience.resilience.gpu.classes.<class>.services` exists in `docker/compose.yaml` with `profiles == [<class>]`, and its URL port equals the deploy port (`vllm-reasoning` → `deploy.reasoning.port`, `openjev` → `deploy.openjev.port`, `llamacpp-large` → `deploy.large.port`) | error | offline |
| C08b | `resilience.resilience.gpu.compose_cmd` + `-f` + `<compose_file>` + `config` + `--quiet` exits 0 within 20 s. On a non-Windows host or without `wsl.exe` on `PATH` the result is a `warn` "not checked on this host" | error | online |
| C09 | Every `models.models.clients.*` entry with `kind: openai_compat`, `off_network: false` and `gpu_class` `reasoning` or `large` has a `base_url` port equal to `deploy.reasoning.port` or `deploy.large.port` respectively (R-51: 8000 and 8200; `local-large-offload` uses 8200) | error | offline |
| C10 | Every `models.models.clients.*` entry with `off_network: false` has `context_window` ≤ the `max_model_len` its server is started with (R-52): `deploy.reasoning.max_model_len` for `gpu_class: reasoning`, `deploy.large.ctx` for `gpu_class: large`. With the defaults, `local-30b` has `context_window = 32768` and `max_model_len = 32768`. The real card is a Phase 4 verification item (T10-31) | error | offline |
| C11 | `hybrid`: `security.egress.purposes` ⊆ {`reasoning_final`}, plus `reasoning` only when `security.data_policy.chat_approved` is true (R-38); `premium`: ⊆ {`reasoning`, `reasoning_final`, `bulk_classification`} | error | offline |
| C12 | `security.secrets.backend: dotenv` only when the env has `HERNESS_ENV=dev` or the profile is `synth` | error | offline |
| C13 | Deploy pins (U10-07 deploy-time rules) hold for `reasoning`, `openjev`, `large` | warn | offline |
| C14 | `security.redaction.custom_patterns` names are unique case-insensitively | error | offline |
| C16 | Plain-text credential sweep over the merged raw dict: every string under a key named in the U10-12 secret key list or ending in `_secret` is a `secret:` reference whose name matches `SECRET_NAME` (R-72; a bare name is an error with the hint "write secret:<name>"); no string anywhere yields a `CREDENTIAL` span from U10-36's detectors | error | offline |
| C17 | Every enabled `sources.sources.*.base_url` (any depth) uses `https`, unless its host is loopback | error | offline |
| C20 | Every enabled SDK source (`SDK_SOURCE_KINDS`, U10-58: `snowflake`, `mongodb`) and every enabled `dataverse` source with `auth.method: msal_client_credentials` has a non-empty `sources.sources.<name>.hosts` list; for `snowflake` it contains `<account>.snowflakecomputing.com`; for `dataverse` it contains `login.microsoftonline.com`. Nothing is derived from `base_url` for these sources (R-06) | error | offline |
| C21 | `security.ui.expose.enabled` together with `roles.default_role: viewer` (D8 recommends `denied`) | warn | offline |
| C23 | `security.redaction.directory_file` is set, exists and is readable | warn | online |
| C24 | `hybrid` and `premium` have at least one destination and one purpose | error | offline |
| C25 | `security.data_policy.chat_approved: true` only together with `hybrid_approved: true` or `premium_approved: true` (R-38) | error | offline |

IDs C15, C18, C19 and C22 are not used: those rules are structural and live in U10-09, U10-17, U10-18 and U10-04.

#### U10-21 herness.core.config_sources.BootstrapConfig, load_bootstrap

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass) and function |
| Purpose | Minimal config for the socket guard before the full load (design 10 §5.1 step 1). |
| Signature | `BootstrapConfig`: `profile: ProfileName`; `security: SecurityConfig`; `source_hosts: tuple[str, ...]`. `load_bootstrap(profile: ProfileName \| None = None, config_dir: Path = Path("config"), env: Mapping[str, str] \| None = None) -> BootstrapConfig` |
| Preconditions | none |
| Postconditions | `security` equals `herness.yaml` merged with the profile overlay's `security` subtree; `source_hosts` are lower-cased hostnames. |
| Algorithm | 1. Resolve the profile as U10-09 step 1. 2. Load `herness.yaml` and the profile file (U10-15); deep-merge their `security` subtrees; validate with `SecurityConfig`. 3. Load `sources.yaml` and compute the source hosts with the rule of U10-58 step 1 (R-06): for each source under `sources` whose mapping does not have `enabled: false`, add every entry of its `hosts` list; when the source name is not in `SDK_SOURCE_KINDS`, also add `urllib.parse.urlsplit(v).hostname` of every `base_url` value at any depth inside it whose enclosing mapping does not have `enabled: false`. Nothing is derived from `base_url` for SDK sources. 4. Apply the U10-09 step 6 rule. |
| Side effects | reads three files |
| Errors | `ConfigError` as U10-15 and U10-09 |
| Concurrency | thread-safe |
| Complexity and limits | < 50 ms |
| Security notes | TH10-15, TH10-48: the guard is active before other imports can open a socket. |
| Tests | UT10-24, ST10-55 |

#### U10-22 herness.core.config_validate.SECTION_VALIDATORS

Removed (R-04): see U10-109. A table of lazy import strings run from L0 would execute owner code that needs the warehouse or other higher layers (for example impl 04's catalog validator, DD04-20) inside the L0 loader. Owner validators are now registered by the composition root and run after `load_config` (U10-109).

#### U10-109 herness.core.config_validate.OwnerValidator, register_owner_validator, run_owner_validators, reset_owner_validators

| Field | Content |
|-------|---------|
| Kind | protocol and functions (start-up validation hook; port pattern of R-04) |
| Purpose | Run the owner validators that design 10 §5.1 step 4 delegates (decisions T03-02 (herness.enrich.settings.check_decider_refs), metrics T04-03 (herness.metrics.catalog.metrics_owner_validator), resilience and source schedules T08-02 (herness.core.jobs.validate.validate_resilience_config)) after `load_config`, from the composition root, so validators that need the warehouse or other layers above L0 never run inside the L0 loader. |
| Signature | `OwnerValidator` (`typing.Protocol`): `__call__(self, cfg: HernessConfig, *, offline: bool) -> Iterable[ConfigIssue \| Mapping[str, str]]`. `register_owner_validator(name: str, fn: OwnerValidator) -> None` (`name` matches `^[a-z][a-z0-9_.-]{0,63}$`). `run_owner_validators(cfg: HernessConfig, *, offline: bool) -> list[ConfigIssue]`. `reset_owner_validators() -> None` (tests only, called by the `reset_core` fixture; `reset_config` keeps registrations so a re-initialised config is still validated). |
| Preconditions | `register_owner_validator` is called only by a composition root (`herness.cli`, `app/common`), which imports the owner modules (L1–L5 may import them) and registers each validator before it calls `init_config`. A second registration of the same `name` with a different object raises `ConfigError("duplicate owner validator <name>")`; the same object is a no-op. |
| Postconditions | `run_owner_validators` never raises for a validator problem; every result is a `ConfigIssue`. |
| Invariants | The registry holds callables only; `herness.core` imports no owner module for it (import-linter clean without an exception). |
| Algorithm | `run_owner_validators`: for each registered validator in name order: 1. Call `fn(cfg, offline=offline)` and materialise at most 500 results. 2. Convert each result: a `ConfigIssue` is kept; a mapping with keys `severity` (`error` or `warn`), `path` and `message`, and optional `file`, becomes `ConfigIssue(severity, path, message[:300], file)`; anything else becomes `error` issue `validator <name> returned an invalid issue` at path `<name>`. 3. A `HernessError` or any other exception raised by the validator becomes one `error` issue `validator <name> failed: <ExceptionClass>` at path `<name>` (never the message, which may hold values). 4. A validator whose dependency is absent (for example no warehouse `CURRENT` yet on a new install) returns `warn` issues itself; this is the documented contract of the protocol. 5. Return the issues sorted as U10-13. The start-up call (F10-01 step 3a) logs each issue as `config.validate.issue` and, when any has severity `error`, raises `ConfigError("owner validation failed", issues=...)` so the process exits 3 (R-46) before a job starts; `herness config validate` and `herness doctor` call it through U10-13 step 3 and report instead of raising. |
| Side effects | whatever the owner validators do (reads only, by contract); logs at start-up |
| Errors | `ConfigError` for a duplicate registration and at start-up when an `error` issue exists |
| Concurrency | registration under `_VAL_LOCK` (`threading.Lock`); runs in the calling thread; module registry is ENG §2.3 state of the same kind as the implementation registry, reset by `reset_owner_validators` |
| Complexity and limits | ≤ 500 issues per validator; the start-up run is bounded by the owners' own limits |
| Security notes | TH10-06: owner validators return key paths and messages without values; step 3 drops exception messages. |
| Tests | UT10-81, UT10-19 (owner rows via fake validators) |

### 3.2 Registry (`herness/core/registry.py`)

#### U10-23 herness.core.registry.Kind, register

| Field | Content |
|-------|---------|
| Kind | constant and function (decorator factory) |
| Purpose | Register an implementation under `(kind, name)` (design 10 §3.2). |
| Signature | `Kind = Literal["connector", "monitoring_adapter", "decider", "llm_client", "tool", "embedder", "renderer"]`. `register(kind: Kind, name: str) -> Callable[[T], T]` |
| Preconditions | `name` matches `^[a-z0-9][a-z0-9_.-]{0,63}$`, else `ConfigError("invalid registry name: <name>")`; `kind` in `Kind`, else `ConfigError`. |
| Postconditions | The decorated object is returned unchanged and stored. |
| Algorithm | 1. Validate. 2. Under `_REG_LOCK`, if `(kind, name)` already maps to a different object (identity), raise `ConfigError("duplicate registration <kind>:<name>")`; the same object is a no-op. 3. Store. |
| Side effects | module registry (permitted by ENG §2.3) |
| Errors | as above |
| Concurrency | lock-protected (`_REG_LOCK`, `threading.RLock`) |
| Complexity and limits | O(1) |
| Security notes | none |
| Tests | UT10-25, UT10-26 |

#### U10-24 herness.core.registry.get

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Resolve a registered class or factory without instantiating it. |
| Signature | `get(kind: Kind, name: str) -> Any` |
| Preconditions | as U10-23 |
| Postconditions | Returns the registered object. |
| Algorithm | 1. If registered, return it. 2. If `(kind, name)` is in `_BUILTINS`, import `module` with `importlib.import_module`, take `attr`, store it (the import may already have registered the same object through the decorator; identity makes that a no-op) and return it. 3. Load entry points once per process (U10-25 step 1), then retry step 1. 4. Raise `ConfigError("unknown <kind> '<name>'; available: <comma list>")` with `available(kind)`. |
| Side effects | imports modules; may load plugins |
| Errors | unknown name → `ConfigError`; import failure of a built-in → `ConfigError("cannot import <module>")` from the `ImportError` |
| Concurrency | `_REG_LOCK` held during steps 2–3 |
| Complexity and limits | O(1) after first import |
| Security notes | TH10-38. |
| Tests | UT10-25, UT10-26 |

#### U10-25 herness.core.registry.available

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | List resolvable names for a kind. |
| Signature | `available(kind: Kind) -> list[str]` |
| Algorithm | 1. On first call per process, iterate `importlib.metadata.entry_points(group="herness.plugins")`; for each, `ep.load()` (a module import whose decorators register implementations) and log `registry.plugin.loaded` at WARNING with `entry_point`, `distribution` (`ep.dist.name`) and `version`; an exception during load is logged as `registry.plugin.failed` at ERROR and skipped. 2. Return the sorted union of registered names for `kind` and `_BUILTINS` names for `kind`. |
| Side effects | plugin imports (once) |
| Errors | none raised |
| Concurrency | `_REG_LOCK`; a module flag records that entry points were loaded |
| Complexity and limits | O(n log n) |
| Security notes | TH10-38: every plugin load is visible at WARNING and listed by doctor (U10-90 `plugins` row). |
| Tests | UT10-27, ST10-46 |

#### U10-26 herness.core.registry._BUILTINS, reset_registry

| Field | Content |
|-------|---------|
| Kind | constant and function |
| Purpose | Static table of built-in implementations; test reset. |
| Signature | `_BUILTINS: dict[tuple[Kind, str], str]` of `"module:attr"`; `reset_registry() -> None` |
| Algorithm | The table starts empty in T10-04. Each owner spec's task card adds its own rows (impl 01 connectors and monitoring adapters, impl 03 deciders and embedders, impl 05 LLM clients and tools, impl 09 renderers). `reset_registry` clears registrations and the entry-point flag, keeping `_BUILTINS`. |
| Side effects / Errors / Concurrency | module state / none / `_REG_LOCK` |
| Complexity and limits | n/a |
| Security notes | Built-ins are import strings, so the table creates no upward import edge for import-linter. |
| Tests | UT10-25 |

### 3.3 Secrets (`herness/core/secrets.py`)

#### U10-27 herness.core.secrets.SECRET_NAME, SecretRef, SecretRefStr, SecretNameStr

| Field | Content |
|-------|---------|
| Kind | constant and types |
| Purpose | Secret reference forms (design 10 §3.3). |
| Signature | `SECRET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")`. `class SecretRef(str)` with class method `parse(value: str) -> SecretRef` accepting `secret:<name>` or a bare name and property `name -> str` (lower-cased). `SecretRefStr = Annotated[str, AfterValidator(...)]` requiring `^secret:` + a `SECRET_NAME` match. `SecretNameStr = Annotated[str, AfterValidator(...)]` requiring a bare `SECRET_NAME` match. Settings modules cannot import this module (R-03), so owner settings models (and U10-04) declare every secret-reference field, including fields named `*_secret`, as `Annotated[str, StringConstraints(pattern=r"^secret:[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")]`, the same pattern text as `SecretRefStr` (R-72: settings models hold secret references as `secret:` strings and never import this module); C16 (U10-20) re-checks every such value with `SECRET_NAME` at load, so a drifted local pattern cannot admit a plain-text credential. `SecretRefStr` and `SecretNameStr` remain for non-settings code (`SecretNameStr` types the bare name argument of `secrets set`). |
| Preconditions | none |
| Postconditions | `SecretRef.name` is lower-case. |
| Algorithm | `parse`: strip an optional `secret:` prefix, match `SECRET_NAME`, else `ConfigError("invalid secret name")` (the value is not echoed); store the lower-cased name. |
| Side effects | none |
| Errors | `ConfigError`; inside pydantic, a `ValueError` becomes a validation error |
| Concurrency | immutable |
| Complexity and limits | name ≤ 64 chars |
| Security notes | TH10-06: a plain-text credential cannot satisfy these types. |
| Tests | UT10-30 |

#### U10-28 herness.core.secrets.resolve

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Resolve a secret value at the point of use (design 10 §5.2). |
| Signature | `resolve(ref: SecretRef \| str) -> SecretStr` |
| Preconditions | The backend choice comes from `get_config().security.secrets.backend`; before config is initialised the backend is `keyring`. |
| Postconditions | The value is added to the known-values set (U10-32). The value is not cached for reuse. |
| Algorithm | 1. `SecretRef.parse(ref)`. 2. Select the backend (U10-33). 3. `backend.get(name)`; `None` → `ConfigError("secret not found: <name>")`. 4. Add the value to `_KNOWN` (U10-32). 5. Return `SecretStr(value)`. |
| Side effects | backend read |
| Errors | missing → `ConfigError("secret not found: <name>")`; backend failure → `ConfigError("secret backend unavailable: <backend>; run as the account that owns the credential")` and log `secrets.backend.unavailable` |
| Concurrency | thread-safe (`_KNOWN` guarded by `_KNOWN_LOCK`) |
| Complexity and limits | one backend call (1–5 ms for keyring) |
| Security notes | TH10-07: callers hold `SecretStr` and call `get_secret_value()` only when building a header. |
| Tests | UT10-28, FT10-05 |

#### U10-29 herness.core.secrets.resolve_json

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Resolve a compound credential stored as a JSON object. |
| Signature | `resolve_json(ref: SecretRef \| str) -> dict[str, SecretStr]` |
| Preconditions | as U10-28 |
| Postconditions | Every member value is in the known-values set. |
| Algorithm | 1. `raw = resolve(ref).get_secret_value()`. 2. `json.loads(raw)`; not an object, or a member that is not a string → `ConfigError("secret <name> is not a JSON object of strings")`. 3. Add each member value to `_KNOWN`. 4. Return members wrapped in `SecretStr`. |
| Side effects | as U10-28 |
| Errors | as above; `json.JSONDecodeError` → `ConfigError` (no content echoed) |
| Concurrency | thread-safe |
| Complexity and limits | value ≤ 16 KiB |
| Security notes | TH10-07. |
| Tests | UT10-29 |

#### U10-30 herness.core.secrets.exists

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Presence check without adding the value to memory. |
| Signature | `exists(name: str) -> bool` |
| Algorithm | Parse the name; return `backend.get(name) is not None`; the value is discarded immediately and not added to `_KNOWN`. |
| Side effects | backend read |
| Errors | backend failure → `ConfigError` as U10-28 |
| Concurrency | thread-safe |
| Complexity and limits | one backend call |
| Security notes | none |
| Tests | UT10-28 |

#### U10-31 herness.core.secrets.set_secret, delete_secret

| Field | Content |
|-------|---------|
| Kind | function (two) |
| Purpose | Store or remove a secret (CLI and rekey job only; design 10 §3.3). `delete_secret` and the `actor` parameter are additive (delta D10-04). |
| Signature | `set_secret(name: str, value: str, *, actor: str) -> None`; `delete_secret(name: str, *, actor: str) -> None` |
| Preconditions | Backend `keyring` (dotenv is read-only: `ConfigError("dotenv backend is read-only")`). `value` is 8–16,384 characters with no newline or NUL, else `ConfigError("secret value rejected: length or characters")`. |
| Postconditions | Backend updated; one audit line written first. |
| Algorithm | `set_secret`: 1. Validate. 2. `audit("admin_action", actor, action="secret_set", target=<name>)` (U10-60); if audit raises, stop (the action does not happen). 3. `backend.set(name, value)`. `delete_secret`: audit `action="secret_rotate"` with `target=<name>`, then `backend.delete(name)`; deleting a missing name is a no-op. |
| Side effects | audit line, backend write |
| Errors | validation → `ConfigError`; audit failure → `FatalError` (U10-60) |
| Concurrency | thread-safe; callers are single processes |
| Complexity and limits | n/a |
| Security notes | TH10-08 (audited by name only), TH10-07 (value never logged). |
| Tests | UT10-33 |

#### U10-32 herness.core.secrets.known_values, scrub_secrets

| Field | Content |
|-------|---------|
| Kind | function (two) |
| Purpose | The log scrubber (design 10 §5.2). |
| Signature | `known_values() -> frozenset[str]`; `scrub_secrets(logger: Any, method_name: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]` (structlog processor signature) |
| Preconditions | none |
| Postconditions | No string in the returned event contains a known value or a `CREDENTIAL`/`URL_TOKEN` span. |
| Algorithm | `known_values`: snapshot of `_KNOWN`. `scrub_secrets`: 1. When `_KNOWN` changed since the last call, rebuild one compiled alternation of `re.escape(v)` for all known values, longest first. 2. Walk `event_dict` values recursively (dicts, lists, tuples; depth ≤ 6); for each string, replace alternation matches with `***`. 3. Run U10-36's `CREDENTIAL` and `URL_TOKEN` detectors on each string and replace their spans with `[SECRET]`. 4. Tracebacks are rendered to text by the T00-06 (herness.core._log_pipeline) processors before this processor, so they are scrubbed too. T00-07 (herness.core.logging.configure_logging) inserts this processor last before the renderer; T05-11 (herness.harness.tracing.Tracer) applies the same function to every trace event. |
| Side effects | none |
| Errors | none raised; a failure inside the processor replaces the whole event with `{"event": "log.scrub.failed"}` so nothing unscrubbed is emitted |
| Concurrency | `_KNOWN_LOCK` for the rebuild; the compiled pattern is swapped atomically |
| Complexity and limits | O(total string length); strings longer than 64 KiB are truncated to 64 KiB before scanning |
| Security notes | TH10-07 (last line of defence, ENG §3.6). |
| Tests | UT10-34, UT10-35, ST10-15 |

#### U10-33 herness.core.secrets._SecretBackend, _KeyringBackend, _DotenvBackend

| Field | Content |
|-------|---------|
| Kind | protocol and classes (private, logic worth testing) |
| Purpose | The two backends of design 10 §3.3. |
| Signature | Protocol methods: `get(name: str) -> str \| None`; `set(name: str, value: str) -> None`; `delete(name: str) -> None`. |
| Preconditions | `_DotenvBackend` is constructed only when `HERNESS_ENV=dev` or profile `synth`, else `ConfigError("dotenv secrets backend is refused outside dev and synth")`. |
| Algorithm | `_KeyringBackend`: service `herness`, username = lower-cased name. Values ≤ 1,200 characters are stored directly. Longer values (Windows Credential Manager limits a credential blob to 2,560 bytes stored as UTF-16) are split into 1,200-character chunks stored as `<name>#1` … `<name>#n`, and the entry `<name>` holds `chunked:v1:<n>`; `get` reassembles; `delete` removes all chunks. `keyring.errors.KeyringError` or `RuntimeError` → `ConfigError("secret backend unavailable: keyring")`. `_DotenvBackend`: reads `<repo root>/.env` once (U10-18 grammar); variable name `HERNESS_SECRET__` + name with `.` and `-` replaced by `_`, upper-cased; `set` and `delete` raise `ConfigError("dotenv backend is read-only")`. |
| Side effects | Windows Credential Manager or `.env` read |
| Errors | as above |
| Concurrency | thread-safe (stateless apart from the dotenv cache) |
| Complexity and limits | value ≤ 16 KiB (14 chunks) |
| Security notes | TH10-11. Credentials are per Windows account, so `svc-herness` must hold its own secrets (runbook step 4). |
| Tests | UT10-31, UT10-32, ST10-26, FT10-05 |

#### U10-34 herness.core.secrets.referenced_secret_names

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | All secret names referenced by a config, for C06, doctor and `secrets status`. |
| Signature | `referenced_secret_names(cfg: HernessConfig) -> list[str]` |
| Algorithm | Walk `effective_dict(cfg)`: collect names from strings starting with `secret:` (R-72); add `redact.hmac_key` and `ui_user_ref_key`; lower-case, de-duplicate, sort. Subtrees whose mapping has `enabled: false` are skipped. |
| Side effects / Errors / Concurrency | none / none / pure |
| Complexity and limits | O(size of config) |
| Security notes | none |
| Tests | UT10-28, UT10-72 |

### 3.4 Redaction (`herness/core/redact_patterns.py`, `redact_directory.py`, `redact.py`, `redact_scan.py`)

#### U10-35 herness.core.redact.EntityType, Span, RedactionResult

| Field | Content |
|-------|---------|
| Kind | constant and classes (frozen dataclasses) |
| Purpose | Redaction value types (design 10 §3.4). |
| Signature | `EntityType = Literal["EMAIL", "PHONE", "IP", "PERSON", "EMPLOYEE_ID", "USER_ID", "CREDENTIAL", "URL_TOKEN", "CARD", "NATIONAL_ID", "CUSTOM"]`. `DETECTION_ORDER: tuple[EntityType, ...] = ("CREDENTIAL", "URL_TOKEN", "EMAIL", "CARD", "NATIONAL_ID", "EMPLOYEE_ID", "USER_ID", "PHONE", "IP", "PERSON", "CUSTOM")`. `Span(start: int, end: int, type: EntityType, replacement: str)`. `RedactionResult(text: str, counts: dict[EntityType, int])`. |
| Invariants | `0 ≤ start < end ≤ len(text)`; `counts` holds only types with count ≥ 1. |
| Algorithm | Declarations only. `Span` orders by `start`. |
| Side effects / Errors / Concurrency | none / none / immutable |
| Complexity and limits | n/a |
| Security notes | none |
| Tests | UT10-36 |

#### U10-36 herness.core.redact_patterns.Detector, build_detectors, TOKEN_PATTERN

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass), function, constant |
| Purpose | Compiled detectors with cheap prefilters (design 10 §5.3 table and throughput design). |
| Signature | `Detector(type: EntityType, prefilter: Callable[[str], bool], find: Callable[[str], Iterator[tuple[int, int, str]]])`, where `find` yields `(start, end, value_for_pseudonym)`. `build_detectors(cfg: RedactionConfig) -> tuple[Detector, ...]` returns detectors in `DETECTION_ORDER` except `PERSON` (U10-38). `TOKEN_PATTERN = re.compile(r"\[(?:EMAIL\|PHONE\|IP\|PERSON\|EMPLOYEE_ID\|USER_ID\|CARD\|NATIONAL_ID\|CUSTOM)_[0-9a-f]{10}\]\|\[SECRET\]")`. |
| Preconditions | `cfg` validated (U10-04). |
| Postconditions | All regexes compiled once. |
| Algorithm | Detectors (case-insensitive unless noted). **CREDENTIAL** (prefilter: text contains `=`, `:`, `-----`, `AKIA`, `gh`, `xox`, `eyJ` or `AccountKey`): (a) PEM `-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----`, span = whole block; (b) `\bauthorization\s*:\s*(?:bearer\|basic)\s+([A-Za-z0-9._~+/=-]+)`, span = group 1; (c) `\b(?:password\|passwd\|pwd\|secret\|api[_-]?key\|token)\s*[:=]\s*([^\s&"'<>,;]+)`, span = group 1; (d) case-sensitive known formats, span = whole match: `\bAKIA[0-9A-Z]{16}\b`, `\bgh[pousr]_[A-Za-z0-9]{36,255}\b`, `\bxox[abpors]-[A-Za-z0-9-]{10,200}\b`, `\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}`; (e) `AccountKey=([A-Za-z0-9+/=]{20,})`, span = group 1; (f) URL user info `\b[a-z][a-z0-9+.-]*://([^\s/:@]+:[^\s/@]+)@`, span = group 1. **URL_TOKEN** (prefilter `://`): URLs `\bhttps?://[^\s<>"']+`; parse the query with `urllib.parse`; for each parameter whose lower-cased name is `token`, `access_token`, `key`, `sig`, `signature`, `code`, `password`, `sv` or `se`, the value substring is a span. **EMAIL** (prefilter `@`): `\b[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,253}\.[a-z]{2,24}\b`. **CARD** (prefilter ≥ 13 ASCII digits): `\b(?:\d[ -]?){12,18}\d\b`, kept when it has 13–19 digits and `luhn_valid` holds. **NATIONAL_ID**, **EMPLOYEE_ID**, **USER_ID** (prefilter: a digit) and **CUSTOM** (no prefilter): config patterns, whole match. **PHONE** (prefilter ≥ 9 digits): candidates `(?<![\w+])\+?\d[\d ().-]{7,20}\d(?!\w)`; kept when the digit count is 9–15, the candidate contains no substring matching `\d{4}-\d{2}-\d{2}` (dates) or `\d{1,3}(?:\.\d{1,3}){3}` (IPv4-like); the lookbehind makes `INC0012345`/`CHG0012345` ineligible. **IP** (prefilter: `.` and a digit, or ≥ 2 `:`): IPv4 `\b(?:25[0-5]\|2[0-4]\d\|1?\d?\d)(?:\.(?:25[0-5]\|2[0-4]\d\|1?\d?\d)){3}\b`; IPv6 candidates `(?<![\w:])[0-9A-Fa-f:]{2,39}(?![\w:])` with ≥ 2 colons, accepted when `ipaddress.IPv6Address` parses them. |
| Side effects / Errors | none / none (config regexes were validated) |
| Concurrency | immutable; compiled patterns are thread-safe |
| Complexity and limits | linear in text length for the shipped patterns |
| Security notes | TH10-14. |
| Tests | UT10-36, UT10-38 |

#### U10-37 herness.core.redact_patterns.luhn_valid, normalize_value

| Field | Content |
|-------|---------|
| Kind | function (two, pure) |
| Purpose | Card checksum and pseudonym input normalization (design 10 §5.3). |
| Signature | `luhn_valid(digits: str) -> bool`; `normalize_value(type: EntityType, value: str) -> str` |
| Preconditions | `digits` holds ASCII digits only. |
| Algorithm | `luhn_valid`: from the right, double every second digit, subtract 9 when > 9, sum; valid when sum mod 10 is 0. `normalize_value`: `EMAIL` → stripped lower-case; `PHONE` → digits only, prefix `1` when 10 digits and the original did not start with `+`, result `+` + digits (E.164 form); `PERSON` → `casefold`, whitespace collapsed, stripped, and `^([^,]+),\s*(.+)$` rewritten to `<group 2> <group 1>`; `EMPLOYEE_ID`, `USER_ID`, `NATIONAL_ID`, `CARD` → upper-case with spaces and `-` removed; `IP` → `str(ipaddress.ip_address(value))`; `CUSTOM` → unchanged. |
| Side effects / Errors / Concurrency | none / none / pure |
| Complexity and limits | O(len) |
| Security notes | Stable normalization gives one token per person across formats. |
| Tests | UT10-39, PT10-04 |

#### U10-38 herness.core.redact_directory.NameDirectory

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | Aho-Corasick person-name matcher (design 10 §5.3 `PERSON`). |
| Signature | `NameDirectory.from_files(directory_file: Path \| None, extra_names: Sequence[str], display_names_file: Path \| None) -> NameDirectory` (class method); `find(text: str) -> list[tuple[int, int, str]]` returning `(start, end, canonical_name)`; attributes `size: int`, `variant_count: int`. |
| Preconditions | CSV header columns `display_name`, `alt_names` (`alt_names` separated by `;`). |
| Postconditions | The automaton is built and immutable. |
| Invariants | Every key is a lower-cased variant of ≥ 2 tokens, or a single token listed in `extra_names`. |
| Algorithm | Build: 1. Read the CSV with `csv.DictReader` (UTF-8, ≤ 200 MiB); a missing file logs `redact.directory.missing` at WARNING and yields an empty directory. 2. For each `display_name` with ≥ 2 tokens: canonical = `normalize_value("PERSON", display_name)`; variants `first last` (lower-cased as written), `last, first` and `last,first` (first = all tokens except the last). 3. Add each `alt_names` entry lower-cased with the same canonical. 4. Add `extra_names` entries (any token count) as their own canonical. 5. Add lines of `display_names_file` (U10-39) as in step 2. 6. Build a `pyahocorasick` `Automaton` mapping key → canonical and call `make_automaton()`. Find: 1. `low = text.lower()`; when `len(low) != len(text)`, build `low` per character with an index map back to `text`. 2. Iterate `automaton.iter(low)`. 3. Keep a match when the characters before start and after end are not alphanumeric. 4. Resolve overlaps longest first, then leftmost. |
| Side effects | reads files at build |
| Errors | unreadable file (permission) → `ConfigError("cannot read directory_file")` |
| Concurrency | immutable after build; `find` is thread-safe |
| Complexity and limits | build ≈ 2 s and ≈ 150 MB for 100k names; find O(len + matches) |
| Security notes | The directory is personal data, ACL-protected (§7(f)); its content is never logged. |
| Tests | UT10-46 |

#### U10-39 herness.core.redact_directory.update_display_names

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Persist user display names seen in lake `*_display` fields so `PERSON` covers them (design 10 §5.3). |
| Signature | `update_display_names(names: Iterable[str], *, data_dir: Path) -> int` (new names added) |
| Preconditions | Called by impl 03 at the start of its text stage with the distinct values of user-reference `*_display` columns. |
| Postconditions | `<data_dir>/cache/redact/display_names.txt` holds the sorted union, one name per line. |
| Algorithm | 1. Read the existing file (absent → empty). 2. Add stripped names with 2–8 tokens and 3–128 characters. 3. If the set grew, write atomically. 4. Return the count added. |
| Side effects | one file write |
| Errors | `OSError` → `StoreBusy("display_names write failed")` |
| Concurrency | single writer: only the build pipeline job calls it (impl 08 exclusive kinds) |
| Complexity and limits | ≤ 1,000,000 names |
| Security notes | Personal data under `data/`, covered by the folder ACL. |
| Tests | UT10-46 |

#### U10-40 herness.core.redact.Redactor (constructor, key_id)

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | Detect and replace PII and secrets (design 10 §3.4). |
| Signature | `__init__(cfg: RedactionConfig, hmac_key: bytes, directory: NameDirectory \| None = None)`; attribute `key_id: str` |
| Preconditions | `len(hmac_key) == 32`, else `ConfigError("redaction key must be 32 bytes")`. |
| Postconditions | `key_id = hashlib.sha256(hmac_key).hexdigest()[:8]`; detectors built (U10-36). |
| Invariants | Key, detectors and directory never change after construction. |
| Algorithm | Store config, key, detectors and directory (empty when `None`). With `cfg.ner == "presidio"`, `presidio_analyzer` is imported lazily at the first `scan(..., ner=True)`; a missing extra raises `ConfigError("install the ner extra")`. |
| Side effects / Errors | none / as above |
| Concurrency | immutable; all methods thread-safe |
| Complexity and limits | n/a |
| Security notes | The key is never logged; `repr` shows only `key_id`. |
| Tests | UT10-40 |

#### U10-41 herness.core.redact.Redactor.scan

| Field | Content |
|-------|---------|
| Kind | method |
| Purpose | Detect spans without replacing (redaction, egress re-scan, fixture scanner, impl 06 claim check). |
| Signature | `scan(self, text: str, *, ner: bool = False) -> list[Span]` |
| Preconditions | `len(text) ≤ 4,000,000`, else `RedactionFailed("text too long")`. |
| Postconditions | Spans do not overlap, are sorted, and never cover an existing pseudonym token. |
| Algorithm | 1. Protected ranges = all `TOKEN_PATTERN` matches. 2. For each detector in `DETECTION_ORDER` (PERSON from the directory at its position): skip when the prefilter is false; accept each `(start, end, value)` that overlaps neither a protected range nor an accepted span; replacement is `[SECRET]` for `CREDENTIAL` and `URL_TOKEN`, else `pseudonym(type, value)`. 3. `IP` spans are detected regardless of `mask_ip` (U10-42 decides replacement). 4. When `ner` is true and configured, run Presidio `PERSON` on the text with accepted spans masked and add non-overlapping hits (design 10 §5.3: only for `redacted_text` payloads leaving the machine). 5. Sort and return. |
| Side effects | none |
| Errors | `RedactionFailed` for oversize text; a detector exception is wrapped as `RedactionFailed(<type>)` |
| Concurrency | thread-safe |
| Complexity and limits | < 50 ms per MB (BT10-05) |
| Security notes | TH10-14. |
| Tests | UT10-36, UT10-37, UT10-38, UT10-45, BT10-05 |

#### U10-42 herness.core.redact.Redactor.redact

| Field | Content |
|-------|---------|
| Kind | method |
| Purpose | Return redacted text and counts. |
| Signature | `redact(self, text: str \| None) -> RedactionResult \| None` |
| Preconditions | as U10-41 |
| Postconditions | `None` → `None`; `""` → `RedactionResult("", {})`; redacting the output again changes nothing (PT10-02). |
| Algorithm | 1. `None` → `None`. 2. `spans = scan(text)`. 3. Drop `IP` spans when `cfg.mask_ip` is false. 4. Join unchanged segments and replacements left to right. 5. Count spans per type. |
| Side effects | none |
| Errors | `RedactionFailed` (fail closed; raw text is never returned on error) |
| Concurrency | thread-safe |
| Complexity and limits | ≥ 5,000 records/s per core at 1.5 KB average (BT10-03) |
| Security notes | TH10-14. |
| Tests | UT10-37–UT10-42, PT10-02, PT10-03, BT10-03 |

#### U10-43 herness.core.redact.Redactor.redact_batch

| Field | Content |
|-------|---------|
| Kind | method |
| Purpose | Redact a sequence in order. |
| Signature | `redact_batch(self, texts: Sequence[str \| None]) -> list[str \| None]` |
| Algorithm | Per item: `None` → `None`; else `redact(item).text`; `RedactionFailed` → `None` and `herness_redact_records_total{result="failed"}` += 1. |
| Side effects / Errors | metrics / none raised |
| Concurrency | thread-safe |
| Complexity and limits | linear |
| Security notes | Fail closed per item. |
| Tests | UT10-43 |

#### U10-44 herness.core.redact.Redactor.pseudonym

| Field | Content |
|-------|---------|
| Kind | method (pure given the key) |
| Purpose | Stable pseudonym token (design 10 §5.3). |
| Signature | `pseudonym(self, type: EntityType, value: str) -> str` |
| Postconditions | Output matches `^\[[A-Z_]+_[0-9a-f]{10}\]$`. |
| Algorithm | `h = hmac.new(key, (type + ":" + normalize_value(type, value)).encode("utf-8"), hashlib.sha256).hexdigest()[:10]`; return `"[" + type + "_" + h + "]"`. |
| Side effects / Errors / Concurrency | none / none / thread-safe |
| Complexity and limits | O(len(value)) |
| Security notes | Standard-library HMAC only (ENG §5.7). |
| Tests | UT10-39, UT10-40, PT10-05 |

#### U10-45 herness.core.redact.get_redactor, reset_redactor

| Field | Content |
|-------|---------|
| Kind | function (two) |
| Purpose | Process-wide redactor built from config (design 10 §3.4). |
| Signature | `get_redactor() -> Redactor`; `reset_redactor() -> None`; private `_load_key(ref: str) -> bytes` |
| Algorithm | Under `_RED_LOCK`: return the cached instance if present. Else `cfg = get_config()`; key = `bytes.fromhex(resolve(cfg.security.redaction.key).get_secret_value())` (not 64 hex characters → `ConfigError("redact.hmac_key must be 64 hex characters")`); directory = `NameDirectory.from_files(directory_file, extra_names, cfg.paths.data / "cache/redact/display_names.txt")`; build, cache, log `redact.directory.loaded`. `reset_redactor` clears the cache (U10-10 reset hook). |
| Side effects | keyring read, directory build |
| Errors | `ConfigError` for a missing or malformed key |
| Concurrency | lock-protected lazy init |
| Complexity and limits | first call ≈ 2 s with 100k names |
| Security notes | TH10-14. |
| Tests | UT10-40 |

#### U10-46 herness.core.redact.redact_text

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Wrapper used by impl 05 (trace payloads), impl 07, impl 08 (`last_error`), impl 09 (chat input, Jira titles). |
| Signature | `redact_text(text: str \| None) -> str \| None` |
| Algorithm | `None` → `None`. Else `get_redactor().redact(text).text`; on `RedactionFailed`, log `redact.record.failed` (no text) and return `None` (fail closed). |
| Side effects | log on failure |
| Errors | only `ConfigError` from `get_redactor` |
| Concurrency | thread-safe |
| Complexity and limits | as U10-42 |
| Security notes | TH10-14. |
| Tests | UT10-42 |

#### U10-47 herness.core.redact.redact_table

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Bulk redaction for `enrich.text_redacted` (design 10 §3.4, §5.3). |
| Signature | `redact_table(tbl: pa.Table, text_cols: Sequence[str], id_col: str = "record_id", workers: int \| None = None) -> pa.Table` |
| Preconditions | `id_col` and every `text_cols` entry exist as string columns, else `SchemaViolation("redact_table: missing column <c>")`. |
| Postconditions | Output schema `(<id_col>: string, text: string)`; same row order and count. |
| Algorithm | 1. `workers` default = `get_config().sources.build.threads` (T02-01 (herness.model.settings.BuildSettings) key), minimum 1. 2. Split into chunks of 20,000 rows. 3. With `workers == 1` or one chunk, run in-process; else `ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(profile, overrides, config_dir))`, where `_init_worker` calls `init_config` and builds its own redactor. Each chunk travels as built-in lists (`list[str]` IDs, `list[list[str \| None]]` texts) and returns `list[str \| None]` plus a failure count (standard-library process serialisation of built-in types only; ENG exception X-2 in §13). 4. Per row: redact each text column separately; join non-null results with `"\n\n"`; all null → null. A row whose redaction raises gets `text = NULL` and counts as failed; raw text is never substituted. 5. Assemble the table; log `redact.table.completed` (`rows`, `failed_rows`, `workers`, `duration_ms`); record metrics. |
| Side effects | worker processes; logs; metrics |
| Errors | schema errors as above; a crashed worker → `StoreBusy("redaction worker crashed")` (retryable, so the build job retries) |
| Concurrency | caller blocks; workers are processes |
| Complexity and limits | 6M records < 5 min on 16 cores (BT10-04); memory ≈ 2 × chunk text × workers |
| Security notes | TH10-14; fail closed. |
| Tests | UT10-43, UT10-44, FT10-07, BT10-04 |

#### U10-48 herness.core.redact.RedactionFailed

| Field | Content |
|-------|---------|
| Kind | class (exception, subclass of `FatalError`) |
| Purpose | Redaction of one text failed. |
| Signature | `RedactionFailed(message: str)`; the message names the entity type or `text too long`, never the text |
| Algorithm | Declaration only. |
| Side effects / Errors / Concurrency | none |
| Complexity and limits | n/a |
| Security notes | Fail-closed contract of design 10 §5.3. |
| Tests | UT10-43 |

#### U10-49 herness.core.redact_scan.main

| Field | Content |
|-------|---------|
| Kind | function (entry behind `python -m herness.core.redact --scan PATH`) |
| Purpose | Fixture PII scanner run by the spec 11 pre-commit hook. |
| Signature | `main(argv: Sequence[str] \| None = None) -> int` |
| Preconditions | `herness/core/redact.py` ends with a `__main__` guard calling `sys.exit(redact_scan.main())`. |
| Postconditions | Exit 0 no finding; 1 findings; 2 usage error. |
| Algorithm | 1. Parse `--scan PATH` (repeatable) with `argparse`. 2. Build a `Redactor` with an all-zero 32-byte key (detection does not depend on the key), default `RedactionConfig` and no directory (no real directory in the repository). 3. Read `security.redaction.denylist_domains` from `config/herness.yaml` merged with `config/profiles/synth.yaml` (U10-15, no full load). 4. Walk files with suffix `.csv`, `.json`, `.jsonl`, `.yaml`, `.yml`, `.txt`, `.md`, `.sql`, `.parquet` (string columns via `pyarrow.parquet`); a file > 50 MiB is a finding "file too large to scan". 5. Allow rules (reserved synthetic ranges, spec 11 §5.1.4): `EMAIL` ending `@example.com` or `@example.org`; `PHONE` whose normalized form is `+120255501` plus two digits (the fictional form `+1-202-555-01xx`, R-56); `IP` in `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`; `EMPLOYEE_ID` any; `CARD` starting `4111`; `CREDENTIAL` and `URL_TOKEN` whose value starts (case-insensitive) with `synthetic` (R-67); `NATIONAL_ID` starting with `9` or `000`. 6. A denylisted domain appearing as a host suffix (case-insensitive, label-bounded) is a finding. 7. Print `<path>:<line>:<col> <TYPE>` per finding (never the value); return the code. |
| Side effects | reads files; stdout |
| Errors | unreadable file → finding "unreadable", exit 1 |
| Concurrency | single-threaded |
| Complexity and limits | linear in bytes |
| Security notes | Keeps real PII out of the repository (TH10-06; spec 11 §9). |
| Tests | UT10-47, ST10-30 |

### 3.5 Egress guard (`herness/core/egress.py`, `egress_log.py`, `egress_socket.py`)

#### U10-50 herness.core.egress.Purpose, PayloadClass and constants

| Field | Content |
|-------|---------|
| Kind | constant |
| Purpose | Closed sets and profile tables (design 10 §3.5, §4.3, §5.4). |
| Signature | `Purpose = Literal["reasoning_final", "reasoning", "bulk_classification", "model_download"]`; `PayloadClass = Literal["aggregated_evidence", "redacted_text", "none"]`; `PAYLOAD_CLASSES_BY_PROFILE = {"local": ∅, "synth": ∅, "hybrid": {"aggregated_evidence"}, "premium": {"aggregated_evidence", "redacted_text"}}`; `MODEL_DOWNLOAD_HOSTS = {"huggingface.co", "cdn-lfs.huggingface.co"}` (verification V-17); `MAX_RESPONSE_BYTES = 52_428_800`; `CHARS_PER_TOKEN = 3.5`; `BLOCKING_TYPES` = `EMAIL`, `PHONE`, `CARD`, `NATIONAL_ID`, `CREDENTIAL`, `URL_TOKEN`, `EMPLOYEE_ID`, `USER_ID`, `PERSON` (design 10 §5.4 step 6; `IP` added when `mask_ip`); `LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}`. |
| Algorithm | Declarations. |
| Side effects / Errors / Concurrency | none |
| Complexity and limits | n/a |
| Security notes | TH10-14, TH10-23. |
| Tests | UT10-49 |

#### U10-51 herness.core.egress.EgressGuard.check

| Field | Content |
|-------|---------|
| Kind | method |
| Purpose | Decide one off-network request and log the decision (design 10 §5.4 steps 1–7). |
| Signature | `check(self, url: str, body: bytes, purpose: Purpose, payload_class: PayloadClass, token_estimate: int \| None = None) -> None`; private `_check_and_log(self, url, body, purpose, payload_class, token_estimate, *, method: str, run_id: str \| None, task_id: str \| None) -> EgressTicket` with `EgressTicket(egress_id: str, tokens_in: int, started_monotonic: float)`. |
| Preconditions | The guard was built from the current config (U10-56). |
| Postconditions | Either one `allowed` line was written and a ticket returned, or one `blocked` line and one audit `egress` line were written and `EgressBlocked` raised. Nothing is sent by this method. |
| Algorithm | `egress_id = "egr_" + new_ulid()`. Checks in order, stopping at the first failure (reason code in backticks): 1. `security.egress.enabled` true (`profile_forbids_egress`); a `model_download` inside an open download window (U10-55) skips steps 1–2. 2. `purpose` in `security.egress.purposes` (`purpose_not_allowed`); `payload_class` in `PAYLOAD_CLASSES_BY_PROFILE[profile]` (`payload_class_not_allowed`); `payload_class == "none"` only with `model_download`; in profile `hybrid`, purpose `reasoning` additionally requires `security.data_policy.chat_approved` (`chat_not_approved`; R-38: in `hybrid` only chat `cloud` mode uses `reasoning`). 3. Parse with `httpx.URL`: scheme `https` (`scheme_not_https`); port absent or 443 (`port_not_443`); no user info (`userinfo_present`); host not an IP literal (`ip_literal`); lower-cased host exactly in `security.egress.destinations`, or, for a windowed `model_download`, in `MODEL_DOWNLOAD_HOSTS` or equal to the registry host of a pinned `deploy.*.image` (U10-82 step 1) (`host_not_allowed`). 4. `len(body) ≤ max_request_bytes` (`body_too_large`); streaming bodies are refused by the transport (`streaming_body`). 5. `tokens = token_estimate or ceil(len(body) / CHARS_PER_TOKEN)`; `tokens ≤ max_tokens_per_request` (`tokens_per_request`); `EgressLog.tokens_today() + tokens ≤ max_tokens_per_day` (`tokens_per_day`). 6. Re-scan: decode UTF-8 (`body_not_utf8`); when the body parses as JSON scan each string leaf and object key separately, else the whole text; each string is scanned as-is and again after `unicodedata.normalize("NFKC", s)` with U+200B–U+200D, U+2060 and U+FEFF removed; a string > 1,000,000 chars → `string_too_long`; count hits per type (tokens are protected by construction); any hit of a `BLOCKING_TYPES` type → `pii_detected`. 7. Allowed: write the log line (U10-57) with `decision="allowed"`, `tokens_in`, `payload_sha256` (SHA-256 hex of `body`), `scan_hits={}`, and return the ticket. Blocked: write the line with `decision="blocked"`, `reason`, `scan_hits` (counts only); `audit("egress", "system", egress_id=..., reason=...)`; raise `EgressBlocked("egress blocked (<egress_id>): <reason>", egress_id=..., reason=...)` with the attributes of U10-108 (R-19). A failure writing either log raises `EgressBlocked(... "egress_log_failed")`; nothing is sent (fail closed). |
| Side effects | egress log line; audit line when blocked; metrics |
| Errors | `EgressBlocked` for every refusal |
| Concurrency | thread-safe; step 5 and the append run inside `EgressLog.locked()`, so concurrent calls cannot both pass a cap only one fits |
| Complexity and limits | scan < 50 ms per MB (BT10-05) |
| Security notes | TH10-14, TH10-16, TH10-17, TH10-20, TH10-22, TH10-23, TH10-49. LLM02, LLM10. |
| Tests | UT10-48–UT10-54, ST10-07, ST10-09–ST10-13, ST10-33, ST10-35, ST10-56, FT10-08 |

#### U10-52 herness.core.egress.EgressGuard.http_client

| Field | Content |
|-------|---------|
| Kind | method |
| Purpose | The only way to obtain a synchronous client for a non-local endpoint (design 10 §3.5). |
| Signature | `http_client(self, purpose: Purpose, payload_class: PayloadClass, *, run_id: str \| None = None, task_id: str \| None = None, timeout: float = 120.0) -> httpx.Client` |
| Preconditions | `0 < timeout ≤ 3,600`. `purpose == "model_download"` → `EgressBlocked("model_download only inside deploy pull")`. |
| Postconditions | Transport is `GuardedTransport`; `follow_redirects=False`; `trust_env=False`; `timeout=httpx.Timeout(timeout, connect=10.0)`. |
| Algorithm | 1. `ssl_ctx = ssl.create_default_context(cafile=certifi.where())`, `minimum_version = ssl.TLSVersion.TLSv1_2` (hostname checking and `CERT_REQUIRED` are defaults). 2. Inner `httpx.HTTPTransport(verify=ssl_ctx, proxy=security.network.http_proxy, retries=0)`. 3. Wrap in `GuardedTransport`. 4. Return `httpx.Client(transport=..., follow_redirects=False, trust_env=False, timeout=...)`. |
| Side effects | none until a request is sent |
| Errors | as preconditions |
| Concurrency | the client is thread-safe per `httpx` |
| Complexity and limits | n/a |
| Security notes | TH10-15, TH10-17, TH10-18 (no env proxies, TLS 1.2+, verification on). |
| Tests | UT10-52, ST10-39 |

#### U10-53 herness.core.egress.EgressGuard.async_http_client

| Field | Content |
|-------|---------|
| Kind | method |
| Purpose | Async twin used by T05-08 (herness.harness.llm.anthropic_client.AnthropicClient). |
| Signature | `async_http_client(self, purpose: Purpose, payload_class: PayloadClass, *, run_id: str \| None = None, task_id: str \| None = None, timeout: float = 120.0) -> httpx.AsyncClient` (the design's `**kw` are exactly these keywords) |
| Algorithm | As U10-52 with `httpx.AsyncHTTPTransport` and `AsyncGuardedTransport`. |
| Side effects / Errors | as U10-52 |
| Concurrency | async-safe |
| Complexity and limits | n/a |
| Security notes | as U10-52 |
| Tests | UT10-52 |

#### U10-54 herness.core.egress.GuardedTransport, AsyncGuardedTransport

| Field | Content |
|-------|---------|
| Kind | class (two; `httpx.BaseTransport` / `httpx.AsyncBaseTransport` wrapping an inner transport) |
| Purpose | Run `check()` inside `handle_request` before the pool opens a socket, then log completion (design 10 §5.4, §4.5). |
| Signature | `__init__(inner, *, guard: EgressGuard, purpose: Purpose, payload_class: PayloadClass, run_id: str \| None, task_id: str \| None)`; `handle_request(request) -> httpx.Response`; `handle_async_request(request) -> httpx.Response` (async); `close()` / `aclose()` delegate. |
| Preconditions | none |
| Postconditions | For every request that reached the inner transport, exactly one `completed` line with the same `egress_id` is written when the response closes or the send fails. |
| Algorithm | 1. If `request.content` raises `httpx.RequestNotRead` (a streaming body), take the guard's blocked path with reason `streaming_body`. 2. `ticket = guard._check_and_log(str(request.url), request.content, ...)`; the async version runs it with `asyncio.to_thread`. 3. Call the inner transport. 4. Wrap the response stream in a counting stream that raises `EgressBlocked(... "response_too_large")` past `MAX_RESPONSE_BYTES`; the request is sent with `Accept-Encoding: identity`, a `gzip`/`x-gzip`/`deflate` body is decoded by the transport and the cap counts decoded bytes (`bytes_in` is the decoded size), and any other `content-encoding` is refused unread with `EgressBlocked(... "unsupported_encoding")` and a `completed` line with that reason (T10-17 review ruling). 5. On close: when `content-type` starts with `application/json` and the body ≤ 10 MiB, read `usage.input_tokens`/`usage.output_tokens` (Anthropic) or `usage.prompt_tokens`/`usage.completion_tokens` (OpenAI-compatible); write the `completed` line with `status_code`, `bytes_in`, `latency_ms` (monotonic), `tokens_in` (provider figure or the estimate), `tokens_out` (figure or `null`). 6. On an inner exception, write the `completed` line with `status_code=null`, `reason=<exception class>`, then re-raise. |
| Side effects | egress lines; metrics |
| Errors | `EgressBlocked`; transport exceptions propagate (T05-08 (herness.harness.llm.anthropic_client.AnthropicClient) maps them) |
| Concurrency | per-request state |
| Complexity and limits | response ≤ 50 MiB |
| Security notes | TH10-19 (allowed line precedes the socket), TH10-21. |
| Tests | UT10-54, ST10-40, ST10-41, IT10-03 |

#### U10-55 herness.core.egress.EgressGuard.download_window

| Field | Content |
|-------|---------|
| Kind | method (context manager; additive, delta D10-04) |
| Purpose | Permit purpose `model_download` for `herness deploy pull --allow-download` only (design 10 §5.4). |
| Signature | `download_window(self, *, allow_download: bool, actor: str) -> ContextManager[None]` |
| Preconditions | `allow_download` true (else `EgressBlocked("deploy pull requires --allow-download")`); profile not `synth` (else `EgressBlocked("profile synth cannot download")`); `os.environ` lacks `HERNESS_WORKER=1`, which T08-21 (herness.core.jobs.run_worker) sets in worker processes (else `EgressBlocked("model_download is refused inside jobs")`). |
| Postconditions | The window flag is cleared on exit, including on exception. |
| Algorithm | 1. Check preconditions. 2. Set the thread-local flag. 3. Log `egress.download_window.opened`; `audit("admin_action", actor, action="deploy_pull", target="download_window")`. 4. Yield. 5. Clear the flag; log `egress.download_window.closed`. |
| Side effects | log and audit lines |
| Errors | as preconditions |
| Concurrency | thread-local flag |
| Complexity and limits | n/a |
| Security notes | TH10-23. |
| Tests | UT10-55, ST10-35 |

#### U10-56 herness.core.egress.EgressGuard (constructor), get_guard, reset_guard

| Field | Content |
|-------|---------|
| Kind | constructor and functions |
| Purpose | Process-wide guard (design 10 §3.5). |
| Signature | `EgressGuard(cfg: HernessConfig, redactor_factory: Callable[[], Redactor], log: EgressLog)`; `get_guard() -> EgressGuard`; `reset_guard() -> None` |
| Algorithm | `get_guard`: under `_GUARD_LOCK`, build once from `get_config()`, `get_redactor` (called lazily at the first re-scan, so a profile without egress never needs the key) and `EgressLog(cfg.paths.logs, config_hash, profile)`. `reset_guard` clears it (U10-10 hook). |
| Side effects / Errors | none / none |
| Concurrency | lock-protected lazy init |
| Complexity and limits | n/a |
| Security notes | none |
| Tests | UT10-48 |

#### U10-57 herness.core.egress_log.EgressLog

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | Append egress lines (design 10 §4.5) and compute today's token total. |
| Signature | `__init__(logs_dir: Path, config_hash: str, profile: str)`; `locked() -> ContextManager[None]`; `write(line: dict[str, Any]) -> None`; `tokens_today(now: datetime) -> int` |
| Preconditions | `logs_dir` exists (created with `mkdir(parents=True, exist_ok=True)`). |
| Postconditions | Each line is canonical JSON plus `\n` in `egress-<YYYY-MM-DD>.jsonl` (UTC date of `ts`). |
| Invariants | No payload field. Keys are exactly design 10 §4.5 plus `reason` on completed lines. |
| Algorithm | `locked`: enter U10-61 `log_lock(<logs>/.egress.lock)`. `write`: add `ts` (ISO-8601 UTC, milliseconds, `Z`), `profile`, `config_hash`; unknown keys → `SchemaViolation`; append with U10-61, passing `lock_held=True` inside `locked()`. `tokens_today`: keep `(date, byte_offset, total, map)` in memory; read new bytes of today's file from `byte_offset`; for `allowed` lines add `tokens_in` and remember it by `egress_id`; for `completed` lines add `(tokens_in + (tokens_out or 0)) − <remembered allowed tokens_in>`; reset at UTC date change. |
| Side effects | appends and reads |
| Errors | `StoreBusy` from the lock |
| Concurrency | cross-process lock file plus an in-process lock |
| Complexity and limits | incremental read; ≤ 100,000 map entries per day |
| Security notes | TH10-19, TH10-22. |
| Tests | UT10-52, UT10-54, ST10-13 |

#### U10-58 herness.core.egress_socket.SocketPolicy, install_socket_guard, reset_socket_guard

| Field | Content |
|-------|---------|
| Kind | class and functions |
| Purpose | Process-level defense in depth (design 10 §5.4 "Socket guard"). |
| Signature | `SocketPolicy(allowed_hosts: frozenset[str], proxy_host: str \| None)` with `check_getaddrinfo(host, port) -> None`, `check_connect(address) -> None`, `check_sendto(address) -> None`. `install_socket_guard(cfg: HernessConfig \| BootstrapConfig) -> None` (widened type, delta D10-04). `reset_socket_guard() -> None`. |
| Preconditions | none |
| Postconditions | One audit hook registered per process; the latest policy is active; `HF_HUB_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`, `DO_NOT_TRACK=1` set in `os.environ`. |
| Algorithm | Constant `SDK_SOURCE_KINDS = frozenset({"snowflake", "mongodb"})` (sources whose vendor SDK builds its own client, R-06). `install_socket_guard`: 1. Allowed (R-06) = loopback (`LOOPBACK_HOSTS`) ∪ the source `hosts` lists (`source_hosts` of a bootstrap config, or, from a full config, the union of `sources.sources.<name>.hosts` of enabled sources plus the `base_url` hosts of enabled sources whose name is not in `SDK_SOURCE_KINDS`; nothing is derived from `base_url` for SDK sources) ∪ the egress allowlist (`security.egress.destinations` when egress is enabled, and `security.network.extra_allowed_hosts`) ∪ the proxy host; profile `synth` allows loopback only. 2. Set the three variables. 3. Store the policy in `_POLICY`. 4. First call only: `sys.addaudithook(_hook)`. 5. Log `egress.guard.installed` (`profile`, `allowed_hosts_count`). `_hook(event, args)`: return when `_POLICY` is `None` or the thread-local re-entrancy flag is set; `socket.getaddrinfo` → `check_getaddrinfo(args[0], args[1])`; `socket.connect` → `check_connect(args[1])`; `socket.sendto` → `check_sendto(args[1])`. `check_getaddrinfo`: allow `None`, `""`, `localhost` and loopback literals; allow a host in `allowed_hosts`, and when it is not in the resolution cache (300 s expiry) set the re-entrancy flag, call `socket.getaddrinfo(host, port)`, add the addresses to `_RESOLVED`, clear the flag; allow an IP literal already in `_RESOLVED`; else log `egress.socket.blocked` and raise `EgressBlocked("socket blocked: <host>")`. `check_connect` / `check_sendto`: allow loopback IPs, IPs in `_RESOLVED`, and `AF_UNIX` addresses (str or bytes); else raise `EgressBlocked`. `reset_socket_guard`: `_POLICY = None`, clear `_RESOLVED` (the hook stays and becomes a no-op; U10-10 hook). |
| Side effects | environment variables; audit hook |
| Errors | `EgressBlocked` raised from the audit hook aborts the socket operation |
| Concurrency | `_POLICY` swapped atomically; `_RESOLVED` under a lock; thread-local re-entrancy flag |
| Complexity and limits | < 20 µs per `connect` (BT10-06) |
| Security notes | TH10-15, TH10-16, TH10-24, TH10-48. Module state `_POLICY`/`_RESOLVED` is ENG §2.3 exception X-1 (§13). |
| Tests | UT10-56, PT10-07, ST10-08, ST10-29, ST10-55, IT10-15, BT10-06 |

#### U10-59 herness.core.egress.loopback_http_client, aloopback_http_client

| Field | Content |
|-------|---------|
| Kind | function (R-06; owned by this spec; implemented in `herness/core/egress_clients.py` and re-exported from `herness.core.egress`) |
| Purpose | The only client for local model servers (vLLM, OpenJev, llama.cpp) and their health checks, so nothing outside `egress.py` constructs an `httpx` client (ENG §2.1, lint ST10-25). |
| Signature | `loopback_http_client(base_url: str, *, timeout_s: float, bearer: SecretStr \| None = None) -> httpx.Client`: exactly the R-06 signature `(base_url: str, *, timeout_s: float)` (no default for `timeout_s`), plus the optional keyword-only `bearer` with default `None`, so a call written to R-06 is valid. Async variant `aloopback_http_client(base_url: str, *, timeout_s: float, bearer: SecretStr \| None = None) -> httpx.AsyncClient`, with the same checks over `httpx.AsyncHTTPTransport` and an async `LoopbackOnlyTransport`. |
| Preconditions | `base_url` parses with `httpx.URL`, has scheme `http` or `https`, no user info, and a host in `LOOPBACK_HOSTS` (U10-50), else `EgressBlocked("loopback client used for <host>", reason="not_loopback")` before any client exists. `0 < timeout_s ≤ 3,600`, else `ConfigError`. |
| Postconditions | The returned client (sync or async) has `base_url=base_url`, transport `LoopbackOnlyTransport`, `follow_redirects=False`, `trust_env=False`, `timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 5.0))`. |
| Invariants | Every request sent through the client, including one with an absolute URL that overrides `base_url`, goes to a host in `LOOPBACK_HOSTS`. |
| Algorithm | 1. Check the preconditions. 2. `inner = httpx.HTTPTransport(retries=0)` (`httpx.AsyncHTTPTransport(retries=0)` for the async variant; no proxy). 3. Wrap it in `LoopbackOnlyTransport`, whose `handle_request` reads `request.url.host`, lower-cases it and raises `EgressBlocked("loopback client used for <host>", reason="not_loopback")` when it is not in `LOOPBACK_HOSTS`; it also refuses a request whose URL carries user info. The refusal is logged as `egress.loopback.blocked` (host only) and counted in `herness_socket_blocked_total{event="loopback_client"}`. 4. When `bearer` is given, set the default header `Authorization: Bearer <bearer.get_secret_value()>`; the header value never appears in logs (U10-32). 5. Return the client. |
| Side effects | none until a request is sent; a refused request writes one log line |
| Errors | `EgressBlocked` (`not_loopback`), `ConfigError` for the timeout |
| Concurrency | thread-safe (`httpx.Client`); the async variant is async-safe |
| Complexity and limits | n/a |
| Security notes | TH10-15, TH10-47. Local calls are not egress: they write no egress line and pass no redaction re-scan. |
| Tests | UT10-74, ST10-54 |

#### U10-110 herness.core.egress.source_http_client

| Field | Content |
|-------|---------|
| Kind | function (R-06: the factory for `httpx`-based source connectors; implemented in `herness/core/egress_clients.py` and re-exported from `herness.core.egress`) |
| Purpose | The only way for a source connector (impl 01: ServiceNow, Jira, monitoring adapters, the Dataverse Web API) to obtain an `httpx` client, restricted to that source's hosts, with TLS verification that cannot be switched off. |
| Signature | `source_http_client(source: str, base_url: str, *, timeout_s: float, auth: httpx.Auth \| None = None, verify: Literal[True] \| Path = True, max_connections: int = 4, max_response_bytes: int = 104_857_600, headers: Mapping[str, str] \| None = None) -> httpx.Client`. `source` and `base_url` positional; the rest keyword-only. The four parameters requested by impl 01 come first; the others are optional additions with defaults. |
| Preconditions | `source` is a key of `sources.sources` whose mapping does not have `enabled: false`, else `ConfigError("unknown or disabled source <source>")`. `base_url` parses with `httpx.URL`, has no user info, uses `https` (or `http` only when its host is in `LOOPBACK_HOSTS`), else `EgressBlocked("source client refused: <reason>", reason=...)` with reason `scheme_not_https` or `userinfo_present`. `verify` is `True` or an existing CA bundle file; any other value, including `False`, raises `ConfigError("TLS verification cannot be disabled for source <source>")`. `0 < timeout_s ≤ 600`; `1 ≤ max_connections ≤ 64`; `1 ≤ max_response_bytes ≤ 1,073,741,824`; else `ConfigError`. |
| Postconditions | The client has `base_url=base_url`, transport `SourceHostTransport`, `follow_redirects=False`, `trust_env=False`, `timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 10.0))`, `limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections)`, `auth=auth`, and the given `headers`. |
| Invariants | Every request, including one with an absolute URL or a response-supplied next link, goes only to a host in the source allowlist and in the process egress allowlist, over verified TLS (except loopback `http`). |
| Algorithm | 1. Source allowlist (R-06): the lower-cased entries of `sources.sources.<source>.hosts`, plus the host of `base_url` when `source` is not in `SDK_SOURCE_KINDS` (U10-58). The host of `base_url` must be in it, else `EgressBlocked(..., reason="host_not_allowed")`. 2. Process allowlist: the `allowed_hosts` of the installed `SocketPolicy` (U10-58), or, when none is installed, the allowlist U10-58 step 1 computes from `get_config()`; the `base_url` host must be in it too (fail closed). 3. TLS: `ssl_ctx = ssl.create_default_context(cafile=<verify path> if a path else certifi.where())`, `minimum_version = ssl.TLSVersion.TLSv1_2`, `check_hostname` true, `verify_mode = CERT_REQUIRED`. 4. `inner = httpx.HTTPTransport(verify=ssl_ctx, proxy=security.network.http_proxy, retries=0)` (retries belong to the caller's T08-04 (herness.core.resilience.policies.RetryPolicy)). 5. Wrap in `SourceHostTransport`, whose `handle_request` refuses, before the inner transport is called, any request whose host is not in both allowlists (`host_not_allowed`), whose scheme is not `https` for a non-loopback host (`scheme_not_https`), or whose URL has user info (`userinfo_present`); each refusal raises `EgressBlocked("source client refused: <reason>", reason=...)`, logs `egress.source.blocked` (source, host, reason) and increments `herness_socket_blocked_total{event="source_client"}`. 6. The response stream is wrapped in a counting stream that raises `EgressBlocked("source response too large", reason="response_too_large")` past `max_response_bytes`. 7. Return the client. Source traffic is on-network and is not egress under design 10 §3.5: it writes no egress JSONL line, passes no redaction re-scan, and needs no data-policy approval; the socket guard (U10-58) still applies underneath. |
| Side effects | none until a request is sent; refused requests write one log line |
| Errors | `ConfigError`, `EgressBlocked` (`host_not_allowed`, `scheme_not_https`, `userinfo_present`, `response_too_large`); transport errors propagate to the caller (impl 01 maps them) |
| Concurrency | thread-safe (`httpx.Client`) |
| Complexity and limits | response ≤ `max_response_bytes` (default 100 MiB); connection pool ≤ `max_connections` |
| Security notes | TH10-51, TH10-15, TH10-18. |
| Tests | UT10-82, ST10-58, ST10-59 |

### 3.6 Audit (`herness/core/audit.py`)

#### U10-60 herness.core.audit.AuditEvent, audit

| Field | Content |
|-------|---------|
| Kind | constant and function |
| Purpose | Append one audit line with the hash chain (design 10 §3.6, §4.6). |
| Signature | `AuditEvent = Literal["review_decision", "recommendation_decision", "config_change", "admin_action", "auth", "egress"]`; `audit(event: AuditEvent, actor: str, **fields: Any) -> None` |
| Preconditions | `actor` matches `^[0-9a-f]{32}$` (a `user_ref`) or is `system` or `eval`, else `SchemaViolation("audit actor invalid")`. `fields` keys are exactly the allowed set below; values are `str` (≤ 256 chars), `int`, `bool`, `None` or a list of ≤ 200 such strings; `admin_action.action` is in the action list. |
| Postconditions | One line appended to `<paths.logs>/audit-<YYYY-MM-DD>.jsonl`, or an exception with nothing written. |
| Algorithm | 1. Validate. 2. Every string value is checked against `known_values()` (substring) and U10-36's `CREDENTIAL` detector; a hit → `SchemaViolation("audit field would contain a secret")`. 3. Build `{"ts", "audit_id": "aud_" + new_ulid(), "event", "actor", "fields", "config_hash": <cached hash or null>, "prev_hash"}`. 4. Append with U10-61 in chain mode (`chain_glob="audit-*.jsonl"`, lock `.audit.lock`): `prev_hash` = SHA-256 hex of the previous line's UTF-8 bytes without the newline (previous line of today's file, else of the newest earlier audit file), or 64 zeros when there is none; serialise canonical JSON (sorted keys, compact) plus `\n`; flush and `os.fsync`. 5. `herness_audit_lines_total{event}` += 1. 6. `StoreBusy` from the lock → log `audit.write.failed` and raise `FatalError("audit write failed: <event>")`; callers do not perform the audited action (design 10 §6). |
| Side effects | file append |
| Errors | `SchemaViolation`, `FatalError` |
| Concurrency | cross-process lock (U10-61) |
| Complexity and limits | < 5 ms typical |
| Security notes | TH10-08, TH10-09, no secrets in audit. |
| Tests | UT10-57, UT10-58, ST10-17, ST10-18, FT10-01 |

Allowed fields per event (design 10 §4.6; action list extended by delta D10-07):

| Event | Fields |
|-------|--------|
| `review_decision` | `item_id`, `kind`, `status`, `decided_by`, `note_len` |
| `recommendation_decision` | `rec_id`, `decision`, `decided_by` |
| `config_change` | `old_hash`, `new_hash`, `changed_paths`, `profile` |
| `admin_action` | `action`, `target`, optional `counts` (`k=v;k=v` string), optional `detail` (≤ 256 chars) |
| `auth` | `user_ref`, `role`, `result` |
| `egress` | `egress_id`, `reason` |

`admin_action.action` values: `secret_set`, `secret_rotate`, `privacy_delete`, `deploy_up`, `deploy_down`, `deploy_rollback`, `deploy_pull`, `deploy_prune`, `deploy_install`, `deploy_render`, `profile_switch`, `purge`, `backup`, `redact_rekey`.

#### U10-61 herness.core.audit.log_lock, append_jsonl_locked

| Field | Content |
|-------|---------|
| Kind | function (two) |
| Purpose | Cross-process locked append shared by audit, egress and deploy-history logs. |
| Signature | `log_lock(lock_path: Path, *, timeout_s: float = 10.0) -> ContextManager[None]`; `append_jsonl_locked(path: Path, line_builder: Callable[[bytes \| None], bytes], *, lock_path: Path, chain_glob: str \| None = None, lock_held: bool = False) -> None`; `line_builder` receives the previous line (chain mode) or `None`. |
| Preconditions | Parent directory exists. With `lock_held=True` the caller holds `log_lock(lock_path)`. |
| Postconditions | The line is appended atomically with respect to other callers. |
| Algorithm | `log_lock`: take a per-process `threading.Lock` for `lock_path`, then open `lock_path` and take an exclusive OS lock (`msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` on Windows, `fcntl.flock(fd, LOCK_EX \| LOCK_NB)` elsewhere), polling every 50 ms up to `timeout_s`; timeout → `StoreBusy("log lock timeout: <name>")`; release in `finally`. `append_jsonl_locked`: 1. Unless `lock_held`, enter `log_lock`. 2. With `chain_glob`, read the previous line by seeking backwards from the end of `path`, or of the newest earlier file matching `chain_glob` in name order, to the last `\n`-terminated line. 3. `data = line_builder(prev)`. 4. Open `path` in `"ab"`, write, flush, `os.fsync`. 5. Leave the lock. |
| Side effects | lock file and append |
| Errors | `StoreBusy` on lock timeout; `OSError` → `StoreBusy("log write failed: <name>")` |
| Concurrency | cross-process exclusive lock |
| Complexity and limits | lock wait ≤ 10 s (a lock wait, not a retry; delta D10-08) |
| Security notes | TH10-09. |
| Tests | UT10-57, FT10-01 |

#### U10-62 herness.core.audit.verify_chain, ChainReport

| Field | Content |
|-------|---------|
| Kind | function and class (frozen dataclass) |
| Purpose | Detect silent edits (design 10 §4.6; doctor check). |
| Signature | `verify_chain(logs_dir: Path) -> ChainReport`; `ChainReport(ok: bool, files: int, lines: int, first_break: str \| None)` (`<file>:<line number>`) |
| Algorithm | 1. List `audit-*.jsonl` by name. 2. Accept any `prev_hash` on the first line of the oldest file (older files may be purged). 3. Every other line's `prev_hash` must equal SHA-256 of the previous line's bytes (possibly in the previous file). 4. Each line parses as JSON with the §4.6 keys, an `aud_` ID and a non-decreasing `ts`. 5. A final line without `\n` is a break unless the file's mtime is < 2 s old (a write in progress). 6. Stop at the first break. |
| Side effects | reads |
| Errors | none raised; unreadable file → break `<file>:0` |
| Concurrency | read-only |
| Complexity and limits | ≈ 1 s per 1M lines |
| Security notes | TH10-09. |
| Tests | UT10-59, ST10-17, PT10-08 |

#### U10-63 herness.core.audit.record_config_change

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Design 10 §5.1 step 5, run by the CLI entry and the dashboard entry after `init_config` (kept out of `load_config` so the import order stays acyclic). |
| Signature | `record_config_change(cfg: HernessConfig, *, actor: str = "system") -> str` (current hash) |
| Postconditions | If the hash differs from the last audited one: one `config_change` line and `data/config_snapshots/<hash>.yaml` exist. |
| Algorithm | 1. `h = config_hash(cfg)`. 2. Enter `log_lock(<logs>/.audit.lock)`; read `<paths.data>/config_snapshots/LAST`; if missing, scan audit files newest first for the last `config_change` and take `new_hash`. 3. Equal to `h` → leave and return. 4. Write snapshot `<h>.yaml` atomically (`yaml.safe_dump(effective_dict(cfg), sort_keys=True)`) when absent. 5. `changed_paths` = sorted dotted paths whose values differ between the old snapshot (if present) and the new, at most 200, then a final entry `+<n> more`. 6. Write the `config_change` line (`old_hash`, `new_hash=h`, `changed_paths`, `profile`) through the private `_audit_locked` (U10-60 body with `lock_held=True`). 7. Write `LAST` atomically; leave the lock. |
| Side effects | snapshot file, audit line, `LAST` |
| Errors | as U10-60 |
| Concurrency | the audit lock serialises concurrent starts, so one change produces one line |
| Complexity and limits | < 100 ms |
| Security notes | TH10-08. Snapshots hold references only. |
| Tests | UT10-21 |

#### U10-64 herness.core.audit.last_secret_set_times

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Last-set time per secret for `secrets status` (design 10 §3.7). |
| Signature | `last_secret_set_times(logs_dir: Path, names: Iterable[str]) -> dict[str, datetime \| None]` |
| Algorithm | Scan audit files newest first; for `admin_action` lines with `action` `secret_set` or `secret_rotate`, keep the newest `ts` per `target`; stop when all names are found or files end. |
| Side effects / Errors / Concurrency | reads / none / read-only |
| Complexity and limits | linear |
| Security notes | none |
| Tests | UT10-72 |

### 3.7 Operator commands, deployment, doctor and maintenance (`herness/admin/`)

Impl 09 owns the CLI command table, options and rendering (R-47); this section specifies behavior only. Every `cmd_*` function is called by the impl 09 Typer command of the same name (T09-24 (herness._cli.cmd_admin)) after T09-20 (herness._cli.identity.check_command_role) has checked the role of design 10 §3.7 (`admin`, `admin (OS)` or `any`) and raised `PermissionDenied` otherwise. "admin (OS)" also requires the process to run elevated (`ctypes.windll.shell32.IsUserAnAdmin()` on Windows, `os.geteuid() == 0` elsewhere), else `PermissionDenied("run from an elevated shell")`. Each `cmd_*` returns `T09-20 (herness._cli.output.CommandResult)` (`ok`, `data`, `warnings`, `exit_code`) and never prints; impl 09 renders it. Exit codes follow R-46 (corrected; design 09 §5.8 plus 14) and are mapped by T09-20 (herness._cli.output.exit_code_for): `0` success; `1` general failure, including a `doctor` FAIL, invalid config found by `config validate` (errors, or warnings with `--strict`) and `FatalError`; `2` usage error (Typer only); `3` `ConfigError` raised at load; `7` `NotFound`; `9` `ModelUnavailable`; `11` `PermissionDenied`; `13` `EgressBlocked`; the other codes of the R-46 table as mapped there. `actor` is the caller's `user_ref`, computed by T09-20 (herness._cli.identity.cli_actor) from the OS user.

#### U10-65 herness.admin.commands_config.cmd_config_validate

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | `herness config validate` (design 10 §3.7, §5.1). |
| Signature | `cmd_config_validate(*, config_dir: Path, profile: ProfileName \| None, offline: bool, strict: bool) -> CommandResult` |
| Algorithm | 1. Resolve the profile as U10-09 step 1. 2. `issues = validate(config_dir, profile, offline=offline)`. 3. `data = {"profile", "config_hash", "issues": [str(i) ...]}`; `config_hash` uses `key_id="unresolved"` when offline, and then a `warn` issue "config_hash computed without key_id; not comparable to builds" is appended. 4. Exit 1 on any `error`; else 1 when `strict` and any `warn`; else 0 (R-46: invalid config found by `config validate` is a general failure; 3 is kept for a `ConfigError` raised at load). |
| Side effects / Errors | as U10-13 / none raised for config problems |
| Concurrency | single-threaded |
| Complexity and limits | as U10-13 |
| Security notes | Role `any`; no secret values in output. |
| Tests | UT10-20, IT10-11 |

#### U10-66 herness.admin.commands_config.cmd_config_show

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | `herness config show` (admin). |
| Signature | `cmd_config_show(*, config_dir: Path, profile: ProfileName \| None) -> CommandResult` |
| Algorithm | Load with U10-09; `data = effective_dict(cfg, redact_secrets=True)`. |
| Side effects / Errors | reads config / `ConfigError` → exit 3 (T09-20 (herness._cli.output.exit_code_for)) |
| Concurrency / Complexity | single-threaded / n/a |
| Security notes | TH10-07. |
| Tests | ST10-16 |

#### U10-67 herness.admin.commands_config.cmd_config_hash

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | `herness config hash` (any). |
| Signature | `cmd_config_hash(*, config_dir: Path, profile: ProfileName \| None) -> CommandResult` |
| Algorithm | Load with U10-09; `data = {"config_hash": config_hash(cfg)}`. |
| Side effects / Errors | keyring read / `ConfigError` → exit 3 |
| Concurrency / Complexity | single-threaded / < 1 s |
| Security notes | none |
| Tests | UT10-15 |

#### U10-68 herness.admin.commands_secrets.cmd_secrets_init

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | `herness secrets init` (admin (OS)): create `redact.hmac_key` and `ui_user_ref_key` if absent, with escrow confirmation (design 10 §3.7, §5.3). |
| Signature | `cmd_secrets_init(*, actor: str, prompt: Callable[[str], str], show: Callable[[str], None]) -> CommandResult`; impl 09 supplies `prompt` (console input) and `show` (console write that bypasses logging). |
| Preconditions | Backend `keyring`. |
| Postconditions | Both keys exist and were escrow-confirmed, or exit 1 with nothing new stored for the unconfirmed name. |
| Algorithm | For each of `redact.hmac_key`, `ui_user_ref_key`: 1. `exists(name)` → report `present`, continue. 2. `value = secrets.token_hex(32)`. 3. `show` the name and value once with the instruction to store it in the corporate password vault. 4. `prompt("Type ESCROWED after storing <name> in the vault")`; any other answer → report `not created`, continue. 5. `set_secret(name, value, actor=actor)`. Exit 0 when both are present or created, else 1. |
| Side effects | keyring writes; audit lines |
| Errors | `ConfigError` from the backend |
| Concurrency | interactive |
| Complexity and limits | n/a |
| Security notes | TH10-07: the value is shown only through `show`, never logged or placed in `data`. |
| Tests | UT10-72 |

#### U10-69 herness.admin.commands_secrets.cmd_secrets_set

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | `herness secrets set NAME` (admin (OS)); value from a hidden prompt, never an argument. |
| Signature | `cmd_secrets_set(name: str, *, actor: str, prompt: Callable[[str], str]) -> CommandResult` |
| Algorithm | 1. Parse `name` (U10-27). 2. `v1 = prompt("Value for <name>")`, `v2 = prompt("Repeat")`; mismatch → exit 1 "values differ". 3. When `v1` starts with `{`, it must parse as a JSON object of strings, else exit 1. 4. `set_secret(name, v1, actor=actor)`. |
| Side effects / Errors | keyring write, audit / `ConfigError` → exit 3 |
| Concurrency | interactive |
| Complexity and limits | value ≤ 16 KiB |
| Security notes | TH10-07. |
| Tests | UT10-33 |

#### U10-70 herness.admin.commands_secrets.cmd_secrets_status

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | `herness secrets status` (admin). |
| Signature | `cmd_secrets_status(*, cfg: HernessConfig) -> CommandResult` |
| Algorithm | `names = referenced_secret_names(cfg)`; per name `present = exists(name)` and `last_set` from `last_secret_set_times`; `data = {"secrets": [{"name", "present", "last_set"}]}`; exit 1 when any is missing; a warning "rotate per policy" for `last_set` older than 90 days (design 10 §5.2). |
| Side effects / Errors | keyring and audit reads / `ConfigError` → exit 3 |
| Concurrency / Complexity | single-threaded / n/a |
| Security notes | Names only. |
| Tests | UT10-72 |

#### U10-71 herness.admin.commands_secrets.cmd_secrets_rekey

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | `herness secrets rekey` (admin (OS)): stage a new redaction key and plan the job (design 10 §5.3 step 1). |
| Signature | `cmd_secrets_rekey(*, actor: str, prompt: Callable[[str], str], show: Callable[[str], None]) -> CommandResult` |
| Preconditions | `redact.hmac_key.next` absent, else exit 1 "a rekey is already staged". |
| Algorithm | 1. `value = secrets.token_hex(32)`; show and require `ESCROWED` as U10-68, else exit 1 storing nothing. 2. `set_secret("redact.hmac_key.next", value, actor=actor)`. 3. `T08-14 (herness.core.jobs.schedule_rekey)()` → job ID and planned time. 4. `audit("admin_action", actor, action="redact_rekey", target="staged", detail=<planned ISO time>)`. 5. `data = {"job_id", "planned_at"}`. |
| Side effects | keyring write, job enqueue, audit |
| Errors | `ConfigError`, `StoreBusy` |
| Concurrency / Complexity | single-threaded / n/a |
| Security notes | Cancelling the job leaves the old key active (design 10 §5.3). |
| Tests | UT10-73 |

#### U10-72 herness.admin.commands_deploy.cmd_deploy_render, cmd_deploy_pull, cmd_deploy_up, cmd_deploy_down, cmd_deploy_rollback, cmd_deploy_prune, cmd_deploy_install

| Field | Content |
|-------|---------|
| Kind | function (seven thin wrappers) |
| Purpose | The `deploy` rows of design 10 §3.7 plus `deploy install` (ENG E4; command row owned by impl 09, R-47; install path R-58). |
| Signature | `cmd_deploy_render(*, cfg, actor)`; `cmd_deploy_pull(*, cfg, actor, allow_download: bool)`; `cmd_deploy_up(gpu_class: Literal["reasoning", "decider", "large"], *, cfg, actor)`; `cmd_deploy_down(gpu_class, *, cfg, actor)`; `cmd_deploy_rollback(gpu_class, *, cfg, actor)`; `cmd_deploy_prune(*, cfg, actor)`; `cmd_deploy_install(bundle_dir: Path, *, cfg, actor)`; all return `CommandResult`. Role `admin`; `pull` and `install` are admin (OS). |
| Algorithm | render → U10-79; pull → U10-92; up/down → U10-84; rollback, prune → U10-85; install → U10-86 `load`, U10-87, U10-88. Each maps the unit's return value to `data`. |
| Side effects / Errors | those of the called unit (impl 09 maps errors to exit codes) |
| Concurrency / Complexity | single CLI process / n/a |
| Security notes | TH10-01, TH10-31, TH10-36. |
| Tests | UT10-60–UT10-66, ST10-45 |

#### U10-73 herness.admin.commands_data.cmd_privacy_delete

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | `herness privacy delete --record-id ID ... --reason-ref REF` (admin): one `maintenance` job per record (design 10 §5.5). |
| Signature | `cmd_privacy_delete(record_ids: Sequence[str], *, reason_ref: str, actor: str) -> CommandResult` |
| Preconditions | 1–100 IDs, each matching `^[a-z][a-z0-9_]{0,31}:[a-z][a-z0-9_]{0,63}:[A-Za-z0-9._:@/+-]{1,200}$` and without `..`; `reason_ref` matches `^[A-Za-z0-9._:/-]{1,64}$`. Violations → `ConfigError("invalid record_id at position <n>")` (value not echoed). |
| Algorithm | Signature adds `inline: bool = False` (keyword-only; admin-only `--inline`, R-45). Per ID: `T08-12 (herness.core.jobs.enqueue)("maintenance", {"action": "privacy_delete", "record_id", "reason_ref", "requested_by": actor}, "none", priority=80, idem_key="privacy_delete:" + id)`. `data = {"jobs": [{"record_id", "job_id"}]}`. When `T08-12 (herness.core.jobs.worker_alive)()` is false (R-44), add the warning "no worker is running; start it with `herness worker` or rerun with `--inline`". With `inline`, each job runs in-process through `T08-22 (herness.core.jobs.run_inline)` after the maintenance handler is registered (U10-75), and `data` also carries each job's outcome. |
| Side effects / Errors | job rows / `ConfigError`, `StoreBusy` |
| Concurrency / Complexity | single-threaded / ≤ 100 IDs |
| Security notes | TH10-42: input validated before it reaches file and SQL paths. |
| Tests | ST10-31 |

#### U10-74 herness.admin.commands_data.cmd_maintenance

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | `herness maintenance backup\|purge [--dry-run]` (admin). |
| Signature | `cmd_maintenance(action: Literal["backup", "purge"], *, dry_run: bool, actor: str) -> CommandResult` |
| Algorithm | Signature adds `inline: bool = False` (keyword-only, R-45). `enqueue("maintenance", {"action", "dry_run", "requested_by": actor}, "none", priority=80, idem_key="maintenance:<action>:manual:<UTC date>")`; `data = {"job_id"}`. No live worker (R-44) → the same warning as U10-73. With `inline`, run the job through `T08-22 (herness.core.jobs.run_inline)`. |
| Side effects / Errors | job row / `StoreBusy` |
| Concurrency / Complexity | single-threaded / n/a |
| Security notes | none |
| Tests | UT10-68 |

#### U10-75 herness.admin.register_handlers

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Register the `maintenance` job handler with T08-12 (herness.core.jobs.register_handler). |
| Signature | `register_handlers(*, memory_purge: Callable[[str], int]) -> None`; `memory_purge(record_id)` is bound by the composition root to `T07-26 (herness.harness.memory.MemoryStore.purge)` with `record_id=` (R-54) and returns the number of purged memory items. |
| Algorithm | `T08-12 (herness.core.jobs.register_handler)("maintenance", functools.partial(handle_maintenance, memory_purge=memory_purge))` (U10-104); impl 08 calls the result with one argument, `ctx` (R-42). Called by `herness.cli` (composition root, R-04) when it starts the worker and before `run_inline`. |
| Side effects / Errors | handler registry entry / none |
| Concurrency | once per process at start |
| Complexity and limits | n/a |
| Security notes | none |
| Tests | UT10-68 |

#### U10-76 herness.admin.wsl.CmdResult, run_cmd

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass) and function |
| Purpose | The only subprocess runner in `herness/admin` (ENG §3.5, §2.5). |
| Signature | `CmdResult(returncode: int, stdout: str, stderr: str, duration_s: float)`; `run_cmd(argv: Sequence[str], *, timeout_s: float, stdin: bytes \| None = None, encoding: str = "utf-8", check: bool = False) -> CmdResult` |
| Preconditions | `argv[0]` in the allowlist `wsl.exe`, `powershell.exe`, `icacls.exe`, `manage-bde.exe`, `w32tm.exe`, `sc.exe`, `schtasks.exe`, `gh`, `uv`, `nvidia-smi`, `docker`, else `ConfigError("command not allowed: <argv0>")`; elements are `str` without NUL; `timeout_s` ≤ 7,200. |
| Postconditions | The process has exited or was killed. |
| Algorithm | 1. `shutil.which(argv[0])`; missing → `CmdResult(127, "", "not found", 0)`. 2. `subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout_s, shell=False)`. 3. Decode with `encoding`, replacing errors (`wsl.exe -l` output is UTF-16LE; callers pass `encoding="utf-16-le"`). 4. `TimeoutExpired` → kill; `CmdResult(124, ..., "timeout")`. 5. `check` and non-zero → `FatalError("<argv0> failed: exit <code>")`. 6. Log `admin.cmd.completed` at DEBUG with `argv0`, `returncode`, `duration_s` only. |
| Side effects | one subprocess |
| Errors | as above |
| Concurrency | thread-safe |
| Complexity and limits | output ≤ 16 MiB captured (truncated beyond) |
| Security notes | TH10-05 (no shell). |
| Tests | UT10-71, ST10-06 |

#### U10-77 herness.admin.wsl.run_wsl, run_powershell

| Field | Content |
|-------|---------|
| Kind | function (two) |
| Purpose | Run a Linux program in the distro without a shell; run a fixed PowerShell cmdlet. |
| Signature | `run_wsl(distro: str, argv: Sequence[str], *, user: Literal["default", "root"] = "default", timeout_s: float, stdin: bytes \| None = None) -> CmdResult`; `run_powershell(cmdlet: str, params: Mapping[str, str], *, timeout_s: float) -> CmdResult` |
| Preconditions | `distro` satisfies U10-07. Each `argv` element matches `^[A-Za-z0-9._:/@=+,%{}-]{1,512}$`, except elements that U10-79 and U10-83 mark as fixed script constants, else `ConfigError("unsafe argument for wsl")`. `cmdlet` in `Get-NetFirewallHyperVVMSetting`, `Set-NetFirewallHyperVVMSetting`, `Get-NetFirewallRule`; parameter names `^[A-Za-z]{1,40}$`, values `^[A-Za-z0-9{}\-:.* ]{1,100}$`. |
| Algorithm | `run_wsl`: `run_cmd(["wsl.exe", "-d", distro] + (["-u", "root"] if root) + ["--exec"] + argv, ...)`; `--exec` runs the program without the Linux default shell. `run_powershell`: `run_cmd(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", <cmdlet> + " -<name> '<value>'"... + (" \| ConvertTo-Json -Depth 3" for Get-*)])`; values are single-quoted after the allowlist check (the class excludes quotes, so no escaping is needed). |
| Side effects / Errors | subprocess / `ConfigError` for unsafe input, others as U10-76 |
| Concurrency | thread-safe |
| Complexity and limits | as U10-76 |
| Security notes | TH10-05. Impl 08's `compose_cmd` (T08-17 (herness.core.jobs.gpu_services.ComposeRunner)) uses `--` (shell); delta D10-09 asks for `--exec`. |
| Tests | UT10-71, ST10-06 |

#### U10-78 herness.admin.deploy.DeployHistory

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | Record promoted pinned sets per GPU class for rollback and prune (design 10 §5.6.5). |
| Signature | `__init__(path: Path)` (default `<paths.data>/deploy/history.jsonl`); `record(gpu_class: str, pins: dict[str, str], config_hash: str, now: datetime) -> None`; `last_promoted(gpu_class: str, n: int = 2) -> list[dict[str, str]]` |
| Algorithm | `record`: append `{"ts", "gpu_class", "pins", "config_hash"}` (canonical JSON) via U10-61 with lock `<paths.data>/deploy/.deploy.lock` when `pins` differs from the class's newest entry. `pins` = `image`, `model`, `revision` (reasoning, decider) or `image`, `gguf`, `sha256` (large). `last_promoted`: newest `n` distinct pin sets for the class. |
| Side effects / Errors | append / `StoreBusy` |
| Concurrency | cross-process lock |
| Complexity and limits | ≤ 10,000 lines |
| Security notes | TH10-32. |
| Tests | UT10-65 |

#### U10-79 herness.admin.deploy.render_env

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | `herness deploy render`: write `deploy.env_file` inside WSL (design 10 §7.4). |
| Signature | `render_env(cfg: HernessConfig, *, actor: str) -> dict[str, str]` (key → `"set"`; never values) |
| Preconditions | U10-07 deploy-time pin rules hold, else `ConfigError`. |
| Postconditions | `env_file` is `root:root`, mode `600`, holding exactly the keys below; `<paths.data>/deploy/rendered.json` = `{"config_hash", "env_sha256", "ts"}`. |
| Algorithm | 1. Keys: `REASONING_IMAGE`, `REASONING_PORT`, `REASONING_MODEL`, `REASONING_REVISION`, `REASONING_SERVED_NAME`, `REASONING_GPU_UTIL` (`gpu_memory_utilization`), `REASONING_MAX_MODEL_LEN`, `REASONING_TOOL_PARSER`, `REASONING_PARSER` from `deploy.reasoning`; `OPENJEV_IMAGE`, `OPENJEV_PORT`, `OPENJEV_MODEL`, `OPENJEV_GPU_UTIL`, `OPENJEV_MAX_NUM_SEQS`, `OPENJEV_CANVAS` from `deploy.openjev`; `LARGE_IMAGE`, `LARGE_PORT`, `LARGE_GGUF`, `LARGE_CTX`, `LARGE_GPU_LAYERS` from `deploy.large`; `MODEL_ROOT` = `deploy.model_root`; `VLLM_API_KEY` = `resolve("vllm.api_key")`; `OPENJEV_API_KEY` = `resolve("OPENJEV_API_KEY")`. 2. No value may contain `\n`, `\r` or NUL (`ConfigError("env value for <KEY> invalid")`). 3. Content = sorted `KEY=value` lines. 4. `run_wsl(distro, ["sh", "-c", SCRIPT, "sh", env_file], user="root", stdin=content, timeout_s=30)` where the constant `SCRIPT` is `umask 077; cat > "$1.tmp" && chown root:root "$1.tmp" && chmod 600 "$1.tmp" && mv "$1.tmp" "$1"`; no config value enters the script text (`env_file` is positional `$1`, validated by U10-07). This is one of two fixed shell scripts in this spec (the other is in U10-83). 5. Write `rendered.json` atomically with the content's SHA-256. 6. `audit("admin_action", actor, action="deploy_render", target=env_file)`. |
| Side effects | file in WSL, local JSON, audit |
| Errors | `ConfigError`; `FatalError` on non-zero exit |
| Concurrency | single CLI process |
| Complexity and limits | < 5 s |
| Security notes | TH10-28 (the only place secrets reach containers), TH10-05. |
| Tests | UT10-60, ST10-34 |

#### U10-80 docker/compose.yaml

| Field | Content |
|-------|---------|
| Kind | configuration artifact |
| Purpose | Compose services `vllm-reasoning`, `openjev`, `llamacpp-large` (design 10 §5.6.2). |
| Signature | Content is design 10 §5.6.2 verbatim, plus one addition: `llamacpp-large` gets `environment: *offline` so every service carries the three offline flags. Further hardening (`security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`, read-only model volumes) is delta D10-10, applied only after approval and Phase 4 verification. |
| Invariants | Checked by UT10-75, ST10-43, ST10-44: ports published only as `127.0.0.1:${VAR}:<port>`; no `privileged`, `network_mode: host`, `pid: host`, `cap_add`, or `/var/run/docker.sock` mount; `restart: "no"`; every service has `HF_HUB_OFFLINE`, `HF_HUB_DISABLE_TELEMETRY`, `DO_NOT_TRACK` = `"1"`; `vllm-reasoning` has `VLLM_NO_USAGE_STATS: "1"`; every `image` is `${<NAME>_IMAGE}`. |
| Side effects / Errors / Concurrency | n/a |
| Complexity and limits | n/a |
| Security notes | TH10-26, TH10-27, TH10-29. |
| Tests | UT10-75, ST10-43, ST10-44 |

#### U10-81 herness.admin.deploy_pull.firewall_window

| Field | Content |
|-------|---------|
| Kind | function (context manager) |
| Purpose | Open WSL outbound for the duration of a pull and always close it (design 10 §9.2). |
| Signature | `firewall_window(*, max_minutes: int = 120, actor: str) -> ContextManager[None]` |
| Preconditions | Windows host, elevated (else `PermissionDenied`). |
| Postconditions | On exit, `DefaultOutboundAction` of the WSL VM is `Block`, verified by reading it back. |
| Algorithm | Constant `WSL_VM_CREATOR_ID = "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}"` (verification V-20). 1. `run_powershell("Set-NetFirewallHyperVVMSetting", {"Name": WSL_VM_CREATOR_ID, "DefaultOutboundAction": "Allow"}, timeout_s=30)`; non-zero → `FatalError`. 2. Log `deploy.firewall.window_opened` (WARNING); start a `threading.Timer(max_minutes × 60)` that runs step 4 if the body is still running. 3. Yield. 4. `finally` (and timer): set `Block`; read back with `Get-NetFirewallHyperVVMSetting`; accept `DefaultOutboundAction` rendered as `Block` or its enum integer; retry the set once after 2 s; still not `Block` → log `deploy.firewall.restore_failed` (CRITICAL) and raise `FatalError("firewall window could not be closed; run the Block command manually")`. 5. Log `deploy.firewall.window_closed`. |
| Side effects | host firewall setting |
| Errors | `PermissionDenied`, `FatalError` |
| Concurrency | one window per host via `log_lock(<paths.data>/locks/deploy-pull.lock)` |
| Complexity and limits | ≤ 120 minutes open |
| Security notes | TH10-39, TH10-45. |
| Tests | UT10-63, ST10-47, ST10-51, FT10-06 |

#### U10-82 herness.admin.deploy_pull.pull_images

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Pull images by digest and verify them (ENG §5.6). |
| Signature | `pull_images(cfg: HernessConfig, *, guard: EgressGuard) -> list[dict[str, str]]` (`service`, `digest`, `status`) |
| Preconditions | Inside `firewall_window` and `guard.download_window`; pins valid. |
| Algorithm | For `reasoning`, `openjev`, `large`: 1. `ref = deploy.<c>.image`; registry host = first path segment when it contains `.` or `:`, else `registry-1.docker.io`. 2. `guard.check("https://<host>/v2/", b"", "model_download", "none")` records the download (hosts other than `MODEL_DOWNLOAD_HOSTS` are accepted for this purpose only when they are the registry of a pinned image; U10-51 step 3 reads the pinned registries from `deploy.*.image`). 3. `run_wsl(distro, ["docker", "pull", ref], timeout_s=3600)`; failure → `FatalError("image pull failed: <service>")`. 4. `run_wsl(distro, ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", ref], timeout_s=60)`; the pinned `sha256:<hex>` must be the digest suffix of one entry, else `FatalError("image digest mismatch: <service>")` and `deploy.pull.failed`. 5. Log `deploy.pull.image_verified`. |
| Side effects | images in WSL Docker; egress lines |
| Errors | `FatalError`; `EgressBlocked` outside a window |
| Concurrency | sequential |
| Complexity and limits | ≤ 1 h per image |
| Security notes | TH10-33. |
| Tests | UT10-62, ST10-19 |

#### U10-83 herness.admin.deploy_pull.pull_weights, verify_weights

| Field | Content |
|-------|---------|
| Kind | function (two) |
| Purpose | Download weights by pinned revision and verify every blob hash; verify the GGUF by SHA-256. |
| Signature | `pull_weights(cfg: HernessConfig, *, guard: EgressGuard) -> list[dict[str, str]]`; `verify_weights(cfg: HernessConfig, gpu_class: Literal["reasoning", "decider", "large"], *, full: bool) -> dict[str, str]` (`status` `ok` or a reason, `files`) |
| Preconditions | as U10-82 |
| Algorithm | `pull_weights`, for `(model, revision)` of `reasoning` and `openjev`: 1. `guard.check("https://huggingface.co/" + model + "/resolve/" + revision, b"", "model_download", "none")`. 2. `run_wsl(distro, ["docker", "run", "--rm", "--entrypoint", "huggingface-cli", "-e", "HF_HUB_OFFLINE=0", "-e", "HF_HUB_DISABLE_TELEMETRY=1", "-v", model_root + ":/root/.cache/huggingface", <reasoning image>, "download", model, "--revision", revision], timeout_s=7200)` (the pinned vLLM image ships the Hugging Face CLI; name is verification V-21). 3. OpenJev takes no revision variable, so write `refs/main` of its `models--<org>--<name>` directory to the pinned revision with `run_wsl(["sh", "-c", "printf %s \"$1\" > \"$2\"", "sh", revision, <refs path>], user="root")` (fixed script, positional arguments) so offline resolution picks the pinned snapshot (verification V-15). 4. `verify_weights(..., full=True)`. `verify_weights`: 1. `<model_root>/hub/models--<org>--<name>/snapshots/<revision>` exists (`run_wsl(["test", "-d", dir])`). 2. List links with `run_wsl(["find", dir, "-type", "l", "-printf", "%p %l\n"])`. 3. Blobs whose file name is 64 hex characters (LFS content hash): when `full`, `run_wsl(["sha256sum", <≤ 100 blob paths>])` per batch must equal the name, else status `hash_mismatch:<file>`; when not `full`, each blob exists with non-zero size. 4. `large`: `<model_root>/gguf/<gguf>` exists; when `full`, its `sha256sum` equals `deploy.large.sha256`. The GGUF is placed by the operator (no download source is configured; open item O-7). 5. Write manifest `<paths.data>/deploy/weights-<class>-<revision or sha256[:12]>.json` (`{path: size}`) atomically. |
| Side effects | files in WSL, egress lines, manifest |
| Errors | `FatalError` on download failure or mismatch; the pull aborts and nothing is rendered or promoted |
| Concurrency | sequential |
| Complexity and limits | hashing ≈ 60 s per 20 GB |
| Security notes | TH10-30, TH10-34, TH10-35. LLM03, LLM04. |
| Tests | UT10-62, ST10-20, ST10-21, FT10-09 |

#### U10-84 herness.admin.deploy.class_up, class_down

| Field | Content |
|-------|---------|
| Kind | function (two) |
| Purpose | `herness deploy up\|down <class>` (design 10 §3.7). |
| Signature | `class_up(cfg: HernessConfig, gpu_class: Literal["reasoning", "decider", "large"], *, actor: str, now: datetime) -> dict[str, str]`; `class_down(cfg, gpu_class, *, actor: str) -> dict[str, str]` |
| Preconditions | `rendered.json.config_hash == config_hash(cfg)`, else `ConfigError("run herness deploy render first")`; pins valid. |
| Algorithm | `class_up`: 1. `audit("admin_action", actor, action="deploy_up", target=<class>:<pinned digest or gguf sha>)`. 2. When `T08-12 (herness.core.jobs.worker_alive)()` (heartbeat within 3 × `heartbeat_s`, R-44): set `worker.requested_class = <class>` through T08-18 (herness.core.jobs.request_gpu_class); log `deploy.class.requested` with `via="worker"`; poll `T08-18 (herness.core.jobs.gpu_state)().loaded_class()` every 5 s until it equals the class or `start_timeout_s + 300` s pass (timeout → `ModelUnavailable("worker did not load <class>")`). Containers are never started directly. 3. Otherwise: take the T08-17 (herness.core.jobs.gpu_lock.GpuLock) lock `data/locks/gpu.lock` without waiting (held → `StoreBusy("GPU lock held")`); stop services of the other classes with `resilience.gpu.compose_cmd + ["-f", compose_file, "--profile", <other>, "stop"]`; start `compose_cmd + ["-f", compose_file, "--profile", <class>, "up", "-d"] + <all services of the class>` (including `openjev`); poll each service's health URL from `resilience.resilience.gpu.classes.<class>.services` with `loopback_http_client(<service url>, timeout_s=10.0, bearer=...)` (bearer `resolve("secret:OPENJEV_API_KEY")` for OpenJev, R-53) every 5 s until healthy or `start_timeout_s` (→ `ModelUnavailable`); release the lock. 4. When healthy, `DeployHistory.record(...)`. `class_down`: audit `action="deploy_down"`; with a worker, request class `none`; else under the GPU lock run `compose_cmd + ["-f", compose_file, "--profile", <class>, "stop"]`. |
| Side effects | containers, audit, history |
| Errors | `ConfigError`, `StoreBusy`, `ModelUnavailable` |
| Concurrency | GPU lock or worker arbiter |
| Complexity and limits | ≤ `start_timeout_s` (900–1,200 s) |
| Security notes | TH10-31, TH10-32. |
| Tests | UT10-64, ST10-45 |

#### U10-85 herness.admin.deploy.rollback_class, prune

| Field | Content |
|-------|---------|
| Kind | function (two) |
| Purpose | Rollback after a config revert; prune old digests and revisions (design 10 §5.6.5). |
| Signature | `rollback_class(cfg, gpu_class, *, actor: str, now: datetime) -> dict[str, str]`; `prune(cfg, *, actor: str) -> dict[str, int]` |
| Algorithm | `rollback_class`: 1. `verify_weights(cfg, class, full=False)` and `docker image inspect` of the pinned image must succeed without a download, else `ConfigError("rollback target not on disk; run deploy pull")`. 2. `render_env`. 3. `class_down`, then `class_up`. 4. Audit `action="deploy_rollback"`. `prune`: 1. Keep set per class = two newest `last_promoted` pin sets plus the current pins. 2. For each repository of a kept image, list `docker image ls --digests --format "{{json .}}" <repo>` and `docker image rm <repo>@<digest>` every digest not kept. 3. For each model directory, remove snapshot directories whose revision is not kept (`run_wsl(["rm", "-rf", "--", <dir>], user="root")`, `<dir>` checked to be under `model_root`), then blobs no remaining snapshot links to. 4. Remove `*.gguf` under `<model_root>/gguf` not kept. 5. Audit `action="deploy_prune"` with counts. Prune refuses (`StoreBusy`) while `gpu_state().loaded_class()` uses an item it would remove. |
| Side effects | removes images and files in WSL |
| Errors | `ConfigError`, `StoreBusy`, `FatalError` |
| Concurrency | single CLI process |
| Complexity and limits | n/a |
| Security notes | TH10-46 (rollback needs no download). |
| Tests | UT10-65, ST10-52 |

#### U10-86 herness.admin.attest.ReleaseBundle

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass) with loader |
| Purpose | A release bundle directory produced by release CI (ENG §5.6, R-58). |
| Signature | Fields: `root: Path`; `wheel: Path` (`herness-<version>-py3-none-any.whl`); `version: str`; `requirements: Path` (`requirements.txt`, from `uv export --frozen --no-emit-project` with hashes); `sbom: Path` (`sbom.cdx.json`); `trusted_root: Path` (`trusted_root.jsonl`); `provenance_bundles: dict[str, Path]` (`<file>.provenance.sigstore.json` for the wheel and `requirements.txt`); `sbom_bundle: Path` (`<wheel>.sbom.sigstore.json`); `duckdb_extensions: dict[str, Path]` (`duckdb/<name>.duckdb_extension` for the DuckDB version pinned in `requirements.txt` and platform `windows_amd64`, currently only `excel`; each with its provenance bundle in `provenance_bundles`). Class method `load(root: Path) -> ReleaseBundle`. |
| Preconditions | `root` is a directory. |
| Postconditions | All paths resolve inside `root`, none is a symlink, each file exists and is ≤ 200 MiB. |
| Algorithm | 1. List `root` (non-recursive); exactly one wheel matching `^herness-(\d+\.\d+\.\d+[a-z0-9.+-]*)-py3-none-any\.whl$`. Files under `duckdb/` must match `^[a-z0-9_]{1,40}\.duckdb_extension$`. 2. Per expected file: `p.resolve()` is under `root.resolve()`, `p.is_symlink()` false, size ≤ 200 MiB. 3. A missing file or any extra non-hidden file → `ConfigError("release bundle invalid: <file>")`. |
| Side effects / Errors | directory reads / `ConfigError` |
| Concurrency | pure apart from reads |
| Complexity and limits | n/a |
| Security notes | TH10-40. |
| Tests | ST10-48 |

#### U10-87 herness.admin.attest.verify_bundle

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Verify SLSA Build L2 provenance of the wheel and `requirements.txt`, and the attested SBOM, before any installation (ENG E4). |
| Signature | `verify_bundle(bundle: ReleaseBundle, cfg: HernessConfig) -> dict[str, Any]` (`version`, `wheel_sha256`, `components`, `licences_checked`) |
| Preconditions | `deploy.release.repo` and `deploy.release.signer_workflow` set, else `ConfigError("deploy.release is not configured")`; `gh` on `PATH` (§14). |
| Postconditions | Returns only when every check passed; otherwise raises and nothing is installed. |
| Algorithm | 1. Provenance, for `f` in (wheel, requirements, each DuckDB extension file): `run_cmd(["gh", "attestation", "verify", f, "--bundle", <f provenance bundle>, "--repo", repo, "--signer-workflow", signer_workflow, "--deny-self-hosted-runners", "--custom-trusted-root", <trusted_root>, "--predicate-type", "https://slsa.dev/provenance/v1", "--format", "json"], timeout_s=120)`; non-zero → `FatalError("attestation verification failed: <file>")`, log `deploy.install.refused`. These flags verify offline against the bundled trusted root (verification V-22). 2. SBOM: the same command for the wheel with `--bundle <sbom bundle>` and `--predicate-type https://cyclonedx.org/bom`; take the verified predicate from the JSON output as the SBOM; the loose `sbom.cdx.json` must equal it after canonical JSON serialisation, else refuse. 3. Components: parse `requirements.txt` into `{name: version}` (normalised names, `==` pins); every SBOM component of type `library` has the same version as in requirements and every requirement appears in the SBOM; else refuse. 4. Licences: every component's licence (`licenses[].license.id` or `.expression`) is one of `MIT`, `BSD-2-Clause`, `BSD-3-Clause`, `Apache-2.0`, `PSF-2.0`, `Python-2.0`, `ISC`, `MPL-2.0` (ENG §5.6), or the component is in `deploy.release.licence_exceptions`; else refuse naming the components. 5. Log `deploy.install.verified`; return the summary with the wheel SHA-256. |
| Side effects | subprocesses |
| Errors | `ConfigError`, `FatalError` |
| Concurrency | single-threaded |
| Complexity and limits | < 30 s |
| Security notes | TH10-36, TH10-37. LLM03. |
| Tests | UT10-66, ST10-22, ST10-23, ST10-24, IT10-14 |

#### U10-88 herness.admin.attest.install_bundle

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Install a verified release into the Herness virtual environment. This is the only install path on the target box; development uses an editable install (R-58). |
| Signature | `install_bundle(bundle: ReleaseBundle, summary: dict[str, Any], *, venv: Path, actor: str) -> dict[str, str]` |
| Preconditions | The wheel's SHA-256 recomputed now equals `summary["wheel_sha256"]` (else `FatalError("bundle changed after verification")`). `worker_alive()` false (else `StoreBusy("stop the worker before installing")`). |
| Algorithm | 1. Copy the bundle files to `<repo root>/.release/<version>/` (atomic per file) and re-hash the copied wheel against the summary; keep the two newest version directories, delete older ones. 2. `audit("admin_action", actor, action="deploy_install", target=<version>, detail="sha256:" + wheel_sha256)`. 3. `run_cmd(["uv", "pip", "sync", "--require-hashes", "--python", <venv python>, <copied requirements.txt>], timeout_s=1800, check=True)`. 4. `run_cmd(["uv", "pip", "install", "--no-deps", "--python", <venv python>, <copied wheel>], timeout_s=600, check=True)`. 4a. U10-112 installs and verifies the DuckDB extensions from the copied bundle. 5. Return `{"version", "wheel_sha256", "duckdb_extensions"}`. |
| Side effects | venv changes, `.release/` files, audit |
| Errors | `FatalError`, `StoreBusy` |
| Concurrency | single CLI process |
| Complexity and limits | ≤ 40 min |
| Security notes | TH10-36, TH10-44. |
| Tests | IT10-14, ST10-53 |

#### U10-112 herness.admin.duckdb_ext.install_duckdb_extensions

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Install and verify the DuckDB `excel` extension offline as part of `herness deploy install` (impl 01 O-3: the files connector reads `.xlsx` with `autoinstall_known_extensions` off, so the extension must already be installed). |
| Signature | `install_duckdb_extensions(bundle: ReleaseBundle, cfg: HernessConfig, *, venv: Path) -> dict[str, str]` (extension name → installed version). Module constant `EXT_INSTALL_SCRIPT: str` (the fixed Python source run in the venv interpreter). |
| Preconditions | U10-87 verified the bundle, including the extension's provenance; U10-88 installed the wheel, so the venv holds the pinned `duckdb`. `cfg.deploy.release.duckdb_extensions["excel"]` is set, else `ConfigError("deploy.release.duckdb_extensions.excel is not pinned")`. |
| Postconditions | `excel` is installed in the DuckDB extension directory of the Herness service account and loads in a fresh connection with autoinstall and autoload off; no network was used. |
| Algorithm | For each `name` in `cfg.deploy.release.duckdb_extensions` (sorted): 1. `path = bundle.duckdb_extensions[name]` (missing → `ConfigError("release bundle invalid: duckdb/<name>.duckdb_extension")`). 2. SHA-256 of `path` must equal the pin, else `FatalError("duckdb extension hash mismatch: <name>")` and log `deploy.install.refused` with `reason="extension_hash"`. 3. `run_cmd([<venv python>, "-I", "-c", EXT_INSTALL_SCRIPT, str(path), name], timeout_s=120, env={"HERNESS_ENV": <current>, "NO_PROXY": "*"})`. The script opens `duckdb.connect(":memory:", config={"autoinstall_known_extensions": False, "autoload_known_extensions": False, "allow_unsigned_extensions": False})`, runs `INSTALL '<path>'` (a local file, so DuckDB performs no download, and the signature check of official extensions stays on), then `LOAD <name>`, then prints as JSON the row of `duckdb_extensions()` for `name` (`loaded`, `installed`, `extension_version`, `install_path`); non-zero exit → `FatalError("duckdb extension install failed: <name>")`. 4. Re-hash the file at `install_path`; it must equal the pin, else `FatalError`. 5. Log `deploy.install.extension_installed` with `name`, `extension_version`, `sha256`; collect the version. The `deploy_install` audit line of U10-88 gains `detail` entries `ext:<name>=<sha256>`. |
| Side effects | writes the DuckDB extension directory; subprocess; logs |
| Errors | `ConfigError`, `FatalError` |
| Concurrency | single CLI process (inside `deploy install`) |
| Complexity and limits | < 30 s; extension file ≤ 200 MiB (U10-86) |
| Security notes | TH10-52. No network: the extension comes only from the verified bundle, its hash is pinned in file-only config, and unsigned extensions stay refused. |
| Tests | UT10-85, ST10-60 |

#### U10-89 herness.admin.doctor_host.CheckResult, doctor_checks

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass) and function |
| Purpose | The checks design 10 §5.6.3 adds to T09-22 (herness._cli.doctor.run_doctor). |
| Signature | `CheckResult(name: str, status: Literal["PASS", "WARN", "FAIL"], detail: str, fix: str)` (impl 09 JSON `{"name", "status", "detail", "fix"}`); `doctor_checks(cfg: HernessConfig) -> list[CheckResult]` |
| Algorithm | 1. Run every callable of `HOST_CHECKS + GPU_CHECKS` in a `ThreadPoolExecutor(max_workers=8)`; each has signature `(cfg) -> CheckResult` and its own timeouts. 2. A check raising a `HernessError` yields `FAIL` with `detail` = error class name. 3. On a non-Windows host, Windows-only checks return `WARN` "not applicable on this host". 4. Return results in table order. |
| Side effects | per check |
| Errors | none raised |
| Concurrency | thread pool of independent checks |
| Complexity and limits | < 60 s with models running (BT10-07) |
| Security notes | Details never contain secret values or file contents. |
| Tests | UT10-71, IT10-12, BT10-07 |

#### U10-90 herness.admin.doctor_host.HOST_CHECKS

| Field | Content |
|-------|---------|
| Kind | constant (ordered tuple of check callables) |
| Purpose | Host-side checks (design 10 §5.6.3, §5.5). |
| Signature | One private function `_check_<name>(cfg) -> CheckResult` per row below. |
| Algorithm | Table below; subprocess timeouts 10 s. |
| Side effects / Errors / Concurrency | subprocesses and reads / none raised / independent |
| Complexity and limits | each ≤ 10 s |
| Security notes | TH10-13, TH10-25, TH10-26, TH10-38, TH10-41, TH10-43. |
| Tests | UT10-71, ST10-28, ST10-38, ST10-42, ST10-49 |

| Name | PASS condition | FAIL / WARN | Fix hint |
|------|----------------|-------------|----------|
| `python_uv` | `sys.version_info[:2] == (3, 12)` and `shutil.which("uv")` | FAIL | Install Python 3.12 and uv |
| `config_valid` | `validate(config_dir, cfg.profile)` has no `error` | FAIL on errors; WARN on warnings | `herness config validate` |
| `secrets_present` | every `referenced_secret_names` entry exists | FAIL listing missing names | `herness secrets set <name>` |
| `acl_data` | `icacls.exe <paths.data>` lists `BUILTIN\Administrators:(OI)(CI)(F)` and `<machine or domain>\svc-herness:(OI)(CI)(M)`, no line containing `BUILTIN\Users`, `Everyone` or `Authenticated Users`, and no inherited ACE `(I)`; the same for `config\profiles`, `security.redaction.directory_file` and `<repo root>\.env` when present | FAIL | The `icacls` command of design 10 §5.5 |
| `bitlocker` | `manage-bde.exe -status <drive>` reports `Protection On` for the system drive, the drive of `paths.data` and the drive of `paths.backup_target` | FAIL on `Protection Off`; WARN on access denied | Enable BitLocker |
| `audit_chain` | `verify_chain(paths.logs).ok` | FAIL with `first_break` | Investigate; restore from backup |
| `disk_free` | `shutil.disk_usage(paths.data).free ≥ 50 GiB` | FAIL | Free space |
| `clock` | `w32tm.exe /query /status` "Phase Offset" absolute value < 5 s, and `abs(run_wsl(["date", "+%s.%N"]) − host time) < 5 s` | FAIL above 5 s; WARN when unavailable | `w32tm /resync`; `wsl --shutdown` |
| `ports_loopback` | for 8000, 8100, 8200 (R-51) and `security.ui.port`, every `psutil.net_connections(kind="tcp")` entry in `LISTEN` has a loopback `laddr.ip` | FAIL listing port and address | Fix the bind or compose ports |
| `ports_expected` | when no class is loaded (`gpu_state().loaded_class() == "none"` or no worker and no healthy service), nothing listens on 8000, 8100, 8200 | WARN with the owning process name (`psutil.Process(pid).name()`) | Stop the unexpected listener |
| `socket_guard` | `socket.create_connection(("1.1.1.1", 443), timeout=2)` raises `EgressBlocked` | FAIL if it connects or raises anything else | Defect: the guard is not installed |
| `plugins` | no `herness.plugins` entry point was loaded | WARN listing distribution and version | Remove unapproved plugins |
| `services` | manager `nssm`: `sc.exe query herness-worker` finds the service; `task_scheduler`: `schtasks.exe /Query /TN herness-worker`; and `schtasks.exe /Query /TN herness-wsl` exists | WARN | Register services (design 10 §5.6.4) |

#### U10-91 herness.admin.doctor_gpu.GPU_CHECKS

| Field | Content |
|-------|---------|
| Kind | constant (ordered tuple of check callables) |
| Purpose | WSL, Docker, GPU, image, weight, health and firewall checks (design 10 §5.6.3, §9.2). |
| Signature | as U10-90 |
| Algorithm | Table below; WSL calls through U10-77; timeouts 10–20 s. |
| Side effects / Errors / Concurrency | subprocesses / none raised / independent |
| Complexity and limits | each ≤ 20 s |
| Security notes | TH10-26, TH10-29, TH10-30, TH10-33, TH10-39. |
| Tests | UT10-71, ST10-44 |

| Name | PASS condition | FAIL / WARN | Fix hint |
|------|----------------|-------------|----------|
| `wsl_distro` | `run_cmd(["wsl.exe", "-l", "-q"], encoding="utf-16-le")` lists `deploy.wsl_distro` | FAIL | Runbook step 1 |
| `wsl_systemd_docker` | `run_wsl(["systemctl", "is-active", "docker"])` prints `active` | FAIL | Enable systemd; start docker |
| `docker_api_local` | `run_wsl(["ss", "-ltn"])` shows no listener on 2375 or 2376 | FAIL | Remove the TCP host from the Docker daemon config |
| `gpu_visible` | `run_wsl(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])` succeeds | FAIL | Install the Windows driver with WSL support |
| `gpu_vram` | reported `memory.total` ≥ 24,000 MiB | FAIL | A 24 GB+ GPU is required |
| `images_pinned` | per class, `docker image inspect` of the pinned ref succeeds and `RepoDigests` contains the pinned digest | FAIL per class | `herness deploy pull --allow-download` |
| `weights_pinned` | `verify_weights(cfg, class, full=False)` is `ok` per class | FAIL per class | `herness deploy pull --allow-download` |
| `model_health` | each service of the loaded class answers 2xx on its health URL through `loopback_http_client(<service url>, timeout_s=10.0, bearer=...)` (OpenJev bearer `secret:OPENJEV_API_KEY`, R-53) | FAIL; WARN when no class is loaded | `herness deploy up <class>` |
| `firewall_outbound` | `Get-NetFirewallHyperVVMSetting -Name <WSL_VM_CREATOR_ID>` reports `DefaultOutboundAction` `Block` | FAIL | The Block command of design 10 §9.2 |
| `firewall_inbound` | `Get-NetFirewallRule -DisplayName 'herness-block-inbound-*'` returns enabled rules covering 8000, 8100, 8200 and `security.ui.port` | WARN | Add the inbound block rules (runbook) |
| `env_file` | `run_wsl(["stat", "-c", "%a %U", env_file], user="root")` prints `600 root` | FAIL | `herness deploy render` |

#### U10-92 herness.admin.deploy_pull.run_pull

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | `herness deploy pull --allow-download` orchestration (design 10 §3.7, §5.6.3 step 5). |
| Signature | `run_pull(cfg: HernessConfig, *, allow_download: bool, actor: str) -> dict[str, Any]` |
| Preconditions | `allow_download` (else `ConfigError("deploy pull requires --allow-download")`); pins valid; no worker alive (else `StoreBusy("stop the worker before pulling")`). |
| Algorithm | 1. Stop all compose services (`compose_cmd + ["-f", compose_file, "--profile", "reasoning", "--profile", "decider", "--profile", "large", "stop"]`) so no model server runs while outbound is open. 2. Inside `guard.download_window(allow_download=True, actor=actor)` and `firewall_window(actor=actor)`: U10-82, then U10-83. 3. After the window closes, `verify_weights(full=False)` per class again. 4. Audit `action="deploy_pull"`, `target="complete"`. 5. Return statuses. A failure propagates after the window is closed; nothing is rendered or promoted. |
| Side effects | as U10-81–U10-83 |
| Errors | as the called units |
| Concurrency | pull lock `<paths.data>/locks/deploy-pull.lock` |
| Complexity and limits | window ≤ 120 min |
| Security notes | TH10-23, TH10-45. |
| Tests | UT10-63, ST10-51 |

#### U10-93 config/herness.yaml

| Field | Content |
|-------|---------|
| Kind | configuration artifact |
| Purpose | Root config template (design 10 §4.2, §7.2–§7.4). |
| Signature | `version: 1`; `paths`; `security` (`data_policy`, `secrets`, `redaction` per §7.2 plus `denylist_domains: []`, `egress` defaults, `network`, `ui` per §7.3); `logging`; `retention`; `backup`; `deploy` per §7.4 with its placeholders plus `release: {repo: null, signer_workflow: null, licence_exceptions: []}`. Every value equals the design default. |
| Invariants | Loads without errors for `local` and `synth` (placeholders give C13 warnings only). |
| Algorithm / Side effects / Errors / Concurrency | n/a |
| Complexity and limits | n/a |
| Security notes | Secure defaults (ENG §5.7): egress off, loopback bind, `default_role: viewer`, `mask_ip: true`. |
| Tests | UT10-76, IT10-01 |

#### U10-94 config/profiles/{local,hybrid,premium,synth}.yaml

| Field | Content |
|-------|---------|
| Kind | configuration artifact |
| Purpose | Profile overlays (design 10 §4.3). |
| Signature | `local.yaml`: `version: 1` only. `hybrid.yaml`: `security.egress: {enabled: true, destinations: [api.anthropic.com], purposes: [reasoning_final]}` plus the impl 05 `models` role and fallback overlay. `premium.yaml`: `security.egress: {enabled: true, destinations: [api.anthropic.com, api.typesafe.ai], purposes: [reasoning, reasoning_final, bulk_classification]}` plus the impl 05 overlay and impl 06 swarm caps. `synth.yaml`: `security.secrets.backend: dotenv`, `security.redaction.denylist_domains` (corporate domains supplied at Phase 1, open item O-8), plus impl 11's `mappings` and `sources` content. |
| Invariants | No overlay sets `security.data_policy`; `synth.yaml` never enables egress. |
| Algorithm / Side effects / Errors / Concurrency | n/a |
| Complexity and limits | n/a |
| Security notes | TH10-03. |
| Tests | UT10-76 |

#### U10-95 .streamlit/config.toml, .env.example

| Field | Content |
|-------|---------|
| Kind | configuration artifact |
| Purpose | Streamlit hardening (design 10 §9.2) and the dev env template. |
| Signature | `.streamlit/config.toml`: `server.address = "127.0.0.1"`, `server.headless = true`, `server.enableXsrfProtection = true`, `browser.gatherUsageStats = false`. `.env.example`: `HERNESS_ENV=dev`, `HERNESS_PROFILE=synth`, and `HERNESS_SECRET__<NAME>=` with an empty value for each named secret of design 10 §3.3 (`OPENJEV_API_KEY`, `TYPESAFE_API_KEY`, `VLLM_API_KEY`, `ANTHROPIC_API_KEY`, `REDACT_HMAC_KEY`, `UI_USER_REF_KEY`). |
| Invariants | `.env` is in `.gitignore`; `.env.example` holds no values. |
| Algorithm / Side effects / Errors / Concurrency | n/a |
| Complexity and limits | n/a |
| Security notes | TH10-12, TH10-06. |
| Tests | UT10-76 |

#### U10-96 herness.admin.maintenance.run_backup

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Nightly backup (design 10 §5.5 "Backup"). |
| Signature | `run_backup(cfg: HernessConfig, ctx: JobContext, *, now: datetime, dry_run: bool = False) -> dict[str, int]` |
| Preconditions | `paths.backup_target` is an existing directory, else `FatalError("backup target missing")`. |
| Postconditions | Today's ops copy passed `integrity_check`, or the job failed leaving no partial copy. |
| Algorithm | 1. Ops: source `sqlite3.connect("file:<data>/ops.sqlite?mode=ro", uri=True)`; destination `<target>/ops/.ops-<YYYY-MM-DD>.sqlite.tmp`; `src.backup(dst, pages=4096, sleep=0.05)` (online, consistent under WAL); `PRAGMA integrity_check` on the copy must return the single row `ok`, else delete the temp file and raise `FatalError("backup integrity_check failed")`; `os.replace` to `ops-<date>.sqlite`. 2. Mirror copies (copy when absent at the target or size/mtime differ; never delete at the target; temp then `os.replace`): `data/config_snapshots/` → `<target>/config_snapshots/`; `data/models/` → `<target>/models/`; `data/reports/` → `<target>/reports/`; `data/logs/audit-*.jsonl`, `egress-*.jsonl` → `<target>/logs/`; `data/cache/decisions/` when `backup.include_cache`; `data/raw/` when `backup.include_lake`. `ctx.heartbeat()` every 100 files. 3. Delete `ops-*.sqlite` copies not selected by U10-97. 4. `audit("admin_action", "system", action="backup", target=<target>, counts=...)`. 5. With `dry_run`, count only. |
| Side effects | files at the target; audit |
| Errors | `FatalError`; `OSError` → `FatalError("backup copy failed: <relative path>")` |
| Concurrency | exclusive job kind (impl 08); ops writers continue during the copy |
| Complexity and limits | 1 GB ops store < 2 min (BT10-08) |
| Security notes | TH10-43 (target drive BitLocker-checked by doctor). Secrets are not backed up. |
| Tests | UT10-67, IT10-08, FT10-04, BT10-08 |

#### U10-97 herness.admin.maintenance.select_backup_keep

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Choose ops copies to keep (14 daily, 8 weekly). |
| Signature | `select_backup_keep(dates: Sequence[date], *, keep_daily: int, keep_weekly: int, today: date) -> set[date]` |
| Algorithm | 1. The `keep_daily` newest dates ≤ `today`. 2. Group by ISO year-week; for the `keep_weekly` newest weeks keep the newest date of each. 3. Return the union. |
| Side effects / Errors / Concurrency | none / none / pure |
| Complexity and limits | O(n log n) |
| Security notes | none |
| Tests | UT10-67 |

#### U10-98 herness.admin.maintenance.run_purge

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Nightly retention purge (design 10 §5.5 "Retention"). |
| Signature | `run_purge(cfg: HernessConfig, ctx: JobContext, *, now: datetime, dry_run: bool = False) -> dict[str, int]` |
| Algorithm | 1. Lake (one of the three permitted lake rewrites, R-57): `cutoff` = `now` minus `raw_lake_months` calendar months (year/month arithmetic, day clamped); call `T02-03 (herness.store.lake_purge.purge_partitions_before)(cutoff, today=now.date())`, which deletes whole `dt=YYYY-MM-DD` partitions older than `cutoff`; its `deferred_locked` count is reported as `skipped_in_use`; with `dry_run` the partitions are counted without the call. 2. Traces: delete `data/traces/*.jsonl` with mtime older than `traces_days`. 3. `egress-<date>.jsonl`, `audit-<date>.jsonl`, `herness-<date>.jsonl`: delete when the name date is older than the respective days value; never today's file. 4. Reports: delete `data/reports/<run_id>/` and `data/reports/eval/<run_id>/` whose `manifest.json` mtime (directory mtime when absent) is older than `reports_days`. 5. Chat: `T09-03 (herness.store.ops.chat.purge_chat)(before=now − chat_days)` (area `chat`, R-08). 6. Rekey cleanup: when `data/cache/rekey/SWAPPED` exists and the `CURRENT` build (T02-09 (herness.store.warehouse.read_current)) is promoted with `started_at` after the marker's `swapped_at`, delete decision-cache files under `data/cache/decisions/` with mtime before `swapped_at`, then the marker (design 10 §5.3 step 4). 7. Every deleted path must resolve inside `paths.data` and not be a symlink. 8. `audit("admin_action", "system", action="purge", target="retention", counts=...)`. 9. `dry_run` counts only. `ctx.heartbeat()` every 100 deletions. |
| Side effects | deletions; audit |
| Errors | `PermissionError` (file in use) → skipped, counted `skipped_in_use`, retried next night |
| Concurrency | exclusive job kind |
| Complexity and limits | linear in files |
| Security notes | ASVS v5.0.0-V14.2 retention; containment (ENG §5.7); lake deletion only through the impl 02 primitive (R-57). |
| Tests | UT10-68, IT10-09 |

#### U10-99 herness.admin.privacy.run_privacy_delete

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | The privacy deletion of design 10 §5.5 "Deletion requests": the seven design steps plus step 3b, the memory purge (R-54). |
| Signature | `run_privacy_delete(cfg: HernessConfig, ctx: JobContext, *, now: datetime, memory_purge: Callable[[str], int]) -> JobOutcome`; the payload is read from `ctx.job.payload` (R-42). |
| Preconditions | The payload carries `record_id`, `reason_ref`, `requested_by` validated as U10-73 (re-validated here); optional `request_id` on continuation jobs. |
| Postconditions | Request `done` after step 7, or `running` with the failed step recorded. |
| Algorithm | Request rows go through `herness.store.ops.privacy` (U10-105, R-08). Step identifiers are the strings `1`, `2`, `3`, `3b`, `4`, `5`, `6`, `7`; each result is recorded with `record_deletion_step`, and a step already recorded `done` is skipped on retry. 1. `open_deletion_request(record_id)` returns the request in `pending`/`running`, else `create_deletion_request(...)` inserts `request_id = "del_" + new_ulid()` as `pending`; `set_deletion_status(request_id, "running")`. From here T01-05 (herness.connectors.deletion.DeletionFilter) and impl 02 staging drop the record (deletion set of `running`/`done` requests). 2. Lake pass 1: `T02-03 (herness.store.lake_purge.purge_record_ids)([record_id])` (the permitted lake rewrite, R-57); store its counts. 3. `T03-34 (herness.enrich.purge_record)(record_id)`; store its counts. 3b. Memory (R-54): `memory_purge(record_id)`, bound to `T07-26 (herness.harness.memory.MemoryStore.purge)` with `record_id=` (U10-75); it removes memory items, memory vectors and FTS rows that cite the record; store the count. `ModelUnavailable` from a vector failure marks the step `failed` and is re-raised so impl 08 retries. 4. `T05-12 (herness.store.ops.evidence.scrub_record_from_evidence)(record_id)` (impl 05 owns the `evidence` area, R-08, R-09) and `T06-06 (herness.store.ops.findings.scrub_record_from_findings)(record_id, conn=<connection>)` (impl 06 owns the `finding` table, R-77); delete `data/traces/*.jsonl` files whose bytes contain `record_id`. 5. First visit: `T08-12 (herness.core.jobs.enqueue)("build_pipeline", <impl 08 nightly payload>, "none", priority=60, idem_key="privacy_rebuild:" + request_id)` (`build_pipeline` starts with no GPU class, R-43); store the job ID; enqueue a continuation `maintenance` job (same payload plus `request_id`, `scheduled_for = now + 30 min`, `idem_key = "privacy_delete:<request_id>:wait:<n>"`); return `JobOutcome(status="done", result={"waiting_for": <job_id>})`. Continuation: build job `done` and a build newer than the step-5 time is `CURRENT` → `T02-21 (herness.model.promote.cleanup_builds)(mode="post", keep_last=1, protect=frozenset(), layout=<data layout>, now=now)`; a non-empty `deferred` marks step 5 `failed` and the job is retried; build `failed` → re-enqueue it (at most 3 times, then request `failed`); still running → next continuation in 30 minutes. 6. `set_deletion_status(request_id, "done", completed_at=now)`; `audit("admin_action", "system", action="privacy_delete", target=record_id)`. 7. Lake pass 2: repeat step 2; if rows were removed, repeat steps 3 and 3b. A failing step records `failed` with the error class, leaves the request `running`, and re-raises so impl 08 retries. |
| Side effects | lake, decision cache, labels, vectors, memory store, ops rows, traces, warehouse files; audit |
| Errors | step errors re-raised (`StoreBusy` and `ModelUnavailable` retryable; others `FatalError`) |
| Concurrency | exclusive `maintenance` job kind; T01-05 (herness.connectors.deletion.DeletionFilter) and impl 02 staging drop the record from step 1 onward |
| Complexity and limits | lake scan limited to the record's source/entity (impl 02 primitive) |
| Security notes | TH10-42, TH10-50. |
| Tests | UT10-69, IT10-06, FT10-03, ST10-50, ST10-57 |

#### U10-100 herness.admin.privacy.purge_lake_record

Removed (R-57, R-09): see `T02-03 (herness.store.lake_purge.purge_record_ids)`, the impl 02 primitive that performs the permitted lake rewrite for privacy deletion. U10-99 steps 2 and 7 call it.

#### U10-101 herness.admin.rekey.run_rekey

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | The planned redaction-key rotation (design 10 §5.3 steps 2–4). |
| Signature | `run_rekey(cfg: HernessConfig, ctx: JobContext, *, now: datetime) -> JobOutcome` |
| Preconditions | `redact.hmac_key.next` exists, unless the saved state shows the swap began (else `FatalError("no staged key")`). |
| Postconditions | New key active and labels re-keyed, or old key active and nothing downstream changed. |
| Algorithm | State via `ctx.save_state`/`load_state` (`step`, `new_key_id`, `old_key_id`). 1. Load both keys and their ids (U10-40). If the active key's id equals a saved `new_key_id`, go to 4b. 2. `build_rekey_map` (U10-102) → `data/cache/rekey/<new_key_id>.parquet`; save `step=2`. 3. `rekey_labels` (U10-103); save `step=3`. 4. a. `set_secret("redact.hmac_key", <new value>, actor="system")`; b. `delete_secret("redact.hmac_key.next", actor="system")`; c. write `data/cache/rekey/SWAPPED` atomically (`swapped_at`, `old_key_id`, `new_key_id`); d. `audit("admin_action", "system", action="redact_rekey", target=new_key_id, detail="old=" + old_key_id)`; e. `reset_redactor()`. 5. Return `done`. A failure before 4a leaves the old key and `.next` in place (design 10 §6). |
| Side effects | keyring, files, audit |
| Errors | `FatalError` |
| Concurrency | exclusive job in the impl 08 planned slot |
| Complexity and limits | ≈ 20 min for 6M records on 16 cores |
| Security notes | Key values never logged; ids logged. |
| Tests | UT10-70, IT10-07, FT10-02 |

#### U10-102 herness.admin.rekey.build_rekey_map

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Map each record's old `content_hash` to its new one. |
| Signature | `build_rekey_map(cfg: HernessConfig, new_redactor: Redactor, *, out_path: Path, ctx: JobContext) -> int` |
| Algorithm | 1. Open the `CURRENT` warehouse read-only (T02-09 (herness.store.warehouse.open_readonly)). 2. Stream `enrich.text_redacted` (`record_id`, `content_hash`) with the classifier text composed by T03-05 (herness.enrich.text.compose_text) from `core.*`, in batches of 20,000. 3. Redact with `new_redactor` (failed record → `new_content_hash = NULL`, reported); `new_content_hash = sha256(redacted).hexdigest()[:32]` (spec 00 §5). 4. Write `(record_id, old_content_hash, new_content_hash)` atomically; `ctx.heartbeat()` per batch. |
| Side effects / Errors | one parquet file / `SchemaViolation` when tables are missing |
| Concurrency | single job |
| Complexity and limits | memory ≤ one batch |
| Security notes | Raw text stays in process; only hashes are written. |
| Tests | UT10-70 |

#### U10-103 herness.admin.rekey.rekey_labels

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Re-key human, gold and teacher labels (design 10 §5.3 step 3). |
| Signature | `rekey_labels(labels_root: Path, rekey_map: Path) -> dict[str, int]` (`rekeyed`, `kept_old`, `ambiguous`) |
| Algorithm | 1. Build `old → set(new)` from the map (NULL new hashes ignored). 2. For each parquet under `data/labels/*/{human,gold,teacher}/`: exactly one new hash → replace; several → one row per new hash (`ambiguous`); none → keep the old hash (`kept_old`, reported). 3. Write `.tmp`, then `os.replace`. |
| Side effects / Errors | label rewrites / `OSError` → `StoreBusy` |
| Concurrency | single job |
| Complexity and limits | linear |
| Security notes | Gold-set integrity (LLM04). |
| Tests | UT10-70, IT10-07 |

#### U10-104 herness.admin.maintenance.handle_maintenance

| Field | Content |
|-------|---------|
| Kind | function (job handler) |
| Purpose | Dispatch `maintenance` jobs by `payload.action` (impl 08 §5.1). |
| Signature | `handle_maintenance(ctx: JobContext, *, memory_purge: Callable[[str], int]) -> JobOutcome`; `memory_purge` is bound by U10-75 with `functools.partial`, so impl 08 calls the handler with `ctx` only (R-42). |
| Algorithm | Read `payload = ctx.job.payload` (R-42). `backup` → U10-96; `purge` → U10-98; `privacy_delete` → U10-99 with `memory_purge`; `rekey` → U10-101; other → `ConfigError("unknown maintenance action")`. `now` from T00-04 (herness.core.time.now). Returns `JobOutcome(status="done", result=<counts>)` unless the unit returns its own outcome. Scheduled nightly runs (impl 08) enqueue `backup` then `purge`. |
| Side effects / Errors | as the called unit |
| Concurrency | exclusive job kind |
| Complexity and limits | n/a |
| Security notes | none |
| Tests | UT10-68 |

### 3.8 Privacy ops-store area (`herness/store/ops/privacy.py`, R-08)

Every function here obtains its connection through `herness.store.ops.core.connection()` and writes only through `herness.store.ops.core.run_write()`; JSON columns go through `dump_json()`/`load_json()` (R-10). The `deletion_request` table and its indexes (`deletion_request_status`, `deletion_request_record`) are created by impl 02 migration 005 (U02-53), so this area needs no migration of its own; the range 080–089 (R-11) stays unused.

#### U10-105 herness.store.ops.privacy.DeletionRequest, create_deletion_request, get_deletion_request, open_deletion_request, set_deletion_status, record_deletion_step

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass) and functions |
| Purpose | Read and write `deletion_request` rows for U10-99 (formerly requested from impl 02 as delta D10-12; owned here by R-08, R-09). |
| Signature | `DeletionRequest(request_id: str, record_id: str, requested_by: str, reason_ref: str, status: Literal["pending", "running", "done", "failed"], steps: dict[str, dict[str, Any]], created_at: datetime, completed_at: datetime \| None)`. `create_deletion_request(*, record_id: str, requested_by: str, reason_ref: str, now: datetime) -> DeletionRequest`. `get_deletion_request(request_id: str) -> DeletionRequest` (missing → `NotFound`, R-19). `open_deletion_request(record_id: str) -> DeletionRequest \| None` (the newest request in `pending` or `running`). `set_deletion_status(request_id: str, status: Literal["running", "done", "failed"], *, completed_at: datetime \| None = None) -> None`. `record_deletion_step(request_id: str, step: Literal["1", "2", "3", "3b", "4", "5", "6", "7"], *, status: Literal["done", "failed"], at: datetime, counts: Mapping[str, int], error: str \| None = None) -> None`. |
| Preconditions | `record_id` matches the U10-73 pattern; `requested_by` matches `^[0-9a-f]{32}$`; `reason_ref` matches `^[A-Za-z0-9._:/-]{1,64}$`; violations → `ConfigError` naming the field only. |
| Postconditions | `create_deletion_request` inserts `status = 'pending'`, `steps = '{}'`, `created_at = now`. `record_deletion_step` stores `steps[step] = {"status", "at", "counts", "error"}`, replacing an earlier entry for the same step. |
| Invariants | At most one request per `record_id` is in `pending`/`running`: `create_deletion_request` runs the `open_deletion_request` query inside the same `run_write` transaction and returns the existing row when one exists (idempotency key: `record_id` among open requests). Status moves only `pending → running → done` or `running → failed`; any other transition raises `ConfigError("invalid deletion status transition")`. |
| Algorithm | Each write is one `run_write(op="privacy_<function>")` transaction: read the row (`read_one`), check the transition or merge the `steps` JSON in Python, write it back with one `UPDATE ... WHERE request_id = ?`. Times are fixed-width UTC text (T00-04 (herness.core.time.format_utc)). |
| Side effects | `deletion_request` rows |
| Errors | `ConfigError`, `NotFound`, `StoreBusy` (from the core API) |
| Concurrency | serialised by the ops-store write path of impl 02; the exclusive `maintenance` job is the only writer |
| Complexity and limits | O(1) per call; `steps` ≤ 8 entries |
| Security notes | TH10-42. Rows hold the `record_id` and the pseudonymous `user_ref` only. |
| Tests | UT10-77 |

#### U10-106 herness.store.ops.privacy.scrub_record_from_evidence, scrub_record_from_findings

Removed (R-09, R-77): see T05-12 (herness.store.ops.evidence.scrub_record_from_evidence) (U05-75) for the evidence scrub and T06-06 (herness.store.ops.findings.scrub_record_from_findings) (U06-144) for the finding scrub. Impls 05 and 06 own the `evidence` and `finding` tables; U10-99 step 4 calls both functions.

#### U10-111 herness.store.ops.privacy.deleted_record_ids

| Field | Content |
|-------|---------|
| Kind | function (area `privacy`, R-08, R-09; replaces the impl 01 definition U01-29, which now refers here) |
| Purpose | The deletion set for one source entity: the `record_id`s with a `deletion_request` in `running` or `done` (design 01 §5.6), read by impl 01's deletion filter and impl 02's staging. |
| Signature | `deleted_record_ids(source: str, entity: str) -> list[str]` (both positional); sorted, unique. |
| Preconditions | `source` is a connector name (never `monitoring:<tool>`) and `entity` an entity name, each matching `^[a-z][a-z0-9_]{0,63}$`, else `ConfigError` naming the parameter. |
| Postconditions | Every returned ID starts with `<source>:<entity>:`. Requests in `pending` or `failed` are not returned. |
| Invariants | Read-only. |
| Algorithm | `read_all("SELECT DISTINCT record_id FROM deletion_request WHERE status IN ('running', 'done') AND substr(record_id, 1, ?) = ? ORDER BY record_id", (len(prefix), prefix), max_rows=1_000_000)` with `prefix = f"{source}:{entity}:"`; `substr` avoids `LIKE` wildcard escaping. Return the first column as a list. |
| Side effects | one read |
| Errors | `StoreBusy` from `read_all`; more than 1,000,000 IDs → `SchemaViolation` from `read_all` |
| Concurrency | safe from any thread |
| Complexity and limits | O(requests); 100,000 IDs ≈ 4 MB |
| Security notes | TH10-42. The IDs are never logged. |
| Tests | UT10-83 |

### 3.9 Chat approval check and error attributes (`herness/core/egress.py`, `herness/core/errors.py`)

#### U10-107 herness.core.egress.cloud_chat_allowed

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Tell T08-19 (herness.core.jobs.chat_model_profile), which selects the chat mode (C08-06), whether chat `cloud` mode (model purpose `reasoning`, payload class `aggregated_evidence`) may run under the current profile (R-38). |
| Signature | `cloud_chat_allowed(cfg: HernessConfig) -> bool` |
| Preconditions | `cfg` passed U10-09. |
| Postconditions | `True` exactly when a call with purpose `reasoning` and payload class `aggregated_evidence` would pass U10-51 steps 1–2. |
| Algorithm | 1. `security.egress.enabled` false → `False`. 2. `reasoning` not in `security.egress.purposes` → `False`. 3. Profile `premium` → `True`. 4. Profile `hybrid` → `security.data_policy.chat_approved` (R-38). 5. Any other profile → `False`. When the result is `False`, T08-19 (herness.core.jobs.chat_model_profile) selects the local model; U10-51 step 2 enforces the same rule again at the guard (`chat_not_approved`). |
| Side effects / Errors / Concurrency | none / none / pure |
| Complexity and limits | O(1) |
| Security notes | TH10-49. |
| Tests | UT10-79, ST10-56 |

#### U10-108 herness.core.errors.EgressBlocked, ConfigError (attributes declared by this spec)

| Field | Content |
|-------|---------|
| Kind | class attributes (R-19: `herness.core.errors` is owned by impl 00; the attributes of these two classes are declared here) |
| Purpose | Let callers read why egress was refused and which config issues failed without parsing messages. |
| Signature | `EgressBlocked(message: str, *, egress_id: str \| None = None, reason: str \| None = None, hint: str \| None = None, details: dict[str, str] \| None = None)`; attributes `egress_id: str \| None` (`egr_<ulid>` for guard decisions, `None` for socket-guard and loopback refusals) and `reason: str \| None` (a reason code of U10-51, U10-58 or U10-59). `ConfigError(message: str, *, issues: Sequence[object] = (), hint: str \| None = None, details: dict[str, str] \| None = None)`; attribute `issues: tuple[object, ...]` holding `ConfigIssue` instances (typed `object` because `herness.core.errors` may not import `herness.core.config`). `hint` and `details` are the `HernessError` attributes added by R-19. |
| Preconditions | `reason`, when given, matches `^[a-z0-9_]{1,40}$`; `details` values contain no secret (U10-32 scrubs them anyway). |
| Postconditions | Both classes keep their taxonomy parents (`EgressBlocked` is a `FatalError`; `ConfigError` as impl 00 declares). |
| Algorithm | Store the keyword arguments as attributes; `__str__` is the message only. |
| Side effects / Errors / Concurrency | none / none / immutable after construction |
| Complexity and limits | `issues` ≤ 1,000 entries |
| Security notes | TH10-19 (the reason reaches the fallback trace of T08-09 (herness.core.resilience.ModelChain) without payload). |
| Tests | UT10-80 |

## 4. State and data

### 4.1 Ops store

This spec owns the ops-store area `herness/store/ops/privacy.py` (R-08), which writes table `deletion_request` (design 02 §5.5) (U10-105, U10-111). The rewrites of `evidence.result_sample` and `finding.numbers` belong to the owners of those tables, `T05-12 (herness.store.ops.evidence.scrub_record_from_evidence)` (R-08, R-09) and `T06-06 (herness.store.ops.findings.scrub_record_from_findings)` (R-77); this spec only calls them. The table and its indexes `deletion_request_status` (`status`) and `deletion_request_record` (`record_id`) are created by impl 02 migration 005 (U02-53, R-11). No column or table exists only in this spec, so no migration in this spec's range 080–089 is needed.

| Column | Type | Null | Constraint | Meaning |
|--------|------|------|-----------|---------|
| `request_id` | TEXT | no | PK, `del_<ulid>` | Deletion request |
| `record_id` | TEXT | no | U10-73 pattern | Record to delete |
| `requested_by` | TEXT | no | 32 hex `user_ref` | Requesting admin |
| `reason_ref` | TEXT | yes in the schema; always set by U10-105 | ≤ 64 chars | External ticket reference |
| `status` | TEXT | no | `pending`, `running`, `done`, `failed` | Lifecycle |
| `steps` | TEXT (JSON) | no | object, default `{}`, keyed by step identifier `1`, `2`, `3`, `3b`, `4`, `5`, `6`, `7`; each value `{"status": "done"\|"failed", "at": ts, "counts": {..}, "error": class \| null}` | Step log |
| `created_at`, `completed_at` | TEXT (UTC fixed width) | `completed_at` yes | spec 00 §8 | Times |

Indexes: those of U02-53 (no index is requested from impl 02 any more). Idempotency key: at most one request per `record_id` in `pending`/`running` (U10-105 invariant, U10-99 step 1); job `idem_key` `privacy_delete:<record_id>`. Rewrites of `evidence` and `finding` rows are idempotent: a second scrub finds nothing to drop.

### 4.2 Files

| Path | Writer | Format | Idempotency / atomicity | Retention |
|------|--------|--------|-------------------------|-----------|
| `data/logs/audit-<YYYY-MM-DD>.jsonl` | U10-60 | canonical JSON lines, hash chain (design 10 §4.6) | append under `.audit.lock`; `fsync` per line | `audit_log_days` (730) |
| `data/logs/egress-<YYYY-MM-DD>.jsonl` | U10-57 | design 10 §4.5 | append under `.egress.lock` | `egress_log_days` (365) |
| `data/logs/.audit.lock`, `.egress.lock` | U10-61 | empty lock files | n/a | permanent |
| `data/config_snapshots/<config_hash>.yaml` | U10-63 | `yaml.safe_dump` of `effective_dict` | written once per hash, atomic | never purged (backed up) |
| `data/config_snapshots/LAST` | U10-63 | one hash | atomic replace | permanent |
| `data/cache/redact/display_names.txt` | U10-39 | one name per line | atomic replace | permanent (personal data) |
| `data/cache/rekey/<new_key_id>.parquet` | U10-102 | `record_id`, `old_content_hash`, `new_content_hash` | atomic | kept until the next rekey |
| `data/cache/rekey/SWAPPED` | U10-101 | JSON `swapped_at`, `old_key_id`, `new_key_id` | atomic; deleted by U10-98 step 6 | transient |
| `data/deploy/history.jsonl` | U10-78 | JSON lines | append under `.deploy.lock`; one line per new pin set | permanent |
| `data/deploy/rendered.json` | U10-79 | JSON | atomic replace | permanent |
| `data/deploy/weights-<class>-<id>.json` | U10-83 | `{path: size}` | atomic | pruned with its revision |
| `data/locks/deploy-pull.lock` | U10-81, U10-92 | lock file | n/a | permanent |
| `/opt/herness/docker.env` (WSL) | U10-79 | `KEY=value` | temp then `mv`, root 600 | replaced on render |
| `<repo root>/.release/<version>/` | U10-88 | bundle copy | per-file atomic | two newest versions |
| `<backup_target>/ops/ops-<date>.sqlite` and mirrors | U10-96 | SQLite, file copies | temp then `os.replace` | U10-97 keep policy for ops; mirrors never deleted |
| Windows Credential Manager, service `herness` | U10-31, U10-33 | per-name entries, chunked above 1,200 chars | per `set` | until rotated |

### 4.3 In-memory state

| State | Module | Model | Reset |
|-------|--------|-------|-------|
| Cached config and reset hooks | `config` | lock-protected | `reset_config` |
| Registry and entry-point flag | `registry` | lock-protected | `reset_registry` |
| Owner-validator registry | `config_validate` | lock-protected | `reset_owner_validators` |
| `_KNOWN` secret values and compiled scrub pattern | `secrets` | lock-protected; values held only for scrubbing | cleared by `reset_config` hook |
| Cached `Redactor` | `redact` | lock-protected lazy | `reset_redactor` |
| Cached `EgressGuard`, token counter | `egress`, `egress_log` | lock-protected | `reset_guard` |
| `_POLICY`, `_RESOLVED`, re-entrancy flag | `egress_socket` | atomic swap + lock; thread-local flag | `reset_socket_guard` |

### 4.4 Transaction boundaries

- Audit and egress lines: one line per lock hold; the chain read and the append happen under the same lock.
- `record_config_change`: snapshot, audit line and `LAST` under one audit-lock hold.
- Privacy deletion: each step (including 3b) is its own unit of work; its result is recorded with `record_deletion_step` in its own `run_write` transaction before the next step starts. Evidence and finding scrubs commit per batch of 500 rows.
- Rekey: steps 2 and 3 produce new files swapped with `os.replace`; the key swap (step 4) happens only after both succeed.

## 5. Control flows

### F10-01 Process start and config load

| Step | Unit | State change | On failure |
|------|------|--------------|-----------|
| 1 | T09-20 (herness.cli.main) calls U10-21 `load_bootstrap` | none | `ConfigError` → exit 3 before any socket opens |
| 2 | U10-58 `install_socket_guard(bootstrap)` | audit hook, env vars | cannot fail except by programming error |
| 3 | U10-10 `init_config` → U10-09 (sources U10-15–U10-19, checks U10-20 offline) | config cache | `ConfigError` → exit 3; doctor shows the issues |
| 3a | Composition root registers owner validators (U10-109) before step 3, then calls `run_owner_validators(cfg, offline=True)` (skipped for `config validate`, `doctor`, `secrets *` and `deploy *`, which report instead) | none | any `error` issue → `ConfigError` → exit 3 (R-46); no job starts |
| 4 | U10-58 `install_socket_guard(cfg)` (full source host list) | policy replaced | as step 2 |
| 5 | U10-63 `record_config_change(cfg)` | snapshot, audit line, `LAST` | `FatalError` (audit) → exit 1; no job starts |
| 6 | T00-07 (herness.core.logging.configure_logging) configured with U10-32 processor | logging | n/a |

### F10-02 Redaction of a build's text (called by impl 03)

| Step | Unit | State change | On failure |
|------|------|--------------|-----------|
| 1 | U10-39 `update_display_names` | display names file | `StoreBusy` → job retry |
| 2 | U10-45 `get_redactor` (after `reset_redactor` so new names load) | cache | `ConfigError` (key missing) → job fails |
| 3 | U10-47 `redact_table` | none (returns table) | row failure → NULL text and counter; worker crash → `StoreBusy` retry |
| 4 | impl 03 writes `enrich.text_redacted` and `content_hash` | warehouse | impl 03 |

### F10-03 Guarded off-network call (hybrid or premium)

| Step | Unit | State change | On failure |
|------|------|--------------|-----------|
| 1 | T05-08 (herness.harness.llm.anthropic_client.AnthropicClient) obtains `get_guard().async_http_client(purpose, payload_class, run_id, task_id)` (U10-53) | none | `EgressBlocked` for `model_download` |
| 2 | U10-54 transport reads request body | none | streaming body → blocked path |
| 3 | U10-51 steps 1–6 under the egress lock | none | `EgressBlocked` with reason; blocked line and audit line written; T08-09 (herness.core.resilience.ModelChain) falls back to a local model |
| 4 | U10-51 step 7 writes `allowed` line | egress log | log write fails → `EgressBlocked("egress_log_failed")`; nothing sent |
| 5 | Inner transport opens the socket; U10-58 allows the destination host | network | transport error → `completed` line with reason, error re-raised to T05-08 (herness.harness.llm.anthropic_client.AnthropicClient) and from there to T08-09 (herness.core.resilience.ModelChain) |
| 6 | U10-54 response close writes `completed` line with usage | egress log | response > 50 MiB (decoded) → `EgressBlocked("response_too_large")`; `content-encoding` other than identity, gzip or deflate → `EgressBlocked("unsupported_encoding")` |

### F10-04 Blocked socket (any profile)

| Step | Unit | State change | On failure |
|------|------|--------------|-----------|
| 1 | Any library calls `socket.getaddrinfo(host)` or `connect` | none | — |
| 2 | U10-58 hook checks policy | resolution cache | host not allowed → `EgressBlocked` raised in the caller, `egress.socket.blocked` logged, metric incremented |

### F10-05 Secret set and rotation

| Step | Unit | State change | On failure |
|------|------|--------------|-----------|
| 1 | T09-20 (herness._cli.identity.check_command_role) (admin (OS)) | none | `PermissionDenied` → exit 11 (R-46) |
| 2 | U10-69 prompts twice | none | mismatch → exit 1 |
| 3 | U10-31 audit `secret_set` | audit line | `FatalError` → secret not stored |
| 4 | U10-33 backend write | Credential Manager | `ConfigError` (backend unavailable) → exit 3; audit line remains (records the attempt) |
| 5 | Clients pick up the value at the next construction (worker restarts clients at job start, design 10 §5.2) | none | n/a |

### F10-06 Model pull (`deploy pull --allow-download`)

| Step | Unit | State change | On failure |
|------|------|--------------|-----------|
| 1 | U10-92 preconditions; stop services | containers stopped | `ConfigError` / `StoreBusy` → nothing changed |
| 2 | U10-55 download window opened (audited) | flag, audit | `EgressBlocked` → stop |
| 3 | U10-81 firewall outbound `Allow` | host firewall | `FatalError` → window never opened |
| 4 | U10-82 pull and verify images | images | mismatch → `FatalError`; step 6 still runs |
| 5 | U10-83 pull and verify weights | model files, manifests | mismatch → `FatalError`; step 6 still runs |
| 6 | U10-81 `finally`: outbound `Block`, verified | host firewall | restore failure → CRITICAL log, `FatalError` with manual fix |
| 7 | U10-55 window closed; U10-92 re-verify and audit | audit | failure → exit 1; nothing rendered or promoted |

### F10-07 Release install (`deploy install BUNDLE`)

| Step | Unit | State change | On failure |
|------|------|--------------|-----------|
| 1 | U10-86 `ReleaseBundle.load` | none | `ConfigError` (missing, extra, symlink, outside root) |
| 2 | U10-87 provenance of wheel and requirements | none | `FatalError`, `deploy.install.refused`; nothing installed |
| 3 | U10-87 SBOM attestation, components, licences | none | `FatalError`; nothing installed |
| 4 | U10-88 copy, re-hash, audit | `.release/`, audit | hash mismatch → `FatalError`; audit failure → nothing installed |
| 5 | U10-88 `uv pip sync --require-hashes`, then wheel install | venv | `FatalError`; the operator re-runs install of the previous version from `.release/` |
| 6 | U10-112 DuckDB `excel` extension: pinned hash, local `INSTALL`, `LOAD`, re-hash of the installed file | DuckDB extension directory | `FatalError`, `deploy.install.refused`; the wheel is installed but the files connector refuses `.xlsx` until the install is re-run |

### F10-08 Upgrade and rollback (design 10 §5.6.5)

| Step | Unit | State change | On failure |
|------|------|--------------|-----------|
| 1 | Operator edits `deploy.<class>` and the impl 05 `models.yaml` entry on a branch | config | C07/C09/C13 issues in validate |
| 2 | F10-06 | images, weights | abort |
| 3 | impl 11 `herness eval` against the candidate | eval report | merge only when not worse on the golden set |
| 4 | U10-79 render, U10-84 up | env file, containers, history, audit (`config_change` via F10-01 step 5) | `ModelUnavailable` → rollback |
| 5 | Rollback: revert config, U10-85 `rollback_class` | containers | target missing → `ConfigError` "run deploy pull" |

### F10-09 Doctor

| Step | Unit | State change | On failure |
|------|------|--------------|-----------|
| 1 | T09-22 (herness._cli.doctor.run_doctor) calls U10-89 with the loaded config (or a config-less run listing `config_valid` FAIL) | none | — |
| 2 | U10-90 and U10-91 checks in parallel | none | each check returns FAIL/WARN; none raises |
| 3 | T09-22 (herness._cli.doctor.run_doctor) renders the table; exit 1 on any FAIL (R-46), else 0 | none | — |

### F10-10 Rekey (design 10 §5.3)

| Step | Unit | State change | On failure |
|------|------|--------------|-----------|
| 1 | U10-71 stages `.next` and calls `T08-14 (herness.core.jobs.schedule_rekey)` | keyring, job | exit 1; no key staged without escrow |
| 2 | impl 08 runs `maintenance {"action": "rekey"}` before the Saturday `build_pipeline` | job | — |
| 3 | U10-101 steps 2–3 (U10-102, U10-103) | map file, label files | `FatalError`; old key active; `.next` kept for retry |
| 4 | U10-101 step 4 key swap, marker, audit | keyring, marker, audit | crash between 4a and 4b → retry detects the new key id and resumes at 4b |
| 5 | Next `build_pipeline` re-embeds and re-classifies (impl 03) | warehouse, cache | impl 03 |
| 6 | U10-98 step 6 deletes the old decision cache after promotion | cache | skipped until promotion |

### F10-11 Nightly backup and F10-12 purge

| Step | Unit | State change | On failure |
|------|------|--------------|-----------|
| 1 | T08-14 (herness.core.jobs.scheduler.run_scheduler) enqueues `maintenance {"action": "backup"}` at `backup.nightly_at` then `{"action": "purge"}` | jobs | — |
| 2 | U10-104 → U10-96 | backup target | `FatalError` → job retried per impl 08 (`max_attempts` 2) |
| 3 | U10-104 → U10-98 | deletions | files in use skipped |

### F10-13 Privacy deletion

| Step | Unit | State change | On failure |
|------|------|--------------|-----------|
| 1 | U10-105 open or create request; status `running` | `deletion_request` | `StoreBusy` → job retry |
| 2 | `T02-03 (herness.store.lake_purge.purge_record_ids)` (R-57) | lake files | `StoreBusy` → step `failed`, job retry |
| 3 | `T03-34 (herness.enrich.purge_record)` | decision cache, labels, vectors | step `failed`, job retry |
| 3b | `memory_purge` → `T07-26 (herness.harness.memory.MemoryStore.purge)` (R-54) | memory items, memory vectors, FTS rows | `ModelUnavailable` or `StoreBusy` → step `failed`, job retry |
| 4 | U05-75 evidence scrub (impl 05) and U06-144 finding scrub (impl 06, R-77); trace file deletion | `evidence`, `finding`, traces | `StoreBusy` → retry |
| 5 | rebuild with `build_pipeline` (no GPU class, R-43), then `cleanup_builds(mode="post", keep_last=1)` | warehouse files | build failure → re-enqueue ≤ 3 times, then request `failed`; deferred files → retry |
| 6 | U10-105 status `done`; audit `privacy_delete` | request, audit | audit failure → `FatalError`, retry |
| 7 | second lake pass; repeat 3 and 3b when rows were removed | lake, caches, memory | as steps 2, 3, 3b |

Each step records its result in `deletion_request.steps`, and a failing step leaves the request `running` so the job retries from that step. Connectors (impl 01) and staging (impl 02) exclude the record from step 1 onward; the second lake pass (step 7) removes rows a running sync committed before its next checkpoint.

## 6. Error handling

| Failure condition | Class raised | Caught where | Retry / fallback | User-visible effect | Log event |
|-------------------|--------------|--------------|------------------|---------------------|-----------|
| Invalid YAML, duplicate key, alias, unknown key, failed cross-check, gate missing | `ConfigError` | CLI entry (impl 09), worker start | none | exit 3; doctor lists issues | `config.load.failed` |
| `security.*` or `profile` from env or `--set` | `ConfigError` | CLI entry | none | exit 3 naming the variable | `config.load.failed` |
| Secret missing | `ConfigError("secret not found: <name>")` | client construction site | none | exit 3; `secrets status` shows missing | `secrets.resolve.failed` |
| Keyring backend unavailable | `ConfigError` | as above | none | hint: run as the owning account | `secrets.backend.unavailable` |
| Egress refused by any rule | `EgressBlocked` (`FatalError`) | T08-09 (herness.core.resilience.ModelChain) | no retry; next local chain entry | exit 13 when surfaced to CLI (R-46); task records reason | `egress.call.blocked` |
| Chat `cloud` mode in `hybrid` without `chat_approved` (R-38) | none (U10-107 returns `False`); `EgressBlocked("chat_not_approved")` if a caller bypasses it | T08-19 (herness.core.jobs.chat_model_profile) | chat runs on the local model | answer from the local model | `egress.call.blocked` (bypass only) |
| Loopback client given or sent to a non-loopback host | `EgressBlocked` (`not_loopback`) | caller (impl 05, impl 08, doctor) | none | health check or local call fails | `egress.loopback.blocked` |
| Source client request to a host outside its allowlist, non-`https`, with user info, or oversized response; TLS verification disabled | `EgressBlocked`; `ConfigError` for `verify=False` | impl 01 connector | none (impl 01 maps it to a failed sync) | sync fails with the reason | `egress.source.blocked` |
| Owner validator reports an `error` at start-up | `ConfigError` (U10-109) | composition root | none | exit 3 (R-46); `config validate` lists the issue | `config.validate.issue` |
| Socket to a non-allowed host | `EgressBlocked` | caller's library | none | as above | `egress.socket.blocked` |
| Egress log write fails | `EgressBlocked("egress_log_failed")` | T08-09 (herness.core.resilience.ModelChain) | none | call not sent | `egress.call.blocked` |
| Redaction exception on one record | `RedactionFailed`, caught inside U10-43/U10-47 | same unit | none | `text = NULL`; impl 03 DQ warning | `redact.record.failed` |
| Redaction worker process crash | `StoreBusy` | impl 08 job runner | job retry | build delayed | `redact.table.failed` |
| Audit lock timeout or write error | `StoreBusy` inside, then `FatalError` | caller of `audit` | lock wait ≤ 10 s; no retry | the audited action does not happen | `audit.write.failed` |
| Audit field would contain a secret | `SchemaViolation` | caller | none | action refused (programming defect) | `audit.write.failed` |
| Model server unhealthy after `deploy up` | `ModelUnavailable` | CLI | impl 08 retries swaps for the worker path | exit 9 (R-46) | `deploy.class.failed` |
| Image digest or weight hash mismatch | `FatalError` | CLI | none | exit 1; firewall closed | `deploy.pull.failed` |
| Firewall cannot be restored | `FatalError` | CLI | one re-set attempt | exit 1 with manual command | `deploy.firewall.restore_failed` (CRITICAL) |
| Attestation or SBOM check fails | `FatalError` | CLI | none | exit 1; nothing installed | `deploy.install.refused` |
| `deploy.install.extension_installed` | INFO | `name`, `extension_version`, `sha256` | U10-112 |
| Rekey fails before key swap | `FatalError` | impl 08 job runner | job retry (`max_attempts` 2) | old key active; `.next` kept | `rekey.failed` |
| Deletion step fails (including memory purge step 3b) | step error (`StoreBusy`, `ModelUnavailable` or `FatalError`) | impl 08 job runner | job retry from the failed step | request stays `running` | `privacy.delete.step_failed` |
| Backup `integrity_check` fails | `FatalError` | impl 08 job runner | job retry | no copy kept for that night | `maintenance.backup.failed` |
| Purge meets a file in use | none (counted) | U10-98 | next night | count `skipped_in_use` | `maintenance.purge.completed` |
| Scrub processor fails | none | U10-32 | none | event replaced by `log.scrub.failed` | `log.scrub.failed` |

## 7. Security

### 7(a) Trust boundaries touched

TB6 (host → off-network endpoints: egress guard, socket guard), TB8 (host ↔ WSL2 containers: compose, env file, ports, firewall), TB9 (registries, model hub, release artifacts: pull and install verification), TB10 (operator → CLI, config files, `.env`), and TB3 as the redaction provider for text that reaches prompts.

### 7(b) STRIDE threat table

| ID | TB | STRIDE | Threat | L | I | Control | Reference | Test |
|----|----|--------|--------|---|---|---------|-----------|------|
| TH10-01 | TB10 | S | A non-admin OS user runs admin commands (`secrets set`, `privacy delete`, `deploy *`) | M | H | T09-20 (herness._cli.identity.check_command_role) from `security.ui.roles`; admin (OS) elevation check; Credential Manager is per account; folder ACLs | ASVS v5.0.0-V8.2 | ST10-36 |
| TH10-02 | TB10 | T | A stray env variable or `--set` enables egress or changes security settings | M | H | `security.*` file-only (U10-09 step 3, U10-18) | ASVS v5.0.0-V13.1 | ST10-01, ST10-02 |
| TH10-03 | TB10 | T | A profile overlay self-approves the data policy | L | H | Overlays may not set `security.data_policy` (U10-17); gate read only from `herness.yaml` | ASVS v5.0.0-V13.1 | ST10-03 |
| TH10-04 | TB10 | T/D | Malicious YAML: alias bomb, duplicate keys hiding values, huge file | L | M | Alias/anchor rejection, duplicate-key rejection, 5 MiB cap (U10-15) | ASVS v5.0.0-V15 (safe deserialisation) | ST10-05 |
| TH10-05 | TB10/TB8 | T/E | Config values injected into `wsl.exe`/PowerShell command lines | M | H | Strict value patterns (U10-07), argv-only runner with allowlist and `--exec` (U10-76, U10-77); fixed scripts take values as positional arguments | ASVS v5.0.0-V1.2 | ST10-06 |
| TH10-06 | TB10 | I | Plain-text credentials or real PII committed to config or fixtures | M | H | C16 sweep; `SecretRefStr` types; `detect-secrets`; fixture scanner (U10-49) | ASVS v5.0.0-V13.3 | ST10-04, ST10-30 |
| TH10-07 | TB10 | I | Secret values appear in `config show`, snapshots, logs, traces, exceptions or reports | M | H | References only on config; `SecretStr`; `scrub_secrets`; audit field check | ASVS v5.0.0-V13.3, V16.2 | ST10-14, ST10-15, ST10-16, ST10-18 |
| TH10-08 | TB10 | R | An admin denies a config change, secret change, deletion or deploy | M | M | Audit lines for `config_change` and every `admin_action`, actor = `user_ref` | ASVS v5.0.0-V16.3 | ST10-17 |
| TH10-09 | TB10 | T | Audit log edited, truncated or reordered to hide an action | L | H | SHA-256 hash chain; doctor `audit_chain`; folder ACL; nightly backup copy | ASVS v5.0.0-V16.4 | ST10-17 |
| TH10-10 | TB10 | D | ReDoS-prone redaction pattern in config stalls builds and egress checks | L | M | Pattern length cap, nested-quantifier rejection (U10-04) | ASVS v5.0.0-V2.2 | ST10-37 |
| TH10-11 | TB10 | E | `dotenv` secrets backend used in production to bypass Credential Manager protection | L | H | C12 and U10-33 refuse outside dev/`synth` | ASVS v5.0.0-V13.3 | ST10-26 |
| TH10-12 | TB10 | E | Dashboard exposed on the LAN without the proxy, or identity header trusted from anywhere | M | H | Both identity checks of R-50: C05 at config time (loopback bind, `trusted_proxy` required with exposure and loopback); at request time T09-13 (app.common.auth.resolve_identity) trusts the header only when `expose.enabled`, `trusted_proxy` is set and the peer equals `trusted_proxy`; `.streamlit/config.toml` | ASVS v5.0.0-V13.2 | ST10-27 |
| TH10-13 | TB10 | I | `.env`, directory CSV or `data\` readable by other local users | M | H | Runbook ACLs; doctor `acl_data` | ASVS v5.0.0-V14.2 | ST10-38 |
| TH10-14 | TB6 | I | Raw PII or secrets sent to a cloud model in a prompt | M | H | Redaction before any model (impl 03, impl 05); egress re-scan with JSON decoding and NFKC (U10-51 step 6) | LLM02; ASVS v5.0.0-V14.2 | ST10-10, ST10-11, IT10-03 |
| TH10-15 | TB6 | I | A code path builds its own HTTP client and bypasses the guard | M | H | AST lint test; socket audit hook; `loopback_http_client` for local use | LLM02; ASVS v5.0.0-V15 | ST10-25, ST10-08 |
| TH10-16 | TB6 | E/I | Off-network call in profile `local` | L | H | Profile gate (step 1) and socket guard allowlist | ASVS v5.0.0-V13.2 | ST10-07 |
| TH10-17 | TB6 | S | Look-alike host, user-info trick, IP literal, non-443 port or redirect reaches another server | M | H | Exact host match, `https` + 443, no user info or IP, no redirects, each request checked | ASVS v5.0.0-V12.2 | ST10-09, ST10-32 |
| TH10-18 | TB6 | T/I | TLS interception or downgrade on the cloud call | L | H | TLS ≥ 1.2, verification on, certifi CA, `trust_env=False` | ASVS v5.0.0-V12.1, V12.2 | ST10-39 |
| TH10-19 | TB6 | R | Cannot prove what left the machine, or when | M | M | `allowed` line with `payload_sha256` written before the socket opens; `completed` line; `egress` audit for blocks | ASVS v5.0.0-V16.3 | ST10-40, ST10-33 |
| TH10-20 | TB6 | D | Unbounded token spend or oversized requests | M | M | Per-request and daily token caps; body size cap | LLM10 | ST10-12 |
| TH10-21 | TB6 | T/D | Malicious or huge response from the endpoint | L | M | 50 MiB response cap; timeouts; impl 05 parses into schemas | LLM05; ASVS v5.0.0-V4 | ST10-41 |
| TH10-22 | TB6 | I | Payload text leaks into egress or audit logs | L | H | Log line schema without payload; only hit counts | ASVS v5.0.0-V16.2 | ST10-13 |
| TH10-23 | TB6 | E | A job or prompt-driven path uses `model_download` to exfiltrate | L | H | Purpose refused in `http_client`; window only for `deploy pull`, refused in workers and `synth` | LLM06 | ST10-35 |
| TH10-24 | TB6 | I | Libraries phone home (telemetry, hub downloads) | M | M | Offline env variables; socket guard | LLM03 | ST10-29 |
| TH10-25 | TB8 | S | A local process squats a model port and receives prompts and the bearer key | L | M | Doctor `ports_expected`; ports only on loopback; accepted residual (R-3) | ASVS v5.0.0-V12.3 | ST10-42 |
| TH10-26 | TB8 | I | Container or UI ports reachable from the LAN | M | H | `127.0.0.1` publishing; inbound firewall rules; doctor `ports_loopback` | ASVS v5.0.0-V13.2 | ST10-28 |
| TH10-27 | TB8 | E | Privileged containers, Docker API on TCP, socket mounts | L | H | Compose lint; doctor `docker_api_local` | ASVS v5.0.0-V13.2 | ST10-43 |
| TH10-28 | TB8 | I | Container secrets exposed (env file world-readable or on `/mnt/d`) | M | H | Root-owned mode 600 env file on ext4; path rule; doctor `env_file`; residual R-4 (`docker inspect`) | ASVS v5.0.0-V13.3 | ST10-34 |
| TH10-29 | TB8 | I | Containers download at runtime or send telemetry | M | M | Offline env on every service; Hyper-V default outbound Block; doctor `firewall_outbound` | LLM03 | ST10-44 |
| TH10-30 | TB8 | T | Weights on disk replaced after pull | L | H | Full hash verify on pull; doctor presence check; BitLocker; residual R-5 | LLM04 | ST10-20 |
| TH10-31 | TB8 | D | Two GPU classes loaded → OOM, crashed services | M | M | `deploy up` defers to the worker arbiter or holds `gpu.lock` | LLM10 | ST10-45 |
| TH10-32 | TB8 | R | Unknown who started or changed a model | L | M | `deploy_up`/`deploy_down`/`deploy_rollback` audit; history file | ASVS v5.0.0-V16.3 | ST10-45 |
| TH10-33 | TB9 | T | Container image tag moved or image tampered | M | H | Digest pinning (C13, U10-07); `RepoDigests` check | LLM03; SLSA | ST10-19 |
| TH10-34 | TB9 | T | Hugging Face weights tampered or revision moved | L | H | 40-hex commit revisions; blob SHA-256 check | LLM03, LLM04 | ST10-20 |
| TH10-35 | TB9 | T | GGUF file tampered | L | H | SHA-256 pin | LLM03 | ST10-21 |
| TH10-36 | TB9 | S/T | Wheel built off CI, on a self-hosted runner, from another repo or workflow, or modified | L | H | `gh attestation verify` with repo, signer workflow, hosted-runner and SLSA predicate checks; re-hash before install | SLSA Build L2; ASVS v5.0.0-V15 | ST10-22, ST10-23 |
| TH10-37 | TB9 | T | Dependency substitution or disallowed licence in a release | L | H | Attested requirements with hashes; attested SBOM equals requirements; licence allowlist | ENG §5.6 | ST10-24 |
| TH10-38 | TB9 | E | A malicious plugin registers through entry points | L | H | Plugin loads logged at WARNING and listed by doctor; residual R-6 | ASVS v5.0.0-V15 | ST10-46 |
| TH10-39 | TB9 | D/I | Pull firewall window left open | L | H | `finally` restore with read-back; timer; doctor `firewall_outbound` | ASVS v5.0.0-V13.2 | ST10-47 |
| TH10-40 | TB9 | T/E | Release bundle with symlinks or path traversal | L | M | Containment and symlink checks (U10-86) | ASVS v5.0.0-V5 | ST10-48 |
| TH10-41 | TB10 | I | Disk theft exposes data and weights | L | H | BitLocker on system and data drives; doctor | ASVS v5.0.0-V14.2 | ST10-49 |
| TH10-42 | TB10 | I | A deleted record survives in some store | M | H | Seven-step deletion with second lake pass; staging and connector filters | ASVS v5.0.0-V14.2 | ST10-31, ST10-50 |
| TH10-43 | TB10 | I | Backup copies stored unencrypted | M | H | Doctor BitLocker check includes the backup drive | ASVS v5.0.0-V14.2 | ST10-49 |
| TH10-44 | TB9 | R | Unknown which release is installed | L | M | `deploy_install` audit with version and wheel SHA-256 | ASVS v5.0.0-V16.3 | ST10-53 |
| TH10-45 | TB9/TB8 | I | During the pull window a running process exfiltrates through open outbound | L | H | All model services stopped before the window; worker must be stopped; 120-minute cap | ASVS v5.0.0-V13.2 | ST10-51 |
| TH10-46 | TB9 | D | Registry or hub unavailable blocks recovery | M | M | Two previous pinned sets stay on disk; rollback needs no download | SLSA; ENG §5.6 | ST10-52 |
| TH10-47 | TB8/TB6 | I/S | A model-client `base_url` or a redirect points the loopback client at a non-loopback host, sending prompts and the bearer key off the machine without the egress guard | L | H | `loopback_http_client` checks `base_url` at construction and every request host in `LoopbackOnlyTransport`; no redirects; no env proxies (U10-59, R-06) | ASVS v5.0.0-V12.3 | ST10-54 |
| TH10-48 | TB6 | I | A vendor SDK (Snowflake, `pymongo`, `msal`) connects to a host the operator never listed, for example one derived from a tampered `base_url` or returned by the service | L | H | Socket-guard allowlist = source `hosts` lists ∪ egress allowlist ∪ loopback; nothing derived from `base_url` for SDK sources; C20 requires `hosts` for SDK sources (U10-21, U10-58, R-06) | ASVS v5.0.0-V13.2 | ST10-55 |
| TH10-49 | TB6 | I | Chat `cloud` mode sends aggregated evidence to a cloud model in `hybrid` without the recorded D5 approval for chat | M | H | `security.data_policy.chat_approved` (file-only); C11, C25; `cloud_chat_allowed`; guard step 2 `chat_not_approved` (U10-03, U10-51, U10-107, R-38) | LLM02; ASVS v5.0.0-V13.2 | ST10-56 |
| TH10-51 | TB6 | I/S | A source connector's client sends credentials to a host outside the source's allowlist (response-supplied next link, redirect, absolute URL, tampered `base_url`) or over TLS without verification | M | H | `source_http_client` checks every request host against the source `hosts` list and the process allowlist, refuses redirects, non-`https` and user info, and cannot disable TLS verification (U10-110, R-06) | ASVS v5.0.0-V12.2, ASVS v5.0.0-V12.3 | ST10-58, ST10-59 |
| TH10-52 | TB9 | T | A tampered or unsigned DuckDB extension is installed, or the install downloads one from the network | L | H | Extension only from the verified bundle (U10-87 provenance), SHA-256 pinned in `deploy.release.duckdb_extensions`, local `INSTALL` with unsigned extensions refused, re-hash of the installed file (U10-112) | SLSA Build L2; ASVS v5.0.0-V15 | ST10-60 |
| TH10-50 | TB10 | I | A deleted record survives in agent memory (memory items, memory vectors, FTS index) and is recalled into later prompts | M | H | Deletion step 3b `MemoryStore.purge(record_id)`, repeated after the second lake pass (U10-99, R-54) | ASVS v5.0.0-V14.2; LLM08 | ST10-57 |

### 7(c) ASVS 5.0 mapping (Level 2)

Requirement numbers are cited at section level because this spec does not confirm individual requirement numbers against the official text (ENG §5.2).

| ASVS section | Requirement area | Control in this spec | Units | Tests |
|--------------|------------------|----------------------|-------|-------|
| V11.1 Cryptographic inventory and documentation | Algorithms and keys are listed | Inventory: HMAC-SHA256 pseudonyms (`redact.hmac_key`), HMAC-SHA256 `user_ref` (`ui_user_ref_key`), SHA-256 for hashes, chains, config and content hashes; TLS via the standard library; Sigstore via `gh` | U10-44, U10-60, U10-11 | UT10-39 |
| V11.2 Secure cryptography implementation | Vetted libraries, no custom crypto | `hashlib`, `hmac`, `secrets`, `ssl` only; constant-time comparison where a MAC is compared (`hmac.compare_digest` in U10-62 and U10-88) | U10-44, U10-62, U10-88 | UT10-59 |
| V11.3 Encryption algorithms | Approved algorithms | Data at rest encrypted by BitLocker (doctor); no application-level encryption | U10-90 | ST10-49 |
| V11.4 Hashing | Approved hash functions | SHA-256 everywhere; no MD5/SHA-1 for security | all | ruff `S324` |
| V11.5 Random values | CSPRNG | `secrets.token_hex(32)` for keys; ULIDs from T00-05 (herness.core.ids.new_ulid) | U10-68, U10-71 | UT10-72 |
| V11.6 Public key cryptography | Signature verification | Sigstore attestation verification of releases | U10-87 | ST10-22 |
| V12.1 General TLS | TLS ≥ 1.2, current ciphers | `ssl.create_default_context` with minimum TLS 1.2 | U10-52 | ST10-39 |
| V12.2 HTTPS to external services | Certificate verification, no downgrade | Verification on, HTTPS and 443 only, no env proxies; source clients always verify TLS | U10-51, U10-52, U10-110 | ST10-09, ST10-39, ST10-59 |
| V12.3 Service-to-service | Internal services | Loopback-only model servers with bearer keys; `loopback_http_client` refuses non-loopback hosts | U10-59, U10-80 | UT10-74, ST10-28, ST10-54 |
| V13.1 Configuration documentation | Documented config and secure defaults | This spec §9; defaults: `local`, egress off, loopback | U10-02–U10-07, U10-93 | UT10-76 |
| V13.2 Backend communication configuration | Least privilege, allowlisted outbound | Egress allowlist; socket guard over explicit source `hosts` lists; chat approval gate; firewall | U10-51, U10-58, U10-81, U10-107 | ST10-07, ST10-08, ST10-47, ST10-55, ST10-56 |
| V13.3 Secret management | Secrets in a vault, not in code or config | Credential Manager backend; `secret:` references; env file only for containers; rotation via `secrets set` | U10-27–U10-33, U10-79 | ST10-04, ST10-26, ST10-34 |
| V13.4 Unintended information leakage | No debug or metadata leakage | `config show` references only; errors name identifiers only; Streamlit usage stats off | U10-12, U10-95 | ST10-16 |
| V14.1 Data protection documentation | Classified data | §7(f) | — | — |
| V14.2 General data protection | Minimisation, retention, deletion, no sensitive data in logs | Redaction; retention purge; privacy deletion including memory and ops-store scrubs; ACLs; BitLocker | U10-42, U10-98, U10-99, U10-90 | IT10-06, UT10-68, ST10-50, ST10-57 |
| V14.3 Client-side data protection | Browser storage | Not applicable here (impl 09) | — | — |
| V16.1 Security logging documentation | Event inventory | §8 | — | — |
| V16.2 General logging | Structured, UTC, no sensitive data | structlog JSON, scrubber | U10-32 | ST10-15 |
| V16.3 Security events | Log admin actions, auth decisions, egress, config changes | Audit events and egress log | U10-60, U10-57 | ST10-17, ST10-40 |
| V16.4 Log protection | Tamper evidence, access control | Hash chain, ACL, backup | U10-62 | ST10-17 |
| V16.5 Error handling | Fail securely, generic messages | Fail closed in redaction, egress and audit; messages without values | U10-42, U10-51, U10-60 | FT10-01, FT10-07, FT10-08 |

### 7(d) LLM Top 10 and AI RMF

| Item | Control here | Tests |
|------|--------------|-------|
| LLM01 Prompt injection | Supplies redaction and the untrusted-data stance (design 10 §9.1); delimiting with `<untrusted_data source=... record_id=...>` is T05-15 (herness.harness.tools.wrap_untrusted) and T07-06 (herness.harness.memory.render.wrap_untrusted) (R-20) | owner tests |
| LLM02 Sensitive information disclosure | Redaction, egress re-scan, socket guard, scrubber, chat approval gate | ST10-10, ST10-11, ST10-14, ST10-56 |
| LLM03 Supply chain | Digest and revision pinning, hash checks, release attestation | ST10-19–ST10-24 |
| LLM04 Data and model poisoning | Weight hashes; label re-key without loss | ST10-20, IT10-07 |
| LLM05 Improper output handling | Response size cap | ST10-41 |
| LLM06 Excessive agency | `model_download` unreachable from jobs; DuckDB lockdown is T05-13 (herness.harness.warehouse.open_warehouse) | ST10-35 |
| LLM07 System prompt leakage | Prompts hold no secrets (scrubber is the backstop) | ST10-14 |
| LLM08 Vector and embedding weaknesses | Embeddings from redacted text; deletion purges enrichment vectors (T03-34 (herness.enrich.purge_record)) and memory vectors (T07-26 (herness.harness.memory.MemoryStore.purge), R-54) | IT10-06, ST10-57 |
| LLM09 Misinformation | Not applicable to this component | — |
| LLM10 Unbounded consumption | Token caps per request and day | ST10-12 |

AI RMF: Govern (data-policy gate, audit trail of config and admin actions), Manage (egress blocks fall back to local models; rekey and upgrade are planned, audited operations).

### 7(e) Secrets used

| Name | Resolved by | Used in |
|------|-------------|---------|
| `redact.hmac_key`, `redact.hmac_key.next` | U10-45, U10-101 | Redaction and rekey |
| `ui_user_ref_key` | impl 09 via U10-28 | `user_ref` HMAC (audit actor) |
| `vllm.api_key`, `OPENJEV_API_KEY` (reference `secret:OPENJEV_API_KEY`, R-53; also used by impl 08 health checks) | U10-79, U10-84, U10-91 | Env file for containers; health checks |
| `anthropic.api_key`, `TYPESAFE_API_KEY`, source credentials | impl 05, impl 03, impl 01 via U10-28/U10-29 | HTTP headers only |

### 7(f) Data classification

| Data | Class |
|------|-------|
| Config values and snapshots | internal |
| Secret values (Credential Manager, env file) | confidential |
| Redacted text, pseudonym tokens | personal (pseudonymised) |
| Name directory, display names file | personal |
| Egress log lines | internal |
| Audit lines (`actor` is a `user_ref`) | personal (pseudonymised) |
| `deletion_request` rows | internal |
| Backups | confidential (contain all of the above) |
| Deploy history, manifests, image digests | internal |
| Model weights and images | public |

### 7(g) Accepted residual risks

| ID | Risk | Reason | Owner |
|----|------|--------|-------|
| R-1 | Names absent from the directory are not masked when `ner: none` | Presidio is too slow and too noisy for bulk (design 10 §5.3) | Data protection lead |
| R-2 | Containers run as root with `ipc: host` | Required by vLLM; mitigated by loopback ports and firewall; hardening is delta D10-10 | Platform admin |
| R-3 | A local process holding a model port before the container starts receives traffic | Single-user service host; doctor warns | Platform admin |
| R-4 | Members of the WSL `docker` group can read container env with `docker inspect` | Only `svc-herness` is in the group | Platform admin |
| R-5 | Weights changed after pull are detected only on the next full verify (pull or rollback) | Full hashing takes minutes; doctor checks presence and size | Platform admin |
| R-6 | Installed plugins run with full process rights | Plugins come only from the attested, SBOM-listed environment | Security lead |
| R-7 | Streaming responses do not report provider token usage; the daily cap uses the request estimate | Provider-specific SSE parsing is out of scope | Security lead |

## 8. Observability

### 8.1 Log events

| Event | Level | Fields | When |
|-------|-------|--------|------|
| `config.load.completed` | INFO | `profile`, `config_hash`, `duration_ms` | U10-09 success |
| `config.load.failed` | ERROR | `profile`, `issue_count` | U10-09 failure (at CLI entry) |
| `config.validate.issue` | WARNING | `severity`, `path`, `file` | each warn issue at load |
| `config.cache.replaced` | WARNING | `profile` | second `init_config` |
| `config.change.recorded` | INFO | `old_hash`, `new_hash`, `changed_count` | U10-63 |
| `registry.plugin.loaded` | WARNING | `entry_point`, `distribution`, `version` | U10-25 |
| `registry.plugin.failed` | ERROR | `entry_point`, `error_type` | U10-25 |
| `secrets.resolve.failed` | ERROR | `name`, `backend` | U10-28 |
| `secrets.backend.unavailable` | ERROR | `backend` | U10-28, U10-33 |
| `redact.directory.loaded` | INFO | `names`, `variants`, `duration_ms` | U10-45 |
| `redact.directory.missing` | WARNING | none | U10-38 |
| `redact.record.failed` | WARNING | `record_id` (when known), `error_type` | U10-46, U10-47 |
| `redact.table.completed` | INFO | `rows`, `failed_rows`, `workers`, `duration_ms` | U10-47 |
| `redact.table.failed` | ERROR | `error_type` | worker crash |
| `egress.guard.installed` | INFO | `profile`, `allowed_hosts_count` | U10-58 |
| `egress.call.allowed` | INFO | `egress_id`, `purpose`, `payload_class`, `destination`, `bytes_out`, `tokens_in`, `run_id`, `task_id` | U10-51 |
| `egress.call.blocked` | WARNING | `egress_id`, `reason`, `purpose`, `destination`, `scan_hits` | U10-51 |
| `egress.call.completed` | INFO | `egress_id`, `status_code`, `latency_ms`, `bytes_in` | U10-54 |
| `egress.socket.blocked` | WARNING | `host`, `event` | U10-58 |
| `egress.loopback.blocked` | WARNING | `host` | U10-59 |
| `egress.source.blocked` | WARNING | `source`, `host`, `reason` | U10-110 |
| `egress.download_window.opened` / `closed` | INFO | `actor` | U10-55 |
| `audit.write.failed` | ERROR | `event`, `error_type` | U10-60 |
| `admin.cmd.completed` | DEBUG | `argv0`, `returncode`, `duration_s` | U10-76 |
| `deploy.render.completed` | INFO | `env_file`, `keys` | U10-79 |
| `deploy.pull.image_verified` | INFO | `service`, `digest` | U10-82 |
| `deploy.pull.weights_verified` | INFO | `gpu_class`, `model`, `revision`, `files` | U10-83 |
| `deploy.pull.failed` | ERROR | `step`, `reason` | U10-82, U10-83 |
| `deploy.firewall.window_opened` | WARNING | `max_minutes` | U10-81 |
| `deploy.firewall.window_closed` | INFO | `duration_s` | U10-81 |
| `deploy.firewall.restore_failed` | CRITICAL | none | U10-81 |
| `deploy.class.requested` | INFO | `gpu_class`, `via` (`worker`\|`direct`) | U10-84 |
| `deploy.class.failed` | ERROR | `gpu_class`, `reason` | U10-84 |
| `deploy.install.verified` | INFO | `version`, `wheel_sha256` | U10-87 |
| `deploy.install.refused` | ERROR | `reason`, `file` | U10-87 |
| `maintenance.backup.completed` / `failed` | INFO / ERROR | `files`, `bytes`, `duration_ms` / `error_type` | U10-96 |
| `maintenance.purge.completed` | INFO | per-kind counts, `dry_run` | U10-98 |
| `privacy.delete.step_completed` | INFO | `request_id`, `step`, `counts` | U10-99 |
| `privacy.delete.step_failed` | ERROR | `request_id`, `step`, `error_type` | U10-99 |
| `privacy.delete.completed` | INFO | `request_id` | U10-99 |
| `rekey.step.completed` / `rekey.failed` | INFO / ERROR | `step`, `old_key_id`, `new_key_id` / `step`, `error_type` | U10-101 |
| `log.scrub.failed` | ERROR | none | U10-32 |

Component name for all events: `security` (core modules) or `admin` (`herness/admin`).

### 8.2 Metrics (via `T08-05 (herness.store.ops.metrics.record_metric_samples)`, ENG E5, R-12)

| Metric | Type | Labels |
|--------|------|--------|
| `herness_config_load_seconds` | histogram | `profile` |
| `herness_redact_records_total` | counter | `result` (`ok`, `failed`) |
| `herness_redact_spans_total` | counter | `type` |
| `herness_redact_seconds` | histogram | none |
| `herness_egress_calls_total` | counter | `decision`, `purpose`, `reason` |
| `herness_egress_tokens_total` | counter | `direction` (`in`, `out`), `destination` |
| `herness_egress_bytes_total` | counter | `direction` |
| `herness_socket_blocked_total` | counter | `event` |
| `herness_audit_lines_total` | counter | `event` |
| `herness_audit_write_failures_total` | counter | none |
| `herness_maintenance_backup_seconds` | histogram | none |
| `herness_maintenance_purged_total` | counter | `kind` |
| `herness_privacy_deletions_total` | counter | `status` |
| `herness_privacy_scrubbed_rows_total` | counter | `table` (`evidence`, `finding`, `memory_item`) |
| `herness_deploy_pull_seconds` | histogram | none |

### 8.3 Trace events

This component writes no trace events (T05-11 (herness.harness.tracing.Tracer) is the only trace writer). `EgressBlocked` carries `egress_id` and `reason`, which T08-09 (herness.core.resilience.ModelChain) copies into its `fallback` trace event (reason `egress_blocked`). T05-11 (herness.harness.tracing.Tracer) applies U10-32 to trace events.

### 8.4 Health

No long-running component of this spec needs `health()`; U10-89–U10-91 are the doctor checks.

## 9. Configuration

Keys owned here (all in `config/herness.yaml` or profile overlays; `security.*` is file-only). "Restart" means a process restart is needed to take effect.

| Key | Type | Default | Validation | Restart | Sensitivity |
|-----|------|---------|-----------|---------|-------------|
| `profile` (via `--profile`/`HERNESS_PROFILE`) | `ProfileName` | `local` | gate (U10-09) | yes | internal |
| `paths.data`, `paths.logs`, `paths.backup_target` | path | `data`, `data/logs`, `E:/herness-backup` | U10-02 | yes | internal |
| `security.data_policy.*` (`hybrid_approved`, `premium_approved`, `chat_approved` (R-38), `approved_by`, `approved_on`) | bool, str, date | false, null | gate (U10-09), C11, C25 | yes | internal |
| `security.secrets.backend` | `keyring`\|`dotenv` | `keyring` | C12 | yes | internal |
| `security.redaction.*` | U10-04 | U10-04 | U10-04, C14, C23 | yes (changes `config_hash`) | internal (directory path points at personal data) |
| `security.egress.*` | U10-03 | disabled | U10-03, C04, C11, C24 | yes | internal |
| `security.network.extra_allowed_hosts`, `http_proxy` | U10-03 | `[]`, null | C04, C20 | yes | internal |
| `security.ui.*` | U10-05 | loopback 8501 | C05, C21 | yes | internal |
| `logging.level` | enum | `INFO` | U10-06 | yes | internal |
| `retention.*` | int | U10-06 | U10-06 | no (read per purge) | internal |
| `backup.*` | U10-06 | U10-06 | U10-06 | no | internal |
| `deploy.*` (incl. `deploy.release.*`) | U10-07 | design 10 §7.4 | U10-07, C07, C08a, C09, C10, C13 | re-render and `deploy up` | internal |

Keys read from other owners: `models.models.{roles, fallback, clients}` including `clients.*.context_window` (impl 05; C10, R-52), `resilience.resilience.gpu.*` (impl 08), `sources.sources.*.base_url`, `enabled`, `account`, `auth.method` and `sources.sources.<name>.hosts` (impl 01; socket guard and C20, R-06), `sources.dq.*` and `sources.build.threads` (impl 02, composed per R-69), `models.deciders` (impl 03, composed per R-76), `memory.injection_patterns` (impl 07, produced by the loader). Environment variables: `HERNESS_PROFILE`, `HERNESS_ENV`, `HERNESS_SYNTH_CONFIG`, `HERNESS_SECRET__*` (dev only), `HERNESS_WORKER` (set by impl 08).

## 10. Performance and capacity

| ID | Target (design 10 §8) | Dataset and hardware | Pass threshold |
|----|------------------------|----------------------|----------------|
| BT10-01 | `load_config` + offline validation | shipped templates plus owner defaults; dev box (16 cores) | p95 < 1 s over 20 runs |
| BT10-02 | `config_hash` | same config, `key_id` given | p95 < 50 ms; identical across Windows and Linux CI |
| BT10-03 | Redaction throughput | spec 11 corpus replayed to 100k records, avg 1.5 KB, 100k-name directory; one core | ≥ 5,000 records/s |
| BT10-04 | Bulk redaction | 6M records synthetic, 16 workers (marker `nightly`) | < 5 min |
| BT10-05 | `Redactor.scan` in egress | 1 MB JSON evidence pack | < 50 ms per MB |
| BT10-06 | Socket hook overhead | 10,000 loopback connects with and without the hook | < 20 µs mean difference per connect |
| BT10-07 | Doctor | target PC, reasoning class loaded (marker `deploy`) | < 60 s |
| BT10-08 | Nightly ops backup | 1 GB ops store fixture with concurrent writers | < 2 min |

Enforced limits: config file 5 MiB; `--set` value 4,096 chars; redaction text 4,000,000 chars; redaction chunk 20,000 rows; worker count = `sources.build.threads`; egress body `max_request_bytes`; response 50 MiB; JSON leaf 1,000,000 chars; audit field 256 chars; doctor thread pool 8; subprocess timeouts per unit; firewall window 120 minutes.

## 11. Test specification

Common fixtures (in `tests/support/`, impl 11 layout): `tmp_config` (copies the repository `config/` templates plus minimal owner sections into `tmp_path`), `fake_keyring` (an in-memory `keyring` backend set with `keyring.set_keyring`), `frozen_now` (`freezegun` at `2026-09-24T12:00:00Z`), `reset_core` (calls `reset_config`, `reset_registry` and `reset_owner_validators` after each test), `fake_runner` (replaces U10-76 `run_cmd` with a table of argv prefix → `CmdResult`; the only permitted OS-level mock, ENG §6), `fake_socket` (monkeypatched `socket.socket.connect` recording calls), `respx` routes for `api.anthropic.com`. Test files: `tests/unit/test_config_*.py`, `test_registry.py`, `test_secrets.py`, `test_redact_*.py`, `test_egress_*.py`, `test_audit.py`, `test_admin_*.py`, `tests/integration/test_security_*.py`, `tests/fault/test_security_faults.py`, `tests/security/test_st10_*.py`, `tests/bench/test_bt10.py`.

### 11.1 Unit tests (marker `unit`)

| ID | Unit / flow | Setup | Action | Expected |
|----|-------------|-------|--------|----------|
| UT10-01 | U10-09 file < profile | `tmp_config`; key set differently in `herness.yaml` and `profiles/local.yaml` | load | profile value wins; `paths.*` absolute |
| UT10-02 | U10-18 profile < env | env `HERNESS_LOGGING__LEVEL=DEBUG` | load | `DEBUG` |
| UT10-03 | U10-09 env < CLI | env and `--set logging.level=ERROR` | load | `ERROR` |
| UT10-04 | merge rules | overlay with a map and a list | load | maps merged key by key; lists replaced |
| UT10-05 | U10-16 files | remove `sources.yaml`; separately remove `eval.yaml` | load | `ConfigError("config file missing: sources.yaml")`; `cfg.eval is None` |
| UT10-06 | U10-16, U10-08 | unknown root key; unknown nested field | load | `ConfigError` naming the key path, no value echoed |
| UT10-07 | U10-15 | duplicate key at line 7 | load | message contains `duplicate key` and `:7` |
| UT10-08 | U10-15 | anchor and alias in `metrics.yaml` | load | `ConfigError` "anchors and aliases" |
| UT10-09 | U10-17, U10-09 | gate set; `--profile hybrid` | load | `cfg.profile == "hybrid"`; `models.models.roles.writer == "claude-opus"` |
| UT10-10 | U10-18, U10-09 | `HERNESS_SECURITY__EGRESS__ENABLED=true`; separately `--set security.egress.enabled=true` and `--set profile=hybrid` | load | `ConfigError` "file-only" each |
| UT10-11 | U10-09 gate | `hybrid` without `hybrid_approved`; `premium` without `approved_on` | load | `ConfigError` "requires recorded approval" |
| UT10-12 | U10-09 step 6, U10-17 | `local` with egress enabled in `herness.yaml`; `synth` overlay enabling egress | load | `ConfigError` each; `local` and `synth` defaults have empty egress lists |
| UT10-13 | U10-17 | overlay sets `security.data_policy.hybrid_approved` | load | `ConfigError` |
| UT10-14 | U10-17 | `HERNESS_SYNTH_CONFIG` with `mappings` only; with `sources`; with profile `local` | load | merged; `ConfigError`; `ConfigError` |
| UT10-15 | U10-11 | same config with reordered keys and different `logging.level` | hash twice | equal; matches `^cfg_[0-9a-f]{16}$` |
| UT10-16 | U10-11 | change one weight in `weights.yaml` | hash | differs |
| UT10-17 | U10-11 | fake keyring with sentinel values | capture canonical JSON input | contains `secret:` references; no sentinel |
| UT10-18 | U10-12 | raw dict with `password: plain` bypassing validation | `effective_dict` | value `***`; references unchanged |
| UT10-19 | U10-20, U10-03–U10-07, U10-109 | one parametrised case per C-rule (C01–C25, including C05 loopback bind and proxy, C10 context window, C11 and C25 chat approval, C20 SDK `hosts`) and per model validator (patterns, ports, pins); fake owner validators registered through U10-109 | `validate` | exactly the expected issue with severity and path |
| UT10-20 | U10-65, U10-14 | configs with an error, only a warning, clean | `cmd_config_validate` with and without `strict` | exit 1 / 0 without strict and 1 with strict / 0 (R-46); issue line format `severity path file: message` |
| UT10-21 | U10-63 | two starts with the same hash, then a changed hash | `record_config_change` ×3 | one `config_change` line after the change, `changed_paths` lists the key, snapshot file exists, `LAST` updated |
| UT10-22 | U10-10 | none | `get_config` twice, `reset_config`, `get_config` | same object, then a new object; reset hooks called |
| UT10-23 | U10-19 | `a.b`, `a.b.c=1` after `a.b=2`, `bad seg=1`, 13 segments | parse | `ConfigError` each with key path |
| UT10-24 | U10-21 | sources with enabled and disabled `base_url`s; an enabled `snowflake` source with `base_url` and `hosts: [acme.snowflakecomputing.com]`; a `servicenow` source with `hosts: [sso.example.com]` | `load_bootstrap` | hosts of enabled sources only, lower-cased; the Snowflake `base_url` host is absent and its `hosts` entry present; the ServiceNow `base_url` host and its `hosts` entry both present (R-06) |
| UT10-25 | U10-23, U10-24, U10-26 | `_BUILTINS` row pointing at a test module | `get` | lazy import; same object on second call |
| UT10-26 | U10-24, U10-23 | unknown name; conflicting registration | `get`; `register` | `ConfigError` listing `available`; `ConfigError` duplicate |
| UT10-27 | U10-25 | test entry point in a fixture distribution | `available` | name listed; `registry.plugin.loaded` logged |
| UT10-28 | U10-28, U10-30, U10-34 | fake keyring | resolve present, missing; `exists` | `SecretStr`; `ConfigError("secret not found: x")` without value; bool |
| UT10-29 | U10-29 | JSON object, JSON list, invalid JSON | `resolve_json` | dict of `SecretStr`; `ConfigError` twice; members in `known_values` |
| UT10-30 | U10-27 | `secret:Foo.Bar`, `OPENJEV_API_KEY`, `x`, `secret:bad name` | parse, pydantic types | names lower-cased; errors for invalid |
| UT10-31 | U10-33 | `.env` with `HERNESS_SECRET__VLLM_API_KEY` | dotenv backend with `HERNESS_ENV=dev`; without | resolves `vllm.api_key`; `ConfigError` |
| UT10-32 | U10-33 | 3,000-char value | set, get, delete | 3 chunks written; reassembled equal; all removed |
| UT10-33 | U10-31, U10-69 | fake keyring, audit dir | set value of 7 chars; valid value | `ConfigError`; audit line `secret_set` with name only, then stored |
| UT10-34 | U10-32 | resolved sentinel | log an event containing it in nested dict and list | output has `***` |
| UT10-35 | U10-32 | event with `password=abc123xyz` and a SAS URL | process | `[SECRET]` in both |
| UT10-36 | U10-36, U10-41 | table of positive and negative strings per type | `scan` | expected spans and types |
| UT10-37 | U10-41 | email inside a URL with `token=`; card inside a credential | `scan` | earlier type wins; no overlap |
| UT10-38 | U10-36 | `INC0012345`, `2026-09-24 21:14`, `10.1.2.3`, `+1 415 555 0142` | `scan` | only the phone is detected |
| UT10-39 | U10-44, U10-37 | `Jane Doe`, `Doe, Jane`, `JANE  DOE` with directory | `redact` | one token for all three; format `[PERSON_<10 hex>]` |
| UT10-40 | U10-40, U10-45 | two keys | pseudonym same value | different tokens; `key_id` = SHA-256 prefix; 31-byte key → `ConfigError` |
| UT10-41 | U10-42 | `mask_ip` false | redact text with an IP | IP kept; `scan` still reports it |
| UT10-42 | U10-42, U10-46 | `None`, `""`, detector raising | `redact`, `redact_text` | `None`; empty result; `redact_text` returns `None` and logs |
| UT10-43 | U10-47, U10-43, U10-48 | table with 2 text columns, one poisoned row | `redact_table(workers=1)` | joined text; poisoned row NULL; `failed_rows=1` |
| UT10-44 | U10-47 | 50k rows | `workers=1` vs `workers=4` | identical output |
| UT10-45 | U10-41 | text containing `[PERSON_0123456789]` and `[SECRET]` | `scan` | tokens not detected (digit-only hex not a phone) |
| UT10-46 | U10-38, U10-39 | directory CSV with alt names; `extra_names` single token | `find`, `update_display_names` | variants match word-bounded; single first names only when listed; display file sorted, count returned |
| UT10-47 | U10-49 | fixture tree with allowed synthetic values; one real-looking email; a denylisted domain | `main(["--scan", dir])` | exit 0 for allowed-only tree; exit 1 with `path:line:col TYPE` lines and no values |
| UT10-48 | U10-51 step 1, U10-56 | profile `local` | `check` | `EgressBlocked` reason `profile_forbids_egress`; blocked line and audit line |
| UT10-49 | U10-51 step 2, U10-50 | hybrid; purpose `reasoning`; payload `redacted_text` | `check` | `purpose_not_allowed`; `payload_class_not_allowed` |
| UT10-50 | U10-51 step 3 | hybrid | URLs `http://api.anthropic.com`, `:8443`, `https://1.2.3.4`, `https://u:p@api.anthropic.com`, `https://evil.com` | the five reason codes |
| UT10-51 | U10-51 step 4 | hybrid | body of `max_request_bytes + 1` | `body_too_large` |
| UT10-52 | U10-51 step 5, U10-57, U10-52 | egress log with 2,999,000 tokens today | 2,000-token request; oversized per-request estimate | `tokens_per_day`; `tokens_per_request`; client config asserted (`follow_redirects` false, `trust_env` false) |
| UT10-53 | U10-51 step 6 | bodies with raw email; `password=...`; only pseudonym tokens | `check` | blocked `pii_detected` with counts; blocked; allowed |
| UT10-54 | U10-54, U10-57 | respx 200 JSON with `usage` | send via client | `allowed` then `completed` line, same `egress_id`, provider tokens |
| UT10-55 | U10-55 | `allow_download` false; `HERNESS_WORKER=1`; profile synth; valid | enter window | `EgressBlocked` ×3; valid window allows `huggingface.co` `model_download` and audits |
| UT10-56 | U10-58 `SocketPolicy` | policy with one allowed host | check getaddrinfo/connect for loopback, allowed, resolved IP, other | allow, allow, allow, `EgressBlocked` |
| UT10-57 | U10-60, U10-61 | empty logs dir, then a previous-day file | write lines on two dates | first `prev_hash` 64 zeros; next-day first line chains to previous day's last line; canonical JSON |
| UT10-58 | U10-60 | fields with unknown key; invalid actor; value containing a known secret | `audit` | `SchemaViolation` each; nothing written |
| UT10-59 | U10-62 | valid chain; edited line; deleted line; swapped lines; truncated tail | `verify_chain` | `ok`; break at the right `file:line` for each |
| UT10-60 | U10-79 | `fake_runner`, fake keyring | `render_env` | stdin content has exactly the listed keys, sorted; secrets only in stdin; `rendered.json` hash; audit line; value with newline → `ConfigError` |
| UT10-61 | U10-07 | pins with placeholder, tag instead of digest, 39-hex revision | deploy-time validation | `ConfigError("deploy.<key> is not pinned")` each |
| UT10-62 | U10-82, U10-83 | `fake_runner` returning RepoDigests and `sha256sum` output | `pull_images`, `verify_weights(full=True)` | ok on match; `FatalError` on mismatch |
| UT10-63 | U10-81, U10-92 | `fake_runner`; body raises | enter window | `Allow` then `Block` set and read back; exception re-raised |
| UT10-64 | U10-84 | worker alive (fake impl 08) vs no worker | `class_up("reasoning")` | alive: requested class set, no compose call; not alive: GPU lock taken, other classes stopped, `up -d` argv, health polled; audit and history written |
| UT10-65 | U10-85, U10-78 | history with 4 pin sets | `prune`; `rollback_class` without local image | removes only non-kept digests/revisions; `ConfigError` "run deploy pull" |
| UT10-66 | U10-87 | `fake_runner` for `gh`; SBOM predicate fixtures | `verify_bundle` | exact `gh` argv (repo, signer workflow, `--deny-self-hosted-runners`, trusted root, predicate types); summary on success |
| UT10-67 | U10-97, U10-96 | 60 daily dates | `select_backup_keep(14, 8)` | 14 newest plus newest per last 8 ISO weeks |
| UT10-68 | U10-98, U10-104, U10-74, U10-75 | fixture data tree with old and new files | `handle_maintenance({"action": "purge", "dry_run": true})` then `false` | counts equal; files older than each retention deleted; today's logs kept; audit counts |
| UT10-69 | U10-99, U10-104 | fake impl 02, impl 03 and `memory_purge` functions; step 4 raising once | run, then rerun | request `running` with step 4 `failed`; rerun skips steps 1–3b and completes; `memory_purge` called with the `record_id` after step 3 and again in step 7 when pass 2 removed rows; continuation job enqueued at step 5 with GPU class `none` |
| UT10-70 | U10-101–U10-103 | fixture warehouse and labels (one ambiguous hash) | run | map written; labels re-keyed; counts `ambiguous=1`; key swapped; marker written |
| UT10-71 | U10-89–U10-91, U10-76, U10-77 | recorded outputs of `icacls`, `manage-bde`, `wsl -l -q` (UTF-16LE), `nvidia-smi`, `w32tm`, PowerShell JSON; psutil fake | each check | PASS/WARN/FAIL per fixture variant; unsafe argv rejected |
| UT10-72 | U10-68, U10-70, U10-64, U10-34 | fake keyring; prompts answering `ESCROWED` and `no` | `cmd_secrets_init`; `cmd_secrets_status` | key created only when escrowed; status lists names, presence and last-set time from audit |
| UT10-73 | U10-71 | `.next` present; absent | `cmd_secrets_rekey` | exit 1 "already staged"; stages and calls fake `schedule_rekey`, audit `redact_rekey` |
| UT10-74 | U10-59 | none | `loopback_http_client("http://127.0.0.1:8000", timeout_s=5.0)` and `aloopback_http_client` with the same arguments; a `get("/health")` on each; both functions with `"http://10.0.0.5:8000"`; `timeout_s=0` | clients built with `follow_redirects` false and `trust_env` false, requests reach the transport; `EgressBlocked` with `reason="not_loopback"` before any client exists (both variants); `ConfigError` |
| UT10-75 | U10-80, U10-20 C08a | repository `docker/compose.yaml`, impl 08 default `resilience.yaml` | cross-check | no issues; altering a port gives C08a error |
| UT10-76 | U10-93–U10-95 | repository templates | load `local`, `synth`, `hybrid` | `local`/`synth` load (C13 warnings only); `hybrid` fails the gate; `.env.example` has no values |
| UT10-77 | U10-105 | migrated temporary ops store (impl 02 migrations) | create twice for one `record_id`; `record_deletion_step` for `3` then `3b`; `set_deletion_status` `pending → done`; `get_deletion_request` of an unknown ID | one row (second create returns it); `steps` holds keys `3` and `3b`; `ConfigError("invalid deletion status transition")`; `NotFound` |
| UT10-78 | Removed (R-77) | — | — | the evidence scrub is tested by UT05-126 and the finding scrub by impl 06's tests of U06-144 |
| UT10-79 | U10-107, U10-51 step 2 | configs: `local`; `hybrid` without `chat_approved`; `hybrid` with it and `reasoning` in purposes; `premium` | `cloud_chat_allowed`; guard `check` with purpose `reasoning` in `hybrid` without approval | `False`, `False`, `True`, `True`; `EgressBlocked` with reason `chat_not_approved` |
| UT10-80 | U10-108 | none | construct `EgressBlocked("x", egress_id="egr_01", reason="host_not_allowed")`, `ConfigError("y", issues=[issue])`, and both without keywords | attributes set; defaults `None` and `()`; `str()` is the message only; taxonomy parents unchanged |
| UT10-82 | U10-110 | config with `servicenow` (`base_url` `https://corp.service-now.com`, `hosts: []`) and `snowflake` (`hosts: [acme.snowflakecomputing.com]`); fake socket policy | `source_http_client("servicenow", base_url, timeout_s=30.0)`; `source_http_client("snowflake", "https://acme.snowflakecomputing.com", timeout_s=30.0)`; disabled source; `timeout_s=0`; a response of `max_response_bytes + 1` from a respx route | clients built with `follow_redirects` false, `trust_env` false, the configured timeouts and limits; both allowed; `ConfigError` twice; `EgressBlocked` `response_too_large` |
| UT10-84 | U10-08, U10-16 | `sources.yaml` with `sources`, `dq` and `build`; `models.yaml` with `models` (including `roles`), `harness` and `deciders`; variants with an unknown top-level key in each file | load | `cfg.sources.dq`, `cfg.sources.build.threads`, `cfg.sources.enabled_sources()`, `cfg.models.models.roles` and `cfg.models.deciders` are populated from the owners' models (R-69, R-76); each unknown key → `ConfigError` naming the file and key |
| UT10-85 | U10-112 | fixture bundle with a small file as `duckdb/excel.duckdb_extension`; `fake_runner` for the venv interpreter returning recorded JSON | install with the right pin; with a wrong pin; with no pin; fake install whose `install_path` file differs | exact argv (`-I`, `EXT_INSTALL_SCRIPT`, path, `excel`); version returned and `deploy.install.extension_installed` logged; wrong pin → `FatalError` before any subprocess; no pin → `ConfigError`; differing installed file → `FatalError` |
| UT10-83 | U10-111 | ops store with requests for `servicenow:incident:*` in `pending`, `running`, `done`, `failed` and one for `jira:issue:*` | `deleted_record_ids("servicenow", "incident")`; invalid `source` | sorted IDs of the `running` and `done` requests only; `ConfigError` |
| UT10-81 | U10-109, F10-01 step 3a | fake owner validators: one returning a `ConfigIssue`, one a mapping, one an invalid object, one raising `RuntimeError("secret-ish text")`; a duplicate registration | `register_owner_validator`; `run_owner_validators`; start-up call with an `error` issue | issues converted and sorted; invalid object and exception each become one `error` issue whose message has no exception text; duplicate name with another object → `ConfigError`; start-up raises `ConfigError` with `issues` (exit 3) |

### 11.2 Property tests (marker `unit`, hypothesis)

| ID | Unit | Property |
|----|------|----------|
| PT10-01 | U10-11 | For random nested dicts inserted as a section, any key permutation gives the same hash |
| PT10-02 | U10-42 | `redact(redact(x).text).text == redact(x).text` for random text with injected PII |
| PT10-03 | U10-42, U10-41 | `scan(redact(x).text)` has no span of a blocking type |
| PT10-04 | U10-37 | `luhn_valid` equals a reference implementation for random digit strings |
| PT10-05 | U10-44 | Pseudonym is deterministic, 10 hex chars, and differs across types for the same value |
| PT10-06 | U10-19 | Round trip: generated key paths and YAML scalar values parse back to the same nested dict |
| PT10-07 | U10-58 | Random hostnames not in the allowlist are always blocked |
| PT10-08 | U10-62 | Any single-byte mutation of any line of a valid chain is detected |

### 11.3 Integration tests (marker `integration`)

| ID | Flow | Setup | Action | Expected |
|----|------|-------|--------|----------|
| IT10-01 | F10-01 | full `tmp_config` with owner defaults, profile `synth` | `validate(offline=True)` | no errors |
| IT10-02 | U10-41, U10-42 | impl 11 corpus `tests/unit/test_redaction_corpus.py` (≥ 2,000 labelled sentences; size and thresholds owned here and used by impl 11, R-56; phones in the form `+1-202-555-01xx`) | redact | recall ≥ 0.99 per patterned type; names ≥ 0.95; precision ≥ 0.90; no `INC`/`CHG` masked |
| IT10-03 | F10-03 | hybrid with gate; respx | adapter-like call with aggregated evidence | allowed and completed lines; no payload text in any log file |
| IT10-04 | F10-03, F10-04 | profile `local`; `fake_socket`; respx | every `http_client()` request | `EgressBlocked`; zero connects recorded |
| IT10-05 | U10-60 with impl 02 | fixture ops store | approve a `review_item` through `T02-07 (herness.store.ops.shared.decide_review_item)` (R-33) | exactly one `review_decision` line |
| IT10-06 | F10-13 | fixture lake, cache, labels, vectors, memory store (impl 07, memory items citing the record), evidence, findings, traces, build (fake impl 08 job completion) | privacy delete end to end, including a sync commit between steps 1 and 2 | record absent from all stores (memory included, R-54) and the next build; next fixture sync does not re-ingest; second lake pass removes the late row |
| IT10-07 | F10-10 | fixture labels (human, gold, teacher) | rekey job | every label mapped to its new hash; no loss |
| IT10-08 | F10-11 | ops store with a writer thread | backup | copy passes `integrity_check` |
| IT10-09 | F10-12 | data tree spanning 3 years | purge | per-key deletions exactly at the cut-offs |
| IT10-10 | secrets e2e | fake pipeline (fixture connector sync, one chat turn with T11-23 (tests.support.fake_llm.FakeLLMClient) from `tests/support/fake_llm.py`, R-65) with sentinel secrets | run and grep | see ST10-14 |
| IT10-11 | U10-65 via impl 09 | `typer.testing.CliRunner` | `herness config validate --offline --strict` on a clean, a warning-only and an error config | exit codes 0/1/1 (R-46); JSON output validates |
| IT10-12 | F10-09 | Linux CI | doctor | Windows checks WARN "not applicable"; others evaluate |
| IT10-13 | Phase 4 acceptance | target PC (marker `deploy`, manual plus scripted) | doctor; `deploy up` each class; `netstat -ano` | all PASS; each class healthy; only one GPU service running; loopback-only binds |
| IT10-14 | F10-07 | fixture bundle with a fake `gh` script returning recorded JSON; temp venv | `cmd_deploy_install` | wheel installed; audit `deploy_install`; `.release/<version>/` created |
| IT10-15 | U10-58 | subprocess running `install_socket_guard` in `local` | `socket.create_connection(("1.1.1.1", 443))` and `urllib.request.urlopen("https://example.org")` | both raise `EgressBlocked` |

### 11.4 Fault tests (marker `fault`)

| ID | Condition | Action | Expected |
|----|-----------|--------|----------|
| FT10-01 | Audit lock held by another process | `audit()` | `FatalError` after 10 s wait (clock advanced); the audited action (a secret set) did not happen |
| FT10-02 | Rekey killed after step 3, before 4a | rerun | old key active until the rerun swaps; hashes and labels unchanged before the rerun |
| FT10-03 | `os.replace` on a lake file raises `PermissionError` | privacy delete | request `running`, step 2 `failed`; retry completes |
| FT10-04 | Copy corrupted before `integrity_check` | backup | temp deleted; `FatalError`; no `ops-<date>.sqlite` |
| FT10-05 | Keyring raises `KeyringError` | resolve | `ConfigError` with hint; `secrets.backend.unavailable` |
| FT10-06 | Set `Block` fails twice | exit window | CRITICAL log; `FatalError` with manual command |
| FT10-07 | One redaction worker process killed | `redact_table` | `StoreBusy` raised; no partial table |
| FT10-08 | Egress log disk full (`OSError`) | `check` | `EgressBlocked("egress_log_failed")`; no socket opened |
| FT10-09 | Weight download exits non-zero | `run_pull` | firewall restored; nothing rendered; exit 1 |

### 11.5 Security tests (marker `security`; each attempts the attack)

| ID | Threat | Attack | Expected |
|----|--------|--------|----------|
| ST10-01 | TH10-02 | Env `HERNESS_SECURITY__EGRESS__ENABLED=true`, also via `.env` with `HERNESS_ENV=dev` | `ConfigError`; egress stays off |
| ST10-02 | TH10-02 | `--set security.egress.destinations=[evil.com]` | `ConfigError` |
| ST10-03 | TH10-03 | `profiles/hybrid.yaml` sets `data_policy.hybrid_approved: true` | `ConfigError` |
| ST10-04 | TH10-06 | `sources.yaml` with `password: hunter2hunter2` and `api_key_secret: "sk-live..."` | C16 errors |
| ST10-05 | TH10-04 | Billion-laughs YAML; 6 MiB file; duplicate `enabled` key | `ConfigError` each, within 1 s |
| ST10-06 | TH10-05 | `deploy.reasoning.model: "a/b;rm -rf /"`, revision with `$(id)`, distro with space | `ConfigError` at load or in `run_wsl`; no subprocess started |
| ST10-07 | TH10-16 | Profile `local`, client from `get_guard().http_client` to `api.anthropic.com` | `EgressBlocked`; zero connects |
| ST10-08 | TH10-15 | `socket.create_connection`, `http.client.HTTPSConnection`, `urllib.request` to a non-allowed host | `EgressBlocked` from the hook |
| ST10-09 | TH10-17 | `api.anthropic.com.evil.com`, `https://api.anthropic.com@evil.com`, `https://API.ANTHROPIC.COM:443/` (allowed), IPv6 literal, `http://` | blocked except the case-folded valid host |
| ST10-10 | TH10-14 | JSON body with `"john@corp.com"` and `"José García"` (directory name) | `pii_detected` |
| ST10-11 | TH10-14 | Full-width `ｊｏｈｎ＠ｃｏｒｐ．ｃｏｍ`; zero-width characters inside an email and a card | `pii_detected` |
| ST10-12 | TH10-20 | Streaming body; oversized body; cumulative day cap exceeded across two processes | blocked with the three reasons |
| ST10-13 | TH10-22 | Payload with marker `ZQX-UNIQUE-7731` allowed and blocked | marker absent from every file under `data/logs/` |
| ST10-14 | TH10-07 | IT10-10 run with sentinel secret values | zero hits in `data/logs/`, `data/traces/`, `data/config_snapshots/`, an `ops.sqlite` `.dump`, rendered reports |
| ST10-15 | TH10-07 | Exception whose message contains a resolved secret, logged with `exc_info` | `***` in output |
| ST10-16 | TH10-07 | `config show`, `effective_dict`, snapshot with sentinel secrets resolved | no sentinel |
| ST10-17 | TH10-08, TH10-09 | Edit a field, delete a line, reorder two lines, truncate a file | doctor `audit_chain` FAIL naming the line |
| ST10-18 | TH10-07 | `audit(..., detail=<known secret>)` | `SchemaViolation`; nothing written |
| ST10-19 | TH10-33 | Registry returns an image whose `RepoDigests` lacks the pin | `FatalError`; nothing rendered |
| ST10-20 | TH10-34, TH10-30 | One blob's content altered | `hash_mismatch:<file>` |
| ST10-21 | TH10-35 | GGUF byte flipped | `FatalError` |
| ST10-22 | TH10-36 | Wheel modified after signing (fake `gh` exits 1) | install refused; venv untouched |
| ST10-23 | TH10-36 | Attestation from another repo, another workflow, a self-hosted runner | refused each (argv carries the constraints; fake `gh` enforces them) |
| ST10-24 | TH10-37 | SBOM missing; SBOM not attested; component version differs from requirements; GPL component | refused each |
| ST10-25 | TH10-15 | AST scan of `herness/`, `app/`, `tools/` for `httpx.Client(`, `httpx.AsyncClient(`, `httpx.HTTPTransport(`, `httpx.AsyncHTTPTransport(`, `requests.`, `urllib.request`, `anthropic.Anthropic(`/`AsyncAnthropic(` without `http_client=`, outside `herness/core/egress.py` and `herness/core/egress_clients.py` (R-06; no connector allowance); vendor SDK constructors (Snowflake, `pymongo`, `msal`) are not flagged (their hosts are held by ST10-55); a planted violation in a temp module | planted violation fails; repository passes |
| ST10-26 | TH10-11 | `backend: dotenv` with profile `local`, no `HERNESS_ENV` | `ConfigError` |
| ST10-27 | TH10-12 | `security.ui.bind: 0.0.0.0` with and without expose; expose without `trusted_proxy`; `trusted_proxy: 10.0.0.9` | C05 errors each (R-50) |
| ST10-28 | TH10-26 | psutil fake: vLLM listening on `0.0.0.0:8000` | doctor `ports_loopback` FAIL |
| ST10-29 | TH10-24 | After install, `huggingface_hub`-style request to `huggingface.co` from Python | `EgressBlocked`; `HF_HUB_OFFLINE=1` set |
| ST10-30 | TH10-06 | Fixture with a real-format SSN `123-45-6789` and an email at a denylisted domain | scanner exit 1 |
| ST10-31 | TH10-42 | `--record-id "../../etc:x:y"`, `"servicenow:incident:*"`, 101 IDs | `ConfigError`; nothing enqueued |
| ST10-32 | TH10-17 | Allowed host responds 302 to `https://evil.com` | redirect not followed; no request to `evil.com` |
| ST10-33 | TH10-19 | Blocked call | audit `egress` line with `egress_id`, `reason`; no payload |
| ST10-34 | TH10-28 | `deploy.env_file: /mnt/d/herness/docker.env`; fake stat returns `644` | `ConfigError`; doctor `env_file` FAIL |
| ST10-35 | TH10-23 | `http_client("model_download", "none")`; window inside a worker process | `EgressBlocked` both |
| ST10-36 | TH10-01 | CLI as a viewer runs `secrets set`, `privacy delete`, `deploy up` (impl 09 CliRunner) | exit 11 (`PermissionDenied`, R-46); no keyring write, job or audit `admin_action` |
| ST10-37 | TH10-10 | `custom_patterns: {bad: "(a+)+$"}`; 600-char pattern | `ConfigError` |
| ST10-38 | TH10-13 | `icacls` output with `BUILTIN\Users:(RX)` and an `(I)` ACE | `acl_data` FAIL |
| ST10-39 | TH10-18 | Local TLS server with a self-signed cert added to destinations (test-only override of the CA), server offering only TLS 1.0 | certificate error; handshake failure; `trust_env` ignores `HTTPS_PROXY` |
| ST10-40 | TH10-19 | Transport instrumented to record order | `allowed` line written before the inner transport is called; `completed` after close |
| ST10-41 | TH10-21 | Response streaming 60 MiB | `EgressBlocked("response_too_large")` |
| ST10-42 | TH10-25 | No worker; unknown process listening on 8100 | doctor `ports_expected` WARN with process name |
| ST10-43 | TH10-27 | Compose lint on the repository file and on mutated copies (`privileged: true`, `0.0.0.0:8000:8000`, docker.sock mount) | repository passes; each mutation fails |
| ST10-44 | TH10-29 | Compose copy without `HF_HUB_OFFLINE` on one service; firewall fake reporting `Allow` | lint fails; doctor `firewall_outbound` FAIL |
| ST10-45 | TH10-31, TH10-32 | Live worker; `deploy up large` | no compose invocation; requested class set; `deploy_up` audit line |
| ST10-46 | TH10-38 | Fixture plugin distribution | WARNING `registry.plugin.loaded`; doctor `plugins` WARN listing it |
| ST10-47 | TH10-39 | Exception thrown mid-pull | final firewall state `Block` |
| ST10-48 | TH10-40 | Bundle with a symlinked wheel; with `../` in a bundle path; with an extra file | `ConfigError` each |
| ST10-49 | TH10-41, TH10-43 | `manage-bde` fake reporting `Protection Off` for the backup drive | `bitlocker` FAIL |
| ST10-50 | TH10-42 | After IT10-06, byte search for the record's unique description text across `data/` | zero hits outside the audit log (which holds only the `record_id`) |
| ST10-51 | TH10-45 | Pull while a model service runs; timer expiry during a stalled download | services stopped before `Allow`; timer restores `Block` |
| ST10-52 | TH10-46 | Outbound blocked (socket guard and fake firewall), previous pins on disk | `rollback_class` succeeds without any download command |
| ST10-53 | TH10-44 | Successful install | audit `deploy_install` with version and `sha256:` detail |
| ST10-54 | TH10-47 | Loopback client (sync and async variants) pointed at a non-loopback host: `base_url` `http://10.0.0.5:8000`, `http://127.0.0.1.evil.com:8000`, `http://u:p@127.0.0.1:8000`; a client built for `http://127.0.0.1:8000` sending an absolute URL `http://example.org/`; a loopback stub server answering 302 to `http://example.org/` | `EgressBlocked` (`not_loopback`) for each host case, raised before any socket (fake socket records zero connects); the redirect is not followed; bearer value absent from logs |
| ST10-55 | TH10-48 | Socket guard installed from a config with an enabled `snowflake` source whose `base_url` host is `acme.snowflakecomputing.com` and whose `hosts` omit it; then with `hosts: [acme.snowflakecomputing.com]`; an SDK-style `socket.create_connection` to `evil.snowflakecomputing.com` | first: connect to `acme.snowflakecomputing.com` raises `EgressBlocked` and C20 reports an error; second: allowed; the unlisted host is always blocked |
| ST10-56 | TH10-49 | Profile `hybrid` with `hybrid_approved` but `chat_approved: false`: (a) `reasoning` added to `security.egress.purposes`; (b) config built in the test with `reasoning` present to bypass C11, then a chat-style guarded call with purpose `reasoning` and `aggregated_evidence`; (c) `chat_approved: true` without `hybrid_approved` | (a) C11 error; (b) `cloud_chat_allowed` is `False` and the call raises `EgressBlocked` `chat_not_approved` with zero connects and an audit `egress` line; (c) C25 error |
| ST10-58 | TH10-51 | Source client for `servicenow` (`base_url` `https://corp.service-now.com`): `get("https://evil.example.com/api")`; a response-supplied next link to `https://corp.service-now.com.evil.com/`; a 302 to another host; `base_url` `http://corp.service-now.com`; `base_url` `https://u:p@corp.service-now.com`; a `snowflake` client whose `base_url` host is not in its `hosts` | `EgressBlocked` with reasons `host_not_allowed`, `host_not_allowed`, redirect not followed, `scheme_not_https`, `userinfo_present`, `host_not_allowed`; zero connects to any other host (fake socket) |
| ST10-59 | TH10-51, TH10-18 | Source client with `verify=False`; with a CA bundle path that does not exist; against a local TLS server with a self-signed certificate not in the bundle; with env `SSL_CERT_FILE` and `HTTPS_PROXY` set to attacker values | `ConfigError` "TLS verification cannot be disabled"; `ConfigError`; certificate verification error, no request body sent; env values ignored (`trust_env=False`) |
| ST10-60 | TH10-52 | Bundle whose `excel.duckdb_extension` is altered after signing; separately, socket guard active in the install subprocess | install refused with `extension_hash` before any `INSTALL`; with the correct file the install completes with no socket opened |
| ST10-57 | TH10-50 | After IT10-06, memory recall and FTS search for the record's unique description text and for its `record_id`; memory vector store lookup for the purged memory IDs; a deletion run whose step 3b fails once | zero memory items, FTS rows or vectors cite the record; the failed run leaves the request `running` with step `3b` `failed`, and the retry completes it |

### 11.6 Benchmarks

BT10-01–BT10-08 as defined in §10 (marker `bench`; BT10-04 and BT10-07 also `nightly`/`deploy`).

## 12. Task cards

Phase order: Phase 1 cards T10-01–T10-15, Phase 3 cards T10-16–T10-21 and T10-33, Phase 4 cards T10-22–T10-32. T10-32 is placed before T10-29 because T10-29 depends on it (IDs are never renumbered).

#### T10-01 Section models for herness.yaml

| Field | Content |
|-------|---------|
| Goal | `herness/core/settings.py` defines every `herness.yaml` section model. |
| Depends on | T00-03 (herness.core.errors) |
| Units | U10-02–U10-07 |
| Files | `herness/core/settings.py` |
| Tests | UT10-19 (model cases), UT10-61, ST10-37 |
| Threats | TH10-05, TH10-10 |
| Acceptance checks | `pytest -k "UT10-19 or UT10-61 or ST10-37"` passes; `mypy --strict herness/core` 0 errors |
| Blocked by | none |
| Size | M |

#### T10-02 YAML reading and layer sources

| Field | Content |
|-------|---------|
| Goal | Safe YAML loading, the four settings sources, override parsing and the bootstrap loader exist. |
| Depends on | T10-01 |
| Units | U10-15–U10-19, U10-21 |
| Files | `herness/core/config_sources.py` |
| Tests | UT10-05–UT10-08, UT10-10, UT10-13, UT10-14, UT10-23, UT10-24, PT10-06, ST10-01, ST10-02, ST10-03, ST10-05 |
| Threats | TH10-02, TH10-03, TH10-04 |
| Acceptance checks | listed tests pass; a 6 MiB file is rejected in < 1 s |
| Blocked by | none |
| Size | M |

#### T10-03 Root config, load, cache, hash

| Field | Content |
|-------|---------|
| Goal | `load_config`, `init_config`/`get_config`/`reset_config`, `config_hash`, `effective_dict` and `ConfigIssue` work, and `EgressBlocked` and `ConfigError` carry the attributes of R-19. |
| Depends on | T10-02; T00-05 (herness.core.ids.canonical_json); T00-03 (herness.core.errors) (taxonomy with `hint`, `details`, R-19); T00-09 (pyproject.toml import-linter contracts) settings exception (R-03); T01-02 (herness.connectors.settings.SourcesConfig), T02-01 (herness.model.settings.DqSettings), T02-01 (herness.model.settings.BuildSettings), T03-02 (herness.enrich.settings.DecidersSettings), T05-04 (herness.harness.llm.settings.ModelsConfig) (the composed sibling sections, R-69, R-76); impls 01–11 other owner `settings.py` modules (stubs with `extra="forbid"` accepted until owners land) |
| Units | U10-01, U10-08–U10-12, U10-14, U10-108 |
| Files | `herness/core/config.py`, `herness/core/errors.py` (the two attribute sets only) |
| Tests | UT10-01–UT10-04, UT10-09, UT10-11, UT10-12, UT10-15–UT10-18, UT10-22, UT10-80, UT10-84, PT10-01, BT10-01, BT10-02, ST10-16 |
| Threats | TH10-02, TH10-03, TH10-07 |
| Acceptance checks | tests pass; `lint-imports` passes with the impl 00 settings exception and no new contract; `config_hash` equal on Windows and Linux CI |
| Blocked by | none |
| Size | M |

#### T10-04 Registry

| Field | Content |
|-------|---------|
| Goal | `register`, `get`, `available`, `_BUILTINS`, `reset_registry` exist with entry-point loading. |
| Depends on | T00-03 (herness.core.errors) |
| Units | U10-23–U10-26 |
| Files | `herness/core/registry.py` |
| Tests | UT10-25–UT10-27, ST10-46 (registry part) |
| Threats | TH10-38 |
| Acceptance checks | tests pass |
| Blocked by | none |
| Size | S |

#### T10-05 Audit log

| Field | Content |
|-------|---------|
| Goal | Hash-chained audit writing, locked append, chain verification, config-change recording and last-set lookup. |
| Depends on | T10-03; T00-05 (herness.core.ids.new_ulid) |
| Units | U10-60–U10-64 |
| Files | `herness/core/audit.py` |
| Tests | UT10-21, UT10-57–UT10-59, PT10-08, FT10-01, ST10-17, ST10-18 |
| Threats | TH10-07, TH10-08, TH10-09 |
| Acceptance checks | tests pass; two processes writing 1,000 lines each produce a valid chain |
| Blocked by | D10-08 (lock wait replaces "retried 3×") |
| Size | M |

#### T10-06 Secrets core

| Field | Content |
|-------|---------|
| Goal | Secret reference types, both backends, resolution, set/delete with audit, referenced-name listing. |
| Depends on | T10-05 |
| Units | U10-27–U10-31, U10-33, U10-34 |
| Files | `herness/core/secrets.py` |
| Tests | UT10-28–UT10-33, FT10-05, ST10-26 |
| Threats | TH10-06, TH10-08, TH10-11 |
| Acceptance checks | tests pass; `detect-secrets` clean |
| Blocked by | none |
| Size | M |

#### T10-07 Log scrubber

| Field | Content |
|-------|---------|
| Goal | `known_values` and `scrub_secrets` exist and T00-07 (herness.core.logging.configure_logging) can install the processor. |
| Depends on | T10-06, T10-08; T00-07 (herness.core.logging) |
| Units | U10-32 |
| Files | `herness/core/secrets.py` |
| Tests | UT10-34, UT10-35, ST10-15 |
| Threats | TH10-07 |
| Acceptance checks | tests pass; the T00-07 (herness.core.logging.configure_logging) test with the processor passes |
| Blocked by | none |
| Size | S |

#### T10-08 Redaction detectors

| Field | Content |
|-------|---------|
| Goal | All patterned detectors, prefilters, Luhn and normalization. |
| Depends on | T10-01 |
| Units | U10-35, U10-36, U10-37 |
| Files | `herness/core/redact_patterns.py`, `herness/core/redact.py` (types only) |
| Tests | UT10-36, UT10-38, PT10-04 |
| Threats | TH10-14 |
| Acceptance checks | tests pass |
| Blocked by | none |
| Size | M |

#### T10-09 Name directory

| Field | Content |
|-------|---------|
| Goal | Aho-Corasick directory with variants and the display-names file. |
| Depends on | T10-08 |
| Units | U10-38, U10-39 |
| Files | `herness/core/redact_directory.py` |
| Tests | UT10-46 |
| Threats | TH10-14 |
| Acceptance checks | tests pass; 100k-name build < 5 s on the dev box |
| Blocked by | none |
| Size | S |

#### T10-10 Redactor

| Field | Content |
|-------|---------|
| Goal | `Redactor` (scan, redact, batch, pseudonym), `get_redactor`, `redact_text`, `RedactionFailed`; registers the `config._KEY_ID_PROVIDER` hook used by `config_hash`. |
| Depends on | T10-06, T10-08, T10-09 |
| Units | U10-40–U10-46, U10-48 |
| Files | `herness/core/redact.py` |
| Tests | UT10-37, UT10-39–UT10-42, UT10-45, PT10-02, PT10-03, PT10-05 |
| Threats | TH10-14 |
| Acceptance checks | tests pass; `mypy --strict` clean |
| Blocked by | none |
| Size | M |

#### T10-11 Table redaction and fixture scanner

| Field | Content |
|-------|---------|
| Goal | `redact_table` with process pool and `python -m herness.core.redact --scan`. |
| Depends on | T10-10 |
| Units | U10-47, U10-49 |
| Files | `herness/core/redact.py`, `herness/core/redact_scan.py` |
| Tests | UT10-43, UT10-44, UT10-47, FT10-07, ST10-30 |
| Threats | TH10-06, TH10-14 |
| Acceptance checks | tests pass; `python -m herness.core.redact --scan tests/fixtures` exits 0 on the impl 11 fixtures |
| Blocked by | none (D10-15 resolved by R-56) |
| Size | M |

#### T10-12 Cross-checks and full validation

| Field | Content |
|-------|---------|
| Goal | `CROSS_CHECKS` (C01–C25), `run_cross_checks`, the start-up owner-validator hook and `validate` exist. |
| Depends on | T10-03, T10-04, T10-06, T10-08 |
| Units | U10-13, U10-20, U10-109 (U10-22 removed) |
| Files | `herness/core/config_validate.py`, `herness/core/config.py` |
| Tests | UT10-19, UT10-75, UT10-81, ST10-04, ST10-27 |
| Threats | TH10-06, TH10-11, TH10-12, TH10-16, TH10-48, TH10-49 |
| Acceptance checks | tests pass; offline validate of templates < 1 s; T09-20 (herness.cli.main) registers the owner validators before `init_config` and runs F10-01 step 3a |
| Blocked by | D10-05 (each owner registers its validator through U10-109) for the owner rows only |
| Size | M |

#### T10-13 Config templates

| Field | Content |
|-------|---------|
| Goal | Repository `config/herness.yaml`, four profile overlays, `.env.example`, `.streamlit/config.toml`. |
| Depends on | T10-12; T05-04 (herness.harness.llm.settings.ModelsConfig) role names for the profile overlays; T11-16 (synth profile agreement check) |
| Units | U10-93, U10-94, U10-95 |
| Files | `config/herness.yaml`, `config/profiles/*.yaml`, `.env.example`, `.streamlit/config.toml` |
| Tests | UT10-76, IT10-01 |
| Threats | TH10-03, TH10-12 |
| Acceptance checks | `herness config validate --profile synth --offline` exits 0; `--profile hybrid` exits 1 (gate) |
| Blocked by | O-8 (denylist domains; empty list until supplied) |
| Size | S |

#### T10-14 Config and secrets commands

| Field | Content |
|-------|---------|
| Goal | `config validate|show|hash`, `secrets init|set|status` behavior for impl 09. |
| Depends on | T10-05, T10-06, T10-12; T09-20 (herness.cli command table) |
| Units | U10-65–U10-70 |
| Files | `herness/admin/__init__.py`, `herness/admin/commands_config.py`, `herness/admin/commands_secrets.py` |
| Tests | UT10-20, UT10-72, IT10-11, ST10-36 |
| Threats | TH10-01, TH10-07 |
| Acceptance checks | tests pass via impl 09 CliRunner |
| Blocked by | none (D10-02 resolved by R-07) |
| Size | M |

#### T10-15 Redaction quality gates

| Field | Content |
|-------|---------|
| Goal | Corpus recall/precision and throughput benchmarks run in CI and nightly. |
| Depends on | T10-11; T11-15 (tools.synth.pii_corpus.write_pii_corpus) |
| Units | U10-41, U10-42, U10-47 (tests only) |
| Files | none (tests only) |
| Tests | IT10-02, BT10-03, BT10-04, BT10-05 |
| Threats | TH10-14 |
| Acceptance checks | IT10-02 thresholds met; BT10-03 ≥ 5,000 records/s |
| Blocked by | none |
| Size | S |

#### T10-16 Egress log and check

| Field | Content |
|-------|---------|
| Goal | `EgressLog`, `EgressGuard.check` (design 10 §5.4 steps 1–7 with the chat rule of R-38) and `cloud_chat_allowed` exist. |
| Depends on | T10-03, T10-05, T10-10 |
| Units | U10-50, U10-51, U10-56, U10-57, U10-107 |
| Files | `herness/core/egress.py`, `herness/core/egress_log.py` |
| Tests | UT10-48–UT10-53, UT10-79, ST10-10–ST10-13, ST10-33, ST10-56, FT10-08, BT10-05 |
| Threats | TH10-14, TH10-16, TH10-19, TH10-20, TH10-22, TH10-49 |
| Acceptance checks | tests pass; T08-19 (herness.core.jobs.chat_model_profile) calls `cloud_chat_allowed` |
| Blocked by | none |
| Size | M |

#### T10-17 Guarded clients and lint

| Field | Content |
|-------|---------|
| Goal | Guarded sync and async clients, transports, download window, loopback client and the AST lint test. |
| Depends on | T10-16 |
| Units | U10-52–U10-55, U10-59 |
| Files | `herness/core/egress.py`, `herness/core/egress_clients.py` |
| Tests | UT10-54, UT10-55, UT10-74, ST10-07, ST10-09, ST10-25, ST10-32, ST10-35, ST10-39–ST10-41, ST10-54, IT10-03, IT10-04 |
| Threats | TH10-15, TH10-17, TH10-18, TH10-21, TH10-23, TH10-47 |
| Acceptance checks | tests pass; T05-08 (herness.harness.llm.anthropic_client.AnthropicClient) constructs with `http_client=get_guard().async_http_client(...)`; impl 03, impl 05 and impl 08 local clients use `loopback_http_client(base_url, timeout_s=...)` or `aloopback_http_client` |
| Blocked by | D10-04 (additive APIs other than `loopback_http_client`, which R-06 settles) |
| Size | M |

#### T10-18 Socket guard

| Field | Content |
|-------|---------|
| Goal | Audit-hook socket guard installed first in `cli.main`, with the allowlist of R-06. |
| Depends on | T10-02, T10-16; T01-01 (herness.connectors.settings_base.SourceSettings) `hosts` field |
| Units | U10-58 |
| Files | `herness/core/egress_socket.py` |
| Tests | UT10-56, PT10-07, ST10-08, ST10-29, ST10-55, IT10-15, BT10-06 |
| Threats | TH10-15, TH10-16, TH10-24, TH10-48 |
| Acceptance checks | tests pass; T09-20 (herness.cli.main) calls it before any other import that opens sockets |
| Blocked by | none |
| Size | S |

#### T10-19 Maintenance handler registration and commands

| Field | Content |
|-------|---------|
| Goal | `handle_maintenance`, `register_handlers`, `cmd_maintenance`, `cmd_privacy_delete` exist (dispatch only). |
| Depends on | T10-14; T08-12 (herness.core.jobs.register_handler), T08-12 (herness.core.jobs.enqueue), T08-22 (herness.core.jobs.run_inline), T08-12 (herness.core.jobs.worker_alive) |
| Units | U10-73, U10-74, U10-75, U10-104 |
| Files | `herness/admin/commands_data.py`, `herness/admin/maintenance.py`, `herness/admin/__init__.py` |
| Tests | ST10-31 |
| Threats | TH10-42 |
| Acceptance checks | ST10-31 passes; unknown action raises `ConfigError`; the handler registered by `herness.cli` is called with `ctx` only (R-42) |
| Blocked by | none |
| Size | S |

#### T10-20 Backup and purge

| Field | Content |
|-------|---------|
| Goal | Nightly backup and retention purge jobs. |
| Depends on | T10-19; T09-03 (herness.store.ops.chat.purge_chat); T02-03 (herness.store.lake_purge.purge_partitions_before); T02-09 (herness.store.warehouse.read_current) |
| Units | U10-96, U10-97, U10-98 |
| Files | `herness/admin/maintenance.py` |
| Tests | UT10-67, UT10-68, IT10-08, IT10-09, FT10-04, BT10-08 |
| Threats | TH10-43 |
| Acceptance checks | tests pass |
| Blocked by | none |
| Size | M |

#### T10-21 Security end-to-end suite

| Field | Content |
|-------|---------|
| Goal | Sentinel secret-leak run and audit integration. |
| Depends on | T10-07, T10-17; T05-11 (herness.harness.tracing.Tracer); T11-23 (tests.support.fake_llm.FakeLLMClient) and the impl 11 fixture connector; T02-07 (herness.store.ops.shared.decide_review_item) |
| Units | U10-32, U10-60 (tests only) |
| Files | none (tests only) |
| Tests | IT10-05, IT10-10, ST10-14 |
| Threats | TH10-07 |
| Acceptance checks | zero sentinel hits |
| Blocked by | none |
| Size | S |

#### T10-33 Source HTTP client

| Field | Content |
|-------|---------|
| Goal | `herness.core.egress.source_http_client` exists for impl 01's `httpx`-based connectors (R-06). |
| Depends on | T10-17, T10-18; T01-01 (herness.connectors.settings_base.SourceSettings) `hosts` field |
| Units | U10-110 |
| Files | `herness/core/egress_clients.py`, `herness/core/egress.py` (re-export only) |
| Tests | UT10-82, ST10-58, ST10-59 |
| Threats | TH10-51 |
| Acceptance checks | tests pass; ST10-25 passes with no connector allowance once impl 01 T01-14 uses the factory |
| Blocked by | none |
| Size | S |

T10-33 is a Phase 3 card placed here, after the Phase 3 cards it depends on (IDs are never renumbered).

#### T10-22 Command runners

| Field | Content |
|-------|---------|
| Goal | `run_cmd`, `run_wsl`, `run_powershell` with allowlists. |
| Depends on | T10-14 |
| Units | U10-76, U10-77 |
| Files | `herness/admin/wsl.py` |
| Tests | UT10-71 (runner cases), ST10-06 |
| Threats | TH10-05 |
| Acceptance checks | tests pass; ruff `S603`/`S607` suppressions listed in §13 only |
| Blocked by | none |
| Size | S |

#### T10-23 Compose file and render

| Field | Content |
|-------|---------|
| Goal | `docker/compose.yaml`, `DeployHistory` and `render_env`. |
| Depends on | T10-22, T10-06 |
| Units | U10-78, U10-79, U10-80 |
| Files | `docker/compose.yaml`, `herness/admin/deploy.py` |
| Tests | UT10-60, UT10-75, ST10-34, ST10-43, ST10-44 |
| Threats | TH10-26, TH10-27, TH10-28, TH10-29 |
| Acceptance checks | tests pass; `docker compose -f docker/compose.yaml config` succeeds with a sample env file |
| Blocked by | V-16 (vLLM entrypoint form, llama.cpp `curl`) for Phase 4 runtime only |
| Size | M |

#### T10-24 Pull with firewall window

| Field | Content |
|-------|---------|
| Goal | `firewall_window`, `pull_images`, `pull_weights`, `verify_weights`, `run_pull`, `cmd_deploy_pull`. |
| Depends on | T10-23, T10-17 |
| Units | U10-81, U10-82, U10-83, U10-92, U10-72 (pull) |
| Files | `herness/admin/deploy_pull.py`, `herness/admin/commands_deploy.py` |
| Tests | UT10-62, UT10-63, FT10-06, FT10-09, ST10-19–ST10-21, ST10-47, ST10-51 |
| Threats | TH10-30, TH10-33–TH10-35, TH10-39, TH10-45 |
| Acceptance checks | tests pass with `fake_runner` |
| Blocked by | V-15, V-17, V-20, V-21 for the real-host run (IT10-13) |
| Size | M |

#### T10-25 Class up/down, rollback, prune

| Field | Content |
|-------|---------|
| Goal | `class_up`, `class_down`, `rollback_class`, `prune` and their commands. |
| Depends on | T10-23, T10-24; T08-12 (herness.core.jobs.worker_alive), T08-18 (herness.core.jobs.gpu_state), T08-18 (herness.core.jobs.request_gpu_class), T08-17 (herness.core.jobs.gpu_lock.GpuLock) |
| Units | U10-84, U10-85, U10-72 (up, down, rollback, prune, render) |
| Files | `herness/admin/deploy.py`, `herness/admin/commands_deploy.py` |
| Tests | UT10-64, UT10-65, ST10-45, ST10-52 |
| Threats | TH10-31, TH10-32, TH10-46 |
| Acceptance checks | tests pass |
| Blocked by | none |
| Size | M |

#### T10-26 Release verification and install

| Field | Content |
|-------|---------|
| Goal | `ReleaseBundle`, `verify_bundle`, `install_bundle`, the offline DuckDB `excel` extension install (impl 01 O-3), `cmd_deploy_install` (ENG E4). |
| Depends on | T10-22; T00-15 (.github/workflows/release.yml) producing the bundle |
| Units | U10-86, U10-87, U10-88, U10-112, U10-72 (install) |
| Files | `herness/admin/attest.py`, `herness/admin/duckdb_ext.py`, `herness/admin/commands_deploy.py` |
| Tests | UT10-66, UT10-85, IT10-14, ST10-22–ST10-24, ST10-48, ST10-53, ST10-60 |
| Threats | TH10-36, TH10-37, TH10-40, TH10-44, TH10-52 |
| Acceptance checks | tests pass; a real release bundle from CI verifies offline on the dev box, and afterwards `duckdb` in the venv loads `excel` with autoinstall off and the network disabled |
| Blocked by | V-22 (`gh` flags) |
| Size | M |

#### T10-27 Doctor host checks

| Field | Content |
|-------|---------|
| Goal | `CheckResult`, `doctor_checks`, `HOST_CHECKS`. |
| Depends on | T10-22, T10-05, T10-12, T10-18 |
| Units | U10-89, U10-90 |
| Files | `herness/admin/doctor_host.py` |
| Tests | UT10-71 (host rows), IT10-12, ST10-17 (doctor part), ST10-28, ST10-38, ST10-42, ST10-49 |
| Threats | TH10-13, TH10-25, TH10-26, TH10-41, TH10-43 |
| Acceptance checks | tests pass; T09-22 (herness._cli.doctor.run_doctor) renders the rows |
| Blocked by | none |
| Size | M |

#### T10-28 Doctor GPU checks

| Field | Content |
|-------|---------|
| Goal | `GPU_CHECKS`. |
| Depends on | T10-27, T10-24 |
| Units | U10-91 |
| Files | `herness/admin/doctor_gpu.py` |
| Tests | UT10-71 (GPU rows), ST10-44 (doctor part), BT10-07 |
| Threats | TH10-29, TH10-39 |
| Acceptance checks | tests pass |
| Blocked by | V-19, V-20 for the real host |
| Size | M |

#### T10-32 Privacy ops-store area

| Field | Content |
|-------|---------|
| Goal | `herness/store/ops/privacy.py` provides the `deletion_request` functions and the deletion-set read used by impl 01 and impl 02 (R-08; the evidence and finding scrubs are `T05-12 (herness.store.ops.evidence.scrub_record_from_evidence)` and `T06-06 (herness.store.ops.findings.scrub_record_from_findings)`, R-09, R-77). |
| Depends on | T02-04 (herness.store.ops.core) (`connection`, `run_write`, `read_one`, `read_all`, `dump_json`, `load_json`); T02-06 (herness/store/migrations/005_review_chat_privacy.sql) (U02-53); T00-03 (herness.core.errors.NotFound) (R-19) |
| Units | U10-105, U10-111; U10-106 removed (R-77) |
| Files | `herness/store/ops/privacy.py`, `herness/store/ops/__init__.py` (the spec-10 `__all__` block only) |
| Tests | UT10-77, UT10-83 |
| Threats | TH10-42 |
| Acceptance checks | tests pass; UT02-68 (no duplicate names across `__all__` blocks) passes, with `deleted_record_ids` exported only from the spec-10 block; `lint-imports` shows `herness.store.ops.privacy` imports nothing above L1 |
| Blocked by | none |
| Size | S |

#### T10-29 Privacy deletion

| Field | Content |
|-------|---------|
| Goal | Privacy deletion with the seven design steps plus the memory purge step 3b (R-54). |
| Depends on | T10-19, T10-32; T02-03 (herness.store.lake_purge.purge_record_ids); T02-21 (herness.model.promote.cleanup_builds); T03-34 (herness.enrich.purge_record); T05-12 (herness.store.ops.evidence.scrub_record_from_evidence); T06-06 (herness.store.ops.findings.scrub_record_from_findings); T07-26 (herness.harness.memory.MemoryStore.purge); T01-05 (herness.connectors.deletion.DeletionFilter) (deletion-set reload) |
| Units | U10-99, U10-104 (memory binding), U10-75 (memory binding); U10-100 removed |
| Files | `herness/admin/privacy.py`, `herness/admin/maintenance.py`, `herness/admin/__init__.py` |
| Tests | UT10-69, IT10-06, FT10-03, ST10-50, ST10-57 |
| Threats | TH10-42, TH10-50 |
| Acceptance checks | tests pass; `herness.cli` binds `memory_purge` to `T07-26 (herness.harness.memory.MemoryStore.purge)` when it registers the handler |
| Blocked by | none |
| Size | M |

#### T10-30 Rekey

| Field | Content |
|-------|---------|
| Goal | `secrets rekey` staging and the rekey job. |
| Depends on | T10-10, T10-19; T08-14 (herness.core.jobs.schedule_rekey); T03-05 (herness.enrich.text.compose_text); T02-09 (herness.store.warehouse.open_readonly) |
| Units | U10-71, U10-101, U10-102, U10-103 |
| Files | `herness/admin/rekey.py`, `herness/admin/commands_secrets.py` |
| Tests | UT10-70, UT10-73, IT10-07, FT10-02 |
| Threats | none new (integrity) |
| Acceptance checks | tests pass |
| Blocked by | none |
| Size | M |

#### T10-31 Phase 4 deployment acceptance

| Field | Content |
|-------|---------|
| Goal | The install runbook (design 10 §5.6.3) executed on the target PC with doctor all PASS. |
| Depends on | T10-23–T10-28 |
| Units | none (acceptance) |
| Files | none |
| Tests | IT10-13, BT10-07 |
| Threats | all TB8 threats |
| Acceptance checks | doctor all PASS; each class healthy; one GPU service at a time; `netstat -ano` shows loopback-only listeners on 8000, 8100, 8200, 8501 |
| Blocked by | D7, V-15–V-21; R-52 verification of `max_model_len` and `context_window` on the real card |
| Size | S |

## 13. Design deltas and open items

### 13.1 Design deltas

Status values follow the consistency pass. The binding rulings are in [`DECISIONS.md`](DECISIONS.md) (R-01–R-77); where a ruling changes a design spec, that change is pending in `DECISIONS.md` §9 and this spec already implements the ruling.

| ID | Spec | Change | Reason | Status |
|----|------|--------|--------|--------|
| D10-01 | ENG §2.1, 00 §3 | Allow `herness.core.config` to import owner `settings.py` modules of L1–L5; add a contract that `settings.py` modules import only stdlib, `pydantic`, `herness.core.errors`, `herness.core.secrets` and other `settings.py` | Design 10 §3.1 types the root config with owner models, which is an upward import under ENG §2.1 | Accepted (R-03), narrowed: the allowed imports are stdlib, `pydantic`, `herness.core.types`, `herness.core.errors`; impl 00 encodes the exception; secret-reference fields use local patterns plus C16 (U10-27) |
| D10-02 | ENG §2.1, 00 §3 | New L5 package `herness/admin/` for commands, job handler, deploy, doctor checks | These need L1–L4 imports that `herness.core` may not have | Accepted (R-07) |
| D10-03 | 10 §3.7, §7.4; 09 command table; 00 §3 | Add `deploy install BUNDLE` (admin (OS)) and `deploy.release.{repo, signer_workflow, licence_exceptions}`; release CI publishes wheel, hashed `requirements.txt`, CycloneDX SBOM, provenance and SBOM attestations and the Sigstore trusted root; production installs the verified wheel non-editable (00 §3 says editable) | ENG E4 | Resolved by R-47 (command row in impl 09) and R-58 (editable in development, verified wheel on the target); the `deploy.release` keys stay in design 10 §7.4 as part of the pending design edit |
| D10-04 | 10 §3.1, §3.3, §3.5 | Additive APIs: `init_config`, `reset_config`, `delete_secret`, `set_secret(..., actor)`, `EgressGuard.download_window`, `loopback_http_client`, `install_socket_guard(cfg: HernessConfig \| BootstrapConfig)` | Needed for the CLI entry, rekey, pull, lint rule and bootstrap | `loopback_http_client`: Accepted (R-06) with the exact R-06 signature plus `aloopback_http_client`; the other APIs: Still open |
| D10-05 | 01, 03, 04, 08 | Each owner exposes a validator returning config issues, registered by the composition root through U10-109 and run after `load_config` (the `SECTION_VALIDATORS` import table is removed) | Design 10 §5.1 delegates these checks without an interface; validators such as impl 04's catalog check need layers above L0 | Still open (hook defined by U10-109 following R-04; owners must register) |
| D10-06 | 00 §7 | `EgressBlocked` gains optional `egress_id` and `reason` attributes; `ConfigError` gains optional `issues` | Fallback traces and `validate` need them without string parsing | Resolved by R-19 (declared here as U10-108) |
| D10-07 | 10 §4.6 | Extend `admin_action.action` with `deploy_down`, `deploy_pull`, `deploy_prune`, `deploy_install`, `deploy_render`, `redact_rekey` (already in §5.3); add optional `counts` and `detail` fields | Every admin action must be audited | Still open |
| D10-08 | 10 §6 | Replace "`StoreBusy` retried 3×, then `FatalError`" for audit with "lock wait up to 10 s, then `FatalError`" | Audit is a Phase 1 JSONL file, not SQLite; the impl 08 retry layer is Phase 3 and ENG §3.4 forbids local retries | Still open |
| D10-09 | 08 §7 | `resilience.gpu.compose_cmd` uses `wsl.exe -d herness --exec docker compose ...` instead of `--` | `--` passes arguments through the Linux shell (injection surface) | Still open |
| D10-10 | 10 §5.6.2 | Add `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`, read-only model volumes; verify at Phase 4 | Container hardening (ASVS V13.2); residual R-2 | Still open |
| D10-11 | 05 §7 vs 10 §7.4 | Reconcile `models.clients.local-30b.context_window` (65536) with `deploy.reasoning.max_model_len` (32768, "≥ context_window") | Contradiction | Resolved by R-51, R-52: `context_window = 32768` ≤ `max_model_len`; C10 is an `error` check; Phase 4 verification on the real card |
| D10-12 | 02 | `herness.store.ops` adds `create_deletion_request`, `get_deletion_request`, `set_deletion_status`, `record_deletion_step`, `scrub_record_from_evidence`, `scrub_record_from_findings`, and index `deletion_request(record_id, status)` | ENG §2.1: only `herness.store.ops` writes the ops store | Resolved by R-08, R-09: area `herness/store/ops/privacy.py` is owned here (U10-105, U10-111); `scrub_record_from_evidence` is U05-75 (T05-12) and `scrub_record_from_findings` is U06-144 (T06-06, R-77), both called by U10-99; the impl 02 indexes on `status` and `record_id` suffice, so no migration in 080–089 (R-11) |
| D10-13 | 02 §3.1 | State that privacy deletion rewrites lake files (exception to "the lake is append-only") | Design 10 §5.5 step 2 | Resolved by R-57 (privacy deletion, retention purge and compaction are the exceptions; the rewrite is impl 02's `purge_record_ids`) |
| D10-14 | 08 §7 | Health `bearer_secret: openjev.api_key` must be `OPENJEV_API_KEY` (design 10 §3.3 name) | Name mismatch; C06 would fail | Resolved by R-53 (`secret:OPENJEV_API_KEY`) |
| D10-15 | 11 §5.1.4 | Synthetic phones in a form of ≥ 9 digits; synthetic credential and URL-token values start with `synthetic` | `+1-555-01xx` has 8 digits, below the 9-digit phone rule; the fixture scanner allow rules need a marker | Resolved by R-56 (`+1-202-555-01xx`; thresholds and corpus size owned here) and R-67 (credential and token marker `synthetic`, applied by U10-49 step 5) |
| D10-16 | 05 §7 vs 10 §5.6.2 | `local-large-offload.base_url` port 8080 must be 8200 (host port of `llamacpp-large`) | C09 fails on the defaults | Resolved by R-51 |
| D10-17 | 09 §5.6 command table | Add `secrets rekey`, `deploy up`/`down`/`rollback large`, `config hash --profile`, `deploy install` | Rows missing from 09 | Resolved by R-47 |
| D10-18 | 09 §9.1 vs 10 §5.1 | Settle whether a non-loopback `security.ui.bind` is ever allowed | Contradiction | Resolved by R-50: the app always binds to loopback; C05 and the request-time peer check are both stated (U10-05) |
| D10-19 | 10 §5.4 | Socket-guard allowlist cannot derive Snowflake, MongoDB or MSAL hosts from `base_url` | Gap | Resolved by R-06: per-source `sources.<name>.hosts`; allowlist = those lists ∪ egress allowlist ∪ loopback; nothing derived from `base_url` for SDK sources (U10-21, U10-58, C20) |
| D10-20 | 07 | Spec 07 names `guard_egress`; the design API is `EgressGuard.check`/`http_client` | Name mismatch | Resolved by R-55 (`get_guard()`) |
| D10-21 | ENG §2.1, 01 | R-06 lets only `herness.core.egress` build `httpx` clients, but impl 01's `herness.connectors.http.http_client` (U01-58) built the host-guarded client for `httpx`-based sources | Gap in R-06 found in this pass | Resolved by R-06 through the factory `source_http_client` (U10-110, T10-33), requested by impl 01 (T01-14); ST10-25 keeps no connector allowance. Design 10 §3.5 gains the factory in the pending design edit |
| D10-24 | 01 | `herness.store.ops.deleted_record_ids` reads `deletion_request`, which is in area `privacy`; it moves from impl 01 (U01-29) to this spec (U10-111) | R-08, R-09 | Accepted (R-08, R-09); impl 01 references U10-111 |
| D10-22 | 07 | R-54 names `MemoryStore.purge(record_id)`; the earlier impl 07 draft had `MemoryLifecycle.purge(*, author_ref=None, record_id=None, now=None)`. This spec calls the purge with the keyword `record_id=` through the `memory_purge` binding (U10-75), which is valid for the R-54 form and the keyword form | Name difference between the ruling and the earlier impl 07 draft | Accepted (R-54); impl 07 publishes the public name |
| D10-23 | 10 §5.1 | Exit codes follow R-46 (corrected: design 09 §5.8 plus 14): `config validate` returns 1 on errors, and on warnings with `--strict`; `doctor` returns 1 on any FAIL; a `ConfigError` at load returns 3; `ModelUnavailable` 9, `PermissionDenied` 11 and `EgressBlocked` 13 (restored) | R-46 | Accepted (R-46, corrected) |
| D10-25 | 10 §3.1, §4.2; 01; 02; 03; 05 | `sources.yaml` and `models.yaml` hold sibling top-level sections owned by different specs; the root config composes them (`SourcesFileConfig`, `ModelsFileConfig` in U10-08) | Settings modules may not import each other (R-03) | Accepted (R-69, R-76); impl 03 models the top-level `deciders` section of `models.yaml` as U03-150 (T03-02), read as `cfg.models.deciders` |
| D10-26 | 10 §3.3 | Secret references in settings are `secret:<name>` strings only; the bare-name form for `*_secret` fields is withdrawn (U10-27, C16) | R-72 | Accepted (R-72); impl 03 U03-150 already uses `secret:` references |
| D10-27 | 02 §2.3; 06 | `scrub_record_from_findings` rewrites `finding.numbers`, a table owned by impl 06 | R-08, R-09 | Resolved by R-77: impl 06 owns it as U06-144 (T06-06); U10-106 removed; U10-99 step 4 calls it |
| D10-28 | 10 §3.7, §7.4; 00 (release workflow); 01 O-3 | `deploy install` also installs the DuckDB `excel` extension offline from the release bundle (U10-112, T10-26); new key `deploy.release.duckdb_extensions`; release CI (T00-15) adds `duckdb/excel.duckdb_extension` for the pinned DuckDB version with a provenance attestation | Impl 01's files connector reads `.xlsx` with autoinstall off (its O-3) | Resolved: impl 00 T00-15 (U00-60) adds the extension file, its `SHA256SUMS` and its provenance attestation to the release; design 10 §7.4 gains the key in the pending design edit |

ENG exceptions this spec needs (listed per ENG §2.4 and §3):

| ID | Rule | Exception | Reason |
|----|------|-----------|--------|
| X-1 | ENG §2.3 module state | `egress_socket._POLICY`, `_RESOLVED`; `secrets._KNOWN`; the owner-validator registry of `config_validate` (U10-109) | Audit hooks cannot be removed; the scrubber must see resolved values; owner validators are bound by the composition root (R-04); reset by `reset_config` hooks or `reset_owner_validators` |
| X-2 | ENG §3.5 no pickle across processes | `redact_table` uses `ProcessPoolExecutor`, which serialises built-in lists of strings | Only standard types, never data read from disk |
| X-3 | ruff `S603`, `S607` | `herness/admin/wsl.py` only | Allowlisted argv subprocess runner |
| X-4 | ENG §3.2 `strict=True` | `Path` fields of `PathsConfig`, `RedactionConfig.directory_file` in lax mode | YAML has no path type |

### 13.2 Open questions inherited (design 10 §11) with current defaults

| ID | Question | Default | Blocks |
|----|----------|---------|--------|
| O-1 | D5 hybrid/premium allowed | Not allowed; gates fail | nothing (tests use fixture approvals) |
| O-2 | D7 OpenJev on non-Blackwell GPU | Verify at Phase 4; fallback teacher (impl 03) | T10-31 |
| O-3 | Hosted Jev URL, auth, retention (D24) | `api.typesafe.ai` in `premium.yaml`; premium not configured | nothing |
| O-4 | `wsl.exe` in service sessions; Hyper-V firewall cmdlets | Verify at Phase 4 (V-19, V-20); fallback auto-logon | T10-28, T10-31 |
| O-5 | Name directory source (D10) | HR export CSV | nothing |
| O-6 | Corporate retention (D11) | Design defaults | nothing |
| O-7 | SSO provider (D12); GGUF download source | Entra ID via oauth2-proxy; GGUF placed manually | nothing |
| O-8 | `denylist_domains` content | Empty list until supplied by the product owner | T10-13 (content only) |
| O-9 | D9 Jira titles | Redacted at display (`redact_text`) | nothing |
| O-10 | Keyring chunking above 1,200 chars (Windows blob limit) | Chunked storage as U10-33 | nothing; confirm on the target in T10-31 |
| O-11 | Provider usage for streaming responses | Estimate only (R-7) | nothing |

### 13.3 Verification items (open-questions.md §b) blocking cards

| ID | Item | Blocks |
|----|------|--------|
| V-15 | OpenJev health, warm-up, `python3`, offline behavior with `HF_HUB_OFFLINE`; `refs/main` pinning approach | T10-24 real run, T10-31 |
| V-16 | vLLM entrypoint form; `curl` in the llama.cpp image | T10-23 runtime, T10-31 |
| V-17 | Hugging Face CDN hostnames | T10-24 real run |
| V-19 | `wsl.exe` from a non-interactive service session | T10-28, T10-31 |
| V-20 | WSL Hyper-V VM creator ID and cmdlets | T10-24, T10-28 |
| V-21 | Hugging Face CLI name in the pinned vLLM image (`huggingface-cli` or `hf`) | T10-24 real run |
| V-22 | `gh attestation verify` flags (`--bundle`, `--signer-workflow`, `--deny-self-hosted-runners`, `--custom-trusted-root`, `--predicate-type`, `--format json`) on the pinned `gh` version, offline | T10-26 |

## 14. Dependencies

### 14.1 Third-party packages and tools

| Package / tool | Min version | Licence | Use |
|----------------|-------------|---------|-----|
| `pydantic` | 2.9 | MIT | Models |
| `pydantic-settings` | 2.5 | MIT | Sources |
| `pyyaml` | 6.0 | MIT | Safe YAML |
| `keyring` | 25.0 | MIT | Credential Manager backend |
| `httpx` | 0.27 | BSD-3-Clause | Guarded transports |
| `certifi` | 2024.8 | MPL-2.0 | CA bundle (httpx dependency) |
| `structlog` | 24 | MIT or Apache-2.0 | Scrub processor host |
| `pyahocorasick` | 2.1 | BSD-3-Clause | Name directory |
| `pyarrow` | 17 | Apache-2.0 | `redact_table`, lake rewrite, labels |
| `psutil` | 5.9 | BSD-3-Clause | Doctor port checks |
| `presidio-analyzer`, `spacy` (extra `ner`) | 2.2, 3.7 | MIT | Optional NER |
| Dev: `hypothesis`, `respx`, `freezegun`, `pytest-benchmark`, `import-linter`, `detect-secrets` | per 00 §9 and ENG E3 | MIT, BSD, Apache-2.0 | Tests and gates |
| Tools: Docker Engine, NVIDIA Container Toolkit, WSL2, NSSM or Task Scheduler, GitHub CLI `gh` (new, for `deploy install`), `uv`, optional Caddy and oauth2-proxy | `gh` ≥ 2.49 (attestation commands) | Apache-2.0 / MIT | Deployment |

No new Python dependency is added beyond 00 §9 except `psutil` (already listed there) and `certifi` (transitive of `httpx`). `gh` is a new host tool (delta D10-03).

### 14.2 Internal dependencies (implementation specs)

| Spec | Units used from it |
|------|--------------------|
| 00 | `T00-03 (herness.core.errors)` (taxonomy with `hint`, `details`, `NotFound`; R-19), `T00-05 (herness.core.ids.new_ulid)`, `T00-05 (herness.core.ids.canonical_json)` and `sha256_hex` (R-14), `T00-04 (herness.core.time)` (UTC now), `T00-07 (herness.core.logging)` (pipeline hosting `scrub_secrets`), the import-linter settings exception (R-03) |
| 01 | `T01-02 (herness.connectors.settings.SourcesConfig)` (the connector sections of `sources.yaml`, composed by U10-08, R-69; source models with local secret-reference patterns, R-03, and the `hosts` list per source, R-06), `T01-05 (herness.connectors.deletion.DeletionFilter)` (deletion-set reload; source schedules are validated by impl 08's owner validator); impl 01 is the consumer of `source_http_client` (U10-110) and `deleted_record_ids` (U10-111) |
| 02 | `T02-04 (herness.store.ops.core)` (`connection`, `run_write`, `read_one`, `read_all`, `dump_json`, `load_json`; R-10), `T02-06 (herness/store/migrations/005_review_chat_privacy.sql)` (`deletion_request`), `T02-03 (herness.store.lake_purge.purge_record_ids)` and `purge_partitions_before` (R-57), `T02-21 (herness.model.promote.cleanup_builds)`, `T02-09 (herness.store.warehouse.read_current)` (`CURRENT` reader), `T02-01 (herness.model.settings.DqSettings)` and `T02-01 (herness.model.settings.BuildSettings)` (the `sources.yaml` sections `dq` and `build`, including the `sources.build.threads` key, composed by U10-08, R-69), `T02-01 (herness.model.settings.MappingsConfig)` |
| 03 | `T03-34 (herness.enrich.purge_record)`, `T03-02 (herness.enrich.settings.DecidersSettings)` (U03-150; the top-level `models.yaml` section `deciders`, key path `models.deciders`, composed by U10-08, R-76), `T03-05 (herness.enrich.text.compose_text)`, `T03-02 (herness.enrich.settings.check_decider_refs)` (owner validator, R-71), caller of `update_display_names` and `redact_table` |
| 04 | `T04-03 (herness.metrics.catalog.metrics_owner_validator)` (owner validator, R-71) |
| 05 | `T05-04 (herness.harness.llm.settings.ModelsConfig)` (the `models.yaml` sections `models`, holding `roles`, and `harness`, R-76), `T05-12 (herness.store.ops.evidence.scrub_record_from_evidence)` (privacy deletion step 4, R-09), Anthropic adapter using `async_http_client`, trace writer applying `scrub_secrets`, profile role overlays |
| 06 | Swarm caps in `premium.yaml`; `Blackboard.post` uses `Redactor.scan` |
| 07 | `T07-02 (herness.harness.memory.settings.MemoryConfig)` (`injection_patterns` field); redaction before writes; `T07-26 (herness.harness.memory.MemoryStore.purge)` (deletion step 3b, R-54) |
| 08 | `T08-12 (herness.core.jobs.enqueue)`, `register_handler`, `run_inline` (R-45), `worker_alive` (R-44), `gpu_state`, `schedule_rekey`, requested-class setter, `data/locks/gpu.lock` helper, `JobContext`, `JobOutcome`, metric sink (ENG E5), `T08-02 (herness.core.jobs.validate.validate_resilience_config)` (owner validator, R-71), `T08-09 (herness.core.resilience.ModelChain)` (fallback chain), `T08-19 (herness.core.jobs.chat_model_profile)` and `chat_policy` (chat mode selection calling `cloud_chat_allowed`, R-38, C08-06), `HERNESS_WORKER` marker |
| 09 | `T09-20 (herness.cli command table)` (R-47) and `CommandResult`, role check, `doctor` command, exit-code mapping (R-46), `T09-03 (herness.store.ops.chat.purge_chat)`, `T09-13 (app.common.auth.resolve_identity)` (identity-header trust at request time, R-50), composition-root registration of owner validators and of the `maintenance` handler |
| 11 | Redaction corpus (size and thresholds owned here, R-56), fixture scanner hook, `FakeLLMClient` (R-65), fixture connector, `synth` profile content, release CI bundle |
