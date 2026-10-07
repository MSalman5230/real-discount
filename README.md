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

## Docker

GitHub Actions builds and tests a Linux/amd64 image on every push to
`master`, then publishes it to GitHub Container Registry:

```text
ghcr.io/msalman5230/real-discount:latest
ghcr.io/msalman5230/real-discount:master
ghcr.io/msalman5230/real-discount:sha-<commit>
```

Pull requests targeting `master` build and test without publishing.
Publishing uses the
repository's automatic `GITHUB_TOKEN` with `packages: write`; no Docker Hub
account or additional repository secret is needed.
See [GitHub's container publishing documentation](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images).

The Dockerfile and `compose.yaml` are in the repository root; the workflow
is `.github/workflows/docker.yml`. Credentials, Telegram sessions and local
results are excluded from the Docker build context.

### Directory mappings

| Host path (beside compose.yaml) | Container path | Purpose |
| --- | --- | --- |
| `./.env` | `/app/.env` (read only) | Telegram credentials and settings; numeric settings reload while running. |
| `./data` | `/data` (read/write) | Telegram session, group binding, monitor lock, inbox, reading/delivery state, graphs and Matplotlib cache. |

To store data in another directory, change the `source: ./data` value in
`compose.yaml` to that directory's absolute path. Keep its target as `/data`.
Use a local filesystem that supports SQLite and file locks, and keep one
monitor running per session/data directory. No port mapping is needed.

### First run

Install Docker with Linux containers and Docker Compose. Create `.env`
from `.env.example`, then fill the Telegram values described above.
Keep `TELEGRAM_USER_SESSION=telegram-user`; relative session paths resolve
inside `/data` in Docker.

Create the host data directory. On Windows, use PowerShell:

```powershell
New-Item -ItemType Directory -Force data
```

On Linux, allow the container's user (UID/GID 10001) to write to it:

```sh
mkdir -p data
sudo chown -R 10001:10001 data
```

Pull the published image:

```sh
docker compose pull
```

If the GitHub package is private, first run `docker login ghcr.io -u YOUR_GITHUB_USERNAME`
and use a GitHub personal access token with `read:packages` as the password.
Alternatively, build locally with `docker compose build`.

For a new Telegram session, run this interactively and enter the Telegram
login code and 2FA password when prompted. It saves the session, verifies
the approved group and processes any new queued messages once, then exits:

```sh
docker compose run --rm real-discount watch --once
```

Start the monitor in the background after the login succeeds:

```sh
docker compose up -d
docker compose logs -f
```

Stop it with `docker compose down`. The host `data` directory survives
container replacement. To update later, run `docker compose pull` followed
by `docker compose up -d`.

### Move an existing local session to Docker

Stop the local monitor before copying its files. Copy `telegram-user.session`
(or the session named in your `.env`), `.telegram-group-binding.json`, and the
entire `results` directory into `data`, preserving their names. For example,
`results/telegram-inbox.sqlite3` becomes `data/results/telegram-inbox.sqlite3`.
Include any SQLite journal/WAL files and session sidecar files if present.
Keep `.env` beside `compose.yaml`, then apply the Linux ownership command
above if needed. This preserves the login and processed-message state.
Do not run the local and Docker monitors simultaneously.

Without Docker, paths continue to use the repository directory by default.
To choose another data directory, set `REAL_DISCOUNT_DATA_DIR` in the process
environment before starting the monitor.

### Verify the image locally

```sh
docker build --target test -t real-discount:test .
docker compose build
docker compose run --rm real-discount --help
```
