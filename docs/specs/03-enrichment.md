# 03 — Enrichment

Status: Draft v2 · 2026-09-24 · Depends on: 00, 02, 05, 08, 10. Phase 4.

v2 aligns with shared contracts v2 (spec 00, spec 02). The former requested changes are resolved (§13).

## 1. Purpose and scope

Enrichment turns free text into typed, cached, calibrated labels and structure that the deterministic metrics engine (spec 04) can count. It never produces numbers used in metrics. It produces labels with probabilities, cluster memberships and links.

In scope (package `herness/enrich/`): PII-masked text preparation, embeddings, incident clustering and cluster naming, the decider framework and its backends (OpenJev, Laya, hosted Jev, LLM fallback), the decision cache, teacher→student distillation, calibration, bulk inference with escalation, deep-mode voting and active learning, incident↔change linking, and service-map mapping suggestions.

Out of scope: the redaction algorithm (spec 10), the job queue and GPU scheduling (spec 08), the reasoning LLM client (spec 05), the review UI (spec 09), and how metrics weight labels (spec 04).

## 2. Responsibilities

1. Fill `enrich.text_redacted` for incidents, changes and problems in the new warehouse file.
2. Keep LanceDB `ticket_embedding` current, embedding only new `content_hash` values.
3. Cluster incidents into recurring-issue clusters with IDs that stay stable across nightly runs, and name them.
4. Classify every in-scope record against `config/decisions.yaml`, reusing the persistent decision cache so that unchanged text is never re-classified.
5. Own the shared types `DecisionInput`, `DecisionOutput`, `QuestionSet` (spec 00 §6) and the `Decider` implementations.
6. Run the distillation loop that produces accepted Laya versions, and gate which questions may use Laya labels in scoring.
7. Write `enrich.incident_change_link` and `review_item` rows of kind `mapping_suggestion` and `label_check`.
8. Build the `enrich.decision_wide` view from the active question set.

Module layout. The spec 00 §3 files are listed first; the helper modules below them are internal to this package:

| Module | Content |
|---|---|
| `enrich/settings.py` | pydantic model for `config/decisions.yaml` (spec 00 §3, spec 10) |
| `enrich/text.py` | text composition, redaction call, `content_hash` |
| `enrich/embed.py` | bge-m3 encoder, LanceDB upsert, index maintenance |
| `enrich/cluster.py` | PCA, prototypes, HDBSCAN, assignment, ID matching, naming |
| `enrich/decide.py` | `Decider` protocol, gate, escalation chain, resolver, writer |
| `enrich/cache.py` | decision cache read/write/migrate |
| `enrich/calibrate.py` | temperature scaling, ECE |
| `enrich/deciders/` | `openjev.py`, `laya.py`, `jev_hosted.py`, `llm.py`, `ensemble.py` |
| `enrich/distill.py` | sampling, teacher labeling, training, evaluation, acceptance, active learning |
| `enrich/link_changes.py` | incident↔change linking |
| `enrich/mapping_suggest.py` | service-map suggestions |
| `enrich/pipeline.py` (internal) | `run_enrichment()`: stage order, checkpoints, report |

## 3. Interfaces

### 3.1 Entry points

```python
# herness/enrich/pipeline.py
def run_enrichment(wh: duckdb.DuckDBPyConnection, build_id: str, *, depth: Literal["fast","standard","deep"],
                   ctx: JobContext, prev_warehouse: Path | None) -> EnrichReport: ...
# ctx: spec 08 JobContext (job_id, lease, GPU-class switching and service control used in §5.1).
# EnrichReport: pydantic; per-stage counts (rows, cache_hits, embedded, decided, escalated, failed),
# durations, decider versions used, question acceptance map. Spec 08 stores it in job.result.

# herness/enrich/embed.py — used by spec 05's semantic_search tool
def embed_query(text: str) -> np.ndarray: ...
# bge-m3 from embedding.path. Same text normalization and model settings as ticket embeddings (§5.2).
# normalize_embeddings=True, returns float32[1024]. The caller passes redacted text.
# Device: CUDA when a GPU class that allows it is loaded (none or decider, per spec 08 worker state).
# Otherwise CPU in fp32 (≈ 50–150 ms for a short query). The model is loaded lazily once per process.
# Must stay in sync with ticket_embedding.model; raises ConfigError on a model mismatch.

# herness/enrich/distill.py
def run_distill(*, round_kind: Literal["initial","active"], ctx: JobContext) -> DistillReport: ...   # job kind "distill"
def accept_model(version: str, questions: Sequence[str] | None = None) -> None   # human-confirmed promotion
def rollback_model(to_version: str) -> None
```

CLI wiring (spec 09 owns the CLI): `herness enrich [--stage S] [--depth D]`, `herness distill [--active]`, `herness laya accept <version>`, `herness laya rollback <version>`, `herness laya status`.

### 3.2 Shared types (owned here, declared in `herness/core/types.py`)

```python
QuestionType = Literal["choice", "bool", "score"]
Entity = Literal["incident", "change", "problem"]

class Question(BaseModel, frozen=True):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")        # becomes a column in enrich.decision_wide
    type: QuestionType
    instructions: str = Field(min_length=10, max_length=1000)
    options: dict[str, str] | None = None     # choice only: label -> description, 2..255 entries
    options_source: Literal["static", "core.team", "core.service"] = "static"   # dynamic options built per build
    levels: tuple[str, str, str, str] | None = None   # score only: descriptions of levels 0,1,2,3
    applies_to: tuple[Entity, ...] = ("incident",)
    threshold: float = Field(ge=0.5, le=0.999)       # calibrated-probability gate
    scoring_use: bool = True                          # label feeds spec 04 when accepted
    fingerprint: str = ""                             # sha256[:16] of canonical JSON of the fields above; set by loader

class QuestionSet(BaseModel, frozen=True):
    version: str = Field(pattern=r"^qs-\d{4}-\d{2}-\d{2}(\.\d+)?$")   # e.g. qs-2026-10-01.1
    questions: tuple[Question, ...]
    def get(self, qid: str) -> Question: ...
    def for_entity(self, entity: Entity) -> "QuestionSet": ...

class DecisionInput(BaseModel, frozen=True):
    record_id: str
    entity: Entity
    content_hash: str            # spec 00 §5, over `text`
    text: str                    # redacted text (enrich.text_redacted.text or a pair text, §5.8)
    question_ids: tuple[str, ...] | None = None   # None = all questions applying to entity

class Answer(BaseModel, frozen=True):
    answer: str                  # choice: option label; bool: "true"|"false"; score: "0".."3"
    probability: float           # RAW probability of `answer` from the backend (not calibrated)
    distribution: dict[str, float]   # RAW probability for every option/level/"true","false"; sums to 1±1e-3
    backend_confidence: float | None = None   # e.g. OpenJev 1 - H(p)/ln K; informational

class DecisionOutput(BaseModel, frozen=True):
    record_id: str
    content_hash: str
    decider: str                 # "laya" | "openjev" | "jev" | "llm" | "ensemble" | "human"
    decider_version: str
    answers: dict[str, Answer]   # keyed by question id; missing key = backend could not answer
    error: str | None = None     # per-item error class name when answers is empty
```

`Decider.decide(items, questions)` (spec 00 §6) returns one `DecisionOutput` per input, in input order. Deciders never calibrate and never gate. Calibration, gating and escalation live in `decide.py` so that every backend is interchangeable.

Wire mapping for all Jev-shape backends: `bool`→`noul`, `choice`→`choice` with `criteria = options`, `score`→`score` with `criteria = levels` (list, 4 levels). Choice option keys must not be `true`, `false`, `yes`, `no` (Laya follows such labels instead of their descriptions [L1]); the loader rejects them with `ConfigError`.

### 3.3 Backends

**OpenJev** (`deciders/openjev.py`, name `openjev`, version = image tag + model alias, e.g. `openjev-0.4.0/openjev-latest`). Verified from the README [O1]:

- `POST {base_url}/v1/systemone`, optional `Authorization: Bearer <OPENJEV_API_KEY>`. `GET /v1/models` is the health check.
- Request: `{"model": "openjev-latest", "state": "<text>", "questions": {"<qid>": {"type": "noul|choice|score", "instructions": "...", "criteria": {...} | [...]}}, "samples": k, "steps": 1, "think": 0}`. `samples` 1–32 reads averaged; `steps` 1–8 denoise steps; `think` 0–4096 tokens. Choice ≤ 255 options; score 2–10 levels.
- Response: `{"model": "...", "answers": {"<qid>": ...}, "usage": {"input_tokens": n, "output_tokens": 0}}`. `noul` answer: `{"noul": P(yes)}`. `choice`: `{"choice", "probabilities", "confidence"}`. `score`: `{"score", "legend", "probabilities", "confidence"}`, where `score` is probability-weighted [J1]. Confidence = `1 − H(p)/ln K`. When uncertain and `samples` is unset, the server takes 3 extra reads automatically [O1].
- Errors: 400 invalid questions/model, 401/403 auth, 422 validation, 429 rate limited, 529 overloaded [O1].
- Adapter: `score` answer uses the argmax level of `probabilities`, not the weighted `score`. Whether `probabilities` is a dict keyed by option/level or a list is not shown in the README: **verify at Phase 4** against the pinned image and freeze in a recorded fixture.
- Concurrency: `httpx.AsyncClient`, 64 requests in flight (server default `OPENJEV_MAX_NUM_SEQS=64`), one record per request with all its questions. Sync `decide()` wraps `asyncio.run`.
- Deployment: image `razorback16/openjev:0.4.0` pinned by digest (spec 10), bound to `127.0.0.1:8080`, model `nvidia/diffusiongemma-26B-A4B-it-NVFP4` (~18 GB), needs 24 GB+ VRAM [O1].

**Laya** (`deciders/laya.py`, name `laya`, version = local model version `laya-<yyyymmdd>-<n>`). Verified from the PyPI/README text of `laya` 0.3.20 [L1]:

- `agent = laya.load(path_or_repo, fast=...)`; `agent.predict_batch(states, questions, batch_size=64, sort_by_length=True)` returns results in input order, shape as `predict`: `result["answers"][qid]` with `choice` / `score` / `noul` fields. All questions of a state run in one forward pass.
- Checkpoints: `convaiinnovations/laya` (ModernBERT-large, 421M, 512 context, English), `laya-multilingual` (mmBERT-base, 322M), `laya-typed-decisions` (ModernBERT-large, 421M, 1024 context). The README also loads them as `subfolder="typed-decisions"` of `convaiinnovations/laya`; the exact repo path is **verify at Phase 4**.
- Limits: base checkpoints are near chance zero-shot (0.36 vs 0.318 random); fine-tuning is required. Choice questions with more than ~20 options degrade because options share a `head_max_len` budget (192 tokens English); mitigations are raising `agent.cfg["head_max_len"]`, or `laya.predict_shortlist(agent, state, questions, embed_fn=...)` over top-k labels [L1].
- Calibration: the fine-tuning notebook fits one `temperature` per type; published ECE 0.081 after domain temperature fitting [L1]. Herness applies its own per-question temperature on top (§5.6). We disable nothing in Laya's config, and we calibrate on its output probabilities.
- Whether `choice` answers expose a full `probabilities` distribution through `predict_batch` is **verify at Phase 4**. If they don't, the adapter reads the head logits through Laya's prediction hooks [L2].
- Loading: only local directories under `data/models/laya/<version>/` in production (`HF_HUB_OFFLINE=1`). Device `cuda`, bf16. `fast=True` (TileLang path, `laya[fast]`) is optional and must pass the parity test in §10.

**Hosted Jev** (`deciders/jev_hosted.py`, name `jev`, version = returned `model` string). `POST https://api.typesafe.ai/v1/systemone`, `Authorization: Bearer <key>`, model `jev-latest`, same request/response shape; 401/422/429/529 errors; input $0.042/MTok, output free; 70–500 ms latency [J1, J2]. The OpenJev README example uses host `api.codiv.ai`; the base URL is **verify at Phase 4**. Disabled unless the active profile enables it. The adapter's `httpx.Client` is built on spec 10's `GuardedTransport`, so every request passes the egress guard: the allowlist and data-policy check, a redaction re-scan, and an `egr_<ulid>` audit line. A refusal raises `EgressBlocked`. The key comes from `herness.core.secrets` (spec 10).

**LLM decider** (`deciders/llm.py`, name `llm`, version = `<profile name>/<model>` of the resolved client). The client comes from spec 05's `client_for(role, profile=..., depth=...)`. It uses role `enrich_decider` for classification and role `cluster_namer` for §5.3 step 8. Calls use spec 05 JSON-schema structured output. The request is one record with all its questions. The schema is built from the questions: each question maps to `{"answer": enum}` with the enum being option labels, `true/false` or `0..3`. Run k samples at temperature 0.7 (k = 3 standard, 5 deep, varied instruction paraphrases). The distribution is Laplace-smoothed votes: `p(a) = (votes(a) + 0.5) / (k + 0.5·K)`. Validation failure is handled with a repair prompt (spec 08, max 2), then an item error. It has three uses: escalations when OpenJev is unavailable, the third ensemble member in deep mode, and the fallback teacher (§5.8 step 2, decision D7).

`health()` for all backends (spec 00 §6): OpenJev and Jev call `GET /v1/models`; Laya checks that the `CURRENT` version loads and its manifest hash matches; the LLM decider calls the spec 05 client health check. Each raises `ModelUnavailable`. Circuit breakers use the `source_health` key `decider:<name>` (spec 02 §5.1).

**Ensemble** (`deciders/ensemble.py`, name `ensemble`): deep mode only, §5.9.

## 4. Data contracts

### 4.1 Warehouse tables written (spec 02 §4.4)

| Table | Written by stage | Rule |
|---|---|---|
| `enrich.text_redacted` | `text` | one row (`record_id`, `entity`, `text`, `content_hash`) per incident/change/problem with non-empty text; `300_attach_decisions.sql` copies `content_hash` into `core.incident` |
| `enrich.cluster`, `enrich.cluster_member` | `cluster` | noise records get no `cluster_member` row |
| `enrich.decision` | `decide` | one row per (`record_id`, `question`) that has a final answer; `probability` is calibrated |
| `enrich.decision_wide` | `decide` | view generated from the active `QuestionSet` (`<qid>`, `<qid>_p`) |
| `enrich.incident_change_link` | `link` | ≤ 3 changes per incident, `score` ∈ (0,1] |

Column semantics for `enrich.decision`: `decider` is the decider that produced the final answer. `escalated = true` when that decider is not the primary decider for the question. `review_status` is `pending` while an open `label_check` exists, and `confirmed`/`corrected` after a human decision. A corrected answer is written with `decider = 'human'` and `probability = 1.0`. `agreement` is filled only for `decider = 'ensemble'` (§5.9) and is NULL otherwise. `decided_at` is the cache row time, not the build time.

### 4.2 Text and content hash

`text = normalize(short_description) + "\n\n" + normalize(description)`, where `normalize` = NFKC, collapse whitespace, strip, then truncate to 4,000 characters. That text goes through `herness.core.redact` (spec 10), and `content_hash` = sha256 hex[:32] of the redacted text (spec 00 §5). Rotating the redaction key changes every `content_hash` (spec 00 §5). The effect is a full re-embedding and re-classification, so a rotation needs a planned night with enough capacity (§11). Structured fields (priority, service) are **not** part of the classifier state, so the cache key stays a pure text hash. For problems, `root_cause_text` stands in for `description`.

Incremental redaction: the stage attaches `prev_warehouse` read-only. It copies rows from the previous `enrich.text_redacted` for records whose `source_updated_at` is unchanged, and redacts only the rest.

### 4.3 Decision cache (persistent Parquet)

Path `data/cache/decisions/<question_set_version>/decider=<name>/decider_version=<v>/part-<ulid>.parquet` (spec 00 §4). Columns: `content_hash VARCHAR`, `question VARCHAR`, `question_fingerprint VARCHAR`, `answer VARCHAR`, `probability DOUBLE` (raw), `distribution MAP(VARCHAR, DOUBLE)` (raw), `backend_confidence DOUBLE`, `samples SMALLINT`, `decided_at TIMESTAMPTZ`. Lookup key: (`content_hash`, `question`, `question_fingerprint`, `decider`, `decider_version`). The cache stores **raw** outputs, so recalibration never requires re-inference. Human labels are stored separately (§4.5).

When `question_set_version` changes, `cache.migrate(old, new)` copies rows whose `question_fingerprint` is unchanged into the new version directory. Only changed or added questions are re-classified.

Part files are written to `*.tmp` and then `os.replace`d. Readers ignore `*.tmp`. Compaction (merge parts smaller than 64 MB) runs at the end of `decide`.

### 4.4 Model and state files

| Path | Content |
|---|---|
| `data/models/laya/<version>/` | checkpoint (`model.safetensors`, `rl_agent_config.json`, tokenizer), `calibration.json` (per-question T), `manifest.json`, `eval.json` (gate results, read by spec 11) |
| `data/models/laya/CURRENT` | active version (text; atomic replace) |
| `data/models/calibration/<decider>/<decider_version>/<question_set_version>.json` | per-question T for openjev / jev / llm / ensemble |
| `data/models/clusters/<algorithm_version>/<snapshot_id>/` | `pca.npz`, `prototypes.npy`, `proto_cluster.parquet`, `centroids.parquet` (cluster_id, 1024-d centroid, size, named_centroid, label, root_cause_category, retired_at), `CURRENT` |

`manifest.json`: `{version, parent_version, base_checkpoint, teacher: "openjev"|"llm", teacher_version, question_set_version, train_data_sha256, n_train, hyperparams, weights_sha256, accepted_questions: [qid], status: "candidate"|"accepted"|"retired", created_at, accepted_by, accepted_at}`.

`eval.json` holds the classifier gate results. It is written by `distill.py` step 6 and read by spec 11:

```json
{"version": "laya-20261004-1", "question_set_version": "qs-2026-10-01.1", "gold_path": "data/labels/qs-2026-10-01.1/gold/",
 "gold_sha256": "…", "evaluated_at": "2026-10-04T03:12:00.000000Z",
 "questions": {"root_cause": {"type": "choice", "question_fingerprint": "…", "n_gold": 1480,
     "laya": {"accuracy": 0.83, "macro_f1": 0.66, "mae": null, "within_one": null, "ece": 0.041, "temperature": 1.37,
              "coverage_at_threshold": 0.78, "accuracy_at_threshold": 0.91},
     "teacher": {"decider": "openjev", "accuracy": 0.84, "ece": 0.052},
     "system": {"accuracy": 0.85},
     "criteria": {"min_accuracy": 0.80, "min_macro_f1": 0.60, "max_ece": 0.05, "min_coverage": 0.70, "max_gap_to_teacher": 0.02},
     "passed": {"min_accuracy": true, "min_macro_f1": true, "max_ece": true, "min_coverage": true, "max_gap_to_teacher": true},
     "accepted_proposed": true}},
 "macro_metric": 0.81}
```

`system` is the accuracy after gate plus simulated escalation, where escalated rows take the teacher's gold-scored answer. Score questions fill `mae` and `within_one` and leave `macro_f1` null. `accepted_proposed` is true when every criterion passed. Acceptance itself is the human step in §5.8 step 7, recorded in `manifest.json`.

### 4.5 Labels store

`data/labels/<question_set_version>/` (spec 00 §4) has three subdirectories of Parquet parts (`part-<ulid>.parquet`, written tmp-then-rename):

- `teacher/`: `content_hash`, `record_id`, `question`, `question_fingerprint`, `answer`, `distribution`, `decider`, `decider_version`, `round`, `stratum`, `purpose` (`initial` \| `active`).
- `human/`: spot-check confirmations and corrections. Columns: `content_hash`, `record_id`, `question`, `question_fingerprint`, `answer`, `labeled_by`, `labeled_at`, `item_id`.
- `gold/`: the same columns as `human/` plus `fold` (0/1) and `adjudicated BOOLEAN`. Frozen once built. Spec 11 reads the same path.

Writers: enrichment, which syncs approved `label_check` review items into `human/` or `gold/` by payload `purpose`, and the review UI through the same function. Gold rows are never used for training.

### 4.6 `review_item` payloads (ops store, spec 02 §5)

`label_check`: `{"record_id", "content_hash", "question", "question_fingerprint", "question_set_version", "answer", "probability", "decider", "decider_version", "purpose": "spot_check"|"gold"|"ensemble_disagreement", "text_ref": "enrich.text_redacted"}`. The reviewer's decision adds `note = {"answer": "<human answer>"}`. The payload stores no text: the UI reads the redacted text from the warehouse.

`mapping_suggestion`: `{"subject_type": "jira_component"|"team", "jira_project", "jira_component", "team_id", "service_id", "score", "fuzzy": 0–1, "semantic": 0–1, "cooccurrence": 0–1, "evidence_counts": {...}, "algorithm_version"}`.

## 5. Behavior

### 5.1 Pipeline position and stage order

`run_enrichment` runs inside the nightly `build_pipeline` job (spec 02 §4.1) after SQL 000–299 and before 300–399. It uses the same DuckDB connection and writes into the new `wh-<build_id>.duckdb`. GPU work runs under GPU class `decider`. Spec 08 provides the GPU class switching inside the job and the start/stop of the OpenJev and reasoning-model services, through `JobContext`. This spec only states when a switch is needed. Stages run in this order:

1. `text`: redaction and `enrich.text_redacted` (CPU).
2. `embed`: bge-m3 on new hashes (GPU, only model resident).
3. `decide-primary`: Laya over records whose cache misses (GPU; bge-m3 unloaded first).
4. `decide-escalate`: needs the OpenJev service (requested from spec 08). Handles escalations, non-accepted questions, and change-link decider questions. If OpenJev is unavailable (decision D7 or an open circuit), this stage is skipped and its queue moves to step 6.
5. `cluster`: incremental assignment nightly; full recluster when due (§5.4). GPU used for k-means.
6. `reasoning phase` (GPU class `reasoning`, switch via spec 08): cluster naming, and LLM-decider escalations for whatever step 4 could not do.
7. `link`, `suggest` (CPU), `resolve` (write `enrich.decision`, the view, `label_check` items).

Each stage checks its inputs by content hash and skips finished work, so a rerun after a crash is cheap (§6). Deep mode adds stage 4b (ensemble) and runs self-consistency settings.

### 5.2 Embeddings

- Model `BAAI/bge-m3` via `sentence-transformers`, loaded from a local path, `model.half()` (fp16), `max_seq_length = 512`, `normalize_embeddings=True`, 1024-d float32 stored.
- Input: `enrich.text_redacted.text` whose `content_hash` has no vector in `ticket_embedding` (anti-join against the table's `content_hash` column loaded as Arrow). Texts are deduplicated by hash, sorted by length, and encoded with `batch_size=128`, then reduced to 64 on CUDA OOM (retry that batch once per size).
- Upsert: `merge_insert("record_id").when_matched_update_all().when_not_matched_insert_all()` with (`record_id`, `entity`, `service_id`, `opened_at`, `content_hash`, `model`, `vector`). Records sharing a hash reuse one vector. Rows whose `record_id` no longer exists in `core.*` are deleted.
- Flush every 20 batches (~2,560 texts), which is the checkpoint.
- Index: IVF_PQ, cosine, `num_partitions = round(sqrt(n))`, `num_sub_vectors = 64`. Rebuilt when rows grow by > 20 % since the last build of the index, otherwise `optimize()`.

### 5.3 Clustering algorithm (full recluster)

Input: incident vectors opened within `clustering.window_days` (default 1,095, so ≈ 5M). `n` = row count.

1. **PCA** 1024→64. Randomized PCA (`sklearn.decomposition.PCA(svd_solver="randomized")`) is fit on a uniform sample of 200k vectors. The fit is reused until `algorithm_version` changes, so projections stay comparable between runs. Project all n vectors on GPU in chunks of 262,144, then L2-normalize. Cost O(n·1024·64) ≈ 3·10¹¹ FLOP.
2. **Prototypes**: spherical k-means on GPU (torch, fp32), `k = clamp(n/250, 1,000, 20,000)`, k-means++ initialization on a 1M sample, 15 Lloyd iterations over all points in chunks of 32,768 (chunk × k similarity matrix ≤ 2.6 GB). Empty prototypes are re-seeded from the farthest points. Cost 2·n·k·64·15 ≈ 2·10¹⁴ FLOP for n = 5M, k = 20k: minutes on a 24 GB card. Prototype weight `w_p` = member count.
3. **HDBSCAN on prototypes**: `sklearn.cluster.HDBSCAN(min_cluster_size=5, min_samples=3, metric="euclidean", cluster_selection_method="eom")` on the k prototype vectors (euclidean on unit vectors is monotone in cosine). Cost ≈ O(k log k) with tree acceleration, k ≤ 20k: under 5 min on CPU. Prototypes labeled −1 are noise.
4. **Assignment**: each incident goes to its nearest prototype p with similarity s (already computed in the final Lloyd pass). Its cluster is `cluster(p)` when p is not noise and `s ≥ assign_min_sim` (0.60). `membership_prob = hdbscan_probability(p) × min(1, (s − 0.60)/(0.85 − 0.60))`.
5. **Pruning**: clusters with fewer than `min_incidents` (25) members become noise.
6. **Centroids and stable IDs**: centroid = normalized mean of members' **1024-d** vectors (independent of the PCA fit). Build the cosine matrix between new centroids and the previous snapshot's active centroids. Solve `scipy.optimize.linear_sum_assignment` on `1 − cos` and accept pairs with `cos ≥ 0.85`, which inherit the old `cluster_id`. Unmatched new clusters are then compared with clusters retired in the last 90 days, and `cos ≥ 0.90` revives the old ID. Remaining clusters get a new `cl_<ulid>` (spec 00 §5). Unmatched old clusters are marked `retired_at`. A split keeps the ID on the side with the higher centroid similarity. A merge keeps the ID of the matched predecessor.
7. **Descriptors**: `size`, `first_seen`/`last_seen` (min/max `opened_at`), `service_ids` (top 5 by member share, ≥ 5 % each). `top_terms`: c-TF-IDF. Each cluster's texts (sample ≤ 2,000) form one document; `TfidfVectorizer(ngram_range=(1,2), min_df=2, max_features=200_000, stop_words="english")` with redaction placeholder tokens excluded; top 10 terms.
8. **Naming**: a cluster is (re)named when it is new, when `cos(centroid, named_centroid) < 0.95`, or when its size ratio versus the named size falls outside [0.5, 2]. The largest `naming.max_llm_calls` (500) such clusters go to the LLM decider's client, `client_for("cluster_namer", profile=..., depth=...)` (spec 05), with 20 representative texts (nearest to the centroid, deduplicated, each ≤ 600 characters, redacted), `top_terms` and service names. The output schema is `{"label": string ≤ 60 chars, "root_cause_category": enum(root_cause options)}` with `additionalProperties: false`. Other clusters get `label = "auto: " + " / ".join(top_terms[:3])`, and `root_cause_category` = majority `root_cause` answer among members that have one (NULL otherwise). The first run makes a few hundred LLM calls; nightly runs make tens.
9. Write `enrich.cluster`, `enrich.cluster_member`, and a new snapshot directory. Then atomically update the snapshot `CURRENT`.

`algorithm_version` = `proto-hdbscan-v1-pca64-<pca_fit_id>`.

### 5.4 Clustering cadence

Nightly (incremental): new or changed incident vectors are projected with the stored PCA, assigned to stored prototypes (step 4) and written. Existing memberships are copied from the previous warehouse. Cluster descriptors are recomputed in SQL. No IDs change.

A full recluster (§5.3) runs when any of these holds: 7 days have passed since the last full run (default aligned with the Sunday deep run, spec 00 D3); drift, meaning more than 10 % of the night's new incidents fall below `assign_min_sim`; `algorithm_version` changed; or it is forced from the CLI.

### 5.5 `config/decisions.yaml` and the question set

```yaml
question_set_version: qs-2026-10-01.1
primary_decider: laya            # per-question override allowed
escalation_chain: [openjev, llm] # jev replaces openjev when profile enables hosted Jev
questions:
  - id: root_cause
    type: choice
    applies_to: [incident, problem]
    instructions: "Most likely root cause category of this IT incident, based on the description."
    options:
      software_defect: "application bug, code error, regression"
      config_change: "misconfiguration, bad parameter, config drift"
      capacity: "resource exhaustion, disk full, CPU/memory saturation, throttling"
      infrastructure: "hardware, network, storage, datacenter failure"
      dependency: "third-party or upstream service failure"
      data_issue: "bad data, failed batch, integration payload error"
      access_identity: "login, permissions, certificates, password, MFA"
      user_error: "how-to, user mistake, training need"
      unknown: "not enough information to tell"
    threshold: 0.70
  - id: change_caused
    type: bool
    applies_to: [incident]
    instructions: "The description states or strongly implies the issue started after a deployment, release, patch or change."
    threshold: 0.80
  - id: repeat_issue
    type: bool
    applies_to: [incident]
    instructions: "The description says this problem happened before or keeps recurring."
    threshold: 0.80
  - id: business_impact
    type: score
    applies_to: [incident]
    instructions: "Business impact described in the text."
    levels: ["none or single user", "team or workaround available", "department or degraded service", "customer-facing outage or revenue loss"]
    threshold: 0.60
  - id: owning_team
    type: choice
    options_source: core.team       # options = active core.team names (label = team_id, description = name)
    applies_to: [incident]
    instructions: "Which support team should own this issue?"
    threshold: 0.60
    scoring_use: false
acceptance:                          # per-type defaults; per-question override under questions[].acceptance
  choice: {min_accuracy: 0.80, min_macro_f1: 0.60, max_ece: 0.05, min_coverage: 0.70, max_gap_to_teacher: 0.02}
  bool:   {min_accuracy: 0.88, max_ece: 0.05, min_coverage: 0.75, max_gap_to_teacher: 0.02}
  score:  {max_mae: 0.45, min_within_one: 0.92, max_ece: 0.06, min_coverage: 0.65}
```

Loader rules (raise `ConfigError`): unique ids; choice has 2–255 options (static, or dynamic after resolution); score has exactly 4 levels; `options_source` choices with more than 255 options are pre-shortlisted per record to the 64 options with the highest bge-m3 similarity between text and option description. Laya receives choice questions with more than 20 options through `predict_shortlist` with k = 16 (verify at Phase 4). Any change to a question's fields changes its fingerprint and requires a new `question_set_version`.

### 5.6 Calibration

For each (decider, decider_version, question), `calibrate.py` fits a temperature T > 0 on the gold set by minimizing NLL (`scipy.optimize.minimize_scalar`, bounds [0.05, 10]):

- choice/score: `p'_i = softmax(log(p_i + 1e-9) / T)`.
- bool: `p' = σ(logit(p) / T)`.

It uses 2-fold cross-fitting on `gold.fold`: fit on one fold and measure ECE on the other (15 equal-mass bins, top-label ECE), and report the mean. The stored T is then fit on both folds. With fewer than 100 gold rows for a question, it uses T = 1.0 and marks the question `uncalibrated`, which cannot be accepted.

### 5.7 Bulk inference, gate and escalation

For each entity and each record with text:

1. **Resolve from cache.** Human label → final. Else the cache row for the primary decider at its current version. If its calibrated probability is ≥ the question threshold, it is final (`escalated=false`). If it is below the threshold, look for a cache row from the escalation deciders in chain order. The first one present is final (`escalated=true`) whatever its probability, and `enrich.decision.probability` shows the confidence. If no row is present, the (record, question) pair joins the escalation queue.
2. **Primary misses** → Laya batches: 256 states per `predict_batch` call (`batch_size=64` inside, `sort_by_length=True`), all questions for the entity in one pass. Results are written to the cache every 20 calls (~5k records), which is the checkpoint. The gate then applies as in step 1.
3. **Primary per question**: a question is Laya-primary only when it is listed in `accepted_questions` of the active Laya manifest. Otherwise the teacher decider (OpenJev; the LLM decider when OpenJev is unavailable, D7) is primary for that question. Because 5M records exceed teacher nightly capacity, non-accepted questions are decided only for records opened within `bootstrap_window_days` (90) and within the nightly budget. Other records stay undecided, and DQ reports the coverage (spec 02 §4.7).
4. **Escalation queue**: sorted by `scoring_use` desc, then `opened_at` desc, capped by `escalation.max_rows_per_night` (150k). It is sent to OpenJev (`samples` unset, so the server refines uncertain reads). If OpenJev is unavailable (`CircuitOpen` or D7, spec 08), the queue moves to the LLM decider during the reasoning phase with its own cap (`llm_max_rows_per_night`, 20k). Rows left over stay queued implicitly (cache miss) for the next night.
5. **Spot-check sampling for production labels**: each night, create `label_check` items for `min(50, 0.1 %)` of newly decided rows per question, half uniform and half with probability in [threshold, threshold + 0.1]. The global cap is 300 open items per question.
6. **Write**: `resolve` materializes `enrich.decision` for all in-scope records from the cache plus human labels (a DuckDB join over Parquet), then creates the `enrich.decision_wide` view with one `MAX(answer) FILTER (WHERE question='q')` and `MAX(probability) FILTER (...)` per question.

### 5.8 Distillation loop (job kind `distill`, GPU class `decider`)

Runs on demand or weekly (spec 08 schedules it). Steps:

1. **Stratified sample** of incidents (and problems when questions apply), excluding gold hashes and deduplicated by `content_hash`. Strata: service (top 50 by volume + `other`) × priority band (1–2, 3, 4–5) × quarter of `opened_at` × text length (< 120, 120–600, > 600 chars). Allocation is `n_h ∝ sqrt(N_h)` with a minimum of 5 per non-empty stratum. At most 2 % of the sample may come from one clustering prototype, which limits near-duplicates. Size `distill.sample_size` = 30,000 (range 20–50k). Membership and stratum go into the `teacher/` label parts (§4.5).
2. **Teacher labeling**: OpenJev with `samples: 3` over all questions. Outputs go to the decision cache (they are also valid production escalation results) and to `teacher/`. At ~30 req/s on a 24 GB card that is about 17 min per 30k.
   **Fallback teacher (decision D7).** OpenJev's default NVFP4 weights may not run on a non-Blackwell 24 GB card. When `health()` fails or `deciders.openjev.enabled: false`, the local reasoning LLM decider labels the sample instead: `client_for("enrich_decider", ...)` with k = 3 votes (§3.3), in GPU class `reasoning`, switched via spec 08. The impact:
   - **Throughput**: assume ~5 records/s with 3 votes on a 30B-A3B model under vLLM batching, to be measured at Phase 4. The default sample becomes `distill.sample_size_llm_teacher` = 20,000 (≈ 1.1 h at that rate, versus ≈ 17 min on OpenJev), and active-learning rounds drop to 2,000 rows each.
   - **Label quality**: vote-based distributions are coarse (K+1 distinct values at k = 3), so soft-label targets carry less information. Teacher ECE is measured on gold like any decider.
   - **Nightly escalation**: capacity falls to `llm_max_rows_per_night`, so Laya coverage at threshold matters more.
   - **Records**: `manifest.json` and `eval.json` record `teacher: "llm"`. The acceptance criteria are unchanged, and `max_gap_to_teacher` is measured against the LLM teacher.
3. **Human spot-check**: `label_check` items (`purpose=spot_check`) for `max(200, min(500, 1 %))` teacher rows per question, half uniform and half with teacher probability < 0.7. A question whose reviewed disagreement rate exceeds 15 % (≥ 100 reviews) is blocked from training until its instructions are revised (new fingerprint). Human corrections replace teacher labels in training with sample weight 3.
4. **Gold set** (built once per question fingerprint, then frozen): 1,500 records by the same stratification (independent draw), sent as `label_check` items with `purpose=gold`. A record counts only after 2 reviewers agree, or after a third reviewer adjudicates. Each class with ≥ 1 % prevalence needs ≥ 30 examples, topped up by targeted sampling on teacher-predicted class. Folds are assigned by hash parity.
5. **Fine-tune Laya**: base = `convaiinnovations/laya` (English) or the previous accepted version (config `distill.init_from: base|previous`, default `base` on the initial round, `previous` on active rounds). Training uses Laya's published fine-tuning procedure (RLCD: proper-scoring-rule rewards, GRPO-style policy gradient, per-type temperature fit), vendored from the notebook into `distill.py` behind a `LayaTrainer` adapter [L1, L3]. The notebook is the only documented training interface: the exact function names are **verify at Phase 4**. Fallback if extraction is impractical: supervised fine-tuning with KL divergence to teacher distributions (soft labels) on encoder + head. Starting hyperparameters: 4 epochs; AdamW lr 2e-5 encoder / 1e-4 head; weight decay 0.01; warmup 6 %; effective batch 32 (micro 8 × accum 4); bf16; gradient checkpointing on encoder and head (`model.head_checkpointing = True`); `max_len` 512; 10 % of teacher rows as validation for early stopping on validation NLL (patience 1 epoch); seed fixed and recorded. Reference runtime is 4–5 h for 4 epochs over ~30k questions on 2×T4 [L1]; the budget on one 24 GB card is ≤ 6 h.
6. **Evaluate** on gold per question: accuracy, macro-F1 (choice), MAE and within-one accuracy (score), ECE after the stored T, coverage at threshold, and the teacher's gold accuracy for the gap check. Results go to `eval.json` (§4.4).
7. **Accept**: `eval.json` marks a question `accepted_proposed` when every `acceptance` criterion holds (§5.5). The candidate becomes active only through `herness laya accept <version>` (human). This writes `data/models/laya/CURRENT` and sets `status` and `accepted_questions` in the manifest (a subset of the proposed questions). Questions not accepted keep the teacher decider as primary (§5.7 step 3). Rollback: `herness laya rollback <version>` rewrites `CURRENT` to an earlier accepted version. Its cache rows still exist under its `decider_version`, so rollback needs no re-inference for text that was already classified. Versions are never deleted while referenced by `CURRENT` or by the last 3 builds.

### 5.9 Deep-mode levers (owned here)

**Self-consistency**: OpenJev `samples: 5` (standard: unset); LLM decider k = 5 votes. If OpenJev is unavailable (D7), the ensemble has two members, Laya and the LLM decider, and the LLM labels the whole band within `ensemble.llm_max_rows`. Laya is deterministic and runs a single pass.

**Ensemble voting** (stage 4b, `depth=deep`): the band is rows where calibrated Laya probability < `ensemble.band` (0.90), for questions with `scoring_use`, capped at 300k rows (≈ 3 h OpenJev). OpenJev labels all band rows. The LLM decider labels band rows where Laya and OpenJev argmax disagree (cap 20k, reasoning phase). The combination is a weighted log-linear pool of **calibrated** distributions:

`log q(a) = Σ_d w_{d,q} · log(p'_d(a) + 1e-6)`, normalized over answers; then `q' = softmax(log q / T_ens)`.

Weights `w_{d,q}` = decider's gold accuracy on question q divided by the sum over present deciders (a missing member is dropped and weights renormalize). `T_ens` is fit on gold like any decider (§5.6). Agreement = share of present deciders whose argmax equals the pooled argmax. Final: `decider='ensemble'`, `decider_version` = sha256[:12] of the member versions and weights, `probability = q'(argmax)`, and `escalated=true` when the pooled argmax differs from Laya's. Agreement < 2/3 creates a `label_check` (`purpose=ensemble_disagreement`, cap 500/night). Ensemble outputs are cached like any decider and take precedence over single-decider rows when present for the current ensemble version.

**Active learning** (job `distill`, `round_kind=active`):

1. Score a pool of 500k undecided-by-teacher records with the current Laya.
2. Compute uncertainty `u = max over scoring questions of (1 − p'_max)`.
3. Take the top 20k by u, then keep ≤ 5 per prototype until 5,000 are chosen (diversity).
4. Label those with OpenJev and add them to the training set.
5. Retrain from `previous` and evaluate on gold.

Stop when the gold macro metric (mean of per-question primary metric over `scoring_use` questions) improves < 0.5 pp for 2 consecutive rounds, or after `max_rounds` (5). Each round yields a candidate version, which needs acceptance as in §5.8 step 7.

### 5.10 Incident↔change linking

1. `source_field`: `core.incident.caused_by_change_id` resolves to `core.change.record_id` → `score = 1.0`.
2. `time_ci_window`: candidate changes with `t_c = coalesce(actual_end, actual_start, planned_end)` satisfy `t_c ∈ [opened_at − 72 h, opened_at + 1 h]` or `actual_start ≤ opened_at ≤ actual_end`, and match on CI or service. `score = m · exp(−Δh / 12) · b`, clipped to 1. `m` = 1.0 for the same `ci_id` and 0.6 for the same `service_id` only. `Δh` = hours from `t_c` to `opened_at`, floored at 0. `b` = 1.25 when `outcome ∈ (unsuccessful, backed_out, successful_with_issues)`, × 1.1 when `type = 'emergency'`. Keep links with `score ≥ 0.30`, top 3 per incident. SQL is a range join bucketed by `service_id` and day: O(Σ over services of incidents × changes in the window).
3. `decider` (optional, `change_link.use_decider: true`): for time-window candidates with score in [0.30, 0.70], ask the question `change_caused_pair` (bool, defined in decisions.yaml with `applies_to: [incident]`, `scoring_use: false`). The state is `"INCIDENT:\n<redacted incident text>\n\nCHANGE:\n<redacted change short_description + description>"`, hashed as its own `content_hash` and cached. The row becomes `method='decider'`, `score = 0.5·heuristic + 0.5·p'(true)`. Cap 30k pairs per night.
4. A source-field link overrides the others for the same pair. There is one row per (incident, change).

### 5.11 Mapping suggestions

Subjects: distinct (`project`, `component`) from `core.work_item` with `service_id` NULL, and `core.team` rows with no `core.service_map` row. Candidates: all `core.service`. Scores:

- `fuzzy` = `rapidfuzz.fuzz.token_set_ratio(norm(subject_name), norm(service_name)) / 100`, where `norm` lowercases, strips punctuation and expands abbreviations from `decisions.yaml: mapping_suggest.abbreviations`.
- `semantic` = cosine of bge-m3 embeddings of `"<name>: <description or top 20 work item summaries / incident short descriptions, redacted>"`. These vectors are computed in memory, not persisted.
- `cooccurrence` (teams only) = share of the team's incidents with that `service_id`.
- `score = 0.35·fuzzy + 0.45·semantic + 0.20·cooccurrence` (for Jira subjects the cooccurrence weight moves to semantic).

Emit the top 3 per subject with `score ≥ 0.60` as `review_item(kind='mapping_suggestion', status='pending')` through the ops store API. No suggestion is re-emitted if a pending or rejected item exists for the same (subject, service_id). Nothing is auto-approved: `core.service_map` reads only approved items (spec 02 §4.3).

## 6. Errors and resilience

| Situation | Error (spec 00 §7) | Handling |
|---|---|---|
| OpenJev / Jev connection error, timeout (30 s), 5xx, 529 | `ModelUnavailable` | spec 08 retry policy for model endpoints (`config/resilience.yaml`); breaker key `decider:<name>`; `CircuitOpen` → escalation moves to next chain member |
| Hosted Jev refused by egress guard | `EgressBlocked` | fatal for the `jev` backend in this run; chain continues with the next member |
| 429 | `RateLimited(retry_after)` | honor `Retry-After`; reduce concurrency by half for 60 s |
| 400/422, malformed answers, unknown option, distribution not summing to 1±1e-3 | `OutputValidationError` | retry the item once; then item `error` set, item goes to next chain member |
| 401/403, missing key | `AuthError` | fatal for that backend; the chain continues without it; job logs error |
| Laya / bge-m3 CUDA OOM | `RecoverableError` subclass handling | halve batch, retry batch; at batch 1 → `FatalError` for stage |
| LLM naming schema failure after repairs | `OutputValidationError` | auto label fallback (§5.3 step 8) |
| decisions.yaml invalid, option limit exceeded | `ConfigError` | job fails before GPU work |
| Laya CURRENT missing / hash mismatch with manifest | `ConfigError` | teacher decider primary for all questions; warn in report |
| LanceDB / Parquet write busy | `StoreBusy` | retry per spec 08 |

**Resumability**: the `build_pipeline` job restarts the build on crash (spec 02 §7). All enrichment progress lives outside the warehouse: decision cache parts flushed every 20 Laya calls or every 2,000 OpenJev answers, LanceDB committed every 20 embedding batches, cluster snapshots written atomically. A rerun redoes at most one checkpoint interval per stage. Stage durations and counts go to `EnrichReport`. `distill` checkpoints training each epoch into `data/models/laya/<version>/checkpoints/` and resumes from the last one.

**Degraded modes** (never block promotion by themselves): OpenJev down means LLM escalation within its cap, and the remaining rows stay undecided. The reasoning LLM down means auto labels for clusters and no LLM escalations. Decision coverage below 95 % raises a DQ warning (spec 02 §4.7).

## 7. Configuration

All enrichment settings live in `config/decisions.yaml` (owner 03, spec 00 §11), validated by `herness/enrich/settings.py`. Spec 10 owns loading, precedence and profiles. Spec 05 owns `config/models.yaml`, which maps the roles `enrich_decider` and `cluster_namer` to model profiles; this spec only names the roles. Retry policies and breakers are in `config/resilience.yaml` (spec 08).

Backends and runtime (besides the question set in §5.5):

```yaml
embedding: {model: BAAI/bge-m3, path: data/models/bge-m3/<rev>/, dtype: fp16, batch_size: 128, max_seq_length: 512}
deciders:
  laya:     {current_file: data/models/laya/CURRENT, device: cuda, dtype: bf16, fast: false, call_batch: 256, batch_size: 64}
  openjev:  {enabled: true, base_url: "http://127.0.0.1:8080", model: openjev-latest,   # image pin: spec 10
             concurrency: 64, timeout_s: 30, samples: {fast: 1, standard: null, deep: 5}, api_key_secret: OPENJEV_API_KEY}
  jev:      {enabled: false, base_url: "https://api.typesafe.ai", model: jev-latest, concurrency: 16, api_key_secret: TYPESAFE_API_KEY}
  llm:      {role: enrich_decider, votes: {fast: 1, standard: 3, deep: 5}, temperature: 0.7}
escalation: {max_rows_per_night: 150000, llm_max_rows_per_night: 20000, bootstrap_window_days: 90}
spot_check: {nightly_rate: 0.001, nightly_max_per_question: 50, open_cap_per_question: 300}
ensemble: {band: 0.90, max_rows: 300000, llm_max_rows: 20000, disagreement_review_cap: 500}
distill: {sample_size: 30000, sample_size_llm_teacher: 20000, gold_size: 1500, init_from: base, spot_check_min: 200,
          spot_check_max: 500, block_disagreement: 0.15,
          active: {pool: 500000, candidates: 20000, per_round: 5000, per_round_llm_teacher: 2000, per_prototype: 5,
                   min_gain_pp: 0.5, patience: 2, max_rounds: 5}}
clustering: {window_days: 1095, pca_dims: 64, pca_sample: 200000, proto_per: 250, k_min: 1000, k_max: 20000, iters: 15,
             min_cluster_size: 5, min_samples: 3, assign_min_sim: 0.60, full_sim: 0.85, min_incidents: 25,
             match_cos: 0.85, revive_cos: 0.90, revive_days: 90, rename_cos: 0.95, full_every_days: 7, drift_share: 0.10,
             naming: {role: cluster_namer, max_llm_calls: 500, examples: 20, example_chars: 600}}
change_link: {before_h: 72, after_h: 1, tau_h: 12, ci_weight: 1.0, service_weight: 0.6, min_score: 0.30,
              top_n: 3, use_decider: true, decider_band: [0.30, 0.70], decider_max_pairs: 30000}
mapping_suggest: {min_score: 0.60, top_n: 3, weights: {fuzzy: 0.35, semantic: 0.45, cooccurrence: 0.20},
                  abbreviations: {pmt: payment, auth: authentication}}
```

## 8. Performance targets

Reference: one 24 GB NVIDIA GPU, 16 cores, 64 GB RAM, NVMe (spec 02 §9).

| Operation | Volume | Target |
|---|---|---|
| Redaction, incremental | 10k changed records | < 1 min (full 5.5M: < 30 min, spec 10 owns speed) |
| bge-m3 embedding | full 5.5M texts / nightly 10k | ≥ 700 texts/s → < 2.5 h / < 30 s |
| Laya bulk inference (5 questions) | full 5M / nightly 10k | ≥ 400 records/s → < 3.5 h / < 1 min |
| OpenJev escalation | per night | ≥ 25 records/s (all questions) on 24 GB; 150k cap ≈ 1.7 h |
| LLM teacher / LLM escalation (D7 fallback) | 20k sample / 20k nightly cap | ≈ 5 records/s with 3 votes (measure at Phase 4) → ≈ 1.1 h |
| Escalation share after acceptance | nightly | ≤ 10 % of decided (record, question) pairs |
| Full recluster | 5M incidents | < 30 min end to end excluding naming; naming ≤ 500 calls < 45 min |
| Nightly incremental clustering | 10k | < 2 min |
| Change linking (heuristic) | 5M incidents × 0.5M changes | < 3 min in DuckDB |
| Mapping suggestions | ≤ 2k subjects × ≤ 5k services | < 5 min |
| Nightly enrichment total (no recluster, no escalation backlog) | 10k new/changed | < 20 min |
| Laya fine-tune | 30k records | ≤ 6 h |

Throughput numbers are targets to confirm at Phase 4 on the actual card. Published figures are Laya 7.2 ms/question batched on T4 [L1] and OpenJev 57 req/s at 64 concurrency on RTX PRO 6000 [O1].

## 9. Security

- Every model input (embedding, Laya, OpenJev, Jev, LLM decider, cluster naming, mapping embeddings) is built from `enrich.text_redacted` or text redacted by `herness.core.redact` (spec 10). Raw `core.*` text is never passed to a model.
- Hosted Jev (`GuardedTransport`) and any non-local LLM (spec 05 clients) go through the spec 10 egress guard. It refuses requests when the profile does not allow off-network use, and re-scans text for PII patterns. Only single redacted tickets are sent, no aggregates beyond that.
- OpenJev binds to `127.0.0.1` with `OPENJEV_API_KEY` set. The key comes from `herness.core.secrets` (spec 10), never from config files or logs. Hosted Jev traffic uses `GuardedTransport` only.
- Model files load only from `data/models/` as safetensors. Manifests store sha256 of weight files, verified on load. HF downloads are pinned by revision and done once, and runtime sets `HF_HUB_OFFLINE=1`.
- Logs record counts, hashes and IDs, never ticket text (spec 00 §8). `review_item` payloads carry no text.
- Mapping suggestions and label corrections affect scoring only after human approval.

## 10. Tests and acceptance criteria

Unit (`tests/unit/enrich/`):
- Question loader: limits (255 options, 4 levels, forbidden bool-word labels), fingerprint stability, dynamic options.
- Wire mapping bool/choice/score ↔ Jev shape, using recorded OpenJev 0.4.0 fixtures (respx). Score argmax is taken over `probabilities`, not `score`.
- Calibration: synthetic overconfident distributions recover T within 5 %; cross-fit ECE computation against a hand-worked example.
- Ensemble pooling: two agreeing and one disagreeing member give the expected pooled distribution; missing members renormalize.
- Change-link scoring table-driven cases (window edges, CI vs service, outcome boost, cap at 1).
- Mapping scoring; rejected suggestion is never re-emitted.

Integration (`tests/integration/enrich/`, synthetic data from spec 11):
- **Cache hit**: two consecutive pipeline runs on an unchanged lake. The second makes 0 decider calls and 0 embeddings, and yields identical `enrich.decision`. Changing one question's instructions re-classifies only that question.
- **Stable cluster IDs**: full recluster on the same data plus 1 % new incidents keeps IDs for ≥ 95 % of clustered incident mass. Nightly incremental runs change no IDs.
- **Planted clusters**: synthetic planted recurring issues are recovered with ARI ≥ 0.80 on planted members.
- **Links**: planted change-caused incidents get precision ≥ 0.80 and recall ≥ 0.70 at `score ≥ 0.5`.
- **Mapping**: no `core.service_map` row with `link_source='suggested_approved'` exists without an approved `review_item`.
- **Laya fast path parity** (if enabled): argmax agreement ≥ 98 % vs the stock path on 1k states.

Fault (`tests/fault/`): kill OpenJev mid-escalation, and the escalation moves to the LLM decider, with no duplicate cache rows after rerun. Malformed OpenJev JSON causes one retry, then the next chain member. Kill the job mid-Laya, and the rerun resumes from the last flushed part (≤ 1 checkpoint of rework). Laya CURRENT corrupted gives teacher-primary degraded mode. With OpenJev disabled (D7 simulation), distillation completes with the LLM teacher and records `teacher: "llm"` in the manifest.

Phase 4 acceptance (real or realistic data, gold set 1–2k, results read from `data/models/laya/<version>/eval.json` by spec 11):
- Per accepted question: `acceptance` thresholds in §5.5 are met, including ECE ≤ 0.05 (score ≤ 0.06) after calibration and gap to teacher ≤ 2 pp.
- A report compares Laya vs OpenJev vs human per question (accuracy, macro-F1/MAE, ECE, coverage), as the plan's verification requires.
- Escalation share ≤ 10 %, and nightly enrichment < 20 min on a 10k increment.

## 11. Open questions

1. OpenJev `probabilities` field shape (dict vs list) and score `legend` format: freeze from the pinned image at Phase 4.
2. Laya training entry points outside the notebook, and whether `predict_batch` exposes full choice distributions: verify at Phase 4. The fallback training is soft-label SFT.
3. Hosted Jev base URL (`api.typesafe.ai` in docs vs `api.codiv.ai` in the OpenJev README).
4. `owning_team` with more than 255 active teams, or teams without descriptions: shortlist quality needs measurement. The question may stay non-scoring.
5. Multilingual tickets: route non-English text to `laya-multilingual` via `laya.Router`, or treat them as out of scope? Default: English checkpoint; share of non-Latin text reported.
6. Whether changes and problems should also be clustered. Default: incidents only.
7. Gold labeling effort (1,500 records × 2 reviewers) needs named reviewers per org.
8. D7: which teacher does Phase 4 run on the target card? If OpenJev NVFP4 does not run, the LLM teacher is the default and the throughput and quality impact in §5.8 applies. A non-NVFP4 OpenJev weight variant would need spec 10 to pin it.
9. Resolved: the roles `enrich_decider` and `cluster_namer` are defined in spec 05's `config/models.yaml`.
10. Resolved (spec 10 v2 §5.5 step 3): `herness.enrich.purge_record(record_id) -> dict` is a deletion step; it removes `ticket_embedding` rows by `record_id`, and decision-cache and label rows by `content_hash` when no other live record shares the hash.
11. Resolved (spec 08 v2 §5.11, spec 10 v2 §5.3): rekey runs only in spec 08's planned Saturday night slot, and the rekey job re-keys human, gold and teacher labels through a `record_id` → hash map.
12. Resolved (spec 10 v2 §3.3): secret names `OPENJEV_API_KEY` and `TYPESAFE_API_KEY` are confirmed.

## 12. Dependencies

- Specs: 00 (contracts, IDs, errors), 02 (tables, ops store API, lake), 05 (`client_for(role, profile=, depth=)`, structured output), 08 (`JobContext`, job queue, GPU class switching inside `build_pipeline`, OpenJev/reasoning service start/stop, retry policies, breakers), 09 (CLI, review UI for `label_check` and `mapping_suggestion`), 10 (`herness.core.redact`, `GuardedTransport` egress guard, `herness.core.secrets`, container pins, deletion requests), 11 (synthetic data with planted clusters and change-caused incidents; reads `gold/` and `eval.json`).
- Packages: `sentence-transformers`, `torch` (CUDA), `lancedb`, `scikit-learn` (PCA, HDBSCAN, TF-IDF), `scipy` (Hungarian, minimize_scalar), `rapidfuzz`, `laya` (pinned at Phase 4; extras `fast` optional), `httpx`, `tenacity`, `pydantic`, `duckdb`, `pyarrow`.
- Services: OpenJev container (`razorback16/openjev:0.4.0`, digest pinned), reasoning LLM via vLLM, optional hosted Jev.

Sources:

- [O1] OpenJev README, <https://github.com/razorback16/openjev> (fetched 2026-09-24)
- [J1] TypeSafe API reference, <https://docs.typesafe.ai/api.md>
- [J2] TypeSafe blog, <https://typesafe.ai/blog/introducing-system-one-models-and-jev>
- [L1] `laya` 0.3.20 README on PyPI, <https://pypi.org/project/laya/> (mirrors <https://github.com/NandhaKishorM/laya>)
- [L2] Laya docs, <https://nandhakishorm.github.io/laya/> (hooks, API reference; not fetched)
- [L3] Laya fine-tuning notebook, <https://github.com/NandhaKishorM/laya/blob/main/notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb> (not fetched)

## 13. Contract changes (resolved)

1. `cluster_id` format `cl_<ulid>`: spec 00 §5.
2. Labels store `data/labels/<question_set_version>/{teacher,human,gold}/`: spec 00 §4; column layout in §4.5 here; spec 11 reads `gold/`.
3. Enrichment may write `data/models/` (cluster snapshots, calibration, `eval.json`): spec 00 §4.
4. `core.incident.content_hash` filled in `300_attach_decisions.sql`: spec 02 §4.1 and §4.3.
5. `enrich.decision.agreement`: spec 02 §4.4.
6. `review_item.payload` schemas per kind owned by 03: spec 02 §5.5; defined in §4.6 here.
7. GPU class switching inside `build_pipeline`, and OpenJev/reasoning service start/stop: spec 08, used through `JobContext` (§5.1 here).
8. `Question`, `Answer` in `herness/core/types.py`, and `Decider.health()`: spec 00 §6.
9. `rapidfuzz>=3`, `scipy`: spec 00 §9.
10. Decision cache schema: spec 00 §4 points to §4.3 here.
