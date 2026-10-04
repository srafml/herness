# T01-13 re-review, fix round 1 (scoped: I1, I2, m1, m2)

Commit 102a8a2 (8494a59..102a8a2, test files only).

## Runs
- `uv run pytest -k "ST01_11 or ST01_14" -q -p no:logging` (PYTHONUTF8=1): 38 passed (was 25; 13 new self-tests)
- `--require-test-ids` on test_connectors_http_lint.py: 37 passed
- ruff check and ruff format --check on the 3 changed files: clean
- mypy `--explicit-package-bases` on the changed test files: 1 error (see n1). The project gate (`uv run mypy`, files = herness and tools) does not cover tests/.

## Findings status
| Finding | Status | Evidence |
|---|---|---|
| I1a `.query(` / QUERY | Fixed | `query` added to `_VERB_CALLS` and `QUERY` to `_OTHER_VERBS` (test_connectors_http_lint.py:32-37). Self-tests: `client.query(...)`, `send(method='QUERY')`. |
| I1b `Request(non-GET/POST)` + send | Fixed | `_constructor_findings` (lines ~95-110) flags `<httpx/httpx2 or alias>.Request(...)` and a from-imported `Request` / `Request as R` whose method is not a literal GET or POST. A non-literal method is flagged. The allowed-forms test adds `httpx.Request("GET")` and `Request(method="POST")` sent via `client.send`. |
| I2 httpx2 and module aliases | Fixed | `_HTTP_MODULES = {httpx, httpx2}`. The regex is now `httpx2?`. The import check covers both roots. `_bound_names` records `import httpx[2][.sub] as X`. |
| m1 sync_kill source | Fixed | Optional third argv `source`, default `files` (sync_kill.py). |
| m2 watermark comment | Fixed | Comments at test_files_crash_fault.py:264 and :281 mark the watermark asserts as guards only. |

## Are the self-tests genuine?
I re-ran the 13 new snippets with every new check reverted in memory, without editing the repo: `_HTTP_MODULES = {httpx}`, the old regex, QUERY and `query` removed, and `_constructor_findings` stubbed to `[]`.
- 12 of 13 are MISSED, so each fails without the new checks.
- 1 is still flagged: `httpx2.Request(method='DELETE', ...)`. The pre-existing `method=` literal check already catches it. The snippet is still valid coverage but does not prove the new Request check (n2).
- With the checks restored, all 13 are flagged.

## Real scan / weakening
- The herness/connectors scan is still green, with no new false positives.
- No existing check was removed or loosened. `_OTHER_VERBS` and `_VERB_CALLS` are supersets of the old ones. The old self-tests are all unchanged and pass.

## New minor notes (non-blocking)
- n1. test_connectors_http_lint.py:63: `modules, requests = set(_HTTP_MODULES), set()`. Strict mypy reports `Need type annotation for "requests"` [var-annotated]. The project gate does not cover tests, so this is not blocking. Fix: `requests: set[str] = set()`.
- n2. test_connectors_http_lint.py:~288: the `httpx2.Request(method='DELETE', ...)` snippet is also caught by the old `method=` check. Use a positional `httpx2.Request('DELETE', '/x')` to exercise the new path.
- n3. `.query(` is flagged for any attribute call, e.g. pandas `df.query(...)` or an ORM `session.query(...)`. There is no hit today, but a future connector (for example Mongo, Snowflake or Dataverse via pandas) may trip it. This is a deliberate trade-off (fail closed); document it in the comment at line 36, next to the `.connect(` note.
- (Info) Module rebinding (`x = httpx2; x.Client()`) is still not flagged. It is out of scope here: ST10-25 resolves rebinding across herness/.

## Verdict
**Approved.** I1, I2, m1 and m2 are fixed. n1-n3 are optional polish.
