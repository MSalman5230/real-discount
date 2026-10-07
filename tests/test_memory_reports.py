import builtins
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

from deal_report import analyse_history, main, render_graph, save_report
from settings import Settings
from telegram_monitor import TelegramBot


def example_report():
    result = {
        "product_id": "EXAMPLE",
        "product_name": "Example deal",
        "product_url": "https://www.amazon.in/dp/EXAMPLE",
        "history_url": "https://pricehistory.app/p/example",
        "pricehistory_assessment": {"label": "Yes"},
        "summary": {"latest_price": 700},
        "history": {"Price": [{"x": "2026-01-01", "y": 1000},
                              {"x": "2026-06-30", "y": 700}]},
    }
    analyse_history(result, as_of="2026-07-01")
    return result


class MemoryReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Matplotlib loads its one-time font cache separately from reports.
        # In Docker this cache lives in the /tmp RAM mount.
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot

    def test_real_graph_is_a_valid_png_without_any_report_file_write(self):
        original_open = builtins.open

        def read_only_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                self.fail(f"Graph unexpectedly wrote a file: {file}")
            return original_open(file, mode, *args, **kwargs)

        with (
            patch("builtins.open", side_effect=read_only_open),
            patch.object(Path, "mkdir", side_effect=AssertionError("Unexpected directory")),
            render_graph(example_report()) as graph,
        ):
            self.assertEqual(graph.tell(), 0)
            self.assertEqual(graph.read(8), b"\x89PNG\r\n\x1a\n")
            graph.seek(0)
            with Image.open(graph) as image:
                image.load()
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, (1950, 1260))
        self.assertTrue(graph.closed)
        import matplotlib.pyplot as plt
        self.assertEqual(plt.get_fignums(), [])

    def test_telegram_upload_encodes_bytes_from_memory_and_rewinds_the_buffer(self):
        bot = TelegramBot.__new__(TelegramBot)
        bot.base = "https://example.invalid/"
        bot.session = Mock()
        payloads = []

        def post(url, data, files, timeout):
            request = requests.Request("POST", url, data=data, files=files).prepare()
            payloads.append(request.body)
            response = Mock()
            response.json.return_value = {"ok": True, "result": {"message_id": 123}}
            return response

        bot.session.post.side_effect = post
        with BytesIO(b"png payload in memory") as graph:
            graph.seek(0, 2)
            self.assertEqual(bot.send_report("private", graph, "caption"), {"message_id": 123})
            self.assertFalse(graph.closed)  # The caller owns and releases it.
        self.assertTrue(graph.closed)
        self.assertIn(b"png payload in memory", payloads[0])
        self.assertIn(b'filename="deal-6months.png"', payloads[0])
        self.assertIn(b"Content-Type: image/png", payloads[0])

    def test_cli_does_not_export_or_render_when_output_is_not_requested(self):
        with (
            patch.object(sys, "argv", ["deal_report.py", "https://fkrt.co/example"]),
            patch("settings.read_settings", return_value=Settings()),
            patch("deal_report.prepare_report", return_value=example_report()),
            patch("deal_report.save_report") as export,
            patch("deal_report.render_graph") as render,
            patch("builtins.print"),
        ):
            main()
        export.assert_not_called()
        render.assert_not_called()

    def test_files_are_still_available_as_an_explicit_export(self):
        with tempfile.TemporaryDirectory() as directory:
            _, graph, caption = save_report(example_report(), Path(directory))
            self.assertTrue(graph.is_file())
            self.assertEqual({item.suffix for item in Path(directory).iterdir()},
                             {".png", ".json", ".txt"})
            self.assertIn("Example deal", caption)


if __name__ == "__main__":
    unittest.main()
