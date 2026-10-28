"""Tiny inline-SVG chart helpers (no plotting dependency, no JavaScript)."""

from __future__ import annotations

from html import escape
from typing import List, Optional, Sequence, Tuple

WIDTH, HEIGHT = 640, 260
PAD_L, PAD_R, PAD_T, PAD_B = 60, 20, 20, 50
PALETTE = ("#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#17becf")


def _frame(title: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{escape(title)}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" height="{HEIGHT}" '
        f'font-family="sans-serif" font-size="11"><title>{escape(title)}</title>'
        f'<rect width="100%" height="100%" fill="#fff"/>{body}</svg>'
    )


def _axes(y_max: float, y_fmt: str) -> str:
    plot_h = HEIGHT - PAD_T - PAD_B
    parts = [f'<line x1="{PAD_L}" y1="{HEIGHT - PAD_B}" x2="{WIDTH - PAD_R}" '
             f'y2="{HEIGHT - PAD_B}" stroke="#333"/>',
             f'<line x1="{PAD_L}" y1="{PAD_T}" x2="{PAD_L}" y2="{HEIGHT - PAD_B}" stroke="#333"/>']
    for i in range(5):
        frac = i / 4
        y = HEIGHT - PAD_B - frac * plot_h
        parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{WIDTH - PAD_R}" y2="{y:.1f}" '
                     f'stroke="#eee"/>')
        parts.append(f'<text x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end">'
                     f'{escape(y_fmt.format(frac * y_max))}</text>')
    return "".join(parts)


def line_chart(title: str, x_labels: Sequence[str],
               series: Sequence[Tuple[str, Sequence[float]]], y_fmt: str = "{:.0f}",
               y_max: Optional[float] = None) -> str:
    """Categorical-x line chart. ``series`` is a list of (name, values)."""
    n = max((len(v) for _, v in series), default=0)
    top = y_max if y_max is not None else max((max(v) for _, v in series if v), default=0.0)
    top = top or 1.0
    plot_w = WIDTH - PAD_L - PAD_R
    plot_h = HEIGHT - PAD_T - PAD_B

    def xy(i: int, v: float) -> Tuple[float, float]:
        x = PAD_L + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)
        return x, HEIGHT - PAD_B - plot_h * (v / top)

    parts = [_axes(top, y_fmt)]
    step = max(1, n // 12)
    for i in range(0, n, step):
        if i < len(x_labels):
            x, _ = xy(i, 0)
            parts.append(f'<text x="{x:.1f}" y="{HEIGHT - PAD_B + 16}" text-anchor="middle">'
                         f'{escape(str(x_labels[i]))}</text>')
    for idx, (name, values) in enumerate(series):
        color = PALETTE[idx % len(PALETTE)]
        points = " ".join("{:.1f},{:.1f}".format(*xy(i, v)) for i, v in enumerate(values))
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"/>')
        if len(values) <= 30:
            for i, v in enumerate(values):
                x, y = xy(i, v)
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>')
        ly = PAD_T + 12 + 14 * idx
        parts.append(f'<rect x="{WIDTH - PAD_R - 150}" y="{ly - 8}" width="10" height="10" '
                     f'fill="{color}"/><text x="{WIDTH - PAD_R - 136}" y="{ly + 1}">'
                     f'{escape(name)}</text>')
    return _frame(title, "".join(parts))


def bar_chart(title: str, labels: Sequence[str], values: Sequence[float],
              y_fmt: str = "{:.0f}") -> str:
    top = max(values, default=0.0) or 1.0
    plot_w = WIDTH - PAD_L - PAD_R
    plot_h = HEIGHT - PAD_T - PAD_B
    n = max(len(values), 1)
    slot = plot_w / n
    parts: List[str] = [_axes(top, y_fmt)]
    for i, (label, value) in enumerate(zip(labels, values)):
        h = plot_h * value / top
        x = PAD_L + i * slot + slot * 0.15
        parts.append(f'<rect x="{x:.1f}" y="{HEIGHT - PAD_B - h:.1f}" width="{slot * 0.7:.1f}" '
                     f'height="{h:.1f}" fill="{PALETTE[0]}"/>')
        parts.append(f'<text x="{x + slot * 0.35:.1f}" y="{HEIGHT - PAD_B + 16}" '
                     f'text-anchor="middle">{escape(str(label))}</text>')
    return _frame(title, "".join(parts))
