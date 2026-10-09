"""Tests for ``herness deploy pull`` (impl 10 U10-81 to U10-83, U10-92, U10-72 ``pull``).

UT10-62 (image digests, weight hashes, the download window's registry hosts) and UT10-63
(firewall window, ``run_pull`` orchestration, the ``cmd_deploy_pull`` wrapper), all over the
``fake_runner`` table scripted by ``tests.support.pull_host``.
"""

from __future__ import annotations

import ctypes
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.support.egress_harness import audit_fields, egress_lines, make_guard
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_runner import FakeRunner
from tests.support.pull_host import ACTOR, GGUF, PS, WSL, PullHost, repo_dir, start_pull_host

from herness.admin import commands_deploy, deploy_pull
from herness.core import egress
from herness.core.errors import (
    ConfigError,
    EgressBlocked,
    FatalError,
    PermissionDenied,
    StoreBusy,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def host(
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> PullHost:
    del fake_keyring
    return start_pull_host(tmp_path, fake_runner, monkeypatch)


def _inner(host: PullHost, program: str) -> list[list[str]]:
    """Argvs (after ``--exec``) of every WSL call of ``program``."""
    calls = [a[a.index("--exec") + 1 :] for a in host.runner.argvs if "--exec" in a]
    return [a for a in calls if a[0] == program]


def _manifest(host: PullHost, name: str) -> dict[str, int]:
    path = Path(host.cfg.paths.data) / "deploy" / f"weights-{name}.json"
    data: dict[str, int] = json.loads(path.read_text(encoding="utf-8"))
    return data


# --- UT10-62 pull_images ---------------------------------------------------------------------


def test_ut10_62_pull_images_ok_and_registry_egress(host: PullHost) -> None:
    """UT10-62 every pinned digest found in ``RepoDigests``: ok; registries are recorded."""
    guard = make_guard(host.cfg)
    with guard.download_window(allow_download=True, actor=ACTOR):
        result = deploy_pull.pull_images(host.cfg, guard=guard)
    assert [r["service"] for r in result] == ["reasoning", "openjev", "large"]
    assert all(r["status"] == "ok" and r["digest"].startswith("sha256:") for r in result)
    pulls = [c for c in host.runner.calls if c.argv[4:6] == ["docker", "pull"]]
    assert [c.argv[6] for c in pulls] == [
        host.cfg.deploy.reasoning.image,
        host.cfg.deploy.openjev.image,
        host.cfg.deploy.large.image,
    ]
    assert {c.timeout_s for c in pulls} == {3600}
    lines = egress_lines(Path(host.cfg.paths.logs))
    assert [(x["destination"], x["decision"], x["purpose"]) for x in lines] == [
        ("registry-1.docker.io", "allowed", "model_download"),
        ("registry-1.docker.io", "allowed", "model_download"),
        ("ghcr.io", "allowed", "model_download"),
    ]


def test_ut10_62_pull_images_digest_mismatch(host: PullHost) -> None:
    """UT10-62 ``RepoDigests`` without the pin: ``FatalError`` naming the service."""
    host.repo_digests[host.cfg.deploy.openjev.image] = ["razorback16/openjev@sha256:" + "e" * 64]
    guard = make_guard(host.cfg)
    with (
        guard.download_window(allow_download=True, actor=ACTOR),
        structlog.testing.capture_logs() as logs,
        pytest.raises(FatalError, match="image digest mismatch: openjev"),
    ):
        deploy_pull.pull_images(host.cfg, guard=guard)
    expected = {"event": "deploy.pull.failed", "service": "openjev", "reason": "image_digest"}
    assert any(expected.items() <= line.items() for line in logs)


@pytest.mark.parametrize(
    "stdout", ["not json", "[]", '{"RepoDigests": []}', '[{"RepoDigests": 7}]']
)
def test_ut10_62_pull_images_garbled_inspect_is_mismatch(host: PullHost, stdout: str) -> None:
    """UT10-62 unreadable ``docker image inspect`` output is a mismatch (fail closed)."""
    host.runner.add([*WSL, "docker", "image", "inspect"], stdout)
    guard = make_guard(host.cfg)
    with (
        guard.download_window(allow_download=True, actor=ACTOR),
        pytest.raises(FatalError, match="image digest mismatch: reasoning"),
    ):
        deploy_pull.pull_images(host.cfg, guard=guard)


def test_ut10_62_pull_images_inspect_failure_and_pull_failure(host: PullHost) -> None:
    """UT10-62 a failing inspect is a mismatch; a failing pull is ``image pull failed``."""
    guard = make_guard(host.cfg)
    host.fail_prefix = ("docker", "image", "inspect")
    with guard.download_window(allow_download=True, actor=ACTOR):
        with pytest.raises(FatalError, match="image digest mismatch: reasoning"):
            deploy_pull.pull_images(host.cfg, guard=guard)
        host.fail_prefix = ("docker", "pull")
        with pytest.raises(FatalError, match="image pull failed: reasoning"):
            deploy_pull.pull_images(host.cfg, guard=guard)


def test_ut10_62_pull_images_outside_window_blocked(host: PullHost) -> None:
    """UT10-62 outside a download window the registry check refuses; nothing is pulled."""
    with pytest.raises(EgressBlocked):
        deploy_pull.pull_images(host.cfg, guard=make_guard(host.cfg))
    assert host.runner.calls == []


def test_ut10_62_window_admits_only_pinned_registries(host: PullHost) -> None:
    """UT10-62 the window adds the pinned images' registries, not any registry."""
    guard = make_guard(host.cfg)
    with guard.download_window(allow_download=True, actor=ACTOR):
        guard.check("https://ghcr.io/v2/", b"", "model_download", "none")
        with pytest.raises(EgressBlocked) as caught:
            guard.check("https://quay.io/v2/", b"", "model_download", "none")
    assert caught.value.reason == "host_not_allowed"
    assert egress.registry_host("vllm/vllm-openai@sha256:" + "a" * 64) == "registry-1.docker.io"
    assert egress.registry_host("Reg.Example:5000/x/y") == "reg.example:5000"
    assert egress.registry_host("ubuntu") == "registry-1.docker.io"


# --- UT10-62 verify_weights ------------------------------------------------------------------


def test_ut10_62_verify_weights_full_ok_writes_manifest(host: PullHost) -> None:
    """UT10-62 matching ``sha256sum`` output: ok, LFS blobs hashed, manifest written."""
    reasoning = deploy_pull.verify_weights(host.cfg, "reasoning", full=True)
    decider = deploy_pull.verify_weights(host.cfg, "decider", full=True)
    large = deploy_pull.verify_weights(host.cfg, "large", full=True)
    assert reasoning == decider == {"status": "ok", "files": "3"}
    assert large == {"status": "ok", "files": "1"}
    hashed = [p for argv in _inner(host, "sha256sum") for p in argv[1:]]
    model = host.cfg.deploy.reasoning.model
    assert host.blob(model, "model.safetensors") in hashed
    assert host.blob(model, "config.json") not in hashed  # 40-hex git blob: no LFS hash
    assert GGUF in hashed
    revision = host.cfg.deploy.reasoning.revision
    assert _manifest(host, f"reasoning-{revision}") == {
        "config.json": 1011,
        "model.safetensors": 1017,
        "sub/tokenizer.model": 1019,
    }
    assert _manifest(host, f"large-{host.cfg.deploy.large.sha256[:12]}") == {
        "qwen3-large.gguf": 4096
    }
    find = _inner(host, "find")[0]
    assert find == ["find", host.snapshot(model), "-type", "l", "-printf", "%l=", "-print0"]
    assert _inner(host, "find")[1] == [
        "find", host.snapshot(model), "-mindepth", "1", "-not", "-type", "d",
        "-not", "-type", "l", "-print0",
    ]  # fmt: skip


def test_ut10_62_verify_weights_mismatch(host: PullHost) -> None:
    """UT10-62 a wrong or missing ``sha256sum`` line is ``hash_mismatch:<file>``."""
    model = host.cfg.deploy.reasoning.model
    host.hashes[host.blob(model, "sub/tokenizer.model")] = "0" * 64
    result = deploy_pull.verify_weights(host.cfg, "reasoning", full=True)
    assert result == {"status": "hash_mismatch:sub/tokenizer.model", "files": "0"}
    del host.hashes[host.blob(model, "sub/tokenizer.model")]
    result = deploy_pull.verify_weights(host.cfg, "reasoning", full=True)
    assert result["status"] == "hash_mismatch:sub/tokenizer.model"
    revision = host.cfg.deploy.reasoning.revision
    assert not (
        Path(host.cfg.paths.data) / "deploy" / f"weights-reasoning-{revision}.json"
    ).exists()


def test_ut10_62_verify_weights_garbled_sha_line_is_mismatch(host: PullHost) -> None:
    """UT10-62 a ``sha256sum`` line that does not parse never counts as a match."""
    host.runner.add([*WSL, "sha256sum"], "garbage\n\\" + "a" * 64 + "  x\n")
    result = deploy_pull.verify_weights(host.cfg, "large", full=True)
    assert result["status"] == "hash_mismatch:qwen3-large.gguf"


def test_ut10_62_verify_weights_batches_of_100(host: PullHost) -> None:
    """UT10-62 ``sha256sum`` and ``stat`` get at most 100 paths per call."""
    model = host.cfg.deploy.reasoning.model
    snap = host.snapshot(model)
    for i in range(150):
        blob = f"{i:064x}"
        host.links[snap][f"shard-{i}.bin"] = f"../../blobs/{blob}"
        host.sizes[f"{repo_dir(model)}/blobs/{blob}"] = 10
        host.hashes[f"{repo_dir(model)}/blobs/{blob}"] = blob
    assert deploy_pull.verify_weights(host.cfg, "reasoning", full=True)["status"] == "ok"
    assert [len(a) - 1 for a in _inner(host, "sha256sum")] == [100, 52]
    assert [len(a) - 3 for a in _inner(host, "stat")] == [100, 53]


def test_ut10_62_verify_weights_quick_mode(host: PullHost) -> None:
    """UT10-62 ``full=False``: no hashing; an empty or missing LFS blob is reported."""
    model = host.cfg.deploy.openjev.model
    assert deploy_pull.verify_weights(host.cfg, "decider", full=False)["status"] == "ok"
    assert _inner(host, "sha256sum") == []
    host.sizes[host.blob(model, "model.safetensors")] = 0
    assert deploy_pull.verify_weights(host.cfg, "decider", full=False)["status"] == (
        "missing:model.safetensors"
    )
    del host.sizes[host.blob(model, "config.json")]
    assert deploy_pull.verify_weights(host.cfg, "decider", full=False)["status"] == (
        "missing:config.json"
    )
    del host.sizes[GGUF]
    assert deploy_pull.verify_weights(host.cfg, "large", full=False)["status"] == (
        "missing:qwen3-large.gguf"
    )


@pytest.mark.parametrize(
    ("target", "status"),
    [
        ("/etc/passwd", "link_invalid:model.safetensors"),
        ("../../../other/blobs/" + "a" * 64, "link_invalid:model.safetensors"),
        ("../../blobs/x=y", "link_invalid:unknown"),
    ],
)
def test_ut10_62_verify_weights_link_outside_blobs(
    host: PullHost, target: str, status: str
) -> None:
    """UT10-62 a snapshot link that does not resolve into ``<repo>/blobs`` fails closed."""
    host.links[host.snapshot(host.cfg.deploy.reasoning.model)]["model.safetensors"] = target
    assert deploy_pull.verify_weights(host.cfg, "reasoning", full=True)["status"] == status


def test_ut10_62_verify_weights_snapshot_states(host: PullHost) -> None:
    """UT10-62 missing, empty and unlistable snapshots are reasons, not ok."""
    snap = host.snapshot(host.cfg.deploy.reasoning.model)
    host.links[snap] = {}
    assert (
        deploy_pull.verify_weights(host.cfg, "reasoning", full=True)["status"] == "snapshot_empty"
    )
    host.fail_prefix = ("find",)
    assert deploy_pull.verify_weights(host.cfg, "reasoning", full=True)["status"] == "list_failed"
    del host.links[snap]
    assert deploy_pull.verify_weights(host.cfg, "reasoning", full=True)["status"] == (
        "snapshot_missing"
    )
    with pytest.raises(ConfigError):
        deploy_pull.verify_weights(host.cfg, "openjev", full=True)  # type: ignore[arg-type]


# --- UT10-62 pull_weights --------------------------------------------------------------------


def test_ut10_62_pull_weights_argv_refs_and_verify(host: PullHost) -> None:
    """UT10-62 download argv as specified; OpenJev ``refs/main`` via the fixed script; ok."""
    guard = make_guard(host.cfg)
    with guard.download_window(allow_download=True, actor=ACTOR):
        result = deploy_pull.pull_weights(host.cfg, guard=guard)
    assert [r["gpu_class"] for r in result] == ["reasoning", "decider", "large"]
    assert all(r["status"] == "ok" for r in result)
    deploy, rev = host.cfg.deploy, host.cfg.deploy.reasoning.revision
    runs = [c for c in host.runner.calls if c.argv[4:6] == ["docker", "run"]]
    assert [c.argv[4:] for c in runs] == [
        ["docker", "run", "--rm", "--entrypoint", "huggingface-cli", "-e", "HF_HUB_OFFLINE=0",
         "-e", "HF_HUB_DISABLE_TELEMETRY=1", "-v", "/opt/herness/hf:/root/.cache/huggingface",
         deploy.reasoning.image, "download", model, "--revision", rev]
        for model in (deploy.reasoning.model, deploy.openjev.model)
    ]  # fmt: skip
    assert {c.timeout_s for c in runs} == {7200}
    [refs] = [c for c in host.runner.calls if "-u" in c.argv]
    assert refs.argv == [
        "wsl.exe", "-d", "herness", "-u", "root", "--exec", "sh", "-c",
        'printf %s "$1" > "$2"', "sh", rev, f"{repo_dir(deploy.openjev.model)}/refs/main",
    ]  # fmt: skip
    lines = egress_lines(Path(host.cfg.paths.logs))
    assert [(x["destination"], x["path"]) for x in lines] == [
        ("huggingface.co", f"/{m}/resolve/{rev}")
        for m in (deploy.reasoning.model, deploy.openjev.model)
    ]


@pytest.mark.parametrize(
    ("prefix", "message"),
    [
        (("docker", "run"), "weight download failed: reasoning"),
        (("sh", "-c"), "weight refs write failed: decider"),
    ],
)
def test_ut10_62_pull_weights_failures(
    host: PullHost, prefix: tuple[str, ...], message: str
) -> None:
    """UT10-62 a failed download or ``refs/main`` write is ``FatalError``."""
    host.fail_prefix = prefix
    guard = make_guard(host.cfg)
    with (
        guard.download_window(allow_download=True, actor=ACTOR),
        pytest.raises(FatalError, match=message),
    ):
        deploy_pull.pull_weights(host.cfg, guard=guard)


def test_ut10_62_pull_weights_mismatch_is_fatal(host: PullHost) -> None:
    """UT10-62 a hash mismatch after download aborts the pull with ``FatalError``."""
    host.hashes[host.blob(host.cfg.deploy.openjev.model, "model.safetensors")] = "1" * 64
    guard = make_guard(host.cfg)
    with (
        guard.download_window(allow_download=True, actor=ACTOR),
        pytest.raises(FatalError, match=r"decider: hash_mismatch:model\.safetensors"),
    ):
        deploy_pull.pull_weights(host.cfg, guard=guard)


# --- UT10-63 firewall_window -----------------------------------------------------------------


def test_ut10_63_body_raises_allow_then_block_read_back(host: PullHost) -> None:
    """UT10-63 ``Allow`` then ``Block`` set and read back; the body's exception re-raised."""

    def body() -> None:
        with deploy_pull.firewall_window(actor=ACTOR):
            assert host.outbound == "Allow"
            msg = "boom"
            raise RuntimeError(msg)

    with structlog.testing.capture_logs() as logs, pytest.raises(RuntimeError, match="boom"):
        body()
    assert host.events == ["set:Allow", "set:Block", "get"]
    assert host.outbound == "Block"
    sets = [c.argv[-1] for c in host.runner.calls if c.argv[:4] == PS]
    assert sets[0] == (
        "Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' "
        "-DefaultOutboundAction 'Allow'"
    )
    assert sets[2].startswith("Get-NetFirewallHyperVVMSetting -Name '{40E0AC32")
    assert {c.timeout_s for c in host.runner.calls} == {30}
    events = [(x["event"], x["log_level"]) for x in logs]
    assert events == [
        ("deploy.firewall.window_opened", "warning"),
        ("deploy.firewall.window_closed", "info"),
    ]
    assert (Path(host.cfg.paths.data) / "locks" / "deploy-pull.lock").exists()


def test_ut10_63_clean_exit_and_enum_integer_readback(host: PullHost) -> None:
    """UT10-63 a clean body closes the window; the enum integer 4 reads back as ``Block``."""
    host.readback = lambda state: json.dumps(
        [{"DefaultOutboundAction": 4 if state == "Block" else 2}]
    )
    with deploy_pull.firewall_window(actor=ACTOR, max_minutes=5):
        pass
    assert host.events == ["set:Allow", "set:Block", "get"]


@pytest.mark.parametrize("stdout", ["", "nope", "[]", '{"DefaultOutboundAction": "Allow"}', "[1]"])
def test_ut10_63_unreadable_readback_retries_then_fails(host: PullHost, stdout: str) -> None:
    """UT10-63 a read-back that is not ``Block`` is retried once, then ``FatalError``."""
    host.readback = lambda _state: stdout
    with (
        pytest.raises(FatalError, match="could not be closed"),
        deploy_pull.firewall_window(actor=ACTOR),
    ):
        pass
    assert host.events == ["set:Allow", "set:Block", "get", "set:Block", "get"]


def test_ut10_63_readback_runner_error_counts_as_open(host: PullHost) -> None:
    """UT10-63 a failing Get- call or a raising runner never counts as closed."""
    calls = {"n": 0}

    def flaky(argv: list[str]) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:  # the first Get-: runner raises
            msg = "pipe"
            raise OSError(msg)
        return host._ps(argv)

    host.runner.add(PS, flaky)
    with deploy_pull.firewall_window(actor=ACTOR):
        pass
    assert host.outbound == "Block"
    assert host.events == ["set:Allow", "set:Block", "set:Block", "get"]


def test_ut10_63_allow_failure_still_closes(host: PullHost) -> None:
    """UT10-63 a failed ``Allow`` raises ``FatalError`` and ``Block`` is still enforced."""
    original = host._ps

    def refuse_allow(argv: list[str]) -> Any:
        if "'Allow'" in argv[-1]:
            return deploy_pull.wsl.CmdResult(1, "", "", 0.0)
        return original(argv)

    host.runner.add(PS, refuse_allow)
    with (
        pytest.raises(FatalError, match="could not be opened"),
        deploy_pull.firewall_window(actor=ACTOR),
    ):
        pytest.fail("body must not run")
    assert host.events == ["set:Block", "get"]


@pytest.mark.parametrize("minutes", [0, 121, -1])
def test_ut10_63_window_cap(host: PullHost, minutes: int) -> None:
    """UT10-63 the window is capped at 120 minutes."""
    with pytest.raises(ConfigError), deploy_pull.firewall_window(actor=ACTOR, max_minutes=minutes):
        pass
    assert host.events == []


@pytest.mark.skipif(sys.platform != "win32", reason="windll exists on Windows only")
def test_ut10_63_requires_elevated_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-63 not elevated (or not Windows): ``PermissionDenied`` before anything runs."""
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined,unused-ignore]
    monkeypatch.setattr(shell32, "IsUserAnAdmin", lambda: 0)
    with pytest.raises(PermissionDenied):
        deploy_pull._require_elevated()
    monkeypatch.setattr(shell32, "IsUserAnAdmin", lambda: 1)
    deploy_pull._require_elevated()
    monkeypatch.setattr(deploy_pull.os, "name", "posix")
    with pytest.raises(PermissionDenied):
        deploy_pull._require_elevated()


# --- UT10-63 run_pull and cmd_deploy_pull ----------------------------------------------------


def test_ut10_63_run_pull_orders_and_audits(host: PullHost) -> None:
    """UT10-63 stop, Allow, pulls, Block read back, then the quick verify and the audit."""
    result = deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert [r["status"] for r in result["images"]] == ["ok"] * 3
    assert [r["status"] for r in result["weights"]] == ["ok"] * 3
    assert {k: v["status"] for k, v in result["verified"].items()} == dict.fromkeys(
        ("reasoning", "decider", "large"), "ok"
    )
    ev = host.events
    assert ev[:2] == ["compose:stop", "set:Allow"]
    closed = ev.index("set:Block")
    assert ev[closed + 1] == "get"
    assert all(e.split(" ")[0] in {"stat", "test", "find"} for e in ev[closed + 2 :])
    assert "sha256sum" in " ".join(ev[:closed])
    [stop] = [c for c in host.runner.calls if c.argv[-1] == "stop"]
    gpu = host.cfg.resilience.resilience.gpu
    assert stop.argv == [
        *gpu.compose_cmd, "-f", gpu.compose_file,
        "--profile", "reasoning", "--profile", "decider", "--profile", "large", "stop",
    ]  # fmt: skip
    targets = [f["target"] for f in audit_fields(Path(host.cfg.paths.logs), "admin_action")]
    assert targets == ["download_window", "complete"]


def test_ut10_63_run_pull_preconditions(host: PullHost, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-63 no ``--allow-download``: ``ConfigError``; worker alive: ``StoreBusy``."""
    with pytest.raises(ConfigError, match="requires --allow-download"):
        deploy_pull.run_pull(host.cfg, allow_download=False, actor=ACTOR)
    monkeypatch.setattr(deploy_pull, "worker_alive", lambda: True)
    with pytest.raises(StoreBusy, match="stop the worker"):
        deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert host.runner.calls == []


def test_ut10_63_run_pull_stop_failure_never_opens(host: PullHost) -> None:
    """UT10-63 services that cannot be stopped: ``FatalError`` and no ``Allow``."""
    gpu = host.cfg.resilience.resilience.gpu
    host.runner.add([*gpu.compose_cmd, "-f", gpu.compose_file], "", returncode=1)
    with pytest.raises(FatalError, match="could not stop the model services"):
        deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert "set:Allow" not in host.events


def test_ut10_63_run_pull_quick_verify_after_window(host: PullHost) -> None:
    """UT10-63 a blob gone once the window closed fails the pull; no ``complete`` audit."""
    blob = host.blob(host.cfg.deploy.reasoning.model, "model.safetensors")
    host.on_block = lambda: host.sizes.pop(blob, None)
    with pytest.raises(FatalError, match="after the window failed: reasoning"):
        deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert host.outbound == "Block"
    targets = [f["target"] for f in audit_fields(Path(host.cfg.paths.logs), "admin_action")]
    assert "complete" not in targets


def test_ut10_63_cmd_deploy_pull_wraps_run_pull(host: PullHost) -> None:
    """UT10-63 ``cmd_deploy_pull`` maps ``run_pull``'s return value to ``data``."""
    result = commands_deploy.cmd_deploy_pull(cfg=host.cfg, actor=ACTOR, allow_download=True)
    assert result.ok
    assert result.exit_code == 0
    assert set(result.data) == {"images", "weights", "verified"}
    assert "cmd_deploy_pull" in commands_deploy.__all__


def test_ut10_63_failed_readback_command_is_not_closed(host: PullHost) -> None:
    """UT10-63 a Get- call exiting non-zero never counts as ``Block``."""
    get = (
        f"Get-NetFirewallHyperVVMSetting -Name '{deploy_pull.WSL_VM_CREATOR_ID}'"
        " | ConvertTo-Json -Depth 3"
    )
    host.runner.add([*PS, get], '{"DefaultOutboundAction": "Block"}', returncode=1)
    with (
        pytest.raises(FatalError, match="could not be closed"),
        deploy_pull.firewall_window(actor=ACTOR),
    ):
        pass
    assert host.outbound == "Block"  # set, but never confirmed by a successful read-back


def test_ut10_63_interrupt_in_retry_still_critical_and_block(
    host: PullHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-63 Ctrl+C in the retry delay: CRITICAL ``restore_failed``, one more ``Block``."""

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    host.readback = lambda _state: '{"DefaultOutboundAction": "Allow"}'
    monkeypatch.setattr(deploy_pull.clock, "sleep", interrupted)
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(KeyboardInterrupt),
        deploy_pull.firewall_window(actor=ACTOR),
    ):
        pass
    assert host.events == ["set:Allow", "set:Block", "get", "set:Block"]
    assert [x["event"] for x in logs if x["log_level"] == "critical"] == [
        "deploy.firewall.restore_failed"
    ]


def test_ut10_62_weight_download_failure_logs_gpu_class(host: PullHost) -> None:
    """UT10-62 ``deploy.pull.failed`` for a failed download names the GPU class."""
    host.fail_prefix = ("docker", "run")
    guard = make_guard(host.cfg)
    with (
        guard.download_window(allow_download=True, actor=ACTOR),
        structlog.testing.capture_logs() as logs,
        pytest.raises(FatalError),
    ):
        deploy_pull.pull_weights(host.cfg, guard=guard)
    expected = {
        "event": "deploy.pull.failed",
        "gpu_class": "reasoning",
        "reason": "weight_download",
    }
    assert any(expected.items() <= line.items() for line in logs)
