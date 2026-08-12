# claudetop

A live terminal dashboard for Claude Code: what is running right now, what it
is costing, and how close you are to your subscription limits.

Read-only. It watches the files Claude Code already writes on your machine and
makes one HTTPS call a minute to Anthropic's own usage endpoint, reusing the
OAuth token Claude Code already holds. Nothing is sent anywhere else, the token
is never copied or logged, and no session is ever started, stopped or written
to. Set `"usage_api": false` in the config to drop the network call entirely.

```
✻ Claude Sessions
6 sessions · polling ~/.claude

  Status         Name              Kind          Branch   Cost      Uptime   PID
  ⠹ Marinating…  refactor          background    HEAD     $398.49   92h31m   48848
  ○ idle         reviewer          interactive   HEAD     $5.10     1h37m    38000
```

## Views

The home view is the usage page: limits, spend and what is running right now.
Every other view is a toggle — the same key, or `esc`, brings you home.

| Key | View | What it shows |
|-----|------|---------------|
| — | Home | 5h / 7d limit gauges with reset countdown and projection, spend by window, burn rate, line charts of spend per hour and per day, and the live session strip. |
| `s` | Sessions | Every live session and background job, with status, branch, cost and uptime. |
| `a` | Analytics | Activity counters, cache hit rate, cost by model, cost by repo, most-used tools, and today's most expensive sessions. |
| `m` | Star map | Sessions drawn as a constellation, each with its background jobs orbiting it. |
| `h` | — | Hide every dollar figure, for screen sharing. |
| `r` | — | Force a refresh. |
| `ctrl+p` | Customize | Every option, grouped, with a live preview beside it. `↑↓` moves, `←→` chooses a value, and **enter confirms** — nothing is written until you press it. `r` stages the default, `esc` discards anything unconfirmed and a second `esc` closes. |
| `q` | — | Quit. |

A compact 5h / 7d gauge strip sits under the title in every view, so you never
have to leave a view to check where you are against the limits.

## The sky

The background fills every view, and it is not decoration — it is a second,
ambient read on the same data. Pick a pack in Customize; all of them speak the
same language:

| What you see | What it means |
|--------------|---------------|
| More of it, moving faster | Live burn rate and how many sessions are working. Idle is still and sparse; flat out is about 2.6x as dense and visibly in motion. |
| A meteor | An event just happened — green for a background job finishing, red for a session that needs your input, terracotta for a new prompt. |
| A level along the bottom | 5h utilization. It rises as you spend and turns red past 90%. |
| Its colour | The model burning most right now, when `model_tint` is on. |

Packs: `stars` (default), `rain`, `embers`, `ocean` (waterline is your 5h
window), `city` (a skyline where each building is a repo and its height is
that repo's 30-day spend), `matrix` (one falling column per working session,
spelled from their names), and `off`.

Writing another is a class with `set_activity`, `set_limit`, `emit` and
`frame` — see `skies.py`. Because the contract is data-in / cells-out, a new
pack cannot change what any number means.

Textual cannot show a lower layer through an upper widget's cells, so the
panels paint their own dimmed stars in the gaps between text. That keeps the
field continuous across a panel edge while leaving the text readable.

## Install

Requires Python 3.10 or newer.

```bash
# recommended: an isolated install with its own venv
pipx install git+<your repo url>

# or from a clone
pipx install .

# or plain pip
pip install .
```

Then run `claudetop`.

Works on Windows and macOS. Nothing in the dashboard is platform-specific; the
optional pywin32 dependency is only used by the separate Ctrl+Alt+M hotkey
script in `scripts/`, which raises the dashboard window on Windows.

## Where the numbers come from

| Panel | Source | Network |
|-------|--------|---------|
| Sessions, jobs, star map | `~/.claude/sessions/*.json`, `~/.claude/daemon/roster.json`, `~/.claude/jobs/*/state.json` | none |
| Spend, burn rate, activity, cache hit, tools, repos | `~/.claude/projects/**/*.jsonl` transcripts | none |
| 5h / 7d limits, extra-usage credits | `GET https://api.anthropic.com/api/oauth/usage` | one call per minute |

The last good limits response is cached on disk, so a cold start still shows
numbers while the first call is in flight — or while the API is rate limiting
you. Stale figures are labelled as such rather than silently shown as current.

The usage call reuses the OAuth token Claude Code already holds
(`~/.claude/.credentials.json`, or the login keychain on macOS). Nothing is sent
anywhere else, and the token is never written to disk or logged by claudetop.
Set `"usage_api": false` in the config to turn the call off; the rest of the
usage view still works.

### Cost accuracy

Costs are computed from transcript token counts at the rates in `pricing.py`,
not billed amounts, so treat them as a close estimate. Two details matter:

- One assistant message is often written to the transcript across several lines
  that each repeat the same usage block, so tokens are counted once per message
  id.
- Cache reads bill at about 0.1x the input rate and cache writes at about 1.25x.

## Configuration

Everything optional. Create the file with `claudetop --config`, or write it
yourself:

- Windows `%APPDATA%\claudetop\config.json`
- macOS `~/Library/Application Support/claudetop/config.json`
- Linux `~/.config/claudetop/config.json`

```json
{
  "theme": { "preset": "black", "accent": "#da7756" },
  "worktree_root": "C:/worktrees",
  "worktree_base_dir": "C:/Users/you/repos",
  "worktree_org": "your-github-owner",
  "usage_api": true,
  "usage_poll_seconds": 60,
  "stats_retention_days": 31,
  "hide_costs": false
}
```

Most of this is editable in the app with `ctrl+s`; the file is the full set.

Presets: `black` (default, terracotta on true black), `warm`, `midnight`,
`gruvbox`. Any single color in a preset can be overridden by name — `bg`,
`panel`, `text`, `dim`, `faint`, `accent`, `red`, `green`, `yellow`, `border`,
`star`. Set `hide_costs` to blank out every dollar figure for screen sharing.
Colors are compiled into the Textual stylesheet at startup, so a theme change
lands on the next launch; the settings screen marks those options with a `*`.
Older configs naming `espresso` or `espresso-warm` still load — they are the
same palettes as `black` and `warm`.

### Customize

`ctrl+p` covers all of this; the config file is only needed for the two things
the page cannot express (`panels`, and individual theme colours).

| Key | Options | What it does |
|-----|---------|--------------|
| `sky` | stars, rain, embers, ocean, city, matrix, off | the ambient background |
| `motion` | off, calm, normal, lively | animation budget — turn it down over SSH, off for a still screen |
| `model_tint` | true/false | colour the background by the busiest model |
| `gauges` | bar, blocks, dial, trend | how the 5h and 7d meters draw |
| `layout` | full, compact, minimal, charts | which panels the home page stacks |
| `panels` | e.g. `["limits","live"]` | explicit panel order, overrides `layout` |
| `session_colors` | hash, status, off | hash gives each session a stable colour everywhere |
| `weather` | true/false | plain-language forecast line under the gauges |
| `idle_screensaver_minutes` | 0 to disable | full-screen sky and clock once you stop typing |
| `session_sparklines` | true/false | 24h cost trace per row in the sessions view |

`claudetop --paths` prints the config and cache locations.

## Performance

Charts are continuous lines drawn with box glyphs. They sample finer than
their unit and plot a rate: the 24h chart takes a reading every 15 minutes and
shows dollars per hour, the 14-day chart reads every 6 hours and shows dollars
per day. That keeps the shape fine grained while the axis stays in units you
recognise — so a peak can exceed any real hour's total, and the header names
it as a rate. Each chart has value labels at the top, midpoint and zero with a
sparse gridline, and a dashed rule at the mean of the non-idle samples.

The first launch reads every transcript once — about 5 seconds for 430 MB — and
caches per-file results. Per-message detail is pruned after `stats_retention_days`,
but per-month token sums are kept forever, so long-horizon totals survive
the prune (`summary["monthly"]`, not currently charted). After that only the new tail of each file is read, so
refreshes are effectively free. The cache lives in the platform cache directory
and can be deleted at any time; it will rebuild.

## The worktree board

Optional, and off unless you configure it. If you keep one git worktree per
repo per ticket, `claudetop-wt` prints them with branch, clean/dirty,
ahead/behind and PR state, and `claudetop-wt --json` prints the same for a
script or a Claude skill to read. There is no view for it inside the app — the
star map covers the same ground.

It expects `<worktree_root>/<ticket>/<repo>`, with the base clones those
worktrees hang off in `<worktree_base_dir>/<repo>`. Set `worktree_root` to turn
it on, `worktree_org` to get PR state from `gh`, and
`worktree_ticket_glob` (e.g. `"PROJ-*"`) if the root holds anything that is not
a ticket.

A fresh worktree is missing everything git does not track, so a repo can look
clean and still not build. Name what a build needs per repo and the board flags
the worktrees that do not have it. An entry is a path that must exist, or a
path that must contain a string — for the local-only edits that never get
committed:

```json
"worktree_bootstrap": {
  "my-rust-svc": [
    ".cargo/config.toml",
    { "path": "Cargo.toml", "contains": "gdal = \"0.17" }
  ]
}
```

Repos with a `package.json` are judged by `node_modules` with no config needed.
