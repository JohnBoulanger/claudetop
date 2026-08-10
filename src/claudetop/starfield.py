"""The sky: a starfield that reacts to what Claude Code is actually doing.

One StarModel owns every animated thing and is shared by two consumers:

  StarField  a full-screen widget on the bottom layer, so stars fill any part
             of the screen no widget covers
  widgets.starred_panel  paints the same sky, dimmed, through the gaps inside
             a panel — Textual cannot show a lower layer through an upper
             widget's cells, so the panels draw their own stars

What the sky is telling you:

  drift speed   live burn rate. An idle machine has a still field; several
                busy sessions pull the stars into visible motion.
  meteors       events. Green when a background job finishes, red when a
                session starts waiting on you, terracotta on a new prompt.
  comet         the 5h limit window. It crosses the sky once per window, so
                its position is how long you have until the limit resets.
  tide          5h utilization, as a glow along the bottom. It turns red
                past 90%.
"""

import math
import random
import time

GLYPHS = ["·", "·", "·", ".", "✦", "✧", "+"]
SPARKLE = "✻"
DENSITY = 48           # roughly one star per N cells
FPS = 8

METEOR_GLYPHS = {(1, 1): "╲", (-1, 1): "╱", (1, -1): "╱", (-1, -1): "╲"}
COMET_TAIL = "····"


class Meteor:
    __slots__ = ("x", "y", "dx", "dy", "color", "life", "trail")

    def __init__(self, x, y, dx, dy, color, trail=6):
        self.x, self.y = float(x), float(y)
        self.dx, self.dy = dx, dy
        self.color = color
        self.trail = trail
        self.life = 1.0

    def step(self):
        self.x += self.dx
        self.y += self.dy
        self.life -= 0.035

    def cells(self):
        """Head first, then a fading tail behind it."""
        gx = METEOR_GLYPHS.get((1 if self.dx > 0 else -1,
                                1 if self.dy > 0 else -1), "─")
        out = [(int(self.x), int(self.y), "✦", 1.0)]
        for i in range(1, self.trail):
            out.append((int(self.x - self.dx * i), int(self.y - self.dy * i),
                        gx, max(0.0, 1.0 - i / self.trail)))
        return out


class StarModel:
    """Positions, brightness and events. Rendering lives in the consumers."""

    def __init__(self, palette):
        self.p = palette
        self.w = self.h = 0
        self.stars = []
        self.meteors = []
        self.warp = 0.0          # 0 = still, 1 = busy
        self.limit_pct = None    # 5h utilization
        self.limit_frac = None   # how far through the 5h window we are
        self._last = time.time()

    # ----------------------------------------------------------- geometry

    def resize(self, w, h):
        if w == self.w and h == self.h:
            return
        self.w, self.h = max(0, w), max(0, h)
        self._rebuild()

    def _rebuild(self):
        if not self.w or not self.h:
            self.stars = []
            return
        count = max(1, (self.w * self.h) // DENSITY)
        self.stars = []
        for _ in range(count):
            special = random.random() < 0.14  # ~1 in 7 is a ✻ sparkle
            self.stars.append({
                "x": random.uniform(0, self.w),
                "y": random.randrange(self.h),
                "phase": random.uniform(0, math.tau),
                "rate": random.uniform(0.04, 0.12),
                "amp": random.uniform(0.45, 0.85),
                "depth": random.uniform(0.35, 1.0),  # parallax: near stars fly
                "glyph": SPARKLE if special else random.choice(GLYPHS),
                "special": special,
            })

    # -------------------------------------------------------------- inputs

    def set_activity(self, busy_sessions=0, burn_per_hour=0.0):
        """Warp is mostly about how many sessions are working; spend nudges it."""
        by_count = min(1.0, busy_sessions / 4.0)
        by_burn = min(1.0, (burn_per_hour or 0.0) / 60.0)
        self.warp = max(by_count, by_burn * 0.8)

    def set_limit(self, pct, elapsed_fraction):
        self.limit_pct = pct
        self.limit_frac = elapsed_fraction

    def emit(self, kind):
        """kind: 'done' | 'blocked' | 'prompt'."""
        if not self.w or not self.h:
            return
        color = {"done": self.p["green"], "blocked": self.p["red"],
                 "prompt": self.p["accent"]}.get(kind, self.p["star"])
        left_to_right = random.random() < 0.5
        dx = 1.6 if left_to_right else -1.6
        x = 0 if left_to_right else self.w - 1
        y = random.randrange(max(1, self.h // 2))
        self.meteors.append(Meteor(x, y, dx, 0.55, color))
        del self.meteors[:-6]  # never let a burst of events pile up

    # --------------------------------------------------------------- frame

    def tick(self):
        now = time.time()
        dt = min(0.5, now - self._last)
        self._last = now
        drift = self.warp * dt * 9.0     # cells/second at full warp
        for s in self.stars:
            s["phase"] += s["rate"]
            if drift:
                s["x"] -= drift * s["depth"]
                if s["x"] < 0:
                    s["x"] += self.w
        for m in self.meteors:
            m.step()
        self.meteors = [m for m in self.meteors
                        if m.life > 0 and -8 <= m.x <= self.w + 8 and m.y <= self.h]

    # -------------------------------------------------------------- render

    def _star_cells(self):
        """{(x, y): (glyph, brightness, special)} for this frame."""
        cells = {}
        for s in self.stars:
            b = (math.sin(s["phase"]) + 1.0) * 0.5 * s["amp"]
            if b < 0.30:  # most stars are dark most of the time
                continue
            cells[(int(s["x"]) % max(1, self.w), s["y"])] = (s["glyph"], b,
                                                             s["special"])
        return cells

    def _brightness_color(self, b, special, dim):
        if dim:
            # Inside a panel the sky is a texture, not a feature.
            return self.p["faint"]
        if special and b >= 0.72:
            return self.p["accent"]
        if b >= 0.68:
            return self.p["star"]
        if b >= 0.46:
            return self.p["dim"]
        return self.p["faint"]

    def frame(self, dim=False):
        """{(x, y): (glyph, color)} — everything in the sky this frame."""
        out = {}
        for (x, y), (glyph, b, special) in self._star_cells().items():
            out[(x, y)] = (glyph, self._brightness_color(b, special, dim))

        if not dim:
            for (x, y, glyph, fade) in self._tide_cells():
                out[(x, y)] = (glyph, fade)
            for (x, y, glyph, fade) in self._comet_cells():
                out[(x, y)] = (glyph, fade)
            for m in self.meteors:
                for (x, y, glyph, fade) in m.cells():
                    if 0 <= x < self.w and 0 <= y < self.h and fade > 0.2:
                        out[(x, y)] = (glyph, m.color if fade > 0.6
                                       else self.p["faint"])
        return out

    def _comet_cells(self):
        """One pass across the sky per 5h window: an ambient reset countdown."""
        if self.limit_frac is None or not self.w or self.h < 4:
            return []
        x = int(self.limit_frac * (self.w - 1))
        y = 1 + int((math.sin(self.limit_frac * math.pi) * (self.h - 4)) * 0.25)
        cells = [(x, y, "☄", self.p["star"])]
        for i, ch in enumerate(COMET_TAIL, start=1):
            cells.append((x - i, y, ch, self.p["faint"]))
        return [(cx, cy, g, c) for cx, cy, g, c in cells if 0 <= cx < self.w]

    def _tide_cells(self):
        """A glow along the bottom whose height tracks the 5h limit."""
        if not self.limit_pct or not self.w or self.h < 6:
            return []
        band = max(1, int(self.h * 0.14))
        rows = max(0, min(band, round(band * self.limit_pct / 100.0)))
        if not rows:
            return []
        color = self.p["red"] if self.limit_pct >= 90 else (
            self.p["accent"] if self.limit_pct >= 75 else self.p["green"])
        cells = []
        t = time.time()
        for r in range(rows):
            y = self.h - 1 - r
            crest = r == rows - 1
            for x in range(self.w):
                # A slow swell so the surface is never a straight line.
                wave = math.sin(x / 7.0 + t * 0.6 + r) * 0.5 + 0.5
                if crest:
                    if wave > 0.62:
                        cells.append((x, y, "▁", color))
                elif wave > 0.25:
                    cells.append((x, y, "·", color))
        return cells
