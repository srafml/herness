# T00-14 report: hosted CI workflow

Status: DONE_WITH_CONCERNS (NEEDS_CONTEXT only on the check_traceability item, per binding ruling 1)
Commit: c00e262 feat(ci): add hosted CI workflow (T00-14), branch worktree-agent-ab0823b1ca9b57912

## What was built
- `.github/workflows/ci.yml` (181 lines, budget 250), U00-59 as specified:
  - Triggers: push/pull_request to main, schedule "17 3 * * *", workflow_dispatch, workflow_call.
  - concurrency group `ci-${{ github.ref }}`, `cancel-in-progress: ${{ github.event_name != 'schedule' }}`.
  - Top-level `permissions: contents: read`; no job widens it. `defaults.run.shell: bash` (gives `-eo pipefail` on both OSes, needed for the detect-secrets pipe).
  - Workflow env: UV_FROZEN "1", PYTHONUTF8 "1", HERNESS_ENV test, OSV_SCANNER_VERSION "2.6.0", OSV_SCANNER_SHA256.
  - Jobs: lint (15 min), types (20), test matrix ubuntu/windows (30; job name `test (${{ matrix.os }})` so the required checks are exactly `test (ubuntu-latest)` / `test (windows-latest)`), audit (20; runs on every event including schedule). lint/types/test have `if: github.event_name != 'schedule'`.
  - setup-uv: version "0.11.8", python-version "3.12", enable-cache true, cache-dependency-glob uv.lock. Every checkout: persist-credentials false.
  - lint: steps in spec order; detect-secrets = `git ls-files -z | grep -zvE '^(uv\.lock|\.secrets\.baseline|tests/fixtures/pii_corpus\.jsonl)$' | xargs -0 uv run detect-secrets-hook --baseline .secrets.baseline`; check_traceability verbatim, no continue-on-error.
  - test: pytest selection and coverage options come from matrix values through `env:` (SELECTION, COVERAGE); eval step guarded by `matrix.os == 'ubuntu-latest' && hashFiles('tests/eval/mock_scripts/**') != ''`; ci-reports artifact, retention 14.
  - audit: mkdir build; uv export; pip-audit (exit 0/1 accepted, >1 fails); osv-scanner download + `sha256sum --check --strict` against env; `osv-scanner scan source -L "requirements.txt:build/requirements.lock.txt" --format json --output-file build/osv.json` (0/1 accepted); check_audit; summary appended to $GITHUB_STEP_SUMMARY (if: always(), only when the file exists); runtime venv via step env UV_PROJECT_ENVIRONMENT=build/runtime-venv; cyclonedx-py; check_licences (enforce, the default); `audit` artifact (build/*.json + build/audit-summary.md, retention 30, if: always()).
- `tests/unit/repo/test_workflows.py`: ST00-06, one function `test_st00_06_workflow_hardening` parametrized over ci.yml and release.yml (release.yml skipped until T00-15 writes it; a missing ci.yml fails). Parses with yaml.safe_load (handles YAML 1.1 `on` -> True). Checks: third-party `uses:` (job and step level) match owner/repo@40-hex and each raw line has a `# vX.Y.Z` tag comment; no pull_request_target; top-level permissions == {contents: read}; no run: contains `${{ github.event.`, `${{ github.head_ref`, `${{ inputs.`; no self-hosted in runs-on/strategy; every actions/checkout has persist-credentials false. For ci.yml also: triggers, cron, branches, env, OSV env shapes, job timeouts, schedule guards, matrix OSes.
- `pyproject.toml` `[tool.herness.licences].approved`: 10 provisional entries (below).

## Pinned SHAs (all resolved live 2026-09-25)
Verified with `git ls-remote https://github.com/<repo> refs/tags/<t> refs/tags/<t>^{}` (all are lightweight tags: no `^{}` line, so the ref SHA is the commit) and `curl -I https://github.com/<repo>/releases/latest` (redirect names the tag). Input names checked against each action.yml at the pinned SHA.
- actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1 (latest release)
- astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7  # v10.2.0 (latest release)
- actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a  # v7.0.1 (latest release)
- osv-scanner v2.6.0 (latest release; tag commit e840a6e8adb14b7777c78e26cfbf6e2abc1d1fc6). OSV_SCANNER_SHA256 = ca69b3d3cd08f889a49dc0a383122f71cc528b83803671df5fd874d97485b108 for osv-scanner_linux_amd64, taken from the release asset osv-scanner_SHA256SUMS; cross-checked by downloading the binary and running sha256sum (match).
Lines carrying 40/64-hex values have `# pragma: allowlist secret` (same convention as T00-12's .pre-commit-config.yaml), so detect-secrets passes without touching .secrets.baseline.

## O-02 (osv-scanner flags on the pinned version)
Ran osv-scanner v2.6.0 (windows_amd64 binary, hash also matched SHA256SUMS) locally: `scan source -L requirements.txt:<lock> --format json --output-file <out>` parsed 237 packages, exit 1 (vulns found), valid JSON. `--output` is deprecated in v2, so `--output-file` is used.

## Licence approvals added (O-03)
All with approved_by = "provisional: T00-14 build (owner sign-off pending, O-03)", approved_on = 2026-09-25. Found by running the audit chain locally (Windows runtime venv, cyclonedx-py, check_licences: 9 LC001 denials) plus a PyPI metadata review of the Linux-only runtime deps (cuda-bindings Apache-2.0, cuda-pathfinder Apache-2.0, jeepney MIT, secretstorage BSD-3-Clause, triton MIT classifier, nvidia-* report-only, cuda-toolkit: no licence metadata, so UNKNOWN).

| package | licence text matched | reason |
|---|---|---|
| cffi | MIT-0 | MIT No Attribution, permissive |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | all parts permissive (bundled code) |
| pillow | MIT-CMU | HPND-family permissive |
| protobuf | UNKNOWN | SBOM has no licence; PyPI says 3-Clause BSD |
| pyahocorasick | UNKNOWN | PyPI says BSD-3-Clause and Public-Domain |
| pydeck | UNKNOWN | PyPI says Apache License 2.0 |
| pyphen | MPL-1.1 | tri-licence GPL-2.0+/LGPL-2.1+/MPL-1.1, MPL-1.1 elected; via weasyprint |
| regex | Apache-2.0 AND CNRI-Python | CNRI-Python permissive |
| transformers | UNKNOWN | PyPI says Apache 2.0 License |
| cuda-toolkit | UNKNOWN | Linux-only NVIDIA metapackage without licence metadata (NVIDIA EULA, like nvidia-*) |

After adding them, check_licences on the local SBOM exits 0.

chardet 5.2.0 (LGPL): pulled in by cyclonedx-bom, so it is dev/CI toolchain only. It is not in the runtime SBOM (built from the --no-dev runtime venv), so no approval entry was needed or added. Flagged for the owner: it runs in CI, never ships.
filelock (Unlicense, named in O-03) was not reported as denied in the SBOM.

## Deviations (with reasons)
1. ci-reports upload runs only on the Ubuntu leg (`if: always() && matrix.os == 'ubuntu-latest'`): only Ubuntu writes coverage/eval files, and upload-artifact v4+ rejects a second artifact with the same name in one run.
2. OSV_SCANNER_VERSION / OSV_SCANNER_SHA256 live in the top-level workflow env (spec: "workflow env values") next to the three §3.8 env values.
3. `mkdir -p build` before uv export (directory must exist); `defaults.run.shell: bash` (pipefail, and Windows uses bash for the `$SELECTION`/`$COVERAGE` pytest step).
4. ST00-06 is one parametrized function rather than several sharing the ID: tools.check_traceability reports TR007 "test id used twice" for repeated IDs, and CI runs that tool.

## Commands and results
- `uv run ruff check .` / `ruff format --check .`: clean. `uv run mypy`: no issues (26 files). `uv run lint-imports`: 8 kept, 0 broken. check_type_ownership exit 0, check_module_size exit 0.
- `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`: 317 passed, 1 skipped (release.yml), 5 deselected, 1 xfailed (IT00-02, pre-existing traceability xfail).
- RED: before ci.yml existed, the test run failed (ci.yml missing). GREEN: 1 passed, 1 skipped.
- Mutation check of ST00-06 on temp copies of ci.yml: tag-ref uses, missing tag comment, pull_request_target, contents: write, `${{ github.head_ref }}` in run, self-hosted runner, persist-credentials true, wrong cron: all caught.
- actionlint 1.7.12 (release zip, SHA-256 checked against its checksums file, run from scratchpad): exit 0, no findings.
- Local audit chain (outputs in scratchpad, not the repo): uv export OK; pip-audit exit 1 (setuptools 81.0.0 GHSA-h35f-9h28-mq5c); osv-scanner exit 1; check_audit exit 0 (AU002 warnings setuptools 6.1, torch 5.3; AU010 skipped torch); cyclonedx-py SBOM OK; check_licences exit 1 before approvals, 0 after.
- `uv run python -m tools.check_traceability`: exit 1, 278 finding lines, all doc debt (TR001/2/4/5 in specs 01-11 plus pre-existing TR007 in tests/unit/core); none from test_workflows.py.
- Commit through the real pre-commit chain (PYTHONUTF8=1, PRE_COMMIT_ALLOW_NO_CONFIG=1): all hooks passed.

## Concerns
- NEEDS_CONTEXT (ruling 1): the lint job runs check_traceability verbatim and will be red until the spec doc debt (and the pre-existing TR007 duplicates in tests/unit/core/test_log_pipeline.py and test_logging.py) is cleaned, so the `lint` required check cannot go green yet.
- O-03 approvals are provisional; owner sign-off pending. They were derived from a Windows runtime SBOM plus PyPI metadata for the Linux-only deps; the Linux SBOM on the runner could differ in details. Check the first real audit run.
- The nightly audit shares the concurrency group `ci-refs/heads/main` with pushes to main: the scheduled run never cancels others, but a push to main during the nightly run cancels it (cancel-in-progress is evaluated for the newcomer). Changing the group would deviate from the spec; left verbatim.
- When release.yml calls ci.yml via workflow_call, T00-15 should give release.yml a concurrency group different from `ci-${{ github.ref }}` to avoid a caller/callee collision.
- Every job runs `uv sync --all-extras` (torch cu128 + CUDA wheels); audit syncs twice. Watch disk and time on the first hosted run (uv cache hardlinks should keep the second sync cheap).
- Local audit found setuptools 81.0.0 (GHSA-h35f-9h28-mq5c, 6.1, fix available) and torch CVE-2025-3000 (5.3): report-only warnings today.
- Acceptance items that need a real GitHub repo (five green checks on a PR, branch protection, artifact contents on the runner) cannot be verified locally. README branch-protection text was not changed in this card.

## Fix round 1

Commit: b208219 fix(ci): review fixes for hosted CI workflow (T00-14)

- Important (README §9): the protected-`main` sentence now names the five required status checks `lint`, `types`, `test (ubuntu-latest)`, `test (windows-latest)` and `audit` of `.github/workflows/ci.yml` (one-sentence edit).
- Minor (untrusted expressions): the exact-text check is replaced by a whitespace-tolerant regex that matches any `${{ ... }}` expression (up to its closing `}}`) that reads `github.event.`/`github.event[`, `github.head_ref` or `inputs.`/`inputs[`. `github.event_name` stays allowed.
- Minor (runners): every non-reusable job's `runs-on` must lie within {ubuntu-latest, windows-latest}. A `${{ matrix.<key> }}` value is expanded from the matrix list and `include` entries. The whole `jobs` tree is still scanned for `self-hosted`. Reusable-workflow jobs (`uses:`, e.g. release.yml's `ci`) are exempt because they have no runner.
- Not changed (parked for owner): the cuda-toolkit/UNKNOWN provisional approvals.

Mutation checks on temp copies of ci.yml, all caught:
- `${{github.event.pull_request.title}}` (no spaces)
- `${{ format('{0}', inputs.x) }}`
- `${{  github . head_ref }}`
- a matrix runner of macos-latest
- a job on ubuntu-22.04

Not flagged, as intended: `${{ github.event_name }}`, and the unmodified file.

Gates: ruff check and format clean; mypy clean; lint-imports 8 kept; check_type_ownership 0; check_module_size 0. `(unit or integration) and not slow`: 317 passed, 1 skipped, 1 xfailed (pre-existing). ST00-06: 1 passed, 1 skipped (release.yml). check_traceability reports no findings from test_workflows.py. The real pre-commit chain passed.
