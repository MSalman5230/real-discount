import asyncio
import json
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
from pipelines.common import build_result, json_object
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
        processor.state = {'sent': []}
        result = {'product_id': 'PRODUCT', 'six_month_analysis': {'median_rule_verdict': 'WAIT', 'drop_percent': 20}}
        with patch('telegram_monitor.read_settings', return_value=Settings()), \
             patch('telegram_monitor.prepare_report', return_value=result), \
             patch('telegram_monitor.save_report') as save:
            outcome = processor.process(-100123, 1, ['https://fkrt.co/test'])
        self.assertEqual(outcome[0]['status'], 'below_threshold')
        save.assert_not_called()
        processor.bot.send_report.assert_not_called()

    def test_invalid_link_does_not_stop_other_links_in_post(self):
        processor = DeliveryProcessor.__new__(DeliveryProcessor)
        processor.bot = None
        processor.state = {'sent': []}
        good = {'product_id': 'PRODUCT', 'six_month_analysis': {'median_rule_verdict': 'WAIT', 'drop_percent': 10}}
        with patch('telegram_monitor.read_settings', return_value=Settings()), \
             patch('telegram_monitor.prepare_report', side_effect=[PriceHistoryError('No data', 'no_history'), good]):
            results = processor.process(-100123, 1, ['https://fkrt.co/bad', 'https://fkrt.co/good'])
        self.assertEqual([r['status'] for r in results], ['failed', 'below_threshold'])

    def test_qualifying_message_uses_configured_threshold_and_sends_graph(self):
        processor = DeliveryProcessor.__new__(DeliveryProcessor)
        processor.bot = Mock()
        processor.recipient = 'private'
        processor.state = {'sent': []}
        result = {'product_id': 'PRODUCT', 'six_month_analysis': {'median_rule_verdict': 'BUY', 'drop_percent': 26}}
        url = 'https://fkrt.co/good'
        with tempfile.TemporaryDirectory() as directory, \
             patch('telegram_monitor.STATE_PATH', Path(directory) / 'state.json'), \
             patch('telegram_monitor.read_settings', return_value=Settings(threshold=25)), \
             patch('telegram_monitor.prepare_report', return_value=result) as prepare, \
             patch('telegram_monitor.save_report', return_value=(result, Path('graph.png'), 'caption')):
            outcome = processor.process(-100123, 1, [url])
            prepare.assert_called_once_with(url, 6, 25)
            processor.bot.send_report.assert_called_once_with('private', Path('graph.png'), 'caption')
            self.assertEqual(outcome[0]['status'], 'sent')
            self.assertEqual(len(json.loads((Path(directory) / 'state.json').read_text())['sent']), 1)


class InboxTests(unittest.TestCase):
    def test_every_message_in_a_large_poll_is_captured_once(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = MessageQueue(Path(directory) / 'inbox.sqlite3', -100123)

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
            reopened = MessageQueue(Path(directory) / 'inbox.sqlite3', -100123)
            self.assertFalse(reopened.capture(-100123, 1, ['https://fkrt.co/example']))
            self.assertIsNone(reopened.claim())
            reopened.close()

    def test_claimed_message_is_not_replayed_after_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inbox.sqlite3'
            inbox = MessageQueue(path, -100123)
            inbox.capture(-100123, 1, ['https://fkrt.co/example'])
            self.assertIsNotNone(inbox.claim())
            inbox.close()
            reopened = MessageQueue(path, -100123)
            self.assertIsNone(reopened.claim())
            reopened.close()

    def test_pending_messages_survive_restart_and_wrong_group_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inbox.sqlite3'
            inbox = MessageQueue(path, -100123)
            with self.assertRaises(ValueError):
                inbox.capture(-100999, 1, ['https://fkrt.co/example'])
            inbox.capture(-100123, 1, ['https://fkrt.co/example'])
            inbox.close()
            reopened = MessageQueue(path, -100123)
            self.assertEqual(reopened.claim()['message_id'], 1)
            reopened.close()

    def test_worker_processes_every_message_even_after_an_unexpected_error(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = MessageQueue(Path(directory) / 'inbox.sqlite3', -100123)
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
