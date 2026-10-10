"""Weight-verification helpers of ``herness deploy pull`` (impl 10 U10-83 steps 1-5).

Size-forced private sibling of ``herness.admin.deploy_pull`` (T10-24 review round 2), which
keeps the public API. In full mode every linked blob is checked over its whole content against
its name: SHA-256 for LFS blobs, the git blob id for the others (review round 3 ruling). Everything
here is fail-closed: a missing or garbled ``stat``, ``find``, ``sha256sum`` or ``git hash-object``
line is a failure status, never ok (TH10-30, TH10-34, TH10-35); ``LIMIT`` caps ``run`` (T10-28).
"""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Final

from herness.admin import wsl
from herness.core import time as clock
from herness.core.config import HernessConfig
from herness.core.ids import canonical_json

__all__ = ["LIMIT", "check_files", "check_snapshot", "hf_repo", "run", "snapshot_links"]
__all__ += ["write_manifest"]

BATCH: Final = 100  # paths per ``stat`` / ``sha256sum`` / ``git hash-object`` call
_LFS_BLOB: Final = re.compile(r"[0-9a-f]{64}")  # LFS blob: named by its content SHA-256
_GIT_BLOB: Final = re.compile(r"[0-9a-f]{40}")  # non-LFS blob: named by its git blob id (SHA-1)
_BLOB_TARGET: Final = re.compile(r"(?:\.\./)+blobs/(?:[0-9a-f]{40}|[0-9a-f]{64})")  # HF links
_SHA_LINE: Final = re.compile(r"([0-9a-f]{64}) [ *](.+)")
# (per-call cap s, monotonic deadline) of the doctor's weights_pinned row; past it, exit 124
LIMIT: ContextVar[tuple[float, float] | None] = ContextVar("pull_verify_limit", default=None)


def hf_repo(cfg: HernessConfig, gpu_class: str) -> tuple[str, str, str]:
    """``(model, revision, repo dir)`` of a Hugging Face class (``decider`` is OpenJev)."""
    section = cfg.deploy.reasoning if gpu_class == "reasoning" else cfg.deploy.openjev
    repo = f"{cfg.deploy.model_root}/hub/models--{section.model.replace('/', '--')}"
    return section.model, section.revision, repo


def run(cfg: HernessConfig, argv: Sequence[str], timeout_s: float = 600) -> wsl.CmdResult:
    """One program in the deploy distro, through ``herness.admin.wsl`` (no shell)."""
    if (limit := LIMIT.get()) is not None:
        timeout_s = min(timeout_s, limit[0], limit[1] - clock.monotonic())
        if timeout_s <= 0:  # the check's deadline has passed: a timeout, fail closed
            return wsl.CmdResult(124, "", "time limit", 0.0)
    return wsl.run_wsl(cfg.deploy.wsl_distro, argv, timeout_s=timeout_s)


def _sizes(cfg: HernessConfig, paths: Sequence[str]) -> dict[str, int]:
    """``{path: size}`` from ``stat``; a path without a parsable line is absent (missing)."""
    found: dict[str, int] = {}
    for start in range(0, len(paths), BATCH):
        out = run(cfg, ["stat", "-c", "%s=%n", *paths[start : start + BATCH]]).stdout
        for line in out.splitlines():
            size, _eq, path = line.partition("=")
            if size.isdigit():
                found[path] = int(size)
    return found


def _sha256(cfg: HernessConfig, paths: Sequence[str]) -> dict[str, str]:
    """``{path: hex}`` from ``sha256sum`` batches; missing or garbled lines are left out."""
    found: dict[str, str] = {}
    for start in range(0, len(paths), BATCH):
        batch = paths[start : start + BATCH]
        for line in run(cfg, ["sha256sum", *batch], timeout_s=7200).stdout.splitlines():
            if (match := _SHA_LINE.fullmatch(line)) is not None:
                found[match.group(2)] = match.group(1)
    return found


def snapshot_links(cfg: HernessConfig, snapshot: str, repo: str) -> dict[str, str] | str:
    """Step 2: ``{relative file: blob path}`` of the snapshot's links, or a failure status."""
    # ``%l=`` + ``-print0``: the spec's printf format fails the U10-77 pattern (T10-24 ruling)
    listing = run(cfg, ["find", snapshot, "-type", "l", "-printf", "%l=", "-print0"])
    # Review round 1: a non-link file is never hash-checked, so any is refused (no ``!``: U10-77)
    others = ["find", snapshot, "-mindepth", "1", "-not", "-type", "d", "-not", "-type", "l"]
    other = run(cfg, [*others, "-print0"])
    if listing.returncode != 0 or other.returncode != 0:
        return "list_failed"
    if other.stdout:  # review round 2: any output at all, even a bare NUL, is refused
        stray = next(filter(None, other.stdout.split("\0")), "")
        return f"not_a_link:{posixpath.relpath(stray, snapshot) if stray else 'unknown'}"
    links: dict[str, str] = {}
    for record in filter(None, listing.stdout.split("\0")):
        target, _eq, path = record.partition("=")
        rel = posixpath.relpath(path, snapshot) if path.startswith(snapshot + "/") else ""
        blob = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        shaped = record.count("=") == 1 and _BLOB_TARGET.fullmatch(target) is not None
        if not (rel and shaped) or posixpath.dirname(blob) != f"{repo}/blobs":
            return f"link_invalid:{rel or 'unknown'}"
        links[rel] = blob
    return links or "snapshot_empty"


def _git_ids(cfg: HernessConfig, paths: Sequence[str]) -> dict[str, str]:
    """``{path: git blob id}`` from ``git hash-object`` batches (sha1 of ``blob <size>\\0`` +
    content); output that is not one 40-hex line per path gives no ids (a mismatch)."""
    found: dict[str, str] = {}
    for start in range(0, len(paths), BATCH):
        batch = paths[start : start + BATCH]
        done = run(cfg, ["git", "hash-object", "--no-filters", "--", *batch], timeout_s=7200)
        lines = done.stdout.splitlines()
        if done.returncode == 0 and len(lines) == len(batch):
            found |= {
                p: oid for p, oid in zip(batch, lines, strict=True) if _GIT_BLOB.fullmatch(oid)
            }
    return found


def check_files(
    cfg: HernessConfig,
    files: dict[str, str],
    expected: dict[str, str],
    *,
    git: dict[str, str] | None = None,
    full: bool,
) -> tuple[str, dict[str, int]]:
    """Steps 3-4: sizes of every file; ``expected`` SHA-256 hashes and ``git`` ids when full."""
    git = git or {}
    sizes = _sizes(cfg, sorted(set(files.values())))
    for rel, path in sorted(files.items()):
        if path not in sizes or (rel in expected and sizes[path] == 0):
            return f"missing:{rel}", {}
    if full:
        hashes = _sha256(cfg, sorted({files[rel] for rel in expected}))
        for rel, sha in sorted(expected.items()):
            if hashes.get(files[rel]) != sha:
                return f"hash_mismatch:{rel}", {}
        ids = _git_ids(cfg, sorted({files[rel] for rel in git}))
        for rel, oid in sorted(git.items()):
            if ids.get(files[rel]) != oid:
                return f"blob_mismatch:{rel}", {}
    return "ok", {rel: sizes[path] for rel, path in files.items()}


def check_snapshot(
    cfg: HernessConfig, links: dict[str, str], *, full: bool
) -> tuple[str, dict[str, int]]:
    """Every linked blob checked against its name: SHA-256 (LFS) or git blob id (review r3)."""
    names = {rel: posixpath.basename(blob) for rel, blob in links.items()}
    lfs = {rel: name for rel, name in names.items() if _LFS_BLOB.fullmatch(name)}
    git = {rel: name for rel, name in names.items() if _GIT_BLOB.fullmatch(name)}
    return check_files(cfg, links, lfs, git=git, full=full)


def write_manifest(cfg: HernessConfig, name: str, manifest: dict[str, int]) -> None:
    """Step 5: ``<paths.data>/deploy/weights-<...>.json``, written atomically."""
    path = Path(cfg.paths.data) / "deploy" / f"weights-{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
