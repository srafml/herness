# Review: T00-15 — release workflow (.github/workflows/release.yml)

Verdict: **Needs fixes**

## Spec compliance (U00-60, impl 00 §3.8, D10-28)

- Trigger `push` tags `v*.*.*` only — Pass (`release.yml:14-16`)
- Jobs `ci`/`build`/`publish`, `needs` — Pass (`ci` no needs; `build` needs `ci`; `publish` needs `build`)
- Permissions per job (only `build` has `id-token: write`) — Pass; top-level `contents: read`, `ci` `contents: read`, `build` `contents: read`+`id-token: write`+`attestations: write`, `publish` `contents: write`. Verified by `test_st00_08_release_workflow` and manual read; no other job widens `id-token`.
- `checkout` (`fetch-depth: 0`, `persist-credentials: false`) then `setup-uv` (`version: "0.11.8"`, `python-version: "3.12"`, cache options) — Pass (`release.yml:48-57`), matches ci.yml's approved pattern and U00-49's `required-version`.
- Tag verification: object type must be `tag` (annotated) else fail; `verification.verified == true` else fail; via `env: TAG`/`GH_TOKEN`, no expression interpolation in the run body — Pass (`release.yml:59-76`). API call shapes (`git/ref/tags/$TAG`, `git/tags/$obj_sha`) match GitHub's real REST endpoints and the brief's literal example.
- Version match vs `pyproject.toml` via `tomllib`, else fail — Pass (`release.yml:77-86`).
- Both verification steps precede `uv build` — Pass, confirmed both by the new unit test and by step order in the file (`uv build` is `release.yml:87`).
- Runtime venv + SBOM "exactly as ci.yml's audit job", writing `dist/herness-<version>.cdx.json` — Pass. Compared line-by-line against `.github/workflows/ci.yml`'s `audit` job: identical `UV_PROJECT_ENVIRONMENT=build/runtime-venv` sync and identical `cyclonedx-py environment build/runtime-venv --output-reproducible --of JSON -o ...` invocation, only the output path differs as required.
- Licence gate in enforce mode — Pass (`--mode enforce` explicit; ci.yml's audit job relies on the tool's `enforce` default — same effective behaviour, confirmed by reading `tools/check_licences.py`'s argparse default).
- DuckDB `excel` extension download for uv.lock's pinned DuckDB version (`1.5.5`, confirmed by reading `uv.lock:664-666`) and `linux_amd64`, into `dist/duckdb/excel.duckdb_extension` + `dist/duckdb/SHA256SUMS` — Pass. URL form `https://extensions.duckdb.org/v<version>/linux_amd64/<name>.duckdb_extension.gz` verified LIVE with `curl -fsSI`: returns `200 OK`, `Content-Type: application/gzip`, so `gunzip -c ... > file` decompresses it correctly. Fail-closed on download error: `curl -fsSL` (`-f` = fail on HTTP error, non-zero exit) combined with the workflow's `defaults.run.shell: bash`, which GitHub Actions resolves to `bash --noprofile --norc -eo pipefail {0}` — any failing command (curl, gunzip, sha256sum) aborts the job. Confirmed by replicating the exact multi-line heredoc-in-`$()` script locally (`bash -n` + execution): parses and runs correctly, prints `1.5.5`.
- `attest-build-provenance` subjects = wheel, sdist, extension — Pass (`release.yml:104-107`, explicit paths, no glob ambiguity).
- `attest-sbom` subjects = wheel + SBOM — Pass (`release.yml:109-111`).
- Upload `dist/*` as artifact `dist`, retention 90 days — Pass (`release.yml:112-116`). Note: `actions/upload-artifact`'s own glob engine (not bash) recurses into a matched directory and preserves structure, so `dist/duckdb/` is correctly included here.
- Publish: download artifact `dist`; `gh release create "$TAG" dist/* --verify-tag --title "$TAG" --notes-file <from tag annotation>` — Fail, see Important finding below (the `dist/*` glob is expanded by bash, not the artifact-glob engine, and bash's `*` does not descend into `dist/duckdb/`).
- §3.8 rules: SHA pins 40-hex with tag comment — Pass, and the three new pins were spot-verified live with `git ls-remote --tags`:
  - `actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8` — matches `v4^{}` deref, which equals the `v4.2.2` tag (latest release). Confirmed.
  - `actions/attest-sbom@c604332985a26aa8cf1bdc465b92731239ec6b9e` — matches `v4^{}` deref, equals `v4.1.0` tag (latest). Confirmed.
  - `actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c` — matches `v8` tag, equals `v8.0.1` (latest). Confirmed.
  - Reused pins (`checkout`, `setup-uv`, `upload-artifact`) are the already-approved T00-14 pins, unchanged.
- Untrusted values only via `env`, `persist-credentials: false`, hosted runners only, no `pull_request_target` — Pass, and also covered generically by the existing parametrized `test_st00_06_workflow_hardening(name="release.yml")`, now exercised for real (previously skipped).
- Bash error handling (`set -eo pipefail` semantics via `defaults.run.shell: bash`) — Pass, same approved pattern as ci.yml, no explicit `set -e` needed per step.
- ST00-08 covers every clause of its row — Pass. `test_st00_08_release_workflow` (`tests/unit/repo/test_workflows.py:183-231`) checks: trigger is tag-push `v*.*.*` only; `build` needs `ci`; `build` has `id-token`/`attestations: write` and no other job does; tag-verify and version-match step indices precede the `uv build` step index; `attest-build-provenance` and `attest-sbom` are used; `subject-path` includes the wheel and sdist. Docstring's first line starts with `ST00-08`. Function name follows the `test_st00_08_...` convention (global constraints). One function, not split per clause — avoids the TR007 duplicate-test-id trap as the report claims.
- `≤150` lines — Pass, file is 142 lines (diff `@@ -0,0 +1,142`).
- Re-ran `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/unit/repo -q -p no:logging` in the worktree: 7 passed, no failures/warnings.

## Findings

### Critical
None.

### Important

1. `gh release create "$TAG" dist/* ...` (release.yml:141) will not attach the DuckDB extension or its SHA256SUMS as release assets, because bash's `dist/*` glob does not descend into `dist/duckdb/`. `mkdir -p dist/duckdb` (release.yml:97) plus the extension/SHA256SUMS writes (release.yml:100-101) put those two files one level below `dist/`. In the publish job's `run:` step, `dist/*` is expanded by bash (not the `@actions/*` glob engine used by `upload-artifact`/`attest-*`, which does recurse into matched directories) — plain bash pathname expansion only lists the immediate children of `dist/`, so the argument list `gh` receives includes the literal directory `dist/duckdb` (not its contents) alongside the wheel/sdist/SBOM. `gh release create` expects file arguments; passing it a directory will either error (most likely, since gh opens each argument to read/upload its bytes — reading a directory as a file errors on Linux) and fail the whole publish job every release, or at best silently drop it, meaning the extension and its checksum are never actually published to the GitHub release. Since the brief's own Purpose field states these files (wheel, SBOM, "the DuckDB excel extension with its SHA256SUMS", attestations) are the inputs of T10-26 (`herness deploy install`), this breaks the downstream consumer if it relies on the GitHub release rather than the build-job artifact.
   This is plan-mandated: the brief's U00-60 row specifies both the nested `dist/duckdb/excel.duckdb_extension` path and the literal `gh release create "$TAG" dist/* ...` command verbatim, and the implementer followed both literally — the conflict is inherent in the brief, not an implementation choice. Flagging for controller attention; a fix needs either a recursive/explicit asset list (e.g. `dist/*.whl dist/*.tar.gz dist/*.cdx.json dist/duckdb/*`, or `shopt -s globstar` with a `dist/**` type pattern and directory filtering) or a brief correction.

### Minor

1. Duplicate version lookup. The project version is read from `pyproject.toml` via a fresh `uv run python -c "import tomllib; ..."` subprocess twice — once in the version-match step (release.yml:81) and again in the SBOM/licence-gate step (release.yml:94) to name the SBOM file. Not incorrect, just a small DRY/perf nit; could be computed once and passed via `$GITHUB_ENV` or a step output.
2. The `ci` job's explicit `permissions: contents: read` (release.yml:35-36) is redundant with the top-level default. Harmless (and matches the brief's literal table), just noted for completeness.
3. `concurrency: group: release-${{ github.ref }}` (release.yml:18-19) is not called for anywhere in impl 00 §3.8's rules for the release workflow (only ci.yml's concurrency group is specified there); it's a reasonable extra safety measure (prevents overlapping releases) but is an unrequested addition — the report's "per the card" phrasing overstates the spec basis for it.

## Quality

- Step naming, ordering and comments are clear and mirror ci.yml's established conventions (same pin format, same `defaults.run.shell: bash`, same env block).
- Error handling in the hand-written checks (tag verification, version match, DuckDB download) explicitly builds a message, echoes to stderr, and exits 1 — clean, matches ENG's message-before-raise style used elsewhere and fails closed.
- The regenerate-notes-in-publish deviation (report's stated reason: `RUNNER_TEMP` does not survive across jobs/runners) is sound engineering and does not violate the "generated from the tag annotation" requirement — it re-reads the tag's message from the GitHub API, still via `env`/no interpolation.
- Test coverage of ST00-08 is thorough and precisely scoped to the row's clauses; no assertions-that-assert-nothing.

## Assessment

**Task quality:** Needs fixes.
**Reasoning:** The workflow otherwise matches U00-60 and impl 00 §3.8 verbatim (trigger, jobs, permissions, step order, attestations, pins verified live), and the new test is sound, but the publish job's asset upload as written will not correctly attach the DuckDB extension and its SHA256SUMS to the GitHub release, undermining the stated purpose of shipping those files as release inputs for T10-26 — a functional defect that needs a fix or an explicit brief correction before this can be trusted to produce a usable release.

---

## Re-review round 1

Scope: fix commit `ed755d6` (diff `T00-15-fix1.diff`, base `54e578b`). Checked only the three items the sub-controller called out.

- Important (`gh release create dist/*`) — Fixed. `release.yml:141-142` now runs `gh release create "$TAG" dist/*.whl dist/*.tar.gz dist/*.cdx.json dist/duckdb/* --repo "$GITHUB_REPOSITORY" --verify-tag --title "$TAG" --notes-file "$RUNNER_TEMP/tag-notes.md"`. `dist/duckdb/*` is a bash glob that expands to the two files directly (`excel.duckdb_extension`, `SHA256SUMS`), not the directory itself, so `gh` now receives explicit file paths for every release asset — the original defect (a bare `dist/*` passing the `dist/duckdb` directory as a file argument) is gone. `actions/download-artifact` (build → publish, `path: dist`) restores the nested `dist/duckdb/` structure before this step runs, so the glob has something to match. A one-line comment at `release.yml:139-140` explains the reasoning. `test_st00_08_release_workflow` (`tests/unit/repo/test_workflows.py:186-188, 203-211`) now also asserts the `gh release create` run text contains `dist/duckdb/*`, `dist/*.whl`, `dist/*.tar.gz` and `dist/*.cdx.json`, and the docstring documents the fix. Confirmed this deviation from the literal `dist/*` was accepted by the sub-controller ruling, as stated in the re-review request.
- Minor 1 (duplicate version lookup) — Fixed correctly and safely. The version is now read once in the "check the tag matches the project version" step (`release.yml:79`), and only after the `$TAG != v$version` guard passes is it exported with `echo "PROJECT_VERSION=$version" >> "$GITHUB_ENV"` (`release.yml:80`). `$version` is a `pyproject.toml` semver string (digits/dots only, sourced from `tomllib`, not from any untrusted event input), so the plain (non-heredoc) `$GITHUB_ENV` append form is safe — no newline/quote injection risk. The SBOM/licence-gate step (`release.yml:87-88`) now consumes `$PROJECT_VERSION` for both the `cyclonedx-py -o` path and the `check_licences --sbom` argument, dropping the second `tomllib` subprocess entirely. Behaviourally identical to before, one fewer subprocess.
- Minor 3 (report wording) — Fixed. `T00-15-report.md:22` now reads "Correction after review round 1: impl 00 section 3.8 only specifies a concurrency group for ci.yml; the release.yml group is an implementer addition (not spec-mandated) ... the original wording here overstated the spec basis for it", with a matching correction note further down the report (lines ~132-135). No longer claims "per the card".
- Minor 2 (redundant `ci` job `permissions: contents: read`) — left as-is per the ruling; not re-checked further, no regression.
- Re-ran `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/unit/repo -q -p no:logging` in the worktree: 7 passed, no failures.
- File size: 144 lines, still within the 150-line budget.

**Verdict: Approved**
