"""Tests for herness.enrich.laya_admin (U03-138 ... U03-140, T03-33)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.support.fake_laya import write_laya_version
from tests.unit.enrich import _laya_admin_fixtures as fx

from herness.core.config import HernessConfig
from herness.core.errors import ConfigError
from herness.enrich import laya_admin
from herness.enrich.labels import gold_digest
from herness.enrich.laya_models import read_current, write_current
from herness.enrich.layout import EnrichPaths

pytestmark = pytest.mark.unit

type AuditCalls = list[tuple[tuple[object, ...], dict[str, object]]]


@pytest.fixture
def cfg(tmp_path: Path) -> HernessConfig:
    return fx.build_cfg(tmp_path)


@pytest.fixture
def paths(cfg: HernessConfig) -> EnrichPaths:
    return EnrichPaths.from_config(cfg)


@pytest.fixture
def audit_calls(monkeypatch: pytest.MonkeyPatch) -> AuditCalls:
    """Records `laya_admin.audit` calls instead of writing a real audit line."""
    calls: AuditCalls = []
    monkeypatch.setattr(laya_admin, "audit", lambda *a, **kw: calls.append((a, kw)))
    return calls


# --- UT03-130 ---------------------------------------------------------------------------------


def test_ut03_130_accept_subset_then_refused(
    paths: EnrichPaths, audit_calls: AuditCalls, cfg: HernessConfig
) -> None:
    """UT03-130 accept [root_cause]: CURRENT written, manifest updated, audit called; accept
    [change_caused] (not proposed) raises ConfigError."""
    store = fx.gold_store(paths)
    digest = gold_digest(store.read("gold"))
    write_laya_version(paths.laya_root(), fx.V1, status="candidate")
    fx.write_eval(
        paths, fx.V1, gold_sha256=digest,
        questions={
            "root_cause": {"accepted_proposed": True},
            "change_caused": {"accepted_proposed": False},
        },
    )  # fmt: skip

    laya_admin.accept_model(fx.V1, ["root_cause"])

    assert read_current(paths) == fx.V1
    manifest = json.loads((paths.laya_dir(fx.V1) / "manifest.json").read_text("utf-8"))
    assert manifest["status"] == "accepted"
    assert manifest["accepted_questions"] == ["root_cause"]
    assert manifest["accepted_by"].startswith("os:")
    assert manifest["accepted_at"] is not None
    assert len(audit_calls) == 1
    (_args, kwargs) = audit_calls[0]
    assert kwargs == {"action": "laya_accept", "target": fx.V1, "detail": ["root_cause"]}

    with pytest.raises(ConfigError, match="not accepted_proposed"):
        laya_admin.accept_model(fx.V1, ["change_caused"])


def test_ut03_130_default_accepts_every_proposed_question(paths: EnrichPaths) -> None:
    """UT03-130 no `questions` argument: every `accepted_proposed` question is accepted."""
    store = fx.gold_store(paths)
    digest = gold_digest(store.read("gold"))
    write_laya_version(paths.laya_root(), fx.V1, status="candidate")
    fx.write_eval(
        paths, fx.V1, gold_sha256=digest,
        questions={
            "root_cause": {"accepted_proposed": True},
            "change_caused": {"accepted_proposed": True},
            "business_impact": {"accepted_proposed": False},
        },
    )  # fmt: skip

    laya_admin.accept_model(fx.V1)

    manifest = json.loads((paths.laya_dir(fx.V1) / "manifest.json").read_text("utf-8"))
    assert manifest["accepted_questions"] == ["change_caused", "root_cause"]


def test_ut03_130_empty_question_set_rejected(paths: EnrichPaths) -> None:
    """UT03-130 no proposed questions, or an explicit empty list, raises ConfigError."""
    store = fx.gold_store(paths)
    digest = gold_digest(store.read("gold"))
    write_laya_version(paths.laya_root(), fx.V1, status="candidate")
    fx.write_eval(paths, fx.V1, gold_sha256=digest, questions={})

    with pytest.raises(ConfigError, match="accepted_proposed question"):
        laya_admin.accept_model(fx.V1)
    with pytest.raises(ConfigError, match="accepted_proposed question"):
        laya_admin.accept_model(fx.V1, [])


def test_ut03_130_missing_eval_json_rejected(paths: EnrichPaths) -> None:
    """UT03-130 no eval.json next to the manifest raises ConfigError (nothing was evaluated)."""
    write_laya_version(paths.laya_root(), fx.V1, status="candidate")
    with pytest.raises(ConfigError, match=r"eval\.json missing"):
        laya_admin.accept_model(fx.V1)


def test_ut03_130_oversized_or_malformed_eval_json_rejected(paths: EnrichPaths) -> None:
    """UT03-130 an eval.json over the size cap, or without a `questions` object, is refused."""
    write_laya_version(paths.laya_root(), fx.V1, status="candidate")
    huge = paths.laya_dir(fx.V1) / "eval.json"
    huge.write_bytes(b" " * (laya_admin._MAX_EVAL_BYTES + 1))
    with pytest.raises(ConfigError, match="too large"):
        laya_admin.accept_model(fx.V1)
    huge.write_text(json.dumps({"version": fx.V1}), encoding="utf-8")  # no "questions" key
    with pytest.raises(ConfigError, match="invalid"):
        laya_admin.accept_model(fx.V1)


# --- UT03-131 ---------------------------------------------------------------------------------


def test_ut03_131_rollback_and_status(paths: EnrichPaths, audit_calls: AuditCalls) -> None:
    """UT03-131 rollback changes CURRENT and audits `from=`; status lists both versions."""
    write_laya_version(paths.laya_root(), fx.V1, status="accepted")
    write_laya_version(paths.laya_root(), fx.V2, status="accepted")
    write_current(paths, fx.V2)

    laya_admin.rollback_model(fx.V1)

    assert read_current(paths) == fx.V1
    (_args, kwargs) = audit_calls[0]
    assert kwargs == {"action": "laya_rollback", "target": fx.V1, "detail": f"from={fx.V2}"}

    status = laya_admin.laya_status()
    assert status["current"] == fx.V1
    versions = status["versions"]
    assert isinstance(versions, list)
    by_version = {v["version"]: v for v in versions}
    assert set(by_version) == {fx.V1, fx.V2}
    assert all(v["status"] == "accepted" for v in by_version.values())
    assert [v["version"] for v in versions] == [fx.V2, fx.V1]  # newest first


def test_ut03_131_rollback_requires_accepted_status(paths: EnrichPaths) -> None:
    """UT03-131 rollback to a candidate (never accepted) version raises ConfigError."""
    write_laya_version(paths.laya_root(), fx.V1, status="candidate")
    with pytest.raises(ConfigError, match="status not allowed"):
        laya_admin.rollback_model(fx.V1)


def test_ut03_131_rollback_from_missing_current_records_none(
    paths: EnrichPaths, audit_calls: AuditCalls
) -> None:
    """UT03-131 rollback with no prior CURRENT: audit `detail` reads `from=none`."""
    write_laya_version(paths.laya_root(), fx.V1, status="accepted")
    laya_admin.rollback_model(fx.V1)
    (_args, kwargs) = audit_calls[0]
    assert kwargs["detail"] == "from=none"


def test_ut03_131_status_skips_and_flags_bad_entries(paths: EnrichPaths) -> None:
    """UT03-131 a non-matching dir is skipped; a matching one with a bad manifest is invalid."""
    paths.laya_root().mkdir(parents=True)
    (paths.laya_root() / "CURRENT").write_bytes(b"")
    (paths.laya_root() / "notes").mkdir()
    write_laya_version(paths.laya_root(), fx.V1, status="accepted")
    (paths.laya_root() / fx.V2).mkdir()  # matches the pattern but has no manifest

    status = laya_admin.laya_status()

    versions = status["versions"]
    assert isinstance(versions, list)
    by_version = {v["version"]: v for v in versions}
    assert set(by_version) == {fx.V1, fx.V2}
    assert by_version[fx.V2]["status"] == "invalid"
    assert by_version[fx.V1]["status"] == "accepted"
    assert status["current"] is None


def test_ut03_131_status_reads_accepted_proposed_and_macro_metric(paths: EnrichPaths) -> None:
    """UT03-131 an eval.json next to the manifest supplies accepted_proposed and macro_metric."""
    write_laya_version(paths.laya_root(), fx.V1, status="candidate")
    fx.write_eval(
        paths, fx.V1, macro_metric=0.91,
        questions={
            "root_cause": {"accepted_proposed": True},
            "change_caused": {"accepted_proposed": False},
        },
    )  # fmt: skip

    status = laya_admin.laya_status()

    versions = status["versions"]
    assert isinstance(versions, list)
    entry = versions[0]
    assert isinstance(entry, dict)
    assert entry["accepted_proposed"] == ["root_cause"]
    assert entry["macro_metric"] == pytest.approx(0.91)


def test_ut03_131_status_without_eval_has_empty_proposed_and_no_metric(
    paths: EnrichPaths,
) -> None:
    """UT03-131 a version with no eval.json: accepted_proposed [], macro_metric None."""
    write_laya_version(paths.laya_root(), fx.V1, status="accepted")

    status = laya_admin.laya_status()

    entry = status["versions"][0]  # type: ignore[index]
    assert isinstance(entry, dict)
    assert entry["accepted_proposed"] == []
    assert entry["macro_metric"] is None


def test_ut03_131_status_oversized_or_invalid_manifest_is_invalid(paths: EnrichPaths) -> None:
    """UT03-131 a manifest over the size cap, or failing schema validation, is invalid."""
    write_laya_version(paths.laya_root(), fx.V1, status="accepted")
    (paths.laya_dir(fx.V1) / "manifest.json").write_bytes(
        b" " * (laya_admin._MAX_MANIFEST_BYTES + 1)
    )
    write_laya_version(paths.laya_root(), fx.V2, status="accepted")
    (paths.laya_dir(fx.V2) / "manifest.json").write_text(json.dumps({"version": fx.V2}), "utf-8")

    status = laya_admin.laya_status()

    versions = status["versions"]
    assert isinstance(versions, list)
    by_version = {v["version"]: v for v in versions}
    assert by_version[fx.V1]["status"] == "invalid"
    assert by_version[fx.V2]["status"] == "invalid"


def test_ut03_131_status_survives_an_os_error_on_one_entry(
    paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-131 an OSError probing one entry's kind marks only that entry invalid."""
    write_laya_version(paths.laya_root(), fx.V1, status="accepted")
    write_laya_version(paths.laya_root(), fx.V2, status="accepted")
    real_is_dir = Path.is_dir

    def flaky_is_dir(self: Path) -> bool:
        if self.name == fx.V1:
            msg = "boom"
            raise OSError(msg)
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", flaky_is_dir)

    status = laya_admin.laya_status()

    versions = status["versions"]
    assert isinstance(versions, list)
    by_version = {v["version"]: v for v in versions}
    assert by_version[fx.V1]["status"] == "invalid"
    assert by_version[fx.V2]["status"] == "accepted"
