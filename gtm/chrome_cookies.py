"""Copy one site's login cookies out of the user's everyday Chrome (macOS) so gtm's own profile starts signed in.

Only cookies whose host belongs to the requested site are read. Nothing is written back to Chrome and the cookie
database is read from a temporary copy.
"""
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME = Path.home() / "Library/Application Support/Google/Chrome"
DOMAINS = {"x": ("x.com", "twitter.com"), "linkedin": ("linkedin.com",)}
AUTH_COOKIE = {"x": "auth_token", "linkedin": "li_at"}
SAMESITE = {0: "None", 1: "Lax", 2: "Strict"}


class CookieImportError(RuntimeError):
    pass


def safe_storage_key() -> bytes:
    """Chrome's cookie key. Reading it makes macOS ask for the login password (Keychain)."""
    if sys.platform != "darwin":
        raise CookieImportError("cookie import only works on macOS")
    r = subprocess.run(["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise CookieImportError("could not read Chrome's key from the Keychain (access denied, or Chrome is not installed)")
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


def read_cookies(db: Path, key: bytes, domains: tuple[str, ...]) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal", "-shm"):
            if Path(str(db) + suffix).exists():
                shutil.copy2(str(db) + suffix, Path(tmp) / ("Cookies" + suffix))
        conn = sqlite3.connect(Path(tmp) / "Cookies")
        try:
            where = " OR ".join("host_key = ? OR host_key LIKE ?" for _ in domains)
            args = [a for d in domains for a in (d, f"%.{d}")]
            rows = conn.execute(
                f"SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite "
                f"FROM cookies WHERE {where}", args).fetchall()
        finally:
            conn.close()
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


def profiles() -> list[Path]:
    try:
        info = json.loads((CHROME / "Local State").read_text())["profile"]["info_cache"]
        names = list(info)
    except (OSError, KeyError, ValueError):
        names = ["Default"]
    return [CHROME / n for n in names if _db_files(CHROME / n)]


def import_login(site: str, root_profiles: list[Path] | None = None) -> list[dict]:
    """The site's cookies from the first Chrome profile that is signed in to it."""
    found = root_profiles if root_profiles is not None else profiles()
    if not found:
        raise CookieImportError("no Chrome profile found")
    key = safe_storage_key()
    for prof in found:
        cookies = read_cookies(_db_files(prof), key, DOMAINS[site])
        if any(c["name"] == AUTH_COOKIE[site] for c in cookies):
            return cookies
    raise CookieImportError(f"none of your Chrome profiles is signed in to {site}")
