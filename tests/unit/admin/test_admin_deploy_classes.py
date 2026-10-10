"""Tests for ``herness deploy up|down|rollback|prune`` (impl 10 U10-84, U10-85, U10-72).

UT10-64 (``class_up`` / ``class_down`` through the worker arbiter or under the GPU lock),
UT10-65 (``prune`` keeps the current pins and the two newest promoted pin sets;
``rollback_class`` refuses a target that is not on disk) and the thin ``cmd_deploy_*``
wrappers, all over the ``fake_runner`` table scripted by ``tests.support.deploy_host``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.support.deploy_host import (
    GGUF_DIR,
    DeployHost,
    FakeHealth,
    FakeJobs,
    FakeTime,
    history_path,
    render,
    start_deploy_host,
)
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_runner import FakeRunner
from tests.support.pull_host import ACTOR, repo_dir

from herness.admin import _deploy_prune, commands_deploy, deploy
from herness.core import config as c
from herness.core import egress
from herness.core.errors import (
    ConfigError,
    EgressBlocked,
    FatalError,
    ModelUnavailable,
    StoreBusy,
)
from herness.core.jobs.gpu_lock import GpuLock

pytestmark = pytest.mark.unit

SVC = "herness"
JEV_KEY = "jev-key-91c2-sentinel"  # pragma: allowlist secret - test sentinel
VLLM_KEY = "vllm-key-7f3a-sentinel"  # pragma: allowlist secret - test sentinel
REASONING_URL = "http://127.0.0.1:8000"
OPENJEV_URL = "http://127.0.0.1:8100"
LARGE_URL = "http://127.0.0.1:8200"


class Env:
    """One test's host, fake worker, health client and clock."""

    def __init__(self, parts: tuple[DeployHost, FakeJobs, FakeHealth, FakeTime]) -> None:
        self.host, self.jobs, self.health, self.time = parts
        self.cfg: c.HernessConfig = self.host.cfg

    @property
    def compose_calls(self) -> list[list[str]]:
        gpu = self.cfg.resilience.resilience.gpu
        prefix = [*gpu.compose_cmd, "-f", gpu.compose_file]
        return [a[len(prefix) :] for a in self.host.runner.argvs if a[: len(prefix)] == prefix]

    def audit(self) -> list[dict[str, Any]]:
        files = sorted(Path(self.cfg.paths.logs).glob("audit-*.jsonl"))
        lines = [json.loads(x) for p in files for x in p.read_text("utf-8").splitlines()]
        return [x["fields"] for x in lines]

    def history(self) -> list[dict[str, Any]]:
        path = history_path(self.cfg)
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text("utf-8").splitlines()]


@pytest.fixture
def env(
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> Env:
    fake_keyring.store[(SVC, "vllm.api_key")] = VLLM_KEY
    fake_keyring.store[(SVC, "openjev_api_key")] = JEV_KEY
    return Env(start_deploy_host(tmp_path, fake_runner, monkeypatch))


@pytest.fixture
def rendered(env: Env) -> Env:
    render(env.host)
    env.host.runner.calls.clear()
    return env


def _digest(cfg: c.HernessConfig, section: str) -> str:
    image: str = getattr(cfg.deploy, section).image
    return image.partition("@")[2]


# --- UT10-64 class_up / class_down -----------------------------------------------------------


def test_ut10_64_worker_alive_requests_class_no_compose(rendered: Env) -> None:
    """UT10-64 alive worker: the requested class is set, no compose call; audit and history."""
    env = rendered
    env.jobs.alive = True
    env.jobs.load_after = 2
    result = deploy.class_up(env.cfg, "reasoning", actor=ACTOR, now=deploy.clock.now())
    assert result == {"gpu_class": "reasoning", "via": "worker", "status": "loaded"}
    assert env.jobs.requested == ["reasoning"]
    assert env.compose_calls == []
    assert env.host.runner.calls == []
    assert env.time.sleeps == [5.0, 5.0]
    target = "reasoning:" + _digest(env.cfg, "reasoning")
    assert {"action": "deploy_up", "target": target} in env.audit()
    [entry] = env.history()
    assert entry["gpu_class"] == "reasoning"
    assert entry["pins"] == {
        "image": env.cfg.deploy.reasoning.image,
        "model": env.cfg.deploy.reasoning.model,
        "revision": env.cfg.deploy.reasoning.revision,
    }
    assert entry["config_hash"] == c.config_hash(env.cfg)


def test_ut10_64_no_worker_lock_stop_up_and_health(rendered: Env) -> None:
    """UT10-64 no worker: GPU lock taken, other classes stopped, ``up -d`` argv, health polled."""
    env = rendered
    env.health.unhealthy[REASONING_URL] = 2
    held: list[bool] = []

    def probe(argv: list[str]) -> Any:
        try:
            with GpuLock(Path(env.cfg.paths.data) / "locks" / "gpu.lock"):
                held.append(False)
        except ConfigError:
            held.append(True)
        return env.host._compose(argv)

    gpu = env.cfg.resilience.resilience.gpu
    env.host.runner.add([*gpu.compose_cmd, "-f", gpu.compose_file], probe)
    result = deploy.class_up(env.cfg, "reasoning", actor=ACTOR, now=deploy.clock.now())
    assert result == {"gpu_class": "reasoning", "via": "compose", "status": "loaded"}
    assert env.compose_calls == [
        ["--profile", "decider", "stop"],
        ["--profile", "large", "stop"],
        ["--profile", "reasoning", "up", "-d", "vllm-reasoning"],
    ]
    assert held == [True, True, True]
    assert [g[:2] for g in env.health.gets] == [(REASONING_URL, "/health")] * 3
    assert {g[3] for g in env.health.gets} == {10.0}
    assert env.time.sleeps == [5.0, 5.0]
    assert env.jobs.requested == []
    assert any(f["action"] == "deploy_up" for f in env.audit())
    assert len(env.history()) == 1
    with GpuLock(Path(env.cfg.paths.data) / "locks" / "gpu.lock"):  # released afterwards
        pass


def test_ut10_64_decider_starts_openjev_with_bearer(rendered: Env) -> None:
    """UT10-64 the decider class starts ``openjev`` too; its health GET carries the key."""
    env = rendered
    deploy.class_up(env.cfg, "decider", actor=ACTOR, now=deploy.clock.now())
    assert env.compose_calls[-1] == ["--profile", "decider", "up", "-d", "openjev"]
    assert env.health.gets == [(OPENJEV_URL, "/v1/models", JEV_KEY, 10.0)]
    assert env.history()[0]["pins"]["image"] == env.cfg.deploy.openjev.image


def test_ut10_64_large_pins_and_gguf_target(rendered: Env) -> None:
    """UT10-64 the large class audits the GGUF sha and records image, gguf, sha256."""
    env = rendered
    deploy.class_up(env.cfg, "large", actor=ACTOR, now=deploy.clock.now())
    large = env.cfg.deploy.large
    assert {"action": "deploy_up", "target": f"large:{large.sha256}"} in env.audit()
    assert env.history()[0]["pins"] == {
        "image": large.image,
        "gguf": large.gguf,
        "sha256": large.sha256,
    }
    assert env.health.gets[0][0] == LARGE_URL


def test_ut10_64_gpu_lock_held_is_store_busy(rendered: Env) -> None:
    """UT10-64 another GPU owner holds the lock: ``StoreBusy("GPU lock held")``, no compose."""
    env = rendered
    with (
        GpuLock(Path(env.cfg.paths.data) / "locks" / "gpu.lock"),
        pytest.raises(StoreBusy, match="GPU lock held"),
    ):
        deploy.class_up(env.cfg, "reasoning", actor=ACTOR, now=deploy.clock.now())
    assert env.compose_calls == []
    assert env.history() == []


def test_ut10_64_not_rendered_is_config_error(env: Env) -> None:
    """UT10-64 no ``rendered.json``, or one for another config: run render first."""
    with pytest.raises(ConfigError, match="run herness deploy render first"):
        deploy.class_up(env.cfg, "reasoning", actor=ACTOR, now=deploy.clock.now())
    path = Path(env.cfg.paths.data) / "deploy" / "rendered.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ('{"config_hash": "other"}', "not json", "[]"):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError, match="run herness deploy render first"):
            deploy.class_up(env.cfg, "reasoning", actor=ACTOR, now=deploy.clock.now())
    assert env.host.runner.calls == []
    assert env.audit() == []


def test_ut10_64_unknown_class_is_config_error(rendered: Env) -> None:
    """UT10-64 a class outside reasoning, decider, large is refused before anything runs."""
    with pytest.raises(ConfigError, match="gpu_class"):
        deploy.class_up(rendered.cfg, "none", actor=ACTOR, now=deploy.clock.now())  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="gpu_class"):
        deploy.class_down(rendered.cfg, "bogus", actor=ACTOR)  # type: ignore[arg-type]


def test_ut10_64_health_timeout_is_model_unavailable(rendered: Env) -> None:
    """UT10-64 a service never healthy within ``start_timeout_s``: ``ModelUnavailable``."""
    env = rendered
    env.health.always_down.add(LARGE_URL)
    with pytest.raises(ModelUnavailable, match="llamacpp-large"):
        deploy.class_up(env.cfg, "large", actor=ACTOR, now=deploy.clock.now())
    assert sum(env.time.sleeps) >= 1200
    assert env.history() == []
    with GpuLock(Path(env.cfg.paths.data) / "locks" / "gpu.lock"):  # released on failure
        pass


def test_ut10_64_health_exception_reads_unhealthy(rendered: Env) -> None:
    """UT10-64 a failing health GET counts as not healthy (it never escapes)."""
    env = rendered
    env.health.raise_for.add(REASONING_URL)
    with pytest.raises(ModelUnavailable):
        deploy.class_up(env.cfg, "reasoning", actor=ACTOR, now=deploy.clock.now())


@pytest.mark.parametrize("error", [EgressBlocked, ConfigError], ids=["egress", "config"])
def test_ut10_64_health_guard_errors_propagate(
    rendered: Env, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """UT10-64 ``EgressBlocked`` / ``ConfigError`` from the loopback client are not swallowed."""

    def refuse(*_a: Any, **_k: Any) -> Any:
        msg = "refused"
        raise error(msg)

    monkeypatch.setattr(egress, "loopback_http_client", refuse)
    with pytest.raises(error, match="refused"):
        deploy.class_up(rendered.cfg, "reasoning", actor=ACTOR, now=deploy.clock.now())
    assert rendered.time.sleeps == []


def test_ut10_64_compose_failure_is_model_unavailable(rendered: Env) -> None:
    """UT10-64 a failing ``up -d`` is ``ModelUnavailable``; no history line."""
    env = rendered
    env.host.fail_prefix = ("up", "-d", "vllm-reasoning")
    with pytest.raises(ModelUnavailable, match="compose up failed"):
        deploy.class_up(env.cfg, "reasoning", actor=ACTOR, now=deploy.clock.now())
    assert env.history() == []


def test_ut10_64_worker_never_loads_is_model_unavailable(rendered: Env) -> None:
    """UT10-64 the worker does not load the class within start_timeout_s + 300 s."""
    env = rendered
    env.jobs.alive, env.jobs.load_after = True, None
    with pytest.raises(ModelUnavailable, match="worker did not load reasoning"):
        deploy.class_up(env.cfg, "reasoning", actor=ACTOR, now=deploy.clock.now())
    assert 1200 <= sum(env.time.sleeps) <= 1205
    assert env.compose_calls == []


def test_ut10_64_worker_gone_falls_back_to_compose(rendered: Env) -> None:
    """UT10-64 a worker that disappears before the request: the GPU-lock path runs."""
    env = rendered
    env.jobs.alive, env.jobs.answer = True, "no_worker"
    result = deploy.class_up(env.cfg, "reasoning", actor=ACTOR, now=deploy.clock.now())
    assert result["via"] == "compose"
    assert env.compose_calls[-1][-3:] == ["up", "-d", "vllm-reasoning"]


def test_ut10_64_class_down_worker_and_compose(rendered: Env) -> None:
    """UT10-64 down: the worker is asked for ``none``; without one, ``stop`` under the lock."""
    env = rendered
    env.jobs.alive = True
    assert deploy.class_down(env.cfg, "large", actor=ACTOR) == {
        "gpu_class": "large",
        "via": "worker",
        "status": "requested",
    }
    assert env.jobs.requested == ["none"]
    assert env.compose_calls == []
    env.jobs.alive = False
    assert deploy.class_down(env.cfg, "large", actor=ACTOR)["via"] == "compose"
    assert env.compose_calls == [["--profile", "large", "stop"]]
    env.jobs.alive, env.jobs.answer = True, "no_worker"
    assert deploy.class_down(env.cfg, "decider", actor=ACTOR)["status"] == "stopped"
    downs = [f for f in env.audit() if f["action"] == "deploy_down"]
    assert [f["target"] for f in downs] == ["large", "large", "decider"]


def test_ut10_64_class_down_failure_and_lock(rendered: Env) -> None:
    """UT10-64 a failing ``stop`` is ``ModelUnavailable``; a held lock is ``StoreBusy``."""
    env = rendered
    env.host.fail_prefix = ("stop",)
    with pytest.raises(ModelUnavailable, match="compose stop failed"):
        deploy.class_down(env.cfg, "reasoning", actor=ACTOR)
    with (
        GpuLock(Path(env.cfg.paths.data) / "locks" / "gpu.lock"),
        pytest.raises(StoreBusy, match="GPU lock held"),
    ):
        deploy.class_down(env.cfg, "reasoning", actor=ACTOR)


# --- UT10-65 prune ---------------------------------------------------------------------------


def _hex(ch: str, width: int = 64) -> str:
    return ch * width


def _seed_history(env: Env) -> dict[str, Any]:
    """Four reasoning pin sets and three large pin sets in history; all on disk."""
    cfg, host = env.cfg, env.host
    history = deploy.DeployHistory(history_path(cfg))
    model = cfg.deploy.reasoning.model
    repo = repo_dir(model)
    image_repo = cfg.deploy.reasoning.image.partition("@")[0]
    shared = _hex("c", 40)
    revs = [_hex(str(i), 40) for i in range(1, 5)]
    digests = [f"sha256:{_hex(str(i))}" for i in range(1, 5)]
    for i, (rev, digest) in enumerate(zip(revs, digests, strict=True)):
        pins = {"image": f"{image_repo}@{digest}", "model": model, "revision": rev}
        history.record("reasoning", pins, "h", deploy.clock.now())
        host.add_snapshot(model, rev, {"model.safetensors": _hex("abcd"[i]), "config.json": shared})
        host.images[image_repo].append(digest)
    host.blob_files[repo] += [_hex("e"), "tmp.incomplete"]  # an orphan blob and a partial one
    large = cfg.deploy.large
    ggufs = ["old-1.gguf", "old-2.gguf", "old-3.gguf"]
    for name in ggufs:
        pins = {"image": large.image, "gguf": name, "sha256": _hex("f")}
        history.record("large", pins, "h", deploy.clock.now())
    host.gguf_files += [*ggufs, "readme.txt"]
    jev_repo = cfg.deploy.openjev.image.partition("@")[0]
    host.images[jev_repo].append(f"sha256:{_hex('9')}")
    return {"repo": repo, "revs": revs, "digests": digests, "image_repo": image_repo,
            "jev_repo": jev_repo}  # fmt: skip


def test_ut10_65_prune_removes_only_unkept(env: Env) -> None:
    """UT10-65 history with 4 pin sets: only non-kept digests, revisions, blobs, GGUFs go."""
    seeded = _seed_history(env)
    repo, revs, digests = seeded["repo"], seeded["revs"], seeded["digests"]
    counts = deploy.prune(env.cfg, actor=ACTOR)
    assert counts == {"images": 3, "snapshots": 2, "blobs": 3, "gguf": 1, "failed": 0}
    assert sorted(env.host.image_rm) == sorted(
        [f"{seeded['image_repo']}@{digests[0]}", f"{seeded['image_repo']}@{digests[1]}",
         f"{seeded['jev_repo']}@sha256:{_hex('9')}"]
    )  # fmt: skip
    removed = env.host.removed
    assert set(removed) == {
        f"{repo}/snapshots/{revs[0]}",
        f"{repo}/snapshots/{revs[1]}",
        f"{repo}/blobs/{_hex('a')}",
        f"{repo}/blobs/{_hex('b')}",
        f"{repo}/blobs/{_hex('e')}",
        f"{GGUF_DIR}/old-1.gguf",
    }
    assert removed.index(f"{repo}/snapshots/{revs[0]}") < removed.index(f"{repo}/blobs/{_hex('a')}")
    kept = env.host.snapshot_dirs[repo]
    assert set(kept) == {revs[2], revs[3], env.cfg.deploy.reasoning.revision}
    assert f"{repo}/blobs/tmp.incomplete".rpartition("/")[2] in env.host.blob_files[repo]
    assert _hex("c", 40) in env.host.blob_files[repo]
    assert "readme.txt" in env.host.gguf_files
    rm_calls = [a for a in env.host.runner.argvs if "rm" in a and "-rf" in a]
    assert all(a[:6] == ["wsl.exe", "-d", "herness", "-u", "root", "--exec"] for a in rm_calls)
    [fields] = [f for f in env.audit() if f["action"] == "deploy_prune"]
    assert fields["counts"] == "images=3;snapshots=2;blobs=3;gguf=1;failed=0"


def test_ut10_65_prune_keeps_current_without_history(env: Env) -> None:
    """UT10-65 with no history only the current pins are kept; current items stay."""
    counts = deploy.prune(env.cfg, actor=ACTOR)
    assert counts == {"images": 0, "snapshots": 0, "blobs": 0, "gguf": 0, "failed": 0}
    assert env.host.removed == []
    assert env.host.image_rm == []


def test_ut10_65_prune_refuses_while_swapping(env: Env) -> None:
    """UT10-65 a class swap in progress: ``StoreBusy``, nothing removed."""
    _seed_history(env)
    env.jobs.loaded = "swapping"
    with pytest.raises(StoreBusy, match="swap"):
        deploy.prune(env.cfg, actor=ACTOR)
    assert env.host.removed == []
    assert env.host.image_rm == []


def test_ut10_65_prune_refuses_item_of_loaded_class(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-65 a plan that would remove an item the loaded class uses: ``StoreBusy``."""
    env.jobs.loaded = "reasoning"
    plan = _deploy_prune.PrunePlan(images=[env.cfg.deploy.reasoning.image])
    monkeypatch.setattr(_deploy_prune, "plan", lambda *_a, **_k: plan)
    with pytest.raises(StoreBusy, match="loaded GPU class"):
        deploy.prune(env.cfg, actor=ACTOR)
    assert env.host.image_rm == []
    env.jobs.loaded = "large"  # another class loaded: the reasoning image may go
    assert deploy.prune(env.cfg, actor=ACTOR)["images"] == 1


def test_ut10_65_prune_refuses_path_outside_model_root(env: Env) -> None:
    """UT10-65 a listed path outside ``model_root`` or not a direct child: ``FatalError``."""
    _seed_history(env)
    for bad in ("/etc/passwd\0", f"{GGUF_DIR}/../../../etc/x.gguf\0", f"{GGUF_DIR}/a/b.gguf\0"):
        env.host.listing_override[GGUF_DIR] = bad
        with pytest.raises(FatalError, match="model_root"):
            deploy.prune(env.cfg, actor=ACTOR)
    assert env.host.removed == []
    assert env.host.image_rm == []
    with pytest.raises(FatalError, match="model_root"):
        _deploy_prune.under_root(env.cfg, "/opt/herness/hf-other/x")


def test_ut10_65_unreadable_snapshot_keeps_blobs(env: Env) -> None:
    """UT10-65 a remaining snapshot whose links cannot be read: no blob of that repo goes."""
    seeded = _seed_history(env)
    repo = seeded["repo"]
    env.host.snapshot_dirs[repo].append("not-a-revision")  # kept, no links readable
    counts = deploy.prune(env.cfg, actor=ACTOR)
    assert counts["snapshots"] == 2
    assert counts["blobs"] == 0
    assert "not-a-revision" in env.host.snapshot_dirs[repo]


def test_ut10_65_prune_missing_dirs_and_failures(env: Env) -> None:
    """UT10-65 missing directories prune nothing; a failing ``rm`` or image ``rm`` counts."""
    gone = {"image": env.cfg.deploy.reasoning.image, "model": "org/gone", "revision": _hex("7", 40)}
    deploy.DeployHistory(history_path(env.cfg)).record("reasoning", gone, "h", deploy.clock.now())
    seeded = _seed_history(env)  # the oldest pin set names a model with no directory on disk
    env.host.rm_fails.add(f"{GGUF_DIR}/old-1.gguf")
    env.host.runner.add(
        ["wsl.exe", "-d", "herness", "--exec", "docker", "image", "rm",
         f"{seeded['jev_repo']}@sha256:{_hex('9')}"], returncode=1,
    )  # fmt: skip
    with structlog.testing.capture_logs() as logs:
        counts = deploy.prune(env.cfg, actor=ACTOR)
    assert counts == {"images": 2, "snapshots": 2, "blobs": 3, "gguf": 0, "failed": 2}
    assert repo_dir("org/gone") not in env.host.snapshot_dirs
    failed = [x for x in logs if x["event"] == "deploy.prune.remove_failed"]
    assert [(x["kind"], x["item"], x["returncode"]) for x in failed] == [
        ("images", f"{seeded['jev_repo']}@sha256:{_hex('9')}", 1),
        ("gguf", f"{GGUF_DIR}/old-1.gguf", 1),
    ]


@pytest.mark.parametrize("returncode", [1, 124], ids=["find-error", "timeout"])
def test_ut10_65_failed_snapshot_listing_removes_no_blob(env: Env, returncode: int) -> None:
    """UT10-65 a failing or timed-out snapshot listing: no ``rm`` there, pinned blobs survive."""
    seeded = _seed_history(env)
    repo = seeded["repo"]
    blobs_before = list(env.host.blob_files[repo])
    find = ["wsl.exe", "-d", "herness", "--exec", "find", f"{repo}/snapshots"]
    env.host.runner.add(find, returncode=returncode)
    counts = deploy.prune(env.cfg, actor=ACTOR)
    assert counts["snapshots"] == 0
    assert counts["blobs"] == 0
    assert not any(p.startswith(repo + "/") for p in env.host.removed)
    assert env.host.blob_files[repo] == blobs_before


def test_ut10_65_kept_revision_missing_from_listing_removes_no_blob(env: Env) -> None:
    """UT10-65 a kept revision absent from the snapshot listing: no blob of the repo goes."""
    seeded = _seed_history(env)
    repo = seeded["repo"]
    env.host.snapshot_dirs[repo].remove(env.cfg.deploy.reasoning.revision)  # current not listed
    counts = deploy.prune(env.cfg, actor=ACTOR)
    assert counts["snapshots"] == 2  # unkept revisions may still go
    assert counts["blobs"] == 0
    assert not any(p.startswith(f"{repo}/blobs/") for p in env.host.removed)


def test_ut10_65_failed_blob_or_gguf_listing_removes_nothing(env: Env) -> None:
    """UT10-65 a failing blobs or GGUF listing plans no removal there."""
    seeded = _seed_history(env)
    wsl_find = ["wsl.exe", "-d", "herness", "--exec", "find"]
    env.host.runner.add([*wsl_find, f"{seeded['repo']}/blobs"], returncode=124)
    env.host.runner.add([*wsl_find, GGUF_DIR], returncode=1)
    counts = deploy.prune(env.cfg, actor=ACTOR)
    assert (counts["blobs"], counts["gguf"], counts["snapshots"]) == (0, 0, 2)


def test_ut10_65_in_use_check_ignores_malformed_history(env: Env) -> None:
    """UT10-65 a malformed newest entry of the loaded class is ignored, not a ``KeyError``."""
    _seed_history(env)
    path = history_path(env.cfg)
    bad = {"image": env.cfg.deploy.reasoning.image, "revision": _hex("8", 40)}  # no model
    line = {"ts": "2026-09-24T12:00:00Z", "gpu_class": "reasoning", "pins": bad,
            "config_hash": "h"}  # fmt: skip
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    env.jobs.loaded = "reasoning"
    assert deploy.prune(env.cfg, actor=ACTOR)["snapshots"] == 2


def test_ut10_65_prune_skips_invalid_history_and_listing_failure(env: Env) -> None:
    """UT10-65 malformed history pin sets are ignored; a failing image listing removes none."""
    path = history_path(env.cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    bad = {"image": "../x", "model": "a/../../b", "revision": "zz"}
    line = {"ts": "2026-09-24T12:00:00Z", "gpu_class": "reasoning", "pins": bad,
            "config_hash": "h"}  # fmt: skip
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    env.host.runner.add(["wsl.exe", "-d", "herness", "--exec", "docker", "image", "ls"],
                        returncode=1)  # fmt: skip
    counts = deploy.prune(env.cfg, actor=ACTOR)
    assert counts["images"] == 0
    listed = [a for a in env.host.runner.argvs if a[4:7] == ["docker", "image", "ls"]]
    assert all("../x" not in a for a in listed)
    assert listed[0][7:10] == ["--digests", "--format", "{{.Digest}}"]


# --- UT10-65 rollback_class ------------------------------------------------------------------


def test_ut10_65_rollback_without_local_image(rendered: Env) -> None:
    """UT10-65 the pinned image is not on disk: ``ConfigError`` "run deploy pull"."""
    env = rendered
    env.host.repo_digests.pop(env.cfg.deploy.reasoning.image)
    with pytest.raises(ConfigError, match="rollback target not on disk; run deploy pull"):
        deploy.rollback_class(env.cfg, "reasoning", actor=ACTOR, now=deploy.clock.now())
    assert env.compose_calls == []
    assert not any(f["action"] == "deploy_rollback" for f in env.audit())


def test_ut10_65_rollback_inspect_fails_or_weights_missing(rendered: Env) -> None:
    """UT10-65 a failing inspect or weights not on disk: the same ``ConfigError``."""
    env = rendered
    env.host.fail_prefix = ("docker", "image", "inspect")
    with pytest.raises(ConfigError, match="run deploy pull"):
        deploy.rollback_class(env.cfg, "large", actor=ACTOR, now=deploy.clock.now())
    env.host.fail_prefix = None
    env.host.links.clear()
    with pytest.raises(ConfigError, match="run deploy pull"):
        deploy.rollback_class(env.cfg, "decider", actor=ACTOR, now=deploy.clock.now())
    env.host.runner.add(
        ["wsl.exe", "-d", "herness", "--exec", "docker", "image", "inspect"], "not json"
    )
    with pytest.raises(ConfigError, match="run deploy pull"):
        deploy.rollback_class(env.cfg, "large", actor=ACTOR, now=deploy.clock.now())


def test_ut10_65_rollback_renders_down_up_and_audits(env: Env) -> None:
    """UT10-65 on-disk target: verify, render, down, up, ``deploy_rollback`` audit."""
    result = deploy.rollback_class(env.cfg, "reasoning", actor=ACTOR, now=deploy.clock.now())
    assert result == {"gpu_class": "reasoning", "via": "compose", "status": "loaded"}
    actions = [f["action"] for f in env.audit()]
    assert actions == ["deploy_render", "deploy_down", "deploy_up", "deploy_rollback"]
    assert env.compose_calls[0] == ["--profile", "reasoning", "stop"]
    assert env.compose_calls[-1][-3:] == ["up", "-d", "vllm-reasoning"]
    target = "reasoning:" + _digest(env.cfg, "reasoning")
    assert {"action": "deploy_rollback", "target": target} in env.audit()


# --- U10-72 wrappers -------------------------------------------------------------------------


def test_ut10_60_cmd_deploy_render(rendered: Env) -> None:
    """UT10-60 ``cmd_deploy_render`` maps ``render_env`` to ``data``."""
    result = commands_deploy.cmd_deploy_render(cfg=rendered.cfg, actor=ACTOR)
    assert result.ok
    assert result.exit_code == 0
    assert result.data["VLLM_API_KEY"] == "set"  # pragma: allowlist secret - key name
    assert VLLM_KEY not in json.dumps(result.data)


def test_ut10_64_cmd_deploy_up_and_down(rendered: Env) -> None:
    """UT10-64 ``cmd_deploy_up`` / ``cmd_deploy_down`` map the class units to ``data``."""
    env = rendered
    up = commands_deploy.cmd_deploy_up("reasoning", cfg=env.cfg, actor=ACTOR)
    assert up.ok
    assert up.data == {"gpu_class": "reasoning", "via": "compose", "status": "loaded"}
    down = commands_deploy.cmd_deploy_down("reasoning", cfg=env.cfg, actor=ACTOR)
    assert down.data["status"] == "stopped"
    assert down.exit_code == 0


def test_ut10_65_cmd_deploy_rollback_and_prune(env: Env) -> None:
    """UT10-65 ``cmd_deploy_rollback`` / ``cmd_deploy_prune`` map the units to ``data``."""
    rolled = commands_deploy.cmd_deploy_rollback("large", cfg=env.cfg, actor=ACTOR)
    assert rolled.data["gpu_class"] == "large"
    pruned = commands_deploy.cmd_deploy_prune(cfg=env.cfg, actor=ACTOR)
    assert pruned.ok
    assert pruned.data == {"images": 0, "snapshots": 0, "blobs": 0, "gguf": 0, "failed": 0}
