"""Security tests for ``herness deploy pull`` (impl 10 ST10-19 to ST10-21, ST10-47, ST10-51).

Tampered images, weight blobs and GGUF files are refused (TH10-30, TH10-33 to TH10-35); the
firewall window ends in ``Block`` when the pull throws (TH10-39); model services are stopped
before ``Allow`` and the timer closes a window whose download stalls (TH10-45). Every host call
is answered by ``fake_runner`` through ``tests.support.pull_host``.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_runner import FakeRunner
from tests.support.pull_host import ACTOR, GGUF, PS, WSL, PullHost, start_pull_host

from herness.admin import deploy, deploy_pull
from herness.admin.wsl import CmdResult
from herness.core.errors import FatalError

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


def _nothing_rendered(host: PullHost) -> bool:
    rendered = Path(host.cfg.paths.data) / "deploy" / "rendered.json"
    return not rendered.exists() and all(deploy.SCRIPT not in a for a in host.runner.argvs)


def test_st10_19_repo_digests_without_pin_refused(host: PullHost) -> None:
    """ST10-19 the registry's image lacks the pinned digest: ``FatalError``, nothing rendered."""
    ref = host.cfg.deploy.large.image
    host.repo_digests[ref] = [ref.partition("@")[0] + "@sha256:" + "9" * 64]
    with pytest.raises(FatalError, match="image digest mismatch: large"):
        deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert _nothing_rendered(host)
    assert host.outbound == "Block"
    assert not any(a[4:6] == ["docker", "run"] for a in host.runner.argvs)  # no weights pulled


def test_st10_19_digest_must_be_the_suffix(host: PullHost) -> None:
    """ST10-19 the pinned hex inside another entry (not as its digest) is no match."""
    ref = host.cfg.deploy.reasoning.image
    digest = ref.rpartition("@")[2]
    host.repo_digests[ref] = [f"vllm/vllm-openai@{digest}0", f"evil/{digest}@sha256:{'1' * 64}"]
    with pytest.raises(FatalError, match="image digest mismatch: reasoning"):
        deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)


def test_st10_20_altered_blob_is_hash_mismatch(host: PullHost) -> None:
    """ST10-20 one blob's content altered: ``hash_mismatch:<file>``; the pull aborts."""
    model = host.cfg.deploy.reasoning.model
    host.hashes[host.blob(model, "model.safetensors")] = "5" * 64  # sha256 of the altered bytes
    result = deploy_pull.verify_weights(host.cfg, "reasoning", full=True)
    assert result["status"] == "hash_mismatch:model.safetensors"
    with pytest.raises(FatalError, match=r"hash_mismatch:model\.safetensors"):
        deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert _nothing_rendered(host)
    assert host.outbound == "Block"


def test_st10_20_spoofed_link_record_refused(host: PullHost) -> None:
    """ST10-20 a link target holding ``=`` plus a snapshot path cannot pass as a genuine blob."""
    model = host.cfg.deploy.reasoning.model
    snap = host.snapshot(model)
    genuine = host.links[snap]["model.safetensors"]  # ../../blobs/<64 hex>
    # The link really points at "<genuine>=<snap>/evil"; its record reads
    # "<genuine>=<snap>/evil=<snap>/model.safetensors".
    host.links[snap] = {"model.safetensors": f"{genuine}={snap}/evil"}
    result = deploy_pull.verify_weights(host.cfg, "reasoning", full=True)
    assert result["status"].startswith("link_invalid:")
    with pytest.raises(FatalError, match="link_invalid"):
        deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert host.outbound == "Block"


@pytest.mark.parametrize(
    "target", ["../../blobs/" + "a" * 63, "../../blobs/" + "A" * 64, "../../blobs/../blobs/x"]
)
def test_st10_20_link_target_must_be_a_blob_name(host: PullHost, target: str) -> None:
    """ST10-20 a target not shaped ``(../)+blobs/<40 or 64 hex>`` is ``link_invalid``."""
    host.links[host.snapshot(host.cfg.deploy.openjev.model)]["config.json"] = target
    status = deploy_pull.verify_weights(host.cfg, "decider", full=False)["status"]
    assert status == "link_invalid:config.json"


def test_st10_20_regular_file_in_snapshot_refused(host: PullHost) -> None:
    """ST10-20 a tampered regular file replacing a link is refused, full and quick."""
    model = host.cfg.deploy.reasoning.model
    snap = host.snapshot(model)
    del host.links[snap]["model.safetensors"]
    host.regular[snap] = ["model.safetensors"]
    for full in (True, False):
        result = deploy_pull.verify_weights(host.cfg, "reasoning", full=full)
        assert result == {"status": "not_a_link:model.safetensors", "files": "0"}
    with pytest.raises(FatalError, match=r"not_a_link:model\.safetensors"):
        deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert host.outbound == "Block"


def test_st10_21_flipped_gguf_is_fatal(host: PullHost) -> None:
    """ST10-21 a GGUF byte flipped: its SHA-256 differs from the pin and the pull fails."""
    host.hashes[GGUF] = host.cfg.deploy.large.sha256[:-1] + "e"
    status = deploy_pull.verify_weights(host.cfg, "large", full=True)["status"]
    assert status == "hash_mismatch:qwen3-large.gguf"
    with pytest.raises(FatalError, match="weight verification failed: large"):
        deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert host.outbound == "Block"


def test_st10_47_exception_mid_pull_leaves_block(host: PullHost) -> None:
    """ST10-47 an exception thrown mid-pull: the final firewall state is ``Block``."""

    def explode(argv: list[str]) -> CmdResult:
        assert host.outbound == "Allow"
        msg = "connection reset"
        raise RuntimeError(msg)

    host.runner.add([*WSL, "docker", "run"], explode)
    with pytest.raises(RuntimeError, match="connection reset"):
        deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert host.outbound == "Block"
    assert host.events[-2:] == ["set:Block", "get"]


def test_st10_47_keyboard_interrupt_leaves_block(host: PullHost) -> None:
    """ST10-47 even a ``KeyboardInterrupt`` in the body closes the window."""

    def interrupt(argv: list[str]) -> CmdResult:
        raise KeyboardInterrupt

    host.runner.add([*WSL, "docker", "pull"], interrupt)
    with pytest.raises(KeyboardInterrupt):
        deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert host.outbound == "Block"


def test_st10_51_services_stopped_before_allow(host: PullHost) -> None:
    """ST10-51 a model service running: compose ``stop`` precedes ``Allow``."""
    deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert host.events.index("compose:stop") < host.events.index("set:Allow")
    assert host.outbound == "Block"


def test_st10_51_timer_restores_block_during_stalled_download(
    host: PullHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-51 a download stalls past the window: the timer sets ``Block`` while it runs."""
    monkeypatch.setattr(deploy_pull, "_MINUTE_S", 0.05)
    blocked = threading.Event()
    seen: dict[str, str] = {}

    def on_block() -> None:
        blocked.set()

    def stalled(argv: list[str]) -> CmdResult:
        assert host.outbound == "Allow"
        assert blocked.wait(10), "timer did not close the window"
        seen["during"] = host.outbound  # still inside the download call
        return CmdResult(1, "", "network unreachable", 0.0)

    host.on_block = on_block
    host.runner.add([*WSL, "docker", "pull"], stalled)
    with pytest.raises(FatalError):
        deploy_pull.run_pull(host.cfg, allow_download=True, actor=ACTOR)
    assert seen["during"] == "Block"
    assert host.outbound == "Block"


def test_st10_51_timer_expiry_aborts_a_clean_body(
    host: PullHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-51 a body that outlives the window is not a success: ``FatalError`` on exit."""
    monkeypatch.setattr(deploy_pull, "_MINUTE_S", 0.01)
    blocked = threading.Event()
    host.on_block = blocked.set
    with (
        pytest.raises(FatalError, match="expired after 1 minutes"),
        deploy_pull.firewall_window(actor=ACTOR, max_minutes=1),
    ):
        assert blocked.wait(10)
    assert host.outbound == "Block"


def test_st10_51_timer_close_failure_still_closed_by_finally(
    host: PullHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-51 the timer's close fails twice; the body's exit closes it (third set)."""
    monkeypatch.setattr(deploy_pull, "_MINUTE_S", 0.01)
    host.set_block_fails = 2
    done = threading.Event()
    original = host._ps

    def watch(argv: list[str]) -> CmdResult:
        result = original(argv)
        if host.events.count("get") == 2:
            done.set()
        return result

    host.runner.add(PS, watch)
    with (
        pytest.raises(FatalError, match="expired"),
        deploy_pull.firewall_window(actor=ACTOR, max_minutes=1),
    ):
        assert done.wait(10)
    assert host.outbound == "Block"
