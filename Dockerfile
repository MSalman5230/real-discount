FROM python:3.11-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REAL_DISCOUNT_DATA_DIR=/data \
    MPLCONFIGDIR=/tmp/matplotlib \
    MPLBACKEND=Agg

WORKDIR /app

COPY requirements.txt requirements-telegram.txt ./
RUN python -m pip install --no-cache-dir -r requirements-telegram.txt

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /data \
    && chown app:app /data

COPY --chown=app:app deal_report.py message_queue.py monitor_lock.py price_history.py settings.py telegram_monitor.py ./
COPY --chown=app:app pipelines/ ./pipelines/

USER app

FROM base AS test
COPY --chown=app:app tests/ ./tests/
RUN python -m unittest discover -s tests -v

FROM base AS runtime
VOLUME ["/data"]
STOPSIGNAL SIGINT
ENTRYPOINT ["python", "telegram_monitor.py"]
CMD ["watch"]
