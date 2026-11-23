import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import ui


def home() -> Path:
    p = Path(os.environ.get("GTM_HOME") or Path.home() / ".agent_sessions_gtm")
    p.mkdir(parents=True, exist_ok=True)
    return p


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, path TEXT, project TEXT,
  offset INTEGER DEFAULT 0, first_ts TEXT, last_ts TEXT);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, session_id TEXT, uuid TEXT UNIQUE,
  role TEXT, text TEXT, ts TEXT, emb BLOB);
CREATE INDEX IF NOT EXISTS chunks_ts ON chunks(ts);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, content='chunks', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TABLE IF NOT EXISTS my_posts(
  id TEXT PRIMARY KEY, platform TEXT, text TEXT, created_at TEXT,
  likes INTEGER DEFAULT 0, reposts INTEGER DEFAULT 0, replies INTEGER DEFAULT 0,
  score REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS drafts(
  id INTEGER PRIMARY KEY, created_at TEXT, platform TEXT, text TEXT,
  why TEXT, status TEXT DEFAULT 'new');
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, step TEXT, status TEXT, detail TEXT,
  trigger TEXT, command TEXT, llm_calls INTEGER DEFAULT 0, tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0);
"""


def connect() -> sqlite3.Connection:
    c = sqlite3.connect(home() / "gtm.db", timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    have = {r["name"] for r in c.execute("PRAGMA table_info(runs)")}
    for col, typ in (("trigger", "TEXT"), ("command", "TEXT"), ("llm_calls", "INTEGER DEFAULT 0"),
                     ("tokens_in", "INTEGER DEFAULT 0"), ("tokens_out", "INTEGER DEFAULT 0"), ("cost_usd", "REAL DEFAULT 0")):
        if col not in have:
            c.execute(f"ALTER TABLE runs ADD COLUMN {col} {typ}")
    return c


def _config_path() -> Path:
    return home() / "config.json"


def load_config() -> dict:
    p = _config_path()
    return json.loads(p.read_text()) if p.exists() else {}


def save_config(cfg: dict) -> None:
    p = _config_path()
    p.write_text(json.dumps(cfg, indent=2))
    p.chmod(0o600)


def cfg(key: str, default=None):
    """Env var GTM_<KEY> wins over config.json."""
    return os.environ.get("GTM_" + key.upper()) or load_config().get(key, default)


def kv_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def kv_set(conn, key, value):
    conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


USAGE = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}


def add_usage(tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0) -> None:
    """Called by llm.complete; `record` stores the delta accumulated while a step ran."""
    USAGE["calls"] += 1
    USAGE["tokens_in"] += tokens_in
    USAGE["tokens_out"] += tokens_out
    USAGE["cost_usd"] += cost_usd


def hours_since(ts: str | None) -> float | None:
    if not ts:
        return None
    then = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


STEP_LABELS = {
    "ingest": "reading your Claude Code sessions",
    "embed": "building the search index (the first run downloads the models)",
    "draft": "drafting posts",
    "learn": "learning your writing style from your posts",
    "sync_x": "pulling your posts from X",
    "sync_linkedin": "pulling your posts from LinkedIn",
    "tick": "running the scheduled job",
}


def reason(exc: BaseException) -> str:
    """A one-line cause for a log: no tracebacks or Playwright call logs, but keep the page that was being loaded."""
    msg = str(exc)
    url = re.search(r'navigating to "([^"]+)"', msg)
    first = " ".join(msg.split("Call log:")[0].split())
    out = f"{type(exc).__name__}: {first}" if first else type(exc).__name__
    return f"{out} ({url.group(1)})" if url else out


class record:
    """Log one pipeline step to `runs`. Use `with record(conn, "sync_x") as r: ...; r.detail = "12 posts"`.

    Status is ok, skip (r.skip("why")) or error (exception is logged and re-raised unless swallow=True).
    Also stores what triggered it (GTM_TRIGGER=schedule from launchd, else manual), the gtm command line,
    and the LLM calls, tokens and cost used while the step ran.
    """

    def __init__(self, conn, step: str, swallow: bool = False):
        self.conn, self.step, self.swallow = conn, step, swallow
        self.status, self.detail = "ok", ""

    def skip(self, why: str):
        self.status, self.detail = "skip", why

    def __enter__(self):
        self.started = iso_now()
        self.base = dict(USAGE)
        self._spinner = ui.working(STEP_LABELS.get(self.step, self.step))
        self._spinner.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._spinner.__exit__(None, None, None)
        if exc:
            self.status, self.detail = "error", reason(exc)[:500]
        used = {k: USAGE[k] - self.base[k] for k in USAGE}
        self.conn.execute(
            """INSERT INTO runs(started_at, finished_at, step, status, detail, trigger, command,
                                llm_calls, tokens_in, tokens_out, cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (self.started, iso_now(), self.step, self.status, str(self.detail),
             os.environ.get("GTM_TRIGGER", "manual"), "code_gtm " + " ".join(sys.argv[1:]),
             used["calls"], used["tokens_in"], used["tokens_out"], used["cost_usd"]),
        )
        self.conn.commit()
        tok = f"  [{used['calls']} llm call(s), {used['tokens_in'] + used['tokens_out']:,} tokens, ${used['cost_usd']:.2f}]" if used["calls"] else ""
        mark, paint = {"ok": ("✓", "32"), "skip": ("-", "33"), "error": ("✗", "31")}[self.status]
        mark = f"\033[{paint}m{mark}\033[0m" if ui.color() else mark
        ui.say(f"{ui.dim(f'{datetime.now():%H:%M:%S}')}  {mark} {self.step:<13} {' '.join(str(self.detail).split())[:140]}{tok}")
        return bool(exc) and self.swallow
