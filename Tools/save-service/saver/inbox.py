"""Watches Dumps/singlefile_inbox for pages saved with the SingleFile
browser extension + Companion (a real logged-in browser session on the
desktop, synced over to the server) and raw PDFs, and queues an import job
for each. Polling instead of inotify: inotify doesn't see changes made
through FUSE or network filesystems (e.g. Unraid user shares) reliably, and
this only needs to run a few times an hour."""
import threading
import time

from .config import settings
from .jobctx import log

# Sync tools write to a temp name and rename when complete (Syncthing:
# ".syncthing.<name>.tmp"), so a matching *.html/*.pdf is whole; the age
# check covers other writers.
MIN_FILE_AGE = 2.0


class InboxWatcher:
    def __init__(self, manager):
        self.manager = manager
        self._seen = set()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="inbox-watcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def scan_once(self) -> int:
        inbox = settings.singlefile_inbox_dir
        if not inbox.is_dir():
            return 0
        queued = 0
        for path in sorted(inbox.glob("*.html")) + sorted(inbox.glob("*.pdf")):
            key = str(path)
            if key in self._seen:
                continue
            try:
                if time.time() - path.stat().st_mtime < MIN_FILE_AGE:
                    continue
            except OSError:
                continue
            # Seen until restart: a failed import stays put for inspection
            # instead of being retried every poll.
            self._seen.add(key)
            self.manager.submit_file(path)
            log(f"Inbox: queued {path.name}")
            queued += 1
        return queued

    def _run(self):
        log(f"Watching {settings.singlefile_inbox_dir} for SingleFile captures and PDFs")
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as e:  # noqa: BLE001 - keep watching
                log(f"WARNING: inbox scan failed: {e}")
            self._stop.wait(settings.inbox_poll_seconds)
