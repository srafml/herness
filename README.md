# Herness

## 1. Herness

Herness analyses IT service-management, change, monitoring and delivery data and produces evidence-backed answers to two questions: what to fund next, and which groups should improve and how. It follows six principles ([design 00 §2](docs/specs/00-overview-and-contracts.md#2-principles-binding-on-every-component)): numbers come from SQL; every number is traceable; a deterministic core with pluggable edges; idempotent and resumable jobs; local by default; small, plain Python.

## 2. Status

Phase 1 (foundation) in progress. Open decisions: [docs/specs/open-questions.md](docs/specs/open-questions.md).

## 3. Requirements

- Windows 11 Pro with WSL2 (Linux supported).
- Python 3.12 through `uv` (the version pinned in `pyproject.toml`, `[tool.uv] required-version`).
- An NVIDIA GPU with 24 GB or more for Phases 3–4 (design 10).

## 4. Quick start (development)

```bash
uv sync --frozen                 # installs herness in editable mode (R-58)
uv run pre-commit install
export HERNESS_ENV=dev           # PowerShell: $env:HERNESS_ENV = "dev"
cp .env.example .env             # optional
uv run pytest -m unit
uv run python tools/synth_data.py --seed 7 --scale tiny
uv run herness config validate --profile synth
```

## 5. Repository layout

See [design 00 §3](docs/specs/00-overview-and-contracts.md#3-runtime-and-repository-layout) and the module map (§2) of each implementation spec in [docs/impl/](docs/impl/).

## 6. Configuration and secrets

See [design 10](docs/specs/10-config-security-deployment.md). Secrets live in Windows Credential Manager (`herness secrets set`). Never put secrets in files.

## 7. Development workflow

Work is organised as task cards in the implementation specs. A card is done when it meets ENG §8 (definition of done) and passes the quality gates of ENG §7. Run the checks locally with `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run lint-imports`, `uv run python -m tools.check_type_ownership` and `uv run pytest -m unit`.

## 8. Testing

Markers and selections are defined in design 11 §4.1–4.2: `unit`, `integration`, `fault`, `eval`, `gpu`, `slow`. Commit gate: `pytest -m unit`. Push gate: `pytest -m "(unit or integration or fault) and not gpu and not slow"`.

## 9. CI and releases

The `main` branch is protected: the hosted CI checks must pass, and releases are cut from signed annotated tags `vX.Y.Z`. A release produces the wheel, the sdist, a CycloneDX SBOM and signed build provenance. Verify an attestation with `gh attestation verify <wheel> --repo <owner>/<repo>`. The target box installs only the release wheel, through `herness deploy install`, which runs that verification and the SBOM check first (R-58, design 10).

## 10. Security

Report vulnerabilities internally to the project owner; do not open public issues. Engineering security rules: [ENG §5](docs/impl/ENG-STANDARDS.md).

## 11. Documentation index

- [docs/architecture.html](docs/architecture.html)
- [docs/specs/](docs/specs/): design specs
- [docs/impl/](docs/impl/): implementation specs, decisions and engineering standards

## 12. Licence

Proprietary. Internal use only.
