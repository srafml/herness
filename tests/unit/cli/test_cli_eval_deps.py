"""Tests for herness._cli._eval_deps, the composition root of `eval` jobs (T09-24, U09-98).

The collaborators (warehouse pool, verifier, swarm and chat deps, the services themselves,
the model registry, the judge and the memory store) are replaced by recording fakes on their
modules, which the factory imports lazily; the loaded config is the `cli_env` config tree.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support.cli_env import CliEnv
from tests.unit.eval._runner_env import FakeCtx

import herness.core.config
import herness.core.jobs.handlers
import herness.core.resilience
import herness.eval.judge
import herness.harness.llm.registry
import herness.harness.memory
import herness.harness.pipelines.chat
import herness.harness.swarm.run
import herness.harness.verifier
import herness.harness.warehouse
from herness._cli import _eval_deps
from herness.core.config import init_config
from herness.core.errors import ConfigError
from herness.core.types import JobOutcome
from herness.eval import runner
from herness.eval.scripted_client import ScriptedLLMClient, ScriptedRegistry

pytestmark = pytest.mark.unit

JUDGE_SCRIPT = "- match: {model_role: eval_judge}\n  turns:\n    - final: {text: ok}\n"


@dataclasses.dataclass(frozen=True)
class FakeDeps:
    """Stands for SwarmDeps / ChatDeps: the two fields the factory replaces."""

    warehouses: object
    verifier: object


class Pool:
    def __init__(self, warehouse_dir: Path, sql: object) -> None:
        self.dir, self.sql = warehouse_dir, sql
        self.closed = 0

    def close_all(self) -> None:
        self.closed += 1

    def get(self, build_id: str) -> SimpleNamespace:
        return SimpleNamespace(cursor=lambda: f"cursor:{build_id}")


class Registry:
    def __init__(self, cfg: object, *, profile: str) -> None:
        self.profile = profile

    def client(self, name: str) -> str:
        return f"client:{name}"

    def config(self, name: str) -> SimpleNamespace:
        return SimpleNamespace(name=name, model="judge-model")


@dataclasses.dataclass
class World:
    """What the fakes recorded."""

    built: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = dataclasses.field(
        default_factory=list
    )
    chains: list[object] = dataclasses.field(default_factory=list)
    store: object = dataclasses.field(default_factory=object)
    closed: list[object] = dataclasses.field(default_factory=list)


@pytest.fixture
def world(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> World:
    """The cli_env config loaded; every heavy collaborator a recording fake."""
    init_config(None, (), cli_env.write_config())
    found = World()

    def record(name: str) -> Any:
        def make(*args: Any, **kwargs: Any) -> SimpleNamespace:
            found.built.append((name, args, kwargs))
            made = SimpleNamespace(name=name, args=args, kwargs=kwargs)
            made.close = lambda: found.closed.append(made)
            return made

        return make

    deps = FakeDeps(warehouses="own-pool", verifier="own-verifier")
    monkeypatch.setattr(herness.harness.warehouse, "WarehousePool", Pool)
    monkeypatch.setattr(herness.harness.verifier, "Verifier", record("verifier"))
    monkeypatch.setattr(herness.harness.swarm.run.SwarmDeps, "from_config", lambda cfg: deps)
    monkeypatch.setattr(herness.harness.pipelines.chat.ChatDeps, "from_config", lambda cfg: deps)
    monkeypatch.setattr(herness.harness.swarm.run, "Swarm", record("swarm"))
    monkeypatch.setattr(herness.harness.pipelines.chat, "ChatService", record("chat"))
    monkeypatch.setattr(herness.eval.judge, "RubricJudge", record("judge"))
    monkeypatch.setattr(herness.harness.llm.registry, "LLMRegistry", Registry)
    monkeypatch.setattr(herness.harness.memory, "get_memory_store", lambda: found.store)
    monkeypatch.setattr(herness.core.resilience, "bind_chain_registry", found.chains.append)
    monkeypatch.setattr(_eval_deps._Root, "path", None)
    monkeypatch.setattr(_eval_deps._Registries, "key", None)
    monkeypatch.setattr(_eval_deps._Jobs, "open", {})
    return found


def _ctx(**payload: Any) -> FakeCtx:
    return FakeCtx({"profile": "local", "repeat": 1, **payload})


def test_cv_t09_24_bind_installs_factory_and_teardown_handler(
    cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T09-24 `bind`: set_deps_factory(make_deps), then `run_eval_job` registered for
    `eval` (idempotent); the checkout root is the config directory's parent."""
    order: list[object] = []
    monkeypatch.setattr(runner._Binding, "factory", None)
    monkeypatch.setattr(_eval_deps._Root, "path", None)
    monkeypatch.setattr(runner, "set_deps_factory", lambda f: order.append(f.__name__))
    monkeypatch.setattr(herness.core.jobs.handlers, "register_handler",
                        lambda kind, h: order.append((kind, h)))  # fmt: skip
    _eval_deps.bind(Path("config"))
    assert order == ["make_deps", ("eval", _eval_deps.run_eval_job)]
    assert _eval_deps._Root.path == cli_env.root.resolve()


def test_cv_t09_24_make_deps_shares_one_pool_and_verifier_per_job(
    world: World, cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T09-24 one WarehousePool and one Verifier per job serve warehouse_for, the swarm and
    the chat service; one swarm per profile within the job; registries are process-wide."""
    monkeypatch.setattr(_eval_deps._Root, "path", cli_env.root)
    ctx = _ctx()
    deps = _eval_deps.make_deps(ctx)  # type: ignore[arg-type]
    assert [b[0] for b in world.built] == ["verifier"]  # nothing else built yet
    pool = world.built[0][1][1]
    assert isinstance(pool, Pool)
    assert deps.warehouse_for("b1") == "cursor:b1"
    swarm, chat = deps.swarm_for("hybrid"), deps.chat_for("premium")
    assert deps.swarm_for("hybrid") is swarm
    assert swarm.kwargs["deps"].warehouses is pool
    assert swarm.kwargs["deps"].verifier.name == "verifier"
    assert chat.kwargs["deps"].warehouses is pool
    assert swarm.args[1].profile == "hybrid"
    assert chat.args[1].profile == "premium"
    assert swarm.args[2] is world.store
    assert world.chains == [chat.args[1]]  # the chat mode policy reads the chain registry
    assert (deps.memory, deps.job, deps.repo_root) == (None, ctx, cli_env.root)
    assert deps.data_root == Path(herness.core.config.get_config().paths.data)
    assert deps.settings is herness.core.config.get_config().eval
    other = _eval_deps.make_deps(_ctx())  # type: ignore[arg-type]
    assert other.swarm_for("hybrid") is not swarm  # a new job, its own swarm and pool
    assert other.swarm_for("hybrid").args[1] is swarm.args[1]  # ... one registry per process


def test_cv_t09_24_run_eval_job_closes_the_jobs_resources(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T09-24 (review r2 ruling) `run_eval_job` calls `handle_eval` and, in `finally`,
    closes every swarm the job built and its pool, also when the handler raises."""
    ctx = _ctx()
    built: list[Any] = []

    def handle(job_ctx: Any) -> JobOutcome:
        deps = _eval_deps.make_deps(job_ctx)
        built.extend([deps.swarm_for("local"), deps.swarm_for("hybrid")])
        built.append(_eval_deps._Jobs.open[job_ctx.job_id].pool)
        return JobOutcome(status="done", result={"run_id": "r"})

    monkeypatch.setattr(runner, "handle_eval", handle)
    assert _eval_deps.run_eval_job(ctx).status == "done"  # type: ignore[arg-type]
    assert world.closed == built[:2]
    assert built[2].closed == 1
    assert _eval_deps._Jobs.open == {}

    def boom(job_ctx: Any) -> JobOutcome:
        _eval_deps.make_deps(job_ctx).swarm_for("local")
        msg = "suite missing"
        raise ConfigError(msg)

    monkeypatch.setattr(runner, "handle_eval", boom)
    with pytest.raises(ConfigError):
        _eval_deps.run_eval_job(ctx)  # type: ignore[arg-type]
    assert len(world.closed) == 3
    assert _eval_deps._Jobs.open == {}
    _eval_deps.close_job("never-built")  # no-op


def test_cv_t09_24_judge_uses_eval_judge_client_and_root_cache(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CV-T09-24 the judge: `eval.judge.profile` client on the loaded profile, temperature
    from eval.yaml, a relative cache dir under the checkout root, impl 10 redaction."""
    from herness.core.redact import redact_text  # noqa: PLC0415

    judge = _eval_deps.make_deps(_ctx()).judge_for()  # type: ignore[arg-type]
    settings = herness.core.config.get_config().eval
    assert settings is not None
    assert judge.args == ("client:local-judge", judge.args[1])
    assert judge.args[1].name == "local-judge"
    assert judge.kwargs["cache_dir"] == Path.cwd() / "data" / "cache" / "judge"
    assert judge.kwargs["temperature"] == settings.judge.temperature
    assert judge.kwargs["redact"] is redact_text
    assert judge.kwargs["tracer"] is None
    absolute = tmp_path / "cache"
    patched = settings.model_copy(update={"judge": settings.judge.model_copy(
        update={"cache_dir": str(absolute)})})  # fmt: skip
    cfg = herness.core.config.get_config().model_copy(update={"eval": patched})
    monkeypatch.setattr(herness.core.config, "get_config", lambda: cfg)
    again = _eval_deps.make_deps(_ctx())  # type: ignore[arg-type]
    assert again.judge_for().kwargs["cache_dir"] == absolute


def test_cv_t09_24_mock_llm_wraps_registries_in_scripted_registry(
    world: World, tmp_path: Path
) -> None:
    """CV-T09-24 `--mock-llm`: swarm, chat and judge registries are ScriptedRegistry over the
    job's own book; swarm and judge calls resolve the ops task's dedup key, chat is `chat`."""
    mock = tmp_path / "mock"
    mock.mkdir()
    (mock / "judge.yaml").write_text(JUDGE_SCRIPT, encoding="utf-8")
    deps = _eval_deps.make_deps(_ctx(mock_llm_dir=str(mock)))  # type: ignore[arg-type]
    swarm, chat = deps.swarm_for("local"), deps.chat_for("local")
    registries = (swarm.args[1], chat.args[1])
    assert all(isinstance(r, ScriptedRegistry) for r in registries)
    swarm_client, chat_client = (r.client("anything") for r in registries)
    assert isinstance(swarm_client, ScriptedLLMClient)
    assert swarm_client._resolver is not None
    assert chat_client._resolver is None
    assert isinstance(deps.judge_for().args[0], ScriptedLLMClient)
    other = _eval_deps.make_deps(_ctx(mock_llm_dir=str(mock)))  # type: ignore[arg-type]
    other_client = other.swarm_for("local").args[1].client("x")
    assert other_client._book is not swarm_client._book  # each job its own book
    assert world.chains
    assert not isinstance(world.chains[0], ScriptedRegistry)


def test_cv_t09_24_registries_rebuilt_for_a_new_config(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T09-24 the process-wide registries follow the config object (no DuckDB handle)."""
    first = _eval_deps.make_deps(_ctx()).swarm_for("local").args[1]  # type: ignore[arg-type]
    cfg = herness.core.config.get_config().model_copy()
    monkeypatch.setattr(herness.core.config, "get_config", lambda: cfg)
    second = _eval_deps.make_deps(_ctx()).swarm_for("local").args[1]  # type: ignore[arg-type]
    assert second is not first


def test_cv_t09_24_make_deps_needs_eval_settings(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CV-T09-24 no `eval` section → ConfigError; a bad mock directory → ConfigError (3)."""
    cfg = herness.core.config.get_config()
    monkeypatch.setattr(herness.core.config, "get_config",
                        lambda: cfg.model_copy(update={"eval": None}))  # fmt: skip
    with pytest.raises(ConfigError, match="eval settings are not configured"):
        _eval_deps.make_deps(_ctx())  # type: ignore[arg-type]
    monkeypatch.setattr(herness.core.config, "get_config", lambda: cfg)
    with pytest.raises(ConfigError):
        _eval_deps.make_deps(_ctx(mock_llm_dir=str(tmp_path / "none")))  # type: ignore[arg-type]
    assert _eval_deps._Jobs.open == {}
