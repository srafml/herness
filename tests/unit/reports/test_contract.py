"""Tests for herness.reports.contract: constants, fields, load, lookups, checks (T09-05)."""

from __future__ import annotations

import ast
import json
import os
import sys
from collections.abc import Collection
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
from structlog.testing import capture_logs
from tests.support.report_drafts import (
    FINDING_ID,
    Q1,
    Q2,
    REC_ID,
    RUN_ID,
    ULID,
    draft_dict,
    make_draft,
    number_ref,
    paragraph,
    recommendation,
)

from herness.core import config as _config
from herness.core import errors as e
from herness.core.types import NumberRef, ReportDraft
from herness.core.types.reports import _RUN_ID_RE
from herness.reports import contract as c
from herness.store import ops

pytestmark = pytest.mark.unit

_OTHER_REC = "rec_01J9ZQ4Y8M6V3K2N1P0R5T7W9Y"  # pragma: allowlist secret


class FakeLookups:
    """Lookups over fixed sets; records every call."""

    def __init__(
        self,
        known: Collection[str] = (Q1, Q2),
        recs: Collection[str] = (REC_ID,),
        verified: Collection[str] = (FINDING_ID,),
    ) -> None:
        self.known, self.recs, self.verified = set(known), set(recs), set(verified)
        self.calls: list[tuple[str, object]] = []

    def known_query_ids(self, query_ids: Collection[str]) -> set[str]:
        self.calls.append(("known_query_ids", list(query_ids)))
        return set(query_ids) & self.known

    def run_rec_ids(self, run_id: str) -> set[str]:
        self.calls.append(("run_rec_ids", run_id))
        return set(self.recs)

    def verified_finding_ids(self, finding_ids: Collection[str]) -> set[str]:
        self.calls.append(("verified_finding_ids", list(finding_ids)))
        return set(finding_ids) & self.verified


def _violations(draft: ReportDraft, lookups: FakeLookups | None = None) -> e.ReportContractError:
    with pytest.raises(e.ReportContractError) as exc:
        c.check_render_contract(draft, lookups=lookups or FakeLookups())
    assert exc.value.details["code"] == "contract_violation"
    return exc.value


def _rec_copy(draft: ReportDraft, **update: Any) -> ReportDraft:
    rec = draft.recommendations[0].model_copy(update=update)
    return draft.model_copy(update={"recommendations": [rec]})


# --- UT09-93 constants and the no-own-regex rule --------------------------------------------------


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (RUN_ID, True),
        ("run_0123456789ABCDEFGHJKMNPQRS", True),  # pragma: allowlist secret
        ("run_" + ULID.lower(), False),
        # the next id ends in I, which is not Crockford base32
        ("run_01J9ZQ4Y8M6V3K2N1P0R5T7W9I", False),  # pragma: allowlist secret
        ("run_01J9ZQ4Y8M6V3K2N1P0R5T7W9", False),  # pragma: allowlist secret
        ("run_../../etc/passwd", False),
        (f"{RUN_ID}\n", False),
        ("", False),
    ],
)
def test_ut09_93_run_id_re_accept_reject(value: str, ok: bool) -> None:
    """UT09-93 RUN_ID_RE accept/reject table."""
    assert (c.RUN_ID_RE.fullmatch(value) is not None) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (Q1, True),
        ("q_0000000000000000", True),
        ("q_0123456789ABCDEF", False),
        ("q_0123456789abcde", False),
        ("q_0123456789abcdef0", False),
        ("Q_0123456789abcdef", False),
        ("q_0123456789abcdeg", False),
    ],
)
def test_ut09_93_query_id_re_accept_reject(value: str, ok: bool) -> None:
    """UT09-93 QUERY_ID_RE accept/reject table."""
    assert (c.QUERY_ID_RE.fullmatch(value) is not None) is ok


def test_ut09_93_run_id_re_matches_shared_type_pattern() -> None:
    """UT09-93 RUN_ID_RE is the pattern ReportManifest validates with (T09-01 carry-over)."""
    assert c.RUN_ID_RE.pattern == _RUN_ID_RE


def test_ut09_93_constants_verbatim() -> None:
    """UT09-93 closed sets and limits of U09-03."""
    assert frozenset({"1"}) == c.SUPPORTED_SCHEMA_VERSIONS
    assert frozenset({"done", "partial"}) == c.RENDERABLE_RUN_STATUSES
    assert c.SECTION_IDS == (
        "executive_summary", "recommendations", "portfolio", "org_scorecards", "actions",
        "retrospective", "risks_and_caveats", "method",
    )  # fmt: skip
    assert c.UNCITED_TEXT_MAX == 80
    assert c.DRAFT_MAX_BYTES == 20_971_520


def test_ut09_93_module_compiles_no_marker_or_numeral_regex() -> None:
    """UT09-93 AST scan: only RUN_ID_RE and QUERY_ID_RE are compiled; no regex text of its own."""
    tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
    re_calls: list[str] = []
    compiled_into: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
        ):
            re_calls.append(node.func.attr)
        if isinstance(node, ast.AnnAssign | ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute) and func.attr == "compile":
                targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
                compiled_into += [t.id for t in targets if isinstance(t, ast.Name)]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for token in ("\\[", "\\d", "[0-9]+", "(%|", "\\$"):
                assert token not in node.value, node.value
    assert re_calls == ["compile", "compile"]
    assert compiled_into == ["RUN_ID_RE", "QUERY_ID_RE"]
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "herness.core.numbers"
        for alias in node.names
    }
    assert {"find_uncited", "parse_markers"} <= imported


# --- UT09-05 iter_text_fields -------------------------------------------------------------------


def _ut09_05_draft() -> ReportDraft:
    rec2 = recommendation(
        2,
        numbers=[
            number_ref(),
            number_ref("n2", "usd", "10"),
            number_ref("n3", "usd", "5"),
            number_ref("n4", "pct", 1.5),
        ],
        expected_usd_ref="n2", expected_delta_ref="n4", confidence_ref=None, effort_usd_ref="n3",
        action_levers=[
            {"entity_type": "team", "entity_id": "t1", "metric": "mttr", "delta_usd_ref": "n2"},
            {"entity_type": "org", "entity_id": "o1", "metric": "cost", "delta_usd_ref": "n3"},
        ],
    )  # fmt: skip
    return make_draft(
        sections=[
            {"id": "executive_summary", "title": "S", "paragraphs": [paragraph(), paragraph()]},
            {"id": "portfolio", "title": "P", "paragraphs": [paragraph("Third")]},
        ],
        recommendations=[recommendation(1), rec2],
        ranked_entities=[],
        caveats=["First caveat"],
        prior_outcomes_commentary=paragraph("Last time [[n1]]"),
    )


def test_ut09_05_iter_text_fields_order_and_refs() -> None:
    """UT09-05 exact ordered where list, numbers, finding ids and refs."""
    fields = list(c.iter_text_fields(_ut09_05_draft()))
    assert [f.where for f in fields] == [
        "title",
        "sections[0].paragraphs[0]",
        "sections[0].paragraphs[1]",
        "sections[1].paragraphs[0]",
        "recommendations[0].headline",
        "recommendations[0].summary",
        "recommendations[1].headline",
        "recommendations[1].summary",
        "caveats[0]",
        "prior_outcomes_commentary",
    ]
    by = {f.where: f for f in fields}
    assert by["title"].text == "Funding review"
    assert by["title"].numbers == by["caveats[0]"].numbers == ()
    assert by["caveats[0]"].text == "First caveat"
    assert [n.id for n in by["sections[1].paragraphs[0]"].numbers] == ["n1"]
    assert by["recommendations[0].headline"].refs == ()
    assert by["recommendations[0].headline"].finding_ids == (FINDING_ID,)
    assert by["recommendations[0].summary"].refs == (
        ("expected_usd_ref", "n2"),
        ("confidence_ref", "n1"),
        ("action_levers[0].delta_usd_ref", "n2"),
    )
    assert by["recommendations[1].summary"].refs == (
        ("expected_usd_ref", "n2"),
        ("expected_delta_ref", "n4"),
        ("effort_usd_ref", "n3"),
        ("action_levers[0].delta_usd_ref", "n2"),
        ("action_levers[1].delta_usd_ref", "n3"),
    )
    assert [n.id for n in by["recommendations[1].headline"].numbers] == ["n1", "n2", "n3", "n4"]
    assert by["prior_outcomes_commentary"].text == "Last time [[n1]]"
    assert by["prior_outcomes_commentary"].finding_ids == (FINDING_ID,)


@pytest.mark.parametrize(
    ("lever", "key"),
    [
        ({"entity_id": "t1", "metric": "m", "delta_usd_ref": "n2"}, "entity_type"),
        ({"entity_type": "team", "entity_id": "t1", "metric": "m"}, "delta_usd_ref"),
        ("not a dict", "entity_type"),
    ],
)
def test_ut09_05_bad_action_lever_raises(lever: object, key: str) -> None:
    """UT09-05 a lever that is not a dict or lacks a key is a ReportContractError."""
    draft = _rec_copy(make_draft(), action_levers=[lever])
    with pytest.raises(e.ReportContractError) as exc:
        list(c.iter_text_fields(draft))
    assert exc.value.message == f"recommendations[0].action_levers[0] is missing {key}"


# --- UT09-09, UT09-10 load_draft ----------------------------------------------------------------


def _write_draft(root: Path, run_id: str = RUN_ID, body: bytes | None = None) -> Path:
    path = root / run_id / "draft.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(draft_dict()).encode() if body is None else body)
    return path


def test_ut09_09_missing_draft_is_draft_missing(tmp_path: Path) -> None:
    """UT09-09 no file: ReportContractError code draft_missing."""
    with pytest.raises(e.ReportContractError) as exc:
        c.load_draft(RUN_ID, reports_root=tmp_path)
    assert exc.value.details == {"code": "draft_missing", "run_id": RUN_ID}
    assert exc.value.message == f"Run `{RUN_ID}` has no valid report draft."


def test_ut09_09_loads_valid_draft_unmodified(tmp_path: Path) -> None:
    """UT09-09 a valid draft loads, matches its run_id and the file is untouched."""
    path = _write_draft(tmp_path)
    before = path.read_bytes()
    draft = c.load_draft(RUN_ID, reports_root=tmp_path)
    assert draft.run_id == RUN_ID
    assert path.read_bytes() == before


def test_ut09_09_default_root_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-09 reports_root None resolves to cfg.paths.data / reports."""
    _write_draft(tmp_path / "reports")
    fake = SimpleNamespace(paths=SimpleNamespace(data=str(tmp_path)))
    monkeypatch.setattr(_config, "get_config", lambda: fake)
    assert c.load_draft(RUN_ID).run_id == RUN_ID


@pytest.mark.parametrize("run_id", ["run_bad", "../x", "", f"{RUN_ID}/.."])
def test_ut09_09_invalid_run_id(tmp_path: Path, run_id: str) -> None:
    """UT09-09 an invalid run_id is refused before any path is built."""
    with pytest.raises(e.ReportContractError) as exc:
        c.load_draft(run_id, reports_root=tmp_path)
    assert exc.value.message == "invalid run_id"
    assert exc.value.details == {"code": "invalid_run_id"}


def test_ut09_10_schema_error_names_where(tmp_path: Path) -> None:
    """UT09-10 a bad query_id in a NumberRef: details.where is the dotted location."""
    body = draft_dict(sections=[{"id": "executive_summary", "title": "S", "paragraphs": [
        paragraph(), paragraph(numbers=[number_ref(query_id="q_bad")]),
    ]}])  # fmt: skip
    _write_draft(tmp_path, body=json.dumps(body).encode())
    with pytest.raises(e.ReportContractError) as exc:
        c.load_draft(RUN_ID, reports_root=tmp_path)
    assert exc.value.details["code"] == "draft_invalid"
    assert exc.value.details["run_id"] == RUN_ID
    assert exc.value.details["where"] == "sections.0.paragraphs.1.numbers.0.query_id"
    assert exc.value.__cause__ is None


def test_ut09_10_several_errors_and_bad_json(tmp_path: Path) -> None:
    """UT09-10 locations are joined by ", "; unparsable JSON reports the root."""
    _write_draft(tmp_path, body=json.dumps(draft_dict(title="", depth="x")).encode())
    with pytest.raises(e.ReportContractError) as exc:
        c.load_draft(RUN_ID, reports_root=tmp_path)
    assert exc.value.details["where"] == "depth, title"
    _write_draft(tmp_path, body=b"{not json")
    with pytest.raises(e.ReportContractError) as exc:
        c.load_draft(RUN_ID, reports_root=tmp_path)
    assert exc.value.details["where"] == "(root)"


def test_ut09_10_symlinked_draft_refused(tmp_path: Path) -> None:
    """UT09-10 a symlinked draft.json is refused (TH09-18)."""
    real = _write_draft(tmp_path / "elsewhere")
    link = tmp_path / "reports" / RUN_ID / "draft.json"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(real, link)
    except OSError:
        pytest.skip("symlinks need privileges on this host")
    with pytest.raises(e.ReportContractError) as exc:
        c.load_draft(RUN_ID, reports_root=tmp_path / "reports")
    assert exc.value.details == {"code": "draft_invalid", "run_id": RUN_ID}


def test_ut09_10_symlink_check_precedes_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-10 a path reported as a symlink is refused before it is read (no privilege needed)."""
    _write_draft(tmp_path)
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "draft.json")
    with pytest.raises(e.ReportContractError) as exc:
        c.load_draft(RUN_ID, reports_root=tmp_path)
    assert exc.value.details == {"code": "draft_invalid", "run_id": RUN_ID}


def test_ut09_10_symlinked_run_dir_outside_root_refused(tmp_path: Path) -> None:
    """UT09-10 a run directory resolving outside the reports root is refused."""
    _write_draft(tmp_path / "elsewhere")
    root = tmp_path / "reports"
    root.mkdir()
    target, link = tmp_path / "elsewhere" / RUN_ID, root / RUN_ID
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi  # noqa: PLC0415 - Windows only: a junction needs no symlink privilege

        _winapi.CreateJunction(str(target), str(link))
    with pytest.raises(e.ReportContractError) as exc:
        c.load_draft(RUN_ID, reports_root=root)
    assert exc.value.details == {"code": "draft_invalid", "run_id": RUN_ID}


def test_ut09_10_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-10 a draft above DRAFT_MAX_BYTES is refused with its size."""
    path = _write_draft(tmp_path)
    monkeypatch.setattr(c, "DRAFT_MAX_BYTES", 100)
    with pytest.raises(e.ReportContractError) as exc:
        c.load_draft(RUN_ID, reports_root=tmp_path)
    assert exc.value.details["size_bytes"] == str(path.stat().st_size)


def test_ut09_10_run_id_mismatch(tmp_path: Path) -> None:
    """UT09-10 a draft stored under another run's directory is refused."""
    other = "run_0123456789ABCDEFGHJKMNPQRS"  # pragma: allowlist secret
    _write_draft(tmp_path, run_id=other)
    with pytest.raises(e.ReportContractError) as exc:
        c.load_draft(other, reports_root=tmp_path)
    assert exc.value.details["draft_run_id"] == RUN_ID
    assert exc.value.details["run_id"] == other


# --- UT09-11 … UT09-17 check_render_contract ----------------------------------------------------


def test_ut09_11_schema_version(tmp_path: Path) -> None:
    """UT09-11 schema_version "2" (validation bypassed) is a schema_version violation."""
    draft = make_draft().model_copy(update={"schema_version": "2"})
    exc = _violations(draft)
    assert exc.details["rules"] == "schema_version"
    assert exc.details["where"] == "schema_version: unsupported schema_version"
    assert exc.message == f"1 report contract violations in run {RUN_ID}"


def test_ut09_12_unresolved_and_malformed_markers() -> None:
    """UT09-12 marker [[n9]] without a ref and malformed [[x1]]: two violations with paths."""
    draft = make_draft(sections=[{"id": "executive_summary", "title": "S", "paragraphs": [
        paragraph("Cost [[n1]] then [[n9]] and [[x1]] and [[n9]]"),
    ]}])  # fmt: skip
    exc = _violations(draft)
    assert exc.details["where"] == (
        "sections[0].paragraphs[0]: malformed marker, "
        "sections[0].paragraphs[0]: marker [[n9]] has no NumberRef"
    )
    assert exc.details["rules"] == "malformed_marker, unresolved_marker"
    assert exc.message.startswith("2 report contract violations")


def test_ut09_12_duplicate_number_ids() -> None:
    """UT09-12 duplicate NumberRef ids in a field are a violation (validation bypassed)."""
    draft = make_draft()
    dup = [*draft.recommendations[0].numbers, draft.recommendations[0].numbers[0]]
    exc = _violations(_rec_copy(draft, numbers=dup))
    assert exc.details["where"] == (
        "recommendations[0].headline: duplicate NumberRef id, "
        "recommendations[0].summary: duplicate NumberRef id"
    )


def test_ut09_13_unknown_query_id() -> None:
    """UT09-13 fake lookups missing one id: unknown query_id at its first where."""
    lookups = FakeLookups(known=(Q1,))
    exc = _violations(make_draft(), lookups)
    assert exc.details["where"] == f"recommendations[0].headline: unknown query_id {Q2}"
    assert exc.details["rules"] == "unknown_query_id"
    assert ("known_query_ids", [Q1, Q2]) in lookups.calls


def test_ut09_13_draft_query_ids_checked_and_format() -> None:
    """UT09-13 draft.query_ids are looked up too; malformed ids are violations, never looked up."""
    q3 = "q_1111111111111111"
    draft = make_draft().model_copy(update={"query_ids": [Q1, q3, "bad"]})
    lookups = FakeLookups()
    exc = _violations(draft, lookups)
    assert exc.details["where"] == (
        "query_ids[2]: invalid query_id, query_ids[1]: unknown query_id " + q3
    )
    assert exc.details["rules"] == "invalid_query_id, unknown_query_id"
    assert ("known_query_ids", [Q1, Q2, q3]) in lookups.calls


def test_ut09_14_rec_id_of_another_run() -> None:
    """UT09-14 a rec_id not recorded by the run is unknown."""
    draft = make_draft(recommendations=[recommendation(1, rec_id=REC_ID)])
    lookups = FakeLookups(recs=(_OTHER_REC,))
    exc = _violations(draft, lookups)
    assert exc.details["where"] == "recommendations[0].rec_id: unknown rec_id"
    assert ("run_rec_ids", RUN_ID) in lookups.calls


def test_ut09_15_unverified_finding() -> None:
    """UT09-15 a cited finding whose status is proposed is not verified."""
    lookups = FakeLookups(verified=())
    exc = _violations(make_draft(), lookups)
    assert (
        exc.details["where"] == f"sections[0].paragraphs[0]: finding {FINDING_ID} is not verified"
    )
    assert exc.details["rules"] == "unverified_finding"
    assert ("verified_finding_ids", [FINDING_ID]) in lookups.calls


def test_ut09_16_wrong_unit_and_unknown_ref() -> None:
    """UT09-16 expected_usd_ref to a pct number; an unknown delta_usd_ref."""
    draft = make_draft()
    pct = NumberRef.model_validate(number_ref("n5", "pct", 2.0))
    numbers = [*draft.recommendations[0].numbers, pct]
    lever = {"entity_type": "team", "entity_id": "t1", "metric": "m", "delta_usd_ref": "n7"}
    lever_pct = dict(lever, delta_usd_ref="n5")
    bad = _rec_copy(
        draft, numbers=numbers, expected_usd_ref="n5", effort_usd_ref="n1",
        action_levers=[lever, lever_pct],
    )  # fmt: skip
    exc = _violations(bad)
    where = "recommendations[0].summary"
    assert exc.details["where"] == (
        f"{where}.expected_usd_ref: wrong unit, {where}.effort_usd_ref: wrong unit, "
        f"{where}.action_levers[0].delta_usd_ref: unknown ref, "
        f"{where}.action_levers[1].delta_usd_ref: wrong unit"
    )
    assert exc.details["rules"] == "wrong_unit, unknown_ref"


def test_ut09_16_confidence_ref_any_unit_but_must_resolve() -> None:
    """UT09-16 confidence_ref may be any unit; an unknown one is still an unknown ref."""
    exc = _violations(_rec_copy(make_draft(), confidence_ref="n8"))
    assert exc.details["where"] == "recommendations[0].summary.confidence_ref: unknown ref"


def test_ut09_17_valid_draft_passes() -> None:
    """UT09-17 a valid draft returns None and consults each lookup once."""
    draft = make_draft(recommendations=[recommendation(1, rec_id=REC_ID)])
    lookups = FakeLookups()
    with capture_logs() as logs:
        assert c.check_render_contract(draft, lookups=lookups) is None
    assert logs == []
    assert [name for name, _ in lookups.calls] == [
        "known_query_ids", "run_rec_ids", "verified_finding_ids",
    ]  # fmt: skip


def test_ut09_17_no_ids_skips_lookups() -> None:
    """UT09-17 a draft citing nothing makes no lookup calls."""
    draft = make_draft(
        sections=[{"id": "executive_summary", "title": "S", "paragraphs": []}],
        recommendations=[], ranked_entities=[], query_ids=[],
    )  # fmt: skip
    lookups = FakeLookups()
    c.check_render_contract(draft, lookups=lookups)
    assert lookups.calls == []


def test_ut09_17_violation_logs_and_caps_listing() -> None:
    """UT09-17 violations log reports.contract.violated; the listing caps at 200 entries."""
    text = " ".join(["[[x]]"] * 250)
    draft = make_draft(sections=[{"id": "executive_summary", "title": "S", "paragraphs": [
        paragraph(text, numbers=[], finding_ids=[]),
    ]}])  # fmt: skip
    with capture_logs() as logs:
        exc = _violations(draft)
    assert exc.message == f"250 report contract violations in run {RUN_ID}"
    assert logs == [
        {
            "component": "reports.contract", "event": "reports.contract.violated",
            "log_level": "warning", "run_id": RUN_ID,
            "count": 250, "rules": "malformed_marker",
        }
    ]  # fmt: skip
    listed = c._listed([f"w{i}" for i in range(250)])
    assert listed.split(", ")[-2:] == ["w199", "… and 50 more"]
    assert len(listed.split(", ")) == 201
    assert c._listed(["a", "b"]) == "a, b"


# --- UT09-13 … UT09-15 OpsContractLookups (U09-07) ----------------------------------------------


@pytest.fixture
def wh_con() -> duckdb.DuckDBPyConnection:
    """An in-memory warehouse with a meta.evidence table."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA meta; CREATE TABLE meta.evidence (query_id VARCHAR)")
    return con


def test_ut09_13_ops_lookups_union_ops_and_warehouse(
    wh_con: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-13 known_query_ids = ops evidence plus meta.evidence, chunked, only argument ids."""
    ids = [f"q_{n:016x}" for n in range(1, 1201)]
    wh_con.executemany("INSERT INTO meta.evidence VALUES (?)", [[q] for q in ids[600:1100]])
    wh_con.execute("INSERT INTO meta.evidence VALUES ('q_ffffffffffffffff')")
    seen: list[list[str]] = []

    def present(query_ids: Collection[str]) -> set[str]:
        seen.append(list(query_ids))
        return set(ids[:10]) | {"q_eeeeeeeeeeeeeeee"}

    monkeypatch.setattr(ops, "ui_evidence_ids_present", present)
    found = c.OpsContractLookups(wh_con, build_id="b1").known_query_ids([*ids, ids[0]])
    assert found == set(ids[:10]) | set(ids[600:1100])
    assert seen == [ids]


def test_ut09_13_ops_lookups_duckdb_error_is_query_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT09-13 a DuckDB error becomes QueryError naming build_id."""
    monkeypatch.setattr(ops, "ui_evidence_ids_present", lambda ids: set())
    lookups = c.OpsContractLookups(duckdb.connect(":memory:"), build_id="b7")
    with pytest.raises(e.QueryError) as exc:
        lookups.known_query_ids([Q1])
    assert exc.value.details == {"build_id": "b7"}
    assert "b7" in exc.value.message


def test_ut09_14_ops_lookups_run_rec_ids(
    wh_con: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-14 run_rec_ids delegates to ui_run_rec_ids."""
    monkeypatch.setattr(ops, "ui_run_rec_ids", lambda run_id: {f"rec_for_{run_id}"})
    assert c.OpsContractLookups(wh_con).run_rec_ids(RUN_ID) == {f"rec_for_{RUN_ID}"}


def test_ut09_15_ops_lookups_verified_findings(
    wh_con: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-15 verified_finding_ids asks for status verified and returns only argument ids."""
    asked: list[tuple[set[str], str]] = []

    def with_status(ids: Collection[str], status: str) -> set[str]:
        asked.append((set(ids), status))
        return {"fnd_a", "fnd_other"}

    monkeypatch.setattr(ops, "ui_finding_ids_with_status", with_status)
    found = c.OpsContractLookups(wh_con).verified_finding_ids(["fnd_a", "fnd_b"])
    assert found == {"fnd_a"}
    assert asked == [({"fnd_a", "fnd_b"}, "verified")]


# --- UT09-18 unconfirmed_weight_keys ------------------------------------------------------------


def test_ut09_18_unconfirmed_weight_keys() -> None:
    """UT09-18 top-level and nested unconfirmed blocks, sorted, dotted for nested."""
    weights: dict[str, object] = {
        "zeta": {"unconfirmed": True, "inner": {"unconfirmed": True}},
        "alpha": {"unconfirmed": True, "x": 1},
        "beta": {"c": {"unconfirmed": True}, "d": {"unconfirmed": False}, "e": 3},
        "gamma": 3,
        "delta": {"unconfirmed": "true", "deep": {"deeper": {"unconfirmed": True}}},
    }
    assert c.unconfirmed_weight_keys(weights) == ["alpha", "beta.c", "zeta", "zeta.inner"]
    assert c.unconfirmed_weight_keys({}) == []
