#!/usr/bin/env python3
"""Regenerate Waypoint folder-note indexes after a restructure.

There must be exactly one writer of these indexes, or sync conflicts
follow (two machines rewriting the same folder note before they sync). So:

  - with the save service configured (SAVE_SERVICE_URL, see vault.py) and
    managing waypoints, this only asks the service to refresh; nothing is
    written from this machine
  - otherwise it writes the indexes locally with the service's generator
    (Tools/save-service/saver/waypoints.py), which reproduces the Waypoint
    plugin's output exactly

New folders still need a folder note carrying the Waypoint flag; the
generator fills every folder note that has the flag or a Begin/End block.
"""
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vault import SERVICE_URL, VAULT, file_lock, service_request  # noqa: E402

sys.dont_write_bytecode = True  # no __pycache__ inside the synced vault
sys.path.insert(0, str(VAULT / "Tools" / "save-service"))
from saver import waypoints  # noqa: E402

APPLY = "--apply" in sys.argv


def create_missing_folder_notes() -> list:
    """Restructures create directories; give each one a flag-only folder
    note for the generator to fill. Only this script creates them, so it
    stays a single writer."""
    missing = [d / f"{d.name}.md" for d in [VAULT / "Areas", *(VAULT / "Areas").rglob("*")]
               if d.is_dir() and not d.name.startswith(".") and not (d / f"{d.name}.md").exists()]
    if APPLY:
        for note in missing:
            note.write_text(waypoints.WAYPOINT_FLAG + "\n", encoding="utf-8")
    for note in missing:
        print(f"  {'created' if APPLY else 'would create'}: {note.relative_to(VAULT)}")
    return missing


with file_lock():
    create_missing_folder_notes()

if SERVICE_URL:
    if not APPLY:
        print(f"{len(waypoints.plan(VAULT))} folder notes are stale; the save service at {SERVICE_URL} "
              "regenerates them (run with --apply to trigger it now)")
        sys.exit(0)
    try:
        changed = service_request("POST", "/api/waypoints/refresh")["changed"]
        print(f"save service regenerated {len(changed)} folder notes")
        sys.exit(0)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
        print("WARNING: the save service doesn't manage waypoints (MANAGE_WAYPOINTS off) - writing locally")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"save service unreachable ({e}); not writing locally - that would make two writers")

changes = waypoints.plan(VAULT)
for note, _ in changes:
    print(f"  {note.relative_to(VAULT)}")
if APPLY:
    with file_lock():
        written = waypoints.refresh(VAULT)
    print(f"total writes: {len(written)}")
else:
    print(f"{len(changes)} folder notes would change (dry run; --apply to write)")
