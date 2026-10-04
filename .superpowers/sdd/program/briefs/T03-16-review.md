# T03-16 review — Ensemble and registration

Reviewed: worktree agent-adbb66aff419524b3, base a46205f, head 924da66 (content in 78b6cd1). Read-only.

Evidence gathered by the reviewer:
- `pytest tests/unit/enrich -q -p no:logging -k "UT03_63 or UT03_64 or UT03_65 or UT03_66 or UT03_67 or PT03_07 or UT03_45"` using the worktree .venv: 45 passed. Branch coverage: ensemble.py 100 %, deciders/__init__.py 100 %.
- ruff check and format --check: clean. Project mypy (`files = herness, tools`): 0 issues in 262 files. lint-imports: 13 kept, 0 broken. check_module_size and check_type_ownership: clean. Line counts 196/200 and 89/90. The tree was clean after the tests ran.
- Independent probe (scratch script): `ensemble_version` equals a hand-built sha256 over canonical JSON (`sorted` members as arrays, `f"{d}:{q}"` keys, `format(w, ".6f")`)[:12] and does not depend on the order of the weights mapping. `pool_log_linear` returns bitwise-identical output for all 6 member and weight orderings. For a tied pool [0.5, 0.5], argmax is index 0. Unanimous members with zero entries give agreement 1.

## Spec compliance

| Unit / test | Result | Notes |
|---|---|---|
| U03-65 pool_log_linear | ✅ | Weights renormalized over present members, with equal weights when all are zero or absent. Uses log(p+1e-6), a max-shifted softmax, np.argmax (lowest-index ties) for both the pool and the members, and ConfigError when there are no members. The extra precondition checks (vector shape, negative or non-finite weight) are allowed. Output does not depend on member order (names are sorted). |
| U03-66 ensemble_version | ✅ | Matches the postcondition exactly (verified independently). |
| U03-67 EnsembleDecider | ✅ | Reads only the cache: pushdown filter on content_hash, fingerprint, decider and version, then an exact per-question fingerprint check and a per-member (decider, version) pair check in Python. Calibrates with `apply_temperature(T)` from `CalibrationStore.temperature`, where a missing file means T=1.0. It returns the raw pool, sets `backend_confidence = agreement` and `probability = q[argmax]`, and omits questions that have no rows. It holds no client and calls no backend. Only cache OSError is mapped (StoreBusy or FatalError). The pick of the latest `decided_at` row is deterministic across part-file order (tie broken by content). |
| U03-68 register_deciders | ✅ | Five static `registry.register("decider", name)(cls)` calls. Idempotent, a clash raises the registry's ConfigError, and registry.py is not edited. |
| U03-69 build_decider | ✅ (with ⚠️) | Disabled backend -> `ConfigError("decider <name> disabled")`. Jev without a key -> `AuthError("decider jev has no api key")` (no secret name or value in the message). OpenJev key is optional through `secrets.exists`/`resolve`. `samples_override or settings.openjev.samples[depth]`. LLM gets votes[depth] and the settings temperature. Secrets are resolved only here. |
| UT03-63 | ✅ | Pooled vector to 1e-9 against a term-by-term reference; renormalization (scale invariance, absent `jev` weight); agreement 2/3; all-zero weights; tie; invalid inputs. |
| UT03-64 | ✅ | Changed weight and changed member version both change the version; the version is stable and does not depend on member order. |
| UT03-65 | ✅ | 3 members and 2 (+1) items; the calibrated member (T=2) is checked numerically; agreement; non-member version and stale fingerprint rows are ignored; absent questions; question_ids; latest row wins; write-order independence; StoreBusy. |
| UT03-66 | ✅ | Register twice on a clean registry (autouse `reset_registry` in tests/conftest.py); clash -> ConfigError. |
| UT03-67 | ✅ | openjev disabled -> ConfigError; jev without key -> AuthError; plus samples, override, key, image-tag forms, llm and laya wiring. |
| PT03-07 | ✅ | Hypothesis: output sums to 1, agreement is in [0,1], and unanimous members give that argmax with agreement 1. |

Item checks from the dispatch:
1. Deterministic pooling: ✅ (probe confirms bitwise order independence).
2. Version: ✅.
3. The decider reads only cached member rows and calls no LLM or backend: ✅.
4. Degradation: ✅ consistent. The ensemble has no backend, so a member backend failure or CircuitOpen upstream only means that member's rows are absent. The pool then renormalizes over the present members (design §5.9, F03-08 step 2: OpenJev unavailable -> Laya + LLM), and a question with no rows is absent. Missing calibration gives T=1.0 (CalibrationStore). A missing gold weight for any present member gives equal weights for that question (F03-08 step 4); the `ensemble_weights_default` warning is correctly left to U03-89. Nothing crashes on missing data.
5. Registration: ✅.
6. build_decider: ✅. The builder's deviations are acceptable:
   - image_tag fallback to the digest prefix: see ⚠️ 1.
   - Jev samples read from `settings.openjev.samples`: JevSettings has no samples, and "Jev: same" is a reasonable reading.
   - ConfigError for `ensemble` and for a missing llm tuple: these make the stated preconditions explicit. The ensemble is built by `run_ensemble_pool` (U03-89).
7. No text or secret in logs or messages: ✅. Neither module logs. The error messages are static or name only the decider.
8. Budgets, coverage, test IDs, docstrings, pytestmark, layering, mypy, ruff: ✅ (see Minor 5 for the test-only mypy note).

## ⚠️ Items for the controller

1. Cross-spec conflict on the OpenJev image tag. U03-52 derives `image_tag` from "the text between `:` and `@`" of `deploy.openjev.image`. Impl 10's deploy-time strict pin `^[a-z0-9][a-z0-9._/-]{0,200}@sha256:[0-9a-f]{64}$` forbids a `:tag`, so a production-valid image has no tag. The builder falls back to the first 12 hex characters of the digest (`openjev-<digest12>/<model>`). This is a sound choice: the version still changes whenever the pinned image changes. It needs a ruling (DECISIONS/DD row) so that UT03-50's `razorback16/openjev:0.4.0@sha256:...` fixtures and the docs agree on one form.
2. U03-67 says "Errors: none beyond IO (StoreBusy)". A malformed or mismatched calibration file raises ConfigError from `CalibrationStore.load` (T03-10 behaviour), and an OSError while reading a calibration file propagates unmapped (see Minor 1). Failing fast on a corrupt calibration file is defensible, but the spec does not list it.

## Findings

### Critical
None.

### Important
None.

### Minor
1. `herness/enrich/deciders/ensemble.py:177-181`: `_temperature` calls `CalibrationStore.temperature` without mapping OSError. A locked calibration file (EACCES/EBUSY) escapes as a raw `PermissionError` instead of StoreBusy, while the cache read at :158-162 is mapped. Fix: wrap the call in `try/except OSError as exc: raise io_error(exc, "cannot read calibration", decider=self.name) from exc`, or map it once inside `CalibrationStore._read` (T03-10 owner).
2. `herness/enrich/deciders/ensemble.py:23`: imports the private helper `_asked` from the sibling module `openjev.py`. This couples the ensemble to the OpenJev HTTP module (and its imports) and depends on a private name. Fix: move `_asked` (impl 03 §3.9 shared rules) to a shared, non-private location (e.g. `herness/enrich/decide.py` or `deciders/_shared.py`) and import it from there in both modules.
3. `herness/enrich/deciders/ensemble.py:151`: repeats the `q.fingerprint or question_fingerprint(q)` rule already private in `herness/enrich/cache.py:62-63` (`_fingerprint`). If one copy changes, the ensemble silently stops matching cache rows. Fix: expose one helper (e.g. `cache.current_fingerprint`) and use it in both places.
4. `tests/unit/enrich/test_ensemble.py:240-264` (UT03-65): no row exercises the Python-side `(decider, version)` pair check at `ensemble.py:166`. The pushdown `isin` on the version set already drops `("llm","local/other")`, so deleting the check leaves every test green (a mutation survives). Add a row written under another member's version (e.g. decider `llm` with version `LAYA[1]`) and assert that it is ignored. Also, no test pins a golden `ensemble_version` string for UT03-64, so the exact canonical form (`d:q` keys, `.6f`) is only checked by the reviewer's probe. Add one literal expected hash. Also, no choice or score member is calibrated with T != 1 (only bool). Add one.
5. `tests/unit/enrich/test_ensemble.py:282`: `assert decider.health() is None` fails mypy strict (`func-returns-value`) when the test is checked. Tests are outside the configured mypy `files`, so no gate fails. Fix: call `decider.health()` without asserting its result.
6. `herness/enrich/deciders/ensemble.py:35-36, 140-142, 195-196` and `__init__.py:71-72`: `# fmt: skip` hand-packing is used to fit the 200/90 line budgets, which leaves no headroom. Consider moving `_labels`/`_vector` to a shared label module (the same label-order rule exists in `evaluate.py:42`, `resolve.py:129` and `_sft_loop.py:38`). That frees lines and removes a fourth copy of the bool/score label order.
7. `herness/enrich/deciders/__init__.py:62,76-79`: `registry.get` runs before the disabled check. If the registry was not populated, a disabled backend reports "unknown decider" instead of "decider <name> disabled". This is harmless because the composition root registers first. Optionally check `enabled` before the lookup.

## Verdict
**Approved.** Every unit and test row matches the spec. Pooling and versioning are verified exact and order-independent, the ensemble never touches a backend, and registration and the factory follow U03-68/69. The remaining items are Minor or are cross-spec rulings for the controller (⚠️ 1 image tag).
