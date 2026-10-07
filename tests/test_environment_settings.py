import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from settings import Settings, read_settings
from telegram_monitor import DeliveryProcessor


class EnvironmentSettingsTests(unittest.TestCase):
    def test_container_settings_work_without_an_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {"HISTORY_MONTHS": "12", "DISCOUNT_THRESHOLD_PERCENT": "35",
                      "TELEGRAM_POLL_SECONDS": "8", "TELEGRAM_DETECTION_MODE": "poll",
                      "TELEGRAM_ALLOW_ACCOUNT_UPDATES": "false"}
            with patch("settings.ROOT", root), patch.dict(os.environ, values, clear=True):
                self.assertEqual(read_settings(), Settings(12, 35, 8))
                self.assertFalse((root / ".env").exists())

    def test_missing_environment_settings_keep_the_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("settings.ROOT", Path(directory)), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(read_settings(), Settings())

    def test_environment_settings_are_validated(self):
        invalid = [("HISTORY_MONTHS", "invalid"), ("HISTORY_MONTHS", "0"),
                   ("DISCOUNT_THRESHOLD_PERCENT", "101"),
                   ("TELEGRAM_POLL_SECONDS", "0"), ("TELEGRAM_POLL_SECONDS", "nan"),
                   ("TELEGRAM_DETECTION_MODE", "push"),
                   ("TELEGRAM_ALLOW_ACCOUNT_UPDATES", "true")]
        with tempfile.TemporaryDirectory() as directory:
            for name, value in invalid:
                with self.subTest(name=name, value=value):
                    with patch("settings.ROOT", Path(directory)), patch.dict(os.environ, {name: value}, clear=True):
                        with self.assertRaises(ValueError):
                            read_settings()

    def test_local_file_overrides_environment_and_still_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".env"
            path.write_text("DISCOUNT_THRESHOLD_PERCENT=25\n", encoding="utf-8")
            with patch("settings.ROOT", root), patch.dict(os.environ, {"DISCOUNT_THRESHOLD_PERCENT": "40"}, clear=True):
                self.assertEqual(read_settings().threshold, 25)
                path.write_text("DISCOUNT_THRESHOLD_PERCENT=30\n", encoding="utf-8")
                self.assertEqual(read_settings().threshold, 30)

    def test_delivery_credentials_work_without_a_mounted_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {"TELEGRAM_BOT_TOKEN": "123:example-token", "TELEGRAM_CHAT_ID": "456"}
            with (
                patch("telegram_monitor.ROOT", root),
                patch.dict(os.environ, values, clear=True),
                patch("telegram_monitor.requests.Session") as session,
            ):
                processor = DeliveryProcessor()
                self.assertEqual(processor.recipient, "456")
                self.assertEqual(processor.bot.base, "https://api.telegram.org/bot123:example-token/")
                session.assert_called_once()
                session.return_value.post.assert_not_called()
                self.assertFalse((root / ".env").exists())


if __name__ == "__main__":
    unittest.main()
