"""ST09-19 (TH09-20): secret values never reach CLI output, JSON envelopes or logs (T09-24).

Sentinel values sit in the (fake) keyring for every secret the config references and one more
is typed at the hidden `secrets set` prompt. The real admin commands then run through
``herness.cli.main`` as an admin in an elevated shell: `config show` (human and JSON),
`config validate`, `config hash`, `secrets status`, `secrets set`, `deploy render` (its WSL
step faked: the env file content, which must hold the two deploy secrets, is captured there
and never printed). Stdout, stderr, the JSON envelopes and every file under ``data/logs``
are searched for the sentinels; the run is shown to be non-vacuous.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from tests.support.cli_env import CliEnv
from tests.support.config_tree import write_checked_config
from tests.support.fake_keyring import MemoryKeyring

from herness import cli
from herness._cli import identity
from herness.admin import wsl

pytestmark = pytest.mark.unit

# Sentinels: distinct, searchable, never a value any code would print by itself.
SENTINELS = {
    "redact.hmac_key": "5e" * 32,
    "vllm.api_key": "St0919-vllm-sentinel-value",  # pragma: allowlist secret - sentinel
    "anthropic.api_key": "St0919-anthropic-sentinel-value",  # pragma: allowlist secret - sentinel
    "OPENJEV_API_KEY": "St0919-openjev-sentinel-value",  # pragma: allowlist secret - sentinel
    "TYPESAFE_API_KEY": "St0919-typesafe-sentinel-value",  # pragma: allowlist secret - sentinel
}
TYPED = "St0919-typed-at-the-prompt-value"  # pragma: allowlist secret - test sentinel
ADMIN = "root"


def _run(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert isinstance(exc.value.code, int)
    return exc.value.code


def _config(env: CliEnv) -> Path:
    """The pinned config tree (deploy render needs pins) with `root` as the admin."""
    cfg = write_checked_config(env.root)
    path = cfg / "herness.yaml"
    roles = f"  ui: {{roles: {{admins: [{ADMIN}], reviewers: [], default_role: viewer}}}}\n"
    text = path.read_text(encoding="utf-8").replace("security:\n", "security:\n" + roles, 1)
    path.write_text(text, encoding="utf-8")
    return cfg


def test_st09_19_sentinels_absent_from_cli_output_json_and_logs(
    cli_env: CliEnv, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """ST09-19 sentinel secrets configured; config show, config validate, config hash,
    secrets status / set, deploy render (human and JSON) and logs: no sentinel anywhere."""
    for name, value in SENTINELS.items():
        fake_keyring.store[("herness", name.lower())] = value  # names are stored lower-case
    cfg = _config(cli_env)
    cli_env.user = ADMIN
    monkeypatch.setattr(identity, "_is_elevated", lambda: True)
    monkeypatch.setattr(typer, "prompt", lambda text, **kw: TYPED)
    rendered: list[bytes] = []

    def run_wsl(distro: str, argv: list[str], **kw: Any) -> SimpleNamespace:
        rendered.append(kw["stdin"])
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(wsl, "run_wsl", run_wsl)
    outputs: list[str] = []  # stdout per command (one JSON object with --json)
    errors: list[str] = []
    commands = (
        ["config", "show"], ["--json", "config", "show"], ["--json", "config", "validate"],
        ["config", "validate", "--offline"], ["--json", "config", "hash"],
        ["--json", "secrets", "status"], ["secrets", "status"], ["secrets", "set", "snow.token"],
        ["--json", "secrets", "set", "snow.token"], ["--json", "deploy", "render"],
        ["deploy", "render"], ["--verbose", "secrets", "set", "bad name!"],
    )  # fmt: skip
    codes = []
    for args in commands:
        codes.append(_run(["--config-dir", str(cfg), *args]))
        out, err = capsys.readouterr()
        outputs.append(out)
        errors.append(err)
    assert codes[:2] == [0, 0]
    assert codes[2] in (0, 1)  # validate reports findings as exit 1 (R-46)
    assert json.loads(outputs[2])["data"]["config_hash"]
    assert codes[7:11] == [0, 2, 0, 0]
    assert fake_keyring.store[("herness", "snow.token")] == TYPED  # the prompt value was stored
    logs = [p.read_text("utf-8") for p in sorted((cli_env.root / "data" / "logs").glob("*"))]
    corpus = "\n".join(outputs + errors + logs)
    for sentinel in (*SENTINELS.values(), TYPED):
        assert sentinel not in corpus
    # Non-vacuous: the outputs name the secrets, the env file got the values, audits exist.
    show = json.loads(outputs[1])["data"]
    assert "secret:anthropic.api_key" in json.dumps(show)  # a reference, never a value
    assert "secret:anthropic.api_key" in outputs[0]
    status = json.loads(outputs[5])["data"]["secrets"]
    assert {row["name"] for row in status} >= {"vllm.api_key", "anthropic.api_key"}
    render = json.loads(outputs[9])["data"]
    assert render["VLLM_API_KEY"] == render["OPENJEV_API_KEY"] == "set"
    content = rendered[0].decode("utf-8")
    assert SENTINELS["vllm.api_key"] in content
    assert SENTINELS["OPENJEV_API_KEY"] in content
    joined = "\n".join(logs)
    assert '"secret_set"' in joined
    assert '"deploy_render"' in joined
