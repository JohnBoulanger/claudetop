# claudetop

A live terminal dashboard for Claude Code: what is running right now, what it
is costing, and how close you are to your subscription limits.

Read-only. It watches the files Claude Code already writes on your machine and
makes one HTTPS call to Anthropic's own usage endpoint. It never starts, stops,
or writes to a session.

```
✻ Claude Sessions
6 sessions · polling ~/.claude

  Status         Name              Kind          Branch   Cost      Uptime   PID
  ⠹ Marinating…  EVA-14            background    HEAD     $398.49   92h31m   48848
  ○ idle         reviewer          interactive   HEAD     $5.10     1h37m    38000
```

## Views

The home view is the usage page: limits, spend and what is running right now.
Every other view is a toggle — the same key, or `esc`, brings you home.

| Key | View | What it shows |
|-----|------|---------------|
| — | Home | 5h / 7d limit gauges with reset countdown and projection, spend by window, burn rate, 24h and 14d spend charts, and the live session strip. |
| `s` | Sessions | Every live session and background job, with status, branch, cost and uptime. |
| `t` | Tree | Sessions with their background jobs nested underneath. Collapse with `←`. |
| `w` | Worktrees | Ticket worktrees: branch, clean/dirty, ahead/behind, PR state. Needs `worktree_root` in the config. |
| `a` | Analytics | Activity counters, cache hit rate, cost by model, cost by repo, most-used tools, and today's most expensive sessions. |
| `m` | Star map | Sessions drawn as a constellation, each with its background jobs orbiting it. |
| `f` | — | Focus the selected session's terminal window. |
| `h` | — | Hide every dollar figure, for screen sharing. |
| `r` | — | Force a refresh. |
| `T` | — | Cycle the color preset (applies on next launch). |
| `q` | — | Quit. |

A compact 5h / 7d gauge strip sits under the title in every view, so you never
have to leave a view to check where you are against the limits.

## The sky

The starfield fills the background of every view, and it is not just
decoration — it is a second, ambient read on the same data:

| What you see | What it means |
|--------------|---------------|
| Stars drifting | Live burn rate. An idle machine has a still field; several busy sessions pull it into visible motion. |
| A meteor | An event just happened — green for a background job finishing, red for a session that needs your input, terracotta for a new prompt. |
| The comet | Your 5h limit window. It crosses the sky once per window, so its position is your time to reset. |
| The tide along the bottom | 5h utilization. It rises as you spend and turns red past 90%. |

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

Works on Windows and macOS. Focusing a session window (`f`) uses pywin32 on
Windows and AppleScript against Terminal / iTerm2 on macOS; everywhere else the
key just reports that focusing is unavailable and the rest of the app works
normally.

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
  "theme": { "preset": "espresso", "accent": "#da7756" },
  "worktree_root": "C:/eva-wt",
  "worktree_base_dir": "C:/Users/you/turing-analytics",
  "worktree_org": "Your-Org",
  "usage_api": true,
  "usage_poll_seconds": 60,
  "stats_retention_days": 31,
  "hide_costs": false
}
```

Presets: `espresso` (default, warm on black), `espresso-warm`, `midnight`,
`gruvbox`. Any single color in a preset can be overridden by name — `bg`,
`panel`, `text`, `dim`, `faint`, `accent`, `red`, `green`, `yellow`, `border`,
`star`. Set `hide_costs` to blank out every dollar figure for screen sharing.

`claudetop --paths` prints the config and cache locations.

## Performance

The first launch reads every transcript once — about 4 seconds for 430 MB — and
caches per-file results. After that only the new tail of each file is read, so
refreshes are effectively free. The cache lives in the platform cache directory
and can be deleted at any time; it will rebuild.

## The worktree board on its own

`claudetop-wt --json` prints the same worktree data as the `w` view, for use in
scripts and Claude skills. `claudetop-wt` with no arguments prints a table.
