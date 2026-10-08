import asyncio
import json
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from deal_report import exceeds_threshold
from message_queue import MessageQueue
from monitor_lock import MonitorLock
from pipelines import fetch_history
from pipelines.amazon import normalize as normalize_amazon
from pipelines.common import build_result, extract_product_image_url, json_object
from pipelines.errors import PriceHistoryError
from pipelines.flipkart import canonical_flipkart_url, normalize
from settings import Settings, read_settings
from telegram_monitor import DeliveryProcessor, capture_new_messages, process_inbox


class PipelineTests(unittest.TestCase):
    def test_flipkart_mobile_link_preserves_variant(self):
        url = 'https://dl.flipkart.com/dl/red-tape/p/itm1639fd4ee8851?pid=SHOHHFB8HFGJEX9P&affid=x'
        self.assertEqual(canonical_flipkart_url(url),
                         'https://www.flipkart.com/red-tape/p/itm1639fd4ee8851?pid=SHOHHFB8HFGJEX9P')

    def test_missing_flipkart_assessment_is_optional(self):
        soup = BeautifulSoup('<h1>Boots</h1>', 'html.parser')
        data = {'Price': {'Price': 860}, 'History': {'Price': [{'x': '2026-09-20', 'y': 1064}]}}
        result = normalize('https://fkrt.co/test',
                           'https://www.flipkart.com/boots/p/itmabc?pid=SHOHHFB8HFGJEX9P',
                           soup, data, 'https://pricehistory.app/p/boots', {})
        self.assertEqual(result['store'], 'flipkart')
        self.assertIsNone(result['pricehistory_assessment']['label'])

    def test_no_history_is_a_typed_failure(self):
        with self.assertRaises(PriceHistoryError) as error:
            build_result('a', 'b', 'PID', 'flipkart', BeautifulSoup('', 'html.parser'),
                         {'Price': {'Price': 100}, 'History': {'Price': []}}, 'c', {}, 100)
        self.assertEqual(error.exception.code, 'no_history')

    def test_invalid_and_unsupported_links_fail_without_network_calls(self):
        for url in ['not a link', 'https://example.com/x', 'https://user:password@www.amazon.in/dp/B094QSY1NC']:
            with self.assertRaises(PriceHistoryError):
                fetch_history(url)

    def test_invalid_api_json_is_handled(self):
        response = Mock()
        response.json.side_effect = ValueError('HTML instead of JSON')
        with self.assertRaises(PriceHistoryError) as error:
            json_object(response)
        self.assertEqual(error.exception.code, 'invalid_response')


class ThresholdTests(unittest.TestCase):
    def test_strict_boundary(self):
        self.assertFalse(exceeds_threshold(80, 100, 20))
        self.assertTrue(exceeds_threshold(79.99, 100, 20))
        self.assertFalse(exceeds_threshold(80.01, 100, 20))

    def test_env_changes_are_reloaded_and_defaults_are_20(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('', encoding='utf-8')
            self.assertEqual(read_settings(path).threshold, 20)
            path.write_text('DISCOUNT_THRESHOLD_PERCENT=25\n', encoding='utf-8')
            self.assertEqual(read_settings(path).threshold, 25)
            path.write_text('DISCOUNT_THRESHOLD_PERCENT=30\n', encoding='utf-8')
            self.assertEqual(read_settings(path).threshold, 30)

    def test_below_threshold_never_renders_or_sends(self):
        processor = DeliveryProcessor.__new__(DeliveryProcessor)
        processor.bot = Mock()
        processor.recipient = 'private'
        result = {'product_id': 'PRODUCT', 'six_month_analysis': {'median_rule_verdict': 'WAIT', 'drop_percent': 20}}
        with patch('telegram_monitor.read_settings', return_value=Settings()), \
             patch('telegram_monitor.prepare_report', return_value=result), \
             patch('telegram_monitor.render_graph') as render:
            outcome = processor.process(-100123, 1, ['https://fkrt.co/test'])
        self.assertEqual(outcome[0]['status'], 'below_threshold')
        render.assert_not_called()
        processor.bot.send_report.assert_not_called()

    def test_invalid_link_does_not_stop_other_links_in_post(self):
        processor = DeliveryProcessor.__new__(DeliveryProcessor)
        processor.bot = None
        good = {'product_id': 'PRODUCT', 'six_month_analysis': {'median_rule_verdict': 'WAIT', 'drop_percent': 10}}
        with patch('telegram_monitor.read_settings', return_value=Settings()), \
             patch('telegram_monitor.prepare_report', side_effect=[PriceHistoryError('No data', 'no_history'), good]):
            results = processor.process(-100123, 1, ['https://fkrt.co/bad', 'https://fkrt.co/good'])
        self.assertEqual([r['status'] for r in results], ['failed', 'below_threshold'])

    def test_qualifying_message_sends_and_closes_a_memory_graph_without_writing(self):
        processor = DeliveryProcessor.__new__(DeliveryProcessor)
        processor.bot = Mock()
        processor.recipient = 'private'
        result = {'product_id': 'PRODUCT', 'product_image_url': 'https://images.price.tools/product.jpg',
                  'six_month_analysis': {'median_rule_verdict': 'BUY', 'drop_percent': 26}}
        url = 'https://fkrt.co/good'
        graph = BytesIO(b'png bytes')
        uploaded = []
        processor.bot.send_report.side_effect = lambda recipient, photo, caption, **kwargs: uploaded.append(photo.read())
        with patch('telegram_monitor.read_settings', return_value=Settings(threshold=25)), \
             patch('telegram_monitor.prepare_report', return_value=result) as prepare, \
             patch('telegram_monitor.report_caption', return_value='caption'), \
             patch('telegram_monitor.render_graph', return_value=graph), \
             patch.object(Path, 'write_text', side_effect=AssertionError('Unexpected disk write')), \
             patch.object(Path, 'mkdir', side_effect=AssertionError('Unexpected directory')):
            outcome = processor.process(-100123, 1, [url, url])
        prepare.assert_called_once_with(url, 6, 25)
        processor.bot.send_report.assert_called_once_with(
            'private', graph, 'caption', product_image_url=result['product_image_url'])
        self.assertEqual(uploaded, [b'png bytes'])
        self.assertTrue(graph.closed)
        self.assertEqual(outcome[0]['status'], 'sent')

    def test_upload_failure_releases_the_graph_and_continues_to_the_next_link(self):
        processor = DeliveryProcessor.__new__(DeliveryProcessor)
        processor.bot = Mock()
        processor.bot.send_report.side_effect = [ValueError('upload failed'), None]
        processor.recipient = 'private'
        result = {'product_id': 'PRODUCT', 'six_month_analysis': {'median_rule_verdict': 'BUY', 'drop_percent': 26}}
        graphs = [BytesIO(b'first'), BytesIO(b'second')]
        with patch('telegram_monitor.read_settings', return_value=Settings()), \
             patch('telegram_monitor.prepare_report', return_value=result), \
             patch('telegram_monitor.report_caption', return_value='caption'), \
             patch('telegram_monitor.render_graph', side_effect=graphs):
            outcomes = processor.process(-100123, 1, ['https://fkrt.co/first', 'https://fkrt.co/second'])
        self.assertEqual([item['status'] for item in outcomes], ['failed', 'sent'])
        self.assertTrue(all(graph.closed for graph in graphs))


    def test_product_identifiers_and_error_payloads_do_not_reach_console_logs(self):
        processor = DeliveryProcessor.__new__(DeliveryProcessor)
        processor.bot = None
        processor.recipient = 'private'
        product_id = 'PRIVATE-PRODUCT-CODE'
        url = 'https://www.amazon.in/dp/PRIVATE-LINK'
        result = {'product_id': product_id,
                  'six_month_analysis': {'median_rule_verdict': 'BUY', 'drop_percent': 26}}
        output = StringIO()
        with (patch('telegram_monitor.read_settings', return_value=Settings()),
              patch('telegram_monitor.prepare_report', side_effect=[result, PriceHistoryError(url)]),
              patch('telegram_monitor.report_caption', return_value='private message caption'),
              patch('telegram_monitor.render_graph', return_value=BytesIO(b'chart')),
              redirect_stdout(output), redirect_stderr(output)):
            processor.process(-100123, 1, [url, 'https://fkrt.co/second'])
        logs = output.getvalue()
        for private_value in [product_id, url, 'private message caption']:
            self.assertNotIn(private_value, logs)
        self.assertIn('Prepared report.', logs)
        self.assertIn('PriceHistoryError', logs)


class ProductImageExtractionTests(unittest.TestCase):
    def test_both_store_pipelines_use_the_large_product_metadata_image(self):
        soup = BeautifulSoup('<h1>Product</h1><meta property="og:image" '
                             'content="https://images.price.tools/large.jpg">'
                             '<img class="card-img-top" src="https://images.price.tools/medium.jpg">',
                             'html.parser')
        data = {'Price': {'Price': 700}, 'History': {'Price': [{'x': '2026-01-01', 'y': 1000}]}}
        for adapter, url in [(normalize_amazon, 'https://www.amazon.in/dp/B094QSY1NC'),
                             (normalize, 'https://www.flipkart.com/example/p/itmabc?pid=SFFFBPPPH8NDREHH')]:
            with self.subTest(url=url):
                result = adapter(url, url, soup, data, 'https://pricehistory.app/p/example', {})
                self.assertEqual(result['product_image_url'], 'https://images.price.tools/large.jpg')

    def test_invalid_metadata_falls_back_to_the_main_lazy_product_image(self):
        soup = BeautifulSoup('<meta property="og:image" content="https://[">'
                             '<meta property="og:image" content="javascript:alert(1)">'
                             '<img class="card-img-top" data-src="//images.price.tools/product.jpg" '
                             'src="/assets/placeholder.png">', 'html.parser')
        self.assertEqual(extract_product_image_url(soup, 'https://pricehistory.app/p/example'),
                         'https://images.price.tools/product.jpg')

    def test_missing_product_photo_does_not_use_a_logo_or_recommended_product(self):
        for html in ['', '<img class="card-img-top" src="/assets/images/pricehistory-app-logo.png">',
                     '<img class="lazy" data-src="https://images.price.tools/other-product.jpg">']:
            with self.subTest(html=html):
                self.assertIsNone(extract_product_image_url(BeautifulSoup(html, 'html.parser'),
                                                           'https://pricehistory.app/p/example'))


class InboxTests(unittest.TestCase):
    def test_every_message_in_a_large_poll_is_captured_once(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = MessageQueue(Path(directory) / 'cursor.json', -100123)

            class Client:
                def iter_messages(self, entity, **kwargs):
                    self.arguments = kwargs
                    async def messages():
                        for number in range(1, 151):
                            yield SimpleNamespace(chat_id=-100123, id=number,
                                                  message='https://fkrt.co/example', entities=[], reply_markup=None)
                    return messages()

            client = Client()
            wake = asyncio.Event()
            self.assertEqual(asyncio.run(capture_new_messages(client, object(), -100123, inbox, wake)), 150)
            self.assertIsNone(client.arguments['limit'])
            self.assertEqual(inbox.cursor(), 150)
            self.assertEqual(asyncio.run(capture_new_messages(client, object(), -100123, inbox, wake)), 0)
            ids = []
            while (job := inbox.claim()) is not None:
                ids.append(job['message_id'])
                inbox.finish(job['message_id'], [{'status': 'below_threshold'}])
            self.assertEqual(ids, list(range(1, 151)))
            inbox.close()
            reopened = MessageQueue(Path(directory) / 'cursor.json', -100123)
            self.assertFalse(reopened.capture(-100123, 1, ['https://fkrt.co/example']))
            self.assertIsNone(reopened.claim())
            reopened.close()

    def test_claimed_message_is_not_replayed_after_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cursor.json'
            inbox = MessageQueue(path, -100123)
            inbox.capture(-100123, 1, ['https://fkrt.co/example'])
            self.assertIsNotNone(inbox.claim())
            inbox.close()
            reopened = MessageQueue(path, -100123)
            self.assertIsNone(reopened.claim())
            reopened.close()

    def test_pending_messages_can_be_refetched_after_restart_and_wrong_group_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cursor.json'
            inbox = MessageQueue(path, -100123)
            with self.assertRaises(ValueError):
                inbox.capture(-100999, 1, ['https://fkrt.co/example'])
            inbox.capture(-100123, 1, ['https://fkrt.co/example'])
            inbox.close()
            reopened = MessageQueue(path, -100123)
            self.assertEqual(reopened.cursor(), 0)
            self.assertTrue(reopened.capture(-100123, 1, ['https://fkrt.co/example']))
            self.assertEqual(reopened.claim()['message_id'], 1)
            reopened.close()

    def test_worker_processes_every_message_even_after_an_unexpected_error(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = MessageQueue(Path(directory) / 'cursor.json', -100123)
            for number in range(1, 4):
                inbox.capture(-100123, number, ['https://fkrt.co/example'])
            processor = Mock()
            processor.process.side_effect = [RuntimeError('bad response'), [], []]

            async def drain():
                worker = asyncio.create_task(process_inbox(inbox, processor, asyncio.Event()))
                async def until_empty():
                    while inbox.remaining():
                        await asyncio.sleep(.01)
                try:
                    await asyncio.wait_for(until_empty(), timeout=5)
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

            asyncio.run(drain())
            self.assertEqual([call.args[1] for call in processor.process.call_args_list], [1, 2, 3])
            self.assertIsNone(inbox.claim())
            inbox.close()

    def test_finished_messages_are_discarded_and_checkpoint_size_stays_small(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cursor.json'
            inbox = MessageQueue(path, -100123)
            for number in range(1, 1001):
                inbox.capture(-100123, number, ['https://fkrt.co/private-product'])
                inbox.claim()
                inbox.finish(number, [{'status': 'sent', 'detail': 'private report'}])
            self.assertEqual(inbox.remaining(), 0)
            self.assertEqual(len(inbox.pending), 0)
            self.assertIsNone(inbox.active)
            self.assertLess(path.stat().st_size, 100)
            self.assertEqual(json.loads(path.read_text()),
                             {'version': 1, 'chat_id': -100123, 'last_message_id': 1000})
            self.assertEqual([item.name for item in Path(directory).iterdir()], ['cursor.json'])

    def test_dry_run_does_not_write_or_advance_the_real_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cursor.json'
            normal = MessageQueue(path, -100123, 10)
            normal.close()
            original = path.read_bytes()
            dry = MessageQueue(path, -100123, persist=False)
            dry.capture(-100123, 11, ['https://fkrt.co/test'])
            dry.claim()
            dry.finish(11, [])
            dry.close()
            self.assertEqual(path.read_bytes(), original)
            fresh = Path(directory) / 'dry.json'
            MessageQueue(fresh, -100123, persist=False).close()
            self.assertFalse(fresh.exists())

    def test_failed_checkpoint_does_not_remove_the_pending_message(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = MessageQueue(Path(directory) / 'cursor.json', -100123)
            inbox.capture(-100123, 1, ['https://fkrt.co/test'])
            with patch.object(inbox, '_save_cursor', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    inbox.claim()
            self.assertEqual(inbox.remaining(), 1)
            self.assertIsNone(inbox.active)
            self.assertEqual(inbox.claim()['message_id'], 1)

    def test_legacy_inbox_migration_recovers_only_unclaimed_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / 'old.sqlite3'
            db = sqlite3.connect(legacy)
            db.executescript("""CREATE TABLE scope (group_id INTEGER, cursor INTEGER);
                               INSERT INTO scope VALUES (-100123, 5);
                               CREATE TABLE messages (message_id INTEGER, status TEXT);
                               INSERT INTO messages VALUES (1, 'done'), (2, 'done'),
                                                           (3, 'processing'), (4, 'pending'), (5, 'pending');""")
            db.close()
            original = legacy.read_bytes()
            path = Path(directory) / 'cursor.json'
            inbox = MessageQueue(path, -100123, legacy_path=legacy)
            self.assertEqual(inbox.cursor(), 3)
            self.assertFalse(inbox.capture(-100123, 3, ['https://fkrt.co/already-claimed']))
            inbox.capture(-100123, 4, ['https://fkrt.co/pending'])
            inbox.claim()
            inbox.finish(4, [])
            inbox.close()
            reopened = MessageQueue(path, -100123, legacy_path=legacy)
            self.assertEqual(reopened.cursor(), 4)
            self.assertEqual(legacy.read_bytes(), original)
            with self.assertRaises(ValueError):
                MessageQueue(Path(directory) / 'wrong.json', -100999, legacy_path=legacy)

    def test_capture_applies_backpressure_and_never_loses_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = MessageQueue(Path(directory) / 'cursor.json', -100123, max_pending=2)
            observed = []
            processor = Mock()
            processor.process.side_effect = lambda group, message, links: observed.append(message)

            class Client:
                def iter_messages(self, entity, **kwargs):
                    async def messages():
                        for number in range(1, 11):
                            yield SimpleNamespace(chat_id=-100123, id=number,
                                                  message='https://fkrt.co/test', entities=[], reply_markup=None)
                    return messages()

            async def drain():
                wake = asyncio.Event()
                worker = asyncio.create_task(process_inbox(inbox, processor, wake))
                try:
                    count = await asyncio.wait_for(
                        capture_new_messages(Client(), object(), -100123, inbox, wake, worker), 5)
                    self.assertEqual(count, 10)
                    while inbox.remaining():
                        await asyncio.sleep(.01)
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
            asyncio.run(drain())
            self.assertEqual(observed, list(range(1, 11)))
            self.assertEqual(inbox.remaining(), 0)

    def test_monitor_lock_is_shared_across_processes_and_released(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new-data" / "monitor.lock"
            code = ("from monitor_lock import MonitorLock; "
                    "lock = MonitorLock(__import__('sys').argv[1]); "
                    "lock.__enter__(); lock.__exit__()")
            command = [sys.executable, "-c", code, str(path)]
            with MonitorLock(path):
                child = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                       capture_output=True, text=True, timeout=10)
                self.assertNotEqual(child.returncode, 0)
                self.assertIn("already running", child.stderr)
            child = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)

    def test_a_second_monitor_cannot_open_the_same_inbox(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'monitor.lock'
            with MonitorLock(path):
                with self.assertRaises(ValueError):
                    with MonitorLock(path):
                        self.fail('A second monitor acquired the lock')
            with MonitorLock(path):
                pass


if __name__ == '__main__':
    unittest.main()
