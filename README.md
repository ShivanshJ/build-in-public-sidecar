# build-in-public-sidecar

Ready-to-post X and LinkedIn drafts, written in your own voice from what you actually built.

`build-in-public-sidecar` (the command is `code_gtm`) quietly saves your Claude Code sessions to a local database. A few times a day it reads what you worked on, searches your history for related earlier work (the same bug last month, how an idea evolved), and drafts posts. A page opens in your browser: edit a draft, click Post, and the X or LinkedIn composer opens prefilled. **Nothing is ever posted for you.**

```
Claude Code sessions ──► local database ──► drafts popup in your browser ──► you edit and post
                              ▲
              your past posts (so it sounds like you)
```

> Early-stage, tested on macOS. The X and LinkedIn account pulls haven't been verified against the live sites yet, see [Limitations](docs/reference.md#limitations).

**Requirements:** Python 3.11+, [uv](https://docs.astral.sh/uv/), and [Claude Code](https://claude.com/claude-code) on your PATH (drafts are written by `claude -p`, so there's no separate API key). Google Chrome only if you want it to read your own X / LinkedIn posts. macOS for the built-in scheduler (Linux: cron, see the [reference](docs/reference.md#scheduling-on-linux)).

## Get started

**1. Install**

```sh
uv tool install "build-in-public-sidecar[browser] @ git+https://github.com/ShivanshJ/build-in-public-sidecar"
```

**2. See it work first (optional, installs nothing)**

```sh
code_gtm demo
```

A digest of your recent work goes to `claude -p` to write sample drafts. Pick a project that isn't an employer repo. Everything lives in a throwaway folder it prints at the end, so it's safe to delete.

**3. Turn it on**

```sh
code_gtm setup
```

Adds a `Stop` and `SessionEnd` hook to `~/.claude/settings.json` (a backup is saved next to it) and backfills your existing sessions.

**4. Teach it your voice**

```sh
code_gtm login x            # reuse your everyday browser's login, or sign in once in a window
code_gtm login linkedin     # same
code_gtm sync               # pull your past posts and their likes/replies
```

Skip this and drafts use a plain default style. No Chrome, or don't want browser access? Import instead: `code_gtm import-x tweets.js` or `code_gtm import-posts posts.txt --platform linkedin`. Details on how login reuse works: [reference](docs/reference.md#connecting-x-and-linkedin).

**5. Draft one, then schedule it**

```sh
code_gtm draft              # pick from your 5 most recent sessions (1,3 · m for more · Enter for the last day), then the popup opens
code_gtm schedule           # macOS: runs at 09:30, 13:30 and 18:30 every day
```

Each draft run makes 2 model calls, so it uses real tokens. `code_gtm draft --show-prompt` prints exactly what the model would be sent, without calling it.

**6. See what it wrote, and check it's working**

```sh
code_gtm drafts             # everything it has drafted, newest first (--web reopens the popup)
code_gtm status             # hook, scheduler, accounts, last result of every step (--web for a dashboard)
```

## How it runs (for coding agents and the curious)

Two things run without you:

- **The capture hook** (`python -m gtm.hook`, installed by `code_gtm setup`) fires when a Claude Code turn or session ends. It reads the transcript path Claude gives it, saves only the new text (user and assistant messages, with secrets redacted) to `~/.agent_sessions_gtm/gtm.db`, and never fails your session.
- **The scheduler** (`code_gtm schedule`, a macOS launchd job at 09:30, 13:30 and 18:30) runs **`code_gtm tick`**, the whole pipeline in one command. You can run it yourself any time.

**`code_gtm tick`**, in order:

1. Skips if another tick is running (a lock file), or if the last tick was under 3 hours ago (`min_gap_hours`).
2. **Ingest** any transcript text the hook missed.
3. **Sync** your X and LinkedIn posts, at most once every 20 hours per site (`sync_hours`).
4. **Embed** new text so it's searchable.
5. **Draft**, but only if there are at least 3 new Claude Code prompts since the last draft. Otherwise it records "skip" with the reason. After a long absence it looks back further (24 to 72 hours) in one run instead of sending a pile of popups.
6. **Learn** your voice from your best posts, once a day, after a draft succeeds.

`code_gtm tick --force` ignores the 3-hour gap. Each step writes its result (ok, skip or error, with the reason, tokens and cost) to the database. Read them with `code_gtm status` or `code_gtm log`. Scheduled runs also append to `~/.agent_sessions_gtm/tick.log`.

**Where things live**

| What | Where |
|---|---|
| Database (sessions, posts, drafts, run history) | `~/.agent_sessions_gtm/gtm.db` |
| Settings | `~/.agent_sessions_gtm/config.json` (see the [reference](docs/reference.md#configuration)) |
| Drafts popup pages | `~/.agent_sessions_gtm/drafts/` |
| Browser profile for X / LinkedIn pulls | `~/.agent_sessions_gtm/browser` (treat like a credential) |
| Your learned voice, rewritten daily | `~/.agent_sessions_gtm/style.md` |
| Your extra posting rules, never rewritten | `~/.agent_sessions_gtm/rules.md` |
| The shipped posting guide | `gtm/guides/post_guide.md` |

**If it isn't running:** `code_gtm status` shows whether the hook and scheduler are installed and the last result of every step. After moving or reinstalling the tool, run `code_gtm setup` and `code_gtm schedule` again, because both bake in the path of the Python that ran them.

## Your posting rules

The guide that shapes every draft is [`gtm/guides/post_guide.md`](gtm/guides/post_guide.md). Edit it, or add personal rules in `~/.agent_sessions_gtm/rules.md`. Your learned voice (`code_gtm learn`) is kept separately.

## More

[Reference](docs/reference.md): every command, how it works, configuration, privacy, limitations, updating and uninstalling.

Uninstall: `code_gtm schedule --remove`, delete the `gtm.hook` entries in `~/.claude/settings.json`, and delete `~/.agent_sessions_gtm`.
