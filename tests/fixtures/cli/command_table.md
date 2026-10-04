| Command | Arguments and options | Role | Behavior owner | Units here |
|---------|-----------------------|------|----------------|------------|
| `init` | `--force` | admin | 09 | U09-93 |
| `doctor` | `--fix-hints`, `--sources` | any (also `denied`) | 09, 10 | U09-93, U09-94 |
| `status` | — | viewer | 08, 09 | U09-93 |
| `ui` | `--port N` | viewer | 09 | U09-93 |
| `worker` | `--gpu-class none,reasoning,decider,large`, `--concurrency N`, `--once` | admin | 08 | U09-93, U09-104 |
| `gpu load CLASS` / `gpu unload` | `CLASS` ∈ `reasoning`, `decider`, `large` | admin | 08 | U09-93 |
| `config validate` | `--profile NAME`, `--offline`, `--strict` | any (also `denied`) | 10 | U09-98, U09-106 |
| `config show` | `--profile NAME` | admin | 10 | U09-98 |
| `config hash` | `--profile NAME` | any | 10 | U09-98 |
| `secrets init` | — | admin, elevated | 10 | U09-98 |
| `secrets set NAME` | value from a hidden prompt only | admin, elevated | 10 | U09-98 |
| `secrets status` | — | admin | 10 | U09-98 |
| `secrets rekey` | — | admin, elevated | 10 | U09-98 |
| `deploy render` | — | admin | 10 | U09-98 |
| `deploy pull` | `--allow-download` (required) | admin, elevated | 10 | U09-98 |
| `deploy up CLASS` / `deploy down [CLASS]` | `CLASS` ∈ `reasoning`, `decider`, `large` | admin | 10 | U09-98 |
| `deploy rollback CLASS` | `CLASS` ∈ `reasoning`, `decider`, `large` | admin | 10 | U09-98 |
| `deploy prune` | — | admin | 10 | U09-98 |
| `deploy install BUNDLE_DIR` | — | admin, elevated | 10 (R-58) | U09-98 |
| `sync [SOURCE]` | `--entity E` (repeatable), `--full`, `--backfill --from D --to D`, `--reconcile`, `--check-mapping`, `--discover-fields`, `--wait/--no-wait`, `--inline` | admin | 01 | U09-95 |
| `build` | `--wait/--no-wait`, `--inline` | admin | 02 | U09-95 |
| `enrich` | `--build-id ID`, `--stage S`, `--depth D`, `--wait/--no-wait`, `--inline` | admin | 03 | U09-95 |
| `score` | `--build-id ID`, `--step S`, `--scenario NAME\|USD`, `--wait/--no-wait`, `--inline` | admin | 04 | U09-95 |
| `metrics list` | — | any | 04 | U09-95 |
| `pipeline` | `--from-stage build\|enrich\|score\|dq\|promote`, `--build-id ID`, `--wait/--no-wait`, `--inline` | admin | 02, 08 | U09-95 |
| `report funding\|org` | `--depth D`, `--budget USD` (repeatable), `--top-n N`, `--format html,md,pdf`, `--out DIR`, `--no-strict`, `--open`, `--wait/--no-wait`, `--inline` | admin | 06, 09 | U09-96 |
| `report render RUN_ID` | `--format LIST`, `--out DIR`, `--no-strict` | viewer | 09 | U09-96 |
| `review` | `--kind funding\|org\|both`, `--depth D`, `--at ISO_TIME`, `--wait/--no-wait`, `--inline` | admin | 06, 08 | U09-96 |
| `resume RUN_ID` | `--retry-dead`, `--force`, `--wait/--no-wait`, `--inline` | admin | 08 | U09-96 |
| `decide REC_ID accepted\|rejected\|deferred` | `--reason TEXT` (required), `--effective-at DATE` | reviewer | 07 | U09-96 |
| `chat` | `--session ID`, `--new` | viewer | 09 | U09-99 |
| `jobs list` | `--status S`, `--kind K`, `--limit N` | viewer | 08 | U09-97 |
| `jobs cancel JOB_ID` / `jobs retry JOB_ID` | — | admin | 08 | U09-97 |
| `review-queue list` | `--kind K`, `--status S` | reviewer | 09 | U09-97 |
| `review-queue approve ITEM_ID` / `review-queue reject ITEM_ID` | `--note TEXT` (required for reject) | reviewer (`weight_change`: admin) | 09 | U09-97 |
| `memory list` | `--layer L`, `--status S`, `--limit N` | viewer | 07 | U09-97 |
| `memory approve MEMORY_ID` / `memory reject MEMORY_ID` | `--note TEXT` (required for reject) (R-33) | reviewer | 07 | U09-97, U09-101 |
| `memory export-lora` | `--out DIR` | admin | 07 | U09-97 |
| `memory purge` | `--author-ref HASH` | admin | 07 | U09-97 |
| `eval` | `--suite golden\|classifier\|PATH.yaml`, `--profile NAME`, `--compare PROFILE`, `--depth fast\|standard\|deep`, `--ids G01,F01`, `--tags TAG`, `--repeat N`, `--mock-llm DIR`, `--no-judge`, `--resume RUN_ID`, `--baseline NAME`, `--set-baseline NAME`, `--compare-runs RUN_ID,RUN_ID,...`, `--wait/--no-wait`, `--inline` | admin | 11 | U09-98 |
| `distill` | `--active`, `--wait/--no-wait`, `--inline` | admin | 03 | U09-98 |
| `laya status` / `laya accept VERSION` / `laya rollback VERSION` | — | admin | 03 | U09-98 |
| `privacy delete` | `--record-id ID` (repeatable), `--reason-ref REF`, `--inline` | admin | 10 | U09-98 |
| `maintenance backup` / `maintenance purge` | `--dry-run`, `--inline` | admin | 10 | U09-98 |
