"""Planning and removal steps of ``herness deploy prune`` (impl 10 U10-85 steps 2-4).

Size-forced private sibling of ``herness.admin.deploy`` (T10-25). The plan is built before any
removal. Fail closed (TH10-46): a failed listing removes nothing there; only recognisable names
are candidates; every path must be a direct child of the listed directory under ``model_root``.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from herness.admin import _pull_verify as pv
from herness.admin import wsl
from herness.core.config import HernessConfig
from herness.core.errors import FatalError
from herness.core.logging import get_logger

__all__ = ["PrunePlan", "execute", "plan", "repo_of", "under_root", "valid_pins"]

_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")
_REVISION: Final = re.compile(r"[0-9a-f]{40}")
_BLOB: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_PINS: Final[dict[str, re.Pattern[str]]] = {  # the U10-07 deploy-time pin shapes
    "image": re.compile(r"[a-z0-9][a-z0-9._/-]{0,200}@sha256:[0-9a-f]{64}"),
    "model": re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}"),
    "revision": _REVISION,
    "gguf": re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.gguf"),
    "sha256": re.compile(r"[0-9a-f]{64}"),
}
_RM_TIMEOUT_S: Final = 600
_log = get_logger("admin.deploy")


@dataclass
class PrunePlan:
    """Everything one prune run removes: image refs ``<repo>@<digest>`` and WSL paths."""

    images: list[str] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)
    blobs: list[str] = field(default_factory=list)
    gguf: list[str] = field(default_factory=list)

    def items(self) -> set[str]:
        """Every planned item, for the loaded-class check."""
        return {*self.images, *self.snapshots, *self.blobs, *self.gguf}


def valid_pins(gpu_class: str, pins: Mapping[str, str]) -> bool:
    """True when ``pins`` has the class's keys, each in its pin shape (history lines)."""
    keys = ("image", "gguf", "sha256") if gpu_class == "large" else ("image", "model", "revision")
    return all(isinstance(pins.get(k), str) and _PINS[k].fullmatch(pins[k]) for k in keys)


def repo_of(cfg: HernessConfig, model: str) -> str:
    """The Hugging Face cache directory of ``model`` under ``model_root``."""
    return f"{cfg.deploy.model_root}/hub/models--{model.replace('/', '--')}"


def under_root(cfg: HernessConfig, path: str) -> str:
    """``path`` when it is a normalised path strictly under ``model_root``; else ``FatalError``."""
    root = posixpath.normpath(cfg.deploy.model_root)
    if posixpath.normpath(path) != path or not path.startswith(root + "/"):
        msg = "prune path outside model_root"
        raise FatalError(msg)
    return path


def _children(cfg: HernessConfig, directory: str, kind: str) -> list[str] | None:
    """Direct children of type ``kind``; None when the listing failed (remove nothing)."""
    argv = ["find", directory, "-mindepth", "1", "-maxdepth", "1", "-type", kind, "-print0"]
    done = pv.run(cfg, argv)
    if done.returncode != 0:
        _log.warning("deploy.prune.list_failed", kind=kind, returncode=done.returncode)
        return None
    paths = [p for p in done.stdout.split("\0") if p]
    for path in paths:
        if posixpath.dirname(under_root(cfg, path)) != directory:
            msg = "prune path outside model_root"
            raise FatalError(msg)
    return paths


def _images(cfg: HernessConfig, kept_refs: set[str]) -> list[str]:
    """Step 2: per repository of a kept image, every listed digest that is not kept."""
    found: list[str] = []
    for repo in sorted({ref.partition("@")[0] for ref in kept_refs}):
        # ``{{json .}}`` fails the U10-77 pattern (no space allowed): one digest per line
        argv = ["docker", "image", "ls", "--digests", "--format", "{{.Digest}}", repo]
        done = pv.run(cfg, argv)
        if done.returncode != 0:
            _log.warning("deploy.prune.list_failed", kind="images")
            continue
        for digest in done.stdout.split():
            ref = f"{repo}@{digest}"
            if _DIGEST.fullmatch(digest) and ref not in kept_refs and ref not in found:
                found.append(ref)
    return found


def _model(cfg: HernessConfig, repo: str, kept: set[str], todo: PrunePlan) -> None:
    """Step 3: snapshots not kept, then blobs no remaining snapshot links to (fail closed)."""
    snapshots = _children(cfg, f"{repo}/snapshots", "d")
    if snapshots is None:
        return
    remaining: list[str] = []
    for path in snapshots:
        name = posixpath.basename(path)
        if _REVISION.fullmatch(name) and name not in kept:
            todo.snapshots.append(path)
        else:
            remaining.append(path)
    if not kept <= {posixpath.basename(p) for p in remaining}:  # a kept revision unseen
        _log.warning("deploy.prune.blobs_skipped", reason="kept_revision_missing")
        return
    linked: set[str] = set()
    for snapshot in remaining:
        links = pv.snapshot_links(cfg, snapshot, repo)
        if isinstance(links, str):  # unreadable: no blob of this repo can be proven unused
            _log.warning("deploy.prune.blobs_skipped", reason=links.partition(":")[0])
            return
        linked |= set(links.values())
    for path in _children(cfg, f"{repo}/blobs", "f") or []:
        if _BLOB.fullmatch(posixpath.basename(path)) and path not in linked:
            todo.blobs.append(path)


def plan(
    cfg: HernessConfig,
    *,
    kept_refs: set[str],
    kept_revisions: Mapping[str, set[str]],
    kept_gguf: Iterable[str],
) -> PrunePlan:
    """Steps 2-4 without removing anything: ``kept_revisions`` maps model -> kept revisions."""
    todo = PrunePlan(images=_images(cfg, kept_refs))
    for model in sorted(kept_revisions):
        _model(cfg, repo_of(cfg, model), kept_revisions[model], todo)
    keep = set(kept_gguf)
    for path in _children(cfg, f"{cfg.deploy.model_root}/gguf", "f") or []:
        name = posixpath.basename(path)
        if _PINS["gguf"].fullmatch(name) and name not in keep:
            todo.gguf.append(path)
    return todo


def execute(cfg: HernessConfig, todo: PrunePlan) -> dict[str, int]:
    """Remove the planned items; counts per kind plus ``failed`` (a failure never stops it)."""
    counts = {"images": 0, "snapshots": 0, "blobs": 0, "gguf": 0, "failed": 0}
    for ref in todo.images:
        done = pv.run(cfg, ["docker", "image", "rm", ref], timeout_s=_RM_TIMEOUT_S)
        _count(counts, "images", ref, done.returncode)
    for kind, paths in (("snapshots", todo.snapshots), ("blobs", todo.blobs), ("gguf", todo.gguf)):
        for path in paths:
            argv = ["rm", "-rf", "--", under_root(cfg, path)]
            done = wsl.run_wsl(cfg.deploy.wsl_distro, argv, user="root", timeout_s=_RM_TIMEOUT_S)
            _count(counts, kind, path, done.returncode)
    return counts


def _count(counts: dict[str, int], kind: str, item: str, returncode: int) -> None:
    """One removal outcome; each failure is logged with its kind, item and exit code."""
    if returncode == 0:
        counts[kind] += 1
        return
    counts["failed"] += 1
    _log.warning("deploy.prune.remove_failed", kind=kind, item=item, returncode=returncode)
