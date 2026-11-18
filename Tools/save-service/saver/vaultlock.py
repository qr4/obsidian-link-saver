"""Serializes writes to shared vault files (Area notes, graph.json,
Waypoint folder notes).

Three kinds of writer take turns:
  - worker threads inside this service
  - other processes on the same host (the CLI run inside the container),
    via an flock on CONFIG_DIR/vault.lock
  - tools elsewhere - the vault-maintenance skill restructuring Areas/ on
    the desktop - which take a time-limited *lease* over the HTTP API.
    While a lease is held, workers keep downloading and transcribing but
    block at the point where they would append to an Area note.

The flock lives in CONFIG_DIR rather than in the vault: flock on a FUSE or
network filesystem (e.g. an Unraid user share) is not reliable, and nothing outside this host could
honour it anyway.
"""
import fcntl
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class VaultLock:
    def __init__(self, lock_path_getter):
        self._lock_path_getter = lock_path_getter
        self._cond = threading.Condition()
        self._held = False
        self._lease_id = None
        self._lease_expires = 0.0

    def _lease_active(self) -> bool:
        if self._lease_id and time.monotonic() >= self._lease_expires:
            self._lease_id = None
        return self._lease_id is not None

    @contextmanager
    def __call__(self):
        with self._cond:
            while self._held or self._lease_active():
                # wake up periodically so an expired lease is noticed
                self._cond.wait(timeout=1.0)
            self._held = True
        try:
            with self._flock():
                yield
        finally:
            with self._cond:
                self._held = False
                self._cond.notify_all()

    @contextmanager
    def _flock(self):
        path = Path(self._lock_path_getter())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a")
        except OSError:
            yield  # best effort: the in-process lock above still applies
            return
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # ---- remote leases --------------------------------------------------
    def acquire_lease(self, ttl: float, wait: float = 30.0):
        """Returns a lease id, or None if the lock could not be had within
        `wait` seconds (a worker is mid-write, or another lease is held)."""
        deadline = time.monotonic() + wait
        with self._cond:
            while self._held or self._lease_active():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=min(remaining, 1.0))
            self._lease_id = uuid.uuid4().hex
            self._lease_expires = time.monotonic() + ttl
            return self._lease_id

    def renew_lease(self, lease_id: str, ttl: float) -> bool:
        with self._cond:
            if not self._lease_active() or lease_id != self._lease_id:
                return False
            self._lease_expires = time.monotonic() + ttl
            return True

    def release_lease(self, lease_id: str) -> bool:
        with self._cond:
            if lease_id != self._lease_id:
                return False
            self._lease_id = None
            self._cond.notify_all()
            return True

    def status(self) -> dict:
        with self._cond:
            active = self._lease_active()
            return {
                "writing": self._held,
                "lease_held": active,
                "lease_expires_in": round(self._lease_expires - time.monotonic(), 1) if active else None,
            }


def _default_lock_path():
    from .config import settings
    return settings.lock_path


vault_lock = VaultLock(_default_lock_path)
