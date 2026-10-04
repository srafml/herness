# Report: T10-33 Source HTTP client

Status: DONE

## Summary

Implements `herness.core.egress.source_http_client` (U10-110, R-06): the only way an impl 01
source connector (ServiceNow, Jira, monitoring adapters, the Dataverse Web API) will obtain an
`httpx2` client, restricted to that source's hosts with TLS verification that cannot be
disabled. `SourceHostTransport` refuses, before the inner transport is ever called, any
request whose host is outside both the source allowlist and the process egress allowlist,
whose scheme is not `https` on a non-loopback host, or whose URL carries user info; a response
past `max_response_bytes` raises `EgressBlocked("source response too large", reason=...)`.

## Files changed

| File | Before | After | Budget |
|------|-------:|------:|-------:|
| `herness/core/egress_clients.py` | 143 | 259 | 300 |
| `herness/core/egress.py` | 384 | 385 | 390 |
| `herness/core/_egress_source.py` (new) | 0 | 126 | 200 (new §2 row) |
| `docs/impl/10-config-security-deployment.impl.md` | — | +2 rows/edits | — |
| `pyproject.toml` | — | +1 line | — |
| `tests/unit/core/test_egress_source.py` (new) | 0 | 185 | — |
| `tests/security/test_st10_source_client.py` (new) | 0 | 215 | — |

`herness/core/egress_clients.py` gains `SourceHostTransport` and `source_http_client`.
`herness/core/egress.py`'s only change is the re-export (`source_http_client`, per the brief's
module-map row — `SourceHostTransport` itself is not re-exported, matching the row exactly).

### Why a private sibling (`_egress_source.py`)

Building `SourceHostTransport` + `source_http_client` plus all their non-constructing helpers
(precondition checks, the source/process allowlists, the refusal-reason function, the
body-too-large error) directly in `egress_clients.py` came to 345 lines, over its 300-line
budget. Per the brief's contingency, the non-constructing helpers moved to a new private
sibling `herness/core/_egress_source.py` (126 lines): `check_timeout_s`, `check_int`,
`check_verify`, `enabled_source`, `source_allowlist`, `source_refusal`, `blocked`, `body_error`,
`process_allowlist`, plus `MAX_SOURCE_TIMEOUT_S`/`MAX_SOURCE_RESPONSE_BYTES`. It builds no
`httpx2` client or transport, so the ST10-25 exemption list stays exactly `egress.py` +
`egress_clients.py` (verified: `test_st10_25_egress_files_are_the_only_exemption` and the
whole `test_st10_lint.py` file pass except the known-red case below).

Added a `docs/impl/10-config-security-deployment.impl.md` §2 module-map row for the new
sibling (budget 200, plenty of headroom) and a minimal U10-110 Algorithm step-1 edit recording
the source-allowlist ruling (see "Spec notes" below). Added
`herness.core._egress_source` to the "core base is closed" import-linter `forbidden_modules`
list in `pyproject.toml` alongside its siblings (`_egress_streams`, `_egress_transport`,
`_egress_scan`).

### Import-cycle note

`egress_socket.py` imports `LOOPBACK_HOSTS` from `egress_clients.py`; `_egress_source.py`'s
`process_allowlist` needs `egress_socket.SocketPolicy`/`_allowlist`. To avoid a module-level
three-way cycle (`egress_clients -> _egress_source -> egress_socket -> egress_clients`), the
`egress_socket` import in `process_allowlist` is a local (function-body) import with a
`# noqa: PLC0415` comment, matching the established pattern already used in `audit.py`,
`config.py` and `redact.py` for the same reason.

## Spec notes / rulings applied (binding, from the T10-33 brief)

1. **Source allowlist is asymmetric by source kind** (overrides the literal U10-110 Algorithm
   step 1 text, which read as if `hosts` always widens the client allowlist too): for a source
   **not** in `SDK_SOURCE_KINDS` the client allowlist is **exactly** the lower-cased `base_url`
   host; `hosts` entries only widen the **socket guard** for that source, never the client
   itself (impl 01 U01-15/§7; UT01-97). For an **SDK** source (`snowflake`, `mongodb`) the
   allowlist is the lower-cased `hosts` entries, and the `base_url` **parameter** host must be
   among them. Added the required test (`test_st10_58_hosts_widens_only_the_socket_guard_never_the_client`,
   tagged ST10-58/UT01-97): a servicenow client with `hosts: [sso.example.com]` still refuses a
   request to `sso.example.com` with `host_not_allowed`. I edited the U10-110 Algorithm step 1
   cell in the spec to record this ruling in one sentence (minimal edit, keeps the rest of the
   row unchanged).
2. **Process allowlist**: the installed `SocketPolicy.allowed_hosts` if one is installed, else
   `egress_socket._allowlist(cfg)[0]` computed fresh from `get_config()` — exactly as the brief
   specifies, reusing `egress_socket._allowlist` rather than re-deriving it.
3. **"enabled"**: read directly off `cfg.sources.sources` via `getattr`/`model_fields` (no
   import of `herness.connectors` from `herness.core`, matching the `_source_hosts` pattern
   already used in `egress_socket.py`). A source counts as disabled when its section is absent
   or `enabled` is not `True`.
4. **Metric**: no metric sink exists yet, so a `# T08-05: herness_socket_blocked_total{event="source_client"} += 1`
   marker is left at the one call site (`_egress_source.blocked`), matching the convention used
   throughout `egress.py`/`_egress_transport.py`.
5. **Logging**: `egress.source.blocked` (source, host, reason) via the existing `structlog`
   logger (`get_logger("core.egress")`, same logger name as the rest of the egress component).
6. **No egress JSONL line** for source traffic: confirmed by construction — `source_http_client`
   never touches `EgressLog`/`EgressGuard`; there is no code path that could write one.
7. Signature, postconditions (`follow_redirects=False`, `trust_env=False`, `httpx2.Timeout`,
   `httpx2.Limits`), and error reasons/messages match the brief verbatim (verified against the
   ST10-58/ST10-59 test rows one by one).

## Tests

- `tests/unit/core/test_egress_source.py` — UT10-82 (19 test functions): client configuration
  (no redirects, no env trust, given timeout/limits, real `httpx2.HTTPTransport` inner with
  `retries=0`), an SDK (`snowflake`) client with its `base_url` host listed in `hosts`, a
  disabled source, an unknown source, `timeout_s` bounds (0, negative, >600, NaN — note the cap
  is **600s** here, not the loopback/guarded 3600s cap), `max_connections` bounds (including
  bool/float rejection), `verify` preconditions (`False`, a non-path value, a non-existent CA
  bundle path), and the response-too-large cap (blocked past the limit, allowed exactly at it).
- `tests/security/test_st10_source_client.py` — ST10-58 (6 functions) and ST10-59 (4
  functions): absolute URL to an unrelated host, a look-alike-subdomain "next link", an
  un-followed 302 to another host (with the target host's mock route never called), an
  `http://` `base_url`, `user:pass@` user info, an SDK client whose `base_url` host is missing
  from its own `hosts`, and the UT01-97 "hosts never widens the client" case — all confirmed
  with zero real `socket.connect` calls (`record_connects`) or, for the redirect case, zero
  calls to the evil host's mock route. ST10-59: `verify=False` and a missing CA bundle path are
  `ConfigError`; a real local TLS server (`tests.support.egress_servers.tls_server`) with a
  freshly generated self-signed certificate (`cryptography`, never committed, no private key
  file written to the repo) not in the CA bundle fails with `httpx2.ConnectError` matching
  "CERTIFICATE" before any request body is sent; `SSL_CERT_FILE` and `HTTPS_PROXY` (plus the
  other proxy env vars) set to unreachable/bogus values are ignored (`trust_env=False`) while
  the client's own `verify=<cert path>` succeeds against the same server.

### RED evidence

Ran the two new test files against the pre-implementation state is not directly reproducible
after the fact (the factory and transport did not exist, so collection itself failed with
`AttributeError: module 'herness.core.egress_clients' has no attribute 'source_http_client'`
before I wrote a single test — verified this manually while iterating: every test in both files
failed at the `ec.source_http_client(...)` / `ec.SourceHostTransport` call before any assertion
ran, which is the expected RED for a brand-new factory/transport pair).

### GREEN evidence

```
PYTHONUTF8=1 uv run pytest tests/unit/core/test_egress_source.py tests/security/test_st10_source_client.py -q -p no:logging
............................. [100%]
30 passed in ~2s
```

```
PYTHONUTF8=1 uv run pytest tests/unit/core tests/security -q -p no:logging
1 failed (test_st10_25_repository_passes, known-red), 2215 passed, 2 skipped
```

```
PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging
3 failed (test_it00_01_pre_commit_run_all_files, test_st10_25_repository_passes,
test_st05_13_ast_lint_harness_builds_no_unguarded_clients — all three pre-existing/known-red
per controller-notes-w07.md Addendum 3, openai_compat.py TID251, T05-06b), 6078 passed,
13 skipped, 35 deselected, 2 xfailed
```

## Gate outputs

- `uv run ruff format --check` (touched files): clean.
- `uv run ruff check` (touched files): clean.
- `uv run mypy` (touched files): `Success: no issues found in 5 source files`.
- `uv run lint-imports`: `Contracts: 13 kept, 0 broken.`
- `uv run python -m tools.check_module_size`: exit 0.
- `uv run detect-secrets scan tests/unit/core/test_egress_source.py tests/security/test_st10_source_client.py`: `"results": {}` (no findings; the `secret:sn`/`secret:sf`/`secret:jira` fixture values are `secret:<name>` *references*, not secret values).

## Deviations from the brief

None substantive. The one judgment call — the source-allowlist asymmetry — was explicitly
directed by the "Sub-controller rulings" section of the brief itself, not something I
introduced; I recorded it in the spec (see "Spec notes" above) as instructed.

## Commit

- Checkpoint commit `wip(T10-33): ...` was attempted but the pre-commit `pytest-unit` hook
  aborted it (fails only on the pre-existing `test_st10_25_repository_passes`, known-red per
  controller-notes-w07.md); no commit was created by that attempt (confirmed via `git log`).
- Final commit `56644e6` — `feat(core): add source_http_client for impl 01 connectors (T10-33)`
  — made with `SKIP=pytest-unit` (never `--no-verify`); every other hook (ruff-check,
  ruff-format, mypy, import-linter, detect-secrets, module-size, type-ownership) passed. The
  unit/security suite was run separately and confirmed green apart from the documented
  known-red case.
- `.secrets.baseline` was regenerated by the `detect-secrets` hook (line-number refresh for the
  `docs/impl/10-config-security-deployment.impl.md` edits) and included in the commit; I
  normalized it back to LF before staging (the hook run under Windows had written CRLF).

## Concerns

- None blocking. The only thing worth a controller's attention: I added a spec-note sentence to
  the U10-110 Algorithm cell rather than leaving it unedited, since the literal text otherwise
  contradicts the (binding) sub-controller ruling on the source allowlist; happy to have that
  reworded if the controller prefers different phrasing.

## Fix round 1 (review: T10-33-review.md, verdict "Needs fixes")

Three findings fixed, one (m3) parked as out of scope per the dispatch.

- **I1 (Important #1)** — `herness/core/_egress_source.py` `check_verify`: a `Path` that is not
  an existing file now raises the same verbatim `ConfigError("TLS verification cannot be
  disabled for source <source>")` as `verify=False`, instead of the previously distinct "TLS CA
  bundle not found for source <source>" message. U10-110's Preconditions row is unconditional
  about the wording for "any other value" than `True` or an existing file, so a missing path is
  not a special case. Updated both existing tests that only asserted `ConfigError` with no
  message match to now assert the message text:
  `tests/unit/core/test_egress_source.py::test_ut10_82_verify_missing_ca_bundle_is_config_error`
  and `tests/security/test_st10_source_client.py::test_st10_59_missing_ca_bundle_is_config_error`.

- **m1** — `SourceHostTransport.handle_request` (`herness/core/egress_clients.py`) now sets
  `request.headers["Accept-Encoding"] = "identity"` on every request, right after the
  allowlist/scheme/userinfo check and before calling the inner transport — the same T10-17
  gzip-bomb-fix behavior `LoopbackOnlyTransport._admit` already has, and the same reasoning:
  forcing the request header does **not** make the check optional, because a source is not
  obligated to honor it. `open_body`'s existing `unsupported_encoding` fail-closed path (already
  wired via `_egress_source.body_error`, previously untested) now has two new tests:
  `test_ut10_82_request_carries_accept_encoding_identity` (asserts the mock route's captured
  request carries `accept-encoding: identity` even though the caller asked for `gzip`) and
  `test_ut10_82_unsupported_encoding_is_blocked` (a response answering `Content-Encoding: br`
  despite that header raises `EgressBlocked(reason="unsupported_encoding")`). Spec note: this
  makes `unsupported_encoding` a fifth practical `EgressBlocked` reason a caller may see from
  `source_http_client`, alongside the four U10-110 lists (`host_not_allowed`, `scheme_not_https`,
  `userinfo_present`, `response_too_large`) — same as loopback clients already do; not adding it
  to the spec's Errors row since it is inherited, undocumented-but-already-present behavior of
  the reused `open_body` helper (T10-17), not new to this card, and the review flagged it as a
  documentation gap rather than a behavior change to make.

- **m2** — Added `test_ut10_82_inclusive_bounds_succeed`
  (`tests/unit/core/test_egress_source.py`): builds a client at every inclusive bound edge
  (`timeout_s=600.0`, `max_connections=64`, `max_response_bytes=1_073_741_824`, and the lower
  edge `timeout_s=1.0`/`max_connections=1`/`max_response_bytes=1`), confirming the `<=`/`>=`
  comparisons in `check_timeout_s`/`check_int` are genuinely inclusive.

- **m3**: parked (not in scope for this round per the dispatch).

### Sizes after fix round 1

`herness/core/egress.py` 385/390 (unchanged), `herness/core/egress_clients.py` 263/300 (+4
lines: the `Accept-Encoding` line plus a docstring sentence), `herness/core/_egress_source.py`
128/200 (+2 lines: `check_verify`'s merged branch is net shorter, offset by its docstring note).

### Fix-round test summary

`tests/unit/core/test_egress_source.py` + `tests/security/test_st10_source_client.py`: 33
passed (30 -> 33: three new test functions; two existing tests strengthened to assert the exact
`ConfigError` message instead of just its type). `tests/unit/core` + `tests/security`: 2218
passed, 1 known-red (`test_st10_25_repository_passes`, pre-existing, unrelated), 2 skipped
(Windows symlink privilege, pre-existing). No full-suite run this round, per the dispatch.

### Gates (fix round 1)

`ruff format --check` clean, `ruff check` clean, `uv run mypy` on the four touched files:
`Success: no issues found in 4 source files`, `uv run lint-imports`: 13 kept / 0 broken,
`uv run python -m tools.check_module_size`: exit 0.
