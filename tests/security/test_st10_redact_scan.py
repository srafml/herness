"""Security test for the fixture PII scanner (impl 10 ST10-30, TH10-06, T10-11).

The real-format values are built at runtime so no repository file holds them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from herness.core import redact_scan

pytestmark = pytest.mark.unit

SSN = "-".join(("123", "45", "6789"))
DENYLISTED = "hr" + "@" + "realcorp.test"


def test_st10_30_real_ssn_and_denylisted_email_fail_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST10-30 a fixture with a real-format SSN and an e-mail at a denylisted domain: exit 1."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config" / "profiles").mkdir(parents=True)
    (tmp_path / "config" / "profiles" / "synth.yaml").write_text(
        "security:\n  redaction:\n    denylist_domains: [realcorp.test]\n", "utf-8"
    )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "incidents.jsonl").write_text(
        '{"id": "INC0000001", "text": "user ssn ' + SSN + '"}\n'
        '{"id": "INC0000002", "text": "contact ' + DENYLISTED + '"}\n',
        "utf-8",
    )
    assert redact_scan.main(["--scan", "fixtures"]) == 1
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "fixtures/incidents.jsonl:1:40 NATIONAL_ID",
        "fixtures/incidents.jsonl:2:39 EMAIL",
        "fixtures/incidents.jsonl:2:42 DENYLISTED_DOMAIN",
    ]
    assert SSN not in out
    assert "realcorp" not in out
    assert "hr@" not in out


def test_st10_30_denylisted_host_in_a_url_is_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST10-30 a denylisted host in a URL is a finding although no detector fires on it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "herness.yaml").write_text(
        "security:\n  redaction:\n    denylist_domains: [realcorp.test]\n", "utf-8"
    )
    (tmp_path / "doc.md").write_text("see https://wiki.RealCorp.test/page\n", "utf-8")
    assert redact_scan.main(["--scan", "doc.md"]) == 1
    assert capsys.readouterr().out == "doc.md:1:18 DENYLISTED_DOMAIN\n"
