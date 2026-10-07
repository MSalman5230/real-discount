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

COPY --chown=app:app deal_report.py docker_entrypoint.py message_queue.py monitor_lock.py price_history.py settings.py telegram_monitor.py ./
COPY --chown=app:app pipelines/ ./pipelines/

USER app

FROM base AS test
COPY --chown=app:app tests/ ./tests/
USER root
RUN python -m unittest discover -s tests -p test_docker_entrypoint.py -v
USER app
RUN python -m unittest discover -s tests -v

FROM base AS runtime
# The entrypoint repairs bind-mounted state, then drops to UID/GID 10001.
USER root
VOLUME ["/data"]
STOPSIGNAL SIGINT
ENTRYPOINT ["python", "docker_entrypoint.py"]
CMD ["watch"]
