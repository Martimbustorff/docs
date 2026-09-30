"""Server-rendered inline SVG charts: pure functions from data to an SVG string.

Colours come from CSS classes (`s1`..`s6`, `grid`, `axis`, `bar`, ...) that `static/app.css`
maps to custom properties, so a chart follows light and dark mode and needs no inline style
(the dashboard's CSP allows none). Series after the first also get a dash pattern, so identity
never rests on colour alone. Every piece of text is escaped. The SVG has no xmlns: it is meant
to be inlined in HTML, where the namespace is implied, which keeps the static report free of URLs.

Coordinates are in a fixed viewBox; the stylesheet scales the SVG to its container.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timezone
from html import escape

XValue = float | int | date | datetime
Point = tuple[XValue, float | None]
Formatter = Callable[[float], str]

WIDTH = 440
HEIGHT = 250
MAX_SERIES = 6
MAX_POINTS = 600  # longer series are thinned; a 440-unit-wide plot cannot show more
_CHAR_W = 7.5  # rough width of one 13px label character, for layout
_LABEL_GAP = 12
_LEGEND_ROW = 18
_TOP, _RIGHT, _BOTTOM = 14, 14, 44


def _plain(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.4g}"


def _fmt(value: float) -> str:
    """Coordinates with at most one decimal, no trailing zeros."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "chart"


def _x_number(x: XValue) -> float:
    if isinstance(x, datetime):
        return (x if x.tzinfo else x.replace(tzinfo=timezone.utc)).timestamp()
    if isinstance(x, date):
        return datetime.combine(x, time(0), tzinfo=timezone.utc).timestamp()
    return float(x)


def _is_time(x: XValue) -> bool:
    return isinstance(x, date)  # datetime is a date subclass


def _clean(points: Sequence[Point]) -> list[tuple[float, float]]:
    """Finite (x, y) pairs sorted by x, with x as a number."""
    out = []
    for x, y in points:
        if y is None or x is None:
            continue
        xf, yf = _x_number(x), float(y)
        if math.isfinite(xf) and math.isfinite(yf):
            out.append((xf, yf))
    return sorted(out, key=lambda p: p[0])


def _thin(points: list[tuple[float, float]], max_points: int) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points
    stride = math.ceil(len(points) / max_points)
    thinned = points[::stride]
    return thinned if thinned[-1] == points[-1] else [*thinned, points[-1]]


def nice_ticks(lo: float, hi: float, count: int = 4, integer: bool = False) -> list[float]:
    """Round tick values (steps of 1, 2, 2.5 or 5 x 10^k; no 2.5 and at least 1 when `integer`)
    covering [lo, hi]."""
    if hi <= lo:
        pad = abs(lo) * 0.05 or 1.0
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / max(count, 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    steps = (1, 2, 5, 10) if integer else (1, 2, 2.5, 5, 10)
    step = next(m * magnitude for m in steps if m * magnitude >= raw * (1 - 1e-9))
    if integer:
        step = max(step, 1.0)
    start, end = math.floor(lo / step + 1e-9) * step, math.ceil(hi / step - 1e-9) * step
    n = int(round((end - start) / step))
    return [round(start + i * step, 10) for i in range(n + 1)]


def _time_label(span_s: float) -> Callable[[float], str]:
    if span_s > 180 * 86400:
        pattern = "%Y-%m"
    elif span_s > 2 * 86400:
        pattern = "%m-%d"
    elif span_s > 0:
        pattern = "%m-%d %H:%M"
    else:
        pattern = "%Y-%m-%d"
    return lambda v: datetime.fromtimestamp(v, timezone.utc).strftime(pattern)


class _Frame:
    """Maps data to viewBox coordinates and draws the axes, gridlines and labels."""

    def __init__(
        self,
        x_range: tuple[float, float],
        y_ticks: list[float],
        *,
        x_label: str,
        y_label: str,
        y_format: Formatter,
        x_format: Formatter,
        x_ticks: list[float],
        legend_rows: int = 0,
    ) -> None:
        self.x0, self.x1 = x_range
        if self.x1 <= self.x0:
            half = abs(self.x0) * 0.01 or 1.0
            self.x0, self.x1 = self.x0 - half, self.x1 + half
        self.y_ticks = y_ticks
        self.y0, self.y1 = y_ticks[0], y_ticks[-1]
        self.y_labels = [y_format(v) for v in y_ticks]
        self.left = 12 + _CHAR_W * max(len(label) for label in self.y_labels)
        self.top = _TOP + 16 + legend_rows * _LEGEND_ROW  # 16: the y-axis label row
        self.right, self.bottom = WIDTH - _RIGHT, HEIGHT - _BOTTOM
        self.x_label, self.y_label = x_label, y_label
        self.x_format = x_format
        self.x_ticks = self._fit_x_ticks([t for t in x_ticks if self.x0 <= t <= self.x1])

    @staticmethod
    def _anchors(n: int) -> list[str]:
        """The outer labels are anchored inward so they stay inside the chart."""
        return ["middle"] if n == 1 else ["start", *["middle"] * (n - 2), "end"]

    def _fits(self, ticks: list[float]) -> bool:
        spans = []
        for t, anchor in zip(ticks, self._anchors(len(ticks))):
            x, w = self.px(t), _CHAR_W * len(self.x_format(t))
            spans.append((x, x + w) if anchor == "start" else (x - w, x) if anchor == "end" else (x - w / 2, x + w / 2))
        return all(right[0] - left[1] >= _LABEL_GAP for left, right in zip(spans, spans[1:]))

    def _fit_x_ticks(self, ticks: list[float]) -> list[float]:
        """The densest evenly spaced subset of `ticks` whose labels do not collide."""
        for step in range(1, len(ticks)):
            if self._fits(ticks[::step]):
                return ticks[::step]
        return ticks[:1]

    def px(self, x: float) -> float:
        return self.left + (x - self.x0) / (self.x1 - self.x0) * (self.right - self.left)

    def py(self, y: float) -> float:
        return self.bottom - (y - self.y0) / (self.y1 - self.y0) * (self.bottom - self.top)

    def axes(self) -> list[str]:
        parts = ['<g class="grid">']
        parts += [f'<line x1="{_fmt(self.left)}" x2="{_fmt(self.right)}" y1="{_fmt(self.py(t))}" '
                  f'y2="{_fmt(self.py(t))}"/>' for t in self.y_ticks]  # fmt: skip
        parts.append("</g>")
        parts.append(
            f'<line class="baseline" x1="{_fmt(self.left)}" x2="{_fmt(self.right)}" '
            f'y1="{_fmt(self.bottom)}" y2="{_fmt(self.bottom)}"/>'
        )
        parts.append('<g class="axis">')
        parts += [f'<text x="{_fmt(self.left - 6)}" y="{_fmt(self.py(t) + 4)}" text-anchor="end">'
                  f"{escape(label)}</text>" for t, label in zip(self.y_ticks, self.y_labels)]  # fmt: skip
        for t, anchor in zip(self.x_ticks, self._anchors(len(self.x_ticks))):
            parts.append(
                f'<text x="{_fmt(self.px(t))}" y="{_fmt(self.bottom + 16)}" text-anchor="{anchor}">'
                f"{escape(self.x_format(t))}</text>"
            )
        parts.append(
            f'<text class="label" x="{_fmt(self.left)}" y="{_fmt(self.top - 8)}">{escape(self.y_label)}</text>'
        )
        if self.x_label:
            parts.append(
                f'<text class="label" x="{_fmt((self.left + self.right) / 2)}" y="{HEIGHT - 6}" '
                f'text-anchor="middle">{escape(self.x_label)}</text>'
            )
        parts.append("</g>")
        return parts


def _open_svg(title: str, desc: str, chart_id: str | None) -> list[str]:
    cid = escape(chart_id or "chart-" + _slug(title))
    return [
        f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f'aria-labelledby="{cid}-title {cid}-desc" preserveAspectRatio="xMidYMid meet">',
        f'<title id="{cid}-title">{escape(title)}</title>',
        f'<desc id="{cid}-desc">{escape(desc)}</desc>',
    ]


def _empty(title: str, desc: str, message: str, chart_id: str | None) -> str:
    parts = _open_svg(title, desc, chart_id)
    parts.append(f'<rect class="empty" x="1" y="1" width="{WIDTH - 2}" height="{HEIGHT - 2}" rx="6"/>')
    parts.append(
        f'<text class="axis empty-text" x="{WIDTH / 2:g}" y="{HEIGHT / 2:g}" text-anchor="middle">'
        f"{escape(message)}</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def _legend(names: list[str], left: float) -> tuple[list[str], int]:
    """Legend items laid out in rows across the plot width; returns (svg parts, rows)."""
    parts, x, row = ['<g class="legend">'], left, 0
    for i, name in enumerate(names):
        width = 30 + _CHAR_W * len(name) + 14
        if x > left and x + width > WIDTH - _RIGHT:
            x, row = left, row + 1
        y = _TOP + row * _LEGEND_ROW
        parts.append(
            f'<line class="line s{i + 1}" x1="{_fmt(x)}" x2="{_fmt(x + 24)}" y1="{_fmt(y)}" y2="{_fmt(y)}"/>'
            f'<text x="{_fmt(x + 30)}" y="{_fmt(y + 4)}">{escape(name)}</text>'
        )
        x += width
    parts.append("</g>")
    return parts, row + 1


def multi_line_chart(
    series: Mapping[str, Sequence[Point]],
    *,
    title: str,
    desc: str,
    y_label: str,
    x_label: str = "",
    y_format: Formatter = _plain,
    x_format: Formatter | None = None,
    empty_message: str = "No data yet",
    chart_id: str | None = None,
    max_points: int = MAX_POINTS,
) -> str:
    """Lines on one shared y axis. x values are dates, datetimes or numbers (not mixed).

    A one-point series is drawn as a dot. A legend is drawn when there is more than one series.
    """
    if len(series) > MAX_SERIES:
        raise ValueError(f"at most {MAX_SERIES} series per chart, got {len(series)}")
    raw_x = [x for points in series.values() for x, y in points if x is not None and y is not None]
    cleaned = {name: _thin(_clean(points), max_points) for name, points in series.items()}
    all_points = [p for points in cleaned.values() for p in points]
    if not all_points:
        return _empty(title, desc, empty_message, chart_id)
    xs, ys = [p[0] for p in all_points], [p[1] for p in all_points]
    x_lo, x_hi = min(xs), max(xs)
    is_time = _is_time(raw_x[0])
    if x_format is None:
        x_format = _time_label(x_hi - x_lo) if is_time else _plain
    legend_parts, rows = ([], 0) if len(series) < 2 else _legend(list(series), 12.0)
    frame = _Frame(
        (x_lo, x_hi), nice_ticks(min(ys), max(ys)), x_label=x_label, y_label=y_label, y_format=y_format,
        x_format=x_format, x_ticks=_even_ticks(x_lo, x_hi), legend_rows=rows,
    )  # fmt: skip
    parts = _open_svg(title, desc, chart_id) + frame.axes() + legend_parts
    for i, (name, points) in enumerate(cleaned.items()):
        cls = f"s{i + 1}"
        if len(points) > 1:
            path = " ".join(f"{'M' if j == 0 else 'L'}{_fmt(frame.px(x))},{_fmt(frame.py(y))}"
                            for j, (x, y) in enumerate(points))  # fmt: skip
            parts.append(f'<path class="line {cls}" d="{path}" vector-effect="non-scaling-stroke"/>')
        if points:
            x, y = points[-1]
            label = f"{name}: {y_format(y)} at {x_format(x)}"
            parts.append(
                f'<circle class="dot {cls}" cx="{_fmt(frame.px(x))}" cy="{_fmt(frame.py(y))}" r="4">'
                f"<title>{escape(label)}</title></circle>"
            )
    parts.append("</svg>")
    return "".join(parts)


def line_chart(
    points: Sequence[Point],
    *,
    title: str,
    desc: str,
    y_label: str,
    x_label: str = "",
    name: str = "value",
    y_format: Formatter = _plain,
    x_format: Formatter | None = None,
    empty_message: str = "No data yet",
    chart_id: str | None = None,
    max_points: int = MAX_POINTS,
) -> str:
    """One series; the title names it, so there is no legend."""
    return multi_line_chart(
        {name: points}, title=title, desc=desc, y_label=y_label, x_label=x_label, y_format=y_format,
        x_format=x_format, empty_message=empty_message, chart_id=chart_id, max_points=max_points,
    )  # fmt: skip


def _even_ticks(lo: float, hi: float, count: int = 6) -> list[float]:
    if hi <= lo:
        return [lo]
    return [lo + (hi - lo) * i / count for i in range(count + 1)]


def histogram_bins(values: Sequence[float], bins: int) -> tuple[list[float], list[int]]:
    """Equal-width bin edges over [min, max] and the count in each bin (the last bin is closed)."""
    finite = sorted(v for v in values if v is not None and math.isfinite(v))
    if not finite:
        return [], []
    lo, hi = finite[0], finite[-1]
    if hi == lo:
        return [lo - 0.5, lo + 0.5], [len(finite)]
    bins = max(1, bins)
    width = (hi - lo) / bins
    edges = [lo + width * i for i in range(bins)] + [hi]
    counts = [0] * bins
    for v in finite:
        counts[min(int((v - lo) / width), bins - 1)] += 1
    return edges, counts


def bar_histogram(
    values: Sequence[float],
    *,
    title: str,
    desc: str,
    x_label: str,
    y_label: str = "Count",
    bins: int = 12,
    x_format: Formatter = _plain,
    empty_message: str = "No data yet",
    chart_id: str | None = None,
) -> str:
    """Counts of `values` in equal-width bins, as columns rising from a zero baseline."""
    edges, counts = histogram_bins(values, bins)
    if not counts:
        return _empty(title, desc, empty_message, chart_id)
    frame = _Frame(
        (edges[0], edges[-1]), nice_ticks(0.0, float(max(counts)), integer=True), x_label=x_label, y_label=y_label,
        y_format=lambda v: f"{v:,.0f}", x_format=x_format, x_ticks=edges,
    )  # fmt: skip
    parts = _open_svg(title, desc, chart_id) + frame.axes() + ['<g class="bars">']
    for lo, hi, count in zip(edges, edges[1:], counts):
        if count == 0:
            continue
        x, w = frame.px(lo) + 1, max(frame.px(hi) - frame.px(lo) - 2, 1.0)
        top, base = frame.py(count), frame.bottom
        r = min(4.0, w / 2, base - top)
        path = (
            f"M{_fmt(x)},{_fmt(base)} V{_fmt(top + r)} Q{_fmt(x)},{_fmt(top)} {_fmt(x + r)},{_fmt(top)} "
            f"H{_fmt(x + w - r)} Q{_fmt(x + w)},{_fmt(top)} {_fmt(x + w)},{_fmt(top + r)} V{_fmt(base)} Z"
        )  # square at the baseline, 4px rounded at the data end
        label = f"{x_format(lo)} to {x_format(hi)}: {count}"
        parts.append(f'<path class="bar" d="{path}"><title>{escape(label)}</title></path>')
    parts.append("</g></svg>")
    return "".join(parts)
