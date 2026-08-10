"""claudetop's color palette, with per-user overrides.

One palette drives every widget. Pick a preset and/or override individual
colors in the claudetop config file:

    {
      "theme": { "preset": "espresso", "accent": "#da7756", "bg": "#000000" }
    }

Any key in PRESETS["espresso"] can be overridden. Unknown keys are ignored, so
a config written for a newer version still loads.
"""

from . import paths

# Claude Desktop dark palette — warm espresso, off-white, restrained terracotta.
ESPRESSO = {
    "bg": "#000000",        # main background
    "panel": "#1a1a19",     # elevated surfaces (header row, footer, panel fill)
    "text": "#f0eee6",      # primary text (warm bone)
    "dim": "#8a8578",       # secondary text
    "faint": "#5c584f",     # tertiary text (guides, tracks, truncated detail)
    "accent": "#da7756",    # terracotta — working / brand
    "red": "#e0685c",       # needs-input / over limit
    "green": "#8fae9c",     # live / healthy
    "yellow": "#d9b26f",    # caution
    "border": "#413d38",    # warm border
    "star": "#b8b2a4",      # brightest ordinary starfield star
}

PRESETS = {
    # Named for what you see, not for a mood: the default is a true-black
    # background with Claude's terracotta accent.
    "black": ESPRESSO,
    # The original warm near-black, before the switch to true black.
    "warm": {**ESPRESSO, "bg": "#262624", "panel": "#30302e"},
    # Cooler, higher contrast — closer to openusage's Deep Space.
    "midnight": {
        "bg": "#0C0E16", "panel": "#161928", "text": "#E4E6F0",
        "dim": "#B0B4C8", "faint": "#5C6180", "accent": "#7EB8F7",
        "red": "#F06A7A", "green": "#59D4A0", "yellow": "#F0C75E",
        "border": "#2A2F47", "star": "#A899F0",
    },
    # Warm terminal classic — the look of the openusage screenshots.
    "gruvbox": {
        "bg": "#0d0d0d", "panel": "#282828", "text": "#ebdbb2",
        "dim": "#a89984", "faint": "#665c54", "accent": "#fabd2f",
        "red": "#fb4934", "green": "#b8bb26", "yellow": "#d79921",
        "border": "#504945", "star": "#bdae93",
    },
}

DEFAULT_PRESET = "black"

# Older configs used the previous names; keep them working silently.
ALIASES = {"espresso": "black", "espresso-warm": "warm"}


def resolve(name):
    name = str(name or DEFAULT_PRESET)
    name = ALIASES.get(name, name)
    return name if name in PRESETS else DEFAULT_PRESET


def load(cfg=None):
    """Resolve the active palette: preset, then per-key overrides."""
    cfg = cfg if cfg is not None else paths.load_config()
    over = dict(cfg.get("theme") or {})
    name = resolve(over.pop("preset", DEFAULT_PRESET))
    palette = dict(PRESETS[name])
    for k, v in over.items():
        if k in palette and isinstance(v, str) and v.strip():
            palette[k] = v.strip()
    palette["name"] = name
    return palette


def preset_names():
    return list(PRESETS)


def next_preset(current):
    names = preset_names()
    try:
        i = names.index(resolve(current))
    except ValueError:
        i = -1
    return names[(i + 1) % len(names)]


def save_preset(name):
    """Persist a preset choice without disturbing the rest of the config."""
    paths.update_config(theme={"preset": name})
