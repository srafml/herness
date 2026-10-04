# T00-14 review (hosted CI workflow, U00-59 / ST00-06)

Reviewed commit c00e262 (base a063213) in worktree agent-ab0823b1ca9b57912. Read-only.

### Spec Compliance
- ✅ Triggers: push/pull_request to main, schedule "17 3 * * *", workflow_dispatch, workflow_call (ci.yml:8-15).
- ✅ Concurrency group `ci-${{ github.ref }}`, cancel-in-progress false only for schedule (ci.yml:17-19).
- ✅ §3.8: top-level `permissions: contents: read`, no job widens (ci.yml:21-22); every checkout `persist-credentials: false`; hosted runners only; env UV_FROZEN "1", PYTHONUTF8 "1", HERNESS_ENV test (ci.yml:24-27); setup-uv with enable-cache, cache-dependency-glob uv.lock, python "3.12", version "0.11.8" = pyproject required-version (pyproject.toml:84).
- ✅ Untrusted values: no `${{ ... }}` inside any `run:`; matrix values reach pytest via env SELECTION/COVERAGE (ci.yml:106-111).
- ✅ Pins, verified live by me: `git ls-remote` gives checkout v7.0.1 = 3d3c42e5…b1, setup-uv v10.2.0 = c18668ad…c7, upload-artifact v7.0.1 = 043fb46d…0a (all lightweight tags, no `^{}` line); `/releases/latest` redirects to exactly these tags; tag in trailing comment.
- ✅ OSV: v2.6.0 is the latest release; the published osv-scanner_SHA256SUMS line for osv-scanner_linux_amd64 is ca69b3d3…b108, identical to OSV_SCANNER_SHA256 (ci.yml:30). Download with `curl -f`, then `sha256sum --check --strict` under `bash -eo pipefail`: any mismatch or HTTP error fails the step (fail-closed), and chmod comes only after the check (ci.yml:147-152).
- ✅ pip-audit and osv-scanner accept only exit 0/1 (`|| status=$?; [ status -gt 1 ] && exit`), ci.yml:140-145 and 153-158. osv exit 127/128 fail.
- ✅ Job table: lint 15 / types 20 / test 30 matrix / audit 20; lint, types, test guarded `!= 'schedule'`, audit unguarded; step order and commands match U00-59 verbatim, including detect-secrets with the hook-6 exclude regex (ci.yml:54-58), check_traceability with no continue-on-error (ruling), eval step behind hashFiles (ruling), ci-reports retention 14, audit artifact retention 30 with `if: always()`, runtime venv through step-scoped UV_PROJECT_ENVIRONMENT (ci.yml:163-166).
- ✅ Job name `test (${{ matrix.os }})` gives the required check names `test (ubuntu-latest)` and `test (windows-latest)`.
- ✅ ST00-06 (tests/unit/repo/test_workflows.py) covers every clause of its row: 40-hex pins (job- and step-level `uses`, plus the tag comment), no pull_request_target, top-level permissions == {contents: read}, the three forbidden `run:` substrings, no self-hosted (runs-on and strategy), checkout persist-credentials false. It is parametrized over ci.yml and release.yml; release.yml is skipped until T00-15, and a missing ci.yml fails. Job-level `./` reusable-workflow uses (release.yml → ci.yml) are excluded correctly.
- ✅ Test ID and docstring: `test_st00_06_workflow_hardening`, docstring starts "ST00-06", `pytestmark = pytest.mark.unit`. The `-k ST00_06` selection works (I re-ran it: 1 passed, 1 skipped).
- ✅ Licence approvals (O-03): all 10 entries have exactly the 5 keys that tools/check_licences.py:24 requires, with a TOML date. `approved_by` follows the ruling. The licence texts match local dist metadata (cffi MIT-0, numpy expression, pillow MIT-CMU, regex Apache-2.0 AND CNRI-Python are exact License-Expression values; protobuf, pyahocorasick, pydeck and transformers have only free-text `License:`, so UNKNOWN is consistent with the SBOM having no licence). Nothing invented beyond the cuda-toolkit inference, which the report flags.
- ❌ Acceptance check "branch protection on `main` requires the five checks (documented in README)" is not met. README.md §9 (README.md:45-47) says only "the hosted CI checks must pass" and never names `lint`, `types`, `test (ubuntu-latest)`, `test (windows-latest)`, `audit`. U00-63 §9 also requires "required checks of U00-59". The report says README was not changed.
- ⚠️ Cannot verify from diff or locally: five green checks on a real PR (lint will stay red on doc debt per ruling 1); artifact contents on the runner; branch-protection settings; that the Linux runner SBOM yields the same denial set as the Windows SBOM used for the approvals; the concurrency interaction when release.yml calls ci.yml via workflow_call (T00-15 must use a different group); a push to main cancelling an in-flight nightly audit that shares the group `ci-refs/heads/main`. This last one is spec-verbatim; the report flags it.

### Strengths
- The SHAs, the OSV checksum and the latest-release status all check out against upstream. actionlint is clean per the report.
- The shell exit-code handling and the checksum check are correct and fail-closed. Coverage options go through env rather than expression interpolation.
- The test is compact, parses with safe_load, handles the YAML 1.1 `on` → True quirk, and adds a useful ci.yml contract check (triggers, cron, env, timeouts, schedule guards, matrix).
- Deviations are justified. (1) ci-reports is uploaded only from Ubuntu: only that leg writes the files, and upload-artifact v4+ rejects a duplicate artifact name in one run. (2) OSV values sit in the workflow-level env, which is what "workflow env values" means. (3) `mkdir -p build` and `defaults.run.shell: bash` are needed for pipefail and the Windows `$SELECTION` step. (4) The single parametrized function avoids TR007 from check_traceability.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. README.md:45-47: the card's acceptance check requires README to document that branch protection on `main` requires the five checks. §9 does not name them. Fix: add one sentence to README §9 listing `lint`, `types`, `test (ubuntu-latest)`, `test (windows-latest)` and `audit` as the required status checks. This is a one-line doc edit, outside the card's Files row but required by its acceptance row.

#### Minor (Nice to Have)
2. tests/unit/repo/test_workflows.py:20,78-80: the forbidden-expression check is literal-substring. `${{github.event.x}}` (no space) or `${{  inputs.x }}` gets past it. A regex `\$\{\{\s*(github\.event\.|github\.head_ref|inputs\.)` would close this.
3. tests/unit/repo/test_workflows.py:83-86: the runner check only rejects the string "self-hosted". §3.8 is an allowlist (`ubuntu-latest`, `windows-latest`), and a custom label alone can target a self-hosted runner. Consider asserting that runs-on or matrix values are in the allowlist. The ST00-06 row wording ("no self-hosted runner") is met as is.
4. pyproject.toml:110 (cuda-toolkit entry): the reason "NVIDIA EULA like nvidia-*" is an inference, since the package has no licence metadata. Approving `UNKNOWN` also admits any future version with no metadata. That is acceptable under the provisional O-03 ruling, but the owner should confirm it; putting cuda-toolkit in `report_only` next to `nvidia-*` would be the spec-aligned alternative. The same breadth applies to the four other `UNKNOWN` approvals (pyproject.toml:105-107, 109).

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The workflow is spec-verbatim, correctly hardened and fail-closed, and the SHAs and checksum are verified against upstream. The only gap is the acceptance item requiring README to name the five required checks (one-line doc fix). Re-run of `pytest tests/unit/repo -q -p no:logging`: 5 passed, 1 skipped (release.yml).

## Re-review round 1 (c00e262..b208219)

Scope: Important 1 and Minors 2-3 of the first review (the forbidden-expression regex and the runs-on allow-set). Minor 4 (cuda-toolkit and UNKNOWN approvals) is parked for the owner by ruling.

- ✅ Important 1 (README §9): README.md:47 now names the five required status checks exactly: `lint`, `types`, `test (ubuntu-latest)`, `test (windows-latest)`, `audit`. It says branch protection in the repository settings requires them. This meets the T00-14 acceptance row and U00-63 §9.
- ✅ Minor 2 (tests/unit/repo/test_workflows.py:21-23): FORBIDDEN_IN_RUN is now a whitespace-tolerant regex scoped to the inside of `${{ … }}`. I probed it directly.
  - It matches `${{github.event.x}}`, `${{  inputs.x }}`, `${{ github.head_ref }}`, `${{ github . event['x'] }}` and `${{ inputs['a'] }}`.
  - It does not match `${{ github.event_name }}`, `${{ steps.a.outputs.b }}`, `${{ matrix.os }}`, `$GITHUB_HEAD_REF`, or `github.event.x` outside an expression, so there are no false positives on ci.yml.
  - Not a finding: `${{ toJSON(github.event) }}` (the whole event object, with no `.`) is not caught. The §3.8 rule text names `github.event.*`, so this is within the rule's wording.
- ✅ Minor 3 (test_workflows.py:86-109): the test keeps a document-wide "self-hosted" ban and adds a strict allow-set check.
  - `runs-on` must resolve to a subset of {ubuntu-latest, windows-latest}. `${{ matrix.<key> }}` expands through the matrix values and `include` entries, and an empty expansion fails.
  - Anything else fails, including list labels and dict or group forms.
  - Reusable-workflow jobs (`uses:`) are skipped correctly, which matters for release.yml calling ci.yml.
  - Probe: the ci.yml matrix resolves to ['ubuntu-latest', 'windows-latest'], and `['self-hosted', 'linux']` stays outside the set.
- Re-run `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/unit/repo -q -p no:logging`: 5 passed, 1 skipped (release.yml not written yet).

Remaining findings: none (Minor 4 is parked for the owner).

**Task quality:** Approved
