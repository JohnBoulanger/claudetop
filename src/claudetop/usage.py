"""Claude subscription limit usage — the 5h / 7d gauges.

Data comes from Anthropic's OAuth-scoped usage endpoint, the same one the
`/usage` command and openusage read:

    GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <Claude Code OAuth access token>
    anthropic-beta: oauth-2025-04-20

The token is the one Claude Code already holds, so there is nothing to log in
to. It lives in ~/.claude/.credentials.json on Windows and Linux; on macOS
Claude Code keeps it in the login keychain instead, under the service name
"Claude Code-credentials".

Nothing here ever writes a token anywhere, and the only host contacted is
api.anthropic.com. Set "usage_api": false in the claudetop config to disable
the poll entirely.
"""

import copy
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import paths

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
KEYCHAIN_SERVICE = "Claude Code-credentials"
HTTP_TIMEOUT = 10


# ------------------------------------------------------------------- token

def _token_from_file():
    path = paths.CLAUDE_HOME / ".credentials.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data.get("claudeAiOauth") if isinstance(data, dict) else None


def _token_from_keychain():
    """macOS: Claude Code stores the credential blob in the login keychain."""
    if sys.platform != "darwin":
        return None
    try:
        p = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        data = json.loads(p.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    return data.get("claudeAiOauth") if isinstance(data, dict) else None


def read_token():
    """(token, error). The token is never cached on disk or logged."""
    oauth = _token_from_file() or _token_from_keychain()
    if not oauth:
        return None, "no Claude Code credentials found"
    token = oauth.get("accessToken")
    if not token:
        return None, "credentials have no access token"
    exp = oauth.get("expiresAt")
    if isinstance(exp, (int, float)) and exp > 0 and time.time() * 1000 >= exp:
        # Claude Code refreshes on its next run; a stale token only gives 401s.
        return None, "OAuth token expired — run any Claude Code session to refresh"
    return token, None


# -------------------------------------------------------------------- fetch

def fetch(timeout=HTTP_TIMEOUT):
    """(payload, error). Never raises."""
    token, err = read_token()
    if err:
        return None, err
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "Content-Type": "application/json",
        "User-Agent": "claudetop",
    })
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            # Error bodies are pretty-printed JSON; flatten so the message
            # stays one line and cannot break a panel border.
            detail = " ".join(e.read(256).decode("utf-8", "replace").split())
        except OSError:
            pass
        if e.code in (401, 403):
            return None, "usage API rejected the token — is Claude Code logged in?"
        if e.code == 429:
            return None, "usage API rate limited — backing off"
        return None, f"usage API returned {e.code} {detail}"[:110]
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, " ".join(f"usage API unreachable: {e}".split())[:110]
    try:
        return json.loads(body), None
    except (json.JSONDecodeError, ValueError):
        return None, "usage API returned unparsable JSON"


# ------------------------------------------------------------------ shaping

def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _bucket(raw, label):
    """One limit window -> {label, pct, resets_at, resets_in}."""
    if not isinstance(raw, dict):
        return None
    pct = raw.get("utilization")
    if pct is None:
        return None
    resets = _parse_ts(raw.get("resets_at"))
    secs = None
    if resets is not None:
        secs = max(0, (resets - datetime.now(timezone.utc)).total_seconds())
    return {
        "label": label,
        "pct": float(pct),
        "resets_at": resets,
        "resets_in": secs,
        "used_dollars": raw.get("used_dollars"),
        "limit_dollars": raw.get("limit_dollars"),
    }


def shape(payload):
    """Flatten the API payload into the handful of things the UI shows.

    Anthropic returns a wide response with a rotating cast of code-named
    buckets (most of them null). Only the windows that carry a utilization
    number are kept, in a fixed display order."""
    if not isinstance(payload, dict):
        return {"windows": [], "credits": None}

    named = [
        ("five_hour", "5h limit"),
        ("seven_day", "7d limit"),
        ("seven_day_opus", "7d Opus"),
        ("seven_day_sonnet", "7d Sonnet"),
        ("seven_day_cowork", "7d Cowork"),
        ("seven_day_oauth_apps", "7d OAuth apps"),
    ]
    windows = [b for b in (_bucket(payload.get(k), lbl) for k, lbl in named) if b]

    # `limits` carries per-model scoped windows the flat keys do not name
    # (e.g. a weekly cap that applies only to one model).
    for lim in payload.get("limits") or []:
        if not isinstance(lim, dict) or lim.get("kind") != "weekly_scoped":
            continue
        scope = (lim.get("scope") or {}).get("model") or {}
        name = scope.get("display_name") or scope.get("id")
        if not name:
            continue
        b = _bucket({"utilization": lim.get("percent"),
                     "resets_at": lim.get("resets_at")}, f"7d {name}")
        if b and not any(w["label"] == b["label"] for w in windows):
            b["severity"] = lim.get("severity")
            windows.append(b)

    credits = None
    extra = payload.get("extra_usage")
    spend = payload.get("spend") or {}
    used = spend.get("used") or {}
    if isinstance(extra, dict) or used:
        amount = used.get("amount_minor")
        exponent = used.get("exponent", 2)
        dollars = None
        if isinstance(amount, (int, float)):
            dollars = amount / (10 ** exponent)
        elif isinstance(extra, dict) and isinstance(extra.get("used_credits"), (int, float)):
            dollars = extra["used_credits"] / 100.0
        credits = {
            "used_dollars": dollars,
            "enabled": bool((extra or {}).get("is_enabled") or spend.get("enabled")),
            "limit_dollars": (extra or {}).get("monthly_limit"),
            "currency": used.get("currency") or (extra or {}).get("currency") or "USD",
        }

    return {"windows": windows, "credits": credits}


def projected(window, spent_fraction_of_window):
    """Naive straight-line projection of a window's % at reset time.

    spent_fraction_of_window is how much of the window has already elapsed
    (0..1). Right for the 5h window, which is one working block by
    definition; for the weekly window see project_weekly."""
    if not window or not window.get("pct") or not spent_fraction_of_window:
        return None
    if spent_fraction_of_window <= 0.02:  # too early to say anything useful
        return None
    return min(999.0, window["pct"] / spent_fraction_of_window)


# ------------------------------------------------------- the working week

# Monday is 0, matching datetime.weekday().
WORK_DEFAULTS = {
    "work_days": [0, 1, 2, 3, 4],
    "work_blocks_per_day": 2,   # 5h limit windows you expect to use in a day
    "work_start_hour": 9,
}


def work_shape(cfg):
    cfg = cfg or {}
    days = cfg.get("work_days")
    if not isinstance(days, (list, tuple)) or not days:
        days = WORK_DEFAULTS["work_days"]
    blocks = cfg.get("work_blocks_per_day") or WORK_DEFAULTS["work_blocks_per_day"]
    start = cfg.get("work_start_hour")
    if start is None:
        start = WORK_DEFAULTS["work_start_hour"]
    return set(int(d) for d in days), float(blocks), int(start)


def work_hours_between(start_ts, end_ts, cfg=None):
    """Working hours between two timestamps, per the configured week.

    A working day runs from work_start_hour for blocks x 5 hours — two blocks
    from 09:00 is 09:00 to 19:00. Weekends (or whatever is not in work_days)
    contribute nothing, which is the whole point: a weekly projection that
    counts Saturday night as burn time will always read high.
    """
    if end_ts <= start_ts:
        return 0.0
    work_days, blocks, start_hour = work_shape(cfg)
    span = blocks * 5.0 * 3600.0
    total = 0.0
    day = datetime.fromtimestamp(start_ts).replace(
        hour=0, minute=0, second=0, microsecond=0)
    while day.timestamp() < end_ts:
        if day.weekday() in work_days:
            open_ = day.replace(hour=start_hour).timestamp()
            close = open_ + span
            total += max(0.0, min(close, end_ts) - max(open_, start_ts))
        day += timedelta(days=1)
    return total / 3600.0


def project_weekly(window, cfg=None, now=None):
    """Where the weekly window lands at reset, at your current pace.

    Scales by working hours rather than wall clock: if a third of the week's
    working hours have passed and you are at 9%, you land near 27% — not the
    63% a 24/7 assumption would claim.

    Returns (projected_pct, elapsed_work_hours, total_work_hours) or None.
    """
    if not window or window.get("resets_in") is None or not window.get("pct"):
        return None
    now = now or time.time()
    end = now + window["resets_in"]
    start = end - 7 * 86400
    elapsed = work_hours_between(start, now, cfg)
    total = work_hours_between(start, end, cfg)
    if elapsed < 0.5 or total <= 0:      # too early in the week to say
        return None
    return min(999.0, window["pct"] * total / elapsed), elapsed, total


def weekly_time_to_limit(window, cfg=None, now=None, step_minutes=15):
    """Seconds of wall clock until the weekly window would reach 100%.

    Walks the calendar forward accumulating only working hours, so the answer
    lands on a working afternoon rather than in the middle of Sunday night.
    None when the pace does not reach the cap before reset.
    """
    if not window or window.get("resets_in") is None or not window.get("pct"):
        return None
    now = now or time.time()
    end = now + window["resets_in"]
    start = end - 7 * 86400
    elapsed = work_hours_between(start, now, cfg)
    if elapsed < 0.5:
        return None
    rate = window["pct"] / elapsed                 # percent per working hour
    if rate <= 0:
        return None
    needed = (100.0 - window["pct"]) / rate        # working hours still to go
    if needed <= 0:
        return 0.0
    step = step_minutes * 60
    accrued, t = 0.0, now
    while t < end:
        nxt = min(t + step, end)
        accrued += work_hours_between(t, nxt, cfg)
        if accrued >= needed:
            return nxt - now
        t = nxt
    return None


# ----------------------------------------------------- background refresher

_state = {"windows": [], "credits": None, "error": None,
          "fetched_at": 0.0, "loading": True}
_state_lock = threading.Lock()
_worker_started = False


def snapshot():
    """Latest usage state. Safe to call every UI tick."""
    with _state_lock:
        data = copy.deepcopy(_state)
    for w in data["windows"]:
        # resets_in is only correct at fetch time; re-derive it against now.
        if w.get("resets_at") is not None:
            w["resets_in"] = max(
                0, (w["resets_at"] - datetime.now(timezone.utc)).total_seconds())
    return data


CACHE_NAME = "usage.json"


def _cache_path():
    return paths.cache_dir() / CACHE_NAME


def _load_cached():
    """Last good payload, so a cold start (or a rate limit) still has numbers."""
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None, 0.0
    if not isinstance(raw, dict):
        return None, 0.0
    return raw.get("payload"), float(raw.get("fetched_at") or 0.0)


def _save_cached(payload):
    path = paths.ensure(paths.cache_dir()) / CACHE_NAME
    try:
        path.write_text(json.dumps({"fetched_at": time.time(),
                                    "payload": payload}), encoding="utf-8")
    except OSError:
        pass


def start_background(poll_seconds=60, enabled=True):
    """Start the single usage-API poller (idempotent)."""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    if not enabled:
        with _state_lock:
            _state.update(loading=False, error="usage API disabled in config")
        return

    cached, when = _load_cached()
    if cached:
        with _state_lock:
            _state.update(shape(cached))
            _state["fetched_at"] = when
            _state["loading"] = False

    def loop():
        while True:
            payload, err = fetch()
            with _state_lock:
                _state["loading"] = False
                _state["error"] = err
                if payload is not None:
                    _state.update(shape(payload))
                    _state["fetched_at"] = time.time()
                    _save_cached(payload)
            # A failed poll backs off so a logged-out machine is not hammered,
            # and a rate limit backs off hard — the numbers move slowly anyway.
            if err is None:
                delay = poll_seconds
            elif "rate limited" in err:
                delay = max(poll_seconds, 300)
            else:
                delay = max(poll_seconds, 120)
            time.sleep(delay)

    threading.Thread(target=loop, name="usage-poll", daemon=True).start()


if __name__ == "__main__":  # quick check: python usage.py
    payload, err = fetch()
    if err:
        print("error:", err)
        raise SystemExit(1)
    if os.environ.get("CLAUDETOP_RAW"):
        print(json.dumps(payload, indent=2))
    data = shape(payload)
    for w in data["windows"]:
        mins = int((w["resets_in"] or 0) / 60)
        print(f"{w['label']:<16} {w['pct']:5.1f}%  resets in {mins // 60}h{mins % 60:02d}m")
    if data["credits"] and data["credits"]["used_dollars"] is not None:
        print(f"{'credits used':<16} ${data['credits']['used_dollars']:.2f}")
