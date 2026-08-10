"""Cross-platform config/cache locations and the user config file.

claudetop is meant to be shared with teammates on Windows and macOS, so every
path the app writes to is resolved here rather than hardcoded at the call site.

  config   Windows  %APPDATA%\\claudetop
           macOS    ~/Library/Application Support/claudetop
           Linux    $XDG_CONFIG_HOME/claudetop (or ~/.config/claudetop)
  cache    Windows  %LOCALAPPDATA%\\claudetop\\cache
           macOS    ~/Library/Caches/claudetop
           Linux    $XDG_CACHE_HOME/claudetop (or ~/.cache/claudetop)

Both can be overridden with CLAUDETOP_CONFIG_DIR / CLAUDETOP_CACHE_DIR.
"""

import json
import os
import sys
from pathlib import Path

APP = "claudetop"

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def _env_dir(var):
    v = os.environ.get(var, "").strip()
    return Path(v) if v else None


def config_dir() -> Path:
    override = _env_dir("CLAUDETOP_CONFIG_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP


def cache_dir() -> Path:
    override = _env_dir("CLAUDETOP_CACHE_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / APP


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


CONFIG_FILE = "config.json"

# Everything a teammate might reasonably need to change on their own machine.
# Anything EVA-specific lives here so the rest of the code stays generic.
DEFAULT_CONFIG = {
    "theme": {},                  # color overrides, see theme.py
    "worktree_root": None,        # e.g. "C:/eva-wt"; null disables the 'w' view
    "worktree_base_dir": None,    # base clones dir, defaults to ~/turing-analytics
    "worktree_org": None,         # GitHub org for `gh pr` lookups
    "usage_api": True,            # poll api.anthropic.com/api/oauth/usage
    "usage_poll_seconds": 60,
    "stats_retention_days": 31,   # how long per-message detail is kept in cache
    "hide_costs": False,
    # Everything under Customize (ctrl+p). None means "use the built-in".
    "sky": "stars",               # stars rain embers ocean city matrix off
    "motion": "normal",           # off calm normal lively
    "model_tint": True,           # colour the sky by the busiest model
    "gauges": "bar",              # bar blocks dial trend
    "layout": "full",             # full compact minimal charts
    "panels": None,               # explicit panel order, overrides layout
    "session_colors": "hash",     # hash status off
    "weather": True,              # the one-line forecast under the gauges
    "idle_screensaver_minutes": 0,  # 0 disables
    "session_sparklines": True,   # 24h trace in the sessions view
}


def load_config() -> dict:
    """User config merged over the defaults. Never raises — a broken config
    file falls back to defaults so the dashboard always starts."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy of literals
    path = config_dir() / CONFIG_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return cfg
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k == "theme" and isinstance(v, dict):
                cfg["theme"].update(v)
            else:
                cfg[k] = v
    return cfg


def update_config(**changes) -> Path:
    """Merge changes into the config file, leaving everything else alone.

    A key whose value is a dict is merged one level deep, so writing a single
    theme color does not wipe the rest of the theme block."""
    path = ensure(config_dir()) / CONFIG_FILE
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        cfg = {}
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    try:
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass
    return path


def write_default_config() -> Path:
    """Create config.json with the defaults if it does not exist yet."""
    path = ensure(config_dir()) / CONFIG_FILE
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return path
