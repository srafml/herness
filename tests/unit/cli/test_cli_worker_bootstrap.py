"""Tests for herness.cli.worker_bootstrap and its sibling _worker_boot (T09-27, U09-104)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.support.cli_env import CliEnv

import herness.admin
import herness.connectors.jobs
import herness.core.egress_socket
import herness.core.jobs.handlers
import herness.eval.runner
import herness.harness.memory
import herness.harness.swarm.handler
import herness.harness.tools
import herness.store.ops.resilience
from herness import cli
from herness._cli import _eval_deps, _worker_boot
from herness.core.config import get_config
from herness.core.errors import ConfigError
from herness.harness import memory
from herness.harness.llm import registry

pytestmark = pytest.mark.unit


@pytest.fixture
def boot_env(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> tuple[CliEnv, list[str]]:
    """Env vars set, every owner registration function and the ports faked, order recorded."""

    cfg_dir = cli_env.write_config()
    (cli_env.root / "data").mkdir()
    monkeypatch.setenv("HERNESS_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("HERNESS_DATA_DIR", str(cli_env.root / "data"))
    monkeypatch.setenv("HERNESS_PROFILE", "local")
    monkeypatch.setenv("HERNESS_SET_OVERRIDES", json.dumps(["logging.level=WARNING"]))
    monkeypatch.setattr(_worker_boot, "_HANDLERS", {})
    events: list[str] = []

    def spy(name: str) -> Callable[..., None]:
        def fn(*args: Any, **kwargs: Any) -> None:
            events.append(name)
            if name == "admin":
                events.append("purge" if callable(kwargs["memory_purge"]) else "bad")

        return fn

    def reg(kind: str, handler: object) -> None:
        events.append(f"register:{kind}")

    monkeypatch.setattr(herness.core.egress_socket, "install_socket_guard", spy("guard"))
    monkeypatch.setattr(herness.store.ops.resilience, "bind_core_backends", spy("ports"))
    monkeypatch.setattr(cli, "run_startup_validation", spy("validate"))
    monkeypatch.setattr(herness.admin, "register_handlers", spy("admin"))
    monkeypatch.setattr(herness.connectors.jobs, "register_job_handlers", spy("connectors"))
    monkeypatch.setattr(herness.eval.runner, "register_handlers", spy("eval-owner"))
    monkeypatch.setattr(herness.eval.runner._Binding, "factory", None)  # bound by T09-24
    monkeypatch.setattr(_eval_deps._Root, "path", None)
    monkeypatch.setattr(herness.harness.memory, "register_memory_components", spy("memory"))
    monkeypatch.setattr(herness.harness.swarm.handler, "register_handlers", spy("review"))
    monkeypatch.setattr(herness.harness.memory, "get_memory_store", object)
    monkeypatch.setattr(herness.harness.tools, "tool_registry", object)
    monkeypatch.setattr(herness.core.jobs.handlers, "register_handler", reg)
    return cli_env, events


def test_ut09_101_worker_bootstrap_order_and_idempotence(
    boot_env: tuple[CliEnv, list[str]],
) -> None:
    """UT09-101 guard first, ports bound, validation called, each handler registered once."""
    _env, events = boot_env
    cli.worker_bootstrap()
    first = list(events)
    assert first[0] == "guard"
    assert first.index("ports") < first.index("validate") < first.index("admin")
    for kind in ("build_pipeline", "distill", "chat", "eval"):  # eval: T09-24 run_eval_job
        assert first.count(f"register:{kind}") == 1
    for owner in ("admin", "connectors", "memory", "review"):
        assert first.count(owner) == 1
    assert "purge" in first
    assert "eval-owner" not in first  # replaced by run_eval_job (T09-24 ruling r2)
    assert herness.eval.runner._Binding.factory is _eval_deps.make_deps  # T09-24
    cli.worker_bootstrap()
    assert events == first  # the second call changes nothing


def test_ut09_101_config_follows_environment(boot_env: tuple[CliEnv, list[str]]) -> None:
    """UT09-101 init_config reads profile, data dir and overrides from the environment."""
    env, _events = boot_env
    cli.worker_bootstrap()
    cfg = get_config()
    assert cfg.profile == "local"
    assert cfg.logging.level == "WARNING"
    assert Path(cfg.paths.data) == env.root / "data"


def test_ut09_101_absent_variables_use_defaults(
    boot_env: tuple[CliEnv, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-101 an absent variable means the default (the config dir is then ./config)."""
    env, _events = boot_env
    for name in ("HERNESS_DATA_DIR", "HERNESS_PROFILE", "HERNESS_SET_OVERRIDES"):
        monkeypatch.delenv(name)
    monkeypatch.delenv("HERNESS_CONFIG_DIR")
    monkeypatch.chdir(env.root)
    cli.worker_bootstrap()  # ./config exists under the temp root


@pytest.mark.parametrize("raw", ["not json", '{"a": 1}', "[1, 2]", '["ok", 3]'])
def test_ut09_101_bad_overrides_are_config_error(
    boot_env: tuple[CliEnv, list[str]], monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """UT09-101 HERNESS_SET_OVERRIDES must be a JSON list of strings (ConfigError)."""
    _env, events = boot_env
    monkeypatch.setenv("HERNESS_SET_OVERRIDES", raw)
    with pytest.raises(ConfigError):
        cli.worker_bootstrap()
    assert "ports" not in events


def test_ut09_101_memory_purge_calls_the_store(
    boot_env: tuple[CliEnv, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-101 memory_purge is one module-level callable that calls MemoryStore.purge."""

    calls: list[str] = []

    class Store:
        def purge(self, *, record_id: str) -> int:
            calls.append(record_id)
            return 3

    monkeypatch.setattr(memory, "get_memory_store", Store)
    assert _worker_boot.memory_purge("rec1") == 3
    assert calls == ["rec1"]


def test_ut09_101_llm_factory_is_lazy_and_routes_roles(
    boot_env: tuple[CliEnv, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-101 building the factory builds no client; a call routes the role (R-05)."""
    made: list[str] = []

    class Reg:
        def __init__(self, models: object, *, profile: str) -> None:
            made.append(f"init:{profile}")

        def model_for(self, role: str, depth: str) -> str:
            return f"key-{role}"

        def client(self, key: str) -> str:
            return f"client-{key}"

        def config(self, key: str) -> Any:
            return type("C", (), {"model": "m-1", "max_concurrency": 4})()

    monkeypatch.setattr(registry, "LLMRegistry", Reg)
    cli.worker_bootstrap()
    assert made == []  # nothing built at bootstrap
    assert _worker_boot.llm_factory("enrich_decider") == ("client-key-enrich_decider", "m-1", 4)
    assert made == ["init:local"]


def test_ut09_101_handlers_built_once_per_process(
    boot_env: tuple[CliEnv, list[str]],
) -> None:
    """UT09-101 build_pipeline and distill handlers are cached, so a re-registration matches."""
    first = _worker_boot.cached_handlers()
    assert _worker_boot.cached_handlers() == first
    assert set(first) == {"build_pipeline", "distill"}
