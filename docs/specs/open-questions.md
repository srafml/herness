# Herness — Open Questions

Status: 2026-09-24 · Consolidated from specs 00–11 after the v2 consistency sweep. Resolved items are marked in each spec's "Open questions" section; only open items are listed here.

## (a) Decisions needed from the product owner

Each item has a default that the specs already follow. Work can proceed on the default; changing it later touches the listed specs.

### Carried from spec 00 §10

| # | Question | Current default | Affects |
|---|----------|-----------------|---------|
| D1 | What is the source of truth for service ↔ group ↔ Jira project ↔ org mapping? | CMDB, with `mappings.yaml` overrides | 01, 02, 04 |
| D2 | What are the dollar weights (downtime and toil rates, effort factors, expected reductions, team capacities)? | Placeholders flagged `unconfirmed: true`; reports and dashboard show a banner | 04, 09 |
| D3 | What review cadence do you want? | Nightly standard reviews; weekly deep review Sunday 21:00 | 06, 08 |
| D4 | What are the chat hours? | 08:00–19:00 business timezone; outside those hours a smaller local model answers | 08, 10 |
| D5 | Does data policy allow the hybrid profile (aggregated evidence sent to Claude) or premium? | Not allowed; `hybrid`/`premium` fail validation until an approval is recorded | 05, 06, 10 |
| D6 | Do incidents count against the resolving team or the service owner? | Resolving team for org scores; service owner for funding attribution | 04 |
| D7 | Will OpenJev weights run on the target (non-Blackwell) 24 GB GPU? Needs a test on the real card. | Verify at Phase 4; if not, the local reasoning LLM is the teacher | 03, 10 |

### Other business and policy questions

| # | Question | Current default | Affects |
|---|----------|-----------------|---------|
| D8 | Which role do unlisted users get in the dashboard? | `viewer`; switch to `denied` when LAN exposure is on | 09, 10 |
| D9 | Can Jira titles (`core.work_item.summary`) be shown to models and on screen? They may contain names. | Shown, but passed through redaction first; not blocked | 05, 09, 10 |
| D10 | Where does the name directory for redaction come from: an HR export or a ServiceNow `sys_user` sync? | HR export CSV (`security.redaction.directory_file`) | 01, 10, 11 |
| D11 | Do corporate retention rules or legal holds override the defaults (e.g. 7-year audit)? | Lake 36 months, traces 90 days, audit 730 days, reports 365 days, chat 180 days | 09, 10 |
| D12 | Which SSO provider fronts the dashboard if it is exposed on the LAN? | Entra ID through `oauth2-proxy` behind Caddy | 09, 10 |
| D13 | Which source field defines customer impact minutes? | ServiceNow custom field in `mappings.yaml`; P1/P2 fallback flagged `estimate` | 01, 02, 04 |
| D14 | Which monitoring tools are in use, and how is `availability_pct` defined per tool? | Adapters for Prometheus/Mimir, Datadog, Splunk, Dynatrace; metric defined per tool in `sources.yaml` | 01, 04 |
| D15 | Is commit or merge time available anywhere, for true DORA lead time? | Change request `opened_at` → `actual_end` as a proxy | 04 |
| D16 | Is sprint data needed for carryover metrics? | Period-based carryover | 04 |
| D17 | Who are the gold-set labelers (1,500 records × 2 reviewers, named per org)? | None named; blocks the Phase 4 classifier gate | 03, 11 |
| D18 | Are non-English tickets in scope? | English model only; share of non-Latin text is reported | 03 |
| D19 | Should changes and problems be clustered too? | Incidents only | 03, 09 |
| D20 | Should retrospective findings adjust recommendation confidence only through the memory outcome adjustment (not `score.funding.confidence`)? | Yes, memory only | 06, 07 |
| D21 | Should approved `insight` memory items appear in reports? | Evidence appendix only | 07, 09 |
| D22 | How long after a decision are delivery metrics measured? | 12 weeks, overridable per metric | 07 |
| D23 | Only if D5 is approved: may the deep-mode Writer use more than 200k tokens of Claude context? | No (cost and latency cap) | 05, 07 |
| D24 | Only if premium is approved: which hosted Jev endpoint and data-retention terms are acceptable? | Not configured; premium needs a data-processing agreement | 03, 10 |

## (b) Technical items to verify during implementation

### Before Phase 3 code starts

1. Resolved (coordinator decision, 2026-09-24): spec 05 `HarnessHooks` is the only hooks class; loop detection runs inside the spec 05 loop; spec 08 owns `ModelChain`, `loop_signal_policy` and `save_checkpoint`; spec 06 owns `RunBudget`; spec 05 `Tracer` is the only trace writer. Nothing left to verify. — 00, 05, 06, 08

### Phase 3 (harness)

2. Ollama `/v1` option names for JSON-schema output and thinking (`extra_body.think`). — 05
3. vLLM structured-output form (`response_format` json_schema vs `structured_outputs`) on the pinned vLLM build. — 05
4. DuckDB setting names `enable_external_access` and `lock_configuration` on the pinned DuckDB. — 05
5. Claude Haiku 4.5: whether `temperature` is accepted with thinking on. — 05
6. Claude Sonnet 5 and Haiku 4.5 cache read/write prices. — 05
7. Anthropic strict tool mode with `row_key` as an open object (fallback: list of `{column, value}` pairs). — 05
8. Claude preserved thinking across a compacted (fresh) conversation; decides whether compaction keeps structured tool groups. — 07

### Phase 4 (enrichment, containers)

9. D7: OpenJev NVFP4 weights on the target GPU. — 00, 03, 10
10. OpenJev `probabilities` shape (dict or list) and `legend` format; freeze in a fixture. — 03
11. Laya repo path/subfolder, training entry points outside the notebook, full choice distributions from `predict_batch`, `predict_shortlist` with k = 16. — 03
12. Enrichment throughput targets on the actual card. — 03
13. `owning_team` shortlist quality with more than 255 teams or teams without descriptions. — 03
14. Whether work items get the `root_cause` question; if not, the tier-2 root-cause path in scoring stays off. — 03, 04
15. OpenJev health (`GET /v1/models`), warm-up (`POST /v1/systemone`), `python3` in the image, offline behavior with `HF_HUB_OFFLINE`. — 08, 10
16. vLLM container entrypoint form; `curl` present in the llama.cpp image for its healthcheck. — 10
17. Hugging Face CDN hostnames for `deploy pull`. — 10
18. Hosted Jev base URL and auth scheme (`api.typesafe.ai` vs `api.codiv.ai`). — 03, 10
19. `wsl.exe` from a non-interactive service session (fallback: auto-logon for `svc-herness`). — 10
20. WSL Hyper-V firewall VM creator ID and cmdlets on the target Windows build. — 10

### Phase 5 (outputs)

21. Whether `RecommendationItem.action_levers` and `ReportDraft.flags` should become named types for renderer validation. — 06, 09

### Phase 6 (live sources)

22. ServiceNow: OAuth client credentials allowed, and which inbound rate-limit rule applies (sets `max_concurrency`). — 01
23. Jira: bulk changelog endpoint available on the tenant; custom field IDs per instance. — 01, 02
24. Dataverse: change tracking enabled on the needed tables (delta links for true deletes). — 01
25. ServiceNow field survey: which field holds `acknowledged_at` and customer impact (ties to D13). — 01, 02
26. A tombstoned record restored in the source with an older `sys_updated_on` stays deleted after dedupe; decide whether reconciliation bumps `_source_updated_at`. — 01, 02

### Phase 7 (evaluation)

27. Pin the `local-judge` model: a different family from the Writer's `local-30b`, able to run next to the eval workload. — 05, 11
