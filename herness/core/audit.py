"""Hash-chained audit log, locked JSONL append and chain checks (impl 10 U10-60 to U10-64).

Each line holds the SHA-256 of the previous line (design 10 §4.6, TH10-09); the log lock is a
bounded lock wait, not a retry (delta D10-08).
"""

from __future__ import annotations

import hmac
import importlib
import json
import os
import re
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

import yaml

from herness.core import time as clock
from herness.core.config import HernessConfig, config_hash, effective_dict, get_config
from herness.core.errors import ConfigError, FatalError, SchemaViolation, StoreBusy
from herness.core.ids import canonical_json, is_valid_ulid, new_ulid, sha256_hex
from herness.core.logging import get_logger
from herness.core.redact_patterns import build_detectors
from herness.core.settings import RedactionConfig

__all__ = ["AuditEvent", "ChainReport", "append_jsonl_locked", "audit", "last_secret_set_times"]
__all__ += ["log_lock", "record_config_change", "verify_chain"]

AuditEvent = Literal[
    "review_decision", "recommendation_decision", "config_change", "admin_action", "auth", "egress"
]
_FIELDS: Final[dict[str, frozenset[str]]] = {
    "review_decision": frozenset({"item_id", "kind", "status", "decided_by", "note_len"}),
    "recommendation_decision": frozenset({"rec_id", "decision", "decided_by"}),
    "config_change": frozenset({"old_hash", "new_hash", "changed_paths", "profile"}),
    "admin_action": frozenset({"action", "target"}),
    "auth": frozenset({"user_ref", "role", "result"}),
    "egress": frozenset({"egress_id", "reason"}),
}
_OPTIONAL: Final[dict[str, frozenset[str]]] = {"admin_action": frozenset({"counts", "detail"})}
_ACTIONS: Final = frozenset({
    "secret_set", "secret_rotate", "privacy_delete", "deploy_up", "deploy_down", "deploy_rollback",
    "deploy_pull", "deploy_prune", "deploy_install", "deploy_render", "profile_switch", "purge",
    "backup", "redact_rekey",
})  # fmt: skip
_ACTOR_RE: Final = re.compile(r"[0-9a-f]{32}")
_COUNTS_RE: Final = re.compile(r"[A-Za-z0-9_.]+=[^;=]*(?:;[A-Za-z0-9_.]+=[^;=]*)*")
_LINE_KEYS: Final = frozenset(
    {"ts", "audit_id", "event", "actor", "fields", "config_hash", "prev_hash"}
)
_MAX_STR: Final = 256
_MAX_LIST: Final = 200
_CHAIN_GLOB: Final = "audit-*.jsonl"
_LOCK_NAME: Final = ".audit.lock"
_POLL_S: Final = 0.05
_IN_PROGRESS_S: Final = 2.0
_CREDENTIAL: Final = next(d for d in build_detectors(RedactionConfig()) if d.type == "CREDENTIAL")
_log = get_logger("core.audit")

# Injectable for FT10-01 so a 10 s lock wait runs in fake time.
_monotonic: Callable[[], float] = clock.monotonic
_sleep: Callable[[float], None] = clock.sleep

if sys.platform == "win32":  # pragma: no cover - platform branch, runs on Windows only
    import msvcrt

    def _os_lock(fd: int, *, unlock: bool = False) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)

else:  # pragma: no cover - platform branch, runs on POSIX only
    import fcntl

    def _os_lock(fd: int, *, unlock: bool = False) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _busy(path: Path, *, timed_out: bool = False) -> StoreBusy:
    return StoreBusy(f"log {'lock timeout' if timed_out else 'write failed'}: {path.name}")


@contextmanager
def log_lock(lock_path: Path, *, timeout_s: float = 10.0) -> Iterator[None]:
    """Hold the per-process and the OS lock on ``lock_path``; StoreBusy after ``timeout_s``."""
    local = _THREAD_LOCKS.setdefault(str(lock_path.resolve()), threading.Lock())  # atomic
    with ExitStack() as stack:
        if not local.acquire(timeout=timeout_s):
            raise _busy(lock_path, timed_out=True)
        stack.callback(local.release)
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise _busy(lock_path) from exc
        stack.callback(os.close, fd)
        deadline = _monotonic() + timeout_s
        while True:
            try:
                _os_lock(fd)
                break
            except OSError:
                if _monotonic() >= deadline:
                    raise _busy(lock_path, timed_out=True) from None
                _sleep(_POLL_S)
        stack.callback(_os_lock, fd, unlock=True)
        yield


def _last_line(path: Path) -> bytes | None:
    """The last ``\\n``-terminated line of ``path`` (without the newline), read from the end."""
    with path.open("rb") as fh:
        buf, pos = b"", fh.seek(0, os.SEEK_END)
        while pos > 0 and buf.count(b"\n") < 2:  # noqa: PLR2004 - the terminator and the newline before
            step = min(4096, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
    head, sep, _tail = buf.rpartition(b"\n")
    return head.rpartition(b"\n")[2] if sep else None


def _previous_line(path: Path, chain_glob: str) -> bytes | None:
    earlier = sorted((p for p in path.parent.glob(chain_glob) if p.name < path.name), reverse=True)
    for candidate in [path, *earlier]:
        if candidate.exists() and (line := _last_line(candidate)) is not None:
            return line
    return None


def append_jsonl_locked(
    path: Path,
    line_builder: Callable[[bytes | None], bytes],
    *,
    lock_path: Path,
    chain_glob: str | None = None,
    lock_held: bool = False,
) -> None:
    """Append ``line_builder(prev)`` under the log lock; OSError becomes StoreBusy (U10-61)."""
    with nullcontext() if lock_held else log_lock(lock_path):
        try:
            data = line_builder(_previous_line(path, chain_glob) if chain_glob else None)
            with path.open("ab") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise _busy(path) from exc


def _known_values() -> frozenset[str]:
    """Secret values known to this process (U10-32), imported late to avoid a cycle."""
    try:
        return frozenset(importlib.import_module("herness.core.secrets").known_values())
    except ModuleNotFoundError as exc:
        if exc.name != "herness.core.secrets":
            raise
        return frozenset()  # T10-06: herness.core.secrets is not on the branch yet.


def _is_secret(value: str, known: frozenset[str]) -> bool:
    found = _CREDENTIAL.prefilter(value) and next(iter(_CREDENTIAL.find(value)), None) is not None
    return found or any(item and item in value for item in known)


def _valid_value(value: object) -> bool:
    if isinstance(value, str):
        return len(value) <= _MAX_STR
    if isinstance(value, list):
        ok = all(isinstance(v, str) and len(v) <= _MAX_STR for v in value)
        return ok and len(value) <= _MAX_LIST
    return value is None or isinstance(value, int)


def _validate(event: str, actor: str, fields: dict[str, Any]) -> None:
    if not (actor in {"system", "eval"} or _ACTOR_RE.fullmatch(actor)):
        msg = "audit actor invalid"
        raise SchemaViolation(msg)
    required, optional = _FIELDS.get(event, frozenset()), _OPTIONAL.get(event, frozenset())
    counts = fields.get("counts")
    if not (
        required
        and required <= fields.keys() <= required | optional
        and all(_valid_value(v) for v in fields.values())
        and (event != "admin_action" or fields["action"] in _ACTIONS)
        and (counts is None or (isinstance(counts, str) and _COUNTS_RE.fullmatch(counts)))
    ):
        msg = "audit fields invalid"  # generic: values and key names are never echoed (ENG §3.4)
        raise SchemaViolation(msg)
    known = _known_values()
    strings = [v for f in fields.values() for v in (f if isinstance(f, list) else [f])]
    if any(isinstance(s, str) and _is_secret(s, known) for s in strings):
        _log.error("audit.write.failed", audit_event=event, error_type="SchemaViolation")
        msg = "audit field would contain a secret"
        raise SchemaViolation(msg)


_HASH_MEMO: list[tuple[HernessConfig, str | None]] = []  # config_hash of the last config seen


def _cached_hash(cfg: HernessConfig) -> str | None:
    if not _HASH_MEMO or _HASH_MEMO[0][0] is not cfg:
        try:
            value: str | None = config_hash(cfg)
        except ConfigError:
            value = None
        _HASH_MEMO[:] = [(cfg, value)]
    return _HASH_MEMO[0][1]


def _audit_locked(
    cfg: HernessConfig, event: str, actor: str, fields: dict[str, Any], *, lock_held: bool
) -> None:
    """U10-60 body: validate, build the chained line and append it."""
    _validate(event, actor, fields)
    logs = Path(cfg.paths.logs)
    hash_value = _cached_hash(cfg)

    def build(prev: bytes | None) -> bytes:
        record = {
            "ts": clock.format_utc(clock.now()),
            "audit_id": "aud_" + new_ulid(),
            "event": event,
            "actor": actor,
            "fields": fields,
            "config_hash": hash_value,
            "prev_hash": "0" * 64 if prev is None else sha256_hex(prev),
        }
        return (canonical_json(record) + "\n").encode("utf-8")

    try:
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / f"audit-{clock.utc_day(clock.now())}.jsonl"
        lock, glob = logs / _LOCK_NAME, _CHAIN_GLOB
        append_jsonl_locked(path, build, lock_path=lock, chain_glob=glob, lock_held=lock_held)
    except (StoreBusy, OSError) as exc:
        _log.error("audit.write.failed", audit_event=event, error_type=type(exc).__name__)
        msg = f"audit write failed: {event}"
        raise FatalError(msg) from exc
    # T08-05: herness_audit_lines_total{event} += 1


def audit(event: AuditEvent, actor: str, **fields: Any) -> None:  # noqa: ANN401 - spec signature
    """Append one hash-chained audit line; FatalError when it cannot be written (U10-60)."""
    _audit_locked(get_config(), event, actor, fields, lock_held=False)


@dataclass(frozen=True)
class ChainReport:
    """Result of ``verify_chain``; ``first_break`` is ``<file>:<line number>``."""

    ok: bool
    files: int
    lines: int
    first_break: str | None


def _parse_line(raw: bytes) -> tuple[str, str] | None:
    """``(ts, prev_hash)`` of a well-formed audit line (§4.6 keys, ``aud_`` ID), else None."""
    try:
        record = json.loads(raw.decode("utf-8"))
        ts, prev_hash, audit_id = record["ts"], record["prev_hash"], record["audit_id"]
        clock.parse_utc(ts)
        shaped = set(record) == _LINE_KEYS and prev_hash.isascii() and audit_id.startswith("aud_")
    except (ValueError, TypeError, KeyError, AttributeError, SchemaViolation):
        return None
    return (ts, prev_hash) if shaped and is_valid_ulid(audit_id[4:]) else None


def _linked(parsed: tuple[str, str], last_ts: str, prev: bytes | None) -> bool:
    ts, prev_hash = parsed
    return ts >= last_ts and (prev is None or hmac.compare_digest(prev_hash, sha256_hex(prev)))


def verify_chain(logs_dir: Path) -> ChainReport:
    """Check every audit line's hash link, shape and ts order; stop at the first break."""
    files = sorted(logs_dir.glob(_CHAIN_GLOB)) if logs_dir.is_dir() else []
    prev: bytes | None = None
    last_ts, count = "", 0
    for path in files:
        try:
            lines = path.read_bytes().split(b"\n")
            stale = clock.now().timestamp() - path.stat().st_mtime >= _IN_PROGRESS_S
        except OSError:
            return ChainReport(False, len(files), count, f"{path.name}:0")
        tail = lines.pop()
        for number, raw in enumerate(lines, start=1):
            parsed = _parse_line(raw)
            if parsed is None or not _linked(parsed, last_ts, prev):
                return ChainReport(False, len(files), count, f"{path.name}:{number}")
            prev, last_ts, count = raw, parsed[0], count + 1
        if tail and stale:  # an unterminated tail older than 2 s is a break, not a write
            return ChainReport(False, len(files), count, f"{path.name}:{len(lines) + 1}")
    return ChainReport(True, len(files), count, None)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _audit_records(logs_dir: Path) -> Iterator[dict[str, Any]]:
    """Parsed audit lines, newest first; unparsable lines are skipped."""
    paths = sorted(logs_dir.glob(_CHAIN_GLOB), reverse=True) if logs_dir.is_dir() else []
    for path in paths:
        for raw in reversed(path.read_bytes().splitlines()):
            with suppress(ValueError):
                record = json.loads(raw)
                if isinstance(record, dict) and isinstance(record.get("fields"), dict):
                    yield record


def _last_audited_hash(cfg: HernessConfig) -> str | None:
    last = Path(cfg.paths.data) / "config_snapshots" / "LAST"
    if last.is_file():
        return last.read_text(encoding="utf-8").strip()
    for record in _audit_records(Path(cfg.paths.logs)):
        if record.get("event") == "config_change":
            value = record["fields"].get("new_hash")
            return value if isinstance(value, str) else None
    return None


def _flatten(data: object, prefix: str = "") -> dict[str, object]:
    if not isinstance(data, dict) or not data:
        return {prefix[:-1]: data}
    return {
        k: v for key, value in data.items() for k, v in _flatten(value, f"{prefix}{key}.").items()
    }


def _changed_paths(old: object, new: object) -> list[str]:
    """Sorted dotted paths that differ; over 200 keeps 199 and a ``+<n> more`` entry."""
    left, right = _flatten(old), _flatten(new)
    paths = sorted(k for k in left.keys() | right.keys() if left.get(k, ...) != right.get(k, ...))
    extra = len(paths) - _MAX_LIST + 1
    return paths if extra <= 1 else [*paths[: _MAX_LIST - 1], f"+{extra} more"]


def record_config_change(cfg: HernessConfig, *, actor: str = "system") -> str:
    """Audit a ``config_change`` and snapshot the config when its hash is new (U10-63)."""
    h = config_hash(cfg)
    logs, snaps = Path(cfg.paths.logs), Path(cfg.paths.data) / "config_snapshots"
    for folder in (logs, snaps):
        folder.mkdir(parents=True, exist_ok=True)
    with log_lock(logs / _LOCK_NAME):
        old = _last_audited_hash(cfg)
        if old == h:
            return h
        new_data = effective_dict(cfg)
        if not (snapshot := snaps / f"{h}.yaml").exists():
            _atomic_write(snapshot, yaml.safe_dump(new_data, sort_keys=True))
        old_file = snaps / f"{old}.yaml"
        old_data = (
            yaml.safe_load(old_file.read_text("utf-8")) if old and old_file.is_file() else None
        )
        changed = [] if old_data is None else _changed_paths(old_data, new_data)
        fields = {"old_hash": old, "new_hash": h, "changed_paths": changed, "profile": cfg.profile}
        _audit_locked(cfg, "config_change", actor, fields, lock_held=True)
        _atomic_write(snaps / "LAST", h)
    _log.info("config.change.recorded", old_hash=old, new_hash=h, changed_count=len(changed))
    return h


def last_secret_set_times(logs_dir: Path, names: Iterable[str]) -> dict[str, datetime | None]:
    """Newest ``secret_set``/``secret_rotate`` time per secret name (U10-64)."""
    found: dict[str, datetime | None] = dict.fromkeys(names)
    missing = set(found)
    for record in _audit_records(logs_dir):
        if not missing:
            break
        fields, target = record["fields"], record["fields"].get("target")
        if record.get("event") != "admin_action" or fields.get("action") not in {
            "secret_set",
            "secret_rotate",
        }:
            continue
        if target in missing:
            with suppress(SchemaViolation):
                found[target] = clock.parse_utc(record.get("ts"))
                missing.discard(target)
    return found
