# Report: T00-15 -- release workflow (.github/workflows/release.yml)

Worktree: D:\herness\.claude\worktrees\agent-ab0823b1ca9b57912, branch worktree-agent-ab0823b1ca9b57912.
Commit: 54e578b feat(ci): add release workflow (T00-15).

## What was built

- .github/workflows/release.yml (142 lines, budget <=150). Trigger: push tags v*.*.* only. Jobs:
  - ci (needs nothing, permissions: contents: read): uses ./.github/workflows/ci.yml -- reuses the whole reusable CI workflow (lint, types, test x2, audit) on the tagged commit.
  - build (needs ci; permissions: contents: read, id-token: write, attestations: write):
    checkout (fetch-depth: 0, persist-credentials: false) -> setup-uv -> uv sync --all-extras ->
    verify the tag is annotated and its signature is verified (gh api .../git/ref/tags/$TAG --jq .object.type, then .../git/tags/$obj_sha --jq .verification.verified, both via env: TAG / GH_TOKEN, no expression interpolation in the run body) ->
    check $TAG equals v plus pyproject.toml [project] version via tomllib ->
    uv build ->
    runtime venv and SBOM exactly as the ci.yml audit job (UV_PROJECT_ENVIRONMENT=build/runtime-venv, uv sync --no-dev --all-extras --no-install-project, cyclonedx-py environment build/runtime-venv ... -o dist/herness-$version.cdx.json) ->
    uv run python -m tools.check_licences --sbom dist/herness-$version.cdx.json --mode enforce ->
    download DuckDB excel extension for the version pinned in uv.lock (read live via tomllib, not hardcoded -- currently resolves to 1.5.5), platform linux_amd64, from https://extensions.duckdb.org/v$duckdb_version/linux_amd64/excel.duckdb_extension.gz, gunzip to dist/duckdb/excel.duckdb_extension, write dist/duckdb/SHA256SUMS ->
    actions/attest-build-provenance with subject-path = wheel + sdist + the duckdb extension ->
    actions/attest-sbom with subject-path = wheel, sbom-path = the SBOM ->
    actions/upload-artifact (name: dist, path: dist/*, retention-days: 90).
  - publish (needs build; permissions: contents: write): actions/download-artifact (name: dist, path: dist) -> regenerate the notes file from the tag annotation in this job (gh api .../git/tags/<sha> --jq .message > $RUNNER_TEMP/tag-notes.md, since the build job RUNNER_TEMP does not survive to a new runner) -> gh release create "$TAG" dist/* --repo "$GITHUB_REPOSITORY" --verify-tag --title "$TAG" --notes-file "$RUNNER_TEMP/tag-notes.md".
- Top-level permissions: contents: read; env: UV_FROZEN, PYTHONUTF8, HERNESS_ENV per section 3.8; concurrency: group release-${{ github.ref }} (no cancel-in-progress). Correction after review round 1: impl 00 section 3.8 only specifies a concurrency group for ci.yml; the release.yml group is an implementer addition (not spec-mandated) to avoid one release run clashing with another on the same ref -- the original wording here overstated the spec basis for it.
- Extended tests/unit/repo/test_workflows.py: added a RELEASE path constant and one new test function test_st00_08_release_workflow (a single function, not split per-clause, to avoid a TR007 duplicate-test-id finding -- the existing test_st00_06_workflow_hardening already covers pinning, top-level permissions, untrusted-input and runners/checkout for release.yml through its WORKFLOW_NAMES parametrization). The new function asserts every ST00-08 clause: trigger is tag-push v*.*.* only; build needs ci; build has id-token: write and attestations: write and no other job does; build calls attest-build-provenance with the wheel and sdist in subject-path, and calls attest-sbom; the tag-verification and version-match steps precede the uv build step (checked by locating step indices via the run text, not brittle whole-file string matching).

## New pinned SHAs (resolved live, tag in trailing comment)

Resolved with git ls-remote --tags on each action repo, dereferencing the ^{} annotated-tag pointer and confirming it equals the listed SHA of the latest release tag (all three below turned out to be lightweight tags, so the tags own listed SHA already is the commit SHA):

| Action | Tag | SHA | Verification |
|---|---|---|---|
| actions/attest-build-provenance | v4.2.2 | 4d101475d8b20a2381f78447822ac1eab6504dd8 | git ls-remote --tags https://github.com/actions/attest-build-provenance; refs/tags/v4^{} dereferences to the same SHA as refs/tags/v4.2.2 (the latest release) |
| actions/attest-sbom | v4.1.0 | c604332985a26aa8cf1bdc465b92731239ec6b9e | same repo check; v4^{} dereferences to the same SHA as v4.1.0 (latest) |
| actions/download-artifact | v8.0.1 | 3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c | same check; v8^{} dereferences to the same SHA as v8.0.1 (latest) |

Reused pins (verbatim from ci.yml, T00-14): actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 (v7.0.1), astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 (v10.2.0), actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a (v7.0.1). Every third-party uses: line carries the same "# pragma: allowlist secret" trailing comment as ci.yml, so detect-secrets high-entropy scanning does not flag the hex SHAs.

## DuckDB extension URL form

Impl 10 D10-28 does not itself spell out the exact download URL -- it only says release CI adds the extension file from "the official extension repository" (impl 00 T00-15). I used the DuckDB project documented public extension-repository layout: https://extensions.duckdb.org/v<duckdb_version>/linux_amd64/<name>.duckdb_extension.gz. The DuckDB version is read live from uv.lock via tomllib (locating the package entry named duckdb), not hardcoded -- it currently resolves to 1.5.5. This is a deviation-by-necessity (the exact template was not itself present anywhere I could find in this repo docs tree, beyond the D10-28 pointer to this task) rather than an invented SHA; flagged again below for controller attention.

## Deviations from the brief / notes

- The --notes-file content is regenerated inside the publish job rather than carried over from build, because build and publish run on separate hosted runners and RUNNER_TEMP (or any other job-local file) does not survive between jobs without an artifact upload. Regenerating via gh api .../git/tags/<sha> --jq .message inside publish (still env-only, no expression interpolation inside the run body) satisfies "generated from the tag annotation, passed through env/files, not interpolated" without adding a second cross-job artifact just to carry one text file.
- gh release create is called with an explicit --repo "$GITHUB_REPOSITORY" in publish since that job has no actions/checkout step (not needed there -- it only downloads the dist artifact and calls the GitHub API/CLI) and gh needs repo context to run without a local git checkout.
- Added the ci: job permissions: contents: read explicitly (redundant with the workflow-level default) to match the brief table literally.

## Commands and results

- uv run ruff check .: All checks passed.
- uv run ruff format --check .: 60 files already formatted.
- uv run mypy: Success, no issues found in 26 source files.
- uv run lint-imports: Contracts: 8 kept, 0 broken.
- uv run python -m tools.check_type_ownership: exit 0 (info-only pending-owner lines for specs 05/06/07/09, pre-existing, unrelated).
- uv run python -m tools.check_module_size: exit 0, no output (.github/workflows/*.yml is outside the tool scanned code roots herness/app/tools, so the 150-line budget on release.yml is not machine-enforced -- checked manually, 142 lines).
- uv run pytest tests/unit/repo/test_workflows.py -q: 3 passed (the two parametrized ST00-06 cases for ci.yml and release.yml, plus the new ST00-08 test). Before release.yml existed, the same command showed 1 passed / 1 skipped ("release.yml not written yet"), confirming RED before, GREEN after.
- PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging: 319 passed, 5 deselected, 1 xfailed (pre-existing IT00-02 traceability xfail). T00-14 report recorded 317 passed / 1 skipped before this card; the two extra passes are the new/changed ST00-08 test plus the former ST00-06 skip on release.yml turning into a pass.
- uv run python -m tools.check_traceability: exit 1 (pre-existing doc debt across specs 01-11 and older test files, TR001/2/4/5/7). Filtered the output for test_workflows.py and TR007: no findings reference the new file or function -- the duplicate-test-id trap the brief warned about was avoided by writing one function for ST00-08 instead of several.
- actionlint 1.7.12: downloaded actionlint_1.7.12_windows_amd64.zip and its checksums file from the GitHub release into the session scratchpad (outside the repo), verified SHA-256 against the published checksums file, ran actionlint.exe against release.yml -- exit 0, no findings.
- bash -n on every extracted run: script body (via a small PyYAML-driven check written to the scratchpad) confirmed all 9 shell blocks parse cleanly, including the multi-line python -c heredoc used to read the DuckDB version out of uv.lock.
- git commit through the real pre-commit chain (PYTHONUTF8=1 PRE_COMMIT_ALLOW_NO_CONFIG=1, no --no-verify): every hook passed -- check-merge-conflicts, check-yaml, end-of-files, trailing-whitespace, mixed-line-ending, detect-private-key, added-large-files, ruff-check, ruff-format, mypy, import-linter, detect-secrets, module-size, pytest-unit.

## Line counts vs budgets

- .github/workflows/release.yml: 142 lines (budget <=150).
- tests/unit/repo/test_workflows.py: grew by 52 lines (module-size does not gate tests/, no budget applies).

## Concerns

1. The DuckDB extension-repository URL form (https://extensions.duckdb.org/v<version>/linux_amd64/<name>.duckdb_extension.gz) is the DuckDB project documented public layout, but I could not find it spelled out verbatim anywhere in this repo docs/impl tree (D10-28 only says release CI adds the file; it does not give the URL template itself). If the controller holds a different internal reference for the exact form, this is the line to check first.
2. check_module_size does not scan .github/workflows/*.yml (its code roots are herness, app, tools), so the 150-line budget on this file is not machine-enforced going forward -- verified manually now (142 lines) but nothing will fail CI if it grows past 150 later.
3. No live tag push was exercised against a real GitHub repository (no Actions run, no live gh credentials in this environment), so verification is static: YAML parses, bash -n on every script body, actionlint clean, and the new unit test checks topology, permissions, step ordering and attestation wiring. The gh api / gh release create command shapes match the brief table literally but were not run against a live repository end to end.

## Fix round 1 (review response)

Review: D:\herness\.superpowers\sdd\program\briefs\T00-15-review.md (verdict: Needs fixes).
Fix commit: ed755d6 fix(ci): review fixes for release workflow (T00-15).

### Important -- fixed

release.yml:141 (old) -- gh release create "$TAG" dist/* ... passed dist/* to gh, which bash
expands as a plain pathname glob (not the actions/*-style glob engine that upload-artifact and
attest-* use). Bash's dist/* lists only dist's immediate children, so the argument list included
the literal directory dist/duckdb instead of its contents -- dropping excel.duckdb_extension and
SHA256SUMS from the GitHub release (the T10-26 inputs the brief itself names as the purpose of
shipping them). Fixed by listing the release assets explicitly:
  gh release create "$TAG" dist/*.whl dist/*.tar.gz dist/*.cdx.json dist/duckdb/* \
    --repo "$GITHUB_REPOSITORY" --verify-tag --title "$TAG" --notes-file "$RUNNER_TEMP/tag-notes.md"
--verify-tag, --title and --notes-file are unchanged. Added a one-line comment above the step
explaining why dist/* alone is not used.

Confirmed the download-artifact step restores the duckdb/ subdirectory: the build jobs
upload-artifact step uploads with path: dist/* and name: dist; per GitHub's documented behaviour,
upload-artifact resolves path with its own glob engine (which, unlike bash, recurses into and
preserves the structure of a matched directory), and download-artifact with name: dist,
path: dist recreates the uploaded tree verbatim under dist/, so dist/duckdb/excel.duckdb_extension
and dist/duckdb/SHA256SUMS both land back under dist/duckdb/ in the publish job before the
gh release create step runs. (No live Actions run was available to exercise this end to end in
this environment; this is standard, documented upload-artifact/download-artifact behaviour, also
noted independently by the reviewer.)

Extended ST00-08 (same test_st00_08_release_workflow function, no new function/ID) with a cheap
static assertion: locates the publish jobs run step containing "gh release create" and asserts
its text contains dist/*.whl, dist/*.tar.gz, dist/*.cdx.json and dist/duckdb/* -- so a future
regression back to a bare dist/* glob fails this test. Updated the functions docstring to name
this clause.

Deviation note for the record: the brief U00-60 row specifies both the nested
dist/duckdb/excel.duckdb_extension output path and the literal
gh release create "$TAG" dist/* ... command verbatim; those two are in tension (bash's dist/*
cannot both name a directory and be a file list), so this fix deviates from the brief's literal
publish command text while keeping its --verify-tag/--title/--notes-file shape, in order to
satisfy the brief's own stated purpose (shipping the extension and its SHA256SUMS as release
assets for T10-26).

### Minor 1 -- fixed

release.yml previously read the project version from pyproject.toml via a fresh
uv run python -c "import tomllib; ..." subprocess twice (once in the version-match step, again
in the SBOM-naming step). Fixed: the version-match step now also writes
echo "PROJECT_VERSION=$version" >> "$GITHUB_ENV" once it has confirmed the tag matches; the SBOM
step reads $PROJECT_VERSION directly (GITHUB_ENV writes are automatically exported to every later
step in the same job, no explicit env: mapping needed) instead of recomputing it. One tomllib
subprocess instead of two.

### Minor 2 -- left as is (per sub-controller ruling)

The ci jobs explicit permissions: contents: read stays; it matches the brief's table literally
and is harmless.

### Minor 3 -- report wording corrected

Corrected the "What was built" section above: the release.yml concurrency group
(release-${{ github.ref }}) is an implementer addition to avoid overlapping release runs on the
same ref, not something impl 00 section 3.8 mandates for release.yml (section 3.8 only specifies
a concurrency group for ci.yml). The earlier "per the card" phrasing overstated the spec basis;
replaced in place above.

### Commands and results (fix round 1)

- uv run ruff check .: All checks passed.
- uv run ruff format --check .: 60 files already formatted.
- uv run mypy: Success, no issues found in 26 source files.
- uv run lint-imports: Contracts: 8 kept, 0 broken.
- uv run python -m tools.check_type_ownership: exit 0.
- uv run python -m tools.check_module_size: exit 0 (release.yml now 144 lines, still under the
  150-line budget; still not machine-enforced for .github/workflows/*.yml, see concern 2 above).
- uv run pytest tests/unit/repo/test_workflows.py -q: 3 passed (including the extended
  ST00-08 test with the new dist/duckdb/* assertion).
- PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging:
  319 passed, 5 deselected, 1 xfailed (unchanged from before the fix; no test count regression).
- bash -n on every extracted run: script body: all 9 shell blocks parse cleanly (unchanged count;
  the fixed publish step and the SBOM step are both still syntactically valid).
- actionlint 1.7.12 (same scratchpad binary, previously SHA-256 verified against its release
  checksums file): exit 0, no findings, against the fixed release.yml.
- git commit through the real pre-commit chain (PYTHONUTF8=1 PRE_COMMIT_ALLOW_NO_CONFIG=1,
  no --no-verify): every hook passed. Commit ed755d6.

### Remaining concerns (unchanged from round 1, not addressed by this fix round)

1. The DuckDB extension-repository URL form is still not spelled out verbatim in this repos
   docs/impl tree beyond the D10-28 pointer to this task; unchanged from round 1.
2. check_module_size still does not scan .github/workflows/*.yml, so the (now 144-line) budget
   is still not machine-enforced going forward.
3. Still no live tag push against a real GitHub repository was available in this environment;
   verification of the fixed publish command remains static (YAML parse, bash -n, actionlint,
   the extended unit test) plus the documented upload-artifact/download-artifact behaviour cited
   above, not an actual end-to-end release run.
