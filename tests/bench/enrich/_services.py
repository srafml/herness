"""Real-service helpers of the model rows BT03-02..BT03-05 and BT03-07 naming (T03-36).

Nothing here starts a service: each helper turns the environment variables the controller sets
on the target PC into a configured object, and fails loudly (never silently) when a variable
names something unusable. The variables (all `HERNESS_BENCH_*`, read through `bench_flag`):

* `HERNESS_BENCH_EMBED_MODEL_DIR`: the bge-m3 sentence-transformers directory (safetensors).
* `HERNESS_BENCH_LAYA_DATA`: a data root whose `models/laya/CURRENT` names a verified Laya
  version (the same layout `HERNESS_IT_LAYA_DATA` uses in IT03-16); needs the `laya` package.
* `HERNESS_BENCH_OPENJEV_URL`: the loopback base URL of the running OpenJev service.
* `HERNESS_BENCH_LLM_PROFILE`: the config profile (`config/profiles/*.yaml`) whose
  `models.yaml` roles `enrich_decider` / `cluster_namer` point at the running local LLM;
  `HERNESS_BENCH_CONFIG_DIR` optionally names another config directory than the repo's.
"""

from __future__ import annotations

import atexit
import re
import shutil
import tempfile
from pathlib import Path
from typing import Final

import pytest
from tests.bench.enrich._common import BENCH_CONFIG_ENV
from tests.support.bench import bench_flag

from herness.core import config as herness_config
from herness.core.config import HernessConfig
from herness.core.resilience import bind_ops_backend
from herness.core.types import QuestionSet
from herness.enrich.deciders.llm import CompletionClient
from herness.enrich.questions import PAIR_QUESTIONS, load_question_set
from herness.store.ops import reset_connections
from herness.store.ops.migrate import migrate
from herness.store.ops.resilience import SqliteResilienceBackend

__all__ = ["llm_client", "load_shipped_config", "shipped_questions"]

_REPO: Final = Path(__file__).resolve().parents[3]
_VERSION_CHARS: Final = re.compile(r"[^A-Za-z0-9._:/@+-]")


def load_shipped_config(config: pytest.Config, profile: str = "local") -> HernessConfig:
    """Load the shipped config tree (or `HERNESS_BENCH_CONFIG_DIR`) for `profile`, with
    `paths.data` in a temp folder, and bind the resilience backend to a fresh ops store there
    (the deciders' breakers and retry policies need it); ConfigError (a failed row) when the
    config does not load."""
    override = bench_flag(config, BENCH_CONFIG_ENV)
    config_dir = Path(override) if override else _REPO / "config"
    herness_config.reset_config()
    data = Path(tempfile.mkdtemp(prefix="bt03-data-"))
    atexit.register(shutil.rmtree, data, ignore_errors=True)
    cfg = herness_config.init_config(
        profile, config_dir=config_dir, env={"HERNESS_PATHS__DATA": str(data)}
    )
    reset_connections(path=data / "ops.sqlite")
    migrate()
    bind_ops_backend(SqliteResilienceBackend())
    return cfg


def shipped_questions(cfg: HernessConfig) -> QuestionSet:
    """The configured question set (the shipped `config/decisions.yaml`) as the model rows
    use it: the five incident questions. The shipped `owning_team` takes its options from
    `core.team` at run time (no warehouse here), so it is given twelve static team options of
    the same kind; the pair question is dropped (it is never asked of a single incident)."""
    qs = load_question_set(cfg.decisions)
    teams = {f"team_{i:02d}": f"Team {i:02d} owns the affected service." for i in range(12)}
    questions = tuple(
        q.model_copy(update={"options": teams, "options_source": "static"})
        if q.options_source != "static"
        else q
        for q in qs.questions
        if "incident" in q.applies_to and q.id not in PAIR_QUESTIONS
    )
    return QuestionSet(version=qs.version, questions=questions)


def llm_client(
    config: pytest.Config, profile: str, *, role: str
) -> tuple[CompletionClient, str, int]:
    """The `LLMClient` for `role` of `profile` (spec 05 `client_for`, depth `standard`), a
    decider-version string `<client>/<model>` and the client's `max_concurrency` (the shape
    of impl 03's `LlmFactory`)."""
    from herness.harness.llm.registry import client_for  # noqa: PLC0415 - only for LLM rows

    load_shipped_config(config, profile)
    client, client_cfg = client_for(role, profile=profile, depth="standard")
    version = _VERSION_CHARS.sub("-", f"{client_cfg.name}/{client_cfg.model}")[:128]
    return client, version, client_cfg.max_concurrency  # type: ignore[return-value]
