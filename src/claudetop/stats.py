"""Local usage stats built from the ~/.claude transcripts.

Feeds claudetop's spending and activity panels. Everything here is derived
from files already on disk — no network, no telemetry, nothing leaves the
machine.

Why a cache: the transcript corpus is hundreds of megabytes and grows all day.
Each .jsonl is append-only, so every file is read once from byte 0 and after
that only its new tail is read. Per-file results live in a JSON cache under the
platform cache dir, keyed by (size, mtime), so a restart is nearly free.

Two levels of detail are kept per file:
  totals   all-time counters and per-model token sums (small, kept forever)
  events   one row per assistant message, pruned to retention_days (used for
           the today / 5h / 7d / 30d windows)

Counting rules, learned from the transcript format:
  - One assistant message can be written across several lines (a thinking line
    and a tool_use line, say) and every line repeats the SAME usage block.
    Tokens are therefore counted once per message id; content blocks are
    counted per line, because those genuinely differ.
  - A user line whose content is only tool_result is not a prompt.
  - Sidechain (subagent) traffic still costs money, so it counts toward tokens
    and tools, but it is not counted as a prompt the human typed.
"""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from . import paths
from . import pricing

PROJECTS_DIR = paths.CLAUDE_HOME / "projects"
CACHE_VERSION = 3
CACHE_NAME = "transcripts.json"

EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
ID_MEMORY = 500          # message ids remembered per file for dedup
FIVE_HOURS = 5 * 3600


# --------------------------------------------------------------- line parse

def _ts(o):
    s = o.get("timestamp")
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _tool_name(name):
    """MCP tools are namespaced to death; keep the last, meaningful segment."""
    if name.startswith("mcp__"):
        return name.split("__")[-1]
    return name


def _blank_entry(path: Path):
    return {
        "size": 0, "mtime": 0.0, "offset": 0,
        "sid": path.stem, "project": path.parent.name,
        "cwd": None,     # real working directory, for a readable project name
        "title": None,   # the session's ai-title, for the leaderboard
        "first_ts": None, "last_ts": None,
        "totals": {"msgs": 0, "tools": 0, "prompts": 0,
                   "in": 0, "out": 0, "cr": 0, "cw": 0, "by_model": {}},
        # Per-month token sums, kept forever: events are pruned after a month,
        # so this is the only way to chart spend over a longer horizon.
        "months": {},    # "2026-08" -> {model: [in, out, cr, cw]}
        "events": [],    # [ts, model, in, out, cr, cw, tools, files]
        "prompts": [],   # [ts, ...]
        "files": [],     # [[ts, path], ...]
        "tools": [],     # [[ts, tool name], ...]
        "ids": [],
    }


def project_name(entry):
    """A short, human name for the repo a session ran in."""
    cwd = entry.get("cwd")
    if cwd:
        parts = str(cwd).replace("\\", "/").rstrip("/").split("/")
        if parts and parts[-1]:
            return parts[-1]
    return entry.get("project") or "?"


def _consume(entry, chunk):
    """Fold a chunk of new transcript lines into entry (mutates it)."""
    tot = entry["totals"]
    seen = set(entry["ids"])
    order = list(entry["ids"])

    for raw in chunk:
        # Cheap prefilter — most lines are none of these, and json.loads
        # dominates the cost of a full corpus scan.
        if ('"usage"' not in raw and '"user"' not in raw
                and '"aiTitle"' not in raw and '"lastPrompt"' not in raw):
            continue
        try:
            o = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(o, dict):
            continue
        kind = o.get("type")
        ts = _ts(o)
        if o.get("cwd") and not entry["cwd"]:
            entry["cwd"] = o["cwd"]
        if kind in ("ai-title", "last-prompt"):
            entry["title"] = (o.get("aiTitle") or o.get("slug")
                              or o.get("lastPrompt") or entry["title"])
            continue
        if ts:
            entry["first_ts"] = ts if entry["first_ts"] is None else min(entry["first_ts"], ts)
            entry["last_ts"] = ts if entry["last_ts"] is None else max(entry["last_ts"], ts)

        msg = o.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")

        if kind == "user":
            if o.get("isSidechain"):
                continue
            is_prompt = isinstance(content, str) and content.strip()
            if isinstance(content, list):
                is_prompt = any(isinstance(b, dict) and b.get("type") != "tool_result"
                                for b in content)
            if is_prompt:
                tot["prompts"] += 1
                if ts:
                    entry["prompts"].append(round(ts, 1))
            continue

        if kind != "assistant":
            continue

        # Content blocks are per line — count them every time.
        tools = 0
        touched = []
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                tools += 1
                name = b.get("name") or "?"
                if ts:
                    entry["tools"].append([round(ts, 1), _tool_name(name)])
                if name in EDIT_TOOLS:
                    inp = b.get("input") or {}
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if fp:
                        touched.append(fp)
        tot["tools"] += tools
        for fp in touched:
            if ts:
                entry["files"].append([round(ts, 1), fp])

        # Tokens are per message id — the same usage block is written on every
        # line of a multi-part message, so counting per line doubles the bill.
        mid = msg.get("id")
        if mid and mid in seen:
            if tools and entry["events"]:
                entry["events"][-1][6] += tools
            continue
        if mid:
            seen.add(mid)
            order.append(mid)

        u = msg.get("usage") or {}
        tin = int(u.get("input_tokens") or 0)
        tout = int(u.get("output_tokens") or 0)
        tcr = int(u.get("cache_read_input_tokens") or 0)
        tcw = int(u.get("cache_creation_input_tokens") or 0)
        model = msg.get("model") or "unknown"

        tot["msgs"] += 1
        tot["in"] += tin
        tot["out"] += tout
        tot["cr"] += tcr
        tot["cw"] += tcw
        bm = tot["by_model"].setdefault(model, {"msgs": 0, "in": 0, "out": 0,
                                                "cr": 0, "cw": 0})
        bm["msgs"] += 1
        bm["in"] += tin
        bm["out"] += tout
        bm["cr"] += tcr
        bm["cw"] += tcw

        if ts:
            entry["events"].append([round(ts, 1), model, tin, tout, tcr, tcw,
                                    tools, len(touched)])
            month = entry["months"].setdefault(
                time.strftime("%Y-%m", time.localtime(ts)), {})
            acc = month.setdefault(model, [0, 0, 0, 0])
            acc[0] += tin
            acc[1] += tout
            acc[2] += tcr
            acc[3] += tcw

    entry["ids"] = order[-ID_MEMORY:]
    return entry


def _scan_file(path: Path, entry):
    """Read whatever is new in one transcript. Returns (entry, changed)."""
    try:
        st = path.stat()
    except OSError:
        return entry, False
    if entry and entry["size"] == st.st_size and entry["mtime"] == st.st_mtime:
        return entry, False
    if entry and st.st_size < entry["offset"]:
        entry = None  # truncated or replaced — start over
    if entry is None:
        entry = _blank_entry(path)

    try:
        with open(path, "rb") as fh:
            fh.seek(entry["offset"])
            data = fh.read()
    except OSError:
        return entry, False

    # Only consume whole lines; a half-written last line is left for next pass.
    cut = data.rfind(b"\n")
    if cut < 0:
        entry["size"], entry["mtime"] = st.st_size, st.st_mtime
        return entry, False
    consumed, data = data[:cut + 1], None
    entry["offset"] += len(consumed)
    entry["size"], entry["mtime"] = st.st_size, st.st_mtime

    _consume(entry, consumed.decode("utf-8", "replace").splitlines())
    return entry, True


def _prune(entry, cutoff):
    """Drop per-message detail older than the retention window. Totals stay."""
    entry["events"] = [e for e in entry["events"] if e[0] >= cutoff]
    entry["prompts"] = [t for t in entry["prompts"] if t >= cutoff]
    entry["files"] = [f for f in entry["files"] if f[0] >= cutoff]
    entry["tools"] = [t for t in entry["tools"] if t[0] >= cutoff]


# ------------------------------------------------------------------- cache

def _cache_path():
    return paths.cache_dir() / CACHE_NAME


def load_cache():
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
        return {}
    files = raw.get("files")
    return files if isinstance(files, dict) else {}


def save_cache(files):
    path = paths.ensure(paths.cache_dir()) / CACHE_NAME
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": files}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# --------------------------------------------------------------- aggregate

def _empty_window():
    return {"cost": 0.0, "in": 0, "out": 0, "cr": 0, "cw": 0,
            "msgs": 0, "tools": 0, "prompts": 0, "sessions": 0, "files": 0}


def _window_tokens(w):
    return w["in"] + w["out"] + w["cr"] + w["cw"]


def summarize(files, now=None, five_hour_start=None):
    """Roll the per-file cache up into the windows the UI shows.

    five_hour_start lets the 5h window line up with the real billing block
    (usage API reset time minus five hours) instead of a rolling now-5h."""
    now = now or time.time()
    local_midnight = datetime.fromtimestamp(now).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    bounds = {
        "today": local_midnight,
        "5h": five_hour_start if five_hour_start else now - FIVE_HOURS,
        "7d": now - 7 * 86400,
        "30d": now - 30 * 86400,
    }
    windows = {k: _empty_window() for k in bounds}
    windows["all"] = _empty_window()

    sessions = {k: set() for k in windows}
    touched = {k: set() for k in windows}
    by_model = {"30d": {}, "all": {}}
    by_project = {}          # 30d cost per repo
    by_tool = {}             # 30d call count per tool
    per_session_today = {}   # today's cost per transcript, for the leaderboard
    hourly = [0.0] * 24      # last 24 hours, oldest first
    daily = [0.0] * 14       # last 14 local days, oldest first
    monthly = {}             # "2026-08" -> cost, every month on record
    recent_hour = 0.0        # cost in the last 60 minutes -> burn rate
    hour0 = now - 24 * 3600
    day0 = local_midnight - 13 * 86400

    # The 14-day chart is plotted finer than one point per day, so a busy
    # afternoon does not vanish into a daily total.
    fine_day_step = 6 * 3600                  # 6 hours
    fine_daily = [0.0] * (14 * 86400 // fine_day_step)

    for path, entry in files.items():
        tot = entry.get("totals") or {}
        allw = windows["all"]
        allw["msgs"] += tot.get("msgs", 0)
        allw["tools"] += tot.get("tools", 0)
        allw["prompts"] += tot.get("prompts", 0)
        for k in ("in", "out", "cr", "cw"):
            allw[k] += tot.get(k, 0)
        if tot.get("msgs"):
            sessions["all"].add(path)
        for model, m in (tot.get("by_model") or {}).items():
            acc = by_model["all"].setdefault(model, {"msgs": 0, "tokens": 0, "cost": 0.0})
            acc["msgs"] += m.get("msgs", 0)
            acc["tokens"] += m.get("in", 0) + m.get("out", 0) + m.get("cr", 0) + m.get("cw", 0)
            acc["cost"] += pricing.cost(model, m.get("in", 0), m.get("out", 0),
                                        m.get("cr", 0), m.get("cw", 0))
            allw["cost"] += pricing.cost(model, m.get("in", 0), m.get("out", 0),
                                         m.get("cr", 0), m.get("cw", 0))

        for ym, models in (entry.get("months") or {}).items():
            for model, (tin, tout, tcr, tcw) in models.items():
                monthly[ym] = monthly.get(ym, 0.0) + pricing.cost(
                    model, tin, tout, tcr, tcw)

        proj = project_name(entry)
        for ts, model, tin, tout, tcr, tcw, tools, nfiles in entry.get("events", []):
            c = pricing.cost(model, tin, tout, tcr, tcw)
            if ts >= hour0:
                hourly[min(23, int((ts - hour0) // 3600))] += c
            if ts >= day0:
                daily[min(13, int((ts - day0) // 86400))] += c
                fine_daily[min(len(fine_daily) - 1,
                               int((ts - day0) // fine_day_step))] += c
            if ts >= now - 3600:
                recent_hour += c
            for key, start in bounds.items():
                if ts < start:
                    continue
                w = windows[key]
                w["cost"] += c
                w["in"] += tin
                w["out"] += tout
                w["cr"] += tcr
                w["cw"] += tcw
                w["msgs"] += 1
                w["tools"] += tools
                sessions[key].add(path)
                if key == "30d":
                    acc = by_model["30d"].setdefault(
                        model, {"msgs": 0, "tokens": 0, "cost": 0.0})
                    acc["msgs"] += 1
                    acc["tokens"] += tin + tout + tcr + tcw
                    acc["cost"] += c
                    pacc = by_project.setdefault(
                        proj, {"cost": 0.0, "msgs": 0, "sessions": set()})
                    pacc["cost"] += c
                    pacc["msgs"] += 1
                    pacc["sessions"].add(path)
                if key == "today":
                    sess = per_session_today.setdefault(path, {
                        "cost": 0.0, "msgs": 0, "project": proj,
                        "title": entry.get("title"), "sid": entry.get("sid"),
                        "last_ts": 0.0})
                    sess["cost"] += c
                    sess["msgs"] += 1
                    sess["last_ts"] = max(sess["last_ts"], ts)

        for ts in entry.get("prompts", []):
            for key, start in bounds.items():
                if ts >= start:
                    windows[key]["prompts"] += 1
        for ts, fp in entry.get("files", []):
            for key, start in bounds.items():
                if ts >= start:
                    touched[key].add(fp)
        for ts, tool in entry.get("tools", []):
            if ts >= bounds["30d"]:
                by_tool[tool] = by_tool.get(tool, 0) + 1

    for key, w in windows.items():
        w["sessions"] = len(sessions[key])
        w["files"] = len(touched[key]) if key != "all" else 0
        w["tokens"] = _window_tokens(w)

    def cache_hit(w):
        cr, cw, tin = w["cr"], w["cw"], w["in"]
        if cr + cw <= 0:
            return None
        denom = tin + cr + cw
        return max(0.0, min(100.0, cr / denom * 100)) if denom > 0 else None

    def model_rows(d):
        rows = [{"model": m, **v} for m, v in d.items()]
        rows.sort(key=lambda r: r["cost"], reverse=True)
        return rows

    projects = [{"project": k, "cost": v["cost"], "msgs": v["msgs"],
                 "sessions": len(v["sessions"])} for k, v in by_project.items()]
    projects.sort(key=lambda r: r["cost"], reverse=True)

    tools = [{"tool": k, "calls": v} for k, v in by_tool.items()]
    tools.sort(key=lambda r: r["calls"], reverse=True)

    leaderboard = sorted(per_session_today.values(),
                         key=lambda r: r["cost"], reverse=True)

    # Burn rate: the last hour against the average working hour of the past
    # week. "Hours worked" counts only hours that saw a message, so an
    # overnight gap does not flatter the average.
    active_hours = sum(1 for v in hourly if v > 0)
    week_hours = max(1, len({int(e[0] // 3600)
                             for entry in files.values()
                             for e in entry.get("events", [])
                             if e[0] >= now - 7 * 86400}))
    burn = {
        "now": recent_hour,
        "avg_7d": windows["7d"]["cost"] / week_hours,
        "today_active_hours": active_hours,
    }

    return {
        "windows": windows,
        "cache_hit_7d": cache_hit(windows["7d"]),
        "cache_hit_today": cache_hit(windows["today"]),
        "by_model_30d": model_rows(by_model["30d"]),
        "by_model_all": model_rows(by_model["all"]),
        "by_project_30d": projects,
        "by_tool_30d": tools,
        "sessions_today": leaderboard,
        "hourly_24h": hourly,
        "hourly_start": hour0,   # epoch of the first hourly bucket
        "daily_14d": daily,
        "daily_start": day0,     # local midnight of the first daily bucket
        "fine_daily": fine_daily,            # 6-hour buckets over 14 days
        "fine_daily_step": fine_day_step,
        "monthly": sorted(monthly.items()),   # [("2026-08", cost), ...]
        "burn": burn,
        "transcripts": len(files),
    }


# ----------------------------------------------------- background refresher

_state = {"summary": None, "building": True, "done": 0, "total": 0,
          "scanned_at": 0.0, "error": None}
_state_lock = threading.Lock()
_worker_started = False
_five_hour_start = None


def snapshot():
    """Latest stats. Safe to call every UI tick (returns the shared dict —
    treat it as read-only)."""
    with _state_lock:
        return dict(_state)


def set_five_hour_start(ts):
    """Align the 5h window with the real billing block (from the usage API)."""
    global _five_hour_start
    _five_hour_start = ts


def refresh(files, retention_days=31, progress=None):
    """One pass over the transcript corpus. Returns the updated file cache."""
    cutoff = time.time() - retention_days * 86400
    if not PROJECTS_DIR.exists():
        return files, False
    found = sorted(PROJECTS_DIR.glob("**/*.jsonl"))
    changed = False
    for i, path in enumerate(found):
        key = str(path)
        entry, did = _scan_file(path, files.get(key))
        if did:
            _prune(entry, cutoff)
            changed = True
        files[key] = entry
        if progress:
            progress(i + 1, len(found))
    # Forget transcripts that were deleted.
    alive = {str(p) for p in found}
    for gone in [k for k in files if k not in alive]:
        files.pop(gone)
        changed = True
    return files, changed


def start_background(poll_seconds=5.0, retention_days=31):
    """Start the single transcript-scanning worker (idempotent)."""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    def loop():
        files = load_cache()
        first = True
        while True:
            def progress(done, total):
                if first:
                    with _state_lock:
                        _state["done"], _state["total"] = done, total

            files, changed = refresh(files, retention_days, progress if first else None)
            summary = summarize(files, five_hour_start=_five_hour_start)
            with _state_lock:
                _state["summary"] = summary
                _state["building"] = False
                _state["scanned_at"] = time.time()
            if changed:
                save_cache(files)
            first = False
            time.sleep(poll_seconds)

    threading.Thread(target=loop, name="stats-scan", daemon=True).start()


if __name__ == "__main__":  # quick check: python stats.py
    t0 = time.time()
    files, _ = refresh(load_cache(), progress=lambda d, t: (
        print(f"\rscanning {d}/{t}", end="", flush=True)))
    save_cache(files)
    s = summarize(files)
    print(f"\rscanned {len(files)} transcripts in {time.time() - t0:.1f}s")
    for key in ("today", "5h", "7d", "30d", "all"):
        w = s["windows"][key]
        print(f"{key:>6}  ${w['cost']:>9,.2f}  {w['msgs']:>6} msgs  "
              f"{w['tools']:>6} tools  {w['prompts']:>5} prompts  "
              f"{w['sessions']:>4} sessions  {w['tokens'] / 1e6:>7.1f}M tok")
    if s["cache_hit_7d"] is not None:
        print(f"cache hit 7d: {s['cache_hit_7d']:.1f}%")
    for r in s["by_model_all"][:6]:
        print(f"  {pricing.short_model(r['model']):<16} ${r['cost']:>9,.2f}  "
              f"{r['tokens'] / 1e6:>7.1f}M tok")
