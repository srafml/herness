"""A scripted WSL host for ``herness deploy pull`` tests (impl 10 U10-81 to U10-83, U10-92).

``PullHost.install`` answers, through the ``fake_runner`` table, the firewall cmdlets (a
stateful ``DefaultOutboundAction``), the compose ``stop``, ``docker pull`` / ``image inspect`` /
``run``, the ``refs/main`` script and the ``test`` / ``find`` / ``stat`` / ``sha256sum`` calls of
``verify_weights`` over an in-memory Hugging Face cache and GGUF. Tests tamper with its fields
(``repo_digests``, ``hashes``, ``sizes``, ``set_block_fails`` ...) before running the code.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from tests.support.config_tree import write_checked_config
from tests.support.fake_runner import FakeRunner

from herness.admin import deploy_pull
from herness.admin.wsl import CmdResult
from herness.core import config as c

ACTOR = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret - audit actor id
WSL = ["wsl.exe", "-d", "herness", "--exec"]
WSL_ROOT = ["wsl.exe", "-d", "herness", "-u", "root", "--exec"]
PS = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
ROOT = "/opt/herness/hf"
GGUF = f"{ROOT}/gguf/qwen3-large.gguf"


def _hex(seed: str, width: int = 64) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:width]


def git_blob_id(content: bytes) -> str:
    """Git blob id: SHA-1 of ``blob <size>`` + NUL + content (``git hash-object`` output)."""
    header = b"blob " + str(len(content)).encode() + bytes([0])
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def repo_dir(model: str) -> str:
    return f"{ROOT}/hub/models--{model.replace('/', '--')}"


def _ok(stdout: str = "", returncode: int = 0) -> CmdResult:
    return CmdResult(returncode, stdout, "", 0.0)


@dataclass
class PullHost:
    """In-memory host state behind the ``fake_runner`` rows."""

    cfg: c.HernessConfig
    runner: FakeRunner
    outbound: str = "Block"
    set_block_fails: int = 0  # the next N ``Block`` sets leave the setting unchanged
    readback: Callable[[str], str] | None = None  # override the Get- JSON
    repo_digests: dict[str, list[str]] = field(default_factory=dict)
    fail_prefix: tuple[str, ...] | None = None  # any argv with this prefix exits 1
    links: dict[str, dict[str, str]] = field(default_factory=dict)  # snapshot -> rel -> target
    regular: dict[str, list[str]] = field(default_factory=dict)  # snapshot -> non-link files
    sizes: dict[str, int] = field(default_factory=dict)  # blob path -> size
    hashes: dict[str, str] = field(default_factory=dict)  # blob path -> sha256sum output
    contents: dict[str, bytes] = field(default_factory=dict)  # non-LFS blob path -> bytes
    on_block: Callable[[], None] | None = None
    events: list[str] = field(default_factory=list)

    def snapshot(self, model: str) -> str:
        return f"{repo_dir(model)}/snapshots/{self.cfg.deploy.reasoning.revision}"

    def seed(self) -> None:
        """Pinned digests, two snapshots with LFS and non-LFS files, and the GGUF."""
        for name in ("reasoning", "openjev", "large"):
            ref = getattr(self.cfg.deploy, name).image
            repo, _at, digest = ref.partition("@")
            self.repo_digests[ref] = [f"{repo}@sha256:{'0' * 64}", f"{repo}@{digest}"]
        for model in (self.cfg.deploy.reasoning.model, self.cfg.deploy.openjev.model):
            config = (model + "config").encode().ljust(1011, b" ")  # 1000 + len("config.json")
            config_blob = git_blob_id(config)
            self.contents[f"{repo_dir(model)}/blobs/{config_blob}"] = config
            files = {
                "config.json": config_blob,
                "model.safetensors": _hex(model + "weights"),
                "sub/tokenizer.model": _hex(model + "tok"),
            }
            snap = self.snapshot(model)
            self.links[snap] = {}
            for rel, blob in files.items():
                ups = "../" * (rel.count("/") + 2)
                self.links[snap][rel] = f"{ups}blobs/{blob}"
                path = f"{repo_dir(model)}/blobs/{blob}"
                self.sizes[path] = 1000 + len(rel)
                self.hashes[path] = blob if len(blob) == 64 else _hex(blob)
        self.sizes[GGUF] = 4096
        self.hashes[GGUF] = self.cfg.deploy.large.sha256

    # --- responders ---------------------------------------------------------------------------

    def _ps(self, argv: list[str]) -> CmdResult:
        command = argv[-1]
        if command.startswith("Set-NetFirewallHyperVVMSetting"):
            action = "Allow" if "'Allow'" in command else "Block"
            self.events.append(f"set:{action}")
            if action == "Block" and self.set_block_fails > 0:
                self.set_block_fails -= 1
                return _ok(returncode=1)
            self.outbound = action
            if action == "Block" and self.on_block is not None:
                self.on_block()
            return _ok()
        self.events.append("get")
        if self.readback is not None:
            return _ok(self.readback(self.outbound))
        body = {"Name": deploy_pull.WSL_VM_CREATOR_ID, "DefaultOutboundAction": self.outbound}
        return _ok(json.dumps(body))

    def _wsl(self, argv: list[str]) -> CmdResult:  # noqa: PLR0911 - one branch per program
        inner = argv[argv.index("--exec") + 1 :]
        self.events.append(" ".join(inner[:2]))
        if (
            self.fail_prefix is not None
            and tuple(inner[: len(self.fail_prefix)]) == self.fail_prefix
        ):
            return _ok(returncode=1)
        program = inner[0]
        if inner[:3] == ["docker", "image", "inspect"]:
            digests = self.repo_digests.get(inner[3], [])
            return _ok(json.dumps([{"Id": "sha256:" + "f" * 64, "RepoDigests": digests}]))
        if program in {"docker", "sh"}:
            return _ok()
        if program == "test":
            return _ok(returncode=0 if inner[2] in self.links else 1)
        if program == "find":
            snap = inner[1]
            if inner[2] == "-mindepth":  # files that are neither directories nor links
                found = "".join(f"{snap}/{rel}\0" for rel in self.regular.get(snap, []))
                return _ok(found, 0 if snap in self.links else 1)
            records = [f"{t}={snap}/{rel}\0" for rel, t in self.links.get(snap, {}).items()]
            return _ok("".join(records), 0 if snap in self.links else 1)
        if program == "stat":
            lines = [f"{self.sizes[p]}={p}" for p in inner[3:] if p in self.sizes]
            return _ok("\n".join(lines) + "\n", 0 if len(lines) == len(inner) - 3 else 1)
        if program == "sha256sum":
            lines = [f"{self.hashes[p]}  {p}" for p in inner[1:] if p in self.hashes]
            return _ok("\n".join(lines) + "\n", 0 if len(lines) == len(inner) - 1 else 1)
        if inner[:3] == ["git", "hash-object", "--no-filters"] and inner[3] == "--":
            paths = inner[4:]
            ids = [git_blob_id(self.contents[p]) for p in paths if p in self.contents]
            return _ok("".join(f"{i}\n" for i in ids), 0 if len(ids) == len(paths) else 128)
        return _ok(returncode=127)

    def _compose(self, argv: list[str]) -> CmdResult:
        self.events.append("compose:" + argv[-1])
        return _ok()

    def install(self) -> PullHost:
        """Add the host's rows to the runner table; return ``self``."""
        gpu = self.cfg.resilience.resilience.gpu
        self.runner.add(PS, self._ps)
        self.runner.add(WSL, self._wsl)
        self.runner.add(WSL_ROOT, self._wsl)
        self.runner.add([*gpu.compose_cmd, "-f", gpu.compose_file], self._compose)
        return self

    def blob(self, model: str, rel: str) -> str:
        """The blob path a snapshot file links to."""
        snap = self.snapshot(model)
        return posixpath.normpath(
            posixpath.join(snap, posixpath.dirname(rel), self.links[snap][rel])
        )


def start_pull_host(
    tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> PullHost:
    """A cached, pinned config over ``tmp_path``, an elevated host, no worker, no delays.

    Needs the ``fake_keyring`` fixture active; the root conftest resets the config after.
    """
    monkeypatch.setattr(deploy_pull, "_require_elevated", lambda: None)
    monkeypatch.setattr(deploy_pull, "worker_alive", lambda: False)
    monkeypatch.setattr(deploy_pull, "_RETRY_DELAY_S", 0.0)
    cfg = c.init_config(config_dir=write_checked_config(tmp_path), env={})
    host = PullHost(cfg, runner)
    host.seed()
    return host.install()
