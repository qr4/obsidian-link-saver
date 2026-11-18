#!/usr/bin/env python3
"""Shared vault model: parse Areas/ leaf notes into entries, resolve links.

An "entry" is one top-level `- ` bullet plus its indented continuation
lines - the unit the save service appends per saved URL (tags line, summary, the
[[Dumps/...]] attachment link, the raw URL).
"""
import fcntl
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Walk up from this file (.claude/skills/vault-maintenance/scripts/) to the
# vault root, so the scripts work from any cwd and survive the vault moving.
# OBSIDIAN_VAULT overrides for running against a copy.
VAULT = Path(os.environ.get("OBSIDIAN_VAULT")
             or Path(__file__).resolve().parents[4])
AREAS = VAULT / "Areas"
LOCK = VAULT / ".vault-maintenance.lock"

if not AREAS.is_dir():
    raise SystemExit(f"no Areas/ under {VAULT} - set OBSIDIAN_VAULT to the vault root")

URL_RE = re.compile(r"https?://\S+")
MD_LINK_RE = re.compile(r"\[[^\]\n]*\]\((https?://[^)\s]+)\)")
# Lazy .+? up to the first ]], NOT [^\]]+ - attachment names routinely embed
# brackets (".../Asahi Linux [16292].mkv"), and an excluding class can never
# reach the closing ]] past one, so those links matched *nothing at all* and
# were silently omitted from every link check rather than reported broken.
WIKILINK_RE = re.compile(r"\[\[(.+?)\]\]")
TAG_LINE_RE = re.compile(r"^- ((?:#[a-z0-9][\w-]*\s*)+)$")


# ---------------------------------------------------------------------------
# The save service (Tools/save-service) owns the registry (its SQLite
# database) and appends to Area notes from another machine. Configure it with
# SAVE_SERVICE_URL / SAVE_SERVICE_TOKEN in the environment or in
# Tools/save-service/client.env. Unset, the scripts lock locally only and
# the registry tools refuse to run.
# ---------------------------------------------------------------------------
CLIENT_ENV = VAULT / "Tools" / "save-service" / "client.env"


def _service_config():
    values = {}
    if CLIENT_ENV.is_file():
        for line in CLIENT_ENV.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#"):
                values[key.strip()] = value.strip().strip('"').strip("'")
    url = os.environ.get("SAVE_SERVICE_URL") or values.get("SAVE_SERVICE_URL", "")
    token = os.environ.get("SAVE_SERVICE_TOKEN") or values.get("SAVE_SERVICE_TOKEN", "")
    return url.rstrip("/"), token


SERVICE_URL, SERVICE_TOKEN = _service_config()


def require_service():
    if not SERVICE_URL:
        raise SystemExit("set SAVE_SERVICE_URL (environment or Tools/save-service/client.env): "
                         "the registry lives in the save service")


def service_request(method, path, body=None, timeout=60):
    require_service()
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(SERVICE_URL + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if SERVICE_TOKEN:
        request.add_header("Authorization", f"Bearer {SERVICE_TOKEN}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


@contextmanager
def file_lock():
    """The local flock (other processes on this machine) plus, when the save
    service is configured, a lease on its vault lock: while held, the
    service keeps downloading but does not append to Area notes, so a
    migration's read-modify-write can't lose a freshly saved entry. If the
    service is unreachable it isn't writing either - warn and carry on."""
    LOCK.touch(exist_ok=True)
    with open(LOCK, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        lease = None
        if SERVICE_URL:
            try:
                lease = service_request("POST", "/api/vault-lock", {"ttl": 1800, "wait": 120},
                                        timeout=150)["lease_id"]
            except urllib.error.HTTPError as e:
                fcntl.flock(f, fcntl.LOCK_UN)
                raise SystemExit(f"save service refused the vault lock ({e.code}) - it is mid-write "
                                 "or another tool holds it; try again") from e
            except (urllib.error.URLError, OSError) as e:
                print(f"WARNING: save service at {SERVICE_URL} unreachable ({e}); "
                      "locking locally only", file=sys.stderr)
        try:
            yield
        finally:
            if lease:
                try:
                    service_request("DELETE", f"/api/vault-lock/{lease}")
                except (urllib.error.URLError, OSError) as e:
                    print(f"WARNING: could not release the service lease ({e}); "
                          "it expires on its own in 30 minutes", file=sys.stderr)
            fcntl.flock(f, fcntl.LOCK_UN)


def load_registry():
    """The saved-URL registry from the save service, keyed by entry key."""
    return service_request("GET", "/api/registry")


REGISTRY_FIELDS = ("url", "note", "filename", "area_note", "area_entry")


def save_registry(new, old):
    """Persist registry edits: only the changed fields of changed records
    are sent to the service."""
    patches = []
    for key, record in new.items():
        before = old.get(key, {})
        changed = {f: record[f] for f in REGISTRY_FIELDS if f in record and record[f] != before.get(f)}
        removed = [f for f in REGISTRY_FIELDS if f in before and f not in record]
        if changed or removed:
            patches.append({"key": key, "set": changed, "unset": removed})
    result = service_request("POST", "/api/registry/patch", patches) if patches else {"updated": 0}
    return f"save service updated: {result}"


def is_folder_note(rel):
    p = Path(rel)
    return p.stem == p.parent.name or rel == "Areas.md"


def split_entries(text):
    lines = text.splitlines(keepends=True)
    pre, entries, cur = [], [], None
    for line in lines:
        if line.startswith("- ") or line.rstrip("\n") == "-":
            if cur is not None:
                entries.append("".join(cur))
            cur = [line]
        elif cur is not None:
            cur.append(line)
        else:
            pre.append(line)
    if cur is not None:
        entries.append("".join(cur))
    return "".join(pre), entries


def extract_urls(text):
    """URLs in an entry, tolerating spaces inside them.

    The service writes the source URL on its own indented line, and some sites
    put literal spaces in a path (lcamtuf's comic scans:
    .../communism/Love Letters 30: Love Thine Enemy). A bare \\S+ match stops
    at the first space, so two unrelated pages both truncated to
    ".../communism/Love" and were reported as a duplicate URL for weeks.
    Take a whole line when the line *is* a URL; fall back to the loose
    pattern for URLs sitting inside prose."""
    urls = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^https?://", stripped):
            urls.append(stripped)
        else:
            # [label](url) - the save service's "▶ Watch" / "Browse files"
            # links - would otherwise be captured with the closing ")".
            stripped = MD_LINK_RE.sub(r" \1 ", stripped)
            urls.extend(URL_RE.findall(stripped))
    return urls


def is_content(entry):
    """A saved item rather than a Waypoint index line. Hand-written notes
    count, such as a long note about a tool that links no URL."""
    if re.search(r"https?://|\[\[Dumps/", entry):
        return True
    return len(re.sub(r"\s+", " ", entry).strip()) > 120


class Entry:
    def __init__(self, note, idx, text):
        self.note, self.idx, self.text = note, idx, text
        first = text.splitlines()[0] if text.splitlines() else ""
        m = TAG_LINE_RE.match(first.rstrip())
        self.tags = re.findall(r"#([a-z0-9][\w-]*)", m.group(1)) if m else []
        self.urls = extract_urls(text)
        self.links = WIKILINK_RE.findall(text)
        body = [l.strip() for l in text.splitlines()[1 if m else 0:]]
        # link lines ([[attachment]], [▶ Watch](...) · [[...|Transcript]],
        # bare URLs) are not part of the summary
        body = [l for l in body
                if l and not l.startswith("[") and not URL_RE.match(l)]
        self.summary = " ".join(body).lstrip("- ").strip()

    @property
    def host(self):
        if not self.urls:
            return "-"
        h = urlparse(self.urls[0]).netloc.lower()
        return h[4:] if h.startswith("www.") else h

    def __repr__(self):
        return f"<{self.note}#{self.idx} {self.tags}>"


class Vault:
    def __init__(self):
        self.raw, self.preamble = {}, {}
        for p in sorted(AREAS.rglob("*.md")):
            rel = str(p.relative_to(AREAS))
            pre, ents = split_entries(p.read_text(encoding="utf-8"))
            self.raw[rel], self.preamble[rel] = ents, pre
        self.entries = [Entry(rel, i, t)
                        for rel, ents in self.raw.items()
                        for i, t in enumerate(ents) if is_content(t)]
        self.by_note = defaultdict(list)
        for e in self.entries:
            self.by_note[e.note].append(e)

    def leaves(self):
        return {r: self.by_note.get(r, []) for r in self.raw if not is_folder_note(r)}

    # -- obsidian-style link resolution over the whole vault
    def link_index(self):
        idx = defaultdict(set)
        for p in VAULT.rglob("*"):
            if p.is_dir():
                continue
            rel = str(p.relative_to(VAULT))
            if any(part.startswith(".") and part not in (".config",)
                   for part in Path(rel).parts[:-1]):
                continue
            idx[rel].add(rel)
            idx[p.name].add(rel)
            if p.suffix == ".md":
                idx[rel[:-3]].add(rel)
                idx[p.stem].add(rel)
        return idx

    @staticmethod
    def clean_target(link):
        t = link.split("|")[0].strip()
        # only strip a #heading anchor, never a # that is part of a filename
        if "#" in t and not re.search(r"#\d", t):
            t = t.split("#")[0].strip()
        return t


def norm_url(url):
    url = url.strip().rstrip(").,;'\"")
    p = urlparse(url)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith("youtu.be"):
        return "yt:" + p.path.strip("/")
    q = parse_qs(p.query)
    if "youtube.com" in host and "v" in q:
        return "yt:" + q["v"][0]
    drop = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "ref", "ref_src", "source", "fbclid", "gclid", "si", "t", "feature",
            "ab_channel", "pp", "list", "index", "spm"}
    keep = {k: vv for k, vv in q.items() if k not in drop}
    qs = "&".join(f"{k}={vv[0]}" for k, vv in sorted(keep.items()))
    return f"{host}{p.path.rstrip('/')}" + (f"?{qs}" if qs else "")
