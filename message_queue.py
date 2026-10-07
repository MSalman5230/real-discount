"""Durable inbox keyed by approved group/message ID; capture and claim once."""

import json
import sqlite3
import threading
from pathlib import Path


class MessageQueue:
    def __init__(self, path, group_id, initial_cursor=0):
        self.group_id = group_id
        self.lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS scope (group_id INTEGER PRIMARY KEY, cursor INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                group_id INTEGER NOT NULL, message_id INTEGER NOT NULL, links TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT, PRIMARY KEY (group_id, message_id));
        ''')
        rows = self.db.execute("SELECT group_id FROM scope").fetchall()
        if rows and any(row[0] != group_id for row in rows):
            raise ValueError("Inbox database belongs to a different group.")
        self.db.execute("INSERT OR IGNORE INTO scope VALUES (?, ?)", (group_id, initial_cursor))
        # Never replay a message already claimed by a previous process. A crash
        # may interrupt delivery, but retrying would risk duplicate messages.
        self.db.execute("UPDATE messages SET status='interrupted', detail='Previous worker stopped during processing' WHERE status='processing'")
        self.db.commit()

    def cursor(self):
        with self.lock:
            return self.db.execute("SELECT cursor FROM scope WHERE group_id=?", (self.group_id,)).fetchone()[0]

    def capture(self, group_id, message_id, links):
        if group_id != self.group_id:
            raise ValueError("Refusing to queue a message outside the approved group.")
        with self.lock, self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, NULL)",
                                       (group_id, message_id, json.dumps(links), "pending" if links else "no_links")).rowcount
            self.db.execute("UPDATE scope SET cursor=MAX(cursor, ?) WHERE group_id=?", (message_id, group_id))
            return bool(inserted)

    def claim(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT * FROM messages WHERE status='pending' ORDER BY message_id LIMIT 1").fetchone()
                if row is not None:
                    self.db.execute("UPDATE messages SET status='processing' WHERE group_id=? AND message_id=?",
                                    (row["group_id"], row["message_id"]))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            return {**dict(row), "links": json.loads(row["links"])} if row is not None else None

    def remaining(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM messages WHERE status IN ('pending','processing')").fetchone()[0]

    def finish(self, message_id, outcomes):
        with self.lock, self.db:
            self.db.execute("UPDATE messages SET status='done', detail=? WHERE group_id=? AND message_id=?",
                            (json.dumps(outcomes), self.group_id, message_id))

    def close(self):
        self.db.close()
