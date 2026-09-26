"""Tests for herness.reports.charts: pure inline SVG charts (T09-06)."""

import xml.etree.ElementTree as ET
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.reports import charts

pytestmark = pytest.mark.unit

_BANNED = ("script", "href", "url(")


def _parse(svg: str) -> ET.Element:
    root = ET.fromstring(svg)  # noqa: S314 - trusted generated input
    assert root.tag == "svg"
    assert root.get("role") == "img"
    assert root.get("viewBox")
    assert root[0].tag == "title"
    return root


def _all(root: ET.Element, tag: str) -> list[ET.Element]:
    return list(root.iter(tag))


def _assert_safe(svg: str) -> None:
    root = _parse(svg)
    for el in root.iter():
        assert el.tag != "script"
        for name, value in el.attrib.items():
            assert "href" not in name.lower()
            assert not name.lower().startswith("on")
            assert "url(" not in value.lower()
    assert "<script" not in svg.lower()


def _assert_clean(svg: str) -> None:
    _assert_safe(svg)
    assert not any(word in svg.lower() for word in _BANNED)


def test_ut09_28_bar_h_rects_negative_and_escaping() -> None:
    """UT09-28 3 items incl. a negative: 3 rects, negative width 0, label escaped, no script."""
    svg = charts.bar_h(
        [("a <&> b", Decimal(10)), ("neg", -5.0), ("half", 5.0)],
        title="Priority <script>",
        highlight=frozenset({"half"}),
    )
    _assert_safe(svg)
    assert "a &lt;&amp;&gt; b" in svg
    assert "<script>" not in svg
    assert "href" not in svg
    root = _parse(svg)
    rects = _all(root, "rect")
    assert len(rects) == 3
    widths = [float(r.get("width", "")) for r in rects]
    assert widths[1] == 0.0
    assert "neg" in (rects[1].get("class") or "")
    assert widths[0] == pytest.approx(2 * widths[2], abs=0.1)
    assert "hl" in (rects[2].get("class") or "")
    assert "hl" not in (rects[0].get("class") or "")
    assert root.get("viewBox") == f"0 0 640 {3 * (18 + 6) + 24}"


def test_ut09_28_bar_h_nan_zero_truncation_and_limits() -> None:
    """UT09-28 NaN is width 0 with neg; all-zero input; long labels cut; max_items honoured."""
    root = _parse(charts.bar_h([("x", float("nan")), ("y", 0.0)], title="t"))
    rects = _all(root, "rect")
    assert [float(r.get("width", "")) for r in rects] == [0.0, 0.0]
    assert "neg" in (rects[0].get("class") or "")
    root = _parse(charts.bar_h([("L" * 80, 1.0)] * 5, title="t", max_items=2, width=5000))
    assert len(_all(root, "rect")) == 2
    labels = [t.text for t in _all(root, "text")]
    assert labels[0] == "L" * 59 + "…"
    assert root.get("viewBox", "").startswith("0 0 1600 ")


def test_ut09_29_step_budget_path_and_dashed_budget() -> None:
    """UT09-29 steps with budget below total: one step path and a dashed budget line."""
    steps = [(Decimal(100), Decimal(50)), (Decimal(200), Decimal(-1)), (Decimal(300), Decimal(150))]
    svg = charts.step_budget(steps, budget=Decimal(250), title="Spend")
    _assert_safe(svg)
    root = _parse(svg)
    paths = _all(root, "path")
    assert len(paths) == 1
    d = paths[0].get("d", "")
    assert d.startswith("M")
    assert d.count("H") == 3
    assert d.count("V") == 3
    budget = [ln for ln in _all(root, "line") if ln.get("class") == "budget"]
    assert len(budget) == 1
    assert budget[0].get("stroke-dasharray")
    last_x = float(d.split("H")[-1].split("V")[0])
    assert float(budget[0].get("x1", "")) < last_x


def test_ut09_29_step_budget_empty_and_budget_beyond_total() -> None:
    """UT09-29 empty steps draw axes and budget only; budget > total sets the x range."""
    root = _parse(charts.step_budget([], budget=Decimal(0), title="t"))
    assert _all(root, "path") == []
    assert len([ln for ln in _all(root, "line") if ln.get("class") == "budget"]) == 1
    assert len([ln for ln in _all(root, "line") if ln.get("class") == "axis"]) == 2
    root = _parse(charts.step_budget([(Decimal(10), Decimal(1))], budget=Decimal(20), title="t"))
    lines = _all(root, "line")
    budget = next(ln for ln in lines if ln.get("class") == "budget")
    right = max(float(ln.get("x2", "")) for ln in lines if ln.get("class") == "axis")
    assert float(budget.get("x1", "")) == pytest.approx(right)


def test_ut09_29_step_budget_unconvertible_values_count_as_zero() -> None:
    """UT09-29 an unconvertible or non-finite effort, impact or budget is treated as 0."""
    steps = [
        (None, Decimal(2)),
        (Decimal("NaN"), Decimal("sNaN")),
        (Decimal(4), 1.5),
        (10**5000, Decimal(1)),
    ]
    root = _parse(charts.step_budget(steps, budget="x", title="t"))  # type: ignore[arg-type]
    d = _all(root, "path")[0].get("d", "")
    assert d.split()[2] == "H8.0"
    budget = next(ln for ln in _all(root, "line") if ln.get("class") == "budget")
    assert budget.get("x1") == "8.0"


def test_ut09_30_sparkline_cases() -> None:
    """UT09-30 0 values: empty class; 1: a circle; constant: flat mid line; None gap: 2 lines."""
    root = _parse(charts.sparkline([], title="none"))
    assert root.get("class") == "empty"
    assert len(list(root)) == 1
    root = _parse(charts.sparkline([None, 3.0, None], title="one"))
    assert len(_all(root, "circle")) == 1
    assert _all(root, "polyline") == []
    root = _parse(charts.sparkline([2.0, 2.0, 2.0], title="flat", height=24))
    (line,) = _all(root, "polyline")
    ys = {pt.split(",")[1] for pt in line.get("points", "").split()}
    assert ys == {"12.0"}
    root = _parse(charts.sparkline([1.0, 2.0, None, 3.0, 4.0, None, 5.0], title="gap"))
    assert len(_all(root, "polyline")) == 2
    assert len(_all(root, "circle")) == 1
    _assert_safe(charts.sparkline([1.0, float("nan"), float("inf"), 2.0], title="<x>"))


def test_ut09_31_dot_z_clipped_at_edge() -> None:
    """UT09-31 z = 5 with clip 3 sits at the right edge with class clipped."""
    rows = [("team <a>", 5.0), ("b", 0.0), ("c", None), ("d", -1.5)]
    root = _parse(charts.dot_z(rows, title="z", clip=3.0))
    dots = _all(root, "circle")
    assert len(dots) == 3
    assert dots[0].get("class") == "clipped"
    assert dots[1].get("class") != "clipped"
    zero = next(ln for ln in _all(root, "line") if ln.get("class") == "zero")
    assert float(dots[1].get("cx", "")) == pytest.approx(float(zero.get("x1", "")))
    assert float(dots[0].get("cx", "")) == max(float(d.get("cx", "")) for d in dots)
    assert [t.text for t in _all(root, "text")] == ["team <a>", "b", "c", "d"]


_safe_text = st.text(max_size=150).filter(lambda s: not any(w in s.lower() for w in _BANNED))
_nums = st.one_of(st.floats(), st.decimals())
_size = st.integers(min_value=-10, max_value=5000)


@given(
    st.lists(st.tuples(_safe_text, _nums), max_size=110),
    _safe_text,
    _size,
    st.integers(min_value=-10, max_value=200),
    st.integers(min_value=-10, max_value=200),
)
def test_pt09_07_bar_h_well_formed(
    items: list[tuple[str, Decimal | float]], title: str, width: int, bar: int, most: int
) -> None:
    """PT09-07 bar_h: random input gives well-formed XML without script, href or url(."""
    labels = frozenset(label for label, _ in items[:3])
    _assert_clean(
        charts.bar_h(
            items, title=title, width=width, bar_height=bar, max_items=most, highlight=labels
        )
    )


@given(
    st.lists(st.tuples(st.decimals(), st.decimals()), max_size=520),
    st.decimals(),
    _safe_text,
    _size,
    _size,
)
def test_pt09_07_step_budget_well_formed(
    steps: list[tuple[Decimal, Decimal]], budget: Decimal, title: str, width: int, height: int
) -> None:
    """PT09-07 step_budget: random input gives well-formed XML without script, href or url(."""
    _assert_clean(charts.step_budget(steps, budget=budget, title=title, width=width, height=height))


@given(st.lists(st.one_of(st.none(), st.floats()), max_size=60), _safe_text, _size, _size)
def test_pt09_07_sparkline_well_formed(
    values: list[float | None], title: str, width: int, height: int
) -> None:
    """PT09-07 sparkline: random input gives well-formed XML without script, href or url(."""
    _assert_clean(charts.sparkline(values, title=title, width=width, height=height))


@given(
    st.lists(st.tuples(_safe_text, st.one_of(st.none(), st.floats())), max_size=60),
    _safe_text,
    st.floats(),
    _size,
)
def test_pt09_07_dot_z_well_formed(
    rows: list[tuple[str, float | None]], title: str, clip: float, width: int
) -> None:
    """PT09-07 dot_z: random input gives well-formed XML without script, href or url(."""
    _assert_clean(charts.dot_z(rows, title=title, clip=clip, width=width))


@given(st.text(max_size=200))
def test_pt09_07_hostile_labels_stay_text(label: str) -> None:
    """PT09-07 any label text, markup included, is escaped: no script element or href attribute."""
    hostile = label + '<script>alert(1)</script><a href="x" style="url(y)">'
    _assert_safe(charts.bar_h([(hostile, 1.0)], title=hostile))
    _assert_safe(charts.dot_z([(hostile, 1.0)], title=hostile))
    _assert_safe(charts.sparkline([1.0], title=hostile))
    _assert_safe(charts.step_budget([], budget=Decimal(1), title=hostile))
