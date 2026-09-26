"""Pure-Python inline SVG charts for reports and the dashboard (impl 09 §3.5).

Every chart returns one ``<svg>`` element string with ``role="img"``, a ``<title>`` child and a
fixed ``viewBox``; no script, no href, no external reference and no event attribute. Colours
are CSS variables only. Data text (labels, titles) is stripped of characters XML cannot carry
and escaped (TH09-01). Charts carry no numeric labels: the data table after each chart does.
Out-of-range sizes are clamped into their documented ranges; no function raises.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, localcontext
from typing import Final, NamedTuple
from xml.sax.saxutils import escape, quoteattr

_BAR: Final = "var(--c-bar)"
_ACCENT: Final = "var(--c-accent)"
_MUTED: Final = "var(--c-muted)"
_AXIS: Final = "var(--c-axis)"
_ELLIPSIS: Final = "…"
_TITLE_MAX: Final = 120
_LABEL_MAX: Final = 60
_PAD: Final = 8.0
_ROW: Final = 18
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_MAX_STEPS: Final = 500
_MAX_POINTS: Final = 52
_MAX_DOT_ROWS: Final = 50
# XML 1.0 Char production: anything else would make the SVG ill-formed.
_NON_XML: Final = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")
# Unbounded exponent range and no traps: sums of huge values never raise.
_DEC: Final = Context(prec=28, Emax=MAX_EMAX, Emin=MIN_EMIN, traps=[])


class _Rows(NamedTuple):
    """Geometry of a labelled row chart (bar_h, dot_z)."""

    label_w: float
    span: float
    pitch: int
    size: int


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _num(value: float) -> str:
    return f"{round(value, 1) + 0.0:.1f}"


def _text(value: object, limit: int) -> str:
    clean = _NON_XML.sub("", str(value))
    if len(clean) > limit:
        clean = clean[: limit - 1] + _ELLIPSIS
    return escape(clean)


def _as_float(value: object) -> float | None:
    """Float of a Decimal or number; None for None, NaN or anything unconvertible."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):  # ValueError: signalling NaN Decimal
        return None
    return None if math.isnan(number) else number


def _finite(value: object) -> float | None:
    number = _as_float(value)
    return number if number is not None and math.isfinite(number) else None


def _nonneg(value: object) -> Decimal:
    """A finite non-negative Decimal; negative, non-finite or unconvertible values give 0.

    Called under the trap-free context, where bad text converts to NaN instead of raising.
    """
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except ValueError:  # int beyond the str digit limit
        return _ZERO
    return number if number.is_finite() and number > _ZERO else _ZERO


def _open(width: int, height: int, title: str, cls: str) -> str:
    return (
        f'<svg role="img" class={quoteattr(cls)} viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}"><title>{_text(title, _TITLE_MAX)}</title>'
    )


def _row_label(rows: _Rows, index: int, label: object, cls: str) -> str:
    y = _ROW / 2 + index * rows.pitch + rows.size / 2
    return (
        f'<text class="{cls}" x="{_num(rows.label_w - 6)}" y="{_num(y)}" text-anchor="end" '
        f'dominant-baseline="middle" style="fill:{_AXIS}">{_text(label, _LABEL_MAX)}</text>'
    )


def _bar(rows: _Rows, index: int, width: float, classes: list[str]) -> str:
    fill = _MUTED if "neg" in classes else _ACCENT if "hl" in classes else _BAR
    y = _ROW / 2 + index * rows.pitch
    return (
        f'<rect class="{" ".join(classes)}" x="{_num(rows.label_w)}" y="{_num(y)}" '
        f'width="{_num(width)}" height="{rows.size}" style="fill:{fill}"/>'
    )


def bar_h(
    items: Sequence[tuple[str, Decimal | float]],
    *,
    title: str,
    width: int = 640,
    bar_height: int = 18,
    max_items: int = 25,
    highlight: frozenset[str] = frozenset(),
) -> str:
    """Horizontal bar chart of priority (U09-19); negative and NaN values are 0 wide, ``neg``."""
    width = _clamp(width, 200, 1600)
    size = _clamp(bar_height, 8, 40)
    shown = list(items[: _clamp(max_items, 1, 100)])
    label_w = width * 0.38
    rows = _Rows(label_w, width - label_w - _PAD, size + 6, size)
    values = [_finite(value) for _, value in shown]
    top = max((v for v in values if v is not None and v > 0), default=0.0)
    parts = [_open(width, len(shown) * rows.pitch + 24, title, "chart bar-h")]
    for index, ((label, _), value) in enumerate(zip(shown, values, strict=True)):
        negative = value is None or value < 0
        classes = ["bar", *(["neg"] if negative else []), *(["hl"] if label in highlight else [])]
        bar_w = 0.0 if value is None or negative or top == 0 else value / top * rows.span
        text_cls = "label hl" if "hl" in classes else "label"
        parts += [_row_label(rows, index, label, text_cls), _bar(rows, index, bar_w, classes)]
    parts.append("</svg>")
    return "".join(parts)


def step_budget(
    steps: Sequence[tuple[Decimal, Decimal]],
    *,
    budget: Decimal,
    title: str,
    width: int = 640,
    height: int = 220,
) -> str:
    """Cumulative expected impact against cumulative spend, dashed budget line (U09-20)."""
    width = _clamp(width, 200, 1600)
    height = _clamp(height, 100, 800)
    left, right, top, bottom = _PAD, width - _PAD, _PAD, height - _PAD
    points: list[tuple[float, float]] = []
    with localcontext(_DEC):
        cum_x, cum_y = _ZERO, _ZERO
        cumulative: list[tuple[Decimal, Decimal]] = []
        for effort, impact in steps[:_MAX_STEPS]:
            cum_x, cum_y = cum_x + _nonneg(effort), cum_y + _nonneg(impact)
            cumulative.append((cum_x, cum_y))
        x_max = max(_nonneg(budget), cum_x) or _ONE
        y_max = cum_y or _ONE
        points = [(float(x / x_max), float(y / y_max)) for x, y in cumulative]
        budget_x = left + float(_nonneg(budget) / x_max) * (right - left)
    parts = [
        _open(width, height, title, "chart step-budget"),
        _line("axis", (left, bottom, right, bottom), _AXIS),
        _line("axis", (left, top, left, bottom), _AXIS),
        _line("budget", (budget_x, top, budget_x, bottom), _ACCENT, dashed=True),
    ]
    if points:
        path = [f"M{_num(left)} {_num(bottom)}"]
        for fx, fy in points:
            path.append(f"H{_num(left + fx * (right - left))}")
            path.append(f"V{_num(bottom - fy * (bottom - top))}")
        parts.append(f'<path class="step" d="{" ".join(path)}" style="fill:none;stroke:{_BAR}"/>')
    parts.append("</svg>")
    return "".join(parts)


def _line(
    cls: str, coords: tuple[float, float, float, float], colour: str, *, dashed: bool = False
) -> str:
    x1, y1, x2, y2 = (_num(c) for c in coords)
    dash = ' stroke-dasharray="4 3"' if dashed else ""
    return (
        f'<line class="{cls}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"{dash} '
        f'style="stroke:{colour}"/>'
    )


def _runs(values: Sequence[float | None]) -> list[list[tuple[int, float]]]:
    """Consecutive non-null (index, value) runs, broken at None."""
    runs: list[list[tuple[int, float]]] = [[]]
    for index, value in enumerate(values):
        if value is None:
            runs.append([])
        else:
            runs[-1].append((index, value))
    return [run for run in runs if run]


def sparkline(
    values: Sequence[float | None], *, title: str, width: int = 120, height: int = 24
) -> str:
    """Trend sparkline, oldest first (U09-21); non-finite values count as gaps."""
    width = _clamp(width, 16, 1600)
    height = _clamp(height, 8, 800)
    series = [_finite(value) for value in values[-_MAX_POINTS:]]
    present = [value for value in series if value is not None]
    if not present:
        return _open(width, height, title, "empty") + "</svg>"
    pad = 2.0
    low, high = min(present), max(present)
    step = (width - 2 * pad) / (len(series) - 1) if len(series) > 1 else 0.0

    def point(index: int, value: float) -> tuple[float, float]:
        x = pad + index * step if len(series) > 1 else width / 2
        if high == low:
            return x, height / 2
        # Halved operands keep high - low finite for any pair of finite floats.
        share = (high / 2 - value / 2) / (high / 2 - low / 2)
        return x, pad + share * (height - 2 * pad)

    parts = [_open(width, height, title, "sparkline")]
    for run in _runs(series):
        coords = [point(index, value) for index, value in run]
        if len(coords) == 1:
            x, y = coords[0]
            parts.append(f'<circle cx="{_num(x)}" cy="{_num(y)}" r="1.5" style="fill:{_BAR}"/>')
        else:
            pts = " ".join(f"{_num(x)},{_num(y)}" for x, y in coords)
            parts.append(f'<polyline points="{pts}" style="fill:none;stroke:{_BAR}"/>')
    parts.append("</svg>")
    return "".join(parts)


def dot_z(
    rows: Sequence[tuple[str, float | None]],
    *,
    title: str,
    clip: float = 3.0,
    width: int = 640,
) -> str:
    """z-score dot plot (U09-22); dots beyond ±clip sit at the edge with class ``clipped``."""
    width = _clamp(width, 200, 1600)
    limit = _finite(clip)
    bound = 3.0 if limit is None else max(1.0, min(10.0, limit))
    shown = list(rows[:_MAX_DOT_ROWS])
    label_w = width * 0.38
    geo = _Rows(label_w, width - label_w - _PAD, _ROW, 8)
    height = len(shown) * geo.pitch + 24
    zero_x = label_w + geo.span / 2
    parts = [
        _open(width, height, title, "chart dot-z"),
        _line("zero", (zero_x, _PAD / 2, zero_x, height - _PAD / 2), _AXIS),
    ]
    for index, (label, z_value) in enumerate(shown):
        parts.append(_row_label(geo, index, label, "label"))
        z = _as_float(z_value)
        if z is None:
            continue
        clipped = abs(z) > bound
        cx = label_w + (max(-bound, min(bound, z)) + bound) / (2 * bound) * geo.span
        cy = _ROW / 2 + index * geo.pitch + geo.size / 2
        cls, fill = ("clipped", _ACCENT) if clipped else ("dot", _BAR)
        parts.append(
            f'<circle class="{cls}" cx="{_num(cx)}" cy="{_num(cy)}" r="4" style="fill:{fill}"/>'
        )
    parts.append("</svg>")
    return "".join(parts)
