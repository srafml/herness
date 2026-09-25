"""Tests for herness.core.logging (U00-35 … U00-39)."""

import asyncio
import datetime
import io
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from herness.core import time as clock
from herness.core._log_pipeline import DailyJsonlHandler
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.ids import IdKind, new_id
from herness.core.logging import bind_ids, configure_logging, get_logger, reset_logging

pytestmark = pytest.mark.unit

SENTINEL = "SENTINEL-SECRET-9f3a"
UTC = datetime.UTC
_EARLY = get_logger("core.early")


def _passthrough(logger: object, method_name: str, event_dict: Any) -> Any:
    return event_dict


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    reset_logging()


def _file_lines(log_dir: Path) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("herness-*.jsonl")):
        lines += [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()]
    return lines


def _err_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(text) for text in capsys.readouterr().err.splitlines() if text.strip()]


def _event(lines: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(line for line in lines if line.get("event") == name)


def test_ut00_35_json_line_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-35 one JSON line with the required keys and the extra field."""
    configure_logging("INFO")
    get_logger("core.test").info("core.test.done", n=1)
    line = _event(_err_lines(capsys), "core.test.done")
    assert line["level"] == "info"
    assert line["component"] == "core.test"
    assert len(line["ts"]) == 27
    assert isinstance(line["pid"], int)
    assert line["n"] == 1


def test_ut00_36_day_rollover(configured_logging: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT00-36 lines on either side of UTC midnight go to two day files."""
    current = [datetime.datetime(2030, 1, 1, 23, 59, 59, 900_000, tzinfo=UTC)]
    monkeypatch.setattr(clock, "now", lambda: current[0])
    log = get_logger("core.test")
    log.info("core.test.before")
    current[0] = datetime.datetime(2030, 1, 2, 0, 0, 0, 100_000, tzinfo=UTC)
    log.info("core.test.after")
    first = (configured_logging / "herness-2030-01-01.jsonl").read_text(encoding="utf-8")
    second = (configured_logging / "herness-2030-01-02.jsonl").read_text(encoding="utf-8")
    assert len(first.splitlines()) == 1
    assert len(second.splitlines()) == 1


def test_ut00_37_configuration_errors(tmp_path: Path) -> None:
    """UT00-37 bad level, file without scrubber, and an uncreatable directory."""
    with pytest.raises(ConfigError):
        configure_logging("VERBOSE")
    with pytest.raises(ConfigError):
        configure_logging(log_dir=tmp_path)
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError):
        configure_logging(log_dir=blocker / "sub", scrubber=_passthrough)


@pytest.mark.asyncio
async def test_ut00_38_bind_ids_nesting_and_tasks(configured_logging: Path) -> None:
    """UT00-38 nested bindings stack and restore; asyncio tasks keep their own IDs."""
    log = get_logger("core.test")
    run_id, task_id = new_id(IdKind.RUN), new_id(IdKind.TASK)
    with bind_ids(run_id=run_id):
        with bind_ids(task_id=task_id):
            log.info("core.test.inner")
        log.info("core.test.outer")

    async def work(rid: str) -> None:
        with bind_ids(run_id=rid):
            await asyncio.sleep(0)
            log.info("core.test.task", marker=rid)

    await asyncio.gather(work(new_id(IdKind.RUN)), work(new_id(IdKind.RUN)))
    lines = _file_lines(configured_logging)
    inner, outer = _event(lines, "core.test.inner"), _event(lines, "core.test.outer")
    assert (inner["run_id"], inner["task_id"]) == (run_id, task_id)
    assert outer["run_id"] == run_id
    assert "task_id" not in outer
    tasks = [line for line in lines if line["event"] == "core.test.task"]
    assert len(tasks) == 2
    assert all(line["run_id"] == line["marker"] for line in tasks)


def test_ut00_39_bind_ids_rejects(configured_logging: Path) -> None:
    """UT00-39 unknown keys and invalid IDs are refused without echoing the value."""
    with pytest.raises(SchemaViolation), bind_ids(user="x"):
        pass
    with pytest.raises(SchemaViolation) as info, bind_ids(run_id="run_bad\n{"):
        pass
    assert all("run_bad" not in str(value) for value in info.value.context.values())


def test_ut00_40_get_logger_component() -> None:
    """UT00-40 invalid component names are refused."""
    for bad in ("Harness", "a" * 65):
        with pytest.raises(SchemaViolation):
            get_logger(bad)
    assert get_logger("harness.tool") is not None


def test_ut00_41_key_order(configured_logging: Path) -> None:
    """UT00-41 required keys, pid and context IDs come first, then insertion order."""
    with bind_ids(job_id=new_id(IdKind.JOB)):
        get_logger("core.test").info("core.test.order", zeta=1, alpha=2)
    line = _event(_file_lines(configured_logging), "core.test.order")
    assert list(line)[:8] == [
        "ts",
        "level",
        "event",
        "component",
        "pid",
        "job_id",
        "zeta",
        "alpha",
    ]


def test_ut00_42_event_names(configured_logging: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-42 strict mode raises to the caller; non-strict marks the line."""
    with pytest.raises(SchemaViolation):
        get_logger("core.test").info("Bad Event")
    configure_logging("INFO")
    get_logger("core.test").info("Bad Event")
    line = _event(_err_lines(capsys), "Bad Event")
    assert line["event_name_invalid"] is True


def test_ut00_43_third_party_loggers(capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-43 noisy loggers drop INFO; foreign records get an ext.* component."""
    configure_logging("INFO")
    logging.getLogger("httpx").info("hidden")
    logging.getLogger("httpx").warning("shown")
    logging.getLogger("thirdparty.sub").warning("other")
    lines = _err_lines(capsys)
    assert not [line for line in lines if line["event"] == "hidden"]
    assert _event(lines, "shown")["component"] == "ext.httpx"
    assert _event(lines, "other")["component"] == "ext.thirdparty"


def _fail() -> None:
    password_local = SENTINEL  # noqa: F841 - must not reach the log
    raise ValueError("boom")  # noqa: EM101


def test_ut00_44_exception_without_locals(configured_logging: Path) -> None:
    """UT00-44 exceptions are rendered without locals, so local secrets never appear."""
    try:
        _fail()
    except ValueError:
        get_logger("core.test").exception("core.test.failed")
    line = _event(_file_lines(configured_logging), "core.test.failed")
    assert "exception" in line
    frames = line["exception"][0]["frames"]
    assert frames
    assert all("locals" not in frame for frame in frames)
    for path in configured_logging.glob("*.jsonl"):
        assert SENTINEL not in path.read_text(encoding="utf-8")


def test_ut00_45_reset(configured_logging: Path) -> None:
    """UT00-45 reset removes Herness handlers, closes the file and clears context."""
    get_logger("core.test").info("core.test.x")
    reset_logging()
    root = logging.getLogger()
    assert not [h for h in root.handlers if isinstance(h, DailyJsonlHandler)]
    for path in configured_logging.glob("*.jsonl"):
        os.remove(path)
    assert structlog.contextvars.get_contextvars() == {}


def test_ut00_46_configured_event(configured_logging: Path) -> None:
    """UT00-46 the first file line is core.logging.configured with scrubber true."""
    first = _file_lines(configured_logging)[0]
    assert first["event"] == "core.logging.configured"
    assert first["scrubber"] is True


def test_st00_01_sentinel_never_written(
    capsys: pytest.CaptureFixture[str], configured_logging: Path
) -> None:
    """ST00-01 the scrubber removes the sentinel from values, exceptions and long strings."""
    log = get_logger("core.test")
    log.info("core.test.value", value="x " + SENTINEL)
    try:
        raise ValueError(SENTINEL)  # noqa: TRY301 - test needs a real traceback frame here
    except ValueError:
        log.exception("core.test.exc")
    log.info("core.test.long", long="a" * 1995 + SENTINEL + "b" * 100)
    text = "".join(p.read_text(encoding="utf-8") for p in configured_logging.glob("*.jsonl"))
    assert SENTINEL not in text
    assert SENTINEL not in capsys.readouterr().err
    assert "***" in text


def test_st00_02_nested_secrets_never_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST00-02 secret and free-text keys in lists, at level 5 and deeper never reach a sink."""
    configure_logging("INFO", log_dir=tmp_path, scrubber=_passthrough)
    log = get_logger("core.test")
    log.info("core.test.list", users=[{"api_key": SENTINEL}], items=[{"description": SENTINEL}])
    log.info("core.test.level5", a={"b": {"c": {"d": {"password": SENTINEL}}}})
    log.info("core.test.deep", a={"b": {"c": {"d": {"e": {"api_key": SENTINEL}}}}})
    err = capsys.readouterr().err
    text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.jsonl"))
    for output in (err, text):
        assert SENTINEL not in output
        assert "core.test.deep" in output
    deep = _event([json.loads(line) for line in text.splitlines()], "core.test.deep")
    assert deep["a"]["b"]["c"]["d"]["e"] == "[too deep]"


def test_st00_01_foreign_root_handler_sees_no_secret(configured_logging: Path) -> None:
    """ST00-01 a root handler Herness did not install gets an already guarded, scrubbed record."""
    stream = io.StringIO()
    foreign = logging.StreamHandler(stream)
    logging.getLogger().addHandler(foreign)
    try:
        get_logger("core.test").info(
            "core.test.foreign", api_key=SENTINEL, value="x " + SENTINEL, nested=[{"token": "t"}]
        )
    finally:
        logging.getLogger().removeHandler(foreign)
    seen = stream.getvalue()
    assert "core.test.foreign" in seen
    assert SENTINEL not in seen
    assert "'t'" not in seen


def _raising_scrubber(logger: object, method_name: str, event_dict: Any) -> Any:
    msg = "scrubber boom"
    raise RuntimeError(msg)


def test_cv_2_scrubber_failure_never_leaks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CV-2 a scrubber that raises never echoes the record; log.info does not raise either."""
    configure_logging("INFO", log_dir=tmp_path, scrubber=_raising_scrubber)
    get_logger("core.test").info("core.test.value", value=SENTINEL)
    err = capsys.readouterr().err
    assert SENTINEL not in err
    err_lines = [json.loads(text) for text in err.splitlines() if text.strip()]
    failed = [line for line in err_lines if line.get("event") == "core.logging.emit_failed"]
    assert failed
    assert failed[0]["component"] == "core.logging"
    for path in tmp_path.glob("*.jsonl"):
        assert SENTINEL not in path.read_text(encoding="utf-8")


def test_cv_3_no_handlers_avoids_last_resort(capsys: pytest.CaptureFixture[str]) -> None:
    """CV-3 stderr=False with no log_dir installs a NullHandler, so lastResort can't fire.

    pytest attaches its own root handler for log capture, which already hides lastResort's
    fallback text from stderr; the direct, deterministic check is that Herness's own
    NullHandler is present on root (found > 0 in Logger.callHandlers keeps lastResort unused).
    """
    configure_logging("WARNING", stderr=False)
    root = logging.getLogger()
    assert any(isinstance(h, logging.NullHandler) for h in root.handlers)
    get_logger("core.test").warning("core.test.warn", value=SENTINEL)
    assert SENTINEL not in capsys.readouterr().err


def test_rf_logger_created_before_configure(capsys: pytest.CaptureFixture[str]) -> None:
    """RF-5 a logger created at import time still emits configured JSON lines."""
    configure_logging("INFO")
    _EARLY.info("core.early.ready")
    line = _event(_err_lines(capsys), "core.early.ready")
    assert line["component"] == "core.early"
