# T11-13 verify review: Shards, workers, truth writer, inbox

Reviewed head bc56ca9 (code in 37ea97d), base 35eb982. The worktree was left clean (no changes; status empty). Scratch files are only under `C:\Users\santh\AppData\Local\Temp\w30-s11\verify\`.

### Spec Compliance
- ✅ U11-19 `tools/synth/shards.py` (+ private `_shard_run.py`, `_shard_io.py`)
  - Signatures match: `plan_shards(cat, params)`, `run_shard(shard, ctx)`, `run_all_shards(shards, cat, params, *, seed, root, workers)`. `Shard` fields are unchanged. `ShardResult` has all spec fields, plus additive `row_counts` and `s4_noise`.
  - Lake writes: `TARGET_BYTES = 128 * 2**20`, passed as `LakeWriter(..., target_bytes=TARGET_BYTES)`. The lake defaults to zstd.
  - Pool: `get_context("spawn").Pool(workers, initializer=_init_worker, initargs=(root, seed, params, catalog))`. The initializer loads profile `synth` with `paths.data=<root>/data` and takes the raw root from `data_layout(cfg)`.
  - RNG and chunking: `shard_rng(seed, (source, entity, month.isoformat()))`; chunks of `MAX_ROWS` (131,072).
  - Plan order: the 5 dimension shards first (one each, at the first month), then incident, change, problem, Jira, events and metric_daily.
  - Phases: phase A runs the incident shards and writes the `.npy` index under `truth/.parts/idx/` (`allow_pickle=False`). Phase B runs the rest.
  - `task_sla` rows come from the final incident records, after plants and dirty defects, via `gen_task_slas` (U11-77).
  - Errors: writers are aborted and the error re-raised. A pool failure terminates the pool and raises `FatalError` naming `source/entity/YYYY-MM`.
- ✅ U11-20 `tools/synth/truth_writer.py`
  - All 3 signatures match, plus an additive `config_dir` keyword.
  - `truth_labels.parquet` has exactly the columns `record_id, content_hash, question, answer, pii_spans`. `t2_members` and `t3_pairs` are written.
  - `content_hash` is built as spec 03 `compose_text`, then the synth-profile `Redactor`, then `content_hash`.
  - Every file is written tmp-then-`os.replace`. Labels are written in row groups of 1M.
  - `.parts/` is removed with `rmtree` before `truth.json`, which is written last.
  - A redactor construction failure raises `ConfigError`.
  - `synth_mappings.yaml` holds only `mappings.{custom_fields,enums,service_overrides}`, and the test validates it as `MappingsConfig`.
  - `name_directory.csv` is `first_name,last_name`.
- ✅ U11-21 `tools/synth/inbox.py`: header is exact, rows are in catalog order, money uses `:.2f`, the path is `<root>/data/inbox/service_costs/service_costs.csv`, and the file is written tmp-then-replace.
- ✅ UT11-24: 4 tests carry the `ut11_24` ID and assert what the row says:
  - all files present;
  - `truth.json` newest;
  - `.parts` gone;
  - `content_hash` recomputed independently from the lake payload with a synth redactor.
- ✅ UT11-25: 2 tests carry the ID and check the header, 20 rows, 2-decimal money and catalog order.
- ⚠️ Three spec notes, not defects; they need a controller ruling (details below):
  - (a) The fixed synth HMAC key has no source in the tree.
  - (b) The `name_directory.csv` format conflicts between spec 11 and spec 10.
  - (c) Incident months are generated whole rather than in chunks of 131,072. Lake writes are still batched at 131,072 or fewer. Generating the whole month keeps the incident plants correct, and at full scale a month is about 139k incidents, so the effect on memory is small.

### Mandatory checks evidence
- Card tests: `uv run pytest tests/unit/tools/synth tests/integration/tools/synth -q -p no:logging` gave **274 passed in 31.86s**.
- Gates:
  - `check_module_size` exit 0. Lines against budget: shards 323/350, _shard_run 256/280, _shard_io 194/220, truth_writer 233/250, inbox 41/100. The §2 rows for both private siblings were added in the spec diff.
  - ruff check: clean.
  - ruff format: 50 files already formatted.
  - mypy on the 9 touched files: no issues.
  - lint-imports: 14 kept, 0 broken.
- Reuse: no existing tools/synth public function is duplicated. The card reuses `compose_text`/`content_hash` (spec 03), `Redactor`/`NameDirectory`, `LakeWriter`, `data_layout`, `gen_task_slas`, `apply_dirty`, `assign_fetch`, `to_lake_batch`, `shard_rng` and `build_name_list`.

### Acceptance (real run)
Script `verify\accept.py` ran with seed 7, scale tiny, 2026-06-01..2026-08-31, sources servicenow/jira/monitoring/files, dirty default and 4 spawn workers. It runs `plan_shards`, `run_all_shards`, `write_service_costs`, `write_name_directory`, `write_synth_mappings`, `write_truth` and `.synth_root`, in the same order as U11-23 steps 4-7.
```
data/raw/jira/issue: 12 files, 163 rows
data/raw/monitoring/event: 5 files, 430 rows
data/raw/monitoring/metric_daily: 40 files, 7270 rows
data/raw/servicenow/change_request: 4 files, 331 rows
data/raw/servicenow/cmdb_ci: 2 files, 91 rows
data/raw/servicenow/cmdb_ci_service: 1 files, 20 rows
data/raw/servicenow/cmdb_rel_ci: 1 files, 70 rows
data/raw/servicenow/cmn_department: 1 files, 3 rows
data/raw/servicenow/incident: 42 files, 1551 rows
data/raw/servicenow/problem: 4 files, 41 rows
data/raw/servicenow/sys_user_group: 1 files, 15 rows
data/raw/servicenow/task_sla: 3 files, 1376 rows
  .synth_root 0
  data/inbox/service_costs/service_costs.csv 858
  name_directory.csv 7266
  synth_mappings.yaml 3507
  truth/t2_members.parquet 2223
  truth/t3_pairs.parquet 5200
  truth/truth.json 2800
  truth/truth_labels.parquet 71502
example lake file: jira/issue/dt=2026-09-01/part-01M41V7TJE413C50RFMDQ2QDDX.parquet
dot-prefixed under data/raw: 0
cmn_department exists: True    task_sla exists: True
truth under data?: False       truth.json newest: True
dot-files anywhere in root: ['.synth_root']  (expected marker; no leftover .tmp)
new paths in TMP outside verify/: 0
truth_labels rows 7190, null content_hash 0
```
These counts match the builder's reported listing exactly, so the generation is deterministic across separate runs.

### PII probe (TH11-02 / TH11-07)
I captured the planted values from the shard `pii-*` and `texts-*` parts before `write_truth` removed them: 58 values, of types CARD, CREDENTIAL, EMAIL, EMPLOYEE_ID, IP, PERSON, PHONE and URL_TOKEN. I then searched every artifact under root for them: all lake Parquet (decoded), the truth Parquet and JSON, synth_mappings.yaml, name_directory.csv and service_costs.csv.
- **Lake:** all 58 values appear only in `data/raw/servicenow/incident`, which is expected because the lake carries the planted PII. Examples: CARD `4111134526545343`, EMAIL `boreo.gromberg@example.org`, PERSON `Frelholm, Taran`, URL_TOKEN `...token=synthetic1014c9...`.
- **Truth (`truth/*`):** 0 occurrences. `pii_spans` holds only `{field,start,end,type}`. UT11-24 also shows that `content_hash` is computed over the redacted text, and that redaction changed the text of rows with spans.
- **`synth_mappings.yaml` and `service_costs.csv`:** 0 occurrences.
- **Name directory:** the known name `name_directory[0]` "Sereo Gromane" appears only in `name_directory.csv`. That is expected: the file is the made-up person directory (design 11 §3 table).
- **Path containment:** nothing was written outside root (the TMP snapshot diff is empty). Truth is outside `<root>/data`, and every writer goes through `require_under`.

### Builder concerns: assessment
- (a) **SYNTH_HMAC_KEY** (`truth_writer.py:44`)
  - I searched `config/profiles/synth.yaml`, `config/herness.yaml`, `.env.example` and the tests. No synth key value exists anywhere: synth.yaml only has the comment "Phase 1 uses a fixed test HMAC key", and `.env.example` leaves `HERNESS_SECRET__REDACT_HMAC_KEY=` empty.
  - Pinning a derived constant is the only reproducible option, so I accept it.
  - Spec note: T11-16 (synth profile/.env) must publish this exact 64-hex value as the synth `.env` value. Otherwise pipeline content hashes under `synth` will not match the truth hashes, and the fake LLM's oracle lookups (keyed by `content_hash`) will silently miss.
- (b) **name_directory.csv format:** this is a real cross-spec conflict, not a defect in this card.
  - Spec 11 U11-20 explicitly requires the header `first_name,last_name`.
  - Spec 10 U10-38 `NameDirectory.from_files` requires a `display_name` (and `alt_names`) header.
  - The builder followed spec 11, which is binding here, and fed the names to `NameDirectory` through the `display_names_file` path. Spec 10 step 5 treats that path the same as step 2 (variants `first last`, `last, first`, `last,first`), so the truth hashes are correct.
  - As it stands, pointing `security.redaction.directory_file` at this CSV would load zero names.
  - Spec note for T11-16: either spec 11 switches the CSV to `display_name,alt_names`, or the synth profile feeds the names through `extra_names` or the display-names file.
- (c) **Additive fields** (`ShardResult.row_counts`, `s4_noise`, `AggregateResult`, `WorkerContext`, `write_truth(config_dir=)`): the defaults keep the spec signatures callable as written, and the spec names `WorkerContext`/`AggregateResult` without fields. Acceptable.
- (d) **Budgets and §2 rows:** every file is within budget, and the private-sibling rows (`_shard_run` 280, `_shard_io` 220) are in the spec diff. OK.

### Strengths
- A real two-phase spawn pool with deterministic `imap` ordering. The initializer-failure handling avoids Pool's endless worker respawn.
- Lake writers are aborted on every failure path, and a test confirms no lake or temp file is left behind.
- UT11-24 recomputes `content_hash` independently from the lake payload, and a separate test confirms truth contains no planted PII.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. `tools/synth/shards.py:133` and `:269`: the docstrings say results are summed "in plan order", but they are summed in phase order (all incidents first, then the rest). As a result, the order of `AggregateResult.files` differs from plan order. Fix the wording, or fold the results back by `shard.index`.
2. `tools/synth/_shard_run.py:214` together with `:252`: `gen.records.extend(extra)` runs before `_s4_noise`, so duplicate and later-version re-emits of S4 events count in both the numerator and the denominator of `generated_noise_ratio`. Check against U11-13 whether the ratio should cover only background and plant events.
3. DRY: the tmp-then-`os.replace` helper appears three times in this card's files (`_shard_io.py:82-88`, `truth_writer.py:59-66`, `inbox.py:32-37`). `inbox` and `truth_writer` could reuse `_shard_io._replace_write`.
4. The builder reported no TDD RED evidence (tests were written after the implementation). This is a process note only.

### Spec notes to record (controller)
- SN-a: the synth profile's fixed HMAC key, `sha256(b"herness synth profile fixed test key")`, must be carried into the synth `.env` or profile (T11-16).
- SN-b: the `name_directory.csv` header (`first_name,last_name`, spec 11 U11-20) conflicts with the `NameDirectory` CSV contract (`display_name,alt_names`, spec 10 U10-38). Reconcile in T11-16.
- SN-c: incident shards are generated one whole month at a time, because the plants need the month's background. The 131,072-record chunking applies to non-incident generation and to every lake write.

### Assessment
**Task quality:** Approved
**Reasoning:** All three units match spec 11. The tests, the gates and a real tiny generation all pass: no dot files, `cmn_department` and `task_sla` are present, and truth is outside `data/` with no planted PII. The open items are cross-spec gaps for T11-16 and minor polish.
