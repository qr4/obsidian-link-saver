"""yt-dlp cookies.txt management for the web UI.

Accepts what browser extensions export - Netscape cookies.txt ("Get
cookies.txt LOCALLY", Cookie-Editor's Netscape option) or Cookie-Editor's
JSON - validates it, and stores it as a Netscape file with the header line
yt-dlp insists on (without "# Netscape HTTP Cookie File" on the first line
yt-dlp rejects the whole file and every download fails).

Cookie values are secrets: they are written with mode 0600 and never
returned by the API - status() only reports counts, domains and expiry.
"""
import json
import os
import tempfile
import time
from pathlib import Path

from .config import settings

HEADER = "# Netscape HTTP Cookie File"
MAX_BYTES = 2 * 1024 * 1024
# Cookies that indicate a logged-in Google/YouTube session.
AUTH_COOKIES = {"SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
                "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID"}
YOUTUBE_DOMAINS = ("youtube.com", "google.com")


class CookieError(ValueError):
    pass


def _flag(value) -> str:
    return "TRUE" if value else "FALSE"


def parse_netscape(text: str) -> tuple:
    """Returns (cookies, bad_line_count). Each cookie is a dict carrying its
    line in canonical form (what gets stored)."""
    cookies, bad = [], 0
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        http_only = line.startswith("#HttpOnly_")
        body = line[len("#HttpOnly_"):] if http_only else line
        if not body.strip() or (body.startswith("#") and not http_only):
            continue
        fields = body.split("\t")
        if len(fields) == 6:  # some exporters drop the trailing tab of an empty value
            fields.append("")
        if len(fields) != 7 or fields[1].upper() not in ("TRUE", "FALSE") \
                or fields[3].upper() not in ("TRUE", "FALSE"):
            bad += 1
            continue
        try:
            expires = int(float(fields[4] or 0))
        except ValueError:
            bad += 1
            continue
        # Re-emitted with exactly 7 fields: yt-dlp silently skips a line
        # whose empty value lost its trailing tab.
        normalized = ("#HttpOnly_" if http_only else "") + "\t".join(fields)
        cookies.append({"domain": fields[0], "path": fields[2], "secure": fields[3].upper() == "TRUE",
                        "expires": expires, "name": fields[5], "line": normalized})
    return cookies, bad


def json_to_netscape(text: str) -> str:
    """Cookie-Editor / EditThisCookie JSON export -> Netscape lines."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise CookieError(f"not valid JSON: {e}") from e
    if isinstance(data, dict):
        data = data.get("cookies", [])
    if not isinstance(data, list):
        raise CookieError("expected a JSON array of cookies")
    lines = []
    for c in data:
        if not isinstance(c, dict) or not c.get("domain") or not c.get("name"):
            continue
        domain = c["domain"]
        host_only = c.get("hostOnly", not domain.startswith("."))
        if not host_only and not domain.startswith("."):
            domain = "." + domain
        expires = 0 if c.get("session") else int(float(c.get("expirationDate") or c.get("expires") or 0))
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append("\t".join([prefix + domain, _flag(not host_only), c.get("path") or "/",
                                _flag(c.get("secure")), str(expires), c["name"], str(c.get("value", ""))]))
    return "\n".join(lines)


def normalize(text: str) -> str:
    """Validated Netscape text with the mandatory header. Raises CookieError."""
    if len(text.encode()) > MAX_BYTES:
        raise CookieError("file too large for a cookies.txt")
    stripped = text.lstrip("﻿").strip()
    if not stripped:
        raise CookieError("empty file")
    if stripped[0] in "[{":
        stripped = json_to_netscape(stripped)
    cookies, bad = parse_netscape(stripped)
    if not cookies:
        raise CookieError("no cookies found - expected a Netscape cookies.txt "
                          "(tab-separated, 7 fields per line) or a Cookie-Editor JSON export")
    return HEADER + "\n" + "\n".join(c["line"] for c in cookies) + "\n"


def target_path() -> Path:
    return Path(settings.ytdlp_cookies) if settings.ytdlp_cookies else settings.config_dir / "cookies.txt"


def save(text: str) -> dict:
    content = normalize(text)
    path = target_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cookies-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return status()


def delete() -> bool:
    path = target_path()
    if path.exists():
        path.unlink()
        return True
    return False


def status() -> dict:
    """What the UI shows - never the cookie values themselves."""
    path = target_path()
    if not path.is_file():
        return {"present": False, "path": str(path)}
    stat = path.stat()
    try:
        cookies, bad = parse_netscape(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        return {"present": True, "path": str(path), "error": str(e)}
    now = time.time()
    youtube = [c for c in cookies if c["domain"].lstrip(".").endswith(YOUTUBE_DOMAINS)]
    auth = [c for c in youtube if c["name"] in AUTH_COOKIES]
    expiring = [c["expires"] for c in auth if c["expires"] > 0]
    domains = sorted({c["domain"].lstrip(".") for c in cookies})
    return {
        "present": True,
        "path": str(path),
        "count": len(cookies),
        "expired": sum(1 for c in cookies if 0 < c["expires"] < now),
        "domains": domains[:12],
        "domain_count": len(domains),
        "youtube": bool(youtube),
        "logged_in": bool(auth) and not all(0 < c["expires"] < now for c in auth),
        "auth_expires": min(expiring) if expiring else None,
        "updated_at": stat.st_mtime,
        "valid_header": path.read_text(encoding="utf-8", errors="replace").startswith(("# Netscape", "# HTTP Cookie")),
    }
