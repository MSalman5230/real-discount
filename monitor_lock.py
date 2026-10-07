"""Prevent two monitor instances from processing the same inbox."""

import os
from pathlib import Path


class MonitorLock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.file = self.path.open("a+b")
        except PermissionError:
            identity = (f"UID/GID {os.getuid()}:{os.getgid()}" if hasattr(os, "getuid")
                        else "the current user")
            raise ValueError(f"Cannot write monitor lock '{self.path}'. Ensure the data "
                             f"directory and existing lock file are writable by {identity}; "
                             "Docker's /data mount must be read/write.") from None
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError("The Telegram monitor is already running.") from None
        return self

    def __exit__(self, *args):
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
