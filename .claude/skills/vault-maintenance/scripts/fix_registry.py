#!/usr/bin/env python3
"""Re-point the saved-URL registry at post-migration note paths.

Works against the save service's database (SAVE_SERVICE_URL, see vault.py).

A forced re-save locates a prior bullet by searching Areas/<area_note> for the
stored `area_entry` text; a moved note would leave that pointing nowhere and a
re-save would append a duplicate. Re-derive each record's home by searching the
new tree for its bullet, which also repairs records stale from before.
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vault import AREAS, SERVICE_URL, file_lock, load_registry, save_registry

APPLY = "--apply" in sys.argv

# Notes whose bullets were deliberately dropped, mapped to the note that kept
# the surviving entry for the same subject, e.g.
#   "programming/low-level/circuits.md": "electronics/fundamentals/circuits.md",
DROPPED_INTO = {}

notes = {str(p.relative_to(AREAS)): p.read_text(encoding="utf-8")
         for p in sorted(AREAS.rglob("*.md"))}
original = load_registry()
reg = copy.deepcopy(original)
fixed = unresolved = unchanged = 0
report = []

for key, rec in reg.items():
    entry = rec.get("area_entry")
    if not entry:
        continue
    old = rec.get("area_note")
    hits = [rel for rel, text in notes.items() if entry in text]
    if len(hits) == 1:
        new = hits[0]
        if new != old:
            rec["area_note"], rec["note"] = new, f"Areas/{new}"
            fixed += 1
            report.append(f"  {old} -> {new}")
        else:
            unchanged += 1
        continue

    # Bullet text changed (a retag), so match on the URL instead. Compare
    # whole lines: a bare substring test matches URL prefixes and mis-homes
    # records whose URL is a prefix of a sibling's.
    url, fname = rec.get("url", ""), rec.get("filename", "")
    found = None
    for rel, text in notes.items():
        for bullet in text.split("\n- "):
            lines = {l.strip() for l in bullet.splitlines()}
            if (url and url in lines) or (fname and any(fname in l for l in lines)):
                found = (rel, bullet if bullet.startswith("- ") else "- " + bullet)
                break
        if found:
            break
    if found:
        new, bullet = found
        rec["area_note"], rec["note"] = new, f"Areas/{new}"
        rec["area_entry"] = bullet if bullet.endswith("\n") else bullet + "\n"
        fixed += 1
        report.append(f"  {old} -> {new}  (entry text refreshed)")
    else:
        # Bullet deliberately dropped. Keep the record so the URL still
        # dedupes, but clear area_entry so --force appends cleanly instead of
        # warning about a bullet that no longer exists.
        rec.pop("area_entry", None)
        if old and not (AREAS / old).exists():
            new = DROPPED_INTO.get(old)
            if new:
                rec["area_note"], rec["note"] = new, f"Areas/{new}"
            else:
                rec.pop("area_note", None)
        unresolved += 1
        report.append(f"  DROPPED (area_entry cleared): {old}  [{key}]")

print("\n".join(report[-40:]))
print(f"\nrepointed: {fixed}  unchanged: {unchanged}  dropped: {unresolved}")
print(f"registry source: {SERVICE_URL}")
if APPLY:
    with file_lock():
        print(save_registry(reg, original))
