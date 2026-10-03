"""UT05-94: the real prompt files in the package data (impl 05 U05-52 to U05-56, T05-21).

Unlike the RoleSpec mechanics tests, these read the prompts through the real resolver
(`importlib.resources`), so they fail when a file is missing from the package or the wheel.
"""

from __future__ import annotations

import dataclasses
import re
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

import pytest
import yaml
from tests.support.dispatch_standin import make_tool_ctx, use_test_config

from herness.core import config as c
from herness.core.numbers import compile_allowed_patterns, find_uncited
from herness.harness.pipelines.settings import SkepticSettings
from herness.harness.roles import base
from herness.harness.roles.base import ROLE_NAMES, RoleSpec, get_role
from herness.reports.settings import ReportsSection

pytestmark = pytest.mark.unit

_MAX_LINES = 150
_SCALE = "0" + chr(0x2013) + "5"  # the zero to five rating scale, written with an en dash
_ANALYSTS = ("ops", "change", "delivery", "org", "crosscheck", "retrospective", "general")
# 14 files: the card's "15" counted verifier_claim.md, which R-37 removes.
_EXPECTED_FILES = frozenset(
    {
        "_common.md",
        "planner.md",
        "judge.md",
        *(f"analyst_{s}.md" for s in _ANALYSTS),
        "skeptic.md",
        "writer.md",
        "writer_retrospective.md",
        "chat.md",
    }
)
_CHECKS = (
    "confounding",
    "seasonality",
    "mis_mapping",
    "small_sample",
    "double_counting",
    "survivorship",
)
# `reports.allowed_numeral_patterns` (years, ISO dates, quarters, record ids): the owner
# default, which test_ut05_94_allowed_numerals_match_config ties to config/app.yaml.
_ALLOWED_NUMERALS = tuple(ReportsSection().allowed_numeral_patterns)
_APP_YAML = Path(__file__).resolve().parents[4] / "config" / "app.yaml"
# Numerals a unit's invariants allow beyond those patterns.
_EXTRA_NUMERALS: dict[str, tuple[str, ...]] = {
    "planner.md": (_SCALE, r"\b400\b"),  # U05-53: the rating scale and the note limit
    "judge.md": (_SCALE,),
    "skeptic.md": (r"\b13\b", r"\b30\b", r"\b25 %"),  # U05-55: spec 06 §5.7 defaults
}
_ANALYST_COMMON = (
    "## Workflow",
    "list_tables",
    "describe_table",
    "get_metric",
    "get_scores",
    "post_finding",
    "`numbers`",
    "`entity_type`",
    "`entity_id`",
    "`confidence`",
    "request_subtask",
    "AnalystOutput",
    "## Specialty focus",
    "## Pitfalls",
    *(f"`{check}`" for check in _CHECKS),
    "## Stop rule",
    '"unknown"',
)
# Required strings per file, in the order the unit's postconditions list them.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "_common.md": (
        "## Numbers",
        "[[n1]]",
        "NumberRef",
        "`query_id`",
        "`column`",
        "`row_key`",
        "2026-09-24",
        "Q3 2026",
        "INC0012345",
        "PAY-123",
        '"unknown"',
        "`unknowns`",
        "decimal strings",
        "`get_metric`",
        "## Untrusted data",
        '<untrusted_data source="…" record_id="…">',
        "`warehouse`",
        "`memory`",
        "`scratchpad`",
        "`truncation`",
        "Correlation is not cause",
        "hint",
        "## Tools",
        "query_id=",
        "## Final answer",
        "one JSON object",
    ),
    "planner.md": (
        "dedup_key",
        "DQ warning",
        "`notes`",
        "400",
        "wildcard",
        "`objective`",
        *(f"`{s}`" for s in _ANALYSTS),
        "allow-list",
        "budget",
        "PlannerOutput",
        "`tasks`",
        "`rationale`",
        "`unknowns`",
    ),
    "judge.md": (
        _SCALE,
        "must-cover",
        "testable",
        "overlap",
        "DQ warning",
        "JudgeOutput",
        "`scores`",
        "mean",
        "`choice`",
        "lower index",
        "`reasons`",
        "no tools",
    ),
    "analyst_ops.md": (*_ANALYST_COMMON[:13], "MTTR", "MTTA", "SLA", *_ANALYST_COMMON[13:]),
    "analyst_change.md": (
        *_ANALYST_COMMON[:13],
        "change failure rate",
        "`enrich.incident_change_link`",
        "lead time",
        "emergency",
        *_ANALYST_COMMON[13:],
    ),
    "analyst_delivery.md": (
        *_ANALYST_COMMON[:13],
        "cycle time",
        "unplanned",
        "carryover",
        "incident cost",
        *_ANALYST_COMMON[13:],
    ),
    "analyst_org.md": (
        *_ANALYST_COMMON[:13],
        "peer median",
        "`score.org`",
        "`score.action_lever`",
        *_ANALYST_COMMON[13:],
    ),
    "analyst_crosscheck.md": (
        *_ANALYST_COMMON[:13],
        "independent",
        "never the original SQL",
        "agree",
        *_ANALYST_COMMON[13:],
    ),
    "analyst_retrospective.md": (
        *_ANALYST_COMMON[:13],
        "`outcome`",
        "paid off",
        "no effect",
        "worse",
        "inconclusive",
        *_ANALYST_COMMON[13:],
    ),
    "analyst_general.md": (*_ANALYST_COMMON[:13], "objective", *_ANALYST_COMMON[13:]),
    "skeptic.md": (
        "six checks",
        "`confounding`",
        "peer median",
        "`seasonality`",
        "13 weeks",
        "`n_a`",
        "`mis_mapping`",
        "`core.service_map.link_source`",
        "`small_sample`",
        "below 30",
        "exceeds 25 % of a total",
        "`double_counting`",
        "`survivorship`",
        "`query_ids`",
        "`reject`",
        "`revise`",
        "`required_actions`",
        "`uphold`",
        "claim test",
        "Before the six checks",
        "`fail` of the closest check",
        "SkepticOutput",
    ),
    "writer.md": (
        "verified findings",
        "`executive_summary`",
        "`recommendations`",
        "`portfolio`",
        "`org_scorecards`",
        "`actions`",
        "`retrospective`",
        "`risks_and_caveats`",
        "`method`",
        "Omit a section",
        "`finding_id`",
        "`fund`",
        "`expected_usd_ref`",
        "`effort_usd_ref`",
        "`confidence_ref`",
        "`org_action`",
        "`expected_metric`",
        "`action_levers[*].delta_usd_ref`",
        "`score.action_lever`",
        "`delta_usd`",
        "best first",
        "`rec_id`",
        "`rank`",
        "`caveats`",
        "dead tasks",
        "WriterOutput",
    ),
    "writer_retrospective.md": ("`prior_outcomes_commentary`", "one paragraph", '"unknown"'),
    "chat.md": (
        "concise",
        "`numbers`",
        "`query_ids`",
        "`unknowns`",
        "`followups`",
        "`escalate`",
        "`cloud`",
        "ChatAnswer",
    ),
}


def _package_files() -> dict[str, str]:
    """Every file in the package's prompts directory, read through `importlib.resources`."""
    root = resources.files("herness.harness.roles") / "prompts"
    return {e.name: e.read_text(encoding="utf-8") for e in root.iterdir() if e.is_file()}


def _roles() -> list[RoleSpec]:
    return [get_role(n) for n in ROLE_NAMES] + [get_role("writer", variant="retrospective")]


def test_ut05_94_file_set_matches_role_specs() -> None:
    """UT05-94 the package holds exactly the prompt files the RoleSpecs name (14, R-37)."""
    names = set(_package_files())
    assert names == _EXPECTED_FILES
    assert len(names) == 14
    assert "verifier_claim.md" not in names
    assert {f for role in _roles() for f in role.prompt_files} == names


@pytest.mark.parametrize("name", sorted(_EXPECTED_FILES))
def test_ut05_94_size_and_language(name: str) -> None:
    """UT05-94 each prompt is ≤ 150 lines, non-empty, ends with a newline and has no tabs."""
    text = _package_files()[name]
    assert text.strip()
    assert len(text.splitlines()) <= _MAX_LINES
    assert text.endswith("\n")
    assert "\t" not in text
    assert text.startswith("# ")


@pytest.mark.parametrize("name", sorted(_REQUIRED))
def test_ut05_94_required_strings_in_order(name: str) -> None:
    """UT05-94 the unit's required strings are present, in postcondition order."""
    text = _package_files()[name]
    position = 0
    for needle in _REQUIRED[name]:
        found = text.find(needle, position)
        assert found >= 0, f"{name}: {needle!r} missing or out of order"
        position = found + len(needle)


def test_ut05_94_common_delimiter_rules() -> None:
    """UT05-94 `_common.md` names only the R-20 delimiter, never the retired ones."""
    text = _package_files()["_common.md"]
    for needle in ("[[n1]]", "NumberRef", "<untrusted_data", "unknowns", "query_id"):
        assert needle in text
    for retired in ("<ticket_text>", "<memory_context>", "ticket_text", "memory_context"):
        assert retired not in text
    for name, content in _package_files().items():
        assert "<ticket_text>" not in content, name
        assert "<memory_context>" not in content, name


def test_ut05_94_analysts_share_sections() -> None:
    """UT05-94 every analyst prompt has the same four sections and six pitfall lines."""
    files = _package_files()
    for s in _ANALYSTS:
        text = files[f"analyst_{s}.md"]
        headings = [line for line in text.splitlines() if line.startswith("## ")]
        assert headings == ["## Workflow", "## Specialty focus", "## Pitfalls", "## Stop rule"]
        pitfalls = text.split("## Pitfalls", 1)[1].split("## Stop rule", 1)[0]
        lines = [line for line in pitfalls.splitlines() if line.startswith("- ")]
        prose = [line for line in pitfalls.splitlines() if line and not line.startswith("- ")]
        assert len(prose) == 1  # the intro line; each check is one line, no continuation
        assert [line.split("`")[1] for line in lines] == list(_CHECKS)
        assert all(
            line.startswith(f"- `{name}`: ") for line, name in zip(lines, _CHECKS, strict=True)
        )
        assert f"`{s}`" in text


@pytest.mark.parametrize("name", sorted(_EXPECTED_FILES))
def test_ut05_94_numerals_only_where_allowed(name: str) -> None:
    """UT05-94 no numeral outside markers, the allowed patterns and the unit's own exceptions."""
    allowed = compile_allowed_patterns([*_ALLOWED_NUMERALS, *_EXTRA_NUMERALS.get(name, ())])
    assert find_uncited(_package_files()[name], allowed) == ()


def test_ut05_94_skeptic_thresholds_match_defaults() -> None:
    """UT05-94 skeptic.md states the spec 06 §5.7 defaults that `swarm.skeptic` carries."""
    defaults = SkepticSettings()
    text = _package_files()["skeptic.md"]
    assert f"at least {defaults.min_weeks_seasonality} weeks" in text
    assert f"below {defaults.min_sample} " in text
    assert f"exceeds {round(defaults.single_record_share * 100)} % of a total" in text


def test_ut05_94_allowed_numerals_match_config() -> None:
    """UT05-94 the numeral patterns used here are the ones config/app.yaml ships."""
    shipped = yaml.safe_load(_APP_YAML.read_text(encoding="utf-8"))["reports"]
    assert tuple(shipped["allowed_numeral_patterns"]) == _ALLOWED_NUMERALS


def test_ut05_94_planner_wildcard_dedup_key_is_null() -> None:
    """UT05-94 planner.md asks for a JSON null `dedup_key` on wildcards, never an empty one."""
    text = _package_files()["planner.md"]
    assert "`dedup_key`\n  to JSON `null`" in text
    assert "`null` for a wildcard" in text
    assert "empty" not in text


def test_ut05_94_numeral_check_bites() -> None:
    """UT05-94 the numeral check flags a stray number the patterns do not cover."""
    allowed = compile_allowed_patterns(list(_ALLOWED_NUMERALS))
    assert [h.text for h in find_uncited("MTTR rose 42 % in Q3 2026", allowed)] == ["42 %"]


def test_ut05_94_prompt_hash_stable_per_role() -> None:
    """UT05-94 prompt_hash is 16 hex, the same on every read and for a fresh RoleSpec."""
    hashes = {}
    for role in _roles():
        first = role.prompt_hash
        assert re.fullmatch(r"[0-9a-f]{16}", first)
        assert role.prompt_hash == first
        assert dataclasses.replace(role).prompt_hash == first
        hashes[role.name, role.prompt_files] = first
    assert len(set(hashes.values())) == len(hashes)


def _copy_prompts(dest: Path) -> Path:
    src: Traversable = resources.files("herness.harness.roles") / "prompts"
    dest.mkdir()
    for entry in src.iterdir():
        if entry.is_file():
            (dest / entry.name).write_bytes(entry.read_bytes())
    return dest


@pytest.mark.parametrize("changed", sorted(_EXPECTED_FILES))
def test_ut05_94_prompt_hash_follows_content(
    changed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-94 a tmp copy hashes the same; editing one file changes exactly its roles' hashes."""
    real = {(r.name, r.prompt_files): r.prompt_hash for r in _roles()}
    root = _copy_prompts(tmp_path / "prompts")
    monkeypatch.setattr(base, "_prompts_root", lambda: root)
    copied = {(r.name, r.prompt_files): dataclasses.replace(r).prompt_hash for r in _roles()}
    assert copied == real
    with (root / changed).open("a", encoding="utf-8") as fh:
        fh.write("\nOne more line.\n")
    for role in _roles():
        edited = dataclasses.replace(role).prompt_hash
        assert (edited != real[role.name, role.prompt_files]) == (changed in role.prompt_files)


def test_ut05_94_system_block_one_under_cap(tmp_path: Path) -> None:
    """UT05-94 each real role's block 1 (prompts, schema, catalog) fits the 60,000 cap."""
    use_test_config(tmp_path / "cfg")
    try:
        for role in _roles():
            first = role.system_blocks(make_tool_ctx())[0].text
            assert first.startswith(_package_files()["_common.md"].rstrip("\n"))
            assert len(first) <= 60_000
    finally:
        c.reset_config()


def test_ut05_94_only_the_resolver_reads_prompts() -> None:
    """UT05-94 only the prompt resolvers refer to a prompts directory: roles/base.py for the
    role prompts, memory/_compactor_llm.py for the compaction prompt (impl 07 U07-77, U07-99)."""
    harness = Path(base.__file__).resolve().parents[1]
    hits = [
        p.relative_to(harness).as_posix()
        for p in harness.rglob("*.py")
        if re.search(r"""["']prompts["']|prompts/""", p.read_text(encoding="utf-8"))
    ]
    # U07-77 (T07-14): the compaction prompt resolver reads memory/prompts/compaction_notes.md.
    assert sorted(hits) == ["memory/_compactor_llm.py", "roles/base.py"]
