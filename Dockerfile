FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8787 \
    STATE_FILE=/data/bot.sqlite3
WORKDIR /app
RUN install -d -o 10001 -g 10001 /data
COPY --chown=10001:10001 bot.py /app/bot.py
USER 10001:10001
EXPOSE 8787
STOPSIGNAL SIGINT
CMD ["python", "bot.py"]

FROM base AS test
COPY --chown=10001:10001 test_bot.py /app/test_bot.py
CMD ["python", "-W", "error::ResourceWarning", "-m", "unittest", "-v"]

FROM base AS runtime
