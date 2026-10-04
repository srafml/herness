# T03-14 review — Laya model files and LayaDecider backend

Reviewed: worktree agent-aa6ce388466d1eb9d, c3eee74..cbe8866 (3 commits). Read-only.
Checks run by the reviewer: card tests with `--cov-branch` (72 passed, 2 skipped: nested-symlink case without symlink privilege, IT03-16 without laya/CUDA); ruff check + format --check on the 8 card files: clean; mypy on the 2 modules: clean; `tools/check_module_size.py`: exit 0; lint-imports: 13 kept. layout.py, settings.py, gpu.py, jev_wire.py and decide.py are untouched (empty diff stat).
Coverage: `deciders/laya.py` 173 stmts / 0 missed, 38 branches / 0 partial (100 %); `laya_models.py` 190 / 9 missed (95.3 % line), 52 branches / 3 partial (>= 85 % branch). Both meet the thresholds.

### Spec Compliance
- ✅ Spec compliant (minor findings below)

| Unit / test | Result | Note |
|---|---|---|
| U03-115 LayaManifest | ✅ | extra=forbid, strict, frozen; version pattern; accepted => accepted_by + accepted_at; required hyperparams and weight names; relative-name and sha256-hex checks |
| U03-116 read_current | ✅ | reads <= 65 bytes, rejects > 64, ASCII only, pattern-validated; missing file or bad content -> `ConfigError("laya CURRENT missing or invalid")` |
| U03-117 write_current | ✅ | version validated through `laya_dir`; mkstemp in the same dir, flush + fsync, `os.replace`; OSError -> FatalError and the temp file is removed |
| U03-118 verify_model_dir | ✅ | lstat symlink + reparse-point (junction) -> rejected; containment under laya_root; pickle suffixes (case-insensitive) outside checkpoints/; model.safetensors required; manifest <= 256 KB; version match; require_status; hashes in 8 MB blocks; memo per (path, size, mtime_ns) under a lock; messages `<version>: <check>` only |
| U03-119 new_version_id | ✅ | UTC date, max n + 1, starting at 1 |
| U03-57 LayaDecider/load/unload | ✅ | verify -> `HF_HUB_OFFLINE=1` -> lazy `laya.load(dir, fast=)` -> `.to(device, dtype)` else `.model.to`; any load failure -> `ModelUnavailable("laya load")`; unload drops refs and calls `release_cuda()` |
| U03-58 decide | ✅ | grouping by asked-id tuple; >20-option choice questions excluded from the wire and sent to `predict_shortlist(agent, state, [q_wire], embed_fn=, k=16)`; missing embed_fn -> ConfigError before any model call; backoff with `start_batch=call_batch`, `fault_name="decider.batch"`; 30 s timeout -> ModelUnavailable; OutputValidationError (including a choice answer without `probabilities`) -> item error output with the class name; input order kept |
| U03-59 health | ✅ | read_current + verify_model_dir(require_status={"accepted"}); ConfigError -> `ModelUnavailable("laya: <reason>")` |
| UT03-55 | ✅ | bad hash -> ConfigError, laya never loaded |
| UT03-56 | ✅ | 300 items -> predict_batch sizes [256, 44], batch_size 64, sort_by_length; 300 shortlist calls with k=16; missing embed_fn -> ConfigError |
| UT03-57 | ✅ | missing CURRENT, candidate -> ModelUnavailable x2; accepted -> ok |
| UT03-111 | ✅ | |
| UT03-112 | ✅ | |
| UT03-113 | ✅ | model.bin, symlinked dir (real NTFS junction on Windows), wrong status |
| UT03-114 | ✅ | -1, -2 -> -3 |
| ST03-07 | ✅ | flipped byte refused by verify (accept path), load and health; laya never loaded |
| IT03-16 | ✅ (gated) | markers integration + gpu + slow; skips without laya, CUDA and HERNESS_IT_LAYA_DATA; asserts full answer count and >= 98 % argmax agreement. Not run here. |
| UT03-45 extension | ✅ | LayaDecider conforms to the runtime-checkable protocol without loading |

Sub-controller rulings checked: lazy `importlib.import_module("laya")` with a fake module in `sys.modules` ✅; private `_call_with_timeout` matches U08-32 steps 1-5 (daemon thread "herness-timeout", join, `ModelUnavailable("call timed out after <t>s")`, re-raise unchanged, return value) and carries the `T08-07:` marker ✅; tests/support/fake_laya.py ✅; no registry decorator ✅; IT03-16 gated ✅.

Builder-declared deviations (judgement):
- verify_model_dir rejects files missing from `weights_sha256` and any link entry: **acceptable**. U03-115 defines `weights_sha256` as covering every file except manifest/eval/calibration/checkpoints, and this is the only way to enforce the tokenizer-coverage invariant. It fails closed (TH03-05, TH03-08). Carry-over: distill must list every file it writes. Stray OS files (Thumbs.db, .DS_Store) will also fail verification.
- The manifest requires hyperparams trainer/seed/round/round_kind and model.safetensors + rl_agent_config.json hashes: **acceptable**. This is the spec wording ("includes", "covers").
- new_version_id refuses a naive `now`: **acceptable**. The postcondition fixes the date in UTC, and ConfigError is a reasonable error for this.
- load() is idempotent, the instance refuses to reload after unload(), and decide() lazy-loads: **acceptable**. This is consistent with "loaded at most once per instance".
- Shortlist calls go through OOM backoff and the timeout: **acceptable**. The §7 error table says "Laya timeout (30 s per call)".
- A wrong predict_batch result count raises ModelUnavailable: **acceptable**. It is a backend-level failure (§3.9 shared rules) and must not produce misaligned outputs.

- ⚠️ Cannot verify from diff / spec-inherent:
  - V-11: the real `laya.load` signature, `.to(device, dtype)`, the predict_batch result shape, and whether `predict_shortlist` returns a **full-label** `probabilities` distribution. If it returns top-k labels only, `parse_wire_answers` rejects it, and every wide-question item becomes an error output. The fake returns all 30 labels (zeros outside the top k).
  - The memo key (path, size, mtime_ns) is spec-mandated. A same-size tamper that restores mtime within one process is therefore not re-hashed. There is also the usual verify-then-load TOCTOU.
  - A timed-out predict_batch thread keeps running on the GPU (U08-32 semantics), so the next batch can overlap it.
  - IT03-16 has to run on the dev box before `fast` is enabled.

### Strengths
- The security checks are tight: they reject links at the directory and entry level (including Windows junctions), check containment after resolve, match pickle suffixes case-insensitively, cap sizes before parsing, and use fixed-vocabulary error messages carrying version and check context.
- The atomic CURRENT write cleans up its temp file on failure.
- Tests assert real behaviour: exact batch sizes, shortlist k, input order, OOM retry through the timeout thread, error-message anchors, memo re-hash on an mtime change, and a real junction on Windows.
- Module budgets are respected (258/260, 286/330). The two modules have 100 % and 95 % line coverage.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/enrich/deciders/laya.py:272 — Item failures are logged as `enrich.decider.invalid_output` with a `rule` field. That event is not in the §8.1 catalog, which defines `enrich.decide.item_failed` (WARNING, `decider`, `error_class`). Line 169, `enrich.decider.laya_loaded`, is also uncatalogued. Use the catalog event, or add the new events to §8.1.
2. tests/unit/enrich/test_laya_decider.py:99 (and :152-195, which do not use the `agent` fixture) — `HF_HUB_OFFLINE=1` leaks into the rest of the pytest session. `monkeypatch.delenv(raising=False)` records nothing when the variable is absent, so load()'s later assignment is never undone. Verified: after the file runs, `os.environ["HF_HUB_OFFLINE"] == "1"`. Fix: `monkeypatch.setenv("HF_HUB_OFFLINE", "0")` followed by `delenv`, or save and restore in an autouse fixture.
3. herness/enrich/laya_models.py:157-158 — The nested "linked entry present" check is never exercised on Windows: the test at tests/unit/enrich/test_laya_models.py:247-251 skips without symlink privilege. Add the junction fallback that :195-197 already uses.
4. herness/enrich/deciders/laya.py:109 — The dtype comes from `settings.dtype` (bf16|fp32). U03-57 says `torch.bfloat16`. This is reasonable because LayaSettings owns the knob, but it is an undeclared deviation and should go in the report.
5. herness/enrich/deciders/laya.py:231-243, 254-262 — Exceptions from predict_batch or predict_shortlist that are neither a timeout nor an OOM (for example a CUDA RuntimeError) propagate raw instead of as `ModelUnavailable`. §3.9 routes backend-level failures to DeciderChain through ModelUnavailable. The spec names only the timeout, so this is not a spec breach, but consider wrapping (a follow-up with T08-10).
6. herness/enrich/laya_models.py:178-179 (also 92-93, `from exc` with UnicodeDecodeError) — `_fail` is raised inside `except ValidationError` without `from None`. The pydantic error, which contains `input_value=` fragments of the manifest, stays attached as `__context__` and appears in tracebacks. Use `from None` to honour "never file contents".
7. herness/enrich/deciders/laya.py:264 — Per-state shortlist calls reuse `fault_name="decider.batch"`, so fault plans that count `decider.batch` hits now also count shortlist calls. Document this or accept it.
8. tests/support/fake_laya.py — §11 describes the fake as "a 2-layer tiny Laya-shaped fake agent". This one has no model layers. The shape is sufficient for the unit rows, but the gap should be noted.
9. herness/enrich/laya_models.py:60-62 — `created_at` and `accepted_at` are not required to be timezone-aware. This is a nit.

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit and test row in the card is implemented and backed by meaningful tests, all gates pass and coverage is above the thresholds. The declared deviations are defensible readings of U03-115/U03-57/§7. The findings are minor: log-event catalog names, a test env leak, and hardening polish.
