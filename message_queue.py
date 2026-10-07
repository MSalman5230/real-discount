"""Keep pending messages in RAM and persist only a small restart cursor."""

import json
import os
import sqlite3
import threading
from collections import deque
from contextlib import closing
from pathlib import Path


class MessageQueue:
    def __init__(self, path, group_id, initial_cursor=0, *, legacy_path=None,
                 persist=True, max_pending=256):
        if max_pending < 1:
            raise ValueError("The inbox capacity must be positive.")
        self.group_id = group_id
        self.path = Path(path)
        self.persist = persist
        self.max_pending = max_pending
        self.lock = threading.RLock()
        self.pending = deque()
        self.active = None
        state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if state and state["chat_id"] != group_id:
            raise ValueError("Stored reading cursor belongs to a different group.")
        cursor = state.get("last_message_id", initial_cursor)
        # Older versions kept a SQLite inbox. Recover its pending messages from
        # Telegram, without loading payloads or writing to the old database.
        if legacy_path and Path(legacy_path).exists() and state.get("version") != 1:
            cursor = self._legacy_cursor(Path(legacy_path))
        if not isinstance(cursor, int) or cursor < 0:
            raise ValueError("Stored reading cursor must be a non-negative message ID.")
        self.committed_cursor = cursor
        self.scan_cursor = cursor
        if not state or state.get("version") != 1:
            self._save_cursor(cursor, force=True)

    def _legacy_cursor(self, path):
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            scopes = db.execute("SELECT group_id, cursor FROM scope").fetchall()
            if len(scopes) != 1 or scopes[0][0] != self.group_id:
                raise ValueError("Inbox database belongs to a different group.")
            cursor = scopes[0][1]
            pending = db.execute("SELECT MIN(message_id) FROM messages WHERE status='pending'").fetchone()[0]
            return min(cursor, pending - 1) if pending is not None else cursor

    def _save_cursor(self, cursor, *, force=False):
        if cursor == self.committed_cursor and not force:
            return
        if self.persist:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            state = {"version": 1, "chat_id": self.group_id, "last_message_id": cursor}
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(state, separators=(",", ":")))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        self.committed_cursor = cursor

    def cursor(self):
        with self.lock:
            return self.scan_cursor

    def full(self):
        return self.remaining() >= self.max_pending

    def capture(self, group_id, message_id, links):
        if group_id != self.group_id:
            raise ValueError("Refusing to queue a message outside the approved group.")
        with self.lock:
            if message_id <= self.scan_cursor:
                return False
            if self.full():
                raise ValueError("The in-memory inbox is full.")
            self.pending.append({"group_id": group_id, "message_id": message_id,
                                 "links": list(links)})
            self.scan_cursor = message_id
            return True

    def claim(self):
        with self.lock:
            if self.active is not None or not self.pending:
                return None
            job = self.pending[0]
            # Claim before sending, preserving the existing policy: a crash can
            # interrupt an alert, but cannot replay an already claimed message.
            self._save_cursor(job["message_id"])
            self.active = self.pending.popleft()
            return self.active

    def remaining(self):
        with self.lock:
            return len(self.pending) + (self.active is not None)

    def finish(self, message_id, outcomes):
        with self.lock:
            if self.active is None or self.active["message_id"] != message_id:
                raise ValueError("Only the active message can be finished.")
            # No processed links, product histories or delivery details retained.
            self.active = None

    def close(self):
        with self.lock:
            self.pending.clear()
            self.active = None
