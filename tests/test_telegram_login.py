import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.tl.types import ChatInviteAlready

from monitor_lock import MonitorLock
from telegram_monitor import AUTHORIZED_GROUP_NAME, AUTHORIZED_SOURCE, LoginRequired, main, read_group


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        values = {"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "example-hash",
                  "TELEGRAM_PHONE": "+910000000000", "TELEGRAM_SOURCE_CHATS": AUTHORIZED_SOURCE,
                  "TELEGRAM_USER_SESSION": "telegram-user"}
        for patcher in (patch.dict(os.environ, values, clear=True),
                        patch("telegram_monitor.ROOT", self.data),
                        patch("telegram_monitor.DATA_DIR", self.data),
                        patch("telegram_monitor.GROUP_BINDING_PATH", self.data / "binding.json")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = AsyncMock()
        self.client.session = SimpleNamespace(save_entities=True)
        self.client.is_user_authorized.return_value = False
        self.client.return_value = ChatInviteAlready(chat=SimpleNamespace(title=AUTHORIZED_GROUP_NAME))
        factory = patch("telethon.TelegramClient", return_value=self.client)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        peer = patch("telethon.utils.get_peer_id", return_value=-100123)
        peer.start()
        self.addCleanup(peer.stop)

    async def test_detached_startup_never_requests_a_code_and_closes_the_session(self):
        with patch("telegram_monitor.sys.stdin.isatty", return_value=False), \
             patch("telegram_monitor.getpass.getpass") as prompt:
            with self.assertRaisesRegex(LoginRequired, "interactive terminal.*login"):
                await read_group(SimpleNamespace(command="watch"))
        prompt.assert_not_called()
        self.client.connect.assert_awaited_once()
        self.client.start.assert_not_awaited()
        self.client.assert_not_awaited()  # No group requests before authentication.
        self.client.disconnect.assert_awaited_once()
        self.assertFalse((self.data / "binding.json").exists())

    async def test_interactive_login_prompts_without_processing_any_messages(self):
        async def authenticate(*, phone, code_callback, password):
            self.assertEqual(phone, "+910000000000")
            self.assertEqual(code_callback(), "12345")
            self.assertEqual(password(), "example-2fa-secret")

        self.client.start.side_effect = authenticate
        output = io.StringIO()
        with patch("telegram_monitor.sys.stdin.isatty", return_value=True), \
             patch("telegram_monitor.getpass.getpass", side_effect=["12345", "example-2fa-secret"]) as prompt, \
             patch("telegram_monitor.DeliveryProcessor") as processor, \
             patch("telegram_monitor.open_inbox", new_callable=AsyncMock) as inbox, \
             redirect_stdout(output):
            await read_group(SimpleNamespace(command="login"))
        self.assertEqual(prompt.call_count, 2)
        processor.assert_not_called()
        inbox.assert_not_awaited()
        self.client.disconnect.assert_awaited_once()
        self.factory.assert_called_once_with(str(self.data / "telegram-user"), 123, "example-hash",
                                             receive_updates=False, catch_up=False)
        self.assertFalse(self.client.session.save_entities)
        self.assertEqual(json.loads((self.data / "binding.json").read_text())["chat_id"], -100123)
        self.assertIn("Login successful. Session saved.", output.getvalue())
        self.assertNotIn("12345", output.getvalue())
        self.assertNotIn("example-2fa-secret", output.getvalue())

    async def test_saved_session_can_monitor_without_interactive_stdin(self):
        self.client.is_user_authorized.return_value = True
        inbox = MagicMock()
        inbox.remaining.return_value = 0
        inbox.claim.return_value = None
        with patch("telegram_monitor.sys.stdin.isatty", return_value=False), \
             patch("telegram_monitor.getpass.getpass") as prompt, \
             patch("telegram_monitor.DeliveryProcessor"), \
             patch("telegram_monitor.open_inbox", new_callable=AsyncMock, return_value=inbox), \
             patch("telegram_monitor.capture_new_messages", new_callable=AsyncMock, return_value=0) as capture, \
             redirect_stdout(io.StringIO()):
            await read_group(SimpleNamespace(command="watch", dry_run=False, once=True))
        prompt.assert_not_called()
        capture.assert_awaited_once()
        self.client.disconnect.assert_awaited_once()
        inbox.close.assert_called_once()

    async def test_login_failure_disconnects_and_does_not_claim_success(self):
        self.client.start.side_effect = ValueError("Invalid login code")
        output = io.StringIO()
        with patch("telegram_monitor.sys.stdin.isatty", return_value=True), redirect_stdout(output):
            with self.assertRaisesRegex(ValueError, "Invalid login code"):
                await read_group(SimpleNamespace(command="login"))
        self.client.disconnect.assert_awaited_once()
        self.assertNotIn("Login successful", output.getvalue())
        self.assertFalse((self.data / "binding.json").exists())

    async def test_login_command_requires_a_terminal_only_for_an_unauthorized_session(self):
        with patch("telegram_monitor.sys.stdin.isatty", return_value=False):
            with self.assertRaises(LoginRequired):
                await read_group(SimpleNamespace(command="login"))
            self.client.is_user_authorized.return_value = True
            with redirect_stdout(io.StringIO()):
                await read_group(SimpleNamespace(command="login"))
        self.assertEqual(self.client.disconnect.await_count, 2)
        self.client.start.assert_awaited_once()


class LoginCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        patcher = patch("telegram_monitor.DATA_DIR", self.data)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_waiting_container_releases_the_lock_and_prints_dockhand_instructions(self):
        def interrupt_wait(seconds):
            # A login process must be able to acquire the monitor lock while
            # detached startup stays alive waiting for the user's restart.
            with MonitorLock(self.data / ".telegram-monitor.lock"):
                pass
            raise KeyboardInterrupt

        output = io.StringIO()
        with patch("telegram_monitor.sys.argv", ["telegram_monitor.py", "watch"]), \
             patch("telegram_monitor.ROOT", Path("/app")), \
             patch("telegram_monitor.read_group", new_callable=AsyncMock, side_effect=LoginRequired()), \
             patch("telegram_monitor.time.sleep", side_effect=interrupt_wait) as sleep, \
             redirect_stdout(output):
            main()
        sleep.assert_called_once()
        self.assertIn("Monitoring is paused", output.getvalue())
        self.assertIn("python /app/docker_entrypoint.py login", output.getvalue())
        self.assertIn("restart the container", output.getvalue())
        self.assertIn("Stopped.", output.getvalue())

    def test_once_run_reports_missing_login_instead_of_waiting_forever(self):
        with patch("telegram_monitor.sys.argv", ["telegram_monitor.py", "watch", "--once"]), \
             patch("telegram_monitor.read_group", new_callable=AsyncMock, side_effect=LoginRequired("Run login")), \
             patch("telegram_monitor.wait_for_login") as wait, patch("telegram_monitor.sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as failure:
                main()
        self.assertEqual(failure.exception.code, 1)
        wait.assert_not_called()

    def test_login_dispatches_without_watch_arguments(self):
        with patch("telegram_monitor.sys.argv", ["telegram_monitor.py", "login"]), \
             patch("telegram_monitor.read_group", new_callable=AsyncMock) as read, \
             patch("telegram_monitor.wait_for_login") as wait:
            main()
        self.assertEqual(read.await_args.args[0].command, "login")
        wait.assert_not_called()

    def test_connection_failure_does_not_enter_the_login_wait(self):
        with patch("telegram_monitor.sys.argv", ["telegram_monitor.py", "watch"]), \
             patch("telegram_monitor.read_group", new_callable=AsyncMock, side_effect=ConnectionError("offline")), \
             patch("telegram_monitor.wait_for_login") as wait, patch("telegram_monitor.sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as failure:
                main()
        self.assertEqual(failure.exception.code, 1)
        wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
