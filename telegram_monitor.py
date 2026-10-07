"""Deliver deal graphs with a bot; read a chosen group with a user session."""

import argparse
import asyncio
import hashlib
import getpass
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv

from deal_report import prepare_report, save_report
from price_history import PriceHistoryError
from message_queue import MessageQueue
from settings import read_settings
from monitor_lock import MonitorLock

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("REAL_DISCOUNT_DATA_DIR") or ROOT)
STATE_PATH = DATA_DIR / "results" / "telegram-state.json"
GROUP_BINDING_PATH = DATA_DIR / ".telegram-group-binding.json"
READ_STATE_PATH = DATA_DIR / "results" / "telegram-read-state.json"
INBOX_PATH = DATA_DIR / "results" / "telegram-inbox.sqlite3"
AUTHORIZED_SOURCE = "https://t.me/+RMtFLhPbWG9Hn0MQ"
AUTHORIZED_GROUP_NAME = "DealAlerts🔔 Loot Deals"


def authorized_source(value):
    sources = [item.strip() for item in (value or "").split(",") if item.strip()]
    if sources != [AUTHORIZED_SOURCE]:
        raise ValueError("The user session is restricted to the approved DealAlerts group invite only.")
    return sources[0]


def verify_group(title, chat_id, binding=None):
    normalize = lambda text: " ".join(unicodedata.normalize("NFC", text).split())
    if normalize(title) != normalize(AUTHORIZED_GROUP_NAME):
        raise ValueError(f"Invite resolves to {title!r}, which is not the approved group. No messages were read.")
    if binding and binding.get("chat_id") != chat_id:
        raise ValueError("The invite resolved to a different group ID than the previously approved one. No messages were read.")
    return {"chat_id": chat_id, "title": title, "source": AUTHORIZED_SOURCE}


def load_config():
    load_dotenv(ROOT / ".env")


def redact(value):
    value = str(value)
    for key in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_API_HASH", "TELEGRAM_PHONE"]:
        secret = os.environ.get(key)
        if secret:
            value = value.replace(secret, "<redacted>")
    return value


class TelegramBot:
    def __init__(self):
        load_config()
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ValueError("Set TELEGRAM_BOT_TOKEN in .env.")
        self.base = f"https://api.telegram.org/bot{token}/"
        self.session = requests.Session()

    def call(self, method, data=None, files=None):
        try:
            response = self.session.post(self.base + method, data=data or {}, files=files, timeout=40)
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ValueError(f"Telegram {method} failed: {redact(exc)}") from None
        if not result.get("ok"):
            raise ValueError(f"Telegram {method}: {redact(result.get('description', 'request failed'))}")
        return result["result"]

    def discover(self):
        me = self.call("getMe")
        webhook = self.call("getWebhookInfo")
        info = {"bot_username": me.get("username"),
                "group_privacy_disabled": me.get("can_read_all_group_messages", False),
                "webhook_active": bool(webhook.get("url")), "chats": []}
        if not info["webhook_active"]:
            # No offset: inspect pending updates without acknowledging them.
            updates = self.call("getUpdates", {"timeout": 0})
            chats = {}
            for update in updates:
                for name in ["message", "channel_post", "my_chat_member"]:
                    chat = update.get(name, {}).get("chat")
                    if chat:
                        chats[chat["id"]] = {key: chat.get(key) for key in
                                             ["id", "type", "title", "username", "first_name"]}
            info["chats"] = list(chats.values())
        return info

    def send_report(self, recipient, graph, caption):
        with Path(graph).open("rb") as photo:
            sent = self.call("sendPhoto", {"chat_id": recipient, "caption": caption[:1024]},
                             {"photo": (Path(graph).name, photo, "image/png")})
        if len(caption) > 1024:
            time.sleep(1)
            self.call("sendMessage", {"chat_id": recipient, "text": caption[:4096],
                                      "link_preview_options": json.dumps({"is_disabled": True})})
        return sent


def extract_links(text, entities=(), button_urls=()):
    """Include plain links, hidden hyperlinks, media captions and URL buttons."""
    links = re.findall(r"https?://[^\s<>\"']+", text or "", re.I)
    links += [entity.get("url") if isinstance(entity, dict) else getattr(entity, "url", None)
              for entity in entities or ()]
    links += list(button_urls)
    allowed = {"amzn-to.co", "amzn.to", "amzn.urlgeni.us", "flipkart.com", "www.flipkart.com",
               "dl.flipkart.com", "m.flipkart.com", "fkrt.co", "www.fkrt.co"}
    result = []
    for link in links:
        if not link:
            continue
        link = link.rstrip(".,;!)]}")
        try:
            host = (urlsplit(link).hostname or "").lower()
        except ValueError:
            continue
        if host in allowed or re.fullmatch(r"(?:[\w-]+\.)?amazon\.(?:in|com)", host):
            if link not in result:
                result.append(link)
    return result


class DeliveryProcessor:
    def __init__(self, dry_run=False):
        load_config()
        self.recipient = os.environ.get("TELEGRAM_CHAT_ID")
        if not self.recipient and not dry_run:
            raise ValueError("Set TELEGRAM_CHAT_ID in .env after pressing Start on your bot.")
        self.bot = None if dry_run else TelegramBot()
        self.state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {"sent": []}

    def process(self, chat_id, message_id, links):
        outcomes = []
        try:
            settings = read_settings()
        except ValueError as exc:
            print(f"Settings error: {exc}", file=sys.stderr, flush=True)
            return [{"status": "settings_error", "error": str(exc)}]
        for link in links:
            key = hashlib.sha256(f"{chat_id}:{message_id}:{link}".encode()).hexdigest()
            if key in self.state["sent"] and self.bot:
                outcomes.append({"status": "already_sent"})
                continue
            try:
                result = prepare_report(link, settings.months, settings.threshold)
                if result["six_month_analysis"]["median_rule_verdict"] != "BUY":
                    print(f"Skipped {result.get('product_id', 'product')}: {result['six_month_analysis']['drop_percent']:.2f}% is not above {settings.threshold:g}%.", flush=True)
                    outcomes.append({"status": "below_threshold", "drop_percent": result["six_month_analysis"]["drop_percent"]})
                    continue
                _, graph, caption = save_report(result, DATA_DIR / "results" / "alerts" / key)
                if self.bot:
                    self.bot.send_report(self.recipient, graph, caption)
                    self.state["sent"] = (self.state["sent"] + [key])[-2000:]
                    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    temporary = STATE_PATH.with_suffix(".tmp")
                    temporary.write_text(json.dumps(self.state), encoding="utf-8")
                    temporary.replace(STATE_PATH)
                print(f"{'Sent' if self.bot else 'Prepared'} report for {graph.stem}", flush=True)
                outcomes.append({"status": "sent" if self.bot else "dry_run", "product_id": result.get("product_id")})
            except (requests.RequestException, PriceHistoryError, ValueError, OSError) as exc:
                print(f"Product processing failed: {redact(exc)}", file=sys.stderr, flush=True)
                outcomes.append({"status": "failed", "code": getattr(exc, "code", "processing_error"), "error": redact(exc)[:1000]})
        return outcomes


def message_links(message):
    buttons = [button.url for row in getattr(getattr(message, "reply_markup", None), "rows", [])
               for button in row.buttons if getattr(button, "url", None)]
    return extract_links(message.message, message.entities, buttons)


async def capture_new_messages(client, entity, chat_id, inbox, wake):
    count = 0
    # limit=None lets Telethon paginate every new message, not just one or 100.
    async for message in client.iter_messages(entity, min_id=inbox.cursor(), reverse=True, limit=None):
        if message.chat_id != chat_id:
            raise ValueError("Refusing to inspect a message outside the approved group.")
        if inbox.capture(chat_id, message.id, message_links(message)):
            count += 1
            wake.set()
    return count


async def process_inbox(inbox, processor, wake):
    while True:
        job = inbox.claim()
        if job is None:
            wake.clear()
            await wake.wait()
            continue
        try:
            outcomes = await asyncio.to_thread(processor.process, job["group_id"], job["message_id"], job["links"])
        except Exception as exc:
            outcomes = [{"status": "failed", "error": redact(exc)[:1000]}]
            print(f"Message processing error: {redact(exc)}", file=sys.stderr, flush=True)
        inbox.finish(job["message_id"], outcomes)


async def read_group(args):
    try:
        from telethon import TelegramClient, utils, errors
        from telethon.tl.functions.messages import CheckChatInviteRequest
        from telethon.tl.types import ChatInviteAlready
    except ImportError:
        raise ValueError("Install the group-reader dependencies: pip install -r requirements-telegram.txt") from None
    load_config()
    read_settings()  # Validate policy and numeric settings before logging in.
    api_id, api_hash = os.environ.get("TELEGRAM_API_ID"), os.environ.get("TELEGRAM_API_HASH")
    phone = os.environ.get("TELEGRAM_PHONE")
    source = authorized_source(os.environ.get("TELEGRAM_SOURCE_CHATS"))
    if not api_id or not api_hash or not phone:
        raise ValueError("Set TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE and TELEGRAM_SOURCE_CHATS in .env.")
    session_name = os.environ.get("TELEGRAM_USER_SESSION") or "telegram-user"
    session_path = Path(session_name)
    if not session_path.is_absolute():
        session_path = DATA_DIR / session_path
    # No account-wide update subscription; only targeted GetHistory requests.
    client = TelegramClient(str(session_path), int(api_id), api_hash,
                            receive_updates=False, catch_up=False)
    try:
        # Authentication is local; the code and 2FA password are never echoed.
        await client.start(phone=phone,
                           code_callback=lambda: getpass.getpass("Telegram login code (hidden): "),
                           password=lambda: getpass.getpass("Telegram 2FA password (hidden): "))
        invite = re.fullmatch(r"https://t\.me/\+([A-Za-z0-9_-]+)", source)
        membership = await client(CheckChatInviteRequest(invite.group(1)))
        if not isinstance(membership, ChatInviteAlready):
            raise ValueError("Your signed-in account must already be a member of the approved group.")
        entity = membership.chat
        chat_id = utils.get_peer_id(entity)
        previous_binding = json.loads(GROUP_BINDING_PATH.read_text(encoding="utf-8")) if GROUP_BINDING_PATH.exists() else None
        binding = verify_group(entity.title, chat_id, previous_binding)
        GROUP_BINDING_PATH.write_text(json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Verified approved group: {entity.title} (ID {chat_id}).", flush=True)
        processor = DeliveryProcessor(args.dry_run)
        state = json.loads(READ_STATE_PATH.read_text(encoding="utf-8")) if READ_STATE_PATH.exists() else {}
        if state and state.get("chat_id") != chat_id:
            raise ValueError("Stored reading cursor belongs to a different group.")
        last_id = state.get("last_message_id")
        if last_id is None:
            newest = await client.get_messages(entity, limit=1)
            last_id = newest[0].id if newest else 0
        inbox_path = INBOX_PATH if not args.dry_run else DATA_DIR / "results" / "telegram-inbox-dryrun.sqlite3"
        inbox = MessageQueue(inbox_path, chat_id, last_id)
        wake = asyncio.Event()
        worker = asyncio.create_task(process_inbox(inbox, processor, wake))
        print(f"Group-only polling: {entity.title}. Persistent inbox; each message claimed once. Alerts only above the .env threshold.", flush=True)
        try:
            while True:
                try:
                    count = await capture_new_messages(client, entity, chat_id, inbox, wake)
                    if count:
                        print(f"Queued {count} new message(s). Cursor: {inbox.cursor()}.", flush=True)
                    if args.once:
                        while inbox.remaining():
                            await asyncio.sleep(.2)
                        return
                    interval = args.poll_seconds if args.poll_seconds is not None else read_settings().poll_seconds
                    await asyncio.sleep(interval)
                except errors.FloodWaitError as exc:
                    print(f"Telegram rate limit: waiting {exc.seconds} seconds.", file=sys.stderr, flush=True)
                    await asyncio.sleep(exc.seconds)
                except (ConnectionError, OSError, errors.RPCError, ValueError) as exc:
                    print(f"Polling error: {redact(exc)}; retrying in 5 seconds.", file=sys.stderr, flush=True)
                    await asyncio.sleep(5)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            inbox.close()
    finally:
        await client.disconnect()


def main():
    # Windows redirected output otherwise defaults to a legacy code page,
    # which cannot print the approved group's bell emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("discover", help="Check bot identity and available delivery chat IDs")
    watch = subparsers.add_parser("watch", help="Read only configured groups through your Telegram user session")
    watch.add_argument("--once", action="store_true", help="Drain all new messages since the durable cursor and exit")
    watch.add_argument("--dry-run", action="store_true", help="Generate reports without sending them")
    watch.add_argument("--poll-seconds", type=float, default=None,
                       help="Override .env polling interval (minimum 1)")
    args = parser.parse_args()
    try:
        if args.command == "discover":
            print(json.dumps(TelegramBot().discover(), ensure_ascii=True, indent=2))
        else:
            if args.poll_seconds is not None and args.poll_seconds < 1:
                raise ValueError("Polling interval must be at least 1 second.")
            with MonitorLock(DATA_DIR / ".telegram-monitor.lock"):
                asyncio.run(read_group(args))
    except KeyboardInterrupt:
        print("Stopped.")
    except Exception as exc:
        parser.exit(1, f"Error: {redact(exc)}\n")


if __name__ == "__main__":
    main()
