"""UT09-77: dashboard composition root (U09-53)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.support.cli_env import USER_REF_KEY, CliEnv

from app.common import bootstrap
from herness.core import config_validate
from herness.core.config_view import ConfigIssue
from herness.core.errors import ConfigError
from herness.harness.llm import registry as llm_registry
from herness.harness.pipelines import chat as chat_module
from herness.reports.rules import user_ref_for

pytestmark = pytest.mark.unit


@pytest.fixture
def boot(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Env for `herness ui`, spies on the guard, port binding and start-up hook; fake harness."""
    events: list[str] = []
    cfg_dir = cli_env.write_config(admins=["ops-lead"])
    monkeypatch.setenv("HERNESS_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("HERNESS_DATA_DIR", str(cli_env.root / "data"))
    monkeypatch.setenv("HERNESS_SET_OVERRIDES", '["logging.level=DEBUG"]')
    monkeypatch.setattr(bootstrap, "_guard_installed", [])
    monkeypatch.setattr(bootstrap, "install_socket_guard", lambda _b: events.append("guard"))
    monkeypatch.setattr(bootstrap, "bind_core_backends", lambda: events.append("bind"))

    def hook(cfg: object, *, offline: bool) -> list[ConfigIssue]:
        del cfg, offline
        events.append("hook")
        return []

    monkeypatch.setattr(config_validate, "run_owner_validators", hook)
    memory = SimpleNamespace(name="memory")
    monkeypatch.setattr("herness.harness.memory.get_memory_store", lambda: memory)
    monkeypatch.setattr(
        llm_registry, "LLMRegistry", lambda models, *, profile: ("llms", models, profile)
    )

    class FakeChat:
        def __init__(self, cfg: object, llms: object, mem: object) -> None:
            self.args = (cfg, llms, mem)

    monkeypatch.setattr(chat_module, "ChatService", FakeChat)
    return events


def test_ut09_77_builds_services_in_order(boot: list[str]) -> None:
    """UT09-77 guard, then ports bound before the start-up hook; services wired from config."""
    services = bootstrap.build_services()
    assert boot == ["guard", "bind", "hook"]
    assert services.cfg.logging.level == "DEBUG"  # HERNESS_SET_OVERRIDES applied
    assert services.cfg.paths.data.name == "data"
    assert services.memory.name == "memory"  # type: ignore[attr-defined]
    cfg_part, llms, memory = services.chat.args  # type: ignore[attr-defined]
    assert cfg_part is services.cfg.pipelines
    assert llms == ("llms", services.cfg.models, services.cfg.profile)
    assert memory is services.memory
    ref = user_ref_for("ops-lead", USER_REF_KEY.encode())
    assert dict(services.roster) == {ref: "ops-lead"}
    assert "build.meta" in services.queries  # step 10: named queries loaded and guarded (T09-14)
    assert services.queries["org.list"].row_cap == services.cfg.app.app.page_row_limit
    assert services.started_at.tzinfo is not None


def test_ut09_77_guard_installed_once(boot: list[str]) -> None:
    """UT09-77 repeated builds install the socket guard once; `get_services` is cached."""
    bootstrap.build_services()
    bootstrap.build_services()
    assert boot.count("guard") == 1
    assert boot.count("bind") == 2
    bootstrap.get_services.clear()
    try:
        first = bootstrap.get_services()
        assert bootstrap.get_services() is first
    finally:
        bootstrap.get_services.clear()
    assert boot.count("hook") == 3


@pytest.mark.parametrize("raw", ["not json", '{"a": 1}', '["ok", 2]', '"x"'])
def test_ut09_77_bad_set_overrides(
    boot: list[str], monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """UT09-77 bad `HERNESS_SET_OVERRIDES` -> ConfigError before anything is installed."""
    monkeypatch.setenv("HERNESS_SET_OVERRIDES", raw)
    with pytest.raises(ConfigError, match="HERNESS_SET_OVERRIDES"):
        bootstrap.build_services()
    assert boot == []


def test_ut09_77_startup_error_issue(boot: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-77 start-up hook returning an `error` issue -> ConfigError; ports bound first."""

    def failing(cfg: object, *, offline: bool) -> list[ConfigIssue]:
        del cfg, offline
        boot.append("hook")
        return [ConfigIssue("error", "metrics", "weights do not sum to 1", "weights.yaml")]

    monkeypatch.setattr(config_validate, "run_owner_validators", failing)
    with pytest.raises(ConfigError, match="start-up validation failed"):
        bootstrap.build_services()
    assert boot == ["guard", "bind", "hook"]


def test_ut09_77_defaults_without_environment(
    boot: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-77 no `herness ui` variables: `config/` of the working directory, no overrides."""
    for name in ("HERNESS_CONFIG_DIR", "HERNESS_DATA_DIR", "HERNESS_SET_OVERRIDES"):
        monkeypatch.delenv(name)
    services = bootstrap.build_services()  # cli_env's working directory holds the tree
    assert services.cfg.logging.level == "INFO"
    assert boot == ["guard", "bind", "hook"]
