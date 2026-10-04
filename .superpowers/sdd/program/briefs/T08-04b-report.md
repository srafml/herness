# T08-04b report: classify() maps httpx2 exceptions

Status: DONE_WITH_CONCERNS (test-ID note below). Worktree agent-a498c121c18a9fb6e, base f529d37.

## What was implemented
- New private sibling `herness/core/resilience/_classify_httpx.py` (23 lines, budget 120): two class tuples,
  `HTTP_STATUS_ERRORS = (httpx.HTTPStatusError, httpx2.HTTPStatusError)` and
  `HTTP_UNAVAILABLE_ERRORS = (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError,
  httpx2.ConnectError, httpx2.TimeoutException, httpx2.RemoteProtocolError)`. Only exception classes are imported.
- `herness/core/resilience/classify.py` 229 -> 226 lines (budget 230): drops its `import httpx` and
  `_HTTPX_UNAVAILABLE`; rules 2 and 3 now test the sibling tuples. Every httpx tuple member is unchanged
  (add, not replace); `_from_status`, `_body`, `_redacted`, `_msg` are shared, so httpx2 gets the same
  Retry-After parsing (httpx2.Headers is a Mapping), redact-before-cut (4 000 window, drop last 500, cut 500),
  2 KB cap and ResponseNotRead fail-closed. Callable-module (`__class__` swap) untouched.
- No new exports in `resilience/__init__.py`. No pyproject change needed: import-linter has
  include_external_packages=false (13 contracts kept); TID251 bans only httpx2 transports/request functions.
- Spec `docs/impl/08-resilience-and-jobs.impl.md`: §2 row for `_classify_httpx.py` (budget 120; classify.py
  row's Extra imports now "none (httpx classes via _classify_httpx)"), and an "httpx2 twins (T08-04b)" note after
  U08-16 rule 8. `.secrets.baseline` line numbers for that doc refreshed (no entries dropped, LF).

## Tests
New file `tests/unit/core/resilience/test_classify_httpx2.py` (214 lines, pytestmark unit). Every httpx2 case
also asserts the httpx twin gives the identical class, message and retry_after:
- UT08-10: not-a-subclass precondition; status 401/403/404/418/429/500/502/503/504/529 x 4 families;
  429 Retry-After 7 and clamp to 86 400 with config, none without header; ConnectError, Connect/Read/Write/
  PoolTimeout, RemoteProtocolError x 4 families; httpx2.DecodingError stays unclassified (rule 8) like httpx.
- UT08-11: 5 000-char 503 body with e-mail + api_key (redacted, detail <= 500, message <= 2 KB); e-mail
  straddling char 500 and a value split at the 4 000-char window edge (redact-before-cut); multibyte body
  <= 2 048 bytes; redact_text None -> no detail; streamed unread httpx2 body -> no detail.
- UT08-12: each classified httpx2 transport error drives `breaker_transition` (threshold 1) to the same open
  row and `breaker_open` kind as its httpx twin.

RED: `PYTHONUTF8=1 uv run pytest tests/unit/core/resilience/test_classify_httpx2.py -q -p no:logging`
-> 76 failed, 2 passed (e.g. `assert FatalError is AuthError`, message "unclassified HTTPStatusError").
GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/core/resilience -q -p no:logging` -> 479 passed, 1 skipped
(symlink privilege). Coverage: _classify_httpx.py 100 %; classify.py 98 % (line 116 pre-existing).

## Gates
ruff format / ruff check: clean. mypy (full, 218 files): 0 issues. lint-imports: 13 kept, 0 broken.
check_module_size: rc 0 (parser reads the new row: 120). check_type_ownership: rc 0.
Wider suite `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`: 3 failed, 6126 passed,
13 skipped, 2 xfailed. The 3 failures are the known-red ones on this base (IT00-01 pre-commit run, ST10-25
repository scan, ST05-13(a) harness client lint), none touch resilience.

## Deviations / concerns
- Test IDs: the brief says "UT08-11, UT08-12", but in spec §11.1 UT08-12 is the breaker state table (U08-23);
  the classify mapping cases (timeout, connect, 429, 5xx, 4xx) are UT08-10 in the tree. I used UT08-10 for the
  mapping twins, UT08-11 for body/cap twins, and added a UT08-12 test tying httpx2 classification to breaker
  transitions so the brief's IDs are each covered. Controller may want the brief's Tests row read as
  UT08-10/UT08-11(/UT08-12).
- Twin tests live in a new file because test_classify.py is already 456 lines.

## Commit
e778c30 feat(resilience): classify maps httpx2 exceptions (T08-04b). Note: this commit appeared in the worktree
while I was staging (not created by my git commit call; author san k); its tree matches my working tree exactly
(status clean after it), so I did not amend or re-commit. The earlier `wip(T08-04b)` RED checkpoint did not land
(pytest-unit hook fails on the known-red ST10-25).
