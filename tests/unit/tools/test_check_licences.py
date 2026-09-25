"""Tests for tools.check_licences (U00-58)."""

import json
from pathlib import Path
from typing import Any

import pytest

from tools.check_licences import evaluate, licence_texts, main

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]

ALLOWED = frozenset(
    ["MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "PSF-2.0", "Python-2.0", "ISC", "MPL-2.0"]
)

_PYPROJECT = """\
[tool.herness.licences]
allowed = ["MIT", "BSD-3-Clause", "Apache-2.0"]
aliases = { "MIT License" = "MIT" }
report_only = ["nvidia-*"]

[[tool.herness.licences.approved]]
package = "pil*"
licence = "HPND"
approved_by = "reviewer"
approved_on = 2026-09-01
reason = "historical permissive licence"
"""


def _component(name: str, licenses: list[dict[str, Any]] | None) -> dict[str, Any]:
    comp: dict[str, Any] = {"type": "library", "name": name, "version": "1.0", "bom-ref": name}
    if licenses is not None:
        comp["licenses"] = licenses
    return comp


def _sbom(path: Path, components: list[dict[str, Any]]) -> None:
    doc = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {"component": _component("herness", None)},
        "components": [_component("herness", None), *components],
    }
    path.write_text(json.dumps(doc), "utf-8")


def test_ut00_70_evaluates_licence_expressions() -> None:
    """UT00-70 OR, parenthesised AND, WITH and an alias are allowed; GPL alone is denied."""
    assert evaluate("MIT OR GPL-3.0-only", ALLOWED)
    assert evaluate("(Apache-2.0 AND BSD-3-Clause)", ALLOWED)
    assert evaluate("Apache-2.0 WITH LLVM-exception", ALLOWED)
    assert not evaluate("GPL-3.0-only", ALLOWED)
    component = {"name": "x", "licenses": [{"license": {"name": "MIT License"}}]}
    texts = licence_texts(component, {"mit license": "MIT"})
    assert texts == ["MIT"]
    assert evaluate(texts[0], ALLOWED)


def test_st00_16_licence_gate_enforce_and_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST00-16 GPL and unknown are denied, nvidia is report-only, pillow is approved."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(_PYPROJECT, "utf-8")
    sbom = tmp_path / "sbom.cdx.json"
    _sbom(
        sbom,
        [
            _component("ok-lib", [{"license": {"id": "MIT"}}]),
            _component("gpl-lib", [{"license": {"id": "GPL-3.0-only"}}]),
            _component("mystery", None),
            _component("nvidia-cublas-cu12", [{"license": {"name": "NVIDIA Proprietary"}}]),
            _component("Pillow", [{"license": {"id": "HPND"}}]),
        ],
    )
    base = ["--sbom", str(sbom), "--pyproject", str(pyproject)]

    code = main(base)
    out = capsys.readouterr().out

    assert code == 1
    assert "LC001 denied gpl-lib 1.0: GPL-3.0-only" in out
    assert "LC001 denied mystery 1.0: UNKNOWN" in out
    assert "LC002 report-only nvidia-cublas-cu12 1.0: NVIDIA Proprietary" in out
    assert "approved pillow 1.0: HPND" in out
    assert "ok-lib" not in out
    assert "herness" not in out

    code = main([*base, "--mode", "report"])
    out = capsys.readouterr().out

    assert code == 0
    assert "LC001 warning denied gpl-lib 1.0: GPL-3.0-only" in out


def test_check_licences_alternatives_and_nested_expressions() -> None:
    """Separate licence entries are alternatives; nested groups and case-insensitive aliases."""
    component = {
        "name": "dual",
        "licenses": [
            {"license": {"id": "GPL-2.0-only"}},
            {"license": {"name": "apache software license"}},
            {"expression": "MIT AND (BSD-3-Clause OR GPL-2.0-only)"},
        ],
    }
    texts = licence_texts(component, {"apache software license": "Apache-2.0"})
    assert texts == ["GPL-2.0-only", "Apache-2.0", "MIT AND (BSD-3-Clause OR GPL-2.0-only)"]
    assert [evaluate(text, ALLOWED) for text in texts] == [False, True, True]
    assert not evaluate("(MIT) AND (GPL-2.0-only)", ALLOWED)
    assert not evaluate("MIT AND (GPL-2.0-only OR LGPL-2.1-only)", ALLOWED)
    assert licence_texts({"name": "none", "licenses": []}, {}) == ["UNKNOWN"]
    trove = {"license": {"name": "License :: OSI Approved :: MIT License"}}
    lgpl = {"license": {"name": "License :: OSI Approved :: LGPLv2+"}}
    assert licence_texts({"licenses": [trove, lgpl]}, {"MIT License": "MIT"}) == [
        "MIT",
        "License :: OSI Approved :: LGPLv2+",
    ]


@pytest.mark.parametrize(
    ("pyproject_text", "sbom_text"),
    [
        ("[tool.herness]\nx = 1\n", '{"components": []}'),
        ('[tool.herness.licences]\nallowed = "MIT"\n', '{"components": []}'),
        ('[tool.herness.licences]\nallowed = ["MIT"]\n', "{not json"),
        ('[tool.herness.licences]\nallowed = ["MIT"]\n', '{"components": {}}'),
        ('[tool.herness.licences]\nallowed = ["MIT"]\n', '{"components": [{"version": "1"}]}'),
    ],
    ids=["missing-table", "bad-allowed", "bad-json", "bad-components", "nameless"],
)
def test_check_licences_rejects_malformed_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], pyproject_text: str, sbom_text: str
) -> None:
    """A malformed SBOM or licence table is an input error (exit 2)."""
    (tmp_path / "pyproject.toml").write_text(pyproject_text, "utf-8")
    (tmp_path / "sbom.json").write_text(sbom_text, "utf-8")

    code = main(
        ["--sbom", str(tmp_path / "sbom.json"), "--pyproject", str(tmp_path / "pyproject.toml")]
    )

    assert code == 2
    assert "input error" in capsys.readouterr().err
    assert main(["--mode", "enforce"]) == 2
    capsys.readouterr()


def test_check_licences_repository_table_is_valid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The repository's own licence table loads and allows an MIT component."""
    sbom = tmp_path / "sbom.json"
    _sbom(sbom, [_component("ok-lib", [{"license": {"name": "MIT License"}}])])

    code = main(["--sbom", str(sbom), "--pyproject", str(ROOT / "pyproject.toml")])

    assert (code, capsys.readouterr().out) == (0, "")
