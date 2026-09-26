"""Tests for the fixture PII scanner (impl 10 T10-11, U10-49).

Real-looking values are built at runtime so no fixture file of the repository holds them.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from herness.core import redact_scan as rs

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
REAL_EMAIL = "jane" + "@acme-corp.com"
ALLOWED_LINES = [
    "alice@example.com wrote to BOB@EXAMPLE.ORG",
    "call +1-202-555-0142 or 202.555.0199",
    "hosts 192.0.2.10 198.51.100.7 203.0.113.99",
    "employee E123456, card 4111 1111 1111 1111",
    "password=synthetic-pass1 and https://h.example/cb?code=SYNTHETIC-abc",
    "ssn 900-12-3456 or 000-12-3456; values 0.333333333 and uuid 00000000-0000-0000-0000-0",
    "plain ticket INC0012345 with no PII",
]


def _scan(capsys: pytest.CaptureFixture[str], *paths: Path) -> tuple[int, list[str]]:
    argv: list[str] = []
    for path in paths:  # relative to the working directory, as the pre-commit hook passes it
        argv += ["--scan", str(path.resolve().relative_to(Path.cwd().resolve()))]
    code = rs.main(argv)
    return code, capsys.readouterr().out.splitlines()


def _write_config(root: Path, herness: str | None, synth: str | None = None) -> None:
    cfg = root / "config"
    (cfg / "profiles").mkdir(parents=True, exist_ok=True)
    if herness is not None:
        (cfg / "herness.yaml").write_text(herness, "utf-8")
    if synth is not None:
        (cfg / "profiles" / "synth.yaml").write_text(synth, "utf-8")


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty working directory (no ``config/``) holding a ``fx`` fixture tree."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fx").mkdir()
    return tmp_path


# --- UT10-47 allowed-only tree exits 0 ----------------------------------------------------------


def test_ut10_47_allowed_synthetic_values_exit_0(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT10-47 a tree of every scanned suffix with only reserved synthetic values: exit 0."""
    body = "\n".join(ALLOWED_LINES) + "\n"
    fx = workdir / "fx"
    for suffix in (".csv", ".json", ".jsonl", ".yaml", ".yml", ".txt", ".md", ".sql"):
        (fx / "nested").mkdir(exist_ok=True)
        (fx / "nested" / f"f{suffix.upper()}").write_text(body, "utf-8")
    pq.write_table(pa.table({"t": ALLOWED_LINES, "n": list(range(7))}), fx / "t.parquet")
    (fx / "skipped.bin").write_text(REAL_EMAIL, "utf-8")  # suffix not scanned
    assert _scan(capsys, fx) == (0, [])


def test_ut10_47_findings_print_location_and_type_never_the_value(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT10-47 a real-looking e-mail and a denylisted domain: exit 1, `path:line:col TYPE`."""
    _write_config(
        workdir,
        "security:\n  redaction: {denylist_domains: [bigbank.test]}\n",
        "security:\n  redaction: {denylist_domains: [bigbank.test, other.test.]}\n",
    )
    fx = workdir / "fx"
    lines = [
        "ok alice@example.com",
        f"mail {REAL_EMAIL} now",
        "see https://API.BigBank.test/x and notbigbank.test or bigbank.test.evil",
        "host other.test",
        "ssn 123-45-6789, ip 10.1.2.3, phone 212-555-7788, card 5500 0000 0000 0004",
        "password=hunter22 and https://h.example/cb?code=realcode1 employee E654321",
    ]
    (fx / "a.txt").write_text("\n".join(lines) + "\n", "utf-8")
    code, out = _scan(capsys, fx)
    assert code == 1
    assert out == [
        "fx/a.txt:2:6 EMAIL",
        "fx/a.txt:3:17 DENYLISTED_DOMAIN",
        "fx/a.txt:4:6 DENYLISTED_DOMAIN",
        "fx/a.txt:5:5 NATIONAL_ID",
        "fx/a.txt:5:21 IP",
        "fx/a.txt:5:37 PHONE",
        "fx/a.txt:5:56 CARD",
        "fx/a.txt:6:10 CREDENTIAL",
        "fx/a.txt:6:49 URL_TOKEN",
    ]
    text = "\n".join(out)
    for value in (REAL_EMAIL, "bigbank", "123-45-6789", "hunter22", "realcode1"):
        assert value.lower() not in text.lower()


def test_ut10_47_parquet_findings_report_row_and_column_index(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT10-47 Parquet: string columns only; line is the row, col the column index."""
    fx = workdir / "fx"
    table = pa.table(
        {
            "n": [1, 2],
            "a": ["alice@example.com", None],
            "b": pa.array(["fine", f"to {REAL_EMAIL}"], type=pa.large_string()),
        }
    )
    pq.write_table(table, fx / "x.parquet")
    assert _scan(capsys, fx / "x.parquet") == (1, ["fx/x.parquet:2:3 EMAIL"])


def test_ut10_47_unreadable_and_too_large_files_are_findings(
    workdir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-47 invalid UTF-8, a corrupt Parquet file and an oversize file are findings."""
    fx = workdir / "fx"
    (fx / "bad.txt").write_bytes(b"\xff\xfe\xfa not utf-8")
    (fx / "bad.parquet").write_bytes(b"not parquet")
    (fx / "big.md").write_text("x" * 64, "utf-8")
    monkeypatch.setattr(rs, "MAX_FILE_BYTES", 32)
    code, out = _scan(capsys, fx)
    assert code == 1
    assert out == [
        "fx/bad.parquet:0:0 unreadable",
        "fx/bad.txt:0:0 unreadable",
        "fx/big.md:0:0 file too large to scan",
    ]


def test_ut10_47_stat_failure_is_unreadable(
    workdir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-47 an OSError while reading a file is the finding `unreadable`."""
    target = workdir / "fx" / "a.txt"
    target.write_text("ok", "utf-8")

    def refuse(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "read_text", refuse)
    assert _scan(capsys, target) == (1, ["fx/a.txt:0:0 unreadable"])


@pytest.mark.parametrize(
    ("argv", "code"),
    [([], 2), (["--scan"], 2), (["--bogus", "x"], 2), (["--help"], 0), (["--scan", "nope"], 2)],
)
def test_ut10_47_usage_errors_exit_2(
    workdir: Path, capsys: pytest.CaptureFixture[str], argv: list[str], code: int
) -> None:
    """UT10-47 missing or unknown arguments and a missing path exit 2; --help exits 0."""
    assert rs.main(argv) == code
    if argv == ["--scan", "nope"]:
        assert capsys.readouterr().err == "redact --scan: path not found: nope\n"


@pytest.mark.parametrize(
    ("herness", "synth"),
    [
        ("security: [unclosed\n", None),
        (None, "security:\n  redaction: {denylist_domains: bigbank.test}\n"),
        ("security:\n  redaction: {denylist_domains: [1]}\n", None),
    ],
)
def test_ut10_47_invalid_config_yaml_exits_2(
    workdir: Path, capsys: pytest.CaptureFixture[str], herness: str | None, synth: str | None
) -> None:
    """UT10-47 invalid YAML or a non-list denylist in the config files exits 2."""
    _write_config(workdir, herness, synth)
    assert rs.main(["--scan", "fx"]) == 2
    assert capsys.readouterr().err.startswith("redact --scan: ")


def test_ut10_47_config_without_denylist_contributes_nothing(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT10-47 herness.yaml without the key and a missing synth.yaml: no denylist."""
    _write_config(workdir, "version: 1\nsecurity: {}\n")
    (workdir / "fx" / "a.txt").write_text("see bigbank.test\n", "utf-8")
    assert _scan(capsys, workdir / "fx") == (0, [])


def test_ut10_47_module_entry_point_on_repository_fixtures_exits_0() -> None:
    """UT10-47 `python -m herness.core.redact --scan tests/fixtures` (the hook) exits 0."""
    done = subprocess.run(
        [sys.executable, "-m", "herness.core.redact", "--scan", "tests/fixtures"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert (done.returncode, done.stdout) == (0, "")


def test_ut10_47_module_entry_point_exit_code_is_the_scan_result(tmp_path: Path) -> None:
    """UT10-47 the `__main__` guard passes the scanner's exit code to the process."""
    (tmp_path / "a.txt").write_text(f"mail {REAL_EMAIL}\n", "utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "herness.core.redact", "--scan", "a.txt"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert (done.returncode, done.stdout) == (1, "a.txt:1:6 EMAIL\n")
