"""Your own past posts = the style corpus. Sources: X archive export, X API, or plain text files."""
import hashlib
import json
import re
from email.utils import parsedate_to_datetime

import requests

from .db import cfg


def score(likes: int, reposts: int, replies: int) -> float:
    return likes + 2 * reposts + 3 * replies


def upsert(conn, post: dict) -> bool:
    """Insert, or refresh metrics on an existing post. A post is the same if its id matches, or (for posts
    imported from a file, which have no platform id) its text is identical, so re-syncing never duplicates.
    Returns True when the post was new."""
    row = conn.execute("SELECT id FROM my_posts WHERE id=?", (post["id"],)).fetchone() or conn.execute(
        "SELECT id FROM my_posts WHERE platform=? AND text=?", (post["platform"], post["text"])
    ).fetchone()
    if row:
        conn.execute(
            """UPDATE my_posts SET likes=?, reposts=?, replies=?, score=?, created_at=COALESCE(created_at, ?)
               WHERE id=?""",
            (post["likes"], post["reposts"], post["replies"], post["score"], post["created_at"], row["id"]),
        )
        return False
    else:
        conn.execute(
            """INSERT INTO my_posts(id, platform, text, created_at, likes, reposts, replies, score)
               VALUES(:id,:platform,:text,:created_at,:likes,:reposts,:replies,:score)""",
            post,
        )
        return True


def save_pulled(conn, posts: list[dict]) -> tuple[int, int]:
    """Store pulled posts, printing each one (first sentence only). Returns (new, updated)."""
    from . import ui

    ui.say(f"  pulled {len(posts)} posts:")
    new = 0
    for post in posts:
        is_new = upsert(conn, post)
        new += is_new
        ui.show_post(post, is_new)
    conn.commit()
    return new, len(posts) - new


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


def sync_api(conn, limit: int = 100) -> tuple[int, int, int]:
    """Pull your latest posts + metrics via the X API v2 (needs x_bearer_token and x_username)."""
    token, user = cfg("x_bearer_token"), cfg("x_username")
    if not (token and user):
        return 0, 0, 0
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
    posts = [_post(t["id"], t["text"], t["created_at"], t["public_metrics"]["like_count"],
                   t["public_metrics"]["retweet_count"], t["public_metrics"]["reply_count"])
             for t in r.json().get("data", [])]
    new, updated = save_pulled(conn, posts)
    return len(posts), new, updated
