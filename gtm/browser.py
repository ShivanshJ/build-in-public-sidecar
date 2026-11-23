"""Pull your own posts from X and LinkedIn through a logged-in Chrome profile kept on this machine.

`code_gtm login <site>` opens a visible window once so you sign in yourself (no passwords or tokens are stored by gtm;
the session cookies live in ~/.agent_sessions_gtm/browser). `code_gtm sync` then reads your own profile headlessly.
"""
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import ui
from .db import cfg, home, hours_since, iso_now, kv_get, kv_set
from .x_posts import _post, save_pulled, sync_api

SITES = ("x", "linkedin")
LOGIN_URL = {"x": "https://x.com/i/flow/login", "linkedin": "https://www.linkedin.com/login"}


class SessionExpired(RuntimeError):
    pass


X_JS = """() => [...document.querySelectorAll('article[data-testid="tweet"]')].map(a => {
  const time = a.querySelector('time'), link = time && time.closest('a');
  const text = a.querySelector('div[data-testid="tweetText"]'), group = a.querySelector('div[role="group"]');
  const social = a.querySelector('[data-testid="socialContext"]');
  return {href: link ? link.getAttribute('href') : '', datetime: time ? time.getAttribute('datetime') : '',
          text: text ? text.innerText : '', metrics: group ? (group.getAttribute('aria-label') || '') : '',
          social: social ? social.innerText : ''};
})"""

LI_JS = """() => [...document.querySelectorAll('[data-urn^="urn:li:activity"]')].map(a => {
  const q = s => { const e = a.querySelector(s); return e ? e.innerText : ''; };
  return {urn: a.getAttribute('data-urn'), text: q('.update-components-text'),
          age: q('.update-components-actor__sub-description'),
          reactions: q('.social-details-social-counts__reactions-count'),
          comments: q('.social-details-social-counts__comments')};
})"""


def _num(s: str) -> int:
    m = re.search(r"(\d[\d,.]*)\s*([KkMm]?)", s or "")
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1))


def parse_x_metrics(label: str) -> dict:
    """aria-label looks like '5 replies, 2 reposts, 30 likes, 3 bookmarks, 1200 views'."""
    out = {"replies": 0, "reposts": 0, "likes": 0}
    for n, word in re.findall(r"([\d,.]+[KkMm]?)\s+(repl\w+|repost\w*|like\w*)", label or ""):
        out["replies" if word.startswith("repl") else "reposts" if word.startswith("repost") else "likes"] = _num(n)
    return out


def parse_age(age: str, now: datetime | None = None) -> str | None:
    """LinkedIn shows relative ages like '3d', '2w', '5mo', '1yr'."""
    m = re.match(r"\s*(\d+)\s*(mo|yr|[hdw])", age or "")
    if not m:
        return None
    days = {"h": 0, "d": 1, "w": 7, "mo": 30, "yr": 365}[m.group(2)] * int(m.group(1))
    return ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")


def normalize_x(raw: list[dict], username: str) -> list[dict]:
    posts = {}
    for r in raw:
        m = re.fullmatch(rf"/{re.escape(username)}/status/(\d+)", r.get("href", ""), re.I)
        if not m or r.get("social") or not r.get("text", "").strip():
            continue  # someone else's post, a repost, or no text
        posts[m.group(1)] = _post(m.group(1), r["text"].strip(), r.get("datetime") or None, **parse_x_metrics(r["metrics"]))
    return list(posts.values())


def normalize_linkedin(raw: list[dict]) -> list[dict]:
    posts = {}
    for r in raw:
        m = re.search(r"activity:(\d+)", r.get("urn", ""))
        if not m or not r.get("text", "").strip():
            continue
        posts[m.group(1)] = _post(f"linkedin-{m.group(1)}", r["text"].strip(), parse_age(r.get("age", "")),
                                  likes=_num(r.get("reactions", "")), replies=_num(r.get("comments", "")),
                                  platform="linkedin")
    return list(posts.values())


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("browser sync needs playwright: uv pip install 'gtm[browser]'") from None
    return sync_playwright()


def _context(p, headless: bool):
    return p.chromium.launch_persistent_context(
        str(home() / "browser"), channel="chrome", headless=headless, viewport={"width": 1100, "height": 900}
    )


HOME_URL = {"x": "https://x.com/home", "linkedin": "https://www.linkedin.com/feed/"}


def _reuse_browser_login(site: str) -> bool:
    """Offer to copy this site's login cookies from the browser you already use. True when gtm's profile ends up signed in."""
    from .cookies import CookieImportError, find_login, import_login

    if not sys.stdin.isatty():
        return False
    found, notes = find_login(site)
    if not found:
        if notes:
            print(f"Can't reuse a browser login: {'; '.join(notes)}.")
        return False
    name = found.source.name
    if input(f"You're already signed in to {site} in {name}. Reuse that login? (copies only {site}'s cookies; "
             f"macOS may ask for your password) [y/N] ").strip().lower() not in ("y", "yes"):
        return False
    try:
        cookies = import_login(found, site)
        with _playwright() as p:
            ctx = _context(p, headless=True)
            try:
                ctx.add_cookies(cookies)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(HOME_URL[site], wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                _check_login(page, site)
            finally:
                ctx.close()
    except (CookieImportError, SessionExpired) as e:
        print(f"Couldn't reuse it: {e}")
        return False
    except Exception as e:
        print(f"Couldn't reuse it ({type(e).__name__}: {e})")
        return False
    print(f"Done: {site} is connected using your {name} login.")
    return True


def login(conn, site: str) -> None:
    if not _reuse_browser_login(site):
        with _playwright() as p:
            ctx = _context(p, headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(LOGIN_URL[site])
            print("A Chrome window opened for gtm (separate from your everyday browser).")
            input(f"Sign in to {site} there (\"Continue with Google\" works), then press Enter here... ")
            ctx.close()
    kv_set(conn, f"login_{site}", iso_now())


def _check_login(page, site: str) -> None:
    if re.search(r"/(login|i/flow|authwall|checkpoint|uas)\b", page.url):
        raise SessionExpired(f"{site} session expired; run `code_gtm login {site}`")


def _scrape(page, js: str, rounds: int) -> list[dict]:
    seen: dict[str, dict] = {}
    for i in range(rounds):
        for r in page.evaluate(js):
            seen[r.get("href") or r.get("urn") or str(len(seen))] = r
        ui.update(f"scrolling your profile, round {i + 1}/{rounds} ({len(seen)} posts seen)")
        page.mouse.wheel(0, 2500)
        time.sleep(1.2)
    return list(seen.values())


def pull(site: str, rounds: int = 6) -> list[dict]:
    ui.update("starting a headless Chrome with your saved login")
    with _playwright() as p:
        ctx = _context(p, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if site == "x":
                posts = _pull_x(page, rounds)
            else:
                posts = _pull_linkedin(page, rounds)
            if not posts:
                shot = home() / "debug"
                shot.mkdir(exist_ok=True)
                page.screenshot(path=str(shot / f"{site}.png"))
                raise RuntimeError(f"found 0 posts on {site}; layout may have changed (screenshot: {shot / f'{site}.png'})")
            return posts
        finally:
            ctx.close()


def _pull_x(page, rounds: int) -> list[dict]:
    user = cfg("x_username")
    if not user:
        ui.update("finding your X handle")
        page.goto("https://x.com/home", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        _check_login(page, "x")
        href = page.evaluate("() => (document.querySelector('a[data-testid=\"AppTabBar_Profile_Link\"]')||{}).href || ''")
        user = href.rstrip("/").split("/")[-1]
        if not user:
            raise RuntimeError("could not detect your X handle; set x_username in config.json")
    ui.update(f"opening x.com/{user}")
    page.goto(f"https://x.com/{user}", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    _check_login(page, "x")
    return normalize_x(_scrape(page, X_JS, rounds), user)


def _pull_linkedin(page, rounds: int) -> list[dict]:
    ui.update("opening your LinkedIn profile")
    page.goto("https://www.linkedin.com/in/me/", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    _check_login(page, "linkedin")
    page.goto(page.url.split("?")[0].rstrip("/") + "/recent-activity/shares/", wait_until="domcontentloaded", timeout=60_000)
    _check_login(page, "linkedin")
    try:
        page.wait_for_selector('[data-urn^="urn:li:activity"]', timeout=20_000)
    except Exception:
        pass  # no posts, or the layout changed; pull() screenshots it
    return normalize_linkedin(_scrape(page, LI_JS, rounds))


def sync_site(conn, site: str, r, force: bool = False) -> None:
    """Fill the recorder `r` (see db.record). Prefers the X API when a bearer token is configured.

    Scheduled ticks sync each site at most every `sync_hours` (default 20): re-reading your profile three times
    a day adds nothing, costs API reads, and is more scraping than needed. `code_gtm sync` passes force=True.
    """
    api = site == "x" and cfg("x_bearer_token") and cfg("x_username")
    if not api and not kv_get(conn, f"login_{site}"):
        r.skip(f"not connected; run `code_gtm login {site}`")
        return
    age = hours_since(kv_get(conn, f"last_sync_{site}"))
    if not force and age is not None and age < float(cfg("sync_hours", 20)):
        r.skip(f"synced {age:.0f}h ago (every {float(cfg('sync_hours', 20)):g}h; `code_gtm sync` forces)")
        return
    if api:
        ui.update("asking the X API for your latest posts")
        total, new, updated = sync_api(conn)
        how = "api"
    else:
        posts = pull(site)
        new, updated = save_pulled(conn, posts)
        total, how = len(posts), "browser"
    r.detail = f"{total} posts via {how}: {new} new, {updated} already saved (likes and replies refreshed)"
    kv_set(conn, f"last_sync_{site}", iso_now())
