import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telegram_monitor import capture_new_messages, open_inbox


GROUP_ID = -100123
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def message(number, minutes_ago, group_id=GROUP_ID):
    return SimpleNamespace(id=number, chat_id=group_id,
                           date=NOW - timedelta(minutes=minutes_ago),
                           message='https://fkrt.co/example', entities=[], reply_markup=None)


class HistoryClient:
    def __init__(self, messages):
        self.messages = messages
        self.lookups = []
        self.fetched_ids = []

    async def get_messages(self, entity, *, limit, offset_date=None):
        self.lookups.append((limit, offset_date))
        return [item for item in reversed(self.messages)
                if offset_date is None or item.date < offset_date][:limit]

    def iter_messages(self, entity, *, min_id, reverse, limit):
        assert reverse and limit is None

        async def messages():
            for item in self.messages:
                if item.id > min_id:
                    self.fetched_ids.append(item.id)
                    yield item
        return messages()


class RestartWindowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_path = Path(directory.name) / 'cursor.json'
        self.legacy_path = Path(directory.name) / 'old.sqlite3'
        for target, value in [('READ_STATE_PATH', self.state_path),
                              ('INBOX_PATH', self.legacy_path)]:
            patcher = patch('telegram_monitor.' + target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        clock = patch('telegram_monitor.datetime')
        clock.start().now.return_value = NOW
        self.addCleanup(clock.stop)
        self.history = [message(1, 1440), message(2, 120), message(3, 31),
                        message(4, 30), message(5, 20), message(6, 0)]

    def save_cursor(self, number):
        self.state_path.write_text(json.dumps(
            {'version': 1, 'chat_id': GROUP_ID, 'last_message_id': number}))

    async def restart(self, history=None, *, persist=True):
        client = HistoryClient(self.history if history is None else history)
        inbox = await open_inbox(client, object(), GROUP_ID, persist=persist)
        self.addCleanup(inbox.close)
        return client, inbox

    async def capture(self, client, inbox):
        return await capture_new_messages(client, object(), GROUP_ID, inbox, asyncio.Event())

    async def test_long_outage_skips_old_history_and_includes_30_minute_boundary(self):
        self.save_cursor(1)
        client, inbox = await self.restart()
        self.assertEqual(client.lookups, [(1, NOW - timedelta(minutes=30))])
        self.assertEqual(inbox.cursor(), 3)
        self.assertEqual(json.loads(self.state_path.read_text())['last_message_id'], 3)
        self.assertEqual(await self.capture(client, inbox), 3)
        self.assertEqual(client.fetched_ids, [4, 5, 6])
        while (job := inbox.claim()) is not None:
            inbox.finish(job['message_id'], [])
        reopened_client, reopened = await self.restart()
        self.assertEqual(await self.capture(reopened_client, reopened), 0)
        self.assertEqual(reopened_client.fetched_ids, [])

    async def test_recent_checkpoint_is_not_rewound(self):
        self.save_cursor(5)
        original = self.state_path.read_bytes()
        client, inbox = await self.restart()
        self.assertEqual(inbox.cursor(), 5)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(await self.capture(client, inbox), 1)
        self.assertEqual(client.fetched_ids, [6])

    async def test_no_recent_messages_advances_checkpoint_without_fetching_backlog(self):
        self.save_cursor(1)
        client, inbox = await self.restart(self.history[:3])
        self.assertEqual(inbox.cursor(), 3)
        self.assertEqual(await self.capture(client, inbox), 0)
        self.assertEqual(client.fetched_ids, [])
        self.assertEqual(json.loads(self.state_path.read_text())['last_message_id'], 3)

    async def test_group_with_only_recent_history_resumes_unclaimed_messages(self):
        self.save_cursor(4)
        client, inbox = await self.restart(self.history[3:])
        self.assertEqual(inbox.cursor(), 4)
        self.assertEqual(await self.capture(client, inbox), 2)
        self.assertEqual(client.fetched_ids, [5, 6])

    async def test_first_run_still_starts_at_latest_message(self):
        client, inbox = await self.restart()
        self.assertEqual(inbox.cursor(), 6)
        self.assertEqual(await self.capture(client, inbox), 0)
        self.assertEqual(client.fetched_ids, [])

    async def test_empty_group_can_capture_future_messages(self):
        client, inbox = await self.restart([])
        self.assertEqual(inbox.cursor(), 0)
        self.assertEqual(await self.capture(client, inbox), 0)
        client.messages.append(message(1, 0))
        self.assertEqual(await self.capture(client, inbox), 1)

    async def test_dry_run_skips_old_history_without_changing_checkpoint(self):
        self.save_cursor(1)
        original = self.state_path.read_bytes()
        client, inbox = await self.restart(persist=False)
        self.assertEqual(await self.capture(client, inbox), 3)
        self.assertEqual(client.fetched_ids, [4, 5, 6])
        while (job := inbox.claim()) is not None:
            inbox.finish(job['message_id'], [])
        self.assertEqual(self.state_path.read_bytes(), original)

    async def test_legacy_pending_messages_obey_the_same_time_limit(self):
        with closing(sqlite3.connect(self.legacy_path)) as db:
            db.executescript("""CREATE TABLE scope (group_id INTEGER, cursor INTEGER);
                               INSERT INTO scope VALUES (-100123, 6);
                               CREATE TABLE messages (message_id INTEGER, status TEXT);
                               INSERT INTO messages VALUES (2, 'pending'), (4, 'pending');""")
        original = self.legacy_path.read_bytes()
        client, inbox = await self.restart()
        self.assertEqual(inbox.cursor(), 3)
        self.assertEqual(await self.capture(client, inbox), 3)
        self.assertEqual(client.fetched_ids, [4, 5, 6])
        self.assertEqual(self.legacy_path.read_bytes(), original)

    async def test_boundary_message_from_another_group_is_rejected(self):
        self.save_cursor(1)
        original = self.state_path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'outside the approved group'):
            await self.restart([message(3, 31, group_id=-100999)])
        self.assertEqual(self.state_path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
