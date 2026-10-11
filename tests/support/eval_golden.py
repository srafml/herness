"""tests.support.eval_golden: the mock golden eval environment (impl 11 T11-32, U11-75).

`scored_build(seed, scale, base)` makes a synthetic build the golden suite can run on: the
generated lake built by the real spec 02 build, the truth-planted enrichment of
`tests.support.metrics_synth` and the real spec 04 scoring (the `tiny_build` / `small_build`
fixtures keep the T02-22 fake score hook, so their builds have no `score.*` tables; T11-34
note). The funding window is 1,095 days (SN-1 of IT04-08, CO-T04-22a) and `meta.build`
says `synthetic` (`metrics_synth` builds under the `local` profile, which records `real`).

`golden_env(build, work)` gives a test or a script one eval setup: the build's warehouse
file, `CURRENT` and truth copied under `work`, the full test config with `paths.data` there,
a migrated ops store with the spec 08 backends, a fixed-key redactor and the warehouse and
memory tools. `GoldenEnv.handle(payload)` binds `deps` (the composition the `herness eval`
factory of T09-24 will do) and runs `handle_eval` in-process (the `--inline` path, R-45).

`deps(ctx)` builds the real `ChatService`, `Swarm` and `RubricJudge`; with `mock_llm_dir`
every one of them answers from one `ScriptedRegistry` over the payload's script book. Chat
calls resolve to the dedup key `chat` (design 11 §5.2), review calls to their task's dedup
key, judge calls to the question id. Rendering is a no-op (the eval reads `draft.json`).
"""

from __future__ import annotations

import contextlib
import dataclasses
import shutil
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Final

import duckdb
from tests.support.dispatch_standin import use_test_config
from tests.support.metrics_synth import SynthEnv, generate_root, release
from tests.support.metrics_synth import scored_build as _scored_build
from tests.unit.eval._runner_env import FakeCtx

from herness.core import config as herness_config
from herness.core import redact as r
from herness.core import time as clock
from herness.core.config import HernessConfig
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import bind_chain_registry
from herness.core.settings import RedactionConfig
from herness.eval import runner
from herness.eval.judge import RubricJudge
from herness.eval.runner import EvalDeps
from herness.eval.scripted import load_scripts
from herness.eval.scripted_client import DedupKeyResolver, ScriptedLLMClient, ScriptedRegistry
from herness.eval.settings import EvalConfig
from herness.eval.truth import TruthManifest, load_truth
from herness.harness import warehouse_tools
from herness.harness.llm.registry import LLMRegistry
from herness.harness.memory import MemoryStore, register_memory_components
from herness.harness.memory._facade import Embedder, RelatednessCache, VectorIndex
from herness.harness.pipelines.chat import ChatDeps, ChatService
from herness.harness.swarm.run import Swarm, SwarmDeps
from herness.harness.tools import tool_registry
from herness.harness.verifier import Verifier
from herness.harness.warehouse import WarehousePool
from herness.store import ops
from herness.store.ops import evidence
from herness.store.ops.migrate import migrate
from herness.store.ops.resilience import bind_core_backends

__all__ = [
    "FUNDING_WINDOW_DAYS",
    "GoldenBuild",
    "GoldenEnv",
    "chat_or_task_key",
    "golden_env",
    "scored_build",
]

REPO: Final = Path(__file__).resolve().parents[2]
SUITE: Final = REPO / "tests" / "eval" / "golden.yaml"
MOCK_SCRIPTS: Final = REPO / "tests" / "eval" / "mock_scripts"
WRITER_DEAD_SCRIPTS: Final = REPO / "tests" / "eval" / "mock_scripts_writer_dead"
FUNDING_WINDOW_DAYS: Final = 1095  # SN-1 of IT04-08 (CO-T04-22a): E2 links sit at start + 30 d
_KEY: Final = bytes(range(32))


@dataclasses.dataclass(frozen=True)
class GoldenBuild:
    """A scored synthetic build: the generated root, its build id and its truth."""

    root: Path
    build_id: str
    truth: TruthManifest

    @property
    def data_root(self) -> Path:
        return self.root / "data"

    @property
    def warehouse(self) -> Path:
        return self.data_root / "warehouse" / f"wh-{self.build_id}.duckdb"


def scored_build(seed: int, scale: str, base: Path) -> GoldenBuild:
    """Generate `seed`/`scale` under `base`, build, plant the enrichment and score it."""
    root = generate_root(seed, scale, base / "synth")
    env = SynthEnv.create(root, base / "work", funding_window_days=FUNDING_WINDOW_DAYS)
    try:
        build = _scored_build(env)
    finally:
        release()
    con = duckdb.connect(str(env.layout.warehouse / f"wh-{build.build_id}.duckdb"))
    try:  # the build is synthetic; the `local` profile recorded `real`
        con.execute("UPDATE meta.build SET dataset_kind = 'synthetic'")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return GoldenBuild(root, build.build_id, load_truth(root / "truth"))


def chat_or_task_key(resolve: Callable[[str | None], Any]) -> DedupKeyResolver:
    """The review task's dedup key; None for the one task of a chat run, so chat calls take
    the key `chat` of design 11 §5.2 (U11-40 step 2)."""

    def key(task_id: str | None) -> str | None:
        row = resolve(task_id)
        if row is None or row.spec.role == "chat":
            return None
        return str(row.spec.dedup_key)

    return key


def _no_render(run_id: str, formats: object) -> None:
    del run_id, formats


@dataclasses.dataclass
class GoldenEnv:
    """One eval setup over a copy of a scored build (module doc)."""

    build: GoldenBuild
    work: Path
    cfg: HernessConfig
    settings: EvalConfig
    repo_root: Path
    chat_verifier: Any | None = None  # ST11-05: a Verifier that lets every answer through
    pools: list[WarehousePool] = dataclasses.field(default_factory=list)

    @property
    def data_root(self) -> Path:
        return self.work / "data"

    def out_dir(self, run_id: object) -> Path:
        return self.data_root / "reports" / "eval" / str(run_id)

    def registry(self, mock_dir: str | None) -> LLMRegistry:
        inner = LLMRegistry(self.cfg.models, profile=self.cfg.profile, egress_enabled=True)
        if mock_dir is None:
            return inner
        book = load_scripts(Path(mock_dir))
        client = ScriptedLLMClient(book, dedup_key_resolver=chat_or_task_key(ops.get_task))
        return ScriptedRegistry(inner, client)  # type: ignore[return-value]

    def deps(self, ctx: Any) -> EvalDeps:
        """The composition of one eval job (the T09-24 factory's job, done test-side)."""
        payload: Mapping[str, Any] = ctx.job.payload
        llms = self.registry(payload.get("mock_llm_dir"))
        bind_chain_registry(llms)
        memory = MemoryStore(
            self.cfg.memory, conn_factory=ops.connection, vectors=VectorIndex(),
            embedder=Embedder(model_name=self.cfg.decisions.embedding.model),
            redactor=r.get_redactor(), llms=llms,
            allowed_numeral_patterns=self.cfg.app.reports.allowed_numeral_patterns,
            data_root=self.data_root, relatedness=RelatednessCache(),
        )  # fmt: skip
        register_memory_components(tool_registry(), memory)
        # One warehouse pool for chat, swarm and the runner: DuckDB shares one instance per
        # file and process, and a second pool's hardening `SET` fails on its locked config.
        pool = WarehousePool(self.data_root / "warehouse", self.cfg.models.harness.sql)
        self.pools.append(pool)
        shared = {
            "warehouses": pool,
            "verifier": Verifier(evidence, pool, self.cfg.models.harness.verifier),
        }
        chat_deps = dataclasses.replace(ChatDeps.from_config(self.cfg), **shared)
        if self.chat_verifier is not None:
            chat_deps = dataclasses.replace(chat_deps, verifier=self.chat_verifier)
        chat = ChatService(self.cfg.pipelines, llms, memory, deps=chat_deps)
        swarm_deps = dataclasses.replace(SwarmDeps.from_config(self.cfg), render=_no_render,
                                         **shared)  # fmt: skip
        swarm = Swarm(self.cfg.pipelines, llms, memory, deps=swarm_deps)
        key = self.settings.judge.profile
        judge = RubricJudge(
            llms.client(key), llms.config(key), cache_dir=self.work / "judge",
            temperature=self.settings.judge.temperature, tracer=None, redact=r.redact_text,
        )  # fmt: skip
        return EvalDeps(
            warehouse_for=lambda build_id: pool.get(build_id).cursor(),
            swarm_for=lambda profile: swarm,
            chat_for=lambda profile: chat, judge_for=lambda: judge, memory=memory, job=ctx,
            clock=clock.now, data_root=self.data_root, settings=self.settings,
            repo_root=self.repo_root,
        )  # fmt: skip

    def handle(self, payload: dict[str, Any]) -> tuple[FakeCtx, Any]:
        """Bind `deps`, run `handle_eval` in-process (the `--inline` path, R-45)."""
        ctx = FakeCtx(payload)
        runner.set_deps_factory(self.deps)
        return ctx, runner.handle_eval(ctx)  # type: ignore[arg-type]


def _copy_build(build: GoldenBuild, work: Path) -> None:
    wh = work / "data" / "warehouse"
    wh.mkdir(parents=True)
    shutil.copyfile(build.warehouse, wh / build.warehouse.name)
    (wh / "CURRENT").write_text(build.build_id, encoding="ascii")
    shutil.copytree(build.root / "truth", work / "truth")


@contextlib.contextmanager
def golden_env(
    build: GoldenBuild, work: Path, *, repo_root: Path | None = None,
    chat_verifier: Any | None = None,
) -> Iterator[GoldenEnv]:  # fmt: skip
    """One eval setup under `work` (module doc); process state is restored afterwards."""
    _copy_build(build, work)
    data = (work / "data").as_posix()
    cfg = use_test_config(work / "cfgroot", (f"paths.data={data}",))
    previous = r._State.redactor
    directory = NameDirectory.from_files(None, (), None)
    r._State.redactor = r.Redactor(RedactionConfig(directory_file=None), _KEY, directory)
    ops.reset_connections(path=work / "data" / "ops.sqlite")
    env = GoldenEnv(build, work, cfg, EvalConfig.model_validate(_eval_section()),
                    repo_root or work, chat_verifier)  # fmt: skip
    try:
        migrate()
        bind_core_backends()
        warehouse_tools.register_warehouse_tools()
        yield env
    finally:
        for pool in env.pools:
            pool.close_all()
        runner.set_deps_factory(None)  # type: ignore[arg-type]
        r._State.redactor = previous
        ops.reset_connections()
        herness_config.reset_config()


def _eval_section() -> dict[str, Any]:
    """`config/eval.yaml` without its `version` key (the U11-52 model)."""
    import yaml  # noqa: PLC0415 - only here

    raw = yaml.safe_load((REPO / "config" / "eval.yaml").read_text(encoding="utf-8"))
    raw.pop("version")
    return dict(raw)
