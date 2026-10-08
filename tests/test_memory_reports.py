import builtins
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests
from PIL import Image

from deal_report import analyse_history, main, render_graph, save_report
from settings import Settings
from telegram_monitor import TelegramBot, download_product_image


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


class ProductPhotoTests(unittest.TestCase):
    def source(self, payload=None):
        if payload is None:
            with BytesIO() as image_bytes, Image.new("RGBA", (30, 20), (255, 0, 0, 0)) as image:
                image.save(image_bytes, format="PNG")
                payload = image_bytes.getvalue()
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Length": str(len(payload))}
        response.iter_content.return_value = [payload]
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response
        return session, response

    def test_photo_download_and_jpeg_conversion_do_not_write_to_disk(self):
        session, response = self.source()
        with (patch("telegram_monitor.make_session", return_value=session),
              patch("builtins.open", side_effect=AssertionError("Unexpected file access")),
              patch.object(Path, "mkdir", side_effect=AssertionError("Unexpected directory")),
              download_product_image("https://images.price.tools/product.png") as photo):
            self.assertEqual(photo.tell(), 0)
            with Image.open(photo) as image:
                image.load()
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (30, 20))
                self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        self.assertTrue(photo.closed)
        session.get.assert_called_once_with("https://images.price.tools/product.png",
                                            stream=True, timeout=(5, 15))
        response.__exit__.assert_called_once()
        session.__exit__.assert_called_once()

    def test_invalid_or_oversized_photos_are_omitted(self):
        for case in ["invalid", "header_limit", "stream_limit", "dimensions"]:
            with self.subTest(case=case):
                session, response = self.source(b"not an image")
                if case == "header_limit":
                    response.headers["Content-Length"] = "10000001"
                elif case == "stream_limit":
                    response.headers = {}
                    response.iter_content.return_value = [b"x" * 65536] * 153
                elif case == "dimensions":
                    with BytesIO() as payload, Image.new("RGB", (401, 20)) as image:
                        image.save(payload, format="PNG")
                        session, response = self.source(payload.getvalue())
                with patch("telegram_monitor.make_session", return_value=session), patch("builtins.print"):
                    self.assertIsNone(download_product_image("https://images.price.tools/product.jpg"))
                response.__exit__.assert_called_once()
                session.__exit__.assert_called_once()

    def test_failed_download_omits_photo_and_missing_url_does_not_request(self):
        session, response = self.source()
        response.raise_for_status.side_effect = requests.HTTPError("not found")
        with patch("telegram_monitor.make_session", return_value=session), patch("builtins.print"):
            self.assertIsNone(download_product_image("https://images.price.tools/missing.jpg"))
        with patch("telegram_monitor.make_session") as transport:
            self.assertIsNone(download_product_image(None))
        transport.assert_not_called()

    def test_album_upload_uses_memory_attachments_and_releases_the_product_photo(self):
        bot = TelegramBot.__new__(TelegramBot)
        bot.base = "https://example.invalid/"
        bot.session = Mock()
        uploaded = []

        def post(url, data, files, timeout):
            self.assertTrue(url.endswith("sendMediaGroup"))
            import json
            media = json.loads(data["media"])
            self.assertEqual(media, [{"type": "photo", "media": "attach://product", "caption": "deal caption"},
                                     {"type": "photo", "media": "attach://graph"}])
            self.assertEqual(data["chat_id"], "private")
            uploaded.append(requests.Request("POST", url, data=data, files=files).prepare().body)
            response = Mock()
            response.json.return_value = {"ok": True, "result": [{"message_id": 1}, {"message_id": 2}]}
            return response

        bot.session.post.side_effect = post
        photo = BytesIO(b"product jpeg in memory")
        with (BytesIO(b"chart png in memory") as graph,
              patch("telegram_monitor.download_product_image", return_value=photo),
              patch.object(Path, "open", side_effect=AssertionError("Unexpected file access"))):
            graph.seek(0, 2)
            result = bot.send_report("private", graph, "deal caption", "https://images.price.tools/product.jpg")
            self.assertEqual(result, [{"message_id": 1}, {"message_id": 2}])
            self.assertFalse(graph.closed)
            self.assertTrue(photo.closed)
        self.assertTrue(graph.closed)
        self.assertIn(b"product jpeg in memory", uploaded[0])
        self.assertIn(b"chart png in memory", uploaded[0])
        self.assertIn(b'filename="product.jpg"', uploaded[0])
        self.assertIn(b'filename="deal-6months.png"', uploaded[0])

    def test_album_failure_still_releases_the_product_photo(self):
        bot = TelegramBot.__new__(TelegramBot)
        photo = BytesIO(b"product")
        with (BytesIO(b"chart") as graph,
              patch("telegram_monitor.download_product_image", return_value=photo),
              patch.object(bot, "call", side_effect=ValueError("upload failed"))):
            with self.assertRaisesRegex(ValueError, "upload failed"):
                bot.send_report("private", graph, "caption", "https://images.price.tools/product.jpg")
            self.assertFalse(graph.closed)
            self.assertTrue(photo.closed)
        self.assertTrue(graph.closed)

    def test_photo_download_failure_still_sends_the_chart_with_caption(self):
        bot = TelegramBot.__new__(TelegramBot)
        with (BytesIO(b"chart") as graph,
              patch("telegram_monitor.download_product_image", return_value=None),
              patch.object(bot, "call", return_value={"message_id": 1}) as send):
            bot.send_report("private", graph, "caption", "https://images.price.tools/missing.jpg")
            send.assert_called_once_with("sendPhoto", {"chat_id": "private", "caption": "caption"},
                                         {"photo": ("deal-6months.png", graph, "image/png")})

    def test_long_album_caption_is_also_sent_as_text(self):
        bot = TelegramBot.__new__(TelegramBot)
        photo = BytesIO(b"product")
        caption = "x" * 1100
        with (BytesIO(b"chart") as graph,
              patch("telegram_monitor.download_product_image", return_value=photo),
              patch.object(bot, "call", return_value=[]) as send,
              patch("telegram_monitor.time.sleep")):
            bot.send_report("private", graph, caption, "https://images.price.tools/product.jpg")
        self.assertEqual([call.args[0] for call in send.call_args_list], ["sendMediaGroup", "sendMessage"])
        self.assertEqual(send.call_args_list[1].args[1]["text"], caption)
        self.assertTrue(photo.closed)


if __name__ == "__main__":
    unittest.main()
