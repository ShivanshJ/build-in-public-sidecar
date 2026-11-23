import json
import os
import re
from pathlib import Path

STRIP = re.compile(
    r"<(system-reminder|local-command-caveat|local-command-stdout|command-message|command-args|command-name)>.*?</\1>",
    re.S,
)
SECRETS = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|Bearer\s+[A-Za-z0-9._\-]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|(?i:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*\S{6,})",
    re.S,
)
MAX_CHUNK = 1200
MIN_CHUNK = 20


def redact(text: str) -> str:
    return SECRETS.sub("[REDACTED]", text)


def clean(text: str) -> str:
    return redact(STRIP.sub("", text)).strip()


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def split_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        while len(para) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:limit])
            para = para[limit:]
        if cur and len(cur) + len(para) + 2 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return [c for c in out if len(c) >= MIN_CHUNK]


def parse(lines):
    """Yield {uuid, role, text, ts, cwd} for human-readable user/assistant text only."""
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") not in ("user", "assistant") or r.get("isSidechain") or r.get("isMeta"):
            continue
        text = clean(_text((r.get("message") or {}).get("content")))
        for i, piece in enumerate(split_text(text)):
            yield {"uuid": f"{r.get('uuid')}:{i}", "role": r["type"], "text": piece,
                   "ts": r.get("timestamp"), "cwd": r.get("cwd")}


def ingest_file(conn, path) -> int:
    """Incrementally ingest a transcript JSONL; returns number of new chunks."""
    path = Path(path)
    sid = path.stem
    row = conn.execute("SELECT offset FROM sessions WHERE session_id=?", (sid,)).fetchone()
    off = row["offset"] if row else 0
    if off > os.path.getsize(path):
        off = 0
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return 0
    n, project, first, last = 0, None, None, None
    for rec in parse(data[: end + 1].decode("utf-8", "replace").splitlines()):
        cur = conn.execute(
            "INSERT OR IGNORE INTO chunks(session_id, uuid, role, text, ts) VALUES(?,?,?,?,?)",
            (sid, rec["uuid"], rec["role"], rec["text"], rec["ts"]),
        )
        n += cur.rowcount
        project = project or rec["cwd"]
        first = first or rec["ts"]
        last = rec["ts"]
    conn.execute(
        """INSERT INTO sessions(session_id, path, project, offset, first_ts, last_ts) VALUES(?,?,?,?,?,?)
           ON CONFLICT(session_id) DO UPDATE SET offset=excluded.offset, path=excluded.path,
             project=COALESCE(sessions.project, excluded.project),
             first_ts=COALESCE(sessions.first_ts, excluded.first_ts),
             last_ts=COALESCE(excluded.last_ts, sessions.last_ts)""",
        (sid, str(path), project, off + end + 1, first, last),
    )
    conn.commit()
    return n


def ingest_all(conn, root=None) -> int:
    root = Path(root or Path.home() / ".claude" / "projects")
    return sum(ingest_file(conn, p) for p in root.glob("*/*.jsonl"))
