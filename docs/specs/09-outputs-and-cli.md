# 09 — Outputs and CLI

Status: Draft v2 · 2026-09-24 · Depends on: 00, 02, 04, 06, 07, 08, 10. Phase 5.

v2 aligns with shared contracts v2 (spec 00 §4, §6, §7, §11, §12) and data model v2 (spec 02).

## 1. Purpose and scope

This spec defines everything a person touches: generated reports (`herness/reports/`), the Streamlit dashboard and chat UI (`app/`), and the Typer CLI (`herness/cli.py`). These components compute nothing new. They read the warehouse (through `CURRENT`), the ops store and the Writer's `ReportDraft`, and render them with evidence links.

In scope: the fields of `ReportDraft` the renderer requires, report outline and formats, dashboard pages, chat UI, user identity and roles in the UI, the CLI command surface and exit codes, user-facing error messages, the `chat_session` and `chat_message` tables (owned here, schema in spec 02 §5.5), `config/app.yaml`, and the `ReportManifest` type.

Out of scope: how the Writer produces `ReportDraft` and how chat answers are produced (06), recommendation, decision and outcome semantics (07), job queue, worker and chat-hours policy (08), score formulas and the portfolio optimizer (04), config loading, secrets, reverse proxy/SSO, retention and deployment behaviour (10), eval suites (11).

## 2. Responsibilities

- Render a review run to self-contained HTML, Markdown and optional PDF in `data/reports/<run_id>/`, with every number linked to its evidence entry.
- Refuse to render a number without a `query_id` (strict mode).
- Serve a read-only dashboard over the promoted warehouse and the ops store; write to the ops store only through `herness.store.ops` and the owning specs' functions.
- Run the chat UI: persist `chat_session` / `chat_message`, stream answers from spec 06's `ChatService`, show evidence and verification status, collect feedback and corrections.
- Enforce roles (viewer / reviewer / admin; `denied`) from spec 10's allowlist for every write action.
- Register every CLI command (including those whose behaviour other specs own), with stable exit codes and a `--json` mode.
- Show every failure as "what went wrong + how to fix it".

## 3. Interfaces

### 3.1 Modules owned here

```text
herness/reports/
  contract.py      load_draft(run_id) -> ReportDraft; check_render_contract(draft, ...) -> None  (raises ReportContractError)
  render.py        render_run(run_id, formats, out_dir=None, strict=True, now=None) -> ReportManifest
  charts.py        bar_h(), step_budget(), sparkline(), dot_z() -> SVG str (pure Python, no JS)
  templates/       base.html.j2, funding_review.html.j2, org_review.html.j2, *.md.j2, partials/
app/
  Home.py          Overview page
  pages/           01_Funding_Ranking.py … 11_Chat.py (§5.4)
  common/          wh.py (warehouse access), queries.sql.yaml, auth.py, widgets.py (evidence expander, badges), sanitize.py
herness/cli.py     Typer app `herness`
```

Number formatting, numeral scanning and evidence collection are helpers inside `render.py` / `contract.py` (spec 00 §3 layout). `render_run` never calls a model.

`ReportManifest` (owner 09, `herness/core/types.py`): `run_id`, `kind`, `build_id`, `template_version`, `rendered_at`, `publishable`, `files: dict[str, str]` (name → SHA-256), `numbers_total`, `numbers_linked`, `uncited: list[dict]` (`{"where", "text"}`), `evidence_entries`, `warnings: list[str]`.

### 3.2 Consumed interfaces

| Interface | Owner | Used for |
|-----------|-------|----------|
| `ReportDraft` (+ `Section`, `Paragraph`, `RecommendationItem`, `RankedEntity`, `Coverage`) at `data/reports/<run_id>/draft.json` | 06 | Report input (spec 00 §12.2) |
| `NumberRef`, `VerificationResult` | 05 | Numbers and verification status |
| `ChatService.answer(session_id, text, user_ref, mode) -> Iterator[ChatEvent]` in `herness/harness/pipelines/chat.py`; `ChatEvent`, `ChatAnswer` | 06 | Chat streaming (spec 00 §12.3) |
| `jobs.chat_policy(now) -> ChatMode`, `jobs.chat_next_live_at(now)` | 08 | Chat hours banner and routing; ETA shown for `defer` |
| `jobs.enqueue`, `jobs.cancel`, `jobs.retry`, `jobs.get`, `jobs.list_jobs`, `jobs.worker_alive`, `jobs.status_snapshot`; `worker` table heartbeats | 08 | CLI and dashboard job control |
| `MemoryStore.propose`, `approve`, `reject`, `decide` | 07 | Corrections, memory review items, recommendation decisions |
| `optimize_portfolio(scenario, persist=False, run_id=...) -> PortfolioResult` | 04 | Ad-hoc what-if only (`herness score --scenario`, dashboard Portfolio page). Report custom scenarios come from `ReportDraft.portfolio_custom` |
| `load_catalog()`, `MetricCatalog.describe()`, `run_scoring(build_id, steps)` | 04 | `score`, `metrics list`, Change/Delivery page metric sets |
| `get_redactor().redact(text)` | 10 | Chat input before storage; candidate titles at display |
| `audit(event, actor, **fields)` | 10 | `review_decision`, `recommendation_decision`, `admin_action`, `auth` |
| Config sections `security.ui.*`, `retention.*` | 10 | Identity, roles, bind address, chat/report retention |

### 3.3 Chat events the UI handles

`ChatEvent` is defined by spec 06 with exactly these kinds. `ChatMode` (spec 08) is `Literal["live", "small_model", "defer", "cloud"]`.

| `type` | Payload (spec 06 §4.5) | UI effect |
|------|---------|-----------|
| `mode` | `mode` (`ChatMode`), `message` | Banner above the answer (§5.5 step 7) |
| `token` | `text` | Appended to the streamed draft |
| `tool` | `name`, `query_id` (nullable), `ok` | "Running query…" line in a collapsed status box |
| `evidence` | `query_id` | Added to the evidence list (details from ops `evidence`) |
| `verification` | `result` (`VerificationResult`), `status` (`verified` \| `partial` \| `unverified`), `removed_claims` | Badge; removed claims listed |
| `escalated` | `run_id`, `job_id` | Escalation notice with link to Runs & Traces (§5.5 step 8) |
| `final` | `answer` (`ChatAnswer`: `text`, `numbers`, `query_ids`, …), `run_id` | Draft replaced by the final text; UI reloads the assistant `chat_message` row (written by `ChatService` before this event) for numbers and evidence links |
| `error` | `error_type`, `message`, `hint` | Error box with Retry |

**Row ownership.** The UI inserts only the user `chat_message` row (and `chat_session` rows). `ChatService` (spec 06) writes every assistant row, including queued and escalation answers. The UI never inserts an assistant row; it only updates `feedback` / `feedback_note` on existing rows and keeps streaming state in `st.session_state`.

## 4. Data contracts

### 4.1 Fields of `ReportDraft` the renderer requires

Spec 06 owns `ReportDraft` and guarantees these fields (spec 00 §12.2). The renderer ignores fields it does not list here.

| Field | Required use |
|-------|--------------|
| `schema_version` | Must equal a version the templates support (`"1"`); otherwise `ReportContractError` |
| `run_id`, `kind` (`funding_review` \| `org_review`), `depth`, `profile`, `build_id`, `title` | Header; `build_id` selects the warehouse file |
| `banners` (subset of `dq_warnings`, `unconfirmed_weights`, `partial_run`, `hybrid_fallback`, `budget_exhausted`) | Banner strip; the renderer also derives banners from data (§5.2) and shows the union |
| `sections[]`: `id` ∈ `executive_summary`, `recommendations`, `portfolio`, `org_scorecards`, `actions`, `retrospective`, `risks_and_caveats`, `method`; `title`; `paragraphs[]` | Prose placed into the outline slot with the same `id` |
| `Paragraph.text`, `Paragraph.numbers: list[NumberRef]`, `Paragraph.finding_ids` | Markers in `text` resolve to `numbers` of the same paragraph |
| `recommendations[]`: `rank`, `kind`, `target_type`, `target_id`, `rec_id` (null until spec 07 wrote it), `headline`, `summary`, `numbers: list[NumberRef]`, `expected_metric`, `*_ref` ids (`expected_usd_ref`, `expected_delta_ref`, optional `confidence_ref`, `effort_usd_ref`), `action_levers[]` (`entity_type`, `entity_id`, `metric`, `delta_usd_ref`), `finding_ids` | Recommendation cards; every `*_ref` is a `NumberRef.id` in the item's own `numbers` |
| `ranked_entities[]` | Shown in the run appendix as the ranked list the run produced (same order as eval grading, spec 11) |
| `flags` | Extra caveats (e.g. partial coverage per entity), listed in the caveats section |
| `coverage` (`publishable`, task and finding counts) | Header stats; `publishable = false` → "Not for decision" watermark |
| `contested`, `removed`, `dead_tasks` | Caveats section and run appendix |
| `query_ids` | Seed of the evidence appendix |
| `verification` | Header: gate 2 result |

Rules (spec 00 §12.1):

1. Model-written text (`Paragraph.text`, `headline`, `summary`) contains numbers only as `[[<id>]]` with `<id>` matching `n[0-9]+`, resolving to a `NumberRef.id` in the same object's `numbers`.
2. A numeral outside a marker is **uncited** unless it matches `config/app.yaml: reports.allowed_numeral_patterns` (the same list the Verifier uses).
3. Every `NumberRef.query_id` exists in ops `evidence` or in `meta.evidence` of `build_id`.
4. Every non-null `rec_id` exists in `recommendation` with the same `run_id`; every `finding_id` exists in `finding` with `status = 'verified'`.
5. `NumberRef.format` drives display; when absent, `unit` picks a default (`usd` → `usd_compact`, `pct` → `pct1`, `count` → `int`, `hours` → `hours1`, `minutes` → `minutes0`, `ratio` → `ratio2`, else plain).

Structured tables (funding ranking, portfolio, scorecards, levers, outcomes, DQ) are not taken from the draft. The renderer reads them from `score.*`, `meta.*` and ops tables for the same `build_id`, so table numbers are deterministic and carry the row's `query_ids`.

### 4.2 Report output layout (`data/reports/<run_id>/`, spec 00 §4)

| File | Writer | Content |
|------|--------|---------|
| `draft.json` | 06 | `ReportDraft` after Verifier gate 2 (input; never modified here) |
| `report.html` | 09 | Self-contained: inline CSS and SVG, no scripts, no external URL, CSP meta tag (§9.3) |
| `report.md` | 09 | Same outline, GitHub-flavoured Markdown with evidence anchors |
| `report.pdf` | 09 | Only when requested and WeasyPrint is importable |
| `manifest.json` | 09 | `ReportManifest` |

Files are written as `*.tmp` and moved with `os.replace`; `manifest.json` is written last. Retention follows `retention.reports_days` (spec 10, `herness maintenance purge`).

### 4.3 Evidence entry (appendix and UI expander)

| Field | Source |
|-------|--------|
| `query_id`, `sql`, `params`, `row_count`, `executed_at`, `result_sample` | ops `evidence` if present, else `meta.evidence` of the run's build |
| `build_id` | ops `evidence.build_id`, or the warehouse's own `build_id` |
| `used_by` | `NumberRef` locations, table rows and `finding_id`s that cite it (computed at render) |

`meta.evidence.result_sample` exists from data model v2 (spec 02 §4.7). Only for warehouse files built before v2, where the column is missing, the entry shows "Sample not stored for this build".

HTML anchor `id="ev-<query_id>"`. Every rendered number is `<a class="num" href="#ev-<query_id>" title="<column> · <row_key>">$1.84M</a>`. Markdown uses `[$1.84M](#ev-q_3f9a0c1d2e4b5a67)` with `<a id="ev-q_…"></a>` before each appendix entry.

### 4.4 Chat tables (owned here; columns in spec 02 §5.5)

- `chat_session`: one row per conversation. `user_ref` per §9.2. `title` = first question after redaction, ≤ 60 chars. `summary` = rolling summary maintained by `MemoryStore.session_save_turn` (spec 07 §5.12), called by spec 06 `ChatService`.
- `chat_message`: `role` ∈ `user`, `assistant`, `system`. User rows are inserted by the UI, with `content` stored after `redact()` (spec 10). Assistant rows are written only by `ChatService` (spec 06 §5.13), never by the UI; this spec defines their meaning: `content` = final text with markers already rendered to plain numbers, `status` (`queued` → `streaming` → `done` \| `failed`), `verified` from the `VerificationResult`, `run_id` (the chat run), `query_ids`, `meta` (`{"mode", "model", "job_id", "latency_ms", "numbers": [NumberRef…]}` so evidence links survive reload). `feedback` / `feedback_note` are the only assistant-row fields the UI updates.
- Retention: `retention.chat_days` of inactivity, purged by `herness maintenance purge` (spec 10) through `herness.store.ops.purge_chat(before)`.

## 5. Behavior

### 5.1 Report rendering steps

1. Load `run`. Render when `run.status` ∈ `done`, `partial`; other statuses → `ReportContractError("run not finished")`.
2. Load `data/reports/<run_id>/draft.json`, validate as `ReportDraft`, then check §4.1 rules.
3. Open `wh-<build_id>.duckdb` read-only (the run's build, not necessarily `CURRENT`). If retired, fail with the fix "re-run the review on the current build".
4. Resolve markers, format numbers, read tables from `score.*`, `meta.dq_result`, `meta.build`, and ops `recommendation`, `decision_log`, `outcome`, `task`; derive banners (§5.2).
5. Scan model text for uncited numerals. Strict mode (default): any hit → `ReportContractError` listing the field path and span. Non-strict: the span is wrapped in `<mark class="uncited">` and listed in `manifest.uncited`.
6. Collect evidence entries for every cited `query_id` (markers, table rows, findings, draft `query_ids`), ordered by first use.
7. Render Jinja2 (`autoescape=True` for `.html.j2`, `StrictUndefined`). Model text is escaped first; markers are then replaced with renderer-built `Markup` anchors.
8. Write files (§4.2); log `report_rendered` with counts.

Spec 06 calls `render_run(run_id, formats=app.reports.formats)` as the last step of every review pipeline. The CLI can re-render any time with `herness report render <run_id>`.

### 5.2 Report outline

| # | Section (draft section id) | Content | Sources |
|---|---------|---------|---------|
| 0 | Header | Title, `run_id`, kind, `build_id`, data as of (max of `meta.build.source_watermarks`), depth, profile, rendered time, coverage and gate-2 stats; "Not for decision" watermark when `coverage.publishable = false` | draft, `meta.build` |
| 1 | Banners | Union of draft `banners` and derived ones: `unconfirmed_weights` (list every `weights.yaml` key with `unconfirmed: true`, D2), `dq_warnings` (`meta.dq_result.passed = false`), `partial_run` (`run.status = 'partial'` or dead tasks), `hybrid_fallback` / off-network profile (`run.profile ≠ 'local'`), `budget_exhausted` | draft, config, `meta.dq_result`, `run` |
| 2 | Executive summary (`executive_summary`) | Paragraphs, 3–5 key numbers | draft |
| 3 | Funding: top-N recommendations (`recommendations`) | One card per `kind = 'fund'` item: headline, summary, expected $, effort $, priority and WSJF from `score.funding`, confidence label (`high` ≥ 0.7, `medium` ≥ 0.4, else `low`), findings; `score.funding` top-N table with horizontal bar chart of `priority`; rows with `unconfirmed = true` marked | draft, `score.funding` |
| 3 | Org: top-N orgs/teams to improve (`recommendations`) | One card per `kind = 'org_action'` item; `score.org` composite rank top-N; z-score dot plot | draft, `score.org` |
| 4 | Portfolio under budget scenarios (`portfolio`) | Per `score.portfolio.scenario`: budget, `solver_status`, selected candidates by `order_rank`, cumulative `expected_impact_usd` step chart vs budget. Each entry of `draft.portfolio_custom` (one `PortfolioResult` per custom `--budget` scenario, computed by spec 06 during the run) is rendered first as its own scenario block with the same table and chart; its numbers link to the result's input `query_ids` in ops `evidence`. The renderer never re-runs the optimizer, so the report shows exactly what the Writer cited | `score.portfolio`, `draft.portfolio_custom`, ops `evidence`, draft |
| 5 | Org scorecards (`org_scorecards`) | Per entity in scope: metric, value, `peer_group`, `peer_median`, z-score, trend sparkline, `sample_size`, composite, rank, `flags` | `score.org`, `metrics.metric_value` |
| 6 | Recommended actions (`actions`) | `score.action_lever` top levers by `delta_usd`, `rationale_template` filled from `template_params`, `target_kind` shown; draft `action_levers` linked to their cards | `score.action_lever`, draft |
| 7 | Did last quarter's recommendations work? (`retrospective`) | Recommendations of the same `run.kind` created `reports.prior_outcomes_window_days` before this run: summary, latest `decision_log` row, `outcome` rows per `measurement` (metric, baseline, actual, delta, verdict, `query_id`); counts per verdict. Empty state: "No measured outcomes yet." | `recommendation`, `decision_log`, `outcome`, draft |
| 8 | Data quality and caveats (`risks_and_caveats`) | Failed DQ checks, unmapped share, unconfirmed weights, `dead_tasks` (role, objective, first line of `last_error`), `contested` findings, `removed` items, `flags` | `meta.dq_result`, draft |
| 9 | Method (`method`) | Depth, profile, models per role, verification approach | draft, `run` |
| 10 | Evidence appendix | Each §4.3 entry: SQL (monospace), params, build, row count, first `reports.evidence_sample_rows` rows of `result_sample` (cells truncated to 200 chars), used-by back-links | ops `evidence`, `meta.evidence` |
| 11 | Run appendix | `ranked_entities`, task counts by role/status, `run.token_usage`, `cost_usd`, `config_hash` | draft, `run`, `task` |

Charts are inline SVG from `charts.py`; each is followed by a small data table that carries the evidence links. Colours are CSS variables; `prefers-color-scheme` gives light and dark; `@media print` expands all `<details>`.

### 5.3 Dashboard runtime

- **Warehouse access** (`app/common/wh.py`): `get_conn()` re-reads `CURRENT` at most every `app.current_recheck_s` (60 s) per session and returns a read-only DuckDB connection cached by `st.cache_resource` keyed on `build_id`. A change shows a toast "Data refreshed to build <id>". Connections to non-current builds close after 5 min grace; spec 02 §7 tolerates the brief Windows file lock.
- **Named queries only.** Pages call `wh.query(name, **params)`; SQL lives in `app/common/queries.sql.yaml`, params are bound, each query has a row cap (`app.page_row_limit`).
- **Caching.** `st.cache_data(ttl=app.cache_ttl_s.warehouse, max_entries=500)` keyed on `(build_id, name, params)`, so promotion invalidates naturally. Ops reads use `ttl=app.cache_ttl_s.ops` and are cleared after the same session writes.
- **Writes** go through `herness.store.ops` or the owning spec's function (§3.2), check the role server-side, and write an `audit` line.
- **Evidence widget** (`widgets.evidence(query_ids)`): expander per `query_id` with SQL (`st.code`), params, build, row count and `result_sample` as a dataframe.

### 5.4 Dashboard pages

| File | Page | Data | Controls and actions | Role |
|------|------|------|----------------------|------|
| `Home.py` | Overview | `meta.build` (current + last 3), `meta.dq_result`, last 10 `run`, `job` counts by status, oldest queued age, running jobs with `lease_expires_at`, failed in 24 h, `worker` rows (`gpu_class_loaded`, `heartbeat_at`), `source_health` | Links to failing items | viewer |
| `01_Funding_Ranking.py` | Funding Ranking | `score.funding` (incl. `title`, `unconfirmed`, `flags`), `score.funding_attribution` | Filters: `candidate_type`, project, org, min confidence, top N; sort by `priority` or `wsjf`; row → evidence, attribution breakdown by `tier`, linked findings and recommendations | viewer |
| `02_Portfolio.py` | Portfolio Scenarios | `score.portfolio` | Scenario selector, compare two scenarios, step chart budget vs cumulative impact, `solver_status` | viewer |
| `03_Org_Scorecards.py` | Org Scorecards | `score.org`, `metrics.metric_value` (last 8 periods), `core.org`, `core.team`, `metrics.org_closure` | Entity type, org subtree, metric; peer table (value vs `peer_median`, z, `sample_size`); trend line; evidence | viewer |
| `04_Action_Levers.py` | Action Levers | `score.action_lever` | Entity filter; ranked by `delta_usd`; filled rationale; evidence | viewer |
| `05_Clusters.py` | Recurring Issue Clusters | `enrich.cluster`, `enrich.cluster_member`, `enrich.text_redacted` | Filter by service, root cause, min size; detail: label, `top_terms`, size over time, 20 members with highest `membership_prob` showing `number`, `opened_at`, priority and **redacted text only** | viewer |
| `06_Change_Health.py` | Change Health | `metrics.metric_value` for catalog domain `change`, `enrich.incident_change_link` counts | Service / team / period filters; change-caused incident list (numbers, no text) | viewer |
| `07_Delivery_Health.py` | Delivery Health | `metrics.metric_value` for catalog domain `delivery` | Project / team / period filters | viewer |
| `08_Recommendations.py` | Recommendations & Decisions | `recommendation` (`confidence`, `confidence_basis`), latest `decision_log` per `rec_id`, `outcome` | Filter by run, kind, decision state; Accept / Reject / Defer with reason (≥ 10 chars) and optional `effective_at` → `MemoryStore.decide`; outcome table with verdict and evidence | decide: reviewer |
| `09_Review_Queue.py` | Review Queue | `review_item` with `status = 'pending'` | Tab per `kind`; payload rendered per kind (schemas in 03, 04, 07); Approve / Reject with note. `memory_write` → `MemoryStore.approve/reject`; other kinds → `ops.decide_review_item` (owners act on approved items at their next step) | reviewer; `weight_change`: admin |
| `10_Runs_and_Traces.py` | Runs & Traces | `run`, `task`, `finding` counts, `resilience_event`, trace JSONL | Run list (kind, depth, status, duration, tokens, `cost_usd`); run detail: tasks by status with `last_error`, dead tasks highlighted, trace viewer (event filter, 200 per page); actions: resume run, retry/cancel job, re-render report | view: viewer; actions: admin |
| `11_Chat.py` | Chat | `chat_session`, `chat_message`, ops `evidence` | §5.5 | viewer |

Every page shows the current `build_id` and data-as-of time in the sidebar, and the D2 banner while any weight is unconfirmed. No `CURRENT` → every page shows only "No promoted build yet. Run `herness pipeline`."

### 5.5 Chat UI

1. **Session.** Sidebar lists the user's sessions (`user_ref` = current user) by `last_active_at`; "New chat" creates a row. Sessions are private to their owner in the UI.
2. **Turn.** The UI inserts the redacted user message, then iterates `ChatService.answer(...)` inside `st.chat_message("assistant")`; token events stream via `st.write_stream` in muted style labelled "Draft — verifying numbers…". One in-flight turn per session.
3. **Verification.** Badge from the `VerificationResult`: `Verified`, `Partially verified` (with 06's notice "Some figures could not be verified and were removed."), or `Unverified`. The final text replaces the draft.
4. **Evidence.** Under each assistant message an expander "Evidence (n queries)"; each number in the answer links to its entry.
5. **Feedback.** Thumbs up/down plus optional note → `ops.update_chat_message`. Feedback does not change memory.
6. **Corrections.** "Correct a fact" form (≤ 1000 chars, optional entity) → `MemoryStore.propose(MemoryProposal(layer="semantic", kind="user_correction", …, via="chat"))`. The UI confirms: "Saved as pending. A reviewer must approve it before it affects answers or scores."
7. **Chat hours.** Before each turn the UI calls `jobs.chat_policy(now)` and passes the `ChatMode` to `ChatService`:
   - `live`: normal, no banner;
   - `small_model`: info banner "Outside chat hours: answering with a reduced model; answers may be less thorough.";
   - `defer`: warning banner "The GPU is running scheduled work. Your question is queued and the answer will appear here after <next slot>." `<next slot>` comes from `jobs.chat_next_live_at(now)`. `ChatService` (spec 06) enqueues the `chat` job (`idem_key = chat:<session_id>:<message_id>`) and writes the assistant placeholder with `status = 'queued'`; the UI never enqueues it. The page polls every `chat.poll_queued_s` while open;
   - `cloud`: banner "Answered by an off-network model under the approved data policy."
8. **Escalation.** On an `escalated` event the UI shows the new `run_id` and a link to Runs & Traces; the executive summary arrives later as an assistant message (spec 06 §5.13).
9. **Limits.** Question ≤ `chat.max_question_chars`; history passed per spec 06/07.

### 5.6 CLI

Global options: `--config-dir PATH`, `--data-dir PATH`, `--profile NAME`, `--set a.b.c=<yaml>` (repeatable; precedence per spec 10 §4.1), `--json`, `--quiet`, `--verbose`, `--version`. `cli.main()` calls `install_socket_guard` first (spec 10).

Long-running commands (`sync`, `build`, `enrich`, `score` before promotion, `pipeline`, new `report`/`review`, `resume`, `eval`, `distill`) enqueue a job (spec 08). With `--wait` (default) the CLI follows progress from `job` and `task` rows every `cli.poll_interval_s`. If no `worker` row has `heartbeat_at` within 60 s, it warns "No worker is running. Start one with `herness worker`." and keeps waiting. `--no-wait` prints the `job_id` and exits 0. Ctrl+C while waiting detaches (the job keeps running) and exits 130.

Behaviour of commands marked with a spec number is defined there; this spec registers them and maps errors to exit codes.

| Command | Arguments / options | Does | Role | Behaviour |
|---------|--------------------|------|------|-----------|
| `init` | `--force` | Create `data/` layout, copy config templates if absent, `ops.migrate()`; then tells the user to run `secrets init` | admin | 09 |
| `doctor` | `--fix-hints`, `--sources` | Spec 10 §5.6.3 checks plus: `CURRENT` readable and build age, ops migrations current, worker heartbeat, WeasyPrint import; `--sources` calls each `Connector.check()`. PASS/WARN/FAIL table with fix per row | any | 10, 09 |
| `config validate` | `--profile P`, `--offline`, `--strict` | Validate all config files | any | 10 |
| `config show` | `--profile P` | Effective config, secrets as `secret:<name>` | admin | 10 |
| `config hash` | — | Print `config_hash` | any | 10 |
| `secrets init` / `set NAME` / `status` | | HMAC key creation and escrow prompt; store a secret; list refs and presence | admin | 10 |
| `deploy render` / `pull [--allow-download]` / `up reasoning\|decider` / `down` / `rollback` / `prune` | | Compose env rendering, pinned pulls, container lifecycle | admin | 10 |
| `gpu load CLASS` / `unload` | | Manual GPU class switch; with a live worker it sets `worker.requested_class` for the arbiter, never starts containers directly (spec 08 §5.8) | admin | 08 |
| `sync` | `[SOURCE]`, `--entity E` (repeatable), `--full`, `--backfill --from D --to D`, `--reconcile`, `--check-mapping`, `--discover-fields` | Job `sync` / `reconcile` | admin | 01 |
| `build` | `--wait/--no-wait` | Job `build_pipeline`, stages 000–299 on a new unpromoted build | admin | 02 |
| `enrich` | `--build-id ID`, `--stage S`, `--depth D` | Enrichment stages on an unpromoted build | admin | 03 |
| `score` | `--build-id ID`, `--step S`, `--scenario NAME\|USD` | Before promotion: `run_scoring`; after promotion with `--scenario`: `optimize_portfolio(persist=False)` and print result | admin | 04 |
| `metrics list` | | `MetricCatalog.describe()` | any | 04 |
| `pipeline` | `--from-stage build\|enrich\|score\|dq\|promote`, `--build-id ID` | build → enrich → score → dq → promote | admin | 02, 08 |
| `report funding\|org` | `--depth`, `--budget USD` (repeatable), `--top-n N`, `--format html,md,pdf`, `--out DIR`, `--no-strict`, `--open`, `--wait/--no-wait` | Enqueue a review job (priority 80), wait, print report paths. Each `--budget` becomes `Scenario("custom_<usd>")` in spec 06 `RunRequest.scenarios`; spec 06 runs `optimize_portfolio(persist=False, run_id=...)` and stores the results in `ReportDraft.portfolio_custom`, which the renderer shows | admin | 06, 09 |
| `report render` | `RUN_ID`, `--format`, `--out DIR`, `--no-strict` | `render_run` only, no model calls | viewer | 09 |
| `review` | `--kind funding\|org\|both`, `--depth`, `--at ISO_TIME`, `--wait/--no-wait` | Enqueue review job(s) now or at `--at` | admin | 06, 08 |
| `resume` | `RUN_ID`, `--retry-dead`, `--force`, `--wait/--no-wait` | Re-enqueue the run; finished tasks are not redone | admin | 08 |
| `decide` | `REC_ID`, `accepted\|rejected\|deferred`, `--reason TEXT` (required), `--effective-at DATE` | `MemoryStore.decide` | reviewer | 07 |
| `chat` | `--session ID`, `--new` | Terminal chat over `ChatService`; `/evidence q_…`, `/sessions`, `/correct <text>`, `/quit`; same mode messages as §5.5 | viewer | 09 |
| `ui` | `--port N` | Launch Streamlit (§9.1) | any | 09 |
| `worker` | `--gpu-class none,reasoning,decider,large`, `--concurrency N`, `--once` | Worker loop (spec 08 §3.8) | admin | 08 |
| `status` | — | `CURRENT` build and age, last pipeline, DQ warnings, workers, running/queued jobs, last 5 runs, chat mode now | viewer | 08, 09 |
| `jobs list` / `cancel JOB_ID` / `retry JOB_ID` | `--status`, `--kind`, `--limit` | `list_jobs`, `cancel`, `retry` | list: viewer; others: admin | 08 |
| `review-queue list` / `approve ITEM_ID` / `reject ITEM_ID` | `--kind`, `--status`, `--note` (required for reject) | As the Review Queue page | reviewer (`weight_change`: admin) | 09 |
| `memory list` | `--layer`, `--status`, `--limit` | `memory_item` rows, content truncated to 120 chars | viewer | 07 |
| `memory approve` / `reject` | `ITEM_ID` (review item), `--note` | `MemoryStore.approve/reject` | reviewer | 07 |
| `memory export-lora` | `--out DIR` | LoRA training export | admin | 07 |
| `memory purge` | `--author-ref HASH` | Erase an author's memory content | admin | 07 |
| `eval` | `--suite golden\|classifier\|PATH.yaml`, `--profile NAME`, `--compare PROFILE`, `--depth fast\|standard\|deep`, `--ids G01,F01`, `--tags TAG`, `--repeat N`, `--mock-llm DIR`, `--no-judge`, `--resume RUN_ID`, `--baseline NAME`, `--set-baseline NAME`, `--compare-runs RUN_ID,RUN_ID,...` | Eval suites; enqueues one `eval` job (spec 11 §3.2) | admin | 11 |
| `distill` | `--active` | Laya distillation job | admin | 03 |
| `laya status` / `accept VERSION` / `rollback VERSION` | | Student model gate | admin | 03 |
| `privacy delete` | `--record-id ID`, `--reason-ref REF` | Deletion request workflow | admin | 10 |
| `maintenance backup` / `purge` | | Backup and retention | admin | 10 |

CLI identity is the OS user, mapped through `security.ui.roles` like the dashboard; `denied` users can run only `--help`, `--version`, `doctor` and `config validate`.

### 5.7 JSON output

With `--json`, stdout carries exactly one JSON object; logs go to stderr.

```json
{"ok": true, "command": "jobs list", "data": {"schema": "cli/1", "jobs": [ ... ]}, "warnings": [], "error": null}
{"ok": false, "command": "report render", "data": null, "warnings": [],
 "error": {"type": "ReportContractError", "exit_code": 12, "message": "2 uncited numbers in the draft",
           "hint": "Re-run the review, or render with --no-strict to inspect.", "details": {"where": ["sections[0].paragraphs[1]"]}}}
```

`data` shapes: list commands → `{"<plural>": [row, …]}` with spec 02 column names; job commands → `{"job_id", "status", "run_id"?}`; `report` → `ReportManifest`; `doctor` → `{"checks": [{"name", "status", "detail", "fix"}]}`.

### 5.8 Exit codes

| Code | Meaning | From |
|------|---------|------|
| 0 | Success (also `--no-wait` enqueue) | |
| 1 | General failure; `doctor` FAIL; invalid config in `config validate`; `PolicyViolation` | other `HernessError` |
| 2 | Usage error | Typer |
| 3 | Configuration error | `ConfigError` |
| 4 | Source auth or availability failure after retries | `AuthError`, `SourceUnavailable`, `RateLimited`, `CircuitOpen` on a source |
| 5 | Build blocked (DQ error or SQL failure) | `SchemaViolation`, DQ |
| 6 | Finished with dead tasks or `run.status = 'partial'` (report still written) | run status |
| 7 | Not found (run, job, item, memory, build, record) | lookup |
| 8 | Store busy or another writer holds the lease | `StoreBusy` |
| 9 | Model or GPU unavailable after the fallback chain | `ModelUnavailable`, `ModelRefused`, `CircuitOpen` on `model:*` |
| 10 | Budget exceeded | `BudgetExceeded` |
| 11 | Permission denied | `PermissionDenied` |
| 12 | Report contract violation | `ReportContractError` |
| 13 | Off-network call blocked | `EgressBlocked` |
| 130 | Interrupted | Ctrl+C |

## 6. Errors and resilience

Every user-facing error prints two lines: `Error: <what went wrong>` and `Fix: <what to do>`. The dashboard shows the same text in `st.error` with a collapsed "Details" block (error type, `run_id`/`job_id`, log path). Stack traces only with `--verbose` or in the log.

| Situation | Message (what) | Fix |
|-----------|----------------|-----|
| `CURRENT` missing | No promoted warehouse yet. | Run `herness pipeline`. |
| Run's build retired | Build `<id>` used by this run was deleted by retention. | Re-run the review: `herness report funding`. |
| `draft.json` missing or invalid | Run `<id>` has no valid report draft. | Check `herness status`; resume with `herness resume <id>`. |
| Uncited numerals (strict) | N numbers in the draft have no evidence. | Re-run the review, or `--no-strict` to inspect. |
| WeasyPrint missing and `pdf` requested | PDF engine not installed. | `uv sync --extra pdf` (GTK/Pango on Windows, spec 10). |
| Lease held | Another pipeline is running (job `<id>`). | Wait, or `herness jobs list`. |
| No worker | No worker is running; the job is queued. | Start `herness worker` or the `herness-worker` task (spec 10 §5.6.4). |
| Model endpoint down | Reasoning model not reachable at `<url>`. | `herness deploy up reasoning`; `herness doctor`. |
| Source auth | `<source>` rejected the credentials. | `herness secrets set <name>`; `herness doctor --sources`. |
| Egress blocked | An off-network call was refused by the data policy. | Use profile `local`, or record the approval in `herness.yaml` (spec 10). |
| Ops store busy (after retries) | Ops store is locked. | Retry; check for a stuck process in `herness status`. |
| Role missing | You need the reviewer role to approve items. | Ask an admin to add you to `security.ui.roles.reviewers`. |

- Rendering is idempotent; its only side effects are files in `data/reports/<run_id>/`. Leftover `*.tmp` files are removed on the next render.
- Partial runs still render with the banner, caveats and watermark; CLI exits 6.
- Each dashboard widget block catches its own exceptions and shows an error card; the rest of the page renders.
- UI ops writes retry on `StoreBusy` per spec 08; on final failure the form keeps the user's input.
- Chat: `ModelUnavailable` → `error` event, assistant row marked `failed` by `ChatService`, Retry button (re-sends the same user row); `BudgetExceeded` → partial answer kept, badge `Unverified`. If `audit` fails, the action does not happen (spec 10 §6).

## 7. Configuration

`config/app.yaml` (owner 09, loaded and validated by spec 10):

```yaml
app:
  cache_ttl_s: {warehouse: 3600, ops: 10}
  current_recheck_s: 60
  page_row_limit: 5000
chat:
  max_question_chars: 4000
  poll_queued_s: 15
reports:
  formats: [html, md]            # pdf optional
  top_n: 10
  strict_numbers: true
  evidence_sample_rows: 20
  allowed_numeral_patterns: ['\b(19|20)\d{2}\b', '\d{4}-\d{2}-\d{2}', 'Q[1-4] \d{4}',
                             '(INC|CHG|PRB)\d+', '[A-Z][A-Z0-9]+-\d+']   # shared with the Verifier (00 §12.1)
  prior_outcomes_window_days: [60, 120]
cli:
  poll_interval_s: 2
```

Owned elsewhere and read here: `security.ui.{bind, port, expose.enabled, expose.trusted_proxy, expose.identity_header, roles.admins, roles.reviewers, roles.default_role}` (spec 10 §7.3; bind, port and identity header live only there) and `retention.{chat_days, reports_days}` (spec 10, `herness.yaml`); chat windows and `schedule.chat.off_hours` in `config/resilience.yaml` (spec 08); `weights.yaml` unconfirmed flags (spec 04, D2).

## 8. Performance targets

Reference: 5M-incident warehouse, single PC per spec 02 §9.

| Operation | Target |
|-----------|--------|
| Dashboard page first load (cold cache) | p95 < 2 s; warm < 0.5 s |
| Cluster detail with 20-member sample | < 1 s |
| Evidence expander open | < 300 ms (ops lookup by PK) |
| Chat time to first token (reasoning model loaded) | < 3 s, excluding model queueing |
| Report render (HTML + MD), ≤ 300 evidence entries | < 30 s; PDF adds < 30 s |
| `report.html` size | < 5 MB |
| `herness --help` | < 1 s (lazy imports of duckdb, streamlit, torch) |

How: pages read `score.*`, `metrics.metric_value`, `enrich.cluster` and keyed lookups only; the only `core.*`/`enrich.cluster_member` access is the capped member sample by `cluster_id`; every query has a row cap.

## 9. Security

### 9.1 Binding and exposure

- `herness ui` starts Streamlit with `--server.address <security.ui.bind>` (default `127.0.0.1`), `--server.port <security.ui.port>` (default 8501; `herness ui --port` overrides for one launch), `--server.headless true`, `--browser.gatherUsageStats false`, `--server.enableXsrfProtection true`, `--server.enableCORS true`.
- LAN access only behind the reverse proxy with SSO (spec 10). The identity header `security.ui.expose.identity_header` is trusted only when `security.ui.expose.enabled` and `security.ui.expose.trusted_proxy` are set and the app is bound to loopback.
- CSRF: Streamlit state changes travel over its websocket session, not form posts; XSRF protection stays on and the proxy enforces same-origin. No custom HTTP endpoints are added.

### 9.2 Identity and roles

- Identity: OS user in local mode and for the CLI; the trusted header behind the proxy.
- `user_ref = hex(HMAC-SHA256(key, lower(identity)))[:32]`, key from secret `ui_user_ref_key` (spec 10, created by `herness secrets init`; independent of the redaction key so key rotation there does not orphan sessions). Only `user_ref` is stored (`chat_session.user_ref`, `decision_log.decided_by`, `review_item.decided_by`, memory provenance). Display names come from hashing the usernames in `security.ui.roles` at startup; unknown refs show as `user:<first 8>`.
- Roles from `security.ui.roles` (`admins`, `reviewers` lists; spec 10 §7.3): `viewer` (read, chat, feedback, propose corrections), `reviewer` (+ decisions, review queue except `weight_change`, memory approve/reject), `admin` (+ `weight_change`, job control, resume, trigger reviews). Unlisted identities get `roles.default_role` (spec 10 default `viewer`; `denied` recommended when `expose.enabled`); `denied` shows "Not authorized" and writes an `auth` audit line. Checks run in the action handler, not only by hiding buttons; a refused action raises `PermissionDenied`.

### 9.3 Content safety

- No raw ticket text in UI or reports: incident/change/problem text only from `enrich.text_redacted`; `score.funding.title` and `core.work_item.summary` pass through `redact()` at display. A unit test parses every named query and fails if it references `short_description`, `description`, `close_notes` or `root_cause_text` of any `core.*` table.
- Model text is untrusted. Reports: autoescape, markers substituted after escaping. Dashboard/chat: never `unsafe_allow_html=True` with model text; `sanitize.py` escapes HTML, strips Markdown images and all links except in-app evidence anchors, then `st.markdown`. Markdown reports escape `<`, `>`, `&` and drop image syntax (spec 10 §9.1).
- `report.html` has no scripts and no external URLs, and carries `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">` (adopted by spec 10 for report files).
- Evidence samples render as escaped table values; SQL in code blocks. Reports never embed credentials or config values marked `secret:`.

## 10. Tests and acceptance criteria

| Test | Tooling | Acceptance |
|------|---------|------------|
| Golden report snapshot | `tests/fixtures/reports/draft_funding.json`, `draft_org.json` + fixture warehouse and ops store; `freezegun` | `report.html` and `report.md` byte-identical to `tests/fixtures/reports/golden/`; updated only with `--snapshot-update` |
| Every number linked | Parse rendered HTML | Every `a.num` href resolves to an existing `id="ev-…"`; every numeral in model-authored text nodes is inside `a.num` or matches `allowed_numeral_patterns`; `numbers_linked == numbers_total` |
| Shared numeral rule | Same fixture strings through the renderer scanner and the Verifier (05) | Identical accept/reject decisions |
| Strict mode | Draft with an uncited "42%" | `ReportContractError`, exit 12; `--no-strict` renders `<mark class="uncited">` |
| Contract validation | Marker without `NumberRef`, unknown `query_id`, unknown `rec_id`, unverified `finding_id`, wrong `schema_version` | Each fails with a field path |
| Old build evidence | Warehouse fixture without `meta.evidence.result_sample` | Entry shows "Sample not stored for this build"; render succeeds |
| Offline HTML | Static scan | No `<script>`, no external URL, CSP meta present |
| Banners | `weights.yaml` with one `unconfirmed: true`; run `partial` with 2 dead tasks; `publishable = false` | Banners and watermark in HTML, MD and dashboard; CLI exit 6 |
| XSS | Draft text with `<script>`, `<img src=x onerror>`, `![](http://x)` | Escaped or stripped in HTML, MD, dashboard and chat |
| Raw text guard | Parse `queries.sql.yaml` | No forbidden columns |
| Streamlit smoke | `streamlit.testing.v1.AppTest.from_file` per page with fixture stores | Each page runs without exception; key elements present; empty-warehouse state shows the fix |
| Roles | AppTest as viewer and as `denied` | Write handlers refuse, nothing written, `auth` audit line |
| Chat flow | Fake `ChatService` emitting all eight event kinds (`mode`, `token`, `tool`, `evidence`, `verification`, `escalated`, `final`, `error`); each `ChatMode` | UI inserts exactly one user row per turn, zero assistant rows and zero `chat` jobs; badge shown; evidence lists query_ids; user text stored redacted; feedback updates only `feedback`/`feedback_note`; correction calls `MemoryStore.propose`; `defer` shows the queued banner |
| Custom portfolio | Draft fixture with `portfolio_custom` | Custom scenario block rendered in the portfolio section; every number links to its input `query_ids` |
| Custom budget | `report funding --budget 2000000` on fixtures with mocked review | `RunRequest.scenarios` holds `custom_2000000`; the renderer shows it from `portfolio_custom` without calling the optimizer; its inputs have ops `evidence` rows |
| CLI | `typer.testing.CliRunner` | Every command `--help` exits 0; exit codes per §5.8 for injected errors; `--json` validates against `cli/1`; nothing but JSON on stdout |
| CURRENT switch | Promote a second fixture build during an AppTest session | After 60 s (clock advanced) the page shows the new `build_id` |
| Performance | Synthetic 5M warehouse (spec 11), timed page queries and render | §8 targets met |

## 11. Open questions

1. Should `core.work_item.summary` for candidates also get a stored redacted copy in `enrich`, instead of redacting at display? Default: redact at display.
2. Row-level evidence: `score.*` rows carry `query_ids` per row, so table cells link at row granularity. Acceptable for v1.

Resolved (2026-09-24, coordinator):

- R1. `ReportDraft` fields: spec 06 aligns to §4.1 (`schema_version`, `rec_id`, `headline`, `action_levers`, `ranked_entities`, `flags`, id-based `*_ref`).
- R2. `ChatEvent` kinds are defined by spec 06 from §3.3; `ChatMode = Literal["live", "small_model", "defer", "cloud"]` (spec 08).
- R3. `user_ref` HMAC key: secret `ui_user_ref_key`, added by spec 10.
- R4. Report CSP `default-src 'none'; style-src 'unsafe-inline'; img-src data:` adopted by spec 10 for report files.
- R5. UI bind, port and identity header live only in `herness.yaml: security.ui` (spec 10); `app.yaml` has no port.
- R6. Job IDs are `job_<ulid>` (spec 00 §5; spec 08 aligns).
- R7. Custom `--budget` scenarios: passed as spec 06 `RunRequest.scenarios`, rendered from `ReportDraft.portfolio_custom` (§5.2); the renderer never calls the optimizer. Ad-hoc what-if stays on `optimize_portfolio(persist=False)`.
- R8. Chat event kinds are exactly spec 06's (`mode`, `token`, `tool`, `evidence`, `verification`, `escalated`, `final`, `error`); `defer` ETA from `jobs.chat_next_live_at(now)`; the `defer` job is enqueued only by spec 06 `ChatService`.
- R9. `eval` options follow spec 11 §3.2; `worker` options follow spec 08 §3.8; job retry is `jobs.retry`.

## 12. Dependencies

- Specs: 00 (IDs, errors, storage, §12 decisions, D2/D4), 02 (tables), 04 (score tables, catalog, `optimize_portfolio`), 05 (`NumberRef`, `VerificationResult`, evidence rows, trace schema), 06 (`ReportDraft`, `ChatService`, `ChatEvent`, pipeline calls `render_run`), 07 (`MemoryStore` decisions, proposals, approvals, chat summary), 08 (job queue, worker, `chat_policy`, retries), 10 (config, secrets, `security.ui`, redaction, audit, egress guard, retention, deploy, privacy), 11 (fixtures, synthetic data, eval commands), 01 and 03 (command behaviour for `sync`, `enrich`, `distill`, `laya`).
- Packages: `jinja2`, `streamlit`, `typer`, `rich`, `duckdb`, `pydantic`, `structlog`; optional `weasyprint` (extra `pdf`). No charting JS; SVG generated in Python.

## 13. Contract changes (resolved)

1. `chat_message` columns `status`, `verified`, `feedback`, `feedback_note`, `meta` → spec 02 §5.5.
2. `chat_session.title` → spec 02 §5.5.
3. `job.kind = 'chat'` → spec 02 §5.2 and spec 08 §5.1.
4. `meta.evidence.result_sample` → spec 02 §4.7, filled by spec 04 §5 (`run_recorded`).
5. `config/app.yaml` → spec 00 §11 (owner 09); roles and bind moved to `herness.yaml: security.ui` (spec 10 §7.3).
6. Shared types: `ReportDraft` (was `WriterOutput`), `ChatEvent`, `ChatAnswer` → owner 06; `ChatMode` → owner 08; `ReportManifest` → owner 09 (spec 00 §6).
7. `ReportContractError(RecoverableError)` and `PermissionDenied(FatalError)` → spec 00 §7.
8. Ops API: chat functions (`create_chat_session`, `append_chat_message`, `update_chat_message`, `purge_chat`) are defined here; `decide_review_item` and read helpers under spec 02 §5's rule that each owning spec lists its `herness.store.ops` functions.
9. Retired-warehouse deletion tolerates open readers → spec 00 §4 and spec 02 §7.
10. Layout `app/common/`, `herness/reports/{contract,render,charts}.py` → spec 00 §3.
11. Writer hand-off file `draft.json` (was `writer.json`) → spec 00 §4 and §12.2.
12. Custom budget scenarios → spec 04 §3.1 `optimize_portfolio(persist=False)`, run by spec 06 and stored in `ReportDraft.portfolio_custom`; the renderer reads that list.
13. `ChatEvent` kinds (§3.3) → spec 06; `ChatMode` literal → spec 08.
14. Secret `ui_user_ref_key`, report CSP, UI bind/port/identity header in `security.ui` → spec 10.
15. Job ID format `job_<ulid>` → spec 00 §5, spec 08 §4.1.
