"""UT10-71 host rows: each doctor host check PASS/WARN/FAIL per recorded fixture (U10-89, U10-90).

Every external program answers through `fake_runner`; `psutil`, `shutil`, `sys`, `socket`,
the platform probe and the GPU state are monkeypatched, so the real host is never read.
"""

from __future__ import annotations

import shutil
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support.doctor_host_env import (
    BDE_DENIED,
    ICACLS_DIR_OK,
    ICACLS_FILE_OK,
    NOW,
    SC_MISSING,
    SC_WORKER_OK,
    SCHTASKS_MISSING,
    SCHTASKS_WSL_OK,
    FakeGpuState,
    Host,
    green_host_rows,
    listen,
    load_cfg,
    manage_bde,
    w32tm,
    with_directory_file,
    with_manager,
    with_paths,
)
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_runner import FakeRunner

from herness.admin import doctor_gpu, wsl
from herness.admin import doctor_host as dh
from herness.core import audit, secrets
from herness.core.config import HernessConfig, reset_config
from herness.core.config_view import ConfigIssue
from herness.core.errors import ConfigError, EgressBlocked

pytestmark = pytest.mark.unit

GIB = 1024**3


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[HernessConfig, Host]]:
    cfg, cfg_dir = load_cfg(tmp_path)
    host = Host(monkeypatch)
    host.install(cfg_dir)
    yield cfg, host
    reset_config()


def _by_name(results: list[dh.HostCheckResult]) -> dict[str, dh.HostCheckResult]:
    return {r.name: r for r in results}


# --- python_uv ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "uv", "status"),
    [
        ((3, 12, 7), "C:\\uv.exe", "PASS"),
        ((3, 13, 0), "C:\\uv.exe", "FAIL"),
        ((3, 12, 1), None, "FAIL"),
    ],
)
def test_ut10_71_python_uv(
    env: tuple[HernessConfig, Host],
    monkeypatch: pytest.MonkeyPatch,
    version: tuple[int, ...],
    uv: str | None,
    status: str,
) -> None:
    """UT10-71 python_uv: PASS only for Python 3.12 with uv on PATH."""
    cfg, _ = env
    monkeypatch.setattr(dh, "sys", SimpleNamespace(version_info=version, platform="win32"))
    monkeypatch.setattr(shutil, "which", lambda name: uv if name == "uv" else None)
    row = dh._check_python_uv(cfg)
    assert (row.name, row.status) == ("python_uv", status)
    assert row.fix == ("" if status == "PASS" else "Install Python 3.12 and uv")


# --- config_valid ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("severities", "status"), [((), "PASS"), (("warn",), "WARN"), (("warn", "error"), "FAIL")]
)
def test_ut10_71_config_valid(
    env: tuple[HernessConfig, Host],
    monkeypatch: pytest.MonkeyPatch,
    severities: tuple[str, ...],
    status: str,
) -> None:
    """UT10-71 config_valid: FAIL on an error issue, WARN on warnings only, else PASS."""
    cfg, _ = env
    seen: list[tuple[Path, str]] = []

    def fake_validate(cfg_dir: Path, profile: str) -> list[ConfigIssue]:
        seen.append((cfg_dir, profile))
        return [ConfigIssue(s, "a.b", "msg", None) for s in severities]  # type: ignore[arg-type]

    monkeypatch.setattr(dh, "validate", fake_validate)
    row = dh._check_config_valid(cfg)
    assert row.status == status
    assert seen == [(dh._config_dir(), cfg.profile)]
    assert row.fix in ("", "herness config validate")


def test_ut10_71_config_valid_real_tree(env: tuple[HernessConfig, Host]) -> None:
    """UT10-71 config_valid runs the real validate on the config directory."""
    cfg, _ = env
    assert dh._check_config_valid(cfg).status in ("PASS", "WARN", "FAIL")


# --- secrets_present ------------------------------------------------------------------------


def test_ut10_71_secrets_present(
    env: tuple[HernessConfig, Host], fake_keyring: MemoryKeyring
) -> None:
    """UT10-71 secrets_present: FAIL listing missing names (no values), PASS when all exist."""
    cfg, _ = env
    names = secrets.referenced_secret_names(cfg)
    value = "Sx-doctor-sentinel-77"  # pragma: allowlist secret - test sentinel
    fake_keyring.set_password("herness", names[0], value)
    row = dh._check_secrets_present(cfg)
    assert row.status == "FAIL"
    assert names[0] not in row.detail
    assert all(n in row.detail for n in names[1:])
    assert value not in row.detail
    assert row.fix == "herness secrets set <name>"
    for name in names:
        fake_keyring.set_password("herness", name, value)
    assert dh._check_secrets_present(cfg).status == "PASS"


# --- acl_data -------------------------------------------------------------------------------


def _acl_cfg(cfg: HernessConfig, tmp_path: Path, cfg_dir: Path) -> HernessConfig:
    directory = tmp_path / "private" / "directory.csv"
    directory.parent.mkdir()
    directory.write_text("name\n", encoding="utf-8")
    (cfg_dir.parent / ".env").write_text("X=1\n", encoding="utf-8")
    return with_directory_file(cfg, directory)


def test_ut10_71_acl_data_pass(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner, tmp_path: Path
) -> None:
    """UT10-71 acl_data PASS: data, profiles, directory file and .env all restricted."""
    cfg, _ = env
    cfg = _acl_cfg(cfg, tmp_path, dh._config_dir())
    fake_runner.add(["icacls.exe"], ICACLS_FILE_OK)
    for d in (cfg.paths.data, dh._config_dir().resolve() / "profiles"):
        fake_runner.add(["icacls.exe", str(d)], ICACLS_DIR_OK)
    row = dh._check_acl_data(cfg)
    assert row.status == "PASS", row.detail
    checked = [argv[1] for argv in fake_runner.argvs]
    assert len(checked) == 4
    assert str(cfg.paths.data) == checked[0]
    assert all(kw["timeout_s"] == 10 for _, kw in fake_runner.calls)


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        (ICACLS_DIR_OK.replace("SYSTEM:(OI)", "SYSTEM:(I)(OI)"), "inherited ACE (I)"),
        (ICACLS_DIR_OK + "                Everyone:(R)\r\n", "Everyone has access"),
        (
            ICACLS_DIR_OK.replace("NT AUTHORITY\\SYSTEM", "NT AUTHORITY\\Authenticated Users"),
            "Authenticated Users has access",
        ),
        (
            ICACLS_DIR_OK.replace("Administrators:(OI)(CI)(F)", "Administrators:(OI)(CI)(M)"),
            "no BUILTIN\\Administrators:(OI)(CI)(F)",
        ),
        (
            ICACLS_DIR_OK.replace("svc-herness:(OI)(CI)(M)", "svc-herness:(OI)(CI)(F)"),
            "no svc-herness:(OI)(CI)(M)",
        ),
        # Grantee allowlist (fix round 1 ruling): any other principal FAILs, named.
        *(
            (f"{ICACLS_DIR_OK}                {who}:{perm}\r\n", f"{who} has access")
            for who, perm in [
                ("CONTOSO\\Domain Users", "(RX)"),
                ("HERNESS-PC\\bob", "(OI)(CI)(R)"),
                ("BUILTIN\\Guests", "(R)"),
                ("NT AUTHORITY\\INTERACTIVE", "(R)"),
                ("CREATOR OWNER", "(OI)(CI)(IO)(F)"),
                ("S-1-5-21-1-2-3-1001", "(F)"),
                ("OTHER-PC\\svc-herness-x", "(M)"),
            ]
        ),
    ],
)
def test_ut10_71_acl_data_fail_variants(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner, output: str, reason: str
) -> None:
    """UT10-71 acl_data FAIL per unsafe icacls variant, naming the path and the reason."""
    cfg, _ = env
    fake_runner.add(["icacls.exe"], output)
    row = dh._check_acl_data(cfg)
    assert row.status == "FAIL"
    assert f"{cfg.paths.data}: {reason}" in row.detail
    assert row.fix.startswith("icacls <path> /inheritance:r")


def test_ut10_71_acl_aces_parsing() -> None:
    """UT10-71 icacls parsing: path prefix cut at the continuation indent or after the path."""
    out = "C:\\a b\\x NT AUTHORITY\\SYSTEM:(F)\r\n         CONTOSO\\Domain Users:(RX)\r\n"
    assert dh._aces(out, "ignored") == [
        ("NT AUTHORITY\\SYSTEM", "(F)"),
        ("CONTOSO\\Domain Users", "(RX)"),
    ]
    one = "C:\\a b\\x BUILTIN\\Administrators:(F)\r\n\r\nSuccessfully processed 1 files\r\n"
    assert dh._aces(one, "C:\\a b\\x") == [("BUILTIN\\Administrators", "(F)")]
    # An unknown prefix stays in the grantee, which then is not allowlisted (fail closed).
    assert dh._aces(one, "D:\\other") == [("C:\\a b\\x BUILTIN\\Administrators", "(F)")]
    assert dh._aces("", "C:\\x") == []


def test_ut10_71_acl_data_missing_and_icacls_error(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner, tmp_path: Path
) -> None:
    """UT10-71 acl_data FAIL when data is missing or icacls exits non-zero; absent files skip."""
    cfg, _ = env
    fake_runner.add(["icacls.exe"], ICACLS_DIR_OK)
    gone = with_paths(cfg, data=tmp_path / "nope")
    assert "nope: missing" in dh._check_acl_data(gone).detail
    fake_runner.add(["icacls.exe", str(cfg.paths.data)], "", returncode=5)
    row = dh._check_acl_data(cfg)
    assert (row.status, row.detail) == ("FAIL", f"{cfg.paths.data}: icacls exit 5")


# --- bitlocker ------------------------------------------------------------------------------


def _bde_cfg(cfg: HernessConfig) -> HernessConfig:
    return with_paths(cfg, data=Path("D:/herness/data"), backup_target=Path("E:/herness-backup"))


def test_ut10_71_bitlocker_pass_checks_three_drives(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner
) -> None:
    """UT10-71 bitlocker PASS: system, data and backup drives all report Protection On."""
    cfg, _ = env
    for drive in ("C:", "D:", "E:"):
        fake_runner.add(["manage-bde.exe", "-status", drive], manage_bde(drive, "Protection On"))
    row = dh._check_bitlocker(_bde_cfg(cfg))
    assert row.status == "PASS"
    assert [argv[2] for argv in fake_runner.argvs] == ["C:", "D:", "E:"]


def test_ut10_71_bitlocker_warn_on_access_denied(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner
) -> None:
    """UT10-71 bitlocker WARN when manage-bde is denied (not elevated)."""
    cfg, _ = env
    fake_runner.add(["manage-bde.exe"], BDE_DENIED, returncode=-2147024891)
    row = dh._check_bitlocker(_bde_cfg(cfg))
    assert row.status == "WARN"
    assert row.fix == "Enable BitLocker"


def test_ut10_71_bitlocker_drives_dedupe(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner
) -> None:
    """UT10-71 bitlocker checks a drive shared by data and backup once."""
    cfg, _ = env
    fake_runner.add(["manage-bde.exe"], manage_bde("C:", "Protection On"))
    one = with_paths(cfg, data=Path("C:/herness/data"), backup_target=Path("C:/bk"))
    assert dh._check_bitlocker(one).status == "PASS"
    assert len(fake_runner.calls) == 1


@pytest.mark.parametrize("value", ["", "garbage", "  "])
def test_ut10_71_bitlocker_bad_systemdrive_warns(
    env: tuple[HernessConfig, Host],
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """UT10-71 bitlocker WARN (never a silent PASS) when %SYSTEMDRIVE% names no drive."""
    cfg, _ = env
    monkeypatch.setenv("SYSTEMDRIVE", value)
    fake_runner.add(["manage-bde.exe"], manage_bde("D:", "Protection On"))
    row = dh._check_bitlocker(_bde_cfg(cfg))
    assert row.status == "WARN"
    assert row.detail.startswith("%SYSTEMDRIVE% (no drive) access denied or unknown")
    assert [argv[2] for argv in fake_runner.argvs] == ["D:", "E:"]


def test_ut10_71_acl_data_pass_names_absent_targets(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner
) -> None:
    """UT10-71 acl_data PASS lists optional targets it could not find (cwd-dependent .env)."""
    cfg, _ = env
    fake_runner.add(["icacls.exe"], ICACLS_DIR_OK)
    row = dh._check_acl_data(cfg)
    env_file = dh._config_dir().resolve().parent / ".env"
    assert (row.status, row.detail) == ("PASS", f"ACLs restricted; not present: {env_file}")


# --- audit_chain ----------------------------------------------------------------------------


def test_ut10_71_audit_chain(
    env: tuple[HernessConfig, Host], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-71 audit_chain PASS on an intact chain, FAIL with first_break otherwise."""
    cfg, _ = env
    assert dh._check_audit_chain(cfg).status == "PASS"
    broken = audit.ChainReport(ok=False, files=1, lines=3, first_break="audit-2026-09-24.jsonl:2")
    monkeypatch.setattr(dh, "verify_chain", lambda logs: broken)
    row = dh._check_audit_chain(cfg)
    assert (row.status, row.detail) == ("FAIL", "chain broken at audit-2026-09-24.jsonl:2")


# --- disk_free ------------------------------------------------------------------------------


@pytest.mark.parametrize(("free", "status"), [(50 * GIB, "PASS"), (50 * GIB - 1, "FAIL")])
def test_ut10_71_disk_free(
    env: tuple[HernessConfig, Host], monkeypatch: pytest.MonkeyPatch, free: int, status: str
) -> None:
    """UT10-71 disk_free: PASS at ≥ 50 GiB free on paths.data, else FAIL."""
    cfg, _ = env
    seen: list[Path] = []

    def usage(path: Path) -> Any:
        seen.append(path)
        return SimpleNamespace(total=free * 2, used=free, free=free)

    monkeypatch.setattr(shutil, "disk_usage", usage)
    assert dh._check_disk_free(cfg).status == status
    assert seen == [cfg.paths.data]


# --- clock ----------------------------------------------------------------------------------


def _clock(fake_runner: FakeRunner, cfg: HernessConfig, host: str, wsl_shift: float | None) -> None:
    fake_runner.add(["w32tm.exe", "/query", "/status"], w32tm(host))
    wsl = ["wsl.exe", "-d", cfg.deploy.wsl_distro, "--exec", "date", "+%s.%N"]
    if wsl_shift is None:
        fake_runner.add(wsl, "", returncode=1, stderr="WSL is not installed")
    else:
        fake_runner.add(wsl, f"{NOW.timestamp() + wsl_shift:.9f}\n")


@pytest.mark.parametrize(
    ("host", "wsl_shift", "status"),
    [
        ("0.0012345s", 0.4, "PASS"),
        ("-6.2500000s", 0.4, "FAIL"),
        ("0.0012345s", -5.5, "FAIL"),
        ("0.0012345s", None, "WARN"),
        ("unavailable", 0.1, "WARN"),
        ("-7.0s", None, "FAIL"),
        ("0.01s", float("nan"), "WARN"),
        ("0.01s", float("inf"), "WARN"),
        ("0.01s", float("-inf"), "WARN"),
    ],
)
def test_ut10_71_clock(
    env: tuple[HernessConfig, Host],
    fake_runner: FakeRunner,
    host: str,
    wsl_shift: float | None,
    status: str,
) -> None:
    """UT10-71 clock: FAIL above 5 s on either source, WARN when a source is unavailable."""
    cfg, _ = env
    _clock(fake_runner, cfg, host, wsl_shift)
    row = dh._check_clock(cfg)
    assert row.status == status, row.detail
    assert fake_runner.argvs[1][:4] == ["wsl.exe", "-d", cfg.deploy.wsl_distro, "--exec"]


# --- ports_loopback / ports_expected --------------------------------------------------------


def test_ut10_71_ports_loopback(env: tuple[HernessConfig, Host]) -> None:
    """UT10-71 ports_loopback: loopback listeners PASS; a LAN bind on a watched port FAILs."""
    cfg, host = env
    ui = cfg.security.ui.port
    host.conns = [
        listen("127.0.0.1", 8000),
        listen("::1", 8100),
        listen("127.0.0.1", ui),
        listen("0.0.0.0", 9999),  # not a watched port  # noqa: S104
        listen("0.0.0.0", 8200, status="ESTABLISHED"),  # noqa: S104
    ]
    assert dh._check_ports_loopback(cfg).status == "PASS"
    host.conns.append(listen("192.168.1.20", ui))
    row = dh._check_ports_loopback(cfg)
    assert (row.status, row.detail) == ("FAIL", f"not loopback: {ui} on 192.168.1.20")
    assert row.fix == "Fix the bind or compose ports"


def test_ut10_71_ports_expected(env: tuple[HernessConfig, Host]) -> None:
    """UT10-71 ports_expected: listeners are fine while a class is loaded, WARN otherwise."""
    cfg, host = env
    host.conns = [listen("127.0.0.1", 8000, pid=10), listen("127.0.0.1", 8200, pid=None)]
    host.processes.names[10] = "wslrelay.exe"
    host.gpu.loaded = "reasoning"
    assert dh._check_ports_expected(cfg).status == "PASS"
    host.gpu.loaded = "none"
    row = dh._check_ports_expected(cfg)
    assert row.status == "WARN"
    assert row.detail == "unexpected listener: 8000 by wslrelay.exe, 8200 by unknown process"
    host.conns = [listen("127.0.0.1", 8100, pid=77)]  # 77 exited: NoSuchProcess
    assert dh._check_ports_expected(cfg).detail == "unexpected listener: 8100 by pid 77"
    host.processes.names[77] = "evil\x1b[2J\r\nname.exe"  # attacker-chosen name is sanitised
    assert dh._check_ports_expected(cfg).detail == "unexpected listener: 8100 by evil[2Jname.exe"
    host.conns = []
    assert dh._check_ports_expected(cfg).status == "PASS"


def test_ut10_71_ports_expected_without_worker(env: tuple[HernessConfig, Host]) -> None:
    """UT10-71 ports_expected with no reachable worker: a healthy service means a class is up."""
    cfg, host = env
    host.conns = [listen("127.0.0.1", 8000, pid=10)]
    host.processes.names[10] = "wslrelay.exe"
    host.gpu = FakeGpuState(
        loaded=ConfigError("jobs backend not bound"), healthy={"vllm-reasoning"}
    )
    assert dh._check_ports_expected(cfg).status == "PASS"
    host.gpu.healthy = set()
    assert dh._check_ports_expected(cfg).status == "WARN"
    host.gpu.loaded = OSError("ops store locked")  # a non-HernessError takes the same fallback
    host.gpu.healthy = {"openjev"}
    assert dh._check_ports_expected(cfg).status == "PASS"


# --- socket_guard ---------------------------------------------------------------------------


def test_ut10_71_socket_guard(
    env: tuple[HernessConfig, Host], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-71 socket_guard: PASS on EgressBlocked; FAIL if it connects or raises otherwise."""
    cfg, _ = env
    calls: list[tuple[Any, Any]] = []
    outcome: list[Any] = [EgressBlocked("blocked", reason="not_allowed")]

    def connect(address: Any, timeout: Any = None) -> Any:
        calls.append((address, timeout))
        if isinstance(outcome[0], BaseException):
            raise outcome[0]
        return outcome[0]

    monkeypatch.setattr(socket, "create_connection", connect)
    assert dh._check_socket_guard(cfg).status == "PASS"
    assert calls[0] == (("1.1.1.1", 443), 2)
    outcome[0] = TimeoutError()
    row = dh._check_socket_guard(cfg)
    assert (row.status, row.detail) == ("FAIL", "connect raised TimeoutError")
    closed: list[bool] = []
    outcome[0] = SimpleNamespace(close=lambda: closed.append(True))
    row = dh._check_socket_guard(cfg)
    assert (row.status, closed) == ("FAIL", [True])
    assert row.fix == "Defect: the guard is not installed"


# --- plugins --------------------------------------------------------------------------------


def test_ut10_71_plugins(env: tuple[HernessConfig, Host], monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-71 plugins: WARN listing distribution and version of herness.plugins entry points."""
    cfg, _ = env
    found: list[Any] = []
    groups: list[str] = []

    def entry_points(*, group: str) -> list[Any]:
        groups.append(group)
        return list(found)

    monkeypatch.setattr(dh.metadata, "entry_points", entry_points)
    assert dh._check_plugins(cfg).status == "PASS"
    found.append(
        SimpleNamespace(name="acme", dist=SimpleNamespace(name="acme-conn", version="1.2"))
    )
    found.append(SimpleNamespace(name="orphan", dist=None))
    row = dh._check_plugins(cfg)
    assert (row.status, row.detail) == ("WARN", "plugins: acme-conn 1.2, orphan ?")
    assert groups == ["herness.plugins", "herness.plugins"]


# --- services -------------------------------------------------------------------------------


def test_ut10_71_services_nssm(env: tuple[HernessConfig, Host], fake_runner: FakeRunner) -> None:
    """UT10-71 services (nssm): sc.exe query herness-worker and the herness-wsl task."""
    cfg, _ = env
    fake_runner.add(["sc.exe", "query", "herness-worker"], SC_WORKER_OK)
    fake_runner.add(["schtasks.exe", "/Query", "/TN", "herness-wsl"], SCHTASKS_WSL_OK)
    assert dh._check_services(with_manager(cfg, "nssm")).status == "PASS"
    fake_runner.add(["sc.exe", "query", "herness-worker"], SC_MISSING, returncode=1060)
    row = dh._check_services(with_manager(cfg, "nssm"))
    assert (row.status, row.detail) == ("WARN", "not registered: herness-worker")


def test_ut10_71_services_task_scheduler(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner
) -> None:
    """UT10-71 services (task_scheduler): schtasks for herness-worker and herness-wsl."""
    cfg, _ = env
    fake_runner.add(["schtasks.exe", "/Query", "/TN"], SCHTASKS_MISSING, returncode=1)
    row = dh._check_services(with_manager(cfg, "task_scheduler"))
    assert (row.status, row.detail) == ("WARN", "not registered: herness-worker, herness-wsl")
    assert fake_runner.argvs[0] == ["schtasks.exe", "/Query", "/TN", "herness-worker"]


# --- doctor_checks --------------------------------------------------------------------------


def _all_green(cfg: HernessConfig, fake_runner: FakeRunner, mp: pytest.MonkeyPatch) -> None:
    """Every host row green; the GPU rows (T10-28) are left out of these host-row tests."""
    green_host_rows(cfg, fake_runner, mp)
    mp.setattr(doctor_gpu, "GPU_CHECKS", ())


def test_ut10_71_doctor_checks_all_pass_in_table_order(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-71 doctor_checks: every host row PASS, one row per check, in HOST_CHECKS order."""
    cfg, _ = env
    _all_green(cfg, fake_runner, monkeypatch)
    rows = dh.doctor_checks(cfg)
    names = [c.__name__.removeprefix("_check_") for c in dh.HOST_CHECKS]
    assert [r.name for r in rows] == names
    assert names == [
        "python_uv", "config_valid", "secrets_present", "acl_data", "bitlocker", "audit_chain",
        "disk_free", "clock", "ports_loopback", "ports_expected", "socket_guard", "plugins",
        "services",
    ]  # fmt: skip
    assert {r.name: r.status for r in rows if r.status != "PASS"} == {}
    assert all(r.fix == "" for r in rows)
    assert {argv[0] for argv in fake_runner.argvs} <= wsl.ALLOWED_COMMANDS  # production argv0s


def test_ut10_71_doctor_checks_error_rows_and_pool(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-71 doctor_checks: a HernessError or other exception is a FAIL row, never raised."""
    cfg, _ = env
    _all_green(cfg, fake_runner, monkeypatch)
    threads: set[str] = set()

    def config_error(cfg_dir: Path, profile: str) -> list[ConfigIssue]:
        threads.add(threading.current_thread().name)
        msg = "secret value Sx-never-shown"
        raise ConfigError(msg)

    def os_error(path: Path) -> Any:
        msg = "disk gone"
        raise OSError(msg)

    monkeypatch.setattr(dh, "validate", config_error)
    monkeypatch.setattr(shutil, "disk_usage", os_error)
    rows = _by_name(dh.doctor_checks(cfg))
    assert rows["config_valid"] == dh.HostCheckResult(
        "config_valid", "FAIL", "ConfigError", "herness config validate"
    )
    assert (rows["disk_free"].status, rows["disk_free"].detail) == ("FAIL", "OSError")
    assert threads
    assert all(name.startswith("doctor") for name in threads)


def test_ut10_71_doctor_checks_gpu_extension_point(
    env: tuple[HernessConfig, Host], fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-71 doctor_checks runs ``doctor_gpu.GPU_CHECKS`` after the host rows (U10-89)."""
    cfg, _ = env
    _all_green(cfg, fake_runner, monkeypatch)

    def _check_gpu_probe(cfg: HernessConfig) -> dh.HostCheckResult:
        return dh.HostCheckResult("gpu_probe", "PASS", "ok", "")

    monkeypatch.setattr(doctor_gpu, "GPU_CHECKS", (_check_gpu_probe,))
    rows = dh.doctor_checks(cfg)
    assert rows[-1].name == "gpu_probe"
    assert len(rows) == len(dh.HOST_CHECKS) + 1


def test_ut10_71_gpu_rows_calling_windows_programs_are_windows_only() -> None:
    """UT10-71 every GPU row but model_health runs wsl.exe/PowerShell: Windows-only (IT10-12)."""
    names = {c.__name__.removeprefix("_check_") for c in doctor_gpu.GPU_CHECKS}
    assert names - dh._WINDOWS_ONLY == {"model_health"}
    assert dh._WINDOWS_ONLY - names == {"acl_data", "bitlocker", "clock", "services"}


def test_ut10_71_check_result_is_frozen() -> None:
    """UT10-71 HostCheckResult (spec CheckResult) is frozen with the impl 09 JSON fields."""
    row = dh.HostCheckResult("x", "PASS", "d", "")
    with pytest.raises(AttributeError):
        row.status = "FAIL"  # type: ignore[misc]
    assert list(row.__dataclass_fields__) == ["name", "status", "detail", "fix"]


def test_ut10_71_fake_runner_rejects_unsafe_argv(fake_runner: FakeRunner) -> None:
    """UT10-71 fixture coverage: the fake keeps the runner's allowlist (unlisted argv0 raises).

    The production runner's rejection of unsafe argv is covered by test_admin_wsl.py (T10-22).
    """
    with pytest.raises(ConfigError):
        wsl.run_cmd(["cmd.exe", "/c", "dir"], timeout_s=1)
    assert fake_runner.calls == []
