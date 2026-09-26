"""Tests for herness.core.resilience.settings: config/resilience.yaml models (T08-26)."""

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from herness.core.resilience import settings as s

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
DESIGN = ROOT / "docs" / "specs" / "08-resilience-and-jobs.md"
_YAML_BLOCK = re.compile(r"## 7\. Configuration.*?```yaml\n(.*?)```", re.DOTALL)


def _design_yaml() -> dict[str, Any]:
    """The design 08 §7 YAML block with the R-43, R-53 and T08-17 C1 amendments applied."""
    match = _YAML_BLOCK.search(DESIGN.read_text(encoding="utf-8"))
    assert match is not None
    raw: dict[str, Any] = yaml.safe_load(match.group(1))
    services = raw["resilience"]["gpu"]["classes"]["decider"]["services"]
    services["openjev"]["health"]["bearer_secret"] = "secret:OPENJEV_API_KEY"  # noqa: S105 - reference (R-53)
    raw["schedule"]["jobs"][0]["job"]["gpu_class"] = "none"  # R-43
    # T08-17 C1 (spec note on design 08 §7): the unquoted flow item splits at its commas.
    vram = ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
    raw["resilience"]["gpu"]["vram_check_cmd"] = vram
    return raw


@pytest.fixture
def raw() -> dict[str, Any]:
    return copy.deepcopy(_design_yaml())


def _locs(exc: pytest.ExceptionInfo[ValidationError]) -> list[tuple[int | str, ...]]:
    return [tuple(err["loc"]) for err in exc.value.errors()]


def _rejects(model: type[BaseModel], data: object) -> pytest.ExceptionInfo[ValidationError]:
    with pytest.raises(ValidationError) as exc:
        model.model_validate(data)
    return exc


def test_ut08_03_cfg_default_equals_design(cfg_default: s.ResilienceConfig) -> None:
    """UT08-03 cfg_default equals design 08 §7 with R-43/R-53 applied, field for field."""
    assert cfg_default == s.ResilienceConfig.model_validate(_design_yaml())
    dumped = cfg_default.model_dump(mode="json", exclude_unset=True)
    assert dumped == _design_yaml()


def test_ut08_03_rulings_and_defaults(cfg_default: s.ResilienceConfig) -> None:
    """UT08-03 nightly class none (R-43), ports 8000/8100/8200 (R-51), OpenJev secret (R-53)."""
    res, sched = cfg_default.resilience, cfg_default.schedule
    assert sched.jobs[0].name == "nightly"
    assert sched.jobs[0].job.gpu_class == "none"
    classes = res.gpu.classes
    assert classes["reasoning"].services["vllm-reasoning"].url == "http://127.0.0.1:8000"
    openjev = classes["decider"].services["openjev"]
    assert openjev.url == "http://127.0.0.1:8100"
    assert openjev.health.bearer_secret == "secret:OPENJEV_API_KEY"  # noqa: S105 - reference
    assert openjev.start_on_entry is False
    large = classes["large"].services["llamacpp-large"]
    assert large.url == "http://127.0.0.1:8200"
    assert large.health.bearer_secret is None
    assert large.start_on_entry is True
    assert res.retry.retry_after_max_s == 86400
    assert res.retry.policies["sqlite_write"] == s.PolicySettings(
        attempts=6, base_s=0.2, cap_s=5, max_elapsed_s=30
    )
    assert res.retry.policies["tool_store"].retry_after_cap_s == 0
    assert res.retry.policies["llm_local"].timeout_s is None
    assert res.jobs.max_attempts["sync"] == 3
    assert sched.jobs[0].then[2].enabled is False
    assert sched.jobs[0].then[0].priority is None
    assert sched.jobs[2].job.payload == {}
    assert sched.windows[0].days is None
    assert sched.windows[4].days == ["SUN"]


def test_ut08_03_field_defaults() -> None:
    """UT08-03 the per-field defaults of U08-06/U08-07 equal design 08 §7."""
    assert s.RetrySettings.model_fields["retry_after_max_s"].default == 86400
    assert s.JobBackoffSettings() == s.JobBackoffSettings(base_s=60, cap_s=3600)
    assert s.FallbackSettings(decider_chain={}).max_repairs == 2
    assert s.LoopSettings() == s.LoopSettings(checkpoint_min_interval_s=5, stop_on_signal_no=2)
    assert s.TaskSettings().max_task_attempts == 3
    assert s.ChatSchedule().off_hours == "small_model"
    assert s.ChatSchedule().in_hours_unavailable == "small_model"
    assert s.RekeySchedule() == s.RekeySchedule(cron="0 19 * * SAT", min_notice_h=12)
    assert s.MaintenanceSchedule().catch_up_max == "12h"
    fields = s.JobsSettings.model_fields
    expected = {
        "lease_s": 300,
        "heartbeat_s": 30,
        "reaper_interval_s": 30,
        "tick_s": 2,
        "cancel_grace_s": 60,
        "shutdown_grace_s": 120,
        "stall_timeout_s": 1800,
        "stall_timeout_large_s": 7200,
        "cpu_slots": 2,
    }
    assert {name: fields[name].default for name in expected} == expected
    gpu = s.GpuSettings.model_fields
    assert gpu["vram_free_threshold_mb"].default == 2000
    assert gpu["stop_timeout_s"].default == 120
    assert gpu["warmup_timeout_s"].default == 120
    assert s.BreakersSection.model_fields["restart_max_per_hour"].default == 1
    schedule = s.ScheduleSection.model_fields
    assert schedule["preempt_grace_min"].default == 15
    assert schedule["batch_in_chat_min_priority"].default == 70


def test_ut08_03_models_frozen_and_closed(cfg_default: s.ResilienceConfig) -> None:
    """UT08-03 models are frozen and reject unknown keys."""
    with pytest.raises(ValidationError):
        cfg_default.resilience.jobs.lease_s = 1  # type: ignore[misc]
    exc = _rejects(s.LoopSettings, {"bogus": 1})
    assert _locs(exc) == [("bogus",)]


def test_ut08_03_missing_policy_key_rejected(raw: dict[str, Any]) -> None:
    """UT08-03 a missing policy key and an extra `gpu_health` key are rejected."""
    del raw["resilience"]["retry"]["policies"]["sqlite_write"]
    exc = _rejects(s.ResilienceConfig, raw)
    assert _locs(exc) == [("resilience", "retry", "policies")]
    assert "sqlite_write" in str(exc.value)
    policies = _design_yaml()["resilience"]["retry"]["policies"]
    policies["gpu_health"] = {"attempts": 1, "base_s": 1, "cap_s": 1, "max_elapsed_s": 1}
    _rejects(s.RetrySettings, {"policies": policies, "job_backoff": {}})


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8000",
        "http://example.com:8000",
        "https://127.0.0.1:8000",
        "http://127.0.0.1",
        "http://user@127.0.0.1:8000",
        "http://127.0.0.1:99999",
        "ftp://localhost:21",
    ],
)
def test_ut08_03_non_loopback_url_rejected(url: str) -> None:
    """UT08-03 a service URL that is not http on a loopback host with an explicit port fails."""
    exc = _rejects(s.ServiceSettings, {"url": url, "health": {"path": "/h"}, "start_timeout_s": 1})
    assert _locs(exc) == [("url",)]


@pytest.mark.parametrize("url", ["http://localhost:8100", "http://[::1]:8200"])
def test_ut08_03_loopback_url_accepted(url: str) -> None:
    """UT08-03 `localhost` and `::1` are loopback hosts."""
    svc = s.ServiceSettings.model_validate(
        {"url": url, "health": {"path": "/h"}, "start_timeout_s": 1}
    )
    assert svc.url == url


@pytest.mark.parametrize(
    "ref", ["openjev.api_key", "secret:openjev", "secret:", "OPENJEV_API_KEY", "secret:A B"]
)
def test_ut08_03_bearer_secret_reference_regex(ref: str) -> None:
    """UT08-03 `bearer_secret` must be a `secret:NAME` reference (R-53)."""
    exc = _rejects(s.HealthCheck, {"path": "/v1/models", "bearer_secret": ref})
    assert _locs(exc) == [("bearer_secret",)]


def test_ut08_03_design_bearer_value_rejected(raw: dict[str, Any]) -> None:
    """UT08-03 the unamended design value `openjev.api_key` is rejected with its path."""
    services = raw["resilience"]["gpu"]["classes"]["decider"]["services"]
    services["openjev"]["health"]["bearer_secret"] = "openjev.api_key"  # noqa: S105 - not a secret
    exc = _rejects(s.ResilienceConfig, raw)
    path = ("resilience", "gpu", "classes", "decider", "services", "openjev", "health")
    assert _locs(exc) == [(*path, "bearer_secret")]


def _gpu(raw: dict[str, Any]) -> dict[str, Any]:
    gpu: dict[str, Any] = raw["resilience"]["gpu"]
    return gpu


def test_ut08_03_gpu_rules(raw: dict[str, Any]) -> None:
    """UT08-03 all three classes present, a service in one class only, clean argv lists."""
    gpu = _gpu(raw)
    del gpu["classes"]["large"]
    _rejects(s.GpuSettings, gpu)
    gpu = _gpu(copy.deepcopy(_design_yaml()))
    gpu["classes"]["large"]["services"]["openjev"] = gpu["classes"]["decider"]["services"][
        "openjev"
    ]
    assert "more than one class" in str(_rejects(s.GpuSettings, gpu).value)
    for key, value in (("compose_cmd", []), ("compose_cmd", ["a\x00b"])):
        gpu = _gpu(copy.deepcopy(_design_yaml()))
        gpu[key] = value
        assert _locs(_rejects(s.GpuSettings, gpu))[0][0] == key
    gpu = _gpu(copy.deepcopy(_design_yaml()))
    gpu["classes"]["reasoning"]["services"]["vllm-bogus"] = gpu["classes"]["reasoning"][
        "services"
    ].pop("vllm-reasoning")
    _rejects(s.GpuSettings, gpu)


def test_ut08_03_health_path_must_start_with_slash() -> None:
    """UT08-03 a health path without a leading `/` is rejected."""
    assert _locs(_rejects(s.HealthCheck, {"path": "health"})) == [("path",)]


def test_ut08_03_policy_bounds() -> None:
    """UT08-03 PolicySettings bounds: attempts 1-100, cap_s >= base_s, positive timeouts."""
    base = {"attempts": 3, "base_s": 2, "cap_s": 20, "max_elapsed_s": 60}
    for bad in (
        {"attempts": 0},
        {"attempts": 101},
        {"base_s": 0},
        {"cap_s": 1},
        {"max_elapsed_s": 0},
        {"timeout_s": 0},
        {"connect_timeout_s": -1},
        {"retry_after_cap_s": -1},
    ):
        _rejects(s.PolicySettings, base | bad)
    assert s.PolicySettings.model_validate(base | {"cap_s": 2}).cap_s == 2


def test_ut08_03_breaker_and_fallback_bounds() -> None:
    """UT08-03 breaker cooldown ordering and decider-chain name rules."""
    _rejects(s.BreakerSettings, {"failure_threshold": 1, "cooldown_s": 60, "cooldown_max_s": 59})
    _rejects(s.BreakerSettings, {"failure_threshold": 0, "cooldown_s": 1, "cooldown_max_s": 1})
    for chain in ({"local": []}, {"local": ["laya", "laya"]}, {"local": ["Laya"]}):
        _rejects(s.FallbackSettings, {"decider_chain": chain})
    _rejects(s.FallbackSettings, {"decider_chain": {}, "max_repairs": 6})


def test_ut08_03_jobs_rules(raw: dict[str, Any]) -> None:
    """UT08-03 every JobKind needs max_attempts in 1-20; heartbeat_s * 3 < lease_s."""
    jobs: dict[str, Any] = raw["resilience"]["jobs"]
    s.JobsSettings.model_validate(jobs)
    for bad in (
        {"max_attempts": {k: v for k, v in jobs["max_attempts"].items() if k != "eval"}},
        {"max_attempts": jobs["max_attempts"] | {"eval": 21}},
        {"heartbeat_s": 100},
        {"cpu_slots": 17},
        {"exclusive_kinds": ["bogus"]},
    ):
        _rejects(s.JobsSettings, jobs | bad)


# UT08-05 (model part; U08-99's cron parse and week coverage belong to T08-02).


def test_ut08_05_four_field_cron_rejected_with_path(raw: dict[str, Any]) -> None:
    """UT08-05 a 4-field cron fails at `schedule.jobs[0].cron`."""
    raw["schedule"]["jobs"][0]["cron"] = "0 19 * *"
    assert _locs(_rejects(s.ResilienceConfig, raw)) == [("schedule", "jobs", 0, "cron")]
    for cron in ("0 19 * * * *", "0 19 * * S@T"):
        _rejects(s.RekeySchedule, {"cron": cron})


def test_ut08_05_unparsable_but_well_shaped_cron_passes_model(raw: dict[str, Any]) -> None:
    """UT08-05 `60 * * * *` has cron shape; the full parse is U08-99's job."""
    raw["schedule"]["jobs"][0]["cron"] = "60 * * * *"
    assert s.ResilienceConfig.model_validate(raw).schedule.jobs[0].cron == "60 * * * *"


def test_ut08_05_then_kind_rejected_with_path(raw: dict[str, Any]) -> None:
    """UT08-05 a `then` step of kind `foo` fails with its path."""
    raw["schedule"]["jobs"][0]["then"][1]["kind"] = "foo"
    assert _locs(_rejects(s.ResilienceConfig, raw)) == [("schedule", "jobs", 0, "then", 1, "kind")]


def test_ut08_05_preload_not_in_classes_rejected(raw: dict[str, Any]) -> None:
    """UT08-05 a window preload outside its classes fails at that window."""
    raw["schedule"]["windows"][2]["preload"] = "large"
    assert _locs(_rejects(s.ResilienceConfig, raw)) == [("schedule", "windows", 2)]


def test_ut08_05_window_class_not_gpu_class_rejected(raw: dict[str, Any]) -> None:
    """UT08-05 a window class that is not a `gpu.classes` key is rejected."""
    raw["schedule"]["windows"][0]["classes"] = ["gpu9"]
    assert _locs(_rejects(s.ResilienceConfig, raw))[0][:4] == ("schedule", "windows", 0, "classes")


def test_ut08_05_cross_check_names_path() -> None:
    """UT08-05 the root validator names the path of a class missing from `gpu.classes`."""
    cfg = s.ResilienceConfig.model_validate(_design_yaml())
    gpu = cfg.resilience.gpu.model_copy(update={"classes": {}})
    res = cfg.resilience.model_copy(update={"gpu": gpu})
    data = {"resilience": res, "schedule": cfg.schedule}
    with pytest.raises(ValidationError, match=r"schedule\.windows\[0\]\.classes"):
        s.ResilienceConfig.model_validate(data)
    sched = cfg.schedule.model_copy(update={"windows": []})
    with pytest.raises(ValidationError, match=r"schedule\.jobs\[0\]\.then\[0\]\.gpu_class"):
        s.ResilienceConfig.model_validate({"resilience": res, "schedule": sched})


@pytest.mark.parametrize(
    ("change", "loc"),
    [
        ({"start": "24:00"}, ("start",)),
        ({"end": "06:00"}, ()),
        ({"classes": []}, ("classes",)),
        ({"classes": ["decider", "decider"]}, ("classes",)),
        ({"days": ["XYZ"]}, ("days", 0)),
        ({"overrun_max_min": 241}, ("overrun_max_min",)),
        ({"name": "Morning"}, ("name",)),
    ],
)
def test_ut08_05_window_rules(change: dict[str, Any], loc: tuple[str | int, ...]) -> None:
    """UT08-05 WindowSpec field rules."""
    window = {"name": "w", "start": "06:00", "end": "08:00", "classes": ["decider"]} | change
    assert _locs(_rejects(s.WindowSpec, window))[0] == loc


@pytest.mark.parametrize(
    "change",
    [
        {"name": "maintenance"},
        {"name": "sync.jira"},
        {"catch_up_max": "8d"},
        {"catch_up_max": "6"},
        {"then": [{"kind": "eval", "gpu_class": "none"}] * 11},
    ],
)
def test_ut08_05_scheduled_job_rules(change: dict[str, Any]) -> None:
    """UT08-05 ScheduledJob name, catch-up and chain-length rules."""
    job = {"name": "j", "cron": "0 5 * * *", "catch_up_max": "7d"}
    job |= {"job": {"kind": "eval", "gpu_class": "none"}} | change
    _rejects(s.ScheduledJob, job)


def test_ut08_05_scheduled_job_names_unique(raw: dict[str, Any]) -> None:
    """UT08-05 duplicate scheduled job names are rejected."""
    raw["schedule"]["jobs"][1]["name"] = "nightly"
    assert "duplicate" in str(_rejects(s.ResilienceConfig, raw).value)


def test_ut08_05_chain_step_rules() -> None:
    """UT08-05 ChainStep priority 0-100, weekday skip_on."""
    step = {"kind": "review", "gpu_class": "reasoning"}
    _rejects(s.ChainStep, step | {"priority": 101})
    _rejects(s.ChainStep, step | {"skip_on": ["sun"]})
    assert s.ChainStep.model_validate(step | {"priority": 0}).priority == 0


@pytest.mark.parametrize("value", ["99999999999999999999d", "6h\n", "٦h", "1w", "10081m", "169h"])
def test_ut08_05_catch_up_max_rejected_as_validation_error(value: str) -> None:
    """UT08-05 bad or huge catch_up_max raises ValidationError, never OverflowError."""
    assert _locs(_rejects(s.MaintenanceSchedule, {"catch_up_max": value})) == [("catch_up_max",)]


@pytest.mark.parametrize("value", ["10080m", "168h", "7d", "0m"])
def test_ut08_05_catch_up_max_bounds_accepted(value: str) -> None:
    """UT08-05 catch_up_max up to exactly 7 days is accepted."""
    assert s.MaintenanceSchedule(catch_up_max=value).catch_up_max == value


def test_ut08_05_ascii_only_times() -> None:
    """UT08-05 window times and cron fields accept ASCII digits only."""
    window = {"name": "w", "start": "0٦:00", "end": "08:00", "classes": ["decider"]}
    assert _locs(_rejects(s.WindowSpec, window))[0] == ("start",)
    _rejects(s.RekeySchedule, {"cron": "0 1٩ * * SAT"})


def test_ut08_03_port_zero_rejected() -> None:
    """UT08-03 port 0 is not an explicit service port."""
    data = {"url": "http://127.0.0.1:0", "health": {"path": "/h"}, "start_timeout_s": 1}
    assert _locs(_rejects(s.ServiceSettings, data)) == [("url",)]
