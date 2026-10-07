# Price history without a browser

Fetch all price records available on PriceHistory.app with Python `requests`
and `beautifulsoup4`. No JavaScript, Crawl4AI, login, or user API key is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip --isolated --disable-pip-version-check install --no-cache-dir -r requirements.txt
.\.venv\Scripts\python.exe price_history.py "https://amzn-to.co/xnXqN2" --output results\history.json --csv results\history.csv
```

Amazon India and Flipkart product and affiliate links are supported. Replace
the link to fetch another product. Without `--output`, the complete JSON is
printed to stdout. `--csv` exports listing-price and offer-price series.

The separate adapters in `pipelines/amazon.py` and `pipelines/flipkart.py`
resolve redirects and normalize each store's response. The Flipkart adapter
preserves the product's `pid` so a variant is not lost. Shared transport calls
`POST /api/search`, reads the returned product
page with BeautifulSoup, extracts that page's public `page` and `token` headers,
and calls `POST /api/price/{page}`. That last endpoint returns the full history
without a date filter; the website's All/1Y buttons change the chart's visible
range rather than download older records.

The source is PriceHistory.app's undocumented API and may change. "All" means
all records the tracker has collected, which may start after the product's
launch. Prices and coupon eligibility can differ by seller, location, and date.
The JSON preserves the source numbers and timestamps, including any precision
difference between offer-price chart points and the source's offer statistics.

## Graph and buying report

```powershell
.\.venv\Scripts\python.exe deal_report.py "https://amzn-to.co/xnXqN2"
```

This creates a PNG graph, report JSON, and Telegram message text in `results`.
It extracts the selected Yes/Okay/Wait/Skip label and supporting prediction
text from the site's "Should you buy at this price?" section. This is reported
separately from your rule: BUY when the latest tracked price is strictly more
than the configured threshold below the six-month time-weighted median,
otherwise WAIT. The default threshold is 20%; exactly 20% does not alert.
If the website has no assessment widget (as with the sample Flipkart product),
its verdict is reported as unavailable.

The graph uses six calendar months ending at the current India time. Each
observation lasts until the next one, with no gap penalty or discarded gaps.
The last price before the cutoff sets the starting price; the final recorded
price lasts until the report time. If no observation predates the cutoff,
the series starts at the first available observation. The report preserves
the website's complete explanation in `pricehistory_assessment`.

## Telegram delivery

Keep your existing bot token in `.env`. Open your bot's private chat and press
Start, then find the private chat ID:

```powershell
.\.venv\Scripts\python.exe telegram_monitor.py discover
```

Set `TELEGRAM_CHAT_ID` in `.env`, then send a graph and its report:

```powershell
.\.venv\Scripts\python.exe deal_report.py "https://amzn-to.co/xnXqN2" --send
```

`--send` delivers only a qualifying BUY report. A below-threshold report is
still saved locally by this manual command, with no Telegram alert.

## Read a group you have joined

An invite link alone does not let a bot join a group. For a group where you
cannot add your bot, this project reads new messages through your own Telegram
account and uses the bot only to send reports to your private chat.

Get an API ID and hash from [my.telegram.org](https://my.telegram.org) under
API development tools. Fill these additional `.env` entries (see `.env.example`):

```dotenv
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=
TELEGRAM_SOURCE_CHATS=
```

This reader is restricted to the approved invite for **DealAlerts🔔 Loot Deals**.
`TELEGRAM_SOURCE_CHATS` must contain that exact invite, with no additional
sources. Before fetching any messages, the reader verifies the group's name
and pins its numeric chat ID. Future runs refuse an invite resolving to a
different group ID. Your signed-in account must already be a member; the
reader never joins new groups or lists your other chats.

Account-wide Telegram updates are disabled (`receive_updates=False` and
`catch_up=False`). The user session makes targeted history requests for the
approved group only, rather than receiving messages from all account chats
and filtering them afterward. Authentication and verification of the supplied
group invite are the only other user API operations. Reports are sent using
the separate bot credentials, not your user account.

```powershell
.\.venv\Scripts\python.exe -m pip --isolated --disable-pip-version-check install --no-cache-dir wheel
.\.venv\Scripts\python.exe -m pip --isolated --disable-pip-version-check install --no-cache-dir --no-build-isolation -r requirements-telegram.txt
.\.venv\Scripts\python.exe telegram_monitor.py watch --once --dry-run
.\.venv\Scripts\python.exe telegram_monitor.py watch
```

The first run prompts locally, with hidden input, for a Telegram login code
and, if enabled, the account's 2FA password. Later runs reuse the local `.session` file. `.env` and
session files are excluded from Git. The reader handles plain product links,
hidden hyperlinks, photo captions and URL buttons. Multiple links within a
post are processed separately.

Configure the reader in `.env`:

```dotenv
HISTORY_MONTHS=6
DISCOUNT_THRESHOLD_PERCENT=20
TELEGRAM_POLL_SECONDS=3
TELEGRAM_DETECTION_MODE=poll
TELEGRAM_ALLOW_ACCOUNT_UPDATES=false
```

The threshold is reloaded for each new post and the polling interval for each
poll, so these settings can be changed while the reader runs. Polling defaults
to every 3 seconds; the minimum is 1 second. `--poll-seconds` overrides the
environment setting. Telegram rate-limit waits are honored.

Each poll requests every message newer than the durable cursor, paging through
as many results as needed. Every message is saved in the SQLite inbox at
`results/telegram-inbox.sqlite3`, keyed by group ID and message ID. A separate
worker processes the inbox while polling continues, so a slow product lookup
does not block capturing new posts. Posts without supported links also advance
the cursor. On the first run with no saved cursor, the reader starts from the
group's latest message and watches subsequent posts.

A message is claimed once before processing. Completed, failed and previously
claimed messages are never replayed; pending messages survive a restart.
This prevents duplicate processing even if a poll returns the same messages.
A process crash during a claimed message can interrupt its report: it is
marked interrupted on restart and is not retried. This is at-most-once
processing, rather than a guarantee of delivery through a crash. Only one
monitor instance may hold the inbox lock.

The worker checks the threshold before creating a graph or sending anything.
Qualifying reports include the graph, time-weighted median, percentage drop,
and available website assessment. Invalid links, missing history and API or
network failures are recorded locally without a Telegram alert; processing
continues with other links and messages. Changes to the threshold apply to
newly processed posts and do not replay old posts.

`watch --once` drains all new messages since the saved cursor and any pending
work, then exits. `--dry-run` uses a separate inbox and creates qualifying
reports locally without sending. `watch` stays running until stopped. A hidden
background instance can be launched with PowerShell; no Windows service or
scheduled task is installed, so it must be restarted after a computer reboot.

Runtime logs are in `results/telegram-monitor.log` and
`results/telegram-monitor-errors.log`; the current background process ID is
in `results/telegram-monitor.pid`.

Validate calculations and extraction:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
