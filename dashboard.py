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
import math
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Footer, Static, Tree

import paths
import pricing
import stats
import theme
import usage
import widgets
import winfocus
import worktrees

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
Screen {{
    background: {BG};
    color: {TEXT};
}}

#banner {{
    padding: 1 2 0 2;
}}

#subtitle {{
    color: {DIM};
    padding: 0 2 1 2;
}}

DataTable, Tree {{
    background: {BG};
    color: {TEXT};
    border: round {BORDER};
    margin: 0 1 1 1;
    height: auto;       /* grow with the number of sessions */
    max-height: 75%;    /* beyond this the table scrolls, art keeps its floor */
}}

#usage {{
    background: {BG};
    color: {TEXT};
    margin: 0 1 1 1;
    height: 1fr;        /* the panels scroll inside this */
    scrollbar-size: 1 1;
}}

#stars {{
    height: 1fr;        /* soak up whatever space the table leaves */
    min-height: 4;      /* ...but never vanish entirely */
    margin: 0 1 0 1;
    background: {BG};
    color: {DIM};
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


class StarField(Static):
    """A subtle, self-contained twinkling starfield.

    Each star sits at a fixed cell and slowly breathes in/out on a sine phase.
    Most are dim and below the visibility threshold at any instant, so the field
    stays quiet — only a few points glimmer at a time. A small fraction are ✻
    sparkles that warm to terracotta at their peak. No assets, no external loop.
    """

    FPS = 8
    DENSITY = 48  # roughly one star per N cells — sparse on purpose

    def on_mount(self):
        self._w = 0
        self._h = 0
        self._stars = []
        self.set_interval(1 / self.FPS, self._tick)

    def _rebuild_stars(self):
        w, h = self._w, self._h
        count = max(1, (w * h) // self.DENSITY)
        self._stars = []
        for _ in range(count):
            special = random.random() < 0.14  # ~1 in 7 is a ✻ sparkle
            self._stars.append({
                "x": random.randrange(w),
                "y": random.randrange(h),
                "phase": random.uniform(0, math.tau),
                "rate": random.uniform(0.04, 0.12),   # slow, gentle twinkle
                "amp": random.uniform(0.45, 0.85),
                "glyph": "✻" if special else random.choice(STAR_GLYPHS),
                "special": special,
            })

    def _resize_if_needed(self):
        w = max(0, self.size.width)
        h = max(0, self.size.height)
        if w != self._w or h != self._h:
            self._w, self._h = w, h
            if w and h:
                self._rebuild_stars()

    def _build(self):
        grid = [[None] * self._w for _ in range(self._h)]
        for s in self._stars:
            s["phase"] += s["rate"]
            b = (math.sin(s["phase"]) + 1.0) * 0.5 * s["amp"]
            if b < 0.30:  # most of the time, a given star is simply dark
                continue
            if s["special"] and b >= 0.72:
                color = ACCENT
            elif b >= 0.68:
                color = STAR_SOFT
            elif b >= 0.46:
                color = DIM
            else:
                color = FAINT
            grid[s["y"]][s["x"]] = (s["glyph"], color)

        t = Text()
        for y in range(self._h):
            for x in range(self._w):
                cell = grid[y][x]
                if cell:
                    t.append(cell[0], style=cell[1])
                else:
                    t.append(" ")
            if y < self._h - 1:
                t.append("\n")
        return t

    def _tick(self):
        self._resize_if_needed()
        if not self._w or not self._h:
            return
        self.update(self._build())


class SessionDashboard(App):
    CSS = CSS
    TITLE = "Claude Session Manager"

    BINDINGS = [
        Binding("f", "focus_selected", "Focus window"),
        Binding("t", "toggle_view", "Tree view"),
        Binding("w", "toggle_worktrees", "Worktrees"),
        Binding("u", "toggle_usage", "Usage"),
        Binding("r", "manual_refresh", "Refresh"),
        Binding("T", "cycle_theme", "Theme"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(TITLE_LINE, id="banner")
        yield Static("", id="subtitle")
        yield DataTable(id="table", cursor_type="row")
        yield Tree("Sessions", id="tree")
        yield DataTable(id="wt-table", cursor_type="row")
        with VerticalScroll(id="usage"):
            yield Static("", id="usage-body")
        yield StarField(id="stars")
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
        wt_table.display = False
        self.query_one("#usage").display = False

        self._view_mode = "table"
        self._selected_key = None
        self._wt_meta = {}
        self._tree_collapsed = set()
        self._usage_subtitle = ""
        self._usage_dirty = True
        self._tick = 0
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
            self.notify("Window focus needs pywin32 (pip install pywin32)",
                        severity="warning")
            return
        if not name or name == "(unnamed)":
            self.notify("Session has no title to match on", severity="warning")
            return
        if winfocus.focus_session(name):
            self.notify(f"Focused window: {name}")
        else:
            self.notify(f"No window titled like “{name}” — is it in its own window?",
                        severity="warning")

    def _show_view(self, mode):
        self._view_mode = mode
        for widget_id, active in (("#table", mode == "table"),
                                  ("#tree", mode == "tree"),
                                  ("#wt-table", mode == "wt"),
                                  ("#usage", mode == "usage")):
            w = self.query_one(widget_id)
            w.display = active
            if active:
                w.focus()
        # The starfield is the main page's floor; the other views want the room.
        self.query_one("#stars").display = mode == "table"
        self.refresh_sessions()

    def action_toggle_view(self):
        self._show_view("tree" if self._view_mode != "tree" else "table")

    def action_toggle_worktrees(self):
        if self._view_mode != "wt":
            worktrees.start_background()  # idempotent; scans off the UI thread
            self._show_view("wt")
        else:
            self._show_view("table")

    def action_toggle_usage(self):
        self._usage_dirty = True
        self._show_view("usage" if self._view_mode != "usage" else "table")

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

        if self._view_mode == "table":
            self._render_table(rows)
        elif self._view_mode == "tree":
            self._render_tree(interactive_rows, by_parent, detached, hidden_old_done)
        elif self._view_mode == "usage":
            # Panels change slowly (the API polls once a minute), so redraw at
            # ~2fps instead of on every 0.2s session tick.
            if self._usage_dirty or self._tick % 5 == 0:
                self._render_usage()
        else:
            self._render_wt()

        total_needing_attention = sum(1 for r in rows if r["status"] == "blocked")
        attn = f" · [b {RED}]{total_needing_attention} need attention[/]" if total_needing_attention else ""
        if self._view_mode == "wt":
            subtitle = self._wt_subtitle + attn
        elif self._view_mode == "usage":
            subtitle = self._usage_subtitle + attn
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

    # ------------------------------------------------------------- usage view

    def _panel(self, title, body, color=None):
        return Panel(body, title=f"[{color or ACCENT}]{title}[/]",
                     title_align="left", box=box.ROUNDED,
                     border_style=BORDER, padding=(0, 1))

    def _limits_panel(self, u, width):
        """Subscription limit gauges, straight from the usage API."""
        lines = []
        if u["loading"]:
            lines.append(Text("contacting the usage API…", style=FAINT))
        elif u["error"] and not u["windows"]:
            lines.append(Text(u["error"], style=RED))
            lines.append(Text("set \"usage_api\": false in the config to hide this",
                              style=FAINT))
        for w in u["windows"]:
            suffix = None
            if w["resets_in"] is not None:
                suffix = f"resets in {widgets.duration(w['resets_in'])}"
            lines.append(widgets.gauge_row(w["label"], w["pct"], width, PALETTE,
                                           suffix=suffix))
            # How far into the window we are tells us where the current burn
            # rate lands by reset time.
            span = 5 * 3600 if w["label"].startswith("5h") else 7 * 86400
            if w["resets_in"] is not None:
                elapsed = max(0.0, min(1.0, (span - w["resets_in"]) / span))
                proj = usage.projected(w, elapsed)
                if proj is not None:
                    style = RED if proj >= 100 else DIM
                    lines.append(Text(f"{'':<13}projected ~{proj:.0f}% by reset",
                                      style=style))
        if u["credits"] and u["credits"].get("used_dollars") is not None:
            lines.append(Text(""))
            lines.append(widgets.leader(
                "Extra usage credits spent",
                widgets.money(u["credits"]["used_dollars"],
                              CONFIG.get("hide_costs")),
                width, PALETTE, value_style=ACCENT))
        return self._panel("⚡ Limits", Group(*lines) if lines else Text("-"))

    def _spending_panel(self, s, width):
        if s is None:
            return self._panel("$ Spending", Text("building transcript cache…",
                                                  style=FAINT))
        hide = bool(CONFIG.get("hide_costs"))
        w = s["windows"]
        rows = [
            ("Today", w["today"], "since local midnight"),
            ("Current 5h block", w["5h"], "aligned to the limit window"),
            ("Last 7 days", w["7d"], "rolling"),
            ("Last 30 days", w["30d"], "rolling"),
            ("All time", w["all"], f"{s['transcripts']} transcripts"),
        ]
        lines = []
        for label, win, note in rows:
            lines.append(widgets.leader(
                label, f"{widgets.money(win['cost'], hide)}  [{note}]",
                width, PALETTE, value_style=TEXT))
        lines.append(Text(""))
        lines.append(Text("Model cost breakdown (30d)", style=f"bold {DIM}"))
        for r in s["by_model_30d"][:6]:
            if r["cost"] < 0.005:
                continue
            lines.append(widgets.leader(
                "  " + pricing.short_model(r["model"]),
                f"{widgets.money(r['cost'], hide)}  {widgets.tokens(r['tokens'])} tok",
                width, PALETTE, label_style=DIM))
        return self._panel("$ Spending", Group(*lines), color=GREEN)

    def _activity_panel(self, s, width):
        if s is None:
            return self._panel("▤ Activity", Text("building transcript cache…",
                                                  style=FAINT))
        w = s["windows"]
        col = 14
        head = Text(f"{'':<18}", style=DIM)
        for name in ("today", "7d", "30d"):
            head.append(f"{name:>{col}}", style=f"bold {DIM}")
        lines = [head]
        metrics = [
            ("Messages", "msgs"), ("Sessions", "sessions"),
            ("Tool calls", "tools"), ("Prompts", "prompts"),
            ("Files touched", "files"),
        ]
        for label, key in metrics:
            t = Text(f"{label:<18}", style=TEXT)
            for name in ("today", "7d", "30d"):
                t.append(f"{widgets.count(w[name][key]):>{col}}", style=DIM)
            lines.append(t)
        t = Text(f"{'Tokens in/out':<18}", style=TEXT)
        for name in ("today", "7d", "30d"):
            prompt_side = w[name]["in"] + w[name]["cr"] + w[name]["cw"]
            cell = f"{widgets.tokens(prompt_side)}/{widgets.tokens(w[name]['out'])}"
            t.append(cell.rjust(col), style=DIM)
        lines.append(t)

        hit = s.get("cache_hit_7d")
        if hit is not None:
            lines.append(Text(""))
            lines.append(widgets.gauge_row("Cache hit 7d", hit, width, PALETTE,
                                           color=GREEN,
                                           suffix="of prompt tokens served from cache"))
        return self._panel("▤ Activity", Group(*lines), color=PALETTE["yellow"])

    def _render_usage(self):
        body = self.query_one("#usage-body", Static)
        width = max(48, (body.content_size.width or self.size.width) - 4)

        u = usage.snapshot()
        st = stats.snapshot()
        summary = st.get("summary")

        # Line the 5h spend window up with the real billing block whenever the
        # API has told us when it resets.
        five = next((w for w in u["windows"] if w["label"].startswith("5h")), None)
        if five and five["resets_in"] is not None:
            stats.set_five_hour_start(time.time() + five["resets_in"] - 5 * 3600)

        body.update(Group(
            self._limits_panel(u, width),
            self._spending_panel(summary, width),
            self._activity_panel(summary, width),
        ))

        parts = []
        if st.get("building"):
            done, total = st.get("done", 0), st.get("total", 0)
            parts.append(f"[{ACCENT}]building transcript cache {done}/{total}[/]")
        elif summary:
            parts.append(f"{summary['transcripts']} transcripts")
        if u["fetched_at"]:
            age = int(time.time() - u["fetched_at"])
            parts.append(f"[{FAINT}]limits {age}s ago[/]")
        elif u["error"]:
            parts.append(f"[{RED}]limits unavailable[/]")
        self._usage_subtitle = " · ".join(parts)
        self._usage_dirty = False

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
