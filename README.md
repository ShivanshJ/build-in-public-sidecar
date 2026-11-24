# build-in-public-sidecar

Ready-to-post X and LinkedIn drafts, written in your own voice from what you actually built.

build-in-public-sidecar (`code_gtm` on the command line) quietly saves your Claude Code sessions to a local database. A few times a day it reads what you worked on, searches your history for related earlier work (the same bug last month, how an idea evolved), and drafts posts. A page opens in your browser: edit a draft, click Post, and the X or LinkedIn composer opens prefilled. **Nothing is ever posted for you.**

```
Claude Code sessions ──► local database ──► drafts popup in your browser ──► you edit and post
                              ▲
              your past posts (so it sounds like you)
```

> Early-stage and tested on macOS. The X and LinkedIn account pulls haven't been verified against the live sites yet, see [Limitations](#limitations).

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://claude.com/claude-code) on your PATH. Drafts are written by `claude -p`, so there's no separate API key.
- Google Chrome, only if you want gtm to read your own X / LinkedIn posts (see step 4)
- macOS for the built-in scheduler. On Linux, use cron (see step 5).

## Get started

### 1. Install

```sh
git clone https://github.com/ShivanshJ/build-in-public-sidecar && cd build-in-public-sidecar
uv tool install ".[browser]"
```

This puts a `code_gtm` command on your PATH (run `uv tool update-shell` if your shell can't find it). The tool lives in its own environment, so there's nothing to activate, and the capture hook keeps working after you delete the clone. To update later: `git pull && uv tool install --reinstall ".[browser]"`, then `code_gtm setup` once more. (If you installed an earlier version, which was named `gtm`, run `uv tool uninstall gtm` once first, or both will try to provide `code_gtm`.)

### 2. See it work first (optional, installs nothing)

```sh
code_gtm demo
```

Runs the real thing once on your own Claude Code sessions and opens two pages: the drafts popup and a status dashboard. It lists your projects so you can **pick one** (or press Enter for all), then **asks before doing anything**, because a digest of your recent work goes to `claude -p` to write the drafts. Pick a project that isn't an employer repo. Everything lives in a throwaway folder it prints at the end, so it's safe to delete. The first run downloads the search models and can take a minute on a large history.

### 3. Turn it on

```sh
code_gtm setup
```

This adds a `Stop` and `SessionEnd` hook to `~/.claude/settings.json` (a backup is saved next to it) and backfills your existing sessions. From now on every session is saved automatically.

### 4. Teach it your voice

```sh
code_gtm login x            # offers to reuse the login from your everyday browser (or opens a window to sign in)
code_gtm login linkedin     # same
code_gtm sync               # pull your past posts and their likes/replies (lists each one as it's saved)
```

If you're already signed in to the site in your everyday browser (Arc, Chrome, Brave, Edge, Vivaldi, Firefox, Zen or Safari; your default browser is tried first), `login` offers to reuse that: say `y` and gtm copies only that site's cookies into its own browser profile. Your browser is never changed. Chromium browsers make macOS ask for your login password to unlock their key; Safari needs Full Disk Access for your terminal (System Settings > Privacy & Security), and `login` tells you if it's missing. Say `n`, or if the copy doesn't produce a working login, a Chrome window opens instead: sign in once there ("Continue with Google" works). Either way gtm never stores your password; its session lives in a dedicated profile, because browsers don't let other programs drive your everyday one. No Chrome, or don't want browser access? Import instead: `code_gtm import-x tweets.js` (your X archive) or `code_gtm import-posts posts.txt --platform linkedin`. With no past posts, drafts use a plain default style.

### 5. Draft one, then schedule it

```sh
code_gtm draft              # lists your 5 most recent sessions: pick some (1,3), m for more, or Enter for the last day; opens the popup
code_gtm schedule           # macOS: runs at 09:30, 13:30 and 18:30 every day
```

`code_gtm draft` makes two model calls, so it uses real tokens. The scheduled run only drafts when you've done something new (3+ prompts since the last draft), so quiet days cost nothing. On Linux, add a cron line instead:

```
30 9,13,18 * * *  GTM_TRIGGER=schedule /path/to/build-in-public-sidecar/.venv/bin/code_gtm tick
```

### 6. See what it wrote, and check it's working

```sh
code_gtm drafts             # every draft it has written, newest first, grouped by run
code_gtm drafts --web       # the same drafts as an editable page, with Copy and Post buttons
code_gtm status             # hook, scheduler, accounts, last result of every step
code_gtm status --web       # the same as a dashboard in your browser, with full history
```

Every draft is kept, so `code_gtm drafts` is your history of what the tool has produced, including drafts from days ago that you didn't get to post. After that you don't need to do anything: when there's something worth posting, the drafts page opens.

## Commands

**Run it**

| Command | What it does |
|---|---|
| `code_gtm draft [--about "<query>"] [--hours N] [--no-send]` | Draft now. In a terminal it first offers your 5 most recent sessions (`m` shows more, numbers like `1,3` pick them, Enter drafts from everything in the last day). `--hours N` or `--about` skip the picker; `--about` grounds drafts in a search; `--no-send` prints instead of opening the popup |
| `code_gtm tick [--force]` | What the scheduler runs: ingest, index, sync accounts, draft if there's new work. Prints a line per step |
| `code_gtm schedule [--remove]` | Install or remove the macOS launchd job |

**Look at things.** `status`, `log` and `drafts` all take `--web` to open the result in your browser instead of the terminal.

| Command | What it shows |
|---|---|
| `code_gtm status` | Is it working: hook, scheduler and the command it runs, accounts, last result of each step, model usage |
| `code_gtm log [--errors]` | Every step ever run: when, scheduled or manual, outcome, tokens and cost |
| `code_gtm drafts [-n N] [--web]` | **History of everything it has drafted**, newest first, grouped by run (default last 10). `--web` opens them as the editable popup so you can still copy or post an old one |
| `code_gtm inbox` | Reopen the latest drafts popup |

**Your posts**

| Command | What it does |
|---|---|
| `code_gtm login x\|linkedin` | Connect an account (reuse your browser's login, or sign in once in a window) |
| `code_gtm sync [x\|linkedin]` | Pull your latest posts and engagement now |
| `code_gtm learn` | Rewrite your style guide from your best posts (also runs daily on its own) |
| `code_gtm import-x <tweets.js>` / `code_gtm import-posts <file> --platform linkedin` | Import posts from an X archive or a text file (posts separated by `---`) |

**Search your own history**

| Command | What it does |
|---|---|
| `code_gtm search "<query>" [--fast]` | Search every session you've had. `--fast` skips the reranker |
| `code_gtm ls` / `code_gtm show <id>` | List sessions / print one (an id prefix is enough) |

**Housekeeping:** `code_gtm setup`, `code_gtm demo`, `code_gtm ingest` (re-scan transcripts), `code_gtm index` (embed anything new).

## Is it working?

Every step (ingest, each account sync, embed, draft, learn) records its outcome: when it ran, whether the scheduler or you started it, the command, the result, and the tokens and cost it used. Long steps show a live spinner with elapsed seconds and say what they're doing: which angles Claude found, each post `code_gtm sync` pulled (first sentence only, marked new or updated), and each draft written. Scheduled runs write the same lines, minus the spinner, to `~/.agent_sessions_gtm/tick.log`.

```
$ code_gtm status
capture hook : installed
scheduler    : loaded, runs `python -m gtm.cli tick` at 09:30, 13:30, 18:30
transcripts  : 18 sessions, 785 chunks, 785 embedded, newest 2h ago
x            : 42 past posts (browser, connected 3d ago; last pull 2h ago)
linkedin     : 0 past posts (not connected; last pull never)
drafts       : 12 total, last tick 2h ago, last draft 2h ago
model usage  : 24 calls, 1,480,000 tokens, $5.10 (all time, per `claude -p` reports)

last run of each step:
  sync_x       error  2h ago    SessionExpired: x session expired; run `code_gtm login x`  [last ok: 1d ago]
  draft        ok     2h ago    3 drafts from the last 24h  61,400 tok $0.21
```

A failing step doesn't stop the others: a broken LinkedIn pull still lets drafting run. Use `code_gtm log --errors` to see only failures.

<details>
<summary>Token usage</summary>

Drafting is two `claude -p` calls per run (find angles, write posts). Each call carries Claude Code's own fixed prompt overhead (about 22k input tokens even for a one-word prompt), so expect tens of thousands of tokens per run, mostly cached. Figures come from `claude -p --output-format json`; a custom `llm_cmd` counts as a call with unknown tokens.
</details>

<details>
<summary>Re-runs don't duplicate or re-download</summary>

Scheduled runs pull each account at most every `sync_hours` (default 20), so the three daily runs read your profile about once a day; `code_gtm sync` always pulls. A post is stored once, matched by its platform id (or by identical text for posts imported from a file), so syncing again only refreshes its likes and replies. Each pull re-reads your most recent ~20 posts, so older posts keep the numbers from when they were last seen. Transcript ingest is incremental.
</details>

## The drafts popup

Drafts open as a local HTML page (no server, no network). Each card is editable, shows a character count (X is flagged over 280), and has **Copy** and **Post** buttons. Post opens the X or LinkedIn composer with your text prefilled.

- Edits in the page aren't saved back. The database keeps the model's original text, so copy or post from the page.
- Closed the page without posting? Run `code_gtm drafts --web` to reopen recent drafts, or `code_gtm inbox` for the latest batch only.
- Every draft is saved in `gtm.db` and each batch as `~/.agent_sessions_gtm/drafts/<timestamp>.html`. A draft's `status` of `sent` means it was shown, not posted.
- Set `notify` to `stdout` to print drafts instead of opening the page.

**When your laptop was off or asleep:** launchd runs the job when the laptop wakes (merging missed runs); powered off means those runs are skipped. You won't get a pile of popups after a long absence: a lock stops overlapping runs, a minimum gap (`min_gap_hours`, default 3) skips back-to-back ones, and one run catches up on everything since the last draft (24 to 72 hours).

## How it works

- **Capture.** The hook reads the transcript path from Claude Code and ingests only new lines. It keeps user and assistant text, drops tool output and system reminders, redacts common secret patterns, and splits text into chunks of at most 1,200 characters (a chunk is one searchable piece of a message; a long prompt becomes several). A scheduled run only drafts when there are at least 3 new prompts since the last draft, and `status` says so when it skips. Standard library only, milliseconds per call, and it never fails your session.
- **Search.** SQLite FTS5 (BM25) plus `BAAI/bge-small-en-v1.5` embeddings (cosine in numpy), fused with reciprocal rank fusion, then the top 30 reranked by a cross-encoder (`Xenova/ms-marco-MiniLM-L-6-v2`). All local, CPU only.
- **Drafting (two model calls).** First the model reads a digest of recent work and proposes up to 4 post angles, each with a search query. Each query searches only sessions *older* than the digest window. Then the writer gets the recent digest, the retrieved history with dates, your style guide, your best and latest posts as examples, and your last drafts (so angles don't repeat). It's told not to invent numbers or streaks and to skip anything confidential; drafts are redacted again and X posts over 280 characters are dropped. Retrieval only searches saved session text, never your repos or files.
- **Your own rules.** Put drafting rules in plain text at `~/.agent_sessions_gtm/rules.md`. They go into every draft prompt, work with any model, and are never overwritten (unlike `style.md`, which is regenerated daily).
- **Learning your voice.** No fine-tuning. Posts are scored `likes + 2*reposts + 3*replies`; the best become few-shot examples and `style.md` is regenerated daily from them.
- **No loops.** Drafting runs with `GTM_DRAFTING=1`, which makes the hook exit, so the drafter never ingests itself.

## Configuration

Settings live in `~/.agent_sessions_gtm/config.json` (mode 600). Any key can be overridden with an environment variable `GTM_<KEY>`; `GTM_HOME` moves the whole data folder.

| Key | Default | Purpose |
|---|---|---|
| `notify` | `popup` | `popup` or `stdout` |
| `hours` | `9,13,18` | Hours (local time, at :30) the scheduler runs. Re-run `code_gtm schedule` after changing |
| `min_gap_hours` | `3` | Minimum hours between ticks |
| `sync_hours` | `20` | Minimum hours between scheduled pulls of each account |
| `llm_cmd` | `claude -p --no-session-persistence` | Any command that reads a prompt on stdin and prints the answer |
| `model` | Claude Code default | Passed as `--model` |
| `x_username`, `x_bearer_token` | none | Read X through its API instead of the browser (pay-per-use, check current pricing) |

## Privacy

- Transcripts stay in `~/.agent_sessions_gtm/gtm.db`, outside any repo, so they can't be committed by accident. Redaction is pattern-based and best effort, not a guarantee.
- A digest of recent work, plus excerpts of related older sessions, is sent to the model behind `claude -p` to write drafts. That includes work in employer repos if you use Claude Code there. **Review drafts for confidential details before posting.**
- Drafts and the popup stay on your machine apart from that model call.
- Reading your accounts through the browser keeps cookies in `~/.agent_sessions_gtm/browser`; treat that folder like a credential. If you let `login` reuse your browser's login, that browser's cookies for that one site (never other sites) are read once and copied there.

## Limitations

- **Account pulls are unverified.** The X and LinkedIn extraction logic is unit-tested and headless Chrome launches against a local test page, but the page selectors haven't been checked against the real sites, and those sites change without notice. This is also scraping, which both sites' terms restrict; reading only your own profile a few times a day is low volume but carries some account risk. If a pull fails, `code_gtm status` shows the error and a screenshot is saved to `~/.agent_sessions_gtm/debug/`. The file imports are the fallback.
- **Login reuse is macOS-only and isn't verified end to end against real browsers** (it is unit-tested on synthetic cookie files; the Safari binary format in particular is parsed from documentation, not yet checked against a real Safari file). X or LinkedIn may also ask you to verify a login that suddenly appears in a second browser; if so, the separate-window sign-in is the fallback.
- LinkedIn has no API for reading your own posts, so the browser is the only automatic route there.
- The popup layout is checked, but its Copy and Post buttons haven't been clicked through in a browser. LinkedIn's prefill link may not behave consistently.
- The launchd job and X API sync haven't been tested against live services. The scheduler only fires while the laptop is awake.
- Drafts can contain mistakes or outdated claims. Always read before posting.

## Uninstall

```sh
code_gtm schedule --remove
```

Then remove the `gtm.hook` entries under `Stop` and `SessionEnd` in `~/.claude/settings.json` (or restore `settings.json.bak-gtm`), and delete `~/.agent_sessions_gtm` to remove all data.

## Development

```sh
uv pip install -e ".[dev]" && python -m pytest
```
