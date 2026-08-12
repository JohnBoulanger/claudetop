"""Collector for a ticket-worktree board.

Optional. It is off until you set `worktree_root` in the claudetop config,
and it assumes one layout:

  <worktree_root>/<ticket>/<repo>     one worktree per repo, per ticket
  <worktree_base_dir>/<repo>          the base clones those worktrees hang off

Two consumers:
  - `claudetop-wt --json` for scripts and Claude skills
  - `claudetop-wt` prints a plain table for a quick look

Read-only by design: it never fetches, prunes, or touches a working tree.
Ahead/behind numbers are as-of-last-fetch.
"""

import argparse
import copy
import json
import subprocess
import threading
import time
from pathlib import Path

from . import paths

# Layout is per-machine, so all of it comes from the claudetop config:
#   worktree_root        where the ticket worktrees live; unset disables this
#   worktree_base_dir    where the base clones live
#   worktree_org         GitHub owner, for `gh pr` lookups; unset skips them
#   worktree_ticket_glob which child dirs of the root count as tickets
#   worktree_main_branch what "behind main" is measured against
#   worktree_bootstrap   {repo: [paths that must exist]}, see bootstrap_state
_CFG = paths.load_config()
_root = _CFG.get("worktree_root")
WT_ROOT = Path(_root) if _root else None
_base = _CFG.get("worktree_base_dir")
BASE_DIR = Path(_base) if _base else None
ORG = _CFG.get("worktree_org") or None
TICKET_GLOB = _CFG.get("worktree_ticket_glob") or "*"
MAIN_BRANCH = _CFG.get("worktree_main_branch") or "main"
BOOTSTRAP = _CFG.get("worktree_bootstrap") or {}
# Anything the root holds that is not a ticket: shared build caches and such.
IGNORE_DIRS = {"_cargo-target"}

GIT_TIMEOUT = 8       # seconds per git call
GH_TIMEOUT = 15       # seconds per gh call
PR_TTL = 60           # seconds a PR lookup stays fresh
BEHIND_MAIN_LIMIT = 50  # commits behind main before a worktree is flagged stale


def _run(args, cwd=None, timeout=GIT_TIMEOUT):
    """Run a command without a shell; (returncode, stdout). -1 on failure."""
    try:
        p = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def _git(path, *args):
    return _run(["git", "-C", str(path), *args])


# ---------------------------------------------------------------- bootstrap

def _satisfied(wt: Path, req):
    """One worktree_bootstrap entry: a path, or {path, contains}. A malformed
    entry counts as unmet rather than crashing the scan thread."""
    if isinstance(req, str):
        return (wt / req).exists()
    if not isinstance(req, dict) or not req.get("path"):
        return False
    target = wt / req["path"]
    needle = req.get("contains")
    if needle is None:
        return target.exists()
    try:
        return needle in target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def bootstrap_state(wt: Path, repo: str):
    """Can this worktree build?

    A fresh worktree is missing everything git does not track, so a repo can
    look clean and still not build. List what a build needs, per repo, in the
    config. An entry is either a path that must exist, or a path that must
    contain a string — for the local-only edits that never get committed:

        "worktree_bootstrap": {
          "my-rust-svc": [
            ".cargo/config.toml",
            {"path": "Cargo.toml", "contains": "gdal = \\"0.17"}
          ]
        }

    With no entry, a JS repo is judged by node_modules and anything else is
    'n/a' rather than guessed at.
    """
    required = BOOTSTRAP.get(repo)
    if required:
        return "ok" if all(_satisfied(wt, r) for r in required) else "missing"
    if (wt / "package.json").exists():
        return "ok" if (wt / "node_modules").exists() else "missing"
    return "n/a"


# ------------------------------------------------------------------- scan

def _worktree_row(ticket_dir: Path, wt: Path):
    ticket = ticket_dir.name
    repo = wt.name
    row = {
        "ticket": ticket, "repo": repo, "path": str(wt),
        "branch": "", "dirty": None, "ahead": None, "behind": None,
        "unpushed": False, "behind_main": None,
        "bootstrap": bootstrap_state(wt, repo),
        "error": None,
    }
    rc, branch = _git(wt, "branch", "--show-current")
    if rc != 0:
        row["error"] = "git failed (not a worktree?)"
        return row
    row["branch"] = branch

    rc, status = _git(wt, "status", "--porcelain")
    row["dirty"] = bool(status) if rc == 0 else None

    rc, _ = _git(wt, "rev-parse", "--verify", "-q", f"origin/{branch}")
    if rc == 0:
        rc2, counts = _git(wt, "rev-list", "--left-right", "--count",
                           f"{branch}...origin/{branch}")
        if rc2 == 0 and counts:
            a, _, b = counts.partition("\t")
            row["ahead"], row["behind"] = int(a or 0), int(b or 0)
    else:
        row["unpushed"] = True

    rc, n = _git(wt, "rev-list", "--count", f"HEAD..origin/{MAIN_BRANCH}")
    if rc == 0 and n.isdigit():
        row["behind_main"] = int(n)
    return row


def _registered_worktrees():
    """path -> base repo, for every extra worktree each base clone registers."""
    reg = {}
    if not BASE_DIR or not BASE_DIR.exists():
        return reg
    for base in sorted(BASE_DIR.iterdir()):
        if not (base / ".git").exists():
            continue
        rc, out = _git(base, "worktree", "list", "--porcelain")
        if rc != 0:
            continue
        base_norm = str(base.resolve()).replace("\\", "/").lower()
        for line in out.splitlines():
            if not line.startswith("worktree "):
                continue
            p = line[len("worktree "):].strip()
            norm = p.replace("\\", "/").lower()
            if norm != base_norm:
                reg[norm] = {"base": str(base), "path": p}
    return reg


def scan():
    """One full read-only pass. Returns {rows, orphans, scanned_at}."""
    rows = []
    on_disk = set()
    if WT_ROOT and WT_ROOT.exists():
        for ticket_dir in sorted(WT_ROOT.glob(TICKET_GLOB)):
            if not ticket_dir.is_dir() or ticket_dir.name in IGNORE_DIRS:
                continue
            for wt in sorted(ticket_dir.iterdir()):
                if not wt.is_dir() or wt.name in IGNORE_DIRS:
                    continue
                on_disk.add(str(wt.resolve()).replace("\\", "/").lower())
                rows.append(_worktree_row(ticket_dir, wt))

    reg = _registered_worktrees()
    strays = [p for p in sorted(on_disk) if p not in reg]
    stale = [info for norm, info in sorted(reg.items())
             if not Path(info["path"]).exists()]
    return {
        "rows": rows,
        "orphans": {"stray_folders": strays, "stale_registrations": stale},
        "scanned_at": time.time(),
    }


# --------------------------------------------------------------- PR lookup

_pr_cache = {}          # (repo, branch) -> (fetched_at, pr_dict_or_None)
_pr_lock = threading.Lock()


def pr_cached(repo, branch):
    """Non-blocking cache read. None = never fetched (render as loading)."""
    with _pr_lock:
        hit = _pr_cache.get((repo, branch))
    return hit[1] if hit else None


def _fetch_pr(repo, branch):
    rc, out = _run(
        ["gh", "pr", "list", "--repo", f"{ORG}/{repo}", "--head", branch,
         "--state", "all", "--limit", "1",
         "--json", "number,state,isDraft,url"],
        timeout=GH_TIMEOUT,
    )
    if rc != 0:
        return {"error": "gh failed"}
    try:
        prs = json.loads(out or "[]")
    except json.JSONDecodeError:
        return {"error": "bad gh output"}
    return prs[0] if prs else {"none": True}


def refresh_prs(rows):
    """Fetch PR state for every row whose cache entry is stale. Sequential on
    purpose (never hammer gh); callers run this off the UI thread."""
    if not ORG:
        return
    now = time.time()
    for r in rows:
        if not r["branch"]:
            continue
        key = (r["repo"], r["branch"])
        with _pr_lock:
            hit = _pr_cache.get(key)
            if hit and now - hit[0] < PR_TTL:
                continue
        pr = _fetch_pr(*key)
        with _pr_lock:
            _pr_cache[key] = (time.time(), pr)


# ------------------------------------------------------------------ flags

def _pr_done(pr):
    return bool(pr) and pr.get("state") in ("MERGED", "CLOSED")


def compute_flags(rows):
    """Two lists from a scan plus whatever PR data has arrived: tickets whose
    every worktree is finished and safe to delete, and worktrees that need a
    look."""
    by_ticket = {}
    for r in rows:
        by_ticket.setdefault(r["ticket"], []).append(r)

    candidates = []
    for ticket, group in sorted(by_ticket.items()):
        def settled(r):
            pr = pr_cached(r["repo"], r["branch"])
            return (r["dirty"] is False and not r["unpushed"]
                    and (r["ahead"] or 0) == 0 and _pr_done(pr))
        if group and all(settled(r) for r in group):
            candidates.append(ticket)

    problems = []
    for r in rows:
        pr = pr_cached(r["repo"], r["branch"])
        if (r["dirty"] or r["unpushed"] or (r["ahead"] or 0) > 0) and _pr_done(pr):
            problems.append({"kind": "leftover work", **r})
        if (r["behind_main"] or 0) > BEHIND_MAIN_LIMIT:
            problems.append({"kind": "far behind main", **r})
        if r["bootstrap"] == "missing":
            problems.append({"kind": "missing bootstrap", **r})
        if r["error"]:
            problems.append({"kind": "scan error", **r})
    return {"ship_done_candidates": candidates, "problems": problems}


# ----------------------------------------------------- background refresher

_state = {"rows": [], "orphans": {"stray_folders": [], "stale_registrations": []},
          "scanned_at": 0.0}
_state_lock = threading.Lock()
_worker_started = False


def snapshot():
    """Latest scan, PR-enriched, plus flags. Safe to call every UI tick."""
    with _state_lock:
        data = copy.deepcopy(_state)
    for r in data["rows"]:
        r["pr"] = pr_cached(r["repo"], r["branch"])
    data["flags"] = compute_flags(data["rows"])
    return data


def start_background(scan_interval=2.0):
    """Start the single scan+PR worker thread (idempotent)."""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    def loop():
        while True:
            data = scan()
            with _state_lock:
                _state.update(data)
            refresh_prs(data["rows"])  # TTL-gated; usually a no-op
            time.sleep(scan_interval)

    threading.Thread(target=loop, name="wt-scan", daemon=True).start()


# -------------------------------------------------------------------- CLI

def _fmt_ab(r):
    if r["unpushed"]:
        return "unpushed"
    if r["ahead"] is None:
        return "?"
    if r["ahead"] == 0 and r["behind"] == 0:
        return "even"
    return f"+{r['ahead']}/-{r['behind']}"


def _fmt_pr(pr):
    if pr is None:
        return "?"
    if pr.get("error"):
        return "unknown"
    if pr.get("none"):
        return "none"
    draft = " draft" if pr.get("isDraft") else ""
    return f"#{pr['number']} {pr['state']}{draft}"


def main():
    ap = argparse.ArgumentParser(description="Ticket-worktree board")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-pr", action="store_true", help="skip gh PR lookups")
    ap.add_argument("ticket", nargs="?", help="scope to one ticket")
    args = ap.parse_args()

    if not WT_ROOT:
        msg = ('Set "worktree_root" in the claudetop config to use this '
               f'({paths.config_dir() / paths.CONFIG_FILE}).')
        print(json.dumps({"error": msg}) if args.json else msg)
        return

    data = scan()
    if args.ticket:
        t = args.ticket.upper()
        data["rows"] = [r for r in data["rows"] if r["ticket"] == t]
    if not args.no_pr:
        refresh_prs(data["rows"])
    for r in data["rows"]:
        r["pr"] = pr_cached(r["repo"], r["branch"])
    data["flags"] = compute_flags(data["rows"])

    if args.json:
        print(json.dumps(data, indent=2))
        return

    if not data["rows"]:
        print(f"No ticket worktrees under {WT_ROOT}.")
    else:
        print(f"{'Ticket':<10} {'Repo':<26} {'Branch':<30} {'Clean':<6} "
              f"{'A/B':<10} {'PR':<18} Bootstrap")
        for r in data["rows"]:
            clean = "?" if r["dirty"] is None else ("dirty" if r["dirty"] else "clean")
            print(f"{r['ticket']:<10} {r['repo']:<26} {r['branch']:<30} "
                  f"{clean:<6} {_fmt_ab(r):<10} {_fmt_pr(r.get('pr')):<18} "
                  f"{r['bootstrap']}")
    f = data["flags"]
    print(f"\nfinished, safe to delete: "
          f"{', '.join(f['ship_done_candidates']) or 'none'}")
    if f["problems"]:
        for p in f["problems"]:
            print(f"problem: {p['kind']}: {p['ticket']}/{p['repo']}")
    o = data["orphans"]
    if o["stray_folders"] or o["stale_registrations"]:
        for s in o["stray_folders"]:
            print(f"stray folder (no base clone registers it): {s}")
        for s in o["stale_registrations"]:
            print(f"stale registration in {s['base']}: {s['path']} "
                  f"(git worktree prune)")


if __name__ == "__main__":
    main()
