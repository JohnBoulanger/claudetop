"""Live, read-only TUI dashboard of all running Claude Code sessions.

Purely observational — it reads Claude Code's own local session-state files
and never writes to or controls any session:
  ~/.claude/sessions/*.json      -> one file per interactive `claude` process
  ~/.claude/daemon/roster.json   -> currently-live background workers
  ~/.claude/jobs/<id>/state.json -> background task state, incl. "blocked"
                                     jobs and the literal text they're
                                     waiting on ("needs")

The 'u' view adds spend and limit usage, which comes from two more places:
  ~/.claude/projects/**/*.jsonl  -> transcripts, for cost and activity (stats)
  api.anthropic.com/oauth/usage  -> the 5h / 7d subscription limits (usage)

Run:  python dashboard.py
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Footer, Static, Tree

from . import paths
from . import pricing
from . import starfield
from . import stats
from . import theme
from . import usage
from . import widgets
from . import focus as winfocus
from . import worktrees

CONFIG = paths.load_config()
PALETTE = theme.load(CONFIG)

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
ROSTER_PATH = Path.home() / ".claude" / "daemon" / "roster.json"
JOBS_DIR = Path.home() / ".claude" / "jobs"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
HOME = str(Path.home())

DONE_JOB_MAX_AGE_HOURS = 6

# session_id -> transcript path (stable once found); and path -> (mtime, parsed detail)
_PATH_CACHE = {}
_DETAIL_CACHE = {}

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BUSY_VERBS = [
    "Pondering", "Percolating", "Noodling", "Ruminating", "Cogitating",
    "Marinating", "Simmering", "Puzzling", "Deliberating", "Synthesizing",
]
STATUS_RANK = {"blocked": 0, "busy": 1, "running": 2, "idle": 3}

# Palette comes from theme.py: a preset plus any per-key overrides the user put
# in their claudetop config. Defaults to the warm espresso look.
BG = PALETTE["bg"]
PANEL = PALETTE["panel"]
TEXT = PALETTE["text"]
DIM = PALETTE["dim"]
FAINT = PALETTE["faint"]
ACCENT = PALETTE["accent"]
RED = PALETTE["red"]
GREEN = PALETTE["green"]
YELLOW = PALETTE["yellow"]
BORDER = PALETTE["border"]

TITLE_LINE = f"[{ACCENT}]✻[/] [b {TEXT}]Claude Sessions[/]"

# Subtle starfield strip. Sparse, mostly-dim stars that gently twinkle in place;
# a small fraction are Claude ✻ sparkles that occasionally warm to terracotta.
STAR_GLYPHS = ["·", "·", "·", ".", "✦", "✧", "+"]
STAR_SOFT = PALETTE["star"]  # brightest ordinary star, never pure white

CSS = f"""
/* The sky is a full-screen layer underneath everything. Any widget that
   paints an opaque background hides it, which is why the panel-based views
   draw their own dimmed stars instead (see widgets.starred_panel). */
Screen {{
    background: {BG};
    color: {TEXT};
    layers: stars content;
}}

#stars {{
    layer: stars;
    position: absolute;
    offset: 0 0;
    width: 100%;
    height: 100%;
    background: {BG};
    color: {DIM};
}}

#banner, #subtitle, #limit-strip, #home, #analytics, #map,
#table, #tree, #wt-table, Footer {{
    layer: content;
}}

#banner {{
    padding: 1 2 0 2;
    background: {BG};
}}

#subtitle {{
    color: {DIM};
    padding: 0 2 0 2;
    background: {BG};
}}

#limit-strip {{
    padding: 0 2 1 2;
    background: {BG};
    height: auto;
}}

DataTable, Tree {{
    background: {BG};
    color: {TEXT};
    border: round {BORDER};
    margin: 0 1 1 1;
    height: auto;       /* grow with the number of sessions */
    max-height: 75%;    /* beyond this the table scrolls, art keeps its floor */
}}

#home, #map {{
    background: {BG};
    color: {TEXT};
    height: 1fr;
}}

#analytics {{
    background: {BG};
    color: {TEXT};
    margin: 0 1 1 1;
    height: 1fr;        /* the panels scroll inside this */
    scrollbar-size: 1 1;
}}

DataTable > .datatable--header {{
    background: {PANEL};
    color: {DIM};
    text-style: bold;
}}

DataTable > .datatable--cursor {{
    background: {ACCENT} 25%;
    color: {TEXT};
}}

Tree > .tree--cursor {{
    background: {ACCENT} 25%;
}}

Tree > .tree--guides {{
    color: {BORDER};
}}

Tree > .tree--guides-hover {{
    color: {BORDER};
}}

Footer {{
    background: {PANEL};
    color: {DIM};
}}

Footer > .footer--key {{
    color: {ACCENT};
    text-style: bold;
}}
"""


def repo_folder(cwd):
    """Just the final path segment — i.e. the repo/working folder name."""
    if not cwd:
        return "?"
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    return parts[-1] if parts and parts[-1] else "?"


def fmt_uptime(started_at_ms):
    if not started_at_ms:
        return "?"
    secs = max(0, int(time.time() - started_at_ms / 1000))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def load_interactive_sessions():
    rows = []
    if not SESSIONS_DIR.exists():
        return rows
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        pid = data.get("pid")
        if pid is None or not psutil.pid_exists(pid):
            continue
        if data.get("kind") != "interactive":
            continue
        status = data.get("status", "idle")
        if status not in ("busy", "idle"):
            status = "blocked"
        rows.append({
            "row_key": f"i-{pid}",
            "pid": pid,
            "session_id": data.get("sessionId"),
            "kind": "interactive",
            "name": data.get("name") or "(unnamed)",
            "cwd": repo_folder(data.get("cwd", "")),
            "status": status,
            "started_at": data.get("startedAt"),
            "focusable": True,
        })
    return rows


def load_roster_workers():
    try:
        data = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    workers = {}
    for short_id, worker in data.get("workers", {}).items():
        pid = worker.get("pid")
        if pid is not None and psutil.pid_exists(pid):
            workers[short_id] = worker
    return workers


def parent_session_id(worker):
    launch_sid = worker.get("dispatch", {}).get("launch", {}).get("sessionId")
    if not launch_sid:
        return None
    return Path(launch_sid).stem


def job_age_hours(updated_at, now):
    if not updated_at:
        return None
    try:
        ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - ts).total_seconds() / 3600


def build_job_tree_data():
    """Returns (by_parent_session_id, detached_jobs, hidden_old_done_count)."""
    by_parent = {}
    detached = []
    hidden_old_done = 0
    if not JOBS_DIR.exists():
        return by_parent, detached, hidden_old_done

    workers = load_roster_workers()
    now = datetime.now(timezone.utc)

    for d in JOBS_DIR.iterdir():
        if not d.is_dir():
            continue
        state_file = d / "state.json"
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue

        state = data.get("state", "unknown")
        age = job_age_hours(data.get("updatedAt"), now)
        if state == "done" and age is not None and age > DONE_JOB_MAX_AGE_HOURS:
            hidden_old_done += 1
            continue

        worker = workers.get(d.name)
        job = {
            "daemon_short": d.name,
            "name": data.get("name") or "(background job)",
            "state": state,
            "needs": data.get("needs") or "",
            "cwd": data.get("cwd", ""),
            "session_id": data.get("sessionId"),
            "live": worker is not None,
            "pid": worker.get("pid") if worker else None,
            "started_at": worker.get("startedAt") if worker else None,
            "intent": data.get("intent") or "",
            "detail": data.get("detail") or "",
        }

        parent_sid = parent_session_id(worker) if worker else None
        if parent_sid:
            by_parent.setdefault(parent_sid, []).append(job)
        else:
            detached.append(job)

    return by_parent, detached, hidden_old_done


def _short(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_label(name, inp):
    """A compact 'what it's doing' string for a tool_use block."""
    inp = inp or {}
    if name == "Bash":
        return "$ " + _short(inp.get("command", ""), 44)
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        return f"{name} {os.path.basename(fp)}" if fp else name
    if name in ("Grep", "Glob"):
        return f"{name} {_short(inp.get('pattern', ''), 30)}"
    if name == "Task":
        return "Task: " + _short(inp.get("description", ""), 34)
    if name and name.startswith("mcp__"):
        return name.split("__")[-1]
    return name or "?"


def find_transcript(session_id):
    if not session_id:
        return None
    if session_id in _PATH_CACHE:
        p = _PATH_CACHE[session_id]
        # A cached hit can go stale only if the file is deleted; re-glob then.
        if p and os.path.exists(p):
            return p
    matches = list(PROJECTS_DIR.glob(f"**/{session_id}.jsonl"))
    path = str(max(matches, key=os.path.getmtime)) if matches else None
    _PATH_CACHE[session_id] = path
    return path


def _parse_transcript(path):
    """Scan a session transcript for activity, git branch, and token totals."""
    branch = model = None
    tin = tout = tcache_r = tcache_w = 0
    doing = title = None
    # One assistant message is often written across several lines (thinking,
    # then tool_use), and every one of those lines repeats the same usage
    # block. Counting per line would roughly double the reported cost.
    seen_ids = set()
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for ln in lines:
        try:
            o = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        t = o.get("type")
        if o.get("gitBranch"):
            branch = o["gitBranch"]
        if t in ("ai-title", "last-prompt"):
            title = o.get("aiTitle") or o.get("slug") or o.get("lastPrompt") or title
        if t != "assistant":
            continue
        msg = o.get("message") or {}
        if not isinstance(msg, dict):
            continue
        if msg.get("model"):
            model = msg["model"]
        mid = msg.get("id")
        if not mid or mid not in seen_ids:
            if mid:
                seen_ids.add(mid)
            u = msg.get("usage") or {}
            tin += u.get("input_tokens", 0) or 0
            tout += u.get("output_tokens", 0) or 0
            tcache_r += u.get("cache_read_input_tokens", 0) or 0
            tcache_w += u.get("cache_creation_input_tokens", 0) or 0
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    doing = _tool_label(b.get("name"), b.get("input"))
                elif b.get("type") == "text" and b.get("text", "").strip():
                    doing = _short(b["text"], 46)
    cost = pricing.cost(model, tin, tout, tcache_r, tcache_w)
    return {
        "branch": branch,
        "model": model,
        "tokens": tin + tout + tcache_r + tcache_w,
        "cost": cost,
        "doing": doing,
        "title": title,
    }


def session_detail(session_id):
    """Parsed transcript detail, cached by file mtime (cheap on hot refresh)."""
    path = find_transcript(session_id)
    if not path:
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _DETAIL_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    detail = _parse_transcript(path)
    _DETAIL_CACHE[path] = (mtime, detail)
    return detail


def fmt_tokens(n):
    if not n:
        return "-"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_cost(c):
    if not c:
        return "-"
    if c < 0.01:
        return "<$0.01"
    return f"${c:.2f}"


SKY = starfield.StarModel(PALETTE)


class StarField(Static):
    """The sky, on the bottom layer, filling the whole screen.

    It owns the animation clock: it is the only thing that calls SKY.tick(),
    so the panels that paint their own dimmed stars stay in step with it
    instead of running the simulation twice as fast.
    """

    FPS = starfield.FPS

    def on_mount(self):
        self.set_interval(1 / self.FPS, self._tick)

    def _tick(self):
        w, h = max(0, self.size.width), max(0, self.size.height)
        if not w or not h:
            return
        SKY.resize(w, h)
        SKY.tick()
        cells = SKY.frame()
        # The sky is sparse, so emit runs of blank cells in one append rather
        # than one per character — a full-screen frame is a few hundred
        # appends instead of several thousand.
        by_row = {}
        for (x, y), cell in cells.items():
            by_row.setdefault(y, []).append((x, cell))
        t = Text(no_wrap=True, overflow="crop")
        for y in range(h):
            x = 0
            for sx, (glyph, color) in sorted(by_row.get(y, [])):
                if sx < x or sx >= w:
                    continue
                if sx > x:
                    t.append(" " * (sx - x))
                t.append(glyph, style=color)
                x = sx + 1
            if x < w:
                t.append(" " * (w - x))
            if y < h - 1:
                t.append("\n")
        self.update(t)


class SessionDashboard(App):
    CSS = CSS
    TITLE = "Claude Session Manager"

    BINDINGS = [
        Binding("escape", "go_home", "Home", show=False),
        Binding("s", "show_sessions", "Sessions"),
        Binding("t", "toggle_view", "Tree"),
        Binding("w", "toggle_worktrees", "Worktrees"),
        Binding("a", "toggle_analytics", "Analytics"),
        Binding("m", "toggle_map", "Star map"),
        Binding("f", "focus_selected", "Focus window"),
        Binding("h", "toggle_costs", "Hide costs"),
        Binding("r", "manual_refresh", "Refresh"),
        Binding("T", "cycle_theme", "Theme"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield StarField(id="stars")
        yield Static(TITLE_LINE, id="banner")
        yield Static("", id="subtitle")
        yield Static("", id="limit-strip")
        yield Static("", id="home")
        yield DataTable(id="table", cursor_type="row")
        yield Tree("Sessions", id="tree")
        yield DataTable(id="wt-table", cursor_type="row")
        with VerticalScroll(id="analytics"):
            yield Static("", id="analytics-body")
        yield Static("", id="map")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#table", DataTable)
        table.add_columns(
            "Status", "Name", "Kind", "Branch", "Cost",
            "Uptime", "PID",
        )

        tree = self.query_one("#tree", Tree)
        tree.show_root = False
        tree.display = False

        wt_table = self.query_one("#wt-table", DataTable)
        wt_table.add_columns(
            "Ticket", "Repo", "Branch", "Clean", "Ahead/Behind",
            "PR", "Bootstrap",
        )
        self._view_mode = "home"
        self._selected_key = None
        self._wt_meta = {}
        self._tree_collapsed = set()
        self._subtitle = ""
        self._hide_costs = bool(CONFIG.get("hide_costs"))
        self._job_states = {}      # for the event meteors
        self._last_prompts = None
        self._tick = 0
        for widget_id in ("#table", "#tree", "#wt-table", "#analytics", "#map"):
            self.query_one(widget_id).display = False

        self.refresh_sessions()
        self.set_interval(0.2, self.refresh_sessions)

        # Both usage collectors run from the start: the transcript scan is the
        # slow one (a first full build takes seconds), so the 'u' view should
        # not have to wait for it, and the session table's costs come from the
        # same transcripts anyway.
        stats.start_background(
            poll_seconds=5.0,
            retention_days=int(CONFIG.get("stats_retention_days") or 31),
        )
        usage.start_background(
            poll_seconds=int(CONFIG.get("usage_poll_seconds") or 60),
            enabled=bool(CONFIG.get("usage_api", True)),
        )

    def on_data_table_row_highlighted(self, event):
        if event.row_key is not None:
            self._selected_key = event.row_key.value

    def on_data_table_row_selected(self, event):
        if event.row_key is not None:
            self._selected_key = event.row_key.value
        self._focus_selected()

    def _focus_worktree_session(self, meta):
        """Jump to the Claude session working this worktree: match the
        session's git branch to the worktree branch, else a session whose
        name mentions the ticket."""
        branch, ticket = meta.get("branch"), meta.get("ticket", "")
        sessions = [r for r in getattr(self, "_row_meta", {}).values()
                    if r.get("focusable")]
        hit = next((r for r in sessions if branch and r.get("branch") == branch),
                   None)
        if hit is None:
            hit = next((r for r in sessions
                        if ticket and ticket.lower() in r["name"].lower()), None)
        if hit:
            self._focus_window(hit["name"])
        else:
            self.notify(f"No running session on {ticket} ({branch or 'no branch'})",
                        severity="warning")

    def on_tree_node_selected(self, event):
        data = getattr(event.node, "data", None)
        if isinstance(data, dict) and data.get("name"):
            self._focus_window(data["name"])

    @staticmethod
    def _tree_key(data):
        """Stable id for a collapsible tree node, so expand/collapse state
        survives the rebuild on every refresh tick."""
        if not isinstance(data, dict):
            return None
        if data.get("tree_key"):
            return data["tree_key"]
        return f"s|{data['session_id']}" if data.get("session_id") else None

    def on_tree_node_collapsed(self, event):
        key = self._tree_key(getattr(event.node, "data", None))
        if key:
            self._tree_collapsed.add(key)

    def on_tree_node_expanded(self, event):
        key = self._tree_key(getattr(event.node, "data", None))
        if key:
            self._tree_collapsed.discard(key)

    def action_focus_selected(self):
        self._focus_selected()

    def _focus_selected(self):
        key = self._selected_key
        if key and key.startswith("wt|"):
            meta = self._wt_meta.get(key)
            if meta:
                self._focus_worktree_session(meta)
            return
        meta = getattr(self, "_row_meta", {}).get(key) if key else None
        if not meta:
            self.notify("No session selected")
            return
        self._focus_window(meta.get("name"))

    def _focus_window(self, name):
        if not winfocus.available():
            self.notify("Window focus is only wired up for Windows (pywin32) "
                        "and macOS (Terminal / iTerm2)", severity="warning")
            return
        if not name or name == "(unnamed)":
            self.notify("Session has no title to match on", severity="warning")
            return
        if winfocus.focus_session(name):
            self.notify(f"Focused window: {name}")
        else:
            self.notify(f"No window titled like “{name}” — is it in its own window?",
                        severity="warning")

    VIEW_WIDGETS = {"home": "#home", "sessions": "#table", "tree": "#tree",
                    "wt": "#wt-table", "analytics": "#analytics", "map": "#map"}

    def _show_view(self, mode):
        self._view_mode = mode
        for name, widget_id in self.VIEW_WIDGETS.items():
            w = self.query_one(widget_id)
            w.display = name == mode
            if name == mode and w.can_focus:
                w.focus()
        self.refresh_sessions()

    def _toggle(self, mode):
        """Every view key is a toggle back to home, so one key gets you out."""
        self._show_view("home" if self._view_mode == mode else mode)

    def action_go_home(self):
        self._show_view("home")

    def action_show_sessions(self):
        self._toggle("sessions")

    def action_toggle_view(self):
        self._toggle("tree")

    def action_toggle_worktrees(self):
        if self._view_mode != "wt":
            worktrees.start_background()  # idempotent; scans off the UI thread
        self._toggle("wt")

    def action_toggle_analytics(self):
        self._toggle("analytics")

    def action_toggle_map(self):
        self._toggle("map")

    def action_toggle_costs(self):
        self._hide_costs = not self._hide_costs
        self.notify("Costs hidden" if self._hide_costs else "Costs shown")

    def action_cycle_theme(self):
        name = theme.next_preset(PALETTE.get("name", theme.DEFAULT_PRESET))
        theme.save_preset(name)
        # Colors are baked into the Textual CSS at import time, so the swap
        # lands on the next launch rather than mid-frame.
        self.notify(f"Theme set to “{name}” — restart claudetop to apply")

    def refresh_sessions(self):
        self._tick += 1
        interactive_rows = load_interactive_sessions()
        by_parent, detached, hidden_old_done = build_job_tree_data()

        live_jobs = [j for jobs in by_parent.values() for j in jobs if j["live"]]
        live_jobs += [j for j in detached if j["live"]]

        bg_rows = []
        for j in live_jobs:
            status = {"blocked": "blocked", "working": "busy"}.get(j["state"], "idle")
            bg_rows.append({
                "row_key": f"b-{j['daemon_short']}",
                "pid": j["pid"],
                "kind": "background",
                "name": j["name"],
                "cwd": repo_folder(j["cwd"]),
                "status": status,
                "started_at": j["started_at"],
                "session_id": j["session_id"],
                "focusable": False,
            })

        rows = interactive_rows + bg_rows

        # Enrich every row with transcript-derived detail (repo branch + cost).
        # session_detail() is cached by file mtime, so this stays cheap on the
        # hot 0.2s refresh — it only re-parses a transcript when it changes.
        for r in rows:
            d = session_detail(r.get("session_id"))
            r["branch"] = d.get("branch") or ""
            r["cost"] = d.get("cost") or 0.0

        rows.sort(key=lambda r: (STATUS_RANK.get(r["status"], 4), r["name"].lower()))

        u = usage.snapshot()
        st = stats.snapshot()
        summary = st.get("summary")
        self._feed_sky(rows, by_parent, detached, u, summary)

        if self._view_mode == "sessions":
            self._render_table(rows)
        elif self._view_mode == "tree":
            self._render_tree(interactive_rows, by_parent, detached, hidden_old_done)
        elif self._view_mode == "wt":
            self._render_wt()
        elif self._view_mode == "home":
            # The panels move slowly, but the sky behind them animates, so the
            # home view redraws at the star clock rather than the data clock.
            self._render_home(rows, u, summary)
        elif self._view_mode == "analytics":
            if self._tick % 5 == 0:
                self._render_analytics(summary)
        elif self._view_mode == "map":
            self._render_map(interactive_rows, by_parent, detached)

        self._render_limit_strip(u, summary)

        total_needing_attention = sum(1 for r in rows if r["status"] == "blocked")
        attn = f" · [b {RED}]{total_needing_attention} need attention[/]" if total_needing_attention else ""
        if self._view_mode == "wt":
            subtitle = self._wt_subtitle + attn
        elif self._view_mode in ("home", "analytics"):
            subtitle = self._data_subtitle(st, u) + attn
        else:
            subtitle = f"{len(rows)} session{'s' if len(rows) != 1 else ''} · polling ~/.claude{attn}"
        self.query_one("#subtitle", Static).update(subtitle)
        self._row_meta = self._row_meta if hasattr(self, "_row_meta") else {}
        for r in rows:
            self._row_meta[r["row_key"]] = r

    def _render_table(self, rows):
        table = self.query_one("#table", DataTable)
        table.clear()
        self._row_meta = {}

        frame = SPINNER_FRAMES[int(time.time() * 8) % len(SPINNER_FRAMES)]

        for r in rows:
            self._row_meta[r["row_key"]] = r
            if r["status"] == "blocked":
                status_cell = f"[b {RED}]● needs input[/]"
            elif r["status"] == "busy":
                verb = BUSY_VERBS[(hash(r["row_key"]) + int(time.time() // 4)) % len(BUSY_VERBS)]
                status_cell = f"[{ACCENT}]{frame} {verb}…[/]"
            elif r["status"] == "running":
                status_cell = f"[{GREEN}]● running[/]"
            else:
                status_cell = f"[{FAINT}]○ idle[/]"

            uptime = fmt_uptime(r["started_at"])
            branch = r.get("branch") or ""
            branch_cell = f"[{DIM}]{self._truncate(branch, 20)}[/]" if branch else f"[{FAINT}]-[/]"
            cost = fmt_cost(r.get("cost"))
            cost_cell = f"[{DIM}]{cost}[/]" if cost != "-" else f"[{FAINT}]-[/]"
            table.add_row(
                status_cell, r["name"], r["kind"], branch_cell,
                cost_cell, uptime,
                str(r["pid"]) if r["pid"] else "-",
                key=r["row_key"],
            )

        if self._selected_key and self._selected_key in self._row_meta:
            for idx, key in enumerate(self._row_meta):
                if key == self._selected_key:
                    table.move_cursor(row=idx)
                    break

    def _render_wt(self):
        """The worktree board (the /wt skill's table, live). All data comes
        from worktrees.snapshot() — the collector thread does the git/gh work,
        so this render is just cache reads and stays cheap on the 0.2s tick."""
        data = worktrees.snapshot()
        rows, flags = data["rows"], data["flags"]
        problem_keys = {(p["ticket"], p["repo"]) for p in flags["problems"]}
        candidate_tickets = set(flags["ship_done_candidates"])

        table = self.query_one("#wt-table", DataTable)
        table.clear()
        self._wt_meta = {}

        for r in sorted(rows, key=lambda r: (r["ticket"], r["repo"])):
            key = f"wt|{r['ticket']}|{r['repo']}"
            self._wt_meta[key] = r

            if (r["ticket"], r["repo"]) in problem_keys:
                ticket_cell = f"[b {RED}]{r['ticket']}[/]"
            elif r["ticket"] in candidate_tickets:
                ticket_cell = f"[{GREEN}]{r['ticket']}[/]"
            else:
                ticket_cell = r["ticket"]

            if r["dirty"] is None:
                clean_cell = f"[{FAINT}]?[/]"
            elif r["dirty"]:
                clean_cell = f"[{RED}]dirty[/]"
            else:
                clean_cell = f"[{DIM}]clean[/]"

            ab = worktrees._fmt_ab(r)
            ab_cell = f"[{DIM}]{ab}[/]" if ab in ("even",) else ab

            pr = r.get("pr")
            pr_text = worktrees._fmt_pr(pr)
            if pr is None:
                pr_cell = f"[{FAINT}]loading…[/]"
            elif pr.get("none") or pr.get("error"):
                pr_cell = f"[{FAINT}]{pr_text}[/]"
            elif pr.get("state") == "MERGED":
                pr_cell = f"[{GREEN}]{pr_text}[/]"
            else:
                pr_cell = pr_text

            boot = r["bootstrap"]
            boot_cell = (f"[{RED}]missing[/]" if boot == "missing"
                         else f"[{FAINT}]{boot}[/]" if boot == "n/a"
                         else f"[{DIM}]ok[/]")

            table.add_row(
                ticket_cell, r["repo"], self._truncate(r["branch"] or "?", 28),
                clean_cell, ab_cell, pr_cell, boot_cell, key=key,
            )

        # clear() above resets the cursor to row 0, so the 0.2s tick would
        # undo every arrow keypress. Put it back on the same worktree.
        if self._selected_key and self._selected_key in self._wt_meta:
            for idx, key in enumerate(self._wt_meta):
                if key == self._selected_key:
                    table.move_cursor(row=idx)
                    break

        parts = [f"{len(rows)} worktree{'s' if len(rows) != 1 else ''}"]
        if candidate_tickets:
            parts.append(f"[{GREEN}]/ship-done ready: "
                         f"{', '.join(sorted(candidate_tickets))}[/]")
        if flags["problems"]:
            parts.append(f"[b {RED}]{len(flags['problems'])} problem"
                         f"{'s' if len(flags['problems']) != 1 else ''}[/]")
        o = data["orphans"]
        n_orphans = len(o["stray_folders"]) + len(o["stale_registrations"])
        if n_orphans:
            parts.append(f"[{RED}]{n_orphans} orphan{'s' if n_orphans != 1 else ''}[/]")
        if not rows:
            parts = ["no ticket worktrees under C:\\eva-wt (created by /ship)"]
        age = time.time() - data["scanned_at"] if data["scanned_at"] else None
        if age is not None and age < 3600:
            parts.append(f"[{FAINT}]scanned {int(age)}s ago[/]")
        self._wt_subtitle = " · ".join(parts) + " · enter focuses the ticket's session"

    # ------------------------------------------------------- sky integration

    def _feed_sky(self, rows, by_parent, detached, u, summary):
        """Tell the starfield what the machine is doing.

        Drift speed comes from how many sessions are working and how fast
        money is going out; the comet and tide come from the 5h limit; and a
        state change since the last tick throws a meteor."""
        busy = sum(1 for r in rows if r["status"] in ("busy", "blocked"))
        burn = (summary or {}).get("burn", {}).get("now", 0.0)
        SKY.set_activity(busy, burn)

        five = next((w for w in u["windows"] if w["label"].startswith("5h")), None)
        if five:
            elapsed = None
            if five["resets_in"] is not None:
                elapsed = max(0.0, min(1.0, (5 * 3600 - five["resets_in"]) / (5 * 3600)))
                # While we have the reset time, line the 5h spend window up
                # with the real billing block.
                stats.set_five_hour_start(time.time() + five["resets_in"] - 5 * 3600)
            SKY.set_limit(five["pct"], elapsed)

        jobs = [j for js in by_parent.values() for j in js] + list(detached)
        for j in jobs:
            key, state = j["daemon_short"], j["state"]
            was = self._job_states.get(key)
            if was is not None and was != state:
                if state == "done":
                    SKY.emit("done")
                elif state == "blocked":
                    SKY.emit("blocked")
            self._job_states[key] = state

        prompts = (summary or {}).get("windows", {}).get("today", {}).get("prompts")
        if prompts is not None:
            if self._last_prompts is not None and prompts > self._last_prompts:
                SKY.emit("prompt")
            self._last_prompts = prompts

    def _sky_for(self, widget):
        """The dimmed sky, in coordinates local to this widget's content."""
        region = widget.content_region
        frame = SKY.frame(dim=True)
        ox, oy = region.x, region.y
        return {(x - ox, y - oy): v for (x, y), v in frame.items()}, (0, 0)

    def _data_subtitle(self, st, u):
        parts = []
        if st.get("building"):
            done, total = st.get("done", 0), st.get("total", 0)
            parts.append(f"[{ACCENT}]building transcript cache {done}/{total}[/]")
        elif st.get("summary"):
            parts.append(f"{st['summary']['transcripts']} transcripts")
        if u["fetched_at"]:
            parts.append(f"[{FAINT}]limits {int(time.time() - u['fetched_at'])}s ago[/]")
        elif u["error"]:
            parts.append(f"[{RED}]limits unavailable[/]")
        if self._hide_costs:
            parts.append(f"[{ACCENT}]costs hidden[/]")
        return " · ".join(parts)

    # -------------------------------------------------------- shared pieces

    def _render_limit_strip(self, u, summary):
        """The one line of limits that stays visible in every view."""
        strip = self.query_one("#limit-strip", Static)
        width = max(40, strip.content_size.width or self.size.width - 4)
        if not u["windows"]:
            strip.update(Text(u["error"] or "reading usage limits…", style=FAINT))
            return

        t = Text(no_wrap=True, overflow="crop")
        for i, w in enumerate(u["windows"][:2]):
            if i:
                t.append("   ")
            t.append(f"{w['label']} ", style=DIM)
            t.append_text(widgets.gauge(w["pct"], 14, PALETTE))
            t.append(f" {w['pct']:.0f}%",
                     style=widgets.gauge_color(w["pct"], PALETTE))
            if w["resets_in"] is not None:
                t.append(f" · {widgets.duration(w['resets_in'])}", style=FAINT)
        burn = (summary or {}).get("burn")
        if burn and not self._hide_costs:
            t.append(f"   burn {widgets.money(burn['now'])}/hr", style=DIM)
            t.append(f" · 7d avg {widgets.money(burn['avg_7d'])}/hr", style=FAINT)
        pad = max(0, width - t.cell_len)
        t.append(" " * pad)
        strip.update(t)

    def _time_to_limit(self, window):
        """At the current rate, when does this window hit 100%?"""
        if not window or window["resets_in"] is None or not window["pct"]:
            return None
        span = 5 * 3600 if window["label"].startswith("5h") else 7 * 86400
        elapsed = span - window["resets_in"]
        if elapsed <= 60:
            return None
        rate = window["pct"] / elapsed           # percent per second
        if rate <= 0:
            return None
        remaining = (100.0 - window["pct"]) / rate
        return None if remaining > window["resets_in"] else remaining

    # ---------------------------------------------------------- home view

    def _limits_lines(self, u, width):
        lines = []
        if u["loading"] and not u["windows"]:
            lines.append(Text("contacting the usage API…", style=FAINT))
        elif u["error"] and not u["windows"]:
            lines.append(Text(self._truncate(u["error"], width), style=RED))
            lines.append(Text('set "usage_api": false in the config to hide this',
                              style=FAINT))
        elif u["error"]:
            # Keep showing the last good numbers, but say they are stale.
            age = widgets.duration(time.time() - u["fetched_at"]) if u["fetched_at"] else "?"
            lines.append(Text(self._truncate(f"{u['error']} · showing figures "
                                             f"from {age} ago", width), style=DIM))
        for w in u["windows"]:
            suffix = (f"resets in {widgets.duration(w['resets_in'])}"
                      if w["resets_in"] is not None else None)
            lines.append(widgets.gauge_row(w["label"], w["pct"], width, PALETTE,
                                           suffix=suffix))
            span = 5 * 3600 if w["label"].startswith("5h") else 7 * 86400
            if w["resets_in"] is None:
                continue
            elapsed = max(0.0, min(1.0, (span - w["resets_in"]) / span))
            proj = usage.projected(w, elapsed)
            note = Text(f"{'':<13}", style=DIM)
            if proj is not None:
                note.append(f"projected ~{proj:.0f}% by reset",
                            style=RED if proj >= 100 else DIM)
            hit = self._time_to_limit(w)
            if hit is not None:
                note.append(f"   hits 100% in {widgets.duration(hit)}", style=RED)
            if note.cell_len > 13:
                lines.append(note)
        if u["credits"] and u["credits"].get("used_dollars") is not None:
            lines.append(widgets.leader(
                "Extra usage credits spent",
                widgets.money(u["credits"]["used_dollars"], self._hide_costs),
                width, PALETTE, value_style=ACCENT))
        return lines

    def _spending_lines(self, s, width):
        if s is None:
            return [Text("building transcript cache…", style=FAINT)]
        hide = self._hide_costs
        w = s["windows"]
        burn = s["burn"]
        lines = [
            widgets.leader("Today", widgets.money(w["today"]["cost"], hide),
                           width, PALETTE),
            widgets.leader("Current 5h block", widgets.money(w["5h"]["cost"], hide),
                           width, PALETTE),
            widgets.leader("Last 7 days", widgets.money(w["7d"]["cost"], hide),
                           width, PALETTE),
            widgets.leader("All time",
                           f"{widgets.money(w['all']['cost'], hide)}  "
                           f"[{s['transcripts']} transcripts]",
                           width, PALETTE, value_style=DIM),
        ]
        rate = Text(no_wrap=True, overflow="crop")
        rate.append("Burn rate  ", style=TEXT)
        rate.append(f"{widgets.money(burn['now'], hide)}/hr", style=ACCENT)
        rate.append("  now vs  ", style=FAINT)
        rate.append(f"{widgets.money(burn['avg_7d'], hide)}/hr", style=DIM)
        rate.append(" average working hour this week", style=FAINT)
        lines.append(rate)

        spark = Text(no_wrap=True, overflow="crop")
        spark.append("Last 24h   ", style=TEXT)
        spark.append_text(widgets.sparkline(s["hourly_24h"], PALETTE))
        peak = max(s["hourly_24h"]) if s["hourly_24h"] else 0
        spark.append(f"  peak {widgets.money(peak, hide)}/hr", style=FAINT)
        lines.append(spark)

        days = Text(no_wrap=True, overflow="crop")
        days.append("Last 14d   ", style=TEXT)
        days.append_text(widgets.sparkline(s["daily_14d"], PALETTE, color=GREEN))
        dpeak = max(s["daily_14d"]) if s["daily_14d"] else 0
        days.append(f"  peak {widgets.money(dpeak, hide)}/day", style=FAINT)
        lines.append(days)
        return lines

    def _live_lines(self, rows, width):
        if not rows:
            return [Text("no sessions running", style=FAINT)]
        frame = SPINNER_FRAMES[int(time.time() * 8) % len(SPINNER_FRAMES)]
        lines = []
        for r in rows[:6]:
            t = Text(no_wrap=True, overflow="crop")
            if r["status"] == "blocked":
                t.append("● ", style=RED)
                state = "needs input"
                state_style = RED
            elif r["status"] == "busy":
                t.append(f"{frame} ", style=ACCENT)
                state = BUSY_VERBS[(hash(r["row_key"]) + int(time.time() // 4))
                                   % len(BUSY_VERBS)] + "…"
                state_style = ACCENT
            else:
                t.append("○ ", style=DIM)
                state = "idle"
                state_style = FAINT
            t.append(f"{self._truncate(r['name'], 24):<24} ", style=TEXT)
            t.append(f"{state:<14}", style=state_style)
            t.append(f"{self._truncate(r.get('branch') or '-', 22):<22} ", style=DIM)
            t.append(f"{widgets.money(r.get('cost'), self._hide_costs):>10}  ",
                     style=DIM)
            t.append(fmt_uptime(r.get("started_at")), style=FAINT)
            lines.append(t)
        if len(rows) > 6:
            lines.append(Text(f"+{len(rows) - 6} more — press s", style=FAINT))
        return lines

    def _render_home(self, rows, u, summary):
        home = self.query_one("#home", Static)
        width = max(48, home.content_size.width)
        height = max(8, home.content_size.height)
        sky, _ = self._sky_for(home)
        inner = width - 4

        blocks, y = [], 0

        def add(title, lines, color):
            nonlocal y
            panel = widgets.starred_panel(title, lines, width, PALETTE, sky=sky,
                                          origin=(0, y), title_color=color)
            blocks.append(panel)
            y += len(lines) + 2

        add("⚡ Limits", self._limits_lines(u, inner), ACCENT)
        add("$ Spending", self._spending_lines(summary, inner), GREEN)
        add("● Live now", self._live_lines(rows, inner), PALETTE["yellow"])

        # Whatever is left below the panels is open sky.
        out = Text(no_wrap=True, overflow="crop")
        for i, b in enumerate(blocks):
            out.append_text(b)
            if i < len(blocks) - 1:
                out.append("\n")
        for row in range(y, height):
            out.append("\n")
            out.append_text(widgets.sky_row(row, width, sky, PALETTE))
        home.update(out)

    # ------------------------------------------------------- analytics view

    def _render_analytics(self, s):
        body = self.query_one("#analytics-body", Static)
        width = max(48, body.content_size.width or self.size.width - 6)
        inner = width - 4
        sky, _ = self._sky_for(body)
        hide = self._hide_costs

        if s is None:
            body.update(widgets.starred_panel(
                "▤ Analytics", [Text("building transcript cache…", style=FAINT)],
                width, PALETTE, sky=sky))
            return

        w = s["windows"]
        col = 14
        head = Text(f"{'':<18}", style=DIM)
        for name in ("today", "7d", "30d"):
            head.append(f"{name:>{col}}", style=f"bold {DIM}")
        activity = [head]
        for label, key in (("Messages", "msgs"), ("Sessions", "sessions"),
                           ("Tool calls", "tools"), ("Prompts", "prompts"),
                           ("Files touched", "files")):
            t = Text(f"{label:<18}", style=TEXT)
            for name in ("today", "7d", "30d"):
                t.append(f"{widgets.count(w[name][key]):>{col}}", style=DIM)
            activity.append(t)
        t = Text(f"{'Tokens in/out':<18}", style=TEXT)
        for name in ("today", "7d", "30d"):
            prompt_side = w[name]["in"] + w[name]["cr"] + w[name]["cw"]
            t.append(f"{widgets.tokens(prompt_side)}/"
                     f"{widgets.tokens(w[name]['out'])}".rjust(col), style=DIM)
        activity.append(t)
        hit = s.get("cache_hit_7d")
        if hit is not None:
            activity.append(widgets.gauge_row(
                "Cache hit 7d", hit, inner, PALETTE, color=GREEN,
                suffix="of prompt tokens from cache"))

        models = [widgets.leader(
            pricing.short_model(r["model"]),
            f"{widgets.money(r['cost'], hide)}  {widgets.tokens(r['tokens'])} tok",
            inner, PALETTE) for r in s["by_model_30d"][:6] if r["cost"] >= 0.005]

        projects = [widgets.leader(
            r["project"],
            f"{widgets.money(r['cost'], hide)}  {r['sessions']} "
            f"session{'s' if r['sessions'] != 1 else ''}",
            inner, PALETTE) for r in s["by_project_30d"][:8]]

        tools = []
        top = s["by_tool_30d"][:8]
        busiest = top[0]["calls"] if top else 1
        for r in top:
            t = Text(no_wrap=True, overflow="crop")
            t.append(f"{self._truncate(r['tool'], 20):<20} ", style=TEXT)
            t.append_text(widgets.gauge(r["calls"] / busiest * 100,
                                        max(8, inner - 35), PALETTE, color=DIM))
            t.append(f" {widgets.count(r['calls']):>7} calls", style=DIM)
            tools.append(t)

        board = []
        for r in s["sessions_today"][:6]:
            label = r["title"] or r["sid"][:8]
            board.append(widgets.leader(
                f"{self._truncate(label, 34)}  [{r['project']}]",
                widgets.money(r["cost"], hide), inner, PALETTE))
        if not board:
            board = [Text("nothing yet today", style=FAINT)]

        sections = [("▤ Activity", activity, PALETTE["yellow"]),
                    ("◆ Models · 30d", models, ACCENT),
                    ("▣ Projects · 30d", projects, GREEN),
                    ("⚒ Tools · 30d", tools, PALETTE["yellow"]),
                    ("★ Today's sessions", board, ACCENT)]

        out = Text(no_wrap=True, overflow="crop")
        y = 0
        for i, (title, lines, color) in enumerate(sections):
            if not lines:
                continue
            out.append_text(widgets.starred_panel(title, lines, width, PALETTE,
                                                  sky=sky, origin=(0, y),
                                                  title_color=color))
            y += len(lines) + 2
            if i < len(sections) - 1:
                out.append("\n")
        body.update(out)

    # -------------------------------------------------------------- star map

    def _render_map(self, interactive_rows, by_parent, detached):
        """Sessions as a constellation: a bright star per session, its
        background jobs orbiting it, joined by guide lines."""
        widget = self.query_one("#map", Static)
        width = max(40, widget.content_size.width)
        height = max(10, widget.content_size.height)
        sky, _ = self._sky_for(widget)
        grid = [[None] * width for _ in range(height)]

        def put(x, y, glyph, style):
            if 0 <= x < width and 0 <= y < height:
                grid[y][x] = (glyph, style)

        def put_text(x, y, s, style):
            for i, ch in enumerate(s):
                put(x + i, y, ch, style)

        sessions = interactive_rows or []
        if not sessions:
            put_text(2, 1, "no interactive sessions running", FAINT)

        columns = max(1, min(3, (width - 4) // 34))
        cell_w = (width - 4) // columns
        # Height per constellation is driven by the busiest session, not by
        # dividing up the screen — otherwise two sessions sit marooned at the
        # top and bottom of an empty sky.
        deepest = max((len(by_parent.get(r["session_id"], [])) for r in sessions),
                      default=0)
        cell_h = min(max(4, deepest + 3), max(4, height - 2))

        for idx, r in enumerate(sessions):
            cx = 3 + (idx % columns) * cell_w
            cy = 1 + (idx // columns) * cell_h
            jobs = by_parent.get(r["session_id"], [])
            blocked = sum(1 for j in jobs if j["state"] == "blocked")
            color = RED if blocked else (ACCENT if r["status"] == "busy" else GREEN)

            put(cx, cy, "✻", f"bold {color}")
            put_text(cx + 2, cy, self._truncate(r["name"], cell_w - 6), f"bold {TEXT}")
            cost = widgets.money(r.get("cost"), self._hide_costs)
            put_text(cx + 2, cy + 1, f"{cost}  pid {r['pid']}", FAINT)

            # Jobs orbit their session, joined by a guide line.
            for k, j in enumerate(jobs[:cell_h - 2]):
                jy = cy + 2 + k
                put(cx, jy, "│" if k < len(jobs) - 1 else "╰", BORDER)
                put(cx + 1, jy, "─", BORDER)
                jcolor = {"blocked": RED, "working": ACCENT,
                          "done": FAINT}.get(j["state"], DIM)
                put(cx + 2, jy, "●" if j["live"] else "○", jcolor)
                put_text(cx + 4, jy, self._truncate(j["name"], cell_w - 10), DIM)

        if detached:
            n = len(detached)
            put_text(3, height - 2,
                     f"{n} dormant background job{'s' if n != 1 else ''}", FAINT)

        out = Text(no_wrap=True, overflow="crop")
        for y in range(height):
            for x in range(width):
                cell = grid[y][x]
                if cell:
                    out.append(cell[0], style=cell[1])
                else:
                    star = sky.get((x, y))
                    if star:
                        out.append(star[0], style=star[1])
                    else:
                        out.append(" ")
            if y < height - 1:
                out.append("\n")
        widget.update(out)
    @staticmethod
    def _truncate(s, n):
        s = " ".join(s.split())
        return s if len(s) <= n else s[: n - 1] + "…"

    def _session_label(self, r, blocked_count):
        if blocked_count or r["status"] == "blocked":
            dot, dot_color = "●", RED
        elif r["status"] == "busy":
            dot, dot_color = "●", ACCENT
        else:
            dot, dot_color = "○", DIM

        t = Text()
        t.append(f"{dot} ", style=dot_color)
        t.append(r["name"], style=f"bold {TEXT}")
        meta = ""
        if r.get("branch"):
            meta += f"   {self._truncate(r['branch'], 24)}"
        meta += f"   pid {r['pid']}"
        cost = fmt_cost(r.get("cost"))
        if cost != "-":
            meta += f"   {cost}"
        t.append(meta, style=DIM)
        if blocked_count:
            t.append(f"   {blocked_count} need input", style=f"bold {RED}")
        return t

    def _job_label(self, job):
        state = job["state"]
        if not job["live"]:
            dot, dot_color, name_color = "○", FAINT, DIM
        elif state == "blocked":
            dot, dot_color, name_color = "●", RED, TEXT
        elif state == "working":
            dot, dot_color, name_color = "●", ACCENT, TEXT
        elif state == "done":
            dot, dot_color, name_color = "○", FAINT, DIM
        else:
            dot, dot_color, name_color = "○", DIM, TEXT

        name = self._truncate(job["name"], 30).ljust(30)
        pid = (f"pid {job['pid']}" if job["pid"] else "dormant").ljust(11)

        t = Text()
        t.append(f"{dot} ", style=dot_color)
        t.append(name, style=name_color)
        t.append(f"  {pid}", style=FAINT)
        if state == "blocked" and job["needs"]:
            t.append(f"  {self._truncate(job['needs'], 44)}", style=FAINT)
        return t

    def _render_tree(self, interactive_rows, by_parent, detached, hidden_old_done):
        tree = self.query_one("#tree", Tree)
        cursor_line = tree.cursor_line
        tree.clear()

        for r in interactive_rows:
            children = by_parent.get(r["session_id"], [])
            blocked_count = sum(1 for j in children if j["state"] == "blocked")
            if children:
                key = f"s|{r['session_id']}"
                node = tree.root.add(self._session_label(r, blocked_count),
                                     data=r,
                                     expand=key not in self._tree_collapsed)
                for j in sorted(children, key=lambda j: j["state"] != "blocked"):
                    node.add_leaf(self._job_label(j), data=j)
            else:
                # No background jobs — render flat, no expand arrow, no filler leaf.
                tree.root.add_leaf(self._session_label(r, 0), data=r)

        if detached:
            det_node = tree.root.add(
                Text("dormant background jobs", style=f"bold {DIM}"),
                data={"tree_key": "detached"},
                expand="detached" not in self._tree_collapsed,
            )
            for j in sorted(detached, key=lambda j: j["state"] != "blocked"):
                det_node.add_leaf(self._job_label(j))

        if hidden_old_done:
            plural = "s" if hidden_old_done != 1 else ""
            tree.root.add_leaf(
                Text(f"+{hidden_old_done} completed job{plural} hidden "
                     f"(older than {DONE_JOB_MAX_AGE_HOURS}h)", style=FAINT)
            )

        # clear() reset the cursor; put it back or the tick eats every keypress.
        tree.cursor_line = cursor_line

    def action_manual_refresh(self):
        self.refresh_sessions()
        self.notify("Refreshed")


if __name__ == "__main__":
    SessionDashboard().run()
