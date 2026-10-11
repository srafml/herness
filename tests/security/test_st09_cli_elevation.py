"""ST09-29 (TH09-27): elevated and inline commands refused before any work (T09-26).

The real commands run through ``herness.cli.main`` on ``cli_env`` (config with roles, OS user,
keyring) over a migrated ``ops_store``: the admin OS user ``root`` in a non-elevated shell runs
``secrets set`` and ``deploy install``; the reviewer ``rev`` runs ``sync --inline``. Spies on
the ``herness.admin`` command functions, the keyring writes, the job queue and impl 08's
``run_inline`` show that each refusal (``PermissionDenied``, exit 11) comes first; the audit
log holds exactly one ``auth`` line per refusal.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.support.cli_env import CliEnv
from tests.support.egress_harness import audit_fields
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

import herness.core.jobs
from herness import cli
from herness._cli import identity
from herness.admin import commands_deploy, commands_secrets
from herness.core.jobs import queue

pytestmark = pytest.mark.integration


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    code = exc.value.code
    assert isinstance(code, int)
    return code


class Spies:
    """Every call that would mean work started: admin commands, keyring writes, jobs."""

    def __init__(self, keyring: MemoryKeyring, bundle: Path) -> None:
        self.calls: list[str] = []
        self.keyring, self.bundle = keyring, bundle

    def record(self, name: str) -> Callable[..., Any]:
        def spy(*args: object, **kwargs: object) -> None:
            del args, kwargs
            self.calls.append(name)
            msg = f"{name} must not be reached"
            raise AssertionError(msg)

        return spy


@pytest.fixture
def spies(
    cli_env: CliEnv, ops_store: OpsStoreHandle, fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Spies:  # fmt: skip
    """Admin ``root``, reviewer ``rev``; a non-elevated shell; every work entry spied."""
    del ops_store
    fake_keyring.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()
    cli_env.write_config(admins=["root"], reviewers=["rev"])
    monkeypatch.setattr(identity, "_is_elevated", lambda: False)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    found = Spies(fake_keyring, bundle)
    monkeypatch.setattr(commands_secrets, "cmd_secrets_set", found.record("cmd_secrets_set"))
    monkeypatch.setattr(commands_deploy, "cmd_deploy_install", found.record("cmd_deploy_install"))
    monkeypatch.setattr(fake_keyring, "set_password", found.record("keyring.set_password"))
    monkeypatch.setattr(herness.core.jobs, "run_inline", found.record("run_inline"))
    monkeypatch.setattr(queue, "claim", found.record("claim"))
    monkeypatch.setattr("typer.prompt", found.record("typer.prompt"))
    return found


def _jobs(cli_env: CliEnv) -> list[tuple[str, ...]]:
    with sqlite3.connect(cli_env.root / "data" / "ops.sqlite") as con:
        return list(con.execute("SELECT job_id FROM job"))


CASES = [
    ("root", ["secrets", "set", "snow.token"], "secrets set"),
    ("root", ["deploy", "install", "{bundle}"], "deploy install"),
    ("rev", ["sync", "files", "--inline"], "sync"),
]


@pytest.mark.parametrize(
    "case", CASES,
    ids=["admin-non-elevated-secrets-set", "admin-non-elevated-deploy-install",
         "reviewer-sync-inline"],
)  # fmt: skip
def test_st09_29_refused_before_admin_call_keyring_write_or_job_claim(
    cli_env: CliEnv, spies: Spies, capsys: pytest.CaptureFixture[str],
    case: tuple[str, list[str], str],
) -> None:  # fmt: skip
    """ST09-29 admin in a non-elevated shell (``secrets set``, ``deploy install``) and a
    reviewer (``sync --inline``): ``PermissionDenied`` (exit 11) before any ``herness.admin``
    call, keyring write or job claim; exactly one ``auth`` audit line each."""
    user, args, path = case
    cli_env.user = user
    keys_before = dict(spies.keyring.store)
    argv = [str(spies.bundle) if a == "{bundle}" else a for a in args]
    capsys.readouterr()
    assert _main(["--json", *argv]) == 11
    envelope = json.loads(capsys.readouterr().out)
    error = envelope["error"]
    assert (error["type"], error["exit_code"]) == ("PermissionDenied", 11)
    assert error["details"] == {"code": "role_missing", "command": path}
    assert spies.calls == []
    assert spies.keyring.store == keys_before
    assert _jobs(cli_env) == []
    auth = audit_fields(cli_env.root / "data" / "logs", "auth")
    assert len(auth) == 1, auth
    role = "admin" if user == "root" else "reviewer"
    assert (auth[0]["role"], auth[0]["result"]) == (role, "denied")
    assert set(auth[0]) == {"result", "role", "user_ref"}  # a user_ref, never the OS user
    assert auth[0]["user_ref"] != user


def test_st09_29_elevated_admin_control_reaches_the_command(
    cli_env: CliEnv, spies: Spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST09-29 control: the same admin in an elevated shell reaches the ``herness.admin``
    ``secrets set`` command (spied) and the admin's ``build --inline`` reaches
    ``run_inline`` (spied: the spy's error exits 1); no ``auth`` line is written."""
    monkeypatch.setattr(identity, "_is_elevated", lambda: True)
    cli_env.user = "root"
    assert _main(["secrets", "set", "snow.token"]) == 1
    assert spies.calls == ["cmd_secrets_set"]
    spies.calls.clear()
    assert _main(["build", "--inline"]) == 1
    assert spies.calls == ["run_inline"]
    assert len(_jobs(cli_env)) == 1
    assert audit_fields(cli_env.root / "data" / "logs", "auth") == []
