"""Tests for herness.enrich.laya_models (U03-115 ... U03-119, T03-14)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.support.fake_laya import manifest_dict, write_laya_version

from herness.core.errors import ConfigError, FatalError
from herness.enrich import laya_models
from herness.enrich.laya_models import (
    LayaManifest,
    new_version_id,
    read_current,
    verify_model_dir,
    write_current,
)
from herness.enrich.layout import EnrichPaths

pytestmark = pytest.mark.unit

_V1 = "laya-20261004-1"
_ACCEPTED = frozenset({"accepted"})


@pytest.fixture
def paths(tmp_path: Path) -> EnrichPaths:
    return EnrichPaths(
        data_root=tmp_path.resolve(),
        embedding_path="data/models/e",
        laya_current_file="data/models/laya/CURRENT",
    )


def _manifest(**changes: object) -> dict[str, object]:
    payload = manifest_dict(_V1, {"model.safetensors": "a" * 64}, status="accepted")
    payload["weights_sha256"] = {"model.safetensors": "a" * 64, "rl_agent_config.json": "b" * 64}
    payload.update(changes)
    return payload


def _rewrite_manifest(directory: Path, **changes: object) -> None:
    payload = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    payload.update(changes)
    (directory / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


# --- U03-115 LayaManifest -------------------------------------------------------------


def test_ut03_111_accepted_without_accepted_by_rejected() -> None:
    """UT03-111 status accepted without accepted_by fails validation."""
    with pytest.raises(ValidationError, match="accepted_by"):
        LayaManifest.model_validate_json(json.dumps(_manifest(accepted_by=None)))


def test_ut03_111_accepted_without_accepted_at_rejected() -> None:
    """UT03-111 status accepted without accepted_at fails validation."""
    with pytest.raises(ValidationError, match="accepted_at"):
        LayaManifest.model_validate_json(json.dumps(_manifest(accepted_at=None)))


def test_ut03_111_valid_manifests_parse() -> None:
    """UT03-111 accepted with both fields and a bare candidate both validate."""
    accepted = LayaManifest.model_validate_json(json.dumps(_manifest()))
    candidate = LayaManifest.model_validate_json(
        json.dumps(_manifest(status="candidate", accepted_by=None, accepted_at=None))
    )
    assert accepted.accepted_by == "admin"
    assert accepted.created_at.tzinfo is not None
    assert candidate.status == "candidate"


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "laya-2026-1"},
        {"weights_sha256": {"rl_agent_config.json": "b" * 64}},
        {"weights_sha256": {"model.safetensors": "a" * 64}},
        {"weights_sha256": {"model.safetensors": "XYZ", "rl_agent_config.json": "b" * 64}},
        {"weights_sha256": {"../x": "a" * 64, "model.safetensors": "a" * 64}},
        {"hyperparams": {"seed": 7}},
        {"teacher": "jev"},
        {"surprise": 1},
        {"n_train": "1000"},
    ],
)
def test_ut03_111_invalid_manifest_fields_rejected(changes: dict[str, object]) -> None:
    """UT03-111 bad version, missing required weights, bad hashes/keys, extras fail."""
    with pytest.raises(ValidationError):
        LayaManifest.model_validate_json(json.dumps(_manifest(**changes)))


# --- U03-116 / U03-117 CURRENT --------------------------------------------------------


def test_ut03_112_write_then_read(paths: EnrichPaths) -> None:
    """UT03-112 write_current then read_current round-trips; file holds version + newline."""
    write_current(paths, _V1)
    assert paths.laya_current().read_bytes() == f"{_V1}\n".encode()
    assert read_current(paths) == _V1
    write_current(paths, "laya-20261004-2")
    assert read_current(paths) == "laya-20261004-2"
    assert sorted(p.name for p in paths.laya_current().parent.iterdir()) == ["CURRENT"]


@pytest.mark.parametrize(
    "content",
    [b"", b"../../etc\n", b"laya-2026-1\n", b"\xff\xfe", b"laya-20261004-1" + b" " * 60],
)
def test_ut03_112_bad_content_raises(paths: EnrichPaths, content: bytes) -> None:
    """UT03-112 empty, traversal, malformed, non-UTF-8 or oversized CURRENT -> ConfigError."""
    paths.laya_current().parent.mkdir(parents=True)
    paths.laya_current().write_bytes(content)
    with pytest.raises(ConfigError, match="laya CURRENT missing or invalid"):
        read_current(paths)


def test_ut03_112_missing_current_raises(paths: EnrichPaths) -> None:
    """UT03-112 a missing CURRENT file -> ConfigError."""
    with pytest.raises(ConfigError, match="laya CURRENT missing or invalid"):
        read_current(paths)


def test_ut03_112_write_invalid_version_rejected(paths: EnrichPaths) -> None:
    """UT03-112 write_current refuses a malformed version before touching disk."""
    with pytest.raises(ConfigError):
        write_current(paths, "../../x")
    assert not paths.laya_current().exists()


def test_ut03_112_write_os_error_is_fatal(
    paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-112 an OS error during the atomic replace -> FatalError, no temp file left."""

    def boom(src: object, dst: object) -> None:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(laya_models.os, "replace", boom)
    with pytest.raises(FatalError, match="laya CURRENT"):
        write_current(paths, _V1)
    assert list(paths.laya_current().parent.iterdir()) == []


# --- U03-118 verify_model_dir ---------------------------------------------------------


def test_ut03_113_good_directory_verifies(paths: EnrichPaths) -> None:
    """UT03-113 a consistent directory returns its manifest, with and without status."""
    write_laya_version(paths.laya_root(), _V1)
    manifest = verify_model_dir(paths, _V1)
    assert manifest.version == _V1
    assert verify_model_dir(paths, _V1, require_status=_ACCEPTED).status == "accepted"


def test_ut03_113_checkpoints_are_ignored(paths: EnrichPaths) -> None:
    """UT03-113 *.pt and unlisted files under checkpoints/ neither fail nor get hashed."""
    directory = write_laya_version(paths.laya_root(), _V1)
    (directory / "checkpoints" / "epoch-1").mkdir(parents=True)
    (directory / "checkpoints" / "epoch-1" / "optimizer.pt").write_bytes(b"x")
    (directory / "eval.json").write_text("{}", encoding="utf-8")
    assert verify_model_dir(paths, _V1).version == _V1


@pytest.mark.parametrize("name", ["model.bin", "extra.pt", "sub/x.pkl", "a.CKPT"])
def test_ut03_113_pickle_format_file_rejected(paths: EnrichPaths, name: str) -> None:
    """UT03-113 an extra model.bin (or *.pt/*.pkl/*.ckpt) outside checkpoints/ -> ConfigError."""
    directory = write_laya_version(paths.laya_root(), _V1)
    (directory / name).parent.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(b"\x80\x04")
    with pytest.raises(ConfigError, match=f"{_V1}: pickle-format file present") as info:
        verify_model_dir(paths, _V1)
    assert info.value.context["version"] == _V1


def test_ut03_113_symlinked_dir_rejected(paths: EnrichPaths, tmp_path: Path) -> None:
    """UT03-113 a symlinked (or junction) version directory -> ConfigError."""
    real = write_laya_version(tmp_path / "elsewhere", _V1)
    paths.laya_root().mkdir(parents=True)
    link = paths.laya_root() / _V1
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi  # noqa: PLC0415 - Windows only: junctions need no developer mode

        _winapi.CreateJunction(str(real), str(link))
    with pytest.raises(ConfigError, match=f"{_V1}: directory is a link"):
        verify_model_dir(paths, _V1)


def test_ut03_113_reparse_point_rejected(
    paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-113 a directory carrying the Windows reparse-point attribute -> ConfigError."""
    write_laya_version(paths.laya_root(), _V1)
    monkeypatch.setattr(laya_models, "_is_reparse", lambda st: True)
    with pytest.raises(ConfigError, match="directory is a link"):
        verify_model_dir(paths, _V1)


def test_ut03_113_wrong_status_rejected(paths: EnrichPaths) -> None:
    """UT03-113 a candidate directory when accepted is required -> ConfigError."""
    write_laya_version(paths.laya_root(), _V1, status="candidate")
    with pytest.raises(ConfigError, match=f"{_V1}: status not allowed"):
        verify_model_dir(paths, _V1, require_status=_ACCEPTED)


def _missing_model(directory: Path) -> None:
    (directory / "model.safetensors").unlink()


def _huge_manifest(directory: Path) -> None:
    (directory / "manifest.json").write_bytes(b" " * (256 * 1024 + 1))


def _bad_json(directory: Path) -> None:
    (directory / "manifest.json").write_text("{not json", encoding="utf-8")


def _other_version(directory: Path) -> None:
    _rewrite_manifest(directory, version="laya-20261004-9")


def _unlisted(directory: Path) -> None:
    (directory / "special_tokens_map.json").write_text("{}", encoding="utf-8")


def _listed_missing(directory: Path) -> None:
    (directory / "tokenizer.json").unlink()


def _no_manifest(directory: Path) -> None:
    (directory / "manifest.json").unlink()


def _nested_link(directory: Path) -> None:
    try:
        (directory / "linked.json").symlink_to(directory / "tokenizer.json")
    except OSError:
        pytest.skip("symlinks need developer mode on Windows")


@pytest.mark.parametrize(
    ("mutate", "check"),
    [
        (_missing_model, "model.safetensors missing"),
        (_huge_manifest, "manifest too large"),
        (_bad_json, "manifest invalid"),
        (_no_manifest, "manifest missing"),
        (_other_version, "manifest version mismatch"),
        (_unlisted, "file not listed in manifest"),
        (_listed_missing, "weight file missing"),
        (_nested_link, "linked entry present"),
    ],
)
def test_ut03_113_failed_checks_named(paths: EnrichPaths, mutate: object, check: str) -> None:
    """UT03-113 each failed check -> ConfigError naming the version and the check."""
    directory = write_laya_version(paths.laya_root(), _V1)
    assert callable(mutate)
    mutate(directory)
    with pytest.raises(ConfigError, match=f"^{_V1}: {check}$"):
        verify_model_dir(paths, _V1)


def test_ut03_113_missing_directory_rejected(paths: EnrichPaths) -> None:
    """UT03-113 a version without a directory -> ConfigError; a file is not a directory."""
    with pytest.raises(ConfigError, match=f"{_V1}: directory missing"):
        verify_model_dir(paths, _V1)
    paths.laya_root().mkdir(parents=True)
    (paths.laya_root() / _V1).write_bytes(b"")
    with pytest.raises(ConfigError, match=f"{_V1}: not a directory"):
        verify_model_dir(paths, _V1)


def test_ut03_113_invalid_version_rejected(paths: EnrichPaths) -> None:
    """UT03-113 a traversing version never reaches the filesystem."""
    with pytest.raises(ConfigError, match="invalid version"):
        verify_model_dir(paths, "../laya-20261004-1")


def test_ut03_113_hash_memoized_per_stat(
    paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-113 hashes are memoized per (path, size, mtime_ns); a changed mtime rehashes."""
    directory = write_laya_version(paths.laya_root(), "laya-20261004-7")
    real = laya_models._hash_file
    calls: list[str] = []

    def counting(path: Path) -> str:
        calls.append(path.name)
        return real(path)

    monkeypatch.setattr(laya_models, "_hash_file", counting)
    verify_model_dir(paths, "laya-20261004-7")
    first = len(calls)
    verify_model_dir(paths, "laya-20261004-7")
    assert len(calls) == first == 4
    stat = (directory / "tokenizer.json").stat()
    os.utime(directory / "tokenizer.json", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000))
    verify_model_dir(paths, "laya-20261004-7")
    assert calls[first:] == ["tokenizer.json"]


def test_ut03_113_hash_reads_in_blocks(tmp_path: Path) -> None:
    """UT03-113 files larger than one 8 MB block hash to the same digest as hashlib."""
    content = bytes(range(256)) * (40_000)  # ~10 MB, two blocks
    path = tmp_path / "big.safetensors"
    path.write_bytes(content)
    assert laya_models._hash_file(path) == hashlib.sha256(content).hexdigest()


def test_ut03_113_concurrent_verification_is_consistent(paths: EnrichPaths) -> None:
    """UT03-113 verify_model_dir from several threads returns the same manifest."""
    write_laya_version(paths.laya_root(), _V1)
    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(verify_model_dir(paths, _V1).version))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [_V1] * 4


# --- U03-119 new_version_id -----------------------------------------------------------


def test_ut03_114_next_id_after_existing(paths: EnrichPaths) -> None:
    """UT03-114 existing -1 and -2 on the same day -> -3; other days and junk are ignored."""
    root = paths.laya_root()
    for name in ["laya-20261004-1", "laya-20261004-2", "laya-20261003-9", "notes", "laya-x"]:
        (root / name).mkdir(parents=True)
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    assert new_version_id(paths, now=now) == "laya-20261004-3"


def test_ut03_114_first_id_and_utc_date(paths: EnrichPaths) -> None:
    """UT03-114 no directories -> -1; the date is taken in UTC."""
    late = datetime(2026, 10, 4, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert new_version_id(paths, now=late) == "laya-20261005-1"


def test_ut03_114_naive_datetime_rejected(paths: EnrichPaths) -> None:
    """UT03-114 a naive `now` is refused (the date must be UTC)."""
    with pytest.raises(ConfigError, match="timezone"):
        new_version_id(paths, now=datetime(2026, 10, 4))  # noqa: DTZ001 - the case under test
