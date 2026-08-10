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
  tide          5h utilization, as a glow along the bottom. It turns red
                past 90%.
"""

import math
import random
import time

GLYPHS = ["·", "·", "·", ".", "✦", "✧", "+"]
SPARKLE = "✻"
DENSITY = 48           # idle: roughly one star per N cells
BUSY_DENSITY_MULT = 2.6  # flat out: this many times as many stars
FPS = 8

METEOR_GLYPHS = {(1, 1): "╲", (-1, 1): "╱", (1, -1): "╱", (-1, -1): "╲"}


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
        self.base_count = 0
        self.meteors = []
        self.warp = 0.0          # 0 = still, 1 = busy
        self.limit_pct = None    # 5h utilization, drives the tide
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
            self.base_count = 0
            return
        # Build the busy-sky pool once and reveal more of it as warp rises.
        # Rebuilding on every change of pace would teleport every star.
        self.base_count = max(1, (self.w * self.h) // DENSITY)
        count = int(self.base_count * BUSY_DENSITY_MULT)
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

    def set_limit(self, pct, elapsed_fraction=None):
        self.limit_pct = pct

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

    def visible_count(self):
        """How much of the star pool is on show: the whole thing at full warp,
        the base field when the machine is idle."""
        extra = len(self.stars) - self.base_count
        return self.base_count + int(extra * self.warp)

    def _star_cells(self):
        """{(x, y): (glyph, brightness, special)} for this frame."""
        cells = {}
        for s in self.stars[:self.visible_count()]:
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
        """{(x, y): (glyph, color)} — everything in the sky this frame.

        dim only dims the *stars*. The tide and the meteors are signals
        rather than texture, and the panels cover most of the screen — if they
        were dropped here they would never be seen at all.
        """
        out = {}
        for (x, y), (glyph, b, special) in self._star_cells().items():
            out[(x, y)] = (glyph, self._brightness_color(b, special, dim))

        for (x, y, glyph, color) in self._tide_cells():
            out[(x, y)] = (glyph, color)
        for m in self.meteors:
            for (x, y, glyph, fade) in m.cells():
                if 0 <= x < self.w and 0 <= y < self.h and fade > 0.2:
                    out[(x, y)] = (glyph, m.color if fade > 0.6
                                   else self.p["faint"])
        return out

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
