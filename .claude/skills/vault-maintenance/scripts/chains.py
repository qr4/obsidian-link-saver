#!/usr/bin/env python3
"""Recover post series from the archive's own back-links.

Slug matching only finds series whose numbering survives into the URL, which
is how MCU land part 1 ("fear-and-loathing-in-the-world-of") and Cursed
circuits #1 were both missed. The author instead marks continuity in prose -
each instalment links to its predecessor as "previous article" - so the
back-link graph is the reliable source, and it catches series that were never
numbered at all.
"""
import glob
import re
import sys
from collections import defaultdict
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from vault import Vault, VAULT

# Which crawled site to analyse: pass the Dumps/single_page/<host> directory
# name, e.g. `chains.py lcamtuf.coredump.cx`.
if len(sys.argv) < 2:
    raise SystemExit(f"usage: {Path(__file__).name} <host-dir under Dumps/single_page/>")
ARCHIVE = str(VAULT / "Dumps" / "single_page" / sys.argv[1])
BACKREF = re.compile(
    r"previous (?:article|post)|previously (?:written|discussed)|earlier article"
    r"|part 1\b|first (?:article|post|instal)", re.I)
MONTH = re.compile(r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                   r"\s+\d{1,2},\s+20\d\d)")

# slug -> (title, date, predecessor slug)
meta, pred = {}, {}
for p in glob.glob(f"{ARCHIVE}/*.html"):
    soup = BeautifulSoup(open(p, encoding="utf-8", errors="ignore").read(), "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    canon = soup.find("link", rel="canonical")
    self_slug = ""
    if canon and "/p/" in canon.get("href", ""):
        self_slug = canon["href"].split("/p/")[-1].rstrip("/")
    if not self_slug:
        continue
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    m = MONTH.search(text)
    meta[self_slug] = (Path(p).name.split(" (2026")[0], m.group(1) if m else "?")
    for a in soup.find_all("a", href=True):
        if "/p/" not in a["href"]:
            continue
        tgt = a["href"].split("/p/")[-1].rstrip("/")
        if tgt != self_slug and BACKREF.search(a.get_text(" ", strip=True)):
            pred.setdefault(self_slug, tgt)

# walk predecessor edges into chains
succ = defaultdict(list)
for child, parent in pred.items():
    succ[parent].append(child)
heads = [s for s in set(list(pred.values()) + list(pred)) if s not in pred]

v = Vault()
note_of = {}
for e in v.entries:
    for u in e.urls:
        if "blog.coredump.cx/p/" in u:
            note_of.setdefault(u.split("/p/")[-1].split("?")[0].rstrip("/"), e.note)


def walk(s, depth=0, out=None):
    out = out if out is not None else []
    out.append((depth, s))
    for c in sorted(succ.get(s, []), key=lambda x: meta.get(x, ("", "zz"))[1]):
        walk(c, depth + 1, out)
    return out


print(f"archived posts: {len(meta)}   back-link edges: {len(pred)}\n")
for h in sorted(heads, key=lambda s: meta.get(s, ("", "zz"))[1]):
    chain = walk(h)
    if len(chain) < 2:
        continue
    areas = {str(Path(note_of[s]).parent) for _, s in chain if s in note_of}
    span = "  ⚠ SPANS AREAS" if len(areas) > 1 else ""
    print(f"══ chain of {len(chain)}{span}")
    for d, s in chain:
        title, date = meta.get(s, (s, "?"))
        where = note_of.get(s, "*** not in vault ***")
        print(f"   {'  ' * d}{title[:46]:<46} {date:<14} {where}")
    print()
