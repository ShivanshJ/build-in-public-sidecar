import json

import pytest

from gtm import cli, search
from gtm.db import connect
from gtm.draft import parse_drafts
from gtm.ingest import clean, ingest_file, parse, redact, split_text


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("GTM_HOME", str(tmp_path / "gtm"))
    return connect()


def rec(uuid, type_, content, **kw):
    return json.dumps({"uuid": uuid, "type": type_, "message": {"content": content},
                       "timestamp": "2026-11-20T10:00:00.000Z", "cwd": "/x/proj", **kw})


def test_parse_keeps_only_human_text():
    lines = [
        rec("1", "user", "build me a BM25 search over my transcripts please"),
        rec("2", "user", [{"type": "tool_result", "content": "ls output that is long enough to count"}]),
        rec("3", "assistant", [{"type": "tool_use", "name": "Bash"}, {"type": "text", "text": "Done: FTS5 index created."}]),
        rec("4", "user", "<system-reminder>ignore me entirely, long reminder text</system-reminder>"),
        rec("5", "user", "meta context that should be dropped entirely", isMeta=True),
        rec("6", "assistant", "sidechain text that should be dropped entirely", isSidechain=True),
        "not json",
    ]
    out = list(parse(lines))
    assert [r["role"] for r in out] == ["user", "assistant"]
    assert out[1]["text"] == "Done: FTS5 index created."


def test_redact_secrets():
    s = "key sk-abcdefghijklmnopqrstuvwx and ghp_abcdefghijklmnopqrstuv and password=hunter2hunter2"
    out = redact(s)
    assert "sk-abc" not in out and "ghp_" not in out and "hunter2" not in out
    assert clean("<system-reminder>x</system-reminder>hello there world, how are you") == "hello there world, how are you"


def test_split_text_respects_limit():
    pieces = split_text("word " * 1000, limit=500)
    assert pieces and all(len(p) <= 500 for p in pieces)


def test_ingest_is_incremental_and_idempotent(conn, tmp_path):
    f = tmp_path / "abc123.jsonl"
    f.write_text(rec("1", "user", "first message about vector search design") + "\n")
    assert ingest_file(conn, f) == 1
    assert ingest_file(conn, f) == 0
    with open(f, "a") as fh:
        fh.write(rec("2", "assistant", "second message about reranking approach") + "\n")
        fh.write(rec("3", "user", "partial line without newline yet"))
    assert ingest_file(conn, f) == 1
    with open(f, "a") as fh:
        fh.write("\n")
    assert ingest_file(conn, f) == 1
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 3


def test_bm25_and_rrf(conn, tmp_path):
    f = tmp_path / "s1.jsonl"
    f.write_text(
        rec("1", "user", "we fixed the launchd scheduling bug on macOS") + "\n"
        + rec("2", "user", "wrote the landing page copy for the product") + "\n"
    )
    ingest_file(conn, f)
    ids = search.bm25(conn, "launchd scheduling")
    assert len(ids) == 1
    assert search.bm25(conn, "!!!") == []
    assert search.rrf([[1, 2], [2, 3]])[0] == 2


def test_parse_drafts_filters_bad_items():
    out = json.dumps({"drafts": [
        {"platform": "x", "text": "ok post", "why": "w"},
        {"platform": "x", "text": "a" * 281, "why": "too long"},
        {"platform": "tiktok", "text": "nope"},
        {"platform": "linkedin", "text": "token=abcdef123456 shipped it", "why": ""},
    ]})
    got = parse_drafts("Sure!\n" + out)
    assert [d["platform"] for d in got] == ["x", "linkedin"]
    assert "abcdef123456" not in got[1]["text"]
    assert parse_drafts("no json here") == []


def test_install_hook_idempotent(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}))
    assert cli.install_hook(s) is True
    assert cli.install_hook(s) is False
    data = json.loads(s.read_text())
    assert len(data["hooks"]["Stop"]) == 2 and "SessionEnd" in data["hooks"]
    assert (tmp_path / "settings.json.bak-gtm").exists()


def test_install_hook_repoints_a_stale_command(tmp_path):
    s = tmp_path / "settings.json"
    old = "/old/venv/bin/python3 -m gtm.hook"
    s.write_text(json.dumps({"hooks": {ev: [{"hooks": [{"type": "command", "command": old}]}] for ev in cli.HOOK_EVENTS}}))
    assert cli.install_hook(s) is True
    data = json.loads(s.read_text())
    assert all(len(data["hooks"][ev]) == 1 and data["hooks"][ev][0]["hooks"][0]["command"].endswith("-m gtm.hook")
               and old not in data["hooks"][ev][0]["hooks"][0]["command"] for ev in cli.HOOK_EVENTS)
    assert cli.install_hook(s) is False


def test_bm25_until_excludes_recent(conn, tmp_path):
    f = tmp_path / "s2.jsonl"
    old = rec("1", "user", "fixed the github pages jekyll deploy error").replace("2026-11-20", "2026-10-01")
    new = rec("2", "user", "hit the github pages deploy error again today")
    f.write_text(old + "\n" + new + "\n")
    ingest_file(conn, f)
    assert len(search.bm25(conn, "github pages deploy")) == 2
    assert len(search.bm25(conn, "github pages deploy", until="2026-11-01T00:00:00.000Z")) == 1


def test_history_for_only_returns_older_work(conn, tmp_path, monkeypatch):
    from gtm import draft

    f = tmp_path / "s3.jsonl"
    old = rec("1", "user", "fixed the github pages jekyll deploy error").replace("2026-11-20", "2026-10-01")
    new = rec("2", "user", "hit the github pages deploy error again today")
    f.write_text(old + "\n" + new + "\n")
    ingest_file(conn, f)
    monkeypatch.setattr(search, "dense", lambda *a, **k: [])
    monkeypatch.setattr(search, "_reranker", lambda: type("R", (), {"rerank": lambda s, q, d: [1.0] * len(d)})())
    out = draft.history_for(conn, [{"angle": "pages bug", "query": "github pages deploy"}], before="2026-11-01T00:00:00.000Z")
    assert "2026-10-01" in out and "again today" not in out


def test_propose_angles_tolerates_garbage(monkeypatch):
    from gtm import draft, llm

    monkeypatch.setattr(llm, "complete", lambda p, **k: "not json at all")
    assert draft.propose_angles("x") == []
    monkeypatch.setattr(llm, "complete", lambda p, **k: 'ok {"angles":[{"angle":"a","query":"q"},{"angle":"b"}]}')
    assert draft.propose_angles("x") == [{"angle": "a", "query": "q"}]


def test_render_page_escapes_untrusted_text():
    from gtm.notify import render_page

    page = render_page([{"platform": "x", "text": "</textarea><script>alert(1)</script>", "why": "<b>why</b>"}])
    assert "<script>alert(1)</script>" not in page
    assert "&lt;/textarea&gt;" in page and "&lt;b&gt;why&lt;/b&gt;" in page


def test_popup_writes_latest_without_opening(tmp_path, monkeypatch):
    from gtm import notify

    monkeypatch.setenv("GTM_HOME", str(tmp_path / "gtm"))
    monkeypatch.setenv("GTM_NO_OPEN", "1")
    path = notify.popup([{"platform": "linkedin", "text": "hello", "why": "w"}])
    assert path.exists() and (path.parent / "latest.html").exists()


def test_tick_skips_when_locked(conn, tmp_path):
    import fcntl

    from gtm.db import home
    from gtm.draft import tick

    with open(home() / "tick.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert tick(conn).startswith("skip: another tick")


def test_tick_skips_within_min_gap(conn):
    from gtm.db import iso_now, kv_set
    from gtm.draft import tick

    kv_set(conn, "last_tick_ts", iso_now())
    assert tick(conn).startswith("skip: last tick was under")


def test_record_logs_ok_skip_and_error(conn):
    from gtm.db import record

    with record(conn, "a") as r:
        r.detail = "fine"
    with record(conn, "b") as r:
        r.skip("nothing to do")
    with record(conn, "c", swallow=True):
        raise ValueError("boom")
    with pytest.raises(ValueError):
        with record(conn, "d"):
            raise ValueError("raised")
    rows = {r["step"]: (r["status"], r["detail"]) for r in conn.execute("SELECT * FROM runs")}
    assert rows["a"] == ("ok", "fine") and rows["b"] == ("skip", "nothing to do")
    assert rows["c"][0] == "error" and "boom" in rows["c"][1] and rows["d"][0] == "error"


def test_parse_x_metrics_and_normalize():
    from gtm.browser import normalize_x, parse_x_metrics

    assert parse_x_metrics("5 replies, 2 reposts, 1,200 likes, 3 bookmarks, 9K views") == {"replies": 5, "reposts": 2, "likes": 1200}
    raw = [
        {"href": "/me/status/1", "datetime": "2026-11-01T10:00:00.000Z", "text": "mine", "metrics": "1 reply, 0 reposts, 4 likes", "social": ""},
        {"href": "/other/status/2", "datetime": "", "text": "theirs", "metrics": "", "social": ""},
        {"href": "/me/status/3", "datetime": "", "text": "boosted", "metrics": "", "social": "You reposted"},
    ]
    posts = normalize_x(raw, "me")
    assert [p["id"] for p in posts] == ["1"] and posts[0]["likes"] == 4 and posts[0]["replies"] == 1


def test_normalize_linkedin_and_age():
    from datetime import datetime, timezone

    from gtm.browser import normalize_linkedin, parse_age

    now = datetime(2026, 11, 22, tzinfo=timezone.utc)
    assert parse_age("2w • Edited", now) == "2026-11-08T00:00:00.000Z" and parse_age("garbage") is None
    posts = normalize_linkedin([{"urn": "urn:li:activity:77", "text": "hi", "age": "3d", "reactions": "1,234", "comments": "5 comments"}])
    assert posts[0]["id"] == "linkedin-77" and posts[0]["likes"] == 1234 and posts[0]["replies"] == 5


def test_upsert_never_duplicates_and_refreshes_metrics(conn, tmp_path):
    from gtm.x_posts import _post, import_text, upsert

    upsert(conn, _post("1", "same text", "2026-11-01T00:00:00.000Z", likes=1))
    upsert(conn, _post("1", "same text", "2026-11-01T00:00:00.000Z", likes=9))
    f = tmp_path / "p.txt"
    f.write_text("a file post\n---\nanother")
    import_text(conn, str(f), "linkedin")
    import_text(conn, str(f), "linkedin")
    upsert(conn, _post("linkedin-77", "a file post", "2026-10-01T00:00:00.000Z", likes=5, platform="linkedin"))
    rows = {r["text"]: r for r in conn.execute("SELECT * FROM my_posts")}
    assert conn.execute("SELECT count(*) FROM my_posts").fetchone()[0] == 3
    assert rows["same text"]["likes"] == 9
    assert rows["a file post"]["likes"] == 5 and rows["a file post"]["created_at"] == "2026-10-01T00:00:00.000Z"


def test_sync_site_throttles_scheduled_runs_but_not_manual(conn, monkeypatch):
    from gtm import browser
    from gtm.db import iso_now, kv_set, record

    calls = []
    monkeypatch.setattr(browser, "pull", lambda site: calls.append(site) or [])
    with record(conn, "s1") as r:
        browser.sync_site(conn, "x", r)
    assert r.status == "skip" and "not connected" in r.detail
    kv_set(conn, "login_x", iso_now())
    with record(conn, "s2") as r:
        browser.sync_site(conn, "x", r)
    with record(conn, "s3") as r:
        browser.sync_site(conn, "x", r)
    with record(conn, "s4") as r:
        browser.sync_site(conn, "x", r, force=True)
    assert calls == ["x", "x"] and r.status == "ok" and "synced 0h ago" in dict(
        (x["step"], x["detail"]) for x in conn.execute("SELECT step, detail FROM runs"))["s3"]


def test_llm_usage_is_recorded_on_the_step(conn, monkeypatch):
    import subprocess

    from gtm import llm
    from gtm.db import record

    out = json.dumps({"result": " hi ", "total_cost_usd": 0.05,
                      "usage": {"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 7}})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
    with record(conn, "draft"):
        assert llm.complete("x") == "hi"
    r = conn.execute("SELECT * FROM runs").fetchone()
    assert (r["llm_calls"], r["tokens_in"], r["tokens_out"], r["cost_usd"]) == (1, 152, 7, 0.05)
    assert r["trigger"] == "manual" and r["command"].startswith("code_gtm")


def test_dashboard_escapes_and_demo_runs_in_a_sandbox(tmp_path, monkeypatch, capsys):
    from gtm import status
    from gtm.db import connect, record

    monkeypatch.setenv("GTM_HOME", str(tmp_path / "real"))
    monkeypatch.setenv("GTM_NO_OPEN", "1")
    c = connect()
    with record(c, "sync_x", swallow=True):
        raise RuntimeError("<script>alert(1)</script>")
    page = status.render_html(status.collect(c))
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page



def test_demo_runs_real_pipeline_on_claude_sessions_in_a_sandbox(tmp_path, monkeypatch):
    import os
    from datetime import datetime, timezone

    from gtm import draft, llm
    from gtm.db import connect

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GTM_HOME", str(tmp_path / "real"))
    monkeypatch.setenv("GTM_NO_OPEN", "1")
    real = connect()
    proj = tmp_path / "home" / ".claude" / "projects" / "-x-proj"
    proj.mkdir(parents=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    (proj / "s1.jsonl").write_text("\n".join([
        rec("1", "user", "add retry logic to the uploader", timestamp=now),
        rec("2", "assistant", "added exponential backoff to uploader.py", timestamp=now)]) + "\n")
    monkeypatch.setattr(search, "embed_pending", lambda conn: 0)
    monkeypatch.setattr(llm, "complete", lambda prompt, **k: json.dumps(
        {"drafts": [{"platform": "x", "text": "Added backoff to my uploader today.", "why": "w"}]}))

    cli.main(["demo", "--yes"])

    sandbox = os.environ["GTM_HOME"]
    assert sandbox != str(tmp_path / "real") and os.path.exists(sandbox + "/status.html")
    assert os.path.exists(sandbox + "/drafts/latest.html")
    sc = connect()
    assert sc.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2
    assert [r["step"] for r in sc.execute("SELECT step FROM runs ORDER BY id")] == ["ingest", "embed", "draft"]
    assert real.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


def test_demo_can_be_limited_to_one_project(tmp_path, monkeypatch):
    import os

    from gtm import llm
    from gtm.db import connect

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GTM_HOME", str(tmp_path / "real"))
    monkeypatch.setenv("GTM_NO_OPEN", "1")
    connect()
    base = tmp_path / "home" / ".claude" / "projects"
    for name, text in (("alpha-app", "build the alpha uploader with retries"), ("beta-site", "fix the beta navbar overflow on mobile")):
        d = base / ("-" + str(tmp_path / "home").strip("/").replace("/", "-") + "-" + name)
        d.mkdir(parents=True)
        (d / "s.jsonl").write_text(rec("1", "user", text, timestamp="2026-01-01T03:04:05.920Z") + "\n")
    monkeypatch.setattr(search, "embed_pending", lambda conn: 0)
    monkeypatch.setattr(llm, "complete", lambda prompt, **k: '{"drafts": []}')

    answers = iter(["beta", "y"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    cli.main(["demo"])

    texts = [r[0] for r in connect().execute("SELECT text FROM chunks")]
    assert texts == ["fix the beta navbar overflow on mobile"]
    answers = iter(["nomatch"])
    with pytest.raises(SystemExit) as e:
        cli.main(["demo"])
    assert "No project matches" in str(e.value)


def test_demo_cancels_without_reading_when_not_confirmed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GTM_HOME", str(tmp_path / "real"))
    proj = tmp_path / "home" / ".claude" / "projects" / "-x-proj"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text(rec("1", "user", "hello there friend") + "\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["demo"])
    assert "Cancelled" in str(e.value)


def test_drafts_command_lists_saved_drafts(conn, capsys, monkeypatch):
    from gtm.db import iso_now

    monkeypatch.setenv("GTM_NO_OPEN", "1")
    assert conn.execute("SELECT 1").fetchone()
    for t in ("first draft", "second draft"):
        conn.execute("INSERT INTO drafts(created_at, platform, text, why) VALUES(?,?,?,?)", (iso_now(), "x", t, "w"))
    conn.commit()
    cli.main(["drafts", "-n", "1"])
    out = capsys.readouterr().out
    assert "second draft" in out and "first draft" not in out


def test_web_flag_opens_dashboard_for_status_and_log(conn, capsys, monkeypatch):
    from gtm.db import home

    monkeypatch.setenv("GTM_NO_OPEN", "1")
    cli.main(["log", "--web"])
    assert (home() / "status.html").exists()
    (home() / "status.html").unlink()
    cli.main(["status", "--web"])
    assert (home() / "status.html").exists()


def test_snippet_shows_first_sentence_trimmed_with_dots():
    from gtm.ui import snippet

    assert snippet("gm") == "gm"
    assert snippet("Shipped it. Then the whole thing broke on Tuesday because the retry loop never backed off.") == \
        "Shipped it. Then the whole thing broke on Tuesday because the retry loop never..."
    assert snippet("A" * 200) == "A" * 80 + "..."
    assert len(snippet("word " * 100)) <= 83


def test_save_pulled_prints_each_post_and_reports_new_vs_updated(conn, capsys):
    from gtm.x_posts import _post, save_pulled

    posts = [_post("1", "Shipped the thing. It took three weeks and a lot of coffee to get it right.", None, likes=3)]
    assert save_pulled(conn, posts) == (1, 0)
    assert save_pulled(conn, posts) == (0, 1)
    out = capsys.readouterr().out
    assert "new" in out and "updated" in out and "Shipped the thing. It took three weeks" in out and "3 likes" in out


def _chrome_profile(home, key, rows, folder="Arc/User Data"):
    """A fake browser data dir whose Cookies db is encrypted the way Chromium does on macOS."""
    import hashlib
    import sqlite3

    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def enc(host, value, host_hash):
        data = (hashlib.sha256(host.encode()).digest() if host_hash else b"") + value.encode()
        pad = padding.PKCS7(128).padder()
        e = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
        return b"v10" + e.update(pad.update(data) + pad.finalize()) + e.finalize()

    prof = home / "Library/Application Support" / folder / "Default"
    prof.mkdir(parents=True)
    db = sqlite3.connect(prof / "Cookies")
    db.execute("create table cookies (host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite)")
    for host, name, value, host_hash in rows:
        db.execute("insert into cookies values (?,?,?,?,?,?,?,?,?)",
                   (host, name, "", enc(host, value, host_hash), "/", (1_800_000_000 + 11644473600) * 1_000_000, 1, 1, 1))
    db.commit()
    db.close()


def _firefox_profile(home, rows, folder="Firefox"):
    import sqlite3

    prof = home / "Library/Application Support" / folder / "Profiles" / "abc.default"
    prof.mkdir(parents=True)
    db = sqlite3.connect(prof / "cookies.sqlite")
    db.execute("create table moz_cookies (host, name, value, path, expiry, isSecure, isHttpOnly, sameSite)")
    for host, name, value in rows:
        db.execute("insert into moz_cookies values (?,?,?,?,?,?,?,?)", (host, name, value, "/", 1_800_000_000, 1, 1, 2))
    db.commit()
    db.close()


def _safari_file(home, rows):
    """Cookies.binarycookies with one page: rows are (host, name, value, flags)."""
    import struct

    recs = []
    for host, name, value, flags in rows:
        strings = [host, name, "/", value]
        offs, body = [], b""
        for t in strings:
            offs.append(56 + len(body))
            body += t.encode() + b"\0"
        size = 56 + len(body)
        recs.append(struct.pack("<IIII4IQdd", size, 0, flags, 0, offs[0], offs[1], offs[2], offs[3], 0,
                                1_800_000_000 - 978307200, 0) + body)
    head = 8 + 4 * len(recs) + 4
    offsets, pos = [], head
    for r in recs:
        offsets.append(pos)
        pos += len(r)
    page = struct.pack("<II", 0x100, len(recs)) + struct.pack(f"<{len(recs)}I", *offsets) + struct.pack("<I", 0) + b"".join(recs)
    f = home / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"cook" + struct.pack(">II", 1, len(page)) + page)


def test_browser_login_import_reads_only_the_requested_site(tmp_path, monkeypatch):
    from gtm import cookies as cc

    key = b"k" * 16
    _chrome_profile(tmp_path, key, [
        (".x.com", "auth_token", "secret-x", True), ("x.com", "ct0", "csrf", False),
        (".google.com", "SID", "google-secret", True), (".linkedin.com", "li_at", "li", True),
        (".notx.com", "auth_token", "lookalike", True)])
    monkeypatch.setattr(cc.Chromium, "_key", lambda self: key)
    found = cc.find_login("x", tmp_path).found
    assert found.source.name == "Arc"
    got = cc.import_login(found, "x")
    assert {c["name"]: c["value"] for c in got} == {"auth_token": "secret-x", "ct0": "csrf"}
    assert got[0]["expires"] == 1_800_000_000 and got[0]["sameSite"] == "Lax" and got[0]["httpOnly"]


def test_find_login_prefers_default_browser_and_needs_no_key(tmp_path, monkeypatch):
    from gtm import cookies as cc

    _chrome_profile(tmp_path, b"k" * 16, [(".x.com", "auth_token", "a", True)], "Arc/User Data")
    _chrome_profile(tmp_path, b"k" * 16, [(".x.com", "auth_token", "c", True)], "Google/Chrome")
    monkeypatch.setattr(cc.Chromium, "_key", lambda self: pytest.fail("key must not be read just to find a login"))
    monkeypatch.setattr(cc, "default_bundle", lambda: "com.google.Chrome")
    assert cc.find_login("x", tmp_path).found.source.name == "Chrome"
    monkeypatch.setattr(cc, "default_bundle", lambda: "company.thebrowser.browser")
    assert cc.find_login("x", tmp_path).found.source.name == "Arc"


def test_find_login_ignores_browsers_not_signed_in(tmp_path):
    from gtm import cookies as cc

    _chrome_profile(tmp_path, b"k" * 16, [(".x.com", "guest_id", "g", True)])
    assert cc.find_login("x", tmp_path).found is None
    assert cc.find_login("linkedin", tmp_path).found is None


def test_firefox_login_is_read_in_plain(tmp_path, monkeypatch):
    from gtm import cookies as cc

    _firefox_profile(tmp_path, [(".linkedin.com", "li_at", "tok"), (".example.com", "other", "no")], folder="zen")
    monkeypatch.setattr(cc, "default_bundle", lambda: "app.zen-browser.zen")
    found = cc.find_login("linkedin", tmp_path).found
    assert found.source.name == "Zen"
    (c,) = cc.import_login(found, "linkedin")
    assert (c["name"], c["value"], c["sameSite"], c["expires"]) == ("li_at", "tok", "Strict", 1_800_000_000)


def test_safari_login_parsed_from_binarycookies(tmp_path, monkeypatch):
    from gtm import cookies as cc

    _safari_file(tmp_path, [(".x.com", "auth_token", "safari-tok", 5), (".google.com", "SID", "nope", 0)])
    monkeypatch.setattr(cc, "default_bundle", lambda: "com.apple.Safari")
    found = cc.find_login("x", tmp_path).found
    assert found.source.name == "Safari"
    (c,) = cc.import_login(found, "x")
    assert (c["name"], c["value"], c["secure"], c["httpOnly"]) == ("auth_token", "safari-tok", True, True)


def test_unsupported_default_browser_is_explained(tmp_path, monkeypatch):
    from gtm import cookies as cc

    monkeypatch.setattr(cc, "default_bundle", lambda: "com.example.Weird")
    monkeypatch.setattr(cc, "app_name", lambda b: "Weird")
    lookup = cc.find_login("x", tmp_path)
    assert lookup.found is None and "Weird" in lookup.notes[0]


def test_login_falls_back_to_window_when_not_interactive():
    from gtm import browser

    assert browser._reuse_browser_login("x") is False  # pytest stdin is not a tty: never touches any browser or the Keychain


def test_popup_shows_when_each_draft_was_written_not_when_opened():
    from gtm.notify import render_page
    from gtm.status import when

    created = "2026-11-20T01:00:54.000Z"
    page = render_page([{"platform": "x", "text": "t", "why": "", "created_at": created}])
    assert when(created) in page


def test_a_manual_draft_counts_as_the_last_draft_so_ticks_dont_redo_it(conn, monkeypatch):
    from gtm import draft, llm, notify
    from gtm.db import kv_get

    monkeypatch.setattr(draft, "digest", lambda c, since, **k: "## project: p\nwork")
    monkeypatch.setattr(draft, "propose_angles", lambda m: [])
    monkeypatch.setattr(llm, "complete", lambda p, **k: '{"drafts":[{"platform":"x","text":"hello world","why":"w"}]}')
    monkeypatch.setattr(notify, "send_batch", lambda d: "stdout")
    draft.draft(conn, about="a topic")
    assert kv_get(conn, "last_draft_ts") is None
    (made,) = draft.draft(conn)
    assert kv_get(conn, "last_draft_ts") == made["created_at"]


def test_llm_failure_reports_the_real_reason_and_retries_once(monkeypatch):
    import subprocess

    from gtm import llm

    calls = []
    out = json.dumps({"is_error": True, "result": "You've hit your usage limit"})
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(1) or subprocess.CompletedProcess(a, 1, out, ""))
    with pytest.raises(RuntimeError, match="usage limit"):
        llm.complete("x")
    assert len(calls) == 2


def test_errors_log_a_clean_reason(conn):
    from gtm import status
    from gtm.db import record

    class TimeoutError_(Exception):
        pass

    TimeoutError_.__name__ = "TimeoutError"
    with record(conn, "sync_linkedin", swallow=True):
        raise TimeoutError_('Page.goto: Timeout 30000ms exceeded.\nCall log:\n  - navigating to "https://www.linkedin.com/in/me/", waiting until "domcontentloaded"')
    r = conn.execute("SELECT * FROM runs").fetchone()
    assert (r["status"], r["detail"]) == ("error", "TimeoutError: Page.goto: Timeout 30000ms exceeded. (https://www.linkedin.com/in/me/)")
    assert "why: TimeoutError: Page.goto: Timeout 30000ms exceeded. (https://www.linkedin.com/in/me/)" in status.render_text(status.collect(conn))


def _prompts(conn, n, parts=1):
    for i in range(n):
        for j in range(parts):
            conn.execute("INSERT INTO chunks(session_id, uuid, role, text, ts) VALUES('s',?,'user','a long enough prompt text here',?)",
                         (f"m{i}:{j}", f"2999-01-01T00:00:0{i}.000Z"))
    conn.commit()


def test_tick_skip_reasons_count_prompts_not_chunks(conn, monkeypatch):
    from gtm import draft

    monkeypatch.setattr(draft.search, "embed_pending", lambda c: 0)
    monkeypatch.setattr("gtm.ingest.ingest_all", lambda c: 0)
    monkeypatch.setattr("gtm.browser.sync_site", lambda c, s, r, force=False: r.skip("off"))
    assert draft.tick(conn, force=True) == "skip: no new Claude Code prompts since yesterday"
    _prompts(conn, 1, parts=5)  # one long prompt split into 5 chunks is still one prompt
    assert draft.tick(conn, force=True) == "skip: only 1 new prompt since yesterday (needs 3)"
    assert {r["step"]: r["status"] for r in conn.execute("SELECT * FROM runs")}["draft"] == "skip"


def test_tick_marks_learn_skipped_when_drafting_fails(conn, monkeypatch):
    from gtm import draft
    from gtm.db import kv_set

    def boom(*a, **k):
        raise RuntimeError("LLM command failed (1): usage limit")

    monkeypatch.setattr(draft, "draft", boom)
    monkeypatch.setattr(draft.search, "embed_pending", lambda c: 0)
    monkeypatch.setattr("gtm.ingest.ingest_all", lambda c: 0)
    monkeypatch.setattr("gtm.browser.sync_site", lambda c, s, r, force=False: r.skip("off"))
    _prompts(conn, 3)
    with pytest.raises(RuntimeError):
        draft.tick(conn, force=True)
    rows = {r["step"]: (r["status"], r["detail"]) for r in conn.execute("SELECT * FROM runs")}
    assert rows["draft"][0] == "error" and "usage limit" in rows["draft"][1]
    assert rows["learn"] == ("skip", "did not run because drafting failed")


def _session(conn, sid, project, ts, prompts, text="did the thing"):
    conn.execute("INSERT INTO sessions(session_id, path, project, first_ts, last_ts) VALUES(?,?,?,?,?)", (sid, "p", project, ts, ts))
    for i in range(prompts):
        conn.execute("INSERT INTO chunks(session_id, uuid, role, text, ts) VALUES(?,?,'user',?,?)", (sid, f"{sid}m{i}:0", f"{text} {sid} {i}", ts))
    conn.commit()


def test_session_picker_lists_five_then_more_and_returns_choices(conn, monkeypatch, capsys):
    from gtm import cli

    for i in range(8):  # s7 is the newest
        _session(conn, f"s{i}", f"/x/proj{i}", f"2026-11-2{i}T10:00:00.000Z", i + 1)

    prompts = []

    def run(*typed):
        answers = iter(typed)
        monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or next(answers))
        return cli._pick_sessions(conn)

    assert run("") is None  # Enter: all recent work
    capsys.readouterr()
    got = run("9", "m", "7, 2")  # 9 is not listed yet; m reveals the rest; then 7th and 2nd newest, in typed order
    out = capsys.readouterr().out
    assert "proj3" in out and out.index("proj3") < out.index("proj2")  # newest first
    assert "m = 3 more" in prompts[1] and "m =" not in prompts[-1]  # offered until everything is shown
    assert got == ["s1", "s6"]


def test_draft_from_picked_sessions_only_reads_those(conn, monkeypatch):
    from gtm import draft, llm, notify
    from gtm.db import kv_get

    _session(conn, "a", "/x/alpha", "2026-11-01T10:00:00.000Z", 2, text="alpha work")
    _session(conn, "b", "/x/beta", "2026-11-02T10:00:00.000Z", 2, text="beta work")
    seen = {}
    monkeypatch.setattr(draft, "propose_angles", lambda m: seen.setdefault("material", m) and [])
    monkeypatch.setattr(llm, "complete", lambda p, **k: '{"drafts":[{"platform":"x","text":"hello world","why":"w"}]}')
    monkeypatch.setattr(notify, "send_batch", lambda d: "stdout")
    draft.draft(conn, sessions=["a"])
    assert "alpha work" in seen["material"] and "beta work" not in seen["material"]
    assert kv_get(conn, "last_draft_ts") is None  # a hand-picked draft doesn't count as "everything covered"


def test_rules_md_goes_into_the_prompt_and_is_optional(tmp_path, monkeypatch):
    from gtm import draft
    monkeypatch.setenv("GTM_HOME", str(tmp_path))
    from gtm import db
    c = db.connect()
    assert "Never use emojis." not in draft.build_prompt("x", c)
    (tmp_path / "rules.md").write_text("Never use emojis.")
    assert "Never use emojis." in draft.build_prompt("x", c)


def test_show_prompt_prints_without_calling_the_model(monkeypatch, capsys):
    from gtm import db, draft, llm
    monkeypatch.setattr(draft, "digest", lambda c, since, **k: "did a thing")
    sent = []
    monkeypatch.setattr(llm, "complete", lambda p, *a, **k: sent.append(p) or '{"angles": []}')
    assert draft.draft(db.connect(), hours=1, show_prompt=True) == []
    assert not any("ghostwriting" in p for p in sent)  # the drafting prompt was shown, never sent
    assert "AUTHOR'S EXTRA RULES" in capsys.readouterr().out
