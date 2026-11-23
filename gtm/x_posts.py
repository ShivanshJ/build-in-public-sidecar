"""Your own past posts = the style corpus. Sources: X archive export, X API, or plain text files."""
import hashlib
import json
import re
from email.utils import parsedate_to_datetime

import requests

from .db import cfg


def score(likes: int, reposts: int, replies: int) -> float:
    return likes + 2 * reposts + 3 * replies


def upsert(conn, post: dict) -> None:
    """Insert, or refresh metrics on an existing post. A post is the same if its id matches, or (for posts
    imported from a file, which have no platform id) its text is identical, so re-syncing never duplicates."""
    row = conn.execute("SELECT id FROM my_posts WHERE id=?", (post["id"],)).fetchone() or conn.execute(
        "SELECT id FROM my_posts WHERE platform=? AND text=?", (post["platform"], post["text"])
    ).fetchone()
    if row:
        conn.execute(
            """UPDATE my_posts SET likes=?, reposts=?, replies=?, score=?, created_at=COALESCE(created_at, ?)
               WHERE id=?""",
            (post["likes"], post["reposts"], post["replies"], post["score"], post["created_at"], row["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO my_posts(id, platform, text, created_at, likes, reposts, replies, score)
               VALUES(:id,:platform,:text,:created_at,:likes,:reposts,:replies,:score)""",
            post,
        )


def _post(id_, text, created_at, likes=0, reposts=0, replies=0, platform="x") -> dict:
    return dict(id=str(id_), platform=platform, text=text, created_at=created_at,
                likes=likes, reposts=reposts, replies=replies, score=score(likes, reposts, replies))


def import_archive(conn, tweets_js: str) -> int:
    """Parse data/tweets.js from the X archive export (Settings > Your account > Download an archive)."""
    raw = open(tweets_js, encoding="utf-8").read()
    items = json.loads(raw[raw.index("[") :])
    n = 0
    for item in items:
        t = item["tweet"]
        text = t["full_text"]
        if text.startswith("RT @") or t.get("in_reply_to_status_id_str"):
            continue
        created = parsedate_to_datetime(t["created_at"]).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        upsert(conn, _post(t["id_str"], text, created, int(t.get("favorite_count", 0)), int(t.get("retweet_count", 0))))
        n += 1
    conn.commit()
    return n


def import_text(conn, path: str, platform: str) -> int:
    """Plain text file, posts separated by a line containing only '---'."""
    n = 0
    for i, block in enumerate(re.split(r"\n\s*---\s*\n", open(path, encoding="utf-8").read())):
        block = block.strip()
        if block:
            digest = hashlib.sha1(block.encode()).hexdigest()[:16]
            upsert(conn, _post(f"{platform}-file-{digest}", block, None, platform=platform))
            n += 1
    conn.commit()
    return n


def sync_api(conn, limit: int = 100) -> int:
    """Pull your latest posts + metrics via the X API v2 (needs x_bearer_token and x_username)."""
    token, user = cfg("x_bearer_token"), cfg("x_username")
    if not (token and user):
        return 0
    h = {"Authorization": f"Bearer {token}"}
    uid = requests.get(f"https://api.x.com/2/users/by/username/{user}", headers=h, timeout=30).json()["data"]["id"]
    r = requests.get(
        f"https://api.x.com/2/users/{uid}/tweets",
        headers=h,
        params={"max_results": min(limit, 100), "exclude": "retweets,replies",
                "tweet.fields": "created_at,public_metrics"},
        timeout=30,
    )
    r.raise_for_status()
    n = 0
    for t in r.json().get("data", []):
        m = t["public_metrics"]
        upsert(conn, _post(t["id"], t["text"], t["created_at"], m["like_count"], m["retweet_count"], m["reply_count"]))
        n += 1
    conn.commit()
    return n
