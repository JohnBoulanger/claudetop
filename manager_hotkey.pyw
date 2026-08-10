"""Global hotkey: Ctrl+Alt+M focuses the claudetop manager window.

Runs headless (launch with pythonw). If the manager window isn't open, it
launches one in a new Windows Terminal window. Self-guarding: only one instance
can own a system-wide hotkey, so a second copy fails RegisterHotKey and exits —
starting it from every shell is harmless.
"""

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import winfocus  # noqa: E402

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_M = 0x4D
WM_HOTKEY = 0x0312
HOTKEY_ID = 1

user32 = ctypes.windll.user32


def launch_manager():
    dash = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.py")
    # -w new opens a new Windows Terminal window; the manager sets its own title.
    subprocess.Popen(
        ["wt.exe", "-w", "new", "pwsh", "-NoExit", "-Command", f'python "{dash}"'],
        creationflags=0x00000008,  # DETACHED_PROCESS
    )


def main():
    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_M):
        return  # another instance already owns the hotkey
    try:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                if not winfocus.focus_manager():
                    launch_manager()
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)


if __name__ == "__main__":
    main()
