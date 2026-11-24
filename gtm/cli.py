import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .db import cfg, connect, home
from .status import PLIST

HOOK_EVENTS = ("Stop", "SessionEnd")


def _short(s: str | None, n: int = 90) -> str:
    return " ".join((s or "").split())[:n]


def cmd_ls(a, conn):
    rows = conn.execute(
        """SELECT s.session_id, s.project, s.first_ts,
             (SELECT count(*) FROM chunks c WHERE c.session_id = s.session_id) AS n,
             (SELECT text FROM chunks c WHERE c.session_id = s.session_id AND role='user' ORDER BY id LIMIT 1) AS first
           FROM sessions s ORDER BY s.last_ts DESC LIMIT ?""",
        (a.limit,),
    )
    for r in rows:
        proj = (r["project"] or "?").rstrip("/").split("/")[-1]
        print(f"{r['session_id'][:8]}  {(r['first_ts'] or '')[:16]}  {proj:<22} {r['n']:>4}  {_short(r['first'])}")


def cmd_show(a, conn):
    rows = conn.execute(
        "SELECT role, text, ts FROM chunks WHERE session_id LIKE ? ORDER BY id", (a.session + "%",)
    ).fetchall()
    if not rows:
        sys.exit("no such session")
    for r in rows:
        print(f"\n[{r['role']}] {r['ts']}\n{r['text']}")


def cmd_search(a, conn):
    from . import search

    search.embed_pending(conn)
    for h in search.search(conn, a.query, k=a.k, rerank=not a.fast):
        print(f"{h['session_id'][:8]}  {(h['ts'] or '')[:10]}  [{h['role']}]  {_short(h['text'], 160)}")


def cmd_ingest(a, conn):
    from .ingest import ingest_all

    print(f"{ingest_all(conn)} new chunks")


def cmd_index(a, conn):
    from . import search

    print(f"embedded {search.embed_pending(conn)} chunks")


def cmd_import_x(a, conn):
    from .x_posts import import_archive

    print(f"imported {import_archive(conn, a.tweets_js)} posts")


def cmd_import_posts(a, conn):
    from .x_posts import import_text

    print(f"imported {import_text(conn, a.file, a.platform)} posts")


def cmd_sync(a, conn):
    from .browser import SITES, sync_site
    from .db import record

    for site in [a.site] if a.site else SITES:
        with record(conn, f"sync_{site}", swallow=True) as r:
            sync_site(conn, site, r, force=True)


def cmd_login(a, conn):
    from .browser import login

    login(conn, a.site)
    print(f"{a.site} connected. Run `code_gtm sync {a.site}` to pull your posts.")


def cmd_learn(a, conn):
    from .draft import learn

    print(learn(conn) or "no posts imported yet; run import-x / import-posts / sync first")


def _pick_sessions(conn, first: int = 5, page: int = 10) -> list[str] | None:
    """Offer the most recent sessions; returns the chosen session ids, or None for 'all recent work'."""
    import re

    from . import ui
    from .draft import recent_sessions
    from .status import when

    rows = recent_sessions(conn)
    if not rows:
        return None
    shown = 0

    def show(n):
        nonlocal shown
        for i, r in enumerate(rows[shown:shown + n], shown + 1):
            proj = (r["project"] or "?").rstrip("/").split("/")[-1]
            n_p = r["prompts"]
            when_ = when(r["last_ts"]) if r["last_ts"] else ""
            print(f"  {i:>2}. {proj:<22} {when_:<16} {n_p:>3} prompt{'' if n_p == 1 else 's'}  {ui.dim(ui.snippet(r['first'], 60, 0))}")
        shown = min(shown + n, len(rows))

    print("Recent sessions:")
    show(first)
    while True:
        more = f" m = {len(rows) - shown} more," if shown < len(rows) else ""
        ans = input(f"\nDraft from which? Numbers like 1,3 ·{more} Enter = all work from the last 24h: ").strip().lower()
        if not ans:
            return None
        if ans == "m" and shown < len(rows):
            show(page)
            continue
        try:
            nums = [int(x) for x in re.split(r"[,\s]+", ans)]
        except ValueError:
            nums = []
        if nums and all(1 <= n <= shown for n in nums):
            return [rows[n - 1]["session_id"] for n in dict.fromkeys(nums)]
        print("Type numbers from the list (like 1,3)" + (", or m for more." if shown < len(rows) else "."))


def cmd_draft(a, conn):
    from . import search, ui
    from .draft import draft

    sessions = None
    if sys.stdin.isatty() and not a.about and a.hours is None:
        sessions = _pick_sessions(conn)
    with ui.working("building the search index"):
        search.embed_pending(conn)
    drafts = draft(conn, about=a.about, hours=a.hours or 24, send=not a.no_send, sessions=sessions, show_prompt=a.show_prompt)
    if a.show_prompt:
        return
    if a.no_send:
        for d in drafts:
            print(f"--- [{d['platform']}] {d['why']}\n{d['text']}\n")
    print(f"{len(drafts)} drafts")


def cmd_tick(a, conn):
    from .draft import tick

    print(tick(conn, force=a.force))


def cmd_inbox(a, conn):
    from . import notify

    if not notify.open_latest():
        print("no drafts yet; run `code_gtm draft` or wait for the next tick")


def cmd_drafts(a, conn):
    import textwrap

    from . import notify
    from .status import when

    rows = conn.execute("SELECT * FROM drafts ORDER BY id DESC LIMIT ?", (a.limit,)).fetchall()
    if not rows:
        print("No drafts yet. Run `code_gtm draft`, or wait for the next scheduled run.")
        return
    if a.web:
        notify.popup([dict(r) for r in reversed(rows)])
        return
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    bold = (lambda t: f"\033[1m{t}\033[0m") if color else (lambda t: t)
    dim = (lambda t: f"\033[2m{t}\033[0m") if color else (lambda t: t)
    width = min(shutil.get_terminal_size((100, 20)).columns, 100) - 4
    batches: dict[str, list] = {}
    for r in rows:
        batches.setdefault(r["created_at"][:16], []).append(r)
    for batch in batches.values():
        print(bold(f"{when(batch[0]['created_at'])}  ·  {len(batch)} draft{'s' if len(batch) != 1 else ''}"))
        for r in sorted(batch, key=lambda r: r["id"]):
            n, x = len(r["text"]), r["platform"] == "x"
            size = f"{n}/280" if x else f"{n} chars"
            print(f"\n  {bold('#' + str(r['id']))}  {'X' if x else 'LinkedIn'}  {dim('· ' + size)}")
            for para in r["text"].split("\n"):
                print(textwrap.fill(para, width, initial_indent="    ", subsequent_indent="    ") if para.strip() else "")
            if r["why"]:
                print(dim(textwrap.fill("why: " + r["why"], width, initial_indent="    ", subsequent_indent="         ")))
        print()
    print(dim("Open these to edit and post: code_gtm drafts --web   ·   Show more: code_gtm drafts -n 20"))


def cmd_status(a, conn):
    from . import status

    print(status.render_text(status.collect(conn)))
    if a.web:
        print(f"opened {status.open_dashboard(conn)}")


def cmd_log(a, conn):
    from . import status
    from .status import _tokens, local

    if a.web:
        print(f"opened {status.open_dashboard(conn)}")
        return

    where = "WHERE status='error'" if a.errors else ""
    for r in conn.execute(f"SELECT * FROM runs {where} ORDER BY id DESC LIMIT ?", (a.limit,)):
        print(f"{local(r['started_at'])}  {(r['trigger'] or '?'):<8} {r['step']:<13} {r['status']:<6} "
              f"{_short(r['detail'], 100)}  {_tokens(r)}")


def cmd_demo(a, conn):
    """Run the real pipeline once on your own Claude Code sessions, inside a throwaway folder.

    main() points GTM_HOME at a temp dir before connecting, so nothing is installed and your real gtm data
    (if any) is untouched. Your sessions are only read from ~/.claude.
    """
    from collections import Counter
    from datetime import datetime, timedelta, timezone

    from . import search, status, ui
    from .db import record
    from .draft import draft
    from .ingest import ingest_file

    root = Path.home() / ".claude" / "projects"
    all_files = list(root.glob("*/*.jsonl"))
    if not all_files:
        sys.exit("No Claude Code sessions found in ~/.claude/projects. Use Claude Code for a bit, then run `code_gtm demo`.")
    strip = str(Path.home()).replace("/", "-") + "-"
    short = lambda f: f.parent.name.removeprefix(strip)
    counts = Counter(short(f) for f in all_files)
    interactive = sys.stdin.isatty() and not a.yes

    choice = ""
    if interactive:
        listed = counts.most_common(20)
        print(f"Found {len(all_files)} sessions in {len(counts)} projects:")
        for i, (name, n) in enumerate(listed, 1):
            print(f"  {i:>2}. {name}  ({n} sessions)")
        if len(counts) > len(listed):
            print(f"      ... and {len(counts) - len(listed)} more (type part of a name to pick one)")
        choice = input("\nDemo which project? Number or part of a name, Enter for all: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(listed):
            choice = listed[int(choice) - 1][0]
    files = all_files
    if choice:
        files = [f for f in all_files if choice.lower() in short(f).lower()]
        if not files:
            sys.exit(f"No project matches '{choice}'.")
    picked = Counter(short(f) for f in files)
    if not a.yes:
        print(f"\nWill read {len(files)} sessions from:")
        for name, n in picked.most_common(8):
            print(f"  {n:>3}  {name}")
        if len(picked) > 8:
            print(f"      ... and {len(picked) - 8} more projects")
        print("\nThis copies them into a throwaway folder, then sends a digest of your latest prompts and replies,\n"
              "plus excerpts of related older sessions (secrets redacted by pattern), to `claude -p` to write drafts.\n"
              "Nothing is installed and nothing is posted.")
        if not sys.stdin.isatty() or input("Continue? [y/N] ").strip().lower() != "y":
            sys.exit("Cancelled. Nothing was read or sent.")

    print("\nrunning the real pipeline on your sessions (first run downloads the search models):\n")
    with record(conn, "ingest", swallow=True) as r:
        total = 0
        for name in picked:
            n = sum(ingest_file(conn, f) for f in files if short(f) == name)
            total += n
            ui.say(f"  {name}: {n} messages")
        r.detail = f"{total} new chunks"
    with record(conn, "embed", swallow=True) as r:
        r.detail = f"{search.embed_pending(conn)} chunks"
    newest = conn.execute("SELECT max(ts) FROM chunks WHERE role='user'").fetchone()[0]
    age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(newest.replace("Z", "+00:00"))).total_seconds() / 3600 \
        if newest else 0
    hours = 24 if age_h < 24 else int(age_h) + 24
    with record(conn, "draft", swallow=True) as r:
        drafts = draft(conn, hours=hours)
        r.detail = f"{len(drafts)} drafts from the last {hours}h"
    path = status.open_dashboard(conn, banner="Demo run on your real ~/.claude sessions, stored in a throwaway folder.")
    print(f"\nOpened the drafts popup and the status dashboard. Demo data lives in {home()}\n"
          f"(delete it any time: rm -rf {home()}). To run this for real: code_gtm setup, code_gtm login x, code_gtm schedule.")


def install_hook(settings: Path | None = None) -> bool:
    """Add (or repoint) our Stop/SessionEnd hooks. Returns True if settings.json changed."""
    settings = settings or Path.home() / ".claude" / "settings.json"
    data = json.loads(settings.read_text()) if settings.exists() else {}
    command = f"{sys.executable} -m gtm.hook"
    hooks = data.setdefault("hooks", {})
    changed = False
    for ev in HOOK_EVENTS:
        entries = hooks.setdefault(ev, [])
        ours = [h for e in entries for h in e.get("hooks", []) if "gtm.hook" in h.get("command", "")]
        if not ours:
            entries.append({"hooks": [{"type": "command", "command": command, "timeout": 15}]})
            changed = True
        for h in ours:
            if h["command"] != command:
                h["command"], changed = command, True
    if changed:
        if settings.exists():
            shutil.copy(settings, settings.with_name(settings.name + ".bak-gtm"))
        settings.write_text(json.dumps(data, indent=2))
    return changed


def cmd_setup(a, conn):
    from .ingest import ingest_all

    print(f"hook {'installed' if install_hook() else 'already installed'} in ~/.claude/settings.json")
    print(f"backfilled {ingest_all(conn)} chunks from existing transcripts")
    print("\nNext: code_gtm demo   |   code_gtm login x   |   code_gtm login linkedin   |   code_gtm schedule   |   code_gtm status")


def cmd_schedule(a, conn):
    uid = os.getuid()
    if a.remove:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(PLIST)], check=False)
        PLIST.unlink(missing_ok=True)
        print("schedule removed")
        return
    hours = [int(h) for h in str(cfg("hours", "9,13,18")).split(",")]
    plist = {
        "Label": "com.gtm.tick",
        "ProgramArguments": [sys.executable, "-m", "gtm.cli", "tick"],
        "StartCalendarInterval": [{"Hour": h, "Minute": 30} for h in hours],
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "GTM_TRIGGER": "schedule"},
        "StandardOutPath": str(home() / "tick.log"),
        "StandardErrorPath": str(home() / "tick.log"),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(PLIST)], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], check=True)
    print(f"scheduled: tick at {', '.join(f'{h}:30' for h in hours)} daily (laptop must be awake). log: ~/.agent_sessions_gtm/tick.log")


def main(argv=None):
    p = argparse.ArgumentParser(prog="code_gtm", description="Capture Claude Code transcripts, search them, draft posts.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("setup", cmd_setup, "one-time: install hook, backfill transcripts, create phone topic")
    add("schedule", cmd_schedule, "run `tick` 3x/day via launchd").add_argument("--remove", action="store_true")
    add("status", cmd_status, "is it working? hook, scheduler, last pulls and drafts, token usage").add_argument(
        "--web", action="store_true", help="also open the dashboard in your browser")
    sp = add("ls", cmd_ls, "list sessions"); sp.add_argument("-n", "--limit", type=int, default=20)
    add("show", cmd_show, "print one session").add_argument("session")
    sp = add("search", cmd_search, "BM25 + cosine + cross-encoder rerank")
    sp.add_argument("query"); sp.add_argument("-k", type=int, default=8)
    sp.add_argument("--fast", action="store_true", help="skip the cross-encoder")
    add("ingest", cmd_ingest, "backfill all ~/.claude transcripts")
    add("index", cmd_index, "embed any un-embedded chunks")
    add("import-x", cmd_import_x, "import posts from X archive tweets.js").add_argument("tweets_js")
    sp = add("import-posts", cmd_import_posts, "import posts from a text file ('---' separated)")
    sp.add_argument("file"); sp.add_argument("--platform", choices=["x", "linkedin"], default="linkedin")
    sp = add("sync", cmd_sync, "pull your latest X / LinkedIn posts + metrics")
    sp.add_argument("site", nargs="?", choices=["x", "linkedin"])
    add("login", cmd_login, "connect X or LinkedIn: sign in once in a browser window").add_argument("site", choices=["x", "linkedin"])
    sp = add("log", cmd_log, "history of every pipeline step (pulls, drafts, errors)")
    sp.add_argument("-n", "--limit", type=int, default=30); sp.add_argument("--errors", action="store_true")
    sp.add_argument("--web", action="store_true", help="open the dashboard instead of printing")
    add("learn", cmd_learn, "rewrite style.md from your best posts")
    sp = add("draft", cmd_draft, "draft posts now")
    sp.add_argument("--about", help="ground drafts in a search query instead of the last day")
    sp.add_argument("--hours", type=int, help="draft from the last N hours (skips the session picker)")
    sp.add_argument("--no-send", action="store_true", help="print instead of showing the popup")
    sp.add_argument("--show-prompt", action="store_true", help="print the full prompt the model would get, without calling it")
    add("tick", cmd_tick, "scheduled job: ingest, index, sync, draft, popup").add_argument(
        "--force", action="store_true", help="ignore the minimum gap since the last tick")
    add("inbox", cmd_inbox, "reopen the latest drafts popup")
    sp = add("drafts", cmd_drafts, "list recent drafts saved in the database")
    sp.add_argument("-n", "--limit", type=int, default=10)
    sp.add_argument("--web", action="store_true", help="open them as an editable page instead of printing")
    sp = add("demo", cmd_demo, "try it on your real Claude Code sessions in a throwaway folder (installs nothing)")
    sp.add_argument("-y", "--yes", action="store_true", help="skip the prompts and use all projects")

    a = p.parse_args(argv)
    if a.fn is cmd_demo:
        os.environ["GTM_HOME"] = tempfile.mkdtemp(prefix="gtm-demo-")
    a.fn(a, connect())


if __name__ == "__main__":
    main()
