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


BRAILLE_BASE = 0x2800
# Dot bit for (column-in-cell, row-in-cell). Braille numbers its dots in a
# famously odd order; this table is the whole of that oddity.
BRAILLE_BIT = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
               (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80}


def braille_chart(values, height, width, palette, color=None, gutter=10,
                  value_fmt=None, labels=None, average=True, grid=True):
    """A line chart at braille resolution: 2 dots wide, 4 tall, per cell.

    A six-row chart is therefore 24 vertical steps rather than 6, and a full
    width panel plots a couple of hundred points instead of one per column —
    which is what makes 15-minute buckets legible.

    Draws, in order: a dim gridline at each labelled level, an average line,
    then the series. Returns the chart rows plus an x-label row.
    """
    rows = []
    if not values or height < 2 or width < 20:
        return rows

    top = max(values) or 1.0
    fmt = value_fmt or (lambda v: f"{v:g}")
    gutter = max(gutter, len(fmt(top)) + 2)
    cols = max(8, width - gutter)
    dot_w, dot_h = cols * 2, height * 4
    color = color or palette["accent"]

    cells = {}                      # (row, col) -> bitmask

    def plot(dx, dy):
        if 0 <= dx < dot_w and 0 <= dy < dot_h:
            key = (dy // 4, dx // 2)
            cells[key] = cells.get(key, 0) | BRAILLE_BIT[(dx % 2, dy % 4)]

    def dot_y(v):
        return int(round((1.0 - min(1.0, max(0.0, v / top))) * (dot_h - 1)))

    # Spread the series across the full dot width and join the points, so a
    # gap between samples reads as a slope rather than two islands.
    n = len(values)
    last = None
    for dx in range(dot_w):
        idx = int(dx * n / dot_w)
        y = dot_y(values[min(n - 1, idx)])
        if last is not None and abs(y - last) > 1:
            step = 1 if y > last else -1
            for fill in range(last + step, y, step):
                plot(dx, fill)
        plot(dx, y)
        last = y

    active = [v for v in values if v > 0]
    mean_row = None
    if average and active:
        mean_row = dot_y(sum(active) / len(active)) // 4

    label_rows = {0: fmt(top), height - 1: fmt(0)}
    if height >= 5:
        label_rows[height // 2] = fmt(top / 2)

    for row in range(height):
        line = Text(no_wrap=True, overflow="crop")
        tag = label_rows.get(row, "")
        style = palette["dim"] if row == 0 else palette["faint"]
        line.append(f"{tag:>{gutter - 1}} ", style=style)
        for col in range(cols):
            bits = cells.get((row, col))
            if bits:
                line.append(chr(BRAILLE_BASE + bits), style=color)
            elif row == mean_row:
                # Dashed, not solid: the average is a reference, not data.
                line.append("╌" if col % 2 == 0 else " ", style=palette["faint"])
            elif grid and row in label_rows:
                line.append("·" if col % 3 == 0 else " ", style=palette["faint"])
            else:
                line.append(" ")
        rows.append(line)

    if labels:
        row = Text(no_wrap=True, overflow="crop")
        row.append(" " * gutter)
        used = 0
        per = cols / max(1, len(labels))
        for i, lab in enumerate(labels):
            if not lab:
                continue
            start = int(i * per)
            if start < used:
                continue
            row.append(" " * (start - used))
            row.append(str(lab), style=palette["faint"])
            used = start + len(str(lab))
        rows.append(row)
    return rows


def linechart(values, height, width, palette, color=None, gutter=9,
              value_fmt=None, labels=None):
    """A compact line chart, drawn with box-drawing glyphs.

    Each point is widened to fill the plot area, then consecutive points are
    joined: a flat run is ─, a rise or fall gets corner glyphs at both ends
    with │ down the middle. The left gutter carries the top and bottom value
    labels; `labels` (one per value, blanks allowed) prints underneath.

    Returns `height` chart rows plus a label row when labels are given.
    """
    rows = []
    if not values or height < 2:
        return rows
    top = max(values) or 1.0
    top_tag = value_fmt(top) if value_fmt else f"{top:g}"
    zero_tag = value_fmt(0) if value_fmt else "0"
    # The gutter has to fit its own labels, or a four-figure total pushes the
    # plot past the panel edge.
    gutter = max(gutter, len(top_tag) + 1, len(zero_tag) + 1)
    plot_w = max(len(values), width - gutter)
    per = max(1, plot_w // len(values))
    cols = per * len(values)
    color = color or palette["accent"]

    # Widen each point across its share of the columns, so the line reads as
    # a shape rather than a spike per cell.
    series = [values[min(len(values) - 1, i // per)] for i in range(cols)]

    def row_of(v):
        # row 0 is the top of the chart
        return height - 1 - int(round(v / top * (height - 1)))

    grid = [[None] * cols for _ in range(height)]
    for x in range(cols):
        y0 = row_of(series[x])
        y1 = row_of(series[x + 1]) if x + 1 < cols else y0
        if y0 == y1:
            grid[y0][x] = "─"
        elif y1 < y0:                      # rising
            grid[y1][x] = "╭"
            grid[y0][x] = "╯"
            for y in range(y1 + 1, y0):
                grid[y][x] = "│"
        else:                              # falling
            grid[y0][x] = "╮"
            grid[y1][x] = "╰"
            for y in range(y0 + 1, y1):
                grid[y][x] = "│"

    for y in range(height):
        line = Text(no_wrap=True, overflow="crop")
        if y == 0:
            line.append(f"{top_tag:>{gutter - 1}} ", style=palette["dim"])
        elif y == height - 1:
            line.append(f"{zero_tag:>{gutter - 1}} ", style=palette["faint"])
        else:
            line.append(" " * gutter)
        x = 0
        while x < cols:
            if grid[y][x] is None:
                run = 0
                while x + run < cols and grid[y][x + run] is None:
                    run += 1
                # A blank cell on the floor row is still the baseline.
                line.append(("·" if y == height - 1 else " ") * run,
                            style=palette["faint"])
                x += run
                continue
            run = 0
            while x + run < cols and grid[y][x + run] == grid[y][x]:
                run += 1
            line.append(grid[y][x] * run, style=color)
            x += run
        rows.append(line)

    if labels:
        row = Text(no_wrap=True, overflow="crop")
        row.append(" " * gutter)
        used = 0
        for i, lab in enumerate(labels):
            if not lab:
                continue
            start = i * per
            if start < used:
                continue
            text = str(lab)[:per * 3]
            row.append(" " * (start - used))
            row.append(text, style=palette["faint"])
            used = start + len(text)
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
