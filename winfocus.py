"""Bring a terminal window to the foreground by matching its title.

Windows Terminal hosts every window in one process, so a PID can't tell two
windows apart — but each session's window carries a distinct title (Claude Code
sets it to the session name, and the manager's title is its App title). Matching
on the title is therefore the reliable way to focus a specific window.

Degrades gracefully: if pywin32 isn't importable, available() is False and the
focus_* calls are no-ops that return False.
"""

MANAGER_TITLE = "Claude Session Manager"

try:
    import ctypes
    import win32api
    import win32con
    import win32gui
    _HAVE = True
except Exception:  # pywin32 missing / non-Windows
    _HAVE = False


def available():
    return _HAVE


def _visible_windows():
    out = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd) or ""
            if title:
                out.append((hwnd, title))

    win32gui.EnumWindows(cb, None)
    return out


def _force_foreground(hwnd):
    """SetForegroundWindow, working around the foreground-lock rule.

    Windows blocks a background process from stealing focus unless it first
    injects input; a null ALT tap satisfies that rule. Restore the window if
    minimised, and fall back to BringWindowToTop if the steal is still denied.
    """
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:
            pass
        try:
            win32api.keybd_event(0x12, 0, 0, 0)                       # ALT down
            win32api.keybd_event(0x12, 0, win32con.KEYEVENTF_KEYUP, 0)  # ALT up
        except Exception:
            pass
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        try:
            win32gui.BringWindowToTop(hwnd)
            return True
        except Exception:
            return False


def _best_match(target):
    """Pick the window whose title matches `target` most tightly.

    Titles often carry a leading status glyph (e.g. a spinner), so an exact
    equality check isn't enough — rank exact, then endswith, then contains, and
    among equals prefer the shortest title (least extra noise).
    """
    if not _HAVE or not target:
        return None
    t = target.strip().lower()
    ranked = []
    for hwnd, title in _visible_windows():
        low = title.lower()
        if t == low:
            score = 0
        elif low.endswith(t):
            score = 1
        elif t in low:
            score = 2
        else:
            continue
        ranked.append((score, len(title), hwnd))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][2]


def focus_title(target):
    hwnd = _best_match(target)
    return _force_foreground(hwnd) if hwnd else False


def focus_session(name):
    """Focus the terminal window for a Claude session by its name."""
    return focus_title(name)


def focus_manager():
    """Focus the claudetop manager window."""
    return focus_title(MANAGER_TITLE)
