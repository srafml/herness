# T10-03 review: Root config, load, cache, hash (build e4ca326, base 2b2e44d)

**Verdict: Approved.** No Critical or Important findings. Minor items below can be done in a follow-up or routed.

## Gates (re-run by the reviewer in the worktree)
- `ruff check .`: all checks passed. `ruff format --check .`: 153 files already formatted.
- `mypy`: no issues in 68 source files.
- `lint-imports`: 11 kept, 0 broken. C1 still holds exactly the one settings exception.
- `tools.check_type_ownership`: exit 0. `tools.check_module_size`: exit 0 (config.py 295/320, config_view.py 131, errors.py 380/380, layout.py 46).
- `PYTHONUTF8=1 pytest -m "(unit or integration) and not slow" -q -p no:logging`: 1455 passed, 7 deselected, 1 xfailed (the existing IT00-02 marker), no warnings.
- `tests/bench/test_bt10.py`: 2 passed. `--require-test-ids --collect-only`: OK (1463 collected).
- Coverage (line/branch): config.py 100/100, config_view.py 100/100, errors.py 100/100, store/layout.py 100/100.
- Extra probe: `config_hash(load_config(...), key_id="k")` gave the same `cfg_f9ed1c5b48be8c6a` for PYTHONHASHSEED 1, 2 and 3, each with a different tmp dir. So the hash does not depend on the absolute tmp path or on set/hash ordering. No settings model has a set-typed field.
- Extra probe: malformed env JSON/YAML, a bad env int, a bad `--set` value and a sentinel in `--set models.deciders=` each gave a ConfigError with no sentinel in str() or issues.

## Spec compliance per unit
| Unit | | Evidence / note |
|---|---|---|
| U10-01 ProfileName, GATED_PROFILES | ✅ | config.py:22 re-export (one declaration in config_sources); config.py:59 |
| U10-08 HernessConfig | ✅ | config.py:101-156. The field set follows design 10 §3.1. Source order: init > GuardedEnv > FilteredDotEnv > ProfileYaml > FilesYaml. frozen and extra=forbid. SourcesFileConfig (config.py:83) and ModelsFileConfig (config.py:90) are declared here. The context guard is in the before-validator and in settings_customise_sources (config.py:127-131, 149). Deviation N3: the root `_restore_version` validator (config.py:133-137) is acceptable, see m4. |
| U10-09 load_config | ✅ | config.py:198-226. Step 1 resolve_profile. Step 2 context var (reset in finally). Step 3 parse, then the file-only ban (config.py:168-173, covering first segment security/profile). Step 4 ValidationError → one ConfigError built with `include_input=False` and `from None`, plus issues and hint (config_view.py:65-75). Step 5 gate (config.py:176-185): hybrid needs hybrid_approved + by + on, premium needs premium_approved + by + on, chat_approved needs by + on. Step 6 (config_sources check_profile_egress). Step 7 absolute paths (config.py:188-195). Step 8 is re-deferred to T10-12 with a marker (config.py:221), per carry-over 9. Step 9 log. |
| U10-10 init/get/reset | ✅ | config.py:232-268. threading.Lock `_CACHE_LOCK`, `_RESET_HOOKS`. A replaced cache logs WARNING `config.cache.replaced`. Hooks run outside the lock. N10 (get_config loads under the lock instead of calling init_config) keeps the same behaviour and avoids a self-deadlock. |
| U10-11 config_hash | ✅ | config.py:273-295 and config_view.py:108-131. The excluded subtrees match the spec. `_inputs` holds redact_key_id and directory_sha256. The directory file is hashed in 1 MiB chunks, OSError → `ConfigError("cannot read directory_file")`. Uses canonical_json and sha256_hex. `_KEY_ID_PROVIDER` hook: None → "unresolved", provider ConfigError → "unresolved". Format `^cfg_[0-9a-f]{16}$`. |
| U10-12 effective_dict | ✅ | config.py:276-279 and config_view.py:21-34, 94-105. The key list is exactly the 10 names; matching is case-insensitive, which is stricter than the spec. Only str values not starting with `secret:` are masked. N6 (Path leaves written as POSIX) is consistent with the U10-11 "Path as POSIX text" postcondition. |
| U10-14 ConfigIssue | ✅ | config_view.py:46-56 (frozen dataclass; `__str__` renders `<severity> <path> <file or ->: <message>`). config.py re-exports it. The ≤300 limit is applied only by the converter (m6). |
| U10-108 EgressBlocked / ConfigError | ✅ (with deviation) | errors.py:257-273 ConfigError has the typed signature, `issues` as a tuple capped at 1000, listed in `_extra_attrs`. errors.py:292-304 EgressBlocked. The caller-side call `EgressBlocked(msg, *, egress_id=, reason=, hint=, details=)` works, the attributes read correctly, `str()` is the message only, and it pickles. The keyword types are `Scalar`, not `str \| None`, because of the 380-line cap (N2). The `reason` pattern is a spec precondition and is not enforced by raising; the property reads "invalid" for a non-matching value, which is consistent with HernessError's "constructor raises nothing". See m1 for the log-field gap. |

## Spec compliance per test row
| Row | | Tests |
|---|---|---|
| UT10-01 | ✅ | test_config_load.py:63 (profile beats file; paths absolute), plus :48, :56, :77, :152, :161 |
| UT10-02 | ✅ | test_config_load.py:89 |
| UT10-03 | ✅ | test_config_load.py:96 |
| UT10-04 | ✅ | test_config_load.py:103 (map merged key by key; tuple list replaced) |
| UT10-09 | ✅ | test_config_load.py:175 |
| UT10-11 | ✅ | test_config_load.py:205-239 (hybrid without flag, hybrid without by, premium without on, premium without flag, chat without on; chat with a record loads) |
| UT10-12 | ✅ | test_config_load.py:242-267 (local and synth with egress in herness.yaml; a synth overlay; empty defaults) |
| UT10-15 | ✅ | test_config_hash.py:55, :71, :82 |
| UT10-16 | ✅ | test_config_hash.py:97, plus :108, :129 |
| UT10-17 | ✅ | test_config_hash.py:145, :171 (dict "keyring" plus a provider hook; the real `fake_keyring` backend and redact provider arrive with the secrets/redact cards) |
| UT10-18 | ✅ | test_config_hash.py:229, :250 |
| UT10-22 | ✅ | test_config_hash.py:262, :277 |
| UT10-80 | ✅ | test_config_errors.py:16, :33, :41 |
| UT10-84 | ✅ | test_config_load.py:276, :295 (5 files), :305 |
| PT10-01 | ✅ | test_config_hash.py:216 (hypothesis, 60 examples, a random section inserted into the real effective dict) |
| BT10-01 | ✅ | tests/bench/test_bt10.py:19 (p95 < 1 s) |
| BT10-02 | ✅ (Windows half) | tests/bench/test_bt10.py:28. Cross-OS equality is not pinned by any test (⚠️ 1, m3). |
| ST10-16 | ✅ (partial by nature) | tests/security/test_st10_config.py:107. Covers effective_dict and the hash input. `config show` and snapshots do not exist yet (T10-14). |

Extra rows added under the carry-overs: UT10-06 nested half (test_config_load.py:120, :135), UT10-10 --set/env half (:184, :196), ST10-02 (test_st10_config.py:101), ST10-37 e2e (test_st10_config.py:75, :88).

## Carry-overs 1-18
| # | Tag | Status | Evidence |
|---|---|---|---|
| 1 | DO | done | herness/store/layout.py:9 direct import; :42-46 typed `HernessConfig`. Tests: test_store_layout.py (monkeypatched and real-config UT02-66). |
| 2 | DO | done | config_view.py:69 `errors(include_url=False, include_input=False, include_context=False)`; config.py:217 `from None`. Tests: test_config_load.py:120-149 and test_st10_config.py:75-95 assert that no sentinel appears in str() or repr(issues). |
| 3 | DO | done | tests/security/test_st10_config.py:75 (ReDoS, 600-char pattern, plain key) and :88 (NUL in paths.data). |
| 4 | DO | done | Every owner section loads from YAML lists through load_config (test_config_load.py:315; UT10-84 with the design 01 example; UT10-04 tuple replace). |
| 5 | DO | done | No `\d` in config.py, config_view.py or errors.py (checked with grep). errors.py:301 uses `[a-z_]{1,40}`. |
| 6 | RE | re-deferred | Deploy cards U10-79/82/83. |
| 7 | DO | done | config.py:276-279 only dumps; there is no re-validation path. The dump over the design 01 sources example is tested at test_config_load.py:285-287. |
| 8 | NOTE | no action | YAML native dates kept. |
| 9 | DO-partial | done (partial as ruled) | Every owner model on base is a field (config.py:108-125). The load test covers the shipped decisions/eval/metrics/models/weights plus minimal sources/mappings/resilience (tests/support/config_tree.py, test_config_load.py:315). Re-deferred: the `herness config validate` CLI → T10-14; cross-checks/`validate`/owner validators → T10-12 (marker at config.py:221); synth template → T10-13. Caveat: the shipped files load only after adaptation (see ⚠️ 2). |
| 10 | RE | re-deferred | T10-33 / U01-58. |
| 11 | RE | re-deferred | impl 01 files-connector card. |
| 12 | DO | done | UT10-02/03/09/12 are end-to-end (test_config_load.py:89-267). UT10-06 nested half, UT10-10 --set half and ST10-02 are covered. GATED_PROFILES at config.py:59; re-exports at config.py:22. |
| 13 | RE | re-deferred | w02-s02 integration / T02-11 (render_context.py is not on base). |
| 14 | RE | re-deferred | T11-40 (R3). The tests use local reset fixtures (test_config_load.py:21, test_config_hash.py:31, test_st10_config.py cfg_dir fixture). |
| 15 | RE | re-deferred | T07-02 + T10-12. `_MemoryStub.injection_patterns` receives the U10-16 list (config.py:73-76; test_config_load.py:312). |
| 16 | NOTE | applied | config.py:63-80 closed frozen stubs (R2). |
| 17 | NOTE | no action | — |
| 18 | NOTE | done | config.py:123 `resilience: ResilienceConfig`. |

Every [DO] item is closed; there are no missing items.

## Builder concerns: reviewer judgement
1. config_view.py split: accepted. It is a helper module forced out by the size limit, which impl 10 §1 allows. It imports only errors, ids, pydantic and stdlib, and was added to "core base is closed". Impl 10 §2 needs a module-map row (controller or spec follow-up).
2. errors.py at 380 / EgressBlocked without a typed `__init__`: acceptable. From the caller's view the signature is honoured: every keyword of U10-108 is accepted, the attributes are read-only and correct, and `str()` is the message only. Static typing is looser (`egress_id=7` passes mypy and reads as None). The `reason` pattern is a spec precondition, not a raise requirement. The builder's claim that "a malformed code never becomes a payload channel" is only true for the property (m1). If the controller raises the errors.py budget by about 11 lines, a typed `__init__` should replace it.
3. metrics/weights `version` re-insertion: acceptable. The spec's U10-16 and impl 04's owner models conflict. A cleaner fix would be `_check_version(keep=True)` for metrics/weights in config_sources (T10-02 file), m4. A spec note is needed.
4. POSIX paths in effective_dict: correct and needed. Verified stable across hash seeds and tmp dirs.
5. ModelsFileConfig `_PREFIX=None`: acceptable. It only turns off the owner's root-level conversion so that the root loader can name `models.yaml`. Nested owner ConfigErrors pass through without `issues`/`hint` (m5).
6. pyproject edits: acceptable. No new contract was added. The same named settings exception is added to two later contracts (store-no-upward, core-must-not-import-store/harness), and those contracts could not hold otherwise. C1 still holds exactly one ignore.
7. Shipped config/*.yaml not loading as-is: out of this card's Files. Needs routing (⚠️ 2).

## ⚠️ Items (cannot verify here / need controller routing)
1. Acceptance "config_hash equal on Windows and Linux CI": only the Windows side was verified. No test pins a golden hash value, so a Linux divergence would not fail CI (see m3).
2. Shipped `config/decisions.yaml`, `eval.yaml` and `models.yaml` lack `version: 1`, and `metrics.yaml` has `metrics: []`. With `herness.yaml` and the profiles also absent, the repo `config/` does not load as-is. Route to T10-13 (templates/profiles), T04-08 (metrics catalog) and the owners (03, 05, 11) whose tests validate those files directly.
3. Impl 10 §2 module-map row for `herness/core/config_view.py`, and the errors.py budget question (N2): controller or spec decision.
4. BT10-01 absolute timing: the builder reports a p95 of about 60 ms; the reviewer's bench run passed the < 1 s threshold on this box.

## Findings

### Critical
None.

### Important
None.

### Minor
- m1 errors.py:292-304. EgressBlocked stores `reason`/`egress_id` raw in `_context`, so `to_log_fields` emits a malformed reason verbatim (checked: `reason='PAYLOAD secret data here'` appears in log fields while `.reason == "invalid"`; `egress_id=5` is logged as 5). The property masking does not cover logs. Callers are internal and the scrubber still runs. Fix with the typed `__init__` once the budget allows, or normalise in to_log_fields.
- m2 config.py:282-288. `_key_id` maps every ConfigError from the provider to "unresolved", including U10-28's "secret backend unavailable". The spec only gives "missing" → unresolved. A backend outage would silently produce a hash that is not comparable to builds. Narrow it when the redact card registers the provider.
- m3 tests/bench/test_bt10.py:28. Add a golden-value test (a fixed fake dict with a Path leaf, a Decimal and a date, and a fixed key_id → a literal `cfg_…`) so that the cross-OS acceptance is enforced on Linux CI.
- m4 config.py:133-137. The `version` restore on the root model duplicates knowledge that belongs to the U10-16 loader. `_check_version(..., keep=True)` for metrics/weights in config_sources would keep the root free of per-section logic ("add no validation of their own"). The spec also needs a note.
- m5 config.py:90-98 / config_view.py:65-75. Owner-raised ConfigErrors (models.*, harness.*, and other `_PREFIX` owners) bypass the converter, so they carry no `issues`, no `hint="herness config validate"` and no file. That is inconsistent with U10-09 step 4 for those sections. It is acceptable until T10-12, but should be noted.
- m6 config_view.py:46-56. `ConfigIssue` does not enforce its `message ≤ 300` limit; only `validation_error` truncates. T10-12 validators constructing issues directly could exceed it.
- m7 config_view.py:59-62. The file attribution is inferred from the section name. An error that came from env or `--set` (for example `HERNESS_RETENTION__TRACES_DAYS=x`) is reported as `(herness.yaml)`, which is misleading. The message format `<loc> (<file>): <msg>` also differs from the spec's `<loc>: <msg>` (justified by UT10-84).
- m8 config_sources.py:157-164 (T10-02 type). `LoadContext` does not hold the parsed overrides that U10-09 step 2 lists. There is no consumer yet.
- m9 config.py:247-259. `init_config`/`get_config` hold the non-reentrant `_CACHE_LOCK` across `load_config`, which logs and will later call provider and validator hooks. Any future hook that calls `get_config()` during a load deadlocks. The spec mandates a `threading.Lock`; document the rule next to `_RESET_HOOKS`/`_KEY_ID_PROVIDER`.
- m10 Process note: the report says parts of config.py were written before their tests (not strict TDD order). Coverage is 100/100, so there is no functional impact.

## Assessment
**Task quality:** Approved
**Reasoning:** All eight units and all 18 test rows are implemented and tested end-to-end through `load_config`. Every [DO] carry-over is closed with evidence, and every gate is green with 100% line and branch coverage on the changed modules. The remaining items are minor hardening and routing notes (cross-OS golden hash, shipped-template fixes for T10-13/T04-08, the typed EgressBlocked `__init__` once the budget allows).


---

## Re-review (fix round 1): commit e683a51 on top of e4ca326

**Verdict: Needs fixes.** One Important item, and it is a one-line test fix. m1, m2 and m3 are otherwise resolved.

### Gates (re-run by the reviewer)
- ruff check: clean. ruff format --check: 153 files already formatted.
- mypy: no issues in 68 files.
- check_module_size: exit 0 (errors.py 380/380, config.py 297/320).
- lint-imports: 11 kept, 0 broken.
- `PYTHONUTF8=1 pytest -m "(unit or integration) and not slow" -q -p no:logging`: 1458 passed, 7 deselected, 1 xfailed (IT00-02), no warnings.
- Coverage: config.py and errors.py 100% line and branch.

### m1: masked where stored. Resolved
errors.py:292-304. `egress_id` and `reason` are now instance attributes listed in `_extra_attrs` and never put into `_context`.
- A non-str `egress_id` becomes None.
- A `reason` that does not match `[a-z_]{1,40}` becomes "invalid".
- to_log_fields and pickle carry only the masked values. This is proven by test_config_errors.py:41-52, which plants a sentinel.

The U10-108 caller signature `EgressBlocked(message, *, egress_id: str | None, reason: str | None, hint, details)` is now statically typed for the two R-19 attributes.

### m2: key_id fallback. Resolved
config.py:285-290. U10-11 says "when no provider is registered or the secret is missing, K = unresolved". U10-28 raises exactly `ConfigError("secret not found: <name>")` for a missing secret, so the prefix match is faithful. A backend failure now propagates (test_config_hash.py:188-201).
- Residual: the match is coupled to the message string. When the secrets card lands, it should keep the prefix or offer a typed marker. This is a note, not a finding.

### m3: pinned hash test. Resolved, and the test is sound
test_config_hash.py:208-226.
- The json-mode dump comes from `pydantic_core.to_json`, which renders Path with backslashes on Windows, so the test really exercises `posix_paths`.
- The input includes Decimal, date, a tuple and int dict keys, and the `logging` subtree is dropped.
- I recomputed the value by hand: a POSIX-form dict plus `_inputs`, run through `canonical_json` and `sha256_hex`, gives `cfg_700ea630db05d0a7`, which matches the pin.

It uses a fake model rather than a loaded HernessConfig. That is justified, because pinning the full loaded config would break on every template change.

### EgressBlocked constructor regressions (callers grepped)
The only constructors in the tree are in tests/unit/core/test_config_errors.py; there are no production callers yet.
- `message` is no longer positional-only. Every other HernessError subclass uses `/`. This is harmless: `EgressBlocked(message="x")` now binds to the message instead of becoming a context key. It is a Minor consistency point (r2).
- "No free context keywords" is not actually true. `**kw: _Kw` still forwards any keyword to HernessError's `**context`. mypy accepts `host="h"` because `_Kw` includes `str`, and at runtime I checked that `EgressBlocked("m", host="h").context == {"host": "h"}`. Existing context behaviour is therefore preserved, so this is not a regression. The only problem is that the builder's note is inaccurate.
- The HernessError contract says the constructor raises nothing, and that now breaks for a mistyped `details`. `EgressBlocked("m", details="oops")` passes mypy (`_Kw` includes `str`, and the call is `type: ignore`d) and raises `AttributeError: 'str' object has no attribute 'items'` at runtime (checked). The base HernessError types `details` as a Mapping, so mypy catches the same mistake there. Minor (r3), because callers are internal. It goes away with a properly typed `hint: str | None = None, details: Mapping[str, str] | None = None` once the errors.py budget allows about 2 more lines.
- A non-str `reason` whose `str()` happens to match the pattern would be stored un-coerced (`self.reason = reason`). This is theoretical; I did not raise it as a finding.

### New findings

#### Critical
None.

#### Important
- r1 tests/unit/core/test_config_errors.py:37. `assert e.EgressBlocked("x", reason=3).reason  # type: ignore[arg-type] == "invalid"` has the `== "invalid"` inside the comment. The assertion only checks truthiness, so it would also pass if the raw `3` were stored. That makes it a test that asserts nothing meaningful. Fix: `assert e.EgressBlocked("x", reason=3).reason == "invalid"  # type: ignore[arg-type]`.

#### Minor
- r2 errors.py:298-299. `message` is not positional-only, unlike HernessError and every sibling subclass (`/`). Restore the `/` when the signature is next touched.
- r3 errors.py:29, 298-300. `_Kw = str | Mapping[str, str] | None` together with the `type: ignore[arg-type]` loses static checking of `hint`/`details`. A `details` given as a str raises AttributeError from the constructor, which breaks the "constructor raises nothing" contract. Also, the fix note's claim that the constructor takes "no free context keywords" is inaccurate, since any keyword still reaches `context`. Replace with explicit `hint`/`details` parameters when the controller grants the errors.py budget (N2).

### Status of the round-0 minors
m1, m2 and m3 are closed. m4-m10 are unchanged and remain Minor or routed as stated above.

**Task quality:** Needs fixes (r1 only; it is a one-line test correction and needs no further review round beyond confirming the line).
