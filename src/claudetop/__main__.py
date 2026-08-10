"""Command line entry point: `claudetop`, or `python -m claudetop`."""

import argparse

from . import __version__, paths


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="claudetop",
        description="Live dashboard of Claude Code sessions, spend and limits.")
    ap.add_argument("--version", action="version", version=f"claudetop {__version__}")
    ap.add_argument("--config", action="store_true",
                    help="create the config file if missing, print its path, and exit")
    ap.add_argument("--paths", action="store_true",
                    help="print the config and cache locations and exit")
    args = ap.parse_args(argv)

    if args.paths:
        print(f"config  {paths.config_dir() / paths.CONFIG_FILE}")
        print(f"cache   {paths.cache_dir()}")
        print(f"claude  {paths.CLAUDE_HOME}")
        return 0
    if args.config:
        print(paths.write_default_config())
        return 0

    from .app import SessionDashboard  # imported late: it pulls in Textual
    SessionDashboard().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
