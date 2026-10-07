# Real Discount

Polls only **DealAlerts🔔 Loot Deals** for new Amazon and Flipkart links.
Uses PriceHistory.app's six-month time-weighted median to identify discounts.
Sends you a Telegram graph, percentage drop and available buying assessment
only when the price is **more than 20% below the median** (configurable).
Messages are processed once, including across restarts.

## Run

Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --isolated wheel
.\.venv\Scripts\python.exe -m pip install --isolated --no-build-isolation -r requirements-telegram.txt
```

Create `.env` using `.env.example`. Fill these required values:

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=
TELEGRAM_SOURCE_CHATS=https://t.me/+RMtFLhPbWG9Hn0MQ
```

Get the Telegram values:

1. **API ID, API hash and phone:** sign in at [my.telegram.org](https://my.telegram.org/auth) using your phone number with country code (for example, `+91...`). The confirmation code arrives in Telegram. Open **API development tools**, fill the application form (for example, title `Real Discount`, short name `realdiscount`), and select **Create application**. Copy `api_id` into `TELEGRAM_API_ID` and `api_hash` into `TELEGRAM_API_HASH`; use your account's phone number for `TELEGRAM_PHONE`. If you already created an application, reuse its credentials. [Telegram instructions](https://core.telegram.org/api/obtaining_api_id).
2. **Bot token:** open [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts, and put its token in `TELEGRAM_BOT_TOKEN`. [Telegram guide](https://core.telegram.org/bots/tutorial#obtain-your-bot-token).
3. **Private chat ID:** open your new bot and press **Start**. Run the command below and copy your private chat's numeric `id` from `chats` into `TELEGRAM_CHAT_ID`:

```powershell
.\.venv\Scripts\python.exe telegram_monitor.py discover
```

Leave `TELEGRAM_SOURCE_CHATS` as shown; your account must already belong to
that group. These optional settings use the defaults below and reload automatically:

```dotenv
HISTORY_MONTHS=6
DISCOUNT_THRESHOLD_PERCENT=20
TELEGRAM_POLL_SECONDS=3
```

Start the reader:

```powershell
.\.venv\Scripts\python.exe telegram_monitor.py watch
```

Enter the login code and, if enabled, your Telegram 2FA password when prompted
on the first run. Keep the process running; stop with **Ctrl+C** and restart it
after a reboot.
