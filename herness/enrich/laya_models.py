"""Laya model files: manifest, `CURRENT`, directory verification, version ids (impl 03 §3.19).

`verify_model_dir` is the load and promotion gate of `data/models/laya/<version>/` (design
03 §4.4, §9; TH03-05, TH03-08, TH03-16). Errors name the version and the failed check only.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from herness.core.errors import ConfigError, FatalError
from herness.enrich.layout import EnrichPaths

__all__ = ["LayaManifest", "new_version_id", "read_current", "verify_model_dir", "write_current"]

_VERSION_PATTERN: Final = r"^laya-\d{8}-\d+$"
_VERSION_RE: Final = re.compile(r"^laya-(\d{8})-(\d+)$")
_SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
_REL_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*$")
_MAX_MANIFEST_BYTES: Final = 256 * 1024
_MAX_CURRENT_BYTES: Final = 64
_HASH_BLOCK: Final = 8 * 1024 * 1024
_UNHASHED: Final = frozenset({"manifest.json", "eval.json", "calibration.json"})
_PICKLE_SUFFIXES: Final = frozenset({".bin", ".pt", ".pkl", ".ckpt"})
_REQUIRED_WEIGHTS: Final = ("model.safetensors", "rl_agent_config.json")
_REQUIRED_HYPERPARAMS: Final = ("trainer", "seed", "round", "round_kind")
_CURRENT_INVALID: Final = "laya CURRENT missing or invalid"

_hash_memo: dict[tuple[str, int, int], str] = {}
_hash_lock = threading.Lock()


class LayaManifest(BaseModel):
    """`manifest.json` of a Laya version (U03-115, design 03 §4.4); immutable."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: str = Field(pattern=_VERSION_PATTERN)
    parent_version: str | None = Field(pattern=_VERSION_PATTERN)
    base_checkpoint: str = Field(min_length=1, max_length=256)
    teacher: Literal["openjev", "llm"]
    teacher_version: str = Field(min_length=1, max_length=128)
    question_set_version: str = Field(min_length=1, max_length=64)
    train_data_sha256: str = Field(pattern=_SHA256_PATTERN)
    n_train: int = Field(ge=0)
    hyperparams: dict[str, str | int | float | bool]
    weights_sha256: dict[str, str]
    accepted_questions: list[str]
    status: Literal["candidate", "accepted", "retired"]
    created_at: datetime
    accepted_by: str | None
    accepted_at: datetime | None

    @model_validator(mode="after")
    def _check(self) -> LayaManifest:
        if self.status == "accepted" and (self.accepted_by is None or self.accepted_at is None):
            msg = "accepted manifest needs accepted_by and accepted_at"
            raise ValueError(msg)
        if any(key not in self.hyperparams for key in _REQUIRED_HYPERPARAMS):
            msg = "hyperparams must include trainer, seed, round and round_kind"
            raise ValueError(msg)
        if any(name not in self.weights_sha256 for name in _REQUIRED_WEIGHTS):
            msg = "weights_sha256 must cover model.safetensors and rl_agent_config.json"
            raise ValueError(msg)
        for name, digest in self.weights_sha256.items():
            bad_name = _REL_NAME_RE.fullmatch(name) is None or ".." in name.split("/")
            if bad_name or re.fullmatch(_SHA256_PATTERN, digest) is None:
                msg = "weights_sha256 needs relative file names and sha256 hex digests"
                raise ValueError(msg)
        return self


def read_current(paths: EnrichPaths) -> str:
    """Return the active Laya version from `CURRENT` (U03-116). Raises ConfigError.

    Reads at most 64 bytes; the stripped value must match `^laya-\\d{8}-\\d+$` (TH03-08).
    """
    try:
        with paths.laya_current().open("rb") as handle:
            raw = handle.read(_MAX_CURRENT_BYTES + 1)
        version = raw.decode("ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(_CURRENT_INVALID) from exc
    if len(raw) > _MAX_CURRENT_BYTES or _VERSION_RE.fullmatch(version) is None:
        raise ConfigError(_CURRENT_INVALID)
    return version


def write_current(paths: EnrichPaths, version: str) -> None:
    """Atomically point `CURRENT` at `version` (U03-117): temp file, fsync, `os.replace`.

    Callers verify the version first. ConfigError for a bad version, FatalError on OS errors.
    """
    paths.laya_dir(version)  # validates the version pattern (TH03-08)
    target = paths.laya_current()
    tmp_name: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".CURRENT.", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            handle.write(f"{version}\n".encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except OSError as exc:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)
        msg = "could not write laya CURRENT"
        raise FatalError(msg, version=version) from exc


def _fail(version: str, check: str) -> NoReturn:
    msg = f"{version}: {check}"
    raise ConfigError(msg, version=version, check=check)


def _is_reparse(st: os.stat_result) -> bool:
    """True for symlinks and, on Windows, junctions and other reparse points."""
    attributes = getattr(st, "st_file_attributes", 0)
    return stat.S_ISLNK(st.st_mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _check_directory(paths: EnrichPaths, version: str) -> Path:
    directory = paths.laya_dir(version)
    try:
        st = directory.lstat()
    except FileNotFoundError:
        _fail(version, "directory missing")
    except OSError:
        _fail(version, "directory unreadable")
    if _is_reparse(st):
        _fail(version, "directory is a link")
    if not stat.S_ISDIR(st.st_mode):
        _fail(version, "not a directory")
    if not directory.resolve().is_relative_to(paths.laya_root().resolve()):
        _fail(version, "directory outside the laya root")
    return directory


def _list_files(directory: Path, version: str) -> set[str]:
    """Relative POSIX names of every regular file outside `checkpoints/`."""
    files: set[str] = set()
    for entry in directory.rglob("*"):
        relative = entry.relative_to(directory)
        if relative.parts[0] == "checkpoints":
            continue
        if _is_reparse(entry.lstat()):
            _fail(version, "linked entry present")
        if entry.suffix.lower() in _PICKLE_SUFFIXES:
            _fail(version, "pickle-format file present")
        if entry.is_file():
            files.add(relative.as_posix())
    return files


def _read_manifest(directory: Path, version: str) -> LayaManifest:
    try:
        with (directory / "manifest.json").open("rb") as handle:
            raw = handle.read(_MAX_MANIFEST_BYTES + 1)
    except FileNotFoundError:
        _fail(version, "manifest missing")
    except OSError:
        _fail(version, "manifest unreadable")
    if len(raw) > _MAX_MANIFEST_BYTES:
        _fail(version, "manifest too large")
    try:
        return LayaManifest.model_validate_json(raw)
    except ValidationError:
        _fail(version, "manifest invalid")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_HASH_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    """SHA-256 of `path`, memoized per (path, size, mtime_ns) for the process."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _hash_lock:
        cached = _hash_memo.get(key)
    if cached is None:
        cached = _hash_file(path)
        with _hash_lock:
            _hash_memo[key] = cached
    return cached


def _check_hashes(directory: Path, manifest: LayaManifest, files: set[str]) -> None:
    version = manifest.version
    if files - _UNHASHED - set(manifest.weights_sha256):
        _fail(version, "file not listed in manifest")
    for name, expected in sorted(manifest.weights_sha256.items()):
        if name not in files:
            _fail(version, "weight file missing")
        try:
            actual = _sha256(directory / name)
        except OSError:
            _fail(version, "weight file unreadable")
        if actual != expected:
            _fail(version, "weight hash mismatch")


def verify_model_dir(
    paths: EnrichPaths, version: str, *, require_status: frozenset[str] | None = None
) -> LayaManifest:
    """Validate a Laya version directory before load or promotion (U03-118, design 03 §9).

    Checks, in order: the directory exists under `laya_root()` and is not a link; no
    entry outside `checkpoints/` is a link or a `*.bin`/`*.pt`/`*.pkl`/`*.ckpt` file;
    `model.safetensors` exists; the manifest (≤ 256 KB) parses and names `version`; the
    status is in `require_status` when given; every file is listed in `weights_sha256`
    (except manifest, eval and calibration) and every listed file's SHA-256 matches.
    Thread-safe. Raises ConfigError naming the version and the failed check.
    """
    directory = _check_directory(paths, version)
    try:
        files = _list_files(directory, version)
    except OSError:
        _fail(version, "directory unreadable")
    if "model.safetensors" not in files:
        _fail(version, "model.safetensors missing")
    manifest = _read_manifest(directory, version)
    if manifest.version != version:
        _fail(version, "manifest version mismatch")
    if require_status is not None and manifest.status not in require_status:
        _fail(version, "status not allowed")
    _check_hashes(directory, manifest, files)
    return manifest


def new_version_id(paths: EnrichPaths, *, now: datetime) -> str:
    """Next free id `laya-<yyyymmdd>-<n>` for the UTC date of `now` (U03-119).

    The caller creates it with `mkdir(exist_ok=False)`; `FileExistsError` -> `StoreBusy`.
    """
    if now.tzinfo is None:
        msg = "new_version_id needs a timezone-aware datetime"
        raise ConfigError(msg)
    day = now.astimezone(UTC).strftime("%Y%m%d")
    root = paths.laya_root()
    matches = [_VERSION_RE.fullmatch(e.name) for e in root.iterdir()] if root.is_dir() else []
    numbers = [int(m.group(2)) for m in matches if m is not None and m.group(1) == day]
    return f"laya-{day}-{max(numbers, default=0) + 1}"
