# T08-04b review: classify() maps httpx2 exceptions

Reviewed: e778c30 on f529d37 (worktree agent-a498c121c18a9fb6e). Read-only. Verdict: **Approved**.

### Spec Compliance
- ✅ Goal: rules 2 and 3 of U08-16 match httpx2 twins. `classify.py:159` tests `HTTP_STATUS_ERRORS` (httpx + httpx2 `HTTPStatusError`), `classify.py:161` tests `HTTP_UNAVAILABLE_ERRORS` (ConnectError, TimeoutException, RemoteProtocolError for both libraries). Everything else (`_from_status`, `_body`, `_redacted`, `_msg`, `_max_s`) is shared, so the outcomes cannot drift apart.
- ✅ Add, not replace: every httpx class that was matched before is still in the tuples (`_classify_httpx.py:14-23`). The existing `test_classify.py` httpx cases still pass.
- ✅ Hierarchy parity (probe of httpx 0.28.1 vs httpx2 2.13.1): both libraries have the same exception classes and the same MROs, except for one httpx2-only class, `SSEError` (a TransportError). `TimeoutException` has exactly Connect/Read/Write/PoolTimeout under it in both. `ProtocolError`/`LocalProtocolError`/`ProxyError`/`ReadError` etc. were not mapped for httpx, and they are not mapped for httpx2 either (rule 8 → FatalError, checked by probe). `SSEError` → FatalError. That is consistent because the spec does not map it.
- ✅ 429 Retry-After and clamp: httpx2.Headers is a Mapping, so parsing works. Tests cover 7 s, the 86 400 clamp and no header, each asserted identical to the httpx twin (`test_classify_httpx2.py:103-114`).
- ✅ 503 body redact-before-cut (redact the first 4 000 chars; if the body is longer than the window, drop the last 500 redacted chars; then cut to 500) and the ≤ 2 KB cap: this is the shared `_redacted`/`_msg` code. The httpx2 twins cover the 5 000-char body, an e-mail straddling char 500, a value split at the window edge, and a multibyte body (`test_classify_httpx2.py:140-181`).
- ✅ ResponseNotRead fails closed: httpx2.ResponseNotRead is a RuntimeError subclass. `_body`'s `except Exception` catches it (probe: an unread httpx2 503 gives "source call failed: HTTPStatusError HTTP 503"). There is a test at `test_classify_httpx2.py:191-196`.
- ✅ Ordering vs SDK rules: installed openai/anthropic `APIStatusError`/`APIConnectionError`/`APITimeoutError`/`OverloadedError` have no httpx or httpx2 base in their MRO (probe). So rules 2/3 can never capture an SDK exception, and rule 4 is still reached. Nothing changed in the order.
- ✅ Callable module: `herness.core.resilience.classify` is still `_CallableModule` and callable (probe).
- ✅ No new exports: `resilience/__init__.py` is unchanged at 92/120.
- ✅ Size budgets: classify.py is 226/230. `_classify_httpx.py` is 23/120. The §2 row was added and the classify row's import column was updated. The U08-16 "httpx2 twins" note was added.
- ✅ Import rules: ruff TID251 bans only httpx/httpx2 clients and transports. Exception-class imports pass (`ruff check herness/core/resilience tests/unit/core/resilience`: All checks passed). lint-imports: 13 kept, 0 broken. `ruff format --check`: clean.
- ✅ Tests: `PYTHONUTF8=1 uv run pytest tests/unit/core/resilience -q -p no:logging` gives 479 passed, 1 skipped (symlink privilege, environmental). Coverage (branch): `_classify_httpx.py` 100 %, `classify.py` 98 % (line 116 and 2 partial branches were already there before this card). Both are above the 90 / 85 gates. The new file has `pytestmark = pytest.mark.unit` and `test_ut08_NN_` names.
- ✅ Controller ruling on test IDs: I agree with reading the brief's "UT08-11, UT08-12" as UT08-10 (mapping twins) + UT08-11 (body/cap twins) + UT08-12 (breaker tie-in). The spec §11.1 rows are UT08-10 = classify mapping incl. httpx statuses and UT08-11 = 503 body/2 KB (lines 2395-2396). The brief's "UT08-12" does not match the spec row (breaker state table, U08-23), so the ruling is the right reading.
- ⚠️ Cannot verify from diff: the `.secrets.baseline` line shifts (+3 for the two docs/impl/08 entries) look right given the 3 inserted doc lines before them, but I did not run detect-secrets. The report says the commit hooks were skipped only for the known-red ST10-25.

### Strengths
- Each httpx2 case also asserts `_same(...)` against its httpx twin (class, message, retry_after). That turns "fires identically" into something the tests actually check, not just parallel assertions.
- The two libraries are handled as two class tuples in one private sibling. There is no duplicated mapping logic.
- The not-a-subclass precondition test (`test_classify_httpx2.py:77-80`) documents why the twins are needed and would flag it if httpx2 ever re-bases on httpx.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. `tests/unit/core/resilience/test_classify_httpx2.py:184-188`: the httpx2 twin set has no counterpart of `test_classify.py:305` (`redact_text` raising → no detail). The code path is shared, so the risk is low, but "every httpx case" is not literally covered.
2. `tests/unit/core/resilience/test_classify_httpx2.py:199-214`: the test tagged UT08-12 is a classify→breaker tie-in, not a §5.3 state-table row, which is how spec §11.1 defines UT08-12 (U08-23). It is fine under the controller ruling, but the ID stretches the spec row's definition. Consider noting the ruling in the spec's UT08-12 row, or dropping the test.
3. `herness/core/resilience/_classify_httpx.py:14-23` / spec U08-16 note: `httpx2.SSEError` (the only httpx2-only class) is not named. It falls to rule 8 (FatalError), which matches the spec text. If SSE streams from local model servers are expected to be retryable, it may later need a spec decision. Not in scope for this card.

### Assessment
**Task quality:** Approved
**Reasoning:** Both httpx and httpx2 now go through one shared mapping path. That is verified by twin-equality tests, by hierarchy/SDK/callable-module probes, and by green tests, ruff, lint-imports and coverage. Budgets and export rules hold. Only minor test-completeness notes remain.
