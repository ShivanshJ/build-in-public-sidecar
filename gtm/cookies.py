"""Copy one site's login cookies out of the browser you already use (macOS) into gtm's own browser profile.

Each browser family is a `Source` that knows where its cookies live and how to read them; `SOURCES` is the registry
(add a browser by adding a line). Only the requested site's cookies are read, nothing is written back, and databases
are read from a temporary copy. Finding which browser holds a login uses cookie names only; any decryption key
(Chromium's Keychain prompt) is requested later, after the user agreed.
"""
import hashlib
import json
import plistlib
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import NamedTuple

DOMAINS = {"x": ("x.com", "twitter.com"), "linkedin": ("linkedin.com",)}
AUTH_COOKIE = {"x": "auth_token", "linkedin": "li_at"}
SAMESITE = {0: "None", 1: "Lax", 2: "Strict"}
APP_SUPPORT = "Library/Application Support"


class CookieImportError(RuntimeError):
    pass


def _cookie(domain, name, value, path, expires, secure, httponly, samesite=-1) -> dict:
    c = {"name": name, "value": value, "domain": domain, "path": path or "/", "secure": bool(secure), "httpOnly": bool(httponly)}
    if expires:
        c["expires"] = expires
    if samesite in SAMESITE and (samesite != 0 or secure):
        c["sameSite"] = SAMESITE[samesite]
    return c


def _belongs(host: str, site: str) -> bool:
    return any(host.lstrip(".") == d or host.endswith("." + d) for d in DOMAINS[site])


def _host_filter(column: str, site: str) -> tuple[str, list[str]]:
    return (" OR ".join(f"{column} = ? OR {column} LIKE ?" for _ in DOMAINS[site]),
            [a for d in DOMAINS[site] for a in (d, f"%.{d}")])


def _query(db: Path, sql: str, args: list) -> list:
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal", "-shm"):
            if Path(str(db) + suffix).exists():
                shutil.copy2(str(db) + suffix, Path(tmp) / ("db" + suffix))
        conn = sqlite3.connect(Path(tmp) / "db")
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()


class Source(ABC):
    """A browser family. `store` is one profile's cookie file."""

    def __init__(self, name: str, bundle: str):
        self.name, self.bundle = name, bundle

    @abstractmethod
    def stores(self, home: Path) -> list[Path]: ...

    @abstractmethod
    def cookie_names(self, store: Path, site: str) -> set[str]:
        """Names only, no secrets: lets us find the signed-in browser without any permission prompt."""

    @abstractmethod
    def cookies(self, store: Path, site: str) -> list[dict]: ...


class Chromium(Source):
    """Arc, Chrome, Brave, Edge, Vivaldi: encrypted SQLite, key in the macOS Keychain."""

    def __init__(self, name, bundle, folder, keychain):
        super().__init__(name, bundle)
        self.folder, self.keychain = folder, keychain

    @staticmethod
    def _db(profile: Path) -> Path | None:
        return next((profile / r for r in ("Network/Cookies", "Cookies") if (profile / r).exists()), None)

    def stores(self, home):
        data = home / APP_SUPPORT / self.folder
        try:
            names = list(json.loads((data / "Local State").read_text())["profile"]["info_cache"])
        except (OSError, KeyError, ValueError):
            names = ["Default"]
        return [db for n in names if (db := self._db(data / n))]

    def cookie_names(self, store, site):
        where, args = _host_filter("host_key", site)
        return {r[0] for r in _query(store, f"SELECT name FROM cookies WHERE {where}", args)}

    def _key(self) -> bytes:
        r = subprocess.run(["security", "find-generic-password", "-w", "-s", self.keychain], capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            raise CookieImportError(f"could not read {self.name}'s key from the Keychain (access denied?)")
        return hashlib.pbkdf2_hmac("sha1", r.stdout.strip().encode(), b"saltysalt", 1003, 16)

    @staticmethod
    def _decrypt(blob: bytes, key: bytes, host: str) -> str:
        if not blob.startswith((b"v10", b"v11")):
            return blob.decode("utf-8", "replace")
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            raise CookieImportError("needs the 'cryptography' package: `uv tool install --reinstall \".[browser]\"`") from None
        d = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        raw = d.update(blob[3:]) + d.finalize()
        raw = raw[: -raw[-1]]
        if raw[:32] == hashlib.sha256(host.encode()).digest():  # newer Chromium prefixes the host hash
            raw = raw[32:]
        return raw.decode("utf-8", "replace")

    def cookies(self, store, site):
        where, args = _host_filter("host_key", site)
        rows = _query(store, "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite "
                             f"FROM cookies WHERE {where}", args)
        key = self._key() if any(r[3] for r in rows) else b""
        out = []
        for host, name, value, enc, path, expires, secure, httponly, samesite in rows:
            v = self._decrypt(enc, key, host) if enc else value
            if v:
                out.append(_cookie(host, name, v, path, expires and expires / 1_000_000 - 11644473600, secure, httponly, samesite))
        return out


class Firefox(Source):
    """Firefox and Firefox forks (Zen): plain SQLite, no key."""

    def __init__(self, name, bundle, folder):
        super().__init__(name, bundle)
        self.folder = folder

    def stores(self, home):
        return sorted((home / APP_SUPPORT / self.folder / "Profiles").glob("*/cookies.sqlite"))

    def cookie_names(self, store, site):
        where, args = _host_filter("host", site)
        return {r[0] for r in _query(store, f"SELECT name FROM moz_cookies WHERE {where}", args)}

    def cookies(self, store, site):
        where, args = _host_filter("host", site)
        rows = _query(store, f"SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite FROM moz_cookies WHERE {where}", args)
        return [_cookie(h, n, v, p, e / 1000 if e > 1e11 else e, s, o, ss) for h, n, v, p, e, s, o, ss in rows if v]


class Safari(Source):
    """Safari's Cookies.binarycookies. Reading it needs Full Disk Access for the terminal."""

    PATH = "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies"
    MAC_EPOCH = 978307200

    def stores(self, home):
        return [home / self.PATH]

    def _records(self, store: Path):
        try:
            data = store.read_bytes()
        except FileNotFoundError:
            return
        except PermissionError:
            raise CookieImportError("Safari's cookies are protected: give your terminal Full Disk Access "
                                    "(System Settings > Privacy & Security) and try again") from None
        if data[:4] != b"cook":
            raise CookieImportError("unrecognized Safari cookie file")
        (n,) = struct.unpack(">I", data[4:8])
        pos = 8 + 4 * n
        for size in struct.unpack(f">{n}I", data[8:pos]):
            page, pos = data[pos:pos + size], pos + size
            (count,) = struct.unpack("<I", page[4:8])
            for off in struct.unpack(f"<{count}I", page[8:8 + 4 * count]):
                rec = page[off:]
                (flags,) = struct.unpack("<I", rec[8:12])
                url, name, path, value = struct.unpack("<4I", rec[16:32])
                (expiry,) = struct.unpack("<d", rec[40:48])
                text = lambda o: rec[o:rec.index(b"\0", o)].decode("utf-8", "replace")
                yield text(url), text(name), text(value), text(path), expiry + self.MAC_EPOCH, flags

    def cookie_names(self, store, site):
        return {name for host, name, *_ in self._records(store) if _belongs(host, site)}

    def cookies(self, store, site):
        return [_cookie(host, name, value, path, expires if expires > time.time() else 0, flags & 1, flags & 4)
                for host, name, value, path, expires, flags in self._records(store) if _belongs(host, site) and value]


SOURCES: list[Source] = [
    Chromium("Arc", "company.thebrowser.browser", "Arc/User Data", "Arc Safe Storage"),
    Chromium("Chrome", "com.google.Chrome", "Google/Chrome", "Chrome Safe Storage"),
    Chromium("Brave", "com.brave.Browser", "BraveSoftware/Brave-Browser", "Brave Safe Storage"),
    Chromium("Edge", "com.microsoft.edgemac", "Microsoft Edge", "Microsoft Edge Safe Storage"),
    Chromium("Vivaldi", "com.vivaldi.Vivaldi", "Vivaldi", "Vivaldi Safe Storage"),
    Chromium("Chromium", "org.chromium.Chromium", "Chromium", "Chromium Safe Storage"),
    Firefox("Firefox", "org.mozilla.firefox", "Firefox"),
    Firefox("Zen", "app.zen-browser.zen", "zen"),
    Safari("Safari", "com.apple.Safari"),
]


class Found(NamedTuple):
    source: Source
    store: Path


class Lookup(NamedTuple):
    found: Found | None
    notes: list[str]  # why the default browser couldn't be used


def default_bundle() -> str | None:
    """The bundle id of the default web browser, from macOS LaunchServices."""
    try:
        plist = plistlib.loads((Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist").read_bytes())
        return next((h["LSHandlerRoleAll"] for h in plist["LSHandlers"] if h.get("LSHandlerURLScheme") == "https"), None)
    except (OSError, KeyError, ValueError):
        return None


def app_name(bundle: str) -> str:
    r = subprocess.run(["mdfind", f"kMDItemCFBundleIdentifier == '{bundle}'"], capture_output=True, text=True, timeout=5)
    return Path(r.stdout.splitlines()[0]).stem if r.stdout.strip() else bundle


def find_login(site: str, home: Path = Path.home()) -> Lookup:
    """The first browser profile (default browser first) holding a signed-in session for `site`."""
    if sys.platform != "darwin":
        return Lookup(None, ["login reuse only works on macOS"])
    default, notes = default_bundle(), []
    if default and all(s.bundle != default for s in SOURCES):
        try:
            notes.append(f"your default browser ({app_name(default)}) can't be reused")
        except (OSError, subprocess.SubprocessError):
            notes.append("your default browser can't be reused")
    for src in sorted(SOURCES, key=lambda s: s.bundle != default):
        try:
            for store in src.stores(home):
                if AUTH_COOKIE[site] in src.cookie_names(store, site):
                    return Lookup(Found(src, store), [])
        except CookieImportError as e:
            if src.bundle == default:
                notes.append(str(e))
    return Lookup(None, notes)


def import_login(found: Found, site: str) -> list[dict]:
    return found.source.cookies(found.store, site)
