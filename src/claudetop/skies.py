"""Ambient background packs — the animated layer behind every view.

Every pack implements the same tiny contract, so swapping the look never
changes what the dashboard means:

    resize(w, h)                 the area to fill
    set_activity(busy, burn)     how hard the machine is working -> warp 0..1
    set_limit(pct)               5h utilisation, for packs that show a level
    set_model(model)             which model is burning most, for tinting
    set_context(projects, names) optional extras (repo spend, session names)
    emit(kind)                   an event: 'done' | 'blocked' | 'prompt'
    tick()                       advance one frame
    frame(dim=False)             {(x, y): (glyph, colour)} to paint

Because the contract is data-in / cells-out, a teammate can pick a pack in
Customize without any of the numbers changing meaning. `dim` is what the
panels pass when they paint the sky through the gaps in their own text: it
tones the ambient texture down but leaves event and level cues alone, since
the panels cover most of the screen and those cues would otherwise never show.
"""

import math
import random
import time

# Motion budget. Someone on a slow SSH link or with a low tolerance for
# movement should be able to turn this down without losing the information.
MOTION = {
    "off":    {"fps": 0,  "density": 0.6, "speed": 0.0},
    "calm":   {"fps": 3,  "density": 0.7, "speed": 0.45},
    "normal": {"fps": 8,  "density": 1.0, "speed": 1.0},
    "lively": {"fps": 12, "density": 1.4, "speed": 1.5},
}
DEFAULT_MOTION = "normal"

METEOR_GLYPHS = {(1, 1): "╲", (-1, 1): "╱", (1, -1): "╱", (-1, -1): "╲"}

# Rough model families -> palette key, for the optional tint.
MODEL_TINTS = (
    ("opus", "accent"), ("fable", "red"), ("sonnet", "star"),
    ("haiku", "green"), ("mythos", "yellow"),
)


class Meteor:
    """A streak across the sky. Every pack gets these — they are the event
    channel, and events matter more than decoration."""

    __slots__ = ("x", "y", "dx", "dy", "color", "life", "trail")

    def __init__(self, x, y, dx, dy, color, trail=6):
        self.x, self.y = float(x), float(y)
        self.dx, self.dy = dx, dy
        self.color = color
        self.trail = trail
        self.life = 1.0

    def step(self, speed=1.0):
        self.x += self.dx * speed
        self.y += self.dy * speed
        self.life -= 0.035 * max(0.35, speed)

    def cells(self):
        gx = METEOR_GLYPHS.get((1 if self.dx > 0 else -1,
                                1 if self.dy > 0 else -1), "─")
        out = [(int(self.x), int(self.y), "✦", 1.0)]
        for i in range(1, self.trail):
            out.append((int(self.x - self.dx * i), int(self.y - self.dy * i),
                        gx, max(0.0, 1.0 - i / self.trail)))
        return out


class Sky:
    """Shared state and the event layer. Packs override _own_cells()."""

    name = "sky"
    label = "Sky"

    def __init__(self, palette, motion=DEFAULT_MOTION, model_tint=True):
        self.p = palette
        self.w = self.h = 0
        self.warp = 0.0
        self.limit_pct = None
        self.model = None
        self.projects = []
        self.session_names = []
        self.meteors = []
        self.model_tint = model_tint
        self.set_motion(motion)
        self._last = time.time()

    # ------------------------------------------------------------- inputs

    def set_motion(self, motion):
        self.motion = motion if motion in MOTION else DEFAULT_MOTION
        cfg = MOTION[self.motion]
        self.fps = cfg["fps"]
        self.density_scale = cfg["density"]
        self.speed = cfg["speed"]

    def resize(self, w, h):
        if w == self.w and h == self.h:
            return
        self.w, self.h = max(0, w), max(0, h)
        self.rebuild()

    def rebuild(self):
        pass

    def set_activity(self, busy_sessions=0, burn_per_hour=0.0):
        by_count = min(1.0, busy_sessions / 4.0)
        by_burn = min(1.0, (burn_per_hour or 0.0) / 60.0)
        self.warp = max(by_count, by_burn * 0.8)

    def set_limit(self, pct):
        self.limit_pct = pct

    def set_model(self, model):
        self.model = model

    def set_context(self, projects=None, session_names=None):
        if projects is not None:
            self.projects = projects
        if session_names is not None:
            self.session_names = session_names

    # -------------------------------------------------------------- colour

    def accent(self):
        """The pack's working colour, tinted by the busiest model when asked."""
        if self.model_tint and self.model:
            low = str(self.model).lower()
            for needle, key in MODEL_TINTS:
                if needle in low:
                    return self.p[key]
        return self.p["accent"]

    def level_color(self):
        """Green / warm / red by how close the 5h limit is."""
        pct = self.limit_pct or 0
        if pct >= 90:
            return self.p["red"]
        if pct >= 75:
            return self.p["accent"]
        return self.p["green"]

    # -------------------------------------------------------------- events

    def emit(self, kind):
        if not self.w or not self.h:
            return
        color = {"done": self.p["green"], "blocked": self.p["red"],
                 "prompt": self.p["accent"]}.get(kind, self.p["star"])
        left_to_right = random.random() < 0.5
        dx = 1.6 if left_to_right else -1.6
        self.meteors.append(Meteor(0 if left_to_right else self.w - 1,
                                   random.randrange(max(1, self.h // 2)),
                                   dx, 0.55, color))
        del self.meteors[:-6]

    # --------------------------------------------------------------- frame

    def dt(self):
        now = time.time()
        gap = min(0.5, now - self._last)
        self._last = now
        return gap

    def tick(self):
        delta = self.dt()
        self._advance(delta)
        for m in self.meteors:
            m.step(self.speed or 1.0)
        self.meteors = [m for m in self.meteors
                        if m.life > 0 and -8 <= m.x <= self.w + 8
                        and m.y <= self.h]

    def _advance(self, delta):
        pass

    def _own_cells(self, dim):
        return {}

    def frame(self, dim=False):
        out = dict(self._own_cells(dim))
        for m in self.meteors:
            for (x, y, glyph, fade) in m.cells():
                if 0 <= x < self.w and 0 <= y < self.h and fade > 0.2:
                    out[(x, y)] = (glyph, m.color if fade > 0.6
                                   else self.p["faint"])
        return out


# ------------------------------------------------------------------ packs

STAR_GLYPHS = ["·", "·", "·", ".", "✦", "✧", "+"]
SPARKLE = "✻"
STAR_DENSITY = 48
BUSY_DENSITY_MULT = 2.6


class Stars(Sky):
    """The original: a quiet field that thickens and drifts as you work."""

    name, label = "stars", "Stars"

    def rebuild(self):
        if not self.w or not self.h:
            self.stars, self.base_count = [], 0
            return
        per_cell = STAR_DENSITY / max(0.2, self.density_scale)
        self.base_count = max(1, int((self.w * self.h) / per_cell))
        self.stars = [{
            "x": random.uniform(0, self.w),
            "y": random.randrange(self.h),
            "phase": random.uniform(0, math.tau),
            "rate": random.uniform(0.04, 0.12),
            "amp": random.uniform(0.45, 0.85),
            "depth": random.uniform(0.35, 1.0),
            "glyph": SPARKLE if random.random() < 0.14
                     else random.choice(STAR_GLYPHS),
            "special": random.random() < 0.14,
        } for _ in range(int(self.base_count * BUSY_DENSITY_MULT))]

    def set_motion(self, motion):
        super().set_motion(motion)
        self.rebuild()

    def _advance(self, delta):
        drift = self.warp * delta * 9.0 * self.speed
        for s in self.stars:
            s["phase"] += s["rate"] * (self.speed or 0.3)
            if drift:
                s["x"] -= drift * s["depth"]
                if s["x"] < 0:
                    s["x"] += self.w
    def visible_count(self):
        extra = len(self.stars) - self.base_count
        return self.base_count + int(extra * self.warp)

    def _own_cells(self, dim):
        out = {}
        tint = self.accent()
        for s in self.stars[:self.visible_count()]:
            b = (math.sin(s["phase"]) + 1.0) * 0.5 * s["amp"]
            if b < 0.30:
                continue
            if dim:
                color = self.p["faint"]
            elif s["special"] and b >= 0.72:
                color = tint
            elif b >= 0.68:
                color = self.p["star"]
            elif b >= 0.46:
                color = self.p["dim"]
            else:
                color = self.p["faint"]
            out[(int(s["x"]) % max(1, self.w), s["y"])] = (s["glyph"], color)
        return out


class Rain(Sky):
    """Drops fall harder the more you spend."""

    name, label = "rain", "Rain"
    GLYPHS = "│┃╎╷"

    def rebuild(self):
        self.drops = []

    def _target(self):
        area = max(1, self.w * self.h)
        return int(area / 55 * self.density_scale * (0.25 + self.warp))

    def _advance(self, delta):
        if not self.w or not self.h:
            return
        while len(self.drops) < self._target():
            self.drops.append([random.uniform(0, self.w),
                               random.uniform(-self.h, self.h),
                               random.uniform(0.6, 1.0)])
        del self.drops[self._target():]
        fall = delta * 26.0 * self.speed
        for d in self.drops:
            d[1] += fall * d[2]
            if d[1] >= self.h:
                d[1] = -random.uniform(0, 4)
                d[0] = random.uniform(0, self.w)

    def _own_cells(self, dim):
        out = {}
        tint = self.accent()
        for x, y, speed in self.drops:
            iy = int(y)
            if not (0 <= iy < self.h):
                continue
            fast = speed > 0.85
            color = self.p["faint"] if dim else (tint if fast else self.p["dim"])
            out[(int(x) % max(1, self.w), iy)] = (
                self.GLYPHS[0] if fast else self.GLYPHS[3], color)
        return out


class Embers(Sky):
    """Sparks rise off a floor line; the fire burns with your spend."""

    name, label = "embers", "Embers"
    GLYPHS = "·˙°*"

    def rebuild(self):
        self.sparks = []

    def _target(self):
        return int(self.w / 3 * self.density_scale * (0.2 + self.warp))

    def _advance(self, delta):
        if not self.w or not self.h:
            return
        while len(self.sparks) < self._target():
            self.sparks.append([random.uniform(0, self.w), float(self.h - 1),
                                random.uniform(0.5, 1.4),
                                random.uniform(-0.4, 0.4)])
        del self.sparks[self._target():]
        for s in self.sparks:
            s[1] -= delta * 7.0 * s[2] * self.speed
            s[0] += delta * s[3] * 4.0 * self.speed
            if s[1] < 0 or not (0 <= s[0] < self.w):
                s[0], s[1] = random.uniform(0, self.w), float(self.h - 1)

    def _own_cells(self, dim):
        out = {}
        tint = self.accent()
        for x, y, rise, _ in self.sparks:
            iy, ix = int(y), int(x)
            if not (0 <= iy < self.h and 0 <= ix < self.w):
                continue
            height = 1.0 - (iy / max(1, self.h - 1))
            if dim:
                color = self.p["faint"]
            elif height < 0.25:
                color = tint
            elif height < 0.6:
                color = self.p["red"] if rise > 1.0 else self.p["dim"]
            else:
                color = self.p["faint"]
            out[(ix, iy)] = (self.GLYPHS[min(3, int(height * 4))], color)
        return out


class Ocean(Sky):
    """A waterline that rises with the 5h limit; chop tracks your pace."""

    name, label = "ocean", "Ocean"

    def _advance(self, delta):
        self.phase = getattr(self, "phase", 0.0) + delta * (0.6 + self.warp * 2.4) * self.speed

    def _own_cells(self, dim):
        if not self.w or self.h < 6:
            return {}
        pct = self.limit_pct or 0
        depth = max(1, int((self.h - 2) * min(1.0, pct / 100.0)))
        surface = self.h - depth
        color = self.p["faint"] if dim else self.level_color()
        phase = getattr(self, "phase", 0.0)
        out = {}
        for x in range(self.w):
            swell = math.sin(x / 6.0 + phase) + math.sin(x / 11.0 - phase * 0.7)
            top = surface + int(round(swell * 0.8))
            for y in range(max(0, top), self.h):
                if y == max(0, top):
                    out[(x, y)] = ("▁", color)
                elif (x + y) % 3 == 0:
                    out[(x, y)] = ("·", self.p["faint"])
        return out


class City(Sky):
    """A skyline of your repos: tallest building is where the money went."""

    name, label = "city", "City"

    def _advance(self, delta):
        self.blink = getattr(self, "blink", 0.0) + delta * (0.4 + self.warp * 2.0) * self.speed

    def _own_cells(self, dim):
        if not self.w or self.h < 8 or not self.projects:
            return {}
        top_cost = max((p.get("cost") or 0) for p in self.projects) or 1
        out = {}
        span = max(6, min(14, self.w // max(1, len(self.projects))))
        x = 0
        lit_bias = 0.15 + self.warp * 0.5
        blink = getattr(self, "blink", 0.0)
        for i, proj in enumerate(self.projects):
            if x >= self.w:
                break
            height = max(2, int((self.h - 3) * ((proj.get("cost") or 0) / top_cost)))
            width = max(3, span - 2)
            for bx in range(x, min(self.w, x + width)):
                for by in range(self.h - height, self.h):
                    edge = by == self.h - height
                    if dim:
                        color = self.p["faint"]
                    elif edge:
                        color = self.p["border"]
                    else:
                        # Windows light up as the machine gets busier.
                        seed = math.sin(bx * 2.7 + by * 1.3 + i + blink)
                        color = (self.accent() if seed > 1 - lit_bias
                                 else self.p["faint"])
                    glyph = "▔" if edge else ("▪" if not dim and
                                              (bx + by) % 2 == 0 else " ")
                    if glyph != " ":
                        out[(bx, by)] = (glyph, color)
            x += span
        return out


class Matrix(Sky):
    """One falling column per working session, spelled from its own name."""

    name, label = "matrix", "Matrix"
    POOL = "01≡≒∷⋮⋰⋱░▒┆┊"

    def rebuild(self):
        self.columns = []

    def _advance(self, delta):
        if not self.w or not self.h:
            return
        want = max(2, int((self.w / 5) * self.density_scale * (0.2 + self.warp)))
        while len(self.columns) < want:
            self.columns.append({
                "x": random.randrange(self.w),
                "y": random.uniform(-self.h, 0),
                "len": random.randint(3, max(4, self.h // 3)),
                "speed": random.uniform(0.6, 1.6),
            })
        del self.columns[want:]
        for c in self.columns:
            c["y"] += delta * 12.0 * c["speed"] * self.speed
            if c["y"] - c["len"] > self.h:
                c["y"] = random.uniform(-self.h / 2, 0)
                c["x"] = random.randrange(self.w)

    def _glyph_for(self, col, row):
        names = "".join(self.session_names) or ""
        if names and (col + row) % 3 == 0:
            return names[(col * 7 + row) % len(names)]
        return self.POOL[(col * 3 + row) % len(self.POOL)]

    def _own_cells(self, dim):
        out = {}
        tint = self.accent()
        for c in self.columns:
            head = int(c["y"])
            for i in range(c["len"]):
                y = head - i
                if not (0 <= y < self.h):
                    continue
                if dim:
                    color = self.p["faint"]
                elif i == 0:
                    color = tint
                elif i < 3:
                    color = self.p["dim"]
                else:
                    color = self.p["faint"]
                out[(c["x"], y)] = (self._glyph_for(c["x"], y), color)
        return out


class Off(Sky):
    """Nothing at all, for people who want a still screen."""

    name, label = "off", "Off"

    def emit(self, kind):
        pass

    def frame(self, dim=False):
        return {}


PACKS = {p.name: p for p in (Stars, Rain, Embers, Ocean, City, Matrix, Off)}
DEFAULT_PACK = "stars"


def build(name, palette, motion=DEFAULT_MOTION, model_tint=True):
    return PACKS.get(name, PACKS[DEFAULT_PACK])(palette, motion=motion,
                                                model_tint=model_tint)


def pack_names():
    return list(PACKS)


def motion_names():
    return list(MOTION)
