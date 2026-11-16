"""Lifetime reservations for cached Matrix crypto accounts and ratchets.

Keep lock inodes in place: unlinking a held inode would permit a second owner.
Use the gateway's native file-lock primitives, not its PID-reentrant token claim.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading

_mutex = threading.RLock()
_owners: dict[Path, tuple[int, object]] = {}


class CryptoOwnership:
    def __init__(self):
        self._handles = {}

    def acquire(self, path: Path) -> None:
        from gateway.runtime_ownership import _open_checked, _still_names
        from gateway.status import _try_acquire_file_lock
        path = path.resolve()
        with _mutex:
            if path in self._handles:
                return
            existing = _owners.get(path)
            if existing is not None and existing[0] == os.getpid():
                raise RuntimeError("Matrix crypto already has a live owner")
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = _open_checked(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0))
            handle = os.fdopen(fd, "r+", encoding="utf-8")
            try:
                if not _try_acquire_file_lock(handle) or not _still_names(path, fd):
                    raise RuntimeError("Matrix crypto already has a live owner")
            except BaseException:
                handle.close()
                raise
            self._handles[path] = handle
            _owners[path] = (os.getpid(), self)

    def acquire_device(self, user_id: str, device_id: str) -> None:
        from gateway.status import _get_scope_lock_path
        # Only a hash appears on disk; tokens and account/device identifiers are not recorded.
        identity = f"{user_id}\n{device_id}"
        path = _get_scope_lock_path("matrix-crypto-device", identity).with_suffix(".owner")
        self.acquire(path)

    def release(self) -> None:
        from gateway.status import _release_file_lock
        with _mutex:
            for path, handle in list(self._handles.items()):
                if _owners.get(path) == (os.getpid(), self):
                    _release_file_lock(handle)
                    handle.close()
                    del _owners[path]
                    del self._handles[path]
