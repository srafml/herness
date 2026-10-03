"""ST05-21 (TH05-21): the prompt files hold no credentials, URLs or hostnames.

The files are enumerated from the package directory, so a newly added prompt is scanned too.
Scanners: the spec 10 U10-36 detectors (`build_detectors`, `CREDENTIAL` and `URL_TOKEN` plus
the other patterned detectors), the repository's detect-secrets plugins, and a URL and
hostname check. Planted values are built at run time so detect-secrets stays quiet here.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

import pytest
from detect_secrets.core.secrets_collection import SecretsCollection
from detect_secrets.settings import default_settings

from herness.core.redact_patterns import build_detectors
from herness.core.settings import RedactionConfig

pytestmark = pytest.mark.unit

# 14 role prompts (R-37 removed verifier_claim.md from the card's 15) plus the three impl 07
# memory prompts (U07-99: compaction_notes.md from T07-14; chat_summary.md and
# correction_classify.md from T07-21); every prompts directory of the harness is scanned.
_EXPECTED_COUNT = 17
_PROMPT_DIRS = (("herness.harness.roles", "prompts"), ("herness.harness.memory", "prompts"))
_DETECTORS = build_detectors(RedactionConfig())
_SCHEME = re.compile(r"\b[a-z][a-z0-9+.-]{0,31}://|\bwww\.", re.IGNORECASE)
# Every dotted token, in prose and in code spans alike, must be a warehouse reference
# (`schema.table[.column]` in a known schema) or a prompt file name; a token ending in a
# host suffix is a hit unless it is one of the named tables listed here.
_DOTTED = re.compile(r"(?<![\w.-])[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+")
_SCHEMAS = frozenset({"core", "enrich", "score", "metrics"})
_HOST_SUFFIXES = frozenset(
    {"com", "net", "org", "io", "dev", "ai", "app", "cloud", "local", "internal", "lan", "corp"}
    | {"co", "us", "uk", "de", "eu", "in", "localdomain", "home", "test", "example"}
)
_NOT_HOSTS = frozenset({"score.org"})  # the warehouse table of org scores, not a host
_LOCALHOST = re.compile(r"\blocalhost\b", re.IGNORECASE)


def _prompt_paths() -> Iterator[Path]:
    for package, name in _PROMPT_DIRS:
        root = resources.files(package) / name
        with resources.as_file(root) as directory:
            yield from sorted(p for p in Path(directory).iterdir() if p.is_file())


def _detector_hits(text: str) -> list[str]:
    return [
        f"{d.type}:{start}"
        for d in _DETECTORS
        if d.prefilter(text)
        for start, _end, _value in d.find(text)
    ]


def _is_host_like(token: str) -> bool:
    labels = token.lower().split(".")
    if token in _NOT_HOSTS:
        return False
    if labels[-1] in _HOST_SUFFIXES:
        return True
    return labels[0] not in _SCHEMAS and labels[-1] != "md"


def _url_hits(text: str) -> list[str]:
    hits = [m.group() for m in _SCHEME.finditer(text)]
    hits += [m.group() for m in _LOCALHOST.finditer(text)]
    return hits + [t for t in _DOTTED.findall(text) if _is_host_like(t)]


def _detect_secrets_hits(path: Path) -> list[str]:
    collection = SecretsCollection()
    with default_settings():
        collection.scan_file(str(path))
    return [f"{s.type}:{s.line_number}" for _file, s in collection]


def test_st05_21_scans_every_prompt_file() -> None:
    """ST05-21 the scan covers every file in the role and memory prompts directories (17)."""
    paths = list(_prompt_paths())
    assert len(paths) == _EXPECTED_COUNT
    names = {p.name for p in paths}
    assert {"compaction_notes.md", "chat_summary.md", "correction_classify.md"} <= names
    assert all(p.suffix == ".md" for p in paths)


def test_st05_21_prompts_have_no_credentials_or_urls() -> None:
    """ST05-21 zero detector, detect-secrets, URL and hostname hits across all prompt files."""
    hits: dict[str, list[str]] = {}
    scanned = 0
    for path in _prompt_paths():
        text = path.read_text(encoding="utf-8")
        found = _detector_hits(text) + _url_hits(text) + _detect_secrets_hits(path)
        scanned += 1
        if found:
            hits[path.name] = found
    assert scanned == _EXPECTED_COUNT
    assert hits == {}


def test_st05_21_scanners_bite(tmp_path: Path) -> None:
    """ST05-21 each scanner flags a planted credential, URL or hostname."""
    key = "AK" + "IA" + "Q7XK2M4N9P3R5T8W"  # AWS access key id shape, built at run time
    token = "gh" + "p_" + "Zr8Kq2" * 7  # 42 token characters
    assert any(h.startswith("CREDENTIAL") for h in _detector_hits(f"use {key} here"))
    assert any(h.startswith("CREDENTIAL") for h in _detector_hits("api" + "_key = " + token))
    assert _url_hits("see https" + "://example.test/x")
    assert _url_hits("ask the team at build.example.com")
    assert _url_hits("connect to `wh-01.corp.internal` first") == ["wh-01.corp.internal"]
    assert _url_hits("the host is `vllm.gpu-pool`") == ["vllm.gpu-pool"]
    assert _url_hits("the host is `LOCALHOST`") == ["LOCALHOST"]
    assert _url_hits("mirror at `acme.org`") == ["acme.org"]
    assert not _url_hits(
        "read `score.org`, `core.service_map.link_source`, `enrich.incident_change_link`,"
        " `score.portfolio.expected_impact_usd` and `_common.md`."
    )
    planted = tmp_path / "planted.md"
    planted.write_text(f"aws_access_key_id = {key}\n", encoding="utf-8")
    assert _detect_secrets_hits(planted)
