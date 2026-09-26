"""Tests for herness.core.redact_directory (impl 10 UT10-46; T10-09)."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from herness.core import redact_directory as rd
from herness.core.errors import ConfigError, StoreBusy
from herness.core.logging import configure_logging, reset_logging
from herness.core.redact_patterns import normalize_value

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    reset_logging()


def _err_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(text) for text in capsys.readouterr().err.splitlines() if text.strip()]


def _write_csv(path: Path, rows: str) -> Path:
    path.write_text(rows, encoding="utf-8")
    return path


# --- UT10-46: directory CSV, alt names and word-bounded variants -----------------------


def test_ut10_46_csv_with_alt_names_matches_variants(tmp_path: Path) -> None:
    """UT10-46 a display_name and its alt_names all resolve to one canonical, word-bounded."""
    csv_path = _write_csv(
        tmp_path / "directory.csv",
        "display_name,alt_names\nMaria Garcia,Maria G.;M Garcia\n",
    )
    directory = rd.NameDirectory.from_files(csv_path, (), None)
    canonical = normalize_value("PERSON", "Maria Garcia")

    text = "Contact Maria Garcia or Garcia, Maria or M Garcia, but not MariaGarcian today."
    matches = directory.find(text)

    values = {text[s:e].lower() for s, e, _ in matches}
    assert values == {"maria garcia", "garcia, maria", "m garcia"}
    assert {c for _, _, c in matches} == {canonical}
    assert "mariagarcian" not in " ".join(text[s:e].lower() for s, e, _ in matches)


def test_ut10_46_word_bounded_rejects_embedded_substring() -> None:
    """UT10-46 a key embedded in a larger word is rejected; the standalone token matches."""
    directory = rd.NameDirectory.from_files(None, ("ann",), None)
    text = "susann ann"  # "ann" is a substring of "susann" but not word-bounded there
    matches = directory.find(text)
    assert matches == [(7, 10, "ann")]


def test_ut10_46_extra_names_single_first_name_only_when_listed() -> None:
    """UT10-46 a bare first name matches only when it is listed in extra_names."""
    text = "Alice is here"
    without = rd.NameDirectory.from_files(None, (), None)
    assert without.find(text) == []

    with_extra = rd.NameDirectory.from_files(None, ("Alice",), None)
    assert with_extra.find(text) == [(0, 5, "alice")]
    assert with_extra.size == 1
    assert with_extra.variant_count == 1


def test_ut10_46_alt_name_skipped_when_display_name_is_single_token(tmp_path: Path) -> None:
    """UT10-46 alt_names of a single-token display_name are not added (no canonical yet)."""
    csv_path = _write_csv(tmp_path / "directory.csv", "display_name,alt_names\nAlice,Ally\n")
    directory = rd.NameDirectory.from_files(csv_path, (), None)
    assert directory.find("Ally is here") == []
    assert directory.size == 0


def test_ut10_46_blank_alt_and_extra_entries_are_skipped(tmp_path: Path) -> None:
    """UT10-46 an empty alt_names segment and a blank extra_names entry add no key."""
    csv_path = _write_csv(tmp_path / "directory.csv", "display_name,alt_names\nJane Smith,\n")
    directory = rd.NameDirectory.from_files(csv_path, ("", "   "), None)
    assert directory.size == 1  # only "Jane Smith" itself
    assert directory.find("Jane Smith") == [(0, 10, normalize_value("PERSON", "Jane Smith"))]


# --- UT10-46: overlap resolution longest-then-leftmost ---------------------------------


def test_ut10_46_overlap_resolution_prefers_longer_match() -> None:
    """UT10-46 two overlapping candidates: the longer span wins even starting later."""
    directory = rd.NameDirectory.from_files(None, ("Anna Maria", "Maria Garcia"), None)
    text = "anna maria garcia"
    matches = directory.find(text)
    assert matches == [(5, 17, normalize_value("PERSON", "Maria Garcia"))]


def test_ut10_46_overlap_resolution_prefers_leftmost_on_tie() -> None:
    """UT10-46 two equal-length overlapping candidates: the leftmost span wins."""
    directory = rd.NameDirectory.from_files(None, ("bb cc", "cc dd"), None)
    text = "aa bb cc dd ee"
    matches = directory.find(text)
    start = text.index("bb cc")
    assert matches == [(start, start + len("bb cc"), "bb cc")]


# --- UT10-46: non-length-preserving lower() index map -----------------------------------


def test_ut10_46_index_map_for_non_length_preserving_lower() -> None:
    """UT10-46 a Turkish dotted capital I expands under lower(); the span still maps back."""
    directory = rd.NameDirectory.from_files(None, ("squad",), None)
    text = "TEAM İ SQUAD"
    assert len(text.lower()) != len(text)  # confirms the fallback path is exercised

    matches = directory.find(text)
    assert matches == [(7, 12, "squad")]
    assert text[7:12] == "SQUAD"


# --- UT10-46: missing file, permission error --------------------------------------------


def test_ut10_46_missing_directory_file_warns_and_yields_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT10-46 a missing directory_file logs redact.directory.missing and builds empty."""
    configure_logging("INFO")
    directory = rd.NameDirectory.from_files(tmp_path / "missing.csv", (), None)

    assert directory.size == 0
    assert directory.variant_count == 0
    assert directory.find("anything") == []
    lines = _err_lines(capsys)
    missing = next(line for line in lines if line.get("event") == "redact.directory.missing")
    assert missing["level"] == "warning"


def test_ut10_46_unreadable_directory_file_raises_config_error(tmp_path: Path) -> None:
    """UT10-46 a directory_file that cannot be read (not merely missing) raises ConfigError."""
    with pytest.raises(ConfigError) as info:
        rd.NameDirectory.from_files(tmp_path, (), None)  # a directory, not a file
    assert "cannot read directory_file" in str(info.value)


def test_ut10_46_oversized_directory_file_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-46 a directory_file larger than the byte cap raises ConfigError."""
    monkeypatch.setattr(rd, "MAX_DIRECTORY_BYTES", 10)
    csv_path = _write_csv(tmp_path / "directory.csv", "display_name,alt_names\nJane Smith,\n")
    with pytest.raises(ConfigError) as info:
        rd.NameDirectory.from_files(csv_path, (), None)
    assert "cannot read directory_file" in str(info.value)


def test_ut10_46_unreadable_display_names_file_raises_config_error(tmp_path: Path) -> None:
    """UT10-46 a display_names_file that cannot be read raises ConfigError."""
    with pytest.raises(ConfigError):
        rd.NameDirectory.from_files(None, (), tmp_path)  # a directory, not a file


def test_ut10_46_missing_display_names_file_is_silently_empty(tmp_path: Path) -> None:
    """UT10-46 a display_names_file that simply does not exist yields an empty directory."""
    directory = rd.NameDirectory.from_files(None, (), tmp_path / "missing.txt")
    assert directory.size == 0


# --- UT10-46: update_display_names -------------------------------------------------------


def test_ut10_46_display_names_file_sorted_and_count_returned(tmp_path: Path) -> None:
    """UT10-46 valid names are filtered, the cache file is sorted, the count added returns."""
    names = [
        "Zoe Wu",
        "A",  # 1 token: rejected
        "Bob Jones",
        "This Has Way Too Many Different Name Tokens Listed Here",  # 10 tokens: rejected
    ]
    added = rd.update_display_names(names, data_dir=tmp_path)
    assert added == 2

    path = tmp_path / "cache" / "redact" / "display_names.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ["Bob Jones", "Zoe Wu"]

    # Re-adding the same names is a no-op.
    assert rd.update_display_names(["Bob Jones"], data_dir=tmp_path) == 0

    # A genuinely new name grows and re-sorts the file.
    assert rd.update_display_names(["Amy Clark"], data_dir=tmp_path) == 1
    assert path.read_text(encoding="utf-8").splitlines() == ["Amy Clark", "Bob Jones", "Zoe Wu"]


def test_ut10_46_display_names_used_by_from_files(tmp_path: Path) -> None:
    """UT10-46 NameDirectory.from_files picks up names persisted by update_display_names."""
    rd.update_display_names(["Priya Nair"], data_dir=tmp_path)
    display_file = tmp_path / "cache" / "redact" / "display_names.txt"
    directory = rd.NameDirectory.from_files(None, (), display_file)
    assert directory.find("Priya Nair works here") == [
        (0, 10, normalize_value("PERSON", "Priya Nair"))
    ]


def test_ut10_46_write_oserror_raises_store_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-46 an OSError while writing the cache file raises StoreBusy."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")  # noqa: EM101 - test double, message never surfaces

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(StoreBusy) as info:
        rd.update_display_names(["New Person"], data_dir=tmp_path)
    assert "display_names write failed" in str(info.value)


# --- acceptance: 100k-name build under 5 seconds ----------------------------------------


@pytest.mark.slow
def test_ut10_46_100k_name_build_under_5_seconds(tmp_path: Path) -> None:
    """UT10-46 acceptance: building a 100,000-name directory takes under 5 seconds."""
    csv_path = tmp_path / "directory.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("display_name,alt_names\n")
        for i in range(100_000):
            handle.write(f"First{i} Last{i},Alt{i} Name{i}\n")

    start = time.perf_counter()
    directory = rd.NameDirectory.from_files(csv_path, (), None)
    elapsed = time.perf_counter() - start

    assert directory.size == 100_000
    assert elapsed < 5.0
