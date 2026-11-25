"""Show drafts in a local popup: a static HTML page (no server, no network) opened in your browser."""
import html
import os
import shutil
import subprocess
import sys
from datetime import datetime
from urllib.parse import quote

from .db import cfg, home

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gtm: __TITLE__</title>
<style>
  body { font: 15px/1.5 -apple-system, system-ui, sans-serif; background: #f6f6f4; color: #1a1a1a; margin: 0; padding: 32px 16px; }
  main { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; } .sub { color: #777; margin: 0 0 24px; font-size: 13px; }
  h2.batch { font-size: 13px; margin: 28px 0 10px; color: #555; text-transform: uppercase; letter-spacing: .04em; }
  h2.batch:first-of-type { margin-top: 0; }
  .card { background: #fff; border: 1px solid #e3e3df; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  .meta { display: flex; gap: 10px; align-items: baseline; margin-bottom: 10px; }
  .badge { font-size: 11px; font-weight: 600; letter-spacing: .04em; padding: 2px 8px; border-radius: 99px; background: #1a1a1a; color: #fff; }
  .why, .when { color: #777; font-size: 13px; } .when { font-weight: 600; }
  textarea { width: 100%; box-sizing: border-box; border: 1px solid #e3e3df; border-radius: 8px; padding: 10px; font: inherit; resize: none; overflow: hidden; }
  .row { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
  .spacer { flex: 1; } .count { color: #777; font-size: 13px; } .count.over { color: #c0392b; font-weight: 600; }
  button, a.post { font: inherit; font-size: 14px; padding: 7px 14px; border-radius: 8px; border: 1px solid #cfcfca; background: #fff; color: inherit; cursor: pointer; text-decoration: none; }
  a.post { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
</style></head>
<body><main>
<h1>__TITLE__</h1>
<p class="sub">__SUB__</p>
__CARDS__
</main>
<script>
const urls = {
  x: t => "https://x.com/intent/post?text=" + encodeURIComponent(t),
  linkedin: t => "https://www.linkedin.com/feed/?shareActive=true&text=" + encodeURIComponent(t),
};
document.querySelectorAll(".card").forEach(c => {
  const ta = c.querySelector("textarea"), a = c.querySelector(".post"), n = c.querySelector(".count"), p = c.dataset.platform;
  const update = () => {
    a.href = urls[p](ta.value);
    n.textContent = p === "x" ? ta.value.length + "/280" : ta.value.length + " chars";
    n.classList.toggle("over", p === "x" && ta.value.length > 280);
    ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px";
  };
  ta.addEventListener("input", update); update();
  c.querySelector(".copy").addEventListener("click", async e => {
    try { await navigator.clipboard.writeText(ta.value); } catch (_) { ta.select(); document.execCommand("copy"); }
    e.target.textContent = "Copied"; setTimeout(() => e.target.textContent = "Copy", 1500);
  });
});
</script></body></html>
"""

CARD = """<section class="card" data-platform="{platform}">
  <div class="meta"><span class="badge">{label}</span><span class="when">{when}</span><span class="why">{why}</span></div>
  <textarea rows="5" spellcheck="true">{text}</textarea>
  <div class="row"><span class="count"></span><span class="spacer"></span>
    <button class="copy" type="button">Copy</button>
    <a class="post" target="_blank" rel="noopener">Post to {label}</a></div>
</section>"""


def intent_url(platform: str, text: str) -> str:
    q = quote(text, safe="")
    if platform == "x":
        return f"https://x.com/intent/post?text={q}"
    return f"https://www.linkedin.com/feed/?shareActive=true&text={q}"


def _when(created_at: str | None) -> str:
    """When the draft was written, in local time (not when the page is opened)."""
    from .status import when

    return when(created_at) if created_at else ""


def render_page(drafts: list[dict], history: bool = False) -> str:
    """Draft text is model output built from untrusted material, so everything is HTML-escaped.

    history=True shows past drafts in the order given (newest batch first), with a heading per batch.
    """
    parts, last = [], None
    for d in drafts:
        batch = (d.get("created_at") or "")[:16]
        if history and batch != last:
            n = sum(1 for x in drafts if (x.get("created_at") or "")[:16] == batch)
            parts.append(f'<h2 class="batch">{html.escape(_when(d.get("created_at")))} · {n} draft{"s" if n != 1 else ""}</h2>')
            last = batch
        parts.append(CARD.format(
            platform=d["platform"],
            label="X" if d["platform"] == "x" else "LinkedIn",
            when="" if history else html.escape(_when(d.get("created_at"))),
            why=html.escape(d.get("why", "")),
            text=html.escape(d["text"]),
        ))
    n = len(drafts)
    title = f"Draft history: {n} drafts, newest first" if history else f"{n} drafts ready"
    sub = ("Edits here aren't saved. Copy or post from the page." if history
           else "Edit freely, then post. Nothing is sent until you click.")
    return PAGE.replace("__TITLE__", title).replace("__SUB__", sub).replace("__CARDS__", "".join(parts))


def open_file(path) -> None:
    if os.environ.get("GTM_NO_OPEN"):
        return
    opener = "open" if sys.platform == "darwin" else shutil.which("xdg-open")
    if opener:
        subprocess.run([opener, str(path)], check=False)


def popup(drafts: list[dict], history: bool = False):
    """New drafts go to a timestamped page and latest.html (what `inbox` reopens). History has its own page, so
    looking back never replaces the latest batch."""
    out = home() / "drafts"
    out.mkdir(exist_ok=True)
    page = render_page(drafts, history=history)
    path = out / ("history.html" if history else f"{datetime.now():%Y%m%d-%H%M%S}.html")
    for p in (path,) if history else (path, out / "latest.html"):
        p.write_text(page, encoding="utf-8")
        p.chmod(0o600)
    open_file(path)
    return path


def open_latest() -> bool:
    latest = home() / "drafts" / "latest.html"
    if not latest.exists():
        return False
    open_file(latest)
    return True


def send_batch(drafts: list[dict]) -> str:
    """Deliver one tick's drafts. Backends: popup (default) or stdout."""
    if not drafts:
        return "none"
    backend = cfg("notify", "popup")
    if backend == "stdout":
        for d in drafts:
            print(f"--- [{d['platform']}] ---\n{d['text']}\n{intent_url(d['platform'], d['text'])}\n")
    else:
        popup(drafts)
    return backend
