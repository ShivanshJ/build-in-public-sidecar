"""Copy one site's login cookies out of the user's everyday browser (macOS, Chromium family) into gtm's profile.

Only cookies whose host belongs to the requested site are read. Nothing is written back to the browser and the
cookie database is read from a temporary copy. Which browser has the login is found from cookie names alone; the
browser's decryption key (a Keychain prompt) is only requested once the user has agreed.
"""
import hashlib
import json
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

APP_SUPPORT = Path.home() / "Library/Application Support"


class Browser(NamedTuple):
    name: str
    bundle: str
    folder: str
    keychain: str


class Found(NamedTuple):
    browser: Browser
    db: Path


BROWSERS = [
    Browser("Arc", "company.thebrowser.browser", "Arc/User Data", "Arc Safe Storage"),
    Browser("Chrome", "com.google.Chrome", "Google/Chrome", "Chrome Safe Storage"),
    Browser("Brave", "com.brave.Browser", "BraveSoftware/Brave-Browser", "Brave Safe Storage"),
    Browser("Edge", "com.microsoft.edgemac", "Microsoft Edge", "Microsoft Edge Safe Storage"),
    Browser("Vivaldi", "com.vivaldi.Vivaldi", "Vivaldi", "Vivaldi Safe Storage"),
    Browser("Chromium", "org.chromium.Chromium", "Chromium", "Chromium Safe Storage"),
]
DOMAINS = {"x": ("x.com", "twitter.com"), "linkedin": ("linkedin.com",)}
AUTH_COOKIE = {"x": "auth_token", "linkedin": "li_at"}
SAMESITE = {0: "None", 1: "Lax", 2: "Strict"}


class CookieImportError(RuntimeError):
    pass


def safe_storage_key(service: str) -> bytes:
    """The browser's cookie key. Reading it makes macOS ask for the login password (Keychain)."""
    if sys.platform != "darwin":
        raise CookieImportError("cookie import only works on macOS")
    r = subprocess.run(["security", "find-generic-password", "-w", "-s", service], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise CookieImportError("could not read the browser's key from the Keychain (access denied?)")
    return hashlib.pbkdf2_hmac("sha1", r.stdout.strip().encode(), b"saltysalt", 1003, 16)


def decrypt(blob: bytes, key: bytes, host: str) -> str:
    if not blob.startswith((b"v10", b"v11")):
        return blob.decode("utf-8", "replace")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise CookieImportError("cookie import needs the 'cryptography' package: reinstall with `uv tool install --reinstall \".[browser]\"`") from None
    d = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    raw = d.update(blob[3:]) + d.finalize()
    raw = raw[: -raw[-1]]
    if raw[:32] == hashlib.sha256(host.encode()).digest():  # newer Chrome prefixes the host hash
        raw = raw[32:]
    return raw.decode("utf-8", "replace")


def _db_files(profile: Path) -> Path | None:
    for rel in ("Network/Cookies", "Cookies"):
        if (profile / rel).exists():
            return profile / rel
    return None


def _query(db: Path, sql: str, args: list) -> list:
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal", "-shm"):
            if Path(str(db) + suffix).exists():
                shutil.copy2(str(db) + suffix, Path(tmp) / ("Cookies" + suffix))
        conn = sqlite3.connect(Path(tmp) / "Cookies")
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()


def _host_filter(domains: tuple[str, ...]) -> tuple[str, list[str]]:
    return " OR ".join("host_key = ? OR host_key LIKE ?" for _ in domains), [a for d in domains for a in (d, f"%.{d}")]


def read_cookies(db: Path, key: bytes, domains: tuple[str, ...]) -> list[dict]:
    where, args = _host_filter(domains)
    rows = _query(db, "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite "
                      f"FROM cookies WHERE {where}", args)
    out = []
    for host, name, value, enc, path, expires, secure, httponly, samesite in rows:
        v = decrypt(enc, key, host) if enc else value
        if not v:
            continue
        c = {"name": name, "value": v, "domain": host, "path": path or "/", "secure": bool(secure), "httpOnly": bool(httponly)}
        if expires:
            c["expires"] = expires / 1_000_000 - 11644473600  # Chrome epoch is 1601
        if samesite in SAMESITE and (samesite != 0 or secure):
            c["sameSite"] = SAMESITE[samesite]
        out.append(c)
    return out


def default_bundle() -> str | None:
    try:
        plist = plistlib.loads((Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist").read_bytes())
        return next((h["LSHandlerRoleAll"] for h in plist["LSHandlers"] if h.get("LSHandlerURLScheme") == "https"), None)
    except (OSError, KeyError, ValueError):
        return None


def profiles(data_dir: Path) -> list[Path]:
    try:
        names = list(json.loads((data_dir / "Local State").read_text())["profile"]["info_cache"])
    except (OSError, KeyError, ValueError):
        names = ["Default"]
    return [data_dir / n for n in names if _db_files(data_dir / n)]


def _has_login(db: Path, site: str) -> bool:
    where, args = _host_filter(DOMAINS[site])
    return bool(_query(db, f"SELECT 1 FROM cookies WHERE name = ? AND ({where}) LIMIT 1", [AUTH_COOKIE[site], *args]))


def find_login(site: str, base: Path = APP_SUPPORT) -> Found | None:
    """The first browser profile (default browser first) holding a signed-in session for `site`. Reads cookie names only."""
    default = default_bundle()
    for b in sorted(BROWSERS, key=lambda b: b.bundle != default):
        for prof in profiles(base / b.folder):
            db = _db_files(prof)
            if _has_login(db, site):
                return Found(b, db)
    return None


def import_login(found: Found, site: str) -> list[dict]:
    key = safe_storage_key(found.browser.keychain)
    return read_cookies(found.db, key, DOMAINS[site])
