"""Answer "is it working?": hook, scheduler, accounts, and every pipeline step with its tokens and trigger."""
import html
import os
import plistlib
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from .db import cfg, home, hours_since, kv_get

PLIST = Path.home() / "Library/LaunchAgents/com.gtm.tick.plist"


def ago(ts: str | None) -> str:
    h = hours_since(ts)
    if h is None:
        return "never"
    for unit, n in (("d", 24), ("h", 1), ("m", 1 / 60)):
        if h >= n:
            return f"{int(h // n)}{unit} ago"
    return "just now"


def _dt(ts: str) -> datetime:
    return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone()


def local(ts: str) -> str:
    return _dt(ts).strftime("%m-%d %H:%M")


def when(ts: str) -> str:
    """'Today 20:00', 'Yesterday 09:30', 'Nov 20 18:30'."""
    dt = _dt(ts)
    days = (datetime.now().astimezone().date() - dt.date()).days
    return f"{'Today' if days == 0 else 'Yesterday' if days == 1 else dt.strftime('%b %d')} {dt:%H:%M}"


def hook_installed() -> bool:
    f = Path.home() / ".claude" / "settings.json"
    return f.exists() and "gtm.hook" in f.read_text()


def schedule_info() -> dict:
    if not PLIST.exists():
        return {"loaded": False, "command": "", "times": ""}
    plist = plistlib.loads(PLIST.read_bytes())
    loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.gtm.tick"], capture_output=True).returncode == 0
    times = ", ".join(f"{e['Hour']:02d}:{e['Minute']:02d}" for e in plist.get("StartCalendarInterval", []))
    return {"loaded": loaded, "command": " ".join(plist.get("ProgramArguments", [])), "times": times}


def collect(conn, recent: int = 40) -> dict:
    q = lambda s: conn.execute(s).fetchone()[0]
    accounts = []
    for site in ("x", "linkedin"):
        api = site == "x" and cfg("x_bearer_token") and cfg("x_username")
        login = kv_get(conn, "login_" + site)
        how = "X API" if api else f"browser, connected {ago(login)}" if login else "not connected"
        accounts.append((site, q(f"select count(*) from my_posts where platform='{site}'"), how, bool(api or login),
                         ago(kv_get(conn, "last_sync_" + site))))
    totals = conn.execute(
        """SELECT coalesce(sum(llm_calls),0) calls, coalesce(sum(tokens_in),0) tin, coalesce(sum(tokens_out),0) tout, coalesce(sum(cost_usd),0) cost
           FROM runs WHERE step != 'tick' AND llm_calls > 0"""
    ).fetchone()
    return {
        "home": str(home()), "notify": cfg("notify", "popup"), "hook": hook_installed(), "schedule": schedule_info(),
        "transcripts": (q("select count(*) from sessions"), q("select count(*) from chunks"),
                        q("select count(*) from chunks where emb is not null"), ago(q("select max(ts) from chunks"))),
        "accounts": accounts,
        "drafts": (q("select count(*) from drafts"), ago(kv_get(conn, "last_tick_ts")), ago(kv_get(conn, "last_draft_ts"))),
        "latest": conn.execute("SELECT * FROM runs WHERE id IN (SELECT max(id) FROM runs GROUP BY step) ORDER BY id").fetchall(),
        "recent": conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (recent,)).fetchall(),
        "totals": dict(totals),
        "last_ok": {r["step"]: r["t"] for r in conn.execute("SELECT step, max(finished_at) t FROM runs WHERE status='ok' GROUP BY step")},
    }


def _tokens(r) -> str:
    return f"{r['tokens_in'] + r['tokens_out']:,} tok ${r['cost_usd']:.2f}" if r["llm_calls"] else ""


def render_text(d: dict) -> str:
    sched = d["schedule"]
    out = [f"home: {d['home']}  notify: {d['notify']}",
           f"capture hook : {'installed' if d['hook'] else 'NOT installed (run `code_gtm setup`)'}",
           f"scheduler    : {('loaded, runs `' + sched['command'] + '` at ' + sched['times']) if sched['loaded'] else 'NOT loaded (run `code_gtm schedule`)'}",
           "transcripts  : {} sessions, {} chunks, {} embedded, newest {}".format(*d["transcripts"])]
    for site, n, how, _, synced in d["accounts"]:
        out.append(f"{site:<13}: {n} past posts ({how}; last pull {synced})")
    out.append("drafts       : {} total, last tick {}, last draft {}".format(*d["drafts"]))
    t = d["totals"]
    out.append(f"model usage  : {t['calls']} calls, {t['tin'] + t['tout']:,} tokens, ${t['cost']:.2f} (all time, per `claude -p` reports)")
    if d["latest"]:
        out.append("\nlast run of each step (`code_gtm log` for history, `code_gtm status --web` for the dashboard):")
    for r in d["latest"]:
        detail = " ".join((r["detail"] or "").split())
        err = r["status"] == "error"
        extra = f"  [last ok: {ago(d['last_ok'].get(r['step']))}]" if err else ""
        out.append(f"  {r['step']:<13} {r['status']:<6} {ago(r['finished_at']):<9} {detail[:90] if not err else ''}  {_tokens(r)}{extra}".rstrip())
        if err:
            out.append(textwrap.fill(detail, 100, initial_indent="      why: ", subsequent_indent="           "))
    return "\n".join(out)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>code_gtm status</title><style>
body{font:14px/1.5 -apple-system,system-ui,sans-serif;background:#f6f6f4;color:#1a1a1a;margin:0;padding:32px 16px}
main{max-width:1000px;margin:0 auto} h1{font-size:20px;margin:0 0 16px} h2{font-size:14px;margin:24px 0 8px;color:#555}
.card{background:#fff;border:1px solid #e3e3df;border-radius:12px;padding:6px 16px;margin-bottom:8px}
.kv{display:flex;gap:12px;padding:6px 0;border-bottom:1px solid #f0f0ec}.kv:last-child{border:0}.k{width:130px;color:#777;flex:none}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e3df;border-radius:12px;overflow:hidden;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #f0f0ec;vertical-align:top}th{color:#777;font-weight:600;background:#fafaf8}
.b{font-size:11px;font-weight:600;padding:1px 8px;border-radius:99px;color:#fff}.ok{background:#2e8b57}.skip{background:#999}.error{background:#c0392b}
.warn{color:#c0392b;font-weight:600}.banner{background:#fff3cd;border:1px solid #e8d48a;border-radius:8px;padding:8px 12px;margin-bottom:16px}
code{background:#f0f0ec;padding:1px 5px;border-radius:4px}.num{white-space:nowrap}
</style></head><body><main>__BANNER__<h1>code_gtm status <span style="color:#777;font-weight:400">· __NOW__</span></h1>__BODY__</main></body></html>"""


def render_html(d: dict, banner: str = "") -> str:
    e = html.escape
    sched = d["schedule"]
    kv = lambda k, v: f'<div class="kv"><div class="k">{e(k)}</div><div>{v}</div></div>'
    flag = lambda ok, good, bad: e(good) if ok else f'<span class="warn">{e(bad)}</span>'
    rows = [
        kv("capture hook", flag(d["hook"], "installed", "NOT installed: run code_gtm setup")),
        kv("scheduler", (f"loaded: runs <code>{e(sched['command'])}</code> at {e(sched['times'])}" if sched["loaded"]
                         else '<span class="warn">NOT loaded: run code_gtm schedule</span>')),
        kv("transcripts", e("{} sessions, {} chunks, {} embedded, newest {}".format(*d["transcripts"]))),
    ]
    for site, n, how, ok, synced in d["accounts"]:
        rows.append(kv(site, f"{n} past posts · {flag(ok, how, how)} · last pull {e(synced)}"))
    rows.append(kv("drafts", e("{} total · last tick {} · last draft {}".format(*d["drafts"]))))
    t = d["totals"]
    rows.append(kv("model usage", e(f"{t['calls']} calls · {t['tin'] + t['tout']:,} tokens · ${t['cost']:.2f} all time (as reported by claude -p)")))

    def table(rs):
        body = "".join(
            f"<tr><td class='num'>{e(local(r['started_at']))}</td><td>{e(r['trigger'] or '')}</td><td><code>{e(r['command'] or '')}</code></td>"
            f"<td>{e(r['step'])}</td><td><span class='b {e(r['status'])}'>{e(r['status'])}</span></td>"
            f"<td class='num'>{e(_tokens(r))}</td><td>{e(' '.join((r['detail'] or '').split())[:300])}</td></tr>" for r in rs)
        return ("<table><tr><th>when</th><th>trigger</th><th>command</th><th>step</th><th>status</th><th>model usage</th><th>detail</th></tr>"
                + body + "</table>")

    body = (f'<div class="card">{"".join(rows)}</div><h2>Last run of each step</h2>{table(d["latest"])}'
            f'<h2>History (newest first)</h2>{table(d["recent"])}')
    return PAGE.replace("__BANNER__", f'<div class="banner">{e(banner)}</div>' if banner else "") \
               .replace("__NOW__", datetime.now().strftime("%a %b %d, %H:%M")).replace("__BODY__", body)


def open_dashboard(conn, banner: str = "") -> Path:
    from .notify import open_file

    path = home() / "status.html"
    path.write_text(render_html(collect(conn), banner), encoding="utf-8")
    path.chmod(0o600)
    open_file(path)
    return path
