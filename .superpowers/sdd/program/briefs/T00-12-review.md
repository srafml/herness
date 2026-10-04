# Review: T00-12 -- Pre-commit hooks and secrets baseline

## Spec compliance (U00-54, verbatim table)

- ✅ Top-level keys: `minimum_pre_commit_version: "3.2.0"`, `default_install_hook_types: [pre-commit, pre-push]`, `default_stages: [pre-commit]`, `fail_fast: false` (.pre-commit-config.yaml:1-4).
- ✅ Hook group 1 (remote `pre-commit/pre-commit-hooks`): `check-merge-conflict`, `check-toml`, `check-yaml`, `end-of-file-fixer`, `trailing-whitespace` (`--markdown-linebreak-ext=md`), `mixed-line-ending` (`--fix=lf`), `detect-private-key`, `check-added-large-files` (`--maxkb=1024`, `exclude: ^tests/fixtures/`) -- all present, ids and args verbatim (.pre-commit-config.yaml:13-28).
- ✅ `rev` is the full 40-character commit SHA with the release tag as a trailing comment: `3e8a8703264a2f4a69428a0aa4dcb512790b2c8c  # v6.0.0` (.pre-commit-config.yaml:8). Verified against network: `git ls-remote --tags https://github.com/pre-commit/pre-commit-hooks` confirms this SHA is exactly `refs/tags/v6.0.0`, and it is the newest tag in the list (no tag after v6.0.0). Length confirmed 40 chars.
- ✅ Hooks 2-11 (local, `language: system`, `uv run --frozen`): ruff-check, ruff-format, mypy, import-linter, detect-secrets, fixtures-pii-scan, module-size, type-ownership, pytest-unit, pytest-cpu -- every id, entry command, `files`/`exclude` pattern, `pass_filenames`, `always_run` and `stages` value matches the U00-54 table exactly (.pre-commit-config.yaml:30-101). Row 6 exclude pattern `^(uv\.lock|\.secrets\.baseline|tests/fixtures/pii_corpus\.jsonl)$` matches verbatim. Row 11 correctly overrides the pre-commit default stage with `stages: [pre-push]`; row 10 correctly relies on the file-level `default_stages: [pre-commit]`.
- ✅ Hook 7 (`fixtures-pii-scan`) calls `herness.core.redact --scan tests/fixtures`, which does not exist yet (lands with T10-11) -- this is the accepted soft edge per the dispatch instructions; not flagged as a defect.
- ✅ `.secrets.baseline`: generated via the spec's exact command (`detect-secrets scan --exclude-files '^(uv\.lock|tests/fixtures/pii_corpus\.jsonl)$'`); the `regex.should_exclude_file` filter in the baseline matches this pattern verbatim (.secrets.baseline:232-236). 9 files / 20 entries, every entry has `"is_secret": false` (fully audited, 0 unaudited), consistent with the report's claim. Spot-checked several flagged lines (docs/impl/00-foundation.impl.md:1443/1966, tests/unit/connectors/test_settings_base_models.py:204) -- all are documentation prose or synthetic test placeholders (`synthetic-pw`), not real secrets.
- ✅ IT00-01 and ST00-12 present in `tests/integration/repo/test_pre_commit_hooks.py`, each test function's docstring first line starts with its ID (`"""IT00-01 ..."""`, `"""ST00-12 ..."""`), function names contain the ID with `_` (`test_it00_01_...`, `test_st00_12_...`), module sets `pytestmark = pytest.mark.integration` (test_pre_commit_hooks.py:1,439,452,482).
- ✅ ST00-12 builds both fake secrets at runtime: the private-key header is concatenated from string fragments (`_fake_private_key_header`, test_pre_commit_hooks.py:468-471) and the AWS-style key suffix is generated with `random.choices` per-run (`_fake_aws_key`, test_pre_commit_hooks.py:474-478) -- no literal matching secret pattern appears contiguously in source.
- ✅ Re-ran the specified command: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/integration/repo/test_pre_commit_hooks.py -v -p no:logging` -- both tests pass (2 passed in 8.43s), confirming the report's test-evidence claims (including that the real `pre-commit run --all-files` chain, which exercises module-size on the whole repo, now exits 0).
- ✅ Report's stated deviation (removing the IT00-01 xfail/module-size-tolerance machinery added by a prior agent, once the upstream module-size debt was fixed by ef7321b) is justified: it restores the test to the literal spec row (Action: `pre-commit run --all-files`; Expected: exit 0), and the re-run above confirms it now genuinely passes without needing the tolerance.

## ⚠️ Notes (not defects)

- ⚠️ The pinned `rev` line carries an extra trailing `# pragma: allowlist secret` comment beyond the tag comment the spec calls for (.pre-commit-config.yaml:8). Not a verbatim spec text, but functionally necessary (a bare 40-hex-char string trips detect-secrets' `HexHighEntropyString` plugin) and does not change any of the required field values. Not counted as a deviation.
- ⚠️ Ruff-checking `.secrets.baseline` directly (outside the hook's own `\.py$` file scoping) produces spurious `F821` noise because ruff treats the passed file as Python when invoked ad hoc; this is an artifact of how I invoked ruff for a manual check, not a real lint issue -- the pre-commit hook itself only ever targets `\.py$` files, so `.secrets.baseline` is never passed to ruff in practice. Confirmed `ruff check tests/integration/repo/test_pre_commit_hooks.py` alone reports "All checks passed!".

## Findings

### Critical
None.

### Important
None.

### Minor
None.

## Verdict

Approved
