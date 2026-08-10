"""Small rendering helpers shared by claudetop's panels.

Gauges and dotted-leader rows, in the style of openusage's detail panes but
drawn with this app's palette. Everything returns a Rich Text so it can be
dropped straight into a Panel.
"""

from rich.text import Text

# Eighth blocks give a gauge sub-cell resolution, so a 47% bar does not jump
# in whole characters as it fills.
PARTIALS = " ▏▎▍▌▋▊▉█"
TRACK = "░"


def gauge_color(pct, palette):
    """Green under half, then warm, then red as a limit gets close."""
    if pct is None:
        return palette["faint"]
    if pct >= 90:
        return palette["red"]
    if pct >= 75:
        return palette["accent"]
    if pct >= 50:
        return palette["yellow"]
    return palette["green"]


def gauge(pct, width, palette, color=None):
    """A [████▏░░░░] style bar, `width` cells wide, no brackets."""
    width = max(4, int(width))
    pct = 0.0 if pct is None else max(0.0, float(pct))
    frac = min(1.0, pct / 100.0)
    exact = frac * width
    full = int(exact)
    rem = exact - full

    color = color or gauge_color(pct, palette)
    t = Text()
    if full:
        t.append("█" * full, style=color)
    if full < width:
        idx = int(rem * (len(PARTIALS) - 1))
        if idx > 0:
            t.append(PARTIALS[idx], style=color)
            full += 1
    if full < width:
        t.append(TRACK * (width - full), style=palette["faint"])
    return t


def gauge_row(label, pct, width, palette, label_w=12, suffix=None, color=None):
    """`Usage 5h  ███░░░░  47.0%  <suffix>` as one line.

    Pass color to opt out of the limit-style thresholds — a high cache-hit
    ratio is good news and should not turn red."""
    color = color or gauge_color(pct, palette)
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(f"{label[:label_w]:<{label_w}} ", style=palette["text"])
    # Reserve exactly what the trailing text needs, or a long suffix pushes the
    # line past the panel and wraps it.
    reserve = label_w + 1 + 7 + (2 + len(suffix) if suffix else 0)
    bar_w = max(8, width - reserve)
    t.append_text(gauge(pct, bar_w, palette, color=color))
    t.append(f" {0.0 if pct is None else pct:>5.1f}%", style=color)
    if suffix:
        t.append(f"  {suffix}", style=palette["dim"])
    return t


def leader(label, value, width, palette, label_style=None, value_style=None,
           dot=" ·"):
    """`Today Cost · · · · · · · · $100.81` — label left, value right, dotted
    leader between. The dot pattern repeats to fill whatever is left."""
    label = str(label)
    value = str(value)
    gap = max(1, width - len(label) - len(value) - 2)
    fill = (dot * ((gap // len(dot)) + 1))[:gap]
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(label, style=label_style or palette["text"])
    t.append(" " + fill + " ", style=palette["faint"])
    t.append(value, style=value_style or palette["text"])
    return t


def kv_line(pairs, palette, sep="  ·  "):
    """`msgs 22.4k · sess 82 · tools 6.0k` — a compact metric strip."""
    t = Text()
    for i, (k, v) in enumerate(pairs):
        if i:
            t.append(sep, style=palette["faint"])
        t.append(f"{k} ", style=palette["dim"])
        t.append(str(v), style=palette["text"])
    return t


# ------------------------------------------------------------------ format

def money(v, hide=False):
    if hide:
        return "—"
    if v is None:
        return "-"
    if v >= 10000:
        return f"${v:,.0f}"
    if v < 0.01:
        return "<$0.01"
    return f"${v:,.2f}"


def tokens(n):
    if not n:
        return "-"
    if n >= 1_000_000_000:
        return f"{n / 1e9:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}k"
    return str(int(n))


def count(n):
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}k"
    return str(int(n))


def duration(seconds):
    """1h27m / 6d 6h / 45s — for reset countdowns."""
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m"
    return f"{sec}s"
