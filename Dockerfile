FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8787 \
    STATE_FILE=/data/bot.sqlite3
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN install -d -o 10001 -g 10001 /data
COPY --chown=10001:10001 bot.py pubsub.py backend.py mail_service.py imap_mail.py mail_auth.py phrases.py words_ru.txt /app/
USER 10001:10001
EXPOSE 8787
STOPSIGNAL SIGINT
CMD ["python", "backend.py"]

FROM base AS test
COPY --chown=10001:10001 test_*.py /app/
CMD ["python", "-W", "error::ResourceWarning", "-m", "unittest", "-v"]

FROM base AS runtime
