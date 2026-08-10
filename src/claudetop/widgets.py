"""Small rendering helpers shared by claudetop's panels.

Gauges and dotted-leader rows, in the style of openusage's detail panes but
drawn with this app's palette. Everything returns a Rich Text so it can be
dropped straight into a Panel.
"""

from rich.cells import cell_len
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
    # cell_len, not len: a glyph like ⚡ or an emoji occupies two columns.
    gap = max(1, width - cell_len(label) - cell_len(value) - 2)
    fill = (dot * ((gap // len(dot)) + 1))[:gap]
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(label, style=label_style or palette["text"])
    t.append(" " + fill + " ", style=palette["faint"])
    t.append(value, style=value_style or palette["text"])
    return t


ROUND = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}


def starred_panel(title, lines, width, palette, sky=None, origin=(0, 0),
                  title_color=None):
    """A rounded panel that lets the sky show through its empty cells.

    Textual cannot show a lower layer through an upper widget's cells, so a
    panel that wants stars behind its text has to paint them itself. Every
    interior cell the content did not use is a candidate; the star comes from
    the shared sky at that absolute screen position, dimmed, so the field
    stays continuous across panel edges.

    sky is the {(x, y): (glyph, color)} dict from StarModel.frame(dim=True);
    origin is where this panel's top-left sits on screen.
    """
    width = max(20, int(width))
    inner = width - 4  # two border cells and one pad each side
    ox, oy = origin
    tcolor = title_color or palette["accent"]

    out = Text(no_wrap=True, overflow="crop")
    head = f"{ROUND['tl']}{ROUND['h']} "
    out.append(head, style=palette["border"])
    out.append(title, style=f"bold {tcolor}")
    rule = width - cell_len(head) - cell_len(title) - 1
    out.append(" " + ROUND["h"] * max(0, rule - 1) + ROUND["tr"],
               style=palette["border"])
    out.append("\n")

    for row, line in enumerate(lines, start=1):
        out.append(ROUND["v"] + " ", style=palette["border"])
        used = line.cell_len if isinstance(line, Text) else len(line)
        if isinstance(line, Text):
            out.append_text(line)
        else:
            out.append(str(line), style=palette["text"])
        # Fill the rest of the row, dropping a star wherever the sky has one.
        for col in range(used, inner):
            star = sky.get((ox + 2 + col, oy + row)) if sky else None
            if star:
                out.append(star[0], style=star[1])
            else:
                out.append(" ")
        out.append(" " + ROUND["v"], style=palette["border"])
        out.append("\n")

    out.append(ROUND["bl"] + ROUND["h"] * (width - 2) + ROUND["br"],
               style=palette["border"])
    return out


def sky_row(y, width, sky, palette, prefix=None):
    """One full-width line of sky, optionally with text laid over the left."""
    t = Text(no_wrap=True, overflow="crop")
    x = 0
    if prefix is not None:
        t.append_text(prefix)
        x = prefix.cell_len
    row = sorted((sx, cell) for (sx, sy), cell in sky.items()
                 if sy == y and x <= sx < width)
    for sx, (glyph, color) in row:
        if sx > x:
            t.append(" " * (sx - x))
        t.append(glyph, style=color)
        x = sx + 1
    if x < width:
        t.append(" " * (width - x))
    return t


def linechart(values, height, width, palette, color=None, gutter=9,
              value_fmt=None, labels=None, average=True, grid=True):
    """A continuous line chart, drawn with box-drawing glyphs.

    Each point is widened to fill the plot area, then consecutive points are
    joined: a flat run is ─, a rise or fall gets corner glyphs at both ends
    with │ down the middle, so the series reads as one unbroken stroke rather
    than a scatter of marks.

    The gutter carries value labels at the top, the midpoint and zero, each
    with a sparse dotted gridline; the mean of the non-idle points is drawn as
    a dashed rule. `labels` (one per value, blanks allowed) prints underneath.

    Returns `height` chart rows plus a label row when labels are given.
    """
    rows = []
    if not values or height < 2:
        return rows
    top = max(values) or 1.0
    fmt = value_fmt or (lambda v: f"{v:g}")
    top_tag, zero_tag = fmt(top), fmt(0)
    # The gutter has to fit its own labels, or a four-figure total pushes the
    # plot past the panel edge.
    gutter = max(gutter, len(top_tag) + 1, len(zero_tag) + 1)
    n = len(values)
    cols = max(n, width - gutter)
    color = color or palette["accent"]

    # Stretch the series across the whole plot area rather than giving each
    # point a whole column: 56 points in 100 columns should fill the panel,
    # not stop halfway.
    series = [values[min(n - 1, int(i * n / cols))] for i in range(cols)]

    def row_of(v):
        # row 0 is the top of the chart
        return height - 1 - int(round(v / top * (height - 1)))

    canvas = [[None] * cols for _ in range(height)]
    for x in range(cols):
        y0 = row_of(series[x])
        y1 = row_of(series[x + 1]) if x + 1 < cols else y0
        if y0 == y1:
            canvas[y0][x] = "─"
        elif y1 < y0:                      # rising
            canvas[y1][x] = "╭"
            canvas[y0][x] = "╯"
            for y in range(y1 + 1, y0):
                canvas[y][x] = "│"
        else:                              # falling
            canvas[y0][x] = "╮"
            canvas[y1][x] = "╰"
            for y in range(y0 + 1, y1):
                canvas[y][x] = "│"

    label_rows = {0: top_tag, height - 1: zero_tag}
    if height >= 5:
        label_rows[height // 2] = fmt(top / 2)

    live = [v for v in values if v > 0]
    mean_row = row_of(sum(live) / len(live)) if (average and live) else None

    def backdrop(y, x):
        """What sits behind the line at this cell."""
        if y == mean_row:
            return ("╌", palette["faint"]) if x % 2 == 0 else (" ", None)
        if grid and y in label_rows:
            return ("·", palette["faint"]) if x % 3 == 0 else (" ", None)
        return (" ", None)

    for y in range(height):
        line = Text(no_wrap=True, overflow="crop")
        tag = label_rows.get(y, "")
        line.append(f"{tag:>{gutter - 1}} ",
                    style=palette["dim"] if y == 0 else palette["faint"])
        x = 0
        while x < cols:
            if canvas[y][x] is None:
                glyph, style = backdrop(y, x)
                line.append(glyph, style=style)
                x += 1
                continue
            run = 0
            while x + run < cols and canvas[y][x + run] == canvas[y][x]:
                run += 1
            line.append(canvas[y][x] * run, style=color)
            x += run
        rows.append(line)

    if labels:
        row = Text(no_wrap=True, overflow="crop")
        row.append(" " * gutter)
        used = 0
        for i, lab in enumerate(labels):
            if not lab:
                continue
            # Position by fraction, and print the label whole — a tick that
            # reads "Jul" instead of "Jul 28" is worse than a sparser axis.
            start = int(i * cols / len(labels))
            if start < used:
                continue
            row.append(" " * (start - used))
            row.append(str(lab), style=palette["faint"])
            used = start + len(str(lab))
        rows.append(row)
    return rows


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
