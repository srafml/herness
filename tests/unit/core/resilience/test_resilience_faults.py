"""Tests for herness.core.resilience.faults: fault plans and fault_point (T08-08; U08-33/34)."""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import signal
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.support.fault_env import FaultEnv

from herness.core import errors as e
from herness.core import resilience
from herness.core.resilience import ProcessState, faults
from herness.core.resilience._state import NOT_LOADED

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, rules: object, name: str = "plan.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(rules), encoding="utf-8")
    return path


def _rule(**fields: object) -> dict[str, object]:
    return {"point": "llm.call", "action": "error:StoreBusy", **fields}


_FS_ENTRY_POINTS: tuple[tuple[object, str], ...] = (
    (builtins, "open"),
    (io, "open"),
    (os, "open"),
    (os, "stat"),
    (Path, "open"),
    (Path, "stat"),
    (Path, "is_symlink"),
    (Path, "resolve"),
    (Path, "exists"),
)


@contextlib.contextmanager
def _fs_spy() -> Iterator[list[tuple[str, object]]]:
    """Record (and refuse) every open/stat/is_symlink/resolve/exists call inside the block.

    Scoped to the calls under test (so pytest's own reporting is unaffected); a mutant that
    swallows the AssertionError is still caught by the record.
    """
    calls: list[tuple[str, object]] = []

    def make(attr: str) -> Callable[..., object]:
        def spy(*args: object, **_kwargs: object) -> object:
            calls.append((attr, args[0] if args else None))
            msg = f"file access ({attr}) in a test that forbids it"
            raise AssertionError(msg)

        return spy

    with pytest.MonkeyPatch.context() as patch:
        for target, attr in _FS_ENTRY_POINTS:
            patch.setattr(target, attr, make(attr))
        yield calls


def _fire_indices(calls: int, name: str = "llm.call", **labels: str) -> list[int]:
    fired: list[int] = []
    for index in range(1, calls + 1):
        try:
            faults.fault_point(name, **labels)
        except e.StoreBusy:
            fired.append(index)
    return fired


# --- exports -----------------------------------------------------------------------------


def test_ut08_46_package_reexports_fault_api() -> None:
    """UT08-46 the package re-exports fault_point, FaultRule, load_fault_plan, NAMED_POINTS."""
    assert resilience.fault_point is faults.fault_point
    assert resilience.FaultRule is faults.FaultRule
    assert resilience.load_fault_plan is faults.load_fault_plan
    assert resilience.NAMED_POINTS is faults.NAMED_POINTS
    assert len(faults.NAMED_POINTS) == 18


# --- UT08-46 inert without the variable ----------------------------------------------------


def test_ut08_46_no_variable_no_file_access(reset_process_state: ProcessState) -> None:
    """UT08-46 without HERNESS_FAULTS 1 000 calls touch no file and have no effect."""
    with structlog.testing.capture_logs() as logs, _fs_spy() as touched:
        for _ in range(1000):
            assert faults.fault_point("llm.call", model="m") is None
    assert touched == []
    assert reset_process_state.fault_plan is None
    assert reset_process_state.faults_enabled is False
    assert logs == []


def test_ut08_46_empty_variable_is_unset(
    reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-46 an empty HERNESS_FAULTS counts as unset."""
    monkeypatch.setenv("HERNESS_FAULTS", "")
    with _fs_spy() as touched:
        faults.fault_point("sqlite.write", kind="x")
    assert touched == []
    assert reset_process_state.fault_plan is None


def test_ut08_46_spy_catches_stat_only_access(tmp_path: Path) -> None:
    """UT08-46 the spy records every entry point (io.open, stat, is_symlink, ...), not only open."""
    path = _write(tmp_path, [_rule()])
    with _fs_spy() as touched:
        for target, attr in _FS_ENTRY_POINTS:
            with pytest.raises(AssertionError):
                getattr(target, attr)(path)
        with pytest.raises(AssertionError):
            path.is_symlink()  # a bound Path method goes through the spy too
    assert touched == [(attr, path) for _, attr in _FS_ENTRY_POINTS] + [("is_symlink", path)]
    assert path.is_symlink() is False  # restored after the block


# --- UT08-47 load_fault_plan ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("rules", "reason"),
    [
        ([_rule(point="nope.point")], "unknown fault point"),
        ([_rule(action="explode")], "unknown fault action"),
        ([_rule(nth=2, count=3)], "at most one of nth and count"),
        ([_rule(p=0.5)], "p requires seed"),
        ([_rule(nth=0)], "nth must be >= 1"),
        ([_rule(count=0)], "count must be >= 1"),
        ([_rule(p=0.0, seed=1)], "p must be in (0, 1]"),
        ([_rule(p=1.5, seed=1)], "p must be in (0, 1]"),
        ([_rule(retry_after=3)], "retry_after needs 429"),
        ([_rule(action="http_429", retry_after=-1)], "retry_after >= 0"),
        ([_rule(action="error:JobStateError")], "unknown fault action"),
        ([_rule(action="error:")], "unknown fault action"),
        ([_rule(action="delay:0")], "unknown fault action"),
        ([_rule(action="delay:600.5")], "unknown fault action"),
        ([_rule(action="delay:abc")], "unknown fault action"),
        ([_rule(action="kill_service:postgres")], "unknown fault action"),
        ([_rule(extra="x")], "extra"),
        ([_rule(nth="3")], "nth"),
        ([_rule(nth=True)], "nth"),
        ([{"action": "kill"}], "point"),
        ({"point": "llm.call"}, "must be a list"),
        ([_rule()] * 101, "must be a list of at most 100"),
        (["llm.call"], "rule 0: not a mapping"),
    ],
)
def test_ut08_47_invalid_plans_raise_config_error(
    tmp_path: Path, rules: object, reason: str
) -> None:
    """UT08-47 unknown point or action, nth+count, p without seed, … each raise ConfigError."""
    path = _write(tmp_path, rules)
    with pytest.raises(e.ConfigError) as info:
        faults.load_fault_plan(path)
    assert info.value.message.startswith("invalid fault plan: plan.json: ")
    assert reason in info.value.message
    assert str(tmp_path) not in info.value.message


def test_ut08_47_oversized_file(tmp_path: Path) -> None:
    """UT08-47 a 70 KB plan is refused."""
    path = tmp_path / "big.json"
    path.write_text(json.dumps([_rule(model="x" * 70_000)]), encoding="utf-8")
    with pytest.raises(e.ConfigError, match=r"big\.json: larger than 65536 bytes"):
        faults.load_fault_plan(path)


def test_ut08_47_limit_size_accepted(tmp_path: Path) -> None:
    """UT08-47 a plan of exactly 65 536 bytes still loads."""
    body = json.dumps([_rule()])
    path = tmp_path / "edge.json"
    path.write_bytes(body.encode() + b" " * (faults.MAX_PLAN_BYTES - len(body)))
    assert len(faults.load_fault_plan(path).rules) == 1


def test_ut08_47_yaml_suffix_refused(tmp_path: Path) -> None:
    """UT08-47 a valid plan saved with suffix .yaml is refused (R-40)."""
    path = _write(tmp_path, [_rule()], name="plan.yaml")
    with pytest.raises(e.ConfigError, match="JSON only"):
        faults.load_fault_plan(path)


def test_ut08_47_symlink_refused(tmp_path: Path) -> None:
    """UT08-47 a symlinked plan is refused (real symlink; skipped without the privilege)."""
    target = _write(tmp_path, [_rule()])
    link = tmp_path / "link.json"
    try:
        os.symlink(target, link)
    except OSError as exc:
        pytest.skip(f"platform denied symlink creation: {exc}")
    with pytest.raises(e.ConfigError, match=r"link\.json: symlink not allowed"):
        faults.load_fault_plan(link)


def test_ut08_47_symlink_check_without_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-47 the symlink branch, driven by a patched `Path.is_symlink` (no privilege needed)."""
    path = _write(tmp_path, [_rule()])
    monkeypatch.setattr(Path, "is_symlink", lambda _self: True)
    with pytest.raises(e.ConfigError, match="symlink not allowed"):
        faults.load_fault_plan(path)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"[{", "not valid JSON"),
        (b'[{"point": "llm.call", "action": "timeout", "p": NaN, "seed": 1}]', "not valid JSON"),
        (b"\xff\xfe[]", "not valid JSON"),
    ],
)
def test_ut08_47_unparsable_plan(tmp_path: Path, content: bytes, reason: str) -> None:
    """UT08-47 broken JSON, NaN constants and non-UTF-8 bytes raise ConfigError."""
    path = tmp_path / "plan.json"
    path.write_bytes(content)
    with pytest.raises(e.ConfigError, match=reason):
        faults.load_fault_plan(path)


def test_ut08_47_missing_and_directory(tmp_path: Path) -> None:
    """UT08-47 a missing path or a directory is not a plan."""
    with pytest.raises(e.ConfigError, match="unreadable"):
        faults.load_fault_plan(tmp_path / "absent.json")
    folder = tmp_path / "dir.json"
    folder.mkdir()
    with pytest.raises(e.ConfigError, match="not a regular file"):
        faults.load_fault_plan(folder)


def test_ut08_47_valid_plan_state(tmp_path: Path) -> None:
    """UT08-47 a valid plan has one zero counter per rule and a seeded RNG only with p."""
    rules = [
        _rule(nth=3),
        {"point": "sql.query", "action": "timeout", "p": 0.2, "seed": 7},
        {"point": "http.page", "action": "http_429", "retry_after": 7, "count": 3, "source": "j"},
        {"point": "gpu.after_stop", "action": "delay:1.5"},
        {"point": "gpu.after_start", "action": "kill_service:openjev"},
        {"point": "swarm.after_finding_write", "action": "kill", "nth": 5},
    ]
    plan = faults.load_fault_plan(_write(tmp_path, rules))
    assert plan.counters == [0] * 6
    assert [rng is None for rng in plan.rngs] == [True, False, True, True, True, True]
    assert plan.rules[2].retry_after == 7.0
    assert isinstance(plan.rules[0], faults.FaultRule)


# --- UT08-48 selectors ---------------------------------------------------------------------


def test_ut08_48_nth_fires_once(fault_env: FaultEnv) -> None:
    """UT08-48 nth: 3 fires on call 3 only."""
    fault_env([_rule(nth=3)])
    assert _fire_indices(10) == [3]


def test_ut08_48_count_fires_first_calls(fault_env: FaultEnv) -> None:
    """UT08-48 count: 2 fires on calls 1-2."""
    fault_env([_rule(count=2)])
    assert _fire_indices(10) == [1, 2]


def test_ut08_48_probability_is_reproducible(
    fault_env: FaultEnv, reset_process_state: ProcessState
) -> None:
    """UT08-48 p: 0.2, seed: 7 fires on the same call indices on two runs."""
    fault_env([_rule(p=0.2, seed=7)])
    first = _fire_indices(10)
    fault_env([_rule(p=0.2, seed=7)])
    second = _fire_indices(10)
    assert first == second
    assert 0 < len(first) < 10
    assert reset_process_state.faults_enabled is True


def test_ut08_48_label_filter(fault_env: FaultEnv) -> None:
    """UT08-48 label filter model: local-30b fires only for matching labels."""
    fault_env([_rule(model="local-30b")])
    assert _fire_indices(3, model="other") == []
    assert _fire_indices(3) == []
    assert _fire_indices(3, model="local-30b", role="r") == [1, 2, 3]


def test_ut08_48_first_firing_rule_wins(
    fault_env: FaultEnv, reset_process_state: ProcessState
) -> None:
    """UT08-48 rules run in order; the first firing rule wins and later ones are skipped."""
    fault_env([_rule(nth=2), _rule(action="error:AuthError"), _rule(action="error:NotFound")])
    with pytest.raises(e.AuthError):
        faults.fault_point("llm.call")
    with pytest.raises(e.StoreBusy):
        faults.fault_point("llm.call")
    faults.fault_point("sql.query")  # no rule on this point
    plan = reset_process_state.fault_plan
    assert isinstance(plan, faults.FaultPlan)
    assert plan.counters == [2, 1, 0]


def test_ut08_48_unknown_point_or_label_with_plan(fault_env: FaultEnv) -> None:
    """UT08-48 with a plan loaded, an unknown point or label key raises ConfigError."""
    fault_env([_rule()])
    with pytest.raises(e.ConfigError, match="unknown fault point"):
        faults.fault_point("no.such_point")
    with pytest.raises(e.ConfigError, match="unknown fault point or label"):
        faults.fault_point("llm.call", colour="red")


# --- UT08-49 actions -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("point", "action", "error"),
    [
        ("sql.query", "timeout", e.QueryError),
        ("http.page", "timeout", e.SourceUnavailable),
        ("sqlite.write", "timeout", e.StoreBusy),
        ("llm.call", "timeout", e.ModelUnavailable),
        ("http.page", "http_503", e.SourceUnavailable),
        ("sqlite.write", "http_503", e.StoreBusy),
        ("decider.batch", "http_503", e.ModelUnavailable),
        ("llm.output", "malformed_json", e.OutputValidationError),
    ],
)
def test_ut08_49_family_actions(
    fault_env: FaultEnv, point: str, action: str, error: type[e.HernessError]
) -> None:
    """UT08-49 timeout, http_503 and malformed_json raise the class of the U08-34 table."""
    fault_env([{"point": point, "action": action}])
    with pytest.raises(error) as info:
        faults.fault_point(point)
    assert type(info.value) is error
    assert info.value.message == f"fault: {action}"
    if point == "sql.query":
        assert info.value.context["timeout"] is True


@pytest.mark.parametrize("name", sorted(faults._LEAF_ERRORS))
def test_ut08_49_error_action_every_leaf_class(fault_env: FaultEnv, name: str) -> None:
    """UT08-49 error:<Class> raises that leaf class with message `fault: injected`."""
    fault_env([_rule(action=f"error:{name}")])
    with pytest.raises(e.HernessError) as info:
        faults.fault_point("llm.call")
    assert type(info.value).__name__ == name
    assert info.value.message == "fault: injected"
    if isinstance(info.value, e.CircuitOpen):
        assert info.value.key == "fault:llm.call"


def test_ut08_49_http_429_retry_after(fault_env: FaultEnv) -> None:
    """UT08-49 http_429 raises RateLimited carrying the rule's retry_after."""
    fault_env([{"point": "http.page", "action": "http_429", "retry_after": 7, "source": "jira"}])
    with pytest.raises(e.RateLimited) as info:
        faults.fault_point("http.page", source="jira")
    assert info.value.retry_after == 7.0


def test_ut08_49_delay_sleeps_on_fake_clock(
    fault_env: FaultEnv, reset_process_state: ProcessState
) -> None:
    """UT08-49 delay:<s> sleeps through process_state().sleep and returns."""
    slept: list[float] = []
    reset_process_state.sleep = slept.append
    fault_env([{"point": "embed.batch", "action": "delay:2.5"}])
    assert faults.fault_point("embed.batch") is None
    assert slept == [2.5]


def test_ut08_49_kill_signals_own_process(
    fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-49 kill logs CRITICAL and signals its own pid (os.kill patched: nothing dies)."""
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    fault_env([{"point": "job.after_claim", "action": "kill"}])
    with structlog.testing.capture_logs() as logs:
        faults.fault_point("job.after_claim")
    expected = getattr(signal, "SIGKILL", signal.SIGTERM)
    assert kills == [(os.getpid(), expected)]
    assert {"event": "resilience.faults.kill", "log_level": "critical"}.items() <= logs[-1].items()


def test_ut08_49_kill_uses_sigterm_without_sigkill(
    fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-49 on a platform without SIGKILL (Windows) kill falls back to SIGTERM."""
    kills: list[int] = []
    monkeypatch.setattr(os, "kill", lambda _pid, sig: kills.append(sig))
    monkeypatch.delattr(signal, "SIGKILL", raising=False)
    fault_env([{"point": "job.after_claim", "action": "kill"}])
    faults.fault_point("job.after_claim")
    assert kills == [signal.SIGTERM]


def test_ut08_49_kill_service_calls_hook(
    fault_env: FaultEnv, reset_process_state: ProcessState
) -> None:
    """UT08-49 kill_service:<svc> without stub services calls the kill_service_hook."""
    killed: list[str] = []
    reset_process_state.kill_service_hook = killed.append
    fault_env([{"point": "gpu.after_start", "action": "kill_service:openjev"}])
    faults.fault_point("gpu.after_start")
    assert killed == ["openjev"]


def test_ut08_49_kill_service_without_hook_warns(fault_env: FaultEnv) -> None:
    """UT08-49 kill_service with neither stub nor hook logs no_service_hook and returns."""
    fault_env([{"point": "gpu.after_start", "action": "kill_service:openjev"}])
    with structlog.testing.capture_logs() as logs:
        assert faults.fault_point("gpu.after_start") is None
    assert logs[-1]["event"] == "resilience.faults.no_service_hook"
    assert logs[-1]["service"] == "openjev"


class _Client:
    def __init__(self, posts: list[tuple[str, str]], base_url: str) -> None:
        self.posts, self.base_url = posts, base_url

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def post(self, path: str) -> _Client:
        self.posts.append((self.base_url, path))
        return self

    def raise_for_status(self) -> None:
        return None


@pytest.fixture
def fake_egress(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """A stand-in `herness.core.egress` module whose loopback client records POSTs."""
    posts: list[tuple[str, str]] = []
    module = types.ModuleType("herness.core.egress")

    def loopback_http_client(base_url: str, *, timeout_s: float) -> _Client:
        assert timeout_s == 5
        return _Client(posts, base_url)

    module.loopback_http_client = loopback_http_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "herness.core.egress", module)
    return posts


@pytest.mark.parametrize("url", ["http://127.0.0.1:9100", "http://localhost:9", "http://[::1]:9"])
def test_ut08_49_kill_service_posts_to_stub(
    fault_env: FaultEnv,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
    fake_egress: list[tuple[str, str]],
    url: str,
) -> None:
    """UT08-49 with HERNESS_STUB_SERVICES naming the service, POST <url>/__control/kill."""
    hook: list[str] = []
    reset_process_state.kill_service_hook = hook.append
    monkeypatch.setenv("HERNESS_STUB_SERVICES", json.dumps({"openjev": url}))
    fault_env([{"point": "gpu.after_start", "action": "kill_service:openjev"}])
    faults.fault_point("gpu.after_start")
    assert fake_egress == [(url, "/__control/kill")]
    assert hook == []


def test_ut08_49_kill_service_stub_not_named_uses_hook(
    fault_env: FaultEnv,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
    fake_egress: list[tuple[str, str]],
) -> None:
    """UT08-49 stub services that do not name the service fall through to the hook."""
    hook: list[str] = []
    reset_process_state.kill_service_hook = hook.append
    monkeypatch.setenv(
        "HERNESS_STUB_SERVICES", json.dumps({"vllm-reasoning": "http://127.0.0.1:1"})
    )
    fault_env([{"point": "gpu.after_start", "action": "kill_service:openjev"}])
    faults.fault_point("gpu.after_start")
    assert (fake_egress, hook) == ([], ["openjev"])


@pytest.mark.parametrize(
    "stubs",
    [
        '{"openjev": "http://10.0.0.5:8000"}',
        '{"openjev": "http://example.com:8000"}',
        '{"openjev": "ftp://127.0.0.1:21"}',
        '{"openjev": 5}',
        '["openjev"]',
        "not json",
    ],
)
def test_ut08_49_kill_service_bad_stub_config(
    fault_env: FaultEnv,
    monkeypatch: pytest.MonkeyPatch,
    fake_egress: list[tuple[str, str]],
    stubs: str,
) -> None:
    """UT08-49 a non-loopback or malformed HERNESS_STUB_SERVICES raises ConfigError, no POST."""
    monkeypatch.setenv("HERNESS_STUB_SERVICES", stubs)
    fault_env([{"point": "gpu.after_start", "action": "kill_service:openjev"}])
    with pytest.raises(e.ConfigError, match="HERNESS_STUB_SERVICES"):
        faults.fault_point("gpu.after_start")
    assert fake_egress == []


def test_ut08_49_kill_service_without_egress_module(
    fault_env: FaultEnv, reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-49 while herness.core.egress is absent, a stub service falls through to the hook."""
    hook: list[str] = []
    reset_process_state.kill_service_hook = hook.append
    monkeypatch.setitem(sys.modules, "herness.core.egress", None)
    monkeypatch.setenv("HERNESS_STUB_SERVICES", '{"openjev": "http://127.0.0.1:1"}')
    fault_env([{"point": "gpu.after_start", "action": "kill_service:openjev"}])
    faults.fault_point("gpu.after_start")
    assert hook == ["openjev"]


def test_ut08_49_kill_service_other_import_error_propagates(
    fault_env: FaultEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-49 a missing dependency inside the egress module is not swallowed."""

    def broken(name: str) -> Any:
        raise ModuleNotFoundError(name="httpx")

    monkeypatch.setattr(faults.importlib, "import_module", broken)
    monkeypatch.setenv("HERNESS_STUB_SERVICES", '{"openjev": "http://127.0.0.1:1"}')
    fault_env([{"point": "gpu.after_start", "action": "kill_service:openjev"}])
    with pytest.raises(ModuleNotFoundError):
        faults.fault_point("gpu.after_start")


# --- UT08-50 enabling ----------------------------------------------------------------------


def test_ut08_50_first_call_enables(fault_env: FaultEnv, reset_process_state: ProcessState) -> None:
    """UT08-50 the first call logs WARNING resilience.faults.enabled and sets faults_enabled."""
    fault_env([_rule(nth=99), _rule(nth=98)])
    with structlog.testing.capture_logs() as logs:
        for _ in range(3):
            faults.fault_point("llm.call")
    assert logs == [
        {
            "component": "resilience",
            "event": "resilience.faults.enabled",
            "plan": "fault_plan.json",
            "rules": 2,
            "log_level": "warning",
        }
    ]
    assert reset_process_state.faults_enabled is True


def test_ut08_50_invalid_plan_raises_and_stays_disabled(
    fault_env: FaultEnv, reset_process_state: ProcessState
) -> None:
    """UT08-50 an invalid plan under test raises ConfigError and enables nothing."""
    fault_env([_rule(point="bad")])
    with pytest.raises(e.ConfigError):
        faults.fault_point("llm.call")
    assert reset_process_state.fault_plan is NOT_LOADED
    assert reset_process_state.faults_enabled is False


def test_ut08_50_loaded_under_lock_once(
    fault_env: FaultEnv, reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-50 a plan stored by another thread while waiting for the lock is not reloaded."""
    fault_env([_rule()])
    real_lock = reset_process_state.lock

    class _Racing:
        def __enter__(self) -> None:
            real_lock.acquire()
            reset_process_state.fault_plan = None  # the other thread won the race

        def __exit__(self, *_exc: object) -> None:
            real_lock.release()

    monkeypatch.setattr(reset_process_state, "lock", _Racing())
    faults.fault_point("llm.call")
    assert reset_process_state.faults_enabled is False


# --- UT08-105 environment gate -------------------------------------------------------------


@pytest.mark.parametrize(("env", "shown"), [(None, "unset"), ("prod", "prod")])
def test_ut08_105_plan_ignored_outside_test(
    tmp_path: Path,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
    env: str | None,
    shown: str,
) -> None:
    """UT08-105 HERNESS_ENV unset or prod: no file access, no fault, one ignored WARNING."""
    path = _write(tmp_path, [_rule()])
    monkeypatch.setenv("HERNESS_FAULTS", str(path))
    if env is None:
        monkeypatch.delenv("HERNESS_ENV")
    else:
        monkeypatch.setenv("HERNESS_ENV", env)
    # The spy covers only the calls (the plan file is written before it), any path.
    with structlog.testing.capture_logs() as logs, _fs_spy() as touched:
        for _ in range(10):
            faults.fault_point("llm.call")
    assert touched == []
    assert logs == [
        {
            "component": "resilience",
            "event": "resilience.faults.ignored",
            "env": shown,
            "log_level": "warning",
        }
    ]
    assert reset_process_state.faults_enabled is False


def test_ut08_105_plan_applied_under_test(
    tmp_path: Path, reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-105 HERNESS_ENV=test: the same plan is loaded and applied on every call."""
    monkeypatch.setenv("HERNESS_FAULTS", str(_write(tmp_path, [_rule()])))
    assert _fire_indices(10) == list(range(1, 11))
    assert reset_process_state.faults_enabled is True


def test_ut08_105_fault_env_fixture(fault_env: FaultEnv) -> None:
    """UT08-105 the fault_env fixture sets HERNESS_ENV=test and HERNESS_FAULTS to its plan."""
    path = fault_env([_rule()])
    assert os.environ["HERNESS_ENV"] == "test"
    assert os.environ["HERNESS_FAULTS"] == str(path)
    assert json.loads(path.read_text(encoding="utf-8")) == [_rule()]
