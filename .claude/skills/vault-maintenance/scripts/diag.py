#!/usr/bin/env python3
"""Integrity diagnostics: stale/ambiguous links, duplicate URLs and pages."""
import re
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vault import Vault, VAULT, AREAS, norm_url, is_folder_note, WIKILINK_RE

v = Vault()
idx = v.link_index()

# ── stale + ambiguous links ────────────────────────────────────────────
stale, ambig = [], []
for rel, ents in v.raw.items():
    for i, text in enumerate(ents):
        # Shared with vault.Entry rather than a second copy of the pattern -
        # this one had already drifted into the [^\]]+ form that skips any
        # link whose target contains a bracket.
        for link in WIKILINK_RE.findall(text):
            t = v.clean_target(link)
            if not t:
                continue
            hits = idx.get(t, set())
            if not hits:
                stale.append((rel, i, t))
            elif len(hits) > 1:
                inside = {h for h in hits if h.startswith("Areas/")}
                # Only an Areas-vs-Areas tie actually mis-resolves a note
                # link; Areas-vs-Dumps ties Obsidian settles by proximity.
                if len(inside) > 1:
                    ambig.append((rel, i, t, sorted(inside)))

print(f"### STALE LINKS: {len(stale)}")
for rel, i, t in stale:
    print(f"  {rel} #{i} -> [[{t}]]")

print(f"\n### AMBIGUOUS (two Areas notes share a name): {len(ambig)}")
seen = set()
for rel, i, t, hits in ambig:
    if t in seen:
        continue
    seen.add(t)
    print(f"  [[{t}]] :: {hits}")

# ── note-name collisions anywhere in Areas ─────────────────────────────
by_stem = defaultdict(list)
for rel in v.raw:
    by_stem[Path(rel).stem].append(rel)
col = {k: sorted(x) for k, x in by_stem.items() if len(x) > 1}
print(f"\n### AREAS NOTE-NAME COLLISIONS: {len(col)}")
for k, x in sorted(col.items()):
    print(f"  {k}: {x}")

# ── duplicate URLs ─────────────────────────────────────────────────────
by_url = defaultdict(list)
for e in v.entries:
    for u in {norm_url(u) for u in e.urls}:
        by_url[u].append(e)
dupes = {u: es for u, es in by_url.items() if len(es) > 1}
print(f"\n### DUPLICATE URLS: {len(dupes)}")
for u, es in sorted(dupes.items(), key=lambda kv: -len(kv[1])):
    print(f"  {u}")
    for e in es:
        print(f"      {e.note} #{e.idx}")

# ── same attachment referenced twice ───────────────────────────────────
by_dump = defaultdict(list)
for e in v.entries:
    for l in e.links:
        t = v.clean_target(l)
        if t.startswith("Dumps/"):
            by_dump[t].append(e)
dd = {k: x for k, x in by_dump.items() if len(x) > 1}
print(f"\n### SAME ATTACHMENT IN MULTIPLE ENTRIES: {len(dd)}")
for k, x in dd.items():
    print(f"  {k}")
    for e in x:
        print(f"      {e.note} #{e.idx}")

# ── near-duplicate summaries (same page saved twice under two URLs) ────
def toks(s):
    return set(re.findall(r"[a-z]{4,}", s.lower()))

sig = {id(e): toks(e.summary) for e in v.entries}
pairs = []
buckets = defaultdict(list)
for e in v.entries:
    for t in list(sig[id(e)])[:40]:
        buckets[t].append(e)
cand = set()
for t, es in buckets.items():
    if len(es) < 60:
        for a, b in combinations(es, 2):
            cand.add((id(a), id(b)) if id(a) < id(b) else (id(b), id(a)))
byid = {id(e): e for e in v.entries}
for ia, ib in cand:
    a, b = byid[ia], byid[ib]
    ta, tb = sig[ia], sig[ib]
    if not ta or not tb:
        continue
    j = len(ta & tb) / len(ta | tb)
    if j > 0.55:
        pairs.append((round(j, 2), a, b))
pairs.sort(reverse=True, key=lambda x: x[0])
print(f"\n### NEAR-DUPLICATE ENTRIES (jaccard>0.55): {len(pairs)}")
for j, a, b in pairs:
    print(f"  {j}  {a.note} #{a.idx}  <->  {b.note} #{b.idx}")
    print(f"       A {a.urls[0] if a.urls else ''}")
    print(f"       B {b.urls[0] if b.urls else ''}")
    print(f"       {a.summary[:100]}")

# ── entries with no tags ───────────────────────────────────────────────
notag = [e for e in v.entries if not e.tags]
print(f"\n### ENTRIES WITHOUT TAGS: {len(notag)}")
for e in notag:
    print(f"  {e.note} #{e.idx}  [{e.host}] {e.summary[:70]}")

# ── oversized flat dirs (the save service's >15 rule) ────────────────────
print("\n### DIRS WITH >15 LEAF NOTES")
for d in sorted(set(AREAS.rglob("*"))):
    if not d.is_dir():
        continue
    n = [p for p in d.iterdir() if p.suffix == ".md" and p.stem != d.name]
    if len(n) > 15:
        print(f"  {len(n):>3}  {d.relative_to(AREAS)}")

# ── text files that aren't UTF-8 ──────────────────────────────────────
# Self-hosted LiveSync stores these extensions as text and decodes them as
# UTF-8, so any other encoding loses every non-ASCII character (U+FFFD) on
# every device, and the plugin reports the file as corrupted. Convert them
# to UTF-8 before they sync (a GBK-encoded repo README once did this).
LIVESYNC_TEXT = (".md", ".txt", ".svg", ".html", ".csv", ".css", ".js", ".xml", ".canvas")
not_utf8 = []
for root, dirs, files in __import__("os").walk(VAULT):
    dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
    for f in files:
        if f.startswith(".") or not f.endswith(LIVESYNC_TEXT):
            continue
        p = Path(root) / f
        try:
            p.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            not_utf8.append(p.relative_to(VAULT))
print(f"\n### TEXT FILES NOT IN UTF-8 (LiveSync would garble them): {len(not_utf8)}")
for p in not_utf8:
    print(f"  {p}")
